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
# cmd_on brings the radio up through the spinner, which lives outside this
# fragment. Run the command and drop the animation: what this file tests is
# the ORDER the window is armed in, and a spinner would only add frames.
spinner_while(){{ shift; "$@"; }}
cmd_status(){{ log status; }}
{fragment}
cmd_on {window}
"""
            result = subprocess.run(["bash", "-c", harness],
                                    capture_output=True, text=True)
            return result, capture.read_text()

    def test_the_window_is_named_before_the_radio_comes_up(self):
        # `status` has to be able to say "10 minutes" while the gate runs, or
        # the panel's duration row reads as unbounded and moves itself.
        result, events = self.run_on()
        self.assertIn("watch --window 10m", events, events + result.stderr)
        self.assertLess(events.index("watch --window"), events.index("pkexec"),
                        events)

    def test_the_clock_starts_only_once_receiving_works(self):
        # A deadline armed before the gate spends up to 30 s counting down a
        # window nobody can send to: "1 minute" arrived with 35 s left.
        result, events = self.run_on()
        self.assertIn("on-active=10m", events, events + result.stderr)
        self.assertGreater(events.index("on-active=10m"),
                           events.index("pkexec"), events)
        self.assertGreater(events.index("on-active=10m"),
                           events.index("--user start"), events)

    def test_the_radio_is_asked_for_the_window_again_from_that_moment(self):
        # The radio's own deadline began when the gate did, so without this it
        # goes dark a gate's worth of seconds before the panel's clock ends.
        _, events = self.run_on()
        starts = [line for line in events.splitlines() if "start 600" in line]
        self.assertEqual(len(starts), 2, events)
        self.assertGreater(events.rindex("start 600"),
                           events.index("on-active=10m"), events)

    def test_a_one_file_window_declares_itself_before_the_radio(self):
        result, events = self.run_on(window="once")
        self.assertIn("watch --once", events, events + result.stderr)
        self.assertLess(events.index("watch --once"), events.index("pkexec"),
                        events)
        self.assertNotIn("on-active", events)

    def test_an_unbounded_window_arms_no_timer_and_names_no_span(self):
        _, events = self.run_on(window="forever")
        self.assertNotIn("on-active", events)
        self.assertIn("watch", events)
        self.assertNotIn("--once", events)
        self.assertNotIn("--window", events)

    def test_a_radio_that_will_not_start_leaves_no_window_behind(self):
        # A watcher left running with nothing behind it would report a live
        # window, and a one-file watcher would turn off a machine later.
        result, events = self.run_on(radio=1)
        self.assertEqual(result.returncode, 9, events + result.stderr)
        self.assertEqual(events.count("cancel_window"), 2, events)
        self.assertLess(events.rindex("cancel_window"), events.index("die"),
                        events)
        self.assertNotIn("on-active", events)


if __name__ == "__main__":
    unittest.main()
