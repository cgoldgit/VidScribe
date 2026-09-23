"""capture_frames.py 的浏览器回归测试（本机 Chrome + 本地合成视频，不联网）。

对应 2026-09-22 code review 报告里最严重的两条 —— 它们只有真浏览器能验：
  · SEEK_JS 的两个 await 没有超时：媒体加载失败时整轮抓帧**永久挂住**
    （实测 page.evaluate 里那个 Promise 119 秒仍不返回）
  · SAMPLE_JS 的 while 只以「媒体时间推进满 9 秒」为出口：播放停滞时变成
    **页内死循环**（实测 20 秒仍不返回）
  · 外加：seek 被静默忽略、一帧的异常不该报废整轮、黑帧判据

整套用例只用一个本地 HTTP 服务器（支持 Range，Chrome 没 Range 就没法 seek）。
"""
import atexit
import os
import shutil
import tempfile
import time
import unittest

from _helpers import file_server, make_video, write_page

FAST_MS = 2000          # 测试用的短超时：证明「会超时」不需要等 15 秒


def _rig():
    """起一次浏览器 + 本地服务器，全程复用。"""
    if "rig" in _rig.__dict__:
        return _rig.rig
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise unittest.SkipTest("playwright 未安装")
    import capture_frames as cf
    if not cf.find_chrome_exe():
        raise unittest.SkipTest("本机找不到 Chrome")

    work = tempfile.mkdtemp(prefix="vs_browser_")
    make_video(os.path.join(work, "changing.mp4"), 60, kind="testsrc", workdir=work)
    make_video(os.path.join(work, "still.mp4"), 60, kind="still", workdir=work)
    make_video(os.path.join(work, "black.mp4"), 30, kind="black", workdir=work)
    write_page(os.path.join(work, "v.html"), "changing.mp4")
    write_page(os.path.join(work, "still.html"), "still.mp4")
    write_page(os.path.join(work, "black.html"), "black.mp4")
    write_page(os.path.join(work, "dead.html"), "missing_file.mp4")
    write_page(os.path.join(work, "two.html"), "changing.mp4",
               extra_js="document.body.insertAdjacentHTML('beforeend',"
                        "'<video src=\"changing.mp4\" muted></video>');")
    with open(os.path.join(work, "novideo.html"), "w", encoding="utf-8") as f:
        f.write("<html><body>no video here</body></html>")

    srv = file_server(work)
    base = srv.__enter__()
    pw = sync_playwright().start()
    try:
        ctx = pw.chromium.launch_persistent_context(
            os.path.join(work, "prof"), channel="chrome", headless=True,
            viewport={"width": 640, "height": 360},
            args=["--autoplay-policy=no-user-gesture-required"])
    except Exception as e:                       # 浏览器起不来就当跳过
        pw.stop()
        srv.__exit__(None, None, None)
        shutil.rmtree(work, ignore_errors=True)
        raise unittest.SkipTest(f"Chrome 无法启动：{(str(e) or '').splitlines()[0]}")

    _rig.rig = {"work": work, "base": base, "pw": pw, "ctx": ctx,
                "page": ctx.pages[0] if ctx.pages else ctx.new_page(),
                "srv": srv}
    return _rig.rig


@atexit.register
def _cleanup():
    rig = _rig.__dict__.get("rig")
    if not rig:
        return
    try:
        rig["ctx"].close()
        rig["pw"].stop()
    finally:
        rig["srv"].__exit__(None, None, None)
        shutil.rmtree(rig["work"], ignore_errors=True)


def _open(rig, page_name):
    rig["page"].goto(f"{rig['base']}/{page_name}", wait_until="domcontentloaded",
                     timeout=15000)
    return rig["page"]


# ───────────────────────── SEEK_JS ─────────────────────────

def test_seek_js_times_out_when_media_never_loads():
    """回归：媒体加载失败时旧版那个 await 永不返回 → 整轮抓帧静默挂死。"""
    import capture_frames as cf
    rig = _rig()
    page = _open(rig, "dead.html")
    page.wait_for_selector("video", timeout=5000)
    t0 = time.time()
    r = page.evaluate(cf._seek_js(FAST_MS, FAST_MS), 10.0)
    el = time.time() - t0
    assert r["ok"] is False, r
    assert "未就绪" in r["reason"], r
    assert el < 8, f"等了 {el:.1f} 秒，超时没生效"


def test_seek_js_same_position_returns_quickly():
    """赋值给当前所在位置时事件可能不触发，不能傻等到超时。"""
    import capture_frames as cf
    rig = _rig()
    page = _open(rig, "v.html")
    page.wait_for_function("() => document.querySelector('video').readyState >= 2",
                           timeout=20000)
    page.evaluate("""() => { const v = document.querySelector('video');
        v.pause(); v.currentTime = 20; }""")
    t0 = time.time()
    r = page.evaluate(cf._seek_js(FAST_MS, FAST_MS), 20.0)
    el = time.time() - t0
    assert r["ok"] is True, r
    assert el < 1.5, f"同一位置 seek 白等了 {el:.1f} 秒"


def test_seek_js_detects_ignored_assignment():
    """静默失败 A：播放器不认 seek（赋值被忽略）时旧版照样报 ok。"""
    import capture_frames as cf
    rig = _rig()
    page = _open(rig, "v.html")
    page.wait_for_function("() => document.querySelector('video').readyState >= 2",
                           timeout=20000)
    page.evaluate("""() => { const v = document.querySelector('video');
        v.pause();
        Object.defineProperty(v, 'currentTime', {configurable: true,
                                                 get: () => 3, set: () => {}}); }""")
    r = page.evaluate(cf._seek_js(FAST_MS, FAST_MS), 40.0)
    assert r["ok"] is False, f"赋值被忽略了却报成功：{r}"
    assert r.get("actual") == 3 or "seek" in r.get("reason", ""), r


def test_seek_js_without_video_element():
    import capture_frames as cf
    rig = _rig()
    page = _open(rig, "novideo.html")
    r = page.evaluate(cf.SEEK_JS, 10.0)
    assert r["ok"] is False and "video" in r["reason"], r


# ───────────────────────── SAMPLE_JS ─────────────────────────

def test_sample_js_returns_when_playback_stalls():
    """回归：currentTime 不推进而 paused==false 时旧版 while 变成页内死循环。"""
    import capture_frames as cf
    rig = _rig()
    page = _open(rig, "v.html")
    page.wait_for_function("() => document.querySelector('video').readyState >= 2",
                           timeout=20000)
    page.evaluate("""async () => { const v = document.querySelector('video');
        await v.play(); v.playbackRate = 0;      // 时钟冻住，但元素没暂停
        await new Promise(r => setTimeout(r, 300)); }""")
    wall_ms = int((cf.STABLE_BACK_SEC + cf.STABLE_FWD_SEC) * 1000) + 3000
    t0 = time.time()
    r = page.evaluate(cf.SAMPLE_JS, {
        "intervalMs": cf.STABLE_INTERVAL_MS,
        "windowSec": cf.STABLE_BACK_SEC + cf.STABLE_FWD_SEC,
        "blockThresh": cf.STABLE_BLOCK_THRESH, "needStable": cf.STABLE_NEED,
        "targetSec": 20.0, "wallMs": wall_ms, "minFwdSec": cf.STABLE_MIN_FWD_SEC,
        "settle": cf.STABLE_SETTLE, "inkEps": cf.STABLE_INK_EPS})
    el = time.time() - t0
    assert r["stalled"] is True, f"没识别出播放停滞：{r}"
    assert el < wall_ms / 1000 + 5, f"墙钟上限没生效，等了 {el:.1f} 秒"


def test_sample_js_exits_early_but_still_looks_forward():
    """静止画面：允许提前收工，但必须先越过目标时刻 minFwdSec 秒。"""
    import capture_frames as cf
    rig = _rig()
    page = _open(rig, "still.html")
    page.wait_for_function("() => document.querySelector('video').readyState >= 2",
                           timeout=20000)
    page.evaluate("""async () => { const v = document.querySelector('video');
        v.pause(); v.currentTime = 17; await new Promise(r => setTimeout(r, 300)); }""")
    window = cf.STABLE_BACK_SEC + cf.STABLE_FWD_SEC
    t0 = time.time()
    r = page.evaluate(cf.SAMPLE_JS, {
        "intervalMs": cf.STABLE_INTERVAL_MS, "windowSec": window,
        "blockThresh": cf.STABLE_BLOCK_THRESH, "needStable": cf.STABLE_NEED,
        "targetSec": 20.0,
        "wallMs": int(window * 1000) + 6000, "minFwdSec": cf.STABLE_MIN_FWD_SEC,
        "settle": cf.STABLE_SETTLE, "inkEps": cf.STABLE_INK_EPS})
    el = time.time() - t0
    samples = r["samples"]
    assert samples, r
    assert any(s["stable"] for s in samples), "静止画面应当判为稳定"
    assert el < window - 1.0, f"没有提前收工，等了 {el:.1f} 秒"
    assert samples[-1]["t"] >= 20.0 + cf.STABLE_MIN_FWD_SEC - 0.8, \
        f"提前收工太早，只看到 {samples[-1]['t']:.1f}s（要求越过 20+{cf.STABLE_MIN_FWD_SEC}s）"


def test_sample_js_without_video_element():
    import capture_frames as cf
    rig = _rig()
    page = _open(rig, "novideo.html")
    r = page.evaluate(cf.SAMPLE_JS, {
        "intervalMs": 400, "windowSec": 9.0, "blockThresh": 8.0, "needStable": 3,
        "targetSec": 20.0, "wallMs": 15000, "minFwdSec": 3.0, "settle": 3,
        "inkEps": 0.01})
    assert r["samples"] == [] and r["reason"], r


# ───────────────────────── _shoot 端到端 ─────────────────────────

def test_shoot_end_to_end_on_local_page():
    import capture_frames as cf
    rig = _rig()
    page = _open(rig, "v.html")
    out = tempfile.mkdtemp(prefix="vs_shots_")
    try:
        url = f"{rig['base']}/v.html"
        res = cf._shoot(page, 30.0, "shot_00_00_30", out, [url])
        assert res.ok, res.reason
        assert os.path.isfile(os.path.join(out, "shot_00_00_30.png"))
        assert cf._frame_usable(os.path.join(out, "shot_00_00_30.png"))[0]
        assert res.duration > 50, res.duration
    finally:
        shutil.rmtree(out, ignore_errors=True)


def test_shoot_end_to_end_out_of_range_writes_nothing():
    import capture_frames as cf
    rig = _rig()
    page = _open(rig, "v.html")
    out = tempfile.mkdtemp(prefix="vs_shots_")
    try:
        url = f"{rig['base']}/v.html"
        res = cf._shoot(page, 600.0, "shot_00_10_00", out, [url])
        assert not res.ok and "越界" in res.reason, res
        assert not os.path.exists(os.path.join(out, "shot_00_10_00.png"))
    finally:
        shutil.rmtree(out, ignore_errors=True)


def test_shoot_black_video_fails_and_leaves_no_file():
    """真黑帧：拍出来 → 判不合格 → +2s 重拍 → 仍不合格 → 不留文件。"""
    import capture_frames as cf
    rig = _rig()
    page = _open(rig, "black.html")
    out = tempfile.mkdtemp(prefix="vs_shots_")
    try:
        url = f"{rig['base']}/black.html"
        res = cf._shoot(page, 10.0, "shot_00_00_10", out, [url])
        assert not res.ok and "空帧" in res.reason, res
        assert not os.path.exists(os.path.join(out, "shot_00_00_10.png")), \
            "不合格的图必须删掉，不能留在 images/ 里"
    finally:
        shutil.rmtree(out, ignore_errors=True)


def test_run_capture_isolates_one_bad_shot():
    """回归：一帧的异常（页面里冒出第二个 <video> → strict mode 报错）
    只作废那一帧，不能报废整轮（旧版异常会穿出去，磁盘上只剩半截图）。"""
    import capture_frames as cf
    rig = _rig()
    out = tempfile.mkdtemp(prefix="vs_shots_")
    prof = tempfile.mkdtemp(prefix="vs_prof_")
    try:
        bad = f"{rig['base']}/two.html"
        good = f"{rig['base']}/still.html"
        shots = [[30.0, "shot_bad", bad, bad],
                 [20.0, "shot_good", good, good]]
        # 复用同一个 playwright 实例：sync API 不能嵌套
        results = cf._run_capture(rig["pw"], shots, out, prof, "", "", True)
        assert len(results) == 2
        assert results[0].ok is False and results[0].reason, results[0]
        assert results[1].ok is True, results[1].reason
        assert not os.path.exists(os.path.join(out, "shot_bad.png"))
        assert os.path.isfile(os.path.join(out, "shot_good.png"))
    finally:
        shutil.rmtree(out, ignore_errors=True)
        shutil.rmtree(prof, ignore_errors=True)
