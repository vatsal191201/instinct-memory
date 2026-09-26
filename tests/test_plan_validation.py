"""Pure edit-plan checks; no CLI or semantic reconcile run is invoked."""
from _support import VaultTestCase


class PlanValidationTests(VaultTestCase):
    def setUp(self):
        super().setUp()
        self.rc = self.reconciler()
        self.plan_vault = self.rc.Vault(self.vault_path)

    def test_invalid_plan_does_not_partially_edit_records(self):
        before = self.snapshot()
        plan = {'record_edits': [
            {'op': 'add_facts', 'id': 'PERS-june', 'facts': ['Prefers a written agenda.']},
            {'op': 'link', 'id': 'PERS-june', 'links': ['PERS-missing']}]}
        _, problems = self.rc.apply_plan(self.plan_vault, plan, dry_run=False)
        self.assertTrue(problems)
        self.assertEqual(self.snapshot(), before)

    def test_invalid_field_type_is_rejected(self):
        plan = {'record_edits': [{'op': 'add_facts', 'id': 'PERS-june', 'facts': 'not a list'}]}
        before = self.snapshot()
        _, problems = self.rc.apply_plan(self.plan_vault, plan, dry_run=False)
        self.assertTrue(problems)
        self.assertEqual(self.snapshot(), before)

    def test_plan_dry_run_validates_without_writing(self):
        before = self.snapshot()
        plan = {'record_edits': [{'op': 'correct_fact', 'id': 'PREF-mornings',
            'old_text': 'No meetings', 'new_text': 'No meetings before 11:00.'}]}
        count, problems = self.rc.apply_plan(self.plan_vault, plan, dry_run=True)
        self.assertEqual((count, problems), (1, []))
        self.assertEqual(self.snapshot(), before)

    def test_valid_plan_uses_history_preserving_writer(self):
        old = self.plan_vault.find('PREF-mornings').facts[0].render()
        plan = {'record_edits': [{'op': 'correct_fact', 'id': 'PREF-mornings',
            'old_text': 'No meetings', 'new_text': 'No meetings before 11:00.'}]}
        self.assertEqual(self.rc.apply_plan(self.plan_vault, plan, dry_run=False), (1, []))
        facts = self.plan_vault.find('PREF-mornings').facts
        self.assertTrue(facts[0].render().startswith(old + ' [!superseded '))
        self.assertEqual(facts[1].corrects_on, '2026-04-22')
