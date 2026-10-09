"""Fail-closed, per-job exclusion for local model and artifact operations.

Use ``with claim(job_dir):`` around state checks and the complete operation.
Existing locks, including malformed or abandoned ones, require manual review;
this module never guesses whether a PID is alive or retries an operation.
"""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import stat
import uuid


LOCK_NAME = '.note_job.lock'


class JobLockError(ValueError):
    """A fixed, safe error message suitable for status reporting."""


def _directory(job_dir):
    try:
        directory = Path(job_dir).absolute()
        for component in (directory, *directory.parents):
            if component.is_symlink() or (
                    hasattr(component, 'is_junction') and component.is_junction()):
                raise JobLockError('Job lock path cannot contain links')
        if not directory.is_dir():
            raise JobLockError('Job lock requires an existing directory')
        return directory.resolve(strict=True)
    except (OSError, TypeError, ValueError) as exc:
        if isinstance(exc, JobLockError):
            raise
        raise JobLockError('Invalid job lock directory') from None


def _identity(info):
    return info.st_dev, info.st_ino


def _release(directory, nonce, identity):
    """Leave missing, replaced, malformed, or linked locks untouched."""
    try:
        if _directory(directory) != directory:
            return
        path = directory / LOCK_NAME
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or _identity(info) != identity:
            return
        flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
        with os.fdopen(os.open(path, flags), 'rb') as stream:
            if _identity(os.fstat(stream.fileno())) != identity:
                return
            data = stream.read(1025)
        if len(data) > 1024:
            return
        record = json.loads(data)
        if not isinstance(record, dict) or record.get('nonce') != nonce:
            return
        # Check the name again after reading; a replacement does not belong to us.
        current = path.lstat()
        if stat.S_ISREG(current.st_mode) and _identity(current) == identity:
            path.unlink()
    except (FileNotFoundError, JobLockError, ValueError, UnicodeError):
        return
    except OSError:
        raise JobLockError('Job lock could not be released; manual review required') from None


@contextmanager
def claim(job_dir):
    """Atomically claim one existing job directory, yielding its absolute path.

    Keep state reads, cache decisions, model execution, and artifact writes inside
    this context. A pre-existing lock is always rejected, without examining its
    contents. Normal and exceptional exits remove only this claim's own lock.
    """
    directory = _directory(job_dir)
    path = directory / LOCK_NAME
    nonce = uuid.uuid4().hex
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        raise JobLockError('Job is already claimed; existing lock requires manual review') from None
    except OSError:
        raise JobLockError('Job lock could not be created') from None
    try:
        identity = _identity(os.fstat(descriptor))
        with os.fdopen(descriptor, 'w', encoding='utf-8', newline='\n') as stream:
            json.dump({'schema_version': 1, 'nonce': nonce, 'pid': os.getpid()}, stream)
            stream.write('\n')
    except (OSError, ValueError):
        try:
            os.close(descriptor)
        except OSError:
            pass
        # An incomplete lock remains visible; nobody may treat it as available.
        raise JobLockError('Job lock could not be initialized; manual review required') from None
    body_failed = False
    try:
        yield directory
    except BaseException:
        body_failed = True
        raise
    finally:
        try:
            _release(directory, nonce, identity)
        except JobLockError:
            if not body_failed:
                raise
