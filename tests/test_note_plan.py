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
    assert "S000001 00:00:00–00:00:09" in rendered


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
        candidate_json = rendered.split("```json\n", 1)[1].split("\n```", 1)[0]
        assert json.loads(candidate_json) == plan["packets"][0]["visual_candidates"]
