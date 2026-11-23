#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Lineup: a local character viewer for your own copy of the game.

    python lineup.py                        find the game, check it, build what is missing, open the viewer
    python lineup.py --game PATH [--work PATH]
    python lineup.py --find                 list copies found on this machine
    python lineup.py --pick                 choose the game folder in a folder dialog (tkinter, optional)
    python lineup.py --check                check the copy, build nothing
    python lineup.py --build                build only, do not open the viewer
    python lineup.py --stages index,drawables
    python lineup.py --rebuild [stage,...]  force stages to run again
    python lineup.py --status               what is built, how big the work folder is
    python lineup.py --port N --no-browser
    python lineup.py --force                continue despite a version mismatch

Nothing is written to the game folder. Everything Lineup builds goes to the work folder.
"""
import argparse
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENGINE = HERE / "engine"
if str(ENGINE) not in sys.path:
    sys.path.insert(0, str(ENGINE))

MINIMUM_PYTHON = (3, 10)


class Stop(Exception):
    """An expected problem; the message is for the user."""


def parse_args(argv=None):
    p = argparse.ArgumentParser(prog="lineup.py", description="Local character viewer for your own copy of the game.",
                                formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    p.add_argument("--game", metavar="PATH", help="the game folder (holds MicrosoftGame.Config and packs.rpf)")
    p.add_argument("--work", metavar="PATH", help="where Lineup keeps what it builds (default: your user data folder)")
    p.add_argument("--find", action="store_true", help="list copies of the game found on this machine")
    p.add_argument("--pick", action="store_true", help="choose the game folder in a folder dialog")
    p.add_argument("--check", action="store_true", help="check the game folder and stop")
    p.add_argument("--build", action="store_true", help="build what is missing and stop")
    p.add_argument("--stages", metavar="LIST", help="comma-separated stages to build (plus what they need)")
    p.add_argument("--rebuild", nargs="?", const="all", metavar="LIST", help="run stages again (default: all)")
    p.add_argument("--status", action="store_true", help="show what is built and how big the work folder is")
    p.add_argument("--port", type=int, metavar="N", help="port for the viewer (default: first free in 8910-8919)")
    p.add_argument("--no-browser", action="store_true", help="do not open a browser window")
    p.add_argument("--force", action="store_true", help="continue despite a version mismatch or low disk space")
    return p.parse_args(argv)


def need_packages():
    missing = []
    for module, package in (("numpy", "numpy"), ("PIL", "Pillow")):
        try:
            __import__(module)
        except ImportError:
            missing.append(package)
    if missing:
        raise Stop(f"Lineup needs {' and '.join(missing)}. Install with:\n    "
                   f"{Path(sys.executable).name} -m pip install {' '.join(missing)}")


def interactive():
    return sys.stdin is not None and sys.stdin.isatty()


def ask_path():
    if not interactive():
        raise Stop("No game folder found. Run again with --game PATH (the folder that holds "
                   "MicrosoftGame.Config and packs.rpf).")
    import gamefinder
    while True:
        text = input("Paste the path of the game folder (empty to quit): ").strip().strip('"')
        if not text:
            raise Stop("No game folder chosen.")
        path = Path(text).expanduser()
        if gamefinder.is_game_folder(path):
            return path
        print(f"  {path} does not hold MicrosoftGame.Config and packs.rpf.")


def choose(found):
    if len(found) == 1:
        print(f"Found the game in {found[0]}")
        return found[0]
    print("Found several copies:")
    for i, path in enumerate(found, 1):
        print(f"  {i}. {path}")
    if not interactive():
        raise Stop("Several copies found; run again with --game PATH to choose one.")
    while True:
        text = input(f"Which one? [1-{len(found)}] ").strip()
        if text.isdigit() and 1 <= int(text) <= len(found):
            return found[int(text) - 1]


def pick_folder():
    try:
        import tkinter
        from tkinter import filedialog
    except ImportError:
        raise Stop("No folder dialog available (tkinter is not installed). Use --game PATH.") from None
    try:
        root = tkinter.Tk()
    except Exception:
        raise Stop("No display for a folder dialog. Use --game PATH.") from None
    root.withdraw()
    folder = filedialog.askdirectory(title="Choose the game folder (it holds MicrosoftGame.Config and packs.rpf)")
    root.destroy()
    if not folder:
        raise Stop("No folder chosen.")
    return Path(folder)


def resolve_game(args, paths):
    import gamefinder
    if args.pick:
        return pick_folder()
    if args.game:
        return Path(args.game).expanduser()
    for candidate in (os.environ.get("LINEUP_GAME"), paths.load_settings().get("game")):
        if candidate and gamefinder.is_game_folder(candidate):
            return Path(candidate)
    print("Looking for the game (two folders deep in the usual places) ...", flush=True)
    found = gamefinder.find()
    return choose(found) if found else ask_path()


def show_check(game):
    import gamefinder
    ok, problems = gamefinder.check(game)
    version = gamefinder.game_version(game)
    print(f"Game folder: {game}")
    print(f"Game version: {version or 'unknown'} (Lineup supports {gamefinder.SUPPORTED_VERSION})")
    if ok:
        print("The game files match the supported version.")
    else:
        print("Problems:")
        for line in problems:
            print(f"  - {line}")
    return ok, problems


def print_status(build, paths):
    rows, sizes = build.status()
    print(f"Work folder: {paths.WORK}")
    for name, title, state, seconds, finished in rows:
        when = f"  built {finished}, {build.clock(seconds)}" if finished else ""
        print(f"  {name:<11} {'ok' if state == 'done' else 'needs work (' + state + ')':<34}{when}")
    parts = ", ".join(f"{k} {v / 1e9:.2f} GB" for k, v in sizes.items())
    print(f"Work folder size: {sum(sizes.values()) / 1e9:.2f} GB" + (f" ({parts})" if parts else ""))


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")     # a console that cannot show a character must not stop the run
    args = parse_args(argv)
    if sys.version_info < MINIMUM_PYTHON:
        raise Stop(f"Lineup needs Python {MINIMUM_PYTHON[0]}.{MINIMUM_PYTHON[1]} or newer "
                   f"(this is {sys.version_info[0]}.{sys.version_info[1]}).")
    import gamefinder
    if args.find:
        found = gamefinder.find()
        print("\n".join(str(p) for p in found) if found else "No copy of the game found.")
        return 0
    need_packages()
    import paths
    game = resolve_game(args, paths)
    work = Path(args.work).expanduser() if args.work else None
    # Folders first, then every other engine module: they read the folders when they are imported.
    paths.configure(game, work)
    if not game.is_dir():
        raise Stop(f"Game folder not found: {game}")
    ok, problems = show_check(paths.GAME)
    if args.check:
        return 0 if ok else 2
    if not ok and not args.force:
        print("\nLineup's indexes are tied to game version "
              f"{gamefinder.SUPPORTED_VERSION}; other versions place their data at different offsets.\n"
              "Use the matching game files, or run with --force to try anyway (results may be incomplete).")
        return 2
    paths.save_settings(game=paths.GAME, work=paths.WORK)

    import build
    if args.status:
        print_status(build, paths)
        return 0
    stages = [s for s in (args.stages or "").split(",") if s]
    rebuild = [s for s in (args.rebuild or "").split(",") if s]
    if rebuild == ["all"]:
        rebuild = list(build.STAGE_NAMES)
    for name in stages + rebuild:
        if name not in build.STAGES:
            raise Stop(f"Unknown stage '{name}'. Stages: {', '.join(build.STAGE_NAMES)}")
    wanted = stages or None

    receipt = build.load_receipt()
    pending = [n for n in build.order(wanted) if n in rebuild or build.state(build.STAGES[n], receipt) != "done"]
    if pending:
        free = build.free_gb(paths.WORK)
        if free < build.NEEDED_GB and not args.force:
            raise Stop(f"The work folder ({paths.WORK}) needs about {build.NEEDED_GB} GB and only {free:.1f} GB is "
                       "free on that drive. Free some space or pick another drive with --work PATH "
                       "(--force skips this check).")
        print(f"Work folder: {paths.WORK} (needs about {build.NEEDED_GB} GB incl. room to browse, {free:.0f} GB free)")
        try:
            timings = build.run_stages(wanted, rebuild)
        except build.BuildError as exc:
            raise Stop(str(exc)) from None
        ran = {name: sec for name, sec in timings.items() if sec is not None}
        print(f"Build finished in {build.clock(sum(ran.values()))}"
              + (f" ({', '.join(f'{n} {build.clock(t)}' for n, t in ran.items())})" if ran else ""))
    else:
        print("Everything is built.")
    try:
        import native_build
        native_build.build()          # optional speed-up for texture decoding; never fails the run
    except Exception as exc:      # noqa: BLE001  (a locked library, a broken compiler install ...)
        print(f"Texture helpers skipped ({type(exc).__name__}: {exc}); textures will decode more slowly.")
    if args.build:
        return 0

    import server
    return server.serve(port=args.port, open_browser=not args.no_browser)


if __name__ == "__main__":
    try:
        code = main()
    except Stop as exc:
        print(f"\n{exc}", file=sys.stderr)
        code = 2
    except KeyboardInterrupt:
        print("\nStopped. Finished stages are kept; run again to continue.", file=sys.stderr)
        code = 130
    sys.exit(code)
