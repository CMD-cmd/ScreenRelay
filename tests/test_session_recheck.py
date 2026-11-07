import json
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from session_recheck import SessionRecheck


class SessionRecheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.now = 1000.0
        self.runner = Mock(return_value=self.result(self.probe()))
        self.recheck = SessionRecheck(self.directory, '/authorized/native',
                                      clock=lambda: self.now, runner=self.runner)

    def original(self, **fields):
        return dict({'state': 'unknown', 'trusted': True, 'overlay': False,
                     'controllers': [], 'reason': 'uu_windows_unavailable',
                     'sleeping': False, 'timestamp': self.now, 'event': 'heartbeat',
                     'run_id': 'native-run', 'event_id': 41,
                     'display_revision': 7, 'detector_pid': 123}, **fields)

    def probe(self, **fields):
        return dict({'state': 'connected', 'trusted': True, 'overlay': False,
                     'controllers': ['test-controller'], 'reason': 'ax_controller_row',
                     'sleeping': False, 'timestamp': self.now, 'run_id': 'probe-run',
                     'event_id': 0, 'display_revision': 0, 'detector_pid': 999}, **fields)

    @staticmethod
    def result(value, returncode=0):
        return types.SimpleNamespace(stdout=json.dumps(value), returncode=returncode)

    def test_normal_states_never_probe_or_write(self):
        for state in ('connected', 'disconnected'):
            original = self.original(state=state)
            self.assertIs(self.recheck.resolve(original), original)
            self.assertIsNone(self.recheck.next_timeout())
        self.runner.assert_not_called()
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_explicit_command_refreshes_old_connected_observation_once(self):
        original = self.original(state='connected', session_observed_at=self.now-200)
        self.runner.return_value = self.result(self.probe(session_observed_at=self.now, activeControllerCount=1))
        fresh = self.recheck.refresh_for_command(original)
        self.assertEqual(fresh['session_observed_at'],self.now)
        self.assertEqual(fresh['run_id'],original['run_id'])
        self.assertEqual(self.runner.call_count,1)
        self.assertIsNone(self.recheck.next_timeout())

    def test_failed_command_refresh_does_not_schedule_background_retries(self):
        original = self.original(state='connected')
        self.runner.side_effect = subprocess.TimeoutExpired('probe',3)
        self.assertIs(self.recheck.refresh_for_command(original),original)
        self.assertEqual(self.runner.call_count,1)
        self.assertIsNone(self.recheck.next_timeout())

    def test_verified_connection_preserves_native_event_metadata(self):
        original = self.original()
        merged = self.recheck.resolve(original)
        self.assertEqual(merged['state'], 'connected')
        for key in ('run_id', 'event_id', 'display_revision', 'detector_pid'):
            self.assertEqual(merged[key], original[key])
        self.assertEqual(merged['probePID'], 999)
        self.assertEqual(merged['event'], 'session')
        self.assertEqual(merged['session_verification']['attempts'], 1)
        self.assertEqual(original['state'], 'unknown')
        self.runner.assert_called_once_with(['/authorized/native', '--probe'],
                                           timeout=3, check=False,
                                           capture_output=True, text=True)
        self.assertIsNone(self.recheck.next_timeout())
        diagnostic = json.loads((self.directory / 'session-recheck.json').read_text())
        self.assertEqual(diagnostic['last_result'], 'verified')
        self.assertNotIn('test-controller', json.dumps(diagnostic))
        self.assertFalse((self.directory / 'signal.json').exists())

    def test_native_disconnected_overrides_cached_connection_and_clears_episode(self):
        self.recheck.resolve(self.original())
        original = self.original(state='disconnected')
        self.assertIs(self.recheck.resolve(original), original)
        self.recheck.resolve(self.original())
        self.assertEqual(self.runner.call_count, 2)

    def test_probe_disconnected_is_honored(self):
        self.runner.return_value = self.result(self.probe(state='disconnected', controllers=[]))
        self.assertEqual(self.recheck.resolve(self.original())['state'], 'disconnected')

    def test_unsafe_original_never_probed_or_overridden(self):
        cases = ({'trusted': False}, {'trusted': 1}, {'sleeping': True},
                 {'timestamp': self.now - 90}, {'timestamp': self.now + 1},
                 {'timestamp': None}, {'timestamp': '1000'}, {'timestamp': True},
                 {'timestamp': float('nan')}, {'reason': 'accessibility_permission_required'})
        for fields in cases:
            with self.subTest(fields=fields):
                recheck = SessionRecheck(self.directory, '/native', lambda: self.now, self.runner)
                original = self.original(**fields)
                self.assertIs(recheck.resolve(original), original)
                self.assertIsNone(recheck.next_timeout())
        self.runner.assert_not_called()
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_safety_change_discards_existing_verification(self):
        for fields in ({'trusted': False}, {'sleeping': True}, {'timestamp': 900}):
            with self.subTest(fields=fields):
                recheck = SessionRecheck(self.directory, '/native', lambda: self.now, self.runner)
                recheck.resolve(self.original())
                original = self.original(**fields)
                self.assertIs(recheck.resolve(original), original)
                self.assertIsNone(recheck.next_timeout())
                # Returning trust cannot resurrect the discarded snapshot.
                self.assertEqual(recheck.resolve(self.original())['state'], 'unknown')

    def test_cached_timestamp_is_never_renewed_and_expires(self):
        first = self.recheck.resolve(self.original())
        diagnostic = (self.directory / 'session-recheck.json').read_bytes()
        self.now += 89
        cached = self.recheck.resolve(self.original(event_id=50, display_revision=8))
        self.assertEqual(cached['timestamp'], first['timestamp'])
        self.assertEqual(cached['session_verification'], first['session_verification'])
        self.assertEqual(cached['display_revision'], 8)
        self.assertEqual(cached['event_id'], 50)
        self.assertEqual((self.directory / 'session-recheck.json').read_bytes(), diagnostic)
        self.now += 1
        original = self.original()
        self.assertIs(self.recheck.resolve(original), original)
        self.assertIsNone(self.recheck.next_timeout())
        self.runner.assert_called_once()

    def test_three_attempts_and_finite_deadlines(self):
        self.runner.side_effect = subprocess.TimeoutExpired('probe', 3)
        self.recheck.resolve(self.original())
        self.assertEqual(self.recheck.next_timeout(), .75)
        self.now += .5
        self.recheck.resolve(self.original())
        self.assertEqual(self.runner.call_count, 1)
        self.assertEqual(self.recheck.next_timeout(), .25)
        self.now += .25
        self.recheck.resolve(self.original())
        self.assertEqual(self.runner.call_count, 2)
        self.assertEqual(self.recheck.next_timeout(), 2)
        self.now += 2
        self.assertEqual(self.recheck.next_timeout(), 0)
        self.recheck.resolve(self.original())
        self.assertEqual(self.runner.call_count, 3)
        self.assertIsNone(self.recheck.next_timeout())
        self.now += 100
        self.recheck.resolve(self.original())
        self.assertEqual(self.runner.call_count, 3)
        diagnostic = json.loads((self.directory / 'session-recheck.json').read_text())
        self.assertEqual(diagnostic['attempts'], 3)
        self.assertEqual(diagnostic['last_error'], 'timeout')

    def test_probe_validation_and_runner_failures_are_contained(self):
        invalid = [None, [], self.probe(timestamp=self.now + .1),
                   self.probe(timestamp=self.now - 3), self.probe(timestamp='1000'),
                   self.probe(timestamp=float('nan')), self.probe(trusted=False),
                   self.probe(sleeping=True), self.probe(state='unknown'),
                   self.probe(controllers='invalid'), self.probe(overlay=None)]
        for value in invalid:
            with self.subTest(value=value):
                self.runner.return_value = self.result(value)
                recheck = SessionRecheck(self.directory, '/native', lambda: self.now, self.runner)
                original = self.original()
                self.assertIs(recheck.resolve(original), original)
                self.assertEqual(recheck.next_timeout(), .75)
        for outcome in (types.SimpleNamespace(stdout='{bad', returncode=0),
                        types.SimpleNamespace(stdout='{}', returncode=7), OSError('private')):
            with self.subTest(outcome=outcome):
                runner = Mock(side_effect=outcome) if isinstance(outcome, Exception) else Mock(return_value=outcome)
                recheck = SessionRecheck(self.directory, '/native', lambda: self.now, runner)
                original = self.original()
                self.assertIs(recheck.resolve(original), original)
                self.assertNotIn('private', (self.directory / 'session-recheck.json').read_text())

    def test_run_id_and_reason_reset_episode(self):
        self.runner.return_value = self.result(self.probe(state='unknown'))
        self.recheck.resolve(self.original())
        self.recheck.resolve(self.original(run_id='new-native-run'))
        self.recheck.resolve(self.original(run_id='new-native-run', reason='ax_snapshot_incomplete'))
        self.assertEqual(self.runner.call_count, 3)
        diagnostic = json.loads((self.directory / 'session-recheck.json').read_text())
        self.assertEqual(diagnostic['attempts'], 1)

    def test_unsafe_original_cancels_pending_wakeup(self):
        self.runner.side_effect = subprocess.TimeoutExpired('probe', 3)
        self.recheck.resolve(self.original())
        self.recheck.resolve(self.original(trusted=False))
        self.assertIsNone(self.recheck.next_timeout())
        self.assertEqual(self.runner.call_count, 1)

    def test_original_expiring_during_probe_is_not_overridden(self):
        def delayed_probe(*args, **kwargs):
            self.now += 2
            return self.result(self.probe())
        self.runner.side_effect = delayed_probe
        original = self.original(timestamp=self.now - 89)
        self.assertIs(self.recheck.resolve(original), original)
        self.assertIsNone(self.recheck.next_timeout())


if __name__ == '__main__':
    unittest.main()
