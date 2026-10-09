"""Measure note input sizes and local planning time without invoking a model.

Optional --tokenizer uses a local tokenizers JSON. Counts exclude CC system/tool
prompts, history, images and billing. The source video directory is read-only.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import statistics
import sys
import time

import note_plan

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass

BASE_DIR = Path(__file__).resolve().parent.parent


def measure(directory, *, tokenizer_path=None, repeats=9):
    if isinstance(repeats, bool) or not isinstance(repeats, int) or not 1 <= repeats <= 50:
        raise ValueError('Repeat count must be 1..50')
    root = Path(directory).resolve()
    source = (root / 'transcript.json').read_bytes()
    sprite_path = root / 'sprite_index.json'
    sprite_bytes = sprite_path.read_bytes() if sprite_path.exists() else None
    transcript = json.loads(source)
    sprite = json.loads(sprite_bytes) if sprite_bytes else None
    tokenizer = None
    tokenizer_sha = None
    if tokenizer_path:
        from tokenizers import Tokenizer
        tokenizer_path = Path(tokenizer_path)
        tokenizer_sha = hashlib.sha256(tokenizer_path.read_bytes()).hexdigest()
        tokenizer = Tokenizer.from_file(str(tokenizer_path))

    def size(text):
        return {'chars': len(text), 'estimated_tokens':
                len(tokenizer.encode(text, add_special_tokens=False).ids) if tokenizer else None}

    plan = note_plan.build_plan(transcript, sprite)  # warm-up, excluded from timings
    timings = []
    for _ in range(repeats):
        started = time.perf_counter()
        plan = note_plan.build_plan(transcript, sprite)
        timings.append(time.perf_counter() - started)
    segments = note_plan._normalized_segments(transcript)
    texts = {mode: [note_plan.render_packet(p, segments, detail=mode) for p in plan['packets']]
             for mode in ('full', 'compact', 'lean')}
    rules = {name: (BASE_DIR / 'prompts' / name).read_text(encoding='utf-8-sig')
             for name in ('stitcher_system.md', 'writer_system.md')}

    def total(material):
        rows = [size(text) for text in material]
        return {'chars': sum(row['chars'] for row in rows), 'estimated_tokens':
                sum(row['estimated_tokens'] for row in rows) if tokenizer else None}

    return {'schema_version': 1, 'video_id': transcript.get('video_id') or root.name,
            'source_sha256': hashlib.sha256(source).hexdigest(),
            'sprite_sha256': hashlib.sha256(sprite_bytes).hexdigest() if sprite_bytes else None,
            'tokenizer_sha256': tokenizer_sha, 'segment_count': plan['segment_count'],
            'packets': len(plan['packets']),
            'candidate_entries': sum(len(p['visual_candidates']) for p in plan['packets']),
            'build_plan': {'median_seconds': statistics.median(timings), 'timings_seconds': timings},
            'packet_modes': {mode: total(material) for mode, material in texts.items()},
            'writing_rules': {name: size(text) for name, text in rules.items()},
            'packet_and_rules': {
                'compact_stitcher': total([rules['stitcher_system.md'] + '\n\n' + text for text in texts['compact']]),
                'compact_writer': total([rules['writer_system.md'] + '\n\n' + text for text in texts['compact']]),
                'lean_writer': total([rules['writer_system.md'] + '\n\n' + text for text in texts['lean']])},
            'limitations': ['Token counts estimate text with the supplied tokenizer, not provider billing.',
                            'Packet-and-rules excludes task/template/terms, CC system/tool prompts, history and images.',
                            'Local planning time does not measure model, network, ASR or capture latency.']}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('video_id')
    parser.add_argument('--out', type=Path, help='Read source VIDEO_DIR')
    parser.add_argument('--tokenizer', type=Path, help='Optional local tokenizer.json; no download')
    parser.add_argument('--report', type=Path, help='Save measurement JSON outside the source directory')
    parser.add_argument('--repeats', type=int, default=9)
    args = parser.parse_args(argv)
    if not re.fullmatch(r'[A-Za-z0-9_-]+', args.video_id) or args.video_id in ('.', '..'):
        parser.error('Invalid video ID')
    root = (args.out or BASE_DIR / 'output' / args.video_id).resolve()
    try:
        if args.report and args.report.resolve().is_relative_to(root):
            raise ValueError('Benchmark report must be outside the source video directory')
        report = measure(root, tokenizer_path=args.tokenizer, repeats=args.repeats)
        if args.report:
            note_plan._atomic_text(args.report.resolve(), json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    except (OSError, ValueError, ImportError) as exc:
        print(f'note_benchmark: {type(exc).__name__}; cannot measure input', file=sys.stderr)
        return 1
    print(json.dumps({'packets': report['packets'], 'segment_count': report['segment_count'],
                      'packet_modes': report['packet_modes'], 'packet_and_rules': report['packet_and_rules'],
                      'build_median_seconds': report['build_plan']['median_seconds']}, ensure_ascii=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
