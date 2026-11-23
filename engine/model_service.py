# SPDX-License-Identifier: GPL-3.0-or-later
"""Lineup model backend: inventory queries, on-demand GLB decode and material textures."""
import gzip
import json
import os
import re
import sqlite3
import sys
import threading
from collections import Counter, OrderedDict, defaultdict
from contextlib import closing
from pathlib import Path
from paths import GAME, INDEX, LIBRARY, CACHE, ENGINE

try:
    from pso_texture_roles import ROLES as TEXTURE_ROLES
except Exception:
    TEXTURE_ROLES = {}
try:
    from script_tree import _name_hash
except Exception:
    _name_hash = None
from classify import (BAD_PED_DONOR_RE, SENTINEL_TEX, animal_part_keys, garment_default_tint, head_eth_keys,
                      head_skin_tint, load_ttdb_manifest, ped_garment_keys, split_variant)

INVENTORY = LIBRARY / "inventory.sqlite3"
TEXTURES = LIBRARY / "textures.sqlite3"
HIDDEN = ("World map geometry",)
# The categories the viewer lists. The others stay in the database because character features read them
# (gore pieces are under Effects, cloth proxies under Physics & bounds proxies, the body rigs under Other models).
VISIBLE = ("Protagonists", "Characters", "Clothing & accessories")
LIST_COLUMNS = ("id", "name", "category", "subcategory", "family", "archive", "kind", "group_size",
                "texture_refs", "textures_found", "placements", "status")


def _ttdb_ref(path):
    """Map a workspace-relative .ttpf path to the (area, archive) query params used by /textures."""
    parts = Path(path).parts
    stem = Path(path).stem
    if len(parts) >= 4 and parts[0] == "pack" and parts[2] == "data" and parts[3] == "ttdb":
        sub = "/".join(parts[4:-1])
        return (f"{parts[1]}/{sub}" if sub else parts[1], stem)
    if len(parts) >= 3 and parts[0] == "data" and parts[1] == "ttdb":
        return ("/".join(parts[2:-1]), stem)
    return (Path(path).parent.name, stem)


def connect(path):
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, check_same_thread=False)
    db.row_factory = sqlite3.Row
    return db


class ModelLibrary:
    def __init__(self):
        self.lock = threading.RLock()
        self.decode_locks = {}
        self.archives = OrderedDict()
        self._archive_locks = {}
        self._pool = None
        self._tex_pool = None
        self._meta_sig = None
        self._summary_cache = None
        self._archive_cache = None
        self._effects_by_hash = None
        self._animal_fallbacks = None
        self._ped_fallbacks = None
        self._ttdb_manifest = None
        self._native_textures = None
        self._models_by_texture = None
        self._wheel_catalog = None
        self._wardrobe_catalog = None
        self._character_assemblies = None
        self._ped_archetypes = None
        self._embedded_index = OrderedDict()
        self._skeleton_cache = OrderedDict()
        self._skin_cache = OrderedDict()
        self._master_rig_ids = None
        self._animal_master_skels = None
        self._name_to_model_id = None
        self._skin_balance_cache = {}
        self._char_groom_cache = {}
        self._char_outfits_cache = {}
        self._char_overlays_cache = {}
        self._groom_catalog_cache = {}
        from anim_service import AnimService
        self.anim = AnimService()
        (CACHE / "glb").mkdir(parents=True, exist_ok=True)
        (CACHE / "tex").mkdir(parents=True, exist_ok=True)
        (CACHE / "gtex").mkdir(parents=True, exist_ok=True)
        self.decoded = sqlite3.connect(CACHE / "decoded.sqlite3", check_same_thread=False)
        self.decoded.execute("create table if not exists meta(key text primary key, value text)")
        self._sync_cache()

    def _archive_lock(self, path):
        with self.lock:
            lk = self._archive_locks.get(path)
            if lk is None:
                lk = threading.RLock()
                self._archive_locks[path] = lk
            return lk

    def _get_pool(self):
        with self.lock:
            if self._pool is None:
                from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
                workers = max(2, min(12, (os.cpu_count() or 4) // 2))
                main_file = getattr(sys.modules.get("__main__"), "__file__", None)
                if main_file and Path(main_file).is_file():
                    self._pool = ProcessPoolExecutor(max_workers=workers)
                else:
                    self._pool = ThreadPoolExecutor(max_workers=workers)
            return self._pool

    def _get_tex_pool(self):
        with self.lock:
            if self._tex_pool is None:
                from concurrent.futures import ThreadPoolExecutor
                workers = max(4, min(12, os.cpu_count() or 8))
                self._tex_pool = ThreadPoolExecutor(max_workers=workers)
            return self._tex_pool

    def _sync_cache(self):
        sig = self.signature()
        with self.lock:
            stored = dict(self.decoded.execute("select key, value from meta").fetchall())
            if stored.get("signature") != sig:
                # A rebuilt inventory can renumber rows, and a decoder change can alter results: start clean.
                self.decoded.execute("drop table if exists decoded")
                import shutil
                shutil.rmtree(CACHE / "glb", ignore_errors=True)
                (CACHE / "glb").mkdir(parents=True, exist_ok=True)
                self.decoded.execute("insert or replace into meta values ('signature', ?)", (sig,))
            self.decoded.execute("""create table if not exists decoded(
                id integer, lod integer, status text, error text, vertices integer, triangles integer,
                parts integer, file text, info text, source text, primary key(id, lod))""")
            self.decoded.commit()

    @staticmethod
    def signature():
        parts = ["v3"]
        for path in (INVENTORY, LIBRARY / "ped_archetypes.sqlite3", ENGINE / "model_decode.py", ENGINE / "drawables.py"):
            st = path.stat() if path.is_file() else None
            parts.append(f"{path.name}:{st.st_size}:{st.st_mtime_ns}" if st else f"{path.name}:missing")
        return "|".join(parts)

    @staticmethod
    def _decode_name(row):
        return row["base_name"] if row["source"] == "ped_archetype" and row["base_name"] else row["name"]

    def _get_ped_archetypes(self):
        with self.lock:
            if self._ped_archetypes is not None:
                return self._ped_archetypes
        pa_path = LIBRARY / "ped_archetypes.sqlite3"
        out = {}
        if pa_path.is_file():
            try:
                with closing(connect(pa_path)) as db:
                    for r in db.execute("select * from ped_archetype").fetchall():
                        d = dict(r)
                        d["body_drawables"] = json.loads(d.get("body_drawables") or "[]")
                        d["clothing_drawables"] = json.loads(d.get("clothing_drawables") or "[]")
                        d["body_morphs"] = json.loads(d.get("body_morphs") or "{}")
                        try:
                            d["height_scale"] = float(d["height_scale"]) if d.get("height_scale") is not None else 1.0
                        except (TypeError, ValueError):
                            d["height_scale"] = 1.0
                        out[int(d["arch_hash"])] = d
            except Exception:
                pass
        with self.lock:
            self._ped_archetypes = out
        return out

    # Inventory ------------------------------------------------------------------------------------
    def db(self):
        if not INVENTORY.is_file():
            raise ValueError("Model inventory not built. Run: python lineup.py --build")
        return connect(INVENTORY)

    def _ensure_meta(self):
        sig = self.signature()
        with self.lock:
            if self._meta_sig == sig and self._summary_cache is not None:
                return
        with closing(self.db()) as db:
            rows = db.execute("""select category, subcategory, count(*) resources, sum(is_rep) items
                                 from model group by 1,2 order by 1,2""").fetchall()
            status = dict(db.execute("select status, count(*) from model group by 1").fetchall())
            archives = [r[0] for r in db.execute("select distinct archive from model order by 1")]
        cats = OrderedDict()
        for r in rows:
            if r["category"] not in VISIBLE:
                continue
            c = cats.setdefault(r["category"], {"category": r["category"], "resources": 0, "items": 0,
                                                "hidden": r["category"] in HIDDEN, "subcategories": []})
            c["resources"] += r["resources"]
            c["items"] += r["items"]
            c["subcategories"].append({"name": r["subcategory"], "resources": r["resources"], "items": r["items"]})
        order = ["Protagonists", "Characters", "Clothing & accessories", "Animals", "Vehicles", "Weapons", "Props",
                 "Vegetation", "Effects", "Physics & bounds proxies", "Other models", "World map geometry"]
        ordered = sorted(cats.values(), key=lambda c: order.index(c["category"]) if c["category"] in order else 99)
        base = {"categories": ordered, "resourceStatus": status,
                "totals": {"resources": sum(c["resources"] for c in ordered), "items": sum(c["items"] for c in ordered)}}
        with self.lock:
            self._meta_sig = sig
            self._summary_cache = base
            self._archive_cache = archives
            self._animal_fallbacks = None
            self._ped_fallbacks = None
            self._ttdb_manifest = None
            self._models_by_texture = None
            self._wheel_catalog = None
            self._wardrobe_catalog = None
            self._character_assemblies = None
            self._ped_archetypes = None

    def summary(self):
        self._ensure_meta()
        with self.lock:
            decoded = dict(self.decoded.execute("select status, count(*) from decoded where lod=0 group by 1").fetchall())
            return dict(self._summary_cache, decodedThisSession=decoded)

    def rows(self, arg):
        self._ensure_meta()
        where, params = ["is_rep=1"], []
        category, sub, q = arg("category"), arg("subcategory"), arg("q").strip().lower()
        if category and category not in VISIBLE:
            where.append("0")
        elif category:
            where.append("category=?")
            params.append(category)
        else:
            where.append("category in (%s)" % ",".join("?" * len(VISIBLE)))
            params.extend(VISIBLE)
        if sub:
            where.append("subcategory=?")
            params.append(sub)
        if q:
            # Search every resource of a group, not only its representative.
            where.append("group_key in (select group_key from model where name like ? escape '\\' or "
                         "printf('%08x', hash)=?)")
            params.extend(["%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%",
                           q.removeprefix("0x")])
        if arg("archive"):
            where.append("archive=?")
            params.append(arg("archive"))
        if arg("textured") == "1":
            where.append("textures_found>0")
        if arg("placed") == "1":
            where.append("placements>0")
        order = {
            "name": "name",
            "detail": "coalesce(ibytes,0) desc, name",
            "parts": "group_size desc, name",
            "textures": "textures_found desc, texture_refs desc, name",
            "placements": "placements desc, name",
        }.get(arg("sort"), "name")
        start = max(0, int(arg("start", "0")))
        limit = min(300, max(1, int(arg("limit", "150"))))
        sql = " where " + " and ".join(where)
        with closing(self.db()) as db:
            total = db.execute("select count(*) from model" + sql, params).fetchone()[0]
            items = db.execute(f"select {','.join(LIST_COLUMNS)} from model{sql} order by {order} limit ? offset ?",
                               [*params, limit, start]).fetchall()
        with self.lock:
            archives = list(self._archive_cache)
        return {"total": total, "start": start, "rows": [list(r) for r in items], "columns": LIST_COLUMNS,
                "archives": archives}

    def _get_effects(self):
        """Shader effect names per shader hash. Lineup does not read the shader archive, so none are known."""
        return {}

    def _get_ttdb_manifest(self):
        with self.lock:
            if self._ttdb_manifest is not None:
                return self._ttdb_manifest
        manifest = load_ttdb_manifest()
        with self.lock:
            self._ttdb_manifest = manifest
        return manifest

    def _get_native_textures(self):
        with self.lock:
            if self._native_textures is not None:
                return self._native_textures
        index = defaultdict(list)
        for gz in sorted(INDEX.rglob("studio-native-textures-v4.json.gz")):
            try:
                with gzip.open(gz, "rt", encoding="utf-8") as f:
                    doc = json.load(f)
            except Exception:
                continue
            for rec in doc.get("records", ()):
                h = str(rec.get("nameHash") or "").lower().removeprefix("0x")
                if len(h) == 16 and rec.get("gpuStreams"):
                    index[h].append(rec)
        result = dict(index)
        with self.lock:
            self._native_textures = result
        return result

    def _get_animal_fallbacks(self):
        with self.lock:
            if self._animal_fallbacks is not None:
                return self._animal_fallbacks
        redirects = self._get_ttdb_manifest().get("redirects", {})
        with closing(self.db()) as db:
            rows = db.execute("select name, materials from model where category='Animals'").fetchall()
        parsed, all_hashes = [], set()
        for r in rows:
            mats = json.loads(r["materials"] or "[]")
            parsed.append((r["name"], mats))
            for m in mats:
                for t in m["textures"]:
                    th = t["textureHash"]
                    if th != SENTINEL_TEX:
                        all_hashes.add(th)
                        if th in redirects:
                            all_hashes.add(redirects[th][0])
        info = {}
        ordered_hashes = sorted(all_hashes)
        for i in range(0, len(ordered_hashes), 800):
            info.update(self.texture_info(ordered_hashes[i:i + 800]))
        donors = {}
        for name, mats in parsed:
            primary_keys, _ = animal_part_keys(name)
            if not primary_keys:
                continue
            base, _ = split_variant(name.lower())
            rank = (1 if any(w in base for w in ("_corpse", "_law", "_sheriff", "_agh", "_mutated", "_css")) else 0,
                    len(base), base)
            for m in mats:
                for t in m["textures"]:
                    th, param = t["textureHash"], t["parameter"]
                    eff = redirects[th][0] if th not in info and th in redirects else th
                    if eff in info:
                        for pk in primary_keys:
                            cur = donors.get((*pk, param))
                            if cur is None or rank < cur[0]:
                                donors[(*pk, param)] = (rank, eff, name, info[eff])
        with self.lock:
            self._animal_fallbacks = donors
        return donors

    def _get_ped_fallbacks(self):
        with self.lock:
            if self._ped_fallbacks is not None:
                return self._ped_fallbacks
        redirects = self._get_ttdb_manifest().get("redirects", {})
        with closing(self.db()) as db:
            rows = db.execute(
                "select name, materials from model where source != 'ped_archetype' and category in "
                "('Protagonists','Characters','Clothing & accessories','Effects')"
            ).fetchall()
        parsed, all_hashes = [], set()
        for r in rows:
            mats = json.loads(r["materials"] or "[]")
            parsed.append((r["name"], mats))
            for m in mats:
                for t in m["textures"]:
                    th = t["textureHash"]
                    if th != SENTINEL_TEX:
                        all_hashes.add(th)
                        if th in redirects:
                            all_hashes.add(redirects[th][0])
        info = {}
        ordered_hashes = sorted(all_hashes)
        for i in range(0, len(ordered_hashes), 800):
            info.update(self.texture_info(ordered_hashes[i:i + 800]))
        ped_donors, head_norm_donors = {}, {}
        for name, mats in parsed:
            n = name.lower()
            base, _ = split_variant(n)
            if not BAD_PED_DONOR_RE.search(n):
                for m in mats:
                    if m["shaderHash"] == "379efcef":
                        for t in m["textures"]:
                            th = t["textureHash"]
                            eff = redirects[th][0] if th not in info and th in redirects else th
                            srcs = info.get(eff)
                            if t["parameter"] == "1e77bf4c" and srcs and "BC5" in srcs[0]["format"] and max(srcs[0]["width"], srcs[0]["height"]) >= 256:
                                px = srcs[0]["width"] * srcs[0]["height"]
                                hrank = (-px, 0 if n.startswith(("hc_", "head_")) else 1, len(base), base)
                                for hk in head_eth_keys(n):
                                    cur = head_norm_donors.get(hk)
                                    if cur is None or hrank < cur[0]:
                                        head_norm_donors[hk] = (hrank, eff, name, srcs)
            pkeys, _ = ped_garment_keys(n)
            if pkeys and not re.search(r"(?:^|_)head_(?:fr|mr|fb|mb|fo|mo|fc|mc)\d", base) and not BAD_PED_DONOR_RE.search(n):
                for m in mats:
                    for t in m["textures"]:
                        th, param = t["textureHash"], t["parameter"]
                        eff = redirects[th][0] if th not in info and th in redirects else th
                        srcs = info.get(eff)
                        if srcs:
                            w, hh = srcs[0]["width"], srcs[0]["height"]
                            if param in ("c8e8c282", "1e77bf4c") and max(w, hh) < 64:
                                continue
                            prank = (1 if "_cull" in n or "_slod" in n or "gore" in n else 0, -(w * hh), len(base), base)
                            for pk in pkeys:
                                cur = ped_donors.get((*pk, param))
                                if cur is None or prank < cur[0]:
                                    ped_donors[(*pk, param)] = (prank, eff, name, srcs)
        # Seed clean body-skin presets (hand_*, feet_*) and high-res 1024x1024 head normals from the game's
        # appearance catalogues so sibling meshes always inherit highest-res clean human textures.
        from runtime_faces import _load as _load_face_presets
        all_presets = _load_face_presets()
        skin_presets = {
            pname: ptex for pname, ptex in all_presets.items()
            if pname.startswith(("hand_", "feet_")) and not BAD_PED_DONOR_RE.search(pname)
        }
        head_norm_presets = {
            pname: ptex["1e77bf4c"] for pname, ptex in all_presets.items()
            if re.search(r"(?:^|_)head_(?:fr|mr|fb|mb|fo|mo|fc|mc)\d", pname)
            and ptex.get("1e77bf4c") and not BAD_PED_DONOR_RE.search(pname)
        }
        seed_hashes = sorted(
            {h for ptex in skin_presets.values() for h in ptex.values()}
            | set(head_norm_presets.values())
        )
        seed_info = {}
        for i in range(0, len(seed_hashes), 800):
            seed_info.update(self.texture_info(seed_hashes[i:i + 800]))
        for pname, ptex in skin_presets.items():
            pkeys, _ = ped_garment_keys(pname)
            if not pkeys:
                continue
            is_adult_reg = bool(re.search(r"_(?:fr1|mr1)_", pname))
            for param, fh in ptex.items():
                srcs = seed_info.get(fh)
                if srcs:
                    px = srcs[0]["width"] * srcs[0]["height"]
                    prank = (-2 if is_adult_reg else -1, -px, len(pname), pname)
                    for pk in pkeys:
                        cur = ped_donors.get((*pk, param))
                        if cur is None or prank < cur[0]:
                            ped_donors[(*pk, param)] = (prank, fh, f"{pname} (skin preset)", srcs)
        for pname, nfh in head_norm_presets.items():
            srcs = seed_info.get(nfh)
            if srcs and "BC5" in srcs[0]["format"] and max(srcs[0]["width"], srcs[0]["height"]) >= 256:
                px = srcs[0]["width"] * srcs[0]["height"]
                hrank = (-px, 0 if pname.startswith(("player_", "head_", "hc_")) else 1, len(pname), pname)
                for hk in head_eth_keys(pname):
                    cur = head_norm_donors.get(hk)
                    if cur is None or hrank < cur[0]:
                        head_norm_donors[hk] = (hrank, nfh, pname, srcs)
        result = (ped_donors, head_norm_donors)
        with self.lock:
            self._ped_fallbacks = result
        return result

    def texture_info(self, hashes):
        if not hashes:
            return {}
        out = {}
        if TEXTURES.is_file():
            with closing(connect(TEXTURES)) as t:
                q = ",".join("?" * len(hashes))
                rows = t.execute(f"""select t.hash, p.path, p.area, p.package, t.idx, t.width, t.height, t.format,
                                     t.dimension, t.slices, t.mips, t.mip_codecs from texture t join pack p on p.id=t.pack
                                     where t.hash in ({q}) order by t.hash, p.id""", list(hashes)).fetchall()
            for r in rows:
                ttdb_area, ttdb_archive = _ttdb_ref(r["path"])
                out.setdefault(r["hash"], []).append({k: r[k] for k in r.keys() if k != "hash"} |
                                                     {"mipCodecs": json.loads(r["mip_codecs"]),
                                                      "ttdbArea": ttdb_area, "ttdbArchive": ttdb_archive})
        missing = [h for h in hashes if h not in out]
        if missing:
            native = self._get_native_textures()
            for h in missing:
                for rec in native.get(h, ())[:3]:
                    dim_code = rec.get("dimension", 0)
                    dim_str = ("2D" if dim_code == 0 or (dim_code == 1 and rec.get("depth", 1) == 1)
                               else {1: "2DArray", 2: "Cube", 3: "3D"}.get(dim_code, "2D"))
                    out.setdefault(h, []).append({
                        "path": rec["package"],
                        "area": "native",
                        "package": rec["package"],
                        "idx": 0,
                        "width": rec["width"],
                        "height": rec["height"],
                        "format": rec["format"],
                        "dimension": dim_str,
                        "slices": rec.get("depth", 1),
                        "mips": rec["mips"],
                        "mipCodecs": [],
                        "nativeId": rec["id"],
                        "nativeName": rec.get("name") or h,
                    })
        still_missing = [h for h in hashes if h not in out]
        if still_missing:
            with self.lock:
                for h in still_missing:
                    emb_rec = self._embedded_index.get(h)
                    if emb_rec:
                        out.setdefault(h, []).append(dict(emb_rec[6]))
        return out

    def _register_embedded(self, ident, row, raw_member, raw_root, emb, dname=None):
        dim_str = "2D" if emb.get("depth", 1) <= 1 else "3D"
        src_entry = {
            "path": row["archive"],
            "area": "embedded",
            "package": row["archive"],
            "idx": emb["idx"],
            "width": emb["width"],
            "height": emb["height"],
            "format": emb["format"],
            "dimension": dim_str,
            "slices": emb.get("depth", 1),
            "mips": emb["mips"],
            "mipCodecs": [emb["layout"]],
            "embeddedModelId": ident,
            "embeddedName": emb["name"],
            "embeddedLayout": emb["layout"],
        }
        if emb.get("isTintPalette"):
            src_entry["isTintPalette"] = True
            src_entry["tintRowLength"] = emb["tintRowLength"]
            src_entry["tintRows"] = emb["tintRows"]
        with self.lock:
            self._embedded_index[emb["hash"]] = (
                row["archive"], raw_member, raw_root, dname or row["name"], row["kind"], emb, src_entry
            )
            self._embedded_index.move_to_end(emb["hash"])
            while len(self._embedded_index) > 4096:
                self._embedded_index.popitem(last=False)
        return src_entry

    def item(self, ident, lod=0):
        with closing(self.db()) as db:
            r = db.execute("select * from model where id=?", (ident,)).fetchone()
            if not r:
                raise ValueError("Unknown model")
            group = db.execute("""select id, name, archive, member, kind, variant, low_detail, status, is_rep
                                  from model where group_key=? order by name, archive, member""",
                               (r["group_key"],)).fetchall()
        row = dict(r)
        raw_member, raw_root = int(row["member"]), int(row["root"])
        dname = self._decode_name(row)
        pa = self._get_ped_archetypes().get(int(row["hash"])) if row["source"] == "ped_archetype" else None
        mats = json.loads(row.pop("materials") or "[]")
        for m in mats:
            m["textures"] = [t for t in m["textures"] if t["textureHash"] != SENTINEL_TEX]

        emb_list = []
        emb_sources = {}
        try:
            from model_decode import live_shader_info
            live_mats, emb_list = live_shader_info(row["archive"], raw_member, raw_root, dname, row["kind"])
            if len(live_mats) == len(mats):
                for m, lm in zip(mats, live_mats):
                    m["passMask"] = lm["passMask"]
                    m["renderPass"] = lm["renderPass"]
                    if lm.get("params"):
                        m["params"] = lm["params"]
                    existing_slots = {t["slot"] for t in m["textures"]}
                    for lt in lm["textures"]:
                        if lt["textureHash"] != SENTINEL_TEX and lt["slot"] not in existing_slots:
                            m["textures"].append(dict(lt))
                            existing_slots.add(lt["slot"])
                    m["textures"].sort(key=lambda t: t["slot"])
            for emb in emb_list:
                emb_sources[emb["hash"]] = self._register_embedded(ident, row, raw_member, raw_root, emb, dname=dname)
        except Exception:
            pass
        for m in mats:
            m.setdefault("passMask", 16 if m["shaderHash"] == "fc34f08b" else 1)
            m.setdefault("renderPass", "stencil" if m["shaderHash"] == "fc34f08b" else "opaque")

        manifest = self._get_ttdb_manifest()
        redirects = manifest.get("redirects", {})
        ttdb_colors = manifest.get("colors", {})
        query_hashes = set()
        for m in mats:
            for t in m["textures"]:
                th = t["textureHash"]
                query_hashes.add(th)
                if th in redirects:
                    query_hashes.add(redirects[th][0])
        info = self.texture_info(sorted(query_hashes))
        for eh, esrc in emb_sources.items():
            if not any(s.get("area") == "embedded" for s in info.get(eh, ())):
                info.setdefault(eh, []).append(esrc)
        effects = self._get_effects()
        cat = row["category"]
        base_n, _ = split_variant(dname.lower())
        animal_lookup = animal_part_keys(dname)[1] if cat == "Animals" else []
        ped_lookup = ped_garment_keys(dname)[1] if cat in ("Protagonists", "Characters", "Clothing & accessories", "Effects") else []
        is_head_mesh = bool(re.search(r"(?:^|_)head_(?:fr|mr|fb|mb|fo|mo|fc|mc|mi|fi|mk|fk)\d", base_n)) and "gore" not in base_n
        # Real face, body-skin, eye, teeth, and clothing textures from the game's appearance catalogues.
        from runtime_faces import all_presets_for, faces_for
        face = dict(faces_for(dname)) if cat in ("Protagonists", "Characters", "Clothing & accessories") else {}
        if pa:
            if pa.get("face_albedo"):
                face["c8e8c282"] = pa["face_albedo"]
            if pa.get("face_normal"):
                face["1e77bf4c"] = pa["face_normal"]
        face_info = self.texture_info(sorted(set(face.values()))) if face else {}
        preset_label = (
            f"Ped archetype ({row['name']})" if pa
            else ("Face preset (game data)" if is_head_mesh
                  else ("Body skin preset (game data)" if base_n.startswith(("hand_", "feet_")) or "_body" in base_n
                        else "Appearance preset (game data)"))
        )

        from clothing_presets import (
            MUV_SHADER_HASHES, PLACEHOLDER_CLOTHING_TEXTURES, get_store as get_clothing_store,
        )
        for m in mats:
            m["effects"] = effects.get(m["shaderHash"], [])
            has_c8_slot = any(t["parameter"] == "c8e8c282" for t in m["textures"])
            is_muv_mat = m["shaderHash"] in MUV_SHADER_HASHES or (
                any(t["parameter"] == "8adb8480" for t in m["textures"]) and not has_c8_slot
            )
            for t in m["textures"]:
                th = t["textureHash"]
                t["sources"] = info.get(th, [])
                if not t["sources"] and th in redirects:
                    phash, pso_name = redirects[th]
                    if phash in info:
                        t["sources"] = info[phash]
                        t["effectiveHash"] = phash
                        t["derivedFrom"] = f"TTDB ({pso_name})"
                fh = face.get(t["parameter"])
                if fh in PLACEHOLDER_CLOTHING_TEXTURES:
                    fh = None
                if is_muv_mat and t["parameter"] == "c6f46e79":
                    fh = None
                if fh and face_info.get(fh) and (not t["sources"] or th != fh):
                    cur_s0 = t["sources"][0] if t["sources"] else None
                    new_s0 = face_info[fh][0]
                    cur_px = (cur_s0.get("width", 0) * cur_s0.get("height", 0)) if cur_s0 else 0
                    new_px = new_s0.get("width", 0) * new_s0.get("height", 0)
                    cur_srgb = bool(cur_s0 and "SRGB" in cur_s0.get("format", ""))
                    new_srgb = "SRGB" in new_s0.get("format", "")
                    # Never downgrade an existing higher-resolution local texture or replace sRGB albedo with non-sRGB.
                    downgrade = (
                        (cur_s0 is not None and new_px < cur_px)
                        or (t["parameter"] == "c8e8c282" and cur_srgb and not new_srgb)
                    )
                    if not downgrade:
                        t["sources"] = face_info[fh]
                        t["effectiveHash"] = fh
                        t["derivedFrom"] = preset_label
                role = TEXTURE_ROLES.get(int(t["parameter"], 16))
                if role:
                    t["role"] = role["role"]
                    t["spelling"] = role["spelling"]
                if not t["sources"] and cat == "Animals" and animal_lookup:
                    fallbacks = self._get_animal_fallbacks()
                    donor = next((fallbacks[(*lk, t["parameter"])] for lk in animal_lookup
                                  if (*lk, t["parameter"]) in fallbacks), None)
                    if donor:
                        _, donor_hash, donor_name, donor_sources = donor
                        t["sources"] = donor_sources
                        t["effectiveHash"] = donor_hash
                        t["derivedFrom"] = donor_name
                if not t["sources"] and ped_lookup and m["shaderHash"] != "89a1cf72" and not base_n.endswith("_eyes"):
                    ped_donors, head_norm_donors = self._get_ped_fallbacks()
                    if m["shaderHash"] == "379efcef" and t["parameter"] == "1e77bf4c":
                        donor = next((head_norm_donors[hk] for hk in head_eth_keys(dname)
                                      if hk in head_norm_donors), None)
                        if donor:
                            _, donor_hash, donor_name, donor_sources = donor
                            t["sources"] = donor_sources
                            t["effectiveHash"] = donor_hash
                            t["derivedFrom"] = donor_name
                    elif not (is_head_mesh and t["parameter"] == "c8e8c282"):
                        donor = next((ped_donors[(*lk, t["parameter"])] for lk in ped_lookup
                                      if (*lk, t["parameter"]) in ped_donors), None)
                        if donor:
                            _, donor_hash, donor_name, donor_sources = donor
                            t["sources"] = donor_sources
                            t["effectiveHash"] = donor_hash
                            t["derivedFrom"] = donor_name
                if not t["sources"] and t["parameter"] in ("c8e8c282", "8adb8480"):
                    rgb = ttdb_colors.get(th)
                    if rgb:
                        m["tintColor"] = rgb
                        t["ttdbColor"] = rgb

            # Runtime-bound character heads (ped_standard_head = 379efcef) and MUV garments.
            if m["shaderHash"] == "379efcef" and "gore" not in base_n:
                fa = face.get("c8e8c282")
                if fa and face_info.get(fa) and not any(t["parameter"] == "c8e8c282" for t in m["textures"]):
                    role = TEXTURE_ROLES.get(0xc8e8c282, {})
                    m["textures"].append({
                        "slot": len(m["textures"]),
                        "parameter": "c8e8c282",
                        "textureHash": fa,
                        "effectiveHash": fa,
                        "derivedFrom": preset_label,
                        "role": role.get("role", "Base colour"),
                        "spelling": role.get("spelling", "DiffuseSampler"),
                        "sources": face_info[fa],
                    })
                has_srgb_albedo = any(
                    t["parameter"] == "c8e8c282" and t["sources"] and "SRGB" in t["sources"][0].get("format", "")
                    for t in m["textures"]
                )
                if not has_srgb_albedo:
                    m.setdefault("tintColor", head_skin_tint(dname))
                has_norm = any(t["parameter"] == "1e77bf4c" and t["sources"] for t in m["textures"])
                if not has_norm and face_info.get(face.get("1e77bf4c")):
                    fh = face["1e77bf4c"]
                    role = TEXTURE_ROLES.get(0x1e77bf4c, {})
                    m["textures"].append({
                        "slot": len(m["textures"]),
                        "parameter": "1e77bf4c",
                        "textureHash": fh,
                        "effectiveHash": fh,
                        "derivedFrom": preset_label,
                        "role": role.get("role", "Bump / normal"),
                        "spelling": role.get("spelling", "BumpSampler"),
                        "sources": face_info[fh],
                    })
                    has_norm = True
                if not has_norm:
                    _, head_norm_donors = self._get_ped_fallbacks()
                    donor = next((head_norm_donors[hk] for hk in head_eth_keys(dname)
                                  if hk in head_norm_donors), None)
                    if donor:
                        _, donor_hash, donor_name, donor_sources = donor
                        role = TEXTURE_ROLES.get(0x1e77bf4c, {})
                        m["textures"].append({
                            "slot": len(m["textures"]),
                            "parameter": "1e77bf4c",
                            "textureHash": donor_hash,
                            "effectiveHash": donor_hash,
                            "derivedFrom": f"{donor_name} (head rig)",
                            "role": role.get("role", "Bump / normal"),
                            "spelling": role.get("spelling", "BumpSampler"),
                            "sources": donor_sources,
                        })
            elif m["shaderHash"] in MUV_SHADER_HASHES or (
                any(t["parameter"] == "8adb8480" for t in m["textures"])
                and not any(t["parameter"] == "c8e8c282" and t["sources"] for t in m["textures"])
            ):
                if ped_lookup and not any(t["sources"] for t in m["textures"]):
                    ped_donors, _ = self._get_ped_fallbacks()
                    for param, fallback_role in (("c6f46e79", "Specular"), ("1e77bf4c", "Bump / normal")):
                        donor = next((ped_donors[(*lk, param)] for lk in ped_lookup
                                      if (*lk, param) in ped_donors), None)
                        if donor:
                            _, donor_hash, donor_name, donor_sources = donor
                            role = TEXTURE_ROLES.get(int(param, 16), {})
                            m["textures"].append({
                                "slot": len(m["textures"]),
                                "parameter": param,
                                "textureHash": donor_hash,
                                "effectiveHash": donor_hash,
                                "derivedFrom": f"{donor_name} (garment type)",
                                "role": role.get("role", fallback_role),
                                "spelling": role.get("spelling", param),
                                "sources": donor_sources,
                            })
                has_albedo = any(t["parameter"] == "c8e8c282" and t["sources"] for t in m["textures"])
                if not has_albedo and any(t["sources"] for t in m["textures"]):
                    m.setdefault("tintColor", garment_default_tint(dname))
            elif "gore" in base_n and m["shaderHash"] in ("210fbc6a", "379efcef"):
                m.setdefault("tintColor", [0.68, 0.44, 0.40])

        if emb_list:
            used_emb = {t.get("effectiveHash") or t["textureHash"] for m in mats for t in m["textures"]}
            unused_srgb = [
                e for e in emb_list
                if e["hash"] not in used_emb and e.get("depth", 1) <= 1 and not e.get("isTintPalette") and "SRGB" in e["format"]
            ]
            if unused_srgb:
                primary_emb = max(unused_srgb, key=lambda e: e["width"] * e["height"])
                eh = primary_emb["hash"]
                esrc = [emb_sources[eh]]
                for m in mats:
                    if m.get("renderPass") == "stencil" or m["shaderHash"] in ("fa9cc069", "bd71bdbf", "389206d1", "31212453"):
                        continue
                    unres_diff = next(
                        (t for t in m["textures"] if not t["sources"] and (t["parameter"] == "c8e8c282" or "diffuse" in (t.get("role") or "").lower())),
                        None,
                    )
                    if unres_diff:
                        unres_diff["sources"] = esrc
                        unres_diff["effectiveHash"] = eh
                        unres_diff["derivedFrom"] = f"embedded TXD ({primary_emb['name']})"
                    elif not any(t["sources"] for t in m["textures"]):
                        role = TEXTURE_ROLES.get(0xc8e8c282, {})
                        m["textures"].append({
                            "slot": len(m["textures"]),
                            "parameter": "c8e8c282",
                            "textureHash": eh,
                            "effectiveHash": eh,
                            "derivedFrom": f"embedded TXD ({primary_emb['name']})",
                            "role": role.get("role", "Base color"),
                            "spelling": role.get("spelling", "DiffuseSampler"),
                            "sources": esrc,
                        })
            row["embeddedTextures"] = [emb_sources[e["hash"]] | {"hash": e["hash"]} for e in emb_list]

        # Attach 1D tint palette metadata (parameter 0x2c8d7182) for per-vertex blue-channel palette lookup.
        from model_decode import tint_palette_layout
        for m in mats:
            tp_slot = next(
                (t for t in m["textures"]
                 if t["parameter"] == "2c8d7182" and t.get("sources")
                 and (t["sources"][0].get("isTintPalette") or (t["sources"][0].get("height") == 1 and 1 <= (t["sources"][0].get("width") or 0) <= 4096))),
                None,
            )
            if tp_slot:
                src0 = tp_slot["sources"][0]
                w = int(src0["width"])
                row_len, rows = tint_palette_layout(w)
                m["tintPalette"] = {
                    "hash": tp_slot.get("effectiveHash") or tp_slot["textureHash"],
                    "width": w,
                    "rowLength": row_len,
                    "rows": rows,
                }
            # Attach 2D texture array LUT metadata (e.g. vehicle material/paint ramp LUT 19df25d7 -> 128x256x8).
            lut_slot = next(
                (t for t in m["textures"]
                 if t.get("sources") and (t["sources"][0].get("dimension") == "2D array" or (t["sources"][0].get("slices") or 1) > 1)
                 and (t["parameter"] == "19df25d7" or (t["sources"][0].get("width", 0) <= 256 and t["sources"][0].get("height", 0) <= 512))),
                None,
            )
            if lut_slot:
                src0 = lut_slot["sources"][0]
                m["lutArray"] = {
                    "hash": lut_slot.get("effectiveHash") or lut_slot["textureHash"],
                    "parameter": lut_slot["parameter"],
                    "width": int(src0.get("width") or 128),
                    "height": int(src0.get("height") or 256),
                    "slices": int(src0.get("slices") or 1),
                    "format": src0.get("format") or "",
                }

        for key in ("box", "lods", "texture_areas", "archetype_box"):
            row[key] = json.loads(row[key]) if row[key] else None
        row["member"] = hex(raw_member)
        row["root"] = hex(raw_root)
        row["hash"] = f"0x{row['hash']:08x}"
        with self.lock:
            dec = self.decoded.execute(
                "select status,error,vertices,triangles,parts,info from decoded where id=? and lod=?",
                (ident, lod)).fetchone()
        row["decode"] = None if not dec else {"status": dec[0], "error": dec[1], "vertices": dec[2],
                                              "triangles": dec[3], "parts": dec[4], "lod": lod,
                                              "info": json.loads(dec[5]) if dec[5] else None}
        row["materials"] = mats
        row["group"] = [dict(g) | {"member": hex(g["member"])} for g in group]
        if pa:
            row["pedArchetype"] = {
                "index": pa["idx"],
                "archHex": pa["arch_hex"],
                "shortName": pa.get("short_name") or "",
                "exactName": bool(pa.get("name_exact")),
                "outfitCount": pa.get("outfit_count") or 0,
                "bodyRig": pa.get("body_rig") or "",
                "headDrawable": pa.get("head_drawable") or dname,
                "eyeDrawable": pa.get("eye_drawable") or "",
                "teethDrawable": pa.get("teeth_drawable") or "",
                "clothingCount": len(pa.get("clothing_drawables") or ()),
            }
        ws = self._wheel_setup_for(row, raw_member, raw_root)
        if ws:
            row["wheelSetup"] = ws
        wb = self._wardrobe_for(row)
        if wb:
            row["wardrobe"] = wb
        ca = self._character_assembly_for(row)
        if ca:
            row["characterAssembly"] = self._enrich_character_assembly(ident, row, ca)
        if cat in ("Protagonists", "Characters", "Clothing & accessories"):
            # Attach RAGE 9 MUV clothing presets (data.rpf/00001b95b000.pso + 00011ccb2000.pso + 2,709 0DRPs)
            cstore = get_clothing_store()
            has_muv_mat = any(
                (m["shaderHash"] in MUV_SHADER_HASHES or any(t["parameter"] == "8adb8480" for t in m["textures"]))
                and not any(t["parameter"] == "c8e8c282" and t["sources"] and "SRGB" in t["sources"][0].get("format", "") for t in m["textures"])
                for m in mats
            )
            if has_muv_mat or cat == "Clothing & accessories":
                cp = cstore.presets_for_model(dname)
                if cp:
                    row["clothingPresets"] = cp
                    def_pre = cp["defaultPreset"]
                    for m in mats:
                        m_has_srgb = any(
                            t["parameter"] == "c8e8c282" and t["sources"] and "SRGB" in t["sources"][0].get("format", "")
                            for t in m["textures"]
                        )
                        m_is_muv = (
                            m["shaderHash"] in MUV_SHADER_HASHES
                            or any(t["parameter"] == "8adb8480" for t in m["textures"])
                        )
                        if m_is_muv and not m_has_srgb:
                            m["isMuv"] = True
                            m["muvPreset"] = def_pre
            tv = all_presets_for(dname)
            # Filter out known blank/black placeholder hashes from catalogue variants
            tv["albedos"] = [h for h in tv["albedos"] if h not in PLACEHOLDER_CLOTHING_TEXTURES]
            # Share all 6 male genital albedos across base/alt_00/alt_01/alt_02 so every male genital variant can use them.
            if dname.lower().startswith("bodygenitalsgen_mr1_000_regular_ws00_full"):
                alt1 = all_presets_for("bodygenitalsgen_mr1_000_regular_ws00_full_alt_01")
                for h in alt1["albedos"]:
                    if h not in tv["albedos"]:
                        tv["albedos"].append(h)
                for h in alt1["normals"]:
                    if h not in tv["normals"]:
                        tv["normals"].append(h)
            if len(tv["albedos"]) > 1 or len(tv["normals"]) > 1:
                tv_info = self.texture_info(sorted(set(tv["albedos"]) | set(tv["normals"])))
                def _fmt_tv(hashes):
                    out_l = []
                    for h in hashes:
                        srcs = tv_info.get(h)
                        if srcs:
                            s0 = srcs[0]
                            out_l.append({"hash": h, "width": s0["width"], "height": s0["height"], "format": s0["format"]})
                    return out_l
                albs_f, nrms_f = _fmt_tv(tv["albedos"]), _fmt_tv(tv["normals"])
                if len(albs_f) > 1 or len(nrms_f) > 1:
                    row["textureVariants"] = {"albedos": albs_f, "normals": nrms_f}
        rig = self.rig_info_for(row, raw_member, raw_root, dname)
        if rig:
            row["rigInfo"] = rig
        return row

    # Reverse Texture Lookup, Wheels & Wardrobe -----------------------------------------------------
    def by_texture(self, texture_hash, limit=60):
        self._ensure_meta()
        h = (texture_hash or "").strip().lower().removeprefix("0x")
        if len(h) != 16 or any(c not in "0123456789abcdef" for c in h):
            raise ValueError("Invalid 64-bit texture hash")
        lim = max(1, min(200, limit))
        with self.lock:
            if self._models_by_texture is None:
                self._models_by_texture = {}
            cached = self._models_by_texture.get(h)
        if cached is not None:
            return {"hash": h, "total": len(cached), "models": cached[:lim]}

        with self.lock:
            ttdb_rev = getattr(self, "_ttdb_rev", None)
        if ttdb_rev is None:
            redirects = self._get_ttdb_manifest().get("redirects", {})
            rev = defaultdict(list)
            for vh, (phash, pso_name) in redirects.items():
                rev[phash].append((vh, pso_name))
            with self.lock:
                self._ttdb_rev = rev
            ttdb_rev = rev

        via_map = {h: (0, "direct")}
        for vh, pso_name in ttdb_rev.get(h, ()):
            via_map.setdefault(vh, (1, f"TTDB ({pso_name})"))

        like_clauses = " or ".join("materials like ?" for _ in via_map)
        like_args = [f'%"{kh}"%' for kh in via_map]

        with closing(self.db()) as db:
            rows = db.execute(
                f"select id, name, category, subcategory, is_rep, placements, materials "
                f"from model where status='indexed' and is_rep=1 and ({like_clauses})",
                like_args,
            ).fetchall()
            if not rows:
                rows = db.execute(
                    f"select id, name, category, subcategory, is_rep, placements, materials "
                    f"from model where status='indexed' and ({like_clauses}) limit 400",
                    like_args,
                ).fetchall()

        best = {}
        for r in rows:
            mid = r["id"]
            is_world = 1 if r["category"] == "World map geometry" else 0
            mats = json.loads(r["materials"] or "[]")
            for m in mats:
                for t in m.get("textures", ()):
                    th = t.get("textureHash")
                    if th not in via_map:
                        continue
                    via_ord, via_label = via_map[th]
                    param = t.get("parameter", "")
                    role_info = TEXTURE_ROLES.get(int(param, 16), {}) if param else {}
                    role_str = role_info.get("role") or role_info.get("spelling") or param
                    rank = (is_world, 0 if r["is_rep"] else 1, via_ord, -(r["placements"] or 0), r["name"])
                    prev = best.get(mid)
                    if prev is None or rank < prev[0]:
                        best[mid] = (rank, {
                            "id": mid, "name": r["name"], "category": r["category"],
                            "subcategory": r["subcategory"], "isRep": bool(r["is_rep"]),
                            "placements": r["placements"] or 0, "role": role_str, "via": via_label,
                        })

        hits = [v[1] for v in sorted(best.values(), key=lambda x: x[0])]
        with self.lock:
            if len(self._models_by_texture) > 2048:
                self._models_by_texture.clear()
            self._models_by_texture[h] = hits
        return {"hash": h, "total": len(hits), "models": hits[:lim]}

    def _get_wheel_catalog(self):
        with self.lock:
            if self._wheel_catalog is not None:
                return self._wheel_catalog
        rims, tyres = [], []
        with closing(self.db()) as db:
            rows = db.execute("""
                select id, name, box from model
                where category='Vehicles' and subcategory='Shared vehicle parts'
                  and is_rep=1 and status='indexed' and (name glob 'rim_*' or name glob 'tyre_*')
                order by name
            """).fetchall()
        for r in rows:
            bx = json.loads(r["box"]) if r["box"] else None
            if not bx:
                continue
            rad = round(max(bx[1][1] - bx[0][1], bx[1][2] - bx[0][2]) * 0.5, 4)
            wid = round(bx[1][0] - bx[0][0], 4)
            entry = {"id": r["id"], "name": r["name"], "unitRadius": rad, "unitWidth": wid}
            if r["name"].startswith("rim_"):
                rims.append(entry)
            else:
                tyres.append(entry)
        cat = {"rims": rims, "tyres": tyres}
        with self.lock:
            self._wheel_catalog = cat
        return cat

    def _wheel_setup_for(self, row, raw_member, raw_root):
        WHEELED = {"Cars", "Trucks", "Vans & box trucks", "Motorcycles", "Bicycles",
                   "Buses", "Tractors", "Trailers", "Utility vehicles", "Other vehicles"}
        if row["category"] != "Vehicles" or row["subcategory"] not in WHEELED or not row["is_rep"]:
            return None
        from model_decode import vehicle_wheels
        axles = vehicle_wheels(row["archive"], raw_member, raw_root, row["name"], row["kind"])
        if len(axles) < 2:
            return None
        cat = self._get_wheel_catalog()
        rims, tyres = cat["rims"], cat["tyres"]
        if not rims or not tyres:
            return None
        sub = row["subcategory"]
        nm = row["name"].lower()
        max_r = max(a["radius"] for a in axles)

        def _pick(items, prefix):
            return next((x["id"] for x in items if x["name"].startswith(prefix)), items[0]["id"])

        if sub == "Bicycles":
            rim_id, tyre_id = _pick(rims, "rim_bicycle_f_001"), _pick(tyres, "tyre_bicycle_road_001")
        elif sub == "Motorcycles":
            rim_id, tyre_id = _pick(rims, "rim_aerocover_motorbike_f_001"), _pick(tyres, "tyre_motorbike_001")
        elif sub == "Tractors" or max_r >= 0.95:
            rim_id = _pick(rims, "rim_industrial_001" if sub == "Tractors" else "rim_truck_001")
            tyre_id = _pick(tyres, "tyre_tractor_001" if sub == "Tractors" else "tyre_dump_001")
        elif sub in ("Trucks", "Buses", "Trailers", "Vans & box trucks", "Utility vehicles"):
            rim_id, tyre_id = _pick(rims, "rim_truck_001"), _pick(tyres, "tyre_truck_001")
        elif any(k in nm for k in ("offroad", "baja", "sand", "rebel", "mesa", "rancher", "jeep", "quad")):
            rim_id, tyre_id = _pick(rims, "rim_offroad_001"), _pick(tyres, "tyre_offroad_001")
        elif any(k in nm for k in ("muscle", "dominator", "gauntlet", "sabre", "dukes", "vigero", "buccaneer")):
            rim_id, tyre_id = _pick(rims, "rim_muscle_001"), _pick(tyres, "tyre_muscle_001")
        elif max_r <= 0.39:
            rim_id, tyre_id = _pick(rims, "rim_highend_001"), _pick(tyres, "tyre_lowpro_001")
        else:
            rim_id, tyre_id = _pick(rims, "rim_street_001"), _pick(tyres, "tyre_car_001")
        return {
            "axles": axles,
            "defaultRimId": rim_id,
            "defaultTyreId": tyre_id,
            "rims": rims,
            "tyres": tyres,
        }

    def _get_wardrobe_catalog(self):
        with self.lock:
            if self._wardrobe_catalog is not None:
                return self._wardrobe_catalog
        rig_re = re.compile(r"_(fr1|mr1|fb1|mb1|fo1|mo1|fc1|mc1|mi1|mk1)_")
        by_rig = defaultdict(lambda: defaultdict(list))
        with closing(self.db()) as db:
            rows = db.execute("""
                select id, name, category, subcategory from model
                where is_rep=1 and status='indexed' and low_detail=0 and source != 'ped_archetype'
                  and category in ('Protagonists', 'Characters', 'Clothing & accessories')
                order by case category when 'Protagonists' then 0 when 'Clothing & accessories' then 1 else 2 end, name
            """).fetchall()
        for r in rows:
            nm = r["name"].lower()
            if any(bad in nm for bad in ("gore", "cull", "slod", "proxy", "invisible_", "bound", "_fp")):
                continue
            m = rig_re.search(nm)
            if not m:
                continue
            rig = m.group(1)
            sub = r["subcategory"]
            if (
                sub == "Underwear"
                or re.search(r"(?:^|_)(?:undies|brabikini|brabalcony|brabondage|brabustier|brafullcup|brasports|brastrapless|bratriangle|tapepasties|swimsuit|lingerie)", nm)
            ):
                slot = "underwear"
            elif "fullbody" in nm or sub == "Dresses & one-pieces" or "legacyped" in nm:
                slot = "body"
            elif re.match(r"(?:hand|feet|arms|legs|torso)_", nm) or sub == "Body parts & other character pieces" or "_bodyupper" in nm or "_bodylower" in nm:
                slot = "hands_feet"
            elif sub in ("Tops", "Outerwear") or any(k in nm for k in ("jacket", "shirt", "tshirt", "hoodie", "vest", "tanktop", "sweater", "coat", "blazer")):
                slot = "top"
            elif sub == "Bottoms" or any(k in nm for k in ("pants", "jeans", "shorts", "skirt", "leggings", "basebottoms")):
                slot = "bottom"
            elif sub == "Footwear" or any(k in nm for k in ("shoes", "boots", "sandals", "sneakers", "socks")):
                slot = "shoes"
            elif sub == "Hair & facial hair" or any(k in nm for k in ("_hair_", "beard", "brows", "lashes")):
                slot = "hair"
            elif (
                sub in ("Headwear & masks", "Jewelry & accessories")
                or any(k in nm for k in ("hat", "cap", "helmet", "shades", "glasses", "specs", "goggles", "mask", "backpack", "necklace", "piercing", "bandana", "hairaccessories", "fingernails", "toenails", "bracelet", "earring", "watch", "ring"))
            ):
                slot = "headwear"
            elif "_head_" in nm or sub == "Heads & faces":
                slot = "head"
            elif r["category"] == "Clothing & accessories":
                slot = "headwear"
            else:
                continue
            by_rig[rig][slot].append({"id": r["id"], "name": r["name"], "subcategory": sub})
        result = {rig: dict(slots) for rig, slots in by_rig.items()}
        with self.lock:
            self._wardrobe_catalog = result
        return result

    def wardrobe_items(self, rig, slot="", q="", limit=250, offset=0):
        self._ensure_meta()
        cat = self._get_wardrobe_catalog().get((rig or "").lower(), {})
        q_low = (q or "").strip().lower()
        slots_order = ["top", "bottom", "underwear", "body", "shoes", "headwear", "hands_feet", "hair", "head"]
        items = []
        for s in ([slot] if slot else slots_order):
            for it in cat.get(s, ()):
                if not q_low or q_low in it["name"].lower() or q_low in (it.get("subcategory") or "").lower():
                    items.append(it | {"slot": s})
        counts = {s: len(cat.get(s, ())) for s in slots_order}
        off = max(0, int(offset or 0))
        lim = max(1, min(2000, int(limit or 250)))
        return {"rig": rig, "counts": counts, "total": len(items), "offset": off, "limit": lim, "items": items[off:off + lim]}

    def _wardrobe_for(self, row):
        if row["category"] not in ("Protagonists", "Characters", "Clothing & accessories"):
            return None
        dname = self._decode_name(row)
        nm = dname.lower()
        m = re.search(r"_(fr1|mr1|fb1|mb1|fo1|mo1|fc1|mc1|mi1|mk1)_", nm)
        rig = m.group(1) if m else (row.get("pedArchetype", {}).get("bodyRig") or "")
        if not rig:
            return None
        cat = self._get_wardrobe_catalog().get(rig, {})
        if not cat:
            return None
        is_head = bool(re.search(r"(?:^|_)head_|legacyhead", nm))
        starter = []
        def _find(slot, *substrs):
            pool = cat.get(slot, ())
            for sub in substrs:
                hit = next((x["id"] for x in pool if sub in x["name"].lower() and x["id"] != row["id"]), None)
                if hit:
                    return hit
            return pool[0]["id"] if pool and pool[0]["id"] != row["id"] else None
        if is_head:
            if "lucia" in nm and rig == "fr1":
                starter = [x for x in (
                    _find("top", "cs_lucia_jacket", "player_story000", "tshirt"),
                    _find("bottom", "pantsjeans_fr1_000", "jeans", "pants"),
                    _find("shoes", "bootsbiker_fr1_000", "boots", "shoes"),
                    _find("hands_feet", "hand_fr1_000_full", "hand_fr1"),
                ) if x]
            elif "jason" in nm and rig == "mr1":
                starter = [x for x in (
                    _find("top", "cs_jason_jacket", "player_story000", "tshirt"),
                    _find("bottom", "pantsjeans_mr1_001_slim_mw", "jeans", "pants"),
                    _find("shoes", "bootsbiker_mr1_001", "boots", "shoes"),
                    _find("hands_feet", "hand_mr1_000_tshirt", "hand_mr1"),
                ) if x]
            else:
                starter = [x for x in (
                    _find("top", "jacket", "tshirt", "shirt"),
                    _find("bottom", "jeans", "pants", "shorts"),
                    _find("shoes", "shoes", "boots", "sneakers"),
                    _find("hands_feet", f"hand_{rig}_000_full", f"hand_{rig}"),
                ) if x]
        counts = {s: len(v) for s, v in cat.items()}
        initial = {s: cat.get(s, [])[:35] for s in ("top", "bottom", "underwear", "body", "shoes", "headwear", "hands_feet", "hair", "head") if cat.get(s)}
        return {"rig": rig, "isHead": is_head, "starterOutfit": starter, "counts": counts, "slots": initial}

    def _get_character_assemblies(self):
        """Build structural multi-part assemblies for named humans (cs_/player_/u_/sc_/hc_) and multi-part animals."""
        with self.lock:
            if self._character_assemblies is not None:
                return self._character_assemblies

        body_tok_re = re.compile(r"^[mf][a-z]{1,2}\d$")
        animal_part_re = re.compile(r"^(cr[a-z](?:_[a-z0-9]+)+?_[mf]_\d\d)_([a-z]+)_00$")
        skipped_types = ("hair", "beard", "brows", "lashes", "armpithair", "bodyhair", "stubble", "eyecap")
        head_subpart_re = re.compile(r"_(?:eyes|eyecap|earring|eyelashes|lashes|teeth|tattooprojection)(?:_|$)")
        families = (
            ("outer", ("jacket", "coat", "hoodie", "blazer", "parka", "cardigan", "poncho", "cape", "trenchcoat", "raincoat")),
            ("top", ("shirt", "tshirt", "tanktop", "sweatshirt", "sweater", "polo", "blouse", "top", "crop", "tube", "corset", "bodysuit")),
            ("bottom", ("pants", "shorts", "skirt", "jeans", "leggings", "trousers", "joggers", "chinos", "basebottoms")),
            ("full", ("dress", "fullbody", "jumpsuit", "overalls", "romper", "robe", "gown", "boilersuit", "swimsuit", "onesie")),
            ("bra", ("bra",)),
            ("feet", ("shoes", "boots", "sneakers", "sandals", "heels", "slippers", "flipflops", "trainers", "loafers")),
        )

        def _is_at_root_box(box_json):
            if not box_json:
                return False
            try:
                bx = json.loads(box_json) if isinstance(box_json, str) else box_json
                lo, hi = bx[0], bx[1]
                ext = max(hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2])
                mid_x = (lo[0] + hi[0]) * 0.5
                mid_z = (lo[2] + hi[2]) * 0.5
                return ext < 0.12 and mid_z < -0.85 and abs(mid_x) < 0.12
            except Exception:
                return False

        def _split_human(nm):
            toks = nm.split("_")
            for b in range(2, len(toks)):
                if body_tok_re.match(toks[b]):
                    return "_".join(toks[:b - 1]), toks[b - 1], toks[b], "_".join(toks[b + 1:])
            return None

        def _kind_of(owner):
            for pfx, lbl in (("cs_", "Cutscene"), ("player_", "Player"), ("u_", "Unique"), ("sc_", "Special"), ("hc_", "Story")):
                if owner.startswith(pfx):
                    return lbl
            return None

        def _pref(nm):
            return (0 if nm.endswith("_full") else 2) + (1 if ("_alt_" in nm or "_cull_" in nm) else 0)

        def _fam_of(typ):
            for fam, pfxs in families:
                if any(typ.startswith(p) for p in pfxs):
                    return fam
            return typ

        with closing(self.db()) as db:
            rows = db.execute("""
                select id, name, category, box, ibytes, low_detail, kind from model
                where status='indexed' and low_detail=0 and source != 'ped_archetype'
                  and category in ('Protagonists', 'Characters', 'Clothing & accessories', 'Animals')
                order by is_rep desc, coalesce(ibytes, 0) desc, id asc
            """).fetchall()
            pa_model_rows = db.execute("""
                select id, name, hash, subcategory, base_name from model
                where status='indexed' and source='ped_archetype'
            """).fetchall()

        # Best record per lowercase name, skipping tiny fragments/stubs
        by_name = {}
        for r in rows:
            nm = r["name"].lower()
            if nm in by_name:
                continue
            if r["kind"] == "fragment" and r["category"] == "Animals":
                continue
            by_name[nm] = {
                "id": r["id"],
                "name": r["name"],
                "lower": nm,
                "category": r["category"],
                "ibytes": r["ibytes"] or 0,
                "atRoot": _is_at_root_box(r["box"]),
            }

        def _base_skin(part, body):
            for suf in ("nude_cull", "nude_full", "full"):
                hit = by_name.get(f"{part}_{body}_000_{suf}")
                if hit and not hit["atRoot"]:
                    return hit
            return None

        def _base_genitals(body):
            # Only attach full standing-space adult genital meshes where real geometry exists (fr1 / mr1).
            cand = {"fr1": "bodygenitalsgen_fr1_008_regular_ws00_full",
                    "mr1": "bodygenitalsgen_mr1_000_regular_ws00_full"}.get(body)
            if not cand:
                return None
            hit = by_name.get(cand)
            if hit and not hit["atRoot"] and hit["ibytes"] >= 1000:
                return hit
            return None

        def _base_nipples(body):
            # Female fr1 torsos use a separate modular nipple mesh (male mr1 torsos bake nipples directly).
            if body != "fr1":
                return None
            hit = by_name.get("bodynipplesgen_fr1_000_regular_ws00_full")
            if hit and not hit["atRoot"] and hit["ibytes"] >= 500:
                return hit
            return None

        from runtime_faces import all_presets_for
        _anatomy_cache = {}
        _preset_tex_hashes = set()
        from ped_archetypes import MALE_GENITAL_SKINS, VERIFIED_CLEAN_FEET, VERIFIED_CLEAN_HAND
        pa_map = self._get_ped_archetypes()
        _rig_pa_up_norms = defaultdict(Counter)
        _rig_pa_lo_norms = defaultdict(Counter)
        for _pa in pa_map.values():
            _r = (_pa.get("body_rig") or "").lower()
            if not _r:
                continue
            if _pa.get("upper_skin_normal"):
                _rig_pa_up_norms[_r][_pa["upper_skin_normal"]] += 1
            if _pa.get("lower_skin_normal"):
                _rig_pa_lo_norms[_r][_pa["lower_skin_normal"]] += 1

        _anat_raw = {}
        for _rig in ("fr1", "mr1", "fb1", "mb1", "fo1", "mo1", "fc1", "mc1", "mi1", "mk1", "fsk1", "msk1"):
            _up_nm = next((f"hand_{_rig}_000_{s}" for s in ("nude_cull", "nude_full", "full") if f"hand_{_rig}_000_{s}" in by_name), "")
            _lo_nm = next((f"feet_{_rig}_000_{s}" for s in ("nude_cull", "nude_full", "full") if f"feet_{_rig}_000_{s}" in by_name), "")
            _up_p = all_presets_for(_up_nm) if _up_nm else {"albedos": [], "normals": []}
            _lo_p = all_presets_for(_lo_nm) if _lo_nm else {"albedos": [], "normals": []}
            _gen_p = (
                all_presets_for("bodygenitalsgen_fr1_008_regular_ws00_full") if _rig == "fr1"
                else (all_presets_for("bodygenitalsgen_mr1_000_regular_ws00_full_alt_01") if _rig == "mr1"
                      else {"albedos": [], "normals": []})
            )
            _anat_raw[_rig] = (_up_nm, _lo_nm, _up_p, _lo_p, _gen_p)
            for _d in (_up_p, _lo_p, _gen_p):
                _preset_tex_hashes.update(_d["albedos"])
                _preset_tex_hashes.update(_d["normals"])
            _preset_tex_hashes.update(_rig_pa_up_norms.get(_rig, ()))
            _preset_tex_hashes.update(_rig_pa_lo_norms.get(_rig, ()))
        for _g in ("F", "M"):
            for _rgb, _h, _lbl in VERIFIED_CLEAN_HAND[_g]:
                _preset_tex_hashes.add(_h)
            for _rgb, _h, _lbl in VERIFIED_CLEAN_FEET[_g]:
                _preset_tex_hashes.add(_h)
        for _rgb, _h in MALE_GENITAL_SKINS:
            _preset_tex_hashes.add(_h)

        _preset_tex_info = {}
        _ordered_pth = sorted(_preset_tex_hashes)
        for _i in range(0, len(_ordered_pth), 800):
            _preset_tex_info.update(self.texture_info(_ordered_pth[_i:_i + 800]))

        _MALE_GEN_LABELS = {
            "d27599939e20342f": "Very Fair",
            "bce4d91da23689c7": "Fair / Caucasian",
            "270a1487560e55ce": "Warm Tan",
            "0a5e0ae4e524948b": "Olive",
            "35598434464541b0": "Dark Brown",
            "46424e1f69765c64": "Deep Dark",
        }

        def _fmt_presets(hashes, labeled=()):
            res = []
            seen = set()
            for rgb, h, lbl in labeled:
                if h in seen:
                    continue
                srcs = _preset_tex_info.get(h)
                if srcs:
                    s0 = srcs[0]
                    entry = {"hash": h, "width": s0["width"], "height": s0["height"], "format": s0["format"], "label": lbl}
                    if rgb is not None:
                        entry["rgb"] = list(rgb)
                    res.append(entry)
                    seen.add(h)
            for h in hashes:
                if h in seen:
                    continue
                srcs = _preset_tex_info.get(h)
                if srcs:
                    s0 = srcs[0]
                    entry = {"hash": h, "width": s0["width"], "height": s0["height"], "format": s0["format"]}
                    if h in _MALE_GEN_LABELS:
                        entry["label"] = _MALE_GEN_LABELS[h]
                    res.append(entry)
                    seen.add(h)
            return res

        def _pa_prio(pa):
            nm = (pa.get("name") or "").lower()
            for idx_p, pfx in enumerate(("player_", "cs_", "u_", "sc_", "ig_", "s_", "g_", "a_")):
                if nm.startswith(pfx):
                    return idx_p
            return 9

        head_to_pa = {}
        owner_to_pa = {}
        for _pa in sorted(pa_map.values(), key=lambda x: (_pa_prio(x), x.get("name") or "")):
            _hd = (_pa.get("head_drawable") or "").lower()
            if _hd and _hd not in head_to_pa:
                head_to_pa[_hd] = _pa
            _onm = (_pa.get("name") or "").lower()
            if _onm and _onm not in owner_to_pa:
                owner_to_pa[_onm] = _pa
        if "player_story000_00" in owner_to_pa:
            for _al in ("cs_lucia", "hc_cs_lucia", "lucia", "player_lucia"):
                owner_to_pa.setdefault(_al, owner_to_pa["player_story000_00"])
        if "player_story000_01" in owner_to_pa:
            for _al in ("cs_jason", "hc_cs_jason", "cs_jason_old", "jason", "player_jason"):
                owner_to_pa.setdefault(_al, owner_to_pa["player_story000_01"])

        _ETH_FALLBACK = {
            ("F", "cau"): ("27d85c83d55837f1", "613a80072abe54c4", "", "181,137,123"),
            ("F", "asi"): ("09a57311dddec1fa", "53daad3cb9e23528", "", "170,126,100"),
            ("F", "lat"): ("73b0adca2de58beb", "06c4f867d482a5bf", "", "155,121,98"),
            ("F", "mix"): ("73b0adca2de58beb", "06c4f867d482a5bf", "", "155,121,98"),
            ("F", "mid"): ("73b0adca2de58beb", "06c4f867d482a5bf", "", "155,121,98"),
            ("F", "ind"): ("6a602971d0ad81ab", "e14fda608bef9ef5", "", "156,118,93"),
            ("F", "afr"): ("d1301a5592bf22b6", "7e4ad11f38c0f381", "", "99,69,57"),
            ("M", "cau"): ("0bee707e0b7523ef", "149f4a09284aaaa8", "d27599939e20342f", "180,136,122"),
            ("M", "asi"): ("b9667b40e0d354d3", "337d40a9e3c87a70", "270a1487560e55ce", "170,127,100"),
            ("M", "lat"): ("96f2c7a594e9a3e5", "e9f857ca3a4437aa", "0a5e0ae4e524948b", "155,121,98"),
            ("M", "mix"): ("96f2c7a594e9a3e5", "e9f857ca3a4437aa", "0a5e0ae4e524948b", "155,121,98"),
            ("M", "mid"): ("96f2c7a594e9a3e5", "e9f857ca3a4437aa", "0a5e0ae4e524948b", "155,121,98"),
            ("M", "ind"): ("788d82330e4c6e7d", "dac68494338f6bd5", "0a5e0ae4e524948b", "154,120,97"),
            ("M", "afr"): ("fac2d7272f698768", "b5ff690016fc006b", "35598434464541b0", "99,69,57"),
        }
        _ETH_RE = re.compile(r"_(cau|afr|lat|asi|mix|mid|ind)_")

        def _apply_ped_appearance(asm, pa, head_name=""):
            if head_name:
                asm["headDrawable"] = head_name
            if not pa:
                em = _ETH_RE.search((head_name or "").lower())
                body_tok = (asm.get("body") or "").lower()
                if em and body_tok:
                    g = "M" if body_tok.startswith("m") else "F"
                    fb = _ETH_FALLBACK.get((g, em.group(1)))
                    if fb:
                        up_a, lo_a, gen_a, rgb = fb
                        dskin = {"upperAlbedo": up_a, "lowerAlbedo": lo_a, "skinRgb": rgb}
                        if gen_a:
                            dskin["genitalAlbedo"] = gen_a
                        asm["defaultSkin"] = dskin
                return
            asm["pedName"] = pa.get("name") or asm.get("character") or ""
            asm["drpOffset"] = int(pa.get("drp_offset") or 0)
            asm["archHash"] = int(pa.get("arch_hash") or 0)
            if pa.get("face_albedo"):
                asm["faceAlbedo"] = pa["face_albedo"]
            if pa.get("head_drawable"):
                asm["headDrawable"] = pa["head_drawable"]
            dskin = {}
            if pa.get("upper_skin_albedo"):
                dskin["upperAlbedo"] = pa["upper_skin_albedo"]
            if pa.get("upper_skin_normal"):
                dskin["upperNormal"] = pa["upper_skin_normal"]
            if pa.get("lower_skin_albedo"):
                dskin["lowerAlbedo"] = pa["lower_skin_albedo"]
            if pa.get("lower_skin_normal"):
                dskin["lowerNormal"] = pa["lower_skin_normal"]
            if pa.get("genital_skin_albedo"):
                dskin["genitalAlbedo"] = pa["genital_skin_albedo"]
            if pa.get("eye_texture"):
                dskin["eyeAlbedo"] = pa["eye_texture"]
            if pa.get("skin_rgb"):
                dskin["skinRgb"] = pa["skin_rgb"]
            if dskin:
                asm["defaultSkin"] = dskin
            bm = pa.get("body_morphs")
            if isinstance(bm, dict) and bm:
                asm["bodyMorphs"] = bm
            hs = pa.get("height_scale")
            if isinstance(hs, (int, float)) and 0.6 <= hs <= 1.5:
                asm["heightScale"] = round(float(hs), 4)
            # Attach character-native 0DRP clothing MUV presets (e.g. cs_mandy -> leopard bikini 230f0639)
            from clothing_presets import get_store as get_clothing_store
            cstore = get_clothing_store()
            c_defs, c_all = cstore.character_presets(pa.get("name") or asm.get("character") or "")
            if c_defs:
                asm["clothingPresets"] = c_defs
                native_wardrobe = []
                for gnm_l, plist in sorted(c_all.items()):
                    if gnm_l.startswith(("hand_", "feet_", "head_", "bodygenitals", "bodynipples", "eyecap", "teeth", "fingernails", "toenails")):
                        continue
                    hit = by_name.get(gnm_l)
                    if hit and not hit["atRoot"]:
                        p0 = plist[0]
                        native_wardrobe.append({
                            "id": hit["id"],
                            "name": hit["name"],
                            "preset": p0["id"],
                            "label": p0["label"],
                            "swatches": p0["swatches"],
                            "hasPattern": p0["hasPattern"],
                            "presets": plist,
                        })
                if native_wardrobe:
                    asm["nativeWardrobe"] = native_wardrobe

        def _anatomy_variants(body):
            if body in _anatomy_cache:
                return _anatomy_cache[body]
            gen_specs = {
                "fr1": (
                    ("bodygenitalsgen_fr1_008_regular_ws00_full", "Base (6.83 cm)"),
                    ("bodygenitalsgen_fr1_008_regular_ws00_full_alt_00", "Alt 00 (6.88 cm)"),
                    ("bodygenitalsgen_fr1_008_regular_ws00_full_alt_01", "Alt 01 (7.23 cm)"),
                    ("bodygenitalsgen_fr1_008_regular_ws00_full_alt_02", "Alt 02 (7.58 cm)"),
                ),
                "mr1": (
                    ("bodygenitalsgen_mr1_000_regular_ws00_full", "Base — resting 1 (9.0 cm)"),
                    ("bodygenitalsgen_mr1_000_regular_ws00_full_alt_00", "Alt 00 — resting 2 (8.9 cm)"),
                    ("bodygenitalsgen_mr1_000_regular_ws00_full_alt_01", "Alt 01 — extended 1 (13.5 cm)"),
                    ("bodygenitalsgen_mr1_000_regular_ws00_full_alt_02", "Alt 02 — extended 2 (13.4 cm)"),
                ),
            }.get(body, ())
            genitals = []
            for idx_g, (gnm, glbl) in enumerate(gen_specs):
                hit = by_name.get(gnm)
                if hit and not hit["atRoot"] and hit["ibytes"] >= 1000:
                    genitals.append({"id": hit["id"], "name": hit["name"], "label": glbl, "default": idx_g == 0})
            detail_specs = {
                "fr1": (
                    ("bodypiercingsmulti_fr1_000_combo_ws00_full", "Body piercings combo"),
                    ("bodychainsjeweled_fr1_000_full_ws00_full", "Jeweled body chains"),
                    ("fingernailsnatural_fr1_000_short_ws00_full", "Fingernails — natural short"),
                    ("fingernailsnatural_fr1_001_medium_ws00_full", "Fingernails — natural medium"),
                    ("fingernailsbeauty_fr1_000_long_ws00_full", "Fingernails — long beauty 00"),
                    ("fingernailsbeauty_fr1_001_long_ws00_full", "Fingernails — long beauty 01"),
                    ("fingernailsbeauty_fr1_002_long_ws00_full", "Fingernails — long beauty 02"),
                    ("toenailsnatural_fr1_000_short_ws00_full", "Toenails — natural short"),
                ),
                "mr1": (
                    ("bodypiercingsmulti_mr1_000_combo_ws00_full", "Body piercings combo"),
                    ("fingernailsnatural_mr1_000_short_ws00_full", "Fingernails — natural short"),
                ),
            }.get(body, ())
            details = []
            for dnm, dlbl in detail_specs:
                hit = by_name.get(dnm)
                if hit and not hit["atRoot"]:
                    details.append({"id": hit["id"], "name": hit["name"], "label": dlbl})
            raw = _anat_raw.get(body)
            skin_presets = {}
            if raw:
                _up_nm, _lo_nm, _up_p, _lo_p, _gen_p = raw
                _g = "M" if body.startswith("m") else "F"
                _pa_up_n = [h for h, _c in _rig_pa_up_norms.get(body, Counter()).most_common()]
                _pa_lo_n = [h for h, _c in _rig_pa_lo_norms.get(body, Counter()).most_common()]
                _gen_labeled = [(rgb, h, _MALE_GEN_LABELS.get(h, h[:8])) for rgb, h in MALE_GENITAL_SKINS] if body == "mr1" else ()
                up_a = _fmt_presets(_up_p["albedos"], VERIFIED_CLEAN_HAND.get(_g, ()))
                up_n = _fmt_presets(list(_up_p["normals"]) + _pa_up_n)
                lo_a = _fmt_presets(_lo_p["albedos"], VERIFIED_CLEAN_FEET.get(_g, ()))
                lo_n = _fmt_presets(list(_lo_p["normals"]) + _pa_lo_n)
                gn_a = _fmt_presets(_gen_p["albedos"], _gen_labeled)
                gn_n = _fmt_presets(_gen_p["normals"])
                if len(up_a) > 1:
                    skin_presets["upperAlbedo"] = up_a
                if len(up_n) > 1:
                    skin_presets["upperNormal"] = up_n
                if len(lo_a) > 1:
                    skin_presets["lowerAlbedo"] = lo_a
                if len(lo_n) > 1:
                    skin_presets["lowerNormal"] = lo_n
                if len(gn_a) > 1:
                    skin_presets["genitalAlbedo"] = gn_a
                if len(gn_n) > 1:
                    skin_presets["genitalNormal"] = gn_n
            try:
                from char_outfits import eye_presets_for_rig
                ep_raw = eye_presets_for_rig(body)
                if ep_raw:
                    skin_presets["eyeAlbedo"] = [
                        {
                            "hash": e["hash"],
                            "label": e["label"],
                            "hex": e.get("hex", ""),
                            "width": 512,
                            "height": 512,
                            "format": "BC7_UNORM_SRGB",
                        }
                        for e in ep_raw
                    ]
            except Exception:
                pass
            anat = None
            if genitals or details or skin_presets:
                anat = {"genitals": genitals, "details": details, "skinPresets": skin_presets}
            _anatomy_cache[body] = anat
            return anat

        human_groups = defaultdict(list)
        animal_groups = defaultdict(list)
        for nm, rec in by_name.items():
            am = animal_part_re.match(nm)
            if am:
                animal_groups[am.group(1)].append((rec, am.group(2)))
                continue
            sp = _split_human(nm)
            if sp and _kind_of(sp[0]):
                human_groups[sp[0]].append((rec, sp[1], sp[2], sp[3]))

        by_model_id = {}

        # 1. Named human assemblies (cutscene / player / unique / special / story characters)
        for owner, parts in human_groups.items():
            if owner.startswith("hc_cs_") and owner[3:] in human_groups:
                continue
            heads = [p for p in parts if p[1] == "head" and not head_subpart_re.search("_" + p[3])]
            eyes = [p for p in parts if p[1] == "head" and re.search(r"(?:^|_)eyes(?:_|$)", p[3])]
            teeth_parts = [p for p in parts if (p[1] == "head" and re.search(r"(?:^|_)teeth(?:_|$)", p[3])) or p[1].startswith("teeth")]
            if owner.startswith("cs_") and ("hc_" + owner) in human_groups:
                donor = human_groups["hc_" + owner]
                if not heads:
                    heads = [p for p in donor if p[1] == "head" and not head_subpart_re.search("_" + p[3])]
                if not eyes:
                    eyes = [p for p in donor if p[1] == "head" and re.search(r"(?:^|_)eyes(?:_|$)", p[3])]
            clothing = [p for p in parts if p[1] != "head" and not p[1].startswith("teeth")
                        and not any(p[1].startswith(s) for s in skipped_types) and not p[0]["atRoot"]]
            if not heads and not clothing:
                continue

            pieces = []
            head = min(heads, key=lambda p: (_pref(p[0]["lower"]), p[0]["lower"])) if heads else None
            if head:
                pieces.append({"id": head[0]["id"], "name": head[0]["name"], "slot": "Head"})
                stem = head[0]["lower"][:-5] if head[0]["lower"].endswith("_full") else head[0]["lower"]
                if eyes:
                    eye = min(eyes, key=lambda p: (0 if p[0]["lower"].startswith(stem + "_eyes") else 1, _pref(p[0]["lower"]), p[0]["lower"]))
                    pieces.append({"id": eye[0]["id"], "name": eye[0]["name"], "slot": "Eyes"})
                if teeth_parts:
                    tp = min(teeth_parts, key=lambda p: (_pref(p[0]["lower"]), p[0]["lower"]))
                    pieces.append({"id": tp[0]["id"], "name": tp[0]["name"], "slot": "Teeth"})

            chosen = {}
            for p in sorted(clothing, key=lambda x: (_pref(x[0]["lower"]), x[0]["lower"])):
                fam = _fam_of(p[1])
                if fam not in chosen:
                    chosen[fam] = p
            if "full" in chosen and ("top" in chosen or "bottom" in chosen):
                chosen.pop("full", None)
            for p in sorted(chosen.values(), key=lambda x: x[0]["lower"]):
                pieces.append({"id": p[0]["id"], "name": p[0]["name"], "slot": "Skin" if p[1].startswith("body") else "Clothing"})

            body_counts = defaultdict(int)
            for p in parts:
                body_counts[p[2]] += 1
            body = head[2] if head else max(body_counts, key=body_counts.get)
            # Always provide clean nude base skin (hand + feet + adult rig genitals) unless the character has its own full body skin.
            if not any(p[1].startswith("body") for p in chosen.values()):
                hand = _base_skin("hand", body)
                if hand:
                    pieces.append({"id": hand["id"], "name": hand["name"], "slot": "Skin"})
                feet = _base_skin("feet", body)
                if feet:
                    pieces.append({"id": feet["id"], "name": feet["name"], "slot": "Skin"})
                gen = _base_genitals(body)
                if gen:
                    pieces.append({"id": gen["id"], "name": gen["name"], "slot": "Skin"})
                nip = _base_nipples(body)
                if nip:
                    pieces.append({"id": nip["id"], "name": nip["name"], "slot": "Skin"})

            if len(pieces) >= 2:
                asm = {"character": owner, "kind": _kind_of(owner), "body": body, "parts": pieces}
                anat = _anatomy_variants(body)
                if anat:
                    asm["anatomy"] = anat
                pa_match = owner_to_pa.get(owner) or (head_to_pa.get(head[0]["lower"]) if head else None)
                _apply_ped_appearance(asm, pa_match, head[0]["lower"] if head else "")
                for p in pieces:
                    if p["slot"] in ("Head", "Eyes", "Teeth") or (p["slot"] == "Skin" and p["name"].lower().startswith(owner)):
                        by_model_id.setdefault(p["id"], asm)

        # 1b. Universal nude body assembly for EVERY remaining human character head (head_*, hc_*, cs_*, u_*, sc_*, a_head_*)
        # so all characters default to full nude bodies (Head + Eyes + Teeth + nude rig skin) instead of floating heads.
        any_head_re = re.compile(r"^(?:[a-z0-9_]+_)?head_(f[a-z]{1,2}\d|m[a-z]{1,2}\d)_")
        for nm, rec in by_name.items():
            if rec["id"] in by_model_id or rec["atRoot"]:
                continue
            if rec["category"] not in ("Protagonists", "Characters"):
                continue
            if nm.startswith("slod_") or "gore" in nm or "mannequin" in nm or head_subpart_re.search(nm):
                continue
            hm = any_head_re.match(nm)
            if not hm:
                continue
            body = hm.group(1)
            stem = nm[:-5] if nm.endswith("_full") else nm
            pieces = [{"id": rec["id"], "name": rec["name"], "slot": "Head"}]
            for suf, slot in (("_eyes_full", "Eyes"), ("_eyes", "Eyes"), ("_teeth_full", "Teeth"), ("_teeth", "Teeth")):
                hit = by_name.get(stem + suf)
                if hit and not hit["atRoot"] and not any(p["slot"] == slot for p in pieces):
                    pieces.append({"id": hit["id"], "name": hit["name"], "slot": slot})
            hand = _base_skin("hand", body)
            if hand:
                pieces.append({"id": hand["id"], "name": hand["name"], "slot": "Skin"})
            feet = _base_skin("feet", body)
            if feet:
                pieces.append({"id": feet["id"], "name": feet["name"], "slot": "Skin"})
            gen = _base_genitals(body)
            if gen:
                pieces.append({"id": gen["id"], "name": gen["name"], "slot": "Skin"})
            nip = _base_nipples(body)
            if nip:
                pieces.append({"id": nip["id"], "name": nip["name"], "slot": "Skin"})
            if len(pieces) >= 2:
                asm = {"character": stem, "kind": "Character", "body": body, "parts": pieces}
                anat = _anatomy_variants(body)
                if anat:
                    asm["anatomy"] = anat
                _apply_ped_appearance(asm, head_to_pa.get(nm), nm)
                by_model_id[rec["id"]] = asm

        # 1c. Faithful ped archetype assemblies (00011d1ff000.pso + 0DRP catalogues):
        # default to full nude body (Head + Eyes + Teeth + nude rig skin + adult genitals + female nipples)
        # while attaching each archetype's real in-game 0DRP outfit drawables as optional Clothing toggles.
        for pr in pa_model_rows:
            pa = pa_map.get(int(pr["hash"]))
            if not pa:
                continue
            dname_l = (pr["base_name"] or "").lower()
            if "legacyped" in dname_l or "fullbody" in dname_l:
                continue
            body = pa.get("body_rig") or ""
            if not body:
                bm = re.search(r"_(f[a-z]{1,2}\d|m[a-z]{1,2}\d)_", dname_l)
                body = bm.group(1) if bm else ""
            stem = dname_l[:-5] if dname_l.endswith("_full") else dname_l
            pieces = [{"id": pr["id"], "name": pr["name"], "slot": "Head"}]
            seen_ids = {pr["id"]}

            eye_nm = (pa.get("eye_drawable") or "").lower()
            eye_hit = by_name.get(eye_nm) if eye_nm else None
            if not eye_hit:
                for suf in ("_eyes_full", "_eyes"):
                    cand = by_name.get(stem + suf)
                    if cand and not cand["atRoot"]:
                        eye_hit = cand
                        break
            if eye_hit and not eye_hit["atRoot"] and eye_hit["id"] not in seen_ids:
                pieces.append({"id": eye_hit["id"], "name": eye_hit["name"], "slot": "Eyes"})
                seen_ids.add(eye_hit["id"])

            teeth_nm = (pa.get("teeth_drawable") or "").lower()
            teeth_hit = by_name.get(teeth_nm) if teeth_nm else None
            if not teeth_hit:
                for cand_nm in (stem + "_teeth_full", stem + "_teeth", f"teethmodular_{body}_000_regular_ws00_full"):
                    cand = by_name.get(cand_nm)
                    if cand and not cand["atRoot"]:
                        teeth_hit = cand
                        break
            if teeth_hit and not teeth_hit["atRoot"] and teeth_hit["id"] not in seen_ids:
                pieces.append({"id": teeth_hit["id"], "name": teeth_hit["name"], "slot": "Teeth"})
                seen_ids.add(teeth_hit["id"])

            bespoke_bodies = [by_name[b.lower()] for b in (pa.get("body_drawables") or ()) if b.lower() in by_name and not by_name[b.lower()]["atRoot"]]
            if bespoke_bodies:
                for bh in bespoke_bodies:
                    if bh["id"] not in seen_ids:
                        pieces.append({"id": bh["id"], "name": bh["name"], "slot": "Skin"})
                        seen_ids.add(bh["id"])
            elif body:
                for part_hit in (_base_skin("hand", body), _base_skin("feet", body), _base_genitals(body), _base_nipples(body)):
                    if part_hit and part_hit["id"] not in seen_ids:
                        pieces.append({"id": part_hit["id"], "name": part_hit["name"], "slot": "Skin"})
                        seen_ids.add(part_hit["id"])

            for cnm in (pa.get("clothing_drawables") or ()):
                chit = by_name.get(cnm.lower())
                if chit and not chit["atRoot"] and chit["id"] not in seen_ids:
                    pieces.append({"id": chit["id"], "name": chit["name"], "slot": "Clothing"})
                    seen_ids.add(chit["id"])

            if len(pieces) >= 2:
                kind_lbl = "Story Character" if pr["subcategory"] == "Story characters" else "Ped Archetype"
                asm = {"character": pr["name"], "kind": kind_lbl, "body": body, "parts": pieces}
                anat = _anatomy_variants(body)
                if anat:
                    asm["anatomy"] = anat
                _apply_ped_appearance(asm, pa)
                by_model_id[pr["id"]] = asm

        animal_skip = {"fur", "antlerb", "harness", "collar", "collarharness", "hat", "shirt", "costume",
                       "goggles", "cape", "flower", "lace", "ribbon", "rope", "tranquilizer", "crown",
                       "jewel", "vest", "badge", "knife", "corpse", "roadkill", "law", "sheriff", "css"}
        # 2. Multi-part animal assemblies
        for stem, aparts in animal_groups.items():
            clean_parts = [(rec, slot) for rec, slot in aparts if slot not in animal_skip]
            if not any(slot == "body" for _, slot in clean_parts):
                continue
            pieces = [
                {"id": rec["id"], "name": rec["name"], "slot": slot.capitalize()}
                for rec, slot in sorted(clean_parts, key=lambda x: (0 if x[1] == "body" else 1, x[0]["lower"]))
            ]
            if len(pieces) >= 2:
                asm = {"character": stem, "kind": "Animal", "body": "", "parts": pieces}
                for p in pieces:
                    by_model_id.setdefault(p["id"], asm)

        with self.lock:
            self._character_assemblies = by_model_id
        return by_model_id

    def _character_assembly_for(self, row):
        if row["category"] not in ("Protagonists", "Characters", "Clothing & accessories", "Animals"):
            return None
        return self._get_character_assemblies().get(row["id"])

    def _get_name_to_model_id(self):
        with self.lock:
            if self._name_to_model_id is not None:
                return self._name_to_model_id
        out = {}
        with closing(self.db()) as db:
            for r in db.execute(
                "select id, name, base_name, source, vbytes, ibytes from model "
                "where status='indexed' and name is not null order by (source='ped_archetype') asc, ibytes desc"
            ).fetchall():
                nm = (r["name"] or "").lower()
                if nm and nm not in out:
                    out[nm] = int(r["id"])
                bnm = (r["base_name"] or "").lower()
                if bnm and bnm not in out:
                    out[bnm] = int(r["id"])
        with self.lock:
            self._name_to_model_id = out
        return out

    def _attach_poly_ids(self, entry, name_map):
        if not isinstance(entry, dict):
            return
        pm = (entry.get("polyMeshDrawable") or (entry.get("drawable") if entry.get("hasPolyMesh") else "") or "").lower()
        if pm and pm in name_map:
            entry["polyMeshModelId"] = name_map[pm]

    def _enrich_character_assembly(self, ident, row, ca):
        if not ca or ca.get("kind") == "Animal":
            return ca
        out_ca = dict(ca)
        rig = (out_ca.get("body") or "").lower()
        dskin = dict(out_ca.get("defaultSkin") or {})

        # 1. Collar / waist skin harmonization (#6b)
        face_alb = out_ca.get("faceAlbedo") or ""
        if not face_alb and row:
            for m in (row.get("materials") or ()):
                for t in (m.get("textures") or ()):
                    if t.get("parameter") == "c8e8c282" and t.get("sources"):
                        s0 = t["sources"][0]
                        if "SRGB" in (s0.get("format") or ""):
                            face_alb = t.get("effectiveHash") or t.get("textureHash") or ""
                            break
                if face_alb:
                    break
        up_alb = dskin.get("upperAlbedo") or ""
        lo_alb = dskin.get("lowerAlbedo") or ""
        if face_alb and up_alb and rig:
            skey = (face_alb, up_alb, lo_alb, rig)
            with self.lock:
                bal_pack = self._skin_balance_cache.get(skey)
            if bal_pack is None:
                try:
                    import skin_harmonize as sh
                    from ped_archetypes import VERIFIED_CLEAN_HAND
                    bal = sh.compute_skin_color_balance(self, face_alb, up_alb, rig, face_model_id=ident)
                    chosen_up = up_alb
                    if bal.get("severe_mismatch"):
                        g = "M" if rig.startswith("m") else "F"
                        cands = [h for _rgb, h, _lbl in VERIFIED_CLEAN_HAND.get(g, ())]
                        rec = sh.recommend_clean_upper_skin(self, face_alb, rig, cands, face_model_id=ident)
                        if rec and rec.get("hash") and rec["hash"] != up_alb:
                            if (bal.get("deltaE_before", 0.0) - rec.get("deltaE_before", 99.0)) > 2.5:
                                chosen_up = rec["hash"]
                                bal = sh.compute_skin_color_balance(self, face_alb, chosen_up, rig, face_model_id=ident)
                    up_gain = bal.get("gain_srgb") or [1.0, 1.0, 1.0]
                    lo_gain = list(up_gain)
                    if lo_alb:
                        wbal = sh.compute_waist_color_balance(self, chosen_up, lo_alb, rig, upper_gain=up_gain)
                        lo_gain = wbal.get("gain_srgb") or lo_gain
                    bal_pack = {
                        "upperAlbedo": chosen_up,
                        "upperGain": [round(float(x), 4) for x in up_gain],
                        "lowerGain": [round(float(x), 4) for x in lo_gain],
                        "neckDeltaE": {
                            "before": round(float(bal.get("deltaE_before", 0.0)), 2),
                            "after": round(float(bal.get("deltaE_after", 0.0)), 2),
                        },
                    }
                except Exception:
                    bal_pack = {}
                with self.lock:
                    self._skin_balance_cache[skey] = bal_pack
            if bal_pack:
                if bal_pack.get("upperAlbedo"):
                    dskin["upperAlbedo"] = bal_pack["upperAlbedo"]
                if bal_pack.get("upperGain"):
                    dskin["upperGain"] = bal_pack["upperGain"]
                if bal_pack.get("lowerGain"):
                    dskin["lowerGain"] = bal_pack["lowerGain"]
                if bal_pack.get("neckDeltaE"):
                    dskin["neckDeltaE"] = bal_pack["neckDeltaE"]
        if dskin:
            out_ca["defaultSkin"] = dskin

        # 2. Default hair, eyebrows, eyelashes, beard, body hair & color (#1)
        drp_off = int(out_ca.get("drpOffset") or 0)
        ped_name = out_ca.get("pedName") or out_ca.get("character") or (row.get("name") if row else "") or ""
        head_drawable = out_ca.get("headDrawable") or (row.get("base_name") or row.get("name") if row else "") or ""
        skin_rgb = dskin.get("skinRgb") or ""
        if rig or drp_off:
            gkey = (drp_off, ped_name.lower(), rig, head_drawable.lower(), skin_rgb)
            with self.lock:
                gdata = self._char_groom_cache.get(gkey)
            if gdata is None:
                try:
                    import char_groom as cg
                    drp_bytes = cg.read_0drp_bytes(drp_off) if drp_off else None
                    gdata = cg.extract_drp_groom_data(
                        drp_bytes,
                        ped_name=ped_name,
                        rig=rig,
                        head_drawable=head_drawable,
                        skin_rgb=skin_rgb,
                        include_rig_fallbacks=True,
                    )
                    nmap = self._get_name_to_model_id()
                    for slot_k, ent in (gdata.get("defaults") or {}).items():
                        if isinstance(ent, list):
                            for sub_e in ent:
                                self._attach_poly_ids(sub_e, nmap)
                        else:
                            self._attach_poly_ids(ent, nmap)
                    for slot_k, vlist in (gdata.get("variants") or {}).items():
                        if isinstance(vlist, list):
                            for sub_e in vlist:
                                self._attach_poly_ids(sub_e, nmap)
                except Exception:
                    gdata = None
                with self.lock:
                    self._char_groom_cache[gkey] = gdata
            if gdata:
                out_ca["defaultGroom"] = gdata
        return out_ca

    def _drp_context_for_model(self, ident):
        asm = self._get_character_assemblies().get(ident)
        if asm and asm.get("drpOffset"):
            return int(asm["drpOffset"]), int(asm.get("archHash") or 0), (asm.get("pedName") or asm.get("character") or "")
        with closing(self.db()) as db:
            r = db.execute("select name, base_name, hash, source from model where id=?", (ident,)).fetchone()
        if not r:
            return 0, 0, ""
        pa_map = self._get_ped_archetypes()
        pa = pa_map.get(int(r["hash"])) if r["source"] == "ped_archetype" else None
        if not pa:
            nm = (r["name"] or "").lower()
            bnm = (r["base_name"] or "").lower()
            for cand in pa_map.values():
                if (cand.get("name") or "").lower() == nm or (cand.get("head_drawable") or "").lower() in (nm, bnm):
                    pa = cand
                    break
        if pa:
            return int(pa.get("drp_offset") or 0), int(pa.get("arch_hash") or 0), (pa.get("name") or r["name"] or "")
        return 0, 0, (r["name"] or "")

    def char_outfits_for(self, ident):
        drp_off, arch_hash, ped_name = self._drp_context_for_model(ident)
        if not drp_off:
            return {"pedName": ped_name, "outfits": [], "partOverrides": {}}
        ckey = (drp_off, arch_hash)
        with self.lock:
            cached = self._char_outfits_cache.get(ckey)
        if cached is not None:
            return cached
        import char_outfits as co
        raw = co.extract_ped_outfits_by_offset(drp_off, arch_hash=arch_hash)
        nmap = self._get_name_to_model_id()
        outfits_out = []
        for o in (raw.get("outfits") or ()):
            parts_out = []
            for p in (o.get("parts") or ()):
                dnm = (p.get("drawable") or "").lower()
                mid = nmap.get(dnm)
                pe = dict(p)
                if mid is not None:
                    pe["id"] = mid
                parts_out.append(pe)
            if any(p.get("id") for p in parts_out):
                oe = dict(o)
                oe["parts"] = parts_out
                outfits_out.append(oe)
        part_ov = {}
        for dnm, ov_d in (raw.get("part_overrides") or {}).items():
            part_ov[dnm.lower()] = ov_d
            mid = nmap.get(dnm.lower())
            if mid is not None:
                part_ov[str(mid)] = ov_d
        res = {
            "pedName": ped_name,
            "outfits": outfits_out,
            "partOverrides": part_ov,
            "eyeTexture": raw.get("eye_texture") or "",
            "eyeDrawable": raw.get("eye_drawable") or "",
        }
        with self.lock:
            self._char_outfits_cache[ckey] = res
        return res

    def char_overlays_for(self, ident):
        drp_off, arch_hash, ped_name = self._drp_context_for_model(ident)
        if not drp_off:
            return {"pedName": ped_name, "tattoos": [], "faceOverlays": [], "makeupLooks": [], "counts": {}}
        ckey = (drp_off, arch_hash, ped_name.lower())
        with self.lock:
            cached = self._char_overlays_cache.get(ckey)
        if cached is not None:
            return cached
        import char_overlays as cov
        res = cov.extract_ped_overlays_by_drp_offset(drp_off, arch_hash=arch_hash, ped_name=ped_name)
        with self.lock:
            self._char_overlays_cache[ckey] = res
        return res

    def groom_catalog(self, rig):
        rk = (rig or "fr1").strip().lower()
        with self.lock:
            cached = self._groom_catalog_cache.get(rk)
        if cached is not None:
            return cached
        import char_groom as cg
        cat = cg.groom_catalog_for_rig(rk)
        nmap = self._get_name_to_model_id()
        for slot_k in ("hair", "brows", "lashes", "beard", "bodyhair", "accessories"):
            for ent in (cat.get(slot_k) or ()):
                self._attach_poly_ids(ent, nmap)
        with self.lock:
            self._groom_catalog_cache[rk] = cat
        return cat

    # Geometry --------------------------------------------------------------------------------------
    def glb(self, ident, lod=0):
        from model_decode import decode
        key = (ident, lod)
        with self.lock:
            lock = self.decode_locks.setdefault(key, threading.Lock())
        with lock:
            with closing(self.db()) as db:
                r = db.execute("select name, base_name, source, archive, member, root, kind from model where id=?", (ident,)).fetchone()
            if not r:
                raise ValueError("Unknown model")
            dname = self._decode_name(r)
            source = f"{r['archive']}|{r['member']:#x}|{r['root']:#x}|{dname}|lod{lod}"
            with self.lock:
                hit = self.decoded.execute("select status,error,file,source from decoded where id=? and lod=?",
                                           (ident, lod)).fetchone()
            if hit and hit[3] == source:
                if hit[0] == "decoded" and (CACHE / "glb" / hit[2]).is_file():
                    return (CACHE / "glb" / hit[2]).read_bytes()
                if hit[0] == "failed":
                    raise ValueError(hit[1])
            folder = CACHE / "glb" / f"{ident % 256:02x}"
            try:
                info = decode(r["archive"], r["member"], r["root"], dname, r["kind"], folder, lod=lod)
            except Exception as exc:
                with self.lock:
                    self.decoded.execute(
                        "insert or replace into decoded(id,lod,status,error,source) values (?,?,?,?,?)",
                        (ident, lod, "failed", str(exc)[:500], source))
                    self.decoded.commit()
                raise ValueError(f"Decode failed: {exc}")
            rel = f"{folder.name}/{info['file']}"
            keep = {k: info[k] for k in ("geometryBounds", "drawableBounds", "boundsOffset", "boundsDimensionsErrorM",
                                         "fragmentBoundsCheck", "decodePath", "lodLevels", "lod", "atRootStub") if k in info}
            with self.lock:
                self.decoded.execute("insert or replace into decoded values (?,?,?,?,?,?,?,?,?,?)",
                                     (ident, lod, "decoded", None, info["vertices"], info["triangles"], info["parts"],
                                      rel, json.dumps(keep), source))
                self.decoded.commit()
            return (CACHE / "glb" / rel).read_bytes()

    # Textures --------------------------------------------------------------------------------------
    def archive(self, path):
        from ttpf_format import TtpfArchive
        with self.lock:
            arc = self.archives.get(path)
            if arc is not None:
                self.archives.move_to_end(path)
                return arc
        with self._archive_lock(path):
            with self.lock:
                arc = self.archives.get(path)
                if arc is not None:
                    self.archives.move_to_end(path)
                    return arc
            arc = TtpfArchive(GAME / path)
            with self.lock:
                self.archives[path] = arc
                while len(self.archives) > 96:
                    old_path, old_arc = self.archives.popitem(last=False)
                    if old_path != path:
                        try:
                            old_arc.close()
                        except Exception:
                            pass
                return arc

    def texture(self, texture_hash, max_size, model_id=0):
        """PNG of the largest decodable mip no bigger than max_size; supports .ttpf, native RPF, and embedded TXD textures."""
        from ttpf_format import png_bytes
        if not texture_hash or len(texture_hash) != 16 or any(c not in "0123456789abcdef" for c in texture_hash):
            raise ValueError("Invalid texture hash")
        sources = self.texture_info([texture_hash]).get(texture_hash)
        if not sources and model_id > 0:
            try:
                self.item(model_id)
                sources = self.texture_info([texture_hash]).get(texture_hash)
            except Exception:
                pass
        if not sources:
            raise LookupError("Texture not present in any local texture pack")
        src = sources[0]
        if src.get("area") == "embedded":
            from model_decode import decode_embedded_png
            with self.lock:
                emb_rec = self._embedded_index.get(texture_hash)
            if not emb_rec and (model_id or src.get("embeddedModelId")):
                try:
                    self.item(model_id or int(src["embeddedModelId"]))
                    with self.lock:
                        emb_rec = self._embedded_index.get(texture_hash)
                except Exception:
                    pass
            if not emb_rec:
                raise LookupError("Embedded texture metadata unavailable")
            arc_name, raw_member, raw_root, mname, mkind, emb_meta, _ = emb_rec
            levels = ([i for i in range(emb_meta["mips"])
                       if max(max(1, emb_meta["width"] >> i), max(1, emb_meta["height"] >> i)) <= max_size]
                      or [emb_meta["mips"] - 1])
            cached = CACHE / "tex" / f"{texture_hash}_e{levels[0]}.png"
            if cached.is_file():
                return cached.read_bytes(), levels[0]
            blob, level = decode_embedded_png(arc_name, raw_member, raw_root, mname, mkind, texture_hash, max_size=max_size)
            cached = CACHE / "tex" / f"{texture_hash}_e{level}.png"
            cached.write_bytes(blob)
            return blob, level
        if src.get("nativeId"):
            from pso_native_texture_image import native_png
            recs = self._get_native_textures().get(texture_hash, ())
            if not recs:
                raise LookupError("Native RPF texture record unavailable")
            rec = recs[0]
            levels = ([i for i in range(rec["mips"])
                       if max(max(1, rec["width"] >> i), max(1, rec["height"] >> i)) <= max_size]
                      or [rec["mips"] - 1])
            errors = []
            for level in levels:
                cached = CACHE / "tex" / f"{texture_hash}_n{level}.png"
                if cached.is_file():
                    return cached.read_bytes(), level
                try:
                    blob = native_png(GAME, rec, level=level, slice_index=0)
                    cached.write_bytes(blob)
                    return blob, level
                except Exception as exc:
                    errors.append(f"mip {level}: {exc}")
            raise ValueError("No decodable native mip: " + "; ".join(errors[:3]))
        arc = self.archive(src["path"])
        t = arc.textures[src["idx"]]
        levels = [i for i, m in enumerate(t["mips"]) if max(m["width"], m["height"]) <= max_size] or [len(t["mips"]) - 1]
        errors = []
        from ttpf_format import decode_pixels
        for level in levels:
            cached = CACHE / "tex" / f"{texture_hash}_{level}.png"
            if cached.is_file():
                return cached.read_bytes(), level
            try:
                with self._archive_lock(src["path"]):
                    linear = arc.mip_linear(t, level, 0)
                mip = t["mips"][level]
                picture = decode_pixels(linear, t["dxgi"], mip["width"], mip["height"])
                blob = png_bytes(picture.convert("RGBA"))
                cached.write_bytes(blob)
                return blob, level
            except Exception as exc:
                errors.append(f"mip {level}: {exc}")
        raise ValueError("No decodable mip: " + "; ".join(errors[:3]))

    # Multi-core batched geometry & direct GPU-compressed BCn texture streaming --------------------
    def glb_batch(self, ids, lod=0):
        """Return GBAT binary bundle for up to 32 model IDs, decoding cold models in parallel across CPU cores."""
        import struct
        from batch_workers import decode_glb_worker
        clean_ids = []
        seen = set()
        for ident in ids:
            if ident not in seen:
                seen.add(ident)
                clean_ids.append(ident)
            if len(clean_ids) >= 32:
                break
        if not clean_ids:
            return struct.pack("<4sI", b"GBAT", 0)

        results = {}
        cold_jobs = []
        placeholders = ",".join("?" * len(clean_ids))
        with closing(self.db()) as db:
            meta_rows = {
                r["id"]: r
                for r in db.execute(
                    f"select id, name, base_name, source, archive, member, root, kind from model where id in ({placeholders})",
                    clean_ids,
                ).fetchall()
            }
        with self.lock:
            dec_rows = {
                r[0]: r
                for r in self.decoded.execute(
                    f"select id, status, error, file, source from decoded where lod=? and id in ({placeholders})",
                    [lod, *clean_ids],
                ).fetchall()
            }
        for ident in clean_ids:
            r = meta_rows.get(ident)
            if not r:
                results[ident] = b""
                continue
            dname = self._decode_name(r)
            source = f"{r['archive']}|{r['member']:#x}|{r['root']:#x}|{dname}|lod{lod}"
            hit = dec_rows.get(ident)
            if hit and hit[4] == source:
                if hit[1] == "decoded":
                    p = CACHE / "glb" / hit[3]
                    if p.is_file():
                        results[ident] = p.read_bytes()
                        continue
                elif hit[1] == "failed":
                    results[ident] = b""
                    continue
            folder = CACHE / "glb" / f"{ident % 256:02x}"
            cold_jobs.append((ident, lod, r["archive"], r["member"], r["root"], dname, r["kind"], str(folder), source))

        if cold_jobs:
            if len(cold_jobs) == 1:
                outs = [decode_glb_worker(cold_jobs[0])]
            else:
                pool = self._get_pool()
                outs = list(pool.map(decode_glb_worker, cold_jobs))
            with self.lock:
                for ident, lv, ok, rel_or_err, verts, tris, parts, keep, source, data in outs:
                    if ok:
                        self.decoded.execute(
                            "insert or replace into decoded values (?,?,?,?,?,?,?,?,?,?)",
                            (ident, lv, "decoded", None, verts, tris, parts, rel_or_err, json.dumps(keep), source),
                        )
                        results[ident] = data
                    else:
                        self.decoded.execute(
                            "insert or replace into decoded(id,lod,status,error,source) values (?,?,?,?,?)",
                            (ident, lv, "failed", rel_or_err, source),
                        )
                        results[ident] = b""
                self.decoded.commit()

        chunks = [struct.pack("<4sI", b"GBAT", len(clean_ids))]
        for ident in clean_ids:
            blob = results.get(ident, b"")
            chunks.append(struct.pack("<II", ident, len(blob)))
            if blob:
                chunks.append(blob)
        return b"".join(chunks)

    def _parse_gpu_caps(self, caps_str):
        from batch_workers import ALL_GPU_CAPS
        if not caps_str:
            return ALL_GPU_CAPS, "all"
        caps = frozenset(c.strip().lower() for c in caps_str.split(",") if c.strip().lower() in ALL_GPU_CAPS)
        if not caps:
            return frozenset(), "png"
        if caps == ALL_GPU_CAPS:
            return caps, "all"
        return caps, "-".join(sorted(caps))

    def tex_gpu(self, texture_hash, max_size, model_id=0, caps_str="bc1,bc2,bc3,bc5,bc7"):
        """Return GTEX payload: raw unswizzled BCn mip chain for GPU upload, or PNG-wrapped fallback."""
        from batch_workers import (
            analyze_rgba_flags, extract_ttpf_gtex, pack_gtex_png, _write_atomic,
        )
        if not texture_hash or len(texture_hash) != 16 or any(c not in "0123456789abcdef" for c in texture_hash):
            raise ValueError("Invalid texture hash")
        caps, cap_tag = self._parse_gpu_caps(caps_str)
        cache_path = CACHE / "gtex" / f"{texture_hash}_{max_size}_{cap_tag}.gtex"
        if cache_path.is_file():
            return cache_path.read_bytes()

        sources = self.texture_info([texture_hash]).get(texture_hash)
        if not sources and model_id > 0:
            try:
                self.item(model_id)
                sources = self.texture_info([texture_hash]).get(texture_hash)
            except Exception:
                pass
        if not sources:
            raise LookupError("Texture not present in any local texture pack")
        src = sources[0]
        if src.get("area") != "embedded" and not src.get("nativeId"):
            arc = self.archive(src["path"])
            with self._archive_lock(src["path"]):
                return extract_ttpf_gtex(arc, src["idx"], max_size, caps, cache_path=str(cache_path))

        # Embedded TXD or Native RPF fallback -> PNG wrapped in GTEX with server-computed flags
        blob, _ = self.texture(texture_hash, max_size, model_id=model_id)
        from PIL import Image
        import io as _io
        im = Image.open(_io.BytesIO(blob))
        flags = analyze_rgba_flags(im)
        payload = pack_gtex_png(blob, im.width, im.height, flags)
        _write_atomic(cache_path, payload)
        return payload

    def tex_batch(self, items, caps_str="bc1,bc2,bc3,bc5,bc7"):
        """Return TBAT binary bundle of GTEX payloads; extracts cold TTPF BCn textures in parallel across CPU cores."""
        import struct
        from batch_workers import encode_gtex_ttpf_worker
        caps, cap_tag = self._parse_gpu_caps(caps_str)
        caps_tuple = tuple(sorted(caps))
        parsed = []
        seen = set()
        for raw in items:
            parts = raw.split(":")
            if len(parts) < 2:
                continue
            h = parts[0].strip().lower()
            if len(h) != 16 or any(c not in "0123456789abcdef" for c in h):
                continue
            try:
                ms = max(16, min(4096, int(parts[1])))
                mid = int(parts[2]) if len(parts) > 2 and parts[2].strip().isdigit() else 0
            except ValueError:
                continue
            key = f"{h}@{ms}"
            if key in seen:
                continue
            seen.add(key)
            parsed.append((key, h, ms, mid))
            if len(parsed) >= 32:
                break
        if not parsed:
            return struct.pack("<4sI", b"TBAT", 0)

        results = {}
        cold_ttpf = []
        cold_other = []
        need_info_hashes = []
        pending_lookup = []
        for key, h, ms, mid in parsed:
            cp = CACHE / "gtex" / f"{h}_{ms}_{cap_tag}.gtex"
            if cp.is_file():
                results[key] = cp.read_bytes()
            else:
                need_info_hashes.append(h)
                pending_lookup.append((key, h, ms, mid, str(cp)))

        if pending_lookup:
            tinfo = self.texture_info(need_info_hashes)
            for key, h, ms, mid, cp_str in pending_lookup:
                srcs = tinfo.get(h)
                if srcs and srcs[0].get("area") != "embedded" and not srcs[0].get("nativeId"):
                    cold_ttpf.append((key, srcs[0]["path"], srcs[0]["idx"], ms, caps_tuple, cp_str))
                else:
                    cold_other.append((key, h, ms, mid))

        if cold_ttpf:
            if len(cold_ttpf) == 1:
                from batch_workers import extract_ttpf_gtex
                key, rel_path, tex_idx, ms, _, cp_str = cold_ttpf[0]
                try:
                    arc = self.archive(rel_path)
                    with self._archive_lock(rel_path):
                        results[key] = extract_ttpf_gtex(arc, tex_idx, ms, frozenset(caps_tuple), cache_path=cp_str)
                except Exception:
                    results[key] = b""
            else:
                pool = self._get_pool()
                for k, data in pool.map(encode_gtex_ttpf_worker, cold_ttpf):
                    results[k] = data

        for key, h, ms, mid in cold_other:
            try:
                results[key] = self.tex_gpu(h, ms, model_id=mid, caps_str=caps_str)
            except Exception:
                results[key] = b""

        chunks = [struct.pack("<4sI", b"TBAT", len(parsed))]
        for key, _, _, _ in parsed:
            kb = key.encode("utf-8")
            blob = results.get(key, b"")
            chunks.append(struct.pack("<H", len(kb)))
            chunks.append(kb)
            chunks.append(struct.pack("<I", len(blob)))
            if blob:
                chunks.append(blob)
        return b"".join(chunks)

    def tex_array(self, texture_hash):
        """Return raw TARR binary ('TARR', w, h, slices, RGBA8 bytes) for a 2D texture array (e.g. vehicle LUT)."""
        import struct
        from ttpf_format import decode_pixels
        h = (texture_hash or "").strip().lower()
        if len(h) != 16 or any(c not in "0123456789abcdef" for c in h):
            raise ValueError("Invalid texture hash")
        tarr_dir = CACHE / "tarr"
        cp = tarr_dir / f"{h}.tarr"
        if cp.is_file():
            return cp.read_bytes()
        sources = self.texture_info([h]).get(h)
        if not sources:
            raise LookupError("Texture array not found in local texture packs")
        src = sources[0]
        if src.get("area") == "embedded" or src.get("nativeId"):
            raise ValueError("Only TTPF 2D texture arrays are supported")
        arc = self.archive(src["path"])
        with self._archive_lock(src["path"]):
            ti = arc.textures[src["idx"]]
            slices = max(1, int(ti.get("slices") or 1))
            w, h_px = int(ti["width"]), int(ti["height"])
            dxgi = int(ti["dxgi"])
            if w > 512 or h_px > 512 or slices > 32:
                raise ValueError("Texture array exceeds procedural LUT size limit")
            parts = [struct.pack("<4sIII", b"TARR", w, h_px, slices)]
            for s in range(slices):
                raw = arc.mip_linear(ti, 0, slice=s)
                if dxgi in (28, 29):  # R8G8B8A8_UNORM / SRGB
                    rgba = raw[:w * h_px * 4]
                else:
                    rgba = decode_pixels(raw, w, h_px, dxgi)
                parts.append(rgba)
        blob = b"".join(parts)
        tarr_dir.mkdir(parents=True, exist_ok=True)
        tmp = tarr_dir / f"{h}.tarr.tmp"
        tmp.write_bytes(blob)
        os.replace(tmp, cp)
        return blob

    def weapon_liveries(self, q="", offset=0, limit=60):
        """Return sRGB weapon livery/camo textures from weapon_liveries.ttpf."""
        with self.lock:
            cache = getattr(self, "_weapon_liveries_cache", None)
        if cache is None:
            items = []
            try:
                with closing(connect(TEXTURES)) as tdb:
                    rows = tdb.execute("""
                        SELECT t.hash, t.width, t.height, t.format, p.path
                        FROM texture t JOIN pack p ON t.pack = p.id
                        WHERE p.path LIKE '%weapon_liveries%'
                          AND t.format LIKE '%SRGB%'
                          AND t.width >= 64
                        ORDER BY t.width DESC, t.hash ASC
                    """).fetchall()
                for i, r in enumerate(rows):
                    items.append({
                        "index": i,
                        "hash": r["hash"],
                        "width": int(r["width"]),
                        "height": int(r["height"]),
                        "format": r["format"],
                    })
            except Exception:
                items = []
            with self.lock:
                self._weapon_liveries_cache = items
            cache = items
        ql = (q or "").strip().lower()
        filtered = [x for x in cache if not ql or ql in x["hash"]] if ql else cache
        off = max(0, int(offset or 0))
        lim = max(1, min(120, int(limit or 60)))
        return {"total": len(filtered), "offset": off, "items": filtered[off:off + lim]}

    # Skeleton, Skinning & Animation ----------------------------------------------------------------
    def _get_master_rig_ids(self):
        with self.lock:
            if self._master_rig_ids is not None:
                return self._master_rig_ids
        import character_rig as cr
        names = sorted(set(cr.MASTER_RIG_TOKENS.values()) | {"ambient_malebodyalien"})
        out = {}
        with closing(self.db()) as db:
            q = ",".join("?" * len(names))
            for r in db.execute(f"select id, name from model where name in ({q}) order by id", names).fetchall():
                out.setdefault(r["name"], int(r["id"]))
        with self.lock:
            self._master_rig_ids = out
        return out

    def _get_animal_master_skeletons(self):
        """Map every animal family (and species prefix fallback) to its fragment master skeleton."""
        with self.lock:
            if self._animal_master_skels is not None:
                return self._animal_master_skels
        import character_rig as cr
        by_fam = {}
        by_prefix = {}
        with closing(self.db()) as db:
            rows = db.execute("""
                select id, name, base_name, source, family, subcategory, archive, member, root, kind
                from model
                where category='Animals' and kind='fragment'
                order by id
            """).fetchall()
        for r in rows:
            dname = self._decode_name(r)
            cnt = cr.has_skeleton(r["archive"], int(r["member"]), int(r["root"]), dname, r["kind"])
            if cnt < 4:
                continue
            rec = {
                "skeletonId": int(r["id"]),
                "skeletonName": dname,
                "boneCount": int(cnt),
                "subcategory": r["subcategory"] or "",
            }
            fam = (r["family"] or "").lower()
            if fam and (fam not in by_fam or cnt > by_fam[fam]["boneCount"]):
                by_fam[fam] = rec
            # Also index stripped variant prefix (e.g. crc_crab_stoneflorida_m -> matches _m_01) and clade+species (crm_bobcat)
            parts = dname.lower().split("_")
            if len(parts) >= 3:
                for pkey in ("_".join(parts[:3]), "_".join(parts[:2])):
                    if pkey and (pkey not in by_prefix or cnt > by_prefix[pkey]["boneCount"]):
                        by_prefix[pkey] = rec
        out = (by_fam, by_prefix)
        with self.lock:
            self._animal_master_skels = out
        return out

    def rig_info_for(self, row, raw_member=None, raw_root=None, dname=None):
        """Return lightweight skeleton/rig metadata for characters, clothing, animals, and rigged fragments."""
        import character_rig as cr
        cat = row.get("category") or ""
        name = dname or row.get("name") or ""
        body_rig = ""
        if row.get("pedArchetype") and row["pedArchetype"].get("bodyRig"):
            body_rig = row["pedArchetype"]["bodyRig"]
        elif row.get("characterAssembly") and row["characterAssembly"].get("body"):
            body_rig = row["characterAssembly"]["body"]
        elif row.get("wardrobe") and row["wardrobe"].get("rig"):
            body_rig = row["wardrobe"]["rig"]
        asm_kind = (row.get("characterAssembly") or {}).get("kind", "")

        master_name = cr.infer_master_rig_name(name, cat, body_rig=body_rig, assembly_kind=asm_kind)
        if master_name:
            mids = self._get_master_rig_ids()
            sid = mids.get(master_name)
            if sid:
                rig_kind = "male" if ("male" in master_name and "female" not in master_name) else "female"
                return {
                    "skeletonId": sid,
                    "skeletonName": master_name,
                    "rigKind": rig_kind,
                    "hasOwnSkeleton": name.lower() == master_name.lower(),
                    "canAnimate": self.anim.available(),
                }

        # Animals: inherit the family's fragment master skeleton so visible drawable parts (*_body_00, *_head_00, etc.)
        # bind to the full 44-186 bone creature rig rather than a 0- or 3-bone local helper skeleton.
        if cat == "Animals":
            by_fam, by_prefix = self._get_animal_master_skeletons()
            fam = (row.get("family") or "").lower()
            rec = by_fam.get(fam)
            if rec is None:
                parts = (fam or name.lower()).split("_")
                for pkey in ("_".join(parts[:3]), "_".join(parts[:2])):
                    if pkey in by_prefix:
                        rec = by_prefix[pkey]
                        break
            if rec is not None:
                subcat = row.get("subcategory") or rec.get("subcategory") or ""
                return {
                    "skeletonId": rec["skeletonId"],
                    "skeletonName": rec["skeletonName"],
                    "rigKind": "animal",
                    "animalSubcat": subcat,
                    "speciesTokens": cr.animal_species_tokens(rec["skeletonName"], subcat),
                    "boneCount": rec["boneCount"],
                    "hasOwnSkeleton": int(row["id"]) == rec["skeletonId"],
                    "canAnimate": self.anim.available(),
                }

        # Check own crSkeletonData for other rigged characters, fragments, and custom assets
        if cat in ("Animals", "Characters", "Protagonists") or row.get("kind") == "fragment":
            mem = int(raw_member) if raw_member is not None else int(row["member"], 16)
            rt = int(raw_root) if raw_root is not None else int(row["root"], 16)
            cnt = cr.has_skeleton(row["archive"], mem, rt, name, row["kind"])
            if cnt >= 2:
                return {
                    "skeletonId": int(row["id"]),
                    "skeletonName": name,
                    "rigKind": "animal" if cat == "Animals" else "custom",
                    "speciesTokens": cr.animal_species_tokens(name, row.get("subcategory") or "") if cat == "Animals" else [],
                    "boneCount": cnt,
                    "hasOwnSkeleton": True,
                    "canAnimate": self.anim.available(),
                }
        return None

    def skeleton_by_id(self, skel_id):
        import character_rig as cr
        sid = int(skel_id)
        with self.lock:
            hit = self._skeleton_cache.get(sid)
            if hit is not None:
                self._skeleton_cache.move_to_end(sid)
                return hit
        with closing(self.db()) as db:
            r = db.execute("select id, name, base_name, source, archive, member, root, kind, category, subcategory from model where id=?", (sid,)).fetchone()
        if not r:
            raise LookupError(f"Unknown skeleton model id {sid}")
        dname = self._decode_name(r)
        skel = cr.read_skeleton(r["archive"], int(r["member"]), int(r["root"]), dname, r["kind"])
        if skel is None:
            raise LookupError(f"Model {dname} has no crSkeletonData")
        skel["id"] = sid
        cat = r["category"] or ""
        subcat = r["subcategory"] or ""
        if cat == "Animals":
            skel["rigKind"] = "animal"
            skel["speciesTokens"] = cr.animal_species_tokens(dname, subcat)
        else:
            skel["rigKind"] = ("male" if ("male" in dname.lower() and "female" not in dname.lower())
                               else ("female" if "female" in dname.lower() else ("biped" if skel.get("isBiped") else "animal")))
            skel["speciesTokens"] = cr.animal_species_tokens(dname, subcat) if skel["rigKind"] == "animal" else []
        with self.lock:
            self._skeleton_cache[sid] = skel
            self._skeleton_cache.move_to_end(sid)
            while len(self._skeleton_cache) > 64:
                self._skeleton_cache.popitem(last=False)
        return skel

    def _skeleton_match_context(self, skel_id_str):
        """Return (skeleton_id, bone_hashes_set, species_tokens) for a skeletonId query param."""
        s = (skel_id_str or "").strip()
        if not s.isdigit():
            return None, None, None
        try:
            skel = self.skeleton_by_id(int(s))
        except LookupError:
            return None, None, None
        from anim_service import joaat32
        hashes = set()
        for j in skel.get("joints", ()):
            hashes.add(int(j["hash"]) & 0xFFFFFFFF)
            hashes.add(joaat32(j["name"]))
        return int(skel["id"]), hashes, skel.get("speciesTokens") or []

    def skeleton_for_model(self, model_id):
        meta = self.item(int(model_id))
        rig = meta.get("rigInfo")
        if rig and rig.get("skeletonId"):
            return self.skeleton_by_id(rig["skeletonId"])
        return self.skeleton_by_id(int(model_id))

    def remapped_skin_for_model(self, model_id, lod=0, skeleton=None, skeleton_id=None):
        import character_rig as cr
        mid = int(model_id)
        lod = max(0, min(3, int(lod)))
        # The item's own default master skeleton places helper bones whose attach bone the target skeleton lacks.
        default = None
        if skeleton is None:
            skeleton = self.skeleton_by_id(int(skeleton_id)) if skeleton_id else self.skeleton_for_model(mid)
            default = None if skeleton_id else skeleton
        if default is None:
            try:
                default = self.skeleton_for_model(mid)
            except (LookupError, ValueError):
                default = None
        with closing(self.db()) as db:
            r = db.execute("select name, base_name, source, archive, member, root, kind from model where id=?", (mid,)).fetchone()
        if not r:
            raise LookupError(f"Unknown model id {mid}")
        dname = self._decode_name(r)
        part_skin = cr.read_part_skin(r["archive"], int(r["member"]), int(r["root"]), dname, r["kind"], lod=lod)
        if part_skin is None:
            return None
        return cr.remap_part_skin(part_skin, skeleton, default_skeleton=default)

    def skin_binary(self, model_id, lod=0, skeleton_id=None):
        import character_rig as cr
        mid = int(model_id)
        lod = max(0, min(3, int(lod)))
        skel = self.skeleton_by_id(int(skeleton_id)) if skeleton_id else self.skeleton_for_model(mid)
        ckey = (mid, lod, int(skel["id"]))
        with self.lock:
            hit = self._skin_cache.get(ckey)
            if hit is not None:
                self._skin_cache.move_to_end(ckey)
                return hit
        remapped = self.remapped_skin_for_model(mid, lod=lod, skeleton=skel)
        if remapped is None:
            raise LookupError(f"Model {mid} has no skinning streams")
        blob = cr.pack_skin_binary(remapped)
        with self.lock:
            self._skin_cache[ckey] = blob
            self._skin_cache.move_to_end(ckey)
            while len(self._skin_cache) > 256:
                self._skin_cache.popitem(last=False)
        return blob

    # HTTP ------------------------------------------------------------------------------------------
    def serve(self, handler, route, arg):
        if route == "summary":
            return handler.send(self.summary())
        if route == "list":
            return handler.send(self.rows(arg))
        if route == "item":
            lod = max(0, min(3, int(arg("lod", "0"))))
            return handler.send(self.item(int(arg("id")), lod=lod))
        if route == "skeleton":
            try:
                sid = arg("skeletonId").strip()
                if sid.isdigit():
                    skel = self.skeleton_by_id(int(sid))
                else:
                    skel = self.skeleton_for_model(int(arg("id")))
                return handler.send(skel, cache_control="no-store")
            except LookupError as exc:
                return handler.send({"error": str(exc)}, status=404)
            except ValueError as exc:
                return handler.send({"error": str(exc)}, status=422)
        if route == "skin":
            try:
                mid = int(arg("id"))
                lod = max(0, min(3, int(arg("lod", "0") or 0)))
                sid_s = arg("skeleton").strip()
                sid = int(sid_s) if sid_s.isdigit() else None
                blob = self.skin_binary(mid, lod=lod, skeleton_id=sid)
                return handler.send(blob, "application/octet-stream", cache_control="no-store")
            except LookupError as exc:
                return handler.send({"error": str(exc)}, status=404)
            except ValueError as exc:
                return handler.send({"error": str(exc)}, status=422)
        if route == "anims":
            sid, skel_hashes, sp_tokens = self._skeleton_match_context(arg("skeletonId"))
            return handler.send(self.anim.search(
                q=arg("q"),
                category=arg("category"),
                rig=arg("rig", "female") or "female",
                archive=arg("archive"),
                skeleton_id=sid,
                skeleton_hashes=skel_hashes,
                species_tokens=sp_tokens,
                limit=int(arg("limit", "60") or 60),
                offset=int(arg("offset", "0") or 0),
            ))
        if route == "anim-status":
            return handler.send(self.anim.status())
        if route == "blendspaces":
            _sid, _hashes, sp_tokens = self._skeleton_match_context(arg("skeletonId"))
            return handler.send(self.anim.search_blendspaces(
                q=arg("q"),
                dims=arg("dims"),
                domain=arg("domain"),
                rig=arg("rig"),
                species_tokens=sp_tokens,
                limit=int(arg("limit", "50") or 50),
                offset=int(arg("offset", "0") or 0),
            ))
        if route == "blendspace":
            try:
                bs = self.anim.get_blendspace(int(arg("id")))
                return handler.send(bs, cache_control="private, max-age=3600")
            except LookupError as exc:
                return handler.send({"error": str(exc)}, status=404)
            except (ValueError, OSError) as exc:
                return handler.send({"error": str(exc)}, status=422)
        if route == "motion-matchers":
            _sid, skel_hashes, sp_tokens = self._skeleton_match_context(arg("skeletonId"))
            return handler.send(self.anim.search_motion_matchers(
                q=arg("q"),
                rig_category=arg("rigCategory"),
                mode=arg("mode"),
                skeleton_hashes=skel_hashes,
                species_tokens=sp_tokens,
                limit=int(arg("limit", "80") or 80),
                offset=int(arg("offset", "0") or 0),
            ))
        if route == "motion-matcher":
            try:
                mm = self.anim.get_motion_matcher(int(arg("id")), include_poses=(arg("poses") == "1"))
                return handler.send(mm, cache_control="private, max-age=3600")
            except LookupError as exc:
                return handler.send({"error": str(exc)}, status=404)
            except (ValueError, OSError) as exc:
                return handler.send({"error": str(exc)}, status=422)
        if route == "anim-clip":
            try:
                dict_id = int(arg("dictId"))
                ch_raw = arg("clipHash").strip()
                clip_hash = int(ch_raw, 16) if ch_raw.lower().startswith("0x") else int(ch_raw)
                skel_id = int(arg("skeleton"))
                skel = self.skeleton_by_id(skel_id)
                decoded = self.anim.decode_clip_tracks(dict_id, clip_hash, skel["joints"])
                blob = self.anim.pack_anim_binary(decoded)
                return handler.send(blob, "application/octet-stream", cache_control="private, max-age=3600")
            except LookupError as exc:
                return handler.send({"error": str(exc)}, status=404)
            except (ValueError, OSError) as exc:
                return handler.send({"error": str(exc)}, status=422)
        if route == "by-texture":
            try:
                return handler.send(self.by_texture(arg("hash"), int(arg("limit", "60"))))
            except ValueError as exc:
                return handler.send({"error": str(exc)}, status=422)
        if route == "wardrobe":
            return handler.send(self.wardrobe_items(
                arg("rig"), arg("slot"), arg("q"),
                int(arg("limit", "250") or 250),
                int(arg("offset", "0") or 0),
            ))
        if route == "char-effects-catalog":
            import character_effects as ce
            return handler.send(ce.get_catalog(arg("rig", "female") or "female"), cache_control="private, max-age=3600")
        if route == "char-tattoos":
            import character_effects as ce
            return handler.send(ce.search_tattoos(
                arg("q"),
                int(arg("offset", "0") or 0),
                int(arg("limit", "36") or 36),
            ), cache_control="private, max-age=3600")
        if route == "char-gore":
            import character_effects as ce
            return handler.send({"items": ce.get_gore_parts(self, arg("rig", "fr1") or "fr1")},
                                cache_control="private, max-age=3600")
        if route == "groom-catalog":
            try:
                return handler.send(self.groom_catalog(arg("rig", "fr1") or "fr1"),
                                    cache_control="private, max-age=3600")
            except (ValueError, OSError) as exc:
                return handler.send({"error": str(exc)}, status=422)
        if route == "groom-strands":
            try:
                import char_groom as cg
                ids_raw = arg("ids").strip()
                gids = [int(x) for x in ids_raw.split(",") if x.strip().isdigit()]
                if not gids:
                    gid_s = arg("id").strip()
                    if gid_s.isdigit():
                        gids = [int(gid_s)]
                if not gids:
                    return handler.send({"error": "Missing groom id(s)"}, status=422)
                slot = arg("slot", "hair") or "hair"
                max_s = max(200, min(16000, int(arg("max", "4000") or 4000)))
                lod = max(0, min(3, int(arg("lod", "0") or 0)))
                groom_rig = arg("groomRig", "") or arg("groom_rig", "")
                target_rig = arg("targetRig", "") or arg("target_rig", "")
                blob = cg.decode_groom_strands_for_model(
                    gids if len(gids) > 1 else gids[0],
                    max_strands=max_s,
                    lod=lod,
                    slot=slot,
                    groom_rig=groom_rig,
                    target_rig=target_rig,
                    style=True,
                    binary=True,
                )
                return handler.send(blob, "application/octet-stream", cache_control="private, max-age=86400")
            except LookupError as exc:
                return handler.send({"error": str(exc)}, status=404)
            except (ValueError, OSError) as exc:
                return handler.send({"error": str(exc)}, status=422)
        if route == "char-outfits":
            try:
                mid = int(arg("id"))
                return handler.send(self.char_outfits_for(mid), cache_control="private, max-age=3600")
            except (ValueError, OSError) as exc:
                return handler.send({"error": str(exc)}, status=422)
        if route == "char-overlays":
            try:
                import char_overlays as cov
                if arg("presets") == "1":
                    return handler.send(
                        cov.get_overlay_presets(
                            kind=arg("kind", ""),
                            q=arg("q", ""),
                            offset=max(0, int(arg("offset", "0") or 0)),
                            limit=max(1, min(120, int(arg("limit", "36") or 36))),
                            include_damage=(arg("damage", "1") != "0"),
                        ) | {"kinds": cov.list_overlay_preset_kinds()},
                        cache_control="private, max-age=3600",
                    )
                mid = int(arg("id"))
                return handler.send(self.char_overlays_for(mid), cache_control="private, max-age=3600")
            except (ValueError, OSError) as exc:
                return handler.send({"error": str(exc)}, status=422)
        if route == "overlay-texture":
            try:
                import hashlib
                import char_overlays as cov
                h = arg("hash").strip().lower()
                mode = (arg("mode", "mask") or "mask").strip().lower()
                max_res = max(64, min(1024, int(arg("max", "512") or 512)))
                opacity = max(0.0, min(1.0, float(arg("opacity", "1.0") or 1.0)))
                tints_raw = arg("tints").strip()
                tints = json.loads(tints_raw) if tints_raw else []
                ckey = hashlib.sha1(f"{h}|{mode}|{max_res}|{opacity:.3f}|{tints_raw}".encode()).hexdigest()[:16]
                cpath = CACHE / "tex" / f"ov_{ckey}.png"
                if cpath.is_file():
                    return handler.send(cpath.read_bytes(), "image/png", cache_control="private, max-age=86400")
                raw_png, _ = self.texture(h, max_res)
                blob = cov.bake_overlay_layer_png(raw_png, tints, tint_mode=mode, opacity=opacity)
                cpath.write_bytes(blob)
                return handler.send(blob, "image/png", cache_control="private, max-age=86400")
            except LookupError as exc:
                return handler.send({"error": str(exc)}, status=404)
            except (ValueError, OSError) as exc:
                return handler.send({"error": str(exc)}, status=422)
        if route == "weapon-liveries":
            return handler.send(self.weapon_liveries(
                arg("q"),
                int(arg("offset", "0") or 0),
                int(arg("limit", "60") or 60),
            ))
        if route == "tex-array":
            try:
                blob = self.tex_array(arg("hash").lower())
            except LookupError as exc:
                return handler.send({"error": str(exc)}, status=404)
            except ValueError as exc:
                return handler.send({"error": str(exc)}, status=422)
            return handler.send(blob, "application/octet-stream", cache_control="private, max-age=86400")
        if route == "glb-batch":
            lod = max(0, min(3, int(arg("lod", "0"))))
            ids = [int(x) for x in arg("ids").split(",") if x.strip().isdigit()]
            try:
                return handler.send(self.glb_batch(ids, lod=lod), "application/octet-stream",
                                    cache_control="private, max-age=3600")
            except ValueError as exc:
                return handler.send({"error": str(exc)}, status=422)
        if route == "muv-texture":
            try:
                from clothing_presets import get_store as get_clothing_store
                mid = int(arg("id"))
                preset = arg("preset").strip().lower()
                max_res = max(128, min(1024, int(arg("max", "512") or 512)))
                mat_idx = max(0, int(arg("mat", "0") or 0))
                blob = get_clothing_store().bake_muv_albedo(
                    self, mid, preset, max_res=max_res, mat_idx=mat_idx, cache_dir=CACHE,
                )
            except LookupError as exc:
                return handler.send({"error": str(exc)}, status=404)
            except (ValueError, OSError) as exc:
                return handler.send({"error": str(exc)}, status=422)
            return handler.send(blob, "image/png", cache_control="private, max-age=86400")
        if route == "glb":
            lod = max(0, min(3, int(arg("lod", "0"))))
            try:
                if arg("textured") == "1":
                    from glb_textured import build_textured_glb
                    parts = [int(x) for x in arg("parts").split(",") if x.strip().isdigit()]
                    wspec = None
                    wraw = arg("wheels").strip()
                    if wraw:
                        if ":" in wraw:
                            r_s, t_s = wraw.split(":", 1)
                            wspec = {"rimId": int(r_s or 0), "tyreId": int(t_s or 0)}
                        elif wraw == "1":
                            wspec = {}
                    ov_raw = arg("overrides").strip()
                    ov = json.loads(ov_raw) if ov_raw else None
                    at_raw = arg("anatTex").strip()
                    anat_tex = json.loads(at_raw) if at_raw else None
                    mp_raw = arg("muvPresets").strip()
                    muv_presets = json.loads(mp_raw) if mp_raw else {}
                    po_raw = arg("partOverrides").strip()
                    part_overrides = json.loads(po_raw) if po_raw else None
                    bm_raw = arg("bodyMorphs").strip()
                    body_morphs = json.loads(bm_raw) if bm_raw else None
                    hs_raw = arg("heightScale").strip()
                    height_scale = float(hs_raw) if hs_raw else None
                    use_rage9_rig = arg("useRage9Rig") == "1"
                    mid = int(arg("id"))
                    asm_raw = self._get_character_assemblies().get(mid)
                    asm = self._enrich_character_assembly(mid, None, asm_raw) if asm_raw else None
                    if anat_tex is None and asm and asm.get("defaultSkin"):
                        anat_tex = asm["defaultSkin"]
                    if body_morphs is None and asm and asm.get("bodyMorphs"):
                        body_morphs = asm["bodyMorphs"]
                    if height_scale is None:
                        height_scale = float(asm.get("heightScale", 1.0)) if asm else 1.0
                    is_male = bool((asm.get("body") or "").lower().startswith("m")) if asm else False
                    if asm and asm.get("clothingPresets"):
                        # Character-native 0DRP presets as defaults (user overrides in muv_presets take priority)
                        merged_mp = dict(asm["clothingPresets"])
                        merged_mp.update(muv_presets)
                        muv_presets = merged_mp
                    max_tex = max(128, min(8192, int(arg("maxTex", "8192"))))
                    include_rig = arg("rig") == "1"
                    ad_s = arg("animDictId").strip()
                    ac_s = arg("animClipHash").strip()
                    anim_dict_id = int(ad_s) if ad_s.isdigit() else None
                    anim_clip_hash = (int(ac_s, 16) if ac_s.lower().startswith("0x") else int(ac_s)) if ac_s else None
                    anim_root_motion = arg("rootMotion") == "1"
                    blob = build_textured_glb(
                        self, mid, lod=lod, part_ids=parts,
                        wheel_spec=wspec, overrides=ov, max_tex=max_tex,
                        include_rig=include_rig,
                        anim_dict_id=anim_dict_id,
                        anim_clip_hash=anim_clip_hash,
                        anim_root_motion=anim_root_motion,
                        anatomy_tex=anat_tex,
                        muv_presets=muv_presets,
                        part_overrides=part_overrides,
                        body_morphs=body_morphs,
                        height_scale=height_scale,
                        is_male=is_male,
                        use_rage9_rig=use_rage9_rig,
                    )
                    return handler.send(blob, "model/gltf-binary")
                return handler.send(self.glb(int(arg("id")), lod=lod), "model/gltf-binary",
                                    cache_control="private, max-age=3600")
            except ValueError as exc:
                return handler.send({"error": str(exc)}, status=422)
        if route == "tex-gpu":
            try:
                model_id = int(arg("model", "0") or 0) if arg("model").strip().isdigit() else 0
                blob = self.tex_gpu(
                    arg("hash").lower(),
                    max(16, min(4096, int(arg("max", "512") or 512))),
                    model_id=model_id,
                    caps_str=arg("caps", "bc1,bc2,bc3,bc5,bc7"),
                )
            except LookupError as exc:
                return handler.send({"error": str(exc)}, status=404)
            except ValueError as exc:
                return handler.send({"error": str(exc)}, status=422)
            return handler.send(blob, "application/octet-stream", cache_control="private, max-age=3600")
        if route == "tex-batch":
            items = [x for x in arg("items").split(",") if x.strip()]
            try:
                blob = self.tex_batch(items, caps_str=arg("caps", "bc1,bc2,bc3,bc5,bc7"))
            except ValueError as exc:
                return handler.send({"error": str(exc)}, status=422)
            return handler.send(blob, "application/octet-stream", cache_control="private, max-age=3600")
        if route == "texture":
            try:
                model_id = int(arg("model", "0") or 0) if arg("model").strip().isdigit() else 0
                blob, level = self.texture(arg("hash").lower(), max(16, min(8192, int(arg("max", "8192")))), model_id=model_id)
            except LookupError as exc:
                return handler.send({"error": str(exc)}, status=404)
            except ValueError as exc:
                return handler.send({"error": str(exc)}, status=422)
            return handler.send(blob, "image/png", cache_control="private, max-age=3600")
        handler.send({"error": "Unknown route"}, status=404)

