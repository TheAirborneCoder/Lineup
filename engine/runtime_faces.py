# SPDX-License-Identifier: GPL-3.0-or-later
"""Real face textures for character heads, from the game's appearance catalogues.

`face_presets.py` writes LIBRARY/face_presets.sqlite3 from packs.rpf/00011ccb2000.pso and the
2,709 resource-packed 0DRP 0x8dcb4cf5 ped catalogues in packs.rpf. Rows are pre-ordered best-first
per model (direct drawable tag > child-of-drawable-tag > head state drawable, highest-resolution
local sRGB albedo first, then local normal). Only heads directly named by the game's catalogues are
listed; nothing here is inferred or borrowed from another head.

Use: faces_for(model_name) -> {"c8e8c282": albedo_hash, "1e77bf4c": normal_hash} or {}."""
import sqlite3
import threading
from paths import LIBRARY

DB = LIBRARY / "face_presets.sqlite3"
TEXTURES = LIBRARY / "textures.sqlite3"
ALBEDO, NORMAL = "c8e8c282", "1e77bf4c"
ZERO = "0000000000000000"
ALLOWED_HOW = ("drawable tag", "drawable tag (child)", "state drawable")
_lock = threading.Lock()
_cache = None
_all_cache = None


def _load():
    global _cache, _all_cache
    with _lock:
        if _cache is None:
            faces = {}
            all_variants = {}
            if DB.is_file():
                tex_rank = {}
                if TEXTURES.is_file():
                    with sqlite3.connect(TEXTURES.as_uri() + "?mode=ro", uri=True) as tdb:
                        for h, w, hh, fmt in tdb.execute("select hash, width, height, format from texture"):
                            tex_rank[h] = (1, w * hh, 1 if "SRGB" in (fmt or "") else 0)
                how_prio = {h: i for i, h in enumerate(ALLOWED_HOW)}
                best_alb = {}
                best_nrm = {}
                var_alb = {}
                var_nrm = {}
                with sqlite3.connect(DB.as_uri() + "?mode=ro", uri=True) as db:
                    for model, preset, albedo, normal, how in db.execute(
                        "select model, preset, albedo, normal, how from match"
                    ):
                        if how not in how_prio:
                            continue
                        key = model.lower()
                        hp = how_prio[how]
                        is_pso = 1 if (preset or "").startswith("00011ccb2000:") else 0
                        if albedo and albedo != ZERO:
                            tr = tex_rank.get(albedo, (0, 0, 0))
                            score = (tr[0], -hp, tr[1], tr[2], is_pso)
                            cur = best_alb.get(key)
                            if cur is None or score > cur[0]:
                                best_alb[key] = (score, albedo)
                            if tr[0]:
                                d = var_alb.setdefault(key, {})
                                prev = d.get(albedo)
                                if prev is None or score > prev:
                                    d[albedo] = score
                        if normal and normal != ZERO:
                            tr = tex_rank.get(normal, (0, 0, 0))
                            score = (tr[0], -hp, tr[1], is_pso)
                            cur = best_nrm.get(key)
                            if cur is None or score > cur[0]:
                                best_nrm[key] = (score, normal)
                            if tr[0]:
                                d = var_nrm.setdefault(key, {})
                                prev = d.get(normal)
                                if prev is None or score > prev:
                                    d[normal] = score
                for key in set(best_alb) | set(best_nrm):
                    entry = {}
                    if key in best_alb:
                        entry[ALBEDO] = best_alb[key][1]
                    if key in best_nrm:
                        entry[NORMAL] = best_nrm[key][1]
                    if entry:
                        faces[key] = entry
                for key in set(var_alb) | set(var_nrm):
                    albs = [h for h, _ in sorted(var_alb.get(key, {}).items(), key=lambda kv: (kv[1], kv[0]), reverse=True)]
                    nrms = [h for h, _ in sorted(var_nrm.get(key, {}).items(), key=lambda kv: (kv[1], kv[0]), reverse=True)]
                    if albs or nrms:
                        all_variants[key] = {"albedos": albs, "normals": nrms}
            _cache = faces
            _all_cache = all_variants
        return _cache


def faces_for(model_name):
    return dict(_load().get(model_name.lower(), {}))


def all_presets_for(model_name):
    _load()
    hit = _all_cache.get(model_name.lower())
    return {"albedos": list(hit["albedos"]), "normals": list(hit["normals"])} if hit else {"albedos": [], "normals": []}
