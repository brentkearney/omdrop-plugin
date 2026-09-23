"""`omdrop version` has to agree with the manifest it ships beside.

The script carries its own version constant so it can still answer when the
manifest is unreadable, and it warns when the two disagree. A release that
bumps only `manifest.json` therefore makes every `omdrop version` tell the user
their install is broken and ask them to reinstall. That shipped in 0.6.0, and
earlier as 0.4.1 against a 0.4.0 manifest.
"""

import json
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class VersionTests(unittest.TestCase):
    def test_version_matches_manifest_without_warning(self):
        manifest = json.loads((ROOT / "manifest.json").read_text())
        run = subprocess.run([str(ROOT / "bin" / "omdrop"), "version"],
                             capture_output=True, text=True, timeout=30)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.stdout.strip(), f"omdrop {manifest['version']}")
        self.assertEqual(run.stderr, "")


if __name__ == "__main__":
    unittest.main()
