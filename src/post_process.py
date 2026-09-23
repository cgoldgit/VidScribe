"""
post_process.py
将 AI 生成的 book.md 转换为精美的 book.html，
并将 SCREENSHOT 占位符替换为嵌入式视频播放器卡片。
"""
import argparse
import html
import os
import re
import sys
import urllib.parse as urlparse
from html.parser import HTMLParser
import markdown

import timecode

# ─────────────────────────────────────────────
# 可信 / 不可信边界：转义、URL 与图片路径校验
# ─────────────────────────────────────────────
# 标题、描述、URL、图片路径全是**不可信的外部数据**（视频标题、字幕、AI 写的
# Markdown），而成品会发布到 GitHub Pages —— 漏一处转义就是存储型 XSS。
# 这里只用标准库（html / re / urllib.parse）做上下文正确的处理。

_SAFE_SCHEMES = ("http", "https")

# 图片只允许页面内的受控相对路径（images/shot_00_00_10.png 这种）：
# 不许有 scheme、反斜杠、上级目录，也不许是绝对路径。
_IMAGE_SRC_RE = re.compile(
    r"^(?:[A-Za-z0-9_][A-Za-z0-9_.\-]*/)*[A-Za-z0-9_][A-Za-z0-9_.\-]*"
    r"\.(?:png|jpe?g|gif|webp|avif|svg)$"
)

# 允许被嵌进页面的播放器域（卡片将来改坏了，这里也能兜住）
_FRAME_HOSTS = ("player.bilibili.com", "www.youtube-nocookie.com")


def esc_text(value) -> str:
    """文本节点用的转义：把 & < > 变成实体（引号在文本里无害）。"""
    return html.escape("" if value is None else str(value), quote=False)


def esc_attr(value) -> str:
    """属性值用的转义：连引号一起转，防止从属性里逃出去。"""
    return html.escape("" if value is None else str(value), quote=True)


def clean_url(value: str):
    """只放行 http/https URL；javascript:、data:、没有 scheme 的一律返回 None。"""
    if not value or not isinstance(value, str):
        return None
    # 去掉控制字符和空白：`java\tscript:` 这类靠空白拆字的写法要被挡住
    value = re.sub(r"[\x00-\x20\x7f]", "", value)
    parsed = urlparse.urlparse(value)
    if parsed.scheme.lower() not in _SAFE_SCHEMES or not parsed.netloc:
        return None
    return value


def clean_href(value: str):
    """a 的 href：http/https，或页内锚点（#xxx）；其余返回 None（等于去掉 href）。"""
    if not value or not isinstance(value, str):
        return None
    if value.startswith("#"):
        return value if re.fullmatch(r"#[A-Za-z0-9_.\-]{1,64}", value) else None
    return clean_url(value)


def safe_image_src(path: str):
    """图片路径：只放行受控相对路径；不合格返回 None（这张图就不渲染）。"""
    if not path or not isinstance(path, str) or path != path.strip():
        return None
    if path.startswith("/") or "\\" in path or ".." in path or ":" in path:
        return None
    return path if _IMAGE_SRC_RE.match(path) else None


def url_host(value: str):
    """取解析后的主机名（小写）；不是合法 http/https 时返回 None。"""
    safe = clean_url(value)
    if not safe:
        return None
    host = urlparse.urlparse(safe).hostname      # hostname 已剥掉 userinfo 和端口
    return host.lower() if host else None


def _host_is(host: str, domain: str) -> bool:
    """host 等于 domain 或它的子域；`bilibili.com.evil.com` 这种不算。"""
    return bool(host) and (host == domain or host.endswith("." + domain))


# ─────────────────────────────────────────────
# 平台检测与 ID 提取
# ─────────────────────────────────────────────

# 中文 Windows 控制台默认是 GBK：print 遇到打不出来的字符会直接抛
# UnicodeEncodeError —— 在"报错"这条路径上再报错最难受。让它退化成 ?，绝不崩。
try:
    import sys as _sys
    _sys.stdout.reconfigure(errors="replace")
    _sys.stderr.reconfigure(errors="replace")
except Exception:
    pass


def detect_platform(video_url: str) -> str:
    """检测视频来源平台。

    必须按**解析后的 host** 判：以前是 `"bilibili.com" in video_url`，
    于是 `https://evil.com/?bilibili.com` 就能骗过它。
    """
    host = url_host(video_url)
    if host is None:
        return "unknown"
    if _host_is(host, "youtube.com") or host == "youtu.be":
        return "youtube"
    if _host_is(host, "bilibili.com"):
        return "bilibili"
    return "unknown"

def extract_youtube_id(url: str) -> str:
    parsed = urlparse.urlparse(url)
    host = url_host(url)
    if host is None:
        return None
    if _host_is(host, "youtube.com"):
        vid = urlparse.parse_qs(parsed.query).get("v", [None])[0]
    elif host == "youtu.be":
        vid = parsed.path[1:]
    else:
        return None
    # 这个 ID 会拼进 iframe 的 URL：只收字母数字和 - _，其余当成解析失败
    return vid if vid and re.fullmatch(r"[A-Za-z0-9_\-]{1,64}", vid) else None

def extract_bilibili_bvid(url: str) -> str:
    match = re.search(r'(BV[a-zA-Z0-9]+)', url)
    return match.group(1) if match else None

def with_query(url: str, **params) -> str:
    """给 URL 追加查询参数（自动判断 ? 还是 &），键值都做 URL 编码。"""
    sep = "&" if "?" in url else "?"
    return url + sep + "&".join(
        f"{urlparse.quote(str(k), safe='')}={urlparse.quote(str(v), safe='')}"
        for k, v in params.items())


def timestamp_to_seconds(ts: str) -> int:
    """将 HH:MM:SS 或 HH:MM:SS.xxx 转为整数秒（解析实现见 timecode.parse_seconds）"""
    try:
        return timecode.parse_seconds(ts)
    except ValueError:
        return 0

# ─────────────────────────────────────────────
# 嵌入式视频卡片生成
# ─────────────────────────────────────────────

def make_video_card(platform: str, video_id: str, video_url: str, timestamp: str, description: str, image_rel: str = None) -> str:
    """根据平台生成嵌入式视频播放器 HTML 卡片。

    video_url 可以是已带分P 参数的地址（多分P 合集里每张截图属于不同的 P），
    追加 t= 秒数时统一走 with_query，避免拼出 `?p=3?t=20` 这种坏链。

    描述、URL、图片路径都是不可信输入，所以：文本走 esc_text、属性值走 esc_attr、
    URL 先过 clean_url、图片先过 safe_image_src（不合格就当没有这张图）。
    """
    seconds = timestamp_to_seconds(timestamp)
    # 不是 http/https 的"URL"（javascript:、data: …）：不给链接，只留纯文本
    open_url = clean_url(video_url)
    if open_url:
        open_url = with_query(open_url, t=seconds)
    caption = esc_text(description)
    ts_text = esc_text(timestamp)

    def open_link(label):
        """回链：URL 不安全时退化成纯文本时长，绝不输出坏 scheme。"""
        if not open_url:
            return ts_text
        return (f'<a href="{esc_attr(open_url)}" target="_blank" rel="noopener">'
                f'{label} ({ts_text})</a>')

    # 若已提供页内截图，则渲染静态截图卡片（真·截图模式）
    src = safe_image_src(image_rel)
    if src:
        return f'''<div class="video-card screenshot-card">
  <img src="{esc_attr(src)}" alt="{esc_attr(description)}" loading="lazy">
  <p class="video-caption">📷 {caption}<br>{open_link("在 B 站中打开")}</p>
</div>'''

    if platform == "youtube":
        # 使用 nocookie 域名 + 清洁参数，尽可能隐藏悬浮 UI
        vid = urlparse.quote(str(video_id), safe="")
        embed_url = f"https://www.youtube-nocookie.com/embed/{vid}?start={seconds}&rel=0&modestbranding=1&iv_load_policy=3&showinfo=0&controls=1&playsinline=1&cc_load_policy=0"
        open_url = f"https://www.youtube.com/watch?v={vid}&t={seconds}s"
        return f'''<div class="video-card youtube-card">
  <div class="video-wrapper">
    <iframe src="{esc_attr(embed_url)}" frameborder="0" allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture" allowfullscreen loading="lazy"></iframe>
  </div>
  <p class="video-caption">▶ {caption}<br><a href="{esc_attr(open_url)}" target="_blank" rel="noopener">在 YouTube 中打开 ({ts_text})</a></p>
</div>'''

    elif platform == "bilibili":
        bvid = urlparse.quote(str(video_id), safe="")
        embed_url = f"https://player.bilibili.com/player.html?bvid={bvid}&t={seconds}&autoplay=0&high_quality=1"
        return f'''<div class="video-card">
  <div class="video-wrapper">
    <iframe src="{esc_attr(embed_url)}" frameborder="0" allowfullscreen scrolling="no" loading="lazy"></iframe>
  </div>
  <p class="video-caption">▶ {caption}<br>{open_link("在 B 站中打开")}</p>
</div>'''

    # 未知平台，返回纯文本提示（URL 不安全时连链接都不给）
    ref = esc_text(video_url)
    linked = (f'<a href="{esc_attr(open_url)}" target="_blank" rel="noopener">{ref}</a>'
              if open_url else ref)
    return f'<div class="video-card fallback"><p>⚠️ 视频参考：{caption}（{ts_text}）—— {linked}</p></div>'


# ─────────────────────────────────────────────
# Markdown → HTML 转换
# ─────────────────────────────────────────────

def replace_screenshots_with_embeds(md_content: str, video_url: str, images_dir: str = None) -> str:
    """将 Markdown 中的 ![desc](SCREENSHOT:HH:MM:SS) 替换为 HTML 视频卡片"""
    platform = detect_platform(video_url)
    video_id = None
    if platform == "youtube":
        video_id = extract_youtube_id(video_url)
    elif platform == "bilibili":
        video_id = extract_bilibili_bvid(video_url)

    # 匹配两种形态：
    #   1) 占位符 ![描述](SCREENSHOT:HH:MM:SS[.xxx])
    #   2) 已物化图片链接 ![描述](images/shot_HH_MM_SS.png) 或 ![描述](shot_HH_MM_SS.png)
    #      多分P 合集合并成书后，图片名还会带分P 前缀：images/p007_shot_HH_MM_SS.png，
    #      用于把「在 B 站中打开」指向正确的分P（?p=7&t=...）。
    pattern = (
        r'!\[([^\]]*)\]\('
        r'(?:SCREENSHOT:(\d{2}:\d{2}:\d{2}(?:\.\d+)?)'
        r'|(?:images/)?(?:p(\d+)_)?shot_(\d{2})_(\d{2})_(\d{2})\.png'
        r')\)'
    )

    def replacer(match):
        desc = match.group(1)
        page = match.group(3)
        if match.group(2):
            ts = match.group(2).split('.')[0]
        else:
            ts = ":".join(match.group(i) for i in (4, 5, 6))
        base = "shot_" + ts.replace(":", "_") + ".png"
        fname = f"p{int(page):03d}_{base}" if page else base
        image_rel = None
        if images_dir and os.path.exists(os.path.join(images_dir, fname)):
            image_rel = "images/" + fname
        # 每张截图指回它自己的那一分P
        link_url = with_query(video_url, p=int(page)) if page else video_url
        card = make_video_card(platform, video_id, link_url, ts, desc, image_rel=image_rel)
        # 卡片是可信 HTML，但正文还要过白名单清洗 —— 先换成 token，
        # 等清洗完了再由 md_to_html 换回来（否则卡片会被当成原始 HTML 清掉）。
        return stash_video_card(card)

    return re.sub(pattern, replacer, md_content)


# Markdown 渲染结果里允许保留的标签 → 允许的属性。
# 只收本项目启用的扩展（tables / fenced_code / codehilite / toc）真的会产出的东西。
_ALLOWED_TAGS = {
    "p": (), "br": (), "hr": (), "blockquote": (),
    "h1": ("id",), "h2": ("id",), "h3": ("id",), "h4": ("id",),
    "h5": ("id",), "h6": ("id",),
    "strong": (), "em": (),
    "ul": (), "ol": (), "li": (),
    "pre": ("class",), "code": ("class",), "span": ("class",), "div": ("class",),
    "table": (), "thead": (), "tbody": (), "tr": (),
    "th": ("style",), "td": ("style",),
    "a": ("href", "title"),
    "img": ("src", "alt", "title"),
}

# 危险标签：连内容一起丢（它们的"内容"是脚本/样式/子文档，不是正文）
_DROP_TAGS = frozenset({
    "script", "style", "iframe", "object", "embed", "applet", "frame", "frameset",
    "template", "noscript", "svg", "math", "form", "input", "button", "select",
    "textarea", "option", "link", "meta", "base", "title", "audio", "video",
    "source", "track", "canvas", "marquee", "plaintext", "xmp", "listing",
})

# 空元素（没有结束标签）—— 丢内容时不能给它们记账，否则深度永远回不到 0
_VOID_TAGS = frozenset({
    "br", "hr", "img", "input", "link", "meta", "base", "source", "track",
    "embed", "area", "col", "wbr", "param",
})

# class 只放行渲染器自己会写的那几种（class 值本身不能执行，但也没必要开口子）
_CLASS_RE = {
    "div": re.compile(r"^(?:highlight|codehilite|toc|mermaid)$"),
    "pre": re.compile(r"^(?:highlight|codehilite|code)$"),
    "code": re.compile(r"^(?:language-[A-Za-z0-9_+\-]{1,32}|highlight|codehilite)$"),
    "span": re.compile(r"^[A-Za-z0-9_ \-]{1,64}$"),
}
_STYLE_RE = re.compile(r"^text-align:\s*(?:left|right|center|justify);?$")
_ID_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")


def _attr_value(tag: str, name: str, value: str):
    """按上下文校验一个属性值；返回 None 表示这个属性不要了。"""
    if name == "href":
        return clean_href(value)
    if name == "src":
        return safe_image_src(value)            # 图片：只允许受控相对路径
    if name == "class":
        pattern = _CLASS_RE.get(tag)
        return value if pattern and pattern.match(value) else None
    if name == "style":
        return value if _STYLE_RE.match(value) else None
    if name == "id":
        return value if _ID_RE.match(value) else None
    if name in ("alt", "title"):
        return value                            # 纯文本属性：esc_attr 负责转义
    return None


class _Sanitizer(HTMLParser):
    """按白名单清洗 Markdown 渲染出的 HTML —— 也就是"原始 HTML 默认禁用"。

    标准库实现（html.parser），不引入 bleach / nh3 之类的依赖。
    - 危险标签（script / iframe / style …）连内容一起丢；
    - 白名单外的标签"脱壳"：只丢标签，内部文本当纯文本留下；
    - 属性逐个放行，值再按上下文校验（URL 的 scheme、图片相对路径、class 取值…）。
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []
        self._open = []          # 我真正吐出去的开启标签（用来配对闭合）
        self._dropping = 0       # >0：正在丢弃某个危险标签的内容

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if self._dropping:
            if tag in _DROP_TAGS and tag not in _VOID_TAGS:
                self._dropping += 1
            return
        if tag in _DROP_TAGS:
            self._dropping = 1
            return
        allowed = _ALLOWED_TAGS.get(tag)
        if allowed is None:
            return                              # 不认识的标签：脱壳（只留文本）
        pairs = []
        for name, value in attrs:
            name = name.lower()
            if name not in allowed:
                continue
            checked = _attr_value(tag, name, value or "")
            if checked is not None:
                pairs.append((name, checked))
        if tag == "img" and not any(n == "src" for n, _v in pairs):
            return                              # 图片必须是受控相对路径，否则不渲染
        self.out.append("<" + tag + "".join(
            f' {n}="{esc_attr(v)}"' for n, v in pairs) + ">")
        if tag not in ("br", "hr", "img"):
            self._open.append(tag)

    def handle_startendtag(self, tag, attrs):
        # <img … /> 这类自闭合：当普通开启标签处理（空元素本来就不需要闭合）
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if self._dropping:
            if tag in _DROP_TAGS:
                self._dropping -= 1
            return
        if tag in self._open:
            while self._open:
                opened = self._open.pop()
                self.out.append(f"</{opened}>")
                if opened == tag:
                    break

    def handle_data(self, data):
        if not self._dropping:
            self.out.append(esc_text(data))

    def handle_comment(self, data):
        pass                                    # 注释不要（Markdown 里的 <!-- --> 不进成品）

    def handle_decl(self, decl):
        pass

    def handle_pi(self, data):
        pass


def sanitize_html(html_text: str) -> str:
    """按白名单清洗渲染结果（Markdown 里的原始 HTML 默认被禁掉）。"""
    sanitizer = _Sanitizer()
    sanitizer.feed(html_text)
    sanitizer.close()
    return "".join(sanitizer.out)


# ─────────────────────────────────────────────
# 视频卡片：先转 token，正文清洗完再注入
# ─────────────────────────────────────────────
# 卡片是本模块生成的可信 HTML，不能跟正文一起过白名单（会被清掉）。
# 所以先换成**不可碰撞**的 token（每次运行一个随机前缀，正文伪造不出来），
# 等正文清洗完成之后再换回受控组件。

_TOKEN_RUN = os.urandom(6).hex()
_CARD_TOKENS = {}


def stash_video_card(card_html: str) -> str:
    """把可信卡片暂存成 token，返回 token 文本。"""
    token = f"@@VSCARD-{_TOKEN_RUN}-{len(_CARD_TOKENS)}@@"
    _CARD_TOKENS[token] = card_html
    return token


def inject_video_cards(body_html: str) -> str:
    """把 token 换回卡片（只认本模块发出去的 token）。"""
    for token, card in _CARD_TOKENS.items():
        if token not in body_html:
            continue
        # 独占一行的 token 会被 Markdown 包成 <p>…</p>：连壳一起换掉，
        # 免得卡片外面多出一个空段落（和改动前的排版一致）。
        body_html = re.sub(r"<p>\s*" + re.escape(token) + r"\s*</p>",
                           lambda _m: card, body_html)
        body_html = body_html.replace(token, card)
    return body_html


def md_to_html(md_content: str, title: str) -> str:
    """将 Markdown 内容转为完整的 HTML 页面"""
    # 先保护数学公式块（$$...$$）：提取出来暂存，避免内容里的 _ * 等被当成 markdown 语法
    math_store = []

    def _stash(m):
        math_store.append(m.group(0))
        return f"\n\n@@MATHBLOCK{len(math_store) - 1}@@\n\n"

    md_protected = re.sub(r'\$\$(.+?)\$\$', _stash, md_content, flags=re.DOTALL)

    # 将 markdown 转为 HTML 片段
    body_html = markdown.markdown(
        md_protected,
        extensions=['tables', 'fenced_code', 'codehilite', 'toc'],
        extension_configs={
            'codehilite': {'css_class': 'highlight', 'guess_lang': False}
        }
    )

    # 清洗：Markdown 里的原始 HTML（script / iframe / onerror / javascript: …）在这里剥掉。
    # 顺序要紧：先清洗 → 再还原公式 → 最后注入卡片（后两者都是自己人，不该被清洗掉）。
    body_html = sanitize_html(body_html)

    # 还原数学公式块（交给页面里的 KaTeX 渲染）。
    # 用属性级转义（连引号一起）当纯文本插回去：既不会变成标签，万一 token 落在
    # 属性里也逃不出去；文本节点里 &quot; 显示出来还是引号，KaTeX 拿到的原文不变。
    for i, expr in enumerate(math_store):
        body_html = body_html.replace(f"@@MATHBLOCK{i}@@", esc_attr(expr))

    # 注入可信视频卡片（token → 受控组件）
    body_html = inject_video_cards(body_html)

    # 标题是不可信输入（可能来自视频标题）：转义后再放进 <title>。
    # 用单次替换而不是连续 replace：否则标题里恰好写了 {content} 会把正文塞进 <head>。
    return re.sub(r"\{(title|content)\}",
                  lambda m: esc_text(title) if m.group(1) == "title" else body_html,
                  HTML_TEMPLATE)


# ─────────────────────────────────────────────
# 输出复验（写盘之后重新读回来解析）
# ─────────────────────────────────────────────

# 复验时额外放行的标签/属性：卡片是本模块生成的受控组件（可信，不走正文白名单）
_CARD_TAGS = {
    "div": ("class",),
    "p": ("class",),
    "img": ("src", "alt", "loading"),
    "a": ("href", "target", "rel"),
    "iframe": ("src", "frameborder", "allow", "allowfullscreen", "scrolling", "loading"),
}


class _TagCollector(HTMLParser):
    """把成品正文里的标签和属性收集出来（复验用）。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.elements = []

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag.lower(), {k.lower(): (v or "") for k, v in attrs}))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)


def verify_rendered_html(html_text: str) -> list:
    """复验写出去的成品，返回问题清单（空 = 通过）。

    查三件事：
    1. 页面结构完整（正文容器和 </html> 都在）；
    2. 正文里没有"能执行 / 能外连"的残留（白名单被绕过的兜底）；
    3. 没有残留的卡片 token（说明卡片没注入成功，或被清洗干掉了）。
    """
    problems = []
    if "</html>" not in html_text or '<article class="book-content">' not in html_text:
        problems.append("页面结构不完整（缺正文容器或 </html>）")
    for token in _CARD_TOKENS:
        if token in html_text:
            problems.append(f"卡片没有注入，页面里残留 token: {token}")
    if '<article class="book-content">' not in html_text:
        return problems
    content = html_text.split('<article class="book-content">', 1)[1].split("</article>", 1)[0]

    collector = _TagCollector()
    collector.feed(content)
    collector.close()
    for tag, attrs in collector.elements:
        # 卡片本身是受控组件（div/img/a/iframe…），危险标签判定要把它们排除掉
        if tag in _DROP_TAGS and tag not in _CARD_TAGS:
            problems.append(f"正文里出现危险标签 <{tag}>")
            continue
        if tag not in _ALLOWED_TAGS and tag not in _CARD_TAGS:
            problems.append(f"正文里出现未授权标签 <{tag}>")
            continue
        allowed = _ALLOWED_TAGS.get(tag, ()) + _CARD_TAGS.get(tag, ())
        for name, value in attrs.items():
            if name not in allowed:
                problems.append(f"<{tag} {name}=...> 是未授权属性")
            elif name.startswith("on"):
                problems.append(f"<{tag} {name}=...> 是事件属性")
            elif name == "href" and clean_href(value) is None:
                problems.append(f"<{tag} href={value!r}> 不是 http/https 链接")
            elif name == "src":
                if tag == "iframe":
                    host = url_host(value)
                    if host is None or not any(_host_is(host, h) for h in _FRAME_HOSTS):
                        problems.append(f"<iframe src={value!r}> 不是允许的播放器地址")
                elif safe_image_src(value) is None:
                    problems.append(f"<{tag} src={value!r}> 不是受控相对路径")
    return problems


# ─────────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────────

def process_markdown(video_url: str, md_file: str):
    """返回 True 表示 HTML 已写出，False 表示没生成（调用方据此设退出码）。"""
    if not os.path.exists(md_file):
        # 不能只打印就算了：调度方（AI 按 instructions.md 跑这条命令）只看退出码，
        # 退出 0 就会被当成"书已生成"。
        print(f"[ERR] 找不到文件: {md_file}")
        return False

    try:
        with open(md_file, 'r', encoding='utf-8') as f:
            content = f.read()
    except (OSError, UnicodeDecodeError) as e:
        print(f"[ERR] 读不了文件: {md_file} ({e})")
        return False

    # 从 MD 第一行提取标题
    title_match = re.search(r'^#\s+(.+)', content, re.MULTILINE)
    title = title_match.group(1).strip() if title_match else "VidScribe"

    # 1) 将 SCREENSHOT 占位符替换为嵌入式视频卡片 HTML
    print(">> 正在将截图占位符替换为嵌入式视频播放器...")
    screenshot_pattern = (
        r'!\[[^\]]*\]\((?:SCREENSHOT:\d{2}:\d{2}:\d{2}(?:\.\d+)?'
        r'|(?:images/)?(?:p\d+_)?shot_\d{2}_\d{2}_\d{2}\.png)\)'
    )
    count = len(re.findall(screenshot_pattern, content))

    # 渲染阶段的异常也要变成"非零退出"，而不是崩一半还留个半成品：
    # 调度方只看退出码，静默失败会被当成"书已生成"。
    try:
        if count > 0:
            content = replace_screenshots_with_embeds(content, video_url, images_dir=os.path.join(os.path.dirname(os.path.abspath(md_file)), "images"))
            print(f">> 已替换 {count} 处截图为视频播放器卡片")
        else:
            print(">> 未发现截图占位符，直接转换")

        # 2) 将 Markdown 转为精美的 HTML
        print(">> 正在生成 HTML 电子书...")
        html_output = md_to_html(content, title)
    except Exception as e:
        print(f"[ERR] 渲染 HTML 失败: {type(e).__name__}: {e}")
        return False

    # 3) 保存 HTML 文件
    html_file = md_file.replace('.md', '.html')
    try:
        with open(html_file, 'w', encoding='utf-8') as f:
            f.write(html_output)
    except OSError as e:
        print(f"[ERR] 写不了文件: {html_file} ({e})")
        return False

    # 4) 复验：把刚写出去的文件重新读回来解析一遍（不是复查内存里那个字符串）
    try:
        with open(html_file, 'r', encoding='utf-8') as f:
            written = f.read()
    except (OSError, UnicodeDecodeError) as e:
        print(f"[ERR] 复验时读不回文件: {html_file} ({e})")
        return False
    problems = verify_rendered_html(written)
    if problems:
        print("[ERR] 输出复验未通过:")
        for problem in problems:
            print(f"  - {problem}")
        return False

    print(f"\n[OK] 电子书已生成: {html_file}")
    print(f"   请在浏览器中打开此文件阅读！")
    return True


def main():
    parser = argparse.ArgumentParser(description="将 book.md 转换为精美的 book.html 电子书")
    parser.add_argument("video_url", help="原始视频的 URL")
    parser.add_argument("md_file", help="待转换的 Markdown 文件路径")
    args = parser.parse_args()

    if not process_markdown(args.video_url, args.md_file):
        sys.exit(1)


# ─────────────────────────────────────────────
# HTML 模板与 CSS 样式
# ─────────────────────────────────────────────

HTML_TEMPLATE = r'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <!-- CSP：成品会发布到 GitHub Pages，来源只放行必需的这几处（外域一律挡掉）。
         页面自带的 KaTeX/Mermaid 回退、图表初始化、Lightbox 目前仍是内联脚本，
         所以 script-src 还得留 'unsafe-inline' —— 把它们迁到静态文件是后续计划，
         不在本次范围内。file: 是为了本地直接双击打开 book.html 时子资源仍可加载。 -->
    <meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net file:; style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com file:; font-src 'self' data: https://cdn.jsdelivr.net https://fonts.gstatic.com file:; img-src 'self' data: file:; frame-src https://player.bilibili.com https://www.youtube-nocookie.com; connect-src 'self' file:; object-src 'none'; base-uri 'none'; form-action 'none'">
    <title>{title}</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Noto+Serif+SC:wght@400;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
    <!-- KaTeX 数学公式渲染（本地文件，离线可用；本地缺失时回退到 CDN，版本与本地一致） -->
    <link rel="stylesheet" href="../katex/katex.min.css">
    <script src="../katex/katex.min.js"></script>
    <script>
        if (typeof katex === 'undefined') {
            document.write('<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.css">');
            document.write('<script src="https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.js"><\/script>');
        }
    </script>
    <script src="../katex/contrib/auto-render.min.js"></script>
    <script>
        if (typeof renderMathInElement === 'undefined') {
            document.write('<script src="https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/contrib/auto-render.min.js"><\/script>');
        }
    </script>
    <script>
        document.addEventListener("DOMContentLoaded", function () {
            if (typeof renderMathInElement !== 'function') return;
            renderMathInElement(document.body, {
                delimiters: [
                    { left: "$$", right: "$$", display: true },
                    { left: "$", right: "$", display: false }
                ],
                throwOnError: false
            });
        });
    </script>
    <style>
        /* ── 基础重置 ── */
        *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

        :root {
            color-scheme: light;
            --bg-primary: #f7f7f8;
            --bg-secondary: #ffffff;
            --bg-card: #eef0f3;
            --bg-code: #f2f3f5;
            --text-primary: #1f2328;
            --text-secondary: #57606a;
            --text-muted: #6e7781;
            --accent: #2563eb;
            --accent-glow: rgba(37, 99, 235, 0.08);
            --border: #d9dce1;
            --border-subtle: #e7e9ec;
            --radius: 12px;
            --shadow: 0 4px 24px rgba(15, 23, 42, 0.08);
        }

        html {
            scroll-behavior: smooth;
        }

        body {
            font-family: 'Inter', 'Noto Serif SC', -apple-system, sans-serif;
            background: var(--bg-primary);
            color: var(--text-primary);
            line-height: 1.8;
            font-size: 16px;
            -webkit-font-smoothing: antialiased;
        }

        /* ── 文章容器 ── */
        .book-content {
            max-width: 780px;
            margin: 0 auto;
            padding: 60px 32px 120px;
        }

        /* ── 标题 ── */
        h1 {
            font-size: 2.2em;
            font-weight: 700;
            margin: 0 0 12px;
            background: linear-gradient(135deg, #2563eb 0%, #7c3aed 50%, #db2777 100%);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
            letter-spacing: -0.02em;
        }

        h2 {
            font-size: 1.5em;
            font-weight: 600;
            color: var(--text-primary);
            margin: 56px 0 20px;
            padding-bottom: 10px;
            border-bottom: 1px solid var(--border);
        }

        h3 {
            font-size: 1.2em;
            font-weight: 600;
            color: var(--accent);
            margin: 36px 0 12px;
        }

        /* ── 段落与文字 ── */
        p {
            margin: 0 0 16px;
            color: var(--text-primary);
        }

        strong {
            color: #0f172a;
            font-weight: 600;
        }

        em {
            color: var(--text-secondary);
            font-style: italic;
        }

        a {
            color: var(--accent);
            text-decoration: none;
            border-bottom: 1px solid transparent;
            transition: border-color 0.2s;
        }
        a:hover {
            border-bottom-color: var(--accent);
        }

        /* ── 引用 ── */
        blockquote {
            margin: 24px 0;
            padding: 16px 20px;
            background: var(--bg-secondary);
            border-left: 3px solid var(--accent);
            border-radius: 0 var(--radius) var(--radius) 0;
            color: var(--text-secondary);
        }
        blockquote p { margin: 0; }

        /* ── 列表 ── */
        ul, ol {
            margin: 12px 0 20px 24px;
            color: var(--text-primary);
        }
        li {
            margin: 6px 0;
        }

        /* ── 分割线 ── */
        hr {
            border: none;
            height: 1px;
            background: linear-gradient(90deg, transparent, var(--border), transparent);
            margin: 48px 0;
        }

        /* ── 表格 ── */
        table {
            width: 100%;
            border-collapse: separate;
            border-spacing: 0;
            margin: 24px 0;
            border-radius: var(--radius);
            overflow: hidden;
            box-shadow: var(--shadow);
        }
        thead {
            background: var(--bg-card);
        }
        th {
            padding: 12px 16px;
            font-weight: 600;
            text-align: left;
            color: var(--accent);
            font-size: 0.9em;
            text-transform: uppercase;
            letter-spacing: 0.05em;
        }
        td {
            padding: 12px 16px;
            border-top: 1px solid var(--border-subtle);
        }
        tbody tr {
            background: var(--bg-secondary);
            transition: background 0.15s;
        }
        tbody tr:hover {
            background: var(--bg-card);
        }

        /* ── 代码 ── */
        code {
            font-family: 'JetBrains Mono', monospace;
            background: var(--bg-code);
            padding: 2px 7px;
            border-radius: 5px;
            font-size: 0.88em;
            color: #6d28d9;
        }

        pre {
            margin: 20px 0;
            padding: 20px 24px;
            background: var(--bg-code);
            border-radius: var(--radius);
            overflow-x: auto;
            border: 1px solid var(--border);
        }
        pre code {
            background: none;
            padding: 0;
            font-size: 0.9em;
            color: var(--text-primary);
        }

        /* ── 视频嵌入卡片 ── */
        .video-card {
            margin: 32px 0;
            border-radius: var(--radius);
            overflow: hidden;
            background: var(--bg-card);
            border: 1px solid var(--border);
            box-shadow: var(--shadow);
            transition: transform 0.2s, box-shadow 0.2s;
        }
        .video-card:hover {
            transform: translateY(-2px);
            box-shadow: 0 8px 32px rgba(15, 23, 42, 0.12);
        }

        .video-wrapper {
            position: relative;
            padding-bottom: 56.25%; /* 16:9 */
            height: 0;
            overflow: hidden;
        }
        .video-wrapper iframe {
            position: absolute;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
        }

        .video-card img {
            width: 100%;
            display: block;
        }

        .video-caption {
            padding: 12px 16px;
            font-size: 0.88em;
            color: var(--text-secondary);
            text-align: center;
            line-height: 1.6;
            margin: 0;
        }
        .video-caption a {
            color: var(--accent);
        }

        .video-card.fallback {
            padding: 20px;
            text-align: center;
        }

        /* ── Lightbox：截图点击放大 ── */
        .screenshot-card img { cursor: zoom-in; transition: opacity 0.15s; }
        .screenshot-card img:hover { opacity: 0.9; }
        .lightbox {
            position: fixed; inset: 0; z-index: 999;
            display: none; align-items: center; justify-content: center;
            background: rgba(5, 5, 8, 0.92); backdrop-filter: blur(4px);
            cursor: zoom-out; padding: 24px;
        }
        .lightbox.open { display: flex; }
        .lightbox img {
            max-width: 96vw; max-height: 90vh;
            border-radius: 8px; box-shadow: 0 12px 60px rgba(0,0,0,0.7);
        }
        .lightbox-caption {
            position: absolute; bottom: 14px; left: 0; right: 0;
            text-align: center; color: var(--text-secondary); font-size: 0.85em;
        }

        /* ── 顶部信息栏 ── */
        .book-content > blockquote:first-of-type {
            background: var(--accent-glow);
            border-left-color: var(--accent);
            border-radius: var(--radius);
            margin-bottom: 32px;
        }

        /* ── 图表居中 ── */
        .mermaid {
            display: flex;
            justify-content: center;
            margin: 32px 0;
            overflow-x: auto;
        }

        /* ── 滚动条 ── */
        ::-webkit-scrollbar { width: 6px; height: 6px; }
        ::-webkit-scrollbar-track { background: transparent; }
        ::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }
        ::-webkit-scrollbar-thumb:hover { background: var(--text-muted); }

        /* ── 响应式 ── */
        @media (max-width: 640px) {
            .book-content { padding: 32px 16px 80px; }
            h1 { font-size: 1.7em; }
            h2 { font-size: 1.3em; }
        }
    </style>
</head>
<body>
    <article class="book-content">
        {content}
    </article>

    <!-- Mermaid 图表引擎初始化 -->
    <!-- Mermaid 流程图渲染（本地文件，离线可用；本地缺失时回退到 CDN） -->
    <script src="../mermaid/mermaid.min.js"></script>
    <script>
        if (typeof mermaid === 'undefined') {
            document.write('<script src="https://cdn.jsdelivr.net/npm/mermaid@10.6.1/dist/mermaid.min.js"><\/script>');
        }
    </script>
    <script>
        document.addEventListener("DOMContentLoaded", function() {
            mermaid.initialize({ startOnLoad: false, theme: 'default' });
            
            // 兼容普通代码块以及丢失 language-mermaid 类名的情况 (CodeHilite 干扰)
            const codeNodes = document.querySelectorAll('pre code, .highlight code');
            codeNodes.forEach(function(node) {
                const text = node.textContent.trim();
                // 匹配常见的 mermaid 图表前缀
                if (text.startsWith('graph ') || text.startsWith('sequenceDiagram') || text.startsWith('gantt') || text.startsWith('pie') || text.startsWith('classDiagram') || text.startsWith('stateDiagram') || text.startsWith('mindmap')) {
                    const tempDiv = document.createElement('div');
                    tempDiv.className = 'mermaid';
                    tempDiv.textContent = text;
                    
                    // 获取需要替换的最外层父节点 (比如 div.highlight 或者 pre)
                    const wrapper = node.closest('.highlight') || node.parentNode;
                    wrapper.replaceWith(tempDiv); 
                }
            });
            
            // 所有节点转换完成，统一进行图表动态绘制
            mermaid.run();
        });
    </script>
    <!-- Lightbox：截图点击放大 -->
    <script>
        document.addEventListener("DOMContentLoaded", function() {
            const lightbox = document.createElement('div');
            lightbox.className = 'lightbox';
            lightbox.innerHTML = '<img alt=""><div class="lightbox-caption"></div>';
            document.body.appendChild(lightbox);
            const lbImg = lightbox.querySelector('img');
            const lbCap = lightbox.querySelector('.lightbox-caption');
            function openLightbox(src, alt) {
                lbImg.src = src; lbImg.alt = alt || '';
                lbCap.textContent = alt || '';
                lightbox.classList.add('open');
                document.body.style.overflow = 'hidden';
            }
            function closeLightbox() {
                lightbox.classList.remove('open');
                lbImg.src = '';
                document.body.style.overflow = '';
            }
            document.querySelectorAll('.screenshot-card img').forEach(function(img) {
                img.addEventListener('click', function() { openLightbox(img.src, img.alt); });
            });
            lightbox.addEventListener('click', closeLightbox);
            document.addEventListener('keydown', function(e) { if (e.key === 'Escape') closeLightbox(); });
        });
    </script>
</body>
</html>
'''

if __name__ == "__main__":
    main()


