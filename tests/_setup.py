# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared set-up for the unit tests: put engine/ on sys.path and point Lineup at throwaway folders.

None of the tests needs the game. Import this module before any engine module."""
import atexit
import pathlib
import shutil
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parents[1]
ENGINE = REPO / "engine"
for folder in (str(ENGINE), str(REPO / "tools")):
    if folder not in sys.path:
        sys.path.insert(0, folder)

import paths  # noqa: E402

SCRATCH = pathlib.Path(tempfile.mkdtemp(prefix="lineup-tests-"))
atexit.register(shutil.rmtree, SCRATCH, ignore_errors=True)
GAME = SCRATCH / "game"
WORK = SCRATCH / "work"
GAME.mkdir()
paths.configure(GAME, WORK)
