#!/usr/bin/env python3
"""Strata custom action: choose an AirDrop recipient, then use Omdrop to send."""

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys


def selected_files():
    source = os.environ.get("STRATA_ACTION_PATHS")
    if not source:
        raise ValueError("Run this action from Strata’s file context menu.")
    raw = Path(source).read_bytes()
    if not raw or not raw.endswith(b"\0"):
        raise ValueError("No files were selected.")
    paths = [os.fsdecode(path) for path in raw[:-1].split(b"\0")]
    names = set()
    for path in paths:
        if not os.path.isabs(path) or not os.path.isfile(path) or not os.access(path, os.R_OK):
            raise ValueError("Select readable local files. Compress folders before sending them.")
        name = os.path.basename(path)
        if name in names:
            raise ValueError("Two selected files have the same name. Send them separately.")
        names.add(name)
    return paths


def omdrop_program():
    local = Path.home() / ".local/bin/omdrop"
    if local.is_file() and os.access(local, os.X_OK):
        return str(local)
    program = shutil.which("omdrop")
    if not program:
        raise ValueError("Omdrop is not installed.")
    return program


def discover(program):
    status = subprocess.run([program, "status", "--json"], check=True,
                            capture_output=True, text=True)
    state = json.loads(status.stdout)
    if not isinstance(state, dict):
        raise ValueError("Omdrop returned an invalid status.")
    if not state.get("visible"):
        print("Turning Omdrop on for 10 minutes…", flush=True)
        subprocess.run([program, "on", "10m"], check=True)
    print("Looking for devices. Set iPhone AirDrop receiving to Everyone for 10 Minutes, "
          "then keep Photos → Share → AirDrop open until the transfer finishes.", flush=True)
    result = subprocess.run([program, "peers", "-n", "--json"],
                            capture_output=True, text=True)
    if result.returncode:
        raise ValueError(result.stderr.strip() or result.stdout.strip() or "No devices found.")
    peers = json.loads(result.stdout)
    if not isinstance(peers, list) or not peers:
        raise ValueError("No devices found. Open AirDrop on your device, then try OmDrop again.")
    for peer in peers:
        if not isinstance(peer, dict) or not re.fullmatch(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}",
                                                        str(peer.get("mac", ""))):
            raise ValueError("Omdrop returned an invalid device address.")
    return peers


def choose_peer(peers, count):
    try:
        import gi
        gi.require_version("Gtk", "4.0")
        from gi.repository import GLib, Gtk
    except (ImportError, ValueError) as error:
        raise ValueError("The device picker needs GTK 4 and python-gobject.") from error

    Gtk.init()
    loop = GLib.MainLoop()
    chosen = []
    window = Gtk.Window(title="OmDrop", default_width=440, resizable=False)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
    for edge in ("top", "bottom", "start", "end"):
        getattr(box, "set_margin_" + edge)(24)
    window.set_child(box)
    label = Gtk.Label(label=f"Send {count} file{'s' if count != 1 else ''} to:", xalign=0)
    box.append(label)
    labels = [f"{peer.get('name') or 'Nearby Apple device'} · {peer['mac']}" for peer in peers]
    picker = Gtk.DropDown.new_from_strings(labels)
    box.append(picker)
    hint = Gtk.Label(label="On iPhone, keep Photos → Share → AirDrop open until the transfer "
                     "finishes. Accept the transfer there.",
                     wrap=True, xalign=0)
    box.append(hint)
    buttons = Gtk.Box(spacing=8, halign=Gtk.Align.END)
    cancel = Gtk.Button(label="Cancel")
    send = Gtk.Button(label="Send")
    send.add_css_class("suggested-action")
    buttons.append(cancel)
    buttons.append(send)
    box.append(buttons)

    def close(*_):
        loop.quit()
        return False

    def accept(*_):
        chosen.append(peers[picker.get_selected()]["mac"])
        loop.quit()

    cancel.connect("clicked", close)
    send.connect("clicked", accept)
    window.connect("close-request", close)
    window.present()
    loop.run()
    window.destroy()
    return chosen[0] if chosen else None


def main():
    try:
        paths = selected_files()
        program = omdrop_program()
        peer = choose_peer(discover(program), len(paths))
        if peer is None:
            print("Cancelled. Nothing was sent.", flush=True)
            return 1
        current = subprocess.run([program, "peers", "--json"], capture_output=True, text=True)
        peers = json.loads(current.stdout) if current.returncode == 0 else []
        if not isinstance(peers, list) or not any(
                isinstance(item, dict) and item.get("mac") == peer for item in peers):
            raise ValueError("That device is no longer nearby, or its address changed. "
                             "Reopen AirDrop and try OmDrop again.")
        paths = selected_files()
        print("Sending…", flush=True)
        return subprocess.run([program, "send", "--to", peer, "--", *paths]).returncode
    except subprocess.CalledProcessError as error:
        print(error.stderr or error.stdout or str(error), file=sys.stderr, flush=True)
        return 1
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
