"""Editorial evidence checks, not an automated truth detector.

Find actual chapter summaries, retain correction evidence, flag promotional calls
for review. No model/web calls and no deletion or rewriting of note contents.
"""
import re
from urllib.parse import urlsplit


PROMOTIONAL_CUES = (
    re.compile(r'扫码[^\n。]{0,12}(?:购买|报名|领券|优惠)'),
    re.compile(r'(?:欢迎|记得|请|赶紧)[^\n。]{0,8}关注[^\n。]{0,8}(?:公众号|店铺|账号)'),
    re.compile(r'购买链接[^\n。]{0,8}(?:在|见|放)'),
    re.compile(r'优惠码|限时[^\n。]{0,6}(?:优惠|折扣)|(?:感谢|本期)[^\n。]{0,10}赞助商'),
    re.compile(r'领券[^\n。]{0,8}(?:下单|购买)|关注公众号[^\n。]{0,8}(?:优惠|福利)|购买链接\s*[:：]'),
)
SUMMARY_LABEL = re.compile(r'^\s*(?:\*\*本章小结\*\*\s*[:：]?|###\s+(?:\d+(?:\.\d+)*\s+)?本章小结\s*#*\s*$)')


def _structure(content):
    """Sections contain raw body plus visible lines outside fenced code."""
    rows = content.splitlines()
    headings, visible = [], []
    fence = None
    for index, line in enumerate(rows):
        match = re.match(r'^ {0,3}(`{3,}|~{3,})', line)
        if fence is not None:
            if match and match.group(1)[0] == fence[0] and len(match.group(1)) >= fence[1]:
                fence = None
            continue
        if match:
            fence = (match.group(1)[0], len(match.group(1)))
            continue
        visible.append((index, line))
        heading = re.match(r'^(#{1,6})\s+(.+?)\s*#*\s*$', line)
        if heading:
            headings.append((index, len(heading.group(1)), heading.group(2)))
    sections = []
    for start, level, title in headings:
        end = next((i for i, depth, _ in headings if i > start and depth <= level), len(rows))
        sections.append({'title': title, 'level': level, 'line': start + 1,
                         'body': '\n'.join(rows[start + 1:end]),
                         'visible': [(i, text) for i, text in visible if start < i < end]})
    return sections, visible


def _string(value):
    return isinstance(value, str) and bool(value.strip())


def _evidence(items):
    if not isinstance(items, list) or not items:
        return False
    for item in items:
        if not isinstance(item, dict) or item.get('kind') not in ('reference', 'calculation', 'execution') or not _string(item.get('detail')):
            return False
        if item['kind'] == 'reference':
            url = item.get('url')
            if not isinstance(url, str):
                return False
            try:
                parsed = urlsplit(url)
                if parsed.scheme not in ('http', 'https') or not parsed.netloc or parsed.username or parsed.password:
                    return False
            except ValueError:
                return False
    return True


def check_editorial(content, review=None, *, required=False):
    errors, warnings = [], []
    sections, visible = _structure(content)
    chapters = [s for s in sections if s['level'] == 2]
    result = {'errors': errors, 'warnings': warnings, 'chapter_count': len(chapters),
              'summary_count': 0, 'correction_count': 0,
              'limits': ['Checks summary placement and evidence records; cannot verify factual truth or automatically distinguish all promotions.']}

    def issue(code, *, strict=True, **details):
        (errors if required and strict else warnings).append({'code': code, **details})

    if not chapters:
        issue('no_major_chapters')
    for chapter in chapters:
        labels = [(i, line, SUMMARY_LABEL.match(line)) for i, line in chapter['visible'] if SUMMARY_LABEL.match(line)]
        if not labels:
            issue('chapter_summary_missing', section=chapter['title'], line=chapter['line'])
            continue
        if len(labels) != 1:
            issue('chapter_summary_duplicate', section=chapter['title'])
            continue
        position, line, label = labels[0]
        summary = line[label.end():].strip()
        after = [text for i, text in chapter['visible'] if i > position]
        if any(re.match(r'^#{3,6}\s+', text) for text in after):
            issue('chapter_summary_not_at_end', section=chapter['title'])
        summary += '\n' + '\n'.join(after)
        # Stable job anchors/comments are metadata, not summary prose.
        summary = re.sub(r'<!--.*?-->', '', summary, flags=re.S).strip()
        if not summary or re.fullmatch(r'(?:TODO|TBD|待补(?:充)?|待写|待完善|占位)[。.!！]*', summary, re.I):
            issue('chapter_summary_empty', section=chapter['title'])
        else:
            result['summary_count'] += 1
    for index, line in visible:
        if any(pattern.search(line) for pattern in PROMOTIONAL_CUES):
            issue('promotional_language_needs_review', strict=False, line=index + 1, excerpt=line[:160])

    if not isinstance(review, dict):
        issue('fact_review_missing')
        return result
    if review.get('fact_review') != 'complete':
        issue('fact_review_pending')
    elif not _string(review.get('fact_review_reason')):
        issue('fact_review_reason_missing')
    facts = review.get('corrections', [])
    if not isinstance(facts, list):
        issue('invalid_corrections')
        return result
    for record in facts:
        if not isinstance(record, dict):
            issue('invalid_correction')
            continue
        result['correction_count'] += 1
        section, status = record.get('section'), record.get('status')
        targets = [s for s in sections if s['title'] == section]
        ranges = record.get('source_ranges')
        if (record.get('kind') not in ('lecture_error', 'transcription') or not _string(record.get('original'))
                or not isinstance(ranges, list) or not ranges
                or any(not isinstance(pair, list) or len(pair) != 2
                       or any(not isinstance(s, str) or not re.fullmatch(r'S[0-9]{6,}', s) for s in pair)
                       or int(pair[0][1:]) > int(pair[1][1:]) for pair in ranges)):
            issue('invalid_correction_source', section=section)
        display = record.get('display')
        visible_body = '\n'.join(line for _, line in targets[0]['visible']) if len(targets) == 1 else ''
        if not _string(display) or len(targets) != 1 or display not in visible_body:
            issue('correction_not_visible_in_section', section=section)
        if status == 'verified':
            if (not _string(record.get('replacement')) or record.get('replacement') == record.get('original')
                    or not _evidence(record.get('evidence'))):
                issue('correction_evidence_missing', section=section)
            if not isinstance(display, str) or '勘误' not in display:
                issue('correction_label_missing', section=section)
        elif status == 'unresolved':
            if not isinstance(display, str) or '待核实' not in display:
                issue('uncertain_claim_not_labeled', section=section)
            issue('unresolved_factual_claim', strict=False, section=section, original=record.get('original'))
        else:
            issue('correction_status_pending', section=section)
    return result
