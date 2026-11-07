"""Package the locally authored PowerPoint VBA source as a PPAM.

PowerPoint for Mac exports PPTM from its UI. The add-in uses the same package
and VBA project with the PresentationPart content type changed to AddIn.
No Office process is launched and no macro-security preferences are modified.
"""
import hashlib
import json
from pathlib import Path
import sys
from xml.etree import ElementTree as ET
from zipfile import ZipFile

PRESENTATION = 'application/vnd.ms-powerpoint.presentation.macroEnabled.main+xml'
ADDIN = 'application/vnd.ms-powerpoint.addin.macroEnabled.main+xml'


def package(source, destination):
    source, destination = Path(source), Path(destination)
    if source.resolve() == destination.resolve():
        raise ValueError('Source and add-in must be separate files')
    if source.suffix.lower() != '.pptm' or destination.suffix.lower() != '.ppam':
        raise ValueError('Expected a PPTM source and a PPAM destination')
    with ZipFile(source) as archive:
        names = archive.namelist()
        if len(set(names)) != len(names):
            raise ValueError('Duplicate package parts')
        vba = archive.read('ppt/vbaProject.bin')
        types = archive.read('[Content_Types].xml')
        root = ET.fromstring(types)
        main = [item for item in root if item.get('PartName') == '/ppt/presentation.xml']
        if len(main) != 1 or main[0].get('ContentType') != PRESENTATION:
            raise ValueError('Source must be a macro-enabled PowerPoint presentation')
        rels = ET.fromstring(archive.read('ppt/_rels/presentation.xml.rels'))
        if not any(item.get('Type', '').endswith('/vbaProject') and
                   item.get('Target') == 'vbaProject.bin' for item in rels):
            raise ValueError('Missing local VBA project relationship')
        if types.count(PRESENTATION.encode()) != 1:
            raise ValueError('Ambiguous presentation content type')
        replacement = types.replace(PRESENTATION.encode(), ADDIN.encode(), 1)
        # Exclusive creation keeps existing installed add-ins and release files safe.
        with destination.open('xb') as output:
            with ZipFile(output, 'w') as packaged:
                for info in archive.infolist():
                    packaged.writestr(info, replacement if info.filename == '[Content_Types].xml'
                                     else archive.read(info.filename))
    with ZipFile(destination) as packaged:
        if packaged.testzip() is not None or packaged.read('ppt/vbaProject.bin') != vba:
            raise ValueError('Packaged VBA project verification failed')
    return {'source': str(source), 'add_in': str(destination),
            'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
            'add_in_sha256': hashlib.sha256(destination.read_bytes()).hexdigest(),
            'vba_project_sha256': hashlib.sha256(vba).hexdigest(),
            'changed_parts': ['[Content_Types].xml']}


if __name__ == '__main__':
    print(json.dumps(package(*sys.argv[1:]), ensure_ascii=False, indent=2))
