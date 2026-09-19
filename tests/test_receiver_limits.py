"""Resource ceilings for sender-controlled AirDrop request bodies and archives."""

import gzip
import io
import logging
import os
import re
import socket
import subprocess
import tempfile
import threading
import time
import tomllib
import types
import unittest
import unicodedata
import zlib
from pathlib import Path

import libarchive


ROOT = Path(__file__).resolve().parents[1]
SERVE = ROOT / "bin" / "airdrop-serve.py"
CLI = ROOT / "bin" / "omdrop"


def receiver_namespace():
    """Load the pure receive helpers without starting the network service."""
    source = SERVE.read_text()
    start = source.index("KIB = 1024")
    end = source.index("\ndef store_link")
    namespace = {
        "io": io,
        "libarchive": libarchive,
        "logging": logging,
        "os": os,
        "re": re,
        "socket": socket,
        "tempfile": tempfile,
        "threading": threading,
        "time": time,
        "unicodedata": unicodedata,
        "zlib": zlib,
        "UNSAFE_CHAR": re.compile(
            r"[/\\\x00]|[\u202a-\u202e\u2066-\u2069\u200b-\u200f\ufeff]|[\x01-\x1f\x7f-\x9f]"
        ),
    }
    exec(source[start:end], namespace)
    namespace["MIN_FREE_BYTES"] = 0
    return namespace


def config_namespace():
    source = SERVE.read_text()
    start = source.index("def load_config")
    end = source.index("\ndef validate_download_dir")
    namespace = {
        "CONFIG_KEYS": ("name", "model", "download_dir", "max_receive_percent"),
        "STRING_CONFIG_KEYS": ("name", "model", "download_dir"),
        "re": re,
    }
    exec(source[start:end], namespace)
    return namespace


def cpio_bytes(files):
    with tempfile.TemporaryDirectory() as work:
        work = Path(work)
        archive_path = work / "payload.cpio"
        with libarchive.file_writer(str(archive_path), "cpio") as archive:
            for index, (name, data) in enumerate(files):
                source = work / f"source-{index}"
                source.write_bytes(data)
                archive.add_files(str(source), pathname=name)
        return archive_path.read_bytes()


def stored_dvzip(payload):
    return (0x80000000 | len(payload)).to_bytes(4, "big") + payload


class ReceiverConfigTests(unittest.TestCase):
    def test_integer_receive_percentage_is_accepted(self):
        ns = config_namespace()
        with tempfile.NamedTemporaryFile("w", delete=False) as config:
            config.write('name = "Drop"\nmax_receive_percent = 45\n')
        self.addCleanup(os.unlink, config.name)

        loaded = ns["load_config"](config.name)

        self.assertEqual(loaded["max_receive_percent"], 45)

    def test_string_receive_percentage_is_rejected(self):
        ns = config_namespace()
        with tempfile.NamedTemporaryFile("w", delete=False) as config:
            config.write('max_receive_percent = "45"\n')
        self.addCleanup(os.unlink, config.name)

        with self.assertRaises(SystemExit):
            ns["load_config"](config.name)


class RequestBodyLimitTests(unittest.TestCase):
    def setUp(self):
        self.ns = receiver_namespace()

    def test_receive_budget_uses_configured_share_of_free_space(self):
        self.ns["MIN_FREE_BYTES"] = 100
        fake_fs = types.SimpleNamespace(f_bavail=1000, f_frsize=1)
        original = os.statvfs
        os.statvfs = lambda _path: fake_fs
        self.addCleanup(setattr, os, "statvfs", original)

        budget = self.ns["ReceiveBudget"]("/downloads", 30)

        self.assertEqual(budget.byte_limit, 300)
        self.assertEqual(budget.percent, 30)

    def test_content_length_over_limit_is_rejected_before_body_is_read(self):
        source = io.BytesIO(b"abcde")
        target = io.BytesIO()

        with self.assertRaises(self.ns["BodyTooLarge"]):
            self.ns["read_request_body"](
                source, {"Content-Length": "5"}, target, 4
            )

        self.assertEqual(source.tell(), 0)
        self.assertEqual(target.getvalue(), b"")

    def test_chunked_body_stops_before_chunk_that_crosses_limit(self):
        source = io.BytesIO(b"4\r\nabcd\r\n4\r\nefgh\r\n0\r\n\r\n")
        target = io.BytesIO()

        with self.assertRaises(self.ns["BodyTooLarge"]):
            self.ns["read_request_body"](
                source, {"Transfer-Encoding": "chunked"}, target, 7
            )

        self.assertEqual(target.getvalue(), b"abcd")

    def test_body_that_misses_progress_deadline_is_rejected(self):
        deadline = self.ns["ReadDeadline"]()
        deadline.started -= self.ns["INITIAL_READ_SECONDS"] + 1

        with self.assertRaises(socket.timeout):
            self.ns["read_request_body"](
                io.BytesIO(b"x"), {"Content-Length": "1"},
                io.BytesIO(), 1, deadline=deadline
            )

    def test_decompression_stops_at_ceiling(self):
        with tempfile.TemporaryFile() as source, tempfile.TemporaryFile() as target:
            source.write(gzip.compress(b"x" * 32))
            source.seek(0)

            with self.assertRaises(self.ns["UploadLimitError"]):
                self.ns["decode_dvzip"](source, target, 31)

            self.assertLessEqual(target.tell(), 31)


class ArchiveExtractionLimitTests(unittest.TestCase):
    def setUp(self):
        self.ns = receiver_namespace()
        self.byte_limit = 1024 * 1024
        self.ns["MAX_ARCHIVE_MEMBERS"] = 32

    def store(self, files, dest):
        raw = io.BytesIO(stored_dvzip(cpio_bytes(files)))
        budget = types.SimpleNamespace(byte_limit=self.byte_limit)
        return self.ns["store_upload"](raw, dest, "transfer", budget)

    def test_successful_archive_is_streamed_to_files(self):
        with tempfile.TemporaryDirectory() as dest:
            written = self.store([("one.txt", b"one"), ("two.txt", b"two")], dest)

            self.assertEqual(written, ["one.txt", "two.txt"])
            self.assertEqual((Path(dest) / "one.txt").read_bytes(), b"one")
            self.assertEqual((Path(dest) / "two.txt").read_bytes(), b"two")

    def test_member_limit_removes_files_already_extracted(self):
        self.ns["MAX_ARCHIVE_MEMBERS"] = 1
        with tempfile.TemporaryDirectory() as dest:
            with self.assertRaises(self.ns["UploadLimitError"]):
                self.store([("one.txt", b"one"), ("two.txt", b"two")], dest)

            self.assertEqual(list(Path(dest).iterdir()), [])

    def test_per_file_limit_leaves_no_partial_output(self):
        self.byte_limit = 3
        with tempfile.TemporaryDirectory() as dest:
            with self.assertRaises(self.ns["UploadLimitError"]):
                self.store([("large.bin", b"four")], dest)

            self.assertEqual(list(Path(dest).iterdir()), [])

    def test_total_output_limit_removes_files_already_extracted(self):
        self.byte_limit = 5
        with tempfile.TemporaryDirectory() as dest:
            with self.assertRaises(self.ns["UploadLimitError"]):
                self.store([("one.bin", b"123"), ("two.bin", b"456")], dest)

            self.assertEqual(list(Path(dest).iterdir()), [])

    def test_limit_during_a_file_removes_that_partial_file(self):
        self.byte_limit = 3

        class Entry:
            isfile = True
            pathname = "partial.bin"
            size = 0

            @staticmethod
            def get_blocks():
                return iter((b"12", b"34"))

        class Archive:
            def __enter__(self):
                return iter((Entry(),))

            def __exit__(self, *_args):
                return False

        original = libarchive.stream_reader
        libarchive.stream_reader = lambda _stream: Archive()
        self.addCleanup(setattr, libarchive, "stream_reader", original)
        budget = types.SimpleNamespace(byte_limit=self.byte_limit)
        raw = io.BytesIO(stored_dvzip(b"decoded"))

        with tempfile.TemporaryDirectory() as dest:
            with self.assertRaises(self.ns["UploadLimitError"]):
                self.ns["store_upload"](raw, dest, "transfer", budget)

            self.assertEqual(list(Path(dest).iterdir()), [])

    def test_invalid_archive_is_not_saved_as_an_untrusted_raw_file(self):
        budget = types.SimpleNamespace(byte_limit=self.byte_limit)
        raw = io.BytesIO(stored_dvzip(b"not an archive"))
        with tempfile.TemporaryDirectory() as dest:
            with self.assertRaises(Exception):
                self.ns["store_upload"](raw, dest, "transfer", budget)

            self.assertEqual(list(Path(dest).iterdir()), [])


class ReadDeadlineTests(unittest.TestCase):
    def test_handler_sets_an_idle_timeout_on_each_accepted_socket(self):
        source = SERVE.read_text()
        start = source.index("class Handler(")
        end = source.index("\n\n# sharingd opens several connections", start)

        class BaseHandler:
            def setup(self):
                self.connection = self.request

        namespace = {
            "od_server": types.SimpleNamespace(AirDropServerHandler=BaseHandler),
            "READ_IDLE_TIMEOUT_SECONDS": 30,
        }
        exec(source[start:end], namespace)
        left, right = socket.socketpair()
        self.addCleanup(left.close)
        self.addCleanup(right.close)
        handler = namespace["Handler"].__new__(namespace["Handler"])
        handler.request = left

        handler.setup()

        self.assertEqual(left.gettimeout(), 30)


class LimitCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        (root / "home").mkdir()
        fake_bin = root / "bin"
        fake_bin.mkdir()
        systemctl = fake_bin / "systemctl"
        systemctl.write_text("#!/bin/sh\nexit 3\n")
        systemctl.chmod(0o755)
        self.config = root / "config"
        self.env = os.environ.copy()
        self.env.update({
            "HOME": str(root / "home"),
            "XDG_CONFIG_HOME": str(self.config),
            "PATH": f"{fake_bin}:{self.env['PATH']}",
        })

    def run_limit(self, *args, check=True):
        return subprocess.run(
            [str(CLI), "limit", *args],
            env=self.env, text=True, capture_output=True, check=check
        )

    def test_default_and_configured_percentage(self):
        self.assertEqual(self.run_limit().stdout, "30%\n")

        result = self.run_limit("45")

        self.assertIn("45%", result.stdout)
        with (self.config / "airdrop" / "config.toml").open("rb") as config:
            self.assertEqual(tomllib.load(config)["max_receive_percent"], 45)
        self.assertEqual(self.run_limit().stdout, "45%\n")

    def test_percentage_outside_safe_range_is_rejected(self):
        result = self.run_limit("91", check=False)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("integer from 1 to 90", result.stderr)


if __name__ == "__main__":
    unittest.main()
