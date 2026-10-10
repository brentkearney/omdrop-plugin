"""The real kernel-keyring reader fails cleanly instead of crashing.

keyctl_read_alloc returns int. Declared as long, its -1 came back as
4294967295 on aarch64, passed the error check, and string_at(NULL, 4 GiB)
killed the process: `omdrop identity status` segfaulted in any session
that could see the cached key but not read it. Run in a subprocess, so a
crash fails the test instead of killing the suite.
"""
import ctypes.util
import subprocess
import sys
import unittest
from pathlib import Path

BIN = Path(__file__).resolve().parent.parent / 'bin'


@unittest.skipUnless(ctypes.util.find_library('keyutils'), 'needs libkeyutils')
class RealKeyringTests(unittest.TestCase):
    def test_reading_an_unreadable_key_does_not_crash(self):
        # Serial 1 is never a key this user can read: ENOKEY or EACCES.
        code = ('import identity\n'
                'try:\n'
                '    print(repr(identity.Keyring().read(1)))\n'
                'except identity.KeyringError as e:\n'
                '    print("KeyringError")\n')
        result = subprocess.run([sys.executable, '-c', code], cwd=BIN,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, f'crashed: rc={result.returncode} {result.stderr}')
        self.assertIn(result.stdout.strip(), ('None', 'KeyringError'))


if __name__ == '__main__':
    unittest.main()
