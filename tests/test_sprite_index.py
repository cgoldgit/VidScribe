"""sprite_index.py 的回归测试（离线，不联网）。

对应 2026-09-22 code review 报告里的这几条：
  · 单一 0.05 阈值在长讲座上压不下去（实测 712 格 → 202 段 → 23 张图）
  · 同一个阈值又会把「黑底幻灯片」的不同页并掉（漏图）
  · 代表帧取的是「变化刚发生的第一格」＝动画最不完整的那一帧
  · 末段的 until 用了最后一个采样时刻，而不是真实片长
  · 图册 jpg 缓存只判文件存在，半截的坏图会被一直复用
  · 索引图时间戳标签写成 MM:SS（98:26 到底是 98 分还是 1 小时 38 分？）
"""
import json
import os
import shutil
import subprocess
import tempfile

import numpy as np

from _helpers import ffmpeg

import sprite_index as si


def _base(seed, h=18, w=32):
    rng = np.random.default_rng(seed)
    return (rng.integers(0, 2, size=(h, w, 3)) * 255).astype(np.uint8)


def _flip(prev, ratio):
    """翻掉前 ratio 比例的像素：与 prev 的变化占比正好 = ratio。"""
    a = prev.reshape(-1, 3).copy()
    n = int(round(ratio * len(a)))
    a[:n] = 255 - a[:n]
    return a.reshape(prev.shape)


def _build(plan, seed=1):
    """plan[i] = 第 i 格相对上一格的变化占比（plan[0] 忽略）。"""
    cells = [_base(seed)]
    for r in plan[1:]:
        cells.append(_flip(cells[-1], r))
    return cells


def _times(n, step=5):
    return [i * step for i in range(n)]


# ───────────────────────── 分段与代表帧 ─────────────────────────

def test_changed_ratio_matches_plan():
    c = _build([0.0, 0.10, 0.0])
    assert abs(si.changed_ratio(c[0], c[0])) < 1e-9
    assert abs(si.changed_ratio(c[0], c[1]) - 0.10) < 0.02


def test_identical_and_weak_changes_merge_into_one_segment():
    cells = _build([0.0, 0.0, 0.0, 0.02, 0.02, 0.0])
    segs, _ = si.pick_segments(cells, _times(len(cells)), max_cells=9)
    assert len(segs) == 1, segs
    assert segs[0]["rep"] == len(cells) - 1        # 段末格
    assert segs[0]["merged"] == 2                  # 两个弱变化点被并掉了


def test_strong_change_starts_new_segment():
    cells = _build([0.0, 0.0, 1.0, 0.0, 0.0])
    segs, _ = si.pick_segments(cells, _times(len(cells)), max_cells=9)
    assert [s["start"] for s in segs] == [0, 2]
    assert [s["rep"] for s in segs] == [1, 4]
    assert segs[1]["weak"] is False


def test_representative_is_last_cell_not_first():
    """回归：旧版把「变化刚发生的第一格」当代表帧，那是动画最不完整的一帧。"""
    cells = _build([0.0, 0.0, 0.0, 0.0, 1.0])
    segs, _ = si.pick_segments(cells, _times(len(cells)), max_cells=9)
    assert segs[0]["rep"] == 3
    assert np.array_equal(cells[segs[0]["rep"]], cells[3])


def test_max_cells_cap_keeps_biggest_changes():
    """长讲座：变化点远多于预算时，只保留差异最大的，其余明确记为 merged。"""
    n = 31
    plan = [0.0] + [1.0 if i % 2 else 0.6 for i in range(1, n)]
    cells = _build(plan)
    segs, d = si.pick_segments(cells, _times(n), max_cells=9)
    assert len(segs) == 9, f"上限 9 格应当产出 9 段，实际 {len(segs)}"
    kept = [s["start"] for s in segs[1:]]
    assert all(abs(d[b] - 1.0) < 0.02 for b in kept), [round(d[b], 3) for b in kept]
    assert sum(s["merged"] for s in segs) == 22, sum(s["merged"] for s in segs)


def test_cap_is_respected_even_when_everything_changes():
    """整段一直在变（录屏）：也必须压在张数上限内，不能默默产出几十张图。"""
    n = 200
    cells = _build([0.0] + [0.9] * (n - 1))
    segs, _ = si.pick_segments(cells, _times(n), max_cells=18)
    assert len(segs) == 18


def test_long_video_fills_weak_changes_for_coverage():
    """只有弱变化的长视频：补点要铺满全片，不能全挤在一头。

    不变量：要么用满预算，要么每段都短于 MAX_SEG_SEC（不能悄悄留一个超长段）。
    """
    n = 121                                   # 600 秒
    cells = _build([0.0] + [0.02] * (n - 1))
    times = _times(n)
    segs, _ = si.pick_segments(cells, times, max_cells=9)
    assert 5 <= len(segs) <= 9, f"补点数量不对劲：{len(segs)}"
    assert all(s["weak"] for s in segs[1:])
    starts = [times[s["start"]] for s in segs]
    assert starts[-1] > 300, f"末段起点 {starts[-1]}s 太靠前，补点没铺开：{starts}"
    gaps = [starts[i + 1] - starts[i] for i in range(len(starts) - 1)] + \
           [times[-1] - starts[-1]]
    assert max(gaps) <= si.MAX_SEG_SEC + 10, f"仍有超长空档：{gaps}"


def test_short_video_does_not_add_extra_segments():
    """短视频已经够密时不该再补点（回归：516 秒的验证案例必须仍是 1 张图）。"""
    n = 103
    plan = [0.0] * n
    for i in (10, 20, 30, 40, 50, 60, 70, 80):     # 8 个明确换页
        plan[i] = 0.9
    cells = _build(plan)
    segs, _ = si.pick_segments(cells, _times(n), max_cells=18, per_sheet=9)
    assert len(segs) == 9, f"8 个换页 → 9 段，不该再补点，实际 {len(segs)}"


def test_fill_does_not_open_a_new_sheet():
    """补点只能用「强变化点本来就需要的那几张图」里的空位，不能多开一张。

    实测教训：516 秒的验证案例本来 1 张图就够，补点一开张就变 2 张，读图成本翻倍。
    """
    n = 400                                        # 2000 秒
    plan = [0.0] + [0.02] * (n - 1)
    plan[200] = 0.9                                # 一个明确换页 + 全程弱变化
    cells = _build(plan)
    times = _times(n)
    segs_full, _ = si.pick_segments(cells, times, max_cells=18)
    segs_1sheet, _ = si.pick_segments(cells, times, max_cells=18, per_sheet=9)
    assert 15 <= len(segs_full) <= 18, len(segs_full)   # 不限制时一直补到每段够短
    assert len(segs_1sheet) == 9, len(segs_1sheet)      # 强变化点只要 1 张图 → 只补到 9 格


# ───────────────────────── 时间戳格式 ─────────────────────────

def test_fmt_hms_is_always_hh_mm_ss():
    """回归：旧版标签是 MM:SS，98 分钟的片子会烧出「98:26」，AI 照着写进正文
    就再也匹配不上 SCREENSHOT:HH:MM:SS 了。"""
    assert si.fmt_hms(0) == "00:00:00"
    assert si.fmt_hms(65) == "00:01:05"
    assert si.fmt_hms(5910) == "01:38:30"


# ───────────────────────── 图册缓存 ─────────────────────────

def _write_jpeg(path, w=64, h=36):
    """写一张真正的 jpeg（结尾必须带 FFD9）。"""
    arr = _base(3, h, w)
    subprocess.run([ffmpeg(), "-hide_banner", "-loglevel", "error", "-f", "rawvideo",
                    "-pix_fmt", "rgb24", "-s", f"{w}x{h}", "-i", "pipe:0",
                    "-frames:v", "1", "-y", path],
                   input=np.ascontiguousarray(arr).tobytes(), check=True)


def test_jpeg_complete_detects_truncation():
    d = tempfile.mkdtemp(prefix="vs_sprite_")
    try:
        p = os.path.join(d, "a.jpg")
        _write_jpeg(p)
        assert si._jpeg_complete(p)
        blob = open(p, "rb").read()
        half = os.path.join(d, "half.jpg")
        with open(half, "wb") as f:
            f.write(blob[:len(blob) // 2])
        assert not si._jpeg_complete(half)
        empty = os.path.join(d, "empty.jpg")
        open(empty, "wb").close()
        assert not si._jpeg_complete(empty)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_load_sprites_cache_and_invalidation():
    d = tempfile.mkdtemp(prefix="vs_sprite_")
    calls = []
    real = si._download

    def fake_download(url, path):
        calls.append(url)
        _write_jpeg(path)

    si._download = fake_download
    try:
        urls = ["http://x/a.jpg", "http://x/b.jpg"]
        sprites = si.load_sprites(urls, d)
        assert len(sprites) == 2 and calls == urls

        # 二次调用：命中缓存，不该再下载
        calls.clear()
        si.load_sprites(urls, d)
        assert calls == [], f"缓存没命中：{calls}"

        # 半截的坏缓存必须重下（旧版只判文件存在，会把黑图一直用下去）
        calls.clear()
        p = os.path.join(d, "_sprite_00.jpg")
        blob = open(p, "rb").read()
        with open(p, "wb") as f:
            f.write(blob[:len(blob) // 2])
        si.load_sprites(urls, d)
        assert calls == ["http://x/a.jpg"], f"坏缓存没被发现：{calls}"

        # 图册换了 URL（同一视频目录重跑）：旧 jpg 必须作废
        calls.clear()
        si.load_sprites(["http://x/a2.jpg", "http://x/b2.jpg"], d)
        assert calls == ["http://x/a2.jpg", "http://x/b2.jpg"], calls
        cache = json.load(open(os.path.join(d, "_sprite_cache.json"), encoding="utf-8"))
        assert cache["urls"] == ["http://x/a2.jpg", "http://x/b2.jpg"]
    finally:
        si._download = real
        shutil.rmtree(d, ignore_errors=True)


def test_download_is_atomic_and_leaves_no_part_file(tmp_path=None):
    """下载用 .part 中转：中途失败不留半截文件冒充缓存。"""
    d = tempfile.mkdtemp(prefix="vs_sprite_")
    real_urlopen = si.urllib.request.urlopen
    target = os.path.join(d, "_sprite_00.jpg")

    class _FakeResp:
        def __init__(self):
            self.n = 0

        def read(self, _n):
            self.n += 1
            if self.n == 1:
                return b"x" * 1000
            raise OSError("connection reset")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    si.urllib.request.urlopen = lambda *a, **kw: _FakeResp()
    try:
        try:
            si._download("http://x/a.jpg", target)
        except OSError:
            pass
        assert not os.path.exists(target), "失败时不该留下正式缓存文件"
        assert not os.path.exists(target + ".part"), "失败时不该留下 .part"
    finally:
        si.urllib.request.urlopen = real_urlopen
        shutil.rmtree(d, ignore_errors=True)


# ───────────────────────── compose_sheet 标签 ─────────────────────────

def test_compose_sheet_burns_hms_label():
    d = tempfile.mkdtemp(prefix="vs_sprite_")
    try:
        cells = [_base(5, 18, 32), _base(6, 18, 32)]
        out = os.path.join(d, "sheet_01.png")
        si.compose_sheet(cells, [65, 5910], out, 2, 32, 18, si._font_path())
        assert os.path.isfile(out) and os.path.getsize(out) > 0
        import av
        with av.open(out) as c:
            arr = next(c.decode(video=0)).to_ndarray(format="rgb24")
        assert arr.shape[:2] == (36, 64)
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ───────────────────────── 非 B 站视频的跳过 ─────────────────────────

def test_non_bilibili_id_is_skipped_without_error():
    """YouTube 的 id 不该拿去请求 B 站接口（那会拿到 code=-400 的硬错误）。

    画面索引用的是 B 站独有的 videoshot 接口，YouTube 没有等价物。
    要求：直接跳过、不建输出目录、不发起任何网络请求。
    """
    import sys
    d = tempfile.mkdtemp(prefix="vs_sprite_")
    old_argv = sys.argv
    real_api = si._api

    def _no_network(*a, **k):
        raise AssertionError("非 B 站视频不该发起任何网络请求")

    try:
        si._api = _no_network
        sys.argv = ["sprite_index.py", "dQw4w9WgXcQ", "--out", os.path.join(d, "out")]
        si.main()
        assert not os.path.exists(os.path.join(d, "out")), "跳过的路径不该创建输出目录"
    finally:
        si._api = real_api
        sys.argv = old_argv
        shutil.rmtree(d, ignore_errors=True)
