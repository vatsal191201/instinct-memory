from _support import VaultTestCase, retrieval, schema


class RetrievalTests(VaultTestCase):
    def test_alias_hit_beats_body_hit(self):
        records = self.vault.load()
        body_hit = next(r for r in records if r.id == 'PERS-sam')
        body_hit.facts.append(schema.build_fact('2026-09-26', 'Works with jj.'))
        hits, _ = retrieval.search(records, 'jj')
        self.assertEqual([h.record.id for h in hits[:2]], ['PERS-june', 'PERS-sam'])
        self.assertGreater(hits[0].score, hits[1].score)

    def test_coffee_query(self):
        hits, _ = retrieval.search(self.vault.load(), "sam's coffee")
        self.assertEqual(hits[0].record.id, 'PREF-coffee')

    def test_trivial_queries_return_nothing(self):
        for query in ['', ' ', 'the and', 'what about it']:
            with self.subTest(query=query):
                hits, _ = retrieval.search(self.vault.load(), query)
                self.assertEqual(hits, [])
