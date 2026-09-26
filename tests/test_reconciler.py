import contextlib
import io
import json
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
