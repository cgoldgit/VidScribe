"""merge_book.py 的回归测试（离线，全部在 tempfile 临时目录里跑）。

对应 2026-09-22 code review 报告 R0-05 里本次负责的两条：
  · 写模式收集到 problems（缺集 / 缺图 / 残留占位符）后仍照写成品并返回 True
    → 退出码 0，成品里留着悬空占位符和坏图链接；
  · 目标图已存在就不覆盖 → 单集重截的高清帧传播不到合集，
    下游 publish.py 比对"内容无变化"，高清版永远发布不出去。

刻意不做（另案处理）：
staging 目录完整重建 / 清理旧图 / books.json 结构校验 / 图片计数口径。
"""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import traceback

from _helpers import SRC  # noqa: F401  (SRC 保证能 import src/)

import merge_book as mb

BVID = "BVtest1"
BOOK = "测试书"
PNG_SIG = b"\x89PNG\r\n\x1a\n"


def _tmpdir():
    return tempfile.mkdtemp(prefix="vs_test_")


def _png(payload: bytes) -> bytes:
    """一段假的 PNG 字节。merge_book 只搬运文件、不解析内容，够用了。"""
    return PNG_SIG + payload + b"\x00"


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def _write_bytes(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def _env(d, episodes):
    """在临时目录里造一份假的 books.json（BASE_DIR 指向 d）。"""
    cfg = {
        "source": "测试课程",
        "bvid": BVID,
        "source_url": "https://example.invalid/BVtest1",
        "books": [{"dir": BOOK, "episodes": list(episodes)}],
    }
    with open(os.path.join(d, "books.json"), "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False)


def _episode(d, p, img=None, img_bytes=None, placeholder=None):
    """造一集的成品稿：`#` 标题 + 代码块 + 可选图片引用 / 残留占位符。"""
    vdir = os.path.join(d, "output", f"{BVID}_p{p}")
    md = (f"# 第 {p} 集标题\n\n"
          "```c\n"
          "# 围栏里的井号不该被降级\n"
          "```\n\n"
          "本集正文。\n")
    if img:
        md += f"\n![截图](images/{img})\n"
    if placeholder:
        md += f"\n![漏截](SCREENSHOT:{placeholder})\n"
    _write(os.path.join(vdir, "book.md"), md)
    if img and img_bytes is not None:
        _write_bytes(os.path.join(vdir, "images", img), img_bytes)


def _run(argv):
    """按命令行方式跑 main()，返回退出码。"""
    old = sys.argv
    sys.argv = ["merge_book.py"] + argv
    try:
        try:
            mb.main()
        except SystemExit as e:
            return e.code if isinstance(e.code, int) else 1
        raise AssertionError("main() 没有以 SystemExit 结束")
    finally:
        sys.argv = old


def _product(d):
    return os.path.join(d, "output", BOOK)


def _snapshot(root):
    """整个成品目录（含子目录）的 相对路径 → 字节。"""
    out = {}
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            with open(full, "rb") as f:
                out[os.path.relpath(full, root)] = f.read()
    return out


def _patch_base(d):
    """把模块级 BASE_DIR / BOOKS_JSON 指向临时目录，返回原值以便还原。"""
    old = (mb.BASE_DIR, mb.BOOKS_JSON)
    mb.BASE_DIR = d
    mb.BOOKS_JSON = os.path.join(d, "books.json")
    return old


def _restore_base(old):
    mb.BASE_DIR, mb.BOOKS_JSON = old


# ───────────────────────── 陈旧图片 ─────────────────────────

def test_stale_image_is_overwritten_from_source():
    """单集重截后，合集里的同名图必须跟着换成源图（否则高清永远发不出去）。"""
    d = _tmpdir()
    old = _patch_base(d)
    try:
        _env(d, [1])
        _episode(d, 1, img="shot_00_00_10.png", img_bytes=_png(b"new-hd"))
        # 上一轮合并留下的旧图（同名，老内容是糊的）
        dst = os.path.join(_product(d), "images", "p001_shot_00_00_10.png")
        _write_bytes(dst, _png(b"old-480p"))

        code = _run(["--all"])
        assert code == 0, f"退出码应当是 0，实际 {code}"
        with open(dst, "rb") as f:
            got = f.read()
        assert got == _png(b"new-hd"), "合集里还是旧的糊图，重截的帧没传播过来"
    finally:
        _restore_base(old)
        shutil.rmtree(d, ignore_errors=True)


# ───────────────────────── 有问题就必须拒绝 ─────────────────────────

def _assert_fails_closed(d, why):
    """有问题时：非零退出，且已有成品逐字节不变（fail-closed）。"""
    prod = _product(d)
    _write(os.path.join(prod, "book.md"),
           "# 上一版成品\n\n![图](images/p001_shot_00_00_10.png)\n")
    _write_bytes(os.path.join(prod, "images", "p001_shot_00_00_10.png"),
                 _png(b"last-good"))
    before = _snapshot(prod)

    code = _run(["--all"])
    assert code != 0, f"{why}时不该报成功（退出码 {code}）"
    assert _snapshot(prod) == before, f"{why}时改动了已有成品"


def test_missing_episode_fails_closed():
    d = _tmpdir()
    old = _patch_base(d)
    try:
        _env(d, [1, 2])
        _episode(d, 1, img="shot_00_00_10.png", img_bytes=_png(b"e1"))
        _episode(d, 2, img="shot_00_01_20.png", img_bytes=_png(b"e2"))
        os.remove(os.path.join(d, "output", f"{BVID}_p2", "book.md"))   # 第 2 集没写
        _assert_fails_closed(d, "缺一集")
    finally:
        _restore_base(old)
        shutil.rmtree(d, ignore_errors=True)


def test_missing_image_fails_closed():
    d = _tmpdir()
    old = _patch_base(d)
    try:
        _env(d, [1])
        _episode(d, 1, img="shot_00_00_10.png", img_bytes=_png(b"e1"))
        os.remove(os.path.join(d, "output", f"{BVID}_p1", "images", "shot_00_00_10.png"))
        _assert_fails_closed(d, "缺一张图")
    finally:
        _restore_base(old)
        shutil.rmtree(d, ignore_errors=True)


def test_leftover_placeholder_fails_closed():
    d = _tmpdir()
    old = _patch_base(d)
    try:
        _env(d, [1])
        _episode(d, 1, img="shot_00_00_10.png", img_bytes=_png(b"e1"),
                 placeholder="00:01:00")
        _assert_fails_closed(d, "还有没物化的截屏占位符")
    finally:
        _restore_base(old)
        shutil.rmtree(d, ignore_errors=True)


# ───────────────────────── 回归 ─────────────────────────

def test_all_present_merges_normally():
    """全齐时照旧：退出码 0、标题降一级、图片加 pNNN_ 前缀并复制到位。"""
    d = _tmpdir()
    old = _patch_base(d)
    try:
        _env(d, [1, 2])
        _episode(d, 1, img="shot_00_00_10.png", img_bytes=_png(b"e1"))
        _episode(d, 2, img="shot_00_01_20.png", img_bytes=_png(b"e2"))

        code = _run(["--all"])
        assert code == 0, f"退出码应当是 0，实际 {code}"

        with open(os.path.join(_product(d), "book.md"), encoding="utf-8") as f:
            body = f.read()
        assert body.startswith("# 测试书"), "没有 _intro.md 时应写默认导语"
        assert "## 第 1 集标题" in body and "## 第 2 集标题" in body, "标题没降级"
        assert "# 围栏里的井号不该被降级" in body, "代码块里的井号被动了"
        assert "![截图](images/p001_shot_00_00_10.png)" in body
        assert "![截图](images/p002_shot_00_01_20.png)" in body

        for fn, want in (("p001_shot_00_00_10.png", b"e1"),
                         ("p002_shot_00_01_20.png", b"e2")):
            with open(os.path.join(_product(d), "images", fn), "rb") as f:
                assert f.read() == _png(want), fn
    finally:
        _restore_base(old)
        shutil.rmtree(d, ignore_errors=True)


def test_check_mode_semantics_unchanged():
    """--check 只体检不写文件：就绪 0，未就绪非零。"""
    d = _tmpdir()
    old = _patch_base(d)
    try:
        _env(d, [1])
        _episode(d, 1, img="shot_00_00_10.png", img_bytes=_png(b"e1"))

        assert _run(["--all", "--check"]) == 0, "齐活时 --check 应当报就绪"
        assert not os.path.exists(_product(d)), "--check 不该写任何文件"

        os.remove(os.path.join(d, "output", f"{BVID}_p1", "images", "shot_00_00_10.png"))
        assert _run(["--all", "--check"]) != 0, "缺图时 --check 应当报未就绪"
        assert not os.path.exists(_product(d)), "--check 不该写任何文件"
    finally:
        _restore_base(old)
        shutil.rmtree(d, ignore_errors=True)


# ───────────── R0-05 剩余项：配置校验 / staging 重建 / 计数口径 / --allow-partial ─────────────

def _run_cap(argv):
    """按命令行方式跑 main()，返回 (退出码, stdout+stderr 文本)。

    未捕获的异常（Python 堆栈）也算失败：退出码记 1，堆栈写进文本，
    好让"不许甩 traceback 给用户"这类断言能直接查。
    """
    old = sys.argv
    sys.argv = ["merge_book.py"] + argv
    out = io.StringIO()
    code = 1
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            try:
                mb.main()
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else 1
                if isinstance(e.code, str):
                    out.write("\n" + e.code)
            except BaseException:            # noqa: BLE001 —— 就是要把堆栈抓下来看
                out.write("\n" + traceback.format_exc())
    finally:
        sys.argv = old
    return code, out.getvalue()


def _env_raw(d, cfg):
    """直接往临时目录里塞一份指定的 books.json（用来造非法配置）。"""
    with open(os.path.join(d, "books.json"), "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False)


def _base_cfg(**over):
    cfg = {"source": "测试课程", "bvid": BVID,
           "source_url": "https://example.invalid/BVtest1",
           "books": [{"dir": BOOK, "episodes": [1]}]}
    cfg.update(over)
    return cfg


def test_invalid_books_json_exits_nonzero():
    """R0-05 第 5 条：books.json 结构非法要明确报错、非零退出、不许甩堆栈。"""
    cases = {
        "缺 bvid": {"source": "测试课程", "source_url": "https://x.invalid/",
                    "books": [{"dir": BOOK, "episodes": [1]}]},
        "缺 source_url": {"source": "测试课程", "bvid": BVID,
                          "books": [{"dir": BOOK, "episodes": [1]}]},
        "缺 episodes": {"source": "测试课程", "bvid": BVID,
                        "source_url": "https://x.invalid/",
                        "books": [{"dir": BOOK}]},
        "episodes 不是数组": _base_cfg(books=[{"dir": BOOK, "episodes": "1,2"}]),
        "重复集数": _base_cfg(books=[{"dir": BOOK, "episodes": [1, 1]}]),
        "集数不是正整数": _base_cfg(books=[{"dir": BOOK, "episodes": [0]}]),
        "目录名带路径分隔符": _base_cfg(books=[{"dir": "坏/名字", "episodes": [1]}]),
        "两本书同名目录": _base_cfg(books=[{"dir": BOOK, "episodes": [1]},
                                            {"dir": BOOK, "episodes": [1]}]),
    }
    for why, cfg in cases.items():
        d = _tmpdir()
        old = _patch_base(d)
        try:
            _env(d, [1])                       # 稿件和图都齐活：唯一的毛病只有配置
            _episode(d, 1, img="shot_00_00_10.png", img_bytes=_png(b"e1"))
            _env_raw(d, cfg)

            code, text = _run_cap(["--all"])
            assert code != 0, f"{why} 时应当非零退出（实际 {code}）：\n{text}"
            assert "Traceback" not in text, f"{why} 时把 Python 堆栈甩给了用户：\n{text}"
            assert "books.json" in text, f"{why} 时没说清是配置的问题：\n{text}"
            assert sorted(os.listdir(os.path.join(d, "output"))) == [f"{BVID}_p1"], (
                f"{why} 时配置非法却照样动了 output/："
                + repr(sorted(os.listdir(os.path.join(d, "output")))))
        finally:
            _restore_base(old)
            shutil.rmtree(d, ignore_errors=True)


def test_stale_orphan_image_is_removed_on_remerge():
    """R0-05 第 3、4 条：在同级 staging 完整重建；正文已不引用的旧图必须消失。"""
    d = _tmpdir()
    old = _patch_base(d)
    try:
        _env(d, [1])
        _episode(d, 1, img="shot_00_00_10.png", img_bytes=_png(b"e1-hd"))
        prod = _product(d)
        # 上一轮的成品：一张正文还在引用的图（内容是旧的糊图）+ 一张正文已删的孤儿图
        _write(os.path.join(prod, "book.md"), "# 上一版成品\n")
        _write_bytes(os.path.join(prod, "images", "p001_shot_00_00_10.png"),
                     _png(b"old-480p"))
        _write_bytes(os.path.join(prod, "images", "p001_shot_00_09_99.png"),
                     _png(b"orphan"))

        code, text = _run_cap(["--all"])
        assert code == 0, text
        with open(os.path.join(prod, "images", "p001_shot_00_00_10.png"), "rb") as f:
            assert f.read() == _png(b"e1-hd"), "成品里的图不是以源图为准覆盖的"
        assert not os.path.exists(os.path.join(prod, "images", "p001_shot_00_09_99.png")), (
            "正文已不再引用的旧图仍留在成品目录里（staging 重建没生效）")
    finally:
        _restore_base(old)
        shutil.rmtree(d, ignore_errors=True)


def test_orphan_image_does_not_count_into_image_total():
    """R0-05 第 6 条：图片数按正文实际引用统计，不是目录里的文件数。"""
    d = _tmpdir()
    old = _patch_base(d)
    try:
        _env(d, [1, 2])
        _episode(d, 1, img="shot_00_00_10.png", img_bytes=_png(b"e1"))
        _episode(d, 2, img="shot_00_01_20.png", img_bytes=_png(b"e2"))
        prod = _product(d)
        # 成品目录里塞两张正文不引用的历史遗留图
        _write_bytes(os.path.join(prod, "images", "p001_orphan_a.png"), _png(b"a"))
        _write_bytes(os.path.join(prod, "images", "p002_orphan_b.png"), _png(b"b"))

        code, text = _run_cap(["--all"])
        assert code == 0, text
        assert "图片 2 张" in text, f"图片数没有按正文引用统计（目录里另有 2 张孤儿图）：\n{text}"
    finally:
        _restore_base(old)
        shutil.rmtree(d, ignore_errors=True)


def test_intro_referenced_image_survives_rebuild():
    """导语里引用的图（不属于任何一集，只存在成品目录里）不能在重建时被删掉。"""
    d = _tmpdir()
    old = _patch_base(d)
    try:
        _env(d, [1])
        _episode(d, 1, img="shot_00_00_10.png", img_bytes=_png(b"e1"))
        prod = _product(d)
        _write_bytes(os.path.join(prod, "images", "cover.png"), _png(b"cover"))
        _write(os.path.join(prod, "_intro.md"), "# 测试书\n\n![封面](images/cover.png)\n")

        code, text = _run_cap(["--all"])
        assert code == 0, text
        with open(os.path.join(prod, "book.md"), encoding="utf-8") as f:
            body = f.read()
        assert "![封面](images/cover.png)" in body, "导语的图片引用被改掉了"
        assert os.path.exists(os.path.join(prod, "images", "cover.png")), (
            "导语还在引用的图被 staging 重建删掉了")
    finally:
        _restore_base(old)
        shutil.rmtree(d, ignore_errors=True)


def test_allow_partial_gates_partial_output():
    """R0-05 第 2 条：不传 --allow-partial 时缺集不许产出；传了才产出且首页标注缺失。"""
    d = _tmpdir()
    old = _patch_base(d)
    try:
        _env(d, [1, 2])
        _episode(d, 1, img="shot_00_00_10.png", img_bytes=_png(b"e1"))
        _episode(d, 2, img="shot_00_01_20.png", img_bytes=_png(b"e2"))
        os.remove(os.path.join(d, "output", f"{BVID}_p2", "book.md"))    # 第 2 集没写

        prod = _product(d)
        _write(os.path.join(prod, "book.md"), "# 上一版成品\n")
        before = _snapshot(prod)

        code, text = _run_cap(["--all"])
        assert code != 0, f"没传 --allow-partial 时不该报成功：\n{text}"
        assert _snapshot(prod) == before, "没传 --allow-partial 时改动了已有成品"

        code, text = _run_cap(["--all", "--allow-partial"])
        assert code == 0, f"传了 --allow-partial 应当产出：\n{text}"
        with open(os.path.join(prod, "book.md"), encoding="utf-8") as f:
            body = f.read()
        head = "\n".join(body.splitlines()[:6])
        assert "p2" in head and "缺" in head, f"首页没有标注缺失内容：\n{head}"
        assert "## 第 1 集标题" in body, "已有内容的集应当照常合并进去"
        assert mb.PARTIAL_MARK in body, "没有写下 publish 用来识别「部分产出」的标记"
    finally:
        _restore_base(old)
        shutil.rmtree(d, ignore_errors=True)
