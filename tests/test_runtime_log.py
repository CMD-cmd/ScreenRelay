"""Bounded diagnostics use only their own paths and never block recovery."""
import errno
import fcntl
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import controller as c


class RuntimeLogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.path = self.directory / 'runtime-events.jsonl'
        self.previous = self.directory / 'runtime-events.jsonl.1'

    def test_append_keeps_json_lines_and_owner(self):
        self.assertTrue(c.runtime_event(self.directory, 'one', message='中文'))
        first = self.path.read_bytes()
        self.assertTrue(c.runtime_event(self.directory, 'two', value=2))
        self.assertTrue(self.path.read_bytes().startswith(first))
        records = [json.loads(line) for line in self.path.read_text().splitlines()]
        self.assertEqual([r['event'] for r in records], ['one', 'two'])
        self.assertEqual(records[0]['message'], '中文')
        self.assertTrue(all(r['pid'] == os.getpid() for r in records))

    def test_rotation_preserves_previous_file_and_has_a_total_bound(self):
        untouched = self.directory / 'backups'
        untouched.mkdir()
        original = untouched / 'original'
        original.write_bytes(b'protected original')
        with patch.object(c, 'RUNTIME_LOG_BYTES', 1024):
            for number in range(100):
                self.assertTrue(c.runtime_event(self.directory, 'event', number=number, info='x' * 200))
                self.assertLessEqual(self.path.stat().st_size, 1024)
                if self.previous.exists():
                    self.assertLessEqual(self.previous.stat().st_size, 1024)
        self.assertTrue(self.previous.exists())
        recent = [json.loads(line) for line in self.path.read_text().splitlines()]
        older = [json.loads(line) for line in self.previous.read_text().splitlines()]
        self.assertEqual(recent[-1]['number'], 99)
        self.assertLess(older[-1]['number'], recent[0]['number'])
        self.assertEqual(original.read_bytes(), b'protected original')
        self.assertEqual(set(p.name for p in self.directory.iterdir()),
                         {'backups', self.path.name, self.previous.name, '.runtime-events.lock'})

    def test_one_rotation_retains_all_original_bytes(self):
        original = b'x' * 1000
        self.path.write_bytes(original)
        with patch.object(c, 'RUNTIME_LOG_BYTES', 1024):
            self.assertTrue(c.runtime_event(self.directory, 'fresh'))
        self.assertEqual(self.previous.read_bytes(), original)
        self.assertEqual(json.loads(self.path.read_text())['event'], 'fresh')

    def test_oversized_fields_cannot_grow_the_log_unboundedly(self):
        self.assertTrue(c.runtime_event(self.directory, 'oversized', details='x' * 100000))
        record = json.loads(self.path.read_text())
        self.assertTrue(record['fields_omitted'])
        self.assertGreater(record['original_bytes'], 100000)
        self.assertLess(self.path.stat().st_size, c.RUNTIME_EVENT_BYTES)

    def test_symlinks_and_hardlinks_are_never_written_or_replaced(self):
        original = self.directory / 'unrelated'
        original.write_bytes(b'keep')
        for name in ('runtime-events.jsonl', '.runtime-events.lock', 'runtime-events.jsonl.1'):
            for kind in ('symlink', 'hardlink'):
                with self.subTest(name=name, kind=kind):
                    path = self.directory / name
                    path.unlink(missing_ok=True)
                    if kind == 'symlink':
                        path.symlink_to(original)
                    else:
                        os.link(original, path)
                    if name.endswith('.1'):
                        self.path.write_bytes(b'x' * c.RUNTIME_LOG_BYTES)
                    self.assertFalse(c.runtime_event(self.directory, 'must-not-write'))
                    self.assertEqual(original.read_bytes(), b'keep')
                    path.unlink()
            self.path.unlink(missing_ok=True)

    def test_busy_logger_returns_without_waiting(self):
        lock = self.directory / '.runtime-events.lock'
        with lock.open('w') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            self.assertFalse(c.runtime_event(self.directory, 'busy'))
        self.assertFalse(self.path.exists())
        self.assertTrue(c.runtime_event(self.directory, 'released'))

    def test_space_exhaustion_is_not_propagated_into_display_logic(self):
        with patch.object(c.os, 'write', side_effect=OSError(errno.ENOSPC, 'injected')):
            self.assertFalse(c.runtime_event(self.directory, 'failure'))
        self.assertTrue(c.runtime_event(self.directory, 'recovered'))


if __name__ == '__main__':
    unittest.main()
