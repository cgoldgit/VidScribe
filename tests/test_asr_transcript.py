"""asr_transcript.py 的回归测试（全离线：不联网、不下载、不加载模型）。

对应 2026-09-22 code review 报告里的这两条：
  · 多分P 的 p7 会退化成第 1 集，而且结果写进 output/<BV>/（流水线等的是 _p7）
  · 试跑会删掉正式断点：试听 3 分钟就把正式进度清零；试跑后跑全量还会
    从试跑区间的末尾续跑，静默跳过试跑区间之前的所有内容

不碰 output/ 与 .capture-profile/：一律用临时目录，目录名的用例临时把
config.OUTPUT_DIR 指到临时目录（get_video_dir 会建目录，不能让它建到真 output/）。
"""
import json
import os
import shutil
import tempfile

from _helpers import SRC  # noqa: F401  (SRC 保证能 import src/)

import config
import asr_transcript as asr


def _tmpdir():
    return tempfile.mkdtemp(prefix="vs_test_")


def _write_rows(path, rows):
    """按脚本真实的落盘格式写进度 jsonl。"""
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _rows(n, step=10.0):
    """n 段，首尾相接：第 i 段的 end 就是 i*step（n=5 → 末段 end=50.0）。"""
    return [{"start": (i - 1) * step, "end": i * step, "text": f"第{i}段"}
            for i in range(1, n + 1)]


# ───────────────────────── 视频 id 与 URL（缺陷 1） ─────────────────────────

def test_norm_id_keeps_part_suffix():
    """三种输入形态都要保住分P，否则 p7 会退化成第 1 集。"""
    assert asr.norm_id("BV1dr4y1n7vA") == "BV1dr4y1n7vA"
    assert asr.norm_id("https://www.bilibili.com/video/BV1dr4y1n7vA?p=7") == "BV1dr4y1n7vA_p7"
    assert asr.norm_id("BV1dr4y1n7vA_p7") == "BV1dr4y1n7vA_p7"
    # 带尾巴的表单也要认（三要素 ?p= 与 yt-dlp 的 id 拼法一致）
    assert asr.norm_id("https://www.bilibili.com/video/BV1dr4y1n7vA/?p=7") == "BV1dr4y1n7vA_p7"
    assert asr.norm_id("https://www.bilibili.com/video/BV1dr4y1n7vA?spm_id_from=x&p=12") \
        == "BV1dr4y1n7vA_p12"
    # 前导零按数字归一（yt-dlp 拼的是 _p%d，_p07 与 _p7 是同一集）
    assert asr.norm_id("BV1dr4y1n7vA_p07") == "BV1dr4y1n7vA_p7"
    # 显式写了 p=1 就保留，与 batch_transcripts 产出的 BV..._p1 对齐
    assert asr.norm_id("https://www.bilibili.com/video/BV1dr4y1n7vA?p=1") == "BV1dr4y1n7vA_p1"
    # YouTube 不受影响
    assert asr.norm_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ") == "dQw4w9WgXcQ"


def test_video_url_of_points_at_that_episode():
    assert asr.video_url_of("BV1dr4y1n7vA") == "https://www.bilibili.com/video/BV1dr4y1n7vA"
    assert asr.video_url_of("BV1dr4y1n7vA_p7") == "https://www.bilibili.com/video/BV1dr4y1n7vA?p=7"
    assert asr.video_url_of("dQw4w9WgXcQ") == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    # 关键点：从 URL 进来的 p7 转一圈回来还得是 p7
    for arg in ("BV1dr4y1n7vA_p7", "https://www.bilibili.com/video/BV1dr4y1n7vA?p=7"):
        assert asr.video_url_of(asr.norm_id(arg)) == "https://www.bilibili.com/video/BV1dr4y1n7vA?p=7"


def test_output_dir_for_multipart_matches_pipeline():
    """多分P 要落在 output/<BV>_p7/ —— 与 dump_transcript / sprite_index /
    capture_frames / merge_book 的约定一致，否则 AI 去那里找不到稿件。"""
    d = _tmpdir()
    old = config.OUTPUT_DIR
    config.OUTPUT_DIR = d
    try:
        for arg in ("BV1dr4y1n7vA_p7",
                    "https://www.bilibili.com/video/BV1dr4y1n7vA?p=7"):
            vid = asr.norm_id(arg)
            assert os.path.basename(asr.get_video_dir(vid)) == "BV1dr4y1n7vA_p7"
            assert os.path.isdir(os.path.join(d, "BV1dr4y1n7vA_p7"))
        # 单集仍然是原来的目录名：不要凭空多出 _p1 换了目录
        assert os.path.basename(asr.get_video_dir(asr.norm_id("BV1dr4y1n7vA"))) == "BV1dr4y1n7vA"
        # 第 1 集和第 7 集必须是两个目录（旧代码是同一个）
        assert (asr.get_video_dir("BV1dr4y1n7vA")
                != asr.get_video_dir("BV1dr4y1n7vA_p7"))
    finally:
        config.OUTPUT_DIR = old
        shutil.rmtree(d, ignore_errors=True)


# ───────────────────────── 试跑不动正式断点（缺陷 2） ─────────────────────────

def test_sample_mode_does_not_touch_official_progress():
    """预置一份正式断点，跑完试跑模式会做的那些路径后必须逐字节不变。"""
    d = _tmpdir()
    try:
        prog = os.path.join(d, "_asr_progress.jsonl")
        _write_rows(prog, _rows(9))
        before = open(prog, "rb").read()

        path, resume_at = asr.prepare_progress(d, sample_start=300.0, restart=False)
        assert os.path.abspath(path) != os.path.abspath(prog), \
            "试跑不能复用正式断点文件"
        assert resume_at == 0.0, "试跑从 --sample-start 开始，不该读正式进度"
        # 试跑期间会往自己的文件里追加
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"start": 300.0, "end": 302.0, "text": "试跑"},
                               ensure_ascii=False) + "\n")
        assert open(prog, "rb").read() == before, "试跑把正式断点改了/删了"

        # 反复试跑（含带 --restart 的试跑）依旧不许碰正式断点
        asr.prepare_progress(d, sample_start=60.0, restart=False)
        asr.prepare_progress(d, sample_start=60.0, restart=True)
        assert open(prog, "rb").read() == before, "试跑把正式断点改了/删了"
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_sample_then_full_run_does_not_skip_earlier_content():
    """后果 B 的回归：试跑（300~480s）之后跑全量，必须从正式断点末尾续，
    而不是从试跑区间的末尾续——否则试跑区间之前的内容被静默跳过。"""
    d = _tmpdir()
    try:
        prog = os.path.join(d, "_asr_progress.jsonl")
        _write_rows(prog, _rows(5))            # 正式进度到 50s
        asr.prepare_progress(d, sample_start=300.0, restart=False)
        with open(os.path.join(d, "_asr_sample_progress.jsonl"), "a",
                  encoding="utf-8") as f:
            f.write(json.dumps({"start": 300.0, "end": 480.0, "text": "试跑"},
                               ensure_ascii=False) + "\n")

        path, resume_at = asr.prepare_progress(d, sample_start=None, restart=False)
        assert os.path.abspath(path) == os.path.abspath(prog)
        assert resume_at == 50.0, f"应当从正式断点末尾续跑，实际 {resume_at}"
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_official_resume_and_restart_semantics():
    """回归：正式断点续跑与 --restart 的原语义都不能变。"""
    d = _tmpdir()
    try:
        prog = os.path.join(d, "_asr_progress.jsonl")

        # 首次跑：没有断点 → 从 0 开始
        path, resume_at = asr.prepare_progress(d, sample_start=None, restart=False)
        assert os.path.abspath(path) == os.path.abspath(prog)
        assert resume_at == 0.0 and not os.path.exists(prog)

        # 中断后重跑 → 从末尾续接
        _write_rows(prog, _rows(5))
        path, resume_at = asr.prepare_progress(d, sample_start=None, restart=False)
        assert os.path.abspath(path) == os.path.abspath(prog)
        assert resume_at == 50.0, f"应当从 50s 续跑，实际 {resume_at}"

        # --restart → 丢弃正式进度从头开始
        path, resume_at = asr.prepare_progress(d, sample_start=None, restart=True)
        assert os.path.abspath(path) == os.path.abspath(prog)
        assert resume_at == 0.0
        assert not os.path.exists(prog), "--restart 应当丢掉正式断点"

        # 空的进度文件（上轮崩在半路）也不该炸
        open(prog, "w", encoding="utf-8").close()
        assert asr.prepare_progress(d, sample_start=None, restart=False)[1] == 0.0
    finally:
        shutil.rmtree(d, ignore_errors=True)
