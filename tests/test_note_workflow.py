"""Offline workflow checks: isolated jobs, evidence validity, and bounded model execution."""

import copy
import hashlib
import io
import json
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
import sys
import tempfile
import subprocess

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import note_plan
import note_workflow
from _helpers import still_image, write_png


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _fixture(root):
    root = Path(root)
    source = {"video_id": "BVworkflow", "title": "课堂记录", "duration": 40,
              "chapters": [{"start": "00:00:00", "title": "例子"},
                           {"start": "00:00:20", "title": "推导"}],
              "segments": [{"start": i * 10, "end": (i + 1) * 10, "text": text}
                           for i, text in enumerate([
                               "例如先输入三组数据。", "再运行程序，得到第一个结果。",
                               "接着推导第二步公式。", "最后验证公式中的条件。"])]}
    _write_json(root / "transcript.json", source)
    plan = note_plan.generate(root, video_id="BVworkflow")
    assert len(plan["packets"]) == 2
    return root, plan


def _expect_value_error(action):
    try:
        action()
    except ValueError:
        return
    raise AssertionError("Invalid or stale workflow input was not rejected")


def _complete_jobs(root, plan):
    note_workflow.prepare(root)
    for packet in plan["packets"]:
        job = root / "jobs" / packet["id"]
        section = f"讲解 {packet['id']}"
        (job / "draft.md").write_text(
            f"## {section}\n\n{packet['id']} 的完整讲解。\n\n**本章小结**\n\n保留本章例子、操作结果与适用条件。\n",
            encoding="utf-8")
        review = _read_json(job / "review.template.json")
        for entry in review["coverage"]:
            entry["status"] = "written"
            entry["section"] = section
        review["visual_review"] = "complete"
        review["visual_review_reason"] = "逐句检查，本包只需要文字解释。"
        review["fact_review"] = "complete"
        review["fact_review_reason"] = "核查了本包事实，本例没有需要纠正的错误或纯广告。"
        review.setdefault("corrections", [])
        _write_json(job / "review.json", review)


def test_prepare_preserves_existing_drafts_and_human_reviews():
    with tempfile.TemporaryDirectory(prefix="vs_workflow_") as tmp:
        root, plan = _fixture(tmp)
        note_workflow.prepare(root)
        originals = {}
        for packet in plan["packets"]:
            job = root / "jobs" / packet["id"]
            assert (job / "task.md").is_file()
            assert (job / "review.template.json").is_file()
            for name, value in [("draft.md", "人工整理的正文"),
                                ("review.json", '{"human": true}')]:
                path = job / name
                path.write_text(value, encoding="utf-8")
                originals[path] = path.read_bytes()
        note_workflow.prepare(root)
        assert all(path.read_bytes() == data for path, data in originals.items())


def test_prepare_rejects_source_changed_since_plan_generation():
    with tempfile.TemporaryDirectory(prefix="vs_workflow_stale_") as tmp:
        root, _ = _fixture(tmp)
        source = _read_json(root / "transcript.json")
        source["segments"][0]["text"] += "新的事实"
        _write_json(root / "transcript.json", source)
        _expect_value_error(lambda: note_workflow.prepare(root))


def test_prepare_rejects_packet_paths_outside_video_directory():
    with tempfile.TemporaryDirectory(prefix="vs_workflow_path_") as tmp:
        root, plan = _fixture(tmp)
        plan["packets"][0]["path"] = "../outside.md"
        _write_json(root / "note_plan.json", plan)
        _expect_value_error(lambda: note_workflow.prepare(root))


def test_prepare_rejects_unsafe_packet_identifiers():
    with tempfile.TemporaryDirectory(prefix="vs_workflow_id_") as tmp:
        root, plan = _fixture(tmp)
        plan["packets"][0]["id"] = "../../escape"
        _write_json(root / "note_plan.json", plan)
        _expect_value_error(lambda: note_workflow.prepare(root))


def test_assemble_keeps_original_book_and_preserves_packet_order():
    with tempfile.TemporaryDirectory(prefix="vs_workflow_assemble_") as tmp:
        root, plan = _fixture(tmp)
        book = root / "book.md"
        book.write_text("原有正式笔记，不得覆盖。", encoding="utf-8")
        original = book.read_bytes()
        _complete_jobs(root, plan)
        note_workflow.assemble(root)
        assert book.read_bytes() == original
        merged = (root / "book.assembled.md").read_text(encoding="utf-8")
        assert merged.index("P001 的完整讲解") < merged.index("P002 的完整讲解")
        review = _read_json(root / "note_review.assembled.json")
        assert {entry["packet_id"] for entry in review["coverage"]} == {"P001", "P002"}
        assert (root / "assembly_summary.md").is_file()


def test_assemble_rejects_stale_review_and_keeps_previous_assembly():
    with tempfile.TemporaryDirectory(prefix="vs_workflow_review_stale_") as tmp:
        root, plan = _fixture(tmp)
        _complete_jobs(root, plan)
        prior = root / "book.assembled.md"
        prior.write_text("上次完整合稿", encoding="utf-8")
        review_path = root / "jobs/P001/review.json"
        review = _read_json(review_path)
        review["source_sha256"] = "0" * 64
        _write_json(review_path, review)
        _expect_value_error(lambda: note_workflow.assemble(root))
        assert prior.read_text(encoding="utf-8") == "上次完整合稿"


def test_assemble_requires_local_heading_not_another_packets_heading():
    with tempfile.TemporaryDirectory(prefix="vs_workflow_heading_") as tmp:
        root, plan = _fixture(tmp)
        _complete_jobs(root, plan)
        review_path = root / "jobs/P001/review.json"
        review = _read_json(review_path)
        review["coverage"][0]["section"] = "讲解 P002"
        _write_json(review_path, review)
        _expect_value_error(lambda: note_workflow.assemble(root))


def test_assemble_rejects_range_crossing_packet_boundary():
    with tempfile.TemporaryDirectory(prefix="vs_workflow_boundary_") as tmp:
        root, plan = _fixture(tmp)
        _complete_jobs(root, plan)
        review_path = root / "jobs/P001/review.json"
        review = _read_json(review_path)
        entry = review["coverage"][0]
        entry.pop("source_segments", None)
        entry["source_ranges"] = [["S000001", "S000003"]]
        _write_json(review_path, review)
        _expect_value_error(lambda: note_workflow.assemble(root))


def test_assemble_rejects_repeated_coverage_and_missing_source():
    for defect in ("duplicate", "missing"):
        with tempfile.TemporaryDirectory(prefix="vs_workflow_coverage_") as tmp:
            root, plan = _fixture(tmp)
            _complete_jobs(root, plan)
            review_path = root / "jobs/P001/review.json"
            review = _read_json(review_path)
            if defect == "duplicate":
                review["coverage"].append(copy.deepcopy(review["coverage"][0]))
            else:
                entry = review["coverage"][0]
                entry.pop("source_segments", None)
                entry["source_ranges"] = [["S000001", "S000001"]]
            _write_json(review_path, review)
            _expect_value_error(lambda: note_workflow.assemble(root))


def test_assemble_resolves_section_id_inside_the_owning_draft():
    with tempfile.TemporaryDirectory(prefix="vs_workflow_section_id_") as tmp:
        root, plan = _fixture(tmp)
        _complete_jobs(root, plan)
        for packet in plan["packets"]:
            job = root / "jobs" / packet["id"]
            section_id = packet["id"] + ".s01"
            (job / "draft.md").write_text(
                f"<!-- section:{section_id} -->\n## 重复的小标题\n\n该包的例子与推导。\n\n**本章小结**\n\n明确例子的输入、结果与推导条件。\n",
                encoding="utf-8")
            review = _read_json(job / "review.json")
            entry = review["coverage"][0]
            entry.pop("section", None)
            entry["section_id"] = section_id
            _write_json(job / "review.json", review)
        note_workflow.assemble(root)
        review = _read_json(root / "note_review.assembled.json")
        assert all(entry.get("section") for entry in review["coverage"])


def _pending_visual(root, plan):
    _complete_jobs(root, plan)
    review_path = root / "jobs/P001/review.json"
    review = _read_json(review_path)
    review["visual_requests"] = [{"time": "00:00:10", "status": "pending",
                                  "reason": "核查程序运行结果", "section": "讲解 P001"}]
    review["visual_review"] = "pending"
    review["visual_review_reason"] = "需要核查对应画面。"
    _write_json(review_path, review)
    draft = root / "jobs/P001/draft.md"
    draft.write_text(draft.read_text(encoding="utf-8") + "\n![待核结果](SCREENSHOT:00:00:10)\n", encoding="utf-8")
    note_workflow.assemble(root)


def test_missing_frame_does_not_make_visual_review_complete():
    with tempfile.TemporaryDirectory(prefix="vs_workflow_no_frame_") as tmp:
        root, plan = _fixture(tmp)
        _pending_visual(root, plan)
        note_workflow.prepare_visual(root)
        note_workflow.apply_visual(root)
        review = _read_json(root / "note_review.assembled.json")
        assert review["visual_review"] == "pending"
        assert review["visual_requests"][0]["status"] == "pending"


def test_visual_confirmation_requires_current_image_sha():
    with tempfile.TemporaryDirectory(prefix="vs_workflow_image_sha_") as tmp:
        root, plan = _fixture(tmp)
        _pending_visual(root, plan)
        image = root / "images/shot_00_00_10.png"
        image.parent.mkdir()
        write_png(str(image), still_image())
        note_workflow.prepare_visual(root)
        jobs = sorted((root / "visual_jobs").glob("*/job.json"))
        assert len(jobs) == 1
        confirmation = {"source_sha256": plan["source_sha256"], "reason": "检查了这一批画面。",
                        "images": [{"time": "00:00:10", "image_sha256": hashlib.sha256(image.read_bytes()).hexdigest(),
                                    "status": "keep", "description": "程序的运行结果", "reason": "正文需要该结果图。",
                                    "uncertainties": []}]}
        _write_json(jobs[0].parent / "confirmation.json", confirmation)
        image.write_bytes(b"changed after visual confirmation")
        _expect_value_error(lambda: note_workflow.apply_visual(root))
        note_workflow.prepare_visual(root)
        note_workflow.apply_visual(root)
        review = _read_json(root / "note_review.assembled.json")
        assert review["visual_review"] == "pending"
        assert review["visual_requests"][0]["status"] == "pending"


def test_failed_runner_is_recorded_without_printing_raw_model_output():
    with tempfile.TemporaryDirectory(prefix="vs_workflow_runner_") as tmp:
        root, _ = _fixture(tmp)
        note_workflow.prepare(root)
        calls = []
        secret = "PRIVATE_MODEL_OUTPUT_SHOULD_NOT_BE_LOGGED"

        def fake_runner(args, **kwargs):
            calls.append((args, kwargs))
            return subprocess.CompletedProcess(args, 7, stdout=secret, stderr=secret)

        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            _expect_value_error(lambda: note_workflow.run_one(root, job="jobs/P001", runner=fake_runner))
        result = _read_json(root / "jobs/P001/run.json")
        assert result["status"] == "failed", result
        assert len(calls) == 1
        args, kwargs = calls[0]
        assert "-p" in args
        assert "--resume" not in args and "--continue" not in args
        assert kwargs["capture_output"] is True
        assert secret not in output.getvalue()
        assert secret not in json.dumps(result)
        assert not (root / "jobs/P001/review.json").exists()


def test_runner_job_path_cannot_escape_video_directory():
    with tempfile.TemporaryDirectory(prefix="vs_workflow_runner_path_") as tmp:
        root, _ = _fixture(tmp)
        note_workflow.prepare(root)
        def forbidden_runner(*args, **kwargs):
            raise AssertionError("Runner must not start for an unsafe job path")
        _expect_value_error(lambda: note_workflow.run_one(root, job="../outside", runner=forbidden_runner))


def test_visual_cache_reuses_confirmed_frame_but_invalidates_changed_pixels():
    with tempfile.TemporaryDirectory(prefix="vs_workflow_cache_") as tmp:
        root, plan = _fixture(tmp)
        _pending_visual(root, plan)
        image = root / "images/shot_00_00_10.png"
        image.parent.mkdir()
        write_png(str(image), still_image())
        first = note_workflow.prepare_visual(root)
        job = (root / first["tasks"][0]).parent
        _write_json(job / "confirmation.json", {
            "source_sha256": plan["source_sha256"], "reason": "已检查对应画面。",
            "images": [{"time": "00:00:10", "image_sha256": hashlib.sha256(image.read_bytes()).hexdigest(),
                        "status": "keep", "description": "运行结果", "reason": "展示例子的结果。", "uncertainties": []}]})
        note_workflow.apply_visual(root)
        review = _read_json(root / "note_review.assembled.json")
        assert review["visual_requests"][0]["status"] == "captured"
        assert "images/shot_00_00_10.png" in (root / "book.assembled.md").read_text(encoding="utf-8")
        cached = _read_json(root / "visual_confirmations.json")
        assert cached["entries"][0]["image_sha256"] == hashlib.sha256(image.read_bytes()).hexdigest()
        note_workflow.assemble(root)
        reused = note_workflow.prepare_visual(root)
        assert reused["tasks"] == []
        assert reused["reusable_confirmations"] == 1
        note_workflow.apply_visual(root)
        assert _read_json(root / "note_review.assembled.json")["visual_requests"][0]["status"] == "captured"
        write_png(str(image), still_image(seed=19))
        note_workflow.assemble(root)
        changed = note_workflow.prepare_visual(root)
        assert len(changed["tasks"]) == 1
        assert changed["reusable_confirmations"] == 0
        note_workflow.apply_visual(root)
        assert _read_json(root / "note_review.assembled.json")["visual_requests"][0]["status"] == "pending"


def test_runner_produced_is_not_automatic_validation_and_keeps_usage_only():
    with tempfile.TemporaryDirectory(prefix="vs_workflow_produced_") as tmp:
        root, _ = _fixture(tmp)
        note_workflow.prepare(root)
        usage = {"input_tokens": 120, "output_tokens": 30,
                 "cache_read_input_tokens": 90, "cache_creation_input_tokens": 0}
        calls = []
        secret = "PRIVATE_RESPONSE_TEXT"
        def fake_runner(args, **kwargs):
            calls.append(args)
            target = Path(kwargs["cwd"])
            (target / "draft.md").write_text("## 初稿\n\n尚未核查的内容。", encoding="utf-8")
            _write_json(target / "review.json", _read_json(target / "review.template.json"))
            response = {"is_error": False, "usage": usage, "total_cost_usd": 0.01,
                        "num_turns": 2, "result": secret}
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps(response), stderr=secret)
        result = note_workflow.run_one(root, job="jobs/P001", runner=fake_runner)
        assert result["status"] == "produced"
        assert result["usage"] == usage
        assert result["num_turns"] == 2
        assert secret not in json.dumps(result)
        assert not (root / "book.md").exists()
        assert _read_json(root / "jobs/P001/review.json")["coverage"][0]["status"] == "pending"
        _expect_value_error(lambda: note_workflow.run_one(root, job="jobs/P001", runner=fake_runner))
        assert len(calls) == 1


def test_runner_success_without_required_files_fails_and_retains_usage():
    with tempfile.TemporaryDirectory(prefix="vs_workflow_missing_outputs_") as tmp:
        root, _ = _fixture(tmp)
        note_workflow.prepare(root)
        usage = {"input_tokens": 120, "output_tokens": 30}
        def fake_runner(args, **kwargs):
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps({
                "usage": usage, "num_turns": 8, "total_cost_usd": 0.02}), stderr="")
        _expect_value_error(lambda: note_workflow.run_one(root, job="jobs/P001", runner=fake_runner))
        state = _read_json(root / "jobs/P001/run.json")
        assert state["status"] == "failed"
        assert state["usage"] == usage
        assert state["num_turns"] == 8


def test_runner_attempt_budget_prevents_an_extra_model_call():
    with tempfile.TemporaryDirectory(prefix="vs_workflow_attempts_") as tmp:
        root, _ = _fixture(tmp)
        note_workflow.prepare(root)
        calls = []
        def fake_runner(args, **kwargs):
            calls.append(args)
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="")
        for _ in range(3):
            _expect_value_error(lambda: note_workflow.run_one(root, job="jobs/P001", max_attempts=2, runner=fake_runner))
        assert len(calls) == 2
        state = _read_json(root / "jobs/P001/run.json")
        assert state["attempts"] == 2
        assert len(state["history"]) == 2
        assert all(entry["status"] == "failed" for entry in state["history"])


def test_confirming_one_frame_cannot_complete_another_unreviewed_packet():
    with tempfile.TemporaryDirectory(prefix="vs_workflow_visual_scope_") as tmp:
        root, plan = _fixture(tmp)
        _pending_visual(root, plan)
        second_path = root / "jobs/P002/review.json"
        second = _read_json(second_path)
        second["visual_review"] = "pending"
        second["visual_review_reason"] = "本包画面尚未检查，不能判断是否需要额外配图。"
        _write_json(second_path, second)
        note_workflow.assemble(root)
        image = root / "images/shot_00_00_10.png"
        image.parent.mkdir()
        write_png(str(image), still_image())
        prepared = note_workflow.prepare_visual(root)
        job = (root / prepared["tasks"][0]).parent
        _write_json(job / "confirmation.json", {
            "source_sha256": plan["source_sha256"], "reason": "确认本批画面。",
            "images": [{"time": "00:00:10", "image_sha256": hashlib.sha256(image.read_bytes()).hexdigest(),
                        "status": "keep", "description": "运行结果", "reason": "保留结果画面。", "uncertainties": []}]})
        note_workflow.apply_visual(root)
        review = _read_json(root / "note_review.assembled.json")
        assert review["visual_requests"][0]["status"] == "captured"
        assert review["visual_review"] == "pending"


def test_usage_history_keeps_failed_attempt_and_filters_non_counter_fields():
    with tempfile.TemporaryDirectory(prefix="vs_workflow_usage_history_") as tmp:
        root, _ = _fixture(tmp)
        note_workflow.prepare(root)
        calls = []
        secret = "DO_NOT_STORE_THIS_RESPONSE_OR_USAGE_STRING"
        def fake_runner(args, **kwargs):
            calls.append(args)
            if len(calls) == 2:
                target = Path(kwargs["cwd"])
                (target / "draft.md").write_text("## 初稿\n正文。", encoding="utf-8")
                _write_json(target / "review.json", _read_json(target / "review.template.json"))
            response = {"usage": {"input_tokens": 100 if len(calls) == 1 else 20,
                                  "output_tokens": 5, "unknown_field": secret,
                                  "input_tokens_details": {"cached_tokens": 10, "secret": secret}},
                        "result": secret, "num_turns": 1}
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps(response), stderr=secret)
        _expect_value_error(lambda: note_workflow.run_one(root, job="jobs/P001", runner=fake_runner))
        result = note_workflow.run_one(root, job="jobs/P001", runner=fake_runner)
        assert result["status"] == "produced"
        history = result["history"]
        assert [entry["status"] for entry in history] == ["failed", "produced"]
        assert sum(entry["usage"]["input_tokens"] for entry in history) == 120
        assert history[0]["usage"]["input_tokens_details"] == {"cached_tokens": 10}
        assert secret not in json.dumps(result)
        assert secret not in (root / "jobs/P001/run.json").read_text(encoding="utf-8")


def _confirmed_visual(root, plan, status):
    _pending_visual(root, plan)
    image = root / "images/shot_00_00_10.png"
    image.parent.mkdir()
    write_png(str(image), still_image())
    prepared = note_workflow.prepare_visual(root)
    job = (root / prepared["tasks"][0]).parent
    _write_json(job / "confirmation.json", {
        "source_sha256": plan["source_sha256"], "reason": "已检查该画面。",
        "images": [{"time": "00:00:10", "image_sha256": hashlib.sha256(image.read_bytes()).hexdigest(),
                    "status": status, "description": "运行结果", "reason": "确认保留。" if status == "keep" else "无新增教学信息。",
                    "uncertainties": []}]})
    note_workflow.apply_visual(root)
    assert _read_json(root / "note_review.assembled.json")["visual_review"] == "complete"
    return image


def test_changed_visual_purpose_requires_a_new_confirmation():
    with tempfile.TemporaryDirectory(prefix="vs_workflow_purpose_") as tmp:
        root, plan = _fixture(tmp)
        image = _confirmed_visual(root, plan, "keep")
        original_sha = hashlib.sha256(image.read_bytes()).hexdigest()
        review_path = root / "jobs/P001/review.json"
        review = _read_json(review_path)
        review["visual_requests"][0]["reason"] = "改为核查报错位置，原来的运行结果用途不适用。"
        _write_json(review_path, review)
        note_workflow.assemble(root)
        prepared = note_workflow.prepare_visual(root)
        assert prepared["reusable_confirmations"] == 0
        assert len(prepared["tasks"]) == 1
        note_workflow.apply_visual(root)
        assembled = _read_json(root / "note_review.assembled.json")
        assert assembled["visual_review"] == "pending"
        assert assembled["visual_requests"][0]["status"] == "pending"
        assert hashlib.sha256(image.read_bytes()).hexdigest() == original_sha


def test_omitted_visual_becomes_pending_after_frame_replacement():
    with tempfile.TemporaryDirectory(prefix="vs_workflow_omit_invalidated_") as tmp:
        root, plan = _fixture(tmp)
        image = _confirmed_visual(root, plan, "omit")
        book = root / "book.assembled.md"
        assert "<!-- visual-omitted:00:00:10 -->" in book.read_text(encoding="utf-8")
        write_png(str(image), still_image(seed=33))
        prepared = note_workflow.prepare_visual(root)
        assert prepared["reusable_confirmations"] == 0
        assert len(prepared["tasks"]) == 1
        review = _read_json(root / "note_review.assembled.json")
        assert review["visual_requests"][0]["status"] == "pending"
        assert review["visual_review"] == "pending"
        note_workflow.apply_visual(root)
        review = _read_json(root / "note_review.assembled.json")
        assert review["visual_review"] == "pending"
        assert review["visual_requests"][0]["status"] == "pending"
        job = (root / prepared["tasks"][0]).parent
        _write_json(job / "confirmation.json", {
            "source_sha256": plan["source_sha256"], "reason": "核查替换后的新画面。",
            "images": [{"time": "00:00:10", "image_sha256": hashlib.sha256(image.read_bytes()).hexdigest(),
                        "status": "keep", "description": "新运行结果", "reason": "替换画面有新的教学信息。", "uncertainties": []}]})
        note_workflow.apply_visual(root)
        assert "![新运行结果](images/shot_00_00_10.png)" in book.read_text(encoding="utf-8")
        assert _read_json(root / "note_review.assembled.json")["visual_requests"][0]["status"] == "captured"


def test_visual_evidence_cannot_drop_duplicate_or_coerce_unreviewed_packets():
    for defect in ("missing", "duplicate", "non_boolean"):
        with tempfile.TemporaryDirectory(prefix="vs_workflow_visual_evidence_") as tmp:
            root, plan = _fixture(tmp)
            _pending_visual(root, plan)
            second_path = root / "jobs/P002/review.json"
            second = _read_json(second_path)
            second["visual_review"] = "pending"
            second["visual_review_reason"] = "本包尚未检查画面。"
            _write_json(second_path, second)
            note_workflow.assemble(root)
            assembled_path = root / "note_review.assembled.json"
            review = _read_json(assembled_path)
            assert len(review["visual_packets"]) == 2
            if defect == "missing":
                review["visual_packets"] = [review["visual_packets"][0]]
            elif defect == "duplicate":
                review["visual_packets"] = [copy.deepcopy(review["visual_packets"][0]) for _ in range(2)]
            else:
                review["visual_packets"][1]["reviewed"] = "true"
            review["visual_review"] = "complete"
            _write_json(assembled_path, review)
            original = assembled_path.read_bytes()
            _expect_value_error(lambda: note_workflow.prepare_visual(root))
            _expect_value_error(lambda: note_workflow.apply_visual(root))
            assert assembled_path.read_bytes() == original


def test_visual_packet_times_must_match_actual_requests_with_multiplicity():
    for defect in ("missing_time", "extra_time", "duplicate_time"):
        with tempfile.TemporaryDirectory(prefix="vs_workflow_visual_times_") as tmp:
            root, plan = _fixture(tmp)
            _pending_visual(root, plan)
            assembled_path = root / "note_review.assembled.json"
            review = _read_json(assembled_path)
            packets = {packet["packet_id"]: packet for packet in review["visual_packets"]}
            assert packets["P001"]["request_times"] == ["00:00:10"]
            if defect == "missing_time":
                packets["P001"]["request_times"] = []
            elif defect == "extra_time":
                packets["P002"]["request_times"] = ["00:00:30"]
            else:
                packets["P001"]["request_times"] = ["00:00:10", "00:00:10"]
            review["visual_review"] = "complete"
            _write_json(assembled_path, review)
            _expect_value_error(lambda: note_workflow.prepare_visual(root))
            _expect_value_error(lambda: note_workflow.apply_visual(root))


def test_new_editorial_template_stays_pending_until_fact_review_is_done():
    with tempfile.TemporaryDirectory(prefix="vs_workflow_editorial_pending_") as tmp:
        root, plan = _fixture(tmp)
        _complete_jobs(root, plan)
        job = root / "jobs/P001"
        template = _read_json(job / "review.template.json")
        assert template["editorial_policy_version"] == 1
        assert template["fact_review"] == "pending"
        assert template["fact_review_reason"] == ""
        assert template["corrections"] == []
        assert _read_json(job / "job.json")["editorial_policy_version"] == 1
        for entry in template["coverage"]:
            entry["status"] = "written"
            entry["section"] = "讲解 P001"
        template["visual_review"] = "complete"
        template["visual_review_reason"] = "无需额外配图。"
        _write_json(job / "review.json", template)
        _expect_value_error(lambda: note_workflow.assemble(root))


def test_full_sentence_coverage_does_not_replace_a_chapter_summary():
    with tempfile.TemporaryDirectory(prefix="vs_workflow_chapter_summary_") as tmp:
        root, plan = _fixture(tmp)
        _complete_jobs(root, plan)
        draft = root / "jobs/P001/draft.md"
        draft.write_text("## 讲解 P001\n\nP001 的完整讲解。\n", encoding="utf-8")
        _expect_value_error(lambda: note_workflow.assemble(root))


def test_pure_advertisement_can_be_omitted_with_a_traceable_reason():
    with tempfile.TemporaryDirectory(prefix="vs_workflow_advertisement_") as tmp:
        root, _ = _fixture(tmp)
        source = _read_json(root / "transcript.json")
        advertisement = "点击推广链接购买赞助商会员，领取专属优惠券。"
        source["segments"][0]["text"] = advertisement
        _write_json(root / "transcript.json", source)
        plan = note_plan.generate(root, video_id="BVworkflow", force=True)
        _complete_jobs(root, plan)
        review_path = root / "jobs/P001/review.json"
        review = _read_json(review_path)
        review["coverage"] = [
            {"packet_id": "P001", "source_ranges": [["S000001", "S000001"]], "status": "omitted",
             "section": "", "reason": "纯广告：赞助推广与优惠券信息，不含课程知识。"},
            {"packet_id": "P001", "source_ranges": [["S000002", "S000002"]], "status": "written",
             "section": "讲解 P001", "reason": ""}]
        _write_json(review_path, review)
        note_workflow.assemble(root)
        merged = (root / "book.assembled.md").read_text(encoding="utf-8")
        assert advertisement not in merged
        assert "P001 的完整讲解" in merged
        assembled = _read_json(root / "note_review.assembled.json")
        omitted = [entry for entry in assembled["coverage"] if entry["status"] == "omitted"]
        assert len(omitted) == 1
        assert omitted[0]["source_ranges"] == [["S000001", "S000001"]]
        assert "纯广告" in omitted[0]["reason"]


def _verified_correction(root):
    root, _ = _fixture(root)
    source = _read_json(root / "transcript.json")
    source["segments"][0]["text"] = "二加二等于五"
    _write_json(root / "transcript.json", source)
    plan = note_plan.generate(root, video_id="BVworkflow", force=True)
    _complete_jobs(root, plan)
    job = root / "jobs/P001"
    display = "**勘误**：原说法“二加二等于五”应改为“二加二等于四”；整数计算 2 + 2 = 4。"
    correction = {"kind": "lecture_error", "status": "verified", "section_id": "P001.s01",
                  "source_ranges": [["S000001", "S000001"]], "original": "二加二等于五",
                  "replacement": "二加二等于四", "display": display,
                  "evidence": [{"kind": "calculation", "detail": "Python 整数计算 2 + 2 的结果是 4。"}]}
    assert 2 + 2 == 4
    (job / "draft.md").write_text(
        f"<!-- section:P001.s01 -->\n## 数值计算\n\n{display}\n\n**本章小结**\n\n整数加法按给定条件计算，二加二等于四。\n",
        encoding="utf-8")
    review = _read_json(job / "review.json")
    for entry in review["coverage"]:
        entry.pop("section", None)
        entry["section_id"] = "P001.s01"
    review["fact_review_reason"] = "检查原句中的加法结果，使用整数计算纠正错误；其余操作条件未发现异常。"
    review["corrections"] = [correction]
    _write_json(job / "review.json", review)
    return root, plan, correction


def test_verified_correction_is_mapped_to_numbered_section_with_evidence():
    with tempfile.TemporaryDirectory(prefix="vs_workflow_correction_") as tmp:
        root, _, correction = _verified_correction(tmp)
        note_workflow.assemble(root)
        merged = (root / "book.assembled.md").read_text(encoding="utf-8")
        assert correction["display"] in merged
        review = _read_json(root / "note_review.assembled.json")
        assert review["fact_review"] == "complete"
        assert len(review["corrections"]) == 1
        assembled = review["corrections"][0]
        assert assembled["packet_id"] == "P001"
        assert assembled["section"] == "一、数值计算"
        assert assembled["source_ranges"] == [["S000001", "S000001"]]
        for key in ("kind", "status", "original", "replacement", "display", "evidence"):
            assert assembled[key] == correction[key]


def test_verified_correction_cannot_cross_packet_or_lack_visible_evidence():
    for defect in ("cross_packet", "missing_evidence", "missing_display", "other_section"):
        with tempfile.TemporaryDirectory(prefix="vs_workflow_invalid_correction_") as tmp:
            root, _, _ = _verified_correction(tmp)
            review_path = root / "jobs/P001/review.json"
            review = _read_json(review_path)
            correction = review["corrections"][0]
            if defect == "cross_packet":
                correction["source_ranges"] = [["S000001", "S000003"]]
            elif defect == "missing_evidence":
                correction["evidence"] = []
            elif defect == "missing_display":
                correction["display"] = "**勘误**：这个说明没有出现在正文中。"
            else:
                correction["section_id"] = "P002.s01"
            _write_json(review_path, review)
            _expect_value_error(lambda: note_workflow.assemble(root))


def test_legacy_jobs_without_editorial_policy_remain_compatible():
    with tempfile.TemporaryDirectory(prefix="vs_workflow_legacy_editorial_") as tmp:
        root, plan = _fixture(tmp)
        _complete_jobs(root, plan)
        for packet in plan["packets"]:
            job = root / "jobs" / packet["id"]
            metadata = _read_json(job / "job.json")
            metadata.pop("editorial_policy_version", None)
            _write_json(job / "job.json", metadata)
            review = _read_json(job / "review.json")
            for key in ("editorial_policy_version", "fact_review", "fact_review_reason", "corrections"):
                review.pop(key, None)
            _write_json(job / "review.json", review)
            (job / "draft.md").write_text(f"## 讲解 {packet['id']}\n\n旧版 {packet['id']} 正文。\n", encoding="utf-8")
        note_workflow.assemble(root)
        merged = (root / "book.assembled.md").read_text(encoding="utf-8")
        assert "旧版 P001 正文" in merged and "旧版 P002 正文" in merged


def test_unresolved_correction_keeps_public_marker_without_inventing_replacement():
    with tempfile.TemporaryDirectory(prefix="vs_workflow_unresolved_correction_") as tmp:
        root, _, old_correction = _verified_correction(tmp)
        job = root / "jobs/P001"
        review = _read_json(job / "review.json")
        correction = review["corrections"][0]
        display = "**待核实**：原说法“二加二等于五”的上下文有歧义，尚未确认原意。"
        correction.update(status="unresolved", replacement="", evidence=[], display=display)
        review["fact_review_reason"] = "执行了异常核查，但上下文歧义未消除，公开保留待核标记。"
        _write_json(job / "review.json", review)
        draft = job / "draft.md"
        draft.write_text(draft.read_text(encoding="utf-8").replace(old_correction["display"], display), encoding="utf-8")
        note_workflow.assemble(root)
        merged = (root / "book.assembled.md").read_text(encoding="utf-8")
        assert display in merged
        assembled = _read_json(root / "note_review.assembled.json")["corrections"][0]
        assert assembled["status"] == "unresolved"
        assert assembled["replacement"] == ""
        assert assembled["evidence"] == []


def test_inline_runner_receives_full_owned_material_and_inspection_never_invokes_model():
    with tempfile.TemporaryDirectory(prefix="vs_workflow_inline_") as tmp:
        root, plan = _fixture(tmp)
        (root / "TERMS.confirmed.md").write_text("确认术语：向量场。", encoding="utf-8")
        note_workflow.prepare(root)
        summary = note_workflow.inspect_job(root, "jobs/P001")
        assert summary["embedded_inputs"] == 4
        assert not (root / "jobs/P001/run.json").exists()
        def fake_runner(args, **kwargs):
            text = kwargs["input"]
            paths = [note_workflow.BASE_DIR / "prompts/writer_system.md", root / "TERMS.confirmed.md",
                     root / plan["packets"][0]["path"], root / "jobs/P001/review.template.json"]
            assert all(text.count(path.read_text(encoding="utf-8")) == 1 for path in paths)
            assert (root / plan["packets"][1]["path"]).read_text(encoding="utf-8") not in text
            assert text.index("--- writer_system.md ---") < text.index("# 独立写作任务 P001")
            assert len(text) == summary["prompt_chars"]
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="")
        _expect_value_error(lambda: note_workflow.run_one(root, "jobs/P001", runner=fake_runner))
        state = _read_json(root / "jobs/P001/run.json")
        assert state["input_mode"] == "inline" and state["embedded_inputs"] == 4
        assert state["elapsed_seconds"] >= 0


def test_changed_template_is_rejected_before_a_model_call():
    with tempfile.TemporaryDirectory(prefix="vs_workflow_template_") as tmp:
        root, _ = _fixture(tmp)
        note_workflow.prepare(root)
        path = root / "jobs/P001/review.template.json"
        path.write_text('{"changed": true}', encoding="utf-8")
        def forbidden_runner(*args, **kwargs):
            raise AssertionError("Stale template consumed a model call")
        _expect_value_error(lambda: note_workflow.run_one(root, "jobs/P001", runner=forbidden_runner))
        assert not (root / "jobs/P001/run.json").exists()


def test_files_mode_remains_available_without_embedding_packet():
    with tempfile.TemporaryDirectory(prefix="vs_workflow_files_") as tmp:
        root, plan = _fixture(tmp)
        note_workflow.prepare(root)
        summary = note_workflow.inspect_job(root, "jobs/P001", input_mode="files")
        assert summary["embedded_inputs"] == 0
        def fake_runner(args, **kwargs):
            assert (root / plan["packets"][0]["path"]).read_text(encoding="utf-8") not in kwargs["input"]
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="")
        _expect_value_error(lambda: note_workflow.run_one(root, "jobs/P001", input_mode="files", runner=fake_runner))


def test_cross_packet_chapter_is_checked_only_at_actual_ending():
    with tempfile.TemporaryDirectory(prefix="vs_workflow_continuation_") as tmp:
        root, plan = _fixture(tmp)
        _complete_jobs(root, plan)
        first = root / "jobs/P001"
        (first / "draft.md").write_text("## 连续推导\n\n前半段的条件与推导。\n", encoding="utf-8")
        review = _read_json(first / "review.json")
        review["coverage"][0]["section"] = "连续推导"
        review["boundary_notes"] = ["本章推导尚未结束，下一包接续并在真实结尾总结。"]
        _write_json(first / "review.json", review)
        second = root / "jobs/P002"
        (second / "draft.md").write_text(
            "### 继续验证\n\n后半段推导与结果。\n\n**本章小结**：结合前提验证推导结果，并检查适用条件。\n", encoding="utf-8")
        review = _read_json(second / "review.json")
        review["coverage"][0]["section"] = "继续验证"
        _write_json(second / "review.json", review)
        note_workflow.assemble(root)
        merged = (root / "book.assembled.md").read_text(encoding="utf-8")
        assert merged.count("**本章小结**") == 1
        assert "### 1.1 继续验证" in merged


def test_model_failure_records_safe_reason_and_usage_without_raw_errors():
    with tempfile.TemporaryDirectory(prefix="vs_workflow_failure_code_") as tmp:
        root, _ = _fixture(tmp)
        note_workflow.prepare(root)
        def fake_runner(args, **kwargs):
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps({
                "is_error": True, "subtype": "error_max_turns", "num_turns": 9,
                "errors": ["PRIVATE_ERROR_DETAILS"], "usage": {"output_tokens": 123}}), stderr="")
        _expect_value_error(lambda: note_workflow.run_one(root, "jobs/P001", runner=fake_runner))
        state = _read_json(root / "jobs/P001/run.json")
        assert state["failure_code"] == "turn_limit" and state["max_turns"] == 8
        assert state["usage"]["output_tokens"] == 123
        assert "PRIVATE_ERROR_DETAILS" not in json.dumps(state)
