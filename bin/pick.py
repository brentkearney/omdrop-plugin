#!/usr/bin/env python3
"""Ask the person for a folder or files, in Nautilus, and print the paths.

    pick.py folder|files START_DIR TITLE

Prints the chosen folder's absolute path on one line, or each chosen file's
absolute path followed by a NUL, since a filename may hold a newline. Exits 0
with an answer, 1 when the person cancels, and 2 when no chooser can be shown
at all, with the reason on stderr.

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
    else:
        opts["multiple"] = GLib.Variant("b", True)
    if start and os.path.isdir(start):
        # A NUL-terminated byte string: the portal's type for a path.
        opts["current_folder"] = GLib.Variant("ay", os.fsencode(start) + b"\0")
    return opts


def paths_of(results):
    """The chosen local paths. A remote location has no path to send from."""
    paths = []
    for uri in results.get("uris") or []:
        u = urlparse(uri)
        if u.scheme == "file":
            paths.append(unquote(u.path))
    return paths


def hypr_dispatch(lua, *classic):
    """One Hyprland dispatch: the Lua config's form first, then the classic one."""
    out = subprocess.run(["hyprctl", "dispatch", lua], capture_output=True, text=True, timeout=2)
    if out.stdout.strip() != "ok" and classic:
        subprocess.run(["hyprctl", "dispatch", *classic], capture_output=True, timeout=2)


def present_when_mapped(title):
    """Put the chooser in front, as a dialog: floating, centred, focused.

    A chooser opened from the panel carries no parent window and no activation
    token, so Hyprland treats it as an ordinary window: measured 2026-09-23, it
    tiled beside whatever was open and took no focus, so keystrokes went to the
    window that had them before. Wait for the window with our title to appear,
    then float it at a dialog's size, centre it, raise it and focus it, the same
    dispatches omarchy-hyprland-window-pop uses. Anywhere else this does
    nothing, and the compositor's own policy stands.
    """
    if not os.environ.get("HYPRLAND_INSTANCE_SIGNATURE") or not shutil.which("hyprctl"):
        return
    tries = {"left": 50}

    def attempt():
        tries["left"] -= 1
        try:
            clients = json.loads(subprocess.run(["hyprctl", "clients", "-j"], capture_output=True,
                                                text=True, timeout=2).stdout or "[]")
            monitor = json.loads(subprocess.run(["hyprctl", "activeworkspace", "-j"], capture_output=True,
                                                text=True, timeout=2).stdout or "{}")
            monitors = json.loads(subprocess.run(["hyprctl", "monitors", "-j"], capture_output=True,
                                                 text=True, timeout=2).stdout or "[]")
        except (OSError, subprocess.SubprocessError, ValueError):
            return GLib.SOURCE_REMOVE
        match = [c for c in clients if c.get("title") == title]
        if not match:
            return GLib.SOURCE_CONTINUE if tries["left"] > 0 else GLib.SOURCE_REMOVE
        win = match[0]
        w = f"address:{win['address']}"
        # Two thirds of the screen, within a dialog's sensible bounds.
        mon = next((m for m in monitors if m.get("id") == monitor.get("monitorID")), None) \
            or (monitors[0] if monitors else {})
        scale = mon.get("scale") or 1
        sw, sh = mon.get("width", 1600) / scale, mon.get("height", 1000) / scale
        width, height = int(min(max(sw * 0.6, 720), 1100)), int(min(max(sh * 0.66, 520), 800))
        if not win.get("floating"):
            hypr_dispatch(f'hl.dsp.window.float({{ window = "{w}", action = "toggle" }})', "togglefloating", w)
        hypr_dispatch(f'hl.dsp.window.resize({{ window = "{w}", x = {width}, y = {height} }})',
                      "resizewindowpixel", f"exact {width} {height},{w}")
        hypr_dispatch(f'hl.dsp.window.center({{ window = "{w}" }})', "centerwindow", w)
        hypr_dispatch(f'hl.dsp.window.alter_zorder({{ window = "{w}", mode = "top" }})', "alterzorder", f"top,{w}")
        hypr_dispatch(f'hl.dsp.focus({{ window = "{w}" }})', "focuswindow", w)
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

    def finish(paths=None, error=None):
        answer.update(paths=paths, error=error)
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
    present_when_mapped(title)
    loop.run()
    if answer.get("error"):
        raise answer["error"]
    return answer.get("paths")


def via_nautilus(bus, kind, start, title):
    handle = f"{PORTAL_PATH}/request/1_1/omdrop_{os.getpid()}"

    def start_call(finish):
        def done(conn, res):
            try:
                response, results = conn.call_finish(res).unpack()
            except GLib.Error as e:
                finish(error=e)
                return
            finish(paths_of(results) if response == 0 else None)

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
            finish(paths_of(results) if response == 0 else None)

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
    if len(argv) != 4 or argv[1] not in ("folder", "files"):
        print("usage: pick.py folder|files START_DIR TITLE", file=sys.stderr)
        return 2
    kind, start, title = argv[1], argv[2], argv[3]
    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    try:
        paths = via_nautilus(bus, kind, start, title)
    except GLib.Error as e:
        # Absent or not activatable. Anything else (a Nautilus that crashed
        # mid-dialog) is not a reason to pop a second chooser at them.
        if "ServiceUnknown" not in str(e) and "UnknownMethod" not in str(e) \
                and "UnknownObject" not in str(e):
            print(f"The file chooser failed: {e.message}", file=sys.stderr)
            return 2
        try:
            paths = via_portal(bus, kind, start, title)
        except GLib.Error as e2:
            print(f"No file chooser is available: {e2.message}", file=sys.stderr)
            return 2
    if not paths:
        return 1
    if kind == "folder":
        print(paths[0])
    else:
        sys.stdout.write("".join(p + "\0" for p in paths))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
