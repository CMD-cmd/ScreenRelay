"""Create portable source-form VBA add-ins from Screen Relay's own blank containers.

Development tool only; requires oletools/olefile. MS-OVBA requires writers to
emit Version=0xFFFF without performance caches. Office compiles source when the
user enables the add-in through its normal macro security flow.
"""
from pathlib import Path
from zipfile import ZipFile, ZipInfo, ZIP_DEFLATED
from xml.etree import ElementTree as ET
import hashlib
import io
import json
import re
import struct
import sys
import uuid

import olefile
from oletools.olevba import VBA_Project, decompress_stream

from office_compound_file import build_compound
from prepare_portable_office_cursor import module
from build_office_cursor_payload import HOSTS


def digest(value):
    return hashlib.sha256(value).hexdigest()


def compress_literals(data):
    """MS-OVBA chunks with bounded dictionary matches and literal fallback."""
    if not data:
        return b'\x01'
    output = bytearray(b'\x01')
    for start in range(0, len(data), 4096):
        chunk = data[start:start + 4096]
        packed = bytearray()
        positions = {}
        position = 0
        while position < len(chunk):
            flag_index = len(packed)
            packed.append(0)
            for bit in range(8):
                if position == len(chunk):
                    break
                bits = max(4, (position - 1).bit_length())
                maximum = min(len(chunk) - position, (0xFFFF >> bits) + 3)
                length = offset = 0
                prefix = chunk[position:position + 3]
                for candidate in reversed(positions.get(prefix, [])[-128:]):
                    size = 3
                    while size < maximum and chunk[candidate + size] == chunk[position + size]:
                        size += 1
                    if size >= 3 and size > length:
                        length, offset = size, position - candidate
                    if length == maximum:
                        break
                if maximum >= 3 and length >= 3:
                    packed[flag_index] |= 1 << bit
                    packed.extend(struct.pack('<H', ((offset - 1) << (16 - bits)) | (length - 3)))
                    consumed = length
                else:
                    packed.append(chunk[position])
                    consumed = 1
                for index in range(position, position + consumed):
                    positions.setdefault(chunk[index:index + 3], []).append(index)
                position += consumed
        if len(packed) <= 4096:
            output.extend(struct.pack('<H', 0xB000 | (len(packed) - 1)))
            output.extend(packed)
        elif len(chunk) == 4096:
            output.extend(b'\xff\x3f' + chunk)
        else:
            raise ValueError('Short final chunk requires source padding')
    assert decompress_stream(output) == data
    return bytes(output)


def compress_source(data):
    remainder = len(data) % 4096
    if remainder and remainder + (remainder + 7) // 8 > 4096:
        count = 4096 - remainder
        data += (b'\r\n' * ((count + 1) // 2))[:count]
    return compress_literals(data)


def source_project(original, host):
    with olefile.OleFileIO(io.BytesIO(original)) as ole:
        streams = {tuple(path): ole.openstream(path).read() for path in ole.listdir()}
        clsids = {}
        for path in [()] + [tuple(path) for path in ole.listdir(streams=False, storages=True)]:
            entry = ole.root if not path else ole.direntries[ole._find(list(path))]
            if entry.clsid:
                clsids[path] = uuid.UUID(entry.clsid).bytes_le
        project = VBA_Project(ole, '', 'PROJECT', 'VBA/dir', relaxed=False)
        project.parse_project_stream()
        directory = bytearray(project.dir_stream.getvalue())
        previous = project.dir_stream.tell()
        if directory[previous + 6:previous + 12] != bytes.fromhex('130002000000'):
            raise ValueError('Unexpected project cookie record')
        directory[previous + 12:previous + 14] = b'\xff\xff'
        segments = []
        targets = 0
        expected_body = module(host).split('\n', 1)[1].replace('\n', '\r\n').encode('ascii')
        for _ in project.parse_modules():
            current = project.dir_stream.tell()
            item = project.modules[-1]
            segment = directory[previous:current]
            offsets = [previous + match.start() for match in re.finditer(b'\x31\x00\x04\x00\x00\x00', segment)]
            if len(offsets) != 1 or struct.unpack_from('<I', directory, offsets[0] + 6)[0] != item.textoffset:
                raise ValueError('Ambiguous VBA module offset record')
            segment = bytearray(segment)
            struct.pack_into('<I', segment, offsets[0] - previous + 6, 0)
            cookies = [match.start() for match in re.finditer(b'\x2c\x00\x02\x00\x00\x00', segment)]
            if len(cookies) != 1:
                raise ValueError('Ambiguous module cookie record')
            segment[cookies[0] + 6:cookies[0] + 8] = b'\xff\xff'
            path = tuple(item.code_path.split('/'))
            if path not in streams:
                raise ValueError('Module stream is not present under its actual Unicode name')
            source = item.code_raw
            if not source or not source.startswith(b'Attribute VB_Name = '):
                raise ValueError('Missing module source identity')
            if b'UUOfficeCursorInstall' in source:
                targets += 1
                name = module(host).split('\n', 1)[0].split('"')[1]
                old_name = source.splitlines()[0].split(b'"')[1]
                old_unicode = item._name_unicode or item.name
                if old_unicode != name:
                    # Emit an ASCII standard-module identity consistently in
                    # all four length-prefixed dir records and both name maps.
                    for record, old, new in ((0x19, old_name, name.encode('ascii')),
                            (0x47, old_unicode.encode('utf-16le'), name.encode('utf-16le')),
                            (0x1A, old_name, name.encode('ascii')),
                            (0x32, old_unicode.encode('utf-16le'), name.encode('utf-16le'))):
                        before = struct.pack('<HI', record, len(old)) + old
                        after = struct.pack('<HI', record, len(new)) + new
                        if segment.count(before) != 1:
                            raise ValueError('Ambiguous module identity record')
                        segment = segment.replace(before, after, 1)
                    project_text = streams[('PROJECT',)]
                    registration = b'Module=' + old_name + b'\r\n'
                    workspace = b'\r\n' + old_name + b'='
                    if project_text.count(registration) != 1 or project_text.count(workspace) != 1:
                        raise ValueError('Ambiguous standard-module registration')
                    streams[('PROJECT',)] = project_text.replace(registration, b'Module=' + name.encode('ascii') + b'\r\n').replace(
                        workspace, b'\r\n' + name.encode('ascii') + b'=')
                    mapping = old_name + b'\0' + old_unicode.encode('utf-16le') + b'\0\0'
                    if streams[('PROJECTwm',)].count(mapping) != 1:
                        raise ValueError('Ambiguous Unicode module name map')
                    streams[('PROJECTwm',)] = streams[('PROJECTwm',)].replace(mapping,
                        name.encode('ascii') + b'\0' + name.encode('utf-16le') + b'\0\0')
                    del streams[path]
                    path = path[:-1] + (name,)
                source = module(host).replace('\n', '\r\n').encode('ascii')
            streams[path] = compress_source(source)
            segments.append((previous, current, bytes(segment)))
            previous = current
        if targets != 1:
            raise ValueError('Expected exactly one owned native cursor module')
        for start, end, segment in reversed(segments):
            directory[start:end] = segment
        streams[('VBA', 'dir')] = compress_literals(bytes(directory))
        header = streams[('VBA', '_VBA_PROJECT')]
        if len(header) < 7 or header[:2] != b'\xcc\x61':
            raise ValueError('Invalid VBA project header')
        streams[('VBA', '_VBA_PROJECT')] = bytes.fromhex('cc61ffff000000')
        streams = {path: data for path, data in streams.items()
                   if not (path[:-1] == ('VBA',) and re.fullmatch(r'__SRP_[0-9A-Fa-f]+', path[-1]))}
    rebuilt = build_compound(streams, clsids=clsids)
    # Re-read the emitted compound file through an independent parser.
    with olefile.OleFileIO(io.BytesIO(rebuilt)) as ole:
        if set(map(tuple, ole.listdir())) != set(streams):
            raise ValueError('Rebuilt VBA project has different streams')
        for path, data in streams.items():
            if ole.openstream(list(path)).read() != data:
                raise ValueError('Rebuilt VBA stream mismatch')
        check = VBA_Project(ole, '', 'PROJECT', 'VBA/dir', relaxed=False)
        check.parse_project_stream()
        list(check.parse_modules())
        if any(item.textoffset != 0 for item in check.modules):
            raise ValueError('A module performance cache remains')
        actual = [item.code_raw.split(b'\r\n', 1)[1].rstrip(b'\r\n') for item in check.modules
                  if b'UUOfficeCursorInstall' in item.code_raw]
        if actual != [expected_body.rstrip(b'\r\n')]:
            raise ValueError('Portable loader source read-back mismatch')
    return rebuilt


def package(reference, destination, host, part):
    with ZipFile(reference) as archive:
        if archive.testzip() is not None or len(archive.namelist()) != len(set(archive.namelist())):
            raise ValueError('Invalid owned add-in reference')
        if any('vbaProjectSignature' in name or name.startswith('_xmlsignatures/') for name in archive.namelist()):
            raise ValueError('Signed VBA references require a separate signing workflow')
        # Our references are blank development documents; verify that property.
        for name in archive.namelist():
            if re.fullmatch(r'(xl/worksheets/sheet\d+|ppt/slides/slide\d+)\.xml', name):
                root = ET.fromstring(archive.read(name))
                if any(item.text for item in root.iter() if item.tag.rsplit('}', 1)[-1] in ('t', 'v')):
                    raise ValueError('Reference container must be blank')
        project = source_project(archive.read(f'{part}/vbaProject.bin'), host)
        with destination.open('xb') as output, ZipFile(output, 'w', compression=ZIP_DEFLATED) as packaged:
            for name in archive.namelist():
                data = project if name == f'{part}/vbaProject.bin' else archive.read(name)
                if name in ('docProps/core.xml', 'docProps/app.xml'):
                    # Keep namespace declarations and QName-valued attributes
                    # byte-for-byte. Generic XML reserialization can orphan
                    # prefixes in xsi:type or mc:Ignorable values.
                    text = data.decode('utf-8')
                    for field in ('creator', 'lastModifiedBy', 'Company', 'Manager'):
                        pattern = rf'(<(?:[\w]+:)?{field}(?:\s[^>]*)?>).*?(</(?:[\w]+:)?{field}>)'
                        text = re.sub(pattern, lambda match: match[1] + 'Screen Relay' + match[2], text, flags=re.DOTALL)
                    data = text.encode('utf-8')
                if name == 'xl/workbook.xml':
                    data = re.sub(rb'<(?:[\w]+:)?absPath\b[^>]*/>', b'', data)
                info = ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = ZIP_DEFLATED
                packaged.writestr(info, data)
    return project


def prepare(excel_reference, powerpoint_reference, output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    references = {'excel': Path(excel_reference), 'powerpoint': Path(powerpoint_reference)}
    hosts = {}
    for bundle, (host, folder, name, part, _) in HOSTS.items():
        (output / folder).mkdir()
        destination = output / folder / name
        project = package(references[host], destination, host, part)
        hosts[bundle] = {'sha256': digest(destination.read_bytes()), 'vba_project_sha256': digest(project),
            'source_sha256': digest(module(host).replace('\n', '\r\n').encode('ascii'))}
    index = {'schema': 1, 'version': '2.0.0', 'module_version': 2, 'hosts': hosts,
        'project_form': 'MS-OVBA source, Version=0xFFFF, no performance caches'}
    (output / 'templates.json').write_text(json.dumps(index, indent=2, sort_keys=True) + '\n')
    return index


if __name__ == '__main__':
    print(json.dumps(prepare(*sys.argv[1:]), indent=2))
