"""Synthetic checks for chapter summaries and explicitly evidenced corrections.

These gates validate editorial records, not the truth of arbitrary claims.
"""
import copy

import _helpers  # Registers src for the standalone test runner.
import note_editorial


def _book(extra="", first_summary="**本章小结**\n\n本章用边界条件解释递归停止。"):
    return (
        "# 递归课程\n\n"
        "## 1. 递归基础\n\n"
        "先确定边界，再缩小问题规模。\n\n"
        + extra + "\n\n" + first_summary + "\n\n"
        "## 2. 汉诺塔\n\n"
        "把 n-1 层移到辅助柱，再移动底层。\n\n"
        "### 2.3 本章小结\n\n"
        "本章把递归拆成两次子问题和一次底层移动。\n"
    )


def _review():
    return {"fact_review": "complete",
            "fact_review_reason": "已按原字幕核对推导，未发现需更正的陈述。",
            "corrections": []}


def _correction():
    return {"kind": "lecture_error", "status": "verified", "section": "1. 递归基础",
            "source_ranges": [["S000001", "S000002"]],
            "original": "汉诺塔最少移动次数是 2n-1。",
            "replacement": "汉诺塔最少移动次数是 2^n-1。",
            "display": "勘误：课堂所述 2n-1 应为 2^n-1。",
            "evidence": [{"kind": "calculation",
                          "detail": "T(1)=1，T(n)=2T(n-1)+1，故 T(n)=2^n-1。"}]}


def _assert_blocked(content, review, label=""):
    result = note_editorial.check_editorial(content, review, required=True)
    assert result["errors"], (label, result)


def test_complete_each_chapter_summary_and_fact_review_pass():
    result = note_editorial.check_editorial(_book(), _review(), required=True)
    assert result["chapter_count"] == 2, result
    assert not result["errors"], result
    assert not result["warnings"], result


def test_old_notes_missing_summary_and_fact_review_warn_until_required():
    legacy = "# 课程\n\n## 原有章节\n\n课堂内容。\n"
    result = note_editorial.check_editorial(legacy)
    assert not result["errors"], result
    assert result["warnings"], result
    _assert_blocked(legacy, None)


def test_one_missing_chapter_summary_cannot_be_replaced_by_book_summary():
    content = _book(first_summary="") + "\n## 全文总结\n\n以上介绍了递归和汉诺塔。\n"
    _assert_blocked(content, _review())


def test_blank_chapter_summary_blocks_delivery():
    for marker in ("**本章小结**", "### 本章小结", "### 1.3 本章小结"):
        _assert_blocked(_book(first_summary=marker + "\n\n"), _review(), marker)


def test_supported_summary_markers_have_nonempty_content():
    for marker in ("**本章小结**", "### 本章小结", "### 1.3 本章小结"):
        content = _book(first_summary=marker + "\n\n递归需要边界条件和规模缩减。")
        result = note_editorial.check_editorial(content, _review(), required=True)
        assert not result["errors"], (marker, result)


def test_summary_followed_by_new_subsection_is_not_chapter_ending():
    ending = "### 本章小结\n\n递归需要边界。\n\n### 1.4 另一个例子\n\n继续讲解递归。"
    _assert_blocked(_book(first_summary=ending), _review())


def test_duplicate_chapter_summary_is_reported():
    ending = "**本章小结**\n\n第一次总结。\n\n### 本章小结\n\n第二次总结。"
    _assert_blocked(_book(first_summary=ending), _review())


def test_fenced_headings_summaries_and_ads_are_not_real_editorial_content():
    fenced = (
        "```markdown\n## 虚假的大章\n### 本章小结\n扫码购买，领券并关注公众号。\n```\n\n"
        "~~~markdown\n## 另一个虚假的大章\n**本章小结**\n购买链接\n~~~"
    )
    result = note_editorial.check_editorial(_book(extra=fenced), _review(), required=True)
    assert result["chapter_count"] == 2, result
    assert not result["errors"], result
    assert not result["warnings"], result


def test_summary_inside_code_fence_does_not_satisfy_missing_summary():
    content = _book(first_summary="```markdown\n**本章小结**\n这只是代码示例。\n```")
    _assert_blocked(content, _review())


def test_missing_pending_and_invalid_fact_review_block_required_delivery():
    for status in (None, "pending", "unknown", {}, []):
        review = _review()
        if status is None:
            review.pop("fact_review")
        else:
            review["fact_review"] = status
        _assert_blocked(_book(), review, repr(status))


def test_complete_fact_review_needs_nonblank_reason():
    for reason in (None, "", " \n\t"):
        review = _review()
        if reason is None:
            review.pop("fact_review_reason")
        else:
            review["fact_review_reason"] = reason
        _assert_blocked(_book(), review, repr(reason))


def test_verified_calculation_correction_must_be_visible_in_matching_section():
    correction = _correction()
    review = _review()
    review["corrections"] = [correction]
    content = _book(extra=correction["display"])
    result = note_editorial.check_editorial(content, review, required=True)
    assert not result["errors"], result
    _assert_blocked(_book(), review, "hidden correction")
    _assert_blocked(_book().replace("把 n-1 层", correction["display"] + "\n把 n-1 层"),
                    review, "display placed in another chapter")


def test_verified_correction_requires_evidence_and_real_replacement():
    base = _correction()
    for field, value in (("evidence", []), ("original", ""), ("replacement", " "),
                         ("replacement", base["original"])):
        correction = copy.deepcopy(base)
        correction[field] = value
        review = _review()
        review["corrections"] = [correction]
        _assert_blocked(_book(extra=correction["display"]), review, field)


def test_verified_correction_requires_explicit_correction_label():
    correction = _correction()
    correction["display"] = "汉诺塔最少移动次数是 2^n-1。"
    review = _review()
    review["corrections"] = [correction]
    _assert_blocked(_book(extra=correction["display"]), review)


def test_reference_correction_requires_http_url_and_evidence_detail():
    base = _correction()
    for evidence in (
        {"kind": "reference", "detail": "查阅递归公式"},
        {"kind": "reference", "detail": "查阅递归公式", "url": "file:///local/formula"},
        {"kind": "reference", "detail": "查阅递归公式", "url": "javascript:alert(1)"},
        {"kind": "reference", "detail": " ", "url": "https://example.org/formula"},
        {"kind": "guess", "detail": "模型记忆中的公式"},
    ):
        correction = copy.deepcopy(base)
        correction["evidence"] = [evidence]
        review = _review()
        review["corrections"] = [correction]
        _assert_blocked(_book(extra=correction["display"]), review, repr(evidence))


def test_reference_and_execution_evidence_records_are_supported():
    base = _correction()
    for evidence in (
        {"kind": "reference", "detail": "核对推导公式", "url": "https://example.org/formula"},
        {"kind": "reference", "detail": "核对推导公式", "url": "http://example.org/formula"},
        {"kind": "execution", "detail": "运行递归实现，n=3 输出 7 次移动。"},
    ):
        correction = copy.deepcopy(base)
        correction["evidence"] = [evidence]
        review = _review()
        review["corrections"] = [correction]
        result = note_editorial.check_editorial(_book(extra=correction["display"]), review,
                                               required=True)
        assert not result["errors"], (evidence, result)


def test_transcription_fix_accepts_evidence_but_still_needs_visible_correction():
    correction = _correction()
    correction["kind"] = "transcription"
    review = _review()
    review["corrections"] = [correction]
    result = note_editorial.check_editorial(_book(extra=correction["display"]), review,
                                           required=True)
    assert not result["errors"], result
    _assert_blocked(_book(), review)


def test_unresolved_claim_is_visible_as_warning_instead_of_silent_correction():
    correction = _correction()
    correction.update(status="unresolved", replacement="", evidence=[],
                      display="待核实：课堂给出的移动次数公式尚未完成验证。")
    review = _review()
    review["corrections"] = [correction]
    result = note_editorial.check_editorial(_book(extra=correction["display"]), review,
                                           required=True)
    assert not result["errors"], result
    assert result["warnings"], result
    _assert_blocked(_book(), review, "uncertainty hidden from reader")
    correction["display"] = "课堂移动次数为 2^n-1。"
    _assert_blocked(_book(extra=correction["display"]), review, "missing uncertainty label")


def test_correction_display_in_code_fence_is_not_a_reader_visible_erratum():
    correction = _correction()
    review = _review()
    review["corrections"] = [correction]
    _assert_blocked(_book(extra="```text\n" + correction["display"] + "\n```"), review)


def test_strong_promotional_calls_warn_without_blocking_or_changing_content():
    for promotion in ("扫码购买课程。", "现在领券即可下单。", "关注公众号获取优惠。",
                      "购买链接：https://example.org/course"):
        content = _book(extra=promotion)
        original = content
        result = note_editorial.check_editorial(content, _review(), required=True)
        assert not result["errors"], (promotion, result)
        assert result["warnings"], (promotion, result)
        assert content == original


def test_tool_names_ads_topic_and_instructional_scan_are_not_ads():
    text = "使用 Claude Code 与 DeepSeek 整理代码。广告识别是分类任务。操作演示：打开测试扫码页面，检查二维码解码结果。"
    result = note_editorial.check_editorial(_book(extra=text), _review(), required=True)
    assert not result["errors"], result
    assert not result["warnings"], result


def test_quoted_wrong_code_example_is_preserved_without_promotion_warning():
    example = "错误示例用于说明广告分类模型的误判：\n\n```python\ntext = '扫码购买，领券并关注公众号，购买链接'\n```"
    result = note_editorial.check_editorial(_book(extra=example), _review(), required=True)
    assert not result["errors"], result
    assert not result["warnings"], result
