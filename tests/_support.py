import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import _hermes_stub  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugin" / "instinct-memory"
spec = importlib.util.spec_from_file_location("instinct_memory", PLUGIN / "__init__.py",
    submodule_search_locations=[str(PLUGIN)])
plugin = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = plugin
spec.loader.exec_module(plugin)
from instinct_memory import schema, retrieval
from instinct_memory.vault import Vault


class VaultTestCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="instinct-test-")
        self.addCleanup(temp.cleanup)
        self.temp = Path(temp.name)
        self.vault_path = self.temp / "vault"
        shutil.copytree(ROOT / "examples" / "vault", self.vault_path)
        self.hermes_home = self.temp / "hermes"
        self.hermes_home.mkdir()
        env = patch.dict(os.environ, {"HERMES_HOME": str(self.hermes_home),
            "INSTINCT_VAULT": str(self.vault_path), "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_AUTHOR_NAME": "instinct-memory",
            "GIT_AUTHOR_EMAIL": "instinct-memory@users.noreply.github.com",
            "GIT_COMMITTER_NAME": "instinct-memory",
            "GIT_COMMITTER_EMAIL": "instinct-memory@users.noreply.github.com"})
        env.start()
        self.addCleanup(env.stop)
        self.vault = Vault(self.vault_path)
        self.vault.ensure()

    def reconciler(self):
        spec = importlib.util.spec_from_file_location("instinct_reconcile",
            ROOT / "scripts" / "instinct_reconcile.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def git(self, *args):
        return subprocess.run(["git", "-c", "user.name=instinct-memory", "-c",
            "user.email=instinct-memory@users.noreply.github.com", *args],
            cwd=self.vault_path, check=True, capture_output=True, text=True).stdout

    def snapshot(self, directory=None):
        directory = directory or self.vault_path
        return {str(p.relative_to(directory)): p.read_bytes() for p in directory.rglob("*")
            if p.is_file() and ".git" not in p.relative_to(directory).parts
            and ".locks" not in p.relative_to(directory).parts}
