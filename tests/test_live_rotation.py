"""Live orientation handoff and durable recovery, without a physical detour."""
import copy
import unittest

import controller as c
import device_profiles as p
from profile_runtime import DeviceCoordinator
from test_controller import display
import test_profile_runtime as runtime_tests


class ProcessExit(BaseException):
    """Simulate termination outside the ordinary exception/recovery path."""


class LiveRotationTests(unittest.TestCase):
    # Reuse the runtime fixtures without inheriting its separate test cases.
    setUp = runtime_tests.ProfileRuntimeTests.setUp
    tearDown = runtime_tests.ProfileRuntimeTests.tearDown
    advance = runtime_tests.ProfileRuntimeTests.advance
    signal = runtime_tests.ProfileRuntimeTests.signal
    tick = runtime_tests.ProfileRuntimeTests.tick
    enable = runtime_tests.ProfileRuntimeTests.enable

    def rotate(self, orientation='portrait'):
        resolution = '960x1280' if orientation == 'portrait' else '1280x960'
        return self.tick(action='profile_apply', profile_id=p.LEGACY_ID,
                         draft={'orientation': orientation, 'resolution': resolution})

    def original(self):
        return next(d for d in self.bd.devices if d['identity'] == c.TARGET_UUID)

    def buffers(self):
        return [d for d in self.bd.devices if d['kind'] == 'VirtualScreen'
                and d['identity'] != c.TARGET_UUID and d['identity'] != 'unowned']

    def assert_clean(self):
        self.assertFalse(self.buffers())
        journal = self.engine.state.get('journal')
        if journal:
            self.assertFalse(journal.get('transient_virtuals'))
            self.assertEqual(set(journal.get('virtual_targets', {})), {c.TARGET_UUID})
        records = c.read_json(self.directory / 'managed-virtuals.json', {'devices': {}})
        self.assertFalse(any(r['profile_id'].startswith('transition-')
                             for r in records['devices'].values()))
        self.assertFalse(any('切换缓冲屏' in s.get('name', '')
                             for s in self.engine.state.get('managed_virtual_targets', [])))

    def assert_restored(self):
        self.assertTrue(self.bd.devices[0]['connected'])
        self.assertTrue(self.bd.devices[0]['main'])
        self.assertFalse(self.original()['connected'])
        self.assertIsNone(self.engine.state['journal'])
        self.assert_clean()

    def hook_stage(self, stage, callback):
        original_set = self.bd.set
        fired = []
        def setting(device, **fields):
            original_set(device, **fields)
            identity = device['identity']
            hit = ((stage == 'buffer_connected' and identity != c.TARGET_UUID
                    and device['kind'] == 'VirtualScreen' and fields.get('connected') is True)
                   or (stage == 'original_offline' and identity == c.TARGET_UUID
                       and fields.get('connected') is False)
                   or (stage == 'original_connected' and identity == c.TARGET_UUID
                       and fields.get('connected') is True))
            if hit and not fired:
                fired.append(stage)
                callback()
        self.bd.set = setting
        return fired

    def reserve_like_real_provisioner(self):
        original_ensure = self.provisioner.ensure
        def ensure(profile):
            path = self.directory / 'managed-virtuals.json'
            records = c.read_json(path, {'version': 1, 'devices': {}})
            records['devices'][profile['virtual']['identity']] = {
                'profile_id': profile['id'], 'virtual': copy.deepcopy(profile['virtual']),
                'phase': 'reserved'}
            c.atomic_json(path, records)
            return original_ensure(profile)
        self.provisioner.ensure = ensure

    def test_round_trip_preserves_identity_preferences_and_session_baseline(self):
        self.enable()
        candidate = copy.deepcopy(self.coordinator.config)
        candidate['profiles'].append(p.make_profile('ipad-mini-a17-pro'))
        self.coordinator.config = self.coordinator.store.save(candidate)
        baseline = copy.deepcopy(self.engine.state['journal'])
        before = copy.deepcopy(self.coordinator.config)
        self.bd.calls.clear()
        for orientation, resolution in [('portrait', '960x1280'), ('landscape', '1280x960')]:
            self.rotate(orientation)
            self.assertEqual(self.engine.state['mode'], 'ACTIVE')
            self.assertEqual(self.original()['identity'], c.TARGET_UUID)
            self.assertEqual(self.original()['resolution'], resolution)
            self.assertEqual(self.original()['hiDPI'], 'on')
            self.assertTrue(self.original()['main'])
            self.assertEqual(self.coordinator._profile()['orientation'], orientation)
            self.assertEqual(self.coordinator._profile()['cursor_scale'], before['profiles'][0]['cursor_scale'])
            for key in ('physical', 'created', 'protections'):
                self.assertEqual(self.engine.state['journal'].get(key), baseline.get(key))
            self.assertEqual(len(self.coordinator.config['profiles']), len(before['profiles']))
            self.assertEqual(self.coordinator.config['profiles'][1], before['profiles'][1])
            self.assert_clean()
        self.assertFalse(any(identity == 'physical1' and fields.get('connected') is True
                             for identity, fields in self.bd.calls))

    def test_each_retirement_follows_inventory_verified_main_hidpi_replacement(self):
        self.enable()
        snapshots = []
        original_inventory, original_set = self.bd.inventory, self.bd.set
        def inventory():
            result = original_inventory()
            snapshots.append(copy.deepcopy(result))
            return result
        retired = []
        def setting(device, **fields):
            if device['kind'] == 'VirtualScreen' and fields.get('connected') is False:
                self.assertTrue(snapshots)
                replacement = [d for d in snapshots[-1] if d['identity'] != device['identity']
                               and d['connected'] and d['main'] and d['hiDPI'] == 'on']
                self.assertEqual(len(replacement), 1)
                retired.append((device['identity'], replacement[0]['identity']))
            original_set(device, **fields)
            self.assertTrue(any(d['connected'] for d in self.bd.devices))
        self.bd.inventory, self.bd.set = inventory, setting
        self.rotate()
        self.assertEqual(len(retired), 2)
        self.assertEqual(retired[0][0], c.TARGET_UUID)
        self.assertEqual(retired[1], (retired[0][1], c.TARGET_UUID))

    def test_temporary_ownership_reservation_removed_without_touching_other_records(self):
        self.enable()
        unrelated = {'profile_id': 'someone-else', 'virtual': {'name': 'Keep'}}
        c.atomic_json(self.directory / 'managed-virtuals.json',
                      {'version': 1, 'devices': {'unowned': unrelated}})
        self.reserve_like_real_provisioner()
        self.rotate()
        self.assertEqual(c.read_json(self.directory / 'managed-virtuals.json')['devices']['unowned'], unrelated)
        self.assert_clean()
        self.assertEqual(len([d for d in self.bd.devices if d['kind'] == 'VirtualScreen']), 1)

    def test_resolution_only_change_uses_original_without_buffer_provision(self):
        self.enable()
        count = len(self.provisioner.calls)
        self.tick(action='profile_apply', profile_id=p.LEGACY_ID, draft={'resolution': '1024x768'})
        self.assertEqual(self.engine.state['mode'], 'ACTIVE')
        self.assertEqual(self.original()['resolution'], '1024x768')
        self.assertEqual(len(self.provisioner.calls), count)
        self.assert_clean()

    def pause_at(self, stage):
        self.enable()
        fired = self.hook_stage(stage, lambda: c.atomic_json(
            self.directory / 'command.json', {'id': 'urgent-pause', 'action': 'pause'}))
        self.rotate()
        self.assertEqual(fired, [stage])
        self.assertEqual(self.engine.state['mode'], 'PAUSED')
        self.assertFalse(self.engine.state['desired_enabled'])
        self.assert_restored()

    def test_pause_after_buffer_connection(self):
        self.pause_at('buffer_connected')

    def test_pause_after_original_offline(self):
        self.pause_at('original_offline')

    def test_pause_after_original_reconnection(self):
        self.pause_at('original_connected')

    def test_verified_disconnect_during_handoff_restores_physical(self):
        self.enable()
        fired = self.hook_stage('original_offline', lambda: c.atomic_json(
            self.directory / 'signal.json', self.signal('disconnected', [], session_negative_verified=True)))
        self.rotate()
        self.assertEqual(fired, ['original_offline'])
        self.assert_restored()
        self.assertNotEqual(self.coordinator.last_result['status'], 'ok')

    def crash_at(self, stage):
        self.enable()
        unowned = display('unowned', False, False, 'VirtualScreen')
        self.bd.devices.append(unowned)
        self.reserve_like_real_provisioner()
        def crash():
            raise ProcessExit(stage)
        if stage == 'buffer_created':
            original_ensure = self.provisioner.ensure
            fired = []
            def ensure(profile):
                result = original_ensure(profile)
                if profile['id'].startswith('transition-'):
                    fired.append(stage)
                    crash()
                return result
            self.provisioner.ensure = ensure
        elif stage == 'before_cleanup':
            # The public cleanup operation is the exact persisted boundary.
            fired = []
            def discard():
                fired.append(stage)
                crash()
            self.engine.discard_transients = discard
        else:
            fired = self.hook_stage(stage, crash)
        with self.assertRaises(ProcessExit):
            self.rotate()
        self.assertEqual(fired, [stage])
        self.assertTrue(c.read_json(self.directory / 'state.json')['journal']['transient_virtuals'])
        self.engine = c.Engine(self.bd, self.directory, clock=lambda: self.now,
                               monotonic=lambda: self.now, sleeper=self.advance,
                               signal_max_age=90, event_driven=True)
        self.assertEqual(self.engine.state['mode'], 'RESTORING')
        self.engine.tick(self.signal('disconnected', [], session_negative_verified=True))
        self.assert_restored()
        self.assertEqual(next(d for d in self.bd.devices if d['identity'] == 'unowned'), unowned)
        self.assertFalse(any(identity == 'unowned' for identity, _ in self.bd.calls))

    def test_restart_after_buffer_created_before_connection(self):
        self.crash_at('buffer_created')

    def test_restart_after_original_disconnected(self):
        self.crash_at('original_offline')

    def test_restart_after_original_returned_before_cleanup(self):
        self.crash_at('before_cleanup')

    def test_buffer_definition_failure_recovers_reserved_but_absent_buffer(self):
        self.enable()
        before = copy.deepcopy(self.coordinator.config)
        fired = []
        def ensure(profile):
            fired.append(profile['id'])
            self.assertTrue(profile['id'].startswith('transition-'))
            saved = c.read_json(self.directory / 'state.json')['journal']
            self.assertIn(profile['virtual']['identity'], saved['transient_virtuals'])
            raise RuntimeError('Injected buffer creation failure')
        self.provisioner.ensure = ensure
        self.rotate()
        self.assertEqual(len(fired), 1)
        self.assertEqual(self.coordinator.config, before)
        self.assertEqual(self.coordinator.last_result['status'], 'error')
        self.assert_restored()

    def test_original_definition_failure_recovers_and_keeps_previous_profile(self):
        self.enable()
        before = copy.deepcopy(self.coordinator.config)
        original_ensure = self.provisioner.ensure
        fired = []
        def ensure(profile):
            if profile['id'] == p.LEGACY_ID:
                fired.append(profile['id'])
                self.assertFalse(self.original()['connected'])
                self.assertTrue(any(d['connected'] for d in self.buffers()))
                raise RuntimeError('Injected original rebuild failure')
            return original_ensure(profile)
        self.provisioner.ensure = ensure
        self.rotate()
        self.assertEqual(fired, [p.LEGACY_ID])
        self.assertEqual(self.coordinator.config, before)
        self.assertEqual(self.coordinator.last_result['status'], 'error')
        self.assert_restored()

    def test_cleanup_failure_keeps_durable_journal_until_retry(self):
        self.enable()
        self.reserve_like_real_provisioner()
        failures = []
        def fail(device, fields):
            if fields.get('discard'):
                failures.append(device['identity'])
                return True
            return False
        self.bd.fail = fail
        self.rotate()
        self.assertTrue(failures)
        journal = c.read_json(self.directory / 'state.json')['journal']
        self.assertTrue(journal['transient_virtuals'])
        self.assertTrue(self.bd.devices[0]['connected'])
        self.assertEqual(self.engine.state['mode'], 'RESTORING')
        self.bd.fail = None
        self.advance(120)
        self.engine.tick(self.signal('disconnected', [], session_negative_verified=True))
        self.assert_restored()

    def test_no_physical_hardware_rotation_retains_safe_virtual_disconnect_fallback(self):
        self.enable()
        self.bd.devices = [d for d in self.bd.devices if d['kind'] != 'Display']
        self.bd.calls.clear()
        self.rotate()
        self.assertEqual(self.engine.state['mode'], 'ACTIVE')
        self.assertTrue(self.original()['connected'])
        self.assertEqual(self.original()['resolution'], '960x1280')
        self.assert_clean()
        for _ in range(3):
            self.advance(4)
            self.tick(self.signal('disconnected', [], session_negative_verified=True))
        self.assertEqual(self.engine.state['mode'], 'RESTORING')
        self.assertTrue(self.original()['connected'])
        self.assertTrue(self.engine.state['journal'])
        self.assertFalse(any(identity == 'physical1' for identity, _ in self.bd.calls))


if __name__ == '__main__':
    unittest.main()
