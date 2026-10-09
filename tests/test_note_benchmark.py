"""Benchmark the actual source contract, keeping source artifacts read-only."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))
import note_benchmark


def test_measure_preserves_source_and_candidates_without_running_jobs():
    with tempfile.TemporaryDirectory(prefix='vs_benchmark_') as temporary:
        root = Path(temporary)
        source = {'video_id': 'BVbench', 'duration': 30, 'segments': [
            {'start': 0, 'end': 15, 'text': '先输入两组数据。'},
            {'start': 15, 'end': 30, 'text': '再运行并验证结果。'}]}
        sprite = {'sheets': [], 'candidates': [
            {'id': 'C1', 'time': '00:00:10', 'since': 0, 'until': 30}]}
        (root / 'transcript.json').write_text(json.dumps(source), encoding='utf-8')
        (root / 'sprite_index.json').write_text(json.dumps(sprite), encoding='utf-8')
        (root / 'book.md').write_text('人工正文', encoding='utf-8')
        original = {path.name: path.read_bytes() for path in root.iterdir()}
        result = note_benchmark.measure(root, repeats=2)
        assert result['segment_count'] == 2 and result['candidate_entries'] == 1
        assert result['source_sha256'] == hashlib.sha256(original['transcript.json']).hexdigest()
        assert all(v['estimated_tokens'] is None for v in result['packet_modes'].values())
        assert result['packet_modes']['compact']['chars'] > result['packet_modes']['lean']['chars']
        assert {path.name: path.read_bytes() for path in root.iterdir()} == original


def test_cli_refuses_to_write_a_report_over_source_material():
    with tempfile.TemporaryDirectory(prefix='vs_benchmark_path_') as temporary:
        source = Path(temporary) / 'transcript.json'
        source.write_text('protected', encoding='utf-8')
        assert note_benchmark.main(['BVbench', '--out', temporary, '--report', str(source)]) == 1
        assert source.read_text(encoding='utf-8') == 'protected'
