"""为 CC 生成完整的文本阅读包；不调用模型，不把分包当作语义章节。

python src/note_plan.py <video_id> [--out VIDEO_DIR] [--detail lean|compact|full] [--force]
生成 note_plan.summary.md、note_plan.json、note_packets/P*.md、note_review.template.json。
agent 优先读 summary，再按路径读包；完整 JSON 供本地审计。
note_review.json 由 agent 填写，本工具永不覆盖它。
"""

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile

import timecode


for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass


BASE_DIR = Path(__file__).resolve().parent.parent
CUES = {
    "example": re.compile(r"例如|比如|举例|例子|假设"),
    "derivation": re.compile(r"推导|证明|公式|因此|所以|等于|得出"),
    "operation": re.compile(r"首先|然后|接着|点击|运行|输入|打开|执行"),
    "visual": re.compile(r"这张|这幅|这个图|如图|看图|屏幕|横轴|纵轴|箭头|报错|运行结果"),
}


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _normalized_segments(transcript):
    if not isinstance(transcript, dict):
        raise ValueError("transcript 必须是 JSON 对象")
    segments = transcript.get("segments")
    if not isinstance(segments, list) or not segments:
        raise ValueError("transcript.segments 必须是非空数组")
    result = []
    previous_start = -1
    for i, segment in enumerate(segments, 1):
        if not isinstance(segment, dict) or not isinstance(segment.get("text"), str):
            raise ValueError(f"第 {i} 句缺少字符串 text")
        try:
            start = timecode.parse_seconds(segment.get("start"))
            end = timecode.parse_seconds(segment.get("end"))
        except ValueError as exc:
            raise ValueError(f"第 {i} 句时间戳无效: {exc}") from exc
        if end < start or start < previous_start:
            raise ValueError(f"第 {i} 句时间倒置或顺序倒置")
        previous_start = start
        result.append({"id": f"S{i:06d}", "start": start, "end": end,
                       "text": segment["text"]})
    return result


def _chapters(transcript, warnings):
    result = []
    chapters = transcript.get("chapters") or []
    if not isinstance(chapters, list):
        warnings.append("chapters 不是数组，分包仅按字幕顺序进行。")
        return result
    for i, chapter in enumerate(chapters):
        try:
            start = timecode.parse_seconds(chapter.get("start", chapter.get("start_time")))
            title = str(chapter.get("title") or "")
        except (ValueError, AttributeError):
            warnings.append(f"忽略时间戳无效的官方章节 {i + 1}。")
            continue
        result.append((start, i, title))
    return sorted(result)


def _chapter_at(seconds, chapters):
    current = (-1, "")
    for start, number, title in chapters:
        if start > seconds:
            break
        current = (number, title)
    return current


def _visual_catalog(sprite, warnings):
    """Normalize the complete index once, before selecting each packet's interval."""
    result = []
    if not sprite:
        return result
    if not isinstance(sprite, dict) or not isinstance(sprite.get("sheets", []), list):
        warnings.append("画面索引结构无效，候选画面需重新定位。")
        return result
    visible = {}
    for sheet in sprite.get("sheets", []):
        if not isinstance(sheet, dict) or not isinstance(sheet.get("cells", []), list):
            continue
        for cell in sheet.get("cells", []):
            if not isinstance(cell, dict):
                continue
            try:
                timestamp = timecode.parse_seconds(cell.get("time", cell.get("seconds")))
                since = timecode.parse_seconds(cell.get("since", timestamp))
                until = timecode.parse_seconds(cell.get("until", timestamp))
            except ValueError:
                continue
            entry = {"sheet": sheet.get("file", ""), "pos": cell.get("pos"),
                     "time": timecode.format_hms(timestamp), "since": since,
                     "until": until, "weak": bool(cell.get("weak", False)),
                     "merged": cell.get("merged", 0)}
            visible[timestamp] = entry
            result.append(entry)
    # schema 2 的完整目录含概览预算之外的候选，不能仅从 sheets 制作正文包。
    candidates = sprite.get("candidates")
    if isinstance(candidates, list) and candidates:
        result = []
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            try:
                timestamp = timecode.parse_seconds(candidate.get("time", candidate.get("seconds")))
                since = timecode.parse_seconds(candidate.get("since", timestamp))
                until = timecode.parse_seconds(candidate.get("until", timestamp))
            except ValueError:
                continue
            result.append({"id": candidate.get("id", ""),
                           "time": timecode.format_hms(timestamp), "since": since,
                           "until": until, "weak": bool(candidate.get("weak", False)),
                           "overview_visible": timestamp in visible,
                           "sheet": visible.get(timestamp, {}).get("sheet", ""),
                           "pos": visible.get(timestamp, {}).get("pos")})
    return result


def _select_visual_candidates(catalog, start, end, warnings):
    # Keep catalog order and inclusive overlap, including old/unsorted indexes.
    result = [dict(entry) for entry in catalog if entry["until"] >= start and entry["since"] <= end]
    if any(entry.get("overview_visible") is False for entry in result):
        warnings.append("部分候选未展示在概览；按需求局部细查，不能把概览当成完整视觉覆盖。")
    return result


def _visual_candidates(sprite, start, end, warnings):
    return _select_visual_candidates(_visual_catalog(sprite, warnings), start, end, warnings)


def build_plan(transcript, sprite=None, *, max_chars=6000, max_seconds=480,
               context_segments=2, source_sha256=None):
    """不删、不缩写、不重排字幕；每句仅归属一个包，边界上下文另行标记。"""
    if max_chars <= 0 or max_seconds <= 0 or context_segments < 0:
        raise ValueError("分包字符/时长须大于 0，上下文句数须非负")
    segments = _normalized_segments(transcript)
    warnings = []
    chapters = _chapters(transcript, warnings)
    try:
        duration = float(transcript.get("duration") or 0)
    except (TypeError, ValueError):
        raise ValueError("duration 必须是有限的非负数字") from None
    if not math.isfinite(duration) or duration < 0:
        raise ValueError("duration 必须是有限的非负数字")
    if not duration:
        duration = max(s["end"] for s in segments)
        warnings.append("缺少片长，暂取最晚字幕结束时刻；不能据此证明字幕完整。")
    elif max(s["end"] for s in segments) > duration:
        warnings.append("部分字幕超过片长；写作前核实时间轴。")
    groups = []
    first = 0
    size = 0
    chapter = _chapter_at(segments[0]["start"], chapters)
    for i, segment in enumerate(segments):
        next_chapter = _chapter_at(segment["start"], chapters)
        split = i > first and (
            size + len(segment["text"]) > max_chars
            or segment["end"] - segments[first]["start"] > max_seconds
            or next_chapter != chapter)
        if split:
            groups.append((first, i, chapter[1]))
            first, size, chapter = i, 0, next_chapter
        size += len(segment["text"])
        if len(segment["text"]) > max_chars:
            warnings.append(f'{segment["id"]} 单句超过分包上限，完整保留，未截断。')
    groups.append((first, len(segments), chapter[1]))
    packets = []
    catalog = _visual_catalog(sprite, warnings)
    for number, (first, stop, title) in enumerate(groups, 1):
        body = segments[first:stop]
        start, end = body[0]["start"], max(s["end"] for s in body)
        packets.append({
            "id": f"P{number:03d}", "path": f"note_packets/P{number:03d}.md",
            "start": timecode.format_hms(start), "end": timecode.format_hms(end),
            "source_segments": [s["id"] for s in body],
            "context_before": [s["id"] for s in segments[max(0, first - context_segments):first]],
            "context_after": [s["id"] for s in segments[stop:stop + context_segments]],
            "chapter_hint": title,
            "teaching_cues": [{"segment": s["id"], "kinds": kinds} for s in body
                              if (kinds := [kind for kind, regex in CUES.items()
                                            if regex.search(s["text"])])],
            "visual_candidates": _select_visual_candidates(catalog, start, end, warnings),
        })
    if not sprite:
        warnings.append("无画面索引：候选时间来自字幕时须进一步核图，不能声明已检查全片画面。")
    source_sha256 = source_sha256 or _digest(
        json.dumps(transcript, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    return {
        "schema_version": 1, "video_id": transcript.get("video_id", ""),
        "title": transcript.get("title", ""), "video_url": transcript.get("video_url", ""),
        "duration": duration, "source_sha256": source_sha256,
        "settings": {"max_chars": max_chars, "max_seconds": max_seconds,
                     "context_segments": context_segments},
        "segment_count": len(segments), "packets": packets,
        "segment_times": {s["id"]: {"start": s["start"], "end": s["end"]}
                          for s in segments},
        "warnings": list(dict.fromkeys(warnings)),
    }


def _render_full_packet(packet, segments):
    lookup = {s["id"]: s for s in segments}
    lines = [f'# {packet["id"]}: {packet["start"]}–{packet["end"]}', "",
             "这是阅读分包，不是最终章节。按原讲解顺序轻度整理，先读文字，按视觉需求核图。",
             "保留例子中的条件、每步推导、操作输入和结果；跨包的同一例子应接着写。",
             "线索是关键词提示，可能误报和漏报，不能据此删掉其余内容。", "",
             f'官方章节参考：{packet["chapter_hint"] or "无"}', ""]
    for heading, key in [("前文上下文（只读，不重复写入正文）", "context_before"),
                         ("本包完整字幕（须逐项记录 written/omitted 去向）", "source_segments"),
                         ("后文上下文（只读，续写由下一包负责）", "context_after")]:
        lines.extend([f"## {heading}", ""])
        for identifier in packet[key]:
            item = lookup[identifier]
            lines.extend([f'[{identifier} {timecode.format_hms(item["start"])}'
                          f'–{timecode.format_hms(item["end"])}]', item["text"], ""])
    lines.extend(["## 讲解线索（需自行核实）", ""])
    lines.extend(f'- {cue["segment"]}: {", ".join(cue["kinds"])}'
                 for cue in packet["teaching_cues"])
    candidates = "[\n" + ",\n".join(
        json.dumps(candidate, ensure_ascii=False, separators=(",", ":"))
        for candidate in packet["visual_candidates"]) + "\n]"
    lines.extend(["", "## 候选画面元数据（尚未核图）", "",
                  "先写用途和预期信息，再按需要读取候选所在索引；不要一次读取全片高清图。", "",
                  "```json", candidates,
                  "```", "", "## 本包交稿检查", "",
                  "- 定义、条件、原因、原例子、推导、操作和结果是否都得到讲解？",
                  "- 代词能否从前后文确定？不明确的名称/数值/代码须记入待核项，不猜事实。",
                  "- note_review.json 中逐句记录去向；省略重复口头内容时写明理由。",
                  "- visual_requests 记录 time/status/reason/section；pending 必须继续处理。",
                  "- visual_requests 为空时仍须记录判断依据，空清单不能证明画面无遗漏。", ""])
    return "\n".join(lines)


def _table_cell(value):
    """定位字段可以来自外部索引，不能破坏 Markdown 表格。"""
    return str(value).replace("|", "&#124;").replace("\r", " ").replace("\n", " ")


def _tsv_cell(value):
    return str(value).replace("\\", "\\\\").replace("\t", "\\t").replace("\r", "\\r").replace("\n", "\\n")


def render_packet(packet, segments, *, detail="compact"):
    """默认只给模型正文和定位必需字段；完整机器元数据留在 manifest。"""
    if detail not in ("lean", "compact", "full"):
        raise ValueError("detail 必须是 lean、compact 或 full")
    if detail == "full":
        return _render_full_packet(packet, segments)
    lookup = {s["id"]: s for s in segments}
    lines = [f'# {packet["id"]}: {packet["start"]}–{packet["end"]}', "",
             f'官方章节参考：{packet["chapter_hint"] or "无"}', ""]
    for heading, key in [("前文上下文（只读）", "context_before"),
                         ("本包完整字幕", "source_segments"),
                         ("后文上下文（只读）", "context_after")]:
        lines.extend([f"## {heading}", ""])
        for identifier in packet[key]:
            item = lookup[identifier]
            # 不 strip / 拼句 / 截断：原句文本，包括内嵌换行，逐字保留。
            lines.append(f'[{identifier}@{timecode.format_hms(item["start"])}] {item["text"]}')
        lines.append("")
    lines.extend(["## 候选画面（尚未核图）", ""])
    if detail == "lean":
        lines.extend([r"字段与 compact 相同，制表符分隔；\t/\r/\n/\\ 表示字段内的转义字符。",
                      "```tsv", "id\ttime\tsince\tuntil\tweak\toverview_visible\tsheet\tpos"])
    else:
        lines.extend(["| id | time | since | until | weak | overview_visible | sheet | pos |",
                      "|---|---|---|---|---|---|---|---|"])
    for candidate in packet["visual_candidates"]:
        time = candidate["time"]
        # 旧版索引没有 candidate id，用时间生成稳定定位键，不改变原 manifest。
        identifier = candidate.get("id") or "time_" + time.replace(":", "_")
        visible = candidate.get("overview_visible", bool(candidate.get("sheet")))
        fields = [identifier, time, timecode.format_hms(candidate["since"]),
                  timecode.format_hms(candidate["until"]),
                  int(bool(candidate.get("weak"))), int(bool(visible)),
                  candidate.get("sheet") or "-",
                  candidate["pos"] if candidate.get("pos") is not None else "-"]
        if detail == "lean":
            lines.append("\t".join(_tsv_cell(value) for value in fields))
        else:
            lines.append("| " + " | ".join(_table_cell(value) for value in fields) + " |")
    if detail == "lean":
        lines.append("```")
    if not packet["visual_candidates"]:
        lines.append("无候选；需要配图时按字幕时间进一步定位。")
    lines.append("")
    return "\n".join(lines)


def render_summary(plan):
    """agent 的轻量阅读入口；避免整读 manifest 后在包里重复读同样元数据。"""
    lines = ["# 笔记阅读包入口", "",
             "先按下表路径顺序读取文本包，再按包内候选定位必要画面。",
             "完整 note_plan.json 留给本地程序：保留句子归属、起止时间、关键词线索和完整候选元数据。",
             "分包控制单次上下文大小；边界上下文和审阅记录仍有成本，不能保证总 token 下降。", "",
             f'字幕 SHA256：`{plan["source_sha256"]}`',
             f'图册 SHA256：`{plan.get("sprite_sha256") or "无"}`', "",
             "| id | start | end | path | source_count | candidate_count | unseen_count |",
             "|---|---|---|---|---:|---:|---:|"]
    for packet in plan["packets"]:
        candidates = packet["visual_candidates"]
        unseen = sum(candidate.get("overview_visible") is False for candidate in candidates)
        lines.append(f'| {packet["id"]} | {packet["start"]} | {packet["end"]} | '
                     f'{packet["path"]} | {len(packet["source_segments"])} | '
                     f'{len(candidates)} | {unseen} |')
    lines.extend(["", "## 输入与覆盖提醒", ""])
    lines.extend(f"- {warning}" for warning in plan["warnings"])
    if not plan["warnings"]:
        lines.append("未发现分包输入异常；这不证明语义完整或没有漏图。")
    lines.extend(["", "## 阅读与交稿规则（适用于全部包）", "",
                  "- 默认紧凑包以 [句子ID@开始时间] 标记原句，原文完整保留；结束时间在 manifest 的 segment_times。",
                  "- source_segments 是本包负责的原句；前后文只用于理解，不重复写入。阅读包不等于最终章节。",
                  "- 沿讲解顺序保留例子的条件、推导步骤、操作输入与结果；同一例子跨包时继续写完。",
                  "- 关键词线索仅存在于 manifest，可能误报或漏报；须读全部原句，不按线索筛掉内容。",
                  "- 候选表 weak/overview_visible 的 1/0 表示是/否，sheet 与 pos 定位概览；- 表示未展示或不可定位。",
                  "- 完整候选的 merged 等附加字段留在 manifest；涉及步骤变化时按候选时间局部细查，不能只看代表图。",
                  "- 先记录视觉用途，再核相关索引与必要高清帧；候选表不是看过画面的证据。",
                  "- 每句记录 written/omitted 去向；同用途的连续句可记录范围，由本地工具展开核验，不让模型逐个枚举 ID。",
                  "- 不明确的名称、数值或代码记入待核项；visual_requests 记录 time/status/reason/section。",
                  "- 空视觉需求清单不能证明无遗漏；核图未完成保留 pending，并说明检查范围。",
                  "- 首次写作复制 note_review.template.json 后编辑 note_review.json；已有审阅记录时继续编辑。",
                  "- --detail full 可恢复完整包展示；来源和句子归属不因展示模式变化而改变。", ""])
    if plan.get("packet_detail") == "lean":
        lines.append("- 当前 lean 模式候选用 TSV，字段与 compact 一致，未展示候选仍完整保留。")
    return "\n".join(lines)


def review_template(plan):
    return {"schema_version": 1, "video_id": plan["video_id"],
            "source_sha256": plan["source_sha256"],
            "coverage": [{"packet_id": p["id"], "source_segments": p["source_segments"],
                          "status": "pending", "section": "", "reason": ""}
                         for p in plan["packets"]],
            "visual_requests": [], "visual_review": "pending",
            "visual_review_reason": "", "uncertainties": []}


def _atomic_text(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                         dir=path.parent, delete=False) as file:
            name = file.name
            file.write(text)
        os.replace(name, path)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)


def generate(directory, *, video_id=None, force=False, detail="compact", **settings):
    """输入变动需显式 --force；已填的 review 始终保留，SHA 用于判定过期。"""
    directory = Path(directory)
    if detail not in ("lean", "compact", "full"):
        raise ValueError("detail 必须是 lean、compact 或 full")
    source = (directory / "transcript.json").read_bytes()
    transcript = json.loads(source)
    if video_id and transcript.get("video_id") not in (None, "", video_id):
        raise ValueError("字幕 video_id 与命令参数不一致")
    sprite_path = directory / "sprite_index.json"
    sprite_bytes = sprite_path.read_bytes() if sprite_path.exists() else b""
    sprite = json.loads(sprite_bytes) if sprite_bytes else None
    plan = build_plan(transcript, sprite, source_sha256=_digest(source), **settings)
    if video_id:
        plan["video_id"] = video_id
    plan["sprite_sha256"] = _digest(sprite_bytes) if sprite_bytes else None
    plan["packet_detail"] = detail
    plan_path = directory / "note_plan.json"
    if plan_path.exists() and not force:
        old = json.loads(plan_path.read_text(encoding="utf-8"))
        if (old.get("source_sha256"), old.get("settings"), old.get("sprite_sha256")) != (
                plan["source_sha256"], plan["settings"], plan["sprite_sha256"]):
            raise ValueError("输入或分包设置已变动；确认后加 --force 重建，旧 note_review.json 会保留并需重新审核")
    segments = _normalized_segments(transcript)
    # 先完成全部内存渲染，再逐文件原子替换；agent 按 manifest 读取，不按目录通配。
    packets = [(directory / p["path"], render_packet(p, segments, detail=detail))
               for p in plan["packets"]]
    for path, content in packets:
        _atomic_text(path, content)
    _atomic_text(directory / "note_review.template.json",
                 json.dumps(review_template(plan), ensure_ascii=False, indent=2) + "\n")
    _atomic_text(directory / "note_plan.summary.md", render_summary(plan))
    _atomic_text(plan_path, json.dumps(plan, ensure_ascii=False, indent=2) + "\n")
    return plan


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video_id")
    parser.add_argument("--out", type=Path, help="字幕所在 VIDEO_DIR，默认 output/<video_id>")
    parser.add_argument("--max-chars", type=int, default=6000, help="单包输入字符预算，不是成品字数")
    parser.add_argument("--max-seconds", type=int, default=480)
    parser.add_argument("--context-segments", type=int, default=2)
    parser.add_argument("--detail", choices=("lean", "compact", "full"), default="compact",
                        help="阅读包展示密度；完整原句和机器 manifest 始终保留")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    if not args.out and (not re.fullmatch(r"[A-Za-z0-9_-]+", args.video_id)
                         or args.video_id in (".", "..")):
        parser.error("video_id 含非法路径字符")
    directory = args.out or BASE_DIR / "output" / args.video_id
    try:
        plan = generate(directory, video_id=args.video_id, force=args.force,
                        detail=args.detail,
                        max_chars=args.max_chars, max_seconds=args.max_seconds,
                        context_segments=args.context_segments)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"note_plan: {exc}\n")
    print(f'note_plan: {len(plan["packets"])} packets, {plan["segment_count"]} segments -> {directory}')
    print("Read note_plan.summary.md first, then read packets in table order.")
    for warning in plan["warnings"]:
        print("WARNING: " + warning)
    print("Copy note_review.template.json to note_review.json only if no review exists; complete the review before delivery.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
