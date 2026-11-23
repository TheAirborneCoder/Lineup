# SPDX-License-Identifier: GPL-3.0-or-later
"""List every named model resource in the character archives: WORK/index/drawables.csv.

Two passes per archive, both read-only:

  1. scan_packages.find_file  looks at every 16-byte aligned resource start for a dictionary, a standalone
                              drawable (.#dr name) or a fragment (.#ft name);
  2. resource_pages.discover  walks the verified span index for resources the first pass does not recognise.
                              This is where the master body rigs (ambient_femalebody, ambient_malebody, ...)
                              come from: their fragments keep the drawable behind a wider pointer range.

Rows: archive, member offset, drawable root, name hash, name, kind, source.
"""
import csv
import json
import os
from collections import Counter

import archive_index
import resource_pages
import scan_packages
from paths import GAME, INDEX
from script_tree import _name_hash

OUT = INDEX / "drawables.csv"
RECEIPT = INDEX / "drawables.json"
VERSION = 1
SKIP = ("_bvh4", "_script", "core_licenses")


def model_archives():
    """packs.rpf holds the characters; the content packs add a few story and gore pieces."""
    names = ["packs.rpf"] if (GAME / "packs.rpf").is_file() else []
    names += sorted(p.relative_to(GAME).as_posix() for p in (GAME / "pack").glob("*/*.rpf")
                    if not any(s in p.name for s in SKIP))
    return names


def _stamps(names):
    return {n: [(GAME / n).stat().st_size, (GAME / n).stat().st_mtime_ns] for n in names}


def discover_archive(name, seen):
    """Drawables found through the span index that the first pass did not list."""
    src = resource_pages.source(GAME / name)
    rows = []
    for start, _stored, size, _adler, first in archive_index.read_spans(name):
        if first != 0 or start < 16 or size < 64:
            continue
        member = start - 16
        try:
            owners = resource_pages.discover(src, member)
        except resource_pages.NATIVE_ERRORS:
            continue
        for root, text, kind in owners:
            if root is None or (member, root) in seen:
                continue
            seen.add((member, root))
            rows.append((name, hex(member), hex(root), f"{_name_hash(text):08x}", text, kind, "span scan"))
    return rows


def build(progress=None):
    names = model_archives()
    stamps = _stamps(names)
    if os.environ.get("LINEUP_RESCAN") != "1" and OUT.is_file() and RECEIPT.is_file():
        try:
            old = json.loads(RECEIPT.read_text("utf-8"))
            if old.get("version") == VERSION and old.get("archives") == stamps:
                return old
        except ValueError:
            pass
    counts = {}
    tmp = OUT.with_suffix(".pending")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(("archive", "member", "root", "hash", "name", "kind", "source"))
        for i, name in enumerate(names):
            if progress:
                progress(i, len(names), name)
            first = []

            class Collect:
                @staticmethod
                def writerow(row):
                    first.append(tuple(row))

            report = Counter(scan_packages.find_file(GAME / name, Collect))
            seen = {(int(r[1], 16), int(r[2], 16)) for r in first}
            for r in first:
                w.writerow((*r, "package scan"))
            extra = discover_archive(name, seen)
            w.writerows(extra)
            report["span_scan"] = len(extra)
            counts[name] = dict(report)
            f.flush()
    tmp.replace(OUT)
    result = {"version": VERSION, "archives": stamps, "counts": counts}
    RECEIPT.write_text(json.dumps(result, indent=1), "utf-8")
    return result


if __name__ == "__main__":
    print(json.dumps(build()["counts"], indent=1))
