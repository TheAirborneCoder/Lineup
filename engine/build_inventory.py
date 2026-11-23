# SPDX-License-Identifier: GPL-3.0-or-later
"""Build the model inventory: one row per named model resource in the drawable index (drawable_index.py).

For each entry this reads the CPU system data only: drawable box, LOD levels, model/geometry counts,
materials (shader hash) and their 64-bit texture references. Geometry is decoded later on demand.
Every entry gets a row; read failures keep their error text.
"""
import csv
import json
import os
import sqlite3
import struct
import sys
import time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from paths import INDEX, LIBRARY

OUT = LIBRARY / "inventory.build.sqlite3"


def sources():
    """(archive, member, kind) -> [(name, hash, root, source)] from the drawable index."""
    jobs = defaultdict(list)
    with (INDEX / "drawables.csv").open(newline="", encoding="utf-8") as f:
        for x in csv.DictReader(f):
            jobs[(x["archive"], int(x["member"], 16), x["kind"])].append(
                (x["name"], int(x["hash"], 16), int(x["root"], 16), x["source"]))
    return jobs


def work(batch):
    import model_decode as m
    d = m.d
    out = []
    for (arc, member, kind), entries in batch:
        shared, shared_error = None, None
        if kind == "dictionary":
            try:
                system, start, listed = m.read_dictionary(m.archive(arc), member)
                shared = [system, start, {(r, n) for _, r, n in listed}]
            except Exception as exc:
                shared_error = f"{type(exc).__name__}: {exc}"[:400]
        for name, h, root, origin in entries:
            row = dict(name=name, hash=h, archive=arc, member=member, root=root, kind=kind, source=origin)
            try:
                src = m.archive(arc)
                if kind == "dictionary":
                    if shared is None:
                        raise ValueError(shared_error)
                    system, start, listed = shared
                    if (root, name) not in listed:
                        raise ValueError("dictionary entry differs from the index")
                    froot = root
                else:
                    _, system, start, froot, actual = m.read_resource(arc, member, root, name, kind)
                    if actual != name:
                        row.update(name=actual, hash=d._name_hash(actual), indexedName=name)
                (parts, mats), system, start = m.with_pages(
                    lambda s, _: (d.model_geometry(s, froot), m.shader_refs(s, froot)), src, system, start)
                if kind == "dictionary":
                    shared[0], shared[1] = system, start   # later entries reuse the loaded pages
                box = [struct.unpack_from("<3f", system, froot + b) for b in (48, 64)]
                row.update(root=froot, box=json.dumps([list(map(float, v)) for v in box]),
                           lods=json.dumps(m.lod_levels(system, froot)), models=len({p[0] for p in parts}),
                           geometries=len(parts), materials=json.dumps(mats), status="indexed",
                           vbytes=sum(v[1] for *_, v, i in parts), ibytes=sum(i[1] for *_, v, i in parts))
            except Exception as exc:
                row.update(status="read_failed", error=f"{type(exc).__name__}: {exc}"[:400])
            out.append(row)
    return out


def main():
    jobs = sorted(sources().items(), key=lambda kv: (kv[0][0], kv[0][1]))
    append = "--append-missing" in sys.argv
    retry = "--retry-failed" in sys.argv
    if append or retry:
        db = sqlite3.connect(OUT)
        if retry:
            db.execute("delete from model where status != 'indexed'")
            db.commit()
        have = set(db.execute("select archive, member, root from model").fetchall())
        filtered = []
        for key, entries in jobs:
            arc, member, _ = key
            missing = [e for e in entries if (arc, member, e[2]) not in have]
            if missing:
                filtered.append((key, missing))
        jobs = filtered
    total = sum(len(v) for _, v in jobs)
    print("entries", total, "members", len(jobs), flush=True)
    if not (append or retry):
        OUT.unlink(missing_ok=True)
        db = sqlite3.connect(OUT)
    db.executescript("""
        pragma journal_mode=off; pragma synchronous=off;
        create table if not exists model(id integer primary key, name text, hash integer, archive text, member integer,
            root integer, kind text, source text, status text, error text, box text, lods text,
            models integer, geometries integer, materials text, vbytes integer, ibytes integer);
    """)
    batches = [jobs[i:i + 64] for i in range(0, len(jobs), 64)]
    done, t0 = 0, time.time()
    cols = ("name", "hash", "archive", "member", "root", "kind", "source", "status", "error", "box", "lods",
            "models", "geometries", "materials", "vbytes", "ibytes")
    # A few workers reading in member order beat many random readers on a hard disk.
    workers = int(os.environ.get("LINEUP_WORKERS") or min(8, os.cpu_count() or 2))
    with ProcessPoolExecutor(max_workers=workers) as pool:
        for rows in pool.map(work, batches, chunksize=4):
            db.executemany(f"insert into model({','.join(cols)}) values ({','.join('?' * len(cols))})",
                           [tuple(r.get(c) for c in cols) for r in rows])
            done += len(rows)
            if done % 20000 < len(rows):
                db.commit()
                print(done, "of", total, f"{time.time() - t0:.0f}s", flush=True)
    db.commit()
    print(dict(db.execute("select status,count(*) from model group by 1")), flush=True)
    db.close()


if __name__ == "__main__":
    main()
