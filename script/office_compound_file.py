"""Bounded CFB v3 writer for this project's authored Office/VBA streams.

No Office process, temporary files, or third-party packages are required. CLSID
bytes use the on-disk GUID byte order (for example ``uuid.UUID(...).bytes_le``).
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field

FREE = 0xFFFFFFFF
END = 0xFFFFFFFE
FAT_SECTOR = 0xFFFFFFFD
SECTOR_SIZE = 512
MINI_SIZE = 64
CUTOFF = 4096
MAX_INPUT_BYTES = 8 * 1024 * 1024
MAX_ENTRIES = 4096
MAX_DEPTH = 32


def _name_key(name: str) -> tuple[int, tuple[int, ...]]:
    encoded = name.encode("utf-16le")
    upper = name.upper().encode("utf-16le")
    return len(encoded), struct.unpack("<" + "H" * (len(upper) // 2), upper)


def _path(value: object, *, root: bool = False) -> tuple[str, ...]:
    if not isinstance(value, tuple) or (not value and not root) or len(value) > MAX_DEPTH:
        raise ValueError("CFB paths must be nonempty tuples with bounded depth")
    for name in value:
        if not isinstance(name, str) or not name or any(c in name for c in "\\/:!\0"):
            raise ValueError("Illegal CFB entry name")
        try:
            encoded = name.encode("utf-16le")
        except UnicodeError as exc:
            raise ValueError("CFB names must contain valid Unicode") from exc
        if len(encoded) > 62:
            raise ValueError("CFB entry names exceed 31 UTF-16 code units")
    return value


@dataclass
class _Entry:
    path: tuple[str, ...]
    kind: int
    data: bytes = b""
    clsid: bytes = bytes(16)
    left: int = FREE
    right: int = FREE
    child: int = FREE
    parent: int = FREE  # In-memory RB link, not a directory-record field.
    color: int = 1  # CFB: 0 red, 1 black.
    start: int = END
    size: int = 0
    children: list[int] = field(default_factory=list)


def _tree(entries: list[_Entry], owner: int) -> None:
    """Insert siblings using ordinary red-black rotations and recoloring."""
    root = FREE

    def black(index: int) -> bool:
        return index == FREE or entries[index].color == 1

    def rotate_left(index: int) -> None:
        nonlocal root
        node = entries[index]
        pivot_id = node.right
        pivot = entries[pivot_id]
        node.right = pivot.left
        if pivot.left != FREE:
            entries[pivot.left].parent = index
        pivot.parent = node.parent
        if node.parent == FREE:
            root = pivot_id
        elif index == entries[node.parent].left:
            entries[node.parent].left = pivot_id
        else:
            entries[node.parent].right = pivot_id
        pivot.left = index
        node.parent = pivot_id

    def rotate_right(index: int) -> None:
        nonlocal root
        node = entries[index]
        pivot_id = node.left
        pivot = entries[pivot_id]
        node.left = pivot.right
        if pivot.right != FREE:
            entries[pivot.right].parent = index
        pivot.parent = node.parent
        if node.parent == FREE:
            root = pivot_id
        elif index == entries[node.parent].right:
            entries[node.parent].right = pivot_id
        else:
            entries[node.parent].left = pivot_id
        pivot.right = index
        node.parent = pivot_id

    for node_id in sorted(entries[owner].children, key=lambda i: _name_key(entries[i].path[-1])):
        node = entries[node_id]
        node.color = 0
        parent, cursor = FREE, root
        while cursor != FREE:
            parent = cursor
            cursor = entries[cursor].left if _name_key(node.path[-1]) < _name_key(entries[cursor].path[-1]) else entries[cursor].right
        node.parent = parent
        if parent == FREE:
            root = node_id
        elif _name_key(node.path[-1]) < _name_key(entries[parent].path[-1]):
            entries[parent].left = node_id
        else:
            entries[parent].right = node_id
        cursor = node_id
        while cursor != root and not black(entries[cursor].parent):
            parent = entries[cursor].parent
            grand = entries[parent].parent
            if parent == entries[grand].left:
                uncle = entries[grand].right
                if not black(uncle):
                    entries[parent].color = entries[uncle].color = 1
                    entries[grand].color = 0
                    cursor = grand
                else:
                    if cursor == entries[parent].right:
                        cursor = parent
                        rotate_left(cursor)
                        parent = entries[cursor].parent
                        grand = entries[parent].parent
                    entries[parent].color = 1
                    entries[grand].color = 0
                    rotate_right(grand)
            else:
                uncle = entries[grand].left
                if not black(uncle):
                    entries[parent].color = entries[uncle].color = 1
                    entries[grand].color = 0
                    cursor = grand
                else:
                    if cursor == entries[parent].left:
                        cursor = parent
                        rotate_right(cursor)
                        parent = entries[cursor].parent
                        grand = entries[parent].parent
                    entries[parent].color = 1
                    entries[grand].color = 0
                    rotate_left(grand)
        entries[root].color = 1
    entries[owner].child = root


def build_compound(
    streams: dict[tuple[str, ...], bytes],
    clsids: dict[tuple[str, ...], bytes] | None = None,
) -> bytes:
    """Return a standard little-endian CFB v3 file; reject ambiguous input.

    At most 8 MiB of stream data and 4096 total entries are accepted. The output
    is additionally bounded to 109 FAT sectors so the header DIFAT suffices.
    ``()`` denotes the root storage only in ``clsids``; other storages are
    created automatically from stream paths and CLSID paths.
    """
    if not isinstance(streams, dict) or (clsids is not None and not isinstance(clsids, dict)):
        raise ValueError("CFB streams and CLSIDs must be dictionaries")
    clsids = clsids or {}
    if len(streams) + len(clsids) > MAX_ENTRIES:
        raise ValueError("Too many CFB input entries")
    total = 0
    for path, data in streams.items():
        _path(path)
        if not isinstance(data, bytes):
            raise ValueError("CFB stream data must be bytes")
        total += len(data)
        if total > MAX_INPUT_BYTES:
            raise ValueError("CFB stream data exceeds input limit")
    for path, clsid in clsids.items():
        _path(path, root=True)
        if not isinstance(clsid, bytes) or len(clsid) != 16:
            raise ValueError("CFB storage CLSIDs must be exactly 16 bytes")
        if path in streams:
            raise ValueError("A stream cannot carry a storage CLSID")

    storage_paths = {()}
    for path in streams:
        storage_paths.update(path[:i] for i in range(1, len(path)))
    for path in clsids:
        storage_paths.update(path[:i] for i in range(1, len(path) + 1))
    if storage_paths.intersection(streams):
        raise ValueError("CFB path is both a stream and storage")
    if len(storage_paths) + len(streams) > MAX_ENTRIES:
        raise ValueError("Too many CFB directory entries")

    # IDs are deterministic and independent of caller dictionary insertion order.
    paths = [()] + sorted((storage_paths | streams.keys()) - {()})
    entries = [_Entry(path, 5 if not path else (1 if path in storage_paths else 2),
                      streams.get(path, b""), clsids.get(path, bytes(16))) for path in paths]
    ids = {path: index for index, path in enumerate(paths)}
    sibling_names: dict[tuple[str, ...], set[str]] = {}
    for index, path in enumerate(paths[1:], 1):
        upper = path[-1].upper()
        names = sibling_names.setdefault(path[:-1], set())
        if upper in names:
            raise ValueError("Case-insensitive duplicate CFB sibling names")
        names.add(upper)
        entries[ids[path[:-1]]].children.append(index)
    for index, entry in enumerate(entries):
        if entry.kind != 2:
            _tree(entries, index)

    sectors: list[bytes] = []
    fat: list[int] = []

    def allocate(data: bytes) -> int:
        if not data:
            return END
        first = len(sectors)
        count = (len(data) + SECTOR_SIZE - 1) // SECTOR_SIZE
        for offset in range(count):
            sectors.append(data[offset * SECTOR_SIZE:(offset + 1) * SECTOR_SIZE].ljust(SECTOR_SIZE, b"\0"))
            fat.append(first + offset + 1 if offset + 1 < count else END)
        return first

    # Reserve directory sectors before assigning stream chains.
    directory_count = (len(entries) + 3) // 4
    directory_start = allocate(bytes(directory_count * SECTOR_SIZE))
    mini_data = bytearray()
    mini_fat: list[int] = []
    for entry in entries[1:]:
        if entry.kind != 2:
            continue
        entry.size = len(entry.data)
        if not entry.data:
            continue
        if entry.size >= CUTOFF:
            entry.start = allocate(entry.data)
        else:
            first = len(mini_fat)
            count = (entry.size + MINI_SIZE - 1) // MINI_SIZE
            entry.start = first
            mini_data.extend(entry.data.ljust(count * MINI_SIZE, b"\0"))
            mini_fat.extend(first + offset + 1 if offset + 1 < count else END for offset in range(count))
    entries[0].size = len(mini_data)
    entries[0].start = allocate(bytes(mini_data))
    mini_fat_start = allocate(struct.pack("<" + "I" * len(mini_fat), *mini_fat) +
                              b"\xff" * ((-len(mini_fat) * 4) % SECTOR_SIZE)) if mini_fat else END
    mini_fat_count = (len(mini_fat) * 4 + SECTOR_SIZE - 1) // SECTOR_SIZE

    records = bytearray(directory_count * SECTOR_SIZE)
    for index, entry in enumerate(entries):
        name = (entry.path[-1] if entry.path else "Root Entry").encode("utf-16le") + b"\0\0"
        offset = index * 128
        records[offset:offset + len(name)] = name
        struct.pack_into("<HBBIII", records, offset + 64, len(name), entry.kind, entry.color,
                         entry.left, entry.right, entry.child)
        records[offset + 80:offset + 96] = entry.clsid
        # State bits, creation/modification times and high stream-size word remain zero.
        struct.pack_into("<IQ", records, offset + 116, 0 if entry.kind == 1 else entry.start, entry.size)
    for index in range(directory_count):
        sectors[index] = bytes(records[index * SECTOR_SIZE:(index + 1) * SECTOR_SIZE])

    fat_count = 1
    while fat_count * 128 < len(sectors) + fat_count:
        fat_count += 1
    if fat_count > 109:
        raise ValueError("CFB exceeds the header-only DIFAT capacity")
    fat_ids = list(range(len(sectors), len(sectors) + fat_count))
    fat.extend([FAT_SECTOR] * fat_count)
    fat.extend([FREE] * (fat_count * 128 - len(fat)))
    fat_data = struct.pack("<" + "I" * len(fat), *fat)
    sectors.extend(fat_data[index * SECTOR_SIZE:(index + 1) * SECTOR_SIZE] for index in range(fat_count))

    header = bytearray(SECTOR_SIZE)
    header[:8] = bytes.fromhex("d0cf11e0a1b11ae1")
    struct.pack_into("<HHHHH", header, 24, 0x003E, 3, 0xFFFE, 9, 6)
    struct.pack_into("<IIIIIIIII", header, 40, 0, fat_count, directory_start, 0, CUTOFF,
                     mini_fat_start, mini_fat_count, END, 0)
    struct.pack_into("<109I", header, 76, *(fat_ids + [FREE] * (109 - fat_count)))
    return bytes(header) + b"".join(sectors)
