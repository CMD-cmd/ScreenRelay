"""Interrupted rotation recovery boundaries; never touches actual displays."""
import copy
import unittest

import controller as c
import device_profiles as p
import test_profile_runtime as runtime_tests
from test_controller import display
import test_live_rotation as live_tests


class RotationRecoveryTests(unittest.TestCase):
    setUp = runtime_tests.ProfileRuntimeTests.setUp
    tearDown = runtime_tests.ProfileRuntimeTests.tearDown
    advance = runtime_tests.ProfileRuntimeTests.advance
    signal = runtime_tests.ProfileRuntimeTests.signal
    tick = runtime_tests.ProfileRuntimeTests.tick
    enable = runtime_tests.ProfileRuntimeTests.enable
    rotate = live_tests.LiveRotationTests.rotate
    original = live_tests.LiveRotationTests.original
    buffers = live_tests.LiveRotationTests.buffers
    hook_stage = live_tests.LiveRotationTests.hook_stage
    reserve_like_real_provisioner = live_tests.LiveRotationTests.reserve_like_real_provisioner

    def interrupt_original(self):
        def fail():
            raise RuntimeError('original-offline interruption')
        fired = self.hook_stage('original_offline', fail)
        return fired

    def assert_restart_keeps_persistent_fallback(self):
        saved = c.read_json(self.directory / 'state.json')['journal']
        self.assertEqual(saved['target']['identity'], c.TARGET_UUID)
        self.assertFalse(self.buffers())
        self.bd.devices = [d for d in self.bd.devices if d['kind'] != 'Display']
        self.engine = c.Engine(self.bd, self.directory, clock=lambda: self.now,
                               monotonic=lambda: self.now, sleeper=self.advance,
                               signal_max_age=90, event_driven=True)
        self.engine.tick(self.signal('disconnected', [], session_negative_verified=True))
        self.assertEqual(self.engine.state['mode'], 'RESTORING')
        self.assertTrue(self.original()['connected'])
        self.assertTrue(self.engine.state['journal'])
        self.assertIn('keeping virtual safety fallback', self.engine.state['last_error'])
        self.assertNotIn('Missing target', self.engine.state['last_error'])

    def test_cleanup_pivots_target_before_later_protection_restore_failure(self):
        self.bd.groups = [{'tag': 'layout', 'enabled': True}]
        self.enable()
        self.bd.protection_fail = lambda group, enabled: enabled
        fired = self.interrupt_original()
        self.rotate()
        self.assertEqual(fired, ['original_offline'])
        self.assertIn(('layout', True), self.bd.group_calls)
        self.assertEqual(self.engine.state['mode'], 'RESTORING')
        self.assertIn('protection', self.engine.state['last_error'].lower())
        self.assert_restart_keeps_persistent_fallback()

    def test_process_exit_after_real_buffer_delete_retains_durable_fallback(self):
        self.enable()
        discard = self.bd.discard_virtual
        deleted = []
        def exit_after_delete(original):
            discard(original)
            deleted.append(original['identity'])
            self.assertFalse(any(d['identity'] == original['identity'] for d in self.bd.devices))
            raise live_tests.ProcessExit('response lost after delete')
        self.bd.discard_virtual = exit_after_delete
        fired = self.interrupt_original()
        with self.assertRaises(live_tests.ProcessExit):
            self.rotate()
        self.assertEqual(fired, ['original_offline'])
        self.assertEqual(len(deleted), 1)
        self.assertIn(deleted[0], c.read_json(self.directory / 'state.json')['journal']['transient_virtuals'])
        self.bd.discard_virtual = discard
        self.assert_restart_keeps_persistent_fallback()

    def auto_entry_failure(self, failure):
        candidate = copy.deepcopy(self.coordinator.config)
        candidate['profiles'][0].update(orientation='portrait', resolution='960x1280')
        other = p.make_profile('ipad-mini-a17-pro')
        candidate['profiles'].append(other)
        candidate['selected_id'] = other['id']
        self.coordinator.config = self.coordinator.store.save(candidate)
        before = copy.deepcopy(self.coordinator.config)
        self.original().update(connected=True, resolution='1280x960', hiDPI='on')
        cursor_path = self.directory / 'cursor-profile-config.json'
        old_cursor = {'enabled': True, 'scale': 2, 'scope': 'all_supported'}
        c.atomic_json(cursor_path, old_cursor)
        hit = []
        def prove_rebuilt():
            self.assertEqual(self.engine.state['mode'], 'ACTIVE')
            self.assertEqual(self.original()['resolution'], '960x1280')
            self.assertTrue(self.original()['connected'])
            self.assertFalse(self.bd.devices[0]['connected'])
            self.assertFalse(self.buffers())
            hit.append(failure)
        if failure == 'save':
            def save(config):
                self.assertEqual(config['selected_id'], p.LEGACY_ID)
                prove_rebuilt()
                raise OSError('final selected save failed')
            self.coordinator.store.save = save
        else:
            cursor = self.coordinator._cursor
            def write_then_fail(profile):
                cursor(profile)
                self.assertNotEqual(c.read_json(cursor_path), old_cursor)
                prove_rebuilt()
                raise OSError('cursor failed after write')
            self.coordinator._cursor = write_then_fail
        self.tick(action='enable')
        self.assertEqual(hit, [failure])
        self.assertNotEqual(self.engine.state['mode'], 'ACTIVE')
        self.assertTrue(self.bd.devices[0]['connected'])
        self.assertTrue(self.bd.devices[0]['main'])
        self.assertFalse(self.original()['connected'])
        self.assertIsNone(self.engine.state['journal'])
        self.assertEqual(self.coordinator.config, before)
        self.assertEqual(self.coordinator.store.load(), before)
        self.assertEqual(c.read_json(cursor_path), old_cursor)
        self.assertIn('failed', self.coordinator.error)

    def test_auto_orientation_rebuild_selected_save_failure_restores_session(self):
        self.auto_entry_failure('save')

    def test_auto_orientation_rebuild_cursor_write_failure_restores_session(self):
        self.auto_entry_failure('cursor')

    def test_changed_buffer_ownership_blocks_actual_delete_and_retains_journal(self):
        self.enable()
        self.reserve_like_real_provisioner()
        def change_owner():
            path = self.directory / 'managed-virtuals.json'
            records = c.read_json(path)
            identity = self.buffers()[0]['identity']
            records['devices'][identity]['profile_id'] = 'another-owner'
            c.atomic_json(path, records)
            raise RuntimeError('rotation interrupted after ownership changed')
        fired = self.hook_stage('original_offline', change_owner)
        self.rotate()
        self.assertEqual(fired, ['original_offline'])
        self.assertEqual(self.engine.state['mode'], 'RESTORING')
        self.assertIn('ownership changed', self.engine.state['last_error'])
        self.assertEqual(len(self.buffers()), 1)
        self.assertTrue(c.read_json(self.directory / 'state.json')['journal']['transient_virtuals'])
        self.assertFalse(any(fields.get('discard') for _, fields in self.bd.calls))


class VirtualDiscardRecoveryTests(unittest.TestCase):
    def test_fresh_inventory_rejects_changed_or_unsafe_definition_before_cli_write(self):
        original = display('buffer', False, False, 'VirtualScreen')
        safe = display()
        changed = [
            [safe, dict(original, name='someone else')],
            [safe, dict(original, kind='Display')],
            [safe, dict(original, connected=True)],
            [safe, original, dict(original, tag='duplicate')],
            [dict(safe, connected=False), original],
            [dict(safe, mirrored=True), original],
        ]
        for devices in changed:
            with self.subTest(devices=devices):
                bd = c.BetterDisplay()
                bd.inventory = lambda: copy.deepcopy(devices)
                calls = []
                bd._run = lambda *args: calls.append(args)
                with self.assertRaises(RuntimeError):
                    bd.discard_virtual(original)
                self.assertEqual(calls, [])

    def test_discard_uses_fresh_tag_for_exact_owned_identity(self):
        original = display('buffer', False, False, 'VirtualScreen')
        original['tag'] = 'stale-tag'
        current = dict(original, tag='fresh-tag')
        bd = c.BetterDisplay()
        bd.inventory = lambda: [display(), copy.deepcopy(current)]
        calls = []
        bd._run = lambda *args: calls.append(args)
        bd.discard_virtual(original)
        self.assertEqual(calls, [('discard', '-type=VirtualScreen', '-tagID=fresh-tag')])
        self.assertEqual(bd.operations, [('buffer', {'discard': True})])

    def test_delete_response_failure_invalidates_cache_and_retry_avoids_old_tag(self):
        original = display('buffer', False, False, 'VirtualScreen')
        original['tag'] = 'old-tag'
        bd = c.BetterDisplay()
        live = [display(), copy.deepcopy(original)]
        bd._identifier_cache = copy.deepcopy(live)
        reads, calls = [], []
        def inventory():
            reads.append(bd._identifier_cache is None)
            if bd._identifier_cache is None:
                bd._identifier_cache = copy.deepcopy(live)
            return copy.deepcopy(bd._identifier_cache)
        def run(*args):
            calls.append(args)
            live[:] = [d for d in live if d['identity'] != 'buffer']
            raise RuntimeError('CLI response lost after successful deletion')
        bd.inventory, bd._run = inventory, run
        with self.assertRaisesRegex(RuntimeError, 'response lost'):
            bd.discard_virtual(original)
        self.assertIsNone(bd._identifier_cache)
        bd.discard_virtual(original)
        self.assertEqual(reads, [False, True])
        self.assertEqual(calls, [('discard', '-type=VirtualScreen', '-tagID=old-tag')])
        self.assertFalse(any(d['identity'] == 'buffer' for d in live))


if __name__ == '__main__':
    unittest.main()
