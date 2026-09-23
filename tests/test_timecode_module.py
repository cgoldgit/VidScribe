"""单一时间戳模块 src/timecode.py 的解析/格式化，以及"统一后行为不变"的对拍。

背景（R0-02 第 1、3 条）：时间戳解析此前在 6 个文件里各写了一份，判据互不相同。
统一成 timecode.parse_seconds / format_hms / format_mmss 之后，**行为必须不变**，
所以这个文件里最重的一组用例是**对拍**：把每处旧实现的写法逐字逐句抄成本文件里的
`_old_*` 冻结副本，跟新模块及仍保留的公共函数逐一对拍。

只有 3 类差异是**有意**放宽/收紧的（都是真实流水线碰不到的形态，见
`test_only_declared_differences`），其余输入必须逐字节相同。

离线、只用 tempfile；`output/` 只读（真实产物对拍那一条）。
"""
import json
import os
import re
import shutil
import sys
import tempfile
import unittest

from _helpers import SRC  # noqa: F401  (SRC 保证能 import src/)

import timecode
import asr_transcript as asr
import capture_frames as cf
import make_corrected as mc
import post_process as pp
import sprite_index as si
import transcript_quality as quality


# ─────────────────────────────────────────────
# 旧实现的冻结副本（逐字抄自改动前的代码，只用来对拍，不要改成新写法）
# ─────────────────────────────────────────────

def _old_to_sec(ts):
    """覆盖率计算在统一时间戳模块之前使用的解析写法。"""
    try:
        # int(float(x)) 容忍历史 transcript 里的 '01.000' 毫秒后缀
        h, m, s = (int(float(x)) for x in str(ts).split(":"))
        return h * 3600 + m * 60 + s
    except ValueError:
        return 0


def _old_timestamp_to_seconds(ts):
    """post_process.timestamp_to_seconds 改动前的写法。"""
    ts = ts.split('.')[0]  # 去掉毫秒
    parts = ts.split(':')
    if len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    elif len(parts) == 2:
        return int(parts[0]) * 60 + int(parts[1])
    return 0


def _old_fmt_ts(sec):
    """capture_frames.fmt_ts 改动前的写法（四舍五入）。"""
    s = int(round(sec))
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def _old_fmt_hms(sec):
    """sprite_index.fmt_hms 改动前的写法（截断小数）。"""
    s = int(sec)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def _old_hhmmss(sec):
    """asr_transcript.hhmmss 改动前的写法（负数钳到 0 + 四舍五入）。"""
    sec = max(0, int(round(sec)))
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def _old_mmss(start):
    """make_corrected.py:393-395 / asr_transcript.py:363-364 改动前的 MM:SS 行首。"""
    h, m, s = start.split(":")
    return f"[{int(h) * 60 + int(m):02d}:{s}]"


# 对拍用的输入：正常形态 + 毫秒 + 异常形态
PARSE_INPUTS = [
    "00:00:00", "00:01:02", "00:30:25", "01:02:03", "01:38:26", "59:59:59",
    "00:01:02.500", "01:02:03.250", "00:00:59.500",   # 毫秒（既有容错）
    "12:34", "98:26", "99:00",                        # MM:SS
    "1:02:03",                                        # 段位不足两位
    "62", "0",                                        # 纯秒数
    "00:00:60", "00:99:00",                           # 越界
    "坏数据", "", "aa:bb",
]

FORMAT_INPUTS = [0, 1, 9.6, 59, 60, 62.5, 65, 3599, 3600, 5906, 5910, 359999, 1830.869,
                 -1, -0.5, -30]


def _call(fn, arg):
    """调用并把异常也变成可比较的值（旧实现有的输入是抛异常的）。"""
    try:
        return fn(arg)
    except Exception as e:  # noqa: BLE001  对拍要连异常类型一起比
        return f"{type(e).__name__}"


# ─────────────────────────────────────────────
# 1. 解析
# ─────────────────────────────────────────────

def test_parse_accepts_the_four_documented_forms():
    assert timecode.parse_seconds("01:38:26") == 5906          # HH:MM:SS
    assert timecode.parse_seconds("00:01:02.500") == 62        # HH:MM:SS.mmm（截断毫秒）
    assert timecode.parse_seconds("98:26") == 5906             # MM:SS（分承载小时）
    assert timecode.parse_seconds("62") == 62                  # 纯秒数

    # 秒字段的边界：59.999 合法（毫秒），60 越界
    assert timecode.parse_seconds("00:00:59.999") == 59
    assert timecode.parse_seconds("00:59:59") == 3599
    assert timecode.parse_seconds(62) == 62                    # 数值也收
    assert timecode.parse_seconds(62.9) == 62                  # 小数截断


def test_parse_rejects_illegal_values_with_validation_error():
    """非法值一律 ValidationError，而且它必须是 ValueError 的子类。

    子类这件事是**功能要求**不是风格问题：旧调用点写的是
    `except ValueError: return 0`，新异常类型要是冒到调用方去，
    "保持现有行为"当场就破了。
    """
    assert issubclass(timecode.ValidationError, ValueError)

    for bad in ["", "   ", "aa:bb", "00:99:00", "00:00:60", "00:60:00",
                "-1:00:00", "1:2:3:4", "nan:00:00", None, [1], True]:
        try:
            got = timecode.parse_seconds(bad)
        except timecode.ValidationError:
            continue
        raise AssertionError(f"{bad!r} 应该被判非法，却得到 {got!r}")


# ─────────────────────────────────────────────
# 2. 格式化
# ─────────────────────────────────────────────

_HMS_RE = re.compile(r"^\d{2,}:\d{2}:\d{2}$")


def test_format_hms_boundaries():
    assert timecode.format_hms(0) == "00:00:00"
    assert timecode.format_hms(59) == "00:00:59"
    assert timecode.format_hms(60) == "00:01:00"
    assert timecode.format_hms(3600) == "01:00:00"
    assert timecode.format_hms(5910) == "01:38:30"      # >1 小时：进位进小时
    assert timecode.format_hms(359999) == "99:59:59"
    assert timecode.format_hms(9.6) == "00:00:09"       # 截断（四舍五入在各调用点）


def test_format_hms_never_emits_mmss():
    """索引图标签曾经是 MM:SS，98 分钟的片子烧出 "98:26"，AI 照着写进正文
    就再也匹配不上 SCREENSHOT:HH:MM:SS。统一后永不出现这种形态。"""
    for sec in [0, 59, 65, 5906, 5910, 6000, 359999]:
        text = timecode.format_hms(sec)
        assert _HMS_RE.match(text), text
        assert int(text.split(":")[1]) < 60, text       # 中段必须是"分"，不许塞小时
        # 真的能被截图占位符的正则认出来（跨模块一致性）
        assert cf.PLACEHOLDER_RE.fullmatch("SCREENSHOT:" + text), text

    # 对照：MM:SS 那个格式**故意**不被占位符正则接受（硬门禁，不许放宽）
    assert timecode.format_mmss(5906) == "98:26"
    assert not cf.PLACEHOLDER_RE.fullmatch("SCREENSHOT:" + timecode.format_mmss(5906))


# ─────────────────────────────────────────────
# 3. 一致性：新模块 / 旧公共函数 与 冻结的旧实现逐一对拍
# ─────────────────────────────────────────────

def test_parse_call_sites_match_frozen_old_implementations():
    """仍保留包装函数的解析行为：除有意差异外逐一对拍。"""
    pairs = [
        ("post_process.timestamp_to_seconds", _old_timestamp_to_seconds, pp.timestamp_to_seconds),
    ]
    for name, old, new in pairs:
        for ts in PARSE_INPUTS:
            if (name, ts) in DECLARED_PARSE_DIFFS:      # 有意差异见下面那张表
                continue
            assert _call(old, ts) == _call(new, ts), f"{name}({ts!r}) 变了"


def test_format_call_sites_match_frozen_old_implementations():
    """fmt_ts / fmt_hms / hhmmss 的格式化：逐值对拍（含负数与小数）。"""
    pairs = [
        ("capture_frames.fmt_ts", _old_fmt_ts, cf.fmt_ts),
        ("sprite_index.fmt_hms", _old_fmt_hms, si.fmt_hms),
        ("asr_transcript.hhmmss", _old_hhmmss, asr.hhmmss),
    ]
    for name, old, new in pairs:
        for sec in FORMAT_INPUTS:
            assert old(sec) == new(sec), f"{name}({sec!r}) 变了：{old(sec)!r} -> {new(sec)!r}"


def test_shots_spec_rows_unchanged():
    """截图清单（编号/文件名/回链）里的秒数：与冻结的旧写法逐字段对拍。

    这里的输入只可能来自 PLACEHOLDER_RE（严格 HH:MM:SS），所以两边必须一字不差。
    """
    stamps = ["00:00:00", "00:00:10", "01:02:03", "01:38:26", "59:59:59"]
    rows = cf.shots_spec(stamps, "bilibili", "BV1xx411c7mD")
    for ts, row in zip(stamps, rows):
        h, m, s = (int(x) for x in ts.split(":"))          # 改动前的写法
        want = [h * 3600 + m * 60 + s, "shot_" + ts.replace(":", "_"),
                cf.build_url("bilibili", "BV1xx411c7mD", h * 3600 + m * 60 + s),
                cf.build_main_url("bilibili", "BV1xx411c7mD", h * 3600 + m * 60 + s)]
        assert row == want, f"{ts} 的行变了：{row} -> {want}"


def test_mmss_lines_match_frozen_old_implementations():
    """两处 `[MM:SS]` 行首（make_corrected 与 asr 的 transcript.txt）对拍。"""
    starts = ["00:00:00", "00:01:02", "00:30:25", "01:02:03", "01:38:26",
              "59:59:59", "1:02:03"]
    for start in starts:
        want = _old_mmss(start)
        got = "[" + timecode.format_mmss(timecode.parse_seconds(start)) + "]"
        assert want == got, f"[MM:SS] 行首变了：{want} -> {got}"

    # 小时进位进分钟：1 小时 38 分 26 秒写成 [98:26]，这是与 transcript.txt 对齐的既有格式
    assert "[" + timecode.format_mmss(timecode.parse_seconds("01:38:26")) + "]" == "[98:26]"


# 有意放宽/收紧的形态：**只**有这些，且都是真实流水线产生不了的输入。
# 值 = (旧实现的结果, 新实现的结果)；旧实现抛异常时记异常名。
DECLARED_PARSE_DIFFS = {
    # 纯秒数：旧 post_process 只认冒号分隔的 2/3 段
    ("post_process.timestamp_to_seconds", "62"): (0, 62),
    # 越界：新模块校验分/秒范围，旧实现直接按数字计算
    ("post_process.timestamp_to_seconds", "00:00:60"): (60, 0),
    ("post_process.timestamp_to_seconds", "00:99:00"): (5940, 0),
    # 非数字：旧 post_process 直接抛 ValueError，现在兜底成 0
    ("post_process.timestamp_to_seconds", "aa:bb"): ("ValueError", 0),
}


def test_only_declared_differences():
    """把**全部**差异算出来，断言恰好等于上面那张有意差异清单。

    这条用例的意义：将来谁再动了 timecode 的判据，差异集合会立刻不等，
    而不是悄悄混进某个边角输入里。
    """
    dials = [
        ("post_process.timestamp_to_seconds", _old_timestamp_to_seconds, pp.timestamp_to_seconds),
    ]
    diffs = {}
    for name, old, new in dials:
        for ts in PARSE_INPUTS:
            a, b = _call(old, ts), _call(new, ts)
            if a != b:
                diffs[(name, ts)] = (a, b)
    assert diffs == DECLARED_PARSE_DIFFS, (
        "解析行为的差异集合变了：\n  多出来: %r\n  少了:   %r"
        % (sorted(set(diffs) - set(DECLARED_PARSE_DIFFS)),
           sorted(set(DECLARED_PARSE_DIFFS) - set(diffs))))


# ─────────────────────────────────────────────
# 4. 回归：占位符硬门禁 + make_corrected 的行格式
# ─────────────────────────────────────────────

def test_placeholder_re_still_rejects_mmss():
    """PLACEHOLDER_RE 只认 HH:MM:SS（不接受 MM:SS）是**有意**的硬门禁，不许放宽。"""
    d = tempfile.mkdtemp(prefix="vidscribe_timecode_md_")
    try:
        md = os.path.join(d, "book.md")
        with open(md, "w", encoding="utf-8") as f:
            f.write("![a](SCREENSHOT:01:38:26)\n"
                    "![b](SCREENSHOT:98:26)\n"
                    "![c](SCREENSHOT:03:00)\n")
        assert cf.parse_timestamps(md) == ["01:38:26"]
        assert cf.malformed_placeholders(md) == ["03:00", "98:26"]
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_make_corrected_line_format_unchanged():
    """端到端跑 make_corrected.main()（BASE 指到临时目录，不碰 output/），
    行首仍是 [MM:SS]、小时进位进分钟。"""
    d = tempfile.mkdtemp(prefix="vidscribe_timecode_mc_")
    old_base, old_argv = mc.BASE, sys.argv
    try:
        vid = "BVtest_p7"
        os.makedirs(os.path.join(d, "output", vid))
        segs = [{"start": "00:01:02", "end": "00:01:05", "text": "第一句"},
                {"start": "01:38:26", "end": "01:38:28", "text": "第二句"}]
        with open(os.path.join(d, "output", vid, "transcript.json"),
                  "w", encoding="utf-8") as f:
            json.dump({"segments": segs}, f, ensure_ascii=False)
        mc.BASE = d
        sys.argv = ["make_corrected.py", vid]
        mc.main()
        out = open(os.path.join(d, "output", vid, "transcript.corrected.txt"),
                   encoding="utf-8").read()
        assert out == "[01:02] 第一句\n[98:26] 第二句\n", repr(out)

        # 与冻结的旧写法逐行一致
        assert out.splitlines() == [
            _old_mmss("00:01:02") + " 第一句",
            _old_mmss("01:38:26") + " 第二句",
        ]
    finally:
        mc.BASE = old_base
        sys.argv = old_argv
        shutil.rmtree(d, ignore_errors=True)


# ─────────────────────────────────────────────
# 5. 真实产物对拍（只读 output/）
# ─────────────────────────────────────────────

def test_real_transcript_json_paths_agree():
    """拿真实 transcript.json 的每一个 start/end 跑新旧两条路径，必须完全一致。"""
    src = os.path.join(os.path.dirname(SRC), "output", "BV1rXwNzuEmJ", "transcript.json")
    if not os.path.isfile(src):
        raise unittest.SkipTest("没有真实产物，跳过")

    data = json.load(open(src, encoding="utf-8"))
    segs = data["segments"]
    assert segs, "真实产物没有 segments，这条对拍就没意义了"

    values = [s[k] for s in segs for k in ("start", "end")]
    for ts in values:
        assert _old_to_sec(ts) == timecode.parse_seconds(ts), ts
        assert _old_timestamp_to_seconds(ts) == pp.timestamp_to_seconds(ts), ts
        assert _old_mmss(ts) == "[" + timecode.format_mmss(timecode.parse_seconds(ts)) + "]", ts

    # 覆盖率自检这条路径（dump_transcript.main 里那两行）也必须一模一样
    dur = float(data["duration"])
    old_cov = _old_to_sec(segs[-1]["end"]) / dur
    new_cov = quality.coverage_ratio(data)
    assert old_cov == new_cov, (old_cov, new_cov)
