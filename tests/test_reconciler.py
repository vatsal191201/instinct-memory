import contextlib
import io
import json
import os
from unittest.mock import patch

from _support import VaultTestCase

DAY = '2026-09-26'


class ReconcilerTests(VaultTestCase):
    def setUp(self):
        super().setUp()
        self.rc = self.reconciler()
        self.no_llm = patch.object(self.rc, '_llm_plan', side_effect=AssertionError('LLM must stay offline'))
        self.no_llm.start()
        self.addCleanup(self.no_llm.stop)

    def run_main(self, *args):
        with patch('sys.argv', ['instinct_reconcile.py', '--date', DAY, '--no-llm', *args]), contextlib.redirect_stdout(io.StringIO()):
            return self.rc.main()

    def test_dry_run_does_not_write(self):
        (self.vault_path / 'INDEX.md').write_text('stale index\n')
        (self.vault_path / 'timeline/daily' / (DAY + '.md')).unlink()
        before = self.snapshot()
        self.assertEqual(self.run_main('--dry-run'), 0)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.vault_path / '.git').exists())

    def test_invalid_vault_never_commits(self):
        path = self.vault_path / 'records/person/PERS-june.md'
        path.write_text(path.read_text().replace('- jj\n', '- JJ\n'))
        self.assertEqual(self.run_main(), 1)
        self.assertFalse((self.vault_path / '.git').exists())

    def test_validation_runs_without_activity(self):
        (self.vault_path / 'raw' / (DAY + '.jsonl')).unlink()
        (self.vault_path / 'raw/inbox.processed.jsonl').write_text('')
        path = self.vault_path / 'records/person/PERS-june.md'
        path.write_text('not a record')
        self.assertEqual(self.run_main('--dry-run'), 1)

    def add_note(self, day=DAY, text='Sam prefers tea on Fridays.'):
        self.vault.append_raw('inbox.jsonl', {'ts': day + 'T12:00:00+00:00',
            'session_id': 'test-session', 'text': text, 'about': 'PREF-coffee'})

    def test_no_llm_builds_commits_and_archives_after_commit(self):
        self.add_note()
        (self.vault_path / 'INDEX.md').write_text('stale\n')
        (self.vault_path / 'timeline/daily' / (DAY + '.md')).unlink()
        consume = self.rc.Vault.consume_jsonl
        observed = []

        def checked_consume(vault, *args, **kwargs):
            committed_inbox = self.git('show', 'HEAD:raw/inbox.jsonl')
            self.assertIn('tea on Fridays', committed_inbox)
            self.assertIn('tea on Fridays', self.git('show', 'HEAD:timeline/daily/' + DAY + '.md'))
            observed.append(True)
            return consume(vault, *args, **kwargs)

        records = self.snapshot(self.vault_path / 'records')
        with patch.object(self.rc.Vault, 'consume_jsonl', checked_consume):
            self.assertEqual(self.run_main(), 0)
        self.assertEqual(observed, [True])
        self.assertEqual(self.vault.read_jsonl('inbox.jsonl'), [])
        self.assertIn('tea on Fridays', self.vault.read_jsonl('inbox.processed.jsonl')[-1]['text'])
        self.assertIn('PERS-june', (self.vault_path / 'INDEX.md').read_text())
        self.assertTrue((self.vault_path / 'timeline/weekly/2026-W39.md').exists())
        self.assertEqual(records, self.snapshot(self.vault_path / 'records'))
        self.assertEqual(self.git('status', '--porcelain'), '')
        identities = set(self.git('log', '--format=%an <%ae>|%cn <%ce>').splitlines())
        identity = 'instinct-memory <instinct-memory@users.noreply.github.com>'
        self.assertEqual(identities, {identity + '|' + identity})

    def test_failed_commit_leaves_inbox_untouched(self):
        self.add_note()
        inbox = (self.vault_path / 'raw/inbox.jsonl').read_bytes()
        archive = (self.vault_path / 'raw/inbox.processed.jsonl').read_bytes()
        with patch.object(self.rc, 'commit', side_effect=RuntimeError('commit failed')):
            with self.assertRaises(RuntimeError):
                self.run_main()
        self.assertEqual((self.vault_path / 'raw/inbox.jsonl').read_bytes(), inbox)
        self.assertEqual((self.vault_path / 'raw/inbox.processed.jsonl').read_bytes(), archive)

    def test_only_snapshot_notes_from_selected_day_are_archived(self):
        self.add_note()
        self.add_note('2026-09-27', 'Keep this future proposal queued.')
        original_commit = self.rc.commit
        late = 'This note arrived during the commit.'

        def concurrent_commit(vault, message):
            if 'deterministic layer' in message:
                self.add_note(text=late)
            return original_commit(vault, message)

        with patch.object(self.rc, 'commit', concurrent_commit):
            self.assertEqual(self.run_main(), 0)
        remaining = self.vault.read_jsonl('inbox.jsonl')
        self.assertEqual({n['text'] for n in remaining}, {'Keep this future proposal queued.', late})

    def test_rerun_preserves_rollups_and_can_replay_archived_notes(self):
        self.add_note()
        self.assertEqual(self.run_main(), 0)
        before = self.snapshot()
        head = self.git('rev-parse', 'HEAD')
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.git('rev-parse', 'HEAD'), head)
        prompt, _ = self.rc.build_prompt(self.rc.Vault(self.vault_path), DAY, max_records=25)
        self.assertIn('Sam prefers tea on Fridays.', prompt)

    def test_home_relative_vault_override(self):
        with patch.dict(os.environ, {'HOME': str(self.temp), 'INSTINCT_VAULT': '~/vault'}):
            self.assertEqual(self.reconciler().VAULT_ROOT, self.vault_path)
