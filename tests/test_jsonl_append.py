import contextlib
import errno
import json
import subprocess
import sys
from unittest.mock import patch

from _support import ROOT, VaultTestCase
from instinct_memory.vault import JsonlError

DAY = '2026-09-26'


def encoded(entry):
    return json.dumps(entry, ensure_ascii=False).encode('utf-8')


class JsonlAppendTests(VaultTestCase):
    def test_append_separates_unterminated_objects_and_preserves_bytes(self):
        first = {'ts': DAY, 'text': 'First fictional café ☕'}
        second = {'ts': DAY, 'text': 'Second fictional note'}
        for name in ('inbox.jsonl', DAY + '.jsonl'):
            with self.subTest(name=name):
                path = self.vault.layout.raw / name
                before = encoded(first)
                path.write_bytes(before)
                self.vault.append_raw(name, second)
                self.assertEqual(path.read_bytes(), before + b'\n' + encoded(second) + b'\n')
                self.assertEqual(self.vault.read_jsonl(name), [first, second])
                self.vault.append_raw(name, second)
                self.assertEqual(self.vault.read_jsonl(name), [first, second, second])

    def test_append_preserves_blank_tail_and_crlf(self):
        note = {'ts': DAY, 'text': 'A fictional note'}
        path = self.vault.layout.raw / 'inbox.jsonl'
        for before in (b'', b' \t', b'{}\r\n \t', b'{}\r', b'{}\r\n'):
            with self.subTest(before=before):
                path.write_bytes(before)
                self.vault.append_raw('inbox.jsonl', note)
                separator = b'\n' if before and not before.endswith(b'\n') else b''
                self.assertEqual(path.read_bytes(), before + separator + encoded(note) + b'\n')
                scan = self.vault.inspect_jsonl('inbox.jsonl')
                self.assertEqual(scan.errors, [])
                self.assertEqual(scan.entries[-1], note)

    def test_append_rejects_malformed_unterminated_tail_without_repair(self):
        path = self.vault.layout.raw / 'inbox.jsonl'
        for malformed in (b'{"text":', b'\xff', b'[]', b'{}{}', b'{}\n{broken'):
            with self.subTest(malformed=malformed):
                path.write_bytes(malformed)
                for _ in range(2):
                    with self.assertRaises(JsonlError):
                        self.vault.append_raw('inbox.jsonl', {'text': 'New note'})
                    self.assertEqual(path.read_bytes(), malformed)

    def test_archive_separates_unterminated_objects_and_rerun_is_noop(self):
        old = {'ts': DAY, 'text': 'Archived café ☕'}
        new = {'ts': DAY, 'text': 'Queued lunch'}
        archive = self.vault.layout.raw / 'inbox.processed.jsonl'
        inbox = self.vault.layout.raw / 'inbox.jsonl'
        archive.write_bytes(encoded(old))
        inbox.write_bytes(encoded(new))
        self.assertEqual(self.vault.consume_jsonl('inbox.jsonl', entries=[new]), 1)
        self.assertEqual(archive.read_bytes(), encoded(old) + b'\n' + encoded(new) + b'\n')
        self.assertEqual(self.vault.read_jsonl('inbox.processed.jsonl'), [old, new])
        self.assertEqual(inbox.read_bytes(), b'')
        before = self.snapshot()
        self.assertEqual(self.vault.consume_jsonl('inbox.jsonl', entries=[new]), 0)
        self.assertEqual(self.snapshot(), before)

    def test_bad_archive_tail_preserves_both_files(self):
        archive = self.vault.layout.raw / 'inbox.processed.jsonl'
        self.vault.append_raw('inbox.jsonl', {'ts': DAY, 'text': 'Keep queued'})
        for malformed in (b'{"text":', b'\xff', b'[]', b'{}{}'):
            with self.subTest(malformed=malformed):
                archive.write_bytes(malformed)
                before = self.snapshot()
                for _ in range(2):
                    with self.assertRaises(JsonlError):
                        self.vault.consume_jsonl('inbox.jsonl')
                    self.assertEqual(self.snapshot(), before)

    def test_boundary_read_failure_preserves_both_files(self):
        archive = self.vault.layout.raw / 'inbox.processed.jsonl'
        inbox = self.vault.layout.raw / 'inbox.jsonl'
        archive.write_bytes(encoded({'text': 'Archived'}))
        inbox.write_bytes(encoded({'text': 'Queued'}))
        before = self.snapshot()
        with patch.object(self.vault, 'read_jsonl', side_effect=OSError(errno.EIO, 'boundary read failure')):
            for action in (lambda: self.vault.append_raw('inbox.jsonl', {'text': 'New'}),
                           lambda: self.vault.consume_jsonl('inbox.jsonl')):
                with self.assertRaises(OSError):
                    action()
                self.assertEqual(self.snapshot(), before)

    def test_archive_lock_error_preserves_both_files(self):
        self.vault.append_raw('inbox.jsonl', {'ts': DAY, 'text': 'Keep queued'})
        before = self.snapshot()
        lock = self.vault.lock

        @contextlib.contextmanager
        def fail_archive(name, **kwargs):
            if name == 'raw-inbox.processed':
                raise OSError(errno.EIO, 'archive lock failure')
            with lock(name, **kwargs) as acquired:
                yield acquired

        with patch.object(self.vault, 'lock', fail_archive):
            with self.assertRaises(OSError):
                self.vault.consume_jsonl('inbox.jsonl')
        self.assertEqual(self.snapshot(), before)
        # The source lock acquired before the failure must have been released.
        with self.vault.lock('raw-inbox', blocking=False) as acquired:
            self.assertTrue(acquired)

    def test_archive_boundary_check_holds_source_and_destination_locks(self):
        self.vault.append_raw('inbox.jsonl', {'ts': DAY, 'text': 'Queued'})
        append = self.vault._append_jsonl_locked

        def checked_append(*args, **kwargs):
            for name in ('raw-inbox', 'raw-inbox.processed'):
                with self.vault.lock(name, blocking=False) as acquired:
                    self.assertFalse(acquired)
            return append(*args, **kwargs)

        with patch.object(self.vault, '_append_jsonl_locked', checked_append):
            self.assertEqual(self.vault.consume_jsonl('inbox.jsonl'), 1)

    def test_cannot_archive_into_same_file(self):
        self.vault.append_raw('inbox.jsonl', {'text': 'Keep queued'})
        before = self.snapshot()
        with self.assertRaises(ValueError):
            self.vault.consume_jsonl('inbox.jsonl', archive_name='inbox.jsonl')
        self.assertEqual(self.snapshot(), before)

    def test_cli_commits_valid_archive_then_rerun_is_noop(self):
        old = {'ts': DAY, 'text': 'Fictional archived café ☕'}
        new = {'ts': DAY, 'text': 'Fictional queued lunch'}
        raw = self.vault.layout.raw
        archive = raw / 'inbox.processed.jsonl'
        archive.write_bytes(encoded(old))
        (raw / 'inbox.jsonl').write_bytes(encoded(new) + b'\n')
        (raw / (DAY + '.jsonl')).write_bytes(b'')
        command = [sys.executable, str(ROOT / 'scripts/instinct_reconcile.py'),
                   '--date', DAY, '--no-llm']
        first = subprocess.run(command, capture_output=True, text=True, timeout=20)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(first.stderr, '')
        self.assertEqual(self.vault.read_jsonl('inbox.processed.jsonl'), [old, new])
        self.assertEqual(self.vault.read_jsonl('inbox.jsonl'), [])
        self.assertEqual(self.git('show', 'HEAD:raw/inbox.processed.jsonl'), archive.read_text())
        before = self.snapshot()
        head = self.git('rev-parse', 'HEAD')
        for _ in range(2):
            rerun = subprocess.run(command, capture_output=True, text=True, timeout=20)
            self.assertEqual(rerun.returncode, 0, rerun.stderr)
            self.assertEqual(rerun.stderr, '')
            self.assertEqual(self.snapshot(), before)
            self.assertEqual(self.git('rev-parse', 'HEAD'), head)
