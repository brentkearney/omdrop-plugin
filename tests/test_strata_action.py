import importlib.util
import json
import os
import subprocess
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("omdrop_action", Path(__file__).resolve().parents[1] / "share/strata/omdrop/run.py")
action = importlib.util.module_from_spec(spec)
spec.loader.exec_module(action)


class ActionTests(unittest.TestCase):
    def test_a_changed_address_never_starts_a_send(self):
        with patch.object(action, "selected_files", return_value=["/tmp/test.jpg"]), \
             patch.object(action, "omdrop_program", return_value="omdrop"), \
             patch.object(action, "discover", return_value=[{"mac": "02:00:00:00:00:01"}]), \
             patch.object(action, "choose_peer", return_value="02:00:00:00:00:01"), \
             patch.object(action.subprocess, "run", return_value=subprocess.CompletedProcess(
                 [], 0, '[{"mac":"02:00:00:00:00:02"}]')) as run:
            self.assertEqual(action.main(), 1)
            run.assert_called_once_with(["omdrop", "peers", "--json"],
                                        capture_output=True, text=True)

    def test_an_empty_peer_list_explains_how_to_retry(self):
        replies = [subprocess.CompletedProcess([], 0, '{"visible":true}'),
                   subprocess.CompletedProcess([], 0, '[]')]
        with patch.object(action.subprocess, "run", side_effect=replies):
            with self.assertRaisesRegex(ValueError, "Open AirDrop"):
                action.discover("omdrop")

    def test_selection_reaches_sender_as_exact_arguments(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = [root / name for name in ("a photo.jpg", "-option;$(touch BAD)\n.mp3")]
            for path in paths:
                path.write_bytes(b"test")
            inputs = root / "paths"
            inputs.write_bytes(b"".join(os.fsencode(path) + b"\0" for path in paths))
            log = root / "calls"
            program = root / "omdrop"
            program.write_text('''#!/usr/bin/python3
import json, os, sys
with open(os.environ['TEST_CALLS'], 'a') as output:
    output.write(json.dumps(sys.argv[1:]) + '\\n')
if sys.argv[1] == 'status':
    print('{"visible": false}')
elif sys.argv[1] == 'peers':
    print('[{"mac":"02:00:00:00:00:01","name":"Test iPhone"}]')
elif sys.argv[1] == 'send':
    sys.exit(7)
''')
            program.chmod(0o700)
            with patch.dict(os.environ, STRATA_ACTION_PATHS=str(inputs), TEST_CALLS=str(log)), \
                 patch.object(action, "omdrop_program", return_value=str(program)), \
                 patch.object(action, "choose_peer", return_value="02:00:00:00:00:01"):
                self.assertEqual(action.main(), 7)
            calls = [json.loads(line) for line in log.read_text().splitlines()]
            self.assertEqual(calls, [["status", "--json"], ["on", "10m"],
                                    ["peers", "-n", "--json"], ["peers", "--json"],
                                    ["send", "--to", "02:00:00:00:00:01", "--", *map(str, paths)]])
            self.assertFalse((root / "BAD").exists())

    def test_bad_selection_never_starts_omdrop(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            one = root / "one"
            two = root / "two"
            one.mkdir()
            two.mkdir()
            for folder in (one, two):
                (folder / "same.jpg").write_bytes(b"test")
            inputs = root / "paths"
            for paths in ([root], [root / "missing"], [one / "same.jpg", two / "same.jpg"]):
                inputs.write_bytes(b"".join(os.fsencode(path) + b"\0" for path in paths))
                with self.subTest(paths=paths), \
                     patch.dict(os.environ, STRATA_ACTION_PATHS=str(inputs)), \
                     patch.object(action, "omdrop_program") as program:
                    self.assertEqual(action.main(), 1)
                    program.assert_not_called()

    def test_cancel_never_sends(self):
        with patch.object(action, "selected_files", return_value=["/tmp/test.jpg"]), \
             patch.object(action, "omdrop_program", return_value="omdrop"), \
             patch.object(action, "discover", return_value=[{"mac": "02:00:00:00:00:01"}]), \
             patch.object(action, "choose_peer", return_value=None), \
             patch.object(action.subprocess, "run") as run:
            self.assertEqual(action.main(), 1)
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
