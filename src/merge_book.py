"""把若干「分P 小节」合并成一本完整的书。

本项目处理多分P 合集（如 100 集的 C 语言课程）时，按小节逐集生成
output/<BV号>_p<N>/book.md，再按 books.json 的分组合并成一本：
output/<书名>/book.md + images/。

合并时做三件事：
  1. 标题降一级（`#` → `##`），让每一集成为书里的一节（代码块内的 # 不动）；
  2. 图片带上分P 前缀复制到书的 images/，并改写正文里的图片链接；
  3. 若存在 output/<书名>/_intro.md（AI 写的全书导语），置于正文之前。

用法：
    python src/merge_book.py "C语言程序设计 · 01 初识C语言"
    python src/merge_book.py --all
    python src/merge_book.py --all --check     # 只体检，不写文件
    python src/merge_book.py --all --allow-partial   # 缺集/缺图时也先出一版
"""
import argparse
import json
import os
import re
import shutil
import sys

from video_id import make_bilibili_episode_id

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")   # 报错也要能看清

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOOKS_JSON = os.path.join(BASE_DIR, "books.json")

IMG_LINK = re.compile(r'!\[([^\]]*)\]\(images/([^)]+)\)')
FENCE = re.compile(r'^\s*(```|~~~)')

# 部分产出（--allow-partial）的标记：publish.py 认这个字符串，默认拒绝发布带它的成品。
# 改动这里必须同步改 publish.py 的 PARTIAL_MARK。
PARTIAL_MARK = "<!-- partial -->"
PARTIAL_HEAD = ("> ⚠️ 本册为部分产出：以下内容缺失或未就绪"
                "（合并时用了 --allow-partial）。修好后重跑合并即可去掉本提示。")
# Windows 上不允许出现在路径里的字符
WIN_BAD_CHARS = '\\/:*?"<>|'


def video_id(bvid: str, p: int) -> str:
    return make_bilibili_episode_id(bvid, p)


def demote_headings(text: str) -> str:
    """ATX 标题整体降一级；围栏代码块内的 # 原样保留。"""
    out, in_fence, fence_mark = [], False, ""
    for line in text.splitlines():
        m = FENCE.match(line)
        if m:
            mark = m.group(1)
            if not in_fence:
                in_fence, fence_mark = True, mark
            elif mark == fence_mark:
                in_fence = False
            out.append(line)
            continue
        if not in_fence and re.match(r'^#{1,5}\s', line):
            line = "#" + line
        out.append(line)
    return "\n".join(out)


def load_books():
    with open(BOOKS_JSON, encoding="utf-8") as f:
        return json.load(f)


def _dir_name_problem(name: str) -> str:
    """成品目录名合不合法；合法返回空串。"""
    if name in (".", ".."):
        return "不能是 . 或 .."
    if any(c in WIN_BAD_CHARS or ord(c) < 32 for c in name):
        return "含路径分隔符或 Windows 保留字符"
    if name != name.rstrip(". "):
        return "不能以点或空格结尾（Windows 会悄悄改名）"
    return ""


def validate_books(cfg) -> list:
    """校验 books.json 的结构，返回问题清单（空 = 合法）。

    配置坏了要当场说清楚、非零退出，不能让脚本带着坏配置去动成品目录
    （目录名里带路径分隔符的话，会把书合并到 output/ 外面去）。
    """
    problems = []
    if not isinstance(cfg, dict):
        return ["books.json 顶层必须是一个对象"]
    for key in ("bvid", "source", "source_url"):
        v = cfg.get(key)
        if not isinstance(v, str) or not v.strip():
            problems.append(f"{key} 必须是非空字符串")
    books = cfg.get("books")
    if not isinstance(books, list) or not books:
        problems.append("books 必须是非空数组")
        return problems

    seen_dirs = {}
    for i, b in enumerate(books):
        where = f"books[{i}]"
        if not isinstance(b, dict):
            problems.append(f"{where} 必须是一个对象")
            continue
        name = b.get("dir")
        if not isinstance(name, str) or not name.strip():
            problems.append(f"{where}.dir 必须是非空字符串")
        else:
            bad = _dir_name_problem(name)
            if bad:
                problems.append(f"{where}.dir {bad}: {name!r}")
            elif name in seen_dirs:
                problems.append(f"{where}.dir 与 books[{seen_dirs[name]}] 重复: {name!r}")
            else:
                seen_dirs[name] = i
        eps = b.get("episodes")
        if not isinstance(eps, list) or not eps:
            problems.append(f"{where}.episodes 必须是非空数组")
            continue
        seen, dup = set(), set()
        for e in eps:
            if isinstance(e, bool) or not isinstance(e, int) or e < 1:
                problems.append(f"{where}.episodes 里有非法集数 {e!r}（应为正整数）")
            elif e in seen:
                dup.add(e)
            else:
                seen.add(e)
        if dup:
            problems.append(f"{where}.episodes 有重复集数 {sorted(dup)}")
    return problems


def collect_sections(cfg, book):
    """返回 (sections, problems)。section = dict(p, vid, md, images)。"""
    sections, problems = [], []
    for p in book["episodes"]:
        vid = video_id(cfg["bvid"], p)
        vdir = os.path.join(BASE_DIR, "output", vid)
        md_path = os.path.join(vdir, "book.md")
        if not os.path.exists(md_path):
            problems.append(f"p{p}: 缺少 book.md")
            continue
        with open(md_path, encoding="utf-8") as f:
            md = f.read()
        left = re.findall(r'SCREENSHOT:(\d{2}:\d{2}:\d{2})', md)
        if left:
            problems.append(f"p{p}: 还有 {len(left)} 个未物化的截图占位符")
        refs = IMG_LINK.findall(md)
        missing = [fn for _, fn in refs
                   if not os.path.exists(os.path.join(vdir, "images", fn))]
        if missing:
            problems.append(f"p{p}: 缺 {len(missing)} 张图 {missing[:3]}")
        sections.append({"p": p, "vid": vid, "md": md, "vdir": vdir})
    return sections, problems


def merge_one(cfg, book, check_only=False, allow_partial=False) -> bool:
    name = book["dir"]
    out_dir = os.path.join(BASE_DIR, "output", name)
    sections, problems = collect_sections(cfg, book)

    print(f"\n=== {name}（{len(book['episodes'])} 节）===")
    if problems:
        for p in problems:
            print("  [!] " + p)

    if check_only:
        print(f"  {'√ 就绪' if not problems else '× 未就绪'}")
        return not problems

    if problems and not allow_partial:
        print("  × 未就绪，本次不写文件（先修好上面 [!] 的问题再合并）")
        return False

    if not sections:
        print("  × 没有任何可合并的内容")
        return False

    # 在同级 staging 目录里从零重建 book.md 与 images/（不再复用旧目录）：
    # 只有全部成功才整体替换目标目录，失败/中断都不会留下半成品，
    # 上一轮留下、正文已不再引用的图也就自然消失了。
    staging = out_dir + ".staging"
    shutil.rmtree(staging, ignore_errors=True)
    imgdir = os.path.join(staging, "images")
    os.makedirs(imgdir)

    parts = []
    for s in sections:
        p, vid = s["p"], s["vid"]
        # 图片：加上分P 前缀后复制，并改写正文链接
        def repl(m, p=p, vid=vid):
            desc, fn = m.group(1), m.group(2)
            src = os.path.join(s["vdir"], "images", fn)
            if not os.path.exists(src):
                return m.group(0)
            new_fn = f"p{p:03d}_{fn}"
            dst = os.path.join(imgdir, new_fn)
            shutil.copyfile(src, dst)      # 以源图为准，重截的帧必须覆盖旧图
            return f"![{desc}](images/{new_fn})"

        md = IMG_LINK.sub(repl, s["md"])
        md = demote_headings(md).strip()
        parts.append(md)

    intro = ""
    intro_path = os.path.join(out_dir, "_intro.md")
    if os.path.exists(intro_path):
        with open(intro_path, encoding="utf-8") as f:
            intro = f.read().strip()
        # 导语引用的图不来自任何一集，只可能存在成品目录里：一并带进 staging，
        # 免得重建时把还在引用的图删掉（重建只认"引用得到"的图）。
        for fn in {fn for _, fn in IMG_LINK.findall(intro)}:
            old_img = os.path.join(out_dir, "images", fn)
            if os.path.isfile(old_img) and not os.path.exists(os.path.join(imgdir, fn)):
                shutil.copyfile(old_img, os.path.join(imgdir, fn))
    else:
        intro = (f"# {name}\n\n> 本册由《{cfg['source']}》视频课程整理而成，"
                 f"覆盖第 {book['episodes'][0]} ~ {book['episodes'][-1]} 集。\n"
                 f"> 课程主页：{cfg['source_url']}")

    if problems:
        # 首页必须写明缺了什么，否则读者（和你自己）事后无从判断这册书是不是全的。
        note = "\n".join([PARTIAL_HEAD] + [f"> - {p}" for p in problems] + [PARTIAL_MARK])
        body = note + "\n\n" + intro + "\n\n" + "\n\n".join(parts) + "\n"
    else:
        body = intro + "\n\n" + "\n\n".join(parts) + "\n"

    # 目标目录里本次不产出的文件（_intro.md、book.html 等）原样带过去
    if os.path.isdir(out_dir):
        for entry in os.listdir(out_dir):
            if entry in ("book.md", "images"):
                continue
            src_e = os.path.join(out_dir, entry)
            dst_e = os.path.join(staging, entry)
            if os.path.isdir(src_e):
                shutil.copytree(src_e, dst_e, dirs_exist_ok=True)
            else:
                shutil.copyfile(src_e, dst_e)

    with open(os.path.join(staging, "book.md"), "w", encoding="utf-8", newline="\n") as f:
        f.write(body)

    # 内容都造齐了才换目录：旧目录先挪走，新的顶上，最后删旧的；
    # 万一换上失败就把旧的放回来，不许把上一版成品弄丢。
    bak = out_dir + ".old"
    shutil.rmtree(bak, ignore_errors=True)
    moved = os.path.exists(out_dir)
    if moved:
        os.replace(out_dir, bak)
    try:
        os.replace(staging, out_dir)
    except OSError:
        if moved:
            os.replace(bak, out_dir)
        raise
    if moved:
        shutil.rmtree(bak, ignore_errors=True)

    # 图片数按正文实际引用统计：目录里的历史遗留文件不算数
    n_img = len({fn for _, fn in IMG_LINK.findall(body)})
    print(f"  √ 已合并 → {os.path.join(out_dir, 'book.md')}")
    print(f"     正文 {len(body)} 字，图片 {n_img} 张（按正文引用统计）")
    return True


def main():
    ap = argparse.ArgumentParser(description="把分P 小节合并成一本电子书")
    ap.add_argument("dir", nargs="?", help="books.json 里的 dir 字段")
    ap.add_argument("--all", action="store_true", help="合并全部书")
    ap.add_argument("--check", action="store_true", help="只体检不写文件")
    ap.add_argument("--allow-partial", action="store_true",
                    help="缺集/缺图/残留占位符时也先产出一版（成品首页会标注缺失内容，"
                         "publish.py 默认拒绝发布这种成品）")
    args = ap.parse_args()

    try:
        cfg = load_books()
    except FileNotFoundError:
        # 不在仓库里（见 .gitignore）：属于"还没生成"，不是配置坏了
        sys.exit("books.json 不在仓库里：它是做多P 合集时才生成的工作文件，"
                 "请按 instructions.md「多P 合集合并成书」一节生成后再运行。")
    except (OSError, json.JSONDecodeError) as e:
        sys.exit(f"books.json 读取失败：{e}")
    problems = validate_books(cfg)
    if problems:
        for p in problems:
            print("  [!] " + p, file=sys.stderr)
        sys.exit(f"books.json 结构非法（{len(problems)} 处），本次不合并任何书")

    books = cfg["books"]
    if args.dir:
        books = [b for b in books if b["dir"] == args.dir]
        if not books:
            sys.exit(f"books.json 里没有这本书: {args.dir}")

    ok = True
    for b in books:
        ok = merge_one(cfg, b, args.check, args.allow_partial) and ok
    print("\n" + ("√ 全部就绪" if ok else "× 有未就绪的书，见上方 [!]"))
    if ok and args.allow_partial:
        print("  （本次带了 --allow-partial：上方有 [!] 的是部分产出，"
              "首页已标注缺失内容，publish.py 默认拒绝发布）")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
