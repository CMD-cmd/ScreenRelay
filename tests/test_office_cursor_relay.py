import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock
from office_cursor_relay import OfficeCursorRelay


class RelayTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.path=Path(self.temp.name)
        (self.path/'office-cursor-addon.json').write_text('{"installed":true}')
        (self.path/'office-cursor-relay').write_text('test helper')
        self.process=Mock();self.process.poll.return_value=None
        self.popen=Mock(return_value=self.process)
        self.now=100
        self.relay=OfficeCursorRelay(self.path,popen=self.popen,clock=lambda:self.now)

    def tearDown(self):self.temp.cleanup()

    def test_requires_explicit_addin_install_marker(self):
        (self.path/'office-cursor-addon.json').unlink()
        self.relay.sync(True);self.popen.assert_not_called()

    def test_idle_keeps_listener_for_immediate_addin_handshake(self):
        self.relay.sync(False)
        self.assertEqual(self.popen.call_args.args[0],[str(self.path/'office-cursor-relay'),'--directory',str(self.path)])
        self.relay.sync(True);self.relay.sync(False)
        self.assertEqual(self.popen.call_count,1)

    def test_close_terminates_owned_child(self):
        self.relay.sync(True);self.relay.close()
        self.process.terminate.assert_called_once();self.process.wait.assert_called_once_with(timeout=1)
        self.assertIsNone(self.relay.process)

    def test_marker_removal_closes_running_bridge(self):
        self.relay.sync(True)
        (self.path/'office-cursor-addon.json').write_text('{"installed":false}')
        self.relay.sync(True);self.process.terminate.assert_called_once()

    def test_child_failure_backs_off_until_existing_later_event(self):
        self.relay.sync(True);self.process.poll.return_value=2
        self.relay.sync(True);self.relay.sync(True)
        self.assertEqual(self.popen.call_count,1)
        self.now+=60;self.process.poll.return_value=None;self.relay.sync(True)
        self.assertEqual(self.popen.call_count,2)

    def test_uncertain_child_state_never_spawns_duplicate(self):
        self.relay.sync(True);self.process.poll.side_effect=OSError('unknown')
        self.relay.sync(True);self.relay.close()
        self.assertIs(self.relay.process,self.process);self.assertEqual(self.popen.call_count,1)

    def test_slow_shutdown_retains_ownership(self):
        self.relay.sync(True);self.process.wait.side_effect=subprocess.TimeoutExpired('relay',1)
        self.relay.close();self.relay.sync(True)
        self.assertIs(self.relay.process,self.process);self.assertEqual(self.popen.call_count,1)

    def test_completed_slow_shutdown_clears_stale_stopping_error(self):
        self.relay.sync(True)
        self.process.wait.side_effect=subprocess.TimeoutExpired('relay',1)
        self.relay.close()
        self.assertEqual(self.relay.last_error,'Office cursor relay still stopping')
        self.process.poll.return_value=0
        self.relay.close()
        self.assertIsNone(self.relay.process)
        self.assertIsNone(self.relay.last_error)
        self.assertEqual(self.popen.call_count,1)

    def test_close_wait_error_keeps_child_owned_until_confirmed_exit(self):
        self.relay.sync(True)
        self.process.wait.side_effect=OSError('wait interrupted')
        self.relay.close()
        self.assertIs(self.relay.process,self.process)
        self.assertEqual(self.relay.last_error,'wait interrupted')
        self.relay.sync(True)
        self.assertEqual(self.popen.call_count,1)
        self.process.poll.return_value=0
        self.relay.close()
        self.assertIsNone(self.relay.process)
        self.assertIsNone(self.relay.last_error)

    def test_missing_helper_and_bad_marker_do_not_break_controller(self):
        (self.path/'office-cursor-relay').unlink();self.relay.sync(True)
        self.assertIsNotNone(self.relay.last_error);self.popen.assert_not_called()
        (self.path/'office-cursor-addon.json').write_text('bad');self.relay.sync(True)
        self.popen.assert_not_called()


if __name__=='__main__':unittest.main()
