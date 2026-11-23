#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Smoke test of a running Lineup server: lists, then item / glb / texture / skeleton / anims / outfits / grooms
for a handful of characters. Prints one line per request (status, bytes, milliseconds) and exits 1 on any failure.

    python lineup.py --no-browser &
    python tests/api_smoke.py [--url http://127.0.0.1:8910] [--json report.json]

Needs only the standard library. Writes nothing but the optional report.
"""
import argparse
import hashlib
import json
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

FAILS = []
REPORT = []


def get(base, route, **params):
    url = base + route + ("?" + urllib.parse.urlencode(params) if params else "")
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(url, timeout=600) as r:
            body, status = r.read(), r.status
    except urllib.error.HTTPError as e:
        body, status = e.read(), e.code
    return status, body, (time.perf_counter() - t0) * 1000


def check(label, base, route, expect=(200,), **params):
    status, body, ms = get(base, route, **params)
    ok = status in expect and len(body) > 0
    print(f"  {'ok  ' if ok else 'FAIL'} {status} {len(body):>10,} B {ms:8.0f} ms  {label}")
    REPORT.append({"label": label, "status": status, "bytes": len(body), "ms": round(ms), "ok": ok})
    if not ok:
        FAILS.append(label)
        if body[:200]:
            print("       ", body[:200].decode("utf-8", "replace"))
    return status, body


def as_json(body):
    try:
        return json.loads(body)
    except ValueError:
        return {}


def glb_counts(blob):
    """(vertices, triangles) summed over the primitives of a GLB."""
    if blob[:4] != b"glTF":
        return None
    length, kind = struct.unpack_from("<II", blob, 12)
    doc = json.loads(blob[20:20 + length])
    vertices = triangles = 0
    for mesh in doc.get("meshes", []):
        for prim in mesh["primitives"]:
            vertices += doc["accessors"][prim["attributes"]["POSITION"]]["count"]
            if "indices" in prim and prim.get("mode", 4) == 4:
                triangles += doc["accessors"][prim["indices"]]["count"] // 3
    return vertices, triangles


def rows(base, **params):
    data = as_json(get(base, "/api/models/list", **params)[1])
    cols = {c: i for i, c in enumerate(data.get("columns", []))}
    return [{c: r[i] for c, i in cols.items()} for r in data.get("rows", [])], data.get("total", 0)


def pick(base):
    """(label, id) for the characters the smoke test visits."""
    chosen = []
    summary = as_json(get(base, "/api/models/summary")[1])
    subcategories = {c["category"]: [s["name"] for s in c["subcategories"]] for c in summary.get("categories", [])}

    def first(label, n=1, **params):
        found, _ = rows(base, limit=60, **params)
        for r in found[:n]:
            chosen.append((f"{label}: {r['name']}", r["id"]))
        if not found:
            print(f"  note: no rows for {label}")
    first("Lucia head", q="head", category="Protagonists", subcategory="Lucia")
    first("Lucia body", q="body", category="Protagonists", subcategory="Lucia")
    first("Jason head", q="head", category="Protagonists", subcategory="Jason")
    first("Jason body", q="body", category="Protagonists", subcategory="Jason")
    first("story character", n=2, category="Characters", subcategory="Story characters")
    for sub in [s for s in subcategories.get("Characters", []) if s.startswith("Ped archetypes")][:2]:
        first("ped archetype", n=1, category="Characters", subcategory=sub)
    first("ambient body", category="Characters", subcategory="Ambient & legacy bodies")
    first("clothing", category="Clothing & accessories", subcategory="Tops")
    seen, unique = set(), []
    for label, mid in chosen:
        if mid not in seen:
            seen.add(mid)
            unique.append((label, mid))
    return unique


def find_hash(value):
    """The first 16-hex-digit texture hash anywhere in a JSON value."""
    if isinstance(value, str):
        return value if len(value) == 16 and all(c in "0123456789abcdef" for c in value) else None
    for child in (value.values() if isinstance(value, dict) else value if isinstance(value, list) else ()):
        found = find_hash(child)
        if found:
            return found
    return None


def texture_hashes(item):
    for material in item.get("materials", []):
        for t in material.get("textures", []):
            if t.get("sources"):
                yield t.get("effectiveHash") or t["textureHash"], t["textureHash"]


def shared(base, clips):
    """Routes the viewer's side panels call: clips, blendspaces, motion matching, overlays, wardrobe ..."""
    print("\nside-panel routes")
    female = next((v for k, v in clips.items() if "Lucia" in k and v[1]), (None, []))
    male = next((v for k, v in clips.items() if "Jason" in k and v[1]), (None, []))
    if female[1] and male[1]:
        same = [c.get("name") for c in female[1][:10]] == [c.get("name") for c in male[1][:10]]
        print(f"  {'FAIL' if same else 'ok  '} the first animations differ between the female and the male rig")
        if same:
            FAILS.append("female and male animation lists are identical")
    elif not (female[1] or male[1]):
        print("  FAIL no animations returned for either rig")
        FAILS.append("no animations")
    sid, items = female if female[1] else male
    for clip in items[:1]:
        check("anim-clip", base, "/api/models/anim-clip", dictId=clip["dictId"], clipHash=clip["clipHash"], skeleton=sid)
    _, body = check("blendspaces", base, "/api/models/blendspaces", limit=5)
    for row in (as_json(body).get("items") or [])[:1]:
        check("blendspace", base, "/api/models/blendspace", id=row["id"])
    _, body = check("motion-matchers", base, "/api/models/motion-matchers", limit=5)
    for row in (as_json(body).get("items") or [])[:1]:
        check("motion-matcher", base, "/api/models/motion-matcher", id=row["id"])
    check("char-effects-catalog (female)", base, "/api/models/char-effects-catalog", rig="female")
    check("char-effects-catalog (male)", base, "/api/models/char-effects-catalog", rig="male")
    check("char-gore", base, "/api/models/char-gore", rig="fr1")
    check("char-tattoos", base, "/api/models/char-tattoos", limit=6)
    check("char-overlays presets", base, "/api/models/char-overlays", presets=1, limit=6)
    for slot in ("top", "bottom", "shoes", "hair"):
        check(f"wardrobe {slot}", base, "/api/models/wardrobe", rig="fr1", slot=slot, limit=5)


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")     # a console that cannot show a character must not stop the run
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8910")
    ap.add_argument("--json")
    ap.add_argument("--max", type=int, default=2048, help="texture size ceiling")
    args = ap.parse_args()
    base = args.url.rstrip("/")
    status, body = check("info", base, "/api/info")
    print("   ", as_json(body))
    _, body = check("summary", base, "/api/models/summary")
    summary = as_json(body)
    names = [c["category"] for c in summary.get("categories", [])]
    print("    categories:", names)
    for category in names:
        check(f"list {category}", base, "/api/models/list", category=category, limit=20)
    status, body = check("list of a hidden category", base, "/api/models/list", category="Vehicles", limit=5)
    if as_json(body).get("total") != 0:
        print("  FAIL a hidden category must list nothing:", body[:120])
        FAILS.append("hidden category lists rows")
    detail, clips = {}, {}
    for label, mid in pick(base):
        print(f"\n{label}  (id {mid})")
        _, body = check("item", base, "/api/models/item", id=mid)
        item = as_json(body)
        _, blob = check("glb lod0", base, "/api/models/glb", id=mid, lod=0)
        counts = glb_counts(blob)
        entry = {"vertices": counts[0], "triangles": counts[1]} if counts else {}
        print(f"         geometry: {counts}")
        hashes = list(texture_hashes(item))
        if hashes:
            eff, bound = hashes[0]
            status, png = check(f"texture {eff}", base, "/api/models/texture", hash=eff, max=args.max, model=mid)
            entry["textureSha256"] = hashlib.sha256(png).hexdigest() if status == 200 else None
            entry["textureHash"] = eff
        else:
            print("         note: no resolvable texture on this item")
        rig = item.get("rigInfo") or {}
        sid, kind = rig.get("skeletonId", ""), rig.get("rigKind") or "female"
        check("skeleton", base, "/api/models/skeleton", expect=(200, 404, 422), id=mid)
        _, body = check(f"anims (rig {kind}, skeleton {sid})", base, "/api/models/anims", rig=kind, skeletonId=sid, limit=20)
        found = as_json(body)
        entry["animTotal"] = found.get("total")
        entry["firstClip"] = (found.get("items") or [{}])[0].get("name")
        print(f"         animations: {found.get('total')} matching; first: {entry['firstClip']}")
        clips[label] = (sid, found.get("items") or [])
        check("char-outfits", base, "/api/models/char-outfits", expect=(200, 422), id=mid)
        check("skin", base, "/api/models/skin", expect=(200, 404, 422), id=mid, lod=0, skeleton=sid)
        _, body = check("char-overlays", base, "/api/models/char-overlays", expect=(200, 422), id=mid)
        stamp = find_hash(as_json(body))
        if stamp and "overlayChecked" not in detail:
            detail["overlayChecked"] = True
            check(f"overlay-texture {stamp}", base, "/api/models/overlay-texture", hash=stamp, mode="mask", max=256)
        variants = (item.get("clothingPresets") or {}).get("variants") or []
        if variants:
            check(f"muv-texture {variants[0]['id']}", base, "/api/models/muv-texture", id=mid, preset=variants[0]["id"], max=256)
        check("groom-catalog", base, "/api/models/groom-catalog", rig=(item.get("rigInfo") or {}).get("rig") or "fr1")
        detail[label] = entry
    shared(base, clips)
    print(f"\n{len(REPORT)} requests, {len(FAILS)} failed")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump({"requests": REPORT, "items": detail}, f, indent=1)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
