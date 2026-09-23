"""字幕完整性检查：统一单视频和批量抓取的覆盖率判据。"""

import timecode


MIN_COVERAGE = 0.5


def coverage_ratio(transcript):
    """返回字幕覆盖率；缺少字幕或有效时长时返回 None，表示无法计算。

    历史字幕的时间戳可能带毫秒；非法末尾时间戳沿用旧行为按 0 秒处理。
    """
    if not isinstance(transcript, dict):
        return None
    segments = transcript.get("segments") or []
    duration = float(transcript.get("duration") or 0)
    if not segments or duration <= 0:
        return None
    try:
        end = timecode.parse_seconds(segments[-1].get("end"))
    except (AttributeError, ValueError):
        end = 0
    return end / duration


def is_usable(transcript, minimum=MIN_COVERAGE):
    """字幕存在，且可计算时覆盖率不低于门槛。"""
    if not isinstance(transcript, dict) or not transcript.get("segments"):
        return False
    coverage = coverage_ratio(transcript)
    return coverage is None or coverage >= minimum
