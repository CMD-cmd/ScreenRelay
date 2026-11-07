import importlib.util
from pathlib import Path
import tempfile
import unittest
from zipfile import ZipFile

SPEC = importlib.util.spec_from_file_location(
    'powerpoint_package', Path(__file__).resolve().parents[1] / 'script/package_powerpoint_cursor_addin.py')
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class PowerPointPackageTests(unittest.TestCase):
    def fixture(self, directory):
        source = directory / 'source.pptm'
        parts = {'[Content_Types].xml':
                 ('<Types><Override PartName="/ppt/presentation.xml" ContentType="' +
                  MODULE.PRESENTATION + '"/></Types>').encode(),
                 'ppt/presentation.xml': b'<presentation/>',
                 'ppt/_rels/presentation.xml.rels':
                 b'<Relationships><Relationship Type="http://schemas.microsoft.com/office/2006/relationships/vbaProject" Target="vbaProject.bin"/></Relationships>',
                 'ppt/vbaProject.bin': b'fixture VBA project',
                 'docProps/core.xml': b'<metadata/>',
                 'ppt/slides/slide1.xml': b'<slide/>'}
        with ZipFile(source, 'w') as archive:
            for name, value in parts.items():
                archive.writestr(name, value)
        return source, parts

    def test_addin_preserves_compiled_vba_and_every_other_part(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            source, parts = self.fixture(directory)
            output = directory / 'cursor.ppam'
            report = MODULE.package(source, output)
            self.assertEqual(report['changed_parts'], ['[Content_Types].xml'])
            with ZipFile(output) as archive:
                self.assertEqual(set(archive.namelist()), set(parts))
                for name, value in parts.items():
                    if name == '[Content_Types].xml':
                        self.assertIn(MODULE.ADDIN.encode(), archive.read(name))
                    else:
                        self.assertEqual(archive.read(name), value)

    def test_existing_addin_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            source, _ = self.fixture(directory)
            output = directory / 'cursor.ppam'
            output.write_bytes(b'existing add-in')
            with self.assertRaises(FileExistsError):
                MODULE.package(source, output)
            self.assertEqual(output.read_bytes(), b'existing add-in')

    def test_plain_source_is_rejected_before_creating_addin(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            source, parts = self.fixture(directory)
            parts['[Content_Types].xml'] = b'<Types/>'
            with ZipFile(source, 'w') as archive:
                for name, value in parts.items():
                    archive.writestr(name, value)
            output = directory / 'cursor.ppam'
            with self.assertRaises(ValueError):
                MODULE.package(source, output)
            self.assertFalse(output.exists())


if __name__ == '__main__':
    unittest.main()
