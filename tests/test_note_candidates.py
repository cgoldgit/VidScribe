"""Candidate persistence is local, exclusive, metadata-bound, and repairable."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

SRC = Path(__file__).resolve().parent.parent / 'src'
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import note_cache
import note_candidates
import note_results


SECRET = 'private-provider-text-never-include-in-errors'
METADATA = {'packet_id': 'P001', 'source_sha256': 'source-hash', 'editorial_policy_version': 1}
DRAFT = '## 课堂例子\n\n保留完整原句、推导与结果。\n'


def _review():
    review = {
        'coverage': [{'source_ranges': [['S000001', 'S000002']], 'status': 'written',
                      'section_id': 'P001.example', 'reason': ''}],
        'visual_requests': [], 'visual_review': 'pending', 'visual_review_reason': '需要独立核图。',
        'fact_review': 'complete', 'fact_review_reason': '已按原句核查事实。', 'corrections': [],
        'uncertainties': [], 'boundary_notes': [],
    }
    return note_results.stamp_review(review, METADATA)


def _error(action, operation):
    try:
        action()
    except ValueError as error:
        assert str(error) == f'Unable to {operation} writing candidate'
        assert SECRET not in repr(error)
        assert SECRET not in repr(vars(error))
        assert SECRET not in repr(error.args)
        assert error.__suppress_context__ is True
        return
    raise AssertionError('Unsafe candidate operation was accepted')


def _write_json(path, value):
    path.write_bytes((json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8'))


def test_stage_round_trip_binds_attempt_metadata_and_initial_hashes():
    with tempfile.TemporaryDirectory(prefix='vs_candidates_') as tmp:
        job = Path(tmp)
        review = _review()
        original = copy.deepcopy(review)
        relative = note_candidates.stage(job, 1, METADATA, DRAFT, review)
        assert relative == 'candidates/A001'
        assert {p.name for p in (job / relative).iterdir()} == {'draft.md', 'review.json', 'manifest.json'}
        draft, restored, manifest = note_candidates.load(job, 1, dict(reversed(list(METADATA.items()))))
        assert draft == DRAFT and restored == review and review == original
        assert manifest['attempt'] == 1
        assert manifest['metadata_sha256'] == note_cache.digest(METADATA)
        assert manifest['draft_sha256'] == hashlib.sha256(DRAFT.encode('utf-8')).hexdigest()
        assert manifest['review_sha256'] == note_cache.digest(review)
        assert manifest['artifacts_sha256'] == note_cache.digest({'draft_md': DRAFT, 'review': review})
        assert 'source-hash' not in (job / relative / 'manifest.json').read_text(encoding='utf-8')


def test_stage_never_overwrites_an_existing_or_partial_attempt():
    with tempfile.TemporaryDirectory(prefix='vs_candidates_') as tmp:
        job = Path(tmp)
        relative = note_candidates.stage(job, 1, METADATA, DRAFT, _review())
        originals = {path.name: path.read_bytes() for path in (job / relative).iterdir()}
        _error(lambda: note_candidates.stage(job, 1, METADATA, DRAFT + SECRET, _review()), 'stage')
        assert originals == {path.name: path.read_bytes() for path in (job / relative).iterdir()}
        partial = job / 'candidates/A002'
        partial.mkdir()
        (partial / 'draft.md').write_bytes(b'local draft')
        _error(lambda: note_candidates.stage(job, 2, METADATA, DRAFT, _review()), 'stage')
        assert (partial / 'draft.md').read_bytes() == b'local draft'
        _error(lambda: note_candidates.load(job, 2, METADATA), 'load')


def test_load_allows_local_repairs_and_returns_initial_hashes():
    with tempfile.TemporaryDirectory(prefix='vs_candidates_') as tmp:
        job = Path(tmp)
        relative = note_candidates.stage(job, 1, METADATA, DRAFT, _review())
        _, _, original = note_candidates.load(job, 1, METADATA)
        edited_review = _review()
        edited_review['coverage'][0]['source_ranges'] = [['S000001', 'S000999']]
        edited_draft = DRAFT + '\n补回原句中的边界条件。\n'
        (job / relative / 'draft.md').write_bytes(edited_draft.encode('utf-8'))
        _write_json(job / relative / 'review.json', edited_review)
        draft, review, manifest = note_candidates.load(job, 1, METADATA)
        assert draft == edited_draft and review == edited_review and manifest == original
        assert manifest['artifacts_sha256'] != note_cache.digest({'draft_md': draft, 'review': review})


def test_changed_metadata_and_invalid_attempts_are_rejected_without_text():
    with tempfile.TemporaryDirectory(prefix='vs_candidates_') as tmp:
        job = Path(tmp)
        note_candidates.stage(job, 1, METADATA, DRAFT, _review())
        changed = dict(METADATA, source_sha256=SECRET)
        _error(lambda: note_candidates.load(job, 1, changed), 'load')
        for attempt in (True, False, 0, -1, '../' + SECRET, 1.0, 1000000):
            _error(lambda: note_candidates.stage(job, attempt, METADATA, DRAFT, _review()), 'stage')
            _error(lambda: note_candidates.load(job, attempt, METADATA), 'load')
        _error(lambda: note_candidates.load(job, 2, METADATA), 'load')


def test_stage_rejects_raw_responses_and_invalid_input_types():
    with tempfile.TemporaryDirectory(prefix='vs_candidates_') as tmp:
        job = Path(tmp)
        review = dict(_review(), **{SECRET: {'result': SECRET}})
        _error(lambda: note_candidates.stage(job, 1, METADATA, DRAFT, review), 'stage')
        _error(lambda: note_candidates.stage(job, 1, METADATA, [SECRET], _review()), 'stage')
        _error(lambda: note_candidates.stage(job, 1, METADATA, DRAFT, [SECRET]), 'stage')
        _error(lambda: note_candidates.stage(job, 1, [SECRET], DRAFT, _review()), 'stage')
        _error(lambda: note_candidates.stage(job, 1, {SECRET: object()}, DRAFT, _review()), 'stage')
        assert not (job / 'candidates').exists()


def test_load_rejects_malformed_manifest_or_review_safely():
    with tempfile.TemporaryDirectory(prefix='vs_candidates_') as tmp:
        job = Path(tmp)
        relative = note_candidates.stage(job, 1, METADATA, DRAFT, _review())
        manifest_path = job / relative / 'manifest.json'
        original = json.loads(manifest_path.read_text(encoding='utf-8'))
        for manifest in ([SECRET], dict(original, attempt=True), dict(original, attempt=2),
                         dict(original, schema_version=True), dict(original, schema_version=1.0),
                         dict(original, metadata_sha256=SECRET),
                         dict(original, draft_sha256=123), dict(original, **{SECRET: SECRET})):
            _write_json(manifest_path, manifest)
            _error(lambda: note_candidates.load(job, 1, METADATA), 'load')
        manifest_path.write_bytes(SECRET.encode('utf-8'))
        _error(lambda: note_candidates.load(job, 1, METADATA), 'load')
        _write_json(manifest_path, original)
        _write_json(job / relative / 'review.json', [SECRET])
        _error(lambda: note_candidates.load(job, 1, METADATA), 'load')
        (job / relative / 'review.json').write_bytes(b'\xff')
        _error(lambda: note_candidates.load(job, 1, METADATA), 'load')


def test_symlinked_paths_and_ancestors_are_rejected_without_link_privileges():
    with tempfile.TemporaryDirectory(prefix='vs_candidates_') as tmp:
        job = Path(tmp).resolve()
        relative = note_candidates.stage(job, 1, METADATA, DRAFT, _review())
        original_is_link = Path.is_symlink
        for linked in (job, job.parent, job / 'candidates', job / relative,
                       job / relative / 'draft.md', job / relative / 'review.json',
                       job / relative / 'manifest.json'):
            def is_link(path, linked=linked):
                return path == linked or original_is_link(path)
            with patch.object(Path, 'is_symlink', is_link):
                _error(lambda: note_candidates.load(job, 1, METADATA), 'load')
        with patch.object(Path, 'is_symlink', lambda path: path == job / 'candidates/A002'):
            _error(lambda: note_candidates.stage(job, 2, METADATA, DRAFT, _review()), 'stage')
        assert not (job / 'candidates/A002').exists()


def test_junctions_and_resolved_path_escapes_are_rejected():
    with tempfile.TemporaryDirectory(prefix='vs_candidates_') as tmp:
        job = Path(tmp).resolve()
        note_candidates.stage(job, 1, METADATA, DRAFT, _review())
        if hasattr(Path, 'is_junction'):
            with patch.object(Path, 'is_junction', lambda path: path == job / 'candidates'):
                _error(lambda: note_candidates.load(job, 1, METADATA), 'load')
                _error(lambda: note_candidates.stage(job, 2, METADATA, DRAFT, _review()), 'stage')
        original_resolve = Path.resolve
        def escaping(path, *args, **kwargs):
            return job.parent if path == job / 'candidates/A001' else original_resolve(path, *args, **kwargs)
        with patch.object(Path, 'resolve', escaping):
            _error(lambda: note_candidates.load(job, 1, METADATA), 'load')
        _error(lambda: note_candidates.load(job / '../' / job.name, 1, METADATA), 'load')


def test_partial_io_failure_never_creates_a_loadable_manifest():
    with tempfile.TemporaryDirectory(prefix='vs_candidates_') as tmp:
        job = Path(tmp)
        write = note_candidates._write_bytes
        def failing(root, relative, content):
            if relative.endswith('review.json'):
                raise OSError(SECRET)
            return write(root, relative, content)
        with patch.object(note_candidates, '_write_bytes', failing):
            _error(lambda: note_candidates.stage(job, 1, METADATA, DRAFT, _review()), 'stage')
        assert (job / 'candidates/A001/draft.md').is_file()
        assert not (job / 'candidates/A001/manifest.json').exists()
        _error(lambda: note_candidates.load(job, 1, METADATA), 'load')
        _error(lambda: note_candidates.stage(job, 1, METADATA, DRAFT, _review()), 'stage')
