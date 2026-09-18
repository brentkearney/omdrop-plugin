"""A one-file window has to actually close after its one file.

2026-09-17, twice: the watcher announced the file it had stored and stopped,
and the receiver stayed listening -- so the next sender got in too. `off`
calls `cancel_window`, which stops `omdrop-watch.service`; the watcher was
running INSIDE that service, so systemd killed its cgroup partway through the
shutdown it had just started. The close has to outlive the watcher.
"""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OMDROP = ROOT / "bin" / "omdrop"


class OneFileShutdownTests(unittest.TestCase):
    def run_watch(self, once=True):
        source = OMDROP.read_text()
        start = source.index("cmd_watch() {")
        fragment = source[start:source.index("\n# The URL inside", start)]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "arrived.jpg").write_text("x")
            capture = root / "events"
            stored = f"upload ABC: stored {root}/arrived.jpg"
            harness = f"""
set -uo pipefail
SELF={root}/omdrop
UNIT=airdrop-receiver.service
CAPTURE={capture}
effective_dir(){{ printf '%s' {str(root)!r}; }}
journalctl(){{ printf '%s\\n' {stored!r}; }}
systemd-run(){{ printf 'systemd-run %s\\n' "$*" >> "$CAPTURE"; }}
{fragment}
cmd_watch {"--once" if once else ""}
"""
            # $SELF is invoked directly for `handle`, and must be a real file.
            self_stub = root / "omdrop"
            self_stub.write_text(
                '#!/bin/sh\nprintf \'self %s\\n\' "$*" >> "$CAPTURE"\n')
            self_stub.chmod(0o755)
            env = os.environ.copy()
            env["CAPTURE"] = str(capture)
            result = subprocess.run(["bash", "-c", harness], env=env,
                                    capture_output=True, text=True)
            events = capture.read_text() if capture.exists() else ""
            return result, events

    def test_the_close_outlives_the_watcher_that_asked_for_it(self):
        # The regression: `off` ran as a child of the unit `off` stops, so it
        # died with it and the receiver was never stopped.
        result, events = self.run_watch(once=True)
        self.assertIn("off", events, result.stdout + result.stderr)
        self.assertRegex(events, r"systemd-run .*\boff\b")

    def test_an_ordinary_window_is_not_closed_by_an_arrival(self):
        _, events = self.run_watch(once=False)
        self.assertNotIn("off", events)


if __name__ == "__main__":
    unittest.main()
