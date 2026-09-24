"""Received link files: which ones open, and what the handler leaves behind.

A .url or .webloc arrives from whoever is nearby, so the URL inside it is
untrusted. Only web links may reach the browser; anything else is refused
with a notification, whichever way the file was opened.
"""

import os
import plistlib
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OMDROP = ROOT / "bin" / "omdrop"


class LinkFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.log = self.root / "log"
        # The browser and the notifier record what they were asked to do.
        for name in ("omarchy-launch-browser", "omarchy-notification-send"):
            path = self.bin / name
            path.write_text(f'#!/bin/sh\nprintf "{name}|%s\\n" "$1" >> "$LOG"\n')
            path.chmod(0o755)
        self.env = os.environ.copy()
        self.env.update(
            PATH=f"{self.bin}:{self.env['PATH']}",
            LOG=str(self.log),
            XDG_CONFIG_HOME=str(self.root / "config"),
            XDG_DATA_HOME=str(self.root / "data"),
        )

    def run_omdrop(self, *args):
        return subprocess.run([OMDROP, *args], env=self.env, capture_output=True, text=True)

    def logged(self):
        return self.log.read_text().splitlines() if self.log.exists() else []


class OpenLinkTests(LinkFixture):
    def url_file(self, body, name="link.url"):
        path = self.root / name
        path.write_bytes(body.encode())
        return path

    def test_a_windows_shortcut_opens_its_web_page(self):
        link = self.url_file("[InternetShortcut]\r\nURL=https://example.com/a?b=1\r\n")

        result = self.run_omdrop("open-link", str(link))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.logged(), ["omarchy-launch-browser|https://example.com/a?b=1"])

    def test_a_url_line_outside_the_shortcut_section_is_not_the_link(self):
        link = self.url_file("[Other]\nURL=https://decoy.example/\n"
                             "[InternetShortcut]\nURL=https://example.com/\n")

        self.run_omdrop("open-link", str(link))

        self.assertEqual(self.logged(), ["omarchy-launch-browser|https://example.com/"])

    def test_a_link_that_is_not_a_web_page_is_refused_with_a_notification(self):
        for url in ("file:///etc/passwd", "smb://server/share", "x-app://run", "javascript:alert(1)",
                    "https:///no-host", "https://example.com/a b"):
            with self.subTest(url=url):
                self.log.unlink(missing_ok=True)
                link = self.url_file(f"[InternetShortcut]\nURL={url}\n")

                result = self.run_omdrop("open-link", str(link))

                self.assertNotEqual(result.returncode, 0)
                logged = self.logged()
                self.assertEqual(len(logged), 1, logged)
                self.assertTrue(logged[0].startswith("omarchy-notification-send|"), logged)

    def test_a_webloc_opens_whether_xml_or_binary(self):
        for fmt in (plistlib.FMT_XML, plistlib.FMT_BINARY):
            with self.subTest(fmt=fmt):
                self.log.unlink(missing_ok=True)
                link = self.root / "site.webloc"
                link.write_bytes(plistlib.dumps({"URL": "https://example.com/?a=1&b=2"}, fmt=fmt))

                result = self.run_omdrop("open-link", str(link))

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.logged(), ["omarchy-launch-browser|https://example.com/?a=1&b=2"])

    def test_a_webloc_pointing_at_a_file_is_refused(self):
        link = self.root / "local.webloc"
        link.write_bytes(plistlib.dumps({"URL": "file:///home"}))

        result = self.run_omdrop("open-link", str(link))

        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("omarchy-launch-browser", self.log.read_text())

    def test_the_notification_click_uses_the_same_gate(self):
        link = self.url_file("[InternetShortcut]\nURL=file:///etc/passwd\n")

        self.run_omdrop("act", "open", str(link))

        self.assertNotIn("omarchy-launch-browser", self.log.read_text())


@unittest.skipUnless(all(shutil.which(t) for t in
                         ("xdg-mime", "update-mime-database", "update-desktop-database")),
                     "needs xdg-utils, shared-mime-info and desktop-file-utils")
class LinkHandlerInstallTests(LinkFixture):
    def mimeapps(self):
        path = self.root / "config" / "mimeapps.list"
        return path.read_text() if path.exists() else ""

    def test_remove_undoes_install(self):
        self.assertEqual(self.run_omdrop("links", "install").returncode, 0)
        self.assertIn("application/x-webloc=netmojo.omdrop-links.desktop", self.mimeapps())

        self.run_omdrop("links", "remove")

        self.assertNotIn("netmojo.omdrop-links.desktop", self.mimeapps())
        self.assertFalse((self.root / "data" / "applications" / "netmojo.omdrop-links.desktop").exists())
        self.assertFalse((self.root / "data" / "mime" / "packages" / "netmojo.omdrop-webloc.xml").exists())

    def test_a_default_the_user_chose_survives_install_and_remove(self):
        config = self.root / "config"
        config.mkdir()
        (config / "mimeapps.list").write_text(
            "[Default Applications]\napplication/x-mswinurl=my-browser.desktop\n")

        self.run_omdrop("links", "install")
        self.assertIn("application/x-mswinurl=my-browser.desktop", self.mimeapps())
        self.run_omdrop("links", "remove")

        self.assertIn("application/x-mswinurl=my-browser.desktop", self.mimeapps())


if __name__ == "__main__":
    unittest.main()
