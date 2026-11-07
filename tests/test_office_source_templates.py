"""Read-back checks for the actual release add-ins, without executing macros."""
import hashlib
import importlib.util
import io
import json
import re
from pathlib import Path
import sys
import tempfile
import unittest
from zipfile import ZipFile
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'script'))
from build_office_cursor_payload import HOSTS, validate_templates
from prepare_portable_office_cursor import module

HAS_READER = importlib.util.find_spec('olefile') is not None and importlib.util.find_spec('oletools') is not None


class ReleaseTemplateTests(unittest.TestCase):
    def test_real_templates_match_release_hashes_and_current_sources(self):
        self.assertEqual(validate_templates(ROOT / 'Resources/OfficeCursorTemplates')['module_version'], 2)

    def test_release_packages_have_no_developer_home_markers(self):
        for _, folder, name, _, _ in HOSTS.values():
            with self.subTest(host=name), ZipFile(ROOT / 'Resources/OfficeCursorTemplates' / folder / name) as archive:
                data = b''.join(archive.read(name) for name in archive.namelist())
                self.assertFalse(b'example-user' in data, 'A developer home marker remains in the add-in package')
                self.assertFalse('example-user'.encode('utf-16le') in data, 'A UTF-16 developer home marker remains in the package')

    def test_xml_qname_and_compatibility_prefixes_remain_declared(self):
        for _, folder, name, _, _ in HOSTS.values():
            with ZipFile(ROOT / 'Resources/OfficeCursorTemplates' / folder / name) as archive:
                for part in archive.namelist():
                    if not part.endswith('.xml'):
                        continue
                    data = archive.read(part)
                    declarations = {prefix for _, (prefix, _) in ET.iterparse(io.BytesIO(data), events=['start-ns'])} | {'xml'}
                    for item in ET.fromstring(data).iter():
                        for key, value in item.attrib.items():
                            if key == '{http://www.w3.org/2001/XMLSchema-instance}type' and ':' in value:
                                self.assertIn(value.split(':')[0], declarations, (name, part, key))
                            if key.endswith('}Ignorable') or key == 'Requires':
                                for prefix in value.split():
                                    self.assertIn(prefix, declarations, (name, part, key))

    @unittest.skipUnless(HAS_READER, 'Development-only Office source reader is not installed')
    def test_actual_vba_source_and_cache_metadata(self):
        import olefile
        from oletools.olevba import VBA_Project
        for host, folder, name, part, _ in HOSTS.values():
            with self.subTest(host=host), ZipFile(ROOT / 'Resources/OfficeCursorTemplates' / folder / name) as archive:
                with olefile.OleFileIO(io.BytesIO(archive.read(f'{part}/vbaProject.bin')), raise_defects=olefile.DEFECT_INCORRECT) as ole:
                    self.assertEqual(ole.openstream('VBA/_VBA_PROJECT').read()[2:4], b'\xff\xff')
                    self.assertEqual(ole.get_size('VBA/_VBA_PROJECT'), 7)
                    self.assertFalse(any(path[-1].startswith('__SRP_') for path in ole.listdir()))
                    project = VBA_Project(ole, '', 'PROJECT', 'VBA/dir', relaxed=False)
                    project.parse_project_stream()
                    list(project.parse_modules())
                    self.assertTrue(all(item.textoffset == 0 for item in project.modules))
                    directory = project.dir_stream.getvalue()
                    cookies = [match.end() for match in re.finditer(b'(?:\x13|\x2c)\x00\x02\x00\x00\x00', directory)]
                    self.assertEqual(len(cookies), len(project.modules) + 1)
                    self.assertTrue(all(directory[offset:offset + 2] == b'\xff\xff' for offset in cookies))
                    sources = [item.code_raw for item in project.modules if b'UUOfficeCursorInstall' in item.code_raw]
                    self.assertEqual(len(sources), 1)
                    self.assertEqual(sources[0].split(b'\r\n')[0], module(host).split('\n')[0].encode('ascii'))
                    # The existing Mac module identity may use a legacy codepage.
                    body = sources[0].split(b'\r\n', 1)[1].rstrip(b'\r\n')
                    expected = module(host).split('\n', 1)[1].replace('\n', '\r\n').encode('ascii').rstrip(b'\r\n')
                    self.assertEqual(body, expected)

    @unittest.skipUnless(HAS_READER, 'Development-only Office source reader is not installed')
    def test_vba_compression_roundtrips_binary_records_and_long_source(self):
        from prepare_office_cursor_templates import compress_literals
        from oletools.olevba import decompress_stream
        for value in (b'', bytes(range(256)) * 4, b'Private Value As Currency\r\n' * 500,
                      b'x' * 4096, b'abcde' * 2000):
            self.assertEqual(decompress_stream(compress_literals(value)), value)

    @unittest.skipUnless(HAS_READER, 'Development-only Office source reader is not installed')
    def test_regeneration_is_byte_reproducible_from_owned_source_form_containers(self):
        from prepare_office_cursor_templates import prepare
        resources = ROOT / 'Resources/OfficeCursorTemplates'
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / 'Templates'
            result = prepare(resources / 'Excel/UUDisplayNativeCursor.xlam',
                             resources / 'PowerPoint/UUDisplayPowerPointCursor.ppam', target)
            self.assertEqual(result, json.loads((resources / 'templates.json').read_text()))
            for _, folder, name, _, _ in HOSTS.values():
                self.assertEqual((target / folder / name).read_bytes(), (resources / folder / name).read_bytes())


if __name__ == '__main__':
    unittest.main()
