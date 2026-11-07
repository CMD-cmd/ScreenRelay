"""Crash boundaries use only temporary profiles and FakeBD outputs."""
import copy
import unittest
from unittest.mock import patch

import controller as c
import profile_runtime as runtime
from profile_runtime import DeviceCoordinator
import test_profile_runtime as fixtures


class SimulatedCrash(BaseException):
    pass


class ProfileLifecycleTests(unittest.TestCase):
    setUp = fixtures.ProfileRuntimeTests.setUp
    tearDown = fixtures.ProfileRuntimeTests.tearDown
    advance = fixtures.ProfileRuntimeTests.advance
    signal = fixtures.ProfileRuntimeTests.signal
    tick = fixtures.ProfileRuntimeTests.tick
    enable = fixtures.ProfileRuntimeTests.enable

    def restart(self):
        self.engine = c.Engine(self.bd, self.directory, clock=lambda: self.now,
                               monotonic=lambda: self.now, sleeper=self.advance,
                               signal_max_age=90, event_driven=True)
        self.coordinator = DeviceCoordinator(self.engine, self.provisioner)

    def crash_cursor(self, boundary):
        self.enable()
        before = copy.deepcopy(self.coordinator.config)
        original = runtime.atomic_json
        def writing(path, value):
            if boundary == 'complete' and path.name == 'profile-transaction.json' and value['status'] == 'complete':
                raise SimulatedCrash()
            if boundary == 'receipt' and path.name == 'command-result.json' and value['status'] == 'ok':
                raise SimulatedCrash()
            return original(path, value)
        if boundary == 'profiles':
            fault = patch.object(self.coordinator.store, 'save', side_effect=SimulatedCrash())
        else:
            fault = patch.object(runtime, 'atomic_json', writing)
        with fault:
            with self.assertRaises(SimulatedCrash):
                self.tick(action='profile_cursor', scale=2)
        self.assertEqual(c.read_json(self.directory / 'cursor-profile-config.json')['scale'], 2)
        return before

    def test_crash_before_profile_commit_restores_cursor_repeatedly(self):
        before = self.crash_cursor('profiles')
        calls = len(self.bd.calls)
        self.restart()
        self.assertEqual(self.coordinator.config, before)
        self.assertEqual(c.read_json(self.directory / 'cursor-profile-config.json')['scale'], 3)
        self.assertEqual(self.coordinator.last_result['status'], 'error')
        self.assertEqual(self.engine.state['mode'], 'RESTORING')
        self.assertEqual(len(self.bd.calls), calls)
        self.restart()
        self.assertEqual(c.read_json(self.directory / 'cursor-profile-config.json')['scale'], 3)
        self.assertEqual(c.read_json(self.directory / 'profile-transaction.json')['status'], 'interrupted')

    def test_crash_after_profile_commit_completes_receipt_and_projection(self):
        for boundary in ('complete', 'receipt'):
            with self.subTest(boundary=boundary):
                # Each boundary has an independent temporary directory.
                if boundary == 'receipt':
                    self.tearDown()
                    self.setUp()
                self.crash_cursor(boundary)
                # An independently restarted cursor worker may have projected
                # stale settings before the coordinator recovers its commit.
                c.atomic_json(self.directory / 'cursor-profile-config.json', {'enabled': True, 'scale': 3})
                calls = len(self.bd.calls)
                self.restart()
                self.assertEqual(self.coordinator._profile()['cursor_scale'], 2)
                self.assertEqual(c.read_json(self.directory / 'cursor-profile-config.json')['scale'], 2)
                self.assertEqual(self.coordinator.last_result['status'], 'ok')
                self.assertEqual(c.read_json(self.directory / 'command-intent.json')['status'], 'ok')
                self.assertEqual(len(self.bd.calls), calls)
                self.restart()
                self.assertEqual(self.coordinator.last_result['status'], 'ok')

    def test_committed_geometry_recovers_old_journal_before_using_saved_target(self):
        import device_profiles as profiles
        for stale_target in (False, True):
            with self.subTest(stale_target=stale_target):
                if stale_target:
                    self.tearDown()
                    self.setUp()
                self.enable()
                old_target = copy.deepcopy(self.engine.state['target_spec'])
                original = runtime.atomic_json
                def writing(path, value):
                    if path.name == 'profile-transaction.json' and value['status'] == 'complete':
                        raise SimulatedCrash()
                    return original(path, value)
                with patch.object(runtime, 'atomic_json', writing):
                    with self.assertRaises(SimulatedCrash):
                        self.tick(action='profile_apply', automatic=False,
                                  draft={'catalog_id': 'ipad-mini-a17-pro', 'cursor_scale': 2})
                committed = self.coordinator.store.load()
                saved_profile = next(p for p in committed['profiles'] if p['id'] == committed['selected_id'])
                new_target = profiles.target_spec(saved_profile)
                self.assertNotEqual(old_target, new_target)
                if stale_target:
                    self.engine.state['target_spec'] = old_target
                    self.engine._save()
                self.restart()
                self.assertEqual(self.engine.state['mode'], 'RESTORING')
                self.assertEqual(self.engine.state['target_spec'], old_target if stale_target else new_target)
                self.assertEqual(self.coordinator.last_result['status'], 'ok')
                self.coordinator.tick(self.signal())
                self.assertEqual(self.engine.state['mode'], 'ARMED')
                self.assertTrue(self.bd.devices[0]['connected'])
                self.coordinator.tick(self.signal())
                self.assertEqual(self.engine.state['mode'], 'ACTIVE')
                self.assertEqual(self.engine.state['target_spec'], new_target)
                self.assertEqual(self.engine.state['active_profile_id'], saved_profile['id'])
                self.assertEqual(self.coordinator.config, committed)
                self.assertEqual(c.read_json(self.directory / 'cursor-profile-config.json')['scale'], 2)

    def test_external_profile_edit_preserves_transaction_and_blocks_entry(self):
        self.crash_cursor('profiles')
        saved = self.coordinator.store.load()
        saved['automatic'] = False
        self.coordinator.store.save(saved)
        transaction = (self.directory / 'profile-transaction.json').read_bytes()
        self.restart()
        self.assertTrue(self.coordinator.profile_recovery_blocked)
        self.assertIn('事务之外改变', self.coordinator.error)
        self.assertEqual((self.directory / 'profile-transaction.json').read_bytes(), transaction)
        self.assertEqual(self.coordinator.store.load(), saved)
        self.engine._restore()
        self.coordinator.tick(self.signal())
        self.assertNotEqual(self.engine.state['mode'], 'ACTIVE')

    def test_consumed_pause_receipt_waits_for_recovery_across_restart(self):
        self.enable()
        intent = {'version': 1, 'id': 'pause-durable', 'action': 'pause', 'status': 'applying'}
        c.atomic_json(self.directory / 'command-intent.json', intent)
        self.engine.state['last_command'] = intent['id']
        self.engine._begin_restore('Manual pause', category='manual')
        self.restart()
        self.assertEqual(self.coordinator.last_result['status'], 'applying')
        # A newer durable mailbox must not hide the pending pause receipt.
        c.atomic_json(self.directory / 'command.json', {'id': 'other', 'action': 'profile_select'})
        self.coordinator.tick(self.signal('disconnected', []))
        self.assertEqual(self.engine.state['mode'], 'PAUSED')
        self.assertEqual(self.coordinator.last_result['id'], intent['id'])
        self.assertEqual(self.coordinator.last_result['status'], 'ok')
        self.restart()
        self.assertEqual(self.coordinator.last_result['status'], 'ok')

    def test_unknown_transaction_is_preserved_and_blocks_entry(self):
        c.atomic_json(self.directory / 'profile-transaction.json', {'status': 'future'})
        original = (self.directory / 'profile-transaction.json').read_bytes()
        self.restart()
        self.coordinator.tick(self.signal(), {'id': 'enable-safe', 'action': 'enable'})
        self.assertNotEqual(self.engine.state['mode'], 'ACTIVE')
        self.assertEqual((self.directory / 'profile-transaction.json').read_bytes(), original)
        self.assertIn('未知', self.coordinator.error)
