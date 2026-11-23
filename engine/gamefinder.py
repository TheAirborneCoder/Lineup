# SPDX-License-Identifier: GPL-3.0-or-later
"""Find the game folder and check that it is the version Lineup's indexes were made for.

A game folder is one that holds MicrosoftGame.Config and packs.rpf as plain files. Nothing here reads a large
file: the search only asks whether those two names exist, and the check reads the small config file and the sizes
of four archives.
"""
import os
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

SUPPORTED_VERSION = "1.0.2.1"
# Sizes of the archives Lineup reads, in game version 1.0.2.1.
EXPECTED_SIZES = {
    "packs.rpf": 4798213264,
    "data.rpf": 481544304,
    "common.rpf": 6642192,
    "anim.rpf": 4407626384,
}
CONFIG = "MicrosoftGame.Config"
MARKERS = (CONFIG, "packs.rpf")
SKIP_NAMES = {"appdata", "node_modules", "windows", "program files", "program files (x86)", "programdata",
              "$recycle.bin", "system volume information", "proc", "sys", "dev", "library", "snap"}


def is_game_folder(path):
    try:
        return all((Path(path) / name).is_file() for name in MARKERS)
    except OSError:
        return False


def _children(path):
    try:
        with os.scandir(path) as it:
            for entry in it:
                try:
                    if entry.is_dir(follow_symlinks=False) and entry.name.lower() not in SKIP_NAMES \
                            and not entry.name.startswith("."):
                        yield Path(entry.path)
                except OSError:
                    continue
    except OSError:
        return


def search_roots():
    """Folders worth looking in, most likely first."""
    home = Path.home()
    roots = []
    for name in ("Downloads", "Desktop", "Documents", "Games"):
        roots.append(home / name)
    roots.append(home)
    if sys.platform == "win32":
        for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
            drive = Path(f"{letter}:/")
            if drive.exists():
                roots.append(drive)
                roots.append(drive / "Games")
    elif sys.platform == "darwin":
        roots += _children(Path("/Volumes"))
    else:
        user = os.environ.get("USER") or home.name
        for base in (Path("/mnt"), Path("/media") / user, Path("/run/media") / user):
            roots += list(_children(base))
            if base.is_dir():
                roots.append(base)
    unique, seen = [], set()
    for root in roots:
        key = str(root)
        if key not in seen and root.is_dir():
            seen.add(key)
            unique.append(root)
    return unique


def find(depth=2):
    """Game folders found under the usual places, two folders deep. Never reads file contents."""
    found, seen = [], set()

    def visit(folder, level):
        key = os.path.normcase(str(folder))
        if key in seen:
            return
        seen.add(key)
        if is_game_folder(folder):
            found.append(folder)
            return
        if level < depth:
            for child in _children(folder):
                visit(child, level + 1)

    for root in search_roots():
        visit(root, 0)
    return found


def game_version(game):
    """The Version attribute of the Identity element in MicrosoftGame.Config, or None."""
    path = Path(game) / CONFIG
    try:
        root = ET.parse(path).getroot()
        for element in root.iter():
            if element.tag.split("}")[-1] == "Identity" and element.get("Version"):
                return element.get("Version")
    except (OSError, ET.ParseError):
        pass
    try:
        text = path.read_text("utf-8", "replace")
    except OSError:
        return None
    match = re.search(r"<Identity\b[^>]*\bVersion=\"([^\"]+)\"", text)
    return match.group(1) if match else None


def check(game):
    """Compare a folder with the supported version. Returns (ok, [problem, ...]); every problem is listed."""
    game = Path(game)
    problems = []
    if not game.is_dir():
        return False, [f"{game} is not a folder"]
    if not is_game_folder(game):
        problems.append(f"{CONFIG} and packs.rpf were not both found in {game}")
    version = game_version(game)
    if version is None:
        problems.append(f"{CONFIG}: no Identity Version found")
    elif version != SUPPORTED_VERSION:
        problems.append(f"{CONFIG}: version {version}, Lineup supports {SUPPORTED_VERSION}")
    for name, size in EXPECTED_SIZES.items():
        path = game / name
        try:
            actual = path.stat().st_size
        except OSError:
            problems.append(f"{name}: missing")
            continue
        if actual != size:
            problems.append(f"{name}: {actual:,} bytes, expected {size:,}")
    return not problems, problems
