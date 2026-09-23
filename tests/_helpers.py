"""测试小工具：合成媒体文件、本地 HTTP 服务器、写 PNG。

刻意不引入 Pillow / pytest 之外的依赖：写图走 ffmpeg（imageio_ffmpeg 已经
是项目依赖），合成视频用 lavfi。整套测试联网 = 0，只用到本机 Chrome。
"""
import contextlib
import functools
import http.server
import os
import re
import socket
import subprocess
import sys
import threading

import numpy as np

import imageio_ffmpeg

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(REPO, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)


def ffmpeg() -> str:
    return imageio_ffmpeg.get_ffmpeg_exe()


def write_png(path: str, arr: np.ndarray) -> None:
    """把 (H, W, 3) uint8 数组写成 PNG。"""
    h, w = arr.shape[:2]
    subprocess.run(
        [ffmpeg(), "-hide_banner", "-loglevel", "error", "-f", "rawvideo",
         "-pix_fmt", "rgb24", "-s", f"{w}x{h}", "-i", "pipe:0",
         "-frames:v", "1", "-y", path],
        input=np.ascontiguousarray(arr, dtype=np.uint8).tobytes(), check=True)


def still_image(w: int = 640, h: int = 360, seed: int = 7) -> np.ndarray:
    """一张有纹理的静态图（不是整幅单色，免得被判成空帧）。"""
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:h, 0:w]
    img = np.zeros((h, w, 3), dtype=np.float32)
    img[..., 0] = (xx / w) * 200 + 20
    img[..., 1] = (yy / h) * 180 + 25
    img[..., 2] = 90
    # 几块"文字条"，让边缘密度足够高
    for i in range(6):
        y0 = 30 + i * 45
        img[y0:y0 + 14, 40:40 + 380 - i * 20] = 235
    img += rng.normal(0, 3, img.shape)
    return np.clip(img, 0, 255).astype(np.uint8)


def make_video(path: str, seconds: int = 60, size=(640, 360), kind: str = "testsrc",
               workdir: str = None) -> str:
    """合成一段测试视频。kind: testsrc（一直变）/ still（整段静止）/ black（全黑）。"""
    w, h = size
    workdir = workdir or os.path.dirname(path)
    if kind == "still":
        png = os.path.join(workdir, "_still.png")
        if not os.path.isfile(png):
            write_png(png, still_image(w, h))
        cmd = [ffmpeg(), "-hide_banner", "-loglevel", "error", "-loop", "1",
               "-i", png, "-t", str(seconds), "-r", "10",
               "-c:v", "libx264", "-pix_fmt", "yuv420p",
               "-movflags", "+faststart", "-y", path]
    else:
        src = ("testsrc=size=%dx%d:rate=10:duration=%d" % (w, h, seconds)
               if kind == "testsrc" else
               "color=c=black:size=%dx%d:rate=10:duration=%d" % (w, h, seconds))
        cmd = [ffmpeg(), "-hide_banner", "-loglevel", "error", "-f", "lavfi",
               "-i", src, "-c:v", "libx264", "-pix_fmt", "yuv420p",
               "-movflags", "+faststart", "-y", path]
    subprocess.run(cmd, check=True)
    return path


def write_page(path: str, video: str, extra_js: str = "") -> str:
    """一个只有 <video> 的极简页面（模拟播放器）。"""
    html = ("<html><body style='margin:0;background:#000'>"
            f"<video id=v src='{video}' muted playsinline style='width:640px'>"
            "</video>"
            f"<script>{extra_js}</script></body></html>")
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    return path


class _RangeHandler(http.server.SimpleHTTPRequestHandler):
    """支持 Range 的静态服务器 —— Chrome 没有 Range 就没法 seek。"""

    def send_head(self):
        path = self.translate_path(self.path)
        rng = self.headers.get("Range")
        if os.path.isdir(path) or not rng:
            return super().send_head()
        m = re.match(r"bytes=(\d*)-(\d*)", rng)
        if not m:
            return super().send_head()
        size = os.path.getsize(path)
        start = int(m.group(1)) if m.group(1) else 0
        end = min(int(m.group(2)) if m.group(2) else size - 1, size - 1)
        fh = open(path, "rb")
        fh.seek(start)
        self.send_response(206)
        self.send_header("Content-Type", self.guess_type(path))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        self._range_left = end - start + 1
        self._range_fh = fh
        return None

    def do_GET(self):
        res = self.send_head()
        if res is None and getattr(self, "_range_fh", None) is not None:
            fh, left = self._range_fh, self._range_left
            try:
                while left > 0:
                    chunk = fh.read(min(64 * 1024, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
            finally:
                fh.close()
                self._range_fh = None
        elif res is not None:
            self.copyfile(res, self.wfile)
            res.close()

    def log_message(self, *a):
        pass


class _QuietServer(http.server.ThreadingHTTPServer):
    """故意 404 的请求（测"媒体加载失败"用）不要往测试输出里刷 traceback。"""

    def handle_error(self, request, client_address):
        pass


@contextlib.contextmanager
def file_server(directory: str):
    """起一个支持 Range 的本地服务器，返回 base_url。"""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = _QuietServer(
        ("127.0.0.1", port), functools.partial(_RangeHandler, directory=directory))
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        srv.shutdown()
        srv.server_close()
