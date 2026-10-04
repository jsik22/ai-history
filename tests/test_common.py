import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / 'bin/ai_history_common.py'
spec = importlib.util.spec_from_file_location('archive_common_tests', SOURCE)
common = importlib.util.module_from_spec(spec)
spec.loader.exec_module(common)

class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='archive-common-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.codex = common.Archive(self.root, 'codex')
        self.claude = common.Archive(self.root, 'claude')
        self.codex.setup()
        self.claude.setup()

    def row(self, tid='turn', answer='answer'):
        return dict(sid='session123', tid=tid, ts='2026-10-03 23:59:00',
                    cwd=str(Path.home()), prompt=' p\n\n', answer=answer)

    def blocks(self):
        return b''.join(p.read_bytes() for p in self.root.glob('*/*.md'))

    def prepare(self, partial=b''):
        row = self.row()
        target = self.root / '2026-10/2026-10-03.md'
        target.parent.mkdir()
        target.write_bytes(partial)
        common.atomic(self.codex.state / 'transaction.json', dict(
            relative_path=str(target.relative_to(self.root)), offset=0,
            block=self.codex.block(row).decode(), key=common.key(row['sid'],row['tid']),
            sid=row['sid'], tid=row['tid']))
        return row

    def test_other_writer_recovers_prepared_transaction(self):
        row = self.prepare()
        with self.claude.locked():
            self.claude.commit(self.row('other'))
        self.assertTrue(self.blocks().startswith(self.codex.block(row)))
        self.assertEqual(self.blocks().count(b'===== 2026'), 2)
        with self.codex.locked():
            self.assertFalse(self.codex.commit(row))

    def test_partial_append_completed_before_other_writer(self):
        row = self.row()
        self.prepare(self.codex.block(row)[:31])
        with self.claude.locked():
            self.claude.commit(self.row('other'))
        self.assertTrue(self.blocks().startswith(self.codex.block(row)))
        self.assertEqual(self.blocks().count(b'===== 2026'), 2)

    def test_marker_failure_retry_without_duplicate(self):
        original = common.atomic
        fail = [True]
        def atomic(path, value):
            if Path(path).parent.name == 'done' and fail[0]:
                fail[0] = False
                raise OSError('injected marker failure')
            return original(path, value)
        with self.codex.locked(), patch.object(common, 'atomic', atomic):
            with self.assertRaises(OSError):
                self.codex.commit(self.row())
            self.codex.commit(self.row('other'))
            self.assertFalse(self.codex.commit(self.row()))
        self.assertEqual(self.blocks().count(b'===== 2026'), 2)

    def test_ambiguous_content_never_overwritten(self):
        self.prepare(b'unexpected foreign bytes')
        original = self.blocks()
        with self.assertRaises(RuntimeError):
            with self.claude.locked():
                self.claude.commit(self.row('other'))
        self.assertEqual(self.blocks(), original)
        self.assertTrue((self.codex.state / 'transaction.json').exists())
        self.assertFalse((self.root / '.lock').exists())

    def test_exact_existing_block_adopted(self):
        row = self.row()
        with self.claude.locked():
            self.claude.commit(row)
            # Equivalent raw historical block with newly discovered UUID.
            self.assertFalse(self.claude.commit(self.row('discovered'), adopt_existing=True))
        self.assertEqual(self.blocks().count(b'===== 2026'), 1)

    def test_foreign_lock_preserved(self):
        lock = self.root / '.lock'
        lock.mkdir()
        with self.assertRaises(TimeoutError):
            with self.codex.locked(timeout=.02):
                self.fail('must not enter without owning lock')
        self.assertTrue(lock.exists())

    def test_raw_whitespace_and_private_permissions(self):
        row = self.row(answer='a\n\n')
        with self.codex.locked():
            self.codex.commit(row)
        self.assertIn(b'>  p\n> \n> \na\n\n\n\n', self.blocks())
        path = next(self.root.glob('*/*.md'))
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.codex.queue.stat().st_mode & 0o777, 0o700)

if __name__ == '__main__':
    unittest.main()
