"""Publish new writing files exclusively, preserving candidates and paid usage."""
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

SRC = Path(__file__).resolve().parents[1] / 'src'
TESTS = Path(__file__).resolve().parent
for path in (SRC, TESTS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import note_job_lock
import note_usage
import note_workflow
from test_note_optimization import complete, fixture, payload, response


PUBLISH = 'rename' if os.name == 'nt' else 'link'


def _rejects(action):
    try:
        action()
    except (ValueError, OSError):
        return
    raise AssertionError('A conflicting or failed publication was accepted')


def _assert_paid_failed_attempt(root):
    target = root / 'jobs/P001'
    state = note_workflow._json(target / 'run.json')
    assert state['attempts'] == 1 and state['history'][0]['status'] == 'failed'
    assert state['candidate'] == 'candidates/A001'
    assert (target / state['candidate'] / 'draft.md').is_file()
    assert (target / state['candidate'] / 'review.json').is_file()
    assert (target / state['candidate'] / 'manifest.json').is_file()
    usage = note_usage.summarize(root)
    assert usage['overall']['attempts'] == 1 and usage['attempts'][0]['status'] == 'failed'
    assert usage['overall']['tokens']['input_tokens'] == 10
    assert usage['overall']['tokens']['output_tokens'] == 20
    assert usage['overall']['cost_usd_estimate'] == .23
    assert not (target / note_job_lock.LOCK_NAME).exists()
    return state


def test_file_created_at_publication_is_never_overwritten_and_paid_candidate_survives():
    for protected_name in ('draft.md', 'review.json'):
        with tempfile.TemporaryDirectory(prefix='vs_publication_') as temporary:
            root, _ = fixture(temporary)
            target = root / 'jobs/P001'
            protected = target / protected_name
            human = b'HUMAN_WORK_CREATED_AFTER_THE_EXISTENCE_CHECK\n'
            actual_publish = getattr(note_workflow.os, PUBLISH)
            injected, temporary_paths, calls = [], [], []
            def publish_with_conflict(source, destination, *args, **kwargs):
                if Path(destination) == protected and not injected:
                    temporary_paths.append(Path(source))
                    protected.write_bytes(human)
                    injected.append(1)
                return actual_publish(source, destination, *args, **kwargs)
            def runner(*args, **kwargs):
                calls.append(1)
                return response(payload(), total_cost_usd=.23)
            with patch.object(note_workflow.os, PUBLISH, publish_with_conflict):
                _rejects(lambda: note_workflow.run_one(root, 'jobs/P001',
                    output_mode='structured', runner=runner))
            assert injected == [1] and calls == [1]
            assert protected.read_bytes() == human
            assert all(path.parent == target and not path.exists() for path in temporary_paths)
            state = _assert_paid_failed_attempt(root)
            assert state['failure_code'] == 'artifact_write'
            assert not (target / 'recovery.json').exists()
            _rejects(lambda: note_workflow.recover_writing(root, 'jobs/P001'))
            assert protected.read_bytes() == human
            _assert_paid_failed_attempt(root)


def test_second_publication_failure_can_recover_only_the_missing_file_without_a_model():
    with tempfile.TemporaryDirectory(prefix='vs_publication_') as temporary:
        root, _ = fixture(temporary)
        target = root / 'jobs/P001'
        actual_publish = getattr(note_workflow.os, PUBLISH)
        temporary_paths, calls = [], []
        def fail_second_file(source, destination, *args, **kwargs):
            if Path(destination) == target / 'review.json':
                temporary_paths.append(Path(source))
                raise OSError('CONTROLLED_SECOND_PUBLICATION_FAILURE')
            return actual_publish(source, destination, *args, **kwargs)
        def runner(*args, **kwargs):
            calls.append(1)
            return response(payload(), total_cost_usd=.23)
        with patch.object(note_workflow.os, PUBLISH, fail_second_file):
            _rejects(lambda: note_workflow.run_one(root, 'jobs/P001',
                output_mode='structured', runner=runner))
        failed = _assert_paid_failed_attempt(root)
        assert failed['failure_code'] == 'artifact_write' and calls == [1]
        assert (target / 'draft.md').is_file() and not (target / 'review.json').exists()
        original_draft = (target / 'draft.md').read_bytes()
        assert all(path.parent == target and not path.exists() for path in temporary_paths)
        published = []
        def record_publish(source, destination, *args, **kwargs):
            published.append(Path(destination))
            return actual_publish(source, destination, *args, **kwargs)
        def forbidden_model(*args, **kwargs):
            raise AssertionError('Local recovery called a model')
        with patch.object(note_workflow.os, PUBLISH, record_publish), \
                patch.object(note_workflow.subprocess, 'run', forbidden_model):
            result = note_workflow.recover_writing(root, 'jobs/P001')
        assert result['status'] == 'recovered' and result['candidate_edited'] is False
        assert published == [target / 'review.json']
        assert (target / 'draft.md').read_bytes() == original_draft
        assert (target / 'review.json').is_file()
        state = _assert_paid_failed_attempt(root)
        assert state['status'] == 'produced' and state['history'] == failed['history']
        assert note_usage.summarize(root)['recovered_jobs'] == 1


def test_cache_hit_publication_race_preserves_human_files_and_prior_paid_failure():
    for protected_name in ('draft.md', 'review.json'):
        with tempfile.TemporaryDirectory(prefix='vs_cache_publication_') as temporary:
            base = Path(temporary)
            source, _ = fixture(base / 'source')
            complete(source)
            cache = base / 'cache'
            note_workflow.cache_writing(source, 'jobs/P001', cache_dir=cache,
                                        accept_source_review=True)
            root, _ = fixture(base / 'target')
            target = root / 'jobs/P001'
            broken = payload()
            broken['review']['coverage'][0]['source_ranges'] = [['S000001', 'S000001']]
            _rejects(lambda: note_workflow.run_one(root, 'jobs/P001', output_mode='structured',
                runner=lambda *args, **kwargs: response(broken, total_cost_usd=.23)))
            _assert_paid_failed_attempt(root)
            before = (target / 'run.json').read_bytes()
            protected = target / protected_name
            human = b'HUMAN_WORK_CREATED_DURING_CACHE_PUBLICATION\n'
            actual_publish = getattr(note_workflow.os, PUBLISH)
            injected = []
            def cache_conflict(source_path, destination, *args, **kwargs):
                if Path(destination) == protected and not injected:
                    protected.write_bytes(human)
                    injected.append(1)
                return actual_publish(source_path, destination, *args, **kwargs)
            def forbidden_model(*args, **kwargs):
                raise AssertionError('A cache hit called the model')
            with patch.object(note_workflow.os, PUBLISH, cache_conflict):
                _rejects(lambda: note_workflow.run_one(root, 'jobs/P001', reuse_cache=True,
                    cache_dir=cache, runner=forbidden_model))
            assert injected == [1] and protected.read_bytes() == human
            assert (target / 'run.json').read_bytes() == before
            assert not (target / 'reuse.json').exists()
            _assert_paid_failed_attempt(root)
