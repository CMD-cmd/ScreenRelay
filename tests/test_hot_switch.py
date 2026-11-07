"""Live profile changes must retain a recoverable physical baseline.

All output operations use FakeBD; no display or application APIs are invoked.
"""
import copy
import unittest
from unittest.mock import patch

import controller as c
import device_profiles as p
from profile_runtime import DeviceCoordinator
import test_profile_runtime as fixtures
from test_controller import display
import test_multi_device as multi


class SimulatedCrash(BaseException):
    """Escape coordinator's ordinary error recovery, like process termination."""


class HotSwitchTests(unittest.TestCase):
    setUp = fixtures.ProfileRuntimeTests.setUp
    tearDown = fixtures.ProfileRuntimeTests.tearDown
    advance = fixtures.ProfileRuntimeTests.advance
    signal = fixtures.ProfileRuntimeTests.signal
    tick = fixtures.ProfileRuntimeTests.tick
    enable = fixtures.ProfileRuntimeTests.enable
    add_b = multi.MultiDeviceTests.add_b

    def device(self, identity):
        return next(d for d in self.bd.devices if d['identity'] == identity)

    def connect(self, name):
        for _ in range(3):
            self.advance(.2)
            self.tick(self.signal(names=[name]))

    def start_pair(self):
        b = self.add_b()
        self.tick(self.signal(names=['Tablet A']), action='enable')
        self.assertEqual(self.engine.state['mode'], 'ACTIVE')
        self.bd.calls.clear()
        return b

    def baseline(self):
        journal = self.engine.state['journal']
        return copy.deepcopy({k: journal[k] for k in ('physical', 'created', 'protections')})

    def assert_no_physical_wakeup(self):
        physical = {d['identity'] for d in self.bd.devices if d['kind'] == 'Display'}
        self.assertFalse([(identity, fields) for identity, fields in self.bd.calls
                          if identity in physical and fields.get('connected') is True])

    def recover(self, action='pause'):
        detector = self.signal('disconnected', [])
        self.tick(detector, action=action)
        for _ in range(4):
            self.advance(4)
            self.tick(self.signal('disconnected', []))
        self.assertIsNone(self.engine.state['journal'])
        self.assertTrue(self.device('physical1')['connected'])
        self.assertTrue(self.device('physical1')['main'])
        self.assertEqual(self.device('physical1')['resolution'], '1920x1080')

    def assert_managed_off(self, b):
        for identity in (c.TARGET_UUID, b['virtual']['identity']):
            self.assertFalse(self.device(identity)['connected'])

    def test_live_resolution_preserves_baseline_without_reprovisioning(self):
        self.enable()
        before = self.baseline()
        provisions = len(self.provisioner.calls)
        self.bd.calls.clear()
        self.tick(action='profile_apply', profile_id=p.LEGACY_ID,
                  draft={'resolution': '1152x864'})
        self.assertEqual(self.engine.state['mode'], 'ACTIVE')
        self.assertEqual(self.coordinator.last_result['status'], 'ok')
        self.assertEqual(self.device(c.TARGET_UUID)['resolution'], '1152x864')
        self.assertEqual(self.device(c.TARGET_UUID)['hiDPI'], 'on')
        self.assertTrue(self.device(c.TARGET_UUID)['main'])
        self.assertEqual(self.baseline(), before)
        self.assertEqual(len(self.provisioner.calls), provisions)
        self.assert_no_physical_wakeup()
        self.assertFalse(any(i == c.TARGET_UUID and f.get('connected') is False
                             for i, f in self.bd.calls))

    def test_repeated_resolution_changes_restore_original_physical_baseline(self):
        self.enable()
        before = self.baseline()
        for resolution in ('1152x864', '1280x960', '1152x864'):
            self.tick(action='profile_apply', profile_id=p.LEGACY_ID,
                      draft={'resolution': resolution})
            self.assertEqual(self.engine.state['mode'], 'ACTIVE')
            self.assertEqual(self.baseline(), before)
        self.recover()
        self.assertFalse(self.device(c.TARGET_UUID)['connected'])

    def test_handover_verifies_new_main_before_old_screen_disconnect(self):
        b = self.start_pair()
        events = []
        original_set, original_inventory = self.bd.set, self.bd.inventory
        def inventory():
            devices = original_inventory()
            events.append(('inventory', devices))
            return devices
        def setting(device, **fields):
            events.append(('set', device['identity'], fields))
            original_set(device, **fields)
        self.bd.inventory, self.bd.set = inventory, setting
        self.connect('Tablet B')
        self.assertEqual(self.engine.state['active_profile_id'], b['id'])
        self.assertEqual(self.engine.state['mode'], 'ACTIVE')
        old_off = next(i for i, event in enumerate(events) if event[0] == 'set'
                       and event[1] == c.TARGET_UUID and event[2].get('connected') is False)
        self.assertTrue(any(event[0] == 'set' and event[1] == b['virtual']['identity']
                            and event[2].get('connected') is True for event in events[:old_off]))
        self.assertTrue(any(event[0] == 'inventory' and any(
            d['identity'] == b['virtual']['identity'] and d['connected'] and d['main']
            and d['resolution'] == b['resolution'] and d['hiDPI'] == 'on'
            for d in event[1]) for event in events[:old_off]))
        self.assert_no_physical_wakeup()

    def test_a_b_a_keeps_preferences_and_single_original_journal(self):
        b = self.start_pair()
        original = copy.deepcopy(self.coordinator.config['profiles'])
        before = self.baseline()
        for name, identity, scale in [('Tablet B', b['virtual']['identity'], 2),
                                      ('Tablet A', c.TARGET_UUID, 3)]:
            self.connect(name)
            self.assertEqual(self.engine.state['mode'], 'ACTIVE')
            self.assertTrue(self.device(identity)['connected'])
            self.assertEqual(sum(d['connected'] for d in self.bd.devices), 1)
            self.assertEqual(self.baseline(), before)
            self.assertEqual(c.read_json(self.directory / 'cursor-profile-config.json')['scale'], scale)
        self.assertEqual(self.coordinator.config['profiles'], original)
        self.assert_no_physical_wakeup()
        self.recover()
        self.assert_managed_off(b)

    def test_real_disconnect_after_handover_closes_all_touched_targets(self):
        b = self.start_pair()
        self.connect('Tablet B')
        for _ in range(4):
            self.advance(4)
            self.tick(self.signal('disconnected', []))
        self.assertEqual(self.engine.state['mode'], 'ARMED')
        self.assertTrue(self.device('physical1')['connected'])
        self.assert_managed_off(b)

    def select_offline_b(self, b):
        # Persisted selection is independent of the device currently owning the session.
        config = copy.deepcopy(self.coordinator.config)
        config['selected_id'] = b['id']
        self.coordinator.config = self.coordinator.store.save(config)

    def test_active_label_edit_does_not_switch_to_selected_offline_device(self):
        b = self.start_pair()
        self.select_offline_b(b)
        before, calls = self.baseline(), len(self.provisioner.calls)
        self.tick(self.signal(names=['Tablet A']), action='profile_apply',
                  profile_id=p.LEGACY_ID, draft={'label': 'My active tablet'})
        self.assertEqual(self.engine.state['active_profile_id'], p.LEGACY_ID)
        self.assertEqual(self.coordinator._profile(p.LEGACY_ID)['label'], 'My active tablet')
        self.assertEqual(self.baseline(), before)
        self.assertEqual(len(self.provisioner.calls), calls)
        self.assertFalse(self.bd.calls)

    def test_active_cursor_edit_uses_active_profile_when_selection_differs(self):
        b = self.start_pair()
        self.select_offline_b(b)
        self.tick(self.signal(names=['Tablet A']), action='profile_cursor', scale=2.5)
        self.assertEqual(self.coordinator._profile(p.LEGACY_ID)['cursor_scale'], 2.5)
        self.assertEqual(self.coordinator._profile(b['id'])['cursor_scale'], 2)
        self.assertEqual(c.read_json(self.directory / 'cursor-profile-config.json')['scale'], 2.5)
        self.assertEqual(self.engine.state['active_profile_id'], p.LEGACY_ID)
        self.assertFalse(self.bd.calls)

    def test_automatic_toggle_does_not_project_offline_selected_cursor(self):
        b = self.start_pair()
        self.select_offline_b(b)
        before = c.read_json(self.directory / 'cursor-profile-config.json')
        self.tick(self.signal(names=['Tablet A']), action='profile_automatic', automatic=False)
        self.assertEqual(self.coordinator.last_result['status'], 'ok')
        self.assertEqual(c.read_json(self.directory / 'cursor-profile-config.json'), before)
        self.assertEqual(self.engine.state['active_profile_id'], p.LEGACY_ID)
        self.assertFalse(self.bd.calls)

    def interrupt_new_target(self, b, interruption):
        original = self.bd.set
        fired = []
        boundary = []
        def setting(device, **fields):
            original(device, **fields)
            if device['identity'] == b['virtual']['identity'] and fields.get('connected') is True and not fired:
                fired.append(True)
                boundary.append((self.device(c.TARGET_UUID)['connected'],
                                 self.device('physical1')['connected']))
                interruption()
        self.bd.set = setting
        self.connect('Tablet B')
        self.assertTrue(fired, 'Fault injection must reach the new virtual online boundary')
        self.assertEqual(boundary, [(True, False)],
                         'New output must come online while old virtual stays usable and physical stays off')

    def test_pause_after_new_target_online_recovers_both_targets(self):
        b = self.start_pair()
        def pause():
            c.atomic_json(self.directory / 'command.json', {'id': 'urgent-pause', 'action': 'pause'})
        self.interrupt_new_target(b, pause)
        self.assertEqual(self.engine.state['mode'], 'PAUSED')
        self.assertFalse(self.engine.state['desired_enabled'])
        self.assertTrue(self.device('physical1')['connected'])
        self.assert_managed_off(b)

    def test_disconnect_after_new_target_online_recovers_both_targets(self):
        b = self.start_pair()
        def disconnect():
            c.atomic_json(self.directory / 'signal.json',
                          self.signal('disconnected', [], session_negative_verified=True))
        self.interrupt_new_target(b, disconnect)
        self.assertNotEqual(self.engine.state['mode'], 'ACTIVE')
        # interrupt_new_target continues passing its pre-disconnect connected
        # snapshots; the durable verified negative must win those races.
        online_calls = sum(i == b['virtual']['identity'] and f.get('connected') is True
                           for i, f in self.bd.calls)
        self.assertEqual(online_calls, 1)
        for _ in range(4):
            self.advance(4)
            self.tick(self.signal('disconnected', []))
        self.assertTrue(self.device('physical1')['connected'])
        self.assert_managed_off(b)

    def test_old_target_disconnect_failure_keeps_recovery_for_both_targets(self):
        b = self.start_pair()
        failures = []
        def fail(device, fields):
            if device['identity'] == c.TARGET_UUID and fields.get('connected') is False:
                failures.append(any(d['identity'] == b['virtual']['identity'] and d['connected']
                                    for d in self.bd.devices))
                return True
            return False
        self.bd.fail = fail
        self.connect('Tablet B')
        self.assertTrue(failures, 'Old-output disconnect failure must be exercised')
        self.assertTrue(all(failures), 'New target must be online before old-output disconnect is attempted')
        self.assertTrue(any(d['connected'] for d in self.bd.devices))
        self.bd.fail = None
        self.recover()
        self.assert_managed_off(b)

    def test_crash_with_two_targets_online_recovers_owned_outputs_only(self):
        b = self.start_pair()
        # An unrelated virtual definition is outside this automation's ownership.
        self.bd.devices.append(display('unowned', False, False, 'VirtualScreen'))
        crashed_state = []
        def crash():
            crashed_state.append((self.device(c.TARGET_UUID)['connected'],
                                  self.device(b['virtual']['identity'])['connected'],
                                  self.device('physical1')['connected']))
            raise SimulatedCrash()
        with self.assertRaises(SimulatedCrash):
            self.interrupt_new_target(b, crash)
        self.assertEqual(crashed_state, [(True, True, False)])
        self.engine = c.Engine(self.bd, self.directory, clock=lambda: self.now,
                               monotonic=lambda: self.now, sleeper=self.advance,
                               signal_max_age=90, event_driven=True)
        self.coordinator = DeviceCoordinator(self.engine, self.provisioner)
        self.recover()
        self.assert_managed_off(b)
        self.assertFalse(any(i == 'unowned' for i, _ in self.bd.calls))


    def ignore_first_main(self, identity):
        original = self.bd.set
        attempts = []
        def setting(device, **fields):
            old_main = {d['identity']: d['main'] for d in self.bd.devices}
            original(device, **fields)
            if device['identity'] == identity and fields.get('main') is True:
                attempts.append(True)
                if len(attempts) == 1:
                    for d in self.bd.devices:
                        d['main'] = old_main[d['identity']]
        self.bd.set = setting
        return attempts

    def test_new_main_ignored_once_retries_without_physical_wakeup(self):
        b = self.start_pair()
        before = self.baseline()
        attempts = self.ignore_first_main(b['virtual']['identity'])
        self.connect('Tablet B')
        self.assertGreaterEqual(len(attempts), 2)
        self.assertEqual(self.engine.state['mode'], 'ACTIVE')
        self.assertEqual(self.engine.state['active_profile_id'], b['id'])
        self.assertTrue(self.device(b['virtual']['identity'])['main'])
        self.assertFalse(self.device(c.TARGET_UUID)['connected'])
        self.assertEqual(self.baseline(), before)
        self.assert_no_physical_wakeup()

    def test_main_requests_always_ignored_stop_after_three_and_recover(self):
        b = self.start_pair()
        original = self.bd.set
        attempts = []
        def setting(device, **fields):
            previous = {d['identity']: d['main'] for d in self.bd.devices}
            original(device, **fields)
            if device['identity'] == b['virtual']['identity'] and fields.get('main') is True:
                attempts.append(True)
                for d in self.bd.devices:
                    d['main'] = previous[d['identity']]
        self.bd.set = setting
        self.connect('Tablet B')
        self.assertEqual(len(attempts), 3)
        self.assertEqual(self.engine.state['mode'], 'ARMED')
        self.assertIsNone(self.engine.state['journal'])
        self.assertTrue(self.device('physical1')['connected'])
        self.assert_managed_off(b)

    def test_pause_between_main_retries_prevents_further_main_requests(self):
        b = self.start_pair()
        original = self.bd.set
        attempts = []
        def setting(device, **fields):
            previous = {d['identity']: d['main'] for d in self.bd.devices}
            original(device, **fields)
            if device['identity'] == b['virtual']['identity'] and fields.get('main') is True:
                attempts.append(True)
                for d in self.bd.devices:
                    d['main'] = previous[d['identity']]
                c.atomic_json(self.directory / 'command.json', {'id': 'pause-main-retry', 'action': 'pause'})
        self.bd.set = setting
        self.connect('Tablet B')
        self.assertEqual(len(attempts), 1)
        self.assertEqual(self.engine.state['mode'], 'PAUSED')
        self.assertFalse(self.engine.state['desired_enabled'])
        self.assert_managed_off(b)

    def test_handover_failure_restores_absent_cursor_projection(self):
        b = self.start_pair()
        path = self.directory / 'cursor-profile-config.json'
        path.unlink()
        with patch.object(self.coordinator.store, 'save', side_effect=OSError('Final commit failed')):
            self.connect('Tablet B')
        self.assertFalse(path.exists())
        self.assertTrue(self.device('physical1')['connected'])
        self.assert_managed_off(b)

    def test_physical_main_ignored_once_restores_original_layout_on_retry(self):
        secondary = display('physical2', True, False)
        secondary['placement'] = '-1920x120'
        self.bd.devices.append(secondary)
        originals = copy.deepcopy(self.bd.devices[:1] + [secondary])
        self.enable()
        attempts = self.ignore_first_main('physical1')
        self.recover()
        self.assertGreaterEqual(len(attempts), 2)
        for original in originals:
            restored = self.device(original['identity'])
            for field in ('connected', 'main', 'resolution', 'hiDPI', 'placement', 'rotation'):
                self.assertEqual(restored[field], original[field], (original['identity'], field))
        self.assertFalse(self.device(c.TARGET_UUID)['connected'])

    def test_new_catalog_profile_save_failure_recovers_without_discovery_busy_loop(self):
        self.enable()
        before = copy.deepcopy(self.coordinator.config)
        provisions = len(self.provisioner.calls)
        with patch.object(self.coordinator.store, 'save', side_effect=OSError('profile disk unavailable')) as save:
            self.connect('iPad mini (A17 Pro)')
            for _ in range(4):
                self.advance(4)
                self.tick(self.signal(names=['iPad mini (A17 Pro)']))
            self.assertEqual(save.call_count, 1)
        self.assertEqual(self.engine.state['mode'], 'ARMED')
        self.assertIsNone(self.engine.state['journal'])
        self.assertTrue(self.device('physical1')['connected'])
        self.assertFalse(self.device(c.TARGET_UUID)['connected'])
        self.assertEqual(self.coordinator.config, before)
        self.assertEqual(len(self.provisioner.calls), provisions)
        # Removing the injected error alone must not silently retry discovery.
        for _ in range(3):
            self.advance(1)
            self.tick(self.signal(names=['iPad mini (A17 Pro)']))
        self.assertEqual(self.coordinator.config, before)
        self.assertEqual(len(self.provisioner.calls), provisions)
        self.tick(self.signal(names=['iPad mini (A17 Pro)']), action='enable')
        self.assertEqual(self.engine.state['mode'], 'ACTIVE')
        self.assertEqual(len(self.coordinator.config['profiles']), len(before['profiles']) + 1)
        self.assertEqual(len(self.provisioner.calls), provisions + 1)

    def test_new_catalog_provision_failure_reserves_and_reuses_virtual_identity(self):
        self.enable()
        self.provisioner.fail = True
        self.connect('iPad mini (A17 Pro)')
        profiles = self.coordinator.store.load()['profiles']
        reserved = [profile for profile in profiles if profile['catalog_id'] == 'ipad-mini-a17-pro']
        self.assertEqual(len(reserved), 1)
        reserved = reserved[0]
        self.assertTrue(self.device('physical1')['connected'])
        self.assertFalse(self.device(c.TARGET_UUID)['connected'])
        attempts = len(self.provisioner.calls)
        for _ in range(3):
            self.advance(1)
            self.tick(self.signal(names=['iPad mini (A17 Pro)']))
        self.assertEqual(len(self.provisioner.calls), attempts)
        self.provisioner.fail = False
        for _ in range(2):
            self.advance(4)
            self.tick(self.signal('disconnected', []))
        self.tick(self.signal(names=['iPad mini (A17 Pro)']), action='enable')
        self.assertEqual(self.engine.state['mode'], 'ACTIVE')
        self.assertEqual(self.engine.state['active_profile_id'], reserved['id'])
        self.assertEqual(self.coordinator.store.load()['profiles'], profiles)
        self.assertEqual(self.provisioner.calls[-1]['virtual'], reserved['virtual'])
        self.assertTrue(self.device(reserved['virtual']['identity'])['connected'])


    def test_handover_final_profile_save_failure_restores_cursor_projection(self):
        b = self.start_pair()
        config = copy.deepcopy(self.coordinator.config)
        cursor = c.read_json(self.directory / 'cursor-profile-config.json')
        projected = []
        def fail_save(candidate):
            projected.append(c.read_json(self.directory / 'cursor-profile-config.json'))
            self.assertEqual(projected[-1]['scale'], b['cursor_scale'])
            raise OSError('Final profile commit failed')
        with patch.object(self.coordinator.store, 'save', side_effect=fail_save) as save:
            self.connect('Tablet B')
            self.assertEqual(save.call_count, 1)
        self.assertEqual(len(projected), 1, 'Failure must occur after new cursor projection')
        self.assertEqual(self.coordinator.config, config)
        self.assertEqual(self.coordinator.store.load(), config)
        self.assertEqual(c.read_json(self.directory / 'cursor-profile-config.json'), cursor)
        self.assertIsNone(self.engine.state['journal'])
        self.assertTrue(self.device('physical1')['connected'])
        self.assert_managed_off(b)

    def test_handover_cursor_write_then_raise_restores_previous_projection(self):
        b = self.start_pair()
        config = copy.deepcopy(self.coordinator.config)
        cursor = c.read_json(self.directory / 'cursor-profile-config.json')
        original = self.coordinator._cursor
        projected = []
        def write_then_fail(profile):
            original(profile)
            if profile['id'] == b['id']:
                projected.append(c.read_json(self.directory / 'cursor-profile-config.json'))
                raise OSError('Cursor replace succeeded before failure')
        with patch.object(self.coordinator, '_cursor', side_effect=write_then_fail):
            self.connect('Tablet B')
        self.assertEqual(len(projected), 1)
        self.assertEqual(projected[0]['scale'], b['cursor_scale'])
        self.assertEqual(self.coordinator.config, config)
        self.assertEqual(self.coordinator.store.load(), config)
        self.assertEqual(c.read_json(self.directory / 'cursor-profile-config.json'), cursor)
        self.assertIsNone(self.engine.state['journal'])
        self.assertTrue(self.device('physical1')['connected'])
        self.assert_managed_off(b)
