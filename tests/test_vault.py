from _support import VaultTestCase, schema


class VaultTests(VaultTestCase):
    def test_correction_keeps_old_line(self):
        before = self.vault.find('PREF-mornings')
        old_line = before.facts[0].render()
        self.vault.correct_fact('PREF-mornings', 'No meetings', 'No meetings before 11:00.', when='2026-09-27')
        after = self.vault.find('PREF-mornings')
        self.assertEqual(after.facts[0].render(), old_line + ' [!superseded 2026-09-27]')
        self.assertFalse(after.facts[0].is_current())
        self.assertEqual(after.facts[1].corrects_on, before.facts[0].date)
        self.assertEqual(self.vault.validate(), [])

    def test_correction_cannot_delete_original(self):
        rec = self.vault.find('PREF-mornings')
        before = self.snapshot()
        rec.facts = [schema.build_fact('2026-09-27',
            'Correction: No meetings before 11:00. [corrects 2026-04-22]')]
        with self.assertRaises(schema.RecordError):
            self.vault.write(rec)
        self.assertEqual(self.snapshot(), before)

    def test_force_cannot_delete_history(self):
        rec = self.vault.find('PERS-sam')
        before = self.snapshot()
        rec.facts.pop()
        with self.assertRaises(schema.RecordError):
            self.vault.write(rec, force=True)
        self.assertEqual(self.snapshot(), before)
