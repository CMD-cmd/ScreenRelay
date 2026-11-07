import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

import controller as c
import device_profiles as p
from profile_runtime import DeviceCoordinator, VirtualProvisioner
from test_controller import FakeBD, display, target


class Provisioner:
    match = staticmethod(VirtualProvisioner.match)
    def __init__(self, bd):
        self.bd, self.calls = bd, []
        self.fail = False
    def ensure(self, profile):
        self.calls.append(copy.deepcopy(profile))
        if self.fail:
            raise RuntimeError('Injected provision failure')
        existing = self.match(self.bd.devices, profile)
        if existing:
            assert not existing['connected'], 'Cannot redefine a live virtual output'
        else:
            existing = display(profile['virtual']['identity'], False, False, 'VirtualScreen')
            existing['name'] = profile['virtual']['name']
            self.bd.devices.append(existing)
        existing['resolution'] = profile['resolution']
        existing['hiDPI'] = 'on'
        return existing


class ProfileRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self.tmp.name)
        self.now = 1000.0
        self.bd = FakeBD([display(), target()])
        self.bd.directory = self.directory
        self.engine = c.Engine(self.bd, self.directory, clock=lambda: self.now,
                               monotonic=lambda: self.now, sleeper=self.advance,
                               signal_max_age=90, event_driven=True)
        self.engine.state['preferred_virtual_mode'] = dict(c.MODE, resolution='1280x960')
        self.engine._save()
        self.provisioner = Provisioner(self.bd)
        self.coordinator = DeviceCoordinator(self.engine, self.provisioner)
        self.serial = 0
    def tearDown(self):
        self.tmp.cleanup()
    def advance(self, seconds):
        self.now += seconds
    def signal(self, state='connected', names=None, **extra):
        return dict(timestamp=self.now, session_observed_at=self.now, state=state, trusted=True,
                    controllers=['iPad Pro (13 英寸, M4)'] if names is None else names,
                    run_id='run', **extra)
    def tick(self, signal=None, action=None, **fields):
        self.serial += 1
        command = dict(id=str(self.serial), action=action, **fields) if action else None
        if command:
            c.atomic_json(self.directory / 'command.json', command)
        return self.coordinator.tick(self.signal() if signal is None else signal, command)
    def enable(self):
        self.tick(action='enable')
        self.assertEqual(self.engine.state['mode'], 'ACTIVE')
    def test_existing_profile_migration_and_event_driven_idle(self):
        self.enable()
        self.assertEqual(self.coordinator.config['profiles'][0]['resolution'], '1280x960')
        inventories = self.engine.metrics['inventory_calls']
        calls = len(self.provisioner.calls)
        for _ in range(3):
            self.advance(25); self.tick()
        self.assertEqual(self.engine.metrics['inventory_calls'], inventories)
        self.assertEqual(len(self.provisioner.calls), calls)
    def test_workbench_recovery_progress_is_not_a_sticky_error(self):
        self.enable()
        self.engine._begin_restore('Remote disconnected', afterwards='ARMED', category='remote_disconnected')
        self.coordinator.publish(self.signal('disconnected', []))
        self.assertIsNone(c.read_json(self.directory / 'workbench-status.json')['error'])
        self.engine.state['last_error'] = 'Physical output verification failed'
        self.coordinator.publish(self.signal('disconnected', []))
        self.assertEqual(c.read_json(self.directory / 'workbench-status.json')['error'],
                         'Physical output verification failed')
    def test_workbench_status_shares_menu_bar_status_fields(self):
        self.enable()
        snapshot = c.read_json(self.directory / 'workbench-status.json')
        self.assertIs(snapshot['trusted'], True)
        self.assertIs(snapshot['sleeping'], False)
        self.assertEqual(snapshot['mode'], 'ACTIVE')
        self.assertEqual(snapshot['status_detail'].get('next_retry'), 0)
        self.engine._begin_restore('Physical output verification failed', afterwards='ARMED',
                                   category='safety_fault')
        self.coordinator.publish({**self.signal(), 'trusted': False, 'sleeping': True})
        snapshot = c.read_json(self.directory / 'workbench-status.json')
        self.assertIs(snapshot['trusted'], False)
        self.assertIs(snapshot['sleeping'], True)
        self.assertEqual(snapshot['status_detail']['last_error'], 'Physical output verification failed')
        self.assertEqual(snapshot['status_detail']['recovery_context']['category'], 'safety_fault')
    def test_unknown_multiple_and_stale_do_not_create_or_busy_loop(self):
        for detector in (self.signal(names=['My iPad']), self.signal(names=['iPad', 'iPad']),
                         self.signal(timestamp_override=True)):
            if detector.pop('timestamp_override', False): detector['timestamp'] -= 100
            effective = self.tick(detector, action='enable')
            self.assertEqual(self.engine.state['mode'], 'ARMED')
            self.assertFalse(self.provisioner.calls)
            self.assertGreater(self.engine.next_timeout(effective), 0)
            self.assertTrue(self.bd.devices[0]['connected'])
    def test_new_known_device_creates_once_and_reuses_on_reconnect(self):
        detector = self.signal(names=['iPad mini (A17 Pro)'])
        self.tick(detector, action='enable')
        selected = self.coordinator._profile()
        self.assertEqual(selected['catalog_id'], 'ipad-mini-a17-pro')
        self.assertEqual(selected['cursor_scale'], 3.5)
        self.assertEqual(len(self.coordinator.config['profiles']), 2)
        self.assertEqual(self.engine.state['mode'], 'ACTIVE')
        for _ in range(3):
            self.advance(4); self.tick(self.signal('disconnected', []))
        self.assertFalse(next(d for d in self.bd.devices if d['identity'] == selected['virtual']['identity'])['connected'])
        self.tick(self.signal(names=['iPad mini (A17 Pro)']))
        self.assertEqual(self.engine.state['mode'], 'ACTIVE')
        self.assertEqual(len(self.coordinator.config['profiles']), 2)
        self.assertEqual(len(self.provisioner.calls), 2)
    def test_multiple_keep_session(self):
        self.enable()
        count = len(self.provisioner.calls)
        self.tick(self.signal(names=['iPad', 'iPad'], activeControllerCount=2))
        self.assertEqual(len(self.provisioner.calls), count)
        self.assertEqual(self.engine.state['active_profile_id'], p.LEGACY_ID)
        self.assertEqual(self.engine.state['mode'], 'ACTIVE')
    def test_orientation_apply_keeps_physical_off_and_disconnect_recovers(self):
        self.enable()
        baseline = copy.deepcopy({k: self.engine.state['journal'][k]
                                  for k in ('physical', 'created', 'protections')})
        self.bd.calls.clear()
        self.tick(action='profile_apply', profile_id=p.LEGACY_ID,
                  draft={'orientation': 'portrait', 'resolution': '960x1280'})
        self.assertEqual(self.engine.state['mode'], 'ACTIVE')
        self.assertEqual(self.engine._preferred_mode()['resolution'], '960x1280')
        self.assertEqual(self.coordinator._profile()['orientation'], 'portrait')
        self.assertEqual(self.coordinator.last_result['status'], 'ok')
        self.assertFalse(any(identity == 'physical1' and fields.get('connected') is True
                             for identity, fields in self.bd.calls))
        buffer_on = next(i for i, (identity, f) in enumerate(self.bd.calls)
                         if identity not in ('physical1', c.TARGET_UUID) and f.get('connected') is True)
        virtual_off = next(i for i, (identity, f) in enumerate(self.bd.calls)
                           if identity == c.TARGET_UUID and f.get('connected') is False)
        self.assertLess(buffer_on, virtual_off)
        self.assertEqual({k: self.engine.state['journal'][k] for k in baseline}, baseline)
        self.assertFalse(self.engine.state['journal'].get('transient_virtuals'))
        self.assertEqual(len(self.bd.devices), 2)
        for _ in range(3):
            self.advance(4); self.tick(self.signal('disconnected', []))
        self.assertEqual(self.engine.state['mode'], 'ARMED')
        self.assertTrue(self.bd.devices[0]['connected'])
        self.assertFalse(self.bd.devices[1]['connected'])
    def test_offline_save_never_touches_betterdisplay(self):
        self.tick(self.signal('disconnected', []), action='profile_apply', profile_id=p.LEGACY_ID,
                  draft={'orientation': 'portrait', 'resolution': '960x1280'})
        self.assertFalse(self.provisioner.calls)
        self.assertFalse(self.bd.calls)
        self.assertEqual(self.coordinator._profile()['resolution'], '960x1280')
        self.assertIn('下次', self.coordinator.last_result['message'])
    def test_invalid_draft_keeps_active_journal_and_mode(self):
        self.enable()
        journal = copy.deepcopy(self.engine.state['journal'])
        calls = len(self.bd.calls)
        self.tick(action='profile_apply', profile_id=p.LEGACY_ID, draft={'resolution': '9000x9000'})
        self.assertEqual(self.engine.state['mode'], 'ACTIVE')
        self.assertEqual(self.engine.state['journal'], journal)
        self.assertEqual(len(self.bd.calls), calls)
        self.assertEqual(self.coordinator.last_result['status'], 'error')
    def test_cursor_only_does_not_reconfigure_displays_and_is_per_profile(self):
        self.enable()
        calls = len(self.bd.calls)
        self.tick(action='profile_cursor', scale=2.5)
        self.assertEqual(len(self.bd.calls), calls)
        self.assertEqual(self.coordinator._profile()['cursor_scale'], 2.5)
        self.assertEqual(c.read_json(self.directory / 'cursor-profile-config.json')['scale'], 2.5)
    def test_binding_unknown_name_prepares_selected_profile(self):
        self.tick(self.signal('disconnected', []), action='profile_apply', profile_id=p.LEGACY_ID,
                  draft={}, bind_name='我的平板')
        self.tick(self.signal(names=['我的平板']), action='enable')
        self.assertEqual(self.engine.state['mode'], 'ACTIVE')
        self.assertEqual(self.coordinator.identity['source'], 'binding')
    def test_failed_provision_keeps_physical_output_and_original_profile(self):
        self.enable()
        self.provisioner.fail = True
        self.tick(action='profile_apply', profile_id=p.LEGACY_ID,
                  draft={'orientation': 'portrait', 'resolution': '960x1280'})
        self.assertTrue(self.bd.devices[0]['connected'])
        self.assertFalse(self.bd.devices[1]['connected'])
        self.assertEqual(self.coordinator._profile()['orientation'], 'landscape')
        self.assertEqual(self.coordinator.last_result['status'], 'error')
    def test_pause_during_profile_entry_interrupts_and_receipts_survive(self):
        self.enable()
        original = self.bd.set
        fired = False
        def setting(device, **fields):
            nonlocal fired
            original(device, **fields)
            if not fired and device['kind'] == 'VirtualScreen' and fields.get('connected') is True:
                fired = True
                c.atomic_json(self.directory / 'command.json', {'id': 'urgent', 'action': 'pause'})
        self.bd.set = setting
        self.tick(action='profile_apply', profile_id=p.LEGACY_ID,
                  draft={'orientation': 'portrait', 'resolution': '960x1280'})
        self.assertEqual(self.engine.state['mode'], 'PAUSED')
        self.assertFalse(self.engine.state['desired_enabled'])
        self.assertTrue(self.bd.devices[0]['connected'])
        self.assertFalse(self.bd.devices[1]['connected'])
    def test_pause_preempts_automatic_provisioning(self):
        self.tick(self.signal('disconnected', []), action='enable')
        self.tick(self.signal(names=['iPad mini (A17 Pro)']), action='pause')
        self.assertFalse(self.provisioner.calls)
        self.assertEqual(self.engine.state['mode'], 'PAUSED')
    def test_pause_after_provision_before_capture(self):
        self.enable()
        original = self.provisioner.ensure
        def ensure(profile):
            result = original(profile)
            c.atomic_json(self.directory / 'command.json', {'id': 'pause-before-capture', 'action': 'pause'})
            return result
        self.provisioner.ensure = ensure
        self.tick(action='profile_apply', profile_id=p.LEGACY_ID,
                  draft={'orientation': 'portrait', 'resolution': '960x1280'})
        self.assertEqual(self.engine.state['mode'], 'PAUSED')
        self.assertFalse(self.engine.state['desired_enabled'])
        self.assertTrue(self.bd.devices[0]['connected'])
        self.assertFalse(self.bd.devices[1]['connected'])
        self.assertTrue(any(r['id'] == 'pause-before-capture' and r['status'] == 'ok'
                            for r in self.coordinator.results))
    def test_cursor_write_failure_keeps_active_display_and_profile(self):
        self.enable()
        before = copy.deepcopy(self.coordinator.config)
        calls = len(self.bd.calls)
        with patch.object(self.coordinator, '_cursor', side_effect=OSError('write failed')):
            self.tick(action='profile_cursor', scale=2)
        self.assertEqual(self.engine.state['mode'], 'ACTIVE')
        self.assertEqual(len(self.bd.calls), calls)
        self.assertEqual(self.coordinator.config, before)
        self.assertEqual(self.coordinator.store.load(), before)
        self.assertEqual(c.read_json(self.directory / 'cursor-profile-config.json')['scale'], 3)
    def test_malformed_durable_command_does_not_kill_coordinator(self):
        self.enable()
        calls = len(self.bd.calls)
        (self.directory / 'command.json').write_text('{broken')
        self.coordinator.tick(self.signal())
        self.assertIsNone(self.coordinator._latest_manual())
        self.assertEqual(self.engine.state['mode'], 'ACTIVE')
        self.assertEqual(len(self.bd.calls), calls)
    def test_live_virtual_does_not_override_explicit_offline_portrait(self):
        self.bd.devices[1].update(connected=True, hiDPI='on', resolution='1280x960')
        self.tick(self.signal('disconnected', []), action='profile_apply', profile_id=p.LEGACY_ID,
                  draft={'orientation': 'portrait', 'resolution': '960x1280'})
        self.enable()
        self.assertEqual(self.engine._preferred_mode()['resolution'], '960x1280')
        self.assertEqual(self.bd.devices[1]['resolution'], '960x1280')
        self.assertEqual(len(self.provisioner.calls), 2)
        self.assertEqual(self.provisioner.calls[-1]['id'], p.LEGACY_ID)
    def test_sole_virtual_can_rebuild_orientation_using_temporary_virtual(self):
        self.bd.devices[0]['connected'] = False
        self.bd.devices[1].update(connected=True, hiDPI='on', resolution='1280x960')
        self.tick(self.signal('disconnected', []), action='profile_apply', profile_id=p.LEGACY_ID,
                  draft={'orientation': 'portrait', 'resolution': '960x1280'})
        self.tick(action='enable')
        self.assertEqual(self.engine.state['mode'], 'ACTIVE')
        self.assertTrue(self.bd.devices[1]['connected'])
        self.assertEqual(self.bd.devices[1]['resolution'], '960x1280')
        self.assertFalse(self.bd.devices[0]['connected'])
        self.assertFalse(any(identity == 'physical1' and fields.get('connected') is True
                             for identity, fields in self.bd.calls))
        self.assertEqual(len(self.bd.devices), 2)
    def test_offline_cursor_updates_selected_not_last_active_device(self):
        self.enable()
        self.tick(action='pause')
        self.tick(self.signal('disconnected', []), action='profile_apply',
                  draft={'catalog_id': 'ipad-mini-a17-pro'})
        self.tick(self.signal('disconnected', []), action='profile_cursor', scale=2)
        self.assertEqual(self.coordinator._profile()['catalog_id'], 'ipad-mini-a17-pro')
        self.assertEqual(self.coordinator._profile()['cursor_scale'], 2)
        self.assertEqual(self.coordinator._profile(p.LEGACY_ID)['cursor_scale'], 3)
    def test_recovery_uses_original_target_after_new_profile_selected(self):
        self.enable()
        new = p.make_profile('ipad-mini-a17-pro')
        self.engine.state['target_spec'] = p.target_spec(new)
        self.tick(action='recover')
        self.assertEqual(self.engine.state['mode'], 'PAUSED')
        self.assertTrue(self.bd.devices[0]['connected'])
        self.assertFalse(self.bd.devices[1]['connected'])
    def test_corrupt_profile_preserved_and_manual_recovery_still_works(self):
        self.enable()
        path = self.directory / 'profiles.json'
        path.write_text('{broken')
        coordinator = DeviceCoordinator(self.engine, self.provisioner)
        coordinator.tick(self.signal(), {'id': 'recover', 'action': 'recover'})
        self.assertTrue(self.bd.devices[0]['connected'])
        self.assertEqual(path.read_text(), '{broken')
    def test_user_mode_memory_survives_profile_reconnect(self):
        self.enable()
        virtual = next(d for d in self.bd.devices if d['kind'] == 'VirtualScreen')
        virtual['resolution'] = '1152x864'
        self.tick(self.signal(display_revision=2))
        self.advance(2); self.tick(self.signal(display_revision=2))
        self.assertEqual(self.coordinator._profile()['resolution'], '1152x864')
        for _ in range(3):
            self.advance(4); self.tick(self.signal('disconnected', []))
        self.tick()
        self.assertEqual(self.engine._preferred_mode()['resolution'], '1152x864')


class ProvisionerTests(unittest.TestCase):
    def test_exact_native_mode_is_fresh_and_identity_checked(self):
        with tempfile.NamedTemporaryFile() as helper:
            bd = c.BetterDisplay(native_helper=helper.name)
            calls = []
            bd._run = lambda *args: calls.append(args) or ''
            device = display('virtual:2198:1:2', True, False, 'VirtualScreen')
            device.update(native_display_id=42, tag='20', mode_tag='21')
            info = dict(displayID=42, vendor=2198, model=1, serial=2, modes=[
                dict(width=1054, height=692, pixelWidth=2108, pixelHeight=1384, refreshRate=60, modeID=12),
                dict(width=1054, height=692, pixelWidth=1054, pixelHeight=692, refreshRate=60, modeID=13)])
            with patch.object(c.subprocess, 'run', return_value=SimpleNamespace(stdout=json.dumps(info))) as run:
                bd.set(device, resolution='1054x692', hiDPI='on', refreshRate='60')
                self.assertEqual(run.call_args.args[0][-2:], ['--modes', '42'])
                self.assertEqual(calls, [('set', '-tagID=21', '-displayModeNumber=12')])
            info['serial'] = 999
            with patch.object(c.subprocess, 'run', return_value=SimpleNamespace(stdout=json.dumps(info))):
                with self.assertRaisesRegex(RuntimeError, 'identity changed'):
                    bd.set(device, resolution='1054x692', hiDPI='on')
            self.assertEqual(len(calls), 1)
    def test_cli_three_column_modes_and_creation_retry_are_idempotent(self):
        class CLI:
            dry_run = False
            def __init__(self):
                self.devices = [display()]
                self.calls = []
                self.values = {}
                self.fail_after_create = True
            def inventory(self): return copy.deepcopy(self.devices)
            def _run(self, action, *arguments):
                self.calls.append((action, arguments))
                values = dict(a[1:].split('=', 1) for a in arguments if '=' in a)
                if action == 'create':
                    identity = 'virtual:' + ':'.join(values[k] for k in
                        ('virtualScreenVendorNumber', 'virtualScreenModelNumber', 'virtualScreenSerial'))
                    device = display(identity, False, False, 'VirtualScreen')
                    device.update(name=values['virtualScreenName'], tag='201', mode_tag='202')
                    self.devices.append(device)
                    self.values.update(values)
                    if self.fail_after_create:
                        self.fail_after_create = False
                        raise RuntimeError('response lost after create')
                    return 'OK'
                if action == 'set':
                    assert values['tagID'] == '201' and values['type'] == 'VirtualScreen'
                    self.values.update(values); return 'OK'
                if action == 'get':
                    assert values['tagID'] == '201' and values['type'] == 'VirtualScreen'
                    key = arguments[-1][1:]
                    if key == 'resolutionList':
                        return '[' + ', '.join('[Optional(%s), Optional(%s), Optional(0)]' % tuple(mode.split('x'))
                                                for mode in self.values[key].split(',')) + ']'
                    return self.values[key]
        with tempfile.TemporaryDirectory() as directory:
            bd = CLI(); service = VirtualProvisioner(bd, directory)
            profile = p.make_profile('ipad-mini-a17-pro', id='stable-retry')
            with self.assertRaisesRegex(RuntimeError, 'response lost'):
                service.ensure(profile)
            self.assertEqual(c.read_json(Path(directory) / 'managed-virtuals.json')['devices'][profile['virtual']['identity']]['phase'], 'reserved')
            device = service.ensure(profile)
            self.assertFalse(device['connected'])
            self.assertEqual(sum(action == 'create' for action, _ in bd.calls), 1)
            self.assertEqual(c.read_json(Path(directory) / 'managed-virtuals.json')['devices'][profile['virtual']['identity']]['phase'], 'configured')
    def test_definition_uses_exact_owned_identity_and_double_pixel_modes(self):
        profile = p.legacy_profile()
        modes = VirtualProvisioner.resolution_list(profile).split(',')
        self.assertIn('2560x1920', modes)
        self.assertIn('2400x1800', modes)
        self.assertIn('2048x1536', modes)
        profile.update(orientation='portrait', resolution='960x1280')
        self.assertEqual(modes, VirtualProvisioner.resolution_list(profile).split(','))
        wrong = target(); wrong['identity'] = 'virtual:1:2:3'
        with self.assertRaisesRegex(RuntimeError, '同名'):
            VirtualProvisioner.match([wrong], profile)


if __name__ == '__main__':
    unittest.main()
