"""VTT 毫秒时间戳的归一化，以及耗时解析容错（离线，不联网，不碰 output/）。

2026-09-22 的缺陷：yt-dlp 兜底抓字幕时带 `--sub-format vtt/best`，拿到 YouTube
的 VTT 时 webvtt 0.5.1 的 `caption.start` 是 '00:00:01.000'（带毫秒），而
`convert_vtt_to_json` 把它**原样**塞进 segments —— 另外两条路径
（`convert_srt_to_json` 与 youtube-transcript-api）都过 `format_timestamp`
归一化成 'HH:MM:SS'，只有 VTT 这条漏了。

后果：旧覆盖率计算用 `int(x) for x in str(ts).split(":")` 解析
'01.000' 抛 ValueError → 静默 return 0 → 覆盖率算成 0/98 = 0.0% → 打印
"× 覆盖率过低，疑似抓取不全"并 sys.exit(3)，把排查方向引到登录态上。
（B 站不受影响：B 站字幕只有 srt，走的是已归一化的那条路径。）
"""
import os
import tempfile

from _helpers import SRC  # noqa: F401  (SRC 保证能 import src/)

import transcript_quality as quality
from scraper import convert_srt_to_json, convert_vtt_to_json, format_timestamp

# 真实的 WebVTT 文本（YouTube 的 vtt 一定带 .mmm，且 时:分:秒 齐全）
VTT = """WEBVTT

00:00:01.000 --> 00:00:04.500
第一句字幕

01:02:03.250 --> 01:02:05.750
第二句字幕

00:01:38.000 --> 00:01:38.900
最后一句
"""

SRT = """1
00:00:01,000 --> 00:00:04,500
第一句字幕

2
01:02:03,250 --> 01:02:05,750
第二句字幕
"""


def _write(name, text):
    d = tempfile.mkdtemp(prefix="vidscribe_timecode_")
    p = os.path.join(d, name)
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)
    return p


def _full_vtt(duration=98):
    """一份覆盖 0 → duration 秒的完整 VTT（每 2 秒一句）。"""
    blocks = ["%s --> %s\n第 %d 句" % (_ts(i), _ts(i + 2), i)
              for i in range(0, duration, 2)]
    return "WEBVTT\n\n" + "\n\n".join(blocks) + "\n"


def _ts(sec):
    return "00:%02d:%02d.000" % divmod(sec, 60)


def test_vtt_segments_are_normalized_to_hhmmss():
    segs = convert_vtt_to_json(_write("t.zh.vtt", VTT))

    assert [s["start"] for s in segs] == ["00:00:01", "01:02:03", "00:01:38"]
    assert [s["end"] for s in segs] == ["00:00:04", "01:02:05", "00:01:38"]
    assert all("." not in s["start"] + s["end"] for s in segs)
    assert [s["text"] for s in segs] == ["第一句字幕", "第二句字幕", "最后一句"]

    # 下游 make_corrected.py 的 `h, m, s = seg["start"].split(":")` 也能过了
    h, m, s = segs[1]["start"].split(":")
    assert (int(h), int(m), int(s)) == (1, 2, 3)


def test_coverage_accepts_milliseconds():
    """历史遗留 / 手工编辑过的 transcript 里带毫秒时，不要再静默算成 0。"""
    transcript = {"duration": 100, "segments": [{"end": "00:01:02.500"}]}
    assert quality.coverage_ratio(transcript) == 0.62

    transcript["segments"][-1]["end"] = "坏数据"
    assert quality.coverage_ratio(transcript) == 0


def test_full_vtt_coverage_is_not_zero():
    """端到端一点：覆盖全片长的 VTT 算出的覆盖率必须 ≥ 0.5。

    改前末段时间解析因毫秒返回 0 → 0.0% → dump_transcript
    误判"抓取不全"并 sys.exit(3)。
    """
    segs = convert_vtt_to_json(_write("full.zh.vtt", _full_vtt(98)))
    duration = 98.0

    coverage = quality.coverage_ratio({"segments": segs, "duration": duration})

    assert coverage >= 0.5, f"覆盖率 {coverage:.1%}，会被判成抓取失败"


def test_srt_and_transcript_api_paths_unchanged():
    """回归：另外两条路径的产物格式必须还是老样子。"""
    segs = convert_srt_to_json(_write("t.zh.srt", SRT))
    assert [s["start"] for s in segs] == ["00:00:01", "01:02:03"]
    assert [s["end"] for s in segs] == ["00:00:04", "01:02:05"]

    # youtube-transcript-api 路径：秒 → HH:MM:SS
    assert format_timestamp(0) == "00:00:00"
    assert format_timestamp(62.5) == "00:01:02"
    assert format_timestamp(3723.0) == "01:02:03"
