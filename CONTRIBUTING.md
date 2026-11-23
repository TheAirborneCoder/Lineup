# Contributing to Lineup

Thank you for helping. Three rules come first; they are what keep this project safe to publish.

## The three rules

1. **Never add game data. Not to the repository, not to an issue, not to a pull request.**
   That includes: game archives (`.rpf`, `.ttpf`, `.xvd`, `.msixvc` ...), anything extracted from them (`.pso`,
   `.xml`, textures, models, audio, video), shader binaries and shader disassembly listings, databases or caches
   that Lineup built (`.sqlite3`, `.glb`, `.png`, `.dds`, the contents of the work folder), logs that quote game
   data, and screenshots or videos of extracted models, textures or maps. A few bytes of a file's header, or the
   name and offset of a field, are fine in documentation and comments when they explain a format.
   `python tools/check_no_game_data.py` checks the repository, and CI runs it on every push and pull request.
2. **No keys, no decryption, no protection workarounds, and no links to them.** Lineup works on files that are
   already readable on disk. It does not unpack, unlock or circumvent anything, and it does not explain how to get
   the game or its files. Issues and pull requests that do are closed.
3. **Do not copy from other projects' source** unless its licence is compatible with GPL-3.0-or-later, you
   say where it came from, and you keep its notices. When in doubt, write it yourself from the file formats.

## Working on the code

- Python 3.10 or newer. Lineup needs only **numpy** and **Pillow**; do not add a dependency.
- Use LF line endings (`Launch.bat` is the one CRLF file). `.gitattributes` handles this.
- Names, comments and messages are for readers who have not seen the project: plain language, no hype.
- Set the code up with `python -m venv .venv`, activate it, then `python -m pip install -r requirements.txt pyflakes`.
- Before you open a pull request run

      python -m pyflakes engine tools tests lineup.py
      python -m unittest discover -s tests -p "test_*.py"
      python tools/check_no_game_data.py

  The unit tests need no game files. `tests/api_smoke.py` and `tests/page_check.py` need a built work folder and a
  running server (`python lineup.py --no-browser`); run them when you change the server, the viewer or a decoder.
- Say in the pull request what you tested and on which operating system. Windows 10/11 is the only system Lineup has
  been run on so far; reports and fixes for Linux and macOS are especially welcome. If you did not run something,
  say so.
- Keep changes small and say why. Format knowledge belongs in the docstring of the module that uses it and, if it
  helps readers, in `docs/formats.md`.
- Every Python file starts with `# SPDX-License-Identifier: GPL-3.0-or-later`. Contributions are accepted under that
  licence.

## Reporting a problem

Use the bug report form. Give your operating system, Python version, the Lineup commit, the stage that failed
(`python lineup.py --status` lists them) and the console text. Do not attach game files, extracted assets,
screenshots of extracted assets or generated databases.
