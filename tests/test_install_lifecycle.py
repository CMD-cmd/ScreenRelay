"""Installer lifecycle checks never touch real processes or application paths."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('lifecycle_installer', Path(__file__).resolve().parents[1] / 'script/install.py')
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


class InstallLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.support = self.root / 'support'
        self.support.mkdir()
        self.app = self.root / 'Applications/UUDisplayAutomation.app'
        self.agents = self.root / 'agents'
        self.source = self.root / 'source'
        (self.source / 'dist/UUDisplayAutomation.app').mkdir(parents=True)
        for name in ('controller.py', 'storage_recovery.py', 'cursor_profile.py',
                     'office_cursor_relay.py', 'session_recheck.py', 'session_policy.py',
                     'device_profiles.py', 'profile_runtime.py'):
            (self.source / name).write_text('new source')
        for name in ('cursor-image-probe', 'office-cursor-relay', 'native-topology', 'betterdisplaycli'):
            (self.source / 'dist' / name).write_text('new helper')
        self.patches = [patch.object(installer, 'ROOT', self.source),
                        patch.object(installer, 'SUPPORT', self.support),
                        patch.object(installer, 'APP', self.app),
                        patch.object(installer, 'AGENTS', self.agents),
                        patch.object(installer, 'exact_app_pids', return_value=[]),
                        patch.object(installer.subprocess, 'check_output', return_value='disabled services = {\n}\n'),
                        patch.object(installer.subprocess, 'run'),
                        patch('builtins.print')]
        self.mocks = [p.start() for p in self.patches]
        self.addCleanup(lambda: [p.stop() for p in reversed(self.patches)])
        self.addCleanup(self.temp.cleanup)
        self.write('state.json', {'version': 1, 'mode': 'PAUSED', 'desired_enabled': False,
                                  'journal': None, 'last_command': 'old'})

    def write(self, name, value):
        (self.support / name).write_text(json.dumps(value))

    def read(self, name):
        return json.loads((self.support / name).read_text())

    def assert_no_start(self):
        self.assertFalse(any(c.args[0][1] == 'bootstrap' or c.args[0][0] == '/usr/bin/open'
                             for c in self.mocks[6].call_args_list))

    def test_unconsumed_enable_and_profile_are_replaced_by_consumed_pause(self):
        for action in ('enable', 'profile_select'):
            with self.subTest(action=action):
                if action == 'profile_select':
                    # Avoid timestamp backup collisions while keeping evidence.
                    import shutil
                    shutil.rmtree(self.support / 'backups')
                self.write('command.json', {'id': 'unconsumed', 'action': action})
                self.write('profiles.json', {'keep': 'profiles'})
                self.write('managed-virtuals.json', {'keep': 'virtuals'})
                installer.main()
                command, state = self.read('command.json'), self.read('state.json')
                self.assertEqual(command['action'], 'recover')
                self.assertEqual(state['last_command'], command['id'])
                self.assertEqual(state['mode'], 'PAUSED')
                self.assertIs(state['desired_enabled'], False)
                self.assertEqual(self.read('profiles.json'), {'keep': 'profiles'})
                self.assertEqual(self.read('managed-virtuals.json'), {'keep': 'virtuals'})

    def test_state_change_before_lock_refuses_replacement(self):
        def acquired(*args):
            self.write('state.json', {'mode': 'ACTIVE', 'journal': {'physical': {}}})
        with patch.object(installer.fcntl, 'flock', side_effect=acquired):
            with self.assertRaises(SystemExit):
                installer.main()
        self.assertFalse(self.app.exists())
        self.assert_no_start()

    def test_busy_recovery_lock_refuses_replacement(self):
        with patch.object(installer.fcntl, 'flock', side_effect=BlockingIOError), patch.object(installer.time, 'sleep'):
            with self.assertRaises(SystemExit):
                installer.main()
        self.assertFalse(self.app.exists())
        self.assert_no_start()

    def test_install_pause_supersedes_old_exit_request(self):
        state = self.read('state.json')
        state['exit_after_restore'] = {'id': 'old-exit'}
        self.write('state.json', state)
        installer.install_pause_barrier()
        self.assertNotIn('exit_after_restore', self.read('state.json'))

    def test_pending_or_anomalous_cursor_refuses_replacement(self):
        for cursor in ({'status': 'restored', 'pending_originals': [{'path': 'original'}]},
                       {'status': 'applying', 'pending_originals': []},
                       {'status': 'error', 'pending_originals': [], 'error': 'failed'}):
            with self.subTest(cursor=cursor):
                self.write('cursor-profile-state.json', cursor)
                with self.assertRaises(SystemExit):
                    installer.install_pause_barrier()
                self.assertFalse((self.support / 'command.json').exists())

    def test_partial_barrier_writes_never_start_services(self):
        for failing in ('command.json', 'state.json'):
            with self.subTest(failing=failing):
                import shutil
                if (self.support / 'backups').exists():
                    shutil.rmtree(self.support / 'backups')
                self.write('command.json', {'id': 'unconsumed', 'action': 'enable'})
                original = installer.atomic_json
                def writing(path, value):
                    if path.name == failing:
                        raise OSError('injected write interruption')
                    return original(path, value)
                with patch.object(installer, 'atomic_json', side_effect=writing):
                    with self.assertRaises(OSError):
                        installer.main()
                self.assertFalse(self.app.exists())
                self.assert_no_start()
                if failing == 'state.json':
                    self.assertEqual(self.read('command.json')['action'], 'recover')

    def test_disabled_login_preference_is_preserved(self):
        self.mocks[5].return_value = 'disabled services = {\n "local.codex.uu-display" => disabled\n}\n'
        installer.main()
        self.assertTrue(any(c.args[0][0] == '/usr/bin/open' for c in self.mocks[6].call_args_list))
        self.assertFalse(any(c.args[0][1] == 'bootstrap' for c in self.mocks[6].call_args_list))

    def test_install_refreshes_preserved_bundle_dates_and_own_registration(self):
        bundle = self.source / 'dist/UUDisplayAutomation.app'
        (bundle / 'Contents').mkdir()
        (bundle / 'Contents/Info.plist').write_bytes(b'unchanged signed metadata')
        os.utime(bundle, (1000, 1000))
        os.utime(bundle / 'Contents', (1000, 1000))
        installer.main()
        self.assertGreater(self.app.stat().st_mtime, 1000)
        self.assertGreater((self.app / 'Contents').stat().st_mtime, 1000)
        self.assertEqual((self.app / 'Contents/Info.plist').read_bytes(), b'unchanged signed metadata')
        commands = [call.args[0] for call in self.mocks[6].call_args_list]
        registrations = [command for command in commands if command[0].endswith('/lsregister')]
        self.assertEqual(registrations, [[registrations[0][0], '-f', str(self.app)]])
        self.assertIn(['/usr/bin/mdimport', '-i', str(self.app)], commands)

    def test_metadata_tools_failure_does_not_block_service_start(self):
        real_mock = self.mocks[6]
        import subprocess
        def run(command, **kwargs):
            if command[0].endswith('/lsregister'):
                raise OSError('metadata service unavailable')
            if command[0] == '/usr/bin/mdimport':
                raise subprocess.TimeoutExpired(command, 5)
            return type('Result', (), {'returncode': 0})()
        real_mock.side_effect = run
        installer.main()
        self.assertTrue(any(call.args[0][1] == 'bootstrap' for call in real_mock.call_args_list))
        self.assertEqual(self.read('state.json')['mode'], 'PAUSED')
