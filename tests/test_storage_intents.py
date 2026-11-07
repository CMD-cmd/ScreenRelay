"""Partial state/status writes must leave a receipt and never replay commands."""
import copy
import errno
import unittest
from unittest.mock import patch

import controller as c
from profile_runtime import DeviceCoordinator
import test_profile_runtime as fixtures


class StorageIntentTests(unittest.TestCase):
    setUp = fixtures.ProfileRuntimeTests.setUp
    tearDown = fixtures.ProfileRuntimeTests.tearDown
    advance = fixtures.ProfileRuntimeTests.advance
    signal = fixtures.ProfileRuntimeTests.signal

    def partial_write(self, action, failed_file, restart):
        command = {'id': 'split-write', 'action': action}
        if action == 'profile_automatic':
            command['automatic'] = False
        before = copy.deepcopy(self.coordinator.config)
        c.atomic_json(self.directory / 'command.json', command)
        original = c.atomic_json
        fired = []
        def writing(path, value):
            if path.name == failed_file and self.engine.state['last_command'] == command['id']:
                fired.append(True)
                raise OSError(errno.ENOSPC, 'fault after command intent')
            return original(path, value)
        with patch.object(c, 'atomic_json', writing):
            with self.assertRaises(OSError):
                self.coordinator.tick(self.signal(), command)
        self.assertTrue(fired)
        durable = c.read_json(self.directory / 'state.json')
        self.assertEqual(durable.get('last_command') == command['id'], failed_file == 'status.json')
        self.assertEqual(c.read_json(self.directory / 'command-intent.json')['status'], 'applying')
        if restart:
            self.engine = c.Engine(self.bd, self.directory, clock=lambda: self.now,
                monotonic=lambda: self.now, sleeper=self.advance, signal_max_age=90, event_driven=True)
        self.coordinator = DeviceCoordinator(self.engine, self.provisioner)
        self.assertEqual(self.coordinator.last_result['status'], 'error')
        self.assertEqual(self.coordinator.last_result['id'], command['id'])
        self.assertEqual(c.read_json(self.directory / 'command-intent.json')['status'], 'error')
        with patch.object(self.coordinator, '_intent', side_effect=AssertionError('Command replayed')):
            self.coordinator.tick(self.signal(), command)
        self.assertEqual(self.coordinator.config, before)

    def test_profile_state_failure_same_process(self):
        self.partial_write('profile_automatic', 'state.json', False)

    def test_profile_state_failure_restart(self):
        self.partial_write('profile_automatic', 'state.json', True)

    def test_profile_status_failure_restart(self):
        self.partial_write('profile_automatic', 'status.json', True)

    def test_enable_state_failure_restart(self):
        self.partial_write('enable', 'state.json', True)

    def test_enable_status_failure_restart(self):
        self.partial_write('enable', 'status.json', True)
