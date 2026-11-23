## What changed

<!-- One or two sentences. Say what a user will notice. -->

## What was tested, and on which operating system

<!-- For example: "python -m unittest discover -s tests on Windows 11, Python 3.12; ran the viewer against my own
copy of the game and opened three characters." Say what you did not test. -->

## Checklist

- [ ] No game data is included: no archives, extracted files, textures, models, databases, caches, or screenshots of
      extracted assets (`python tools/check_no_game_data.py` passes).
- [ ] No keys, decryption code or protection workarounds, and no links to them.
- [ ] Nothing is copied from another project's source without a compatible licence and attribution.
- [ ] Only numpy and Pillow are needed; `python -m pyflakes engine tools tests lineup.py` is clean.
