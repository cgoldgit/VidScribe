"""Content-addressed writing artifacts, accepted only after a source review."""
import copy
import hashlib
import json
from pathlib import Path

import note_plan

VERSION = 1


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode('utf-8')).hexdigest()


def _entry(directory, key):
    if not isinstance(key, str) or len(key) != 64 or any(c not in '0123456789abcdef' for c in key):
        raise ValueError('Invalid writing cache key')
    directory = Path(directory)
    if directory.is_symlink():
        raise ValueError('Cache directory cannot be a symbolic link')
    root = directory.resolve()
    path = root / (key + '.json')
    if path.is_symlink() or not path.resolve().is_relative_to(root):
        raise ValueError('Cache entry escapes its directory')
    return path


def store(directory, scope, draft, review, *, accepted=False, replace_accepted=False):
    if accepted is not True:
        raise ValueError('Writing cache requires an explicit review against source sentences')
    key = digest(scope)
    path = _entry(directory, key)
    record = {'schema_version': VERSION, 'key': key, 'scope': scope,
              'source_review_accepted': True, 'draft_md': draft, 'review': review,
              'artifacts_sha256': digest({'draft_md': draft, 'review': review})}
    if path.exists():
        previous = load(directory, scope)
        # A corrected draft can supersede an accepted one only with an explicit refresh.
        if previous['artifacts_sha256'] != record['artifacts_sha256']:
            if replace_accepted is not True:
                raise ValueError('An accepted writing cache entry already exists; review before replacing it')
            record['previous_artifacts_sha256'] = previous['artifacts_sha256']
        else:
            return key
    path.parent.mkdir(parents=True, exist_ok=True)
    note_plan._atomic_text(path, json.dumps(record, ensure_ascii=False, indent=2) + '\n')
    return key


def load(directory, scope):
    key = digest(scope)
    path = _entry(directory, key)
    if not path.exists():
        return None
    record = json.loads(path.read_text(encoding='utf-8-sig'))
    if (not isinstance(record, dict) or record.get('schema_version') != VERSION
            or record.get('key') != key or record.get('scope') != scope
            or record.get('source_review_accepted') is not True
            or not isinstance(record.get('draft_md'), str) or not isinstance(record.get('review'), dict)
            or record.get('artifacts_sha256') != digest(
                {'draft_md': record['draft_md'], 'review': record['review']})):
        raise ValueError('Writing cache integrity failed')
    return copy.deepcopy(record)
