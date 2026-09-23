"""capture_frames.py 的回归测试（不需要浏览器，全部离线）。

对应 2026-09-22 code review 报告里的这几条：
  · 找帧判据只看「文件是否存在」 → frames_missing 现在看能不能用
  · 黑帧按字节数判会把合法的深色幻灯片误杀 → 改像素判据
  · seek 校验的结果被丢掉、越界时间戳永远报 ok → 硬门禁 + 不留下错图
  · _pick_stable 兜底返回采样窗口最后一拍（≈目标 +6 秒）
  · MM:SS 占位符匹配不上正则被静默跳过
"""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile

import numpy as np

from _helpers import SRC, still_image, write_png  # noqa: F401  (SRC 保证能 import src/)

import capture_frames as cf


def _tmpdir():
    return tempfile.mkdtemp(prefix="vs_test_")


# ───────────────────────── 时间戳与占位符 ─────────────────────────

def test_fmt_ts_is_hh_mm_ss():
    assert cf.fmt_ts(0) == "00:00:00"
    assert cf.fmt_ts(5910) == "01:38:30"
    assert cf.fmt_ts(9.6) == "00:00:10"      # 四舍五入到秒


def test_parse_timestamps_ignores_bad_and_keeps_hms():
    d = _tmpdir()
    try:
        md = os.path.join(d, "book.md")
        with open(md, "w", encoding="utf-8") as f:
            f.write("![a](SCREENSHOT:00:00:08)\n"
                    "![b](SCREENSHOT:01:38:26)\n"
                    "![c](SCREENSHOT:00:00:08.500)\n"     # 带小数：取整到秒
                    "![d](SCREENSHOT:98:26)\n"            # 索引图标签写成的 MM:SS
                    "![e](SCREENSHOT:03:00)\n")
        assert cf.parse_timestamps(md) == ["00:00:08", "01:38:26"]
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_malformed_placeholders_are_reported():
    """格式不对的占位符原先会被静默跳过（不截图也不报错）。"""
    d = _tmpdir()
    try:
        md = os.path.join(d, "book.md")
        with open(md, "w", encoding="utf-8") as f:
            f.write("![a](SCREENSHOT:01:38:26)\n![b](SCREENSHOT:98:26)\n"
                    "![c](SCREENSHOT:03:00)\n")
        bad = cf.malformed_placeholders(md)
        assert bad == ["03:00", "98:26"], bad
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ───────────────────────── 空帧判据（F 层） ─────────────────────────

def _dark_slide(w=1280, h=720):
    """95% 纯黑 + 几行小字的幻灯片：合法画面，但压缩后极小。

    字必须画成**细笔画**，不能拿大色块冒充：实测一行 28px 真字只占 0.39% 的
    像素，而 700×30 的实心白块占 2.3%（5~10 倍）。早先的测试就用了实心块，
    于是「合法的深色幻灯片不会被误杀」这条看着通过、实际测不到东西
    （真实字形下会栽在「第 99 百分位 < 12」那条判据上）。
    """
    img = np.zeros((h, w, 3), dtype=np.uint8)
    rng = np.random.default_rng(11)
    for y in (330, 372, 414):                 # 三行，像一页 PPT 的标题 + 两条要点
        x = 60
        while x < w - 160:
            wd = int(rng.integers(6, 15))
            if rng.random() < 0.45:
                img[y:y + 2, x:x + wd] = 240          # 横笔
            else:
                img[y:y + 10, x:x + 2] = 240          # 竖笔
            x += wd + int(rng.integers(3, 8))
    return img


def _uniform(value=128, w=1280, h=720):
    return np.full((h, w, 3), value, dtype=np.uint8)


def _content_ratio(arr):
    gray = np.asarray(arr, dtype=np.float32).mean(axis=2)
    return float((gray > cf.BLANK_LEVEL).mean())


def test_frame_usable_spares_dark_slide():
    """回归一：按旧的「小于 10000 字节算黑帧」判据，这张正确的画面会被换掉。"""
    d = _tmpdir()
    try:
        p = os.path.join(d, "shot.png")
        slide = _dark_slide()
        write_png(p, slide)
        size = os.path.getsize(p)
        assert size < cf.MIN_SHOT_BYTES, f"这张图居然有 {size} 字节，测不到要点"
        ratio = _content_ratio(slide)
        assert 0.002 < ratio < 0.006, \
            f"测试用的「字」占 {ratio * 100:.2f}% 像素，不像真实小字（实测真字 0.39%）"
        ok, why = cf._frame_usable(p)
        assert ok, f"合法的深色幻灯片被误判成空帧：{why}"
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_blank_criterion_only_flags_frames_with_no_content():
    """回归二（审查发现的真 bug）：判据曾经是「第 99 百分位灰度 < 12」，
    而一行小字永远站不上 99 百分位（要 ≥1% 像素才够）→ 把最该救的画面判成黑帧。"""
    d = _tmpdir()
    try:
        p = os.path.join(d, "shot.png")
        write_png(p, _dark_slide())
        gray = np.asarray(_dark_slide(), dtype=np.float32).mean(axis=2)
        assert float(np.percentile(gray, 99)) < 12, \
            "这张图的 99 分位本来就该是 0 —— 说明旧判据必然误杀它"
        assert cf._frame_usable(p)[0], "有 0.3% 内容的画面必须判合格"

        # 真正的空帧（有内容的像素 ≈ 0）仍然要抓住
        blank = os.path.join(d, "blank.png")
        write_png(blank, np.zeros((720, 1280, 3), dtype=np.uint8))
        ok, why = cf._frame_usable(blank)
        assert not ok and "全黑" in why or "一个颜色" in why, why
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_frame_usable_rejects_blank_uniform_tiny_truncated():
    d = _tmpdir()
    try:
        black = os.path.join(d, "black.png")
        write_png(black, np.zeros((720, 1280, 3), dtype=np.uint8))
        assert not cf._frame_usable(black)[0], "全黑帧应当判不合格"

        flat = os.path.join(d, "flat.png")
        write_png(flat, _uniform(128))
        assert not cf._frame_usable(flat)[0], "整幅单色应当判不合格"

        tiny = os.path.join(d, "tiny.png")
        write_png(tiny, still_image(64, 48))
        assert not cf._frame_usable(tiny)[0], "过小尺寸应当判不合格"

        good = os.path.join(d, "good.png")
        write_png(good, still_image(640, 360))
        assert cf._frame_usable(good)[0], "正常画面应当判合格"

        # 半截文件（上一轮中断留下的）必须不通过
        trunc = os.path.join(d, "trunc.png")
        with open(good, "rb") as f:
            blob = f.read()
        with open(trunc, "wb") as f:
            f.write(blob[:len(blob) // 2])
        assert not cf._frame_usable(trunc)[0], "半截文件应当判不合格"

        empty = os.path.join(d, "empty.png")
        open(empty, "wb").close()
        assert not cf._frame_usable(empty)[0], "空文件应当判不合格"
        assert not cf._frame_usable(os.path.join(d, "nope.png"))[0]
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_frames_missing_treats_unusable_file_as_missing():
    d = _tmpdir()
    try:
        write_png(os.path.join(d, "shot_00_00_10.png"), still_image(640, 360))
        write_png(os.path.join(d, "shot_00_00_20.png"), np.zeros((720, 1280, 3), dtype=np.uint8))
        missing = cf.frames_missing(["00:00:10", "00:00:20", "00:00:30"], d)
        assert missing == ["00:00:20", "00:00:30"], missing
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ───────────────────────── 选时刻（G 层） ─────────────────────────

def _s(t, ink, stable=False, diff=99.0):
    return {"t": t, "ink": ink, "stable": stable, "diff": diff}


def test_pick_stable_prefers_max_ink_among_near_stable():
    samples = [_s(117.0, 0.10, True), _s(119.0, 0.30, True),   # 目标 120
               _s(121.0, 0.20, True),
               _s(128.0, 0.90, True)]      # 信息量最高，但超出 ±STABLE_MAX_DIST
    pick, how = cf._pick_stable(samples, 120.0)
    assert (pick, how) == (119.0, "stable-near")


def test_pick_stable_never_falls_back_to_window_tail():
    """回归：旧版在没有任何稳定采样时返回 samples[-1]（≈目标 +6 秒），
    文件名写着 00:02:00、画面却来自 00:02:06。"""
    samples = [_s(117.0, 0.05), _s(120.0, 0.40), _s(126.0, 0.01)]
    pick, how = cf._pick_stable(samples, 120.0)
    assert how == "unstable"
    assert pick == 120.0, f"应当取信息量最大的近点，实际 {pick}"
    assert abs(pick - 120.0) <= cf.STABLE_MAX_DIST


def test_pick_stable_falls_back_to_nearest_stable():
    samples = [_s(50.0, 0.90, True), _s(90.0, 0.10, True)]
    pick, how = cf._pick_stable(samples, 65.0)     # 两个都超出 ±STABLE_MAX_DIST
    assert how == "stable-far"
    assert pick == 50.0, f"应当退回离目标最近的稳定点，实际 {pick}"


def test_pick_stable_without_samples_returns_target():
    assert cf._pick_stable([], 42.0) == (42.0, "no-samples")


# ───────────────────────── SEEK_JS / SAMPLE_JS 静态检查 ─────────────────────────

def test_seek_js_has_timeouts_and_no_unreplaced_placeholder():
    assert "__READY_MS__" not in cf.SEEK_JS and "__SEEK_MS__" not in cf.SEEK_JS
    assert str(cf.READY_TIMEOUT_MS) in cf.SEEK_JS
    assert str(cf.SEEK_TIMEOUT_MS) in cf.SEEK_JS
    assert "Date.now()" in cf.SEEK_JS                # 两处等待都有墙钟兜底
    assert "v.seeking" in cf.SEEK_JS                 # seek 落定以 seeking 为准
    assert "addEventListener('loadeddata'" not in cf.SEEK_JS, \
        "别等 loadeddata：seek 后 readyState 掉回 1，而它每个加载周期只触发一次"
    assert "v.pause()" in cf.SEEK_JS                 # 截图前先定住画面


def test_sample_js_has_wall_clock_and_ready_state_guard():
    assert "wallMs" in cf.SAMPLE_JS and "Date.now()" in cf.SAMPLE_JS
    assert "readyState >= 2" in cf.SAMPLE_JS         # 缓冲中不取样
    assert "stalled" in cf.SAMPLE_JS


# ───────────────────────── _shoot 控制流（假 page，离线） ─────────────────────────

class _FakeLocator:
    def __init__(self, page):
        self.page = page

    def screenshot(self, path):
        self.page.written.append(path)
        i = min(len(self.page.written) - 1, len(self.page.shot_images) - 1)
        write_png(path, self.page.shot_images[i])


class _FakePage:
    """只实现 _shoot 用到的那几个方法，用来离线跑完整控制流。"""

    def __init__(self, seek_results, sample_result=None, shot_images=None):
        self.seek_results = list(seek_results)
        self.sample_result = sample_result or {"samples": [], "stalled": False, "reason": ""}
        self.shot_images = ([still_image(640, 360)] if shot_images is None
                            else list(shot_images))
        self.seek_args = []
        self.sample_calls = 0
        self.written = []

    def evaluate(self, js, arg=None):
        if js == cf.SEEK_JS:
            self.seek_args.append(arg)
            return self.seek_results.pop(0) if self.seek_results else {"ok": False,
                                                                      "reason": "canned out"}
        if js == cf.SAMPLE_JS:
            self.sample_calls += 1
            return self.sample_result
        raise AssertionError("unexpected JS passed to evaluate")

    def goto(self, url, **kw):
        pass

    def wait_for_selector(self, sel, timeout=0):
        pass

    def wait_for_timeout(self, ms):
        pass

    def add_style_tag(self, content):
        pass

    def locator(self, sel):
        return _FakeLocator(self)


def _canned_samples(stable_at=3, n=10, t0=57.0, ink=0.3):
    """一串采样：前几拍不稳定，之后稳定，信息量在稳定后不再增长。

    稳定之后的 ink 保持在 ink（画面有内容），不是 0 —— ink=0 表示那一刻
    本来就是黑屏，那是另一个测试要测的场景。
    """
    out = []
    for i in range(n):
        out.append({"t": t0 + i * 0.4, "diff": 0.0 if i >= stable_at else 40.0,
                    "ink": (ink if i >= stable_at else 0.2 + 0.05 * i),
                    "stable": i >= stable_at})
    return out


def test_shoot_ok_path_writes_file_and_reports_pick():
    d = _tmpdir()
    try:
        page = _FakePage(
            seek_results=[{"ok": True, "dur": 60.0, "actual": 57.0},
                          {"ok": True, "dur": 60.0, "actual": 58.2}],
            sample_result={"samples": _canned_samples(), "stalled": False, "reason": ""})
        res = cf._shoot(page, 60.0, "shot_00_01_00", d, ["http://x/embed"])
        assert res.ok, res.reason
        assert os.path.isfile(os.path.join(d, "shot_00_01_00.png"))
        assert res.duration == 60.0 and res.picked > 0
        assert page.sample_calls == 1
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_shoot_out_of_range_fails_without_sampling_or_writing():
    """越界时间戳：必须在 Python 侧拦住（JS 里夹断后自比永远 ok）。"""
    d = _tmpdir()
    try:
        page = _FakePage(seek_results=[{"ok": True, "dur": 60.0, "actual": 57.0}])
        res = cf._shoot(page, 600.0, "shot_00_10_00", d, ["http://x/embed"])
        assert not res.ok and "越界" in res.reason, res
        assert page.sample_calls == 0, "越界时不该再去采样（白等 9 秒）"
        assert page.written == [], "越界时不该截图"
        assert not os.path.exists(os.path.join(d, "shot_00_10_00.png"))
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_shoot_last_second_of_video_is_not_out_of_range():
    """要片尾最后一帧是合法请求：只晚半秒以内不算越界（越界门禁别开太狠）。"""
    d = _tmpdir()
    try:
        page = _FakePage(
            seek_results=[{"ok": True, "dur": 60.0, "actual": 57.0},
                          {"ok": True, "dur": 60.0, "actual": 59.5}],
            sample_result={"samples": _canned_samples(), "stalled": False, "reason": ""})
        res = cf._shoot(page, 60.0, "shot_00_01_00", d, ["http://x/embed"])
        assert res.ok, res.reason
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_shoot_seek_ignored_fails_and_writes_nothing():
    """静默失败 A：播放器不认 seek。旧版只打印一行、照样写盘并算成功。"""
    d = _tmpdir()
    try:
        page = _FakePage(seek_results=[{"ok": False, "reason": "seek 被忽略（currentTime 赋值未生效）"}])
        res = cf._shoot(page, 30.0, "shot_00_00_30", d, ["http://x/embed"])
        assert not res.ok and "seek" in res.reason, res
        assert page.written == []
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_shoot_black_frame_retry_then_fail_leaves_no_file():
    """重拍后仍不合格 → 该帧算失败，且不能留下那张错图。"""
    d = _tmpdir()
    try:
        black = np.zeros((720, 1280, 3), dtype=np.uint8)
        page = _FakePage(
            seek_results=[{"ok": True, "dur": 300.0, "actual": 57.0},
                          {"ok": True, "dur": 300.0, "actual": 58.2},
                          {"ok": True, "dur": 300.0, "actual": 60.2}],
            sample_result={"samples": _canned_samples(), "stalled": False, "reason": ""},
            shot_images=[black])
        res = cf._shoot(page, 60.0, "shot_00_01_00", d, ["http://x/embed"])
        assert not res.ok and "空帧" in res.reason, res
        assert res.retried, "应当尝试过一次 +2s 重拍"
        assert len(page.written) == 2, "应当拍了两次"
        assert not os.path.exists(os.path.join(d, "shot_00_01_00.png")), "不合格的图必须删掉"
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_shoot_black_frame_then_recovered():
    """第一次黑、重拍合格 → 算成功（重拍后必须重新验，结果要采纳）。"""
    d = _tmpdir()
    try:
        black = np.zeros((720, 1280, 3), dtype=np.uint8)
        page = _FakePage(
            seek_results=[{"ok": True, "dur": 300.0, "actual": 57.0},
                          {"ok": True, "dur": 300.0, "actual": 58.2},
                          {"ok": True, "dur": 300.0, "actual": 60.2}],
            sample_result={"samples": _canned_samples(), "stalled": False, "reason": ""},
            shot_images=[black, still_image(640, 360)])
        res = cf._shoot(page, 60.0, "shot_00_01_00", d, ["http://x/embed"])
        assert res.ok, res.reason
        assert res.retried and res.picked == 60.2
        assert os.path.isfile(os.path.join(d, "shot_00_01_00.png"))
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_blank_moment_is_not_retried():
    """审查发现的路径：那一刻本来就是黑屏时，+2s 重拍会把**下一页**拍下来
    挂回原时间戳（错图静默入库）。采样证据能区分「本来就是黑屏」和「拍坏了」。"""
    d = _tmpdir()
    try:
        black = np.zeros((720, 1280, 3), dtype=np.uint8)
        page = _FakePage(
            seek_results=[{"ok": True, "dur": 300.0, "actual": 57.0},
                          {"ok": True, "dur": 300.0, "actual": 58.2}],
            sample_result={"samples": _canned_samples(ink=0.0),   # 采样时就没内容
                           "stalled": False, "reason": ""},
            shot_images=[black, still_image(640, 360)])           # 2 秒后是另一页
        res = cf._shoot(page, 60.0, "shot_00_01_00", d, ["http://x/embed"])
        assert not res.ok and "本身没有内容" in res.reason, res
        assert not res.retried and len(page.written) == 1, "黑屏时刻不该重拍"
        assert not os.path.exists(os.path.join(d, "shot_00_01_00.png"))
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_failed_recapture_keeps_existing_file():
    """重拍失败不能删掉盘上原有的同名图（旧版会 os.remove 把它一起带走）。"""
    d = _tmpdir()
    try:
        existing = os.path.join(d, "shot_00_01_00.png")
        write_png(existing, still_image(640, 360))
        before = open(existing, "rb").read()
        black = np.zeros((720, 1280, 3), dtype=np.uint8)
        page = _FakePage(
            seek_results=[{"ok": True, "dur": 300.0, "actual": 57.0},
                          {"ok": True, "dur": 300.0, "actual": 58.2},
                          {"ok": True, "dur": 300.0, "actual": 60.2}],
            sample_result={"samples": _canned_samples(), "stalled": False, "reason": ""},
            shot_images=[black])                       # 拍两次都是黑的
        res = cf._shoot(page, 60.0, "shot_00_01_00", d, ["http://x/embed"])
        assert not res.ok
        assert os.path.isfile(existing), "盘上原有的图被删掉了"
        assert open(existing, "rb").read() == before, "盘上原有的图被改写了"
        assert not os.path.exists(os.path.join(d, "shot_00_01_00.partial.png")), \
            "半成品必须清理干净"
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_materialize_only_exits_nonzero_when_frames_missing():
    """硬门禁的对偶面：缺图时 --materialize-only 不能报成功（否则 AI 会以为已完成）。"""
    d = _tmpdir()
    try:
        vdir = os.path.join(d, "output", "BVtest1")
        imgdir = os.path.join(vdir, "images")
        os.makedirs(imgdir)
        book = os.path.join(vdir, "book.md")
        with open(book, "w", encoding="utf-8") as f:
            f.write("![a](SCREENSHOT:00:00:10)\n![b](SCREENSHOT:00:01:00)\n")
        write_png(os.path.join(imgdir, "shot_00_00_10.png"), still_image(640, 360))

        old_base, old_argv = cf.BASE_DIR, sys.argv
        cf.BASE_DIR = d
        sys.argv = ["capture_frames.py", "BVtest1", "--materialize-only"]
        try:
            try:
                cf.main()
                raise AssertionError("缺图时不该正常返回")
            except SystemExit as e:
                assert e.code == 1, f"退出码应当是 1，实际 {e.code}"
        finally:
            cf.BASE_DIR, sys.argv = old_base, old_argv

        with open(book, encoding="utf-8") as f:
            content = f.read()
        assert "SCREENSHOT:00:01:00" in content, "缺图时不该物化"
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_try_dedicated_unavailable_without_profile():
    status, detail, results = cf.try_dedicated([], "c:/tmp/definitely_missing",
                                               "c:/tmp/definitely_missing_profile")
    assert status == "unavailable" and results == []


# ───────────────────────── --force（登录大会员后重跑升级画质） ─────────────────────────

def _book(root, timestamps, images=()):
    """造一个最小 output/BVtest1/：book.md 占位符 + 可选预置的合格图。"""
    vdir = os.path.join(root, "output", "BVtest1")
    imgdir = os.path.join(vdir, "images")
    os.makedirs(imgdir)
    with open(os.path.join(vdir, "book.md"), "w", encoding="utf-8") as f:
        for ts in timestamps:
            f.write(f"![a](SCREENSHOT:{ts})\n")
    for ts in images:
        write_png(os.path.join(imgdir, "shot_" + ts.replace(":", "_") + ".png"),
                  still_image(640, 360))
    return imgdir


def _run_main(root, argv, capture=None):
    """在临时 output/ 上跑一遍 main()，返回 (退出码, 打印内容)。

    capture 给了就临时替换掉 try_dedicated（不碰真浏览器），跑完还原。
    """
    buf = io.StringIO()
    old = (cf.BASE_DIR, sys.argv, cf.try_dedicated)
    if capture is not None:
        cf.try_dedicated = capture
    cf.BASE_DIR = root
    sys.argv = ["capture_frames.py"] + argv
    code = 0
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            cf.main()
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 1
    finally:
        cf.BASE_DIR, sys.argv, cf.try_dedicated = old
    return code, buf.getvalue()


def _capture_noop(record):
    """假 capture：把「被要求截哪些时刻」记进 record，再报全部成功（不写盘）。"""
    def fake(shots, imgdir, profile_dir, video_id="", platform=""):
        record.extend(s[0] for s in shots)
        return ("ok", f"captured {len(shots)} frames",
                [cf.ShotResult(ok=True, name=s[1], requested=float(s[0])) for s in shots])
    return fake


def _capture_pages(pages):
    """假 capture：每个时刻用给定的假 page 跑**真**的 _shoot（硬门禁原样生效）。"""
    def fake(shots, imgdir, profile_dir, video_id="", platform=""):
        results = [cf._shoot(pages.pop(0), sec, name, imgdir, [embed])
                   for sec, name, embed, _main in shots]
        bad = [r for r in results if not r.ok]
        return ("partial" if bad else "ok", f"{len(bad)}/{len(results)} 帧未产出", results)
    return fake


def test_frames_missing_force_lists_all_timestamps():
    """--force 只改「要尝试哪些帧」：已存在且合格的帧也必须进待截清单；
    不传 force 时逐字保持旧行为（回归）。"""
    d = _tmpdir()
    try:
        write_png(os.path.join(d, "shot_00_00_10.png"), still_image(640, 360))
        ts = ["00:00:10", "00:00:20"]
        assert cf.frames_missing(ts, d) == ["00:00:20"], "默认行为不该变"
        assert cf.frames_missing(ts, d, force=True) == ts, "--force 必须列出全部时间戳"
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_force_recaptures_frames_that_already_pass_qa():
    """被文档承诺的路径：「登录大会员后重跑重截升级画质」。

    改前：图已存在且合格 → frames_missing 返回空 → main 打印
    "all frames present; nothing to capture" 并退出 0，一张都不重截（空转，
    用户却以为已经升级成功）。
    """
    d = _tmpdir()
    try:
        _book(d, ["00:00:10", "00:00:20"], images=["00:00:10", "00:00:20"])
        got = []
        code, out = _run_main(d, ["BVtest1", "--profile-dir", os.path.join(d, "prof")],
                              capture=_capture_noop(got))
        assert code == 0, out
        assert "nothing to capture" in out, out
        assert got == [], f"不传 --force 时不该重截任何帧，实际 {got}"
    finally:
        shutil.rmtree(d, ignore_errors=True)

    d = _tmpdir()
    try:
        _book(d, ["00:00:10", "00:00:20"], images=["00:00:10", "00:00:20"])
        got = []
        code, out = _run_main(d, ["BVtest1", "--profile-dir", os.path.join(d, "prof"),
                                  "--force"], capture=_capture_noop(got))
        assert code == 0, out
        assert got == [10, 20], f"--force 必须重截全部时间戳，实际 {got}"
        assert "2 frame(s) to capture" in out, out
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_forced_recapture_failure_keeps_existing_image_byte_identical():
    """--force 只改「试哪些帧」，不放宽质量闸门：重截失败时盘上原有的同名图
    必须逐字节不变（.partial.png 机制保留），且不物化、退出码 1。"""
    d = _tmpdir()
    try:
        imgdir = _book(d, ["00:00:10"], images=["00:00:10"])
        existing = os.path.join(imgdir, "shot_00_00_10.png")
        before = open(existing, "rb").read()
        black = np.zeros((720, 1280, 3), dtype=np.uint8)
        page = _FakePage(
            seek_results=[{"ok": True, "dur": 300.0, "actual": 7.0},
                          {"ok": True, "dur": 300.0, "actual": 10.0},
                          {"ok": True, "dur": 300.0, "actual": 12.0}],
            sample_result={"samples": _canned_samples(), "stalled": False, "reason": ""},
            shot_images=[black])                       # 重截两次都是黑帧
        code, out = _run_main(d, ["BVtest1", "--profile-dir", os.path.join(d, "prof"),
                                  "--force"], capture=_capture_pages([page]))
        assert code == 1, f"重截失败必须退出码 1，实际 {code}\n{out}"
        assert "空帧" in out, out
        assert open(existing, "rb").read() == before, "盘上原有的图被改写了"
        assert not os.path.exists(os.path.join(imgdir, "shot_00_00_10.partial.png")), \
            "半成品必须清理干净"
        with open(os.path.join(d, "output", "BVtest1", "book.md"), encoding="utf-8") as f:
            assert "SCREENSHOT:00:00:10" in f.read(), "有帧没出图时不该物化"
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_force_does_not_bypass_out_of_range_gate():
    """回归：--force 不得绕过硬门禁。越界时间戳仍然「不写盘、不物化、退出码 1」，
    连盘上那张（其实合格的）旧图也不能被动。"""
    d = _tmpdir()
    try:
        imgdir = _book(d, ["00:10:00"], images=["00:10:00"])
        existing = os.path.join(imgdir, "shot_00_10_00.png")
        before = open(existing, "rb").read()
        page = _FakePage(seek_results=[{"ok": True, "dur": 60.0, "actual": 57.0}])
        code, out = _run_main(d, ["BVtest1", "--profile-dir", os.path.join(d, "prof"),
                                  "--force"], capture=_capture_pages([page]))
        assert code == 1 and "越界" in out, out
        assert page.written == [], "越界时不该截图"
        assert open(existing, "rb").read() == before, "盘上原有的图被改写了"
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_force_with_materialize_only_is_rejected():
    """--force（要重截）与 --materialize-only（只物化、不截图）自相矛盾。
    静默忽略会让人以为已经强制重截过（正是本次要修的「空转」的翻版），
    所以直接报错退出（argparse 退出码 2）。"""
    d = _tmpdir()
    try:
        _book(d, ["00:00:10"], images=["00:00:10"])
        code, out = _run_main(d, ["BVtest1", "--materialize-only", "--force"])
        assert code == 2, f"矛盾的组合应当报错退出（2），实际 {code}\n{out}"
        assert "cannot be combined" in out, f"应当明确报出这两个参数冲突：{out}"
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ───────────────────── --force-invalid（只补「缺的 / 坏的」） ─────────────────────

def test_force_invalid_lists_only_missing_or_unusable_frames():
    """三个方向都要对：默认不变 / --force-invalid 只列缺的与不合格的 / --force 仍是全部。

    --force-invalid 与默认的找帧判据一致（就是把它写明），差别在它与 --force
    互斥、且打印里说清这一轮在干什么 —— 免得有人以为自己在升级画质。
    """
    d = _tmpdir()
    try:
        imgdir = _book(d, ["00:00:10", "00:00:20", "00:00:30"], images=["00:00:10"])
        # 20 号：盘上有图但整幅全黑（不合格）；30 号：压根没有图
        write_png(os.path.join(imgdir, "shot_00_00_20.png"),
                  np.zeros((720, 1280, 3), dtype=np.uint8))
        outs = {}
        for argv, want in (([], [20, 30]),
                           (["--force-invalid"], [20, 30]),
                           (["--force"], [10, 20, 30])):
            got = []
            code, out = _run_main(d, ["BVtest1", "--profile-dir", os.path.join(d, "prof")]
                                  + argv, capture=_capture_noop(got))
            assert code == 0, f"{argv}: {out}"
            assert got == want, f"{argv} 应当截 {want}，实际 {got}\n{out}"
            outs[tuple(argv)] = out
        assert "--force-invalid" in outs[("--force-invalid",)], \
            f"打印里要说清这一轮只补缺的/坏的：{outs[('--force-invalid',)]}"
        assert "--force: all of them" in outs[("--force",)], \
            f"打印里要说清这一轮是全部重截：{outs[('--force',)]}"
        # --force 那一轮把「已存在且合格」的帧也重截了，manifest 随之更新（三条都在）
        names = {f["name"] for f in
                 _manifest(os.path.join(d, "output", "BVtest1"))["frames"]}
        assert names == {"shot_00_00_10", "shot_00_00_20", "shot_00_00_30"}, names
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_force_and_force_invalid_are_mutually_exclusive():
    """一个说「全部重截」、一个说「只补缺的」：同时给必然有一个是误输入，
    直接报错退出（argparse 退出码 2），并且一个字都不许写盘。"""
    d = _tmpdir()
    try:
        imgdir = _book(d, ["00:00:10"], images=["00:00:10"])
        vdir = os.path.join(d, "output", "BVtest1")
        existing = os.path.join(imgdir, "shot_00_00_10.png")
        img_before = open(existing, "rb").read()
        book = os.path.join(vdir, "book.md")
        book_before = open(book, encoding="utf-8").read()
        got = []
        code, out = _run_main(d, ["BVtest1", "--force", "--force-invalid"],
                              capture=_capture_noop(got))
        assert code == 2, f"互斥的两个参数应当报错退出（2），实际 {code}\n{out}"
        assert "cannot be combined" in out, out
        assert got == [], "报错时不该去截图"
        assert open(existing, "rb").read() == img_before, "报错时不该动盘上的图"
        assert open(book, encoding="utf-8").read() == book_before, "报错时不该改写稿件"
        assert not os.path.exists(os.path.join(vdir, "capture_manifest.json")), \
            "报错时不该留下 manifest"

        # --materialize-only（只物化、不截图）与 --force-invalid 同样自相矛盾
        code2, out2 = _run_main(d, ["BVtest1", "--materialize-only", "--force-invalid"])
        assert code2 == 2 and "cannot be combined" in out2, out2
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_force_invalid_does_not_bypass_hard_gates():
    """--force-invalid 只是「少截一些」，不放宽任何闸门：越界帧照样不写盘、
    不物化、退出码 1，盘上原有的（不合格）图也不动、不留 .partial.png。"""
    d = _tmpdir()
    try:
        imgdir = _book(d, ["00:10:00"])            # 越界时间戳（片长只有 60s）
        existing = os.path.join(imgdir, "shot_00_10_00.png")
        write_png(existing, np.zeros((720, 1280, 3), dtype=np.uint8))  # 不合格 → 会进待截清单
        before = open(existing, "rb").read()
        page = _FakePage(seek_results=[{"ok": True, "dur": 60.0, "actual": 57.0}])
        code, out = _run_main(d, ["BVtest1", "--profile-dir", os.path.join(d, "prof"),
                                  "--force-invalid"], capture=_capture_pages([page]))
        assert code == 1 and "越界" in out, out
        assert page.written == [], "越界时不该截图"
        assert open(existing, "rb").read() == before, "盘上原有的图被改写了"
        assert not os.path.exists(os.path.join(imgdir, "shot_00_10_00.partial.png")), \
            "半成品必须清理干净"
        with open(os.path.join(d, "output", "BVtest1", "book.md"), encoding="utf-8") as f:
            assert "SCREENSHOT:00:10:00" in f.read(), "有帧没出图时不该物化"
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ─────────────────── 截图 manifest（审计「这张图来自哪一秒」） ───────────────────

def _manifest(vdir):
    with open(os.path.join(vdir, "capture_manifest.json"), encoding="utf-8") as f:
        return json.load(f)


def _ok_page(sec):
    """一个能把 sec 号帧拍成功的假 page（采样窗口 [sec-3, sec+6] 内画面稳定）。"""
    t0 = float(sec) - cf.STABLE_BACK_SEC               # 采样从目标 -3s 开始
    picked = t0 + 0.4 * cf.STABLE_NEED                 # 自第 STABLE_NEED 拍起算稳定
    dur = float(sec) + 240.0
    return _FakePage(
        seek_results=[{"ok": True, "dur": dur, "actual": t0},
                      {"ok": True, "dur": dur, "actual": picked}],
        sample_result={"samples": _canned_samples(t0=t0), "stalled": False, "reason": ""})


def _rewrite_draft(vdir, timestamps):
    """把稿件（book.md 与 book.tagged.md）改成新的时间戳列表。"""
    for fn in ("book.md", "book.tagged.md"):
        p = os.path.join(vdir, fn)
        if os.path.exists(p):
            with open(p, "w", encoding="utf-8") as f:
                for ts in timestamps:
                    f.write(f"![a](SCREENSHOT:{ts})\n")


def test_manifest_records_which_second_each_image_came_from():
    """manifest 的用处：G 层会在目标 ±6 秒内挑一拍，文件名/图注写着 00:00:10、
    画面却可能来自 00:00:12.2 —— 在此之前这个信息只打印到控制台，事后查不出来。"""
    d = _tmpdir()
    try:
        imgdir = _book(d, ["00:00:10"])
        black = np.zeros((720, 1280, 3), dtype=np.uint8)
        page = _FakePage(
            seek_results=[{"ok": True, "dur": 300.0, "actual": 7.0},
                          {"ok": True, "dur": 300.0, "actual": 8.2},
                          {"ok": True, "dur": 300.0, "actual": 10.2}],
            sample_result={"samples": _canned_samples(t0=7.0), "stalled": False, "reason": ""},
            shot_images=[black, still_image(640, 360)])    # 第一拍黑 → +2s 重拍成功
        code, out = _run_main(d, ["BVtest1", "--profile-dir", os.path.join(d, "prof")],
                              capture=_capture_pages([page]))
        assert code == 0, out
        vdir = os.path.join(d, "output", "BVtest1")
        man = _manifest(vdir)
        assert (man["video_id"], man["platform"]) == ("BVtest1", "bilibili")
        assert man["method"] == "dedicated", man
        assert man["source"] == "book.md", man
        assert man["frames_needed"] == 1, man
        assert len(man["frames"]) == 1, man["frames"]
        f0 = man["frames"][0]
        assert f0["name"] == "shot_00_00_10"
        assert f0["ok"] is True
        assert f0["requested"] == 10.0, "目标秒（文件名/图注上的秒）"
        assert f0["picked"] == 10.2, "G 层选定秒"
        assert f0["actual"] == 10.2, "播放器实际停住的秒"
        assert f0["retried"] is True and f0["reason"] == ""
        assert f0["duration"] == 300.0 and f0["how"] == "stable-near"
        assert os.path.isfile(os.path.join(imgdir, "shot_00_00_10.png"))
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_manifest_keeps_failed_frames_with_their_reason():
    """失败帧必须留痕：manifest 里没有它反而更糟 —— 看起来像「这一帧没人管过」。
    同时确认硬门禁没被削弱：不合格就不写盘、不物化、退出码 1。"""
    d = _tmpdir()
    try:
        _book(d, ["00:00:10", "00:01:00"])
        black = np.zeros((720, 1280, 3), dtype=np.uint8)
        good = _ok_page(10)
        bad = _FakePage(
            seek_results=[{"ok": True, "dur": 300.0, "actual": 7.0},
                          {"ok": True, "dur": 300.0, "actual": 8.2},
                          {"ok": True, "dur": 300.0, "actual": 10.2}],
            sample_result={"samples": _canned_samples(t0=57.0), "stalled": False, "reason": ""},
            shot_images=[black])                       # 拍两次都是黑的 → 该帧失败
        code, out = _run_main(d, ["BVtest1", "--profile-dir", os.path.join(d, "prof")],
                              capture=_capture_pages([good, bad]))
        assert code == 1, f"有帧没出图必须退出码 1，实际 {code}\n{out}"
        vdir = os.path.join(d, "output", "BVtest1")
        frames = {f["name"]: f for f in _manifest(vdir)["frames"]}
        assert set(frames) == {"shot_00_00_10", "shot_00_01_00"}, \
            f"失败帧不能从 manifest 里静默消失：{sorted(frames)}"
        assert frames["shot_00_00_10"]["ok"] is True
        bad_f = frames["shot_00_01_00"]
        assert bad_f["ok"] is False, bad_f
        assert "空帧" in bad_f["reason"], bad_f
        assert bad_f["retried"] is True and bad_f["requested"] == 60.0, bad_f
        assert not os.path.exists(os.path.join(vdir, "images", "shot_00_01_00.png"))
        with open(os.path.join(vdir, "book.md"), encoding="utf-8") as f:
            assert "SCREENSHOT:00:01:00" in f.read(), "有帧没出图时不该物化"
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_manifest_merges_on_rerun_and_drops_frames_no_longer_tagged():
    """重跑常常只补 1~2 张缺图：整份覆盖会让其余图的记录一起消失（而这份文件
    唯一的用处就是查它们）。所以按帧名合并；同时按当前稿件剪枝 —— 稿子里已经
    删掉的时间戳不再保留（那张图也不再被正文引用）。"""
    d = _tmpdir()
    try:
        _book(d, ["00:00:10", "00:00:20"])
        vdir = os.path.join(d, "output", "BVtest1")
        code, out = _run_main(d, ["BVtest1", "--profile-dir", os.path.join(d, "prof")],
                              capture=_capture_pages([_ok_page(10), _ok_page(20)]))
        assert code == 0, out
        first = {f["name"]: f for f in _manifest(vdir)["frames"]}
        assert set(first) == {"shot_00_00_10", "shot_00_00_20"}, first

        # 稿件改了：00:00:20 换成 00:00:30；10 的图还在且合格，只有 30 要补
        _rewrite_draft(vdir, ["00:00:10", "00:00:30"])
        code, out = _run_main(d, ["BVtest1", "--profile-dir", os.path.join(d, "prof")],
                              capture=_capture_pages([_ok_page(30)]))
        assert code == 0, out
        second = {f["name"]: f for f in _manifest(vdir)["frames"]}
        assert set(second) == {"shot_00_00_10", "shot_00_00_30"}, \
            f"没重截的帧不能被抹掉、稿子里删掉的帧要剪掉：{sorted(second)}"
        assert second["shot_00_00_10"] == first["shot_00_00_10"], "旧记录被改写了"
        assert second["shot_00_00_30"]["ok"] is True
        assert _manifest(vdir)["frames_needed"] == 2
    finally:
        shutil.rmtree(d, ignore_errors=True)
