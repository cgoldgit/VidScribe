"""Bounded CC jobs: prepare, run-one, assemble, visual, apply-visual.

Preparing/assembling never invokes a model. Only run-one starts a fresh CC process.
Draft outputs are separate from book.md; no publication or automatic promotion.
"""
import argparse
import collections
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time

import note_audit
import note_cache
import note_candidates
import note_editorial
import note_job_lock
import note_plan
import note_results
import timecode

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass

BASE_DIR = Path(__file__).resolve().parent.parent


def _json(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _save(path, data):
    note_plan._atomic_text(path, json.dumps(data, ensure_ascii=False, indent=2) + '\n')


def _inside(root, relative):
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise ValueError('Expected a relative path inside the video directory')
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError('Path escapes the video directory')
    return path


def _load_plan(directory):
    root = Path(directory).resolve()
    plan = _json(root / 'note_plan.json')
    if plan.get('source_sha256') != _sha(root / 'transcript.json'):
        raise ValueError('Source changed; rebuild and review the plan first')
    sprite = root / 'sprite_index.json'
    if plan.get('sprite_sha256') != (_sha(sprite) if sprite.exists() else None):
        raise ValueError('Visual index changed; rebuild the plan first')
    owned = []
    packet_ids = set()
    for packet in plan['packets']:
        if not re.fullmatch(r'P\d{3,}', packet['id']) or packet['id'] in packet_ids:
            raise ValueError('Invalid or duplicate packet ID')
        packet_ids.add(packet['id'])
        _inside(root, packet['path'])
        owned.extend(packet['source_segments'])
    if (len(owned) != plan['segment_count'] or len(set(owned)) != len(owned)
            or set(owned) != {f'S{i:06d}' for i in range(1, plan['segment_count'] + 1)}):
        raise ValueError('Plan does not cover the transcript exactly once')
    return root, plan


def _ranges(ids):
    ranges = []
    for identifier in ids:
        if ranges and int(identifier[1:]) == int(ranges[-1][1][1:]) + 1:
            ranges[-1][1] = identifier
        else:
            ranges.append([identifier, identifier])
    return ranges


def _write_job(root, relative, metadata, task, template=None):
    job = _inside(root, relative)
    job.mkdir(parents=True, exist_ok=True)
    metadata['task_sha256'] = hashlib.sha256(task.encode('utf-8')).hexdigest()
    old = job / 'job.json'
    if old.exists() and _json(old) != metadata:
        raise ValueError(f'Job inputs changed: {relative}; use a separate experiment directory')
    # Hand-authored outputs and existing templates are never replaced here.
    if not old.exists():
        _save(old, metadata)
    if not (job / 'task.md').exists():
        note_plan._atomic_text(job / 'task.md', task)
    if template is not None and not (job / 'review.template.json').exists():
        _save(job / 'review.template.json', template)
    return relative + '/task.md'


def _writing_task(root, packet, writer_path):
    packet_path = _inside(root, packet['path'])
    terms = root / 'TERMS.confirmed.md'
    pid = packet['id']
    return (f'# 独立写作任务 {pid}\n\n'
            '这是一个章节任务，已有任务说明优先；不要执行全片生成流程或派生其他 agent。\n'
            f'只读本任务、`{packet_path}` 与下列规则；不读取其他包、全片字幕、旧书稿或图片。\n'
            f'独立写作规则：`{writer_path}`。\n'
            + (f'已有确认术语：`{terms}`。\n' if terms.exists() else '')
            + '逐句阅读，轻度整理口癖；保留条件、原例子、推导、操作、纠错和结果。术语有歧义时保留原词并写待核项，不能猜版本或数字。\n'
            '前后文只帮助理解，不重复写入；跨包未完的例子记在 boundary_notes。\n'
            '只写两个本任务文件：draft.md、review.json；复制 review.template.json 后填写。\n'
            'draft.md 不含书名、统一编号或制作说明；用 ## 和 ### 标题，每个标题前写稳定锚点，例如 '
            f'`<!-- section:{pid}.s01 -->`。\n'
            'coverage 按完整连续句子范围填写 source_ranges，使用 section_id 指向锚点；written/omitted 二选一，omitted 写理由，每句恰好一次。\n'
            '视觉需求写 time(HH:MM:SS)/status=pending/reason/section_id；只提出预期，不声称看过画面。'
            '对应正文用 ![待核用途](SCREENSHOT:HH:MM:SS)。明确无需图片时说明依据，仍需如实填写 visual_review。\n'
            '不得给未核查的图片写确定内容。输出完成后只返回文件路径、歧义和边界提示，不回传全文。\n')


def prepare(directory):
    root, plan = _load_plan(directory)
    tasks = []
    terms = root / 'TERMS.confirmed.md'
    terms_sha = _sha(terms) if terms.exists() else None
    style_sha = _sha(BASE_DIR / 'prompts/stitcher_system.md')
    writer_path = BASE_DIR / 'prompts/writer_system.md'
    writer_sha = _sha(writer_path)
    for packet in plan['packets']:
        packet_path = _inside(root, packet['path'])
        pid = packet['id']
        template = {'source_sha256': plan['source_sha256'], 'packet_id': pid,
                    'coverage': [{'packet_id': pid, 'source_ranges': _ranges(packet['source_segments']),
                                  'status': 'pending', 'section_id': '', 'reason': ''}],
                    'visual_requests': [], 'visual_review': 'pending',
                    'visual_review_reason': '', 'uncertainties': [], 'boundary_notes': [],
                    'editorial_policy_version': 1, 'fact_review': 'pending',
                    'fact_review_reason': '', 'corrections': []}
        task = _writing_task(root, packet, writer_path)
        metadata = {'schema_version': 1, 'kind': 'writing', 'packet_id': pid,
                    'source_sha256': plan['source_sha256'], 'sprite_sha256': plan.get('sprite_sha256'),
                    'packet_path': packet['path'], 'packet_sha256': _sha(packet_path),
                    'terms_sha256': terms_sha, 'style_sha256': style_sha,
                    'writer_sha256': writer_sha, 'editorial_policy_version': 1,
                    'review_template_sha256': hashlib.sha256(
                        (json.dumps(template, ensure_ascii=False, indent=2) + '\n').encode('utf-8')).hexdigest()}
        tasks.append(_write_job(root, f'jobs/{pid}', metadata, task, template))
    manifest = {'source_sha256': plan['source_sha256'], 'tasks': tasks,
                'rule': 'One fresh context per task; main agent receives summaries only.'}
    _save(root / 'note_jobs.json', manifest)
    return manifest


def _headings(text):
    """Ignore fenced code; resolve stable section anchors to actual headings."""
    sections = {}
    titles = set()
    pending = None
    fenced = None
    for line in text.splitlines():
        fence = re.match(r'^\s*(`{3,}|~{3,})', line)
        if fence:
            if fenced is None:
                fenced = fence.group(1)[0]
            elif fence.group(1)[0] == fenced:
                fenced = None
            continue
        if fenced:
            continue
        anchor = re.fullmatch(r'\s*<!-- section:([A-Za-z0-9_.-]+) -->\s*', line)
        if anchor:
            pending = anchor.group(1)
            continue
        heading = re.match(r'^(#{1,6})\s+(.+?)\s*$', line)
        if heading:
            if heading.group(1) not in ('##', '###'):
                raise ValueError('Draft headings must use ## or ###')
            title = heading.group(2)
            titles.add(title)
            if pending:
                if pending in sections:
                    raise ValueError('Duplicate section anchor')
                sections[pending] = title
                pending = None
    if fenced:
        raise ValueError('Unclosed code fence')
    if not titles:
        raise ValueError('Draft requires headings')
    return sections, titles


def _section(entry, sections, titles):
    sid = entry.get('section_id')
    title = sections.get(sid) if sid else entry.get('section')
    if not title or title not in titles:
        raise ValueError('Review refers to a missing section')
    return title


def _number(text, chapter=0, subsection=0):
    lines, mapping, fenced = [], {}, None
    for line in text.splitlines():
        fence = re.match(r'^\s*(`{3,}|~{3,})', line)
        if fence:
            fenced = fence.group(1)[0] if fenced is None else None if fenced == fence.group(1)[0] else fenced
        match = None if fenced or fence else re.match(r'^(#{2,3})\s+(.+?)\s*$', line)
        if match:
            level, title = match.groups()
            if level == '##':
                chapter += 1
                subsection = 0
                number = _chinese(chapter)
                new = f'{number}、{title}'
            else:
                if chapter == 0:
                    raise ValueError('A subsection needs a preceding chapter')
                subsection += 1
                new = f'{chapter}.{subsection} {title}'
            if title in mapping:
                raise ValueError('Duplicate titles within a draft need distinct names')
            mapping[title] = new
            line = level + ' ' + new
        lines.append(line)
    return '\n'.join(lines), mapping, chapter, subsection


def _chinese(number):
    digits = '零一二三四五六七八九'
    if number < 10:
        return digits[number]
    if number < 100:
        return (digits[number // 10] if number >= 20 else '') + '十' + (digits[number % 10] if number % 10 else '')
    return str(number)


def assemble(directory):
    root, plan = _load_plan(directory)
    pieces, coverage, requests, uncertainties, boundaries = [], [], [], [], []
    chapter = subsection = 0
    reviewed, visual_reasons, visual_packets = [], [], []
    corrections, fact_reasons, facts_complete, strict_chapters = [], [], [], set()
    for packet in plan['packets']:
        pid = packet['id']
        job = _inside(root, f'jobs/{pid}')
        metadata = _validate_job(root, job, plan)
        draft = (job / 'draft.md').read_text(encoding='utf-8')
        review = _json(job / 'review.json')
        if review.get('source_sha256') != plan['source_sha256'] or review.get('packet_id') != pid:
            raise ValueError(f'Stale or mismatched review: {pid}')
        sections, titles = _headings(draft)
        numbered, mapping, chapter, subsection = _number(draft, chapter, subsection)
        fact_done = (review.get('fact_review') == 'complete'
                     and bool(str(review.get('fact_review_reason') or '').strip()))
        if metadata.get('editorial_policy_version') == 1:
            if not fact_done:
                raise ValueError(f'Fact review is pending: {pid}')
            local_sections, _ = note_editorial._structure(numbered)
            strict_chapters.update(s['title'] for s in local_sections if s['level'] == 2)
        facts_complete.append(fact_done)
        fact_reasons.append(str(review.get('fact_review_reason') or ''))
        records = review.get('corrections', [])
        if not isinstance(records, list) or any(not isinstance(r, dict) for r in records):
            raise ValueError('Invalid correction records')
        for record in records:
            item = copy.deepcopy(record)
            ids = note_audit.normalize_coverage_ids(item, packet['source_segments'])
            item['section'] = mapping[_section(item, sections, titles)]
            item['source_ranges'] = _ranges(ids)
            item['packet_id'] = pid
            item.pop('section_id', None)
            item.pop('source_segments', None)
            corrections.append(item)
        seen = set()
        for entry in review['coverage']:
            if entry.get('packet_id') != pid:
                raise ValueError('Coverage packet mismatch')
            ids = note_audit.normalize_coverage_ids(entry, packet['source_segments'])
            if seen.intersection(ids):
                raise ValueError('Duplicate source coverage')
            seen.update(ids)
            status = entry.get('status')
            if status == 'written':
                section = mapping[_section(entry, sections, titles)]
            elif status == 'omitted' and str(entry.get('reason') or '').strip():
                section = ''
            else:
                raise ValueError('Pending coverage or missing omission reason')
            coverage.append({'packet_id': pid, 'source_ranges': _ranges(ids), 'status': status,
                             'section': section, 'reason': entry.get('reason', '')})
        if seen != set(packet['source_segments']):
            raise ValueError('Uncovered source sentences')
        for req in review.get('visual_requests', []):
            item = copy.deepcopy(req)
            _check_time(item.get('time'), plan['duration'])
            if item.get('status') not in ('pending', 'captured', 'omitted') or not str(item.get('reason') or '').strip():
                raise ValueError('Invalid visual request')
            item['section'] = mapping[_section(item, sections, titles)]
            item['packet_id'] = pid
            item.pop('section_id', None)
            requests.append(item)
        uncertainties.extend(review.get('uncertainties', []))
        boundaries.extend({'packet_id': pid, 'note': note} for note in review.get('boundary_notes', []))
        reviewed.append(review.get('visual_review') == 'complete' and bool(str(review.get('visual_review_reason') or '').strip()))
        visual_reasons.append(str(review.get('visual_review_reason') or ''))
        visual_packets.append({'packet_id': pid, 'reviewed': reviewed[-1],
                               'reason': visual_reasons[-1],
                               'request_times': [r['time'] for r in requests if r['packet_id'] == pid]})
        pieces.append(f'<!-- packet:{pid} -->\n' + numbered)
    header_path = root / 'book_header.md'
    header = header_path.read_text(encoding='utf-8') if header_path.exists() else (
        f'# {plan.get("title") or plan["video_id"]}\n\n'
        f'> **视频来源**：{plan.get("title") or plan["video_id"]}\n'
        f'> **视频地址**：{plan.get("video_url") or "待补"}\n'
        f'> **片长**：约 {timecode.format_hms(plan["duration"])}\n>\n'
        '> 本文按原讲解顺序整理：清理口癖与明确的识别错误，保留讲解细节、例子与推导，关键画面以截图形式嵌入，并附跳转到原视频对应时间点的链接。\n')
    result = {'schema_version': 1, 'video_id': plan['video_id'], 'source_sha256': plan['source_sha256'],
              'coverage': coverage, 'visual_requests': requests,
              'visual_review': 'complete' if all(reviewed) and all(r['status'] != 'pending' for r in requests) else 'pending',
              'visual_review_reason': '\n'.join(r for r in visual_reasons if r), 'uncertainties': uncertainties}
    result['visual_packets'] = visual_packets
    result.update(fact_review='complete' if all(facts_complete) else 'pending',
                  fact_review_reason='\n'.join(reason for reason in fact_reasons if reason),
                  corrections=corrections)
    content = header.rstrip() + '\n\n' + '\n\n'.join(pieces) + '\n'
    # Chapter endings are checked after assembly so a chapter can cross packets.
    editorial = note_editorial.check_editorial(content, result, required=True)
    errors = [e for e in editorial['errors']
              if not (e['code'].startswith('chapter_summary_') and e.get('section') not in strict_chapters)
              and not (e['code'] in ('fact_review_pending', 'fact_review_reason_missing')
                       and not all(facts_complete))]
    if errors:
        raise ValueError('Editorial review failed: ' + ', '.join(sorted({e['code'] for e in errors})))
    # All input checks precede replacing any assembled artifact.
    note_plan._atomic_text(root / 'book.assembled.md', content)
    _save(root / 'note_review.assembled.json', result)
    summary = ['# 合稿交接', '', '只核查边界、歧义与图片需求；不要重写完整正文。',
               f'覆盖 {len(plan["packets"])} 包；视觉状态 {result["visual_review"]}。', '', '## 跨包续接']
    summary.extend(f'- {x["packet_id"]}: {x["note"]}' for x in boundaries)
    summary.extend(['', '## 内容待核', *[f'- {x}' for x in uncertainties]])
    summary.extend(['', '## 事实审阅',
                    f'状态 {result["fact_review"]}；勘误/待核记录 {len(corrections)} 项。',
                    *[f'- {x["packet_id"]} {x.get("status")}: {x.get("original")}'
                      for x in corrections if x.get('status') == 'unresolved']])
    note_plan._atomic_text(root / 'assembly_summary.md', '\n'.join(summary) + '\n')
    return {'packets': len(pieces), 'visual_status': result['visual_review'],
            'pending_images': sum(r['status'] == 'pending' for r in requests),
            'uncertainties': len(uncertainties), 'boundary_notes': len(boundaries)}


def _check_time(value, duration):
    if not isinstance(value, str) or not re.fullmatch(r'\d{2}:\d{2}:\d{2}', value):
        raise ValueError('Expected HH:MM:SS visual time')
    if timecode.parse_seconds(value) >= duration:
        raise ValueError('Visual time outside video')


def _validate_writing(draft, review, metadata, packet, duration, *, closed=False):
    """Validate returned/cached text before materializing it; semantic review remains separate."""
    if not isinstance(draft, str) or not draft.strip() or not isinstance(review, dict):
        raise ValueError('Invalid writing artifacts')
    if (review.get('source_sha256'), review.get('packet_id')) != (metadata['source_sha256'], packet['id']):
        raise ValueError('Writing provenance mismatch')
    sections, titles = _writing_check(lambda: _headings(draft), 'headings', '$.draft_md')
    if any(not sid.startswith(packet['id'] + '.') for sid in sections):
        raise ValueError('Section anchor belongs to another packet')
    seen, mapped = set(), copy.deepcopy(review)
    for number, entry in enumerate(review.get('coverage', [])):
        location = f'$.review.coverage[{number}]'
        if entry.get('packet_id') != packet['id']:
            raise WritingValidationError('coverage_packet', location)
        ids = _writing_check(lambda: note_audit.normalize_coverage_ids(entry, packet['source_segments']),
                             'coverage_ranges', location + '.source_ranges')
        if seen.intersection(ids):
            raise WritingValidationError('coverage_duplicate', location + '.source_ranges')
        seen.update(ids)
        if entry.get('status') == 'written':
            _writing_check(lambda: _section(entry, sections, titles), 'coverage_section', location + '.section_id')
        elif entry.get('status') != 'omitted' or not isinstance(entry.get('reason'), str) or not entry['reason'].strip():
            raise WritingValidationError('coverage_status', location)
    if seen != set(packet['source_segments']):
        raise WritingValidationError('coverage_missing', '$.review.coverage')
    for number, request in enumerate(review.get('visual_requests', [])):
        location = f'$.review.visual_requests[{number}]'
        _writing_check(lambda: _check_time(request.get('time'), duration), 'visual_time', location + '.time')
        _writing_check(lambda: _section(request, sections, titles), 'visual_section', location + '.section_id')
        if request.get('status') not in ('pending', 'omitted') or not str(request.get('reason') or '').strip():
            raise WritingValidationError('visual_status', location)
    if review.get('fact_review') != 'complete' or not isinstance(review.get('fact_review_reason'), str) or not review['fact_review_reason'].strip():
        raise WritingValidationError('fact_review', '$.review.fact_review')
    for number, record in enumerate(mapped.get('corrections', [])):
        location = f'$.review.corrections[{number}]'
        _writing_check(lambda: note_audit.normalize_coverage_ids(record, packet['source_segments']),
                       'correction_ranges', location + '.source_ranges')
        record['section'] = _writing_check(lambda: _section(record, sections, titles),
                                           'correction_section', location + '.section_id')
        if re.fullmatch(r'\s*(?:\*\*)?(?:勘误|待核实)(?:\*\*)?\s*[:：]?\s*', str(record.get('display') or '')):
            raise WritingValidationError('correction_display', location + '.display')
    editorial = note_editorial.check_editorial(draft, mapped, required=True)
    errors = [item for item in editorial['errors'] if closed or item['code'] not in (
        'chapter_summary_missing', 'no_major_chapters')]
    if errors:
        raise WritingValidationError('editorial', '$.draft_md')
    # A missing ending is acceptable only as an explicit cross-packet handoff.
    if not closed and any(item['code'] == 'chapter_summary_missing' for item in editorial['errors']) and not review.get('boundary_notes'):
        raise WritingValidationError('chapter_boundary', '$.review.boundary_notes')
    return editorial


class WritingValidationError(ValueError):
    def __init__(self, code, path):
        allowed = {'headings', 'coverage_packet', 'coverage_ranges', 'coverage_duplicate', 'coverage_section',
                   'coverage_status', 'coverage_missing', 'visual_time', 'visual_section', 'visual_status',
                   'fact_review', 'correction_ranges', 'correction_section', 'correction_display',
                   'editorial', 'chapter_boundary'}
        self.code = code if code in allowed else 'editorial'
        self.path = note_results._safe_diagnostic_path(path)
        super().__init__('Writing validation failed')


def _writing_check(action, code, path):
    try:
        return action()
    except ValueError:
        raise WritingValidationError(code, path) from None


def _publish_writing_text(path, text):
    """Publish a complete new file atomically without replacing an existing name."""
    path = Path(path)
    directory = note_job_lock._directory(path.parent)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', newline='\n',
                prefix=f'.{path.name}.', dir=directory, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        if os.name == 'nt':
            # Windows rename fails if the destination exists. POSIX rename replaces it.
            os.rename(temporary, path)
        else:
            # Linking publishes the completed inode exclusively on POSIX filesystems.
            os.link(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _materialize_writing(target, draft, review, *, recover=False):
    wanted = {'draft.md': draft.encode('utf-8'),
              'review.json': (json.dumps(review, ensure_ascii=False, indent=2) + '\n').encode('utf-8')}
    missing = []
    for name, content in wanted.items():
        path = target / name
        if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()):
            raise ValueError('Writing artifacts cannot be links')
        if path.exists():
            if not recover or not path.is_file() or path.read_bytes() != content:
                raise ValueError('Existing or edited writing artifacts cannot be replaced')
        else:
            missing.append(name)
    # Validate every existing file before completing a partial pair.
    for name in missing:
        path = target / name
        if path.exists() or path.is_symlink():
            raise ValueError('Writing artifact appeared during materialization')
        _publish_writing_text(path, wanted[name].decode('utf-8'))
    if any((target / name).read_bytes() != content for name, content in wanted.items()):
        raise ValueError('Writing artifacts changed during materialization')


def _run_history(state, *, allow_initial=False):
    """Validate attempt ownership before a model call or local artifact recovery.

    Legacy history entries without an ID use the same one-based index as the
    usage reader. A recovered top-level status may differ from its paid record;
    the accounting fields must agree, so that history cannot hide usage.
    """
    if not isinstance(state, dict):
        raise ValueError('Invalid run state')
    current = state.get('attempts')
    if (isinstance(current, bool) or not isinstance(current, int)
            or current < (0 if allow_initial else 1)):
        raise ValueError('Invalid attempt identifier')
    try:
        history = copy.deepcopy(state.get('history'))
    except (TypeError, ValueError, RecursionError):
        raise ValueError('Invalid run history') from None
    if history is None:
        history = []
    if not isinstance(history, list) or any(not isinstance(item, dict) for item in history):
        raise ValueError('Invalid run history')
    seen = set()
    latest = None
    for number, item in enumerate(history, 1):
        identifier = item.get('attempts', number)
        if (isinstance(identifier, bool) or not isinstance(identifier, int) or identifier < 1
                or identifier in seen or identifier > current):
            raise ValueError('Invalid run history attempt')
        seen.add(identifier)
        item['attempts'] = identifier
        if identifier == current:
            latest = item
    if latest is not None:
        counters = ('usage', 'usage_known', 'cost_usd_estimate', 'elapsed_seconds', 'num_turns')
        try:
            matching = note_cache.digest({key: latest.get(key) for key in counters}) == note_cache.digest(
                {key: state.get(key) for key in counters})
        except (TypeError, ValueError, RecursionError):
            raise ValueError('Invalid run history counters') from None
        if not matching:
            raise ValueError('Conflicting run history counters')
    elif current > 0:
        history.append({key: copy.deepcopy(value) for key, value in state.items() if key != 'history'})
    return history


def recover_writing(directory, job, *, attempt=None):
    """Accept a locally repaired candidate after full validation, with no model call."""
    with note_job_lock.claim(_job_target(directory, job)):
        root, plan, target, metadata, packet = _writing_context(directory, job)
        state = _json(target / 'run.json')
        if state.get('status') != 'failed' or state.get('output_mode') != 'structured':
            raise ValueError('Recovery requires a failed structured writing attempt')
        # Reject a damaged ledger before creating any accepted artifacts.
        history = _run_history(state)
        selected = state['attempts'] if attempt is None else attempt
        if selected != state['attempts']:
            raise ValueError('Recover only the latest attempt; review older candidates separately')
        draft, review, manifest = note_candidates.load(target, selected, metadata)
        # Recheck the transport whitelist after any local edit, then all writing evidence.
        note_candidates._check_stage_review(draft, review)
        review = note_results.stamp_review(review, metadata)
        _validate_writing(draft, review, metadata, packet, plan['duration'])
        _, latest_plan = _load_plan(root)
        if latest_plan != plan or _validate_job(root, target, latest_plan) != metadata:
            raise ValueError('Job inputs changed during local recovery')
        edited = manifest['artifacts_sha256'] != note_cache.digest({'draft_md': draft, 'review': review})
        _materialize_writing(target, draft, review, recover=True)
        recovery = {'schema_version': 1, 'status': 'recovered', 'attempt': selected,
                    'candidate_edited': edited, 'metadata_sha256': note_cache.digest(metadata),
                    'draft_sha256': hashlib.sha256(draft.encode('utf-8')).hexdigest(),
                    'review_sha256': note_cache.digest(review)}
        _save(target / 'recovery.json', recovery)
        # Keep the paid failed attempt's history/status unchanged in the usage ledger.
        state['history'] = history
        state['status'] = 'produced'
        state['recovered_locally'] = True
        _save(target / 'run.json', state)
        return {'status': 'recovered', 'packet_id': packet['id'], 'attempt': selected, 'candidate_edited': edited}


def _writing_context(directory, job):
    root, plan = _load_plan(directory)
    if not isinstance(job, str) or not re.fullmatch(r'jobs/P\d{3,}', job):
        raise ValueError('Writing cache requires a writing job')
    target = _inside(root, job)
    metadata = _validate_job(root, target, plan)
    if metadata.get('kind') != 'writing':
        raise ValueError('Writing cache requires a writing job')
    state = target / 'run.json'
    if state.exists():
        record = _json(state)
        if not isinstance(record, dict):
            raise ValueError('Invalid run state')
        if record.get('status') == 'running':
            raise ValueError('Do not cache or reuse a writing job while its model process is running')
        _run_history(record, allow_initial=True)
    packet = next((p for p in plan['packets'] if p['id'] == metadata['packet_id']), None)
    if packet is None or packet['path'] != metadata['packet_path']:
        raise ValueError('Writing packet does not match its plan')
    return root, plan, target, metadata, packet


def _writing_scope(root, plan, target, metadata, packet):
    index = plan['packets'].index(packet)
    neighbors = plan['packets'][max(0, index - 1):index + 2]
    identifiers = packet['source_segments'] + packet['context_before'] + packet['context_after']
    task = (target / 'task.md').read_text(encoding='utf-8-sig')
    # Relocation changes paths, not the instruction content. All other task edits invalidate reuse.
    task = task.replace(str(root), '<video-dir>').replace(str(BASE_DIR), '<project-dir>')
    template = _json(target / 'review.template.json')
    template.pop('source_sha256', None)
    return {'cache_version': note_cache.VERSION, 'video_id': plan['video_id'],
            'title': plan.get('title'), 'video_url': plan.get('video_url'),
            'packet': packet, 'duration': plan['duration'],
            'segment_times': {sid: plan['segment_times'][sid] for sid in identifiers},
            'neighbors': [{'packet_id': p['id'], 'sha256': _sha(_inside(root, p['path']))}
                          for p in neighbors],
            'rules': {k: metadata.get(k) for k in ('packet_sha256', 'sprite_sha256',
                      'terms_sha256', 'style_sha256', 'writer_sha256', 'editorial_policy_version')},
            'task': task, 'review_template': template}


def _job_target(directory, job):
    if not isinstance(job, str) or not re.fullmatch(r'(jobs/P\d{3,}|visual_jobs/V[a-f0-9]{12})', job):
        raise ValueError('Unknown job path')
    root = Path(directory).absolute()
    _inside(root, job)  # Check containment without hiding links from the lock validator.
    return root / job


def cache_writing(directory, job, **kwargs):
    with note_job_lock.claim(_job_target(directory, job)):
        return _cache_writing(directory, job, **kwargs)


def _cache_writing(directory, job, *, cache_dir=None, accept_source_review=False, replace_cache=False):
    if accept_source_review is not True:
        raise ValueError('Cache only after comparing the full draft with its source sentences')
    root, plan, target, metadata, packet = _writing_context(directory, job)
    draft = (target / 'draft.md').read_bytes().decode('utf-8-sig')
    review = _json(target / 'review.json')
    _validate_writing(draft, review, metadata, packet, plan['duration'], closed=True)
    # Only text with pending/omitted frame requests is reusable; captured images have their own cache.
    if re.search(r'!\[[^\]]*\]\((?!SCREENSHOT:)[^)]*\)', draft):
        raise ValueError('Writing cache cannot include materialized images')
    key = note_cache.store(cache_dir or BASE_DIR / 'output/.note_writing_cache',
                           _writing_scope(root, plan, target, metadata, packet),
                           draft, review, accepted=True, replace_accepted=replace_cache)
    return {'status': 'cached', 'packet_id': packet['id'], 'cache_key': key}


def reuse_writing(directory, job, *, cache_dir=None):
    with note_job_lock.claim(_job_target(directory, job)):
        return _reuse_writing(directory, job, cache_dir=cache_dir)


def _reuse_writing(directory, job, *, cache_dir=None):
    root, plan, target, metadata, packet = _writing_context(directory, job)
    record = note_cache.load(cache_dir or BASE_DIR / 'output/.note_writing_cache',
                             _writing_scope(root, plan, target, metadata, packet))
    if record is None:
        return {'status': 'cache_miss', 'packet_id': packet['id']}
    for name in ('draft.md', 'review.json', 'reuse.json'):
        if (target / name).exists() or (target / name).is_symlink():
            raise ValueError('Existing writing artifacts cannot be replaced by cache')
    review = record['review']
    previous_source = review.get('source_sha256')
    review['source_sha256'] = metadata['source_sha256']
    review = note_results.stamp_review(review, metadata)
    _validate_writing(record['draft_md'], review, metadata, packet, plan['duration'], closed=True)
    provenance = {'schema_version': 1, 'status': 'reused', 'packet_id': packet['id'],
                  'cache_key': record['key'], 'source_sha256': metadata['source_sha256'],
                  'previous_source_sha256': previous_source, 'source_review_accepted': True,
                  'draft_sha256': hashlib.sha256(record['draft_md'].encode('utf-8')).hexdigest(),
                  'review_sha256': note_cache.digest(review)}
    _, latest_plan = _load_plan(root)
    if latest_plan != plan or _validate_job(root, target, latest_plan) != metadata:
        raise ValueError('Job inputs changed during writing reuse')
    _materialize_writing(target, record['draft_md'], review)
    _publish_writing_text(target / 'reuse.json', json.dumps(provenance, ensure_ascii=False, indent=2) + '\n')
    return {'status': 'reused', 'packet_id': packet['id'], 'cache_key': record['key']}


def _cache(root, plan):
    path = root / 'visual_confirmations.json'
    empty = {'source_sha256': plan['source_sha256'], 'sprite_sha256': plan.get('sprite_sha256'), 'entries': []}
    if not path.exists():
        return empty
    data = _json(path)
    if (data.get('source_sha256'), data.get('sprite_sha256')) != (empty['source_sha256'], empty['sprite_sha256']):
        return empty
    fresh = []
    for entry in data.get('entries', []):
        image = _inside(root, entry['image'])
        if image.is_file() and _sha(image) == entry.get('image_sha256'):
            fresh.append(entry)
    empty['entries'] = fresh
    return empty


def _assembled_review(root, plan):
    review = _json(root / 'note_review.assembled.json')
    if review.get('source_sha256') != plan['source_sha256'] or review.get('video_id') != plan['video_id']:
        raise ValueError('Assembled review is stale')
    packets = review.get('visual_packets')
    expected = {p['id'] for p in plan['packets']}
    requests = review.get('visual_requests')
    if not isinstance(packets, list) or not isinstance(requests, list):
        raise ValueError('Assembled review needs per-packet visual evidence')
    if (any(not isinstance(p, dict) for p in packets)
            or len(packets) != len(expected)
            or {p.get('packet_id') for p in packets} != expected):
        raise ValueError('Visual evidence must cover every packet exactly once')
    if any(not isinstance(r, dict) or r.get('packet_id') not in expected for r in requests):
        raise ValueError('Unknown packet in assembled visual requests')
    for packet in packets:
        if not isinstance(packet.get('reviewed'), bool):
            raise ValueError('Visual reviewed flag must be boolean')
        times = packet.get('request_times')
        owned_times = [r['time'] for r in requests if r['packet_id'] == packet['packet_id']]
        if not isinstance(times, list) or collections.Counter(times) != collections.Counter(owned_times):
            raise ValueError('Per-packet visual requests do not match assembled requests')
    return review


def _request_sha(requests):
    items = sorted((r.get('requested_reason', r.get('reason', '')), r.get('section', '')) for r in requests)
    return hashlib.sha256(json.dumps(items, ensure_ascii=False).encode('utf-8')).hexdigest()


def _matching_decisions(cache, review):
    requests = review['visual_requests']
    return {e['time']: e for e in cache['entries'] if e.get('status') in ('keep', 'omit')
            and e.get('request_sha256') == _request_sha([r for r in requests if r['time'] == e['time']])}


def prepare_visual(directory, batch_size=4):
    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or not 1 <= batch_size <= 12:
        raise ValueError('Visual batches must contain 1..12 images')
    root, plan = _load_plan(directory)
    review = _assembled_review(root, plan)
    cached = _cache(root, plan)
    fresh = _matching_decisions(cached, review)
    wanted, missing = {}, []
    for request in review['visual_requests']:
        if request.get('confirmation_sha256') and request['time'] not in fresh:
            request['status'] = 'pending'
        if request['status'] != 'pending':
            continue
        ts = request['time']
        _check_time(ts, plan['duration'])
        if ts in fresh:
            continue
        name = 'images/shot_' + ts.replace(':', '_') + '.png'
        image = _inside(root, name)
        if not image.is_file():
            missing.append(ts)
            continue
        entry = wanted.setdefault(ts, {'time': ts, 'image': name, 'image_sha256': _sha(image), 'requests': []})
        entry['requests'].append({'reason': request.get('requested_reason', request['reason']), 'section': request['section']})
    images = list(wanted.values())
    for image in images:
        image['request_sha256'] = _request_sha([r for r in review['visual_requests'] if r['time'] == image['time']])
    tasks = []
    for i in range(0, len(images), batch_size):
        group = images[i:i + batch_size]
        # Input-derived ID keeps completed batches immutable when other requests finish.
        digest = hashlib.sha256(json.dumps(group, sort_keys=True).encode()).hexdigest()[:12]
        relative = f'visual_jobs/V{digest}'
        metadata = {'schema_version': 1, 'kind': 'visual', 'source_sha256': plan['source_sha256'],
                    'sprite_sha256': plan.get('sprite_sha256'), 'images': group}
        task = ('# 独立图片核查\n\n只读取下列相关帧，不读取其他图片、整本书或全片字幕，不派生其他 agent。\n'
                '检查预期内容是否可见、文字是否清晰、是否新增教学信息。必要时比较本批相关状态；不要把细微代码变化当重复。\n'
                '写本目录 confirmation.json：source_sha256 与输入一致，images 每项保留 time/image_sha256，'
                '填写 status=keep|omit|uncertain、description（仅可见事实）、reason、uncertainties。不确定时用 uncertain。\n'
                '这里只决定配图，不改正文或其他任务文件。最终只回传文件路径、异常摘要。\n\n'
                + json.dumps({'source_sha256': plan['source_sha256'], 'images': [dict(e, image=str(root / e['image'])) for e in group]},
                             ensure_ascii=False, indent=2) + '\n')
        tasks.append(_write_job(root, relative, metadata, task))
    result = {'source_sha256': plan['source_sha256'], 'tasks': tasks, 'missing_images': sorted(set(missing)),
              'reusable_confirmations': len(fresh), 'pending_candidates': len(images)}
    if any(r['status'] == 'pending' for r in review['visual_requests']):
        review['visual_review'] = 'pending'
    _save(root / 'visual_jobs.json', result)
    _save(root / 'note_review.assembled.json', review)
    return result


def apply_visual(directory):
    root, plan = _load_plan(directory)
    review = _assembled_review(root, plan)
    cache = _cache(root, plan)
    by_time = {e['time']: e for e in cache['entries']}
    manifest = _json(root / 'visual_jobs.json') if (root / 'visual_jobs.json').exists() else {'tasks': []}
    for task in manifest['tasks']:
        job = _inside(root, task).parent
        metadata = _validate_job(root, job, plan)
        output = job / 'confirmation.json'
        if not output.is_file():
            continue
        result = _json(output)
        if result.get('source_sha256') != plan['source_sha256']:
            raise ValueError('Visual confirmation source mismatch')
        expected = {e['time']: e for e in metadata['images']}
        returned = result.get('images')
        if not isinstance(returned, list) or {e['time'] for e in returned} != set(expected) or len(returned) != len(expected):
            raise ValueError('Confirmation must cover each assigned image exactly once')
        for entry in returned:
            source = expected[entry['time']]
            if entry.get('image_sha256') != source['image_sha256'] or _sha(_inside(root, source['image'])) != source['image_sha256']:
                raise ValueError('Image changed after visual confirmation')
            if entry.get('status') not in ('keep', 'omit', 'uncertain') or not str(entry.get('reason') or '').strip():
                raise ValueError('Invalid visual decision')
            if entry['status'] == 'keep' and not str(entry.get('description') or '').strip():
                raise ValueError('Kept images need a verified description')
            by_time[entry['time']] = dict(entry, image=source['image'], request_sha256=source['request_sha256'])
    text = (root / 'book.assembled.md').read_text(encoding='utf-8')
    complete = not any(not p['reviewed'] and not p['request_times'] for p in review.get('visual_packets', []))
    for request in review['visual_requests']:
        ts = request['time']
        # A captured decision is valid only while its image/source confirmation remains valid.
        decision = by_time.get(ts)
        if decision and decision.get('request_sha256') != _request_sha([r for r in review['visual_requests'] if r['time'] == ts]):
            decision = None
        if request['status'] == 'omitted' and not decision and not request.get('confirmation_sha256'):
            continue
        if not decision or decision['status'] == 'uncertain':
            request['status'] = 'pending'
            complete = False
            continue
        request['status'] = 'captured' if decision['status'] == 'keep' else 'omitted'
        request.setdefault('requested_reason', request['reason'])
        request['reason'] = decision['reason']
        request['confirmation_sha256'] = decision['image_sha256']
        pattern = (r'!\[[^\]\n]*\]\((?:SCREENSHOT:' + re.escape(ts) + r'|images/shot_'
                   + re.escape(ts.replace(':', '_')) + r'\.png)\)|<!-- visual-omitted:' + re.escape(ts) + r' -->')
        if decision['status'] == 'keep':
            caption = decision['description'].replace('[', '（').replace(']', '）').replace('\n', ' ')
            replacement = f'![{caption}]({decision["image"]})'
            if not re.search(pattern, text):
                raise ValueError('Kept visual request has no image location in the draft')
            text = re.sub(pattern, lambda _: replacement, text)
        else:
            text = re.sub(pattern, f'<!-- visual-omitted:{ts} -->', text)
        for item in decision.get('uncertainties', []):
            value = f'{ts}: {item}'
            if value not in review['uncertainties']:
                review['uncertainties'].append(value)
    # No requests is not evidence that visual review was performed.
    if review['visual_requests'] and (by_time or review['visual_review'] == 'complete'):
        review['visual_review'] = 'complete' if complete else 'pending'
        review['visual_review_reason'] = 'Checked requested frames using source and image SHA; pending/uncertain frames remain unresolved.'
    cache['entries'] = list(by_time.values())
    _save(root / 'visual_confirmations.json', cache)
    _save(root / 'note_review.assembled.json', review)
    note_plan._atomic_text(root / 'book.assembled.md', text)
    return {'visual_status': review['visual_review'], 'cached': len(cache['entries']),
            'pending': sum(r['status'] == 'pending' for r in review['visual_requests'])}


def _validate_job(root, job, plan):
    metadata = _json(job / 'job.json')
    if (metadata.get('source_sha256'), metadata.get('sprite_sha256')) != (plan['source_sha256'], plan.get('sprite_sha256')):
        raise ValueError('Job source or visual index is stale')
    if _sha(job / 'task.md') != metadata.get('task_sha256'):
        raise ValueError('Job instructions changed; prepare a separate experiment')
    if metadata['kind'] == 'writing':
        packet = next((p for p in plan['packets'] if p['id'] == metadata.get('packet_id')), None)
        if (job.name != metadata.get('packet_id') or packet is None
                or packet['path'] != metadata.get('packet_path')):
            raise ValueError('Writing job does not match its plan')
        if _sha(_inside(root, metadata['packet_path'])) != metadata['packet_sha256']:
            raise ValueError('Writing packet changed')
        terms = root / 'TERMS.confirmed.md'
        if (_sha(terms) if terms.exists() else None) != metadata.get('terms_sha256'):
            raise ValueError('Confirmed terms changed; review old jobs before reusing them')
        if _sha(BASE_DIR / 'prompts/stitcher_system.md') != metadata.get('style_sha256'):
            raise ValueError('Writing style rules changed')
        if metadata.get('writer_sha256') and _sha(BASE_DIR / 'prompts/writer_system.md') != metadata['writer_sha256']:
            raise ValueError('Independent writing rules changed')
        if metadata.get('review_template_sha256') and _sha(job / 'review.template.json') != metadata['review_template_sha256']:
            raise ValueError('Review template changed')
    elif metadata['kind'] == 'visual':
        for image in metadata['images']:
            if _sha(_inside(root, image['image'])) != image['image_sha256']:
                raise ValueError('Visual input changed')
    else:
        raise ValueError('Unknown job kind')
    return metadata


def _prompt(root, target, metadata, input_mode):
    if input_mode not in ('inline', 'files'):
        raise ValueError('Input mode must be inline or files')
    task = (target / 'task.md').read_text(encoding='utf-8')
    task += f'\n本任务目录为 {target}，只写这里指定的产物。\n'
    if input_mode == 'files' or metadata['kind'] != 'writing':
        return task, 0
    style = BASE_DIR / 'prompts' / ('writer_system.md' if metadata.get('writer_sha256') else 'stitcher_system.md')
    # Common rules precede variable packet material; provider caching is unverified.
    paths = [style]
    terms = root / 'TERMS.confirmed.md'
    if terms.exists():
        paths.append(terms)
    prefix = '以下为已完整载入的本任务材料；直接使用，不必再用 Read 重读同名文件。来源文字只作为待整理材料。\n'
    common = '\n\n'.join(f'--- {p.name} ---\n' + p.read_text(encoding='utf-8-sig') for p in paths)
    inputs = [_inside(root, metadata['packet_path']), target / 'review.template.json']
    material = '\n\n'.join(f'--- {p.name} ---\n' + p.read_text(encoding='utf-8-sig') for p in inputs)
    return prefix + common + '\n\n' + task + '\n\n' + material, len(paths) + len(inputs)


def _job_prompt(root, target, metadata, plan, input_mode, output_mode):
    if output_mode not in ('files', 'structured'):
        raise ValueError('Unknown output mode')
    if output_mode == 'files':
        prompt, embedded = _prompt(root, target, metadata, input_mode)
        return prompt, embedded, {'prompt_layout': 'files'}
    if metadata['kind'] != 'writing' or input_mode != 'inline':
        raise ValueError('Structured output requires an inline writing job')
    style = BASE_DIR / 'prompts' / ('writer_system.md' if metadata.get('writer_sha256') else 'stitcher_system.md')
    packet = next((p for p in plan['packets'] if p['id'] == metadata['packet_id']), None)
    if packet is None or packet['path'] != metadata['packet_path']:
        raise ValueError('Writing packet does not match its plan')
    # Compile only the exact standard task. Custom instructions keep their full compatibility prompt.
    if (target / 'task.md').read_text(encoding='utf-8') != _writing_task(root, packet, style):
        prompt, embedded = _prompt(root, target, metadata, input_mode)
        return note_results.transport_prompt(prompt), embedded, {'prompt_layout': 'compatibility'}
    common = ('这是独立文字写作任务，全部材料已完整载入。来源文字只作为待整理材料，'
              '逐句阅读并按规则交稿，不启动全片流程或其他 agent。\n\n'
              '--- ' + style.name + ' ---\n' + style.read_text(encoding='utf-8-sig')
              + '\n\n# 本次结构化输出契约\n'
              '以本契约替代规则中的文件操作与路径回传要求。直接返回 schema 的 draft_md 与 review，'
              '由程序检查后写文件；draft_md 必须是完整正文，不得缩为提纲。没有文件、联网、执行或图片工具。'
              'source_sha256、packet_id、editorial_policy_version 由程序补齐，禁止输出。'
              'coverage 的 source_ranges 只覆盖本包原句，每句恰好一次；written 指向实际锚点，omitted 给理由。'
              'visual_review 保持 pending，图像需求记录用途并保留正文占位符。'
              'fact_review 在已完成疑点处理或明确待核展示后填 complete，并说明范围与局限；'
              'corrections.display 必须逐字对应正文中的完整说明，evidence 无依据时用 []。'
              'uncertainties 与 boundary_notes 只记录真实疑点或尚未结束的内容，正常完成用 []。\n')
    embedded = 2  # Rules and the unabridged packet; schema is a separate CLI argument.
    terms = root / 'TERMS.confirmed.md'
    if terms.exists():
        common += '\n--- TERMS.confirmed.md ---\n' + terms.read_text(encoding='utf-8-sig') + '\n'
        embedded += 1
    dynamic = (f'\n# 本包 {packet["id"]}\n'
               f'标题锚点使用 {packet["id"]}.s01 等本包 ID。只写所属原句，边界上下文用于理解。\n'
               '所属原句范围（包含两端）：' + json.dumps(_ranges(packet['source_segments']), separators=(',', ':'))
               + '\n\n--- 本包完整材料 ---\n'
               + _inside(root, metadata['packet_path']).read_text(encoding='utf-8-sig'))
    return common + dynamic, embedded, {
        'prompt_layout': 'compiled', 'shared_prefix_chars': len(common),
        'shared_prefix_sha256': hashlib.sha256(common.encode('utf-8')).hexdigest()}


def inspect_job(directory, job, *, input_mode='inline', output_mode='files'):
    root, plan = _load_plan(directory)
    if not re.fullmatch(r'(jobs/P\d{3,}|visual_jobs/V[a-f0-9]{12})', job):
        raise ValueError('Unknown job path')
    target = _inside(root, job)
    metadata = _validate_job(root, target, plan)
    prompt, embedded, metrics = _job_prompt(root, target, metadata, plan, input_mode, output_mode)
    return {'kind': metadata['kind'], 'input_mode': input_mode,
            'output_mode': output_mode, 'prompt_chars': len(prompt), 'embedded_inputs': embedded,
            'schema_chars': len(note_results.schema_argument()) if output_mode == 'structured' else 0, **metrics}


def run_one(directory, job, **kwargs):
    with note_job_lock.claim(_job_target(directory, job)):
        return _run_one(directory, job, **kwargs)


def _run_one(directory, job, *, max_turns=8, max_attempts=2, input_mode='inline', output_mode='files',
            reuse_cache=False, cache_dir=None, runner=None):
    if (isinstance(max_turns, bool) or not isinstance(max_turns, int) or not 1 <= max_turns <= 30
            or isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or not 1 <= max_attempts <= 5):
        raise ValueError('Invalid turn/attempt limit')
    root, plan = _load_plan(directory)
    if not re.fullmatch(r'(jobs/P\d{3,}|visual_jobs/V[a-f0-9]{12})', job):
        raise ValueError('Unknown job path')
    target = _inside(root, job)
    metadata = _validate_job(root, target, plan)
    if output_mode not in ('files', 'structured') or (output_mode == 'structured' and (
            metadata['kind'] != 'writing' or input_mode != 'inline')):
        raise ValueError('Structured output requires an inline writing job')
    state_path = target / 'run.json'
    state = _json(state_path) if state_path.exists() else {'attempts': 0}
    if not isinstance(state, dict):
        raise ValueError('Invalid run state')
    if state.get('status') == 'running':
        raise ValueError('Previous job is still running or interrupted; review it before any new model call')
    if state.get('status') == 'produced':
        raise ValueError('Job already produced output; validate it before requesting another run')
    history = _run_history(state, allow_initial=True)
    if reuse_cache:
        result = _reuse_writing(directory, job, cache_dir=cache_dir)
        if result['status'] == 'reused':
            return result
    if state['attempts'] >= max_attempts:
        raise ValueError('Job attempt budget exhausted')
    outputs = ('draft.md', 'review.json') if metadata['kind'] == 'writing' else ('confirmation.json',)
    if any((target / name).exists() or (target / name).is_symlink() for name in outputs):
        raise ValueError('Existing outputs must be reviewed or moved before rerunning this job')
    executable = shutil.which('claude') or 'claude'
    args = [executable, '-p', '--output-format', 'json', '--max-turns', str(max_turns),
            '--no-session-persistence', '--tools', 'Read,Write,Edit', '--disallowedTools', 'mcp__*',
            '--permission-mode', 'dontAsk', '--allowedTools', 'Read']
    for name in outputs:
        args.extend([f'Write(./{name})', f'Edit(./{name})'])
    prompt, embedded, metrics = _job_prompt(root, target, metadata, plan, input_mode, output_mode)
    if output_mode == 'structured':
        args = [executable, '-p', '--output-format', 'json', '--max-turns', str(max_turns),
                '--no-session-persistence', '--tools', '', '--disallowedTools', 'mcp__*',
                '--permission-mode', 'dontAsk', '--json-schema', note_results.schema_argument()]
    environment = os.environ.copy()
    # A fresh print process is deliberately independent of the controlling CC session.
    environment.pop('CLAUDECODE', None)
    state = {'attempts': state['attempts'] + 1, 'status': 'running', 'kind': metadata['kind'],
             'usage_known': False, 'input_mode': input_mode,
             'prompt_chars': len(prompt), 'embedded_inputs': embedded,
             'max_turns': max_turns, 'max_attempts': max_attempts, 'output_mode': output_mode,
             'schema_chars': len(note_results.schema_argument()) if output_mode == 'structured' else 0,
             'prompt_sha256': hashlib.sha256(prompt.encode('utf-8')).hexdigest(), **metrics}
    _save(state_path, dict(state, history=history))
    started = time.perf_counter()
    try:
        process = (runner or subprocess.run)(args, input=prompt, capture_output=True,
                    encoding='utf-8', cwd=str(target), timeout=900, env=environment)
        try:
            response = json.loads(process.stdout)
        except ValueError:
            response = None
        if isinstance(response, dict):
            state['usage'] = _numeric_usage(response.get('usage'))
            state['usage_known'] = bool(state['usage'])
            state['cost_usd_estimate'] = _number_or_none(response.get('total_cost_usd'))
            state['num_turns'] = _number_or_none(response.get('num_turns'))
        if process.returncode != 0:
            state['failure_code'] = 'process_exit'
            raise ValueError(f'CC process exited with code {process.returncode}')
        if not isinstance(response, dict) or response.get('is_error'):
            subtype = response.get('subtype') if isinstance(response, dict) else None
            state['failure_code'] = {'error_max_turns': 'turn_limit', 'error_max_budget_usd': 'cost_limit',
                                     'error_during_execution': 'model_error'}.get(
                                         subtype, 'model_error' if isinstance(response, dict) else 'invalid_response')
            raise ValueError('CC reported an error or invalid response')
        state['failure_code'] = 'inputs_changed'
        _, latest_plan = _load_plan(root)
        if latest_plan != plan or _validate_job(root, target, latest_plan) != metadata:
            raise ValueError('Job inputs changed during model execution')
        if output_mode == 'structured':
            state['failure_code'] = 'invalid_structured_output'
            draft, review = note_results.decode(response, metadata)
            state['failure_code'] = 'candidate_storage'
            state['candidate'] = note_candidates.stage(target, state['attempts'], metadata, draft, review)
            state['failure_code'] = 'invalid_structured_output'
            packet = next(p for p in plan['packets'] if p['id'] == metadata['packet_id'])
            _validate_writing(draft, review, metadata, packet, plan['duration'])
            state['failure_code'] = 'artifact_write'
            _materialize_writing(target, draft, review)
        state.pop('failure_code', None)
        for name in outputs:
            if not (target / name).is_file() or not (target / name).stat().st_size:
                state['failure_code'] = 'missing_artifacts'
                raise ValueError('CC did not produce the required artifacts')
        state['status'] = 'produced'
        # Do not record the result text, tool output, stderr, or credentials.
        state['limit'] = 'Produced is not validated; provider billing and coverage require separate checks.'
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as exc:
        state['status'] = 'failed'
        state['failure_type'] = type(exc).__name__
        if isinstance(exc, (note_results.ResultValidationError, WritingValidationError)):
            state['validation'] = {'code': exc.code, 'path': exc.path}
        state.setdefault('failure_code', 'timeout' if isinstance(exc, subprocess.TimeoutExpired) else 'runner_error')
        state['elapsed_seconds'] = time.perf_counter() - started
        state['history'] = history + [copy.deepcopy(state)]
        _save(state_path, state)
        raise ValueError('CC job failed; see run.json counts/status, not raw logs') from None
    state['elapsed_seconds'] = time.perf_counter() - started
    state['history'] = history + [copy.deepcopy(state)]
    _save(state_path, state)
    return state


def _number_or_none(value):
    return value if (isinstance(value, (int, float)) and not isinstance(value, bool)
                     and math.isfinite(value) and value >= 0) else None


def _numeric_usage(usage):
    if not isinstance(usage, dict):
        return {}
    counters = {'input_tokens', 'output_tokens', 'cache_creation_input_tokens',
                'cache_read_input_tokens', 'prompt_tokens', 'completion_tokens',
                'total_tokens', 'prompt_cache_hit_tokens', 'prompt_cache_miss_tokens',
                'cached_tokens', 'reasoning_tokens', 'thinking_tokens',
                'ephemeral_5m_input_tokens', 'ephemeral_1h_input_tokens'}
    details = {'cache_creation', 'input_tokens_details', 'prompt_tokens_details',
               'output_tokens_details', 'completion_tokens_details'}
    result = {}
    for key, value in usage.items():
        if key in counters and _number_or_none(value) is not None:
            result[key] = value
        elif key in details and isinstance(value, dict):
            result[key] = _numeric_usage(value)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('prepare', 'assemble', 'visual', 'apply-visual', 'inspect', 'run-one',
                                            'cache-writing', 'reuse-writing', 'recover-writing'))
    parser.add_argument('video_id')
    parser.add_argument('--out', type=Path)
    parser.add_argument('--job', help='jobs/P001 or visual_jobs/V<hash>')
    parser.add_argument('--batch-size', type=int, default=4)
    parser.add_argument('--max-turns', type=int, default=8)
    parser.add_argument('--max-attempts', type=int, default=2)
    parser.add_argument('--input-mode', choices=('inline', 'files'), default='inline')
    parser.add_argument('--output-mode', choices=('files', 'structured'), default='files')
    parser.add_argument('--cache-dir', type=Path)
    parser.add_argument('--reuse-cache', action='store_true')
    parser.add_argument('--replace-cache', action='store_true', help='Replace an accepted cache after reviewing a corrected draft')
    parser.add_argument('--attempt', type=int, help='Latest structured attempt to recover locally')
    parser.add_argument('--accept-source-review', action='store_true',
                        help='Controller confirms comparison of this draft with every owned source sentence')
    args = parser.parse_args(argv)
    if not re.fullmatch(r'[A-Za-z0-9_-]+', args.video_id):
        parser.error('Invalid video ID')
    directory = args.out or BASE_DIR / 'output' / args.video_id
    try:
        functions = {'prepare': prepare, 'assemble': assemble, 'apply-visual': apply_visual}
        if args.command in functions:
            result = functions[args.command](directory)
        elif args.command == 'visual':
            result = prepare_visual(directory, args.batch_size)
        elif args.job and args.command == 'inspect':
            result = inspect_job(directory, args.job, input_mode=args.input_mode, output_mode=args.output_mode)
        elif args.job and args.command == 'cache-writing':
            result = cache_writing(directory, args.job, cache_dir=args.cache_dir,
                                   accept_source_review=args.accept_source_review, replace_cache=args.replace_cache)
        elif args.job and args.command == 'reuse-writing':
            result = reuse_writing(directory, args.job, cache_dir=args.cache_dir)
        elif args.job and args.command == 'recover-writing':
            result = recover_writing(directory, args.job, attempt=args.attempt)
        elif args.job:
            result = run_one(directory, args.job, max_turns=args.max_turns, max_attempts=args.max_attempts,
                             input_mode=args.input_mode, output_mode=args.output_mode,
                             reuse_cache=args.reuse_cache, cache_dir=args.cache_dir)
            # Model invocation remains limited to run-one; reuse-writing never invokes it.
        else:
            parser.error('run-one requires --job; this command invokes your configured model')
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f'note_workflow: {exc}', file=sys.stderr)
        return 1
    # Keep task lists/full review in files, return only counts to the controlling agent.
    print(json.dumps({k: len(v) if isinstance(v, list) else v for k, v in result.items()
                      if k not in ('usage', 'source_sha256', 'limit', 'rule')}, ensure_ascii=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
