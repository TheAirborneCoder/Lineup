# SPDX-License-Identifier: GPL-3.0-or-later
"""Build everything the viewer reads from the game files, in stages, resumably.

Each stage has a version number and lists the stages it reads from. WORK/build.json keeps one receipt per
finished stage: the stage version, the signature of its inputs (sizes and modification times of the game files it
reads) and the output signature of every stage it depends on. A stage is skipped when all three still match;
otherwise it runs, and so does everything that reads from it.

Stage functions import the engine modules they need only when they run, so paths.configure() has always been
called by then.
"""
import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import paths

FORMAT = 1
STAGE_NAMES = ("index", "names", "drawables", "textures", "inventory", "characters", "grooms", "animations")
# Measured on a full build of game version 1.0.2.1: 0.9 GB (indexes 0.35, databases 0.52, the rest is the decode cache
# after a dozen characters). The cache grows with browsing, so the figure leaves room for it.
NEEDED_GB = 3


class BuildError(Exception):
    """An expected failure with a message the user can act on."""


def child(target, *args):
    """Run "module:function(*args)" in a fresh Python process, as the stand-alone scripts of the reference pipeline
    ran. Several modules keep module-level caches (runtime_faces reads face_presets.sqlite3 once), so a step that
    reads what an earlier step wrote must not share a process with it. The folders travel in the environment."""
    module, function = target.split(":")
    code = ("import importlib, json, sys; sys.path.insert(0, %r); sys.argv = ['lineup']; "
            "getattr(importlib.import_module(%r), %r)(*json.loads(%r))"
            % (str(paths.ENGINE), module, function, json.dumps(list(args))))
    # A fixed hash seed keeps set and dict ordering, and so tie-breaks inside the modules, the same from run to run.
    result = subprocess.run([sys.executable, "-u", "-c", code], env=dict(os.environ, PYTHONHASHSEED="0"))
    if result.returncode:
        raise BuildError(f"{target} stopped with exit code {result.returncode}; the messages above say why. "
                         "Finished stages are kept; run again to continue.")


# --- progress -------------------------------------------------------------------------------------------------


def clock(seconds):
    seconds = int(seconds)
    return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


class Progress:
    """Throttled progress lines: label, percent, elapsed. Plain lines, so a redirected log stays readable."""

    def __init__(self, label, every=15.0):
        self.label = label
        self.every = every
        self.start = time.monotonic()
        self.last = 0.0

    def __call__(self, done, total, note=""):
        now = time.monotonic()
        if now - self.last < self.every and done < total:
            return
        self.last = now
        percent = f" {100 * done / total:5.1f}%" if total else ""
        print(f"    {self.label}{percent}{'  ' + note if note else ''}  [{clock(now - self.start)}]", flush=True)


@contextlib.contextmanager
def argv(*args):
    """Some stage entry points read sys.argv; give them a clean one."""
    saved = sys.argv
    sys.argv = ["lineup", *args]
    try:
        yield
    finally:
        sys.argv = saved


# --- signatures -----------------------------------------------------------------------------------------------


def stamp_of(path):
    try:
        st = Path(path).stat()
    except OSError:
        return None
    return [st.st_size, st.st_mtime_ns]


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()[:20]


def archive_stamps(names):
    return {n: stamp_of(paths.GAME / n) for n in names}


def ttpf_files():
    base = sorted((paths.GAME / "data" / "ttdb").rglob("*.ttpf"))
    for folder in sorted((paths.GAME / "pack").glob("*/data/ttdb")):
        base += sorted(folder.rglob("*.ttpf"))
    return base


# --- stage bodies ---------------------------------------------------------------------------------------------


def index_archives():
    import archive_index
    return archive_index.character_archives()


def model_archives():
    import drawable_index
    return drawable_index.model_archives()


def groom_archives():
    import groom_index
    return groom_index.default_archives()


def animation_archives():
    import anim_service
    return anim_service.ANIM_ARCHIVES


# Members the character modules open by their archive offset (game version 1.0.2.1). A stage that finds one
# missing would otherwise produce empty tables without complaint.
REQUIRED_MEMBERS = (
    "packs.rpf/00011ccb2000.pso", "packs.rpf/00011cfbf000.pso", "packs.rpf/00011d1ff000.pso",
    "packs.rpf/00011dab3000.pso", "packs.rpf/00011cb92000.pso", "packs.rpf/00011cb99000.pso",
    "packs.rpf/00011cba4000.pso", "packs.rpf/00011cc90000.pso", "anim.rpf/000106153000.pso",
    "data.rpf/00001ae00000.pso", "data.rpf/00001b95b000.pso", "data.rpf/00001b058000.pso",
    "data.rpf/00001b1a4000.pso",
)
# Not fatal: the vehicle catalogue only feeds category names for vehicles Lineup does not show.
OPTIONAL_MEMBERS = ("data.rpf/00001c35b000.pso", "common.rpf/xml/000000449000.xml")


def check_members():
    missing = [m for m in REQUIRED_MEMBERS if not (paths.INDEX / m).is_file()]
    if missing:
        raise BuildError("The index stage did not find members the character code needs:\n  "
                         + "\n  ".join(missing)
                         + "\nThe game files may not be version 1.0.2.1 (run: python lineup.py --check).")
    for m in OPTIONAL_MEMBERS:
        if not (paths.INDEX / m).is_file():
            print(f"    note: {m} not found; vehicle names will be missing (not used by the viewer)")


def run_index(ctx):
    import archive_index
    from archive_index import scan_members, scan_spans, scan_xml, supplement_xml
    names = archive_index.character_archives()
    for number, name in enumerate(names, 1):
        path = paths.GAME / name
        size = path.stat().st_size
        print(f"  [{number}/{len(names)}] {name} ({size / 1e9:.2f} GB)", flush=True)
        for stale in (paths.INDEX / name).glob("tmp*"):      # left behind when an earlier scan was interrupted
            stale.unlink(missing_ok=True)
        for step, scan in (("zlib spans", scan_spans), ("PSIN members", scan_members), ("XML members", scan_xml)):
            t0 = time.monotonic()
            scan(path, name, progress=Progress(f"{name}: {step}"))
            print(f"    {name}: {step} done in {clock(time.monotonic() - t0)}", flush=True)
        supplement_xml(path, name)
    check_members()


def run_names(ctx):
    import names as names_module
    return names_module.build(progress=Progress("names", every=10.0))


def run_drawables(ctx):
    import drawable_index

    def progress(i, n, name):
        print(f"  [{i + 1}/{n}] {name}", flush=True)
    drawable_index.build(progress=progress)


def run_textures(ctx):
    import native_textures
    import texture_index
    texture_index.main()
    for name in model_archives():
        t0 = time.monotonic()
        result = native_textures.build_pack(name)
        print(f"  {name}: {result.get('rows', 0)} embedded textures in {clock(time.monotonic() - t0)}", flush=True)


def run_inventory(ctx):
    child("build_inventory:main")
    child("classify:main")


def run_characters(ctx):
    child("face_presets:main")
    child("ped_archetypes:main")
    child("clothing_presets:build_index")
    child("classify:main")           # pass 2: ingests the ped archetype rows and the face-preset donors


def index_grooms():
    import groom_index
    groom_index.build(groom_index.default_archives())


def run_grooms(ctx):
    child("build:index_grooms")


def index_animations():
    import anim_service
    progress = Progress("animations")

    def report(*args):
        if len(args) >= 2 and isinstance(args[0], (int, float)) and isinstance(args[1], (int, float)):
            progress(args[0], args[1])
        else:
            progress(0, 0, " ".join(str(a) for a in args))
    anim_service.build_index(progress_cb=report)


def run_animations(ctx):
    child("build:index_animations")


class Stage:
    def __init__(self, name, title, run, needs=(), version=1, inputs=None, outputs=()):
        self.name, self.title, self.run = name, title, run
        self.needs, self.version = tuple(needs), version
        self.inputs = inputs or (lambda: None)
        self.outputs = outputs     # callable returning the paths that must exist for the stage to count as done


def _lib(*names):
    return lambda: [paths.LIBRARY / n for n in names]


STAGES = {s.name: s for s in (
    Stage("index", "Index the game archives", run_index, version=1,
          inputs=lambda: archive_stamps(index_archives()),
          outputs=lambda: [paths.INDEX / "packs.rpf" / "deflate-spans.bin", paths.INDEX / "packs.rpf" / "members.json"]),
    Stage("names", "Learn names from the game files", run_names, needs=("index",), version=2,
          outputs=_lib("names.json")),
    Stage("drawables", "List the model resources", run_drawables, needs=("index",), version=1,
          inputs=lambda: archive_stamps(model_archives()), outputs=lambda: [paths.INDEX / "drawables.csv"]),
    Stage("textures", "Index textures", run_textures, needs=("index",), version=1,
          inputs=lambda: {"ttpf": {p.relative_to(paths.GAME).as_posix(): stamp_of(p) for p in ttpf_files()},
                          "models": archive_stamps(model_archives())},
          outputs=_lib("textures.sqlite3")),
    Stage("inventory", "Build the model inventory", run_inventory, needs=("drawables", "textures"), version=1,
          inputs=lambda: archive_stamps(model_archives()),
          outputs=_lib("inventory.build.sqlite3", "inventory.sqlite3")),
    Stage("characters", "Characters, outfits and faces", run_characters,
          needs=("inventory", "names", "textures", "index"), version=5,
          outputs=_lib("ped_archetypes.sqlite3", "face_presets.sqlite3", "clothing_presets.sqlite3",
                       "inventory.sqlite3")),
    Stage("grooms", "Index hair, beards and fur", run_grooms, needs=("characters",), version=1,
          inputs=lambda: archive_stamps(groom_archives()),
          outputs=_lib("grooms.sqlite3")),
    Stage("animations", "Index animations", run_animations, needs=("characters", "index"), version=1,
          inputs=lambda: archive_stamps(animation_archives()),
          outputs=_lib("anim_clips.sqlite3")),
)}


# --- receipts -------------------------------------------------------------------------------------------------


def receipt_path():
    return paths.WORK / "build.json"


def load_receipt():
    try:
        value = json.loads(receipt_path().read_text("utf-8"))
        if value.get("format") == FORMAT:
            return value
    except (OSError, ValueError):
        pass
    return {"format": FORMAT, "stages": {}}


def save_receipt(value):
    paths.WORK.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=paths.WORK, delete=False, encoding="utf-8", suffix=".tmp") as f:
        json.dump(value, f, indent=1)
        name = f.name
    os.replace(name, receipt_path())


def order(wanted=None):
    """Stages in dependency order (the declared order already is one), limited to `wanted` plus what they need."""
    if wanted is None:
        return list(STAGE_NAMES)
    keep = set()

    def add(name):
        if name not in keep:
            keep.add(name)
            for need in STAGES[name].needs:
                add(need)
    for name in wanted:
        add(name)
    return [n for n in STAGE_NAMES if n in keep]


def expected(stage, receipt):
    """What a finished receipt of this stage must say right now."""
    return {"version": stage.version, "inputs": digest(stage.inputs()),
            "needs": {n: receipt["stages"].get(n, {}).get("output") for n in stage.needs}}


def state(stage, receipt):
    """'done', or the reason the stage has to run."""
    got = receipt["stages"].get(stage.name)
    if got is None:
        return "not built"
    want = expected(stage, receipt)
    if got.get("version") != want["version"]:
        return "newer stage version"
    if got.get("inputs") != want["inputs"]:
        return "game files changed"
    for n, sig in want["needs"].items():
        if sig is None or got.get("needs", {}).get(n) != sig:
            return f"{n} was rebuilt"
    missing = [p.name for p in stage.outputs() if not Path(p).exists()] if callable(stage.outputs) else []
    if missing:
        return "missing " + ", ".join(missing)
    return "done"


class Context:
    def __init__(self, stage):
        self.progress = Progress(stage.name)


def run_stages(wanted=None, rebuild=(), log=print):
    """Run the stages in order, skipping finished ones. Returns {stage: seconds or None when skipped}."""
    paths.ensure_work()
    receipt = load_receipt()
    forced = set(rebuild)
    results = {}
    names = order(wanted)
    for number, name in enumerate(names, 1):
        stage = STAGES[name]
        why = "rebuild requested" if name in forced else state(stage, receipt)
        if why == "done":
            log(f"[{number}/{len(names)}] {stage.title}: up to date")
            results[name] = None
            continue
        log(f"[{number}/{len(names)}] {stage.title} ({name}: {why})")
        receipt["stages"].pop(name, None)
        save_receipt(receipt)
        t0 = time.monotonic()
        # A forced stage must not trust the receipts of the scanners it calls.
        if name in forced:
            os.environ["LINEUP_RESCAN"] = "1"
        else:
            os.environ.pop("LINEUP_RESCAN", None)
        try:
            extra = stage.run(Context(stage))
        finally:
            os.environ.pop("LINEUP_RESCAN", None)
        seconds = time.monotonic() - t0
        want = expected(stage, receipt)
        # An output signature that is a pure function of the inputs lets an unchanged result stop the cascade.
        output = extra if isinstance(extra, str) else digest([name, want, time.time_ns() if name in forced else 0])
        receipt["stages"][name] = dict(want, output=output, seconds=round(seconds, 1),
                                       finished=time.strftime("%Y-%m-%d %H:%M:%S"))
        save_receipt(receipt)
        log(f"    {name} finished in {clock(seconds)}")
        results[name] = seconds
    return results


def folder_size(folder):
    total = 0
    for root, _dirs, files in os.walk(folder):
        for f in files:
            with contextlib.suppress(OSError):
                total += os.path.getsize(os.path.join(root, f))
    return total


def status():
    """Rows (stage, state text, seconds) and the sizes of the work folder parts."""
    receipt = load_receipt()
    rows = []
    for name in STAGE_NAMES:
        stage = STAGES[name]
        got = receipt["stages"].get(name)
        rows.append((name, stage.title, state(stage, receipt), got.get("seconds") if got else None,
                     got.get("finished") if got else None))
    sizes = {part: folder_size(getattr(paths, part.upper())) for part in ("index", "library", "cache")
             if getattr(paths, part.upper()).is_dir()}
    return rows, sizes


def free_gb(folder):
    probe = Path(folder)
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return shutil.disk_usage(probe).free / 1e9
