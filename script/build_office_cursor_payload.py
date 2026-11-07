"""Build the offline Office cursor payload embedded in Screen Relay's signed app.

The compiled add-ins are release source assets. This build never launches Office
or depends on a developer's Office preferences, home directory, or VBA editor.
"""
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZipFile
import hashlib
import json
import shutil
import subprocess
import sys

from prepare_portable_office_cursor import module

ROOT = Path(__file__).resolve().parents[1]
HOSTS = {
    'com.microsoft.Excel': ('excel', 'Excel', 'UUDisplayNativeCursor.xlam', 'xl',
        'application/vnd.ms-excel.addin.macroEnabled.main+xml'),
    'com.microsoft.Powerpoint': ('powerpoint', 'PowerPoint', 'UUDisplayPowerPointCursor.ppam', 'ppt',
        'application/vnd.ms-powerpoint.addin.macroEnabled.main+xml'),
}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def validate_templates(directory):
    directory = Path(directory)
    index = json.loads((directory / 'templates.json').read_text())
    if index.get('schema') != 1 or index.get('version') != '2.0.0' or index.get('module_version') != 2:
        raise ValueError('Unsupported Office cursor template release')
    for host, (source_host, folder, name, part, content_type) in HOSTS.items():
        entry = index['hosts'][host]
        path = directory / folder / name
        if path.is_symlink() or digest(path.read_bytes()) != entry['sha256']:
            raise ValueError(f'Office template checksum mismatch: {name}')
        expected_source = module(source_host).replace('\n', '\r\n').encode('ascii')
        if entry['source_sha256'] != digest(expected_source):
            raise ValueError(f'Recompile the changed Office loader: {name}')
        with ZipFile(path) as archive:
            if archive.testzip() is not None or len(archive.namelist()) != len(set(archive.namelist())):
                raise ValueError(f'Invalid Office template package: {name}')
            if content_type.encode() not in archive.read('[Content_Types].xml'):
                raise ValueError(f'Invalid Office add-in content type: {name}')
            project = archive.read(f'{part}/vbaProject.bin')
            if not project.startswith(bytes.fromhex('d0cf11e0a1b11ae1')) or digest(project) != entry['vba_project_sha256']:
                raise ValueError(f'Office VBA project checksum mismatch: {name}')
    return index


def build(output, templates=None):
    output = Path(output)
    templates = Path(templates or ROOT / 'Resources/OfficeCursorTemplates')
    index = validate_templates(templates)
    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix='office-payload-', dir=output.parent) as temporary:
        staged = Path(temporary) / 'OfficeCursor'
        staged.mkdir()
        library = staged / 'libUUNativeCursor.dylib'
        subprocess.run(['xcrun', 'clang', '-fobjc-arc', '-O2', '-Wall', '-Wextra', '-Werror',
            '-arch', 'arm64', '-arch', 'x86_64', '-mmacosx-version-min=14.0', '-dynamiclib',
            str(ROOT / 'script/OfficeCursorScaler.m'), '-framework', 'AppKit',
            '-Wl,-install_name,@rpath/libUUNativeCursor.dylib',
            *[f'-Wl,-exported_symbol,_{name}' for name in ('UUOfficeCursorInstall', 'UUOfficeCursorStop',
                'UUOfficeCursorSetScale', 'UUOfficeCursorGetState')], '-o', str(library)], check=True)
        subprocess.run(['codesign', '--force', '--sign', '-', str(library)], check=True)
        subprocess.run(['codesign', '--verify', '--strict', str(library)], check=True)
        architectures = subprocess.check_output(['lipo', '-archs', str(library)], text=True).split()
        if set(architectures) != {'arm64', 'x86_64'}:
            raise ValueError('The Office cursor library must support Apple silicon and Intel')
        hosts = {}
        for host, (_, folder, name, _, _) in HOSTS.items():
            (staged / folder).mkdir()
            shutil.copyfile(templates / folder / name, staged / folder / name)
            hosts[host] = {'file': name, 'resource': f'{folder}/{name}', 'sha256': index['hosts'][host]['sha256']}
        manifest = {'schema': 1, 'version': index['version'], 'module_version': index['module_version'],
            'library': library.name, 'library_sha256': digest(library.read_bytes()), 'hosts': hosts}
        (staged / 'manifest.json').write_text(json.dumps(manifest, indent=2, sort_keys=True) + '\n')
        if output.exists():
            if output.is_symlink() or not output.is_dir():
                raise ValueError('Unsafe generated Office payload location')
            shutil.rmtree(output)
        shutil.copytree(staged, output)
    return manifest


if __name__ == '__main__':
    destination = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / 'dist/office-cursor-payload'
    print(json.dumps(build(destination), indent=2))
