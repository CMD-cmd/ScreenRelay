"""The passive packet mode must obey the native receiver's strict JSON types."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest


@unittest.skipUnless(sys.platform == 'darwin' and shutil.which('xcrun'), 'Requires macOS AppKit')
class OfficeAcceptancePacketTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.helper = Path(cls.temp.name) / 'acceptance'
        source = Path(__file__).resolve().parents[1] / 'script/OfficeCursorAcceptance.m'
        subprocess.run(['xcrun', 'clang', '-fobjc-arc', '-O2', '-Wall', '-Wextra', '-Werror',
                        str(source), '-framework', 'AppKit', '-o', str(cls.helper)],
                       capture_output=True, check=True)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_passive_packets_have_boolean_enable_and_bounded_deadline(self):
        for scale in [1, 3]:
            before = time.time()
            raw = subprocess.check_output([str(self.helper), '--packet', str(scale), '60'], text=True)
            value = json.loads(raw)
            self.assertIs(type(value['enabled']), bool)
            self.assertEqual(value['enabled'], scale > 1)
            self.assertEqual(value['scale'], scale)
            self.assertLessEqual(before + 50, value['expires'])
            self.assertLessEqual(value['expires'], time.time() + 61)

    def test_invalid_passive_packet_is_rejected_without_notification(self):
        for scale, seconds in [('2', '60'), ('3', '61'), ('1', '0')]:
            result = subprocess.run([str(self.helper), '--packet', scale, seconds], capture_output=True)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, b'')


if __name__ == '__main__':
    unittest.main()
