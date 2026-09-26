from _support import VaultTestCase, schema


class SchemaTests(VaultTestCase):
    def text(self):
        return (self.vault_path / "records/person/PERS-june.md").read_text()

    def test_valid_record_and_vault(self):
        record = schema.parse_record(self.text())
        self.assertEqual(record.id, "PERS-june")
        self.assertIn("jj", record.aliases)
        self.assertEqual(schema.validate_record(record), [])
        self.assertEqual(self.vault.validate(), [])

    def test_bad_prefix(self):
        record = schema.parse_record(self.text().replace("id: PERS-june", "id: BAD-june"))
        self.assertTrue(any("id" in p for p in schema.validate_record(record)))

    def test_missing_fact_date(self):
        record = schema.parse_record(self.text().replace("- (2026-08-02)", "-"))
        self.assertTrue(any("undated fact" in p for p in schema.validate_record(record)))

    def test_dangling_link(self):
        record = schema.parse_record(self.text() + "- [[PERS-missing]]\n")
        self.assertTrue(any("dangling link [[PERS-missing]]" in p
            for p in schema.validate_record(record, known_ids=set(self.vault.index()))))

    def test_uppercase_alias_is_rejected(self):
        record = schema.parse_record(self.text().replace('- jj\n', '- JJ\n'))
        self.assertTrue(any('not lowercase' in p for p in schema.validate_record(record)))

    def test_fallback_roundtrip(self):
        from instinct_memory import _frontmatter
        data = {"name": "June", "aliases": ["jj", "sam's colleague"], "sources": []}
        self.assertEqual(_frontmatter.safe_load(_frontmatter.safe_dump(data)), data)
        record = schema.parse_record(self.text())
        rendered = schema.render_record(record)
        self.assertEqual(schema.parse_record(rendered).facts, record.facts)
