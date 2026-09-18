"""`status` reads the window's mode off two units, so they must exist before
anything can be seen.

2026-09-18: choosing 10 minutes and turning Omdrop on moved the duration row to
"Until I turn it off" for the length of the radio gate, then back to 10 minutes
once the timer appeared. Nothing was wrong with the panel: `cmd_on` armed the
window last, and `cmd_status` infers `forever` from a missing timer.
"""

import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OMDROP = ROOT / "bin" / "omdrop"


class WindowArmingOrderTests(unittest.TestCase):
    def run_on(self, window="10m", radio=0):
        source = OMDROP.read_text()
        fragment = source[source.index("cmd_on() {"):source.index("\ncmd_off()")]
        with tempfile.TemporaryDirectory() as directory:
            helper = Path(directory) / "omdrop-discoverable"
            helper.write_text("#!/bin/sh\n")
            helper.chmod(0o755)
            capture = Path(directory) / "events"
            capture.write_text("")
            harness = f"""
set -uo pipefail
SELF=/bin/true
UNIT=airdrop-receiver.service
WATCH=omdrop-watch
WINDOW=omdrop-window
DEFAULT_WINDOW=10m
DISCOVERABLE={helper}
CAPTURE={capture}
log(){{ printf '%s\\n' "$*" >> "$CAPTURE"; }}
require_unit(){{ :; }}
span_seconds(){{ echo 600; }}
cancel_window(){{ log cancel_window; }}
systemd-run(){{ log "systemd-run $*"; }}
systemctl(){{ log "systemctl $*"; }}
pkexec(){{ log "pkexec $*"; return {radio}; }}
ip(){{ return 1; }}
die(){{ log "die $*"; exit 9; }}
cmd_status(){{ log status; }}
{fragment}
cmd_on {window}
"""
            result = subprocess.run(["bash", "-c", harness],
                                    capture_output=True, text=True)
            return result, capture.read_text()

    def test_the_timer_is_armed_before_anyone_can_see_us(self):
        result, events = self.run_on()
        self.assertIn("on-active=10m", events, events + result.stderr)
        self.assertLess(events.index("on-active=10m"), events.index("pkexec"),
                        events)
        self.assertLess(events.index("on-active=10m"),
                        events.index("--user start"), events)

    def test_a_one_file_window_declares_itself_before_the_radio(self):
        # The same gap made a one-file start read as unbounded: `status` knows
        # "once" only from the watcher's --once argument.
        result, events = self.run_on(window="once")
        self.assertIn("watch --once", events, events + result.stderr)
        self.assertLess(events.index("watch --once"), events.index("pkexec"),
                        events)
        self.assertNotIn("on-active", events)

    def test_an_unbounded_window_arms_no_timer(self):
        _, events = self.run_on(window="forever")
        self.assertNotIn("on-active", events)
        self.assertIn("watch", events)
        self.assertNotIn("watch --once", events)

    def test_a_radio_that_will_not_start_leaves_no_window_behind(self):
        # An armed timer with nothing running would turn off a machine that was
        # never on, minutes after the user was told the start failed.
        result, events = self.run_on(radio=1)
        self.assertEqual(result.returncode, 9, events + result.stderr)
        self.assertEqual(events.count("cancel_window"), 2, events)
        self.assertLess(events.rindex("cancel_window"), events.index("die"),
                        events)


if __name__ == "__main__":
    unittest.main()
