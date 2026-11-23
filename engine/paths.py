# SPDX-License-Identifier: GPL-3.0-or-later
"""Where Lineup reads the game from and where it keeps what it builds.

Three places, never mixed:

    GAME     your copy of the game (read only): the folder with MicrosoftGame.Config and the .rpf archives
    WORK     everything Lineup derives from it (indexes, databases, decoded caches); safe to delete
    ENGINE   this folder (code and the format tables that ship with it)

Both folders come from the environment (LINEUP_GAME, LINEUP_WORK), which lineup.py sets before it imports
anything else, so worker processes see the same folders. Without them the saved settings are used.
"""
import json
import os
import sys
from pathlib import Path

APP = "Lineup"
ENGINE = Path(__file__).resolve().parent
REPO = ENGINE.parent


def user_data_dir():
    """The per-user folder for Lineup's settings and, by default, its work files."""
    home = Path.home()
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
        return base / APP
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / APP
    return Path(os.environ.get("XDG_DATA_HOME") or home / ".local" / "share") / APP.lower()


SETTINGS = user_data_dir() / "settings.json"


def load_settings():
    try:
        value = json.loads(SETTINGS.read_text("utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(**changes):
    value = load_settings()
    value.update({k: str(v) for k, v in changes.items() if v is not None})
    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS.write_text(json.dumps(value, indent=2), "utf-8")
    return value


def default_work():
    return user_data_dir() / "work"


_saved = load_settings()
# A placeholder keeps imports working (for --help and the tests) when no game folder is known yet.
GAME = Path(os.environ.get("LINEUP_GAME") or _saved.get("game") or ENGINE / "__game_folder_not_set__")
WORK = Path(os.environ.get("LINEUP_WORK") or _saved.get("work") or default_work())

INDEX = WORK / "index"        # per-archive member indexes: <archive>/deflate-spans.bin, *.pso, xml/
LIBRARY = WORK / "library"    # the databases the viewer reads
CACHE = WORK / "cache"        # decoded models and textures, filled as you browse
NATIVE = WORK / "native"      # optional compiled helpers (texture decoding speed-up)


def configure(game=None, work=None):
    """Set the folders for this process and every process it starts. Call before importing other engine modules,
    which read GAME, INDEX, LIBRARY ... when they are imported."""
    global GAME, WORK, INDEX, LIBRARY, CACHE, NATIVE
    if game is not None:
        os.environ["LINEUP_GAME"] = str(Path(game).resolve())
        GAME = Path(os.environ["LINEUP_GAME"])
    if work is not None:
        os.environ["LINEUP_WORK"] = str(Path(work).resolve())
        WORK = Path(os.environ["LINEUP_WORK"])
    # Worker processes read the same variables, so the environment always holds the final answer.
    os.environ.setdefault("LINEUP_GAME", str(GAME))
    os.environ.setdefault("LINEUP_WORK", str(WORK))
    INDEX, LIBRARY, CACHE, NATIVE = WORK / "index", WORK / "library", WORK / "cache", WORK / "native"


def ensure_work():
    for folder in (INDEX, LIBRARY, CACHE, NATIVE):
        folder.mkdir(parents=True, exist_ok=True)


def game_archives(names=None):
    """Archive paths under GAME, relative POSIX names as keys. Collision (bvh4) archives hold no characters."""
    found = {}
    for path in sorted(GAME.glob("*.rpf")) + sorted((GAME / "pack").glob("*/*.rpf")):
        key = path.relative_to(GAME).as_posix()
        if names is None or key in names:
            found[key] = path
    return found


def archive_path(name):
    """Resolve an archive name stored in a database. Refuses anything outside the game folder."""
    root = GAME.resolve()
    path = (root / name).resolve()
    # Stored names always use forward slashes; a backslash is a separator on Windows only, so refuse it everywhere.
    if "\\" in str(name) or not path.is_relative_to(root) or path.suffix != ".rpf":
        raise ValueError("unknown archive")
    return path
