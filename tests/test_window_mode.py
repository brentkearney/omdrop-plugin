"""`status` has to name the window that is actually running.

The panel's duration row is a control AND a report: it moves itself to the
running window so a reopened panel does not describe a setting nobody chose.
That only works if `mode` is true. It was not -- one-file windows reported
`forever`, because status inferred them from an `omdrop-once.path` unit that
nothing in the plugin has ever created. The watcher is what implements the
mode, so the watcher is what status has to ask.
"""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OMDROP = ROOT / "bin" / "omdrop"


class WindowModeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bin = Path(self.tmp.name) / "bin"
        self.bin.mkdir()
        self.env = os.environ.copy()
        self.env.update(PATH=f"{self.bin}:{self.env['PATH']}",
                        OMDROP_DISCOVERABLE="/nonexistent")

    def status(self, watch_exec, timer_active=False):
        """`omdrop status --json` against a stubbed systemd."""
        # The receiver is active (so there is a window to describe) and the
        # window timer is not, which is the state both non-timed modes share.
        script = f"""#!/bin/sh
case "$1 $2 $3" in
  "--user is-active airdrop-receiver.service") echo active; exit 0 ;;
esac
case "$*" in
  *"is-active omdrop-window.timer"*) exit {0 if timer_active else 3} ;;
  *"show omdrop-watch.service -p ExecStart"*) printf '%s\\n' {watch_exec!r}; exit 0 ;;
  *"show omdrop-window.timer"*) exit 1 ;;
  *is-active*) exit 3 ;;
esac
exit 0
"""
        path = self.bin / "systemctl"
        path.write_text(script)
        path.chmod(0o755)
        result = subprocess.run([OMDROP, "status", "--json"], env=self.env,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    ONCE = "{ path=/usr/bin/omdrop ; argv[]=/usr/bin/omdrop watch --once ; ignore_errors=no }"
    OPEN = "{ path=/usr/bin/omdrop ; argv[]=/usr/bin/omdrop watch ; ignore_errors=no }"

    def test_a_one_file_window_is_reported_as_one_file(self):
        # The regression: this answered "forever", so the panel's duration row
        # showed "Until I turn it off" for a window that stops after one file.
        self.assertEqual(self.status(self.ONCE)["mode"], "once")

    def test_an_unbounded_window_is_still_reported_as_unbounded(self):
        self.assertEqual(self.status(self.OPEN)["mode"], "forever")

    def test_a_timed_window_outranks_both(self):
        self.assertEqual(self.status(self.OPEN, timer_active=True)["mode"], "timed")


if __name__ == "__main__":
    unittest.main()
