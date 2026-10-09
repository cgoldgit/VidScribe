"""Local writing candidates, without raw model responses or automatic acceptance.

An immutable manifest records initial hashes. Controllers may edit the two
artifacts and must validate the current content before publishing it.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import stat

import note_cache
import note_results


VERSION = 1
_MANIFEST_FIELDS = frozenset({
    'schema_version', 'attempt', 'metadata_sha256', 'draft_sha256',
    'review_sha256', 'artifacts_sha256',
})
_PROVENANCE_FIELDS = frozenset({'source_sha256', 'packet_id', 'editorial_policy_version'})


def _reject_links(path):
    for component in (path, *path.parents):
        if component.is_symlink() or (
                hasattr(component, 'is_junction') and component.is_junction()):
            raise ValueError('Candidate path cannot contain links')


def _root(job_dir):
    directory = Path(job_dir).absolute()
    if '..' in directory.parts:
        raise ValueError('Candidate path cannot contain parent traversal')
    _reject_links(directory)
    if not directory.is_dir():
        raise ValueError('Candidate requires an existing job directory')
    return directory.resolve(strict=True)


def _path(root, relative):
    path = root / relative
    _reject_links(path)
    if not path.resolve().is_relative_to(root):
        raise ValueError('Candidate path escapes the job directory')
    return path


def _relative(attempt):
    if (isinstance(attempt, bool) or not isinstance(attempt, int)
            or not 1 <= attempt <= 999999):
        raise ValueError('Invalid candidate attempt')
    return f'candidates/A{attempt:03d}'


def _metadata_digest(metadata):
    if not isinstance(metadata, dict):
        raise ValueError('Candidate metadata must be an object')
    return note_cache.digest(metadata)


def _check_stage_review(draft, review):
    if not isinstance(review, dict):
        raise ValueError('Candidate review must be an object')
    # decode() has already checked the transport schema and added provenance.
    # Check the same whitelist defensively, without changing caller data.
    checked = {key: value for key, value in review.items() if key not in _PROVENANCE_FIELDS}
    for key in _PROVENANCE_FIELDS & review.keys():
        value = review[key]
        if (key == 'editorial_policy_version' and (isinstance(value, bool) or not isinstance(value, int))
                or key != 'editorial_policy_version' and not isinstance(value, str)):
            raise ValueError('Invalid candidate provenance type')
    coverage = checked.get('coverage')
    if isinstance(coverage, list):
        checked['coverage'] = []
        for item in coverage:
            if not isinstance(item, dict):
                checked['coverage'].append(item)
                continue
            if 'packet_id' in item and not isinstance(item['packet_id'], str):
                raise ValueError('Invalid candidate coverage provenance type')
            checked['coverage'].append({key: value for key, value in item.items() if key != 'packet_id'})
    note_results._shape({'draft_md': draft, 'review': checked}, note_results.writing_schema())


def _write_bytes(root, relative, content):
    path = _path(root, relative)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0)
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, 'wb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError('Candidate artifact must be a regular file')
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    _path(root, relative)


def _read_bytes(root, relative):
    path = _path(root, relative)
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise ValueError('Candidate artifact must be a regular file')
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, 'rb') as stream:
        opened = os.fstat(stream.fileno())
        if (not stat.S_ISREG(opened.st_mode)
                or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)):
            raise ValueError('Candidate artifact changed while being opened')
        content = stream.read()
    after = _path(root, relative).lstat()
    if (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino):
        raise ValueError('Candidate artifact changed while being read')
    return content


def stage(job_dir, attempt, metadata, draft, review):
    """Save one schema-checked candidate; never replace an existing attempt."""
    try:
        relative = _relative(attempt)
        metadata_sha256 = _metadata_digest(metadata)
        _check_stage_review(draft, review)
        draft_bytes = draft.encode('utf-8')
        review_bytes = (json.dumps(review, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
        manifest = {
            'schema_version': VERSION, 'attempt': attempt, 'metadata_sha256': metadata_sha256,
            'draft_sha256': hashlib.sha256(draft_bytes).hexdigest(),
            'review_sha256': note_cache.digest(review),
            'artifacts_sha256': note_cache.digest({'draft_md': draft, 'review': review}),
        }
        manifest_bytes = (json.dumps(manifest, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
        root = _root(job_dir)
        _path(root, 'candidates').mkdir(exist_ok=True)
        candidate = _path(root, relative)
        candidate.mkdir(exist_ok=False)
        _write_bytes(root, relative + '/draft.md', draft_bytes)
        _write_bytes(root, relative + '/review.json', review_bytes)
        # A partially written candidate is never loadable as a completed one.
        _write_bytes(root, relative + '/manifest.json', manifest_bytes)
        return relative
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
        raise ValueError('Unable to stage writing candidate') from None


def load(job_dir, attempt, metadata):
    """Load current local content and initial hashes; schema/quality need review."""
    try:
        relative = _relative(attempt)
        metadata_sha256 = _metadata_digest(metadata)
        root = _root(job_dir)
        candidate = _path(root, relative)
        if not candidate.is_dir():
            raise ValueError('Candidate directory is missing')
        manifest = json.loads(_read_bytes(root, relative + '/manifest.json').decode('utf-8-sig'))
        if (not isinstance(manifest, dict) or set(manifest) != _MANIFEST_FIELDS
                or isinstance(manifest['schema_version'], bool)
                or not isinstance(manifest['schema_version'], int) or manifest['schema_version'] != VERSION
                or isinstance(manifest['attempt'], bool) or not isinstance(manifest['attempt'], int)
                or manifest['attempt'] != attempt
                or any(not isinstance(manifest[key], str)
                       or re.fullmatch(r'[0-9a-f]{64}', manifest[key]) is None
                       for key in _MANIFEST_FIELDS - {'schema_version', 'attempt'})
                or manifest['metadata_sha256'] != metadata_sha256):
            raise ValueError('Invalid candidate manifest')
        draft = _read_bytes(root, relative + '/draft.md').decode('utf-8-sig')
        review = json.loads(_read_bytes(root, relative + '/review.json').decode('utf-8-sig'))
        if not isinstance(review, dict):
            raise ValueError('Candidate review must be an object')
        return draft, review, manifest
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
        raise ValueError('Unable to load writing candidate') from None
