"""Fallback frame extraction: download the video source, ffmpeg extract.

Prefer capture_frames.py, which screenshots the platform player directly in
the logged-in browser (full quality, no media downloads). Use this script
only when browser capture is not possible (no Chrome, remote/headless
environment).

Usage: python extract_frames.py <video_id> [video_url]

Reads current output/<video_id>/book.md (book.tagged.md only if it is missing)
for SCREENSHOT:HH:MM:SS placeholders or existing shot image links and renders each timestamp
from output/<video_id>/video_source.mp4 into
output/<video_id>/images/shot_HH_MM_SS.png (the naming post_process.py expects).

If video_source.mp4 is missing and a video_url is given, the source is
downloaded automatically with yt-dlp (best avc1 tier available without
login; pre-download a higher-quality file under the same name to skip).

After extraction the markdown is materialized: every placeholder in book.md
is replaced with a native image link ![desc](images/shot_HH_MM_SS.png), and
the current draft before replacement is saved as book.tagged.md.

The downloaded video source is a fallback-only media file. It stays on disk
after extraction so failed frames can be retried; at the end of the whole
workflow the agent asks the user whether to delete such large media files.
"""
import argparse
import os
from pathlib import Path
import re
import subprocess
import sys

import imageio_ffmpeg
from dotenv import load_dotenv
import timecode

PLACEHOLDER = re.compile(r'!\[([^\]]*)\]\(SCREENSHOT:(\d{2}:\d{2}:\d{2})(?:\.\d+)?\)')
IMAGE_TIMESTAMP = re.compile(r'!\[[^\]]*\]\((?:\./)?images/shot_(\d{2})_(\d{2})_(\d{2})\.png\)')


def draft_paths(vdir, book_name='book.md'):
    """Select one contained draft and its own tagged copy, including experiments."""
    root = Path(vdir).resolve()
    if not isinstance(book_name, str) or not re.fullmatch(r'[A-Za-z0-9_.-]+\.md', book_name):
        raise ValueError('book-name must be a Markdown filename inside the video directory')
    book = (root / book_name).resolve()
    tagged = (root / (Path(book_name).stem + '.tagged.md')).resolve()
    if not book.is_relative_to(root) or not tagged.is_relative_to(root):
        raise ValueError('Draft or tagged file escapes the video directory')
    return str(book), str(tagged)


def current_source(book: str, tagged: str) -> str:
    """当前正文是权威稿件；旧 tagged 仅在正文不存在时兜底。"""
    return book if os.path.isfile(book) else tagged


def requested_timestamps(content: str):
    timestamps = {m.group(2) for m in PLACEHOLDER.finditer(content)}
    timestamps.update(":".join(m.groups()) for m in IMAGE_TIMESTAMP.finditer(content))
    return sorted(ts for ts in timestamps if _valid_timestamp(ts))


def _valid_timestamp(ts):
    try:
        timecode.parse_seconds(ts)
        return True
    except ValueError:
        return False


def _atomic_text(path: str, content: str):
    tmp = path + ".partial"
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)

# 未登录可用的最高 avc1 档；登录态高清晰度可预先自行下载同名文件
DEFAULT_FORMAT = "bv*[vcodec^=avc1]/b[vcodec^=avc1]/b"


# 中文 Windows 控制台默认是 GBK：print 遇到打不出来的字符会直接抛
# UnicodeEncodeError —— 在"报错"这条路径上再报错最难受。让它退化成 ?，绝不崩。
try:
    import sys as _sys
    _sys.stdout.reconfigure(errors="replace")
    _sys.stderr.reconfigure(errors="replace")
except Exception:
    pass

# 加载可选的本机 .env；yt-dlp 子进程会继承 HTTP_PROXY 等标准变量。
load_dotenv()


def ensure_source(src: str, video_url: str) -> None:
    """Download the video source with yt-dlp when missing."""
    if os.path.exists(src):
        return
    if not video_url:
        sys.exit(f"missing video source: {src} (pass the video url as 2nd arg to auto-download)")
    cmd = [sys.executable, "-m", "yt_dlp", "-f", DEFAULT_FORMAT, "--no-part", "-o", src, video_url]
    print("downloading video source ...")
    subprocess.run(cmd, check=True)


def materialize(book: str, tagged: str, imgdir: str) -> None:
    """Replace SCREENSHOT placeholders in book.md with image links."""
    with open(book, encoding="utf-8") as f:
        content = f.read()

    replaced = 0
    def repl(m):
        nonlocal replaced
        desc, ts = m.group(1), m.group(2)
        fname = "shot_" + ts.replace(":", "_") + ".png"
        if os.path.isfile(os.path.join(imgdir, fname)) and os.path.getsize(os.path.join(imgdir, fname)):
            replaced += 1
            return f"![{desc}](images/{fname})"
        return m.group(0)

    new_content = PLACEHOLDER.sub(repl, content)
    if replaced == 0:
        print("materialize: no image links replaced (missing images remain as placeholders)")
        return
    _atomic_text(tagged, content)
    _atomic_text(book, new_content)
    print(f"materialize: replaced {replaced} placeholders with image links")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('video_id', nargs='?', default='BV1pb8o6yE8f')
    parser.add_argument('video_url', nargs='?', default=None)
    parser.add_argument('--out', type=Path)
    parser.add_argument('--book-name', default='book.md')
    args = parser.parse_args()
    video_id, video_url = args.video_id, args.video_url
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    vdir = str(args.out.resolve()) if args.out else os.path.join(base, "output", video_id)
    try:
        book, tagged = draft_paths(vdir, args.book_name)
    except ValueError as exc:
        parser.error(str(exc))
    src = os.path.join(vdir, "video_source.mp4")
    imgdir = os.path.join(vdir, "images")
    os.makedirs(imgdir, exist_ok=True)

    source_md = current_source(book, tagged)
    with open(source_md, encoding="utf-8") as f:
        content = f.read()
    timestamps = requested_timestamps(content)
    if not timestamps:
        sys.exit("no SCREENSHOT placeholders found in " + source_md)

    ensure_source(src, video_url)

    ff = imageio_ffmpeg.get_ffmpeg_exe()
    for ts in timestamps:
        h, m, s = (int(x) for x in ts.split(":"))
        sec = h * 3600 + m * 60 + s
        out = os.path.join(imgdir, "shot_" + ts.replace(":", "_") + ".png")
        tmp = out[:-4] + ".partial.png"
        cmd = [
            ff, "-hide_banner", "-loglevel", "error",
            "-ss", str(sec), "-i", src,
            "-frames:v", "1", "-y", tmp,
        ]
        try:
            subprocess.run(cmd, check=True)
            # ffmpeg 对超出片长的 -ss 可能退出 0，却不产出任何图片。
            # 复用浏览器路线的质量门禁，失败时保住旧图且不物化。
            from capture_frames import _frame_usable
            ok, reason = _frame_usable(tmp)
            if not ok:
                sys.exit(f"frame extraction failed at {ts}: {reason}")
            os.replace(tmp, out)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
        print("saved", os.path.basename(out))
    print(f"DONE: {len(timestamps)} frames")

    materialize(book, tagged, imgdir)


if __name__ == "__main__":
    main()
