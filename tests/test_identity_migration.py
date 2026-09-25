"""The identity moves from ~/.opendrop to ~/.omdrop without ever being lost.

A mistake here costs a user their Apple ID identity: Contacts Only devices
stop answering, and nothing says why. So the rules are pinned: copy once,
only into a missing ~/.omdrop, never touch the original, and leave nothing
behind that looks like an identity when the copy fails.
"""
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OMDROP = ROOT / "bin" / "omdrop"


def migration_functions():
    source = OMDROP.read_text()
    start = source.index('IDENTITY_DIR="$HOME/.omdrop"')
    end = source.index("\n}\n", source.index("migrate_identity() {")) + 3
    return source[start:end]


class IdentityMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.old = self.home / ".opendrop" / "keys"
        self.new = self.home / ".omdrop" / "keys"

    def tearDown(self):
        for path in self.home.rglob("*"):
            if path.is_dir():
                path.chmod(0o700)
        self.tmp.cleanup()

    def migrate(self):
        script = f"set -u\n{migration_functions()}\nmigrate_identity\n"
        return subprocess.run(
            ["bash", "-c", script],
            env={**os.environ, "HOME": str(self.home)},
            capture_output=True,
            text=True,
        ).returncode

    def legacy_identity(self):
        self.old.mkdir(parents=True)
        for name, body in (
            ("certificate.pem", "CERT"),
            ("key.pem", "KEY"),
            ("validation_record.cms", "RECORD"),
        ):
            path = self.old / name
            path.write_text(body)
            path.chmod(0o600)

    def test_copies_every_file_and_leaves_the_original(self):
        self.legacy_identity()
        self.assertEqual(self.migrate(), 0)
        for name, body in (
            ("certificate.pem", "CERT"),
            ("key.pem", "KEY"),
            ("validation_record.cms", "RECORD"),
        ):
            self.assertEqual((self.new / name).read_text(), body)
            self.assertEqual((self.old / name).read_text(), body)
            self.assertEqual(stat.S_IMODE((self.new / name).stat().st_mode), 0o600)
            # A copy, not a link: opendrop elsewhere may rewrite its own files.
            self.assertNotEqual((self.new / name).stat().st_ino, (self.old / name).stat().st_ino)
        self.assertEqual(stat.S_IMODE(self.new.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.new.parent.stat().st_mode), 0o700)

    def test_never_replaces_an_existing_omdrop(self):
        self.legacy_identity()
        self.new.mkdir(parents=True)
        (self.new / "certificate.pem").write_text("MINE")
        self.assertEqual(self.migrate(), 1)
        self.assertEqual((self.new / "certificate.pem").read_text(), "MINE")
        self.assertFalse((self.new / "validation_record.cms").exists())

    def test_nothing_to_copy_creates_nothing(self):
        self.assertEqual(self.migrate(), 1)
        self.assertFalse((self.home / ".omdrop").exists())

    def test_failed_copy_leaves_no_partial_identity(self):
        self.legacy_identity()
        (self.old / "key.pem").chmod(0o000)
        if os.access(self.old / "key.pem", os.R_OK):
            self.skipTest("running as a user who can read mode-000 files")
        self.assertEqual(self.migrate(), 2)
        self.assertFalse((self.home / ".omdrop").exists())
        self.assertEqual(list(self.home.glob(".omdrop.*")), [])
        # And a later attempt, once the file is readable again, still copies.
        (self.old / "key.pem").chmod(0o600)
        self.assertEqual(self.migrate(), 0)
        self.assertEqual((self.new / "key.pem").read_text(), "KEY")


if __name__ == "__main__":
    unittest.main()
