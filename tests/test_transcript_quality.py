"""字幕覆盖率与批量抓取门禁测试（全离线）。"""

import json
import os
import shutil
import tempfile
from types import SimpleNamespace

from _helpers import SRC  # noqa: F401  (SRC 保证能 import src/)

import batch_transcripts as batch
import transcript_quality as quality


def _transcript(end="00:00:50", duration=100, video_id="BVtest_p1"):
    return {
        "video_id": video_id,
        "duration": duration,
        "segments": [{"start": "00:00:00", "end": end, "text": "正文"}],
    }


def test_coverage_boundary_and_millisecond_support():
    assert quality.coverage_ratio(_transcript("00:00:49.999")) == 0.49
    assert not quality.is_usable(_transcript("00:00:49.999"))
    assert quality.coverage_ratio(_transcript("00:00:50.500")) == 0.5
    assert quality.is_usable(_transcript("00:00:50.500"))


def test_missing_duration_is_unknown_not_failure():
    transcript = _transcript(duration=0)
    assert quality.coverage_ratio(transcript) is None
    assert quality.is_usable(transcript)


def test_invalid_end_is_zero_coverage():
    transcript = _transcript(end="坏数据")
    assert quality.coverage_ratio(transcript) == 0
    assert not quality.is_usable(transcript)


def test_existing_file_must_be_readable_and_complete():
    root = tempfile.mkdtemp(prefix="vs_quality_")
    try:
        path = os.path.join(root, "transcript.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(_transcript("00:00:49"), f)
        assert not batch.transcript_file_ready(path)

        with open(path, "w", encoding="utf-8") as f:
            json.dump(_transcript("00:00:50"), f)
        assert batch.transcript_file_ready(path)

        with open(path, "w", encoding="utf-8") as f:
            f.write("{bad json")
        assert not batch.transcript_file_ready(path)

        with open(path, "w", encoding="utf-8") as f:
            json.dump(["valid JSON, wrong shape"], f)
        assert not batch.transcript_file_ready(path)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_batch_rejects_low_coverage_without_overwriting_file():
    root = tempfile.mkdtemp(prefix="vs_batch_quality_")
    old_parse = batch.parse_args
    old_get_dir = batch.get_video_dir
    old_get_transcript = batch.get_transcript
    old_profile = batch.DEFAULT_PROFILE_DIR
    try:
        def get_dir(video_id):
            path = os.path.join(root, video_id)
            os.makedirs(path, exist_ok=True)
            return path

        out_file = os.path.join(get_dir("BVtest_p1"), "transcript.json")
        with open(out_file, "w", encoding="utf-8") as f:
            f.write("previous good file")

        batch.parse_args = lambda: SimpleNamespace(
            bvid="BVtest", start=1, end=1, only="1", force=True, retries=0)
        batch.get_video_dir = get_dir
        batch.get_transcript = lambda *_a, **_k: _transcript("00:00:49")
        batch.DEFAULT_PROFILE_DIR = os.path.join(root, "missing-profile")

        try:
            batch.main()
        except SystemExit as exc:
            assert exc.code == 1
        else:
            raise AssertionError("低覆盖率批量字幕应返回失败")

        assert open(out_file, encoding="utf-8").read() == "previous good file"
    finally:
        batch.parse_args = old_parse
        batch.get_video_dir = old_get_dir
        batch.get_transcript = old_get_transcript
        batch.DEFAULT_PROFILE_DIR = old_profile
        shutil.rmtree(root, ignore_errors=True)


def test_batch_refetches_existing_low_coverage_file():
    root = tempfile.mkdtemp(prefix="vs_batch_existing_")
    old_parse = batch.parse_args
    old_get_dir = batch.get_video_dir
    old_get_transcript = batch.get_transcript
    old_profile = batch.DEFAULT_PROFILE_DIR
    calls = []
    try:
        def get_dir(video_id):
            path = os.path.join(root, video_id)
            os.makedirs(path, exist_ok=True)
            return path

        out_file = os.path.join(get_dir("BVtest_p1"), "transcript.json")
        with open(out_file, "w", encoding="utf-8") as f:
            json.dump(_transcript("00:00:20"), f)

        batch.parse_args = lambda: SimpleNamespace(
            bvid="BVtest", start=1, end=1, only="1", force=False, retries=0)
        batch.get_video_dir = get_dir

        def fetch(*_args, **_kwargs):
            calls.append(True)
            return _transcript("00:01:00")

        batch.get_transcript = fetch
        batch.DEFAULT_PROFILE_DIR = os.path.join(root, "missing-profile")
        batch.main()

        assert len(calls) == 1
        with open(out_file, encoding="utf-8") as f:
            assert quality.coverage_ratio(json.load(f)) == 0.6
    finally:
        batch.parse_args = old_parse
        batch.get_video_dir = old_get_dir
        batch.get_transcript = old_get_transcript
        batch.DEFAULT_PROFILE_DIR = old_profile
        shutil.rmtree(root, ignore_errors=True)
