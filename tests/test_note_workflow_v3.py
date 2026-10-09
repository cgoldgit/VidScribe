"""Prompt compilation, exclusion, usage preservation and safe diagnostics."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import threading

SRC = Path(__file__).resolve().parents[1] / 'src'
TESTS = Path(__file__).resolve().parent
for path in (SRC, TESTS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
import note_job_lock
import note_usage
import note_workflow
from test_note_optimization import fixture, payload, response


def rejects(action):
    try:
        action()
    except ValueError as exc:
        return str(exc)
    raise AssertionError('Invalid workflow operation was accepted')


def failed_snapshot():
    return {'attempts': 1, 'status': 'failed', 'kind': 'writing', 'usage_known': True,
            'usage': {'input_tokens': 100, 'output_tokens': 200,
                      'cache_read_input_tokens': 0, 'cache_creation_input_tokens': 0},
            'cost_usd_estimate': .17, 'elapsed_seconds': 2.5, 'num_turns': 2}


def prompt_for(root, plan, packet_id):
    target = root / 'jobs' / packet_id
    metadata = note_workflow._json(target / 'job.json')
    return note_workflow._job_prompt(root, target, metadata, plan, 'inline', 'structured')


def test_same_job_concurrent_calls_run_model_once_and_preserve_paid_history():
    with tempfile.TemporaryDirectory() as temporary:
        root, _ = fixture(temporary)
        target = root / 'jobs/P001'
        previous = failed_snapshot()
        note_workflow._save(target / 'run.json', previous)
        entered, release = threading.Event(), threading.Event()
        calls, results, errors = [], [], []
        def runner(*args, **kwargs):
            calls.append(1)
            entered.set()
            if not release.wait(5):
                raise AssertionError('Test did not release the running model')
            return response(payload(), total_cost_usd=.23)
        def run_first():
            try:
                results.append(note_workflow.run_one(root, 'jobs/P001', output_mode='structured', runner=runner))
            except BaseException as exc:
                errors.append(exc)
        thread = threading.Thread(target=run_first)
        thread.start()
        try:
            assert entered.wait(5)
            message = rejects(lambda: note_workflow.run_one(root, 'jobs/P001', output_mode='structured', runner=runner))
            assert 'claimed' in message
            assert len(calls) == 1
            active = note_workflow._json(target / 'run.json')
            assert active['attempts'] == 2 and active['status'] == 'running'
            assert active['history'] == [previous]
        finally:
            release.set()
            thread.join(5)
        assert not thread.is_alive() and not errors and len(results) == 1
        state = note_workflow._json(target / 'run.json')
        assert [entry['attempts'] for entry in state['history']] == [1, 2]
        assert state['history'][0] == previous
        assert state['status'] == 'produced' and state['cost_usd_estimate'] == .23
        report = note_usage.summarize(root)
        assert report['overall']['attempts'] == 2
        assert abs(report['overall']['cost_usd_estimate'] - .40) < 1e-10
        assert report['overall']['tokens']['input_tokens'] == 110
        assert report['overall']['tokens']['output_tokens'] == 220
        assert not (target / note_job_lock.LOCK_NAME).exists()


def test_running_snapshot_without_lock_is_refused_without_changing_state():
    with tempfile.TemporaryDirectory() as temporary:
        root, _ = fixture(temporary)
        target = root / 'jobs/P001'
        note_workflow._save(target / 'run.json', {'attempts': 1, 'status': 'running',
                                                'usage_known': False, 'history': []})
        original = (target / 'run.json').read_bytes()
        def forbidden(*args, **kwargs):
            raise AssertionError('Running snapshot triggered another paid call')
        rejects(lambda: note_workflow.run_one(root, 'jobs/P001', output_mode='structured', runner=forbidden))
        assert (target / 'run.json').read_bytes() == original
        assert not (target / note_job_lock.LOCK_NAME).exists()
        assert not (target / 'draft.md').exists() and not (target / 'review.json').exists()


def test_legacy_failed_snapshot_is_preserved_before_second_attempt():
    with tempfile.TemporaryDirectory() as temporary:
        root, _ = fixture(temporary)
        target = root / 'jobs/P001'
        previous = failed_snapshot()
        assert 'history' not in previous
        note_workflow._save(target / 'run.json', previous)
        calls = []
        def runner(*args, **kwargs):
            calls.append(1)
            return response(None, is_error=True, subtype='error_max_turns', total_cost_usd=.23)
        rejects(lambda: note_workflow.run_one(root, 'jobs/P001', output_mode='structured', runner=runner))
        assert calls == [1]
        state = note_workflow._json(target / 'run.json')
        assert state['attempts'] == 2 and [item['attempts'] for item in state['history']] == [1, 2]
        assert state['history'][0] == previous
        assert state['history'][1]['failure_code'] == 'turn_limit'
        report = note_usage.summarize(root)
        assert [entry['attempt'] for entry in report['attempts']] == [1, 2]
        assert report['overall']['tokens']['output_tokens'] == 220
        assert abs(report['overall']['cost_usd_estimate'] - .40) < 1e-10


def test_source_or_terms_changed_during_call_refuses_result_and_preserves_usage():
    for changed in ('source', 'terms'):
        with tempfile.TemporaryDirectory() as temporary:
            root, _ = fixture(temporary)
            target = root / 'jobs/P001'
            def runner(*args, **kwargs):
                if changed == 'source':
                    source = note_workflow._json(root / 'transcript.json')
                    source['segments'][0]['text'] += '新增输入条件。'
                    note_workflow._save(root / 'transcript.json', source)
                else:
                    (root / 'TERMS.confirmed.md').write_text('新增确认术语。', encoding='utf-8')
                return response(payload(), total_cost_usd=.23)
            rejects(lambda: note_workflow.run_one(root, 'jobs/P001', output_mode='structured', runner=runner))
            state = note_workflow._json(target / 'run.json')
            assert state['status'] == 'failed' and state['failure_code'] == 'inputs_changed', state
            assert state['usage']['output_tokens'] == 20 and state['cost_usd_estimate'] == .23
            assert len(state['history']) == 1
            assert not (target / 'draft.md').exists() and not (target / 'review.json').exists()
            assert note_usage.summarize(root)['overall']['attempts'] == 1


def test_plan_duration_or_ownership_changed_during_call_refuses_result_and_preserves_usage():
    for changed in ('duration', 'ownership'):
        with tempfile.TemporaryDirectory() as temporary:
            root, _ = fixture(temporary)
            target = root / 'jobs/P001'
            source_before = (root / 'transcript.json').read_bytes()
            job_before = (target / 'job.json').read_bytes()
            def runner(*args, **kwargs):
                current = note_workflow._json(root / 'note_plan.json')
                if changed == 'duration':
                    current['duration'] += 1
                else:
                    first, second = current['packets'][:2]
                    first['source_segments'], second['source_segments'] = (
                        second['source_segments'], first['source_segments'])
                note_workflow._save(root / 'note_plan.json', current)
                return response(payload(), total_cost_usd=.23)
            rejects(lambda: note_workflow.run_one(root, 'jobs/P001', output_mode='structured', runner=runner))
            state = note_workflow._json(target / 'run.json')
            assert state['status'] == 'failed' and state['failure_code'] == 'inputs_changed', state
            assert state['usage']['output_tokens'] == 20 and state['cost_usd_estimate'] == .23
            assert len(state['history']) == 1
            assert not (target / 'draft.md').exists() and not (target / 'review.json').exists()
            assert (root / 'transcript.json').read_bytes() == source_before
            assert (target / 'job.json').read_bytes() == job_before
            assert note_usage.summarize(root)['overall']['attempts'] == 1


def test_compiled_prompt_keeps_complete_material_and_rules_once_without_metadata_values():
    with tempfile.TemporaryDirectory() as temporary:
        root, plan = fixture(temporary)
        prompt, embedded, metrics = prompt_for(root, plan, 'P001')
        assert metrics['prompt_layout'] == 'compiled' and embedded == 2
        writer = (note_workflow.BASE_DIR / 'prompts/writer_system.md').read_text(encoding='utf-8-sig')
        packet = (root / plan['packets'][0]['path']).read_text(encoding='utf-8-sig')
        assert prompt.count(writer) == 1 and prompt.count(packet) == 1
        source = note_workflow._json(root / 'transcript.json')
        # The complete packet also includes its two following context sentences.
        for segment in source['segments'][:4]:
            assert prompt.count(segment['text']) == 1
        metadata = note_workflow._json(root / 'jobs/P001/job.json')
        for key in ('source_sha256', 'packet_sha256', 'style_sha256', 'writer_sha256',
                    'review_template_sha256', 'task_sha256'):
            assert metadata[key] not in prompt
        assert str(root) not in prompt and str(note_workflow.BASE_DIR) not in prompt
        assert 'review.template.json' not in prompt
        template = (root / 'jobs/P001/review.template.json').read_text(encoding='utf-8')
        assert template not in prompt
        inspection = note_workflow.inspect_job(root, 'jobs/P001', output_mode='structured')
        assert inspection['prompt_layout'] == 'compiled' and inspection['prompt_chars'] == len(prompt)


def test_shared_prompt_prefix_is_identical_for_different_packets():
    with tempfile.TemporaryDirectory() as temporary:
        root, plan = fixture(temporary)
        first, _, first_metrics = prompt_for(root, plan, 'P001')
        second, _, second_metrics = prompt_for(root, plan, 'P002')
        assert first_metrics['prompt_layout'] == second_metrics['prompt_layout'] == 'compiled'
        length = first_metrics['shared_prefix_chars']
        assert length == second_metrics['shared_prefix_chars']
        assert first[:length] == second[:length]
        assert first_metrics['shared_prefix_sha256'] == second_metrics['shared_prefix_sha256']
        assert '# 本包 P001' in first[length:] and '# 本包 P002' in second[length:]
        assert '第1步' in first[length:] and '第3步' in second[length:]


def test_custom_task_uses_compatibility_prompt_and_preserves_all_instructions():
    with tempfile.TemporaryDirectory() as temporary:
        root, plan = fixture(temporary)
        target = root / 'jobs/P001'
        task = target / 'task.md'
        extra = '\n自定义约束：保留列表变量自定义名称，不添加新例子。\n'
        task.write_text(task.read_text(encoding='utf-8') + extra, encoding='utf-8')
        metadata = note_workflow._json(target / 'job.json')
        metadata['task_sha256'] = note_workflow._sha(task)
        note_workflow._save(target / 'job.json', metadata)
        prompt, _, metrics = prompt_for(root, plan, 'P001')
        assert metrics['prompt_layout'] == 'compatibility'
        assert task.read_text(encoding='utf-8') in prompt and prompt.count(extra) == 1
        writer = (note_workflow.BASE_DIR / 'prompts/writer_system.md').read_text(encoding='utf-8-sig')
        packet = (root / plan['packets'][0]['path']).read_text(encoding='utf-8-sig')
        assert prompt.count(writer) == 1 and prompt.count(packet) == 1
        inspection = note_workflow.inspect_job(root, 'jobs/P001', output_mode='structured')
        assert inspection['prompt_layout'] == 'compatibility'


def test_structured_shape_and_coverage_diagnostics_never_export_secret_keys_or_values():
    for defect in ('unknown_key', 'wrong_type', 'foreign_range', 'foreign_section'):
        with tempfile.TemporaryDirectory() as temporary:
            root, _ = fixture(temporary)
            target = root / 'jobs/P001'
            data = copy.deepcopy(payload())
            secret = 'PRIVATE_FIELD_AND_RESPONSE_DATA'
            if defect == 'unknown_key':
                data['review'][secret] = secret
                expected = {'code': 'unexpected', 'path': '$.structured_output.review'}
            elif defect == 'wrong_type':
                data['review']['coverage'] = secret
                expected = {'code': 'type', 'path': '$.structured_output.review.coverage'}
            elif defect == 'foreign_range':
                data['review']['coverage'][0]['source_ranges'] = [['S000001', 'S999999']]
                data['review']['coverage'][0]['reason'] = secret
                expected = {'code': 'coverage_ranges', 'path': '$.review.coverage[0].source_ranges'}
            else:
                data['review']['coverage'][0]['section_id'] = secret
                expected = {'code': 'coverage_section', 'path': '$.review.coverage[0].section_id'}
            message = rejects(lambda: note_workflow.run_one(root, 'jobs/P001', output_mode='structured',
                runner=lambda *args, **kwargs: response(data, result=secret, total_cost_usd=.23)))
            state = note_workflow._json(target / 'run.json')
            assert state['validation'] == expected, state.get('validation')
            assert state['failure_code'] == 'invalid_structured_output'
            assert state['usage']['output_tokens'] == 20 and len(state['history']) == 1
            assert secret not in message and secret not in json.dumps(state)
            assert secret not in json.dumps(note_usage.summarize(root))
            assert not (target / 'draft.md').exists() and not (target / 'review.json').exists()
