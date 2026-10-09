"""Repair staged writing locally without repeating paid model attempts."""
import json
import copy
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
import note_plan
import note_usage
import note_workflow
from test_note_optimization import fixture, payload, response


def rejects(action):
    try:
        action()
    except ValueError as exc:
        return str(exc)
    raise AssertionError('Invalid local recovery was accepted')


def failed_candidate(root):
    """A valid transport result with a one-sentence coverage omission."""
    data = payload()
    data['review']['coverage'][0]['source_ranges'] = [['S000001', 'S000001']]
    calls = []
    def runner(*args, **kwargs):
        calls.append(1)
        return response(data, total_cost_usd=.23)
    rejects(lambda: note_workflow.run_one(root, 'jobs/P001', output_mode='structured', runner=runner))
    assert calls == [1]
    target = root / 'jobs/P001'
    candidate = target / 'candidates/A001'
    state = note_workflow._json(target / 'run.json')
    assert state['status'] == 'failed' and state['validation']['code'] == 'coverage_missing'
    assert state['candidate'] == 'candidates/A001' and candidate.is_dir()
    assert not (target / 'draft.md').exists() and not (target / 'review.json').exists()
    return target, candidate, state


def fix_coverage(candidate):
    review = note_workflow._json(candidate / 'review.json')
    review['coverage'][0]['source_ranges'] = [['S000001', 'S000002']]
    note_workflow._save(candidate / 'review.json', review)
    return review


def forbidden_model(*args, **kwargs):
    raise AssertionError('Local recovery called the model')


def assert_one_paid_failed_attempt(root, old_state, *, recovery_status=None):
    state = note_workflow._json(root / 'jobs/P001/run.json')
    assert state['attempts'] == 1 and state['history'] == old_state['history']
    assert state['history'][0]['status'] == 'failed'
    assert state['usage'] == old_state['usage'] and state['cost_usd_estimate'] == .23
    # A recovered produced state retains its original paid failed history record.
    assert note_workflow._run_history(state, allow_initial=True) == state['history']
    report = note_usage.summarize(root)
    assert report['overall']['attempts'] == 1
    assert report['attempts'][0]['status'] == 'failed'
    assert report['overall']['tokens']['input_tokens'] == 10
    assert report['overall']['tokens']['output_tokens'] == 20
    assert report['overall']['cost_usd_estimate'] == .23
    if recovery_status is not None:
        assert report['recovered_jobs'] == 1
        assert report['recoveries'] == [{'source': 'jobs/P001/recovery.json', 'kind': 'writing',
                                        'status': recovery_status}]
        assert report['changed_recovered_jobs'] == int(recovery_status == 'changed_after_recovery')


def test_locally_repaired_coverage_recovers_without_model_and_preserves_paid_failure():
    with tempfile.TemporaryDirectory() as temporary:
        root, _ = fixture(temporary)
        target, candidate, failed = failed_candidate(root)
        manifest_before = (candidate / 'manifest.json').read_bytes()
        review = fix_coverage(candidate)
        with patch.object(note_workflow.subprocess, 'run', forbidden_model):
            result = note_workflow.recover_writing(root, 'jobs/P001')
        assert result == {'status': 'recovered', 'packet_id': 'P001', 'attempt': 1, 'candidate_edited': True}
        assert (target / 'draft.md').read_bytes() == (candidate / 'draft.md').read_bytes()
        assert note_workflow._json(target / 'review.json') == review
        state = note_workflow._json(target / 'run.json')
        assert state['status'] == 'produced' and state['recovered_locally'] is True
        assert (candidate / 'manifest.json').read_bytes() == manifest_before
        assert note_workflow._json(target / 'recovery.json')['candidate_edited'] is True
        assert_one_paid_failed_attempt(root, failed, recovery_status='recovered')
        assert not (target / note_job_lock.LOCK_NAME).exists()


def test_invalid_transport_shape_never_creates_a_recoverable_candidate():
    with tempfile.TemporaryDirectory() as temporary:
        root, _ = fixture(temporary)
        target = root / 'jobs/P001'
        invalid = payload()
        invalid['review']['PRIVATE_UNEXPECTED_FIELD'] = 'PRIVATE_UNEXPECTED_VALUE'
        rejects(lambda: note_workflow.run_one(root, 'jobs/P001', output_mode='structured',
            runner=lambda *args, **kwargs: response(invalid, total_cost_usd=.23)))
        state = note_workflow._json(target / 'run.json')
        assert state['failure_code'] == 'invalid_structured_output' and 'candidate' not in state
        assert not (target / 'candidates').exists()
        original = (target / 'run.json').read_bytes()
        with patch.object(note_workflow.subprocess, 'run', forbidden_model):
            rejects(lambda: note_workflow.recover_writing(root, 'jobs/P001'))
        assert (target / 'run.json').read_bytes() == original
        assert 'PRIVATE_UNEXPECTED' not in json.dumps(state)
        assert not (target / 'draft.md').exists() and not (target / 'review.json').exists()
        assert_one_paid_failed_attempt(root, state)


def test_recovery_rechecks_schema_coverage_facts_and_chapter_gates_after_local_edits():
    for defect in ('missing_ranges', 'foreign_ranges', 'pending_facts', 'missing_summary', 'empty_summary'):
        with tempfile.TemporaryDirectory() as temporary:
            root, _ = fixture(temporary)
            target, candidate, failed = failed_candidate(root)
            review = fix_coverage(candidate)
            if defect == 'missing_ranges':
                del review['coverage'][0]['source_ranges']
            elif defect == 'foreign_ranges':
                review['coverage'][0]['source_ranges'] = [['S000001', 'S000003']]
            elif defect == 'pending_facts':
                review['fact_review'] = 'pending'
            else:
                draft = (candidate / 'draft.md').read_text(encoding='utf-8')
                draft = draft.split('**本章小结**')[0]
                if defect == 'empty_summary':
                    draft += '**本章小结**\nTODO\n'
                    review['boundary_notes'] = ['仍需处理后续例子。']
                (candidate / 'draft.md').write_text(draft, encoding='utf-8')
            note_workflow._save(candidate / 'review.json', review)
            state_before = (target / 'run.json').read_bytes()
            with patch.object(note_workflow.subprocess, 'run', forbidden_model):
                rejects(lambda: note_workflow.recover_writing(root, 'jobs/P001'))
            assert (target / 'run.json').read_bytes() == state_before
            assert not (target / 'draft.md').exists() and not (target / 'review.json').exists()
            assert not (target / 'recovery.json').exists()
            assert_one_paid_failed_attempt(root, failed)


def test_source_or_task_changes_refuse_recovery_even_after_candidate_repair():
    for changed in ('source', 'task', 'task_and_hash'):
        with tempfile.TemporaryDirectory() as temporary:
            root, _ = fixture(temporary)
            target, candidate, failed = failed_candidate(root)
            fix_coverage(candidate)
            if changed == 'source':
                source = note_workflow._json(root / 'transcript.json')
                source['segments'][0]['text'] += '来源增加新的条件。'
                note_workflow._save(root / 'transcript.json', source)
            else:
                task = target / 'task.md'
                task.write_text(task.read_text(encoding='utf-8') + '\n新的写作要求。\n', encoding='utf-8')
                if changed == 'task_and_hash':
                    metadata = note_workflow._json(target / 'job.json')
                    metadata['task_sha256'] = note_workflow._sha(task)
                    note_workflow._save(target / 'job.json', metadata)
            state_before = (target / 'run.json').read_bytes()
            with patch.object(note_workflow.subprocess, 'run', forbidden_model):
                rejects(lambda: note_workflow.recover_writing(root, 'jobs/P001'))
            assert (target / 'run.json').read_bytes() == state_before
            assert not (target / 'draft.md').exists() and not (target / 'review.json').exists()
            assert not (target / 'recovery.json').exists()
            assert_one_paid_failed_attempt(root, failed)


def test_recovery_never_overwrites_an_existing_human_draft_or_review():
    for protected_name in ('draft.md', 'review.json'):
        with tempfile.TemporaryDirectory() as temporary:
            root, _ = fixture(temporary)
            target, candidate, failed = failed_candidate(root)
            fix_coverage(candidate)
            protected = target / protected_name
            human = b'HUMAN_REVIEW_OR_DRAFT_MUST_BE_KEPT\n'
            protected.write_bytes(human)
            with patch.object(note_workflow.subprocess, 'run', forbidden_model):
                rejects(lambda: note_workflow.recover_writing(root, 'jobs/P001'))
            assert protected.read_bytes() == human
            assert not (target / ('review.json' if protected_name == 'draft.md' else 'draft.md')).exists()
            assert not (target / 'recovery.json').exists()
            assert_one_paid_failed_attempt(root, failed)


def test_partial_primary_pair_recovers_after_second_file_write_failure():
    with tempfile.TemporaryDirectory() as temporary:
        root, _ = fixture(temporary)
        target = root / 'jobs/P001'
        publish_text = note_workflow._publish_writing_text
        calls = []
        def write_with_failure(path, text):
            if Path(path) == target / 'review.json':
                raise OSError('CONTROLLED_PRIMARY_WRITE_FAILURE')
            return publish_text(path, text)
        def runner(*args, **kwargs):
            calls.append(1)
            return response(payload(), total_cost_usd=.23)
        with patch.object(note_workflow, '_publish_writing_text', write_with_failure):
            rejects(lambda: note_workflow.run_one(root, 'jobs/P001', output_mode='structured', runner=runner))
        failed = note_workflow._json(target / 'run.json')
        assert failed['failure_code'] == 'artifact_write' and failed['candidate'] == 'candidates/A001'
        assert calls == [1] and (target / 'draft.md').is_file() and not (target / 'review.json').exists()
        original_draft = (target / 'draft.md').read_bytes()
        writes = []
        def recorded_write(path, text):
            writes.append(Path(path))
            return publish_text(path, text)
        with patch.object(note_workflow.subprocess, 'run', forbidden_model), patch.object(note_workflow, '_publish_writing_text', recorded_write):
            result = note_workflow.recover_writing(root, 'jobs/P001')
        assert result['status'] == 'recovered' and result['candidate_edited'] is False
        assert target / 'draft.md' not in writes and target / 'review.json' in writes
        assert (target / 'draft.md').read_bytes() == original_draft
        assert (target / 'review.json').is_file()
        assert_one_paid_failed_attempt(root, failed, recovery_status='recovered')


def test_recovery_rejects_old_attempts_and_already_recovered_jobs():
    with tempfile.TemporaryDirectory() as temporary:
        root, _ = fixture(temporary)
        target, candidate, failed = failed_candidate(root)
        fix_coverage(candidate)
        with patch.object(note_workflow.subprocess, 'run', forbidden_model):
            rejects(lambda: note_workflow.recover_writing(root, 'jobs/P001', attempt=2))
            note_workflow.recover_writing(root, 'jobs/P001', attempt=1)
            recovered_bytes = (target / 'run.json').read_bytes()
            rejects(lambda: note_workflow.recover_writing(root, 'jobs/P001'))
        assert (target / 'run.json').read_bytes() == recovered_bytes
        assert_one_paid_failed_attempt(root, failed, recovery_status='recovered')


def test_recovery_report_marks_changed_artifacts_or_metadata_without_losing_paid_usage():
    for changed in ('draft', 'review', 'incomplete_review', 'metadata'):
        with tempfile.TemporaryDirectory() as temporary:
            root, _ = fixture(temporary)
            target, candidate, failed = failed_candidate(root)
            fix_coverage(candidate)
            with patch.object(note_workflow.subprocess, 'run', forbidden_model):
                note_workflow.recover_writing(root, 'jobs/P001')
            assert_one_paid_failed_attempt(root, failed, recovery_status='recovered')
            state_before = (target / 'run.json').read_bytes()
            if changed == 'draft':
                draft = target / 'draft.md'
                draft.write_text(draft.read_text(encoding='utf-8') + '\n人工添加的解释。\n', encoding='utf-8')
            elif changed == 'review':
                review = note_workflow._json(target / 'review.json')
                review['fact_review_reason'] += '人工调整审阅范围。'
                note_workflow._save(target / 'review.json', review)
            elif changed == 'incomplete_review':
                (target / 'review.json').write_text('incomplete local JSON edit', encoding='utf-8')
            else:
                metadata = note_workflow._json(target / 'job.json')
                metadata['terms_sha256'] = 'updated_input_fingerprint'
                note_workflow._save(target / 'job.json', metadata)
            assert_one_paid_failed_attempt(root, failed, recovery_status='changed_after_recovery')
            assert (target / 'run.json').read_bytes() == state_before


def test_malformed_recovery_history_is_rejected_before_writing_any_primary_artifact():
    for malformed in ('not a history list', ['not a history object']):
        with tempfile.TemporaryDirectory() as temporary:
            root, _ = fixture(temporary)
            target, candidate, failed = failed_candidate(root)
            fix_coverage(candidate)
            failed['history'] = malformed
            note_workflow._save(target / 'run.json', failed)
            state_before = (target / 'run.json').read_bytes()
            candidate_before = (candidate / 'draft.md').read_bytes()
            with patch.object(note_workflow.subprocess, 'run', forbidden_model):
                rejects(lambda: note_workflow.recover_writing(root, 'jobs/P001'))
            assert (target / 'run.json').read_bytes() == state_before
            assert (candidate / 'draft.md').read_bytes() == candidate_before
            assert not (target / 'draft.md').exists() and not (target / 'review.json').exists()
            assert not (target / 'recovery.json').exists()
            assert not (target / note_job_lock.LOCK_NAME).exists()


def corrupt_ledgers(failed):
    variants = []
    for value in (0, False, '1'):
        corrupted = copy.deepcopy(failed)
        corrupted['attempts'] = value
        variants.append(corrupted)
    for value in (0, True, '1'):
        corrupted = copy.deepcopy(failed)
        corrupted['history'][0]['attempts'] = value
        variants.append(corrupted)
    corrupted = copy.deepcopy(failed)
    corrupted['history'].append(copy.deepcopy(corrupted['history'][0]))
    variants.append(corrupted)
    corrupted = copy.deepcopy(failed)
    corrupted['history'][0]['attempts'] = 2
    variants.append(corrupted)
    corrupted = copy.deepcopy(failed)
    corrupted['history'][0]['usage']['output_tokens'] += 1
    variants.append(corrupted)
    corrupted = copy.deepcopy(failed)
    corrupted['history'][0]['cost_usd_estimate'] += .01
    variants.append(corrupted)
    corrupted = copy.deepcopy(failed)
    corrupted['history'][0]['usage_known'] = False
    variants.append(corrupted)
    variants.append([])
    return variants


def test_invalid_or_conflicting_attempt_ledgers_block_run_recovery_cache_and_reuse():
    for number in range(12):
        with tempfile.TemporaryDirectory() as temporary:
            root, _ = fixture(temporary)
            target, candidate, failed = failed_candidate(root)
            fix_coverage(candidate)
            corrupted = corrupt_ledgers(failed)[number]
            note_workflow._save(target / 'run.json', corrupted)
            state_before = (target / 'run.json').read_bytes()
            cache = root / 'isolated-cache'
            operations = (
                lambda: note_workflow.run_one(root, 'jobs/P001', output_mode='structured',
                                              reuse_cache=True, cache_dir=cache, runner=forbidden_model),
                lambda: note_workflow.recover_writing(root, 'jobs/P001'),
                lambda: note_workflow.cache_writing(root, 'jobs/P001', cache_dir=cache, accept_source_review=True),
                lambda: note_workflow.reuse_writing(root, 'jobs/P001', cache_dir=cache),
            )
            with patch.object(note_workflow.subprocess, 'run', forbidden_model):
                for operation in operations:
                    rejects(operation)
                    assert (target / 'run.json').read_bytes() == state_before
                    assert not (target / 'draft.md').exists() and not (target / 'review.json').exists()
                    assert not (target / 'recovery.json').exists()
                    assert not (target / note_job_lock.LOCK_NAME).exists()
            assert not cache.exists()


def test_initial_zero_attempt_state_can_run_once_and_produces_a_positive_paid_record():
    with tempfile.TemporaryDirectory() as temporary:
        root, _ = fixture(temporary)
        target = root / 'jobs/P001'
        note_workflow._save(target / 'run.json', {'attempts': 0, 'history': []})
        calls = []
        def runner(*args, **kwargs):
            calls.append(1)
            return response(payload(), total_cost_usd=.23)
        state = note_workflow.run_one(root, 'jobs/P001', output_mode='structured', runner=runner)
        assert calls == [1] and state['attempts'] == 1 and state['status'] == 'produced'
        assert [item['attempts'] for item in state['history']] == [1]
        report = note_usage.summarize(root)
        assert report['overall']['attempts'] == 1 and report['overall']['tokens']['output_tokens'] == 20


def test_recovery_preserves_legacy_failed_snapshot_without_history():
    with tempfile.TemporaryDirectory() as temporary:
        root, _ = fixture(temporary)
        target, candidate, failed = failed_candidate(root)
        fix_coverage(candidate)
        legacy = copy.deepcopy(failed)
        del legacy['history']
        note_workflow._save(target / 'run.json', legacy)
        with patch.object(note_workflow.subprocess, 'run', forbidden_model):
            note_workflow.recover_writing(root, 'jobs/P001')
        state = note_workflow._json(target / 'run.json')
        assert state['status'] == 'produced' and state['history'] == [legacy]
        assert state['history'][0]['status'] == 'failed'
        report = note_usage.summarize(root)
        assert report['overall']['attempts'] == 1
        assert report['overall']['tokens']['input_tokens'] == 10 and report['overall']['tokens']['output_tokens'] == 20
        assert report['attempts'][0]['status'] == 'failed' and report['overall']['cost_usd_estimate'] == .23
        assert report['recovered_jobs'] == 1 and report['changed_recovered_jobs'] == 0


def test_legacy_history_without_explicit_attempt_id_keeps_the_usage_reader_numbering():
    with tempfile.TemporaryDirectory() as temporary:
        root, _ = fixture(temporary)
        target, candidate, failed = failed_candidate(root)
        fix_coverage(candidate)
        legacy = copy.deepcopy(failed)
        del legacy['history'][0]['attempts']
        note_workflow._save(target / 'run.json', legacy)
        with patch.object(note_workflow.subprocess, 'run', forbidden_model):
            note_workflow.recover_writing(root, 'jobs/P001')
        state = note_workflow._json(target / 'run.json')
        assert state['history'] == failed['history']
        assert_one_paid_failed_attempt(root, failed, recovery_status='recovered')
