"""Name bindings use temporary stores and never access real displays."""
import copy
import json
import unittest
from unittest.mock import patch

import controller as c
import device_profiles as p
import test_profile_runtime as runtime_tests


class BindingTests(unittest.TestCase):
    setUp = runtime_tests.ProfileRuntimeTests.setUp
    tearDown = runtime_tests.ProfileRuntimeTests.tearDown
    advance = runtime_tests.ProfileRuntimeTests.advance
    signal = runtime_tests.ProfileRuntimeTests.signal
    tick = runtime_tests.ProfileRuntimeTests.tick
    enable = runtime_tests.ProfileRuntimeTests.enable

    def bind(self, profile_id=p.LEGACY_ID, name='iPad Pro (13 英寸, M4)', expected=None, detector=None):
        return self.tick(detector or self.signal(activeControllerCount=1), action='profile_bind',
                         profile_id=profile_id, name=name, expected_profile_id=expected)

    def add_profile(self):
        profile = p.make_profile('ipad-mini-a17-pro', id='other-profile')
        self.coordinator.config['profiles'].append(profile)
        self.coordinator.config = self.coordinator.store.save(self.coordinator.config)
        return profile['id']

    def snapshot(self):
        return c.read_json(self.directory / 'workbench-status.json')

    def test_bind_publishes_saved_original_and_immediate_identity(self):
        self.bind()
        binding = dict(key=p.normalize_name('iPad Pro (13 英寸, M4)'),
                       name='iPad Pro (13 英寸, M4)', profile_id=p.LEGACY_ID)
        self.assertEqual(self.snapshot()['bindings'], [binding])
        self.assertEqual(self.snapshot()['current_binding'], binding)
        self.assertEqual(self.snapshot()['identity']['source'], 'binding')
        self.assertEqual(self.coordinator.store.load()['binding_labels'], {binding['key']: binding['name']})
        self.assertTrue(self.snapshot()['binding_available'])
        self.coordinator.publish(self.signal(activeControllerCount=1,session_held=True))
        self.assertEqual(self.snapshot()['current_binding'],binding)

    def test_persisted_binding_command_does_not_block_disconnect_recovery(self):
        self.enable()
        self.bind()
        command = c.read_json(self.directory / 'command.json')
        for _ in range(3):
            self.advance(4)
            self.coordinator.tick(self.signal('disconnected', []), command)
        self.assertEqual(self.engine.state['mode'], 'ARMED')
        self.assertTrue(self.bd.devices[0]['connected'])
        self.assertFalse(self.bd.devices[1]['connected'])

    def test_persisted_binding_command_does_not_block_sleep_recovery(self):
        self.enable()
        self.bind()
        command = c.read_json(self.directory / 'command.json')
        self.coordinator.tick(self.signal(sleeping=True), command)
        self.assertEqual(self.engine.state['mode'], 'ARMED')
        self.assertTrue(self.bd.devices[0]['connected'])
        self.assertFalse(self.bd.devices[1]['connected'])

    def test_persisted_binding_command_allows_next_device_handover(self):
        other = self.add_profile()
        self.enable()
        self.bind()
        command = c.read_json(self.directory / 'command.json')
        for _ in range(2):
            self.coordinator.tick(self.signal(names=['iPad mini (A17 Pro)'], activeControllerCount=1), command)
        self.assertEqual(self.engine.state['mode'], 'ACTIVE')
        self.assertEqual(self.engine.state['active_profile_id'], other)

    def test_new_binding_command_keeps_sleep_recovery_and_blocks_new_entry(self):
        self.enable()
        self.bind(detector=self.signal(activeControllerCount=1, sleeping=True))
        self.assertEqual(self.coordinator.last_result['status'], 'error')
        self.assertEqual(self.engine.state['mode'], 'ARMED')
        self.assertTrue(self.bd.devices[0]['connected'])
        self.assertFalse(self.bd.devices[1]['connected'])
        calls = len(self.provisioner.calls)
        self.bind()
        self.assertEqual(self.coordinator.last_result['status'], 'ok')
        self.assertEqual(self.engine.state['mode'], 'ARMED')
        self.assertEqual(len(self.provisioner.calls), calls)

    def test_binding_available_requires_full_live_evidence(self):
        for detector in (self.signal(), self.signal(activeControllerCount=True),
                         self.signal(activeControllerCount=2),
                         self.signal('disconnected', [], activeControllerCount=0)):
            self.coordinator.publish(detector)
            self.assertFalse(self.snapshot()['binding_available'])
        self.coordinator.publish(self.signal(activeControllerCount=1))
        self.assertTrue(self.snapshot()['binding_available'])
        self.coordinator.publish(self.signal(activeControllerCount=1, session_held=True))
        self.assertTrue(self.snapshot()['binding_available'])
        self.engine.state['mode'] = 'RESTORING'
        self.coordinator.publish(self.signal(activeControllerCount=1))
        self.assertFalse(self.snapshot()['binding_available'])
        self.engine.state['mode'] = 'PAUSED'
        self.coordinator.config = None
        self.coordinator.publish(self.signal(activeControllerCount=1))
        self.assertFalse(self.snapshot()['binding_available'])

    def test_rebind_cas_and_same_target_idempotence(self):
        other = self.add_profile()
        self.bind()
        self.bind(other, expected=None)
        self.assertEqual(self.coordinator.last_result['status'], 'error')
        self.bind(other, expected=p.LEGACY_ID)
        self.assertEqual(self.coordinator.last_result['status'], 'ok')
        self.bind(p.LEGACY_ID, expected=p.LEGACY_ID)
        self.assertEqual(self.coordinator.last_result['status'], 'error')
        self.bind(other, expected=None)
        self.assertEqual(self.coordinator.last_result['status'], 'ok')
        self.assertEqual(self.snapshot()['current_binding']['profile_id'], other)
        self.assertEqual(len(self.coordinator.results), 5)

    def test_bind_requires_expected_field_and_fresh_explicit_single_count(self):
        self.tick(self.signal(activeControllerCount=1), action='profile_bind', profile_id=p.LEGACY_ID,
                  name='iPad Pro (13 英寸, M4)')
        self.assertEqual(self.coordinator.last_result['status'], 'error')
        detectors = [self.signal(), self.signal(activeControllerCount=2),
                     self.signal(names=['iPad Pro (13 英寸, M4)', 'Another'], activeControllerCount=2),
                     self.signal(names=['Other'], activeControllerCount=1),
                     self.signal(activeControllerCount=1, session_held=True),
                     self.signal(activeControllerCount=1)]
        detectors[-1]['timestamp'] -= 100
        for detector in detectors:
            with self.subTest(detector=detector):
                self.bind(detector=detector)
                self.assertEqual(self.coordinator.last_result['status'], 'error')
                self.assertEqual(self.coordinator.config['bindings'], {})

    def test_offline_unbind_rejects_changed_owner_and_preserves_other_bindings(self):
        other = self.add_profile()
        self.bind()
        self.bind(other, name='Another Tablet', detector=self.signal(names=['Another Tablet'], activeControllerCount=1))
        self.tick(self.signal('disconnected', []), action='profile_unbind', profile_id=other,
                  name=p.normalize_name('iPad Pro (13 英寸, M4)'))
        self.assertEqual(self.coordinator.last_result['status'], 'error')
        self.tick(self.signal('disconnected', []), action='profile_unbind', profile_id=p.LEGACY_ID,
                  name=p.normalize_name('iPad Pro (13 英寸, M4)'))
        self.assertEqual(self.coordinator.last_result['status'], 'ok')
        self.assertEqual(self.coordinator.config['bindings'], {'anothertablet': other})
        self.assertEqual(self.coordinator.config['binding_labels'], {'anothertablet': 'Another Tablet'})
        self.assertIsNone(self.snapshot()['current_binding'])

    def test_active_bind_and_unbind_preserve_display_selection_and_preferences(self):
        other = self.add_profile()
        self.enable()
        before_config = copy.deepcopy(self.coordinator.config)
        before_state = copy.deepcopy(self.engine.state)
        before_cursor = (self.directory / 'cursor-profile-config.json').read_bytes()
        calls = copy.deepcopy(self.bd.calls)
        provisions = copy.deepcopy(self.provisioner.calls)
        self.bind(other)
        self.assertEqual(self.engine.state['mode'], 'ACTIVE')
        self.assertEqual(self.engine.state['journal'], before_state['journal'])
        self.assertEqual(self.engine.state['active_profile_id'], before_state['active_profile_id'])
        self.assertEqual(self.coordinator.config['selected_id'], before_config['selected_id'])
        self.assertEqual(self.coordinator.config['profiles'], before_config['profiles'])
        self.assertEqual(self.coordinator.config['automatic'], before_config['automatic'])
        self.assertEqual((self.directory / 'cursor-profile-config.json').read_bytes(), before_cursor)
        self.assertEqual(self.bd.calls, calls)
        self.assertEqual(self.provisioner.calls, provisions)
        self.tick(self.signal(activeControllerCount=1), action='profile_unbind', profile_id=other,
                  name='iPad Pro (13 英寸, M4)')
        self.assertEqual(self.bd.calls, calls)
        self.assertEqual(self.provisioner.calls, provisions)
        self.assertEqual(self.coordinator.config['selected_id'], before_config['selected_id'])
        self.assertEqual((self.directory / 'cursor-profile-config.json').read_bytes(), before_cursor)
        self.assertEqual(self.snapshot()['identity']['source'], 'name')

    def test_bind_failure_preserves_original_bytes_without_recovery(self):
        self.enable()
        before = self.coordinator.store.path.read_bytes()
        config = copy.deepcopy(self.coordinator.config)
        journal = copy.deepcopy(self.engine.state['journal'])
        calls = copy.deepcopy(self.bd.calls)
        with patch.object(self.coordinator.store, 'save', side_effect=OSError('injected save failure')):
            self.bind()
        self.assertEqual(self.coordinator.store.path.read_bytes(), before)
        self.assertEqual(self.coordinator.config, config)
        self.assertEqual(self.engine.state['journal'], journal)
        self.assertEqual(self.engine.state['mode'], 'ACTIVE')
        self.assertEqual(self.bd.calls, calls)
        self.assertEqual(c.read_json(self.coordinator.transaction_path)['status'], 'error')
        self.assertEqual(self.coordinator.last_result['status'], 'error')

    def test_corrupt_store_is_preserved(self):
        self.coordinator.store.path.write_text('{broken')
        self.bind()
        self.assertEqual(self.coordinator.store.path.read_text(), '{broken')
        self.assertEqual(self.coordinator.last_result['status'], 'error')

    def test_restoring_rejects_binding_and_continues_existing_recovery(self):
        self.enable()
        self.engine._begin_restore('Test recovery', afterwards='ARMED')
        before = self.coordinator.store.path.read_bytes()
        self.bind()
        self.assertEqual(self.coordinator.last_result['status'], 'error')
        self.assertEqual(self.coordinator.store.path.read_bytes(), before)
        self.assertTrue(self.bd.devices[0]['connected'])
        self.assertFalse(self.bd.devices[1]['connected'])

    def test_cas_uses_latest_durable_bindings(self):
        other = self.add_profile()
        latest = copy.deepcopy(self.coordinator.config)
        key = p.normalize_name('iPad Pro (13 英寸, M4)')
        latest['bindings'][key] = other
        self.coordinator.store.save(latest)
        before = self.coordinator.store.path.read_bytes()
        self.bind(expected=None)
        self.assertEqual(self.coordinator.last_result['status'], 'error')
        self.assertEqual(self.coordinator.store.path.read_bytes(), before)

    def test_offline_unbind_tolerates_invalid_current_names(self):
        self.bind()
        self.tick(self.signal('disconnected', ['---']), action='profile_unbind',
                  profile_id=p.LEGACY_ID, name='iPad Pro (13 英寸, M4)')
        self.assertEqual(self.coordinator.last_result['status'], 'ok')
        self.assertEqual(self.coordinator.store.load()['bindings'], {})

    def test_manual_pause_preempts_metadata_commit(self):
        before = self.coordinator.store.path.read_bytes()
        with patch.object(self.coordinator, '_latest_manual', return_value={'id': 'urgent', 'action': 'pause'}):
            self.bind()
        self.assertEqual(self.engine.state['mode'], 'PAUSED')
        self.assertEqual(self.coordinator.store.path.read_bytes(), before)
        self.assertTrue(any(r['id'] == 'urgent' for r in self.coordinator.results))

    def test_legacy_configuration_load_and_label_fallback(self):
        config = copy.deepcopy(self.coordinator.config)
        config['bindings'] = {'My Tablet': p.LEGACY_ID, 'ipadpro13英寸m4': p.LEGACY_ID,
                              'ipadminia17pro': p.LEGACY_ID}
        self.coordinator.store.path.write_text(json.dumps(config))
        self.coordinator.config = self.coordinator.store.load()
        self.assertNotIn('binding_labels', self.coordinator.config)
        self.coordinator.publish(self.signal(activeControllerCount=1))
        labels = {b['key']: b['name'] for b in self.snapshot()['bindings']}
        self.assertEqual(labels['mytablet'], 'mytablet')
        self.assertEqual(labels['ipadpro13英寸m4'], 'iPad Pro (13 英寸, M4)')
        self.assertEqual(labels['ipadminia17pro'], 'iPad mini (A17 Pro)')

    def test_labels_validation_and_legacy_apply_compatibility(self):
        self.tick(self.signal('disconnected', []), action='profile_apply', profile_id=p.LEGACY_ID,
                  draft={}, bind_name='我的平板')
        self.assertEqual(self.coordinator.config['binding_labels'], {'我的平板': '我的平板'})
        for labels in ({'wrong': 'Other'}, {'我的平板': 'Other'}, {'我的平板': 3}):
            candidate = copy.deepcopy(self.coordinator.config)
            candidate['binding_labels'] = labels
            with self.assertRaises(ValueError):
                self.coordinator.store.save(candidate)


if __name__ == '__main__':
    unittest.main()
