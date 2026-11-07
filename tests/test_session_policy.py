import copy
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from session_policy import SessionPolicy
from test_controller import FakeBD, display, target, c


class SessionPolicyTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.policy = SessionPolicy(lambda: self.now)

    def raw(self, **changes):
        value = dict(timestamp=self.now, session_observed_at=self.now,
                     state='connected', trusted=True, sleeping=False,
                     run_id='run1', detector_pid=11, uuPID=22,
                     reason='ax_controller_row', overlay=False, controllers=[], display_revision=1,
                     event_id=1, event='session', ui_hidden=False,
                     ui_minimized=False, ui_observable=True,
                     session_negative_verified=False)
        value.update(changes)
        return value

    def connect(self):
        result = self.policy.evaluate(self.raw(), 'ARMED')
        self.assertEqual(result['state'], 'connected')

    def test_repeated_armed_accepts_same_fresh_observation(self):
        raw = self.raw()
        for _ in range(3):
            result = self.policy.evaluate(raw, 'ARMED')
            self.assertEqual(result['state'], 'connected')
            self.assertFalse(result['session_held'])
            self.assertEqual(result['session_source'], 'native_observation')
        self.now += 89
        self.assertEqual(self.policy.evaluate(self.raw(session_observed_at=1000), 'ARMED')['state'], 'connected')
        self.now += 1
        self.assertEqual(self.policy.evaluate(self.raw(session_observed_at=1000), 'ARMED')['state'], 'unknown')

    def test_leaving_active_blocks_same_fresh_observation(self):
        self.connect()
        self.policy.evaluate(self.raw(), 'ACTIVE')
        for _ in range(3):
            self.assertEqual(self.policy.evaluate(self.raw(), 'ARMED')['state'], 'unknown')
        self.now += .01
        self.assertEqual(self.policy.evaluate(self.raw(), 'ARMED')['state'], 'connected')

    def test_armed_invalidation_still_blocks_observation_replay(self):
        for change in ({'trusted': False}, {'sleeping': True},
                       {'timestamp': 910}, {'state': 'unknown', 'reason': 'native_failure'},
                       {'state': 'unknown', 'reason': 'uu_windows_unavailable'},
                       {'state': 'disconnected', 'reason': 'ax_cached_controller_table_empty',
                        'session_negative_verified': True}):
            with self.subTest(change=change):
                self.policy = SessionPolicy(lambda: self.now)
                self.connect()
                self.policy.evaluate(self.raw(**change), 'ARMED')
                self.assertEqual(self.policy.evaluate(self.raw(), 'ARMED')['state'], 'unknown')
                self.now += .01
                self.assertEqual(self.policy.evaluate(self.raw(), 'ARMED')['state'], 'connected')

    def test_24_hours_hidden_and_minimized_retains_original_evidence(self):
        self.connect()
        for index in range(1, 24 * 60 + 1):
            self.now = 1000 + index * 60
            for flag in ('ui_hidden', 'ui_minimized'):
                raw = self.raw(state='unknown', reason='uu_windows_unavailable',
                               session_observed_at=None, **{flag: True})
                held = self.policy.evaluate(raw, 'ACTIVE')
                self.assertEqual(held['state'], 'connected')
                self.assertTrue(held['session_held'])
                self.assertEqual(held['session_observed_at'], 1000)
                self.assertEqual(held['raw_state'], 'unknown')
                self.assertEqual(raw['state'], 'unknown')

    def test_cached_heartbeat_never_renews_proof_or_activates_armed(self):
        self.connect()
        self.now += 89
        self.policy.evaluate(self.raw(session_observed_at=1000), 'ACTIVE')
        self.now += 2
        held = self.policy.evaluate(self.raw(session_observed_at=1000), 'ACTIVE')
        self.assertTrue(held['session_held'])
        self.assertEqual(held['session_observed_at'], 1000)
        self.assertEqual(self.policy.evaluate(self.raw(session_observed_at=1000), 'ARMED')['state'], 'unknown')
        self.assertEqual(SessionPolicy(lambda: self.now).evaluate(self.raw(session_observed_at=1000), 'ARMED')['state'], 'unknown')
        self.assertEqual(self.policy.evaluate(self.raw(), 'ARMED')['state'], 'connected')

    def test_permission_sleep_stale_and_unknown_fault_drop_latch(self):
        for change in ({'trusted': False}, {'sleeping': True}, {'timestamp': 910},
                       {'timestamp': 1001}, {'reason': 'native_failure'},
                       {'timestamp': float('nan')}, {'timestamp': True}):
            with self.subTest(change=change):
                self.policy = SessionPolicy(lambda: self.now)
                self.connect()
                result = self.policy.evaluate(self.raw(state='unknown', reason='uu_windows_unavailable', **change)
                    if 'reason' not in change else self.raw(state='unknown', **change), 'ACTIVE')
                self.assertNotEqual(result['state'], 'connected')
                self.assertFalse(self.policy.evaluate(self.raw(state='unknown', reason='uu_windows_unavailable'), 'ACTIVE')['session_held'])

    def test_epoch_change_recovers_then_requires_real_positive(self):
        for change in ({'run_id': 'run2'}, {'detector_pid': 12}, {'uuPID': 23}, {'uuPID': None}):
            with self.subTest(change=change):
                self.policy = SessionPolicy(lambda: self.now)
                self.connect()
                value = self.policy.evaluate(self.raw(**change), 'ACTIVE')
                self.assertEqual(value['session_health_fault'], 'generation_changed')
                self.assertFalse(value['session_held'])
                if change.get('uuPID', 1) is not None:
                    self.assertEqual(self.policy.evaluate(self.raw(**change), 'ARMED')['state'], 'connected')

    def test_negative_requires_verification_and_clears_latch(self):
        for reason in self.policy.NEGATIVE_REASONS:
            self.policy = SessionPolicy(lambda: self.now)
            self.connect()
            raw = self.raw(state='disconnected', reason=reason)
            self.assertTrue(self.policy.evaluate(raw, 'ACTIVE')['session_held'])
            raw['session_negative_verified'] = True
            self.assertEqual(self.policy.evaluate(raw, 'ACTIVE')['state'], 'disconnected')
            self.assertFalse(self.policy.evaluate(self.raw(state='unknown', reason='uu_windows_unavailable'), 'ACTIVE')['session_held'])
        self.now += .01
        self.connect()
        value = self.policy.evaluate(self.raw(state='disconnected', reason='uu_not_running', uuPID=None), 'ACTIVE')
        self.assertEqual(value['state'], 'disconnected')
        self.assertNotIn('session_health_fault', value)

    def test_persisted_held_signal_cannot_activate_after_restart(self):
        self.assertEqual(self.policy.evaluate(self.raw(session_held=True, session_source='held_observation'), 'ARMED')['state'], 'unknown')

    def test_refresh_is_once_per_intent_epoch_or_completed_read_episode(self):
        raw = self.raw(session_observed_at=900)
        effective = self.policy.evaluate(raw, 'ARMED')
        self.assertTrue(self.policy.needs_refresh(effective, 'ARMED', True, 'enable1'))
        for _ in range(100):
            self.assertFalse(self.policy.needs_refresh(effective, 'ARMED', True, 'enable1'))
        self.assertTrue(self.policy.needs_refresh(effective, 'ARMED', True, 'enable2'))
        fresh = self.policy.evaluate(self.raw(), 'ARMED')
        self.assertFalse(self.policy.needs_refresh(fresh, 'ACTIVE', True, 'enable2'))
        self.now += 91
        effective = self.policy.evaluate(self.raw(session_observed_at=1000), 'ARMED')
        self.assertTrue(self.policy.needs_refresh(effective, 'ARMED', True, 'enable2'))
        for changes in ({'trusted': False}, {'sleeping': True}, {'timestamp': 0}):
            invalid = dict(effective, **changes)
            self.assertFalse(self.policy.needs_refresh(invalid, 'ARMED', True, 'new'))

    def test_invalid_or_legacy_observation_cannot_establish_connection(self):
        for observed in (None, True, float('nan'), float('inf'), 1001, 910, '1000'):
            with self.subTest(observed=observed):
                raw = self.raw(session_observed_at=observed)
                self.assertEqual(self.policy.evaluate(raw, 'ARMED')['state'], 'unknown')
        for field in ('run_id', 'detector_pid', 'uuPID', 'session_observed_at'):
            raw = self.raw();raw.pop(field)
            self.assertEqual(self.policy.evaluate(raw, 'ARMED')['state'], 'unknown')

    def test_recheck_preserves_native_epoch_and_metadata(self):
        from session_recheck import SessionRecheck
        with tempfile.TemporaryDirectory() as directory:
            verifier = SessionRecheck(directory, '/unused', clock=lambda: self.now)
            verifier._verified = self.raw(run_id='probe', detector_pid=99,
                                           session_observed_at=999, ui_hidden=True)
            verifier._verified_at = self.now
            merged = verifier._merge(self.raw(state='unknown', timestamp=998))
            self.assertEqual(merged['run_id'], 'run1')
            self.assertEqual(merged['detector_pid'], 11)
            self.assertEqual(merged['probePID'], 99)
            self.assertEqual(merged['native_timestamp'], 998)
            self.assertEqual(merged['session_observed_at'], 999)
            self.assertTrue(merged['ui_hidden'])


class EngineContinuityTests(unittest.TestCase):
    raw = SessionPolicyTests.raw

    def setUp(self):
        SessionPolicyTests.setUp(self)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        directory = Path(self.temp.name)
        self.bd = FakeBD([display(), target()]);self.bd.directory = directory
        self.engine = c.Engine(self.bd, directory, clock=lambda: self.now,
                               sleeper=lambda seconds: setattr(self, 'now', self.now + seconds),
                               monotonic=lambda: self.now,
                               event_driven=True, signal_max_age=90)
        signal = self.policy.evaluate(self.raw(), 'DISABLED')
        self.engine.tick(signal, {'id': 'enable', 'action': 'enable'})
        self.assertEqual(self.engine.state['mode'], 'ACTIVE')

    def apply(self, **changes):
        signal = self.policy.evaluate(self.raw(**changes), self.engine.state['mode'])
        return self.engine.tick(signal)

    def test_24_hour_engine_hold_does_not_restore_or_poll_inventory(self):
        calls = copy.deepcopy(self.bd.calls)
        inventory_calls = self.engine.metrics['inventory_calls']
        for minute in range(1, 24 * 60 + 1):
            self.now = 1000 + minute * 60
            self.apply(state='unknown', reason='uu_windows_unavailable',
                       session_observed_at=None, ui_hidden=minute % 2 == 0,
                       ui_minimized=minute % 2 == 1, event='heartbeat', event_id=minute + 1)
            self.assertEqual(self.engine.state['mode'], 'ACTIVE')
        self.assertEqual(self.bd.calls, calls)
        self.assertEqual(self.engine.metrics['inventory_calls'], inventory_calls)

    def test_unverified_negative_does_not_start_disconnect_grace(self):
        self.now += 91
        self.apply(state='disconnected', reason='ax_cached_controller_table_empty',
                   session_negative_verified=False)
        self.assertEqual(self.engine.state['mode'], 'ACTIVE')
        self.assertIsNone(self.engine.disconnected_since)

    def test_display_revision_hotplug_still_hides_added_physical(self):
        self.bd.devices.append(display('hotplug', connected=True, main=False))
        self.now += 91
        self.apply(state='unknown', reason='uu_windows_unavailable',
                   session_observed_at=None, display_revision=2, event='display')
        self.assertEqual(self.engine.state['mode'], 'ACTIVE')
        self.assertFalse(next(d for d in self.bd.devices if d['identity'] == 'hotplug')['connected'])

    def test_target_loss_during_hold_uses_existing_recovery(self):
        self.bd.devices = [d for d in self.bd.devices if d['kind'] != 'VirtualScreen']
        self.now += 91
        self.apply(state='unknown', reason='uu_windows_unavailable', display_revision=2)
        self.assertNotEqual(self.engine.state['mode'], 'ACTIVE')
        self.assertTrue(self.bd.devices[0]['connected'])

    def test_true_disconnect_uses_three_second_grace_and_reconnects(self):
        self.apply(state='disconnected', reason='uu_not_running', uuPID=None)
        self.assertEqual(self.engine.state['mode'], 'ACTIVE')
        self.now += 3
        self.apply(state='disconnected', reason='uu_not_running', uuPID=None)
        self.assertEqual(self.engine.state['mode'], 'RESTORING')
        self.apply(state='disconnected', reason='uu_not_running', uuPID=None)
        self.assertEqual(self.engine.state['mode'], 'ARMED')
        self.apply(uuPID=23)
        self.assertEqual(self.engine.state['mode'], 'ACTIVE')

    def test_hidden_home_candidate_waits_for_confirmation_and_zero_peers(self):
        self.apply(activeControllerCount=2)
        self.now += 1
        self.apply(activeControllerCount=1)
        self.assertEqual(self.engine.state['mode'], 'ACTIVE')
        self.now += 1
        self.apply(state='unknown', reason='ax_home_disconnect_confirmation_pending',
                   ui_hidden=True, activeControllerCount=None)
        self.assertEqual(self.engine.state['mode'], 'ACTIVE')
        self.assertIsNone(self.engine.disconnected_since)
        self.now += 1
        zero = dict(state='disconnected', reason='ax_complete_home_no_local_control',
                    session_negative_verified=True, ui_hidden=True, activeControllerCount=0)
        self.apply(**zero)
        self.assertEqual(self.engine.state['mode'], 'ACTIVE')
        self.now += 3
        self.apply(**zero)
        self.assertEqual(self.engine.state['mode'], 'RESTORING')
        self.apply(**zero)
        self.assertEqual(self.engine.state['mode'], 'ARMED')

    def test_reconnect_within_grace_cancels_verified_hidden_disconnect(self):
        self.apply(state='disconnected', reason='ax_complete_home_no_local_control',
                   session_negative_verified=True, ui_hidden=True)
        self.now += 2
        self.apply(ui_hidden=True)
        self.now += 5
        self.apply(ui_hidden=True)
        self.assertEqual(self.engine.state['mode'], 'ACTIVE')
        self.assertIsNone(self.engine.disconnected_since)

    def test_generation_change_restores_before_new_positive_activation(self):
        self.apply(uuPID=23)
        self.assertEqual(self.engine.state['mode'], 'ARMED')
        self.assertTrue(self.bd.devices[0]['connected'])
        self.apply(uuPID=23)
        self.assertEqual(self.engine.state['mode'], 'ACTIVE')


if __name__ == '__main__':
    unittest.main()
