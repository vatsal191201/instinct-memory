import contextlib
import errno
import io
import json
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from _support import ROOT, VaultTestCase, schema
from instinct_memory import vault as storage

DAY = '2026-09-26'


class StorageFailureTests(VaultTestCase):
    def test_preimage_read_error_preserves_history(self):
        rec = self.vault.find('PERS-sam')
        path = self.vault.path_for(rec)
        before = path.read_bytes()
        rec.facts.pop()
        with self.assertRaises(schema.RecordError):
            self.vault.write(rec)
        read_text = Path.read_text

        def fail_preimage(target, *args, **kwargs):
            if target == path:
                raise OSError(errno.EIO, 'injected preimage read failure')
            return read_text(target, *args, **kwargs)

        for force in (False, True):
            with self.subTest(force=force), patch.object(Path, 'read_text', fail_preimage):
                with self.assertRaises(OSError) as caught:
                    self.vault.write(rec, force=force)
                self.assertEqual(caught.exception.errno, errno.EIO)
            self.assertEqual(path.read_bytes(), before)

    def test_corrupt_preimages_are_not_overwritten(self):
        rec = self.vault.find('PERS-sam')
        path = self.vault.path_for(rec)
        original = path.read_bytes()
        for corrupt in (b'broken frontmatter\n', b'\xff\n',
                        original.replace(b'id: PERS-sam', b'id: PERS-someone'),
                        original.replace(b'- (2026-08-02)', b'-')):
            with self.subTest(corrupt=corrupt[:30]):
                path.write_bytes(corrupt)
                with self.assertRaises((schema.RecordError, UnicodeDecodeError)):
                    self.vault.write(rec)
                self.assertEqual(path.read_bytes(), corrupt)

    def test_missing_preimage_can_be_created_and_sources_roundtrip(self):
        facts = [schema.build_fact(DAY, 'Mira likes café ☕.')]
        sources = ['notes: café ☕', 'https://example.com/fictional']
        rec = self.vault.create('person', 'Mira', facts=facts, sources=sources)
        rec.prose = 'A fictional collaborator.'
        self.vault.write(rec)
        stored = schema.parse_record(self.vault.path_for(rec).read_text())
        self.assertEqual(stored.sources, sources)
        self.assertEqual(stored.facts, facts)

    def test_dangling_preimage_is_not_absence(self):
        rec = self.vault.find('PERS-sam')
        path = self.vault.path_for(rec)
        path.unlink()
        path.symlink_to('missing-record.md')
        with self.assertRaises(FileNotFoundError):
            self.vault.write(rec)
        self.assertTrue(path.is_symlink())

    def test_preimage_inspection_error_is_not_absence(self):
        rec = self.vault.find('PERS-sam')
        path = self.vault.path_for(rec)
        before = path.read_bytes()
        lstat = Path.lstat

        def fail_inspection(target, *args, **kwargs):
            if target == path:
                raise PermissionError(errno.EACCES, 'injected inspection failure')
            return lstat(target, *args, **kwargs)

        with patch.object(Path, 'lstat', fail_inspection):
            with self.assertRaises(PermissionError):
                self.vault.write(rec)
        self.assertEqual(path.read_bytes(), before)

    def test_lock_system_errors_never_yield(self):
        for blocking in (True, False):
            for code in (errno.EIO, errno.ENOLCK, errno.EINTR, errno.EBADF):
                with self.subTest(blocking=blocking, errno=code):
                    with patch.object(storage.fcntl, 'flock', side_effect=OSError(code, 'injected')):
                        with self.assertRaises(OSError) as caught:
                            with self.vault.lock('probe', blocking=blocking):
                                self.fail('lock body ran without acquisition')
                    self.assertEqual(caught.exception.errno, code)

    def test_only_nonblocking_contention_yields_false(self):
        for code in (errno.EAGAIN, errno.EACCES):
            with self.subTest(errno=code):
                with patch.object(storage.fcntl, 'flock', side_effect=OSError(code, 'contention')):
                    with self.vault.lock('probe', blocking=False) as acquired:
                        self.assertFalse(acquired)
                    with self.assertRaises(OSError):
                        with self.vault.lock('probe', blocking=True):
                            self.fail('blocking lock failure ran its body')

    def test_all_mutators_abort_on_lock_failure(self):
        note = {'ts': DAY, 'text': 'A fictional proposal ☕'}
        self.vault.append_raw('inbox.jsonl', note)
        rec = self.vault.find('PERS-sam')
        rec.prose += ' Changed prose.'
        before = self.snapshot()
        mutations = (
            lambda: self.vault.write(rec),
            lambda: self.vault.append_raw('inbox.jsonl', note),
            lambda: self.vault.consume_jsonl('inbox.jsonl', entries=[note]),
        )
        for mutate in mutations:
            with patch.object(storage.fcntl, 'flock', side_effect=OSError(errno.EIO, 'injected')):
                with self.assertRaises(OSError):
                    mutate()
            self.assertEqual(self.snapshot(), before)

    def test_lock_error_prevents_replace_and_lost_append_schedule(self):
        first = {'ts': DAY, 'text': 'Committed note'}
        late = {'ts': DAY, 'text': 'Late note'}
        self.vault.append_raw('inbox.jsonl', first)
        before = self.snapshot()
        atomic = self.vault._atomic_write

        def interleave(path, content):
            self.vault.append_raw('inbox.jsonl', late)
            return atomic(path, content)

        with patch.object(storage.fcntl, 'flock', side_effect=OSError(errno.EIO, 'injected')):
            with patch.object(self.vault, '_atomic_write', side_effect=interleave) as replace:
                with self.assertRaises(OSError):
                    self.vault.consume_jsonl('inbox.jsonl', entries=[first])
                replace.assert_not_called()
        self.assertEqual(self.snapshot(), before)

    def test_late_append_waits_for_consumer_and_remains_queued(self):
        first = {'ts': DAY, 'text': 'Committed note'}
        late = {'ts': DAY, 'text': 'Late note ☕'}
        self.vault.append_raw('inbox.jsonl', first)
        archive_before = self.vault.read_jsonl('inbox.processed.jsonl')
        atomic = self.vault._atomic_write
        lock = self.vault.lock
        attempting = threading.Event()
        main_thread = threading.get_ident()
        futures = []

        @contextlib.contextmanager
        def observed_lock(*args, **kwargs):
            if threading.get_ident() != main_thread:
                attempting.set()
            with lock(*args, **kwargs) as acquired:
                yield acquired

        with ThreadPoolExecutor(max_workers=1) as executor:
            def interleave(path, content):
                # A separate descriptor must see genuine contention at replacement.
                with lock('raw-inbox', blocking=False) as acquired:
                    self.assertFalse(acquired)
                futures.append(executor.submit(self.vault.append_raw, 'inbox.jsonl', late))
                self.assertTrue(attempting.wait(timeout=5))
                self.assertFalse(futures[0].done())
                return atomic(path, content)

            with patch.object(self.vault, 'lock', observed_lock):
                with patch.object(self.vault, '_atomic_write', interleave):
                    self.assertEqual(self.vault.consume_jsonl('inbox.jsonl', entries=[first]), 1)
                futures[0].result(timeout=5)
        self.assertEqual(self.vault.read_jsonl('inbox.jsonl'), [late])
        self.assertEqual(self.vault.read_jsonl('inbox.processed.jsonl'), archive_before + [first])

    def test_jsonl_reports_every_failed_physical_line(self):
        good = json.dumps({'text': 'café ☕\u2028still one physical line'}, ensure_ascii=False).encode()
        data = b'\n' + good + b'\r\n{broken\r\n\xff\n[]\nnull\n\n'
        for name in (DAY + '.jsonl', 'inbox.jsonl'):
            path = self.vault.layout.raw / name
            path.write_bytes(data)
            read = (lambda: self.vault.read_raw(DAY)) if name.startswith(DAY) else (
                lambda: self.vault.read_jsonl(name))
            with self.assertRaises(storage.JsonlError) as caught:
                read()
            result = caught.exception.result
            self.assertEqual(result.nonblank, 5)
            self.assertEqual(len(result.entries), 1)
            self.assertEqual(len(result.errors), 4)
            for number, error in zip(range(3, 7), result.errors):
                self.assertIn(f'{path}:{number}:', error)
            self.assertEqual(path.read_bytes(), data)

    def test_consumer_preserves_malformed_and_unselected_bytes(self):
        note = {'ts': DAY, 'text': 'café ☕'}
        selected = json.dumps(note, ensure_ascii=False).encode() + b'\r\n'
        remaining = b'{broken\r\n\xff\n[]\n\r\n{"text":"future"}'
        inbox = self.vault.layout.raw / 'inbox.jsonl'
        archive = self.vault.layout.raw / 'inbox.processed.jsonl'
        archive_before = archive.read_bytes()
        inbox.write_bytes(selected + remaining)
        self.assertEqual(self.vault.consume_jsonl('inbox.jsonl', entries=[note]), 1)
        self.assertEqual(inbox.read_bytes(), remaining)
        self.assertEqual(archive.read_bytes(), archive_before + selected)

    def test_jsonl_io_failure_is_not_empty_input(self):
        path = self.vault.raw_daily_path(DAY)
        before = path.read_bytes()
        open_path = Path.open

        def fail_read(target, *args, **kwargs):
            if target == path:
                raise OSError(errno.EIO, 'injected JSONL read failure')
            return open_path(target, *args, **kwargs)

        with patch.object(Path, 'open', fail_read):
            with self.assertRaises(OSError):
                self.vault.read_raw(DAY)
        self.assertEqual(path.read_bytes(), before)


class ReconcileFailureTests(VaultTestCase):
    def run_cli(self, *args, wrapper=None):
        script = str(ROOT / 'scripts/instinct_reconcile.py')
        command = [sys.executable, script] if wrapper is None else [sys.executable, '-c', wrapper, script]
        return subprocess.run([*command, '--date', DAY, '--no-llm', *args],
                              capture_output=True, text=True, timeout=20)

    def test_cli_malformed_inputs_fail_twice_before_work(self):
        raw = self.vault.layout.raw
        good = json.dumps({'ts': DAY, 'user': 'Sam likes café ☕'}, ensure_ascii=False).encode()
        (raw / (DAY + '.jsonl')).write_bytes(good + b'\r\n\n{broken\r\n')
        (raw / 'inbox.jsonl').write_bytes(b'{broken\n')
        (raw / 'inbox.processed.jsonl').write_bytes(b'')
        before = self.snapshot()
        for _ in range(2):
            result = self.run_cli()
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn('JSONL inputs: 3 nonblank = 1 valid + 2 failed', result.stdout)
            self.assertIn(f'{DAY}.jsonl:3:', result.stderr)
            self.assertIn('inbox.jsonl:1:', result.stderr)
            self.assertIn('incomplete input', result.stderr)
            after = self.snapshot()
            after.pop('raw/reconcile.log', None)
            self.assertEqual(after, before)
            self.assertFalse((self.vault_path / '.git').exists())

    def test_all_input_files_are_checked_even_in_dry_run(self):
        for name in ('inbox.processed.jsonl', 'memory_tool_writes.jsonl'):
            with self.subTest(name=name):
                path = self.vault.layout.raw / name
                original = path.read_bytes() if path.exists() else None
                path.write_bytes(b'\xff\n')
                before = self.snapshot()
                result = self.run_cli('--dry-run')
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn(f'{name}:1:', result.stderr)
                self.assertEqual(self.snapshot(), before)
                if original is None:
                    path.unlink()
                else:
                    path.write_bytes(original)

    def test_malformed_input_stops_semantic_run_before_processing(self):
        (self.vault.layout.raw / 'inbox.jsonl').write_bytes(b'{broken\n')
        rc = self.reconciler()
        with patch('sys.argv', ['reconcile', '--date', DAY]):
            with patch.object(rc, '_llm_plan') as model, patch.object(rc, 'commit') as commit:
                with patch.object(rc, 'rollup_day') as rollup:
                    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                        self.assertEqual(rc.main(), 1)
                rollup.assert_not_called()
            model.assert_not_called()
            commit.assert_not_called()

    def test_cli_lock_error_is_runtime_failure_and_contention_is_distinct(self):
        before = self.snapshot()
        wrapper = '''import errno, fcntl, runpy, sys
def fail(*args):
    raise OSError(errno.EIO, "injected flock failure")
fcntl.flock = fail
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name="__main__")
'''
        result = self.run_cli(wrapper=wrapper)
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn('injected flock failure', result.stderr)
        after = self.snapshot()
        after.pop('raw/reconcile.log', None)
        self.assertEqual(after, before)
        with self.vault.lock('reconcile'):
            result = self.run_cli()
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn('another reconcile run holds the lock', result.stdout)

    def test_interruption_resume_and_noop_preserve_unicode_sources_and_history(self):
        rc = self.reconciler()
        note = {'ts': DAY, 'text': 'Sam prefers café ☕ on Fridays.'}
        self.vault.append_raw('inbox.jsonl', note)
        before = self.snapshot()
        records = self.snapshot(self.vault.layout.records)
        with patch('sys.argv', ['reconcile', '--date', DAY, '--no-llm']):
            with patch.object(rc, 'commit', side_effect=KeyboardInterrupt('before commit')):
                with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(KeyboardInterrupt):
                    rc.main()
        for name in ('raw/inbox.jsonl', 'raw/inbox.processed.jsonl'):
            self.assertEqual((self.vault_path / name).read_bytes(), before[name])
        result = self.run_cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        snapshot = self.snapshot()
        head = self.git('rev-parse', 'HEAD')
        result = self.run_cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.snapshot(), snapshot)
        self.assertEqual(self.git('rev-parse', 'HEAD'), head)
        self.assertEqual(self.snapshot(self.vault.layout.records), records)
        self.assertEqual(self.vault.read_jsonl('inbox.processed.jsonl').count(note), 1)
        self.assertEqual(self.vault.read_jsonl('inbox.jsonl'), [])
        self.assertIn(note['text'], (self.vault.layout.daily / (DAY + '.md')).read_text())
        self.assertEqual(self.vault.validate(), [])

    def test_empty_dry_run_leaves_absent_target_absent(self):
        import os
        absent = self.temp / 'absent'
        with patch.dict(os.environ, {'INSTINCT_VAULT': str(absent)}):
            result = self.run_cli('--dry-run')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('JSONL inputs: 0 nonblank = 0 valid + 0 failed', result.stdout)
        self.assertFalse(absent.exists())
