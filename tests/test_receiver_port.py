"""Turning receiving off and on again right after a transfer must work.

2026-09-18 03:11Z: an iPhone's TLS session was still open when the receiver
stopped, so the kernel closed it with a FIN the departed phone never
acknowledged. That socket held port 8771 in FIN_WAIT_1, `SO_REUSEADDR` does not
cover that state, and the next start failed 23 times in a row -- the panel said
"The AirDrop receiver would not start" while nothing was listening on the port.
"""

import os
import socket
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SERVE = ROOT / "bin" / "airdrop-serve.py"


def port_is_bindable():
    """A port that is free on the IPv6 wildcard, where the receiver binds."""
    probe = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        probe.bind(("::", 0))
        return probe.getsockname()[1]
    finally:
        probe.close()


class SessionResetTests(unittest.TestCase):
    """The reset is what frees the port, so it has to reach live sessions."""

    def server(self):
        # Import the server class without running the script's argument
        # parsing: the class is the unit under test here.
        source = SERVE.read_text()
        start = source.index("class ThreadingHTTPServerV6")
        body = source[start:source.index("\nKIB = 1024")]
        namespace = {}
        exec("import logging, socket, struct, sys\n"
             "from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler\n"
             + body, namespace)
        return namespace["ThreadingHTTPServerV6"]

    def test_a_live_session_is_reset_rather_than_left_to_linger(self):
        from http.server import BaseHTTPRequestHandler
        cls = self.server()
        srv = cls(("::1", 0), BaseHTTPRequestHandler)
        try:
            port = srv.server_address[1]
            client = socket.create_connection(("::1", port))
            self.addCleanup(client.close)
            conn, _ = srv.socket.accept()
            srv.live.add(conn)

            srv.reset_sessions()

            self.assertEqual(srv.live, set())
            # A reset peer reads as an error or an immediate EOF, never as a
            # half-open connection we would have to wait out.
            client.settimeout(5)
            try:
                self.assertEqual(client.recv(1), b"")
            except ConnectionResetError:
                pass
        finally:
            srv.server_close()

    def test_a_finished_session_is_forgotten(self):
        # Every completed request would otherwise accumulate in the set for the
        # life of the process.
        from http.server import BaseHTTPRequestHandler
        cls = self.server()
        srv = cls(("::1", 0), BaseHTTPRequestHandler)
        try:
            left, right = socket.socketpair()
            self.addCleanup(right.close)
            srv.live.add(left)
            srv.shutdown_request(left)
            self.assertEqual(srv.live, set())
        finally:
            srv.server_close()


@unittest.skipUnless(Path("/sys/class/net/awdl0/address").exists(),
                     "needs the awdl0 interface this receiver serves on")
class BusyPortTests(unittest.TestCase):
    def test_the_receiver_waits_for_its_port_instead_of_moving_to_another(self):
        # Serving on port+1 is worse than not serving: the announcer only ever
        # names the port it was given, so every sender dials a closed door.
        port = port_is_bindable()
        holder = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        holder.bind(("::", port))
        holder.listen(1)

        # Its own identity, config and download folder: the defaults are the
        # user's real ones, and a receiver that finds no certificate there
        # makes one -- which once replaced a real Apple ID identity.
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        root = Path(scratch.name)
        (root / "xdg" / "airdrop").mkdir(parents=True)
        (root / "out").mkdir()
        proc = subprocess.Popen(
            [sys.executable, str(SERVE), "--iface", "awdl0", "--port", str(port),
             "--keys", str(root / "identity"), "--outdir", str(root / "out"),
             "--config", str(root / "xdg" / "airdrop" / "config.toml")],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            env=dict(os.environ, HOME=str(root), XDG_CONFIG_HOME=str(root / "xdg")))
        self.addCleanup(proc.kill)

        waited = self.read_until(proc, "still held", 20)
        self.assertIn("still held", waited, waited)
        self.assertNotIn(f"[::]:{port + 1}", waited, waited)

        holder.close()
        served = self.read_until(proc, "receiving into", 30)
        self.assertIn(f"]:{port},", served, served)
        self.assertIsNone(proc.poll(), served)

    def read_until(self, proc, needle, timeout):
        seen = ""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            seen += line
            if needle in line:
                return seen
        return seen

if __name__ == "__main__":
    unittest.main()
