#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Fail when the repository holds anything that looks like game data.

    python tools/check_no_game_data.py              check every file under the repository
    python tools/check_no_game_data.py FILE ...     check only these files (for example the files of a pull request)

It checks, for each file:
  * the extension is not one of the blocked kinds (game archives, extracted data, textures, models, databases);
  * the first bytes are not the magic number of a game archive or of an image, model, database or compressed stream;
  * the file is text, except for the one image assets/logo.svg (which is text too);
  * a JSON data file is on the allow-list below, and no file is larger than 300 KB unless it is on that list.

Nothing here reads a file beyond its first 8 KB except to measure its size. Exit status 1 when anything is found.
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {".git", ".venv", "__pycache__", "node_modules", ".mypy_cache", ".pytest_cache"}
SIZE_LIMIT = 300 * 1024

BLOCKED_EXTENSIONS = {
    ".rpf", ".ttpf", ".etpf", ".xvd", ".msixvc", ".pso", ".sqlite3", ".glb", ".gltf", ".gtex", ".dds", ".png",
    ".jpg", ".jpeg", ".webp", ".bk2", ".awc", ".fssm", ".dxbc", ".spv", ".s", ".bin", ".db", ".sqlite",
    ".dll", ".so", ".dylib", ".obj", ".exp", ".lib", ".exe", ".pyc", ".zip", ".7z", ".rar", ".tar", ".gz",
}

# The first bytes of files that must never be here: (prefix, what it is).
MAGIC = [
    (b"9FPR", "a game archive (RPF)"),
    (b"RDS5", "a game stream header"),
    (b"PSIN", "a game metadata (PSO) file"),
    (b"ETPF", "a texture pack (ETPF)"),
    (b"SQLite format 3\x00", "an SQLite database"),
    (b"glTF", "a binary glTF model"),
    (b"\x89PNG\r\n\x1a\n", "a PNG image"),
    (b"DDS ", "a DDS texture"),
    (b"\xff\xd8\xff", "a JPEG image"),
    (b"RIFF", "a RIFF container (WebP, WAV ...)"),
    (b"PK\x03\x04", "a ZIP archive"),
    (b"MZ", "a Windows executable"),
    (b"\x7fELF", "a Linux executable"),
]
ZLIB_HEADERS = (b"\x78\x01", b"\x78\x5e", b"\x78\x9c", b"\x78\xda")

# Data files that may be present, each with the reason and the largest size it may have. Every other .json file,
# and every file over 300 KB, is refused. The bcpack_data tables describe the BCPack codec (probability tables,
# rank tables, built-in histories recovered by this project's own clean-room research); they hold no pixels and no
# other content from any texture.
ALLOWED_DATA = {
    "engine/bcpack_data/tables.json": ("rANS probability tables of the BCPack codec, one frequency list per width and table number", 8_000),
    "engine/bcpack_data/bc1_index.json": ("BC1: rank of each 2x2 index quadrant in the codec's fixed 256-entry table", 3_000),
    "engine/bcpack_data/bc1_presets.json": ("BC1: the codec's built-in endpoint history (256 entries) with evidence classes", 100_000),
    "engine/bcpack_data/bc3.json": ("BC3: alpha pair table, colour quadrant table and built-in history entries", 30_000),
    "engine/bcpack_data/bc4.json": ("BC4: index pair table and the codec's built-in endpoint history", 120_000),
    "engine/bcpack_data/bc5.json": ("BC5: index pair table and the codec's built-in history", 90_000),
    "engine/bcpack_data/bc6h.json": ("BC6H: the codec's built-in endpoint and index history with its classes", 60_000),
    "engine/bcpack_data/bc7.json": ("BC7: mode configuration, partition and anchor tables, built-in history", 200_000),
    "engine/script_symbol_names.json": ("this project's own list of script symbol spellings (about 600 words)", 20_000),
}
ALLOWED_IMAGES = {"assets/logo.svg"}


def is_text(head):
    if b"\x00" in head:
        return False
    try:
        head.decode("utf-8")
    except UnicodeDecodeError as error:
        # A multi-byte character cut off at the end of the 8 KB window is fine.
        if error.start < len(head) - 4:
            return False
    return True


def check_file(path, root=ROOT):
    """Reasons this file must not be in the repository (an empty list when it is fine)."""
    path = Path(path)
    try:
        relative = path.resolve().relative_to(Path(root).resolve()).as_posix()
    except ValueError:
        relative = path.name
    problems = []
    extension = path.suffix.lower()
    if extension in BLOCKED_EXTENSIONS:
        problems.append(f"blocked file type {extension}")
    try:
        size = path.stat().st_size
        with path.open("rb") as stream:
            head = stream.read(8192)
    except OSError as error:
        return problems + [f"cannot be read ({error})"]
    for prefix, what in MAGIC:
        if head.startswith(prefix):
            problems.append(f"starts like {what}")
    text = is_text(head)
    if not text and head[:2] in ZLIB_HEADERS:
        problems.append("starts like a zlib stream and is not text")
    if not text and relative not in ALLOWED_IMAGES:
        problems.append("is not a text file")
    if extension == ".json" and relative not in ALLOWED_DATA:
        problems.append("is a JSON data file that is not on the allow-list in tools/check_no_game_data.py")
    if relative in ALLOWED_DATA and size > ALLOWED_DATA[relative][1]:
        problems.append(f"is {size:,} bytes, more than the {ALLOWED_DATA[relative][1]:,} its allow-list entry permits")
    if size > SIZE_LIMIT and relative not in ALLOWED_DATA:
        problems.append(f"is {size:,} bytes, over the {SIZE_LIMIT:,} limit, and is not on the allow-list")
    return problems


def repository_files(root=ROOT):
    for folder, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            yield Path(folder) / name


def check(files=None, root=ROOT):
    """[(path, reason), ...] for everything that fails. `files` defaults to every file under `root`."""
    found = []
    for path in (files if files is not None else repository_files(root)):
        for reason in check_file(path, root):
            found.append((str(path), reason))
    return found


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    files = [Path(a) for a in args] if args else None
    problems = check(files)
    if problems:
        print("Game data or other disallowed files found:")
        for path, reason in problems:
            print(f"  {path}: {reason}")
        print("\nLineup's repository must never hold game files or anything made from them. See CONTRIBUTING.md.")
        return 1
    print(f"No game data found ({'the given files' if files else 'whole repository'} checked).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
