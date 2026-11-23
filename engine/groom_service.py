# SPDX-License-Identifier: GPL-3.0-or-later
"""Studio backend for the Grooms page: list, inspect and export smh strand-hair groom assets.

Reads LIBRARY/grooms.sqlite3 (built by groom_index.py) and decodes with smh_groom.py.
GLBs are cached under CACHE/grooms/.
"""
import sqlite3
import threading
from contextlib import closing

import smh_gen  # noqa: E402
import smh_groom  # noqa: E402
from paths import CACHE as _CACHE, LIBRARY

DB = LIBRARY / "grooms.sqlite3"
CACHE = _CACHE / "grooms"
SORTS = {"order": "archive, ord", "guides": "guides * nodes_per_guide desc", "strands": "strands0 + strands1 desc",
         "length": "longest desc", "size": "bytes desc"}
# Strands drawn per class. 0 means every strand the asset stores (the largest class holds 206,662).
STRAND_LIMITS = (500, 2000, 6000, 20000, 60000, 150000, 0)
TUBE_VERTICES = 30_000_000   # about 700 MB of tube mesh; beyond this the browser runs out of memory
PARTS = [{"index": i, "name": n} for i, n in enumerate(smh_groom.PART_NAMES)]


# Unnamed grooms whose most likely item (groom_likely.py, an estimate) is rated 90% or more.
LIKELY = "name is null and likely_p >= 0.9"


class GroomLibrary:
    def __init__(self):
        self._lock = threading.Lock()

    def db(self):
        if not DB.exists():
            raise LookupError("Groom index missing. Run: python lineup.py --build")
        db = sqlite3.connect(DB.as_uri() + "?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        return db

    def summary(self):
        with closing(self.db()) as db:
            total = db.execute("select count(*) n, sum(lods) lods, sum(strands0) s0, sum(strands1) s1, sum(bytes) b"
                               " from asset").fetchone()
            classes = [dict(r) for r in db.execute(
                "select class name, count(*) assets, sum(strands0 + strands1) strands from asset group by class"
                " order by assets desc")]
            archives = [dict(r) for r in db.execute(
                "select archive name, count(*) assets from asset group by archive order by assets desc")]
            failed = db.execute("select count(*) from lod where error is not null").fetchone()[0]
            named = db.execute("select count(*) from asset where name is not null").fetchone()[0]
            likely = db.execute(f"select count(*) from asset where {LIKELY}").fetchone()[0]
        return {"assets": total["n"], "lods": total["lods"], "strands": [total["s0"], total["s1"]],
                "bytes": total["b"], "classes": classes, "archives": archives, "unreadable": failed, "named": named,
                "likely": likely,
                "strandLimits": STRAND_LIMITS, "parts": PARTS}

    def rows(self, arg):
        """One page of grooms. likely=1 keeps unnamed grooms whose estimated item is rated 90% or more."""
        where, params = [], []
        if arg("class"):
            where.append("class=?")
            params.append(arg("class"))
        if arg("named") == "1":
            where.append("name is not null")
        if arg("likely") == "1":
            where.append(LIKELY)
        if arg("archive"):
            where.append("archive=?")
            params.append(arg("archive"))
        q = (arg("q") or "").strip().lower()
        if q:
            try:
                value = int(q, 16) if q.startswith("0x") else int(q)
            except ValueError:
                value = None
            if value is None:
                # Item name: named grooms first, then grooms whose family hint mentions it.
                where.append("(name like ? escape '\\' or hint like ? escape '\\')")
                like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
                params += [like, like]
            else:
                where.append("(id=? or offset=? or id in (select asset from lod where offset=?))")
                params += [value, value, value]
        clause = (" where " + " and ".join(where)) if where else ""
        limit = max(1, min(200, int(arg("limit", "80") or 80)))
        offset = max(0, int(arg("offset", "0") or 0))
        order = SORTS.get(arg("sort") or "order", SORTS["order"])
        with closing(self.db()) as db:
            count = db.execute("select count(*) from asset" + clause, params).fetchone()[0]
            rows = [dict(r) for r in db.execute(
                "select id, archive, offset, ord, lods, class, guides, nodes_per_guide, triangles, strands0, strands1,"
                f" longest, bytes, name, hint, likely, likely_p from asset{clause} order by {order} limit ? offset ?",
                params + [limit, offset])]
            named = db.execute("select count(*) from asset where name is not null").fetchone()[0]
        for r in rows:
            r["offset"] = hex(r["offset"])
        return {"count": count, "offset": offset, "rows": rows, "named": named}

    def _lod(self, ident, lod):
        with closing(self.db()) as db:
            asset = db.execute("select * from asset where id=?", (ident,)).fetchone()
            if not asset:
                raise LookupError("Unknown groom")
            lods = db.execute("select * from lod where asset=? order by lod", (ident,)).fetchall()
        if not 0 <= lod < len(lods):
            raise LookupError(f"Groom has no LOD {lod}")
        return asset, lods

    def item(self, ident, lod=0):
        asset, lods = self._lod(ident, lod)
        groom = smh_groom.load(lods[lod]["archive"], lods[lod]["offset"])
        info = groom.summary()
        def member(r):
            return {"id": r["id"], "offset": hex(r["offset"]), "order": r["ord"], "lods": r["lods"],
                    "guides": r["guides"], "nodesPerGuide": r["nodes_per_guide"],
                    "strands": r["strands0"] + r["strands1"], "p": r["likely_p"], "startP": r["edge_p"]}

        with closing(self.db()) as db:
            if asset["name"]:        # proven: every groom with this name
                group = [member(r) for r in db.execute("select * from asset where name=? order by archive, ord",
                                                       (asset["name"],))]
                kind = "named"
            elif asset["grp"] is not None:   # estimated: this groom's run in the best alignment
                group = [member(r) for r in db.execute("select * from asset where archive=? and grp=? order by ord",
                                                       (asset["archive"], asset["grp"]))]
                kind = "likely"
            else:
                group, kind = [member(asset)], "single"
            near = lambda order: db.execute("select * from asset where archive=? and ord=?",
                                            (asset["archive"], order)).fetchone()
            before, after = near(group[0]["order"] - 1), near(group[-1]["order"] + 1)
            neighbours = {"before": member(before) if before else None, "after": member(after) if after else None}
            prev, nxt = near(asset["ord"] - 1), near(asset["ord"] + 1)
            adjacent = {"before": member(prev) if prev else None, "after": member(nxt) if nxt else None}
        return {
            "id": asset["id"], "archive": asset["archive"], "offset": hex(asset["offset"]), "order": asset["ord"],
            "class": asset["class"], "name": asset["name"], "nameBasis": asset["name_basis"],
            "hint": asset["hint"], "likely": asset["likely"], "likelyP": asset["likely_p"], "lod": lod,
            "class1Shader": smh_gen.class1_shader(groom), "class1Mode": groom.system[0x12D] if len(groom.system) > 0x12D else None,
            "lods": [{"lod": r["lod"], "offset": hex(r["offset"]), "guides": r["guides"],
                      "nodesPerGuide": r["nodes_per_guide"], "triangles": r["triangles"],
                      "strands": [r["strands0"], r["strands1"]], "bytes": r["bytes"]} for r in lods],
            "groom": info, "parts": PARTS, "group": group, "groupKind": kind, "neighbours": neighbours, "adjacent": adjacent,
            "skin": {"bones": sorted(set(groom.arrays["bone_index"][groom.arrays["bone_weight"] > 0].tolist()))},
        }

    def glb(self, ident, lod=0, strands=6000, curves=False, class1=None, style=True):
        """class1: smh_gen_N to run for class 1 strands instead of the one the asset selects (2, 4 or 16)."""
        strands = max(0, min(10 ** 7, int(strands))) or 10 ** 7      # 0 = all
        class1 = int(class1) if class1 and int(class1) in (2, 4, 16) else None
        _, lods = self._lod(ident, lod)
        row = lods[lod]
        # Keyed by the LOD's archive offset: asset numbers change when the index is rebuilt.
        key = row["archive"].replace("/", "_").removesuffix(".rpf")
        # "sx": strands from executing the game's smh_gen shaders (smh_gen.py), when their listings are present
        # (docs/strand-shaders.md). "px": the Python port in smh_groom.strands().
        engine = "shader" if smh_gen.available() else "port"
        tag = "sx" if engine == "shader" else "px"
        path = CACHE / f"{key}.{row['offset']:x}.{strands}.{tag}{class1 or ''}{'.st' if style else ''}{'.curves' if curves else ''}.glb"
        if path.exists():
            return path.read_bytes()
        with self._lock:
            if path.exists():
                return path.read_bytes()
            groom = smh_groom.load(row["archive"], row["offset"])
            # The vertex budget only guards the browser. Each tube vertex ring is 3 vertices per node, so the
            # chosen strand count is honoured unless the whole groom exceeds TUBE_VERTICES; the legend then
            # shows how many strands were drawn.
            blob, _ = smh_groom.to_glb(groom, max_strands=strands, curves=curves,
                                       vertex_budget=10 ** 9 if curves else TUBE_VERTICES, class1_shader=class1,
                                       style=style and engine == "shader", engine=engine)
            CACHE.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(blob)
            tmp.replace(path)
            return blob

    def serve(self, handler, route, arg):
        try:
            if route == "summary":
                return handler.send(self.summary())
            if route == "list":
                return handler.send(self.rows(arg))
            lod = max(0, min(7, int(arg("lod", "0") or 0)))
            if route == "item":
                return handler.send(self.item(int(arg("id")), lod))
            if route == "glb":
                blob = self.glb(int(arg("id")), lod, int(arg("strands", "6000") or 6000), arg("curves") == "1",
                                arg("class1"), arg("style", "1") != "0")
                return handler.send(blob, "model/gltf-binary", cache_control="private, max-age=3600")
        except LookupError as exc:
            return handler.send({"error": str(exc)}, status=404)
        except (ValueError, smh_groom.GroomError) as exc:
            return handler.send({"error": str(exc)}, status=422)
        return handler.send({"error": "Unknown groom request"}, status=404)
