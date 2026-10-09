"""汇总独立笔记任务的实际计数，不调用模型，不导出原始响应。

python src/note_usage.py <video_id> [--out VIDEO_DIR]
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

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass

BASE_DIR = Path(__file__).resolve().parent.parent
COUNTERS = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens")


def _number(value):
    return value if (isinstance(value, (int, float)) and not isinstance(value, bool)
                     and math.isfinite(value) and value >= 0) else None


def _usage(entry):
    result = dict.fromkeys(COUNTERS)
    usage = entry.get("usage")
    if entry.get("usage_known") is False or not isinstance(usage, dict):
        return result
    if "prompt_tokens" in usage:
        total = _number(usage.get("prompt_tokens"))
        cache = _number(usage.get("prompt_cache_hit_tokens"))
        details = usage.get("prompt_tokens_details")
        if cache is None and isinstance(details, dict):
            cache = _number(details.get("cached_tokens"))
        uncached = _number(usage.get("prompt_cache_miss_tokens"))
        if cache is None and total is not None and uncached is not None and uncached <= total:
            cache = total - uncached
        if uncached is None and total is not None and cache is not None and cache <= total:
            uncached = total - cache
        if total is not None and ((cache is not None and cache > total)
                                  or (uncached is not None and uncached > total)
                                  or (cache is not None and uncached is not None and cache + uncached != total)):
            cache = uncached = None
        result.update(input_tokens=uncached, cache_read_input_tokens=cache,
                      cache_creation_input_tokens=0, output_tokens=_number(usage.get("completion_tokens")))
    else:
        total = _number(usage.get("input_tokens"))
        details = usage.get("input_tokens_details")
        if isinstance(details, dict):  # Responses 的 input 已包含缓存读取。
            cache = _number(details.get("cached_tokens"))
            uncached = total - cache if total is not None and cache is not None and cache <= total else None
            if total is not None and cache is not None and cache > total:
                cache = None
            result.update(input_tokens=uncached, cache_read_input_tokens=cache,
                          cache_creation_input_tokens=0)
        else:  # CC / Anthropic 的 input 不含缓存读取或创建。
            result.update(input_tokens=total,
                          cache_read_input_tokens=_number(usage.get("cache_read_input_tokens")),
                          cache_creation_input_tokens=_number(usage.get("cache_creation_input_tokens")))
        result["output_tokens"] = _number(usage.get("output_tokens"))
    return result


def _contained(root, path):
    resolved = path.resolve()
    if not resolved.is_relative_to(root):
        raise ValueError("Run or report path escapes the video directory")
    return resolved


def _attempts(state):
    history = state.get("history")
    if history is None:
        identifier = state.get("attempts", 1)
        if isinstance(identifier, bool) or not isinstance(identifier, int) or identifier < 1:
            raise ValueError("Invalid attempt identifier")
        return [(identifier, state)]
    if not isinstance(history, list) or any(not isinstance(item, dict) for item in history):
        raise ValueError("Invalid run history")
    records = {}
    for number, item in enumerate(history, 1):
        identifier = item.get("attempts", number)
        if isinstance(identifier, bool) or not isinstance(identifier, int) or identifier < 1:
            raise ValueError("Invalid attempt identifier")
        records[identifier] = item
    current = state.get("attempts", len(history) or 1)
    if isinstance(current, bool) or not isinstance(current, int) or current < 1:
        raise ValueError("Invalid attempt identifier")
    if current not in records:
        records[current] = state
    return sorted(records.items())


def _summary(entries):
    counts = {}
    known_fields = {}
    for name in COUNTERS:
        values = [entry["tokens"][name] for entry in entries if entry["tokens"][name] is not None]
        counts[name] = sum(values) if values else None
        known_fields[name] = len(values)
    costs = [entry["cost_usd_estimate"] for entry in entries if entry["cost_usd_estimate"] is not None]
    elapsed = [entry.get('elapsed_seconds') for entry in entries if entry.get('elapsed_seconds') is not None]
    turns = [entry.get('num_turns') for entry in entries if entry.get('num_turns') is not None]
    return {"attempts": len(entries), "complete_usage_attempts": sum(e["usage_complete"] for e in entries),
            "unknown_usage_attempts": sum(all(v is None for v in e["tokens"].values()) for e in entries),
            "tokens": counts, "known_attempts_by_counter": known_fields,
            "usage_complete": bool(entries) and all(e["usage_complete"] for e in entries),
            "cost_usd_estimate": sum(costs) if costs else None,
            "cost_status": "estimated" if costs else "unknown", "cost_known_attempts": len(costs),
            "cost_complete": bool(entries) and len(costs) == len(entries),
            "elapsed_seconds_sum": sum(elapsed) if elapsed else None,
            "elapsed_known_attempts": len(elapsed),
            "num_turns_sum": sum(turns) if turns else None,
            "turns_known_attempts": len(turns)}


def summarize(directory, *, video_id=None):
    root = Path(directory).resolve()
    if not root.is_dir():
        raise ValueError("Video directory does not exist")
    entries, reuses, recoveries = [], [], []
    for folder, kind in (("jobs", "writing"), ("visual_jobs", "visual")):
        base = _contained(root, root / folder)
        for path in sorted(base.glob("*/run.json")):
            path = _contained(root, path)
            state = json.loads(path.read_text(encoding="utf-8-sig"))
            if not isinstance(state, dict):
                raise ValueError("Run state must be an object")
            for identifier, record in _attempts(state):
                tokens = _usage(record)
                # 只导出白名单字段，不透传异常、模型正文、提示词或 usage 扩展字段。
                entries.append({"source": path.relative_to(root).as_posix(), "kind": kind,
                                "attempt": identifier,
                                "status": record.get("status") if record.get("status") in
                                ("running", "failed", "produced", "pending") else "unknown",
                                "tokens": tokens, "usage_complete": all(v is not None for v in tokens.values()),
                                "cost_usd_estimate": _number(record.get("cost_usd_estimate")),
                                "elapsed_seconds": _number(record.get('elapsed_seconds')),
                                "num_turns": _number(record.get('num_turns')),
                                "prompt_chars": _number(record.get('prompt_chars')),
                                "schema_chars": _number(record.get('schema_chars')),
                                "shared_prefix_chars": _number(record.get('shared_prefix_chars')),
                                "prompt_layout": record.get('prompt_layout') if record.get('prompt_layout') in
                                                  ('compiled', 'compatibility', 'files') else None,
                                "embedded_inputs": _number(record.get('embedded_inputs')),
                                "input_mode": record.get('input_mode') if record.get('input_mode') in ('inline', 'files') else None,
                                "output_mode": record.get('output_mode') if record.get('output_mode') in ('structured', 'files') else None})
    for path in sorted((root / 'jobs').glob('*/reuse.json')):
        path = _contained(root, path)
        state = json.loads(path.read_text(encoding='utf-8-sig'))
        draft_path = _contained(root, path.parent / 'draft.md')
        review_path = _contained(root, path.parent / 'review.json')
        if (not isinstance(state, dict) or state.get('status') != 'reused'
                or state.get('source_review_accepted') is not True):
            raise ValueError('Invalid writing reuse provenance')
        try:
            review = json.loads(review_path.read_text(encoding='utf-8-sig'))
            review_sha = hashlib.sha256(json.dumps(review, ensure_ascii=False, sort_keys=True,
                                                   separators=(',', ':')).encode('utf-8')).hexdigest()
            unchanged = (state.get('draft_sha256') == hashlib.sha256(draft_path.read_bytes()).hexdigest()
                         and state.get('review_sha256') == review_sha)
        except (OSError, ValueError, UnicodeError):
            unchanged = False
        # Routine local repairs must not hide persisted paid/failed attempts.
        reuses.append({'source': path.relative_to(root).as_posix(), 'kind': 'writing',
                       'status': 'reused' if unchanged else 'changed_after_reuse'})
    for path in sorted((root / 'jobs').glob('*/recovery.json')):
        path = _contained(root, path)
        state = json.loads(path.read_text(encoding='utf-8-sig'))
        if not isinstance(state, dict) or state.get('status') != 'recovered':
            raise ValueError('Invalid writing recovery provenance')
        draft_path = _contained(root, path.parent / 'draft.md')
        review_path = _contained(root, path.parent / 'review.json')
        metadata_path = _contained(root, path.parent / 'job.json')
        try:
            review = json.loads(review_path.read_text(encoding='utf-8-sig'))
            metadata = json.loads(metadata_path.read_text(encoding='utf-8-sig'))
            def digest(value):
                return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                                   separators=(',', ':')).encode('utf-8')).hexdigest()
            unchanged = (state.get('draft_sha256') == hashlib.sha256(draft_path.read_bytes()).hexdigest()
                         and state.get('review_sha256') == digest(review)
                         and state.get('metadata_sha256') == digest(metadata))
        except (OSError, ValueError, UnicodeError):
            unchanged = False
        recoveries.append({'source': path.relative_to(root).as_posix(), 'kind': 'writing',
                           'status': 'recovered' if unchanged else 'changed_after_recovery'})
    return {"schema_version": 1, "video_id": video_id or root.name,
            "overall": _summary(entries),
            "by_kind": {kind: _summary([e for e in entries if e["kind"] == kind])
                        for kind in ("writing", "visual")}, "attempts": entries,
            "reused_jobs": len(reuses), "reuses": reuses,
            "changed_reused_jobs": sum(e['status'] == 'changed_after_reuse' for e in reuses),
            "recovered_jobs": len(recoveries), "recoveries": recoveries,
            "changed_recovered_jobs": sum(e['status'] == 'changed_after_recovery' for e in recoveries),
            "limitations": ["Counts are provider/CC reports, not verified billing; total_tokens is never added.",
                            "Missing/invalid counts are null. Partial sums are lower bounds, not zero-cost proof.",
                            "Cache creation is zero only for Responses/OpenAI/DeepSeek formats without that category.",
                            "Costs are estimates; missing attempt costs remain unknown.",
                            "Only persisted runs are counted. Runs outside these jobs and incomplete legacy history are unmeasured.",
                            "Accepted writing reuse is counted separately; no model tokens or cost savings are invented.",
                            "Changed reused artifacts need fresh source review; prior model usage remains counted.",
                            "Local candidate recovery is counted separately and preserves the paid failed attempt."]}


def write_report(directory, *, video_id=None):
    report = summarize(directory, video_id=video_id)
    root = Path(directory).resolve()
    if (root / "note_usage.json").is_symlink():
        raise ValueError("Report path cannot be a symbolic link")
    target = _contained(root, root / "note_usage.json")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=root, delete=False) as stream:
            temporary = stream.name
            json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
        os.replace(temporary, target)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video_id")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.video_id) or args.video_id in (".", ".."):
        parser.error("Invalid video_id")
    directory = args.out or BASE_DIR / "output" / args.video_id
    try:
        report = write_report(directory, video_id=args.video_id)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"note_usage: {type(exc).__name__}; cannot summarize run counters\n")
    summary = report["overall"]
    print(f'note_usage: {summary["attempts"]} attempts, '
          f'{summary["complete_usage_attempts"]} complete, {summary["unknown_usage_attempts"]} unknown')
    print(json.dumps({"tokens": summary["tokens"], "cost_usd_estimate": summary["cost_usd_estimate"],
                      "cost_status": summary["cost_status"], "cost_complete": summary["cost_complete"]}))
    print(f'Report: {Path(directory).resolve() / "note_usage.json"}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
