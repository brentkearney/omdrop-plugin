"""The receiver answers AirDrop the way Apple senders need, end to end.

Each test runs the real receiver on the loopback interface with its own
identity, download folder and settings, and talks to it over TLS the way
sharingd does: chunked /Discover and /Ask bodies, then /Ask and a DVZip
/Upload on one kept-alive connection.
"""

import http.client
import hashlib
import json
import os
import plistlib
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import time
import unittest
import zlib
from pathlib import Path

import libarchive


ROOT = Path(__file__).resolve().parents[1]
SERVE = ROOT / "bin" / "airdrop-serve.py"
NAME = "Test Receiver"
MODEL = "MacBookPro18,3"


def loopback_has_ipv6():
    try:
        return any(line.split()[-1] == "lo" for line in open("/proc/net/if_inet6"))
    except OSError:
        return False


def free_port():
    probe = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    try:
        probe.bind(("::", 0))
        return probe.getsockname()[1]
    finally:
        probe.close()


def self_signed(directory, cn):
    subprocess.run(["openssl", "req", "-newkey", "rsa:2048", "-nodes", "-keyout", "key.pem",
                    "-x509", "-days", "1", "-out", "certificate.pem", "-subj", f"/CN={cn}"],
                   cwd=directory, check=True, capture_output=True)
    return directory / "certificate.pem", directory / "key.pem"


def der(pem_path):
    return ssl.PEM_cert_to_DER_cert(Path(pem_path).read_text())


def dvzip(files):
    """An odc CPIO archive in one zlib-compressed DVZip block, as macOS sends."""
    with tempfile.TemporaryDirectory() as work:
        path = Path(work) / "payload.cpio"
        with libarchive.file_writer(str(path), "cpio") as archive:
            for name, data in files:
                archive.add_file_from_memory(f"./{name}", len(data), data)
        block = zlib.compress(path.read_bytes())
    return len(block).to_bytes(4, "big") + block


@unittest.skipUnless(loopback_has_ipv6() and shutil.which("openssl"),
                     "needs IPv6 on lo and openssl")
class ReceiverFixture(unittest.TestCase):
    visibility = "everyone"
    seed_identity = True

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.identity = root / "opendrop"
        cls.keys = cls.identity / "keys"
        cls.out = root / "out"
        cls.out.mkdir()
        xdg = root / "config"
        (xdg / "omdrop").mkdir(parents=True)
        (xdg / "omdrop" / "visibility").write_text(cls.visibility + "\n")
        if cls.seed_identity:
            cls.keys.mkdir(parents=True)
            self_signed(cls.keys, "Existing Identity")
            cls.record = os.urandom(3718)
            (cls.keys / "validation_record.cms").write_bytes(cls.record)
            cls.seeded = {p.name: p.read_bytes() for p in cls.keys.iterdir()}
        cls.client_dir = root / "client"
        cls.client_dir.mkdir()
        cls.client_cert = self_signed(cls.client_dir, "airdrop")

        cls.port = free_port()
        cls.log = root / "receiver.log"
        # Its own runtime directory too: a live window's identity file on this
        # machine must not decide what the test receiver presents.
        runtime = root / "run"
        runtime.mkdir(mode=0o700)
        env = dict(os.environ, XDG_CONFIG_HOME=str(xdg), XDG_RUNTIME_DIR=str(runtime))
        with open(cls.log, "w") as log:
            cls.proc = subprocess.Popen(
                [sys.executable, str(SERVE), "--iface", "lo", "--port", str(cls.port),
                 "--name", NAME, "--model", MODEL, "--keys", str(cls.identity),
                 "--outdir", str(cls.out), "--config", str(xdg / "airdrop" / "config.toml")],
                stdout=log, stderr=subprocess.STDOUT, env=env)
        deadline = time.monotonic() + 30
        while "receiving into" not in cls.log.read_text():
            if cls.proc.poll() is not None or time.monotonic() > deadline:
                cls.tearDownClass()
                raise AssertionError("receiver did not start:\n" + cls.log.read_text())
            time.sleep(0.05)

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        try:
            cls.proc.wait(10)
        except subprocess.TimeoutExpired:
            cls.proc.kill()
            cls.proc.wait()
        cls.tmp.cleanup()

    def context(self, client_cert=False, tls12=False):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        if client_cert:
            ctx.load_cert_chain(*self.client_cert)
        if tls12:
            # The server's certificate request and its verdict on ours both
            # land inside the handshake, where the test can see them.
            ctx.maximum_version = ssl.TLSVersion.TLSv1_2
        return ctx

    def connection(self, **kw):
        conn = http.client.HTTPSConnection("::1", self.port, timeout=10,
                                           context=self.context(**kw))
        self.addCleanup(conn.close)
        return conn

    def post(self, conn, path, body, headers=None):
        # An iterable body with no length is what makes http.client chunk it,
        # which is how sharingd frames /Discover and /Ask.
        conn.request("POST", path, body=iter([body]), headers=headers or {})
        response = conn.getresponse()
        return response.status, response.read()


class ExistingIdentityTests(ReceiverFixture):
    def test_the_existing_identity_is_served_and_left_untouched(self):
        conn = self.connection()
        conn.connect()
        self.assertEqual(conn.sock.getpeercert(binary_form=True),
                         der(self.keys / "certificate.pem"))
        self.assertEqual({p.name: p.read_bytes() for p in self.keys.iterdir()}, self.seeded)

    def test_discover_names_this_machine_and_carries_its_validation_record(self):
        status, body = self.post(self.connection(), "/Discover",
                                 plistlib.dumps({"SenderRecordData": b"x"}, fmt=plistlib.FMT_BINARY))
        self.assertEqual(status, 200)
        answer = plistlib.loads(body)
        self.assertEqual(answer["ReceiverComputerName"], NAME)
        self.assertEqual(answer["ReceiverModelName"], MODEL)
        # Without the record a Contacts Only sender cannot recognise us.
        self.assertEqual(answer["ReceiverRecordData"], self.record)
        self.assertEqual(json.loads(answer["ReceiverMediaCapabilities"]), {"Version": 1})

    def test_ask_then_upload_on_one_connection_stores_the_files(self):
        conn = self.connection()
        ask = plistlib.dumps({
            "SenderComputerName": "Mac", "SenderModelName": "MacBookPro18,3",
            "TransferType": {"files": 1}, "Items": [],
            "Files": [{"FileName": "hello.txt", "FileType": "public.plain-text", "FileSize": 12}],
        }, fmt=plistlib.FMT_BINARY)
        status, body = self.post(conn, "/Ask", ask)
        self.assertEqual(status, 200)
        self.assertEqual(set(plistlib.loads(body)),
                         {"ReceiverComputerName", "ReceiverModelName", "ReceiverMediaCapabilities"})

        status, _ = self.post(conn, "/Upload", dvzip([("hello.txt", b"hello world\n")]),
                              {"Content-Type": "application/x-dvzip", "TransferID": "T1",
                               "Expect": "100-continue"})
        self.assertEqual(status, 200)
        self.assertEqual((self.out / "hello.txt").read_bytes(), b"hello world\n")

    def test_everyone_mode_serves_a_sender_presenting_a_self_signed_certificate(self):
        # Asking for a certificate would verify it, and a sender whose
        # certificate does not chain to Apple would be refused at TLS.
        status, _ = self.post(self.connection(client_cert=True, tls12=True), "/Discover",
                              plistlib.dumps({}, fmt=plistlib.FMT_BINARY))
        self.assertEqual(status, 200)


class FirstStartTests(ReceiverFixture):
    seed_identity = False

    def test_a_first_start_creates_a_private_self_signed_pair_named_after_the_receiver(self):
        self.assertEqual(self.keys.stat().st_mode & 0o777, 0o700)
        # Only the self-signed names: the Apple identity's names are never written.
        self.assertEqual(sorted(p.name for p in self.keys.iterdir() if not p.name.startswith(".")),
                         ["certificate.self-signed.pem", "key.self-signed.pem"])
        conn = self.connection()
        conn.connect()
        served = conn.sock.getpeercert(binary_form=True)
        self.assertEqual(served, der(self.keys / "certificate.self-signed.pem"))
        subject = subprocess.run(["openssl", "x509", "-inform", "DER", "-noout", "-subject"],
                                 input=served, capture_output=True, check=True).stdout
        self.assertIn(f"CN={NAME}".encode(), subject.replace(b" = ", b"="))

    def test_discover_omits_a_validation_record_this_machine_does_not_have(self):
        status, body = self.post(self.connection(), "/Discover",
                                 plistlib.dumps({}, fmt=plistlib.FMT_BINARY))
        self.assertEqual(status, 200)
        self.assertNotIn("ReceiverRecordData", plistlib.loads(body))


class ContactsOnlyTests(ReceiverFixture):
    visibility = "contacts"

    def test_the_receiver_asks_for_a_client_certificate_and_verifies_it(self):
        # The certificate is what binds a sender's validation record to the
        # connection; a receiver that never asks refuses every contact.
        with self.assertRaises(ssl.SSLError):
            self.connection(client_cert=True, tls12=True).connect()

    def test_a_sender_without_a_certificate_still_completes_the_handshake(self):
        status, _ = self.post(self.connection(tls12=True), "/Discover",
                              plistlib.dumps({}, fmt=plistlib.FMT_BINARY))
        self.assertEqual(status, 200)


class StalledHandshakeTests(ReceiverFixture):
    def test_a_connection_that_never_finishes_its_handshake_does_not_block_the_next_sender(self):
        # A sender that drops off the AWDL link mid-handshake leaves a TCP
        # connection that never sends another byte. Handshaking on the accept
        # thread with no timeout stopped every later connection from being
        # accepted, so the receiver went silent for the rest of its window.
        silent = socket.create_connection(("::1", self.port))
        self.addCleanup(silent.close)
        status, _ = self.post(self.connection(), "/Discover",
                              plistlib.dumps({}, fmt=plistlib.FMT_BINARY))
        self.assertEqual(status, 200)


class InterruptedUploadTests(ReceiverFixture):
    def reset_request(self, path, tid, framing, partial):
        start = len(self.log.read_text())
        conn = self.connection()
        conn.connect()
        conn.sock.sendall(
            (f"POST {path} HTTP/1.1\r\nHost: localhost\r\n"
             f"Content-Type: application/x-dvzip\r\n"
             f"TransferID: {tid}\r\nExpect: 100-continue\r\n"
             f"{framing}\r\n\r\n").encode())
        response = conn.sock.makefile("rb")
        self.assertEqual(response.readline(), b"HTTP/1.1 100 Continue\r\n")
        self.assertEqual(response.readline(), b"\r\n")
        response.close()
        conn.sock.sendall(partial)
        # A sender leaving AWDL can reset the connection between chunks.
        # Give the receiver time to consume the preceding complete chunk.
        time.sleep(0.1)
        conn.sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                             struct.pack("ii", 1, 0))
        conn.close()
        deadline = time.monotonic() + 5
        while True:
            log = self.log.read_text()[start:]
            if "storage failed" in log or "disconnected" in log:
                break
            self.assertLess(time.monotonic(), deadline, log)
            time.sleep(0.02)
        self.assertNotIn("storage failed", log)
        self.assertNotIn('"POST /Upload HTTP/1.1" 507', log)
        self.assertNotIn("Traceback", log)
        self.assertIn(f"{path} peer disconnected:", log)
        return log

    def test_reset_during_upload_releases_slots_and_reports_network_progress(self):
        # More resets than MAX_CONCURRENT_UPLOADS, followed by a successful
        # upload, proves cancellation does not consume the receiver's slots.
        before = sorted(self.out.iterdir())
        for index in range(3):
            tid = f"reset-test-{index}"
            log = self.reset_request("/Upload", tid, "Transfer-Encoding: chunked",
                                     b"4\r\ndata\r\n")
            self.assertIn(f"upload {tid}:", log)
            self.assertIn("9 HTTP body/framing bytes read", log)
            self.assertEqual(sorted(self.out.iterdir()), before)
        status, _ = self.post(self.connection(), "/Upload", dvzip([("after-reset.txt", b"ok")]),
                              {"Content-Type": "application/x-dvzip"})
        self.assertEqual(status, 200)
        self.assertEqual((self.out / "after-reset.txt").read_bytes(), b"ok")

    def test_reset_in_a_content_length_body_counts_partial_progress(self):
        log = self.reset_request("/Upload", "reset-length", "Content-Length: 1000", b"data")
        self.assertIn("4 HTTP body/framing bytes read", log)

    def test_discover_and_ask_can_be_retried_after_a_reset(self):
        for path in ("/Discover", "/Ask"):
            with self.subTest(path=path):
                self.reset_request(path, "reset-metadata", "Transfer-Encoding: chunked",
                                   b"4\r\ndata\r\n")
                status, _ = self.post(self.connection(), path,
                                      plistlib.dumps({}, fmt=plistlib.FMT_BINARY))
                self.assertEqual(status, 200)

    def test_repeated_small_and_larger_uploads_preserve_file_integrity(self):
        conn = self.connection()
        for index, size in enumerate((12, 4 * 1024 * 1024, 12)):
            data = os.urandom(size)
            name = f"repeat-{index}.bin"
            status, _ = self.post(conn, "/Upload", dvzip([(name, data)]),
                                  {"Content-Type": "application/x-dvzip"})
            self.assertEqual(status, 200)
            saved = (self.out / name).read_bytes()
            self.assertEqual(len(saved), size)
            self.assertEqual(hashlib.sha256(saved).digest(), hashlib.sha256(data).digest())


if __name__ == "__main__":
    unittest.main()
