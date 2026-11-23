# Troubleshooting

Run commands from the repository folder. On Windows you can use `Launch.bat` instead of `python lineup.py`; on
Linux `./launch.sh`; on macOS `./launch.sh` or `Launch.command`.

## "No game folder found" or the wrong copy is chosen

Lineup looks two folders deep in the usual places for a folder that holds `MicrosoftGame.Config` and `packs.rpf`.
List what it finds with `python lineup.py --find`. Point it at your copy explicitly with
`python lineup.py --game "PATH"`, or choose it in a dialog with `python lineup.py --pick` (needs tkinter, which is
not installed on every system). The path is saved after the first successful check.

The archives must already be readable on disk as ordinary files. Lineup does not unpack, decrypt or unlock anything.

## "Lineup's indexes are tied to game version 1.0.2.1"

`python lineup.py --check` lists every file that is missing or has a different size, and the game version it
read. Lineup supports 1.0.2.1 only: it finds data by its position in the archives of that version. Another version
will most likely stop with a message about members it cannot find. `--force` continues anyway; results can be
incomplete or wrong.

## The build stopped

The line that starts with `[n/8]` names the stage. Fix the cause (free disk space, a locked file) and run the same
command again: finished stages are kept. To run one stage again and keep a log:

    python lineup.py --build --stages characters > build.log 2>&1

`--rebuild index,names` forces stages to run again together with everything that reads from them.
`python lineup.py --status` shows what is built.

## "The index stage did not find members the character code needs"

The copy is not version 1.0.2.1, or an archive is damaged. Run `python lineup.py --check`.

## Not enough free space

A build needs about 3 GB in the work folder (0.9 GB of indexes and databases, the rest is room for the cache). Use
another drive with `--work PATH`.

## "Port N is in use by another program" or all ports 8910-8919 are in use

Pick another port: `python lineup.py --port 8930`. If Lineup is already running for the same game folder it opens
the page instead of starting a second server. Stop a running server with Ctrl+C in its window.

## Textures decode slowly

Some textures are BCPack-coded and decode much faster with the optional C helpers. They are compiled at the end of
every run when a compiler is found: Visual Studio Build Tools (the C++ workload) on Windows, `gcc` or `clang`
(Xcode command line tools on macOS, `sudo apt install build-essential` on Debian and Ubuntu) elsewhere. Without
one Lineup prints that textures will decode more slowly and carries on. `python engine/native_build.py --force`
tries again and shows the compiler's error.

## "Lineup needs numpy and Pillow"

The launchers install them into a private `.venv`. Without a launcher: `python -m pip install numpy Pillow`.

## Some textures are missing or look wrong

A model's materials name textures by hash. Some names are not in any `.ttpf` pack or embedded texture dictionary of
the archives Lineup reads, so those slots show "not in local packs". Textures that pass through BCPack entries
that are not fully confirmed can show small errors ([bcpack.md](bcpack.md)).

## Starting again

Everything Lineup builds is in the work folder and can be deleted. `python lineup.py --status` prints its
location. Delete the folder and run Lineup again to rebuild. To forget the saved game and work folders, delete the
settings file in your user data folder (see [how-it-works.md](how-it-works.md)). Deleting `.venv` makes the
launcher create it again.

## Reporting a problem

Use the bug report form. Never attach game files, extracted assets, screenshots of extracted assets or databases
from the work folder. Console text is enough.
