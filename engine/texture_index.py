# SPDX-License-Identifier: GPL-3.0-or-later
"""Index every texture in every local .ttpf pack by its 64-bit name hash (the key model materials use)."""
import json
import sqlite3

from ttpf_format import TtpfArchive  # noqa: E402
from paths import GAME, LIBRARY

OUT = LIBRARY / "textures.sqlite3"


def packs():
    found = sorted((GAME / "data/ttdb").rglob("*.ttpf"))
    for base in sorted((GAME / "pack").glob("*/data/ttdb")):
        found += sorted(base.rglob("*.ttpf"))
    return found


def main():
    tmp = OUT.with_suffix(".building.sqlite3")
    tmp.unlink(missing_ok=True)
    db = sqlite3.connect(tmp)
    db.executescript("""
        create table pack(id integer primary key, path text unique, area text, package text, textures integer);
        create table texture(hash text, pack integer, idx integer, width integer, height integer, format text,
            dimension text, slices integer, mips integer, mip_codecs text);
    """)
    total = 0
    for pid, path in enumerate(packs()):
        rel = path.relative_to(GAME).as_posix()
        package = rel.split("/data/ttdb/")[0] if rel.startswith("pack/") else "base"
        area = "loose" if path.parent.name == "ttdb" else path.parent.name
        arc = TtpfArchive(path)
        try:
            rows = []
            for t in arc.textures:
                codecs = []
                for level in range(len(t["mips"])):
                    try:
                        codecs.append("+".join(sorted(set(arc.mip_codecs(t, level)))))
                    except Exception as exc:  # unlocated mip
                        codecs.append("unavailable:" + type(exc).__name__)
                rows.append((t["nameHash"], pid, t["index"], t["width"], t["height"], t["format"],
                             t.get("dimension", "2D"), t.get("slices", 1), len(t["mips"]), json.dumps(codecs)))
            db.execute("insert into pack values (?,?,?,?,?)", (pid, rel, area, package, len(rows)))
            db.executemany("insert into texture values (?,?,?,?,?,?,?,?,?,?)", rows)
            total += len(rows)
            print(rel, len(rows), flush=True)
        finally:
            arc.close()
    db.execute("create index texture_hash on texture(hash)")
    db.commit()
    dupes = db.execute("select count(*) from (select hash from texture group by hash having count(*)>1)").fetchone()[0]
    db.close()
    tmp.replace(OUT)
    print("textures", total, "hashes in more than one pack", dupes)


if __name__ == "__main__":
    main()
