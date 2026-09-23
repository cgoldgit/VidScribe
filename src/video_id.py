"""视频标识的公共解析与构造函数。"""

import re


_BILIBILI_PART_RE = re.compile(r"^(BV\w+?)_p(\d+)$")


def split_bilibili_id(video_id: str):
    """`BV..._p7` → (`BV...`, 7)；普通视频默认返回分P 1。"""
    match = _BILIBILI_PART_RE.match(video_id or "")
    if match:
        return match.group(1), int(match.group(2))
    return video_id, 1


def make_bilibili_episode_id(bvid: str, page: int) -> str:
    """构造流水线统一使用的多分P目录标识。"""
    return f"{bvid}_p{int(page)}"
