import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import watchdog


class WatchdogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self.tmp.name)
        self.patch = patch.object(watchdog, 'DIRECTORY', self.directory)
        self.patch.start()

    def tearDown(self):
        self.patch.stop(); self.tmp.cleanup()

    def run_at(self, now, rows=''):
        with patch.object(watchdog.time, 'time', return_value=now), patch.object(watchdog.subprocess, 'run', return_value=types.SimpleNamespace(stdout=rows)), patch.object(watchdog.os, 'kill') as kill:
            watchdog.main()
            return kill.call_args_list

    def test_fresh_status_never_restarts(self):
        (self.directory/'status.json').write_text(json.dumps({'updated_at': 100}))
        self.assertEqual(self.run_at(105), [])

    def test_wake_requires_second_stale_observation(self):
        (self.directory/'status.json').write_text(json.dumps({'updated_at': 100}))
        self.assertEqual(self.run_at(1000), [])
        self.assertEqual(self.run_at(1030), [])
        self.assertTrue((self.directory/'watchdog-stale.json').exists())

    def test_restart_matches_only_our_controller(self):
        (self.directory/'status.json').write_text(json.dumps({'updated_at': 100}))
        self.run_at(1000)
        rows = f'123 /usr/bin/python3 {self.directory}/controller.py\n456 /usr/bin/python3 /other/controller.py\n'
        calls = self.run_at(1060, rows)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].args[0], 123)

    def test_recovered_heartbeat_clears_suspect(self):
        path = self.directory/'status.json'
        path.write_text(json.dumps({'updated_at': 100}))
        self.run_at(1000)
        path.write_text(json.dumps({'updated_at': 1010}))
        self.assertEqual(self.run_at(1020), [])
        self.assertFalse((self.directory/'watchdog-stale.json').exists())

if __name__ == '__main__': unittest.main()
