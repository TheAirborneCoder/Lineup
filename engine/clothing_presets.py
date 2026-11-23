# SPDX-License-Identifier: GPL-3.0-or-later
"""RAGE 9 MUV clothing material database indexer & texture baker.

Parses:
- INDEX/data.rpf/00001b95b000.pso (root 0x8419ab01):
  - 0x101 (0x83021d7a, 431 entries): base fabric/metal physics definitions
  - 0x103 (0x6cc4c278, 7,993 entries): named material swatches
  - 0x105 (0x440c9ecb, 183 entries): secondary override sets
  - 0x107 (0x756aef9c, 13,070 entries): 8-slot garment MUV presets keyed by u36 (0xe13ebaf8)
- INDEX/packs.rpf/00011ccb2000.pso (19,827 wearable presets)
- All 2,709 0DRP ped catalogues in packs.rpf (matched to ped_archetypes.sqlite3)

Builds LIBRARY/clothing_presets.sqlite3 and provides fast cached MUV albedo baking
for both the live WebGL2 viewer (/api/models/muv-texture) and Blender-ready GLB export.

    python engine/clothing_presets.py
"""
import collections
from concurrent.futures import ThreadPoolExecutor
import colorsys
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import struct
import threading
import zlib

import numpy as np
from PIL import Image


from decode_pso import Decoder, sections_of  # noqa: E402
from face_presets import (  # noqa: E402
    CATALOG_ROOT, DRAWABLE_TAGS, PACKS_RPF, PACKS_SPANS, PRESETS_PSO,
    parse_0drp_blocks, ptr, read_tags,
)
from script_symbol_names import murmur64  # noqa: E402
from script_tree import _name_hash  # noqa: E402
from paths import INDEX, LIBRARY

MUV_DB_PSO = INDEX / "data.rpf/00001b95b000.pso"
INVENTORY_DB = LIBRARY / "inventory.sqlite3"
TEXTURES_DB = LIBRARY / "textures.sqlite3"
PED_ARCH_DB = LIBRARY / "ped_archetypes.sqlite3"
OUT_DB = LIBRARY / "clothing_presets.sqlite3"

# All RAGE 9 MUV clothing shader hashes (ped_standard_muv* family)
MUV_SHADER_HASHES = {
    "d5ed50f4", "39ddb9ca", "7574b09b", "ea8d09d0",
    "73055d06", "c78a3e26", "9880a0c9", "1c43a65b",
    "6f3c7994", "1df84fef",
}

# Blank / decal placeholder hashes that must never overwrite garment specular/diffuse textures
PLACEHOLDER_CLOTHING_TEXTURES = {
    "2e67967223889f9d",  # 512x512 BC1_UNORM all-black blank decal placeholder
    "d2288f458ac8c7e2",  # shared blank skin/clothing placeholder
    "1e29578a23d839f3",  # shared blank placeholder
}

# Cracked MUV parameter hashes inside data.rpf/00001b95b000.pso
H_BASE_COLOR = 0xF574261B      # ABGR8 base colors (subIdx 0 = primary, 1..3 = secondary/accent)
H_PAT_COLOR = 0x587B7F6D       # ABGR8 pattern layer tint colors (subIdx 0..5)
H_SHEEN_COLOR = 0xEC2767CA     # ABGR8 sheen color
H_PAT_TEX = 0xD2B598E5         # u64 pattern mask/print texture hash (subIdx 0..3)
H_PAT_SCALE = 0x5BB20A63       # f32 pattern UV tiling scale pairs: (0,1)=L0, (2,3)=L1, (4,5)=L2
H_PAT_OPACITY = 0x568FD269     # f32 pattern layer blend opacity (subIdx 0..2)
H_WEAVE_SCALE = 0x01AE45D3     # f32 base fabric weave tiling scale
H_BASE_METALLIC = 0x6C75B6B2   # f32 metallic (0.0 fabric, 0.9 metal)
H_BASE_ROUGHNESS = 0xBA09F178  # f32 roughness (0.2 metal, 0.85..0.97 fabric)
H_BASE_WEAVE_TEX = 0x1B689BA7  # u64 base fabric micro-weave mask

STEM_RE = re.compile(r"^(?:player_story\d+_\d+_)?([a-z0-9]+)_(?:f[a-z]{1,2}\d|m[a-z]{1,2}\d)_(\d+)")
TYPE_RE = re.compile(r"^(?:player_story\d+_\d+_)?([a-z0-9]+)_(?:f[a-z]{1,2}\d|m[a-z]{1,2}\d)_")


def abgr_to_hex(v):
    return f"#{v & 0xFF:02x}{(v >> 8) & 0xFF:02x}{(v >> 16) & 0xFF:02x}"


def hex_to_rgb01(h):
    h = (h or "#808080").lstrip("#")
    if len(h) != 6:
        return (0.5, 0.5, 0.5)
    return (int(h[0:2], 16) / 255.0, int(h[2:4], 16) / 255.0, int(h[4:6], 16) / 255.0)


def _color_name(hex_col, metallic=0.0):
    r, g, b = hex_to_rgb01(hex_col)
    h, s, v = colorsys.rgb_to_hsv(r, g, b)
    h_deg = h * 360.0
    if metallic >= 0.5:
        if s > 0.28 and 25 <= h_deg <= 65:
            return "Gold"
        if s > 0.25 and (h_deg < 25 or h_deg > 340):
            return "Copper"
        if v < 0.30:
            return "Gunmetal"
        return "Silver"
    if v < 0.14:
        return "Black"
    if s < 0.12:
        if v > 0.84:
            return "White"
        if v > 0.62:
            return "Light Grey"
        if v > 0.34:
            return "Grey"
        return "Charcoal"
    if s < 0.26 and v > 0.72:
        if 18 <= h_deg <= 55:
            return "Cream"
        if 180 <= h_deg <= 245:
            return "Ice Blue"
        if 300 <= h_deg or h_deg < 18:
            return "Blush"
    if s < 0.35 and 18 <= h_deg <= 48 and v < 0.55:
        return "Brown"
    if s < 0.42 and 25 <= h_deg <= 52 and v >= 0.55:
        return "Tan"
    if h_deg < 12 or h_deg >= 342:
        if v < 0.45:
            return "Burgundy"
        return "Pink" if (v > 0.72 and s < 0.55) else "Red"
    if h_deg < 36:
        return "Brown" if v < 0.48 else "Orange"
    if h_deg < 62:
        return "Olive" if (v < 0.52 and s < 0.55) else "Yellow"
    if h_deg < 155:
        return "Forest Green" if v < 0.42 else ("Mint" if s < 0.38 and v > 0.7 else "Green")
    if h_deg < 195:
        return "Teal" if v < 0.58 else "Cyan"
    if h_deg < 252:
        if v < 0.38:
            return "Navy"
        if s < 0.42:
            return "Slate Blue"
        return "Blue"
    if h_deg < 292:
        return "Purple"
    if h_deg < 342:
        return "Hot Pink" if (s > 0.55 and v > 0.65) else ("Magenta" if v > 0.45 else "Plum")
    return "Grey"


def _garment_stem(name):
    m = STEM_RE.match(name.lower())
    return f"{m.group(1)}_{m.group(2)}" if m else ""


def _garment_type(name):
    m = TYPE_RE.match(name.lower())
    return m.group(1) if m else ""


def _parse_muv_database():
    """Load and resolve all 13,070 8-slot garment presets from data.rpf/00001b95b000.pso."""
    s, _ = sections_of(MUV_DB_PSO.read_bytes())
    d = Decoder(s, {})
    blks = {i + 1: (k, data) for i, (k, data, _) in enumerate(d.blocks)}

    def load_hm(bk_t, bs, es):
        out = {}
        for _bn, (bk, data) in blks.items():
            if bk != bk_t:
                continue
            for r_i in range(len(data) // bs):
                rec = data[r_i * bs:(r_i + 1) * bs]
                for e in range(16):
                    if rec[e] == 0x80:
                        continue
                    sl = rec[16 + e * es:16 + (e + 1) * es]
                    out[struct.unpack_from("<I", sl, 0)[0]] = sl[8:]
        return out

    def read_arr(buf, off, esz):
        cnt, _cap, pb = struct.unpack_from("<IIQ", buf, off)
        b1, o1 = ptr(pb)
        if cnt and b1 in blks and o1 + cnt * esz <= len(blks[b1][1]):
            return blks[b1][1][o1:o1 + cnt * esz], cnt
        return b"", 0

    def parse_params(buf, fo, co, io_, to):
        flts, cols, texs = [], [], []
        if fo is not None:
            raw, cnt = read_arr(buf, fo, 12)
            for i in range(cnt):
                flts.append(struct.unpack_from("<IfBB", raw, i * 12))
        if co is not None:
            raw, cnt = read_arr(buf, co, 12)
            for i in range(cnt):
                cols.append(struct.unpack_from("<IIBB", raw, i * 12))
        if to is not None:
            raw, cnt = read_arr(buf, to, 24)
            for i in range(cnt):
                texs.append(struct.unpack_from("<IxxxxQBB", raw, i * 24))
        return flts, cols, texs

    map_base = load_hm(0x101, 1040, 64)
    map_mat = load_hm(0x103, 1424, 88)
    map_gar = load_hm(0x107, 2832, 176)

    # Pre-parse base materials (metallic, roughness, weave tex)
    base_info = {}
    for bk_, bm in map_base.items():
        flts, _cols, texs = parse_params(bm, 8, None, None, 24)
        met = 0.0
        rough = 0.85
        weave = ""
        for nh, v, _i1, _i2 in flts:
            if nh == H_BASE_METALLIC:
                met = float(v)
            elif nh == H_BASE_ROUGHNESS:
                rough = float(v)
        for nh, v, _i1, _i2 in texs:
            if nh == H_BASE_WEAVE_TEX and v:
                weave = f"{v:016x}"
        base_info[bk_] = (round(met, 3), round(rough, 3), weave)

    # Pre-parse 7,993 material swatches (map_mat)
    mat_info = {}
    for mk, mb in map_mat.items():
        bk_ = struct.unpack_from("<I", mb, 4)[0]
        met, rough, weave = base_info.get(bk_, (0.0, 0.85, ""))
        flts, cols, texs = parse_params(mb, 8, 24, 40, 56)
        base_cols = {}
        pat_cols = {}
        pat_texs = {}
        pat_scales = {}
        pat_opacs = {}
        weave_scale = 200.0
        for nh, v, i1, _i2 in cols:
            if nh == H_BASE_COLOR:
                base_cols[i1] = abgr_to_hex(v)
            elif nh == H_PAT_COLOR:
                pat_cols[i1] = abgr_to_hex(v)
        for nh, v, i1, _i2 in texs:
            if nh == H_PAT_TEX and v:
                pat_texs[i1] = f"{v:016x}"
        for nh, v, i1, _i2 in flts:
            if nh == H_PAT_SCALE:
                pat_scales[i1] = float(v)
            elif nh == H_PAT_OPACITY:
                pat_opacs[i1] = float(v)
            elif nh == H_WEAVE_SCALE:
                weave_scale = float(v)
        mat_info[mk] = {
            "met": met,
            "rough": rough,
            "weave": weave,
            "wscale": weave_scale,
            "bc": base_cols,
            "pc": pat_cols,
            "pt": pat_texs,
            "ps": pat_scales,
            "po": pat_opacs,
        }

    # Resolve all 13,070 garment presets (map_gar)
    resolved = {}
    for gk, gb in map_gar.items():
        s0 = struct.unpack_from("<8I", gb, 8)
        g_flts, g_cols, g_texs = parse_params(gb, 104, 120, 136, 152)

        # Group garment overrides by slotIdx (0..7 or 255=global)
        ov_bc = collections.defaultdict(dict)
        ov_pc = collections.defaultdict(dict)
        ov_pt = collections.defaultdict(dict)
        ov_ps = collections.defaultdict(dict)
        ov_po = collections.defaultdict(dict)
        for nh, v, i1, i2 in g_cols:
            if nh == H_BASE_COLOR:
                ov_bc[i2][i1] = abgr_to_hex(v)
            elif nh == H_PAT_COLOR:
                ov_pc[i2][i1] = abgr_to_hex(v)
        for nh, v, i1, i2 in g_texs:
            if nh == H_PAT_TEX and v:
                ov_pt[i2][i1] = f"{v:016x}"
        for nh, v, i1, i2 in g_flts:
            if nh == H_PAT_SCALE:
                ov_ps[i2][i1] = float(v)
            elif nh == H_PAT_OPACITY:
                ov_po[i2][i1] = float(v)

        slots_out = []
        swatches = []
        label_parts = []
        has_pattern = 0

        for si in range(8):
            mh = s0[si]
            mi = mat_info.get(mh)
            if mi:
                bc = dict(mi["bc"])
                pc = dict(mi["pc"])
                pt = dict(mi["pt"])
                ps = dict(mi["ps"])
                po = dict(mi["po"])
                met = mi["met"]
                rough = mi["rough"]
                weave = mi["weave"]
                wscale = mi["wscale"]
            else:
                bc, pc, pt, ps, po = {}, {}, {}, {}, {}
                met, rough, weave, wscale = 0.0, 0.85, "", 200.0

            # Apply global (255) then per-slot (si) garment overrides
            for key_slot in (255, si):
                bc.update(ov_bc.get(key_slot, ()))
                pc.update(ov_pc.get(key_slot, ()))
                pt.update(ov_pt.get(key_slot, ()))
                ps.update(ov_ps.get(key_slot, ()))
                po.update(ov_po.get(key_slot, ()))

            base_col = bc.get(0, "#7a7a7a")
            layers = []
            for li in range(3):
                th = pt.get(li)
                if not th:
                    continue
                lcol = pc.get(li, base_col)
                # Layer 0 is often micro-weave; layers 1..2 are print/pattern masks (default opacity 0.9 if omitted on high-contrast pattern)
                if li in po:
                    opac = max(0.0, min(1.0, po[li]))
                else:
                    # When 0x568fd269 is omitted for layers 1/2 (e.g. leopard print on bikini11), full pattern blend is active!
                    opac = 0.92 if li >= 1 else 0.35
                su = ps.get(li * 2, ps.get(0, 60.0 if li >= 1 else wscale))
                sv = ps.get(li * 2 + 1, su)
                layers.append({
                    "t": th,
                    "c": lcol,
                    "o": round(opac, 3),
                    "su": round(su, 1),
                    "sv": round(sv, 1),
                })
                if li >= 1 and opac >= 0.45 and su <= 120.0:
                    has_pattern = 1

            slots_out.append({
                "b": base_col,
                "m": met,
                "r": rough,
                "l": layers,
            })

            # Collect distinct swatch colors & label words (skip unpopulated slots)
            if mh or si in ov_bc:
                if base_col not in swatches and len(swatches) < 4:
                    swatches.append(base_col)
                cname = _color_name(base_col, met)
                if cname not in label_parts and len(label_parts) < 3:
                    label_parts.append(cname)

        if not swatches:
            swatches = ["#7a7a7a"]
        if not label_parts:
            label_parts = ["Default"]
        if has_pattern and len(label_parts) >= 1:
            label_parts[0] = f"{label_parts[0]} Print"
        label = " · ".join(label_parts)
        resolved[gk] = (f"{gk:08x}", label, json.dumps(swatches), has_pattern, json.dumps(slots_out, separators=(",", ":")))

    return resolved


def build_index():
    """Build LIBRARY/clothing_presets.sqlite3."""
    print("1/4 Loading MUV material database from data.rpf/00001b95b000.pso ...", flush=True)
    resolved = _parse_muv_database()
    valid_presets = set(resolved.keys())
    print(f"    Resolved {len(resolved):,} 8-slot garment MUV presets.", flush=True)

    inv = sqlite3.connect(INVENTORY_DB.as_uri() + "?mode=ro", uri=True)
    inv.row_factory = sqlite3.Row
    all_models = []
    muv_clothing_models = set()
    all_clothing_models = set()
    for r in inv.execute(
        "SELECT DISTINCT name, category, materials FROM model "
        "WHERE name IS NOT NULL AND status='indexed' "
        "AND category IN ('Clothing & accessories','Protagonists','Characters')"
    ):
        nm = r["name"]
        all_models.append(nm)
        if r["category"] == "Clothing & accessories":
            all_clothing_models.add(nm)
            mats = json.loads(r["materials"] or "[]")
            if any(m.get("shaderHash") in MUV_SHADER_HASHES for m in mats):
                muv_clothing_models.add(nm)
    inv.close()

    m64_models = collections.defaultdict(list)
    j32_models = collections.defaultdict(list)
    for nm in all_models:
        m64_models[murmur64(nm.encode())].append(nm)
        j32_models[_name_hash(nm) & 0xFFFFFFFF].append(nm)

    # Map drp_offset -> ped archetype name from ped_archetypes.sqlite3
    drp_to_arch = {}
    if PED_ARCH_DB.is_file():
        pa_db = sqlite3.connect(PED_ARCH_DB.as_uri() + "?mode=ro", uri=True)
        for off, nm in pa_db.execute("SELECT drp_offset, name FROM ped_archetype WHERE drp_offset > 0"):
            drp_to_arch[int(off)] = nm
        pa_db.close()

    def extract_muv_links(blocks):
        by_id = {}
        presets = []
        for _bn, (bk, data) in blocks.items():
            if bk != 0xF795E8BA:
                continue
            for idx in range(len(data) // 88):
                r = data[idx * 88:(idx + 1) * 88]
                pid, par, c1, _u12, p1, _cat, _u28 = struct.unpack_from("<IIIIQII", r, 0)
                u36 = struct.unpack_from("<I", r, 36)[0]
                p2 = struct.unpack_from("<Q", r, 48)[0]
                c2 = struct.unpack_from("<H", r, 56)[0]
                tags = read_tags(blocks, p2, c2) if c2 else {}
                rb, ro = ptr(p1)
                drawables = set()
                for tk in DRAWABLE_TAGS:
                    for v in tags.get(tk, ()):
                        drawables.update(j32_models.get(v, ()))
                if c1 and rb in blocks and blocks[rb][0] == 0x37E52277:
                    rdata = blocks[rb][1]
                    for si in range(c1):
                        if ro + (si + 1) * 184 > len(rdata):
                            break
                        h0 = struct.unpack_from("<Q", rdata, ro + si * 184 + 8)[0]
                        if h0 in m64_models:
                            drawables.update(m64_models[h0])
                ent = (pid, par, u36, drawables)
                presets.append(ent)
                by_id[pid] = ent
        links = []
        for _pid, par, u36, drawables in presets:
            if not u36 or u36 not in valid_presets:
                continue
            models = set(drawables)
            cur, depth = par, 0
            while not models and cur and cur in by_id and depth < 6:
                _, ppar, _, pdraw = by_id[cur]
                models.update(pdraw)
                cur = ppar
                depth += 1
            for m in models:
                links.append((m, u36))
        return links

    print("2/4 Scanning packs.rpf/00011ccb2000.pso ...", flush=True)
    s_pso, _ = sections_of(PRESETS_PSO.read_bytes())
    d_pso = Decoder(s_pso, {})
    pso_blks = {i + 1: (k, data) for i, (k, data, _) in enumerate(d_pso.blocks)}
    pso_counts = collections.Counter(extract_muv_links(pso_blks))
    print(f"    00011ccb2000.pso links: {len(pso_counts):,} unique (model, preset) pairs.", flush=True)

    print("3/4 Scanning all 2,709 0DRP ped catalogues in packs.rpf ...", flush=True)
    spans = PACKS_SPANS.read_bytes()
    srows = [struct.unpack_from("<QQQII", spans, i * 32) for i in range(len(spans) // 32)]
    n_s = len(srows)

    def worker(ci):
        lo = ci * 4000
        hi = min(n_s, lo + 4000)
        local_drp = collections.Counter()
        local_char = collections.Counter()
        with open(PACKS_RPF, "rb") as f:
            i = lo
            while i < hi:
                off, clen, *_ = srows[i]
                f.seek(off)
                try:
                    head = zlib.decompressobj().decompress(f.read(min(clen, 128)), 32)
                except Exception:
                    i += 1
                    continue
                if len(head) < 24 or head[16:20] != b"0DRP":
                    i += 1
                    continue
                f.seek(off)
                buf = bytearray(zlib.decompress(f.read(clen)))
                j = i
                while (
                    j + 1 < n_s
                    and ((srows[j][0] + srows[j][1] + 15) & -16) == srows[j + 1][0]
                    and len(buf) % 65536 == 0
                ):
                    j += 1
                    f.seek(srows[j][0])
                    buf.extend(zlib.decompress(f.read(srows[j][1])))
                rk, blks_ = parse_0drp_blocks(buf)
                if rk == CATALOG_ROOT and blks_:
                    links = extract_muv_links(blks_)
                    arch = drp_to_arch.get(off, "")
                    for m, u36 in links:
                        local_drp[(m, u36)] += 1
                        if arch:
                            local_char[(arch.lower(), m.lower(), u36)] += 1
                i = j + 1
        return local_drp, local_char

    drp_counts = collections.Counter()
    char_counts = collections.Counter()
    with ThreadPoolExecutor(max_workers=8) as ex:
        for ld, lc in ex.map(worker, range((n_s + 3999) // 4000)):
            drp_counts.update(ld)
            char_counts.update(lc)

    # Combine exact (model, preset) counts + build stem & type fallback tables for 100% MUV coverage
    exact_by_model = collections.defaultdict(collections.Counter)
    exact_src = {}
    for (m, u36), cnt in pso_counts.items():
        exact_by_model[m.lower()][u36] += cnt * 3  # boost canonical catalogue defaults
        exact_src[(m.lower(), u36)] = "catalog"
    for (m, u36), cnt in drp_counts.items():
        exact_by_model[m.lower()][u36] += cnt
        exact_src.setdefault((m.lower(), u36), "0drp")

    by_stem = collections.defaultdict(collections.Counter)
    by_type = collections.defaultdict(collections.Counter)
    global_top = collections.Counter()
    for ml, ctr in exact_by_model.items():
        st = _garment_stem(ml)
        tp = _garment_type(ml)
        for u36, cnt in ctr.items():
            if st:
                by_stem[st][u36] += cnt
            if tp:
                by_type[tp][u36] += cnt
            global_top[u36] += cnt

    # Build final model_preset rows (exact + stem + type fallback so every MUV clothing item has presets)
    model_preset_rows = []
    cov = collections.Counter()
    seen_models = set(exact_by_model.keys())
    for ml, ctr in exact_by_model.items():
        ordered = ctr.most_common()
        for idx_p, (u36, cnt) in enumerate(ordered):
            model_preset_rows.append((ml, u36, cnt, 1 if idx_p == 0 else 0, exact_src.get((ml, u36), "0drp")))
        if ml in {x.lower() for x in muv_clothing_models}:
            cov["exact"] += 1

    # Add stem/type/fallback presets for MUV clothing models not directly referenced
    top_fallback = [u36 for u36, _ in global_top.most_common(12)]
    for nm in sorted(muv_clothing_models):
        ml = nm.lower()
        if ml in seen_models:
            continue
        st = _garment_stem(ml)
        tp = _garment_type(ml)
        if st and st in by_stem:
            ordered = by_stem[st].most_common(24)
            src = "stem"
            cov["stem"] += 1
        elif tp and tp in by_type:
            ordered = by_type[tp].most_common(24)
            src = "type"
            cov["type"] += 1
        else:
            ordered = [(u36, 1) for u36 in top_fallback]
            src = "fallback"
            cov["fallback"] += 1
        for idx_p, (u36, cnt) in enumerate(ordered):
            model_preset_rows.append((ml, u36, cnt, 1 if idx_p == 0 else 0, src))

    char_rows = [(arch, ml, u36, cnt) for (arch, ml, u36), cnt in char_counts.items()]

    print("4/4 Writing LIBRARY/clothing_presets.sqlite3 ...", flush=True)
    db = sqlite3.connect(":memory:")
    db.executescript("""
        create table muv_preset(
            preset_id integer primary key,
            preset_hex text unique,
            label text,
            swatches text,
            has_pattern integer,
            slots_json text
        );
        create table model_preset(
            model_name text,
            preset_id integer,
            count integer,
            is_default integer,
            source text,
            primary key(model_name, preset_id)
        );
        create table character_garment(
            arch_name text,
            model_name text,
            preset_id integer,
            count integer,
            primary key(arch_name, model_name, preset_id)
        );
        create index idx_mp_model on model_preset(model_name, is_default desc, count desc);
        create index idx_cg_arch on character_garment(arch_name, model_name, count desc);
    """)
    db.executemany(
        "insert into muv_preset values (?,?,?,?,?,?)",
        [(k, *v) for k, v in resolved.items()],
    )
    db.executemany("insert or replace into model_preset values (?,?,?,?,?)", model_preset_rows)
    db.executemany("insert or replace into character_garment values (?,?,?,?)", char_rows)
    db.commit()
    blob = db.serialize()
    db.close()
    tmp = OUT_DB.with_suffix(".sqlite3.tmp")
    tmp.write_bytes(blob)
    os.replace(tmp, OUT_DB)

    print(
        f"Wrote {OUT_DB.name}: {len(resolved):,} MUV presets | {len(model_preset_rows):,} model-preset links "
        f"| {len(char_rows):,} character-garment presets | MUV clothing coverage: {dict(cov)} "
        f"(total {sum(cov.values())}/{len(muv_clothing_models)})",
        flush=True,
    )


# Runtime Store & MUV Albedo Baker -----------------------------------------------------------------

class ClothingPresetStore:
    def __init__(self, db_path=OUT_DB):
        self.db_path = Path(db_path)
        self._lock = threading.Lock()
        self._preset_cache = {}
        self._pat_cache = collections.OrderedDict()
        self._bake_locks = {}

    def available(self):
        return self.db_path.is_file()

    def _conn(self):
        c = sqlite3.connect(self.db_path.as_uri() + "?mode=ro", uri=True)
        c.row_factory = sqlite3.Row
        return c

    def get_preset(self, preset_hex):
        ph = (preset_hex or "").lower().removeprefix("0x").zfill(8)
        with self._lock:
            if ph in self._preset_cache:
                return self._preset_cache[ph]
        if not self.available():
            return None
        c = self._conn()
        try:
            r = c.execute(
                "SELECT preset_id, preset_hex, label, swatches, has_pattern, slots_json "
                "FROM muv_preset WHERE preset_hex=?",
                (ph,),
            ).fetchone()
        finally:
            c.close()
        if not r:
            return None
        val = {
            "id": r["preset_hex"],
            "presetId": int(r["preset_id"]),
            "label": r["label"],
            "swatches": json.loads(r["swatches"]),
            "hasPattern": bool(r["has_pattern"]),
            "slots": json.loads(r["slots_json"]),
        }
        with self._lock:
            self._preset_cache[ph] = val
        return val

    def presets_for_model(self, model_name, limit=80):
        """Return defaultPreset + all available MUV variants for a clothing drawable."""
        if not self.available() or not model_name:
            return None
        ml = model_name.lower()
        c = self._conn()
        try:
            rows = c.execute(
                "SELECT mp.preset_id, mp.count, mp.is_default, mp.source, "
                "       p.preset_hex, p.label, p.swatches, p.has_pattern "
                "FROM model_preset mp "
                "JOIN muv_preset p ON p.preset_id = mp.preset_id "
                "WHERE mp.model_name = ? "
                "ORDER BY mp.is_default DESC, mp.count DESC, mp.preset_id ASC "
                "LIMIT ?",
                (ml, limit),
            ).fetchall()
            if not rows:
                # Try base name without _alt_XX / _cull_XX if exact name missed
                base_alt = re.sub(r"_(?:alt|cull)_\d+$", "_full", ml)
                if base_alt != ml:
                    rows = c.execute(
                        "SELECT mp.preset_id, mp.count, mp.is_default, mp.source, "
                        "       p.preset_hex, p.label, p.swatches, p.has_pattern "
                        "FROM model_preset mp "
                        "JOIN muv_preset p ON p.preset_id = mp.preset_id "
                        "WHERE mp.model_name = ? "
                        "ORDER BY mp.is_default DESC, mp.count DESC, mp.preset_id ASC "
                        "LIMIT ?",
                        (base_alt, limit),
                    ).fetchall()
        finally:
            c.close()
        if not rows:
            return None
        variants = [
            {
                "id": r["preset_hex"],
                "label": r["label"],
                "swatches": json.loads(r["swatches"]),
                "hasPattern": bool(r["has_pattern"]),
                "count": int(r["count"]),
                "source": r["source"],
            }
            for r in rows
        ]
        return {
            "defaultPreset": variants[0]["id"],
            "source": rows[0]["source"],
            "total": len(variants),
            "variants": variants,
        }

    def character_presets(self, arch_name):
        """Return {model_name_lower: default_preset_hex} and full list of character-native MUV presets."""
        if not self.available() or not arch_name:
            return {}, {}
        al = arch_name.lower()
        c = self._conn()
        try:
            rows = c.execute(
                "SELECT cg.model_name, cg.count, p.preset_hex, p.label, p.swatches, p.has_pattern "
                "FROM character_garment cg "
                "JOIN muv_preset p ON p.preset_id = cg.preset_id "
                "WHERE cg.arch_name = ? "
                "ORDER BY cg.model_name ASC, cg.count DESC, cg.preset_id ASC",
                (al,),
            ).fetchall()
        finally:
            c.close()
        defaults = {}
        all_by_model = collections.defaultdict(list)
        for r in rows:
            ml = r["model_name"]
            if ml not in defaults:
                defaults[ml] = r["preset_hex"]
            all_by_model[ml].append({
                "id": r["preset_hex"],
                "label": r["label"],
                "swatches": json.loads(r["swatches"]),
                "hasPattern": bool(r["has_pattern"]),
                "count": int(r["count"]),
                "characterNative": True,
            })
        return defaults, dict(all_by_model)

    def _load_pattern_rgb(self, lib, tex_hash):
        """Decode and cache a 0xd2b598e5 pattern texture as (float32 (H,W,3), is_srgb_color, is_r_only)."""
        with self._lock:
            hit = self._pat_cache.get(tex_hash)
            if hit is not None:
                self._pat_cache.move_to_end(tex_hash)
                return hit
        try:
            tinfo = lib.texture_info([tex_hash]).get(tex_hash)
            fmt = (tinfo[0].get("format", "") if tinfo else "").upper()
            raw, _ = lib.texture(tex_hash, 256)
            im = Image.open(io.BytesIO(raw)).convert("RGB")
            arr = np.asarray(im, dtype=np.float32) / 255.0
            g_max = float(arr[:, :, 1].max())
            b_max = float(arr[:, :, 2].max())
            is_r_only = (g_max < 0.04 and b_max < 0.04)
            # Only treat as full-color RGB print when texture is genuinely sRGB with multi-channel chroma
            sat = float((arr.max(axis=2) - arr.min(axis=2)).mean())
            is_color = ("SRGB" in fmt) and (not is_r_only) and (sat > 0.14)
            val = (arr, is_color, is_r_only)
        except Exception:
            val = (None, False, False)
        with self._lock:
            self._pat_cache[tex_hash] = val
            self._pat_cache.move_to_end(tex_hash)
            while len(self._pat_cache) > 256:
                self._pat_cache.popitem(last=False)
        return val

    def bake_muv_albedo(self, lib, model_id, preset_hex, max_res=512, mat_idx=0, cache_dir=None):
        """Bake an authentic sRGB PNG albedo for a MUV garment + preset, cached on disk."""
        preset = self.get_preset(preset_hex)
        if not preset:
            raise LookupError(f"Unknown MUV clothing preset {preset_hex}")
        ph = preset["id"]
        max_res = max(128, min(1024, int(max_res or 512)))
        mat_idx = max(0, int(mat_idx or 0))

        cpath = None
        if cache_dir is not None:
            cdir = Path(cache_dir) / "muv"
            cdir.mkdir(parents=True, exist_ok=True)
            cpath = cdir / f"v3_{int(model_id)}_{mat_idx}_{ph}_{max_res}.png"
            if cpath.is_file():
                return cpath.read_bytes()

        lkey = (int(model_id), mat_idx, ph, max_res)
        with self._lock:
            lock = self._bake_locks.setdefault(lkey, threading.Lock())
        with lock:
            if cpath is not None and cpath.is_file():
                return cpath.read_bytes()

            item = lib.item(int(model_id), lod=0)
            mats = item.get("materials") or []
            mat = next((m for m in mats if m.get("index") == mat_idx), mats[0] if mats else None)
            if not mat:
                raise LookupError(f"Model {model_id} has no material {mat_idx}")

            tmap = {}
            for t in mat.get("textures") or ():
                th = t.get("textureHash") or t.get("effectiveHash")
                if th and th not in PLACEHOLDER_CLOTHING_TEXTURES:
                    tmap[t["parameter"]] = th

            mid_hash = tmap.get("8adb8480")
            spec_hash = tmap.get("c6f46e79")
            params = mat.get("params") or {}
            uv_phys = params.get("6022028b") or [0.0, 0.0, 24.0, 24.0]
            size_u = float(uv_phys[2]) if len(uv_phys) >= 3 and uv_phys[2] > 0.1 else 24.0
            size_v = float(uv_phys[3]) if len(uv_phys) >= 4 and uv_phys[3] > 0.1 else 24.0

            # Decode specular (AO/cavity & target output aspect) and materialid (BC4 zone mask)
            spec_arr = None
            W, H = 512, 512
            if spec_hash:
                try:
                    s_raw, _ = lib.texture(spec_hash, max_res, model_id=int(model_id))
                    s_im = Image.open(io.BytesIO(s_raw)).convert("RGB")
                    W, H = s_im.size
                    spec_arr = np.asarray(s_im, dtype=np.float32) / 255.0
                except Exception:
                    spec_arr = None

            mid_arr = None
            if mid_hash:
                try:
                    m_raw, _ = lib.texture(mid_hash, max_res, model_id=int(model_id))
                    m_im = Image.open(io.BytesIO(m_raw)).convert("L")
                    if spec_arr is None:
                        W, H = m_im.size
                    elif m_im.size != (W, H):
                        m_im = m_im.resize((W, H), Image.Resampling.NEAREST)
                    mid_arr = np.asarray(m_im, dtype=np.uint8)
                except Exception:
                    mid_arr = None

            # Auto-detect & repair the ~7% pre-alpha 2:1 garments whose materialid BC4 was exported
            # cropped to the top half (v=0..0.5 stretched 2x vertically), such as brabikini_fr1_011!
            if mid_arr is not None and spec_arr is not None and W == 2 * H and H >= 64:
                mid_arr = self._repair_2x_crop_matid(mid_arr, spec_arr, item.get("name", ""), preset["slots"])

            if mid_arr is not None:
                # Exact RAGE 9 formula: materialid stores (slot + 0.5) / 64.0 -> values 2, 6, 10, 14, 18, 22, 26, 30
                slot_map = np.clip(mid_arr >> 2, 0, 7)
            else:
                slot_map = np.zeros((H, W), dtype=np.uint8)

            yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
            u_grid = xx / float(W)
            v_grid = yy / float(H)

            out = np.zeros((H, W, 3), dtype=np.float32)
            active_slots = np.unique(slot_map)
            slots = preset["slots"]

            for si in active_slots:
                si_i = int(si)
                mask = (slot_map == si_i)
                if not mask.any():
                    continue
                sd = slots[si_i] if si_i < len(slots) else slots[0]
                bcol = np.array(hex_to_rgb01(sd["b"]), dtype=np.float32)
                met = float(sd.get("m", 0.0))
                col = np.broadcast_to(bcol, (H, W, 3)).copy()

                layers = sd.get("l") or ()
                for li, lyr in enumerate(layers):
                    opac = float(lyr.get("o", 0.0))
                    if opac <= 0.01:
                        continue
                    pat_arr, is_color, is_r_only = self._load_pattern_rgb(lib, lyr["t"])
                    if pat_arr is None:
                        continue
                    su = float(lyr.get("su", 60.0))
                    sv = float(lyr.get("sv", su))
                    # Physical UV tiling: 6022028b [sizeU, sizeV] * scale / 600.0 matches in-game pattern density
                    tile_u = max(0.5, min(18.0, (size_u * su) / 600.0))
                    tile_v = max(0.5, min(18.0, (size_v * sv) / 600.0))
                    ph_t, pw_t, _ = pat_arr.shape
                    tx_ = ((u_grid * tile_u) % 1.0 * pw_t).astype(np.int32) % pw_t
                    ty_ = ((v_grid * tile_v) % 1.0 * ph_t).astype(np.int32) % ph_t
                    sampled = pat_arr[ty_, tx_]
                    lcol = np.array(hex_to_rgb01(lyr["c"]), dtype=np.float32)

                    if is_color:
                        # Full-color sRGB print pattern texture
                        w = opac
                        col = col * (1.0 - w) + (sampled * (0.45 + 0.55 * lcol[None, None, :])) * w
                    elif li == 0 and is_r_only:
                        # Layer 0 R-only BC1/BC7 weave/grain texture: subtle recessed fabric texture
                        # (1.0 = raised thread, 0.0 = weave valley) without crushing base fabric brightness
                        m_val = sampled[:, :, 0:1]
                        w = np.clip((1.0 - m_val) * min(opac, 0.35), 0.0, 0.35)
                        col = col * (1.0 - w) + lcol[None, None, :] * w
                    else:
                        # Grayscale pattern mask (e.g. leopard rosettes, stripes, denim twill): R channel = mask
                        m_val = sampled[:, :, 0:1]
                        eff_opac = opac * (0.35 if (li == 0 and su >= 150.0) else 1.0)
                        w = np.clip(m_val * eff_opac, 0.0, 1.0)
                        col = col * (1.0 - w) + lcol[None, None, :] * w

                if met >= 0.5:
                    # Brighten metallic hardware slightly with subtle highlight curve so silver/gold rings gleam
                    if spec_arr is not None:
                        shine = np.clip(spec_arr[:, :, 0:1] * 0.45 + spec_arr[:, :, 1:2] * 0.55, 0.35, 1.25)
                        col = np.clip(col * (0.85 + 0.45 * shine) + 0.08 * shine, 0.0, 1.0)
                    else:
                        col = np.clip(col * 1.15 + 0.06, 0.0, 1.0)

                out[mask] = col[mask]

            # Apply garment specular AO / cavity shading so fabric folds, seams, and stitching read naturally
            if spec_arr is not None:
                # In RAGE 9 ped_standard_muv, c6f46e79 BC7 G/B channels carry garment AO/cavity (median ~0.55..0.75)
                ao = np.clip(spec_arr[:, :, 1:2] * 0.5 + spec_arr[:, :, 2:3] * 0.5, 0.0, 1.0)
                med = float(np.median(ao))
                if med > 0.05:
                    ao_norm = np.clip(ao / max(med, 0.25), 0.52, 1.16)
                    out = np.clip(out * (0.30 + 0.70 * ao_norm), 0.0, 1.0)

            buf = io.BytesIO()
            Image.fromarray((np.clip(out, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8), mode="RGB").save(
                buf, format="PNG", compress_level=3,
            )
            png = buf.getvalue()
            if cpath is not None:
                try:
                    tmp = cpath.with_suffix(".tmp")
                    tmp.write_bytes(png)
                    os.replace(tmp, cpath)
                except OSError:
                    pass
            return png

    def _repair_2x_crop_matid(self, mid_arr, spec_arr, model_name, slots):
        """Repair pre-alpha 2:1 BC4 materialid textures accidentally exported cropped to y=0..0.5."""
        H, W = mid_arr.shape
        spec_l = spec_arr.mean(axis=2)
        sg = np.abs(np.diff(spec_l, axis=0))
        mg = (np.abs(np.diff(mid_arr.astype(np.float32), axis=0)) > 1.0).astype(np.float32)
        c_1to1 = float((sg * mg).mean())
        mg_half = mg[::2, :]
        c_half = float((sg[:mg_half.shape[0], :] * mg_half).mean())

        nl = (model_name or "").lower()
        is_bikini11 = "brabikini_fr1_011" in nl
        if not is_bikini11 and not (c_half > c_1to1 * 2.2 and c_half > 0.04):
            return mid_arr

        # Squish the 2x-stretched materialid back into its true y=0..H//2 extent
        half_h = H // 2
        top_half = mid_arr[::2, :][:half_h, :]
        fixed = np.zeros_like(mid_arr)
        fixed[:half_h, :] = top_half

        if is_bikini11:
            # brabikini_011 UV atlas: top half (0..H/2) is right cup, bottom half (H/2..H) is its
            # 180-degree mirrored left cup (x < 0.72*W); right columns (x >= 0.84*W) are full-height straps;
            # center gap (0.70..0.84*W) holds the metallic O-ring (slot 7).
            cup_end = int(W * 0.72)
            strap_start = int(W * 0.83)
            # Mirror top cup (0..cup_end) into bottom cup (half_h..H) with slight horizontal fit
            fixed[half_h:, :cup_end] = top_half[::-1, cup_end - 1::-1]
            # Extend vertical strap columns down the full height
            fixed[half_h:, strap_start:] = top_half[:, strap_start:]
            # Assign center hardware region (where O-ring UVs sit) to slot 7 if slot 7 is metallic
            if len(slots) >= 8 and float(slots[7].get("m", 0.0)) >= 0.5:
                fixed[:, cup_end:strap_start] = 30  # slot 7 (4*7 + 2 = 30)
        else:
            # Generic 2x-crop repair (e.g. shortsswim_mr1_005, skirtpencil_fr1_001):
            # lower half continues main garment fabric (slot 0 = value 2)
            fixed[half_h:, :] = 2
        return fixed


_STORE = None


def get_store():
    global _STORE
    if _STORE is None:
        _STORE = ClothingPresetStore()
    return _STORE


if __name__ == "__main__":
    build_index()
