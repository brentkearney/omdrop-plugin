"""Exercise the real CLI installer in an isolated home/config directory."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class StrataInstallTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.config = self.home / "custom-config"
        self.target = self.config / "strata/actions/omdrop"
        self.env = dict(os.environ, HOME=str(self.home), XDG_CONFIG_HOME=str(self.config))

    def command(self, operation):
        return subprocess.run([ROOT / "bin/omdrop", "strata", operation], env=self.env,
                              capture_output=True, text=True)

    def test_install_is_private_regular_idempotent_and_removable(self):
        for _ in range(2):
            result = self.command("install")
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.home / ".config/strata").exists())
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o700)
        for name in ("action.toml", "run.py"):
            installed = self.target / name
            self.assertFalse(installed.is_symlink())
            self.assertEqual(installed.stat().st_mode & 0o777, 0o600)
            self.assertEqual(installed.read_bytes(), (ROOT / "share/strata/omdrop" / name).read_bytes())
        for _ in range(2):
            result = self.command("remove")
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.target.exists())

    def test_existing_edited_and_extra_files_are_preserved(self):
        self.target.mkdir(parents=True)
        mine = self.target / "run.py"
        mine.write_text("my action\n")
        for operation in ("install", "remove"):
            self.assertNotEqual(self.command(operation).returncode, 0)
            self.assertEqual(mine.read_text(), "my action\n")
        mine.unlink()
        self.target.rmdir()
        self.assertEqual(self.command("install").returncode, 0)
        extra = self.target / "personal-note"
        extra.write_text("keep me")
        for operation in ("install", "remove"):
            self.assertNotEqual(self.command(operation).returncode, 0)
            self.assertEqual(extra.read_text(), "keep me")
        extra.unlink()
        mine.write_text("edited action\n")
        for operation in ("install", "remove"):
            self.assertNotEqual(self.command(operation).returncode, 0)
            self.assertEqual(mine.read_text(), "edited action\n")

    def test_symlinks_are_not_followed(self):
        self.target.parent.mkdir(parents=True)
        victim = self.home / "other-action"
        victim.mkdir()
        self.target.symlink_to(victim, target_is_directory=True)
        for operation in ("install", "remove"):
            self.assertNotEqual(self.command(operation).returncode, 0)
        self.assertEqual(list(victim.iterdir()), [])
        self.target.unlink()
        self.assertEqual(self.command("install").returncode, 0)
        (self.target / "run.py").unlink()
        outside = self.home / "my-script"
        outside.write_text("unchanged")
        (self.target / "run.py").symlink_to(outside)
        for operation in ("install", "remove"):
            self.assertNotEqual(self.command(operation).returncode, 0)
        self.assertEqual(outside.read_text(), "unchanged")

    def test_an_unedited_previous_bundle_can_be_updated(self):
        self.assertEqual(self.command("install").returncode, 0)
        (self.target / "run.py").write_text("previous bundled version")
        record = {name: hashlib.sha256((self.target / name).read_bytes()).hexdigest()
                  for name in ("action.toml", "run.py")}
        (self.target / ".omdrop-installed.json").write_text(json.dumps(record))
        result = self.command("install")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.target / "run.py").read_bytes(),
                         (ROOT / "share/strata/omdrop/run.py").read_bytes())
