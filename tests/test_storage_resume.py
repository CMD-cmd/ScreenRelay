"""A recovered disk must clear only its own notice, without unpausing users."""
import copy
import errno
import unittest
from unittest.mock import patch

import test_disk_full_runtime as disk

c = disk.c


class StorageResumeTests(unittest.TestCase):
    setUp = disk.DiskFullRuntimeTests.setUp
    sleep = disk.DiskFullRuntimeTests.sleep
    run_controller = disk.DiskFullRuntimeTests.run_controller
    connected = disk.DiskFullRuntimeTests.connected

    def seed_waiting(self, enabled=True, storage_notice=False):
        engine = self.real_engine(self.bd, self.directory)
        engine.state.update(mode='ARMED' if enabled else 'PAUSED',
                            desired_enabled=enabled, last_command='old-enable')
        if storage_notice:
            reason = '磁盘空间不足，暂停新切屏并保留恢复记录'
            engine.state.update(last_error=reason, recovery_context=dict(
                category='storage_pressure', reason=reason,
                automatic_resume=enabled, timestamp=self.now - 100))
        engine._save()
        self.write(self.directory / 'signal.json', dict(
            timestamp=self.now, state='disconnected', trusted=True, sleeping=False))
        return engine

    def assert_clean_notice(self, enabled=True):
        for name in ('state.json', 'status.json'):
            saved = c.read_json(self.directory / name)
            self.assertIsNone(saved['last_error'])
            self.assertNotEqual(saved.get('recovery_context', {}).get('category'), 'storage_pressure')
            self.assertIsNone(saved['journal'])
            self.assertEqual(saved['desired_enabled'], enabled)
            self.assertEqual(saved['mode'], 'ARMED' if enabled else 'PAUSED')

    def test_same_process_pressure_clears_standby_notice_without_journal(self):
        self.seed_waiting()
        self.free = 128 * 1024 * 1024
        self.on_wait = lambda: setattr(self, 'free', 1024 * 1024 * 1024)
        self.run_controller()
        self.assert_clean_notice()
        self.assertEqual(self.bd.calls, [])
        self.assertEqual(len(self.engines), 2)  # Seed plus the sole runtime owner.
        self.assertEqual(self.reports[-1]['phase'], 'healthy')

    def test_restart_revalidates_then_clears_persisted_standby_notice(self):
        self.seed_waiting(storage_notice=True)
        self.run_controller()
        self.assert_clean_notice()
        self.assertEqual([r['phase'] for r in self.reports], ['pressure', 'healthy'])
        self.assertEqual(self.bd.calls, [])

    def test_restart_waits_for_resume_threshold_before_clearing(self):
        self.seed_waiting(storage_notice=True)
        self.free = 400 * 1024 * 1024  # Above entry threshold, below resume threshold.

        def waiting():
            self.assertEqual(self.guard.active, True)
            self.assertEqual(self.reports[-1]['phase'], 'pressure')
            self.assertEqual(c.read_json(self.directory / 'state.json')
                             ['recovery_context']['category'], 'storage_pressure')
            self.assertEqual(self.bd.calls, [])
            self.free = 1024 * 1024 * 1024

        self.on_wait = waiting
        self.run_controller()
        self.assertTrue(self.waits)
        self.assert_clean_notice()

    def test_successful_free_space_check_cannot_bypass_failed_probe(self):
        self.seed_waiting(storage_notice=True)
        probes = []
        write = disk.os.write

        def write_probe(fd, data):
            if len(data) == 4096 and self.guard.active and not probes:
                probes.append(True)
                raise OSError(errno.EDQUOT, 'injected probe quota')
            return write(fd, data)

        with patch('storage_recovery.os.write', write_probe):
            self.run_controller()
        self.assertTrue(probes)
        self.assertGreaterEqual(len(self.waits), 2)
        self.assert_clean_notice()

    def test_failed_status_commit_cannot_announce_healthy(self):
        self.seed_waiting(storage_notice=True)
        failures = []

        def writing(path, value):
            if (path.name == 'status.json' and value.get('last_error') is None
                    and not failures):
                failures.append(True)
                raise OSError(errno.ENOSPC, 'injected completion status failure')
            return self.write(path, value)

        def waiting():
            self.assertNotIn('healthy', [r['phase'] for r in self.reports])

        self.on_wait = waiting
        with patch.object(c, 'atomic_json', writing):
            self.run_controller()
        self.assertTrue(failures)
        self.assertGreaterEqual(len(self.waits), 2)
        self.assert_clean_notice()
        self.assertEqual(self.reports[-1]['phase'], 'healthy')

    def test_recovery_does_not_enable_a_paused_user(self):
        self.seed_waiting(enabled=False, storage_notice=True)
        self.run_controller()
        self.assert_clean_notice(enabled=False)
        self.assertEqual(self.bd.calls, [])

    def test_other_safety_faults_are_retained(self):
        engine = self.seed_waiting(enabled=False)
        reason = 'Interrupted restart budget reached'
        context = dict(category='restart_budget', reason=reason,
                       automatic_resume=False, timestamp=self.now)
        engine.state.update(last_error=reason, recovery_context=context)
        engine._save()
        self.run_controller()
        saved = c.read_json(self.directory / 'state.json')
        self.assertEqual(saved['recovery_context'], context)
        self.assertEqual(saved['last_error'], reason)
        self.assertFalse(saved['desired_enabled'])
        self.assertEqual(self.reports, [])

    def test_distinct_restore_error_is_not_erased_by_stale_storage_context(self):
        engine = self.seed_waiting(enabled=False, storage_notice=True)
        engine.state['last_error'] = 'Detached display layout verification failed'
        engine._save()
        original = copy.deepcopy(engine.state)
        self.run_controller()
        saved = c.read_json(self.directory / 'state.json')
        self.assertEqual(saved['last_error'], original['last_error'])
        self.assertEqual(saved['recovery_context'], original['recovery_context'])
        self.assertEqual(self.reports, [])


if __name__ == '__main__':
    unittest.main()
