"""视频标识公共函数测试。"""

from _helpers import SRC  # noqa: F401  (SRC 保证能 import src/)

from video_id import make_bilibili_episode_id, split_bilibili_id


def test_split_bilibili_episode_id():
    assert split_bilibili_id("BV1dr4y1n7vA_p7") == ("BV1dr4y1n7vA", 7)
    assert split_bilibili_id("BV1dr4y1n7vA_p07") == ("BV1dr4y1n7vA", 7)


def test_split_plain_id_defaults_to_first_page():
    assert split_bilibili_id("BV1dr4y1n7vA") == ("BV1dr4y1n7vA", 1)
    assert split_bilibili_id("") == ("", 1)


def test_make_bilibili_episode_id_normalizes_page_number():
    assert make_bilibili_episode_id("BV1dr4y1n7vA", 7) == "BV1dr4y1n7vA_p7"
    assert make_bilibili_episode_id("BV1dr4y1n7vA", "07") == "BV1dr4y1n7vA_p7"
