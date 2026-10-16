#!/usr/bin/env python3
"""Opt-in installation of regular files: Strata refuses symlinked actions."""
import hashlib
import json
import os
from pathlib import Path
import sys

SOURCE = Path(__file__).resolve().parents[1] / "share/strata/omdrop"
FILES = ("action.toml", "run.py")
RECORD = ".omdrop-installed.json"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def pristine(target):
    if target.is_symlink() or not target.is_dir():
        return False
    if {p.name for p in target.iterdir()} != {*FILES, RECORD}:
        return False
    if any((target / name).is_symlink() or not (target / name).is_file()
           for name in (*FILES, RECORD)):
        return False
    try:
        expected = json.loads((target / RECORD).read_text())
    except (ValueError, OSError):
        return False
    return expected == {name: digest(target / name) for name in FILES}


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in ("install", "remove"):
        print("usage: omdrop strata install|remove", file=sys.stderr)
        return 1
    target = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "strata/actions/omdrop"
    exists = target.exists() or target.is_symlink()
    try:
        if exists and not pristine(target):
            raise ValueError(f"Left {target} alone: it is an existing or edited action. "
                             "Move it elsewhere before installing, or remove it yourself.")
        if sys.argv[1] == "remove":
            if exists:
                for name in (*FILES, RECORD):
                    (target / name).unlink()
                target.rmdir()
            print("Removed the Omdrop action for Strata.")
            return 0
        # Read every source before changing the installed action.
        contents = {name: (SOURCE / name).read_bytes() for name in FILES}
        if not exists:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.mkdir(mode=0o700)
        for name, content in contents.items():
            (target / name).write_bytes(content)
            (target / name).chmod(0o600)
        (target / RECORD).write_text(json.dumps({name: digest(target / name) for name in FILES}))
        (target / RECORD).chmod(0o600)
        print("Installed the Omdrop action for Strata. Right-click files and choose OmDrop…")
        return 0
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
