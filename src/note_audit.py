"""Local note checks. Structural evidence does not prove semantic completeness.

python src/note_audit.py <video_id> [--out VIDEO_DIR] [--require-review]
Never edits the book or removes images. Similarity findings need human/agent review.
"""
import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import re
import sys
from urllib.parse import unquote, urlsplit

import timecode

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass

BASE_DIR = Path(__file__).resolve().parents[1]
IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
SHOT_RE = re.compile(r"shot_(\d{2}_\d{2}_\d{2})\.png$")


def _atomic_json(path, data):
    tmp = path.with_suffix(path.suffix + '.partial')
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def _fingerprint(path):
    """Decoded pixels and a low resolution comparison; no OCR or paid vision API."""
    import av
    import numpy as np
    with av.open(str(path)) as container:
        frame = next(container.decode(video=0))
        full = frame.to_ndarray(format='rgb24')
        small = frame.reformat(width=160, height=90, format='rgb24').to_ndarray()
    # Exclude common bottom subtitles for similarity warnings, never for exact equality.
    return (hashlib.sha256(full.shape.__repr__().encode() + full.tobytes()).hexdigest(),
            small[:78].astype(np.int16))


def audit_directory(vdir, require_review=False, check_similarity=True):
    vdir = Path(vdir).resolve()
    errors, warnings = [], []
    report = {'schema_version': 1, 'video_dir': str(vdir), 'errors': errors,
              'warnings': warnings, 'similar_images': [], 'review_checked': False,
              'limits': ['Checks file and review consistency; cannot prove that all meaningful images, examples or explanations are present.']}

    def issue(dest, code, **details):
        dest.append({'code': code, **details})

    book_path = vdir / 'book.md'
    if not book_path.is_file():
        issue(errors, 'missing_book')
        report['status'] = 'failed'
        return report
    content = book_path.read_text(encoding='utf-8')
    if not content.strip():
        issue(errors, 'empty_book')
    if 'SCREENSHOT:' in content:
        issue(errors, 'unresolved_screenshot')
    headings = set(re.findall(r'^#{1,6}\s+(.+?)\s*#*\s*$', content, re.M))
    anchors = re.findall(r'参考时间[:：]\s*([^\s)*]+)', content)
    refs = IMAGE_RE.findall(content)
    report['image_references'] = len(refs)
    local_images = {}
    times = set()
    duration = None
    transcript_path = vdir / 'transcript.json'
    if transcript_path.is_file():
        try:
            transcript = json.loads(transcript_path.read_text(encoding='utf-8'))
            duration = float(transcript.get('duration') or 0) or None
        except (ValueError, TypeError, AttributeError):
            issue(errors, 'invalid_transcript')
    for anchor in anchors:
        if not re.fullmatch(r'\d{2}:\d{2}:\d{2}', anchor):
            issue(errors, 'ambiguous_reference_time', time=anchor)
            continue
        try:
            sec = timecode.parse_seconds(anchor)
            if duration and sec >= duration:
                issue(errors, 'reference_time_out_of_range', time=anchor)
        except ValueError:
            issue(errors, 'invalid_reference_time', time=anchor)
    counts = Counter(url for _, url in refs)
    for url, count in counts.items():
        if count > 1:
            issue(warnings, 'repeated_reference', path=url, count=count)
    for caption, url in refs:
        parsed = urlsplit(url)
        if parsed.scheme in ('http', 'https'):
            issue(warnings, 'remote_image_not_checked', path=url)
            continue
        if parsed.scheme or parsed.netloc:
            issue(errors, 'unsupported_image_path', path=url)
            continue
        relative = unquote(parsed.path).strip('<>')
        path = (vdir / relative).resolve()
        if not path.is_relative_to(vdir):
            issue(errors, 'image_outside_video_dir', path=url)
            continue
        if not path.is_file():
            issue(errors, 'missing_image', path=url)
            continue
        local_images[path] = url
        if not caption.strip():
            issue(warnings, 'empty_caption', path=url)
        shot = SHOT_RE.search(path.name)
        if shot:
            ts = shot.group(1).replace('_', ':')
            try:
                sec = timecode.parse_seconds(ts)
                times.add(ts)
                if duration and sec >= duration:
                    issue(errors, 'image_time_out_of_range', time=ts)
            except ValueError:
                issue(errors, 'invalid_image_time', time=ts)
    report['unique_local_images'] = len(local_images)
    byte_groups = defaultdict(list)
    fingerprints = []
    for path, url in local_images.items():
        data = path.read_bytes()
        if not data:
            issue(errors, 'empty_image', path=url)
            continue
        byte_groups[hashlib.sha256(data).hexdigest()].append(url)
        if check_similarity:
            try:
                pixel_hash, small = _fingerprint(path)
                fingerprints.append((url, pixel_hash, small))
            except ImportError:
                issue(warnings, 'image_decode_dependencies_missing')
                check_similarity = False
            except Exception as exc:
                issue(errors, 'unreadable_image', path=url, reason=type(exc).__name__)
    for group in byte_groups.values():
        if len(group) > 1:
            issue(warnings, 'identical_image_bytes', paths=group)
    for i, (url, pixel_hash, small) in enumerate(fingerprints):
        for other, other_hash, other_small in fingerprints[:i]:
            diff = abs(small - other_small).mean(axis=2)
            changed = float((diff > 25).mean())
            mae = float(diff.mean())
            if pixel_hash == other_hash or (changed <= 0.015 and mae <= 4):
                report['similar_images'].append({'paths': [other, url],
                    'exact_pixels': pixel_hash == other_hash,
                    'changed_ratio': round(changed, 5), 'mean_difference': round(mae, 3)})
    if report['similar_images']:
        issue(warnings, 'similar_images_need_review', pairs=len(report['similar_images']),
              reason='Small text/formula changes can be meaningful; do not remove automatically.')

    plan_path, review_path = vdir / 'note_plan.json', vdir / 'note_review.json'
    if not plan_path.is_file() or not review_path.is_file():
        issue(errors if require_review else warnings, 'missing_plan_or_review')
    else:
        try:
            plan = json.loads(plan_path.read_text(encoding='utf-8'))
            review = json.loads(review_path.read_text(encoding='utf-8'))
            _check_review(plan, review, transcript_path, headings, times, duration, errors)
            sprite_path = vdir / 'sprite_index.json'
            current_sprite_hash = hashlib.sha256(sprite_path.read_bytes()).hexdigest() if sprite_path.is_file() else None
            if plan.get('sprite_sha256') != current_sprite_hash:
                issue(errors, 'stale_visual_plan')
            if review.get('uncertainties'):
                issue(warnings, 'unresolved_content_uncertainties', items=review['uncertainties'])
            report['review_checked'] = True
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            issue(errors, 'invalid_plan_or_review', reason=type(exc).__name__)
    report['status'] = 'failed' if errors else ('needs_review' if warnings else 'checks_passed')
    return report


def _check_review(plan, review, transcript_path, headings, times, duration, errors):
    def fail(code, **details):
        errors.append({'code': code, **details})

    if plan.get('schema_version') != 1 or review.get('schema_version') != 1:
        fail('unsupported_review_schema')
    if not transcript_path.is_file():
        fail('missing_transcript_for_review')
    else:
        source_hash = hashlib.sha256(transcript_path.read_bytes()).hexdigest()
        if plan.get('source_sha256') != source_hash or review.get('source_sha256') != source_hash:
            fail('stale_plan_or_review')
    if review.get('video_id') != plan.get('video_id'):
        fail('review_video_mismatch')
    if review.get('visual_review') != 'complete':
        fail('visual_review_pending')
    if not str(review.get('visual_review_reason') or '').strip():
        fail('visual_review_reason_missing')
    packets = plan['packets']
    expected = {p['id']: p['source_segments'] for p in packets}
    all_segments = [s for p in packets for s in p['source_segments']]
    if (len(all_segments) != plan['segment_count'] or len(set(all_segments)) != len(all_segments)
            or set(all_segments) != {f'S{i:06d}' for i in range(1, plan['segment_count'] + 1)}):
        fail('plan_segment_coverage_invalid')
    seen = defaultdict(set)
    for entry in review['coverage']:
        packet_id = entry['packet_id']
        if packet_id not in expected:
            fail('unknown_packet', packet_id=packet_id)
            continue
        covered = entry.get('source_segments')
        if not isinstance(covered, list) or not covered or any(not isinstance(s, str) for s in covered):
            fail('review_segment_mismatch', packet_id=packet_id)
            continue
        if (len(set(covered)) != len(covered) or set(covered) - set(expected[packet_id])
                or seen[packet_id].intersection(covered)):
            fail('review_segment_mismatch', packet_id=packet_id)
        seen[packet_id].update(covered)
        status = entry.get('status')
        if status == 'written':
            section = str(entry.get('section') or '').lstrip('#').strip()
            if not section or section not in headings:
                fail('review_section_missing', packet_id=packet_id, section=section)
        elif status == 'omitted':
            if not str(entry.get('reason') or '').strip():
                fail('omission_reason_missing', packet_id=packet_id)
        else:
            fail('coverage_pending', packet_id=packet_id)
    for packet_id, source_segments in expected.items():
        missing = set(source_segments) - seen[packet_id]
        if missing:
            fail('packet_not_reviewed', packet_id=packet_id, missing_segments=sorted(missing))
    requests = review['visual_requests']
    if not isinstance(requests, list):
        raise ValueError('visual_requests must be a list')
    for req in requests:
        ts, status = req.get('time'), req.get('status')
        if not isinstance(ts, str) or not re.fullmatch(r'\d{2}:\d{2}:\d{2}', ts):
            fail('visual_time_invalid', time=ts)
            continue
        try:
            sec = timecode.parse_seconds(ts)
        except ValueError:
            fail('visual_time_invalid', time=ts)
            continue
        if duration and sec >= duration:
            fail('visual_time_out_of_range', time=ts)
        if status not in ('captured', 'omitted'):
            fail('visual_request_pending', time=ts)
        elif status == 'captured' and ts not in times:
            fail('requested_image_not_in_book', time=ts)
        if not str(req.get('reason') or '').strip():
            fail('visual_reason_missing', time=ts)
        if status == 'captured':
            section = str(req.get('section') or '').lstrip('#').strip()
            if section not in headings:
                fail('visual_section_missing', time=ts)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('video_id')
    parser.add_argument('--out', type=Path, help='Existing video directory to inspect')
    parser.add_argument('--require-review', action='store_true')
    parser.add_argument('--no-similarity', action='store_true', help='Skip decoded image comparison')
    parser.add_argument('--report', type=Path, help='Save JSON here instead of VIDEO_DIR/note_audit.json')
    args = parser.parse_args(argv)
    if not re.fullmatch(r'[A-Za-z0-9_-]+', args.video_id):
        parser.error('video_id must contain only letters, digits, underscores and hyphens')
    vdir = args.out or BASE_DIR / 'output' / args.video_id
    try:
        report = audit_directory(vdir, args.require_review, not args.no_similarity)
        destination = args.report or vdir / 'note_audit.json'
        destination.parent.mkdir(parents=True, exist_ok=True)
        _atomic_json(destination, report)
    except (OSError, UnicodeError, ValueError) as exc:
        print(f'Audit failed: {exc}', file=sys.stderr)
        return 1
    print(f"{report['status']}: errors={len(report['errors'])}, warnings={len(report['warnings'])}, similar_pairs={len(report['similar_images'])}")
    print(destination)
    return 1 if report['errors'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
