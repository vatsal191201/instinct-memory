import json
from unittest.mock import Mock

from _support import VaultTestCase, plugin


class ProviderTests(VaultTestCase):
    def setUp(self):
        super().setUp()
        self.provider = plugin.InstinctMemoryProvider(config={
            'vault_path': str(self.vault_path), 'keep_holographic': False})
        self.provider.initialize('test-session', hermes_home=str(self.hermes_home))
        self.addCleanup(self.provider.shutdown)

    def call(self, name, **args):
        return json.loads(self.provider.handle_tool_call(name, args))

    def test_search_read_and_index(self):
        self.assertEqual(self.call('memory_search', query='jj')['results'][0]['id'], 'PERS-june')
        record = self.call('memory_read', id_or_name='PREF-coffee')
        self.assertEqual(record['name'], 'Coffee')
        self.assertIn('Decaf', record['facts'][-1]['line'])
        index = self.call('memory_index')
        self.assertEqual(index['count'], 6)
        self.assertIn('PERS-june', {r['id'] for r in index['records']})
        self.assertEqual(self.call('memory_index', type='person')['count'], 2)

    def test_note_only_appends_inbox(self):
        before = self.snapshot()
        result = self.call('memory_note', text='Sam prefers tea on Fridays.', about='PREF-coffee')
        self.assertEqual(result['status'], 'queued')
        after = self.snapshot()
        self.assertEqual({p for p in before if before[p] != after[p]}, {'raw/inbox.jsonl'})
        self.assertTrue(after['raw/inbox.jsonl'].startswith(before['raw/inbox.jsonl']))
        note = self.vault.read_jsonl('inbox.jsonl')[-1]
        self.assertEqual(note['text'], 'Sam prefers tea on Fridays.')
        self.assertEqual(note['session_id'], 'test-session')

    def test_agent_has_no_record_write_tool(self):
        self.assertEqual({s['name'] for s in self.provider.get_tool_schemas()},
            {'memory_search', 'memory_read', 'memory_index', 'memory_timeline', 'memory_note'})
        self.assertIn('error', self.call('memory_write', text='change'))

    def test_prompt_is_byte_stable_for_session(self):
        first = self.provider.system_prompt_block()
        self.assertIn('# Sam', first)
        self.assertIn('PERS-june', first)
        (self.vault_path / 'PROFILE.md').write_text('# Changed after initialization\n')
        self.assertEqual(first.encode(), self.provider.system_prompt_block().encode())

    def test_timeline(self):
        daily = self.call('memory_timeline', period='daily', date='2026-09-26')
        self.assertEqual(daily['count'], 1)
        self.assertIn('decaf', daily['entries'][0]['text'])
        self.assertEqual(self.call('memory_timeline', period='weekly')['count'], 1)

    def test_prefetch_and_trivial_query(self):
        self.assertIn('PERS-june', self.provider.prefetch('jj'))
        self.assertEqual(self.provider.recall_status().count, 1)
        self.assertEqual(self.provider.prefetch('hi!'), '')
        self.assertIsNone(self.provider.recall_status())

    def test_raw_capture(self):
        records = self.snapshot(self.vault_path / 'records')
        self.provider.sync_turn('June moved the sync.', 'Noted.',
            turn_author={'id': 'sam', 'name': 'Sam', 'is_bot': False})
        turn = self.vault.read_raw(self.provider._day())[-1]
        self.assertEqual(turn['author']['name'], 'Sam')
        self.assertEqual(turn['user'], 'June moved the sync.')
        self.assertEqual(records, self.snapshot(self.vault_path / 'records'))

    def test_delegate_passthrough(self):
        delegate = Mock()
        delegate.get_tool_schemas.return_value = [{'name': 'fact_store'}, {'name': 'fact_feedback'}]
        delegate.handle_tool_call.return_value = '{"status": "ok"}'
        self.provider._delegate = delegate
        self.assertIn('fact_store', {s['name'] for s in self.provider.get_tool_schemas()})
        for tool in ('fact_store', 'fact_feedback'):
            self.assertEqual(self.call(tool, action='probe'), {'status': 'ok'})
            delegate.handle_tool_call.assert_called_with(tool, {'action': 'probe'})
