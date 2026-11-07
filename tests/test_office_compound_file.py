"""CFB structure checks are independent of optional third-party readers."""
import io
from pathlib import Path
import struct
import sys
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "script"))
from office_compound_file import build_compound

try:
    import olefile
except ImportError:
    olefile = None

FREE, END, FAT = 0xFFFFFFFF, 0xFFFFFFFE, 0xFFFFFFFD


class CompoundReader:
    """Small independent parser: validate allocation and tree properties, not just bytes."""
    def __init__(self, data):
        self.data = data
        assert data[:8] == bytes.fromhex("d0cf11e0a1b11ae1")
        assert len(data) % 512 == 0
        assert struct.unpack_from("<HHHH", data, 26) == (3, 0xFFFE, 9, 6)
        assert data[8:24] == bytes(16) and data[34:40] == bytes(6)
        assert struct.unpack_from("<I", data, 40)[0] == 0
        assert struct.unpack_from("<I", data, 56)[0] == 4096
        assert struct.unpack_from("<II", data, 68) == (END, 0)
        self.fat_ids = list(struct.unpack_from("<109I", data, 76))
        count = struct.unpack_from("<I", data, 44)[0]
        assert 1 <= count <= 109
        assert all(value == FREE for value in self.fat_ids[count:])
        self.fat_ids = self.fat_ids[:count]
        self.fat = list(struct.unpack("<" + "I" * (count * 128), b"".join(self.sector(i) for i in self.fat_ids)))
        for index in self.fat_ids:
            assert self.fat[index] == FAT
        self.used = set(self.fat_ids)
        directory_ids = self.chain(struct.unpack_from("<I", data, 48)[0], self.fat)
        self.claim(directory_ids)
        directory = b"".join(self.sector(i) for i in directory_ids)
        self.entries = []
        for offset in range(0, len(directory), 128):
            record = directory[offset:offset + 128]
            if record[66] == 0:
                assert record == bytes(128)
                continue
            length, kind, color, left, right, child = struct.unpack_from("<HBBIII", record, 64)
            assert 2 <= length <= 64 and length % 2 == 0
            assert record[length - 2:length] == bytes(2)
            assert record[length:64] == bytes(64 - length)
            assert record[96:116] == bytes(20)
            start, size = struct.unpack_from("<IQ", record, 116)
            assert size >> 32 == 0
            if kind == 1:
                assert start == 0 and size == 0
            self.entries.append(dict(name=record[:length - 2].decode("utf-16le"), kind=kind,
                color=color, left=left, right=right, child=child, start=start, size=size,
                clsid=record[80:96]))
        assert self.entries[0]["kind"] == 5 and self.entries[0]["name"] == "Root Entry"
        assert self.entries[0]["left"] == FREE and self.entries[0]["right"] == FREE
        assert self.entries[0]["color"] == 1
        mini_start, mini_count = struct.unpack_from("<II", data, 60)
        mini_ids = self.chain(mini_start, self.fat)
        assert len(mini_ids) == mini_count
        self.claim(mini_ids)
        self.minifat = list(struct.unpack("<" + "I" * (len(mini_ids) * 128), b"".join(self.sector(i) for i in mini_ids)))
        self.mini = self.regular(self.entries[0]["start"], self.entries[0]["size"])
        self.mini_used = set()
        self.paths = {}
        visited = {0}

        def key(name):
            raw = name.upper().encode("utf-16le")
            return len(name.encode("utf-16le")), struct.unpack("<" + "H" * (len(raw) // 2), raw)

        def tree(index, path, lower=None, upper=None):
            if index == FREE:
                return 1
            assert 0 < index < len(self.entries) and index not in visited
            visited.add(index)
            item = self.entries[index]
            assert item["kind"] in (1, 2) and item["color"] in (0, 1)
            name_key = key(item["name"])
            assert lower is None or lower < name_key
            assert upper is None or name_key < upper
            if item["color"] == 0:
                for child in (item["left"], item["right"]):
                    assert child == FREE or self.entries[child]["color"] == 1
            left_height = tree(item["left"], path, lower, name_key)
            right_height = tree(item["right"], path, name_key, upper)
            assert left_height == right_height
            entry_path = path + (item["name"],)
            self.paths[entry_path] = item
            if item["kind"] == 1:
                storage(item, entry_path)
            else:
                assert item["child"] == FREE
            return left_height + item["color"]

        def storage(item, path):
            child = item["child"]
            assert child == FREE or self.entries[child]["color"] == 1
            tree(child, path)

        storage(self.entries[0], ())
        assert visited == set(range(len(self.entries)))
        for path, item in self.paths.items():
            if item["kind"] != 2:
                continue
            if item["size"] < 4096:
                chain = self.chain(item["start"], self.minifat)
                assert len(chain) == (item["size"] + 63) // 64
                assert not self.mini_used.intersection(chain)
                self.mini_used.update(chain)
                assert all((i + 1) * 64 <= len(self.mini) for i in chain)
                item["data"] = b"".join(self.mini[i * 64:(i + 1) * 64] for i in chain)[:item["size"]]
            else:
                item["data"] = self.regular(item["start"], item["size"])
        assert self.used == set(range(len(data) // 512 - 1)), "Unreferenced or multiply allocated regular sectors"
        assert self.mini_used == set(range(len(self.mini) // 64)), "Unreferenced mini sectors"
        assert all(value == FREE for value in self.minifat[len(self.mini_used):])
        assert all(value == FREE for value in self.fat[len(self.used):])

    def sector(self, index):
        assert 0 <= index < len(self.data) // 512 - 1
        return self.data[(index + 1) * 512:(index + 2) * 512]

    @staticmethod
    def chain(start, table):
        result, seen = [], set()
        while start != END:
            assert 0 <= start < len(table) and start not in seen
            seen.add(start)
            result.append(start)
            start = table[start]
        return result

    def claim(self, ids):
        assert not self.used.intersection(ids)
        self.used.update(ids)

    def regular(self, start, size):
        ids = self.chain(start, self.fat)
        assert len(ids) == (size + 511) // 512
        self.claim(ids)
        return b"".join(self.sector(i) for i in ids)[:size]


class CompoundWriterTests(unittest.TestCase):
    def fixture(self):
        return {("VBA", "中文模块", "流" + str(size)): bytes((i * 37 + size) % 256 for i in range(size))
                for size in [0, 63, 64, 4095, 4096, 65537]}

    def check_streams(self, streams, clsids=None):
        data = build_compound(streams, clsids)
        reader = CompoundReader(data)
        self.assertEqual({path: value["data"] for path, value in reader.paths.items() if value["kind"] == 2}, streams)
        if olefile is not None:
            with olefile.OleFileIO(io.BytesIO(data), raise_defects=olefile.DEFECT_INCORRECT) as ole:
                self.assertEqual({tuple(path): ole.openstream(path).read() for path in ole.listdir()}, streams)
        return reader

    def test_nested_unicode_boundary_stream_sizes(self):
        self.check_streams(self.fixture())
        self.check_streams({("根😀", "模块é"): b"unicode", ("根😀", "\x01CompObj"): bytes(64)})

    def test_empty_root_and_empty_stream(self):
        self.check_streams({})
        self.check_streams({("empty",): b""})

    def test_red_black_trees_many_siblings_and_storages(self):
        streams = {("VBA", f"Module{i:03}"): bytes([i % 256]) * (i % 70) for i in range(200)}
        streams.update({(f"Folder{i:03}", "Nested", "data"): bytes(4096 + i) for i in range(70)})
        streams.update({("a",): b"a", ("ZZ",): b"z", ("中",): b"c", ("𠀀",): b"s"})
        reader = self.check_streams(streams)
        self.assertGreater(len(reader.fat_ids), 1)
        self.assertEqual(build_compound(streams), build_compound(dict(reversed(list(streams.items())))))

    def test_storage_clsids_and_zero_metadata(self):
        root = uuid.UUID("00020906-0000-0000-c000-000000000046")
        storage = uuid.UUID("00020820-0000-0000-c000-000000000046")
        streams = {("VBA", "dir"): b"test"}
        data = build_compound(streams, {(): root.bytes_le, ("VBA",): storage.bytes_le, ("空存储",): storage.bytes_le})
        reader = CompoundReader(data)
        self.assertEqual(reader.entries[0]["clsid"], root.bytes_le)
        self.assertEqual(reader.paths[("VBA",)]["clsid"], storage.bytes_le)
        self.assertEqual(reader.paths[("空存储",)]["clsid"], storage.bytes_le)
        self.assertEqual(reader.paths[("VBA", "dir")]["clsid"], bytes(16))
        if olefile is not None:
            with olefile.OleFileIO(io.BytesIO(data), raise_defects=olefile.DEFECT_INCORRECT) as ole:
                self.assertEqual(ole.root.clsid.lower(), str(root))
                self.assertEqual(ole.getclsid(["VBA"]).lower(), str(storage))

    def test_all_mini_or_all_regular_and_multi_fat(self):
        for sizes in [[63] * 140, [4096, 65537, 131073], [64] * 7, [0] * 8]:
            self.check_streams({("stream" + str(i),): bytes([i]) * size for i, size in enumerate(sizes)})

    def test_name_length_counts_utf16_code_units(self):
        self.check_streams({("a" * 31,): b"ok", ("😀" * 15 + "a",): b"ok"})
        for name in ["a" * 32, "😀" * 16, "", "a/b", "a\\b", "a:b", "a!b", "a\0b", "\ud800"]:
            with self.subTest(name=repr(name)), self.assertRaises(ValueError):
                build_compound({(name,): b"bad"})

    def test_conflicting_paths_and_case_duplicates(self):
        for streams, clsids in [
            ({("a",): b"stream", ("a", "child"): b"nested"}, None),
            ({("A",): b"one", ("a",): b"two"}, None),
            ({("A", "one"): b"one", ("a", "two"): b"two"}, None),
            ({("ß",): b"one", ("SS",): b"two"}, None),
            ({("a",): b"stream"}, {("a",): bytes(16)}),
            ({("a",): b"stream"}, {("a", "sub"): bytes(16)}),
            ({(): b"not root storage"}, None),
        ]:
            with self.subTest(streams=streams), self.assertRaises(ValueError):
                build_compound(streams, clsids)

    def test_input_bounds_and_types(self):
        for streams, clsids in [
            ({"path": b"bad"}, None), ({("a",): "text"}, None),
            ({("a",): bytearray(b"a")}, None), ({(1,): b"bad"}, None),
            ({tuple("x" for _ in range(33)): b"bad"}, None),
            ({("x",): bytes(8 * 1024 * 1024 + 1)}, None),
            ({("x",): bytes(8 * 1024 * 1024)}, None),  # Needs extended DIFAT.
            ({("x",): b"x"}, {(): bytes(15)}),
            ({("x",): b"x"}, {(): "not bytes"}),
            ({(str(i),): b"" for i in range(4096)}, None),
            ([], None), ({}, []),
        ]:
            with self.subTest(kind=type(streams).__name__, clsids=clsids), self.assertRaises(ValueError):
                build_compound(streams, clsids)

    @unittest.skipIf(olefile is None, "olefile is an optional test-only reader")
    def test_third_party_reader_roundtrip(self):
        self.check_streams(self.fixture())


if __name__ == "__main__":
    unittest.main()
