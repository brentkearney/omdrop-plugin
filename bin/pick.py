#!/usr/bin/env python3
"""Ask the person for a folder or a file, in Nautilus, and print the path.

    pick.py folder|file START_DIR TITLE

Prints the chosen absolute path and exits 0. Exits 1 when the person cancels,
and 2 when no chooser can be shown at all, with the reason on stderr.

Nautilus first, by calling its portal backend directly. Nautilus 50 implements
org.freedesktop.impl.portal.FileChooser itself, but Omarchy routes the desktop
portal to GTK, so going through org.freedesktop.portal.Desktop would show the
GTK chooser instead. The backend interface answers the call synchronously,
with no Request object to watch. A desktop without Nautilus still gets the
desktop portal's chooser, whichever backend it routes to.
"""

import json
import os
import shutil
import signal
import subprocess
import sys
from urllib.parse import unquote, urlparse

import gi
from gi.repository import Gio, GLib

try:  # GLib 2.80 moved it; older PyGObject only has the deprecated spelling.
    gi.require_version("GLibUnix", "2.0")
    from gi.repository import GLibUnix
    signal_add = GLibUnix.signal_add
except (ImportError, ValueError):
    signal_add = GLib.unix_signal_add

NAUTILUS = "org.gnome.Nautilus"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
IMPL = "org.freedesktop.impl.portal.FileChooser"
APP_ID = "netmojo.omdrop"


def options(kind, start):
    opts = {
        "modal": GLib.Variant("b", False),
        "accept_label": GLib.Variant("s", "Select" if kind == "folder" else "Send"),
    }
    if kind == "folder":
        opts["directory"] = GLib.Variant("b", True)
    if start and os.path.isdir(start):
        # A NUL-terminated byte string: the portal's type for a path.
        opts["current_folder"] = GLib.Variant("ay", os.fsencode(start) + b"\0")
    return opts


def path_of(results):
    uris = results.get("uris") or []
    if not uris:
        return None
    u = urlparse(uris[0])
    return unquote(u.path) if u.scheme == "file" else None


def focus_when_mapped(title):
    """Give the chooser the keyboard, on Hyprland.

    A chooser opened from the panel carries no activation token, so Hyprland
    maps it without focus: measured 2026-09-23, it opened behind the focused
    window's input and keystrokes went to whatever had them before. Wait for
    the window with our title to appear, then focus it. Anywhere else this
    does nothing, and the compositor's own policy stands.
    """
    if not os.environ.get("HYPRLAND_INSTANCE_SIGNATURE") or not shutil.which("hyprctl"):
        return
    tries = {"left": 50}

    def attempt():
        tries["left"] -= 1
        try:
            clients = json.loads(subprocess.run(["hyprctl", "clients", "-j"], capture_output=True,
                                                text=True, timeout=2).stdout or "[]")
        except (OSError, subprocess.SubprocessError, ValueError):
            return GLib.SOURCE_REMOVE
        match = [c for c in clients if c.get("title") == title]
        if not match:
            return GLib.SOURCE_CONTINUE if tries["left"] > 0 else GLib.SOURCE_REMOVE
        addr = match[0]["address"]
        # The Lua config's dispatcher first; the classic one for older setups.
        out = subprocess.run(["hyprctl", "dispatch", f'hl.dsp.focus({{ window = "address:{addr}" }})'],
                             capture_output=True, text=True, timeout=2).stdout.strip()
        if out != "ok":
            subprocess.run(["hyprctl", "dispatch", "focuswindow", f"address:{addr}"],
                           capture_output=True, timeout=2)
        return GLib.SOURCE_REMOVE

    GLib.timeout_add(100, attempt)


def run(bus, dest, handle, start_call, title):
    """Run a chooser to its answer, closing it if we are told to stop.

    A panel that goes away (the shell reloads, or its Process is killed) must
    not leave a chooser on screen whose answer nobody is waiting for. The
    signal is watched from the main loop: a Python handler cannot run while
    the thread sits inside a blocking D-Bus call, which is how the first draft
    hung on SIGTERM with the chooser still open.
    """
    loop = GLib.MainLoop()
    answer = {}

    def finish(path=None, error=None):
        answer.update(path=path, error=error)
        loop.quit()

    def stop():
        bus.call(dest, handle, "org.freedesktop.impl.portal.Request" if dest == NAUTILUS
                 else "org.freedesktop.portal.Request", "Close",
                 None, None, Gio.DBusCallFlags.NONE, 2000, None,
                 lambda *_: finish(), None)
        GLib.timeout_add(2500, finish)
        return GLib.SOURCE_REMOVE

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal_add(GLib.PRIORITY_HIGH, sig, stop)
    start_call(finish)
    focus_when_mapped(title)
    loop.run()
    if answer.get("error"):
        raise answer["error"]
    return answer.get("path")


def via_nautilus(bus, kind, start, title):
    handle = f"{PORTAL_PATH}/request/1_1/omdrop_{os.getpid()}"

    def start_call(finish):
        def done(conn, res):
            try:
                response, results = conn.call_finish(res).unpack()
            except GLib.Error as e:
                finish(error=e)
                return
            finish(path_of(results) if response == 0 else None)

        bus.call(NAUTILUS, PORTAL_PATH, IMPL, "OpenFile",
                 GLib.Variant("(osssa{sv})", (handle, APP_ID, "", title, options(kind, start))),
                 GLib.VariantType("(ua{sv})"), Gio.DBusCallFlags.NONE, GLib.MAXINT, None, done)

    return run(bus, NAUTILUS, handle, start_call, title)


def via_portal(bus, kind, start, title):
    token = f"omdrop_{os.getpid()}"
    sender = bus.get_unique_name()[1:].replace(".", "_")
    handle = f"{PORTAL_PATH}/request/{sender}/{token}"

    def start_call(finish):
        def on_response(_c, _s, _p, _i, _sig, params):
            response, results = params.unpack()
            finish(path_of(results) if response == 0 else None)

        # Subscribed before the call, at the path the portal will use, so a
        # quick answer cannot arrive before anyone is listening.
        bus.signal_subscribe("org.freedesktop.portal.Desktop", "org.freedesktop.portal.Request",
                             "Response", handle, None, Gio.DBusSignalFlags.NO_MATCH_RULE, on_response)
        opts = options(kind, start)
        opts["handle_token"] = GLib.Variant("s", token)

        def done(conn, res):
            try:
                conn.call_finish(res)
            except GLib.Error as e:
                finish(error=e)

        bus.call("org.freedesktop.portal.Desktop", PORTAL_PATH, "org.freedesktop.portal.FileChooser",
                 "OpenFile", GLib.Variant("(ssa{sv})", ("", title, opts)),
                 GLib.VariantType("(o)"), Gio.DBusCallFlags.NONE, 10000, None, done)

    return run(bus, "org.freedesktop.portal.Desktop", handle, start_call, title)


def main(argv):
    if len(argv) != 4 or argv[1] not in ("folder", "file"):
        print("usage: pick.py folder|file START_DIR TITLE", file=sys.stderr)
        return 2
    kind, start, title = argv[1], argv[2], argv[3]
    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    try:
        path = via_nautilus(bus, kind, start, title)
    except GLib.Error as e:
        # Absent or not activatable. Anything else (a Nautilus that crashed
        # mid-dialog) is not a reason to pop a second chooser at them.
        if "ServiceUnknown" not in str(e) and "UnknownMethod" not in str(e) \
                and "UnknownObject" not in str(e):
            print(f"The file chooser failed: {e.message}", file=sys.stderr)
            return 2
        try:
            path = via_portal(bus, kind, start, title)
        except GLib.Error as e2:
            print(f"No file chooser is available: {e2.message}", file=sys.stderr)
            return 2
    if not path:
        return 1
    print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
