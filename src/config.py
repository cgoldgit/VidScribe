import os
from dotenv import load_dotenv

# 加载可选的本机 .env。HTTP_PROXY、HTTPS_PROXY 等标准变量会由网络库和
# 子进程自动读取；浏览器是否走代理仍由浏览器自身配置决定。
load_dotenv()

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "output")

def get_video_dir(video_id: str) -> str:
    """Gets the output directory for a specific video."""
    video_dir = os.path.join(OUTPUT_DIR, video_id)
    os.makedirs(video_dir, exist_ok=True)
    return video_dir
