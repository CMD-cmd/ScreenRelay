"""Bounded, in-process recovery from disk space and quota exhaustion.

Reports are deliberately in-memory IPC payloads; this module never writes status
or logs while under pressure. All filesystem writes stay inside ``directory``.
"""
import errno
import os
from pathlib import Path
import stat
import tempfile
import time


_MIB = 1024 * 1024
_STORAGE_ERRNOS = {errno.ENOSPC, errno.EDQUOT}


def is_storage_error(exc):
    """Recognize explicit exhaustion errors, including wrapped exceptions."""
    seen = set()
    pending = [exc]
    while pending:
        error = pending.pop()
        if error is None or id(error) in seen:
            continue
        seen.add(id(error))
        if getattr(error, 'errno', None) in _STORAGE_ERRNOS:
            return True
        message = str(error).lower()
        if 'no space left' in message or 'disk quota exceeded' in message:
            return True
        pending.extend((getattr(error, '__cause__', None),
                        getattr(error, '__context__', None)))
    return False


def _reason(exc):
    seen = set()
    pending = [exc]
    while pending:
        error = pending.pop()
        if error is None or id(error) in seen:
            continue
        seen.add(id(error))
        if (getattr(error, 'errno', None) == errno.EDQUOT or
                'disk quota exceeded' in str(error).lower()):
            return 'quota'
        pending.extend((getattr(error, '__cause__', None),
                        getattr(error, '__context__', None)))
    return 'space'


class StorageRecovery:
    BACKOFF = (5, 10, 20, 30, 60)
    HEARTBEAT_SECONDS = 30

    def __init__(self, directory, reporter=lambda payload: None,
                 clock=time.monotonic, disk_free=None, reserve_bytes=8 * _MIB,
                 enter_bytes=256 * _MIB, resume_bytes=512 * _MIB):
        self.directory = Path(directory)
        self.reporter = reporter
        self.clock = clock
        self.disk_free = disk_free
        self.reserve_bytes = reserve_bytes
        self.enter_bytes = enter_bytes
        self.resume_bytes = resume_bytes
        self.active = False
        self.retry_at = 0.0
        self.attempts = 0
        self._reason = 'space'
        self._last_report = None
        self._reserve_identity = None
        self._recovery_pending = False
        self._mode = 'STARTING'
        # A previous process may have left its allocation behind. Inspection
        # is read-only and independent of current free space.
        try:
            info = os.lstat(self._reserve_path)
            if self._valid_reserve(info):
                self._reserve_identity = self._identity(info)
        except OSError:
            pass

    @property
    def _reserve_path(self):
        return self.directory / '.storage-emergency.reserve'

    def _free(self):
        try:
            if self.disk_free is not None:
                return max(0, int(self.disk_free()))
            existing = self.directory
            while not existing.exists() and existing != existing.parent:
                existing = existing.parent
            info = os.statvfs(existing)
            return max(0, int(info.f_bavail * info.f_frsize))
        except (OSError, ValueError, TypeError, OverflowError):
            return None

    def low_space(self):
        free = self._free()
        return free is not None and free < self.enter_bytes

    def _valid_reserve(self, info):
        return (stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and
                stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1 and
                info.st_size == self.reserve_bytes and
                getattr(info, 'st_blocks', 0) * 512 >= self.reserve_bytes)

    @staticmethod
    def _identity(info):
        return info.st_dev, info.st_ino

    def ensure_reserve(self):
        """Best-effort allocation; never replaces an unsafe existing path."""
        free = self._free()
        if self.active or free is None or free < self.resume_bytes + self.reserve_bytes:
            return False
        fd = None
        created = False
        identity = None
        try:
            try:
                info = os.lstat(self._reserve_path)
            except FileNotFoundError:
                fd = os.open(self._reserve_path,
                             os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                             getattr(os, 'O_NOFOLLOW', 0), 0o600)
                created = True
                identity = self._identity(os.fstat(fd))
                os.fchmod(fd, 0o600)
                chunk = b'\0' * min(1024 * 1024, self.reserve_bytes)
                remaining = self.reserve_bytes
                while remaining:
                    written = os.write(fd, chunk[:min(len(chunk), remaining)])
                    if written <= 0:
                        raise OSError(errno.EIO, 'Short reserve write')
                    remaining -= written
                os.fsync(fd)
                info = os.fstat(fd)
            if not self._valid_reserve(info):
                return False
            self._reserve_identity = self._identity(info)
            return True
        except OSError:
            return False
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if created and self._reserve_identity != identity:
                self._unlink_if_owned(self._reserve_path, identity)

    def _unlink_if_owned(self, path, identity):
        try:
            info = os.lstat(path)
            if (identity is not None and self._identity(info) == identity and
                    stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()):
                os.unlink(path)
                return True
        except OSError:
            pass
        return False

    def release_reserve(self):
        removed = self._unlink_if_owned(self._reserve_path, self._reserve_identity)
        self._reserve_identity = None
        return removed

    def _schedule(self):
        delay = self.BACKOFF[min(self.attempts, len(self.BACKOFF) - 1)]
        self.attempts += 1
        self.retry_at = self.clock() + delay

    def _report(self, phase, force=False):
        now = self.clock()
        if not force and self._last_report is not None and now - self._last_report < self.HEARTBEAT_SECONDS:
            return
        self._last_report = now
        payload = dict(version=1, pid=os.getpid(), phase=phase,
                       free_bytes=self._free(),
                       retry_after=max(0.0, self.retry_at - now) if self.active else 0.0,
                       reason=self._reason,
                       recovery_pending=self._recovery_pending if self.active else False,
                       mode=self._mode)
        try:
            self.reporter(payload)
        except Exception:
            pass

    def activate(self, exc, recovery_pending=False, mode='STARTING'):
        if self.active:
            self.failed(exc, recovery_pending, mode)
            return
        self.active = True
        self.attempts = 0
        self._reason = _reason(exc)
        self._recovery_pending = bool(recovery_pending)
        self._mode = str(mode)
        self.release_reserve()
        self._schedule()
        self._report('pressure', force=True)

    def heartbeat(self, recovery_pending=False, mode='STARTING', force=False):
        if not self.active:
            return
        self._recovery_pending = bool(recovery_pending)
        self._mode = str(mode)
        self._report('pressure', force=force)

    def ready(self):
        if not self.active or self.clock() < self.retry_at:
            return False
        free = self._free()
        if free is None or free < self.resume_bytes:
            self._schedule()
            return False
        fd = None
        path = None
        identity = None
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(prefix='.storage-probe-', dir=self.directory)
            path = Path(name)
            identity = self._identity(os.fstat(fd))
            data = b'\0' * 4096
            while data:
                written = os.write(fd, data)
                if written <= 0:
                    raise OSError(errno.EIO, 'Short probe write')
                data = data[written:]
            os.fsync(fd)
            os.close(fd)
            fd = None
            # A successful probe includes cleanup, so a failed unlink cannot
            # result in a false healthy signal or accumulating retry files.
            if not self._unlink_if_owned(path, identity):
                raise OSError(errno.EIO, 'Cannot remove storage probe')
            return True
        except OSError as exc:
            self.failed(exc, self._recovery_pending, self._mode)
            return False
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if path is not None:
                self._unlink_if_owned(path, identity)

    def failed(self, exc, recovery_pending=False, mode='STARTING'):
        if not self.active:
            self.activate(exc, recovery_pending, mode)
            return
        reason = _reason(exc)
        changed = reason != self._reason
        self._reason = reason
        self._recovery_pending = bool(recovery_pending)
        self._mode = str(mode)
        self.release_reserve()
        self._schedule()
        self._report('pressure', force=changed)

    def clear(self):
        if not self.active:
            return
        self.active = False
        self.retry_at = 0.0
        self.attempts = 0
        self._recovery_pending = False
        self._report('healthy', force=True)

    def next_timeout(self):
        # A past deadline still yields a positive wait if a caller uses this
        # directly without calling ready() first.
        return max(0.01, self.retry_at - self.clock()) if self.active else 30.0
