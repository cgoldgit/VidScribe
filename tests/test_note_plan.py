"""纯离线分包回归：保留原句与来源、边界上下文、审阅记录安全。"""

import hashlib
import json
from pathlib import Path
import sys
import tempfile

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import note_plan


def transcript(texts=None, chapters=None):
    texts = texts or ["先提出问题。", "例如取三个盘子，先移动最上面的盘。", "所以得到公式。", "最后运行检查结果。"]
    return {"video_id": "BVtest_p2", "title": "课堂记录", "duration": len(texts) * 10,
            "chapters": chapters or [],
            "segments": [{"start": i * 10, "end": (i + 1) * 10, "text": text}
                         for i, text in enumerate(texts)]}


def test_all_segments_owned_once_despite_overlap_context():
    source = transcript()
    plan = note_plan.build_plan(source, max_chars=20, context_segments=2)
    owned = [identifier for packet in plan["packets"] for identifier in packet["source_segments"]]
    assert owned == ["S000001", "S000002", "S000003", "S000004"]
    assert plan["packets"][1]["context_before"] == ["S000001"]
    assert len(set(owned)) == len(owned)
    assert all(p["start"].count(":") == 2 for p in plan["packets"])


def test_long_segment_kept_whole_and_warned():
    text = "原例子的完整输入与结果" * 50
    source = transcript([text, "随后解释"])
    plan = note_plan.build_plan(source, max_chars=20)
    assert len(plan["packets"]) == 2
    content = note_plan.render_packet(plan["packets"][0], note_plan._normalized_segments(source))
    assert text in content
    assert any("单句超过" in warning for warning in plan["warnings"])


def test_official_chapters_split_without_reordering():
    source = transcript(chapters=[{"start": "00:00:00", "title": "问题"},
                                  {"start": "00:00:20", "title": "推导"}])
    plan = note_plan.build_plan(source)
    assert [packet["chapter_hint"] for packet in plan["packets"]] == ["问题", "推导"]
    assert plan["packets"][1]["source_segments"] == ["S000003", "S000004"]


def test_time_budget_and_missing_chapters():
    plan = note_plan.build_plan(transcript(), max_seconds=15, context_segments=0)
    assert len(plan["packets"]) == 4
    assert all(not packet["context_before"] and not packet["context_after"]
               for packet in plan["packets"])


def test_visual_candidates_are_metadata_not_visual_claims():
    sprite = {"sheets": [{"file": "sprite/sheet_01.png", "cells": [
        {"pos": 0, "time": "00:00:10", "since": 0, "until": 20, "merged": 3},
        {"pos": 1, "time": "00:00:55", "since": 50, "until": 60}]}]}
    plan = note_plan.build_plan(transcript(), sprite)
    candidates = plan["packets"][0]["visual_candidates"]
    assert len(candidates) == 1
    assert candidates[0]["merged"] == 3
    assert "description" not in candidates[0]
    kinds = {kind for cue in plan["packets"][0]["teaching_cues"] for kind in cue["kinds"]}
    assert {"example", "derivation", "operation"} <= kinds


def test_bad_segments_fail_instead_of_silently_skipping():
    cases = [{}, {"segments": []}, {"segments": [{"start": "bad", "end": 1, "text": "正文"}]},
             {"segments": [{"start": 2, "end": 1, "text": "正文"}]},
             {"segments": [{"start": 0, "end": 1, "text": None}]}]
    out_of_order = transcript()
    out_of_order["segments"][1]["start"] = -1
    cases.append(out_of_order)
    for source in cases:
        try:
            note_plan.build_plan(source)
        except ValueError:
            pass
        else:
            raise AssertionError(f"非法字幕未被拒绝: {source}")


def test_schema2_keeps_candidates_omitted_from_overview():
    sprite = {"schema_version": 2, "sheets": [{"file": "sprite/sheet_01.png", "cells": [
        {"pos": 0, "time": "00:00:10", "since": 0, "until": 15}]}],
        "candidates": [{"id": "candidate_0001", "time": "00:00:10", "since": 0,
                        "until": 15, "overview_visible": True},
                       {"id": "candidate_0002", "time": "00:00:25", "since": 15,
                        "until": 30, "overview_visible": False}],
        "omitted_candidates": ["candidate_0002"]}
    plan = note_plan.build_plan(transcript(), sprite)
    candidates = plan["packets"][0]["visual_candidates"]
    assert [entry["id"] for entry in candidates] == ["candidate_0001", "candidate_0002"]
    assert candidates[0]["sheet"] == "sprite/sheet_01.png"
    assert candidates[1]["sheet"] == "" and not candidates[1]["overview_visible"]
    assert any("未展示在概览" in warning for warning in plan["warnings"])


def test_missing_and_nonfinite_duration():
    source = transcript()
    source.pop("duration")
    plan = note_plan.build_plan(source)
    assert plan["duration"] == 40
    assert any("不能据此证明" in warning for warning in plan["warnings"])
    for value in [float("inf"), float("nan"), -1, "not numeric"]:
        source["duration"] = value
        try:
            note_plan.build_plan(source)
        except ValueError:
            pass
        else:
            raise AssertionError("非法片长未被拒绝")


def test_invalid_chapters_do_not_lose_source():
    source = transcript(chapters=[{"start": "bad", "title": "问题"}])
    plan = note_plan.build_plan(source)
    assert plan["segment_count"] == 4
    assert any("官方章节" in warning for warning in plan["warnings"])


def test_generate_keeps_review_and_requires_force_for_changed_input():
    with tempfile.TemporaryDirectory(prefix="vs_note_plan_") as temporary:
        directory = Path(temporary)
        source = transcript()
        source_path = directory / "transcript.json"
        source_path.write_text(json.dumps(source, ensure_ascii=False), encoding="utf-8")
        review_path = directory / "note_review.json"
        review_path.write_text("人工已完成的审阅", encoding="utf-8")
        plan = note_plan.generate(directory, video_id="BVtest_p2")
        assert plan["source_sha256"] == hashlib.sha256(source_path.read_bytes()).hexdigest()
        assert review_path.read_text(encoding="utf-8") == "人工已完成的审阅"
        assert (directory / plan["packets"][0]["path"]).exists()
        template = json.loads((directory / "note_review.template.json").read_text(encoding="utf-8"))
        assert template["coverage"][0]["status"] == "pending"
        assert template["visual_review"] == "pending"
        note_plan.generate(directory, video_id="BVtest_p2")  # 幂等
        source["segments"][0]["text"] += "补充事实"
        source_path.write_text(json.dumps(source), encoding="utf-8")
        original_plan = (directory / "note_plan.json").read_bytes()
        try:
            note_plan.generate(directory, video_id="BVtest_p2")
        except ValueError as exc:
            assert "--force" in str(exc)
        else:
            raise AssertionError("改变输入必须显式确认重建")
        assert (directory / "note_plan.json").read_bytes() == original_plan
        replacement = note_plan.generate(directory, video_id="BVtest_p2", force=True)
        assert replacement["source_sha256"] != plan["source_sha256"]
        assert review_path.read_text(encoding="utf-8") == "人工已完成的审阅"


def test_cli_out_directory_and_id_mismatch():
    with tempfile.TemporaryDirectory(prefix="vs_note_plan_cli_") as temporary:
        path = Path(temporary) / "transcript.json"
        path.write_text(json.dumps(transcript()), encoding="utf-8")
        assert note_plan.main(["BVtest_p2", "--out", temporary]) == 0
        assert (Path(temporary) / "note_plan.json").exists()
        try:
            note_plan.main(["BVdifferent", "--out", temporary])
        except SystemExit as exc:
            assert exc.code == 1
        else:
            raise AssertionError("video_id mismatch 必须失败")


def test_invalid_budget_and_path_fail():
    for settings in [{"max_chars": 0}, {"max_seconds": 0}, {"context_segments": -1}]:
        try:
            note_plan.build_plan(transcript(), **settings)
        except ValueError:
            pass
        else:
            raise AssertionError("非法预算未被拒绝")
    try:
        note_plan.main(["../escape"])
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("video_id 路径逃逸未拒绝")


def test_rendered_source_remains_exact_with_milliseconds():
    source = transcript(["条件是 $a=3$\n继续原讲推导。", "结果为 4，别改数值。"])
    source["segments"][0]["start"] = "00:00:00.500"
    source["segments"][0]["end"] = "00:00:09.750"
    plan = note_plan.build_plan(source)
    rendered = note_plan.render_packet(plan["packets"][0], note_plan._normalized_segments(source))
    assert source["segments"][0]["text"] in rendered
    assert source["segments"][1]["text"] in rendered
    assert "[S000001@00:00:00] " + source["segments"][0]["text"] in rendered
    assert plan["segment_times"]["S000001"] == {"start": 0, "end": 9}
    full = note_plan.render_packet(plan["packets"][0], note_plan._normalized_segments(source),
                                   detail="full")
    assert "S000001 00:00:00–00:00:09" in full


def test_summary_lists_all_packets_without_repeating_text_or_segment_ids():
    with tempfile.TemporaryDirectory(prefix="vs_note_summary_") as temporary:
        directory = Path(temporary)
        source = transcript()
        (directory / "transcript.json").write_text(json.dumps(source), encoding="utf-8")
        plan = note_plan.generate(directory, max_chars=20)
        summary = (directory / "note_plan.summary.md").read_text(encoding="utf-8")
        assert plan["source_sha256"] in summary
        for packet in plan["packets"]:
            assert packet["id"] in summary and packet["path"] in summary
            assert packet["start"] in summary and packet["end"] in summary
            for identifier in packet["source_segments"]:
                assert identifier not in summary
        assert all(segment["text"] not in summary for segment in source["segments"])
        assert "source_count" in summary and "candidate_count" in summary and "unseen_count" in summary
        assert "不能保证总 token 下降" in summary
        rendered = (directory / plan["packets"][0]["path"]).read_text(encoding="utf-8")
        assert "```json" not in rendered
        assert "| id | time | since | until | weak | overview_visible | sheet | pos |" in rendered


def test_compact_candidates_keep_unseen_and_legacy_locations():
    sprite = {"sheets": [{"file": "sprite/sheet_01.png", "cells": [
        {"pos": 0, "time": "00:00:10", "since": 0, "until": 15}]}],
        "candidates": [{"id": "candidate_0001", "time": "00:00:10", "since": 0,
                        "until": 15, "weak": True},
                       {"id": "candidate_0002", "time": "00:00:25", "since": 15,
                        "until": 30, "weak": False}]}
    source = transcript()
    plan = note_plan.build_plan(source, sprite)
    packet = plan["packets"][0]
    rendered = note_plan.render_packet(packet, note_plan._normalized_segments(source))
    assert "| candidate_0001 | 00:00:10 | 00:00:00 | 00:00:15 | 1 | 1 | sprite/sheet_01.png | 0 |" in rendered
    assert "| candidate_0002 | 00:00:25 | 00:00:15 | 00:00:30 | 0 | 0 | - | - |" in rendered
    legacy = note_plan.build_plan(source, {"sheets": sprite["sheets"]})
    legacy_rendered = note_plan.render_packet(legacy["packets"][0], note_plan._normalized_segments(source))
    assert "| time_00_00_10 |" in legacy_rendered
    assert "| 0 | 1 | sprite/sheet_01.png | 0 |" in legacy_rendered


def test_full_detail_retains_json_and_compact_keeps_source_exact():
    source = transcript(["  原文空白不能删除。\n下一行也是原文。", "例如这张图先输入，然后检查。"])
    plan = note_plan.build_plan(source)
    normalized = note_plan._normalized_segments(source)
    packet = plan["packets"][0]
    compact = note_plan.render_packet(packet, normalized)
    full = note_plan.render_packet(packet, normalized, detail="full")
    for segment in source["segments"]:
        assert segment["text"] in compact and segment["text"] in full
    assert len(compact) < len(full)
    assert "## 讲解线索" not in compact and "## 讲解线索" in full
    candidate_json = full.split("```json\n", 1)[1].split("\n```", 1)[0]
    assert json.loads(candidate_json) == packet["visual_candidates"]
    try:
        note_plan.render_packet(packet, normalized, detail="unknown")
    except ValueError:
        pass
    else:
        raise AssertionError("未知展示模式须拒绝")


def test_detail_modes_preserve_source_and_legacy_manifest():
    with tempfile.TemporaryDirectory(prefix="vs_note_detail_") as temporary:
        directory = Path(temporary)
        source = transcript()
        (directory / "transcript.json").write_text(json.dumps(source), encoding="utf-8")
        old = note_plan.generate(directory, detail="full")
        legacy = dict(old)
        legacy.pop("packet_detail")
        legacy.pop("segment_times")
        (directory / "note_plan.json").write_text(json.dumps(legacy), encoding="utf-8")
        review = directory / "note_review.json"
        review.write_text("已填审阅", encoding="utf-8")
        compact = note_plan.generate(directory)  # 只改展示，旧来源无需 force。
        assert compact["packet_detail"] == "compact"
        assert compact["source_sha256"] == old["source_sha256"]
        assert compact["settings"] == old["settings"]
        assert compact["packets"] == old["packets"]
        assert compact["segment_times"]["S000004"] == {"start": 30, "end": 40}
        assert review.read_text(encoding="utf-8") == "已填审阅"
        assert "[S000001@" in (directory / compact["packets"][0]["path"]).read_text(encoding="utf-8")
        assert note_plan.main(["BVtest_p2", "--out", temporary, "--detail", "full"]) == 0
        full_text = (directory / compact["packets"][0]["path"]).read_text(encoding="utf-8")
        assert "```json" in full_text


def test_candidate_location_cannot_break_short_table():
    source = transcript()
    sprite = {"sheets": [{"file": "sprite/a|b\n.png", "cells": [
        {"pos": 0, "time": "00:00:10", "since": 0, "until": 15}]}]}
    plan = note_plan.build_plan(source, sprite)
    rendered = note_plan.render_packet(plan["packets"][0], note_plan._normalized_segments(source))
    assert "sprite/a&#124;b .png" in rendered


def test_lean_preserves_source_and_every_candidate_field_with_escaped_locations():
    source = transcript(["  原句空白\n与下一行必须完整。", "继续验证结果。"])
    sprite = {"sheets": [{"file": "sprite/a|b\\c\t\n.png", "cells": [
        {"pos": 0, "time": "00:00:10", "since": 0, "until": 15}]}],
        "candidates": [{"id": "C1", "time": "00:00:10", "since": 0, "until": 15, "weak": True},
                       {"id": "C2", "time": "00:00:18", "since": 15, "until": 20}]}
    plan = note_plan.build_plan(source, sprite)
    text = note_plan.render_packet(plan["packets"][0], note_plan._normalized_segments(source), detail="lean")
    assert all(segment["text"] in text for segment in source["segments"])
    rows = text.split("```tsv\n", 1)[1].split("\n```", 1)[0].splitlines()
    assert rows[0].split("\t") == ["id", "time", "since", "until", "weak", "overview_visible", "sheet", "pos"]
    assert rows[1].split("\t") == ["C1", "00:00:10", "00:00:00", "00:00:15", "1", "1",
                                     "sprite/a|b\\\\c\\t\\n.png", "0"]
    assert rows[2].split("\t") == ["C2", "00:00:18", "00:00:15", "00:00:20", "0", "0", "-", "-"]
    assert len(rows) == len(plan["packets"][0]["visual_candidates"]) + 1


def test_catalog_selection_keeps_unsorted_overlaps_inclusive_boundaries_and_independent_entries():
    sprite = {"candidates": [
        {"id": "long", "time": "00:00:30", "since": 0, "until": 40},
        {"id": "edge", "time": "00:00:10", "since": 10, "until": 20},
        {"id": "invalid", "time": "bad"}], "sheets": []}
    source = transcript()
    plan = note_plan.build_plan(source, sprite, max_seconds=10)
    assert len(plan["packets"]) == 4
    assert [c["id"] for c in plan["packets"][0]["visual_candidates"]] == ["long", "edge"]
    assert [c["id"] for c in plan["packets"][2]["visual_candidates"]] == ["long", "edge"]
    assert [c["id"] for c in plan["packets"][3]["visual_candidates"]] == ["long"]
    plan["packets"][0]["visual_candidates"][0]["sheet"] = "changed"
    assert plan["packets"][1]["visual_candidates"][0]["sheet"] == ""
