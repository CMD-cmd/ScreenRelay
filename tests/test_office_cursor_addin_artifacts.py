"""Verify the prepared Office loaders without launching Office or installing code."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class AddinArtifactTests(unittest.TestCase):
    def test_powerpoint_loader_uses_isolated_content_addressed_library(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            library = output / 'libUUNativeCursor.dylib'
            library.write_bytes(b'isolated-library-fixture')
            digest = hashlib.sha256(library.read_bytes()).hexdigest()
            result = subprocess.run(
                [sys.executable, str(root / 'script/prepare_office_cursor_addin.py'), str(output)],
                capture_output=True, text=True, check=True)
            manifest = json.loads(result.stdout)
            self.assertEqual(manifest, json.loads((output / 'manifest.json').read_text()))
            excel = manifest['hosts']['com.microsoft.Excel']
            powerpoint = manifest['hosts']['com.microsoft.Powerpoint']
            self.assertEqual(manifest['compiled_library_sha256'], digest)
            self.assertEqual(powerpoint['status'], 'prepared_not_loaded_in_powerpoint')
            self.assertNotEqual(excel['library_destination'], powerpoint['library_destination'])
            self.assertEqual(Path(powerpoint['library_destination']).parent.name, 'PowerPoint')
            self.assertIn(digest[:12], Path(powerpoint['library_destination']).name)
            self.assertFalse(manifest['global_macro_preferences_modified'])
            self.assertFalse(manifest['excel_application_modified'])
            for host in [excel, powerpoint]:
                source = (output / host['module']).read_text()
                self.assertIn(host['library_destination'], source)
                self.assertIn('Alias "UUOfficeCursorInstall"', source)
                self.assertIn('Alias "UUOfficeCursorStop"', source)
                self.assertIn('Public Sub Auto_Open()', source)
                self.assertIn('Public Sub Auto_Close()', source)
            pp_source = (output / powerpoint['module']).read_text()
            pp_bytes = (output / powerpoint['module']).read_bytes()
            self.assertIn(b'\r\n', pp_bytes)
            self.assertNotIn(b'\n', pp_bytes.replace(b'\r\n', b''))
            self.assertIn('Attribute VB_Name = "UUDisplayPowerPointCursor"', pp_source)
            self.assertNotIn('Public Sub Auto_Add()', pp_source)
            self.assertNotIn('Public Sub Auto_Remove()', pp_source)
            self.assertEqual(set(p.name for p in output.iterdir()), {
                'libUUNativeCursor.dylib', 'manifest.json',
                'UUDisplayNativeCursor.bas', 'UUDisplayPowerPointCursor.bas'})


if __name__ == '__main__':
    unittest.main()
