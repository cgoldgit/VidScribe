"""Per-job exclusion without model invocation or automatic stale-lock removal."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

SRC = Path(__file__).resolve().parents[1] / 'src'
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
import note_job_lock


def rejects(directory):
    try:
        with note_job_lock.claim(directory):
            raise AssertionError('A conflicting claim was accepted')
    except note_job_lock.JobLockError as exc:
        return str(exc)
    raise AssertionError('Invalid claim was accepted')


def test_same_job_is_exclusive_and_normal_exit_releases_it():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        with note_job_lock.claim(root) as claimed:
            assert claimed == root.resolve()
            record = json.loads((root / note_job_lock.LOCK_NAME).read_text(encoding='utf-8'))
            assert set(record) == {'schema_version', 'nonce', 'pid'}
            assert record['schema_version'] == 1 and len(record['nonce']) == 32
            rejects(root)
        assert not (root / note_job_lock.LOCK_NAME).exists()
        with note_job_lock.claim(root):
            pass


def test_different_jobs_do_not_block_each_other():
    with tempfile.TemporaryDirectory() as temporary:
        first, second = Path(temporary) / 'P001', Path(temporary) / 'P002'
        first.mkdir()
        second.mkdir()
        with note_job_lock.claim(first), note_job_lock.claim(second):
            assert (first / note_job_lock.LOCK_NAME).exists()
            assert (second / note_job_lock.LOCK_NAME).exists()
        assert not (first / note_job_lock.LOCK_NAME).exists()
        assert not (second / note_job_lock.LOCK_NAME).exists()


def test_exception_releases_owned_lock_without_hiding_body_exception():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        try:
            with note_job_lock.claim(root):
                raise RuntimeError('controlled body failure')
        except RuntimeError as exc:
            assert str(exc) == 'controlled body failure'
        else:
            raise AssertionError('Body exception was swallowed')
        assert not (root / note_job_lock.LOCK_NAME).exists()


def test_replaced_lock_is_not_removed_on_exit():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        path = root / note_job_lock.LOCK_NAME
        replacement = {'schema_version': 1, 'nonce': 'other-owner', 'pid': 0}
        with note_job_lock.claim(root):
            path.unlink()
            path.write_text(json.dumps(replacement), encoding='utf-8')
        assert json.loads(path.read_text(encoding='utf-8')) == replacement
        rejects(root)


def test_edited_nonce_does_not_remove_another_owners_lock():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        path = root / note_job_lock.LOCK_NAME
        with note_job_lock.claim(root):
            record = json.loads(path.read_text(encoding='utf-8'))
            record['nonce'] = 'replacement-on-same-inode'
            path.write_text(json.dumps(record), encoding='utf-8')
        assert path.exists()
        assert json.loads(path.read_text(encoding='utf-8'))['nonce'] == 'replacement-on-same-inode'


def test_abandoned_or_invalid_locks_are_rejected_without_reading_their_contents():
    for content in ('', 'INVALID_PRIVATE_LOCK_DATA', json.dumps({'nonce': 'old', 'pid': -1})):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / note_job_lock.LOCK_NAME
            path.write_text(content, encoding='utf-8')
            with patch.object(Path, 'read_text', side_effect=AssertionError('Existing lock was read')):
                message = rejects(root)
            assert 'INVALID_PRIVATE_LOCK_DATA' not in message and str(root) not in message
            assert path.read_text(encoding='utf-8') == content


def test_another_thread_cannot_claim_the_active_job():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        ready, release = threading.Event(), threading.Event()
        errors = []
        def hold():
            try:
                with note_job_lock.claim(root):
                    ready.set()
                    if not release.wait(5):
                        raise AssertionError('Test did not release holder')
            except BaseException as exc:
                errors.append(exc)
        thread = threading.Thread(target=hold)
        thread.start()
        try:
            assert ready.wait(5)
            rejects(root)
        finally:
            release.set()
            thread.join(5)
        assert not thread.is_alive() and not errors
        assert not (root / note_job_lock.LOCK_NAME).exists()


def test_another_process_cannot_claim_the_active_job():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        script = ("import sys; sys.path.insert(0, sys.argv[1]); import note_job_lock\n"
                  "try:\n"
                  "    with note_job_lock.claim(sys.argv[2]):\n"
                  "        print('claimed')\n"
                  "except note_job_lock.JobLockError:\n"
                  "    print('blocked')\n")
        arguments = [sys.executable, '-c', script, str(SRC), str(root)]
        with note_job_lock.claim(root):
            result = subprocess.run(arguments, capture_output=True, text=True, timeout=10)
            assert result.returncode == 0 and result.stdout.strip() == 'blocked', result.stderr
        result = subprocess.run(arguments, capture_output=True, text=True, timeout=10)
        assert result.returncode == 0 and result.stdout.strip() == 'claimed', result.stderr


def test_missing_directory_and_non_directory_are_rejected_without_creation():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        missing = root / 'missing'
        rejects(missing)
        assert not missing.exists()
        file = root / 'file'
        file.write_text('not a directory', encoding='utf-8')
        rejects(file)
        assert file.read_text(encoding='utf-8') == 'not a directory'


def test_linked_ancestor_is_rejected_without_symlink_privileges():
    with tempfile.TemporaryDirectory() as temporary:
        parent = Path(temporary)
        root = parent / 'P001'
        root.mkdir()
        def linked(path):
            return path == parent
        with patch.object(Path, 'is_symlink', linked):
            rejects(root)
        assert not (root / note_job_lock.LOCK_NAME).exists()


def test_windows_junction_is_rejected_without_creating_one():
    if not hasattr(Path, 'is_junction'):
        return
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        with patch.object(Path, 'is_junction', return_value=True):
            rejects(root)
        assert not (root / note_job_lock.LOCK_NAME).exists()


def test_initialization_failure_leaves_a_blocking_lock_and_safe_error():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        with patch.object(note_job_lock.json, 'dump', side_effect=OSError('PRIVATE_ERROR_DATA')):
            message = rejects(root)
        assert 'PRIVATE_ERROR_DATA' not in message and str(root) not in message
        assert (root / note_job_lock.LOCK_NAME).exists()
        rejects(root)


def test_new_file_with_copied_nonce_is_not_removed():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        path = root / note_job_lock.LOCK_NAME
        replacement = root / 'replacement'
        with note_job_lock.claim(root):
            replacement.write_bytes(path.read_bytes())
            before = path.stat().st_ino
            os.replace(replacement, path)
            assert path.stat().st_ino != before
        assert path.exists()


def test_linked_job_path_is_rejected():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        job, link = root / 'actual', root / 'linked'
        job.mkdir()
        try:
            link.symlink_to(job, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            raise unittest.SkipTest(f'Directory symlinks unavailable: {type(exc).__name__}')
        rejects(link)
        nested = job / 'nested'
        nested.mkdir()
        rejects(link / 'nested')
        assert not (job / note_job_lock.LOCK_NAME).exists()
        assert not (nested / note_job_lock.LOCK_NAME).exists()


def test_symlink_lock_is_rejected_without_touching_target():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        target = root / 'private'
        target.write_text('PRIVATE_TARGET_DATA', encoding='utf-8')
        path = root / note_job_lock.LOCK_NAME
        try:
            path.symlink_to(target)
        except (OSError, NotImplementedError) as exc:
            raise unittest.SkipTest(f'File symlinks unavailable: {type(exc).__name__}')
        assert 'PRIVATE_TARGET_DATA' not in rejects(root)
        assert path.is_symlink()
        assert target.read_text(encoding='utf-8') == 'PRIVATE_TARGET_DATA'
