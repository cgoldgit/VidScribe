"""Checks evidence gates using isolated notes and synthetic images."""
import hashlib
import json
from pathlib import Path
import tempfile

from _helpers import still_image, write_png
import note_audit


def _fixture(root):
    root = Path(root)
    (root / 'images').mkdir()
    transcript = {'duration': 120, 'segments': [{'start': '00:00:00', 'end': '00:02:00', 'text': 'example'}]}
    source = root / 'transcript.json'
    source.write_text(json.dumps(transcript), encoding='utf-8')
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    (root / 'book.md').write_text('# Course\n\n## Example\n\nExplanation.\n', encoding='utf-8')
    plan = {'schema_version': 1, 'video_id': 'demo', 'source_sha256': digest,
            'segment_count': 1, 'packets': [{'id': 'P001', 'source_segments': ['S000001']}]}
    review = {'schema_version': 1, 'video_id': 'demo', 'source_sha256': digest,
              'coverage': [{'packet_id': 'P001', 'source_segments': ['S000001'],
                            'status': 'written', 'section': 'Example', 'reason': ''}],
              'visual_requests': [], 'visual_review': 'complete',
              'visual_review_reason': 'Read source and verified this example is spoken explanation.'}
    (root / 'note_plan.json').write_text(json.dumps(plan), encoding='utf-8')
    _review(root, review)
    return root, review


def _review(root, review):
    (root / 'note_review.json').write_text(json.dumps(review), encoding='utf-8')


def _codes(result):
    return {e['code'] for e in result['errors']}


def test_complete_structural_review_passes_without_assuming_images_required():
    with tempfile.TemporaryDirectory() as tmp:
        root, _ = _fixture(tmp)
        result = note_audit.audit_directory(root, require_review=True)
        assert result['status'] == 'checks_passed', result
        assert result['limits']


def test_missing_and_unresolved_images_block_delivery():
    with tempfile.TemporaryDirectory() as tmp:
        root, _ = _fixture(tmp)
        (root / 'book.md').write_text('# Course\n## Example\n![missing](images/shot_00_00_15.png)\n![todo](SCREENSHOT:00:00:20)', encoding='utf-8')
        result = note_audit.audit_directory(root)
        assert {'missing_image', 'unresolved_screenshot'} <= _codes(result)


def test_reference_times_use_unambiguous_hms_and_stay_in_range():
    with tempfile.TemporaryDirectory() as tmp:
        root, _ = _fixture(tmp)
        (root / 'book.md').write_text('# Course\n## Example\n*(参考时间: 00:51)*\n*(参考时间: 00:02:05)*', encoding='utf-8')
        assert {'ambiguous_reference_time', 'reference_time_out_of_range'} <= _codes(note_audit.audit_directory(root))


def test_image_paths_must_stay_inside_video_directory():
    with tempfile.TemporaryDirectory() as tmp:
        root, _ = _fixture(tmp)
        (root / 'book.md').write_text('# Course\n## Example\n![x](../outside.png)', encoding='utf-8')
        assert 'image_outside_video_dir' in _codes(note_audit.audit_directory(root))


def test_pending_coverage_and_visual_requests_block_delivery():
    with tempfile.TemporaryDirectory() as tmp:
        root, review = _fixture(tmp)
        review['coverage'][0]['status'] = 'pending'
        review['visual_requests'] = [{'time': '00:00:15', 'status': 'pending', 'reason': 'diagram'}]
        _review(root, review)
        assert {'coverage_pending', 'visual_request_pending'} <= _codes(note_audit.audit_directory(root))


def test_captured_request_requires_materialized_reference_and_existing_section():
    with tempfile.TemporaryDirectory() as tmp:
        root, review = _fixture(tmp)
        review['visual_requests'] = [{'time': '00:00:15', 'status': 'captured', 'reason': 'diagram', 'section': 'Gone'}]
        _review(root, review)
        assert {'requested_image_not_in_book', 'visual_section_missing'} <= _codes(note_audit.audit_directory(root))


def test_source_changes_invalidate_old_review():
    with tempfile.TemporaryDirectory() as tmp:
        root, _ = _fixture(tmp)
        with (root / 'transcript.json').open('a', encoding='utf-8') as f:
            f.write('\n')
        assert 'stale_plan_or_review' in _codes(note_audit.audit_directory(root))


def test_review_cannot_omit_packets_or_substitute_segment_ids():
    with tempfile.TemporaryDirectory() as tmp:
        root, review = _fixture(tmp)
        review['coverage'][0]['source_segments'] = ['S000002']
        _review(root, review)
        assert 'review_segment_mismatch' in _codes(note_audit.audit_directory(root))
        review['coverage'] = []
        _review(root, review)
        assert 'packet_not_reviewed' in _codes(note_audit.audit_directory(root))


def test_omitted_coverage_requires_explanation():
    with tempfile.TemporaryDirectory() as tmp:
        root, review = _fixture(tmp)
        review['coverage'][0]['status'] = 'omitted'
        _review(root, review)
        assert 'omission_reason_missing' in _codes(note_audit.audit_directory(root))
        review['coverage'][0]['reason'] = 'Greeting without course content'
        _review(root, review)
        assert not note_audit.audit_directory(root)['errors']


def test_duplicate_images_are_reported_and_never_removed():
    with tempfile.TemporaryDirectory() as tmp:
        root, _ = _fixture(tmp)
        a, b = root / 'images/shot_00_00_15.png', root / 'images/shot_00_00_30.png'
        write_png(str(a), still_image())
        b.write_bytes(a.read_bytes())
        (root / 'book.md').write_text('# Course\n## Example\n![a](images/shot_00_00_15.png)\n![b](images/shot_00_00_30.png)', encoding='utf-8')
        result = note_audit.audit_directory(root)
        assert not result['errors'], result
        assert result['similar_images'][0]['exact_pixels']
        assert result['status'] == 'needs_review'
        assert a.is_file() and b.is_file()


def test_corrupt_image_fails_decoding():
    with tempfile.TemporaryDirectory() as tmp:
        root, _ = _fixture(tmp)
        (root / 'images/shot_00_00_15.png').write_bytes(b'not an image')
        (root / 'book.md').write_text('# Course\n## Example\n![a](images/shot_00_00_15.png)', encoding='utf-8')
        assert 'unreadable_image' in _codes(note_audit.audit_directory(root))


def test_missing_review_is_a_warning_unless_explicitly_required():
    with tempfile.TemporaryDirectory() as tmp:
        root, _ = _fixture(tmp)
        (root / 'note_review.json').unlink()
        assert not note_audit.audit_directory(root)['errors']
        assert 'missing_plan_or_review' in _codes(note_audit.audit_directory(root, require_review=True))


def test_empty_visual_list_cannot_bypass_visual_review():
    with tempfile.TemporaryDirectory() as tmp:
        root, review = _fixture(tmp)
        review['visual_review'] = 'pending'
        review['visual_review_reason'] = ''
        _review(root, review)
        assert {'visual_review_pending', 'visual_review_reason_missing'} <= _codes(note_audit.audit_directory(root))


def test_packet_can_split_written_and_omitted_but_not_duplicate_segments():
    with tempfile.TemporaryDirectory() as tmp:
        root, review = _fixture(tmp)
        plan = json.loads((root / 'note_plan.json').read_text(encoding='utf-8'))
        plan['segment_count'] = 2
        plan['packets'][0]['source_segments'].append('S000002')
        (root / 'note_plan.json').write_text(json.dumps(plan), encoding='utf-8')
        review['coverage'].append({'packet_id': 'P001', 'source_segments': ['S000002'],
                                   'status': 'omitted', 'reason': 'Repeated filler'})
        _review(root, review)
        assert not note_audit.audit_directory(root)['errors']
        review['coverage'][1]['source_segments'] = ['S000001', 'S000002']
        _review(root, review)
        assert 'review_segment_mismatch' in _codes(note_audit.audit_directory(root))


def test_cli_report_can_be_saved_outside_read_only_sample():
    with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as other:
        root, _ = _fixture(tmp)
        destination = Path(other) / 'report.json'
        assert note_audit.main(['demo', '--out', str(root), '--report', str(destination), '--require-review']) == 0
        assert json.loads(destination.read_text(encoding='utf-8'))['status'] == 'checks_passed'
        assert not (root / 'note_audit.json').exists()


def test_generated_plan_and_review_template_work_with_delivery_gate():
    import note_plan
    with tempfile.TemporaryDirectory() as tmp:
        root, _ = _fixture(tmp)
        (root / 'note_plan.json').unlink()
        note_plan.generate(root, video_id='demo')
        review = json.loads((root / 'note_review.template.json').read_text(encoding='utf-8'))
        _review(root, review)
        assert 'coverage_pending' in _codes(note_audit.audit_directory(root, require_review=True))
        for coverage in review['coverage']:
            coverage.update(status='written', section='Example')
        review.update(visual_review='complete', visual_review_reason='Checked the source, spoken example requires no picture.')
        _review(root, review)
        assert note_audit.audit_directory(root, require_review=True)['status'] == 'checks_passed'
        (root / 'sprite_index.json').write_text('{"sheets": []}', encoding='utf-8')
        assert 'stale_visual_plan' in _codes(note_audit.audit_directory(root, require_review=True))
