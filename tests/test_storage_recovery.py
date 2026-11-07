import errno
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import storage_recovery as storage

MIB = 1024 * 1024


class StorageRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.now = 100.0
        self.free = 1024 * MIB
        self.reports = []
        self.guard = storage.StorageRecovery(
            self.directory, self.reports.append, lambda: self.now,
            lambda: self.free)
        self.error = OSError(errno.ENOSPC, 'full')

    def due(self):
        self.now = self.guard.retry_at

    def test_only_storage_errors_and_explicit_text(self):
        for code in (errno.ENOSPC, errno.EDQUOT):
            self.assertTrue(storage.is_storage_error(OSError(code, 'error')))
        for code in (errno.EACCES, errno.EIO, errno.ENOENT):
            self.assertFalse(storage.is_storage_error(OSError(code, 'error')))
        self.assertFalse(storage.is_storage_error(RuntimeError('disk is full')))
        self.assertTrue(storage.is_storage_error(RuntimeError('No space left on device')))
        self.assertTrue(storage.is_storage_error(RuntimeError('Disk quota exceeded')))
        wrapper = RuntimeError('write failed')
        wrapper.__cause__ = self.error
        self.error.__context__ = wrapper
        self.assertTrue(storage.is_storage_error(wrapper))

    def test_real_reserve_is_private_allocated_and_idempotent(self):
        self.assertTrue(self.guard.ensure_reserve())
        path = self.directory / '.storage-emergency.reserve'
        info = path.stat()
        self.assertEqual(info.st_size, 8 * MIB)
        self.assertEqual(stat.S_IMODE(info.st_mode), 0o600)
        self.assertGreaterEqual(info.st_blocks * 512, info.st_size)
        self.assertTrue(self.guard.ensure_reserve())
        self.assertEqual(path.stat().st_ino, info.st_ino)
        self.guard.activate(self.error)
        self.assertFalse(path.exists())
        self.assertFalse(self.guard.ensure_reserve())

    def test_reserve_rejects_symlink_other_owner_and_nonregular(self):
        target = self.directory / 'unrelated'
        target.write_text('keep')
        path = self.directory / '.storage-emergency.reserve'
        path.symlink_to(target)
        self.assertFalse(self.guard.ensure_reserve())
        self.assertFalse(self.guard.release_reserve())
        self.assertTrue(path.is_symlink())
        self.assertEqual(target.read_text(), 'keep')
        path.unlink()
        path.mkdir()
        self.assertFalse(self.guard.ensure_reserve())
        self.assertFalse(self.guard.release_reserve())
        path.rmdir()
        self.assertTrue(self.guard.ensure_reserve())
        with patch.object(storage.os, 'getuid', return_value=os.getuid() + 1):
            self.assertFalse(self.guard.ensure_reserve())
            self.assertFalse(self.guard.release_reserve())
        self.assertTrue(path.exists())

    def test_reserve_does_not_remove_replaced_or_unrecognized_file(self):
        path = self.directory / '.storage-emergency.reserve'
        path.write_text('keep')
        self.assertFalse(self.guard.ensure_reserve())
        self.guard.activate(self.error)
        self.assertEqual(path.read_text(), 'keep')
        self.guard.clear()
        path.unlink()
        self.assertTrue(self.guard.ensure_reserve())
        path.rename(self.directory / 'old-reserve')
        path.write_text('replacement')
        self.assertFalse(self.guard.release_reserve())
        self.assertEqual(path.read_text(), 'replacement')

    def test_sparse_reserve_and_hardlinks_are_not_adopted(self):
        path = self.directory / '.storage-emergency.reserve'
        with path.open('wb') as stream:
            stream.truncate(8 * MIB)
        path.chmod(0o600)
        self.assertFalse(self.guard.ensure_reserve())
        self.assertFalse(self.guard.release_reserve())
        self.assertTrue(path.exists())
        path.unlink()
        self.assertTrue(self.guard.ensure_reserve())
        other = self.directory / 'hardlink'
        os.link(path, other)
        fresh = storage.StorageRecovery(self.directory, disk_free=lambda: self.free)
        self.assertFalse(fresh.ensure_reserve())
        self.assertFalse(fresh.release_reserve())
        self.assertTrue(other.exists())

    def test_heartbeat_only_reads_disk_and_never_creates_files(self):
        self.guard.activate(self.error)
        self.now += 30
        with patch.object(storage.os, 'open', side_effect=AssertionError('write')):
            with patch.object(storage.tempfile, 'mkstemp', side_effect=AssertionError('probe')):
                self.guard.heartbeat()
        self.assertEqual(list(self.directory.iterdir()), [])
        self.assertEqual(len(self.reports), 2)

    def test_valid_reserve_can_be_adopted_after_restart(self):
        self.assertTrue(self.guard.ensure_reserve())
        other = storage.StorageRecovery(self.directory, disk_free=lambda: self.free)
        self.assertTrue(other.ensure_reserve())
        self.assertTrue(other.release_reserve())

    def test_low_space_restart_adopts_and_releases_previous_reserve(self):
        self.assertTrue(self.guard.ensure_reserve())
        path = self.directory / '.storage-emergency.reserve'
        self.free = 0
        restarted = storage.StorageRecovery(self.directory, disk_free=lambda: self.free)
        self.assertFalse(restarted.ensure_reserve())
        self.assertTrue(path.exists())
        restarted.activate(self.error)
        self.assertFalse(path.exists())
        self.assertTrue(restarted.active)

    def test_constructor_does_not_adopt_symlink_or_unrelated_file(self):
        target = self.directory / 'unrelated'
        target.write_text('keep')
        path = self.directory / '.storage-emergency.reserve'
        path.symlink_to(target)
        restarted = storage.StorageRecovery(self.directory, disk_free=lambda: 0)
        restarted.activate(self.error)
        self.assertTrue(path.is_symlink())
        self.assertEqual(target.read_text(), 'keep')
        path.unlink()
        path.write_text('keep too')
        restarted = storage.StorageRecovery(self.directory, disk_free=lambda: 0)
        restarted.activate(self.error)
        self.assertEqual(path.read_text(), 'keep too')

    def test_reserve_threshold_and_partial_failure_cleanup(self):
        self.free = 520 * MIB - 1
        self.assertFalse(self.guard.ensure_reserve())
        self.free += 1
        with patch.object(storage.os, 'write', side_effect=self.error):
            self.assertFalse(self.guard.ensure_reserve())
        self.assertEqual(list(self.directory.iterdir()), [])
        self.assertTrue(self.guard.ensure_reserve())

    def test_low_space_and_recovery_hysteresis(self):
        self.free = 256 * MIB
        self.assertFalse(self.guard.low_space())
        self.free -= 1
        self.assertTrue(self.guard.low_space())
        self.guard.activate(self.error)
        self.assertEqual(self.guard.retry_at, 105)
        self.assertFalse(self.guard.ready())
        self.due()
        self.free = 512 * MIB - 1
        self.assertFalse(self.guard.ready())
        self.free += 1
        with patch.object(storage.tempfile, 'mkstemp', wraps=storage.tempfile.mkstemp) as probe:
            self.assertFalse(self.guard.ready())
            probe.assert_not_called()
            self.due()
            self.assertTrue(self.guard.ready())
            self.assertEqual(probe.call_count, 1)
        self.assertEqual(list(self.directory.iterdir()), [])
        self.assertTrue(self.guard.active)

    def test_probe_write_fsync_and_creation_failures(self):
        for operation in ('write', 'fsync'):
            with self.subTest(operation=operation):
                self.guard.activate(self.error)
                self.due()
                with patch.object(storage.os, operation, side_effect=self.error):
                    self.assertFalse(self.guard.ready())
                self.assertTrue(self.guard.active)
                self.assertGreater(self.guard.retry_at, self.now)
                self.assertEqual(list(self.directory.iterdir()), [])
                self.guard.clear()
        self.guard.activate(self.error)
        self.due()
        with patch.object(storage.tempfile, 'mkstemp', side_effect=self.error):
            self.assertFalse(self.guard.ready())
        self.assertGreater(self.guard.retry_at, self.now)

    def test_backoff_caps_and_does_not_poll_early(self):
        self.free = 0
        self.guard.activate(self.error)
        self.assertEqual(self.guard.next_timeout(), 5)
        for delay in (10, 20, 30, 60, 60, 60):
            self.due()
            self.assertGreater(self.guard.next_timeout(), 0)
            self.assertFalse(self.guard.ready())
            self.assertEqual(self.guard.next_timeout(), delay)
            deadline = self.guard.retry_at
            attempts = self.guard.attempts
            for _ in range(20):
                self.assertFalse(self.guard.ready())
            self.assertEqual(self.guard.retry_at, deadline)
            self.assertEqual(self.guard.attempts, attempts)

    def test_ipc_schema_throttle_reason_change_and_healthy(self):
        self.guard.activate(self.error, recovery_pending=True, mode='RECOVERING')
        self.assertEqual(len(self.reports), 1)
        expected = {'version', 'pid', 'phase', 'free_bytes', 'retry_after',
                    'reason', 'recovery_pending', 'mode'}
        self.assertEqual(set(self.reports[0]), expected)
        self.assertEqual(self.reports[0]['pid'], os.getpid())
        self.assertEqual(self.reports[0]['retry_after'], 5)
        self.assertTrue(self.reports[0]['recovery_pending'])
        for _ in range(10):
            self.guard.heartbeat()
        self.assertEqual(len(self.reports), 1)
        self.now += 29.9
        self.guard.heartbeat()
        self.assertEqual(len(self.reports), 1)
        self.now += 0.1
        self.guard.heartbeat(recovery_pending=True, mode='RECOVERING')
        self.assertEqual(len(self.reports), 2)
        self.assertEqual(self.reports[-1]['retry_after'], 0)
        self.guard.failed(OSError(errno.EDQUOT, 'quota'))
        self.assertEqual(self.reports[-1]['reason'], 'quota')
        self.assertEqual(len(self.reports), 3)
        self.guard.clear()
        self.assertEqual(self.reports[-1]['phase'], 'healthy')
        self.assertFalse(self.reports[-1]['recovery_pending'])
        self.assertEqual(self.reports[-1]['retry_after'], 0)
        self.guard.heartbeat(force=True)
        self.assertEqual(len(self.reports), 4)

    def test_ipc_failure_and_unavailable_stat_do_not_kill_process(self):
        def unavailable():
            raise OSError(errno.EIO, 'stat failure')
        self.guard.reporter = lambda payload: (_ for _ in ()).throw(RuntimeError('IPC down'))
        self.guard.disk_free = unavailable
        self.assertFalse(self.guard.low_space())
        self.assertFalse(self.guard.ensure_reserve())
        self.guard.activate(self.error)
        self.due()
        self.assertFalse(self.guard.ready())
        self.guard.heartbeat(force=True)
        self.guard.clear()
        self.assertFalse(self.guard.active)

    def test_same_process_resumes_after_successful_probe(self):
        original_pid = os.getpid()
        self.free = 0
        self.guard.activate(self.error, recovery_pending=True)
        for _ in range(4):
            self.due()
            self.assertFalse(self.guard.ready())
        self.free = 1024 * MIB
        self.due()
        self.assertTrue(self.guard.ready())
        self.guard.clear()
        self.assertEqual(os.getpid(), original_pid)
        self.assertFalse(self.guard.active)
        self.assertEqual(self.guard.attempts, 0)
        self.assertTrue(self.guard.ensure_reserve())

    def test_default_free_uses_available_statvfs_bytes(self):
        guard = storage.StorageRecovery(self.directory)
        info = type('Info', (), {'f_bavail': 7, 'f_frsize': 4096})()
        with patch.object(storage.os, 'statvfs', return_value=info):
            self.assertEqual(guard._free(), 7 * 4096)


if __name__ == '__main__':
    unittest.main()
