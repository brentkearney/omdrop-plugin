import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OMDROP = ROOT / "bin" / "omdrop"

# What send-to-peer --list reports: address, signal, endpoint.
ONE_PEER = "e2:9d:03:6c:58:23   -47 dBm  [fe80::e09d:3ff:fe6c:5823%awdl0]:8770"
THREE_PEERS = "\n".join(
    [
        "fe:63:e6:68:9f:12   -14 dBm  [fe80::fc63:e6ff:fe68:9f12%awdl0]:8770",
        "16:2f:da:27:3d:76   -33 dBm  [fe80::142f:daff:fe27:3d76%awdl0]:8770",
        ONE_PEER,
    ]
)

# What airdrop-send.py logs for a transfer the device accepted and stored.
SENT = (
    "2026-09-18 12:00:00,000 INFO send: +  0.41s FOUND 0e9d036c5823 'hume' "
    "at [fe80::e09d:3ff:fe6c:5823%awdl0]:8770; asking\n"
    "2026-09-18 12:00:02,000 INFO send: +  2.10s ASK -> accepted (1.69s)\n"
    "2026-09-18 12:00:02,600 INFO send: +  2.72s UPLOAD photo.jpg -> ok "
    "(2493 B, 0.62s)"
)
DECLINED = (
    "2026-09-18 12:00:00,000 INFO send: +  0.41s FOUND 0e9d036c5823 'hume' "
    "at [fe80::e09d:3ff:fe6c:5823%awdl0]:8770; asking\n"
    "2026-09-18 12:00:04,000 INFO send: +  4.02s ASK -> declined (3.61s)"
)


class SenderFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.bin = root / "bin"
        self.bin.mkdir()
        self.capture = root / "capture"
        self.capture_list = root / "capture-list"
        self.file = root / "photo.jpg"
        self.file.write_bytes(b"\xff\xd8\xff" + b"0" * 64)

        config = root / "config" / "airdrop"
        config.mkdir(parents=True)
        (config / "config.toml").write_text('name = "Study Mac"\n')

        # Stands in for send-to-peer: records how it was called, answers
        # --list from PEER_LIST, and replays a sender log for a transfer.
        self.sender = self.command(
            "send-to-peer",
            """#!/bin/sh
[ "$1" = --help ] && { echo "  -n, --names"; exit 0; }
out="$CAPTURE"
[ "$1" = --list ] && out="$CAPTURE_LIST"
: > "$out"
for arg do printf '%s\\n' "$arg" >> "$out"; done
if [ "$1" = --list ]; then
  [ -n "$LIST_RC" ] && { echo "could not read the peer table" >&2; exit "$LIST_RC"; }
  [ -n "$PEER_LIST" ] || exit 1
  printf '%s\\n' "$PEER_LIST"
  exit 0
fi
[ -n "$SENDER_STDOUT" ] && printf '%s\\n' "$SENDER_STDOUT"
exit "${SENDER_RC:-0}"
""",
        )
        # The radio's address is the precondition for any transfer, so the
        # tests own what `ip` reports about it.
        self.command(
            "ip",
            """#!/bin/sh
[ "${RADIO:-up}" = up ] || exit 0
echo "    inet6 fe80::1c9a:4bff:fe33:1/64 scope link"
""",
        )
        # Everything except the "is opendrop installed" probe is real python:
        # the name the device is told comes from config.toml, parsed by it.
        self.command(
            "python3",
            f"""#!/bin/sh
case "$*" in
  *"import opendrop"*) exit "${{OPENDROP_RC:-0}}" ;;
esac
exec {sys.executable} "$@"
""",
        )

        self.env = os.environ.copy()
        self.env.update(
            PATH=f"{self.bin}:{self.env['PATH']}",
            CAPTURE=str(self.capture),
            CAPTURE_LIST=str(self.capture_list),
            XDG_CONFIG_HOME=str(root / "config"),
            OMDROP_SENDER=str(self.sender),
            PEER_LIST=ONE_PEER,
            SENDER_STDOUT=SENT,
            LIST_RC="",
            SENDER_RC="",
        )

    def command(self, name, body):
        path = self.bin / name
        path.write_text(body)
        path.chmod(0o755)
        return path

    def run_omdrop(self, *args):
        return subprocess.run(
            [OMDROP, *args], env=self.env, capture_output=True, text=True
        )

    def sender_args(self):
        return self.capture.read_text().splitlines()


class SendCommandTests(SenderFixture):
    def test_send_names_this_computer_the_way_receiving_does(self):
        result = self.run_omdrop("send", str(self.file))

        self.assertEqual(result.returncode, 0, result.stderr)
        args = self.sender_args()
        self.assertEqual(args[-1], str(self.file))
        self.assertEqual(args[args.index("--name") + 1], "Study Mac")

    def test_send_waits_for_a_sleeping_receiver_by_default(self):
        self.run_omdrop("send", str(self.file))

        args = self.sender_args()
        self.assertEqual(args[args.index("--wait") + 1], "30")

    def test_the_only_device_heard_needs_no_address(self):
        self.run_omdrop("send", str(self.file))

        args = self.sender_args()
        self.assertEqual(args[args.index("--mac") + 1], "e2:9d:03:6c:58:23")

    def test_part_of_an_address_is_enough_to_choose_a_device(self):
        self.env["PEER_LIST"] = THREE_PEERS

        result = self.run_omdrop("send", "--to", "6C:58", str(self.file))

        self.assertEqual(result.returncode, 0, result.stderr)
        args = self.sender_args()
        self.assertEqual(args[args.index("--mac") + 1], "e2:9d:03:6c:58:23")

    def test_several_devices_heard_are_offered_in_this_commands_words(self):
        self.env["PEER_LIST"] = THREE_PEERS

        result = self.run_omdrop("send", str(self.file))

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("omdrop send --to fe:63:e6:68:9f:12", result.stderr)
        # The sender's own word for --to is --mac; naming it here would point
        # at an option this command does not have.
        self.assertNotIn("--mac", result.stderr)
        self.assertFalse(self.capture.exists())

    def test_an_address_matching_several_devices_is_refused(self):
        self.env["PEER_LIST"] = THREE_PEERS

        result = self.run_omdrop("send", "--to", ":", str(self.file))

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.capture.exists())

    def test_an_address_nobody_answers_to_is_refused(self):
        result = self.run_omdrop("send", "--to", "aa:bb:cc", str(self.file))

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("omdrop peers", result.stderr)
        self.assertFalse(self.capture.exists())

    def test_an_empty_room_says_a_window_is_what_hears_devices(self):
        self.env["PEER_LIST"] = ""

        result = self.run_omdrop("send", str(self.file))

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("omdrop on", result.stderr)
        self.assertFalse(self.capture.exists())

    def test_a_peer_table_that_cannot_be_read_is_not_an_empty_room(self):
        self.env["LIST_RC"] = "4"

        result = self.run_omdrop("send", str(self.file))

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("could not read the peer table", result.stderr)
        self.assertNotIn("within earshot", result.stderr)

    def test_send_refuses_a_folder_without_sending(self):
        result = self.run_omdrop("send", str(self.file.parent))

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.capture.exists())

    def test_send_says_what_to_do_when_the_radio_is_down(self):
        self.env["RADIO"] = "down"

        result = self.run_omdrop("send", str(self.file))

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("omdrop on", result.stderr)
        self.assertFalse(self.capture.exists())

    def test_a_delivered_file_is_reported_without_the_protocol_log(self):
        result = self.run_omdrop("send", str(self.file))

        self.assertIn("Sent photo.jpg (2493 B, 0.62s)", result.stdout)
        self.assertNotIn("FOUND", result.stdout)
        self.assertNotIn("INFO send", result.stdout)

    def test_a_refused_transfer_fails_and_says_so(self):
        self.env["SENDER_STDOUT"] = DECLINED
        self.env["SENDER_RC"] = "3"

        result = self.run_omdrop("send", str(self.file))

        self.assertEqual(result.returncode, 3)
        self.assertIn("declined", result.stderr)

    def test_verbose_keeps_the_log_a_bug_report_needs(self):
        result = self.run_omdrop("send", "--verbose", str(self.file))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ASK -> accepted", result.stdout)

    def test_peers_lists_what_the_radio_can_hear(self):
        result = self.run_omdrop("peers")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.capture_list.read_text().splitlines(), ["--list"])
        self.assertIn("-47 dBm", result.stdout)

    def test_sending_without_the_driver_half_names_the_installer(self):
        self.env["OMDROP_SENDER"] = str(self.bin / "nothing-here")

        result = self.run_omdrop("send", str(self.file))

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("omdrop install-driver", result.stderr)

    def test_peers_json_gives_the_panel_names_only_where_a_device_answered(self):
        self.env["PEER_LIST"] = "\n".join([
            "22:8b:38:31:89:4e   -34 dBm  [fe80::208b:38ff:fe31:894e%awdl0]:8770  hume",
            "b2:c4:98:5e:e6:be     ? dBm  [fe80::b0c4:98ff:fe5e:e6be%awdl0]:8770  (no response)",
            "f2:f2:ee:c4:5a:f7     ? dBm  [fe80::f0f2:eeff:fec4:5af7%awdl0]:8770  Brent's iPhone",
        ])

        result = self.run_omdrop("peers", "--json", "-n")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), [
            {"mac": "22:8b:38:31:89:4e", "rssi": -34, "name": "hume"},
            {"mac": "b2:c4:98:5e:e6:be", "rssi": None, "name": None},
            {"mac": "f2:f2:ee:c4:5a:f7", "rssi": None, "name": "Brent's iPhone"},
        ])

    def test_peers_json_draws_an_empty_room_as_an_empty_list(self):
        self.env["PEER_LIST"] = ""

        result = self.run_omdrop("peers", "--json")

        self.assertEqual(json.loads(result.stdout), [])


class SendPickTests(SenderFixture):
    """`send --pick`: where the chooser opens, and what it remembers."""

    def setUp(self):
        super().setUp()
        root = Path(self.tmp.name)
        self.downloads = root / "Downloads"
        self.downloads.mkdir()
        (root / "config" / "airdrop" / "config.toml").write_text(
            f'name = "Study Mac"\ndownload_dir = "{self.downloads}"\n')
        self.opened_at = root / "opened-at"
        # Stands in for the chooser: notes where it was asked to open, and
        # answers with PICK_ANSWER, or cancels when that is empty.
        picker = self.command("picker", """#!/bin/sh
printf '%s\\n' "$2" > "$OPENED_AT"
[ -n "$PICK_ANSWER" ] || exit 1
printf '%s\\n' "$PICK_ANSWER"
""")
        self.env.update(OMDROP_PICKER=str(picker), OPENED_AT=str(self.opened_at))

    def pick_and_send(self, answer):
        self.env["PICK_ANSWER"] = str(answer)
        return self.run_omdrop("send", "--pick", "--to", "6c:58")

    def test_the_chooser_opens_where_the_last_file_was_sent_from(self):
        photos = Path(self.tmp.name) / "Photos"
        photos.mkdir()
        (photos / "cat.jpg").write_bytes(b"x")

        first = self.pick_and_send(photos / "cat.jpg")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(self.opened_at.read_text().strip(), str(self.downloads))
        self.assertEqual(self.capture.read_text().splitlines()[-1], str(photos / "cat.jpg"))

        self.pick_and_send(photos / "cat.jpg")
        self.assertEqual(self.opened_at.read_text().strip(), str(photos))

        # A remembered folder that has gone is forgotten, not opened.
        (photos / "cat.jpg").unlink()
        photos.rmdir()
        self.pick_and_send(self.file)
        self.assertEqual(self.opened_at.read_text().strip(), str(self.downloads))

    def test_a_cancelled_chooser_sends_nothing_and_says_nothing(self):
        self.env["PICK_ANSWER"] = ""

        result = self.run_omdrop("send", "--pick", "--to", "6c:58")

        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr, "")
        self.assertFalse(self.capture.exists())


class SoundSettingTests(SenderFixture):
    def test_muting_the_radar_is_remembered(self):
        self.run_omdrop("sound", "off")

        self.assertEqual(self.run_omdrop("sound").stdout.strip(), "off")


if __name__ == "__main__":
    unittest.main()
