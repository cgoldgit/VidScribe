"""publish.py 的两道门禁：物化失败必须中止；*.partial.png 不能进公开仓库。

本工作副本里**没有 git 仓库**（.git 不存在），所以这里不跑真 git：
- "哪些 png 该发布"是纯逻辑，直接离线单测；
- main() 的失败路径注入一个假 run()，它只回放 git 的返回码。
  测的是 publish 自己的控制流 —— "物化失败后到底还走不走 update-ref"、
  "临时工作树里到底有没有那个文件"，不是 git 本身的行为（那部分见
  报告里的"未验证项"：需要真仓库人工验证）。
全程 tempfile，不碰 output/、.capture-profile/，不联网。
"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import traceback

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(REPO, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import publish as pub

CHECKOUT_ERR = ("error: unable to create file BVtest1/images/shot_00_00_10.png: "
                "Permission denied")


def _tmpdir():
    return tempfile.mkdtemp(prefix="vidscribe_pub_test_")


def _make_output(base, vid="BVtest1", extra_images=()):
    """造一个最小 output/<vid>/；图片用假字节就够（publish 只做 copyfile）。"""
    d = os.path.join(base, "output", vid)
    img = os.path.join(d, "images")
    os.makedirs(img)
    for name, body in (("book.html", "<html>v1</html>\n"), ("book.md", "# 测试书\n")):
        with open(os.path.join(d, name), "w", encoding="utf-8") as f:
            f.write(body)
    for name in ("shot_00_00_10.png",) + tuple(extra_images):
        with open(os.path.join(img, name), "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n" + b"0" * 16)
    return d


def _split(args):
    """跳过 -C/--git-dir/--work-tree/-c 这些全局选项，取出子命令、剩余参数、工作树。"""
    i, work = 0, None
    while i < len(args):
        a = args[i]
        if a in ("-C", "-c", "--git-dir", "--work-tree"):
            if a == "--work-tree" and i + 1 < len(args):
                work = args[i + 1]
            i += 2
            continue
        break
    return (args[i] if i < len(args) else ""), args[i + 1:], work


def _tree_files(work):
    if not work or not os.path.isdir(work):
        return []
    out = []
    for root, _dirs, files in os.walk(work):
        for f in files:
            out.append(os.path.relpath(os.path.join(root, f), work).replace("\\", "/"))
    return sorted(out)


class FakeGit:
    """假 git：按子命令回放返回码，并记录调用链 / add 那一刻的工作树内容。"""

    def __init__(self, checkout_rc=0):
        self.checkout_rc = checkout_rc
        self.calls = []
        self.tree_files = None

    def __call__(self, args, env=None, cwd=None, check=True):
        args = list(args)
        self.calls.append(args)
        cmd, rest, work = _split(args)
        if cmd == "checkout-index":
            rc, out, err = self.checkout_rc, "", CHECKOUT_ERR
        elif cmd == "add":
            self.tree_files = _tree_files(work)
            rc, out, err = 0, "", ""
        elif cmd == "show":
            rc, out, err = 1, "", "fatal: path 'manifest.json' does not exist"
        elif cmd == "rev-parse":
            # pages_tip()：假装 pages 分支已存在；另一处是 pages^{tree}
            out = "oldpagesha\n" if "refs/heads/pages" in rest else "oldtree\n"
            rc, err = 0, ""
        elif cmd == "write-tree":
            rc, out, err = 0, "newtree\n", ""
        elif cmd == "commit-tree":
            rc, out, err = 0, "newcommit\n", ""
        else:
            rc, out, err = 0, "", ""
        if check and rc != 0:
            raise RuntimeError(f"git {' '.join(args)} failed: {err.strip()}")
        return subprocess.CompletedProcess(args, rc, out, err)

    def commands(self):
        return [_split(c)[0] for c in self.calls]


def _run_main(base, checkout_rc=0, ids=("BVtest1",)):
    """在临时 BASE 上跑一次 publish.main()（注入假 git）。返回 (fake, SystemExit|None, stderr)。"""
    fake = FakeGit(checkout_rc=checkout_rc)
    old_run, old_base, old_argv = pub.run, pub.BASE, sys.argv
    pub.run, pub.BASE = fake, base
    sys.argv = ["publish.py"] + list(ids)
    err = io.StringIO()
    exc = None
    try:
        with contextlib.redirect_stderr(err):
            try:
                pub.main()
            except SystemExit as e:
                exc = e
    finally:
        pub.run, pub.BASE, sys.argv = old_run, old_base, old_argv
    return fake, exc, err.getvalue()


def test_publishable_png_excludes_partial_and_keeps_normal():
    """发布范围的判据：*.partial.png（截图中间态）排除，正式截图保留。"""
    assert not pub.publishable_png("shot_00_01_00.partial.png")
    assert not pub.publishable_png("shot_00_01_00.PARTIAL.PNG")
    assert not pub.publishable_png("p007_shot_00_01_00.partial.png")
    assert not pub.publishable_png("book.md")
    assert pub.publishable_png("shot_00_01_00.png")
    assert pub.publishable_png("p007_shot_00_01_00.png")


def test_partial_png_is_not_copied_into_the_published_tree():
    """残留的 *.partial.png 一旦被 add 进去，就会永久留在公开仓库的 git 历史里。"""
    base = _tmpdir()
    try:
        _make_output(base, extra_images=["shot_00_01_00.partial.png"])
        fake, exc, err = _run_main(base)
        assert exc is None, f"正常情况下不该中止：{err}"
        assert fake.tree_files is not None, (
            "没有走到 add -A，检查不到发布内容：" + " -> ".join(fake.commands()))
        assert "BVtest1/images/shot_00_00_10.png" in fake.tree_files, fake.tree_files
        assert "BVtest1/images/shot_00_01_00.partial.png" not in fake.tree_files, (
            "中间态图片被发布出去了：" + repr(fake.tree_files))
        # 回归：正常路径必须照旧走到 update-ref（别把好路径一起堵死）
        assert "update-ref" in fake.commands(), (
            "正常路径没有发布：" + " -> ".join(fake.commands()))
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_src_blob_map_uses_the_same_filter():
    """幂等判断（源目录 vs 已发布）必须用同一套过滤标准：否则残留一张
    *.partial.png 就会让"内容没变"永远判成"变了"，每次都白重发一遍。"""
    base = _tmpdir()
    try:
        src = _make_output(base, extra_images=["shot_00_01_00.partial.png"])
        fake = FakeGit()
        old_run = pub.run
        pub.run = fake
        try:
            rels = sorted(pub._src_blob_map(src))
        finally:
            pub.run = old_run
        assert rels == ["book.html", "book.md", "images/shot_00_00_10.png"], rels
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_checkout_index_failure_aborts_before_update_ref():
    """物化失败 = 临时工作树里缺文件。此时绝不能走到 update-ref：
    add -A 会把缺掉的文件记成"已删除"，新树里没有它们，一更新 ref，
    这些文件就从公开的 pages 分支上消失了（还照样打印"已更新/完成"）。"""
    base = _tmpdir()
    try:
        _make_output(base)
        fake, exc, err = _run_main(base, checkout_rc=1)
        cmds = fake.commands()
        assert exc is not None, (
            "checkout-index 失败后必须中止（非零退出），实际正常返回；调用链："
            + " -> ".join(cmds))
        assert exc.code not in (0, None), f"退出码必须非零，实际 {exc.code}"
        assert "update-ref" not in cmds, (
            "checkout-index 失败后仍然执行了 update-ref，公开站点上的文件会被删掉；调用链："
            + " -> ".join(cmds))
        assert "commit-tree" not in cmds, "不该继续提交：" + " -> ".join(cmds)
        # 报错要给出可操作的原因，不是一句"失败了"。
        # （sys.exit(字符串) 的信息挂在 SystemExit.code 上：真命令行里由解释器打到
        #  stderr 并以退出码 1 收场；这里被测试捕获了，所以直接看 code。）
        msg = exc.code if isinstance(exc.code, str) else ""
        assert "unable to create file" in msg, f"没有把 git 的真实原因打出来：{msg!r}"
        assert "中止" in msg, f"没有说清已中止：{msg!r}"
    finally:
        shutil.rmtree(base, ignore_errors=True)


# ───────────── 改名保护 / 纯文字书（缺 images/）/ 拒绝部分产出 ─────────────

class _ManifestGit(FakeGit):
    """在 FakeGit 上补两件事：show 返回一份已有的 manifest；
    记下 add -A 那一刻临时工作树里的 manifest.json（= 将要提交的内容）。"""

    def __init__(self, manifest):
        super().__init__()
        self.manifest = manifest
        self.published_manifest = None

    def __call__(self, args, env=None, cwd=None, check=True):
        cmd, _rest, work = _split(list(args))
        if cmd == "show":                      # publish 读旧 manifest
            self.calls.append(list(args))
            return subprocess.CompletedProcess(
                list(args), 0, json.dumps(self.manifest, ensure_ascii=False), "")
        r = super().__call__(args, env=env, cwd=cwd, check=check)
        if cmd == "add" and work:
            p = os.path.join(work, "manifest.json")
            if os.path.isfile(p):
                with open(p, encoding="utf-8") as f:
                    self.published_manifest = json.load(f)
        return r


def _run_full(base, manifest=None, extra=(), ids=("BVtest1",)):
    """跑一次 publish.main()（注入假 git），返回 (fake, 异常|None, stdout+stderr)。"""
    fake = FakeGit() if manifest is None else _ManifestGit(manifest)
    old_run, old_base, old_argv = pub.run, pub.BASE, sys.argv
    pub.run, pub.BASE = fake, base
    sys.argv = ["publish.py"] + list(ids) + list(extra)
    out = io.StringIO()
    exc = None
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            try:
                pub.main()
            except BaseException as e:       # noqa: BLE001 —— 堆栈要抓下来看
                exc = e
                out.write("\n" + traceback.format_exc())
    finally:
        pub.run, pub.BASE, sys.argv = old_run, old_base, old_argv
    return fake, exc, out.getvalue()


def _exit_code(exc):
    """正常返回记 0；SystemExit 取它的码；其它异常记 1。"""
    if exc is None:
        return 0
    if isinstance(exc, SystemExit):
        return exc.code if isinstance(exc.code, int) else 1
    return 1


def _make_output_without_images(base, vid="BVtest1"):
    """纯文字书：有 book.html / book.md，没有 images/ 目录。"""
    d = os.path.join(base, "output", vid)
    os.makedirs(d)
    for name, body in (("book.html", "<html>纯文字</html>\n"), ("book.md", "# 纯文字书\n")):
        with open(os.path.join(d, name), "w", encoding="utf-8") as f:
            f.write(body)
    return d


def test_unreadable_title_keeps_published_folder():
    """output/<id>/transcript.json 被清理后读不到标题，safe_folder 会回落到 video_id，
    与 manifest 里的旧目录名不等 —— 旧版会删掉旧目录再改名，已公开的链接全废。"""
    base = _tmpdir()
    try:
        _make_output(base)                 # 注意：没有 transcript.json
        manifest = {"BVtest1": {"folder": "已公开的书名", "title": "已公开的书名",
                                "updated": "2026-01-01T00:00:00+08:00", "corrected": False}}
        fake, exc, text = _run_full(base, manifest=manifest)

        assert _exit_code(exc) == 0, f"应当照常发布而不是中止：\n{text}"
        assert "目录改名" not in text, "读不到标题时仍然动了目录名：\n" + text
        assert "保持" in text or "不改名" in text, "没有给出明确警告：\n" + text
        assert fake.published_manifest is not None, "没走到 add -A：" + " -> ".join(fake.commands())
        assert fake.published_manifest["BVtest1"]["folder"] == "已公开的书名", (
            "已公开的目录名被改掉了：" + repr(fake.published_manifest))
        assert "update-ref" in fake.commands(), (
            "改名保护把正常发布一起堵死了：" + " -> ".join(fake.commands()))
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_first_publish_without_manifest_entry_is_not_blocked():
    """manifest 里没有这本书（第一次发布）时走正常流程：回落到 video_id 当目录名。"""
    base = _tmpdir()
    try:
        _make_output(base)
        fake, exc, text = _run_full(base, manifest={})
        assert _exit_code(exc) == 0, f"首次发布被堵死了：\n{text}"
        assert "update-ref" in fake.commands(), " -> ".join(fake.commands())
        assert fake.published_manifest["BVtest1"]["folder"] == "BVtest1", (
            repr(fake.published_manifest))
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_missing_images_dir_does_not_crash():
    """纯文字书没有 images/：不能把 Python 堆栈甩给用户。"""
    base = _tmpdir()
    try:
        _make_output_without_images(base)
        fake, exc, text = _run_full(base, manifest={})
        assert exc is None, f"没有 images/ 目录时抛了异常：\n{text}"
        assert "Traceback" not in text, text
        assert "update-ref" in fake.commands(), " -> ".join(fake.commands())
        assert "BVtest1/book.html" in fake.tree_files, fake.tree_files
        assert "BVtest1/book.md" in fake.tree_files, fake.tree_files
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_src_blob_map_tolerates_missing_images_dir():
    """幂等比对（_src_blob_map）里同样的 os.listdir 也不能炸。"""
    base = _tmpdir()
    try:
        src = _make_output_without_images(base)
        fake = FakeGit()
        old_run = pub.run
        pub.run = fake
        try:
            rels = sorted(pub._src_blob_map(src))
        finally:
            pub.run = old_run
        assert rels == ["book.html", "book.md"], rels
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_publish_rejects_partial_book_unless_flagged():
    """发布默认拒绝 merge_book --allow-partial 的成品（首页带缺失标记）。"""
    base = _tmpdir()
    try:
        import merge_book as mb                    # 两个模块得认同一个标记
        assert mb.PARTIAL_MARK == pub.PARTIAL_MARK, "两边的缺失标记不是同一个字符串"

        d = _make_output(base)
        with open(os.path.join(d, "book.md"), "w", encoding="utf-8") as f:
            f.write("# 某书\n\n> 本册为部分产出：缺 p2\n\n" + pub.PARTIAL_MARK + "\n")

        fake, exc, text = _run_full(base, manifest={})
        assert _exit_code(exc) != 0, f"缺内容的成品不该默认发布出去：\n{text}"
        assert "update-ref" not in fake.commands(), (
            "拒绝之前已经把内容提交上去了：" + " -> ".join(fake.commands()))

        fake2, exc2, text2 = _run_full(base, manifest={}, extra=("--allow-partial",))
        assert _exit_code(exc2) == 0, f"显式允许时应当发布：\n{text2}"
        assert "update-ref" in fake2.commands(), " -> ".join(fake2.commands())
    finally:
        shutil.rmtree(base, ignore_errors=True)


# ───────────────────────── 落地页把远端标题当数据 ─────────────────────────

def test_landing_page_escapes_remote_title():
    """书架页把远端视频标题当**数据**、不是当标记（R0-07 证据里点名的另一处）。

    标题来自平台上传者，是不可信输入，而这个 index.html 是要 push 到公开
    pages 的：一个叫 `<img src=x onerror=alert(1)>` 的视频标题，不能让每个
    打开书架的人都执行它。
    """
    import html as _html
    from html.parser import HTMLParser

    hostile = '<img src=x onerror=alert(1)>'

    class Probe(HTMLParser):
        def __init__(self):
            super().__init__()
            self.tags, self.attrs = set(), set()

        def _scan(self, tag, attrs):
            self.tags.add(tag)
            for k, _v in attrs:
                if k.lower().startswith("on"):
                    self.attrs.add(k.lower())

        handle_starttag = _scan
        handle_startendtag = _scan

    manifest = {"BVbad": {"folder": "正常书名", "title": hostile,
                          "updated": "2026-09-22T12:00:00+08:00", "corrected": False}}
    out = pub.build_index_html(manifest)

    probe = Probe()
    probe.feed(out)
    assert "img" not in probe.tags, "标题里的 img 变成了真标签"
    assert not probe.attrs, f"出现了内联事件属性：{sorted(probe.attrs)}"
    assert _html.escape(hostile) in out, "标题应被整体转义后作为文本出现"
