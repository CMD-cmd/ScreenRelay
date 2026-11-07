"""Display-area geometry, persistence, and journaled virtual-screen transitions."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import controller as c
import device_profiles as p
from profile_runtime import DeviceCoordinator, VirtualProvisioner
import test_profile_runtime as fixtures


def area(landscape='safe', portrait='full', landscape_inset=7, portrait_inset=7):
    return {'landscape': {'mode': landscape, 'inset_percent': landscape_inset},
            'portrait': {'mode': portrait, 'inset_percent': portrait_inset}}


class DisplayAreaProfileTests(unittest.TestCase):
    def test_legacy_roundtrip_keeps_specs_preferences_and_identity(self):
        old = p.make_profile('iphone-16-pro-max', resolution='1434x660', cursor_scale=2.5)
        self.assertEqual(p.validate_profile(old), old)
        self.assertNotIn('display_area', old)
        self.assertEqual(p.display_area_settings(old), area('full'))
        safe = dict(old, display_area=area())
        safe['resolution'] = p.fit_resolution(old['resolution'], safe)
        self.assertEqual(safe['resolution'], '1233x660')
        self.assertEqual(p.canvas_dimensions(safe), (2466, 1320, 'landscape'))
        p.validate_profile(safe)
        full = dict(safe, display_area=area('full'))
        full['resolution'] = p.fit_resolution(safe['resolution'], full)
        self.assertEqual(full['resolution'], old['resolution'])
        for key in ('native_width', 'native_height', 'ppi', 'virtual', 'cursor_scale'):
            self.assertEqual(safe[key], old[key])
        with tempfile.TemporaryDirectory() as directory:
            store = p.ProfileStore(directory)
            config = dict(version=1, automatic=True, selected_id=safe['id'], profiles=[safe], bindings={})
            self.assertEqual(store.save(config), store.load())
            self.assertEqual(store.load()['profiles'][0], safe)

    def test_all_catalogs_and_both_directions_keep_valid_2x_modes(self):
        for item in p.CATALOG:
            for orientation in ('landscape', 'portrait'):
                for inset in (0, 7, 12):
                    with self.subTest(item=item['id'], orientation=orientation, inset=inset):
                        profile = p.make_profile(item['id'], orientation=orientation,
                            display_area=area('safe', 'safe', inset, inset))
                        cw, ch, _ = p.canvas_dimensions(profile)
                        self.assertEqual(min(cw, ch), min(item['native_width'], item['native_height']))
                        for mode in p.presets(profile):
                            p.validate_profile(dict(profile, resolution=mode))
                            w, h = map(int, mode.split('x'))
                            self.assertLessEqual(w * 2, cw + 2)
                            self.assertLessEqual(h * 2, ch + 2)
                            self.assertEqual(p.preferred_mode(dict(profile, resolution=mode))['hiDPI'], 'on')

    def test_independent_directions_generate_both_sets_of_modes(self):
        profile = p.make_profile('iphone-16-pro-max', display_area=area('safe', 'safe', 7, 10))
        self.assertEqual(p.presets(profile)[0], '1233x660')
        self.assertEqual(p.presets(profile, 'portrait')[0], '660x1147')
        sizes = set(VirtualProvisioner.resolution_list(profile).split(','))
        self.assertTrue({'1233x660', '2466x1320', '1147x660', '2294x1320'} <= sizes)
        self.assertEqual(profile['resolution'], '986x528')

    def test_missing_direction_defaults_and_invalid_values_are_rejected(self):
        phone = p.make_profile('iphone-16-pro-max')
        partial = dict(phone, display_area={'portrait': {'mode': 'safe', 'inset_percent': 8.5}})
        normalized = p.validate_profile(partial)
        self.assertEqual(normalized['display_area']['landscape'], {'mode': 'full', 'inset_percent': 7.0})
        self.assertEqual(normalized['display_area']['portrait'], {'mode': 'safe', 'inset_percent': 8.5})
        for value in (None, [], True, {'wrong': {}}, {'portrait': None}, {'portrait': {'extra': 1}},
                      {'portrait': {'mode': 'other'}}, {'portrait': {'inset_percent': True}},
                      {'portrait': {'inset_percent': float('nan')}}, {'portrait': {'inset_percent': float('inf')}},
                      {'portrait': {'inset_percent': -1}}, {'portrait': {'inset_percent': 12.5}},
                      {'portrait': {'inset_percent': 6.2}}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                p.validate_profile(dict(phone, display_area=value))
        square = p.make_profile(label='square', native_width=2000, native_height=2000, ppi=300)
        with self.assertRaises(ValueError):
            p.make_profile(label='square', native_width=2000, native_height=2000, ppi=300, display_area=area())
        self.assertEqual(p.canvas_dimensions(square)[:2], (2000, 2000))

    def test_custom_size_and_boundary_validation_use_effective_canvas(self):
        profile = p.make_profile('iphone-16-pro-max', display_area=area())
        p.validate_profile(dict(profile, resolution='1121x600'))
        with self.assertRaises(ValueError):
            p.validate_profile(dict(profile, resolution='1434x660'))
        with self.assertRaises(ValueError):
            p.validate_profile(dict(profile, native_width=2800))
        self.assertEqual(p.preferred_mode(profile)['resolution'], profile['resolution'])


class DisplayAreaRuntimeTests(unittest.TestCase):
    setUp = fixtures.ProfileRuntimeTests.setUp
    tearDown = fixtures.ProfileRuntimeTests.tearDown
    advance = fixtures.ProfileRuntimeTests.advance
    signal = fixtures.ProfileRuntimeTests.signal
    tick = fixtures.ProfileRuntimeTests.tick

    def connect_phone(self):
        self.tick(self.signal(names=['iPhone 16 Pro Max']), action='enable')
        self.assertEqual(self.engine.state['mode'], 'ACTIVE')
        return copy.deepcopy(self.coordinator._profile())

    def apply_area(self, settings, orientation=None):
        profile = copy.deepcopy(self.coordinator._profile())
        profile['display_area'] = settings
        if orientation:
            profile['orientation'] = orientation
        profile['resolution'] = p.fit_resolution(profile['resolution'], profile)
        self.tick(self.signal(names=['iPhone 16 Pro Max']), action='profile_apply', profile_id=profile['id'],
                  draft={k: profile[k] for k in ('display_area', 'orientation', 'resolution')})
        return profile

    def assert_no_buffers(self):
        self.assertFalse(any(d['kind'] == 'VirtualScreen' and d['name'].startswith('UU · 切换缓冲屏')
                             for d in self.bd.devices))
        journal = self.engine.state.get('journal')
        if journal:
            self.assertFalse(journal.get('transient_virtuals'))

    def test_live_range_roundtrip_preserves_baseline_and_uses_virtual_buffer(self):
        phone = self.connect_phone()
        baseline = copy.deepcopy({k: self.engine.state['journal'][k] for k in ('physical', 'created', 'protections')})
        self.bd.calls.clear()
        for settings in (area(), area('safe', 'safe', 9.5, 10), area('full', 'safe', 9.5, 10)):
            expected = self.apply_area(settings)
            self.assertEqual(self.coordinator.last_result['status'], 'ok')
            self.assertEqual(self.engine.state['mode'], 'ACTIVE')
            actual = self.coordinator._profile()
            self.assertEqual(actual['resolution'], expected['resolution'])
            self.assertEqual(actual['virtual'], phone['virtual'])
            self.assertEqual(actual['cursor_scale'], phone['cursor_scale'])
            self.assertEqual({k:self.engine.state['journal'][k] for k in baseline}, baseline)
            self.assert_no_buffers()
        self.assertFalse(any(i == 'physical1' and f.get('connected') is True for i, f in self.bd.calls))
        self.assertEqual(self.coordinator._profile()['resolution'], phone['resolution'])

    def test_inactive_direction_save_changes_no_output(self):
        self.connect_phone()
        self.bd.calls.clear(); provisions = len(self.provisioner.calls)
        expected = self.apply_area(area('full', 'safe', 7, 10))
        self.assertEqual(self.coordinator.last_result['status'], 'ok')
        self.assertEqual(self.bd.calls, [])
        self.assertEqual(len(self.provisioner.calls), provisions)
        self.assertEqual(self.coordinator._profile()['resolution'], expected['resolution'])

    def test_area_survives_rotation_reconnect_and_profile_handover(self):
        phone = self.connect_phone()
        expected = self.apply_area(area('safe', 'safe', 7, 10))
        portrait = self.apply_area(expected['display_area'], 'portrait')
        self.assertEqual(self.coordinator._profile()['resolution'], portrait['resolution'])
        for _ in range(3):
            self.advance(4); self.tick(self.signal('disconnected', []))
        self.assertTrue(self.bd.devices[0]['connected'])
        for name in ['iPhone 16 Pro Max', 'iPad Pro (13 英寸, M4)', 'iPhone 16 Pro Max']:
            for _ in range(3):
                self.advance(.2); self.tick(self.signal(names=[name]))
        saved = self.coordinator._profile()
        self.assertEqual(saved['virtual'], phone['virtual'])
        self.assertEqual(saved['display_area'], p.display_area_settings(portrait))
        self.assertEqual(saved['resolution'], portrait['resolution'])
        self.assertEqual(saved['cursor_scale'], phone['cursor_scale'])
        before = copy.deepcopy(saved); inventory_count = self.engine.metrics['inventory_calls']
        for _ in range(12):
            self.advance(25); self.tick(self.signal(names=['iPhone 16 Pro Max']))
        self.assertEqual(self.coordinator._profile(), before)
        self.assertEqual(self.engine.metrics['inventory_calls'], inventory_count)

    def test_pause_during_range_change_restores_and_preserves_saved_configuration(self):
        phone = self.connect_phone(); original = self.bd.set; fired = []
        def setting(device, **fields):
            original(device, **fields)
            if not fired and device['kind'] == 'VirtualScreen' and device['identity'] != phone['virtual']['identity'] and fields.get('connected') is True:
                fired.append(True)
                c.atomic_json(self.directory/'command.json', {'id':'safe-pause', 'action':'pause'})
        self.bd.set = setting
        self.apply_area(area())
        self.assertTrue(fired)
        self.assertEqual(self.engine.state['mode'], 'PAUSED')
        self.assertTrue(self.bd.devices[0]['connected'])
        self.assertIsNone(self.engine.state['journal'])
        self.assertEqual(self.coordinator._profile(), phone)
        self.assert_no_buffers()

    def test_provision_failure_recovers_physical_screen_and_original_profile(self):
        phone = self.connect_phone(); self.provisioner.fail = True
        self.apply_area(area())
        self.assertTrue(self.bd.devices[0]['connected'])
        self.assertEqual(self.coordinator._profile(), phone)
        self.assertEqual(self.coordinator.last_result['status'], 'error')
        self.assert_no_buffers()

    def test_offline_save_is_durable_and_makes_no_display_calls(self):
        phone = self.connect_phone()
        self.tick(self.signal('disconnected', []), action='pause')
        self.bd.calls.clear(); count = len(self.provisioner.calls)
        expected = self.apply_area(area())
        self.assertEqual(self.coordinator.last_result['status'], 'ok')
        self.assertEqual(self.bd.calls, [])
        self.assertEqual(len(self.provisioner.calls), count)
        self.assertEqual(p.ProfileStore(self.directory).load()['profiles'][-1]['resolution'], expected['resolution'])

    def test_old_live_aspect_is_entered_then_rebuilt_in_virtual_space(self):
        phone = self.connect_phone()
        self.tick(self.signal('disconnected', []), action='pause')
        expected = self.apply_area(area())
        device = next(d for d in self.bd.devices if d['identity'] == phone['virtual']['identity'])
        device.update(connected=True, resolution=phone['resolution'], hiDPI='on')
        self.bd.devices[0]['connected'] = True
        self.bd.calls.clear()
        self.tick(self.signal(names=['iPhone 16 Pro Max']), action='enable')
        self.assertEqual(self.engine.state['mode'], 'ACTIVE')
        self.assertEqual(device['resolution'], expected['resolution'])
        self.assertFalse(any(i=='physical1' and f.get('connected') is True for i,f in self.bd.calls))
        self.assert_no_buffers()

    def test_missing_live_custom_mode_is_prepared_through_buffer(self):
        phone = self.connect_phone()
        original_ensure = self.provisioner.ensure
        available = {phone['resolution']}
        def query(device, fields):
            if device['identity'] == phone['virtual']['identity'] and fields['resolution'] not in available:
                raise RuntimeError('Requested HiDPI mode is not available on the exact virtual output')
            return 1
        def ensure(profile):
            result = original_ensure(profile)
            if profile['id'] == phone['id']:
                available.add(profile['resolution'])
            return result
        self.bd._virtual_mode_number = query; self.provisioner.ensure = ensure
        self.bd.calls.clear()
        self.tick(self.signal(names=['iPhone 16 Pro Max']), action='profile_apply', profile_id=phone['id'],
                  draft={'resolution':'1304x600'})
        self.assertEqual(self.coordinator.last_result['status'], 'ok')
        self.assertEqual(self.coordinator._profile()['resolution'], '1304x600')
        self.assertFalse(any(i=='physical1' and f.get('connected') is True for i,f in self.bd.calls))
        self.assert_no_buffers()

    def test_process_exit_after_old_output_retires_restores_original_configuration(self):
        phone = self.connect_phone(); original = self.bd.set
        class ProcessExit(BaseException):
            pass
        def setting(device, **fields):
            original(device, **fields)
            if device['identity'] == phone['virtual']['identity'] and fields.get('connected') is False:
                raise ProcessExit()
        self.bd.set = setting
        with self.assertRaises(ProcessExit):
            self.apply_area(area())
        self.bd.set = original
        self.engine = c.Engine(self.bd, self.directory, clock=lambda:self.now, monotonic=lambda:self.now,
                               sleeper=self.advance, signal_max_age=90, event_driven=True)
        self.coordinator = DeviceCoordinator(self.engine, self.provisioner)
        self.tick(self.signal('disconnected', [], session_negative_verified=True))
        self.assertTrue(self.bd.devices[0]['connected'])
        self.assertEqual(self.coordinator._profile(), phone)
        self.assertIsNone(self.engine.state['journal'])
        self.assert_no_buffers()

    def test_recovered_terminal_receipt_remains_visible_after_another_command(self):
        self.connect_phone(); expected = self.apply_area(area())
        terminal = copy.deepcopy(self.coordinator.last_result)
        history = c.read_json(self.directory/'command-results.json')
        for receipt in history:
            if receipt['id'] == terminal['id']:
                receipt['status'] = 'applying'
        c.atomic_json(self.directory/'command-results.json', history)
        intent = c.read_json(self.directory/'command-intent.json'); intent['status'] = 'applying'
        c.atomic_json(self.directory/'command-intent.json', intent)
        self.coordinator = DeviceCoordinator(self.engine, self.provisioner)
        self.assertTrue(any(r['id'] == terminal['id'] and r['status'] == 'ok'
                            for r in c.read_json(self.directory/'command-results.json')))
        self.tick(self.signal(names=['iPhone 16 Pro Max']), action='recover')
        snapshot = c.read_json(self.directory/'workbench-status.json')
        self.assertNotEqual(snapshot['last_result']['id'], terminal['id'])
        self.assertTrue(any(r['id'] == terminal['id'] and r['status'] == 'ok' for r in snapshot['results']))
        self.assertEqual(self.coordinator._profile()['resolution'], expected['resolution'])

    def test_terminal_history_precedes_latest_receipt_write(self):
        self.connect_phone(); original = c.atomic_json
        class ProcessExit(BaseException):
            pass
        def write(path, value):
            if Path(path).name == 'command-result.json' and value.get('status') == 'ok':
                raise ProcessExit()
            return original(path, value)
        with patch('profile_runtime.atomic_json', side_effect=write), self.assertRaises(ProcessExit):
            self.apply_area(area())
        latest = c.read_json(self.directory/'command-result.json')
        self.assertEqual(latest['status'], 'applying')
        self.coordinator = DeviceCoordinator(self.engine, self.provisioner)
        self.assertEqual(self.coordinator.last_result['id'], latest['id'])
        self.assertEqual(self.coordinator.last_result['status'], 'ok')
        self.assertTrue(any(r['id'] == latest['id'] and r['status'] == 'ok'
                            for r in c.read_json(self.directory/'command-results.json')))
