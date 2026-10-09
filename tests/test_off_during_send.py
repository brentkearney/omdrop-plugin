"""Turning Omdrop off mid-send cuts the transfer, and the other end never hears
it end. 2026-10-09: a Mac kept the 0-byte file "in use" and stopped accepting
connections from this machine. `off` asked by a person now refuses while a send
is running; the timers that close a window still force it.
"""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OMDROP = ROOT / "bin" / "omdrop"


class OffDuringSendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.sends = self.root / "omdrop" / "sends"
        self.sends.mkdir(parents=True)
        self.capture = self.root / "events"

    def live_sender(self):
        proc = subprocess.Popen(["sleep", "30"])
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        return proc.pid

    def run_off(self, *args):
        source = OMDROP.read_text()
        start = source.index("SENDS_DIR=")
        end = source.index("\ncmd_toggle() {", start)
        harness = f"""
set -uo pipefail
XDG_RUNTIME_DIR={self.root}
CAPTURE={self.capture}
UNIT=airdrop-receiver.service
DISCOVERABLE=/nonexistent
ID_PY=true
die() {{ printf '%s\\n' "$*" >&2; exit 1; }}
require_unit() {{ :; }}
identity_lock() {{ :; }}
identity_unlock() {{ :; }}
cancel_window() {{ :; }}
cmd_status() {{ :; }}
systemctl() {{ printf 'systemctl %s\\n' "$*" >> "$CAPTURE"; }}
{source[start:end]}
cmd_off {" ".join(args)}
"""
        result = subprocess.run(["bash", "-c", harness], capture_output=True, text=True)
        events = self.capture.read_text() if self.capture.exists() else ""
        return result, events

    def test_off_refuses_while_a_send_is_running(self):
        (self.sends / str(self.live_sender())).write_text("hume\n")

        result, events = self.run_off()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Still sending to hume", result.stderr)
        self.assertIn("omdrop off --force", result.stderr)
        self.assertNotIn("stop", events)

    def test_force_turns_off_even_while_sending(self):
        (self.sends / str(self.live_sender())).write_text("hume\n")

        result, events = self.run_off("--force")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("systemctl --user stop airdrop-receiver.service", events)

    def test_a_send_that_died_does_not_block_off(self):
        dead = subprocess.Popen(["true"])
        dead.wait()
        marker = self.sends / str(dead.pid)
        marker.write_text("hume\n")

        result, events = self.run_off()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("stop airdrop-receiver.service", events)
        self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
