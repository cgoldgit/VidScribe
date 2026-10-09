"""Reject bad structured responses and prove conservative per-packet reuse."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile

SRC = Path(__file__).resolve().parents[1] / 'src'
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
import note_cache
import note_plan
import note_results
import note_usage
import note_workflow


def fixture(root, *, changed=None):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    source = {'video_id': 'BVcache', 'title': '五章', 'duration': 100,
              'chapters': [{'start': i * 20, 'title': f'第{i + 1}章'} for i in range(5)],
              'segments': [{'start': i * 10, 'end': (i + 1) * 10,
                            'text': f'第{i + 1}步，输入数值，检查条件和结果。'} for i in range(10)]}
    if changed is not None:
        source['segments'][changed]['text'] += '还需要验证新条件。'
    note_workflow._save(root / 'transcript.json', source)
    plan = note_plan.generate(root, video_id='BVcache')
    assert len(plan['packets']) == 5
    note_workflow.prepare(root)
    return root, plan


def payload():
    return {'draft_md': '<!-- section:P001.s01 -->\n## 检查输入\n\n输入数值后检查条件，得到第一步的结果。接着执行第二步。\n\n'
                        '**本章小结**\n\n每步都需要检查输入条件。执行后记录结果。\n',
            'review': {'coverage': [{'source_ranges': [['S000001', 'S000002']], 'status': 'written',
                                      'section_id': 'P001.s01', 'reason': ''}],
                       'visual_requests': [], 'visual_review': 'pending',
                       'visual_review_reason': '只有字幕，尚未核图。',
                       'fact_review': 'complete', 'fact_review_reason': '对照原句，本段没有外部事实纠正。',
                       'corrections': [], 'uncertainties': [], 'boundary_notes': []}}


def complete(root):
    job = root / 'jobs/P001'
    data = payload()
    metadata = note_workflow._json(job / 'job.json')
    note_plan._atomic_text(job / 'draft.md', data['draft_md'])
    note_workflow._save(job / 'review.json', note_results.stamp_review(data['review'], metadata))


def rejects(action):
    try:
        action()
    except ValueError:
        return
    raise AssertionError('Invalid writing result was accepted')


def response(data, **updates):
    result = {'structured_output': data, 'is_error': False, 'num_turns': 1,
              'usage': {'input_tokens': 10, 'output_tokens': 20,
                        'cache_read_input_tokens': 0, 'cache_creation_input_tokens': 0}}
    result.update(updates)
    return subprocess.CompletedProcess([], 0, json.dumps(result, ensure_ascii=False), '')


def test_structured_runner_writes_validated_artifacts_and_fills_only_provenance():
    with tempfile.TemporaryDirectory() as tmp:
        root, _ = fixture(tmp)
        calls = []
        def runner(args, **kwargs):
            calls.append(args)
            assert args[args.index('--tools') + 1] == ''
            assert '--json-schema' in args
            assert '第1步' in kwargs['input'] and '第2步' in kwargs['input']
            return response(payload())
        state = note_workflow.run_one(root, 'jobs/P001', output_mode='structured', runner=runner)
        assert state['status'] == 'produced' and len(calls) == 1
        review = note_workflow._json(root / 'jobs/P001/review.json')
        assert review['source_sha256'] == note_workflow._json(root / 'note_plan.json')['source_sha256']
        assert review['coverage'][0]['packet_id'] == 'P001'
        assert review['visual_review'] == 'pending' and review['uncertainties'] == []


def test_bad_structured_results_retain_usage_without_writing_artifacts():
    for defect in ('shape', 'foreign_range', 'missing_sentence', 'pending_facts', 'unseen_visual', 'empty_summary',
                   'empty_summary_with_boundary', 'generic_correction'):
        with tempfile.TemporaryDirectory() as tmp:
            root, _ = fixture(tmp)
            data = payload()
            if defect == 'shape':
                data['review']['source_sha256'] = 'injected'
            elif defect == 'foreign_range':
                data['review']['coverage'][0]['source_ranges'] = [['S000001', 'S000003']]
            elif defect == 'missing_sentence':
                data['review']['coverage'][0]['source_ranges'] = [['S000001', 'S000001']]
            elif defect == 'pending_facts':
                data['review']['fact_review'] = 'pending'
            elif defect == 'unseen_visual':
                data['review']['visual_review'] = 'complete'
            elif defect == 'empty_summary':
                data['draft_md'] = data['draft_md'].split('**本章小结**')[0]
            elif defect == 'empty_summary_with_boundary':
                data['draft_md'] = data['draft_md'].split('**本章小结**')[0] + '**本章小结**\nTODO\n'
                data['review']['boundary_notes'] = ['尚有下一包内容。']
            else:
                data['review']['corrections'] = [{'kind': 'lecture_error', 'status': 'unresolved',
                    'source_ranges': [['S000001', 'S000001']], 'section_id': 'P001.s01',
                    'original': '输入数值', 'replacement': '', 'display': '**待核实**', 'evidence': []}]
            rejects(lambda: note_workflow.run_one(root, 'jobs/P001', output_mode='structured',
                                                  runner=lambda *a, **k: response(data)))
            job = root / 'jobs/P001'
            assert not (job / 'draft.md').exists() and not (job / 'review.json').exists()
            state = note_workflow._json(job / 'run.json')
            assert state['failure_code'] == 'invalid_structured_output' and state['usage']['output_tokens'] == 20


def test_structured_mode_requires_inline_input_before_invoking_runner():
    with tempfile.TemporaryDirectory() as tmp:
        root, _ = fixture(tmp)
        def forbidden(*args, **kwargs):
            raise AssertionError('Unexpected model invocation')
        rejects(lambda: note_workflow.run_one(root, 'jobs/P001', input_mode='files',
                                              output_mode='structured', runner=forbidden))
        assert not (root / 'jobs/P001/run.json').exists()


def test_conflicting_model_provenance_is_rejected():
    metadata = {'source_sha256': 'abc', 'packet_id': 'P001', 'editorial_policy_version': 1}
    review = payload()['review']
    review['packet_id'] = 'P002'
    rejects(lambda: note_results.stamp_review(review, metadata))
    review.pop('packet_id')
    review['coverage'][0]['packet_id'] = 'P002'
    rejects(lambda: note_results.stamp_review(review, metadata))


def test_cache_needs_source_acceptance_and_closed_chapter():
    with tempfile.TemporaryDirectory() as tmp:
        root, _ = fixture(Path(tmp) / 'source')
        complete(root)
        cache = Path(tmp) / 'cache'
        rejects(lambda: note_workflow.cache_writing(root, 'jobs/P001', cache_dir=cache))
        assert not cache.exists()
        draft = root / 'jobs/P001/draft.md'
        draft.write_text(draft.read_text(encoding='utf-8').split('**本章小结**')[0], encoding='utf-8')
        rejects(lambda: note_workflow.cache_writing(root, 'jobs/P001', cache_dir=cache, accept_source_review=True))
        assert not cache.exists()


def test_distant_source_change_reuses_accepted_packet_and_rebinds_source_sha():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        old, old_plan = fixture(base / 'old')
        complete(old)
        cache = base / 'cache'
        note_workflow.cache_writing(old, 'jobs/P001', cache_dir=cache, accept_source_review=True)
        new, new_plan = fixture(base / 'new', changed=9)
        assert old_plan['source_sha256'] != new_plan['source_sha256']
        def forbidden(*args, **kwargs):
            raise AssertionError('A cache hit invoked the model')
        result = note_workflow.run_one(new, 'jobs/P001', reuse_cache=True, cache_dir=cache, runner=forbidden)
        assert result['status'] == 'reused' and not (new / 'jobs/P001/run.json').exists()
        assert (old / 'jobs/P001/draft.md').read_bytes() == (new / 'jobs/P001/draft.md').read_bytes()
        review = note_workflow._json(new / 'jobs/P001/review.json')
        assert review['source_sha256'] == new_plan['source_sha256'] and review['fact_review'] == 'complete'
        report = note_usage.summarize(new)
        assert report['reused_jobs'] == 1 and report['overall']['attempts'] == 0
        assert report['overall']['tokens']['input_tokens'] is None
        (new / 'jobs/P001/draft.md').write_text('later edit', encoding='utf-8')
        report = note_usage.summarize(new)
        assert report['changed_reused_jobs'] == 1 and report['reuses'][0]['status'] == 'changed_after_reuse'


def test_packet_or_neighbor_change_invalidates_writing_cache():
    for changed in (0, 2):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            old, _ = fixture(base / 'old')
            complete(old)
            cache = base / 'cache'
            note_workflow.cache_writing(old, 'jobs/P001', cache_dir=cache, accept_source_review=True)
            new, _ = fixture(base / 'new', changed=changed)
            assert note_workflow.reuse_writing(new, 'jobs/P001', cache_dir=cache)['status'] == 'cache_miss'
            assert not (new / 'jobs/P001/draft.md').exists()


def test_scope_binds_rules_terms_timing_visuals_and_task_content():
    with tempfile.TemporaryDirectory() as tmp:
        root, _ = fixture(tmp)
        root, plan, target, metadata, packet = note_workflow._writing_context(root, 'jobs/P001')
        baseline = note_workflow._writing_scope(root, plan, target, metadata, packet)
        original = note_cache.digest(baseline)
        for field in ('terms_sha256', 'writer_sha256', 'style_sha256', 'sprite_sha256', 'editorial_policy_version'):
            changed = dict(metadata, **{field: 'different'})
            assert note_cache.digest(note_workflow._writing_scope(root, plan, target, changed, packet)) != original
        changed = copy.deepcopy(plan)
        changed['segment_times']['S000001']['end'] -= .5
        assert note_cache.digest(note_workflow._writing_scope(root, changed, target, metadata, packet)) != original
        task = target / 'task.md'
        task.write_text(task.read_text(encoding='utf-8') + '\n额外要求\n', encoding='utf-8')
        assert note_cache.digest(note_workflow._writing_scope(root, plan, target, metadata, packet)) != original


def test_tampered_cache_or_existing_outputs_are_never_overwritten():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        old, _ = fixture(base / 'old')
        complete(old)
        cache = base / 'cache'
        stored = note_workflow.cache_writing(old, 'jobs/P001', cache_dir=cache, accept_source_review=True)
        new, _ = fixture(base / 'new')
        draft = new / 'jobs/P001/draft.md'
        draft.write_text('human draft', encoding='utf-8')
        rejects(lambda: note_workflow.reuse_writing(new, 'jobs/P001', cache_dir=cache))
        assert draft.read_text(encoding='utf-8') == 'human draft'
        entry = cache / (stored['cache_key'] + '.json')
        record = note_workflow._json(entry)
        record['draft_md'] += 'tampered'
        note_workflow._save(entry, record)
        rejects(lambda: note_workflow.reuse_writing(new, 'jobs/P001', cache_dir=cache))


def test_cache_miss_runs_once_and_failure_does_not_trigger_retry():
    with tempfile.TemporaryDirectory() as tmp:
        root, _ = fixture(Path(tmp) / 'source')
        calls = []
        def runner(*args, **kwargs):
            calls.append(1)
            return response(None, is_error=True, subtype='error_max_turns')
        rejects(lambda: note_workflow.run_one(root, 'jobs/P001', reuse_cache=True,
                                              cache_dir=Path(tmp) / 'cache', runner=runner))
        assert len(calls) == 1
        assert note_usage.summarize(root)['overall']['attempts'] == 1


def test_structured_result_never_replaces_files_created_during_the_call():
    with tempfile.TemporaryDirectory() as tmp:
        root, _ = fixture(tmp)
        draft = root / 'jobs/P001/draft.md'
        def runner(*args, **kwargs):
            draft.write_text('human edit while running', encoding='utf-8')
            return response(payload())
        rejects(lambda: note_workflow.run_one(root, 'jobs/P001', output_mode='structured', runner=runner))
        assert draft.read_text(encoding='utf-8') == 'human edit while running'
        assert not (root / 'jobs/P001/review.json').exists()


def test_cache_operations_reject_a_running_job():
    with tempfile.TemporaryDirectory() as tmp:
        root, _ = fixture(tmp)
        complete(root)
        note_workflow._save(root / 'jobs/P001/run.json', {'status': 'running', 'attempts': 1})
        rejects(lambda: note_workflow.cache_writing(root, 'jobs/P001', accept_source_review=True))
        rejects(lambda: note_workflow.reuse_writing(root, 'jobs/P001'))


def test_corrected_cache_needs_an_explicit_accepted_refresh():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        root, _ = fixture(base / 'old')
        complete(root)
        cache = base / 'cache'
        result = note_workflow.cache_writing(root, 'jobs/P001', cache_dir=cache, accept_source_review=True)
        draft = root / 'jobs/P001/draft.md'
        draft.write_text(draft.read_text(encoding='utf-8').replace('第一步的结果', '本步的结果'), encoding='utf-8')
        rejects(lambda: note_workflow.cache_writing(root, 'jobs/P001', cache_dir=cache, accept_source_review=True))
        note_workflow.cache_writing(root, 'jobs/P001', cache_dir=cache,
                                    accept_source_review=True, replace_cache=True)
        record = note_workflow._json(cache / (result['cache_key'] + '.json'))
        assert record['previous_artifacts_sha256'] != record['artifacts_sha256']
        new, _ = fixture(base / 'new')
        note_workflow.reuse_writing(new, 'jobs/P001', cache_dir=cache)
        assert (new / 'jobs/P001/draft.md').read_bytes() == draft.read_bytes()


def test_reuse_keeps_prior_failed_model_usage_in_the_report():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        old, _ = fixture(base / 'old')
        complete(old)
        cache = base / 'cache'
        note_workflow.cache_writing(old, 'jobs/P001', cache_dir=cache, accept_source_review=True)
        new, _ = fixture(base / 'new')
        failed = {'attempts': 1, 'status': 'failed', 'usage': {'input_tokens': 3, 'output_tokens': 7}, 'usage_known': True}
        note_workflow._save(new / 'jobs/P001/run.json', failed)
        note_workflow.reuse_writing(new, 'jobs/P001', cache_dir=cache)
        assert note_workflow._json(new / 'jobs/P001/run.json') == failed
        report = note_usage.summarize(new)
        assert report['reused_jobs'] == 1 and report['overall']['attempts'] == 1
        assert report['overall']['tokens']['output_tokens'] == 7
        (new / 'jobs/P001/review.json').write_text('incomplete local edit', encoding='utf-8')
        report = note_usage.summarize(new)
        assert report['changed_reused_jobs'] == 1 and report['overall']['tokens']['output_tokens'] == 7
