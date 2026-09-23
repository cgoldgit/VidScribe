"""将成品电子书发布到独立 orphan 分支 pages（目录名 = 视频标题）。

只发布三件套：book.html / book.md / images/*.png；不发布 transcript、srt、
tagged 稿等中间物。全程使用 git plumbing + 临时 index/工作树，不切换分支、
不触碰 main 工作区与 output/ 目录。脚本只写本地 ref，推送由执行者显式完成。

用法:
    python publish.py <video_id> [<video_id> ...]
    python publish.py --all
"""
import argparse
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone, timedelta
from urllib.parse import quote

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GD = os.path.join(BASE, ".git")
PAGES = "pages"
CORRECTED = "transcript.corrected.txt"
# merge_book.py --allow-partial 的成品首页会带这个标记；改这里必须同步改 merge_book.py。
PARTIAL_MARK = "<!-- partial -->"


# 中文 Windows 控制台默认是 GBK：print 遇到打不出来的字符会直接抛
# UnicodeEncodeError —— 在"报错"这条路径上再报错最难受。让它退化成 ?，绝不崩。
try:
    import sys as _sys
    _sys.stdout.reconfigure(errors="replace")
    _sys.stderr.reconfigure(errors="replace")
except Exception:
    pass


def run(args, env=None, cwd=BASE, check=True):
    e = os.environ.copy()
    if env:
        e.update(env)
    r = subprocess.run(["git"] + args, cwd=cwd, env=e,
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr.strip()}")
    return r


def safe_folder(title, bvid):
    """标题 → 文件系统安全目录名：/ → ／，其余非法字符删除，空白折叠。"""
    name = (title or "").strip()
    name = name.replace("/", "／")
    name = re.sub(r'[\\:*?"<>|]', "", name)
    name = re.sub(r"\s+", " ", name).strip().strip(".")
    return name or bvid


def publishable_png(name):
    """这张图能不能进公开仓库：排除截图过程的中间态 *.partial.png。

    capture_frames 先拍成 shot_HH_MM_SS.partial.png，验合格才 os.replace 成正式图；
    进程被中断/超时时它会残留在 images/ 里。它不该发布，也不该进 git 历史。
    """
    n = name.lower()
    return n.endswith(".png") and not n.endswith(".partial.png")


def src_images(src):
    """源目录里该发布的截图；没有 images/ 目录（纯文字书）时返回空列表。"""
    try:
        names = os.listdir(os.path.join(src, "images"))
    except OSError:
        return []          # 纯文字书本来就没有 images/：这是正常情况，不是错误
    return sorted(f for f in names if publishable_png(f))


def book_is_partial(path):
    """成品是否带"部分产出"标记（merge_book.py --allow-partial 写的）。"""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return PARTIAL_MARK in f.read()
    except OSError:
        return False


def _src_blob_map(src):
    """源目录三件套 -> blob sha（与 add 相同过滤标准）。"""
    rels = ["book.html", "book.md"]
    if os.path.isfile(os.path.join(src, CORRECTED)):
        rels.append(CORRECTED)
    rels += ["images/" + f for f in src_images(src)]
    out = {}
    for rel in rels:
        r = run(["hash-object", "--", os.path.join(src, *rel.split("/"))])
        out[rel] = r.stdout.strip()
    return out


def _pages_blob_map(folder):
    """pages 分支中某目录的 relpath -> blob sha；不存在返回 {}。"""
    if not folder:
        return {}
    r = run(["-c", "core.quotePath=false", "ls-tree", "-r", PAGES, "--", folder], check=False)
    if r.returncode != 0:
        return {}
    m = {}
    for line in r.stdout.splitlines():
        meta, _, path = line.partition("\t")
        m[path[len(folder) + 1:]] = meta.split()[2]
    return m


def pages_tip():
    r = run(["rev-parse", "--verify", "--quiet", f"refs/heads/{PAGES}"], check=False)
    return r.stdout.strip() if r.returncode == 0 else None


def read_title(video_id):
    p = os.path.join(BASE, "output", video_id, "transcript.json")
    try:
        with open(p, encoding="utf-8") as f:
            t = json.load(f).get("title", "")
        return (t or "").strip()
    except (OSError, json.JSONDecodeError):
        return ""

def _seq(meta):
    m = re.search(r"\[(\d+)[-－]", meta.get("title", ""))
    return int(m.group(1)) if m else 10**9



def build_index_html(manifest):
    """落地页：浅色背景 + 卡片设计（参考 idealclover/homepage 的 daisyUI 语言）。"""
    rows = sorted(manifest.items(), key=lambda kv: (_seq(kv[1]), kv[1].get("updated", "")))
    cards = []
    for i, (vid, meta) in enumerate(rows):
        href = quote(meta["folder"]) + "/book.html"
        txt = (f'<a class="btn" href="{quote(meta["folder"])}/{CORRECTED}">字幕对照</a>'
               if meta.get("corrected") else "")
        cards.append(
            f'      <div class="card">\n'
            f'        <a class="stretch" href="{href}" aria-label="开始阅读"></a>\n'
            f'        <div class="badge b{i % 4}">{i + 1:02d}</div>\n'
            f'        <h2>{html.escape(meta["title"] or vid)}</h2>\n'
            f'        <p class="meta">{html.escape(vid)} · 更新于 {html.escape(meta.get("updated", "")[:10])}</p>\n'
            f'        <div class="grow"></div>\n'
            f'        <div class="actions">{txt}<a class="btn read hb{i % 4}" href="{href}">开始阅读</a></div>\n'
            f'      </div>')
    body = "\n".join(cards) or '      <p class="empty">暂无成品</p>'
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>生成式软件工程电子书架</title>
<style>
  :root {{ color-scheme: light; }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    background: #f6f7f9; color: #1f2328;
    font-family: "Inter", "Noto Sans SC", system-ui, -apple-system, "Segoe UI", sans-serif;
    -webkit-font-smoothing: antialiased; line-height: 1.7;
  }}
  .wrap {{ max-width: 1080px; margin: 0 auto; padding: 64px 24px 48px; }}
  header h1 {{ font-size: 1.9em; font-weight: 700; letter-spacing: -0.02em; margin-bottom: 32px; }}
  header p {{ color: #57606a; margin: 6px 0 36px; font-size: .95em; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 22px; }}
  .card {{
    display: flex; flex-direction: column; gap: 10px;
    background: #ffffff; border-radius: 16px; padding: 24px 22px 18px;
    box-shadow: 0 1px 2px rgba(15,23,42,.04), 0 8px 24px rgba(15,23,42,.06);
    text-decoration: none; color: inherit;
    transition: transform .18s ease, box-shadow .18s ease;
  }}
  .card:hover {{
    transform: translateY(-3px);
    box-shadow: 0 2px 4px rgba(15,23,42,.05), 0 14px 34px rgba(15,23,42,.10);
  }}
  .badge {{
    width: 44px; height: 44px; border-radius: 12px;
    display: flex; align-items: center; justify-content: center;
    font-weight: 700; font-size: .95em;
  }}
  .b0 {{ background: linear-gradient(135deg,#dbeafe,#e0e7ff); color: #1d4ed8; }}
  .b1 {{ background: linear-gradient(135deg,#ede9fe,#fce7f3); color: #6d28d9; }}
  .b2 {{ background: linear-gradient(135deg,#dcfce7,#dbeafe); color: #15803d; }}
  .b3 {{ background: linear-gradient(135deg,#fef3c7,#fce7f3); color: #b45309; }}
  .card h2 {{ font-size: 1.06em; font-weight: 600; line-height: 1.5; }}
  .meta {{ color: #6e7781; font-size: .82em; }}
  .grow {{ flex: 1; }}
  .card {{ position: relative; }}
  .stretch {{ position: absolute; inset: 0; z-index: 1; }}
  .actions {{ position: relative; z-index: 2; display: flex; justify-content: flex-end; align-items: center; gap: 14px; }}
  .txt:hover {{ text-decoration: underline; }}
  .btn {{
    display: inline-block; background: #f0f1f3; color: #1f2328; border-radius: 10px;
    padding: 7px 14px; font-size: .82em; font-weight: 500; text-decoration: none;
  }}
  .btn:hover {{ background: #e4e6ea; }}
  .read.hb0:hover {{ background: linear-gradient(135deg,#dbeafe,#e0e7ff); }}
  .read.hb1:hover {{ background: linear-gradient(135deg,#ede9fe,#fce7f3); }}
  .read.hb2:hover {{ background: linear-gradient(135deg,#dcfce7,#dbeafe); }}
  .read.hb3:hover {{ background: linear-gradient(135deg,#fef3c7,#fce7f3); }}
  .empty {{ color: #6e7781; }}
  footer {{ margin-top: 44px; color: #8b949e; font-size: .8em; text-align: center; }}
  .github-corner:hover .octo-arm {{ animation: octocat-wave 560ms ease-in-out; }}
  @keyframes octocat-wave {{ 0%, 100% {{ transform: rotate(0); }} 20%, 60% {{ transform: rotate(-25deg); }} 40%, 80% {{ transform: rotate(10deg); }} }}
  @media (max-width: 500px) {{ .github-corner:hover .octo-arm {{ animation: none; }} .github-corner .octo-arm {{ animation: octocat-wave 560ms ease-in-out; }} }}
</style>
</head>
<body>
  <a href="https://github.com/cgoldgit/VidScribe" target="_blank" rel="noopener" class="github-corner" aria-label="View my GitHub"><svg width="80" height="80" viewBox="0 0 250 250" style="fill:#151513; color:#fff; position: absolute; top: 0; border: 0; right: 0; z-index: 5;" aria-hidden="true"><path d="M0,0 L115,115 L130,115 L142,142 L250,250 L250,0 Z"/><path d="M128.3,109.0 C113.8,99.7 119.0,89.6 119.0,89.6 C122.0,82.7 120.5,78.6 120.5,78.6 C119.2,72.0 123.4,76.3 123.4,76.3 C127.3,80.9 125.5,87.3 125.5,87.3 C122.9,97.6 130.6,101.9 134.4,103.2" fill="currentColor" style="transform-origin: 130px 106px;" class="octo-arm"/><path d="M115.0,115.0 C114.9,115.1 118.7,116.5 119.8,115.4 L133.7,101.6 C136.9,99.2 139.9,98.4 142.2,98.6 C133.8,88.0 127.5,74.4 143.8,58.0 C148.5,53.4 154.0,51.2 159.7,51.0 C160.3,49.4 163.2,43.6 171.4,40.1 C171.4,40.1 176.1,42.5 178.8,56.2 C183.1,58.6 187.2,61.8 190.9,65.4 C194.5,69.0 197.7,73.2 200.1,77.6 C213.8,80.2 216.3,84.9 216.3,84.9 C212.7,93.1 206.9,96.0 205.4,96.6 C205.1,102.4 203.0,107.8 198.3,112.5 C181.9,128.9 168.3,122.5 157.7,114.1 C157.9,116.9 156.7,120.9 152.7,124.9 L141.0,136.5 C139.8,137.7 141.6,141.9 141.8,141.8 Z" fill="currentColor" class="octo-body"/></svg></a>
  <div class="wrap">
    <header>
      <h1>生成式软件工程电子书架</h1>
    </header>
    <div class="grid">
{body}
    </div>
    <footer>由 VidScribe 流水线自动生成 · 内容衍生自 B 站公开课程视频</footer>
  </div>
</body>
</html>
"""

def main():
    ap = argparse.ArgumentParser(description="发布成品电子书到 pages 分支")
    ap.add_argument("video_id", nargs="*", help="要发布的视频 ID（output/ 下的目录名）")
    ap.add_argument("--all", action="store_true", help="发布 output/ 下所有含 book.html 的视频")
    ap.add_argument("--allow-partial", action="store_true",
                    help="允许发布标注了缺失内容的成品（merge_book.py --allow-partial 的产出）")
    args = ap.parse_args()

    if args.all:
        out = os.path.join(BASE, "output")
        ids = [d for d in sorted(os.listdir(out))
               if os.path.isfile(os.path.join(out, d, "book.html"))]
    else:
        ids = args.video_id
    if not ids:
        sys.exit("没有可发布的视频：请提供 video_id 或使用 --all")

    for vid in ids:
        d = os.path.join(BASE, "output", vid)
        if not os.path.isfile(os.path.join(d, "book.html")):
            sys.exit(f"output/{vid}/book.html 不存在，先完成渲染步骤")
        if book_is_partial(os.path.join(d, "book.md")):
            if not args.allow_partial:
                sys.exit(f"output/{vid}/book.md 标注了缺失内容（是 merge_book.py "
                         "--allow-partial 的部分产出），默认不发布；"
                         "确认要发布这一版请加 --allow-partial")
            print(f">> {vid}: [!] 这册书标注了缺失内容，按 --allow-partial 继续发布")

    tip = pages_tip()
    tmp = tempfile.mkdtemp(prefix="videobook_publish_")
    env = {"GIT_INDEX_FILE": tmp + ".index"}  # 索引文件必须在工作树之外，否则会被 add 进提交
    g = lambda *a, **k: run(list(a), env=env, **k)

    try:
        # 1) 临时 index 装载旧 pages 树（若有），并物化到临时工作树
        if tip:
            g("--git-dir", GD, "--work-tree", tmp, "read-tree", PAGES)
            co = g("--git-dir", GD, "--work-tree", tmp, "checkout-index", "-a", check=False)
            if co.returncode != 0:
                # 物化失败 = 临时工作树里缺文件。再往下走的话，add -A 会把这些
                # "没物化出来"的文件记成已删除，update-ref 一执行，它们就从公开的
                # pages 分支上消失了，而脚本照样打印"已更新"。宁可不发布。
                sys.exit("[ERR] pages 分支内容物化到临时工作树失败，已中止（pages 分支未改动）：\n"
                         f"{(co.stderr or '').strip()}\n"
                         "常见原因：Windows 长路径、文件被占用或杀软锁定、磁盘/临时空间不足。\n"
                         "处理：确认临时目录没有被占用后重跑本命令。")
        else:
            g("--git-dir", GD, "--work-tree", tmp, "read-tree", "--empty")
        # 清理历史误提交进树的索引残留文件
        for stray in ("index", "index.lock"):
            p = os.path.join(tmp, stray)
            if os.path.isfile(p):
                os.remove(p)

        # 2) 读旧 manifest
        manifest = {}
        if tip:
            r = run(["show", f"{PAGES}:manifest.json"], check=False)
            if r.returncode == 0:
                try:
                    manifest = json.loads(r.stdout)
                except json.JSONDecodeError:
                    manifest = {}

        now = datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")

        # 3) 规划目录名、处理改名与冲突
        for vid in ids:
            title = read_title(vid)
            folder = safe_folder(title, vid)
            old = manifest.get(vid, {}).get("folder")
            if old and old != folder and not title:
                # 读不到标题（如 output/<id>/transcript.json 被清理过）时 title 为空，
                # safe_folder 会回落到 video_id —— 这个值和 manifest 里的旧目录名不等，
                # 会被当成"标题改了"：删掉旧目录、改名，已公开的链接就全废了。
                # 说不清标题就不改名：保留已发布的目录名，只更新内容。
                print(f">> {vid}: [!] 读不到 output/{vid}/transcript.json 的标题；"
                      f"保持已发布目录名 {old}，不自动改名（避免已公开的链接失效）")
                folder = old
                title = manifest[vid].get("title", "")
            others = {k: v.get("folder") for k, v in manifest.items() if k != vid}
            if folder in others.values():
                folder = f"{folder} ({vid})"
            src = os.path.join(BASE, "output", vid)
            dst = os.path.join(tmp, folder)

            # 4) 内容未变且目录未改名：保持原发布（含原 updated），保证幂等
            if old == folder and _pages_blob_map(old) == _src_blob_map(src):
                manifest[vid] = {"folder": folder, "title": title,
                                 "updated": manifest[vid].get("updated", now),
                                 "corrected": os.path.isfile(os.path.join(src, CORRECTED))}
                print(f">> {vid}: 内容无变化，保持原发布")
                continue

            if old and old != folder:
                shutil.rmtree(os.path.join(tmp, old), ignore_errors=True)
                print(f">> {vid}: 目录改名 {old} -> {folder}")
            manifest[vid] = {"folder": folder, "title": title, "updated": now,
                             "corrected": os.path.isfile(os.path.join(src, CORRECTED))}

            # 5) 复制三件套到临时工作树
            shutil.rmtree(dst, ignore_errors=True)
            os.makedirs(os.path.join(dst, "images"))
            shutil.copyfile(os.path.join(src, "book.html"), os.path.join(dst, "book.html"))
            shutil.copyfile(os.path.join(src, "book.md"), os.path.join(dst, "book.md"))
            if os.path.isfile(os.path.join(src, CORRECTED)):
                shutil.copyfile(os.path.join(src, CORRECTED), os.path.join(dst, CORRECTED))
            n = 0
            for fn in src_images(src):
                shutil.copyfile(os.path.join(src, "images", fn),
                                os.path.join(dst, "images", fn))
                n += 1
            print(f">> {vid}: {n} 张截图 -> {folder}/")

        # 5) 写 manifest 与落地页
        with open(os.path.join(tmp, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)
        with open(os.path.join(tmp, "index.html"), "w", encoding="utf-8") as f:
            f.write(build_index_html(manifest))

        # 6) 收拢为新树；树未变则跳过提交
        g("-C", tmp, "--git-dir", GD, "--work-tree", tmp, "add", "-A")
        tree = g("--git-dir", GD, "--work-tree", tmp, "write-tree").stdout.strip()
        if tip:
            old_tree = run(["rev-parse", f"{PAGES}^{{tree}}"]).stdout.strip()
            if tree == old_tree:
                print(">> 内容无变化，跳过提交")
                return
        parent = ["-p", tip] if tip else []
        commit = run(["commit-tree", tree] + parent +
                     ["-m", f"publish: {', '.join(ids)}"]).stdout.strip()
        run(["update-ref", f"refs/heads/{PAGES}", commit])
        print(f">> pages 分支已更新: {commit[:8]}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            os.remove(tmp + ".index")
        except OSError:
            pass

    print(f"\n完成。推送请执行: git push origin {PAGES}")


if __name__ == "__main__":
    main()







