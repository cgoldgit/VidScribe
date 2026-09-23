"""全项目唯一的时间戳解析 / 格式化实现（R0-02 第 1、3 条）。

时间戳解析此前在 6 个文件里各写了一份，判据还互不相同：dump/batch 只认冒号
分隔的 2/3 段、post_process 只认 2/3 段、capture_frames 用 split 成三个 int、
ASR 与索引图各有一份「秒 → HH:MM:SS」。带毫秒的 VTT 那次事故
（`_to_sec("00:01:02.500")` 算成 0 → 覆盖率 0.0% → 误判「抓取不全」退出 3）
就是「各写一份」的直接后果。

本模块只有两个方向，共三个函数：
    parse_seconds   文本 → 整数秒
    format_hms      秒 → HH:MM:SS（全项目统一格式）
    format_mmss     秒 → MM:SS（只给与老 transcript.txt 对齐的两个产物用）

各调用点保留自己原有的兜底策略，模块不替它们决定（这些是既有行为，不许改）：

    transcript_quality.coverage_ratio                     解析失败 → 0
    post_process.timestamp_to_seconds                     解析失败 → 0
    asr_transcript.hhmmss                                 负秒钳到 0、四舍五入
    capture_frames.fmt_ts                                 四舍五入到秒
    sprite_index.fmt_hms                                  截断小数
"""


class ValidationError(ValueError):
    """时间戳非法：空值、格式不对、分/秒越界、负数。

    刻意继承 ValueError —— 旧调用点里 `except ValueError: return 0` 这类兜底
    不需要跟着改就继续生效（"解析失败"本来也就是一种 ValueError），
    也只有这样才能做到"非法值抛 ValidationError"而不给调用方多出一种异常。
    """


def _number(text, value):
    """单个字段 → float。只有十进制数字（可带小数）算合法。"""
    try:
        n = float(text.strip())
    except ValueError:
        raise ValidationError(f"时间戳字段不是数字: {value!r}") from None
    # float() 认得 'nan' / 'inf'，它们拿去做 int() 或比大小都不对，直接挡掉
    if n != n or n in (float("inf"), float("-inf")):
        raise ValidationError(f"时间戳字段不是有限数: {value!r}")
    return n


def _field(text, value, limit, what):
    """字段 → 非负整数（小数截断，与旧 int(float(x)) 一致）。

    limit 是**开区间**上界：秒/分要求 `0 <= x < limit`。用开区间而不是
    `<= 59`，是为了让 "00:00:59.500"（带毫秒的合法值）不被 59.5 > 59 误杀。
    limit=None 表示不设上限（小时、以及 MM:SS 里的"分"）。
    """
    if not text.strip():
        raise ValidationError(f"时间戳字段为空: {value!r}")
    n = _number(text, value)
    if n < 0:
        raise ValidationError(f"{what}不能为负: {value!r}")
    if limit is not None and n >= limit:
        raise ValidationError(f"{what}超出上限: {value!r}")
    return int(n)


def parse_seconds(value) -> int:
    """时间戳 → 整数秒。

    接受：
        "HH:MM:SS"      "01:38:26"       -> 5906
        "HH:MM:SS.mmm"  "00:01:02.500"   -> 62     （小数秒截断，不四舍五入）
        "MM:SS"         "98:26"          -> 5906   （分可以超过 59）
        "62" / 62 / 62.9                 -> 62

    非法（抛 ValidationError）：空值、非字符串非数字、"aa:bb"、
    分或秒越界（"00:99:00"）、负数。带毫秒的合法值不算越界：
    "00:00:59.500" -> 59，而 "00:00:60" 非法。

    注意 MM:SS 的"分"不设上限 —— `[98:26]` 这种"分承载小时"的写法是
    transcript.txt / transcript.corrected.txt 的既有排版格式，98 分钟合法；
    而 HH:MM:SS 里的"分"必须 0-59。
    """
    if isinstance(value, bool) or value is None:
        raise ValidationError(f"无法解析的时间戳: {value!r}")
    if isinstance(value, (int, float)):
        return _field(str(value), value, None, "秒数")
    if not isinstance(value, str):
        raise ValidationError(f"无法解析的时间戳: {value!r}")

    parts = value.strip().split(":")
    if len(parts) > 3:
        raise ValidationError(f"时间戳段数过多（最多 时:分:秒）: {value!r}")

    if len(parts) == 1:
        return _field(parts[0], value, None, "秒数")

    # 最后一段是「秒」（允许小数）；它前面是 时:分 或 分
    sec = _field(parts[-1], value, 60, "秒")
    if len(parts) == 2:
        return _field(parts[0], value, None, "分") * 60 + sec
    return (_field(parts[0], value, None, "时") * 3600
            + _field(parts[1], value, 60, "分") * 60 + sec)


def format_hms(seconds) -> str:
    """秒 → HH:MM:SS（全项目统一格式，永不输出 "98:26" 这种分:秒写法）。

    小数**截断**（= 旧 sprite_index.fmt_hms 的行为）。四舍五入、负数钳制
    是各调用点自己的策略，由调用点自己做（见模块 docstring 里的策略表）。
    1 小时以上照样进位成小时：5910 -> "01:38:30"，而不是 "98:30"。
    """
    s = int(seconds)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def format_mmss(seconds) -> str:
    """秒 → "MM:SS"，**分**承载小时：5906 -> "98:26"。

    只给两个与老 transcript.txt 对齐的产物用：
        make_corrected.py 的 transcript.corrected.txt 行首
        asr_transcript.py 的 transcript.txt 行首
    正文与索引图标签**不许**用这个格式（AI 会照着写成 SCREENSHOT:98:26，
    那是 PLACEHOLDER_RE 匹配不上的坏占位符），那边一律 format_hms。
    """
    s = int(seconds)
    return f"{s // 60:02d}:{s % 60:02d}"
