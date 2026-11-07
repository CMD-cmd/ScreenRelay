"""Fresh recovery acknowledgement and lock handoff are mandatory for uninstall."""
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'script'
def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
with patch.dict('sys.modules', {'install': load('install')}):
    uninstall = load('uninstall')


class UninstallLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.support = Path(self.temp.name)
        self.agents = self.support / 'agents'
        self.agents.mkdir()
        self.command_id = 'uninstall-recover-fixed'
        self.now = 1000.0
        self.patches = [patch.object(uninstall, 'SUPPORT', self.support),
                        patch.object(uninstall, 'AGENTS', self.agents),
                        patch.object(uninstall.time, 'time', return_value=self.now),
                        patch.object(uninstall.time, 'sleep'),
                        patch.object(uninstall.uuid, 'uuid4', return_value=SimpleNamespace(hex='fixed')),
                        patch.object(uninstall.subprocess, 'run', return_value=SimpleNamespace(
                            returncode=0, stdout=str(self.support / 'controller.py') + ' --event-driven')),
                        patch('builtins.print')]
        self.mocks = [p.start() for p in self.patches]
        self.addCleanup(lambda: [p.stop() for p in reversed(self.patches)])
        self.confirm()

    def write(self, name, value):
        (self.support / name).write_text(json.dumps(value))

    def confirm(self):
        state = {'mode': 'PAUSED', 'journal': None, 'desired_enabled': False,
                 'last_command': self.command_id, 'exit_completed': {
                     'id': self.command_id, 'timestamp': self.now, 'controller_pid': 101}}
        self.write('state.json', state)
        self.write('status.json', dict(state, updated_at=self.now, recovery_pending=False, controller_pid=101))
        self.write('command-result.json', {'id': self.command_id, 'status': 'ok'})
        self.write('command.json', {'id': self.command_id, 'action': 'recover', 'exit_after_restore': True})
        self.write('cursor-profile-state.json', {'status': 'restored', 'pending_originals': []})

    def assert_not_stopped(self):
        self.assertFalse(any(c.args[0][0] in ('/bin/launchctl', '/usr/bin/pkill')
                             for c in self.mocks[5].call_args_list))
        self.mocks[6].assert_not_called()

    def test_stale_paused_status_cannot_hide_active_state(self):
        self.write('state.json', {'mode': 'ACTIVE', 'journal': {'physical': {}}, 'desired_enabled': True})
        with self.assertRaises(SystemExit):
            uninstall.main()
        self.assert_not_stopped()

    def test_completion_from_another_command_cannot_authorize_stop(self):
        for name in ('state.json', 'status.json'):
            value = json.loads((self.support / name).read_text())
            value['exit_completed']['id'] = 'old-recovery'
            self.write(name, value)
        with self.assertRaises(SystemExit):
            uninstall.main()
        self.assert_not_stopped()

    def test_stale_snapshot_cannot_authorize_stop(self):
        status = json.loads((self.support / 'status.json').read_text())
        status['updated_at'] -= 20
        self.write('status.json', status)
        with self.assertRaises(SystemExit):
            uninstall.main()
        self.assert_not_stopped()

    def test_pending_cursor_cannot_authorize_stop(self):
        self.write('cursor-profile-state.json', {'status': 'recovery_pending', 'pending_originals': [{}]})
        with self.assertRaises(SystemExit):
            uninstall.main()
        self.assert_not_stopped()

    def test_lock_owner_is_never_killed_to_finish_uninstall(self):
        with patch.object(uninstall.fcntl, 'flock', side_effect=BlockingIOError):
            with self.assertRaises(SystemExit):
                uninstall.main()
        self.assert_not_stopped()

    def test_rechecks_state_after_lock_handoff(self):
        def lock(*args):
            self.write('state.json', {'mode': 'ACTIVE', 'journal': {'physical': {}}, 'desired_enabled': True})
        with patch.object(uninstall.fcntl, 'flock', side_effect=lock):
            with self.assertRaises(SystemExit):
                uninstall.main()
        self.assert_not_stopped()

    def test_bootout_failure_never_reports_success_or_removes_registration(self):
        registration = self.agents / (uninstall.LABEL + '.plist')
        registration.write_text('existing registration')
        self.mocks[5].return_value = SimpleNamespace(returncode=5, stdout='')
        with self.assertRaises(SystemExit):
            uninstall.main()
        self.assertTrue(registration.exists())
        self.assertFalse(any(c.args[0][0] == '/usr/bin/pkill' for c in self.mocks[5].call_args_list))
        self.mocks[6].assert_not_called()

    def test_confirmed_recovery_and_free_lock_permit_stop(self):
        uninstall.main()
        self.assertTrue(any(c.args[0][0] == '/bin/launchctl' for c in self.mocks[5].call_args_list))
        self.assertTrue(any(c.args[0][0] == '/usr/bin/pkill' for c in self.mocks[5].call_args_list))
