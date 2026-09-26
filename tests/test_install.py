import os
import subprocess
import sys

from _support import ROOT, VaultTestCase


class InstallTests(VaultTestCase):
    def install(self):
        return subprocess.run(['bash', str(ROOT / 'install.sh')], check=True,
            capture_output=True, text=True, env=os.environ.copy())

    def test_install_is_idempotent_and_preserves_configuration(self):
        config = self.hermes_home / 'config.yaml'
        config.write_text('test configuration stays unchanged\n')
        self.assertIn('hermes config set memory.provider instinct', self.install().stdout)
        first = self.snapshot(self.hermes_home)
        self.install()
        self.assertEqual(self.snapshot(self.hermes_home), first)
        self.assertEqual(config.read_text(), 'test configuration stays unchanged\n')
        self.assertTrue((self.hermes_home / 'plugins/instinct').is_symlink())
        self.assertEqual((self.hermes_home / 'plugins/instinct').resolve(),
            self.hermes_home / 'plugins/instinct-memory')
        self.assertTrue((self.hermes_home / 'plugins/instinct/_frontmatter.py').exists())
        self.assertTrue((self.hermes_home / 'skills/productivity/instinct-memory/SKILL.md').exists())

    def test_installed_reconciler_runs_offline(self):
        self.install()
        before = self.snapshot()
        wrapper = self.hermes_home / 'scripts/instinct_reconcile.sh'
        preview = subprocess.run(['bash', str(wrapper), '--no-llm', '--dry-run', '--date', '2026-09-26'],
            check=True, capture_output=True, text=True)
        self.assertIn('Layer B skipped (--no-llm)', preview.stdout)
        self.assertEqual(self.snapshot(), before)
        script = self.hermes_home / 'scripts/instinct_reconcile.py'
        result = subprocess.run([sys.executable, str(script), '--no-llm', '--date', '2026-09-26'],
            check=True, capture_output=True, text=True)
        self.assertIn('committed:', result.stdout)
        self.assertEqual(self.git('status', '--porcelain'), '')
        self.assertEqual(self.git('remote'), '')
