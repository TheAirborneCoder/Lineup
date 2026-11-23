# SPDX-License-Identifier: GPL-3.0-or-later
"""Index every smh strand-hair groom in the local archives into LIBRARY/grooms.sqlite3.

    python engine/groom_index.py                     # packs.rpf, data.rpf and the pack/* archives
    python engine/groom_index.py packs.rpf           # chosen archives only (others keep their rows)

One row per groom LOD resource (table lod) and one per asset (table asset): an asset is a LOD chain,
3-4 neighbouring resources whose guide count falls by about 0.6 per level. Some chains are stored split
(LOD 0, then another asset, then LODs 1-3); the two halves are joined when their boxes, node counts and
flags agree.
"""
import json
import sqlite3
import sys
import time
from contextlib import closing

import smh_groom  # noqa: E402
from paths import GAME, LIBRARY

DB = LIBRARY / "grooms.sqlite3"
SKIP = ("_bvh4", "_script", "core_licenses")


def default_archives():
    names = ["packs.rpf", "data.rpf"]
    names += sorted(p.relative_to(GAME).as_posix() for p in (GAME / "pack").glob("*/*.rpf")
                    if not any(s in p.name for s in SKIP))
    return [n for n in names if (GAME / n).exists()]


def classify(box, longest):
    """Coarse placement class from the stored box (skeleton-root space, Z up, metres). A heuristic label."""
    (x0, y0, z0), (x1, y1, z1) = box
    width, height = x1 - x0, z1 - z0
    zc = (z0 + z1) / 2
    if abs(x0 + x1) < 0.08 and width < 0.34 and 0.44 < zc < 0.9 and z1 < 1.05 and y0 > -0.5:
        if z1 > 0.745 or height > 0.24:
            return "Scalp hair"
        if height < 0.075 and zc > 0.62:
            return "Brows and lashes"
        return "Facial hair"
    if width < 0.9 and -1.05 < z0 and z1 < 0.52 and longest < 0.08 and abs(x0 + x1) < 0.12 and y1 - y0 < 0.45:
        return "Body hair"
    return "Fur and other"


def read_archive(name):
    src = smh_groom.open_archive(name)
    rows = []
    for offset in smh_groom.scan(src):
        row = {"archive": name, "offset": offset, "error": None}
        try:
            g = smh_groom.read(src, offset, name)
            seg = float(g.length.max())
            row.update(guides=g.guides, ppg=g.nodes_per_guide, tris=g.triangles_n, flags=g.flags,
                       s0=int(g.strand_totals[0]), s1=int(g.strand_totals[1]), bytes=len(g.buffer),
                       box=g.box.astype(float).round(5).tolist(), longest=round(seg, 5),
                       regions=sorted(int(r) for r in set(g.tri_region.tolist())))
        except (smh_groom.GroomError, ValueError, IndexError) as exc:
            row["error"] = str(exc)
        rows.append(row)
    return rows


def _same_asset(a, b):
    if a["ppg"] != b["ppg"] or a["flags"] != b["flags"] or b["guides"] >= a["guides"]:
        return False
    if max(abs(p - q) for u, v in zip(a["box"], b["box"]) for p, q in zip(u, v)) > 0.03:
        return False
    return abs(a["s0"] - b["s0"]) <= max(12, 0.05 * a["s0"]) and abs(a["s1"] - b["s1"]) <= max(12, 0.05 * a["s1"])


def chains(rows):
    ok = [r for r in rows if not r["error"]]
    runs, cur = [], []
    for r in ok:
        if cur and _same_asset(cur[-1], r) and len(cur) < 8:
            cur.append(r)
        else:
            if cur:
                runs.append(cur)
            cur = [r]
    if cur:
        runs.append(cur)
    assets, used = [], set()
    for i, run in enumerate(runs):
        if i in used:
            continue
        full = list(run)
        # A split chain continues one to three runs later.
        for j in (i + 2, i + 3, i + 1):
            if j < len(runs) and j not in used and len(full) < 4 and _same_asset(full[-1], runs[j][0]):
                full += runs[j]
                used.add(j)
                break
        assets.append(full)
    return assets


def build(archives):
    with closing(sqlite3.connect(DB)) as db:
        db.executescript("""
            create table if not exists asset(id integer primary key, archive text, offset integer, ord integer,
                lods integer, class text, guides integer, nodes_per_guide integer, triangles integer,
                strands0 integer, strands1 integer, box text, longest real, bytes integer, regions text,
                name text, name_basis text, hint text);
            create table if not exists lod(id integer primary key, asset integer, lod integer, archive text,
                offset integer, guides integer, nodes_per_guide integer, triangles integer, flags integer,
                strands0 integer, strands1 integer, bytes integer, box text, error text);
            create index if not exists lod_asset on lod(asset, lod);
            create index if not exists asset_class on asset(class, archive, ord);
        """)
        for name in archives:
            start = time.monotonic()
            rows = read_archive(name)
            assets = chains(rows)
            with db:
                db.execute("delete from lod where archive=?", (name,))
                db.execute("delete from asset where archive=?", (name,))
                for order, full in enumerate(assets):
                    top = full[0]
                    cur = db.execute(
                        "insert into asset(archive,offset,ord,lods,class,guides,nodes_per_guide,triangles,strands0,"
                        "strands1,box,longest,bytes,regions) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (name, top["offset"], order, len(full), classify(top["box"], top["longest"]), top["guides"],
                         top["ppg"], top["tris"], top["s0"], top["s1"], json.dumps(top["box"]), top["longest"],
                         sum(r["bytes"] for r in full), json.dumps(top["regions"])))
                    for level, r in enumerate(full):
                        db.execute(
                            "insert into lod(asset,lod,archive,offset,guides,nodes_per_guide,triangles,flags,strands0,"
                            "strands1,bytes,box) values(?,?,?,?,?,?,?,?,?,?,?,?)",
                            (cur.lastrowid, level, name, r["offset"], r["guides"], r["ppg"], r["tris"], r["flags"],
                             r["s0"], r["s1"], r["bytes"], json.dumps(r["box"])))
                for r in rows:
                    if r["error"]:
                        db.execute("insert into lod(asset,lod,archive,offset,error) values(null,null,?,?,?)",
                                   (name, r["offset"], r["error"]))
            failed = sum(1 for r in rows if r["error"])
            print(f"{name}: {len(rows)} groom LOD resources, {len(assets)} assets, {failed} unreadable, "
                  f"{time.monotonic() - start:.0f} s", flush=True)
        total = db.execute("select count(*), sum(lods) from asset").fetchone()
        by_class = db.execute("select class, count(*) from asset group by class order by 2 desc").fetchall()
    print(f"Index: {total[0]} assets, {total[1]} LODs. " + ", ".join(f"{c} {n}" for c, n in by_class))
    # Rebuilding an archive's rows drops its names: link them again.
    import groom_names
    groom_names.main()


if __name__ == "__main__":
    build(sys.argv[1:] or default_archives())
