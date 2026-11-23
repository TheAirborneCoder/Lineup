#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Import every engine module against a throwaway work folder: catches missing names, bad paths and syntax errors
without needing the game. Prints one line per failure; exit status 1 if any module fails.

    python tests/imports.py
"""
import importlib
import pathlib
import sys
import tempfile

ENGINE = pathlib.Path(__file__).resolve().parents[1] / "engine"
sys.path.insert(0, str(ENGINE))

import paths  # noqa: E402

with tempfile.TemporaryDirectory() as scratch:
    paths.configure(pathlib.Path(scratch) / "game", pathlib.Path(scratch) / "work")
    failed = 0
    for module in sorted(p.stem for p in ENGINE.glob("*.py")):
        try:
            importlib.import_module(module)
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {module}: {type(exc).__name__}: {exc}")
    print(f"{failed} of {len(list(ENGINE.glob('*.py')))} modules failed to import")
sys.exit(1 if failed else 0)
