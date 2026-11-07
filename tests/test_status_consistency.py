"""Workbench receipts and current status agree after failures and restart gaps."""
import copy
import errno
import unittest
from unittest.mock import patch

import controller as c
import profile_runtime as runtime
from profile_runtime import DeviceCoordinator
import test_profile_runtime as fixtures


class StatusConsistencyTests(unittest.TestCase):
    setUp = fixtures.ProfileRuntimeTests.setUp
    tearDown = fixtures.ProfileRuntimeTests.tearDown
    advance = fixtures.ProfileRuntimeTests.advance
    signal = fixtures.ProfileRuntimeTests.signal
    tick = fixtures.ProfileRuntimeTests.tick

    def snapshot(self):
        return c.read_json(self.directory / 'workbench-status.json')

    def test_enable_connection_failure_reports_error_after_safe_pause(self):
        self.bd.fail = lambda device, fields: (device['kind'] == 'VirtualScreen'
                                               and fields.get('connected') is True)
        self.tick(action='enable')
        self.assertEqual(self.engine.state['mode'], 'PAUSED')
        self.assertFalse(self.engine.state['desired_enabled'])
        self.assertEqual(self.coordinator.last_result['status'], 'error')
        self.assertIn('Injected failure', self.coordinator.last_result['message'])
        self.assertTrue(self.bd.devices[0]['connected'])
        self.assertFalse(self.bd.devices[1]['connected'])

    def test_enable_waiting_and_active_are_successful(self):
        self.tick(self.signal('disconnected', []), action='enable')
        self.assertEqual(self.engine.state['mode'], 'ARMED')
        self.assertEqual(self.coordinator.last_result['status'], 'ok')
        self.tick(action='enable')
        self.assertEqual(self.engine.state['mode'], 'ACTIVE')
        self.assertEqual(self.coordinator.last_result['status'], 'ok')

    def test_core_commands_publish_applying_before_engine_execution_and_do_not_replay(self):
        for action in ('enable', 'pause', 'recover'):
            with self.subTest(action=action):
                command = dict(id='start-' + action, action=action)
                original = self.engine.tick
                def ticking(detector, submitted=None):
                    if submitted:
                        self.assertEqual(self.snapshot()['last_result']['id'], command['id'])
                        self.assertEqual(self.snapshot()['last_result']['status'], 'applying')
                        self.assertEqual(c.read_json(self.directory / 'command-result.json')['status'], 'applying')
                    return original(detector, submitted)
                with patch.object(self.engine, 'tick', ticking):
                    self.coordinator.tick(self.signal('disconnected', []), command)
                self.assertEqual(self.coordinator.last_result['status'], 'ok')
                if action == 'enable':
                    self.engine.state['last_command'] = 'older-state'
                with patch.object(self.coordinator, '_intent', side_effect=AssertionError('Command replayed')):
                    self.coordinator.tick(self.signal('disconnected', []), command)

    def test_successful_pause_clears_operation_error_but_retains_receipt(self):
        self.tick(action='profile_apply', profile_id=self.coordinator.config['selected_id'],
                  draft={'orientation': 'invalid'})
        failed = copy.deepcopy(self.coordinator.last_result)
        self.assertEqual(failed['status'], 'error')
        self.assertIsNotNone(self.snapshot()['error'])
        self.tick(action='pause')
        self.assertEqual(self.engine.state['mode'], 'PAUSED')
        self.assertIsNone(self.snapshot()['error'])
        self.assertIn(failed, self.coordinator.results)

    def test_unconsumed_manual_command_survives_crash_after_applying_receipt(self):
        for action in ('pause', 'recover'):
            with self.subTest(action=action):
                self.tick(action='enable')
                previous = self.engine.state['last_command']
                command = dict(id='unconsumed-' + action, action=action)
                c.atomic_json(self.directory / 'command.json', command)
                with patch.object(self.engine, 'tick', side_effect=RuntimeError('Crash before consume')):
                    with self.assertRaisesRegex(RuntimeError, 'before consume'):
                        self.coordinator.tick(self.signal(), command)
                self.assertEqual(self.engine.state['last_command'], previous)
                self.coordinator = DeviceCoordinator(self.engine, self.provisioner)
                self.coordinator.tick(self.signal(), command)
                self.assertEqual(self.engine.state['last_command'], command['id'])
                self.assertFalse(self.engine.state['desired_enabled'])
                self.assertEqual(self.engine.state['mode'], 'PAUSED')
                self.assertEqual(self.coordinator.last_result['status'], 'ok')

    def test_error_sources_separate_operation_configuration_and_engine_health(self):
        self.provisioner.fail = True
        self.tick(action='enable')
        self.assertEqual(self.snapshot()['configuration_error'], 'Injected provision failure')
        self.assertIsNone(self.snapshot()['operation_error'])

        self.tick(action='profile_apply', profile_id=self.coordinator.config['selected_id'],
                  draft={'orientation': 'invalid'})
        self.assertEqual(self.snapshot()['configuration_error'], 'Injected provision failure')
        self.assertIn('orientation', self.snapshot()['operation_error'])
        self.engine.state['last_error'] = 'Independent engine fault'
        self.coordinator.publish(self.signal())
        self.assertEqual(self.snapshot()['engine_error'], 'Independent engine fault')
        self.assertEqual(self.snapshot()['status_detail']['last_error'], 'Independent engine fault')
        self.provisioner.fail = False
        self.tick(action='enable')
        self.assertIsNone(self.snapshot()['configuration_error'])
        self.assertIsNone(self.snapshot()['operation_error'])

    def test_older_interrupted_transaction_settles_history_without_replacing_new_pause(self):
        self.tick(action='enable')
        with patch.object(self.coordinator.store, 'save', side_effect=OSError(errno.ENOSPC, 'disk full')):
            with self.assertRaises(OSError):
                self.tick(action='profile_cursor', scale=2.5)
        transaction = c.read_json(self.coordinator.transaction_path)
        self.assertEqual(transaction['status'], 'applying')
        self.tick(action='pause')
        latest = copy.deepcopy(self.coordinator.last_result)
        intent = c.read_json(self.coordinator.intent_path)
        self.coordinator = DeviceCoordinator(self.engine, self.provisioner)
        self.assertEqual(self.coordinator.last_result, latest)
        self.assertEqual(c.read_json(self.directory / 'command-result.json'), latest)
        self.assertEqual(c.read_json(self.coordinator.intent_path), intent)
        historical = next(r for r in self.coordinator.results if r['id'] == transaction['id'])
        self.assertEqual(historical['status'], 'error')
        self.assertEqual(historical['action'], 'profile_cursor')
        self.assertEqual(c.read_json(self.coordinator.transaction_path)['status'], 'interrupted')

    def test_successful_pause_does_not_clear_configuration_recovery_fault(self):
        self.coordinator.profile_recovery_blocked = True
        self.coordinator.error = '无法读取或恢复设备配置：damaged config'
        self.tick(action='pause')
        self.assertEqual(self.coordinator.last_result['status'], 'ok')
        self.assertEqual(self.snapshot()['error'], '无法读取或恢复设备配置：damaged config')

    def test_restart_completes_pending_intent_from_terminal_receipt(self):
        for location in ('command-result.json', 'command-results.json'):
            for status in ('ok', 'error'):
                with self.subTest(location=location, status=status):
                    receipt = dict(id='confirmed', action='enable', status=status,
                                   message='confirmed outcome', timestamp=self.now)
                    c.atomic_json(self.directory / 'command-intent.json',
                                  dict(version=1, id='confirmed', action='enable', status='applying'))
                    c.atomic_json(self.directory / 'command-result.json',
                                  receipt if location == 'command-result.json' else None)
                    c.atomic_json(self.directory / 'command-results.json',
                                  [receipt] if location == 'command-results.json' else [])
                    restored = DeviceCoordinator(self.engine, self.provisioner)
                    self.assertEqual(restored.last_result, receipt)
                    self.assertEqual(c.read_json(restored.intent_path)['status'], status)

    def test_restart_does_not_trust_different_action_receipt(self):
        c.atomic_json(self.directory / 'command-intent.json',
                      dict(version=1, id='confirmed', action='enable', status='applying'))
        c.atomic_json(self.directory / 'command-result.json',
                      dict(id='confirmed', action='pause', status='ok', message='wrong action'))
        restored = DeviceCoordinator(self.engine, self.provisioner)
        self.assertEqual(restored.last_result['status'], 'error')
        self.assertEqual(restored.last_result['action'], 'enable')

    def test_final_intent_write_failure_preserves_real_completed_result(self):
        for action in ('enable', 'profile_automatic'):
            with self.subTest(action=action):
                command = dict(id='final-intent-' + action, action=action, automatic=False)
                c.atomic_json(self.directory / 'command.json', command)
                original = runtime.atomic_json
                def writing(path, value):
                    if path.name == 'command-intent.json' and value.get('status') == 'ok':
                        raise OSError(errno.ENOSPC, 'Injected final intent write failure')
                    return original(path, value)
                with patch.object(runtime, 'atomic_json', writing):
                    with self.assertRaisesRegex(OSError, 'final intent'):
                        self.coordinator.tick(self.signal('disconnected', []), command)
                receipt = c.read_json(self.directory / 'command-result.json')
                self.assertEqual(receipt['status'], 'ok')
                self.assertEqual(receipt['action'], action)
                self.coordinator = DeviceCoordinator(self.engine, self.provisioner)
                self.assertEqual(self.coordinator.last_result, receipt)
                self.assertEqual(c.read_json(self.coordinator.intent_path)['status'], 'ok')

    def test_enable_waits_for_recovery_then_settles_same_receipt(self):
        self.tick(action='enable')
        self.engine._begin_restore('Manual pause', category='manual')
        self.engine.retry_at = self.now + 5
        self.tick(action='enable')
        pending = self.coordinator.last_result
        self.assertEqual(self.engine.state['mode'], 'RESTORING')
        self.assertEqual(pending['status'], 'applying')
        self.advance(6)
        self.tick(self.signal('disconnected', []))
        self.assertEqual(self.engine.state['mode'], 'ARMED')
        self.assertEqual(self.coordinator.last_result['id'], pending['id'])
        self.assertEqual(self.coordinator.last_result['status'], 'ok')

    def test_manual_command_cancels_waiting_enable_without_leaving_pending_history(self):
        self.tick(action='enable')
        self.engine._begin_restore('Manual pause', category='manual')
        self.engine.retry_at = self.now + 5
        self.tick(action='enable')
        pending_id = self.coordinator.last_result['id']
        self.assertEqual(self.coordinator.last_result['status'], 'applying')
        self.advance(6)
        self.tick(action='pause')
        latest = self.coordinator.last_result
        self.assertEqual(latest['action'], 'pause')
        self.assertEqual(latest['status'], 'ok')
        canceled = next(r for r in self.coordinator.results if r['id'] == pending_id)
        self.assertEqual(canceled['status'], 'error')
        self.assertIn('取消', canceled['message'])
        self.assertEqual(c.read_json(self.coordinator.intent_path)['id'], latest['id'])
        self.assertEqual(self.engine.state['mode'], 'PAUSED')
        self.assertFalse(self.engine.state['desired_enabled'])

    def test_status_detail_is_same_engine_projection_with_process_generation(self):
        self.tick(action='enable')
        self.engine.state.update(exit_after_restore='exit', exit_recovery_pending={'reason': 'pending'},
                                 recovery_context={'category': 'manual', 'reason': 'Manual pause'})
        self.coordinator.publish(self.signal())
        snapshot = self.snapshot()
        self.assertEqual(snapshot['controller_pid'], self.engine.status()['controller_pid'])
        self.assertEqual(snapshot['status_detail'],
                         {key: self.engine.status().get(key) for key in snapshot['status_detail']})
        self.assertNotIn('journal', snapshot['status_detail'])
        self.assertNotIn('metrics', snapshot['status_detail'])
        for key in ('mode', 'desired_enabled', 'last_command', 'next_retry', 'recovery_pending',
                    'waiting_for', 'last_error', 'recovery_context', 'exit_after_restore',
                    'exit_recovery_pending'):
            self.assertIn(key, snapshot['status_detail'])


if __name__ == '__main__':
    unittest.main()
