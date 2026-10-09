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
    (root / 'book.md').write_text('# Course\n\n## Example\n\nExplanation.\n\n**本章小结**：给定条件下验证示例结果。\n', encoding='utf-8')
    plan = {'schema_version': 1, 'video_id': 'demo', 'source_sha256': digest,
            'segment_count': 1, 'packets': [{'id': 'P001', 'source_segments': ['S000001']}]}
    review = {'schema_version': 1, 'video_id': 'demo', 'source_sha256': digest,
              'coverage': [{'packet_id': 'P001', 'source_segments': ['S000001'],
                            'status': 'written', 'section': 'Example', 'reason': ''}],
              'visual_requests': [], 'visual_review': 'complete',
              'visual_review_reason': 'Read source and verified this example is spoken explanation.',
              'fact_review': 'complete', 'fact_review_reason': 'Checked the example and its stated conditions.',
              'corrections': []}
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


def test_legacy_editorial_gaps_warn_and_strict_gate_blocks_without_changing_sources():
    with tempfile.TemporaryDirectory() as tmp:
        root, review = _fixture(tmp)
        (root / 'book.md').write_text('# Course\n\n## Example\n\nExplanation.\n', encoding='utf-8')
        for key in ('fact_review', 'fact_review_reason', 'corrections'):
            review.pop(key, None)
        _review(root, review)
        originals = {name: (root / name).read_bytes() for name in ('book.md', 'note_review.json')}
        legacy = note_audit.audit_directory(root, require_review=True)
        assert not legacy['errors'] and legacy['status'] == 'needs_review'
        assert {'chapter_summary_missing', 'fact_review_pending'} <= {e['code'] for e in legacy['warnings']}
        strict = note_audit.audit_directory(root, require_review=True, require_editorial=True)
        assert {'chapter_summary_missing', 'fact_review_pending'} <= _codes(strict)
        assert all((root / name).read_bytes() == value for name, value in originals.items())


def test_require_editorial_cli_uses_the_selected_experiment_artifacts():
    with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as other:
        root, review = _fixture(tmp)
        destination = Path(other) / 'editorial.json'
        assert note_audit.main(['demo', '--out', str(root), '--require-review', '--require-editorial',
                               '--report', str(destination)]) == 0
        review['fact_review'] = 'pending'
        _review(root, review)
        assert note_audit.main(['demo', '--out', str(root), '--require-review', '--require-editorial',
                               '--report', str(destination)]) == 1
        assert 'fact_review_pending' in _codes(json.loads(destination.read_text(encoding='utf-8')))


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
        review.update(visual_review='complete', visual_review_reason='Checked the source, spoken example requires no picture.',
                      fact_review='complete', fact_review_reason='Checked the example.', corrections=[])
        _review(root, review)
        assert note_audit.audit_directory(root, require_review=True)['status'] == 'checks_passed'
        (root / 'sprite_index.json').write_text('{"sheets": []}', encoding='utf-8')
        assert 'stale_visual_plan' in _codes(note_audit.audit_directory(root, require_review=True))


def test_normalize_coverage_accepts_inclusive_ranges_and_legacy_lists_without_mutating():
    allowed = ['S000001', 'S000002', 'S000003', 'S000004']
    entry = {'source_ranges': [['S000003', 'S000004'], ['S000001', 'S000002']]}
    original = json.dumps(entry)
    assert note_audit.normalize_coverage_ids(entry, allowed) == [
        'S000003', 'S000004', 'S000001', 'S000002']
    assert json.dumps(entry) == original
    legacy = {'source_segments': ['S000001', 'S000003']}
    assert note_audit.normalize_coverage_ids(legacy, allowed) == legacy['source_segments']
    assert note_audit.normalize_coverage_ids(legacy, allowed) is not legacy['source_segments']
    assert note_audit.normalize_coverage_ids(
        {'source_ranges': [['S999999', 'S1000000']]}, ['S999999', 'S1000000']) == [
            'S999999', 'S1000000']


def test_normalize_coverage_rejects_ambiguous_malformed_reversed_and_overlapping_ranges():
    allowed = ['S000001', 'S000002', 'S000003', 'S000004']
    invalid = [
        {},
        {'source_segments': ['S000001'], 'source_ranges': [['S000001', 'S000001']]},
        {'source_ranges': []},
        {'source_ranges': None},
        {'source_ranges': 'S000001-S000002'},
        {'source_ranges': ['S000001', 'S000002']},
        {'source_ranges': [[1, 2]]},
        {'source_ranges': [['S1', 'S2']]},
        {'source_ranges': [['S000001', 'S000002', 'S000003']]},
        {'source_ranges': [['S000001\n', 'S000002']]},
        {'source_ranges': [['S000003', 'S000002']]},
        {'source_ranges': [['S000001', 'S000002'], ['S000002', 'S000003']]},
        {'source_ranges': [['S000001', 'S000001'], ['S000001', 'S000001']]},
        {'source_segments': ['S000001', 'S000001']},
        {'source_segments': ['S000005']},
        {'source_segments': [1]},
        {'source_segments': []},
    ]
    for entry in invalid:
        try:
            note_audit.normalize_coverage_ids(entry, allowed)
        except ValueError:
            pass
        else:
            raise AssertionError(f'Invalid coverage accepted: {entry}')


def test_range_expansion_is_bounded_before_iteration_and_rejects_packet_holes():
    from unittest.mock import patch
    with patch.object(note_audit, 'range', side_effect=AssertionError('expanded unsafe range'), create=True):
        for entry, allowed in [
            ({'source_ranges': [['S000001', 'S999999999']]}, ['S000001', 'S999999999']),
            ({'source_ranges': [['S000001', 'S' + '9' * 5000]]}, ['S000001']),
            ({'source_ranges': [['S000001', 'S000005']]}, ['S000001', 'S000002']),
        ]:
            try:
                note_audit.normalize_coverage_ids(entry, allowed)
            except ValueError:
                pass
            else:
                raise AssertionError('Unsafe range accepted')
    # The span is short enough but includes an ID owned by another packet.
    try:
        note_audit.normalize_coverage_ids(
            {'source_ranges': [['S000001', 'S000003']]}, ['S000001', 'S000003', 'S000004'])
    except ValueError:
        pass
    else:
        raise AssertionError('Range crossed a hole in the allowed packet IDs')


def test_audit_accepts_mixed_range_and_legacy_coverage_and_detects_cross_entry_overlap():
    with tempfile.TemporaryDirectory() as tmp:
        root, review = _fixture(tmp)
        plan = json.loads((root / 'note_plan.json').read_text(encoding='utf-8'))
        plan['segment_count'] = 4
        plan['packets'][0]['source_segments'] = ['S000001', 'S000002', 'S000003', 'S000004']
        (root / 'note_plan.json').write_text(json.dumps(plan), encoding='utf-8')
        review['coverage'] = [
            {'packet_id': 'P001', 'source_ranges': [['S000001', 'S000002']],
             'status': 'written', 'section': 'Example', 'reason': ''},
            {'packet_id': 'P001', 'source_segments': ['S000003', 'S000004'],
             'status': 'omitted', 'reason': 'Repeated filler'},
        ]
        _review(root, review)
        assert note_audit.audit_directory(root, require_review=True)['status'] == 'checks_passed'
        review['coverage'][1].pop('source_segments')
        review['coverage'][1]['source_ranges'] = [['S000002', 'S000004']]
        _review(root, review)
        assert 'review_segment_mismatch' in _codes(note_audit.audit_directory(root))


def test_audit_range_coverage_rejects_missing_ids_and_cross_packet_intervals():
    with tempfile.TemporaryDirectory() as tmp:
        root, review = _fixture(tmp)
        plan = json.loads((root / 'note_plan.json').read_text(encoding='utf-8'))
        plan['segment_count'] = 3
        plan['packets'] = [{'id': 'P001', 'source_segments': ['S000001', 'S000002']},
                           {'id': 'P002', 'source_segments': ['S000003']}]
        (root / 'note_plan.json').write_text(json.dumps(plan), encoding='utf-8')
        review['coverage'] = [
            {'packet_id': 'P001', 'source_ranges': [['S000001', 'S000001']],
             'status': 'written', 'section': 'Example'},
            {'packet_id': 'P002', 'source_ranges': [['S000003', 'S000003']],
             'status': 'written', 'section': 'Example'},
        ]
        _review(root, review)
        result = note_audit.audit_directory(root)
        missing = [e for e in result['errors'] if e['code'] == 'packet_not_reviewed']
        assert missing == [{'code': 'packet_not_reviewed', 'packet_id': 'P001',
                            'missing_segments': ['S000002']}], result
        review['coverage'][0]['source_ranges'] = [['S000001', 'S000003']]
        _review(root, review)
        assert 'review_segment_mismatch' in _codes(note_audit.audit_directory(root))


def test_ranges_preserve_omission_section_and_visual_delivery_gates():
    with tempfile.TemporaryDirectory() as tmp:
        root, review = _fixture(tmp)
        coverage = review['coverage'][0]
        coverage.pop('source_segments')
        coverage['source_ranges'] = [['S000001', 'S000001']]
        coverage['status'] = 'omitted'
        review['visual_review'] = 'pending'
        _review(root, review)
        assert {'omission_reason_missing', 'visual_review_pending'} <= _codes(note_audit.audit_directory(root))
        coverage.update(status='written', section='Absent')
        _review(root, review)
        assert 'review_section_missing' in _codes(note_audit.audit_directory(root))


def test_alternate_book_and_review_are_audited_without_touching_formal_artifacts():
    with tempfile.TemporaryDirectory() as tmp:
        root, review = _fixture(tmp)
        assembled_book = root / 'book.assembled.md'
        assembled_review = root / 'note_review.assembled.json'
        assembled_book.write_text('# Draft\n\n## Assembled example\n\nComplete explanation.\n\n**本章小结**：给定条件下验证结果。\n', encoding='utf-8')
        review['coverage'][0]['section'] = 'Assembled example'
        assembled_review.write_text(json.dumps(review), encoding='utf-8')
        (root / 'book.md').write_text('Formal book has ![todo](SCREENSHOT:00:00:10)', encoding='utf-8')
        (root / 'note_review.json').write_text('Unrelated unfinished human review', encoding='utf-8')
        artifacts = [root / 'book.md', root / 'note_review.json', assembled_book, assembled_review]
        originals = {path: path.read_bytes() for path in artifacts}
        result = note_audit.audit_directory(
            root, require_review=True, book_name='book.assembled.md', review_name='note_review.assembled.json')
        assert result['status'] == 'checks_passed', result
        assert result['review_checked']
        assert all(path.read_bytes() == data for path, data in originals.items())
        assert not (root / 'note_audit.json').exists()
        assert 'unresolved_screenshot' in _codes(note_audit.audit_directory(root))


def test_alternate_audit_names_reject_paths_outside_the_video_directory():
    with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as other:
        root, _ = _fixture(tmp)
        outside = Path(other) / 'external.md'
        outside.write_text('Outside artifact must not be read or changed.', encoding='utf-8')
        for field in ('book_name', 'review_name'):
            for name in ('../external.md', str(outside), 'nested/book.md', '', None):
                try:
                    note_audit.audit_directory(root, **{field: name})
                except ValueError:
                    pass
                else:
                    raise AssertionError(f'Unsafe {field} accepted: {name!r}')
        assert outside.read_text(encoding='utf-8') == 'Outside artifact must not be read or changed.'


def test_alternate_audit_rejects_symlink_inputs_that_resolve_outside_video_directory():
    import unittest
    with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as other:
        root, _ = _fixture(tmp)
        external = Path(other) / 'external.txt'
        external.write_text('External content', encoding='utf-8')
        link = root / 'linked.md'
        try:
            link.symlink_to(external)
        except (OSError, NotImplementedError) as exc:
            raise unittest.SkipTest(f'File symlinks unavailable: {type(exc).__name__}')
        for field in ('book_name', 'review_name'):
            try:
                note_audit.audit_directory(root, **{field: link.name})
            except ValueError:
                pass
            else:
                raise AssertionError(f'Escaping symlink accepted for {field}')
        assert link.is_symlink()
        assert external.read_text(encoding='utf-8') == 'External content'
