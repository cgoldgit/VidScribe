"""post_process.py 的成败必须能从退出码看出来。

起因（审查发现）：`process_markdown()` 在 md 文件不存在时只打印 `[ERR]` 然后 return，
`main()` 不看返回值 → 整条命令**退出码 0**。instructions.md 让 AI 跑的就是
`python src/post_process.py "<VIDEO_URL>" output/<video_id>/book.md`，
调度方看到 0 就当成"书已生成"，于是"一个字都没产出"被报成了成功。

退出码是子进程行为，进程内断言看不到，所以这里用真子进程跑（不联网，只用 tempfile）。
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from html.parser import HTMLParser
from urllib.parse import urlparse

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(REPO, "src")
SCRIPT = os.path.join(SRC, "post_process.py")
URL = "https://www.bilibili.com/video/BV1xx411c7mD"

if SRC not in sys.path:
    sys.path.insert(0, SRC)

import post_process as pp


def _tmpdir():
    return tempfile.mkdtemp(prefix="vidscribe_pp_test_")


def _run(md_path):
    """跑真命令；stdout 按字节收（中文 Windows 下子进程走 GBK 管道），只认 ASCII 标记。"""
    return subprocess.run([sys.executable, SCRIPT, URL, md_path],
                          cwd=REPO, capture_output=True)


def test_missing_input_file_exits_nonzero():
    """输入文件不存在时不能退出 0（否则自动调度会把"没生成 HTML"当成功）。"""
    d = _tmpdir()
    try:
        missing = os.path.join(d, "book.md")  # 故意不创建
        p = _run(missing)
        assert p.returncode != 0, (
            f"输入文件不存在时退出码必须非零，实际 {p.returncode}；"
            f"stdout={p.stdout!r} stderr={p.stderr!r}")
        assert not os.path.exists(os.path.join(d, "book.html"))
        # 报错本身也要留在输出里（"[ERR]" 是 ASCII，不受控制台编码影响）
        assert b"[ERR]" in p.stdout, f"没有打印可读的失败原因：{p.stdout!r}"
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_process_markdown_reports_failure_to_its_caller():
    """process_markdown() 要用返回值表达失败（main() 靠它设退出码），不能静默 return。"""
    d = _tmpdir()
    try:
        got = pp.process_markdown(URL, os.path.join(d, "book.md"))
        assert not got, f"文件不存在时应返回失败状态，实际 {got!r}"
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_normal_input_still_exits_zero_and_writes_html():
    """回归：正常输入必须照旧生成 HTML 并退出 0（别把好路径弄坏）。"""
    d = _tmpdir()
    try:
        md = os.path.join(d, "book.md")
        with open(md, "w", encoding="utf-8") as f:
            f.write("# 测试书\n\n> 引言\n\n正文。\n\n![第一张](SCREENSHOT:00:00:10)\n")
        p = _run(md)
        assert p.returncode == 0, (
            f"正常输入应退出 0，实际 {p.returncode}；stdout={p.stdout!r} stderr={p.stderr!r}")

        html = os.path.join(d, "book.html")
        assert os.path.isfile(html), "没有生成 book.html"
        with open(html, encoding="utf-8") as f:
            text = f.read()
        assert "SCREENSHOT:00:00:10" not in text, "占位符没有被替换成播放器卡片"
        assert "player.bilibili.com" in text, "B 站 iframe 没生成"
        assert "测试书" in text, "标题没有写进 <title>"
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ─────────────────────────────────────────────
# R0-07：关闭 HTML 注入面
# ─────────────────────────────────────────────
# 起因（审查发现）：md_to_html / make_video_card 对**不可信外部数据**（视频标题、
# 字幕、AI 写的 Markdown）零转义 —— `<script>`、`<img onerror>`、`javascript:`
# 链接、闭合 `</title>` 全部原样进成品；Markdown 里的原始 HTML 也直通。
# 成品发布到 GitHub Pages，等于存储型 XSS。
#
# 下面的断言都盯"**可执行形式**"：先解析成品，再看标签和属性 —— 而不是拿字符串猜。
# （`<script` 作为正文文本出现是安全的，出现在标签位置才危险。）

BILI_URL = "https://www.bilibili.com/video/BV1xx411c7mD"
REAL_BOOK = os.path.join(REPO, "output", "BV1rXwNzuEmJ")
REAL_URL = "https://www.bilibili.com/video/BV1rXwNzuEmJ"
_FRAME_HOSTS = ("player.bilibili.com", "www.youtube-nocookie.com")


def _run_with(url, md_path):
    """和 _run 一样跑真命令，只是能换 URL（回归真实成品时要用它自己的链接）。"""
    return subprocess.run([sys.executable, SCRIPT, url, md_path],
                          cwd=REPO, capture_output=True)


class _Scanner(HTMLParser):
    """收集元素（标签 + 属性）和文本节点；属性值由解析器解码后拿到。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.elements = []
        self.text = []

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, {k.lower(): (v or "") for k, v in attrs}))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_data(self, data):
        self.text.append(data)


class _CodeText(HTMLParser):
    """取出每个 <code> 元素的文本（模拟页面里那段 mermaid 扫描脚本的 textContent）。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.codes = []
        self._in_code = False

    def handle_starttag(self, tag, attrs):
        if tag == "code":
            self._in_code = True
            self.codes.append("")

    def handle_endtag(self, tag):
        if tag == "code":
            self._in_code = False

    def handle_data(self, data):
        if self._in_code and self.codes:
            self.codes[-1] += data


def _scan(html_text):
    s = _Scanner()
    s.feed(html_text)
    s.close()
    return s


def _code_texts(html_text):
    c = _CodeText()
    c.feed(html_text)
    c.close()
    return c.codes


def _content(html_text):
    """只看 <article class="book-content"> 里注入的正文（模板自带的脚本在它外面）。"""
    start = html_text.index('<article class="book-content">')
    end = html_text.index("</article>", start)
    return html_text[start:end]


def _elements(scanned, tag):
    return [attrs for t, attrs in scanned.elements if t == tag]


def _render(md_text, url=BILI_URL, images_dir=None):
    """和 process_markdown 走同一条路：抽标题 → 占位符→卡片 → 清洗正文→注入卡片。"""
    m = re.search(r"^#\s+(.+)", md_text, re.MULTILINE)
    title = m.group(1).strip() if m else "VidScribe"
    md = pp.replace_screenshots_with_embeds(md_text, url, images_dir=images_dir)
    return pp.md_to_html(md, title)


def _frame_host(src):
    try:
        return (urlparse(src).hostname or "").lower()
    except ValueError:
        return ""


def _dangerous(html_fragment):
    """列出"能执行 / 能外连"的东西：脚本类标签、事件属性、危险 scheme。

    iframe 只放行播放器域（卡片自带的那两个），别的都算危险。
    """
    bad = []
    for tag, attrs in _scan(html_fragment).elements:
        if tag in ("script", "object", "embed", "style", "link", "meta", "base", "form"):
            bad.append("标签 <%s>" % tag)
        elif tag == "iframe" and _frame_host(attrs.get("src", "")) not in _FRAME_HOSTS:
            bad.append("外域 iframe <%s>" % attrs.get("src", ""))
        for name, value in attrs.items():
            if name.startswith("on"):
                bad.append("事件属性 <%s %s=...>" % (tag, name))
            if name in ("href", "src", "action", "srcdoc"):
                cleaned = re.sub(r"[\x00-\x20]", "", value).lower()
                if cleaned.startswith(("javascript:", "data:", "vbscript:", "file:")):
                    bad.append("危险 scheme <%s %s=%s>" % (tag, name, value))
    return bad


def _image_dir(d):
    """临时目录里造一张"已存在的截图"（只判存在性，文件可以是空的）。"""
    images = os.path.join(d, "images")
    os.makedirs(images, exist_ok=True)
    open(os.path.join(images, "shot_00_00_10.png"), "wb").close()
    return images


def test_body_script_cannot_execute():
    """正文里的 <script> 不许以可执行形式进页面。"""
    html = _render("# 书\n\n开头 <script>alert(1)</script> 结尾。\n")
    content = _content(html)
    bad = _dangerous(content)
    assert not bad, "正文里的 <script> 仍然可执行: %s" % bad
    assert not _elements(_scan(content), "script"), "正文里出现了真的 script 标签"
    # 只处理标签，不整段删正文
    assert "开头" in "".join(_scan(content).text) and "结尾" in "".join(_scan(content).text)


def test_raw_html_event_attributes_are_stripped():
    """原始 HTML 的事件属性（onerror / onclick）一律剥掉。"""
    html = _render('# 书\n\n<img src="images/shot_00_00_10.png" onerror="alert(1)">\n\n'
                   '<div onclick="alert(2)">点我</div>\n')
    content = _content(html)
    assert not _dangerous(content), "原始 HTML 的事件属性还在: %s" % _dangerous(content)
    for _tag, attrs in _scan(content).elements:
        assert not [n for n in attrs if n.startswith("on")], attrs


def test_screenshot_caption_payload_stays_text():
    """图片描述里的 <img onerror> 只能当文本/属性值，不能变成真标签。"""
    payload = '<img src=x onerror=alert(2)>'
    d = _tmpdir()
    try:
        html = _render("# 书\n\n![%s](SCREENSHOT:00:00:10)\n" % payload,
                       images_dir=_image_dir(d))
        content = _content(html)
        assert not _dangerous(content), "描述里的 onerror 逃进了页面: %s" % _dangerous(content)
        imgs = _elements(_scan(content), "img")
        assert len(imgs) == 1, "描述里的 <img> 变成了真的标签: %r" % imgs
        assert imgs[0]["src"] == "images/shot_00_00_10.png", imgs[0]
        # alt 必须**等于**原始描述 —— 说明它被当成了数据（而不是标记）
        assert imgs[0]["alt"] == payload, "描述被改坏了: %r" % imgs[0]["alt"]
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_caption_quote_cannot_start_a_new_attribute():
    """描述里的引号不许逃出 alt/href 的引号（属性注入）。"""
    payload = 'x" onerror="alert(9)'
    d = _tmpdir()
    try:
        images = _image_dir(d)
        md = "# 书\n\n![%s](SCREENSHOT:00:00:10)\n" % payload
        # 截图卡片（描述进 alt 属性）和播放器卡片（描述进 caption 文本）都要过
        for html in (_render(md, images_dir=images), _render(md)):
            content = _content(html)
            assert not _dangerous(content), "引号逃逸后出现了事件属性: %s" % _dangerous(content)
            for _tag, attrs in _scan(content).elements:
                assert "onerror" not in attrs, attrs
        imgs = _elements(_scan(_content(_render(md, images_dir=images))), "img")
        assert imgs[0]["alt"] == payload, "alt 被引号截断了: %r" % imgs[0]["alt"]
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_heading_cannot_break_out_of_title():
    """标题里的 </title><script> 只能当文本（标签结构不许变）。"""
    html = _render("# </title><script>alert(3)</script>\n\n正文。\n")
    benign = _render("# 普通标题\n\n正文。\n")
    sig = [(t, tuple(sorted(a))) for t, a in _scan(html).elements]
    sig_benign = [(t, tuple(sorted(a))) for t, a in _scan(benign).elements]
    assert sig == sig_benign, "恶意标题改变了页面结构（多出了标签）"
    assert html.count("<title>") == 1 and html.count("</title>") == 1, "title 被闭合掉了"
    assert "&lt;/title&gt;" in html, "标题没有做 HTML 实体转义"


def test_third_party_iframe_is_removed():
    """正文里的第三方 iframe 一律剥掉（只允许卡片自带的那两个播放器域）。"""
    html = _render('# 书\n\n<iframe src="https://evil.example/steal"></iframe>\n\n正文。\n')
    content = _content(html)
    assert not _elements(_scan(content), "iframe"), "第三方 iframe 被保留了"
    assert not _dangerous(content), _dangerous(content)


def test_void_tag_inside_dropped_block_does_not_eat_the_rest():
    """危险标签里套了空元素（<svg><input></svg>）时，后面的正文不能被连带吃掉。

    （实现里给"丢弃深度"计数，空元素没有结束标签，会把它顶在 >0 上。）
    """
    html = _render("# 书\n\n<svg><input></svg>\n\n后面还有正文。\n")
    content = _content(html)
    assert "后面还有正文" in content, "空元素顶住了丢弃深度，后面整篇正文都没了"
    assert not _dangerous(content), _dangerous(content)


def test_dangerous_urls_are_dropped():
    """javascript: / data: / vbscript:（Markdown 写法和原始 HTML 写法）都不留。"""
    cases = [
        "[点我](javascript:alert(1))",
        "[点我](data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==)",
        '<a href="javascript:alert(1)">点我</a>',
        "![图](data:image/png;base64,AAAA)",
        '<img src="javascript:alert(1)">',
    ]
    for case in cases:
        content = _content(_render("# 书\n\n%s\n" % case))
        bad = _dangerous(content)
        assert not bad, "%s 仍然可执行: %s" % (case, bad)
        for _tag, attrs in _scan(content).elements:
            for name in ("href", "src"):
                value = re.sub(r"[\x00-\x20]", "", attrs.get(name, "")).lower()
                assert not value.startswith(("javascript:", "data:", "vbscript:")), (case, attrs)


def test_dangerous_video_url_is_rejected():
    """javascript: 之类当 video_url → 平台判不出来，卡片里也不许出现这种链接。"""
    for bad_url in ("javascript:alert(1)",
                    "data:text/html,<script>alert(1)</script>",
                    "vbscript:msgbox(1)",
                    "//evil.example/player"):
        assert pp.detect_platform(bad_url) == "unknown", bad_url
        content = _content(_render("# 书\n\n![图](SCREENSHOT:00:00:10)\n", url=bad_url))
        assert not _dangerous(content), "%s 变成了可执行内容: %s" % (bad_url, _dangerous(content))
        assert "player.bilibili.com" not in content, "坏 URL 竟然生成了播放器"


def test_platform_is_decided_by_parsed_host():
    """平台必须按**解析后的 host** 判，不能用子串（evil.com/?bilibili.com 骗不过去）。"""
    assert pp.detect_platform("https://evil.com/?bilibili.com") == "unknown"
    assert pp.detect_platform("https://evil.com/youtube.com/watch?v=1") == "unknown"
    assert pp.detect_platform("https://bilibili.com.evil.com/video/BV1xx411c7mD") == "unknown"
    assert pp.detect_platform("https://www.bilibili.com/video/BV1xx411c7mD") == "bilibili"
    assert pp.detect_platform("https://www.youtube.com/watch?v=dQw4w9WgXcQ") == "youtube"
    assert pp.detect_platform("https://youtu.be/dQw4w9WgXcQ") == "youtube"


def test_host_lookalike_url_does_not_get_a_bilibili_player():
    """端到端：evil.com/?bilibili.com 不许长出 B 站播放器。"""
    content = _content(_render("# 书\n\n![图](SCREENSHOT:00:00:10)\n",
                               url="https://evil.com/?bilibili.com"))
    assert "player.bilibili.com" not in content, "evil.com 竟然生成了 B 站播放器"
    assert not _dangerous(content), _dangerous(content)


def test_normal_content_still_renders():
    """正常内容回归：表格、代码块、公式、Mermaid、两种卡片都要在。"""
    d = _tmpdir()
    try:
        md = (
            "# 测试书\n\n"
            "> 引言\n\n"
            "| 左 | 中 | 右 |\n|:---|:---:|---:|\n| a | b | c |\n\n"
            "```python\nif x < 1 and y > 2:\n    print(\"hi\")\n```\n\n"
            "```mermaid\ngraph LR\n    A[\"甲\"] --> B[\"乙\"]\n```\n\n"
            "公式：$$E = mc^2$$\n\n"
            "![有截图](SCREENSHOT:00:00:10)\n\n"
            "![没截图](SCREENSHOT:00:00:20)\n"
        )
        html = _render(md, images_dir=_image_dir(d))
        content = _content(html)
        s = _scan(content)
        text = "".join(s.text)

        # 表格（含对齐）
        assert _elements(s, "table") and _elements(s, "th") and _elements(s, "td"), "表格丢了"
        assert any(a.get("style", "").startswith("text-align") for a in _elements(s, "td")), \
            "表格对齐丢了"

        # 代码块：codehilite 外壳在，代码文本转义/还原都对
        pres = _elements(s, "pre")
        assert pres and any("highlight" in a.get("class", "") for a in pres), \
            "代码块外壳丢了: %r" % pres
        assert "if x < 1 and y > 2:" in text, "代码块的文本被改坏了"
        assert "&lt; 1" in content, "代码块里的 < 没有转义"
        assert 'print("hi")' in text, "代码块里的引号被改坏了"

        # Mermaid：页面里那段 JS 靠 `pre code` 的文本前缀认图，文本必须还是 graph 开头
        codes = [c.strip() for c in _code_texts(content)]
        assert 'graph LR\n    A["甲"] --> B["乙"]' in codes, "mermaid 源码被改坏了: %r" % codes

        # 公式：$$ 分隔符要原样留给 KaTeX
        assert "$$E = mc^2$$" in text, "公式的 $$ 分隔符丢了"

        # 截图卡片：img 走受控相对路径 + 回链还在
        cards = [a for a in _elements(s, "div") if "screenshot-card" in a.get("class", "")]
        assert len(cards) == 1, "截图卡片丢了: %r" % [a.get("class") for a in _elements(s, "div")]
        imgs = _elements(s, "img")
        assert len(imgs) == 1 and imgs[0]["src"] == "images/shot_00_00_10.png", imgs
        assert "在 B 站中打开 (00:00:10)" in text, "截图卡片的回链文字丢了"

        # 播放器卡片：没截图就走 iframe
        frames = _elements(s, "iframe")
        assert len(frames) == 1 and "player.bilibili.com" in frames[0]["src"], frames
        assert "在 B 站中打开 (00:00:20)" in text, "播放器卡片的回链文字丢了"

        # 模板侧：CSP、Mermaid 初始化、KaTeX、Lightbox 都得在
        assert "Content-Security-Policy" in html, "没有加 CSP"
        assert "mermaid.initialize" in html, "Mermaid 初始化脚本丢了"
        assert "renderMathInElement" in html, "KaTeX 初始化脚本丢了"
        assert "lightbox" in html and ".screenshot-card img" in html, "Lightbox 丢了"
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_real_book_artifact_still_renders():
    """真实成品回归：output/BV1rXwNzuEmJ/book.md 拷到临时目录再渲染（绝不写回 output/）。"""
    src_md = os.path.join(REAL_BOOK, "book.md")
    src_images = os.path.join(REAL_BOOK, "images")
    if not (os.path.isfile(src_md) and os.path.isdir(src_images)):
        raise unittest.SkipTest("没有真实成品可以回归")
    d = _tmpdir()
    try:
        md = os.path.join(d, "book.md")
        shutil.copyfile(src_md, md)
        shutil.copytree(src_images, os.path.join(d, "images"))
        p = _run_with(REAL_URL, md)
        assert p.returncode == 0, "真实成品渲染失败: %r" % p.stdout
        with open(os.path.join(d, "book.html"), encoding="utf-8") as f:
            html = f.read()
        content = _content(html)
        s = _scan(content)

        cards = [a for a in _elements(s, "div") if "screenshot-card" in a.get("class", "")]
        assert len(cards) == 18, "截图卡片数量变了：%d" % len(cards)
        imgs = _elements(s, "img")
        assert len(imgs) == 18, "截图数量变了：%d" % len(imgs)
        assert all(i["src"].startswith("images/") for i in imgs), \
            "截图不再是受控相对路径: %r" % [i["src"] for i in imgs]
        assert not _elements(s, "iframe"), "截图卡片不该带 iframe（18 张图都在）"
        assert not _elements(s, "script"), "正文里出现了脚本"
        assert not _dangerous(content), "真实成品里有可执行内容: %s" % _dangerous(content)

        text = "".join(s.text)
        assert "在 B 站中打开" in text, "回链文字丢了"
        codes = [c.strip() for c in _code_texts(content)]
        assert len([c for c in codes if c.startswith(("graph ", "sequenceDiagram"))]) == 2, \
            "Mermaid 代码块数量变了: %r" % codes
        assert "$$M = (S," in text, "公式丢了"

        # 模板侧（CSP / Mermaid / KaTeX / Lightbox）一个都不能少
        assert "Content-Security-Policy" in html, "没有加 CSP"
        assert "mermaid.initialize" in html, "Mermaid 初始化脚本丢了"
        assert "renderMathInElement" in html, "KaTeX 初始化脚本丢了"
        assert "lightbox" in html and ".screenshot-card img" in html, "Lightbox 丢了"
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_process_markdown_reports_read_failure():
    """读不了的输入（比如传了目录）要返回失败，而不是抛异常。"""
    d = _tmpdir()
    try:
        got = pp.process_markdown(URL, d)
        assert got is False, "读不了输入时应返回失败状态，实际 %r" % got
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_read_failure_exits_nonzero():
    """读不了的输入必须非零退出（不能崩一半、也不能当成功）。"""
    d = _tmpdir()
    try:
        p = _run_with(URL, d)
        assert p.returncode != 0, "读取失败却退出了 0；stdout=%r" % p.stdout
        assert b"[ERR]" in p.stdout, "没有打印可读的失败原因：%r" % p.stdout
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_process_markdown_reports_verify_failure():
    """写盘后复验不通过 → 返回失败（调用方靠它设退出码）。"""
    d = _tmpdir()
    try:
        md = os.path.join(d, "book.md")
        with open(md, "w", encoding="utf-8") as f:
            f.write("# 测试书\n\n正文。\n")
        orig = pp.verify_rendered_html
        pp.verify_rendered_html = lambda *a, **k: ["模拟复验失败"]
        try:
            got = pp.process_markdown(URL, md)
        finally:
            pp.verify_rendered_html = orig
        assert got is False, "复验失败时必须返回失败状态，实际 %r" % got
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_verify_failure_exits_nonzero():
    """端到端：复验不通过 → 命令退出码非零。"""
    d = _tmpdir()
    try:
        md = os.path.join(d, "book.md")
        with open(md, "w", encoding="utf-8") as f:
            f.write("# 测试书\n\n正文。\n")
        code = (
            "import sys\n"
            "sys.path.insert(0, %r)\n"
            "import post_process as pp\n"
            "pp.verify_rendered_html = lambda *a, **k: ['模拟复验失败']\n"
            "sys.argv = ['post_process.py', %r, %r]\n"
            "pp.main()\n"
        ) % (SRC, URL, md)
        p = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True)
        assert p.returncode != 0, "复验失败却退出了 0；stdout=%r" % p.stdout
        assert b"[ERR]" in p.stdout, "没有打印可读的失败原因：%r" % p.stdout
    finally:
        shutil.rmtree(d, ignore_errors=True)
