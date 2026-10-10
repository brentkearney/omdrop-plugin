"""`omdrop skill`: the agent skill is linked where Omarchy's agents look.
An entry named omdrop is replaced only when absent, dangling, or a link into
a plugin's skills/omdrop; remove takes back only such links."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OMDROP = ROOT / "bin" / "omdrop"
SKILL = ROOT / "skills" / "omdrop"


class SkillTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.claude = self.home / ".claude" / "skills"
        self.claude.mkdir(parents=True)
        self.link = self.claude / "omdrop"
        self.env = dict(os.environ, HOME=str(self.home))

    def omdrop(self, *args):
        return subprocess.run([OMDROP, *args], env=self.env, capture_output=True, text=True)

    def test_installs_only_where_an_agent_is_set_up(self):
        result = self.omdrop("skill", "install")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(os.readlink(self.link), str(SKILL))
        self.assertFalse((self.home / ".codex").exists())

    def test_a_skill_directory_the_user_wrote_is_left_in_place(self):
        self.link.mkdir()
        (self.link / "SKILL.md").write_text("mine\n")

        install = self.omdrop("skill", "install")
        self.omdrop("skill", "remove")

        self.assertIn("Left", install.stderr)
        self.assertEqual((self.link / "SKILL.md").read_text(), "mine\n")

    def test_a_live_link_elsewhere_is_left_in_place(self):
        mine = self.home / "my-skill"
        mine.mkdir()
        self.link.symlink_to(mine)

        self.omdrop("skill", "install")
        self.omdrop("skill", "remove")

        self.assertEqual(os.readlink(self.link), str(mine))

    def test_a_link_into_another_plugin_copy_is_replaced(self):
        old = self.home / "plugins" / "netmojo.omdrop" / "skills" / "omdrop"
        old.mkdir(parents=True)
        self.link.symlink_to(old)

        self.omdrop("skill", "install")

        self.assertEqual(os.readlink(self.link), str(SKILL))

    def test_a_dangling_link_is_replaced(self):
        self.link.symlink_to(self.home / "gone")

        self.omdrop("skill", "install")

        self.assertEqual(os.readlink(self.link), str(SKILL))

    def test_remove_takes_back_installed_and_dangling_plugin_links(self):
        self.omdrop("skill", "install")
        self.omdrop("skill", "remove")
        self.assertFalse(os.path.lexists(self.link))

        self.link.symlink_to(self.home / "plugins" / "gone" / "skills" / "omdrop")
        self.omdrop("skill", "remove")
        self.assertFalse(os.path.lexists(self.link))

    def failing(self, command):
        """Put a `command` that always fails ahead of the real one on PATH."""
        bindir = self.home / "bin"
        bindir.mkdir(exist_ok=True)
        fake = bindir / command
        fake.write_text("#!/bin/sh\necho injected failure >&2\nexit 1\n")
        fake.chmod(0o755)
        self.env["PATH"] = f"{bindir}:{os.environ['PATH']}"

    def test_a_link_that_cannot_be_made_fails_the_install(self):
        self.failing("ln")

        result = self.omdrop("skill", "install")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("could not link", result.stderr)
        self.assertNotIn("already installed", result.stdout)
        self.assertFalse(os.path.lexists(self.link))

    def test_a_link_that_cannot_be_removed_fails_the_remove(self):
        self.omdrop("skill", "install")
        self.failing("rm")

        result = self.omdrop("skill", "remove")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("could not remove", result.stderr)
        self.assertNotIn("Removed", result.stdout)
        self.assertTrue(os.path.lexists(self.link))


if __name__ == "__main__":
    unittest.main()
