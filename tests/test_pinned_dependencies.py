"""Everything this plugin installs has to be pinned to an exact revision.

The marketplace's security baseline refuses an unpinned remote build, and its
reviewer refused the listing a second time for a subtler version of the same
thing: `opendrop` was installed by package NAME through an AUR helper, so the
code the receiver imports and executes could change after the reviewed commit.
A name is not a revision. These tests read the install script the plugin would
run and insist every source it fetches is named by a full commit SHA.
"""

import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OMDROP = ROOT / "bin" / "omdrop"


class PinnedDependencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bin = Path(self.tmp.name) / "bin"
        self.bin.mkdir()
        # Nothing is installed and no repository carries the driver, which is
        # the state that makes the plugin emit its full build script. The
        # launcher is stubbed rather than removed: it is what the real run
        # hands the script to, so echoing its argument captures exactly the
        # commands a user's terminal would execute.
        self.command("python3", "#!/bin/sh\nexit 1\n")       # opendrop missing
        self.command("pacman", "#!/bin/sh\nexit 1\n")        # no repo package
        self.command("systemctl", "#!/bin/sh\nexit 3\n")     # ufw inactive
        self.command("omarchy-launch-floating-terminal-with-presentation",
                     "#!/bin/sh\nprintf '%s\\n' \"$1\"\n")
        self.env = os.environ.copy()
        self.env.update(PATH=f"{self.bin}:{self.env['PATH']}",
                        OMDROP_DISCOVERABLE="/nonexistent")

    def command(self, name, body):
        path = self.bin / name
        path.write_text(body)
        path.chmod(0o755)

    def build_script(self):
        result = subprocess.run([OMDROP, "install-driver"], env=self.env,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return OMDROP.read_text(), result.stdout

    def test_the_support_library_is_built_from_a_pinned_revision(self):
        # The regression: `yay -S opendrop` resolves to whatever the AUR holds
        # at install time, which is not the code anyone reviewed.
        _, script = self.build_script()
        self.assertNotIn("yay -S", script)
        self.assertNotIn("paru -S", script)
        self.assertRegex(script, r"checkout --detach [0-9a-f]{40} && makepkg")

    def test_every_pinned_commit_is_a_full_sha_used_literally(self):
        # Two places name each commit -- the constant a reader is shown, and
        # the literal the checkout uses, because a baseline scanner cannot
        # resolve a variable. They must not drift apart.
        source, script = self.build_script()
        for name in ("DRIVER_COMMIT", "OPENDROP_COMMIT"):
            declared = re.search(rf"^{name}=([0-9a-f]{{40}})$", source, re.M)
            self.assertIsNotNone(declared, f"{name} is not declared as a full SHA")
            self.assertIn(declared.group(1), script,
                          f"{name} is not used literally in the install script")

    def test_a_failed_checkout_never_falls_through_to_a_build(self):
        # Fail-closed: a checkout that could not reach the pinned commit must
        # not build whatever the clone happens to be sitting on.
        _, script = self.build_script()
        builds = re.findall(r"^.*\bmakepkg\b.*$", script, re.M)
        self.assertTrue(builds, script)
        for line in builds:
            self.assertRegex(line, r"checkout --detach [0-9a-f]{40} && makepkg")


if __name__ == "__main__":
    unittest.main()
