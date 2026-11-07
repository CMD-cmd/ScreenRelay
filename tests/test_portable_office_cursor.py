"""Portable loader contracts and offline template integrity; no Office or toolchain use."""
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile


SCRIPT = Path(__file__).resolve().parents[1] / 'script'


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPT / (name + '.py'))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


portable = load_script('prepare_portable_office_cursor')
# The payload builder imports its sibling generator, just as its command-line entry does.
with patch.dict(sys.modules, {'prepare_portable_office_cursor': portable}):
    payload = load_script('build_office_cursor_payload')


FIXTURE_HOSTS = {
    'com.microsoft.Excel': ('excel', 'Excel', 'UUDisplayNativeCursor.xlam', 'xl',
                           'application/vnd.ms-excel.addin.macroEnabled.main+xml'),
    'com.microsoft.Powerpoint': ('powerpoint', 'PowerPoint', 'UUDisplayPowerPointCursor.ppam', 'ppt',
                                'application/vnd.ms-powerpoint.addin.macroEnabled.main+xml'),
}
COMPOUND_HEADER = bytes.fromhex('d0cf11e0a1b11ae1')


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def procedure(source, name):
    """Read one emitted VBA procedure, keeping safety checks tied to their caller."""
    match = re.search(
        rf'^(?:Private|Public) (?:Function|Sub) {name}\b.*?^End (?:Function|Sub)$',
        source, re.M | re.S)
    if match is None:
        raise AssertionError(f'Missing VBA procedure: {name}')
    return match.group(0)


class PortableLoaderTests(unittest.TestCase):
    def test_prepare_writes_both_ascii_modules_with_only_crlf_newlines(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / '用户目录 😀' / 'addins'
            portable.prepare(destination)
            expected = {'excel': 'UUDisplayNativeCursor.bas',
                        'powerpoint': 'UUDisplayPowerPointCursor.bas'}
            self.assertEqual({p.name for p in destination.iterdir()}, set(expected.values()))
            for host, name in expected.items():
                with self.subTest(host=host):
                    raw = (destination / name).read_bytes()
                    self.assertEqual(raw.decode('ascii').replace('\r\n', '\n'), portable.module(host))
                    self.assertIn(b'\r\n', raw)
                    self.assertNotRegex(raw.replace(b'\r\n', b''), rb'[\r\n]')
                    self.assertNotIn(str(destination).encode('utf-8'), raw)
                    self.assertNotIn(str(Path.home()).encode('utf-8'), raw)
                    self.assertNotRegex(raw.lower(), rb'/users/|/home/|environ\$?\s*\(|macscript\s*\(')

    def test_host_directory_is_owned_addin_not_active_document(self):
        excel = portable.module('excel')
        self.assertIn('ThisWorkbook.Path', procedure(excel, 'AddinDirectory'))
        self.assertNotIn('ActiveWorkbook', excel)
        powerpoint = portable.module('powerpoint')
        lookup = procedure(powerpoint, 'AddinDirectory')
        self.assertIn('Application.AddIns', lookup)
        self.assertIn('item.FullName', lookup)
        self.assertIn('"uudisplaypowerpointcursor.ppam"', lookup)
        self.assertRegex(lookup, r'If found <> 1 Then Err\.Raise')
        self.assertNotIn('ActivePresentation', powerpoint)
        for host in FIXTURE_HOSTS.values():
            source = portable.module(host[0])
            self.assertIn('folder = AddinDirectory()', procedure(source, 'OpenNativeLibrary'))
            self.assertIn('libUUNativeCursor.dylib', procedure(source, 'OpenNativeLibrary'))
        with self.assertRaises(ValueError):
            portable.module('word')

    def test_unicode_paths_use_nul_terminated_utf8_and_reject_embedded_nul(self):
        for host in ('excel', 'powerpoint'):
            with self.subTest(host=host):
                source = portable.module(host)
                utf8 = procedure(source, 'UTF8')
                # Byte arrays, surrogate-pair handling and a zero-initialized final byte
                # are required for C APIs; this checks generated source, not a VBA runtime.
                self.assertRegex(utf8, r'As Byte\(\)')
                self.assertIn('&HD800&', utf8)
                self.assertIn('&HDBFF&', utf8)
                self.assertIn('&HDC00&', utf8)
                self.assertIn('&HDFFF&', utf8)
                self.assertIn('&HF0 Or', utf8)
                self.assertRegex(utf8, r'If code = 0 Then Err\.Raise')
                self.assertIn('ReDim Preserve bytes(0 To position)', utf8)
                self.assertLess(utf8.index('ReDim Preserve'), utf8.index('UTF8 = bytes'))
                for caller in ('OpenLibrary', 'FindSymbol', 'OpenNativeLibrary'):
                    body = procedure(source, caller)
                    self.assertIn('UTF8(', body)
                    self.assertIn('VarPtr(bytes(0))', body)
                self.assertNotIn('StrConv', source)

    def test_ffi_retry_only_bad_abi_and_eight_byte_return_storage(self):
        for host in ('excel', 'powerpoint'):
            with self.subTest(host=host):
                source = portable.module(host)
                prepare = procedure(source, 'PrepareNative')
                calls = [line.strip() for line in prepare.splitlines() if '= PrepareCall(' in line]
                self.assertEqual(len(calls), 2)
                # libffi FFI_BAD_ABI is 2; other failures must stop rather than retry.
                self.assertRegex(calls[0], r'^result = PrepareCall\(VarPtr\(Cif\(0\)\), 1, 0,')
                self.assertRegex(calls[1], r'^If result = 2 Then result = PrepareCall\(VarPtr\(Cif\(0\)\), 2, 0,')
                self.assertRegex(prepare, r'If result <> 0 Then Err\.Raise')
                call = procedure(source, 'NativeCall')
                self.assertRegex(call, r'Dim returned As Currency\b')
                self.assertIn('VarPtr(returned), 0', call)
                self.assertIn('CopyBytes(VarPtr(result), VarPtr(returned), 4)', call)
                self.assertNotIn('dlclose', source.lower())

    def test_management_policy_is_checked_for_shared_and_host_domains_before_loading(self):
        for bundle_id, (host, *_) in FIXTURE_HOSTS.items():
            with self.subTest(host=host):
                source = portable.module(host)
                check = procedure(source, 'PolicyBlocks')
                self.assertIn('DisableVisualBasicExternalDylibs', check)
                self.assertIn('PolicyBoolean(key, app, VarPtr(valid))', check)
                self.assertIn(f'"{bundle_id}"', procedure(source, 'HostDomain'))
                prepare = procedure(source, 'PrepareNative')
                self.assertIn('PolicyBlocks("com.microsoft.office") Or PolicyBlocks(HostDomain())', prepare)
                self.assertLess(prepare.index('PolicyBlocks('), prepare.index('OpenLibrary('))
                policy_branch = prepare[prepare.index('If PolicyBlocks'):prepare.index('End If')]
                self.assertIn('Err.Raise', policy_branch)
                self.assertNotRegex(source, r'CFPreferencesSet|defaults write|Shell\s*\(')


class TemplateValidationTests(unittest.TestCase):
    """Synthetic ZIP packages exercise validation without real VBA or Office assets."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        sources = self.directory / 'sources'
        portable.prepare(sources)
        self.index = {'schema': 1, 'version': '2.0.0', 'module_version': 2, 'hosts': {}}
        self.members = {}
        for bundle_id, (host, folder, name, part, content_type) in FIXTURE_HOSTS.items():
            path = self.directory / folder / name
            path.parent.mkdir()
            project = COMPOUND_HEADER + b'synthetic-vba-project-' + host.encode('ascii')
            self.members[bundle_id] = {
                '[Content_Types].xml': (
                    '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                    f'<Override PartName="/{part}/main.xml" ContentType="{content_type}"/>'
                    '</Types>').encode('ascii'),
                f'{part}/vbaProject.bin': project,
            }
            self.write_package(bundle_id)
            module_name = 'UUDisplayNativeCursor' if host == 'excel' else 'UUDisplayPowerPointCursor'
            self.index['hosts'][bundle_id] = {
                'sha256': sha256(path.read_bytes()),
                'source_sha256': sha256((sources / (module_name + '.bas')).read_bytes()),
                'vba_project_sha256': sha256(project),
            }
        self.write_index()

    def path(self, bundle_id):
        _, folder, name, _, _ = FIXTURE_HOSTS[bundle_id]
        return self.directory / folder / name

    def write_package(self, bundle_id):
        with ZipFile(self.path(bundle_id), 'w') as archive:
            for name, value in self.members[bundle_id].items():
                archive.writestr(name, value)

    def write_index(self):
        (self.directory / 'templates.json').write_text(json.dumps(self.index))

    def reseal_package(self, bundle_id):
        self.write_package(bundle_id)
        self.index['hosts'][bundle_id]['sha256'] = sha256(self.path(bundle_id).read_bytes())
        self.write_index()

    def test_valid_fixture_returns_release_index_without_mutating_files(self):
        before = {p.relative_to(self.directory): p.read_bytes()
                  for p in self.directory.rglob('*') if p.is_file()}
        self.assertEqual(payload.validate_templates(self.directory), self.index)
        after = {p.relative_to(self.directory): p.read_bytes()
                 for p in self.directory.rglob('*') if p.is_file()}
        self.assertEqual(before, after)

    def test_unknown_release_contract_is_rejected(self):
        for key, invalid in [('schema', 2), ('version', '1.0.0'), ('module_version', 1)]:
            with self.subTest(key=key):
                original = self.index[key]
                self.index[key] = invalid
                self.write_index()
                with self.assertRaisesRegex(ValueError, 'Unsupported'):
                    payload.validate_templates(self.directory)
                self.index[key] = original
        self.write_index()

    def test_modified_source_requires_recompilation_for_each_host(self):
        for bundle_id in FIXTURE_HOSTS:
            with self.subTest(host=bundle_id):
                original = self.index['hosts'][bundle_id]['source_sha256']
                self.index['hosts'][bundle_id]['source_sha256'] = '0' * 64
                self.write_index()
                with self.assertRaisesRegex(ValueError, 'Recompile'):
                    payload.validate_templates(self.directory)
                self.index['hosts'][bundle_id]['source_sha256'] = original
        self.write_index()

    def test_modified_package_is_rejected_by_file_hash(self):
        for bundle_id in FIXTURE_HOSTS:
            with self.subTest(host=bundle_id):
                path = self.path(bundle_id)
                original = path.read_bytes()
                path.write_bytes(original + b'tampered')
                with self.assertRaisesRegex(ValueError, 'template checksum mismatch'):
                    payload.validate_templates(self.directory)
                path.write_bytes(original)

    def test_wrong_content_type_rejected_even_with_updated_package_hash(self):
        for bundle_id in FIXTURE_HOSTS:
            with self.subTest(host=bundle_id):
                original = self.members[bundle_id]['[Content_Types].xml']
                self.members[bundle_id]['[Content_Types].xml'] = b'<Types>application/xml</Types>'
                self.reseal_package(bundle_id)
                with self.assertRaisesRegex(ValueError, 'content type'):
                    payload.validate_templates(self.directory)
                self.members[bundle_id]['[Content_Types].xml'] = original
                self.reseal_package(bundle_id)

    def test_changed_vba_project_rejected_even_with_updated_package_hash(self):
        for bundle_id, (_, _, _, part, _) in FIXTURE_HOSTS.items():
            with self.subTest(host=bundle_id):
                name = f'{part}/vbaProject.bin'
                original = self.members[bundle_id][name]
                self.members[bundle_id][name] = original + b'changed'
                self.reseal_package(bundle_id)
                with self.assertRaisesRegex(ValueError, 'VBA project checksum mismatch'):
                    payload.validate_templates(self.directory)
                self.members[bundle_id][name] = original
                self.reseal_package(bundle_id)

    def test_invalid_vba_header_rejected_even_when_both_hashes_match(self):
        bundle_id = 'com.microsoft.Excel'
        self.members[bundle_id]['xl/vbaProject.bin'] = b'not-a-compound-file'
        self.index['hosts'][bundle_id]['vba_project_sha256'] = sha256(b'not-a-compound-file')
        self.reseal_package(bundle_id)
        with self.assertRaisesRegex(ValueError, 'VBA project checksum mismatch'):
            payload.validate_templates(self.directory)

    def test_symlink_template_rejected_even_if_contents_match(self):
        bundle_id = 'com.microsoft.Excel'
        path = self.path(bundle_id)
        target = self.directory / 'same-template.xlam'
        path.rename(target)
        path.symlink_to(target)
        with self.assertRaisesRegex(ValueError, 'template checksum mismatch'):
            payload.validate_templates(self.directory)

    def test_invalid_templates_fail_build_before_external_tools_or_output_creation(self):
        self.index['hosts']['com.microsoft.Excel']['source_sha256'] = '0' * 64
        self.write_index()
        output = self.directory / 'output'
        with patch.object(payload.subprocess, 'run') as run, patch.object(payload.subprocess, 'check_output') as check:
            with self.assertRaisesRegex(ValueError, 'Recompile'):
                payload.build(output, self.directory)
        run.assert_not_called()
        check.assert_not_called()
        self.assertFalse(output.exists())


if __name__ == '__main__':
    unittest.main()
