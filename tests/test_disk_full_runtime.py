"""Fault-injected daemon tests: no helpers, real displays, or disk filling."""
import copy
import errno
import fcntl
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import controller as c
from storage_recovery import StorageRecovery
from test_controller import FakeBD, display, target


class DiskFullRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / 'runtime'
        self.directory.mkdir()
        self.now = time.time()
        self.free = 1024 * 1024 * 1024
        self.reports, self.waits, self.engines, self.ticks = [], [], [], []
        self.handlers = {}
        self.bd = FakeBD([display(), target()])
        self.bd.directory = self.directory
        self.args = SimpleNamespace(directory=self.directory, binary='fake', event_driven=False, once=True)
        self.on_wait = lambda: None
        self.on_tick = lambda: None
        self.write = c.atomic_json
        self.real_engine = c.Engine
        self.real_wake = c.WakeFIFO
        owner = self

        class Worker:
            def __init__(self, *args, **kwargs):
                pass
            def sync(self, active):
                pass
            def close(self):
                return {}

        class Verifier(Worker):
            def resolve(self, detector):
                return detector
            def next_timeout(self):
                return None

        class Policy(Worker):
            def evaluate(self, detector, mode):
                return detector
            def needs_refresh(self, *args):
                return False

        class Coordinator:
            error = None
            def __init__(self, engine):
                self.engine = engine
            def publish(self, detector):
                pass
            def tick(self, detector, command):
                owner.ticks.append((copy.deepcopy(self.engine.state), copy.deepcopy(command)))
                self.engine.tick(detector, command)
                owner.on_tick()
                return detector
            def _restore_cursor_projection(self, value):
                pass

        class Wake(self.real_wake):
            def wait(self, timeout):
                super().wait(0)
                owner.sleep(timeout)
                super().wait(0)

        for target_name, value in (
            ('controller.BetterDisplay', lambda *a: self.bd),
            ('controller.Engine', self.real_engine),
            ('controller.WakeFIFO', Wake),
            ('cursor_profile.CursorProfileWorker', Worker),
            ('office_cursor_relay.OfficeCursorRelay', Worker),
            ('session_recheck.SessionRecheck', Verifier),
            ('session_policy.SessionPolicy', Policy),
            ('profile_runtime.DeviceCoordinator', Coordinator),
            ('controller.signals.signal', lambda number, handler: self.handlers.setdefault(number, handler)),
        ):
            p = patch(target_name, value)
            p.start()
            self.addCleanup(p.stop)
        # Keep the real class's static command validator available to run_controller.
        original_init = self.real_engine.__init__
        def initialize(value, *args, **kwargs):
            # Display-settling time advances the fake clock; storage/retry waits
            # alone drive on_wait and the fault-injection backoff assertions.
            kwargs.update(clock=lambda: self.now, monotonic=lambda: self.now,
                          sleeper=lambda seconds: setattr(self, 'now', self.now + seconds))
            original_init(value, *args, **kwargs)
            self.engines.append(value)
        p = patch.object(self.real_engine, '__init__', initialize)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(setattr, c, '_runtime_logging', c._runtime_logging)
        self.addCleanup(setattr, c, '_runtime_directory', c._runtime_directory)

    def sleep(self, seconds):
        self.waits.append(seconds)
        self.now += seconds
        self.on_wait()
        self.assertLess(len(self.waits), 100, 'controller spins without bounded retry/stop')

    def run_controller(self):
        def factory(directory, reporter):
            self.guard = StorageRecovery(directory, reporter, lambda: self.now,
                                         lambda: self.free, reserve_bytes=4096)
            return self.guard
        c.run_controller(self.args, storage_factory=factory,
                         reporter=self.reports.append, sleeper=self.sleep)

    def connected(self):
        return dict(timestamp=self.now, state='connected', trusted=True,
                    sleeping=False, session_observed_at=self.now)

    def seed_active(self):
        engine = self.real_engine(self.bd, self.directory)
        engine.tick(self.connected(), {'id': 'old-enable', 'action': 'enable'})
        self.assertEqual(engine.state['mode'], 'ACTIVE')
        self.write(self.directory / 'signal.json', self.connected())
        self.bd.calls.clear()
        self.waits.clear()
        return copy.deepcopy(engine.state['journal'])

    def assert_same_process(self):
        self.assertEqual({r['pid'] for r in self.reports}, {os.getpid()})
        self.assertEqual(self.reports[-1]['phase'], 'healthy')
        self.assertIn('pressure', [r['phase'] for r in self.reports])

    def test_initial_state_write_exhaustion_waits_then_recovers_same_owner(self):
        failures = []
        def write(path, value):
            if Path(path).name == 'state.json' and not self.waits:
                failures.append(path)
                raise OSError(errno.ENOSPC, 'injected')
            return self.write(path, value)
        def waiting():
            with (self.directory / 'controller.lock').open('a') as lock:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.on_wait = waiting
        with patch.object(c, 'atomic_json', write):
            self.run_controller()
        self.assertTrue(failures)
        self.assertEqual(self.waits, [5])
        self.assertEqual(len(self.engines), 1)
        self.assert_same_process()

    def test_initial_directory_creation_exhaustion_retries(self):
        self.args.directory = self.directory / 'new'
        real_mkdir = Path.mkdir
        def mkdir(path, *a, **kw):
            if path == self.args.directory and not self.waits:
                raise OSError(errno.ENOSPC, 'injected mkdir')
            return real_mkdir(path, *a, **kw)
        with patch.object(Path, 'mkdir', mkdir):
            self.run_controller()
        self.assertTrue((self.args.directory / 'state.json').exists())
        self.assertEqual(self.waits, [5])
        self.assert_same_process()

    def runtime_write_failure(self, filename, code):
        journal = self.seed_active()
        armed = [False]
        def write(path, value):
            if Path(path).name == 'effective-session.json':
                armed[0] = True
            if Path(path).name in (filename, 'state.json') and armed[0] and not self.waits:
                raise OSError(code, 'injected runtime write')
            return self.write(path, value)
        def waiting():
            saved = c.read_json(self.directory / 'state.json')
            self.assertEqual(saved['journal']['physical'], journal['physical'])
            self.assertTrue(any(d['connected'] for d in self.bd.devices))
        self.on_wait = waiting
        with patch.object(c, 'atomic_json', write):
            self.run_controller()
        self.assert_same_process()
        self.assertEqual(self.reports[0]['reason'], 'quota' if code == errno.EDQUOT else 'space')
        self.assertIsNone(c.read_json(self.directory / 'state.json')['journal'])

    def test_runtime_state_enospc_preserves_journal(self):
        self.runtime_write_failure('state.json', errno.ENOSPC)

    def test_runtime_effective_session_enospc_preserves_journal(self):
        self.runtime_write_failure('effective-session.json', errno.ENOSPC)

    def test_runtime_effective_session_edquot_preserves_journal(self):
        self.runtime_write_failure('effective-session.json', errno.EDQUOT)

    def test_low_space_blocks_a_new_matching_display_transaction(self):
        self.write(self.directory / 'signal.json', self.connected())
        self.write(self.directory / 'command.json', {'id': 'enable', 'action': 'enable'})
        self.free = 128 * 1024 * 1024
        def waiting():
            self.assertEqual(self.bd.calls, [])
            self.assertEqual(self.ticks, [])
            self.free = 1024 * 1024 * 1024
            self.write(self.directory / 'signal.json', self.connected())
        self.on_wait = waiting
        self.run_controller()
        self.assertTrue(self.bd.calls)
        self.assert_same_process()

    def test_recovery_finishes_old_journal_before_new_matching_entry(self):
        journal = self.seed_active()
        self.free = 0
        self.args.once = False
        def waiting():
            self.free = 1024 * 1024 * 1024
            self.write(self.directory / 'signal.json', self.connected())
        def ticked():
            if len(self.ticks) == 2:
                self.assertEqual(self.engines[-1].state['mode'], 'ACTIVE')
                self.handlers[signal.SIGTERM](signal.SIGTERM, None)
        self.on_wait, self.on_tick = waiting, ticked
        self.run_controller()
        self.assertEqual(len(self.ticks), 2)
        before, _ = self.ticks[0]
        self.assertIsNone(before['journal'])
        self.assertEqual(before['mode'], 'ARMED')
        self.assertEqual(self.ticks[1][0]['journal']['physical'], journal['physical'])
        self.assertEqual(self.ticks[1][0]['mode'], 'ACTIVE')
        restore = next(i for i, (identity, fields) in enumerate(self.bd.calls)
                       if identity == 'physical1' and fields.get('connected') is True)
        entry = next(i for i, (identity, fields) in enumerate(self.bd.calls)
                     if identity == c.TARGET_UUID and fields.get('connected') is True)
        self.assertLess(restore, entry)
        self.assertTrue(any(d['connected'] for d in self.bd.devices))

    def test_persistent_write_failure_backs_off_and_signal_stops_without_restart(self):
        attempts = []
        def write(path, value):
            if Path(path).name == 'state.json':
                attempts.append(self.now)
                raise OSError(errno.ENOSPC, 'persistent')
            return self.write(path, value)
        def waiting():
            if self.now - started >= 125:
                self.handlers[signal.SIGTERM](signal.SIGTERM, None)
        self.on_wait = waiting
        started = self.now
        with patch.object(c, 'atomic_json', write):
            self.run_controller()
        unique = sorted(set(attempts))
        self.assertEqual([b-a for a, b in zip(unique, unique[1:])], [5, 10, 20, 30, 60])
        self.assertEqual(len(self.engines), 1)
        self.assertTrue(all(r['phase'] == 'pressure' for r in self.reports))

    def test_non_space_initial_and_runtime_errors_propagate(self):
        for filename in ('state.json', 'effective-session.json'):
            with self.subTest(filename=filename):
                def write(path, value):
                    if Path(path).name == filename:
                        raise OSError(errno.EACCES, 'permission denied')
                    return self.write(path, value)
                with patch.object(c, 'atomic_json', write), self.assertRaises(OSError) as raised:
                    self.run_controller()
                self.assertEqual(raised.exception.errno, errno.EACCES)
                self.assertEqual(self.waits, [])

    def manual_under_pressure(self, action, newer=False):
        self.seed_active()
        self.args.event_driven = True
        self.write(self.directory / 'command.json', {'id': 'old-enable', 'action': 'enable'})
        manual = {'id': 'fifo-manual', 'action': action}
        def write(path, value):
            if Path(path).name in ('effective-session.json', 'state.json', 'command.json') and not writable[0]:
                if Path(path).name == 'effective-session.json':
                    blocked[0] = True
                if blocked[0]:
                    raise OSError(errno.ENOSPC, 'injected')
            return self.write(path, value)
        writable, blocked = [False], [False]
        def waiting():
            self.assertTrue((self.directory / 'wake.fifo').exists())
            self.assertTrue(c.wake_controller(self.directory, manual))
            writable[0] = True
            if newer:
                self.write(self.directory / 'command.json', {'id': 'new-enable', 'action': 'enable'})
        self.on_wait = waiting
        with patch.object(c, 'atomic_json', write):
            self.run_controller()
        return manual

    def test_fifo_pause_during_full_disk_is_persisted_and_old_enable_not_replayed(self):
        manual = self.manual_under_pressure('pause')
        self.assertEqual(c.read_json(self.directory / 'command.json'), manual)
        self.assertEqual(self.engines[-1].state['mode'], 'PAUSED')
        self.assertFalse(self.engines[-1].state['desired_enabled'])
        self.assert_same_process()

    def test_fifo_recover_during_full_disk_restores_to_paused(self):
        manual = self.manual_under_pressure('recover')
        self.assertEqual(c.read_json(self.directory / 'command.json'), manual)
        self.assertEqual(self.engines[-1].state['mode'], 'PAUSED')
        self.assertIsNone(self.engines[-1].state['journal'])

    def test_newer_durable_command_takes_precedence_over_fifo_fallback(self):
        self.manual_under_pressure('pause', newer=True)
        self.assertEqual(c.read_json(self.directory / 'command.json')['id'], 'new-enable')
        self.assertEqual(self.engines[-1].state['last_command'], 'new-enable')
        self.assertTrue(self.engines[-1].state['desired_enabled'])

    def test_recovery_probe_cannot_bypass_unwritable_critical_journal(self):
        self.bd.groups = [{'tag': 'layout', 'enabled': True}]
        self.args.once = False
        self.write(self.directory / 'signal.json', self.connected())
        self.write(self.directory / 'command.json', {'id': 'enable', 'action': 'enable'})
        rejected = []
        def write(path, value):
            if Path(path).name == 'state.json' and value.get('journal'):
                rejected.append(self.now)
                raise OSError(errno.ENOSPC, 'critical journal cannot be committed')
            return self.write(path, value)
        def waiting():
            self.write(self.directory / 'signal.json', self.connected())
            if len(self.waits) >= 3:
                self.handlers[signal.SIGTERM](signal.SIGTERM, None)
        self.on_wait = waiting
        with patch.object(c, 'atomic_json', write):
            self.run_controller()
        self.assertGreaterEqual(len(rejected), 3)
        self.assertEqual(self.bd.calls, [])
        self.assertEqual(self.bd.group_calls, [])
        self.assertIsNone(c.read_json(self.directory / 'state.json')['journal'])
        self.assertTrue(self.guard.active)

    def test_fifo_accepts_only_bounded_manual_commands(self):
        fifo = self.real_wake(self.directory)
        self.addCleanup(fifo.close)
        for command in ({'id': 'enable', 'action': 'enable'},
                        {'id': 'profile', 'action': 'profile_apply'},
                        {'id': 'x' * 201, 'action': 'pause'}, [],
                        {'id': 1, 'action': 'recover'}):
            self.assertTrue(c.wake_controller(self.directory, command))
            fifo.wait(0)
            self.assertIsNone(fifo.take_manual_command())
        oversized = b'\nUU_DISPLAY_COMMAND ' + json.dumps(
            {'id': 'x' * 20000, 'action': 'pause'}).encode() + b'\n'
        for offset in range(0, len(oversized), 2048):
            os.write(fifo.fd, oversized[offset:offset + 2048])
            fifo.wait(0)
        self.assertIsNone(fifo.take_manual_command())
        os.write(fifo.fd, b'\nUU_DISPLAY_COMMAND {broken}\n')
        fifo.wait(0)
        self.assertIsNone(fifo.take_manual_command())
        for action in ('pause', 'recover'):
            command = {'id': action + 'x' * (200 - len(action)), 'action': action}
            self.assertTrue(c.wake_controller(self.directory, command))
            fifo.wait(0)
            self.assertEqual(fifo.take_manual_command(), {**command, 'previous_command_id': None})
            self.assertIsNone(fifo.take_manual_command())


if __name__ == '__main__':
    unittest.main()
