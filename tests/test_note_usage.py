"""离线计数核验：缓存不重计、失败不丢失、未知不充零。"""

import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
import note_usage


def save(root, job, state):
    path = root / job / "run.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state), encoding="utf-8")
    return path


def anthropic(attempt=1, status="produced", **updates):
    record = {"attempts": attempt, "status": status, "usage_known": True,
              "usage": {"input_tokens": 100, "cache_read_input_tokens": 50,
                        "cache_creation_input_tokens": 20, "output_tokens": 10,
                        "total_tokens": 999999}, "cost_usd_estimate": 0.1}
    record.update(updates)
    return record


def test_latency_and_turns_include_failures_and_leave_legacy_unknown():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        history = [anthropic(1, "failed", elapsed_seconds=2.5, num_turns=2),
                   anthropic(2, elapsed_seconds=4.5, num_turns=3, prompt_chars=1000,
                             input_mode="inline", embedded_inputs=3)]
        save(root, "jobs/P001", dict(history[-1], history=history))
        save(root, "jobs/P002", anthropic())
        report = note_usage.summarize(root)
        assert report["overall"]["elapsed_seconds_sum"] == 7
        assert report["overall"]["elapsed_known_attempts"] == 2
        assert report["overall"]["num_turns_sum"] == 5
        assert report["overall"]["turns_known_attempts"] == 2
        assert report["attempts"][-1]["elapsed_seconds"] is None
        assert report["attempts"][-1]["input_mode"] is None


def test_failed_and_successful_history_count_once_and_split_kind():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        history = [anthropic(1, "failed"), anthropic(2)]
        save(root, "jobs/P001", dict(history[-1], history=history))
        save(root, "visual_jobs/V001", anthropic())
        report = note_usage.summarize(root)
        assert report["overall"]["attempts"] == 3
        assert report["by_kind"]["writing"]["attempts"] == 2
        assert report["by_kind"]["visual"]["attempts"] == 1
        assert report["overall"]["tokens"] == {"input_tokens": 300, "cache_read_input_tokens": 150,
                                              "cache_creation_input_tokens": 60, "output_tokens": 30}
        assert report["overall"]["cost_status"] == "estimated"
        assert report["attempts"][0]["status"] == "failed"
        assert report["attempts"][0]["source"] == "jobs/P001/run.json"
        assert "total_tokens" not in report["overall"]["tokens"]
        assert all("total_tokens" not in entry["tokens"] for entry in report["attempts"])


def test_responses_and_deepseek_cache_is_subtracted_from_input():
    responses = {"usage": {"input_tokens": 120, "input_tokens_details": {"cached_tokens": 90},
                           "output_tokens": 10, "total_tokens": 130}}
    deepseek = {"usage": {"prompt_tokens": 120, "prompt_cache_hit_tokens": 90,
                          "prompt_cache_miss_tokens": 30, "completion_tokens": 10}}
    openai = {"usage": {"prompt_tokens": 120, "prompt_tokens_details": {"cached_tokens": 90},
                        "completion_tokens": 10}}
    miss_only = {"usage": {"prompt_tokens": 120, "prompt_cache_miss_tokens": 30, "completion_tokens": 10}}
    expected = {"input_tokens": 30, "cache_read_input_tokens": 90,
                "cache_creation_input_tokens": 0, "output_tokens": 10}
    for entry in (responses, deepseek, openai, miss_only):
        assert note_usage._usage(entry) == expected


def test_missing_and_invalid_usage_and_cost_are_unknown():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        save(root, "jobs/P001", anthropic(usage_known=False, cost_usd_estimate=None))
        save(root, "jobs/P002", {"attempts": 1, "status": "failed"})
        save(root, "visual_jobs/V001", {"attempts": 1, "usage": {
            "input_tokens": True, "cache_read_input_tokens": -1,
            "cache_creation_input_tokens": float("inf"), "output_tokens": "42"},
            "cost_usd_estimate": float("nan")})
        report = note_usage.summarize(root)
        assert report["overall"]["unknown_usage_attempts"] == 3
        assert all(value is None for value in report["overall"]["tokens"].values())
        assert report["overall"]["cost_usd_estimate"] is None
        assert report["overall"]["cost_status"] == "unknown"


def test_running_attempt_is_added_without_losing_old_failed_usage():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        save(root, "jobs/P001", {"attempts": 2, "status": "running", "usage_known": False,
                                 "history": [anthropic(1, "failed")]})
        report = note_usage.summarize(root)
        summary = report["overall"]
        assert summary["attempts"] == 2
        assert summary["complete_usage_attempts"] == 1
        assert summary["unknown_usage_attempts"] == 1
        assert summary["tokens"]["input_tokens"] == 100
        assert summary["usage_complete"] is False
        assert summary["cost_usd_estimate"] == 0.1 and summary["cost_complete"] is False


def test_partial_counts_do_not_imply_complete_usage_or_zero_cache():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        save(root, "jobs/P001", {"attempts": 1, "usage": {"input_tokens": 120, "output_tokens": 0}})
        report = note_usage.summarize(root)
        assert report["overall"]["tokens"]["input_tokens"] == 120
        assert report["overall"]["tokens"]["output_tokens"] == 0
        assert report["overall"]["tokens"]["cache_read_input_tokens"] is None
        assert report["overall"]["usage_complete"] is False
        assert report["overall"]["unknown_usage_attempts"] == 0


def test_report_only_exports_counter_whitelist_and_no_raw_text():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        secret = "PRIVATE_PROMPT_AND_RESPONSE"
        state = anthropic()
        state.update(prompt=secret, result=secret, failure_type=secret)
        state["usage"]["raw"] = secret
        state["status"] = secret
        save(root, "jobs/P001", state)
        assert note_usage.main(["BVtest", "--out", temporary]) == 0
        text = (root / "note_usage.json").read_text(encoding="utf-8")
        assert secret not in text
        assert json.loads(text)["attempts"][0]["status"] == "unknown"


def test_paths_and_malformed_states_fail_safely():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        for path in (root.parent / "outside", root / ".." / "outside"):
            try:
                note_usage._contained(root.resolve(), path)
            except ValueError:
                pass
            else:
                raise AssertionError("Path escape accepted")
        with patch.object(Path, "glob", return_value=[root.parent / "outside" / "run.json"]):
            try:
                note_usage.summarize(root)
            except ValueError:
                pass
            else:
                raise AssertionError("Run file escape accepted")
        for state in ({"attempts": True}, {"attempts": 0}, {"attempts": 1, "history": ["raw"]}):
            save(root, "jobs/P001", state)
            try:
                note_usage.summarize(root)
            except ValueError:
                pass
            else:
                raise AssertionError("Malformed state accepted")
    try:
        note_usage.main(["../escape"])
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("Unsafe video id accepted")


def test_impossible_cache_and_empty_directory_remain_unknown():
    for usage in ({"input_tokens": 10, "input_tokens_details": {"cached_tokens": 20}},
                  {"prompt_tokens": 10, "prompt_cache_hit_tokens": 20},
                  {"prompt_tokens": 10, "prompt_cache_hit_tokens": 8, "prompt_cache_miss_tokens": 8}):
        result = note_usage._usage({"usage": usage})
        assert result["input_tokens"] is None and result["cache_read_input_tokens"] is None
    with tempfile.TemporaryDirectory() as temporary:
        report = note_usage.summarize(temporary)
        assert report["overall"]["attempts"] == 0
        assert report["overall"]["usage_complete"] is False
        assert report["overall"]["tokens"]["input_tokens"] is None
