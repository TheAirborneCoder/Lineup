# SPDX-License-Identifier: GPL-3.0-or-later
"""Extract all 2,714 ped archetypes from packs.rpf/00011d1ff000.pso + 00011cfbf000.pso + 2,709 0DRP catalogues.

Maps every ped archetype hash in 00011d1ff000.pso to:
- its resolved internal archetype name (when present in any game archive/index, else its alphabetical
  range prefix + short name/hash),
- its exact 0DRP ped appearance catalogue in packs.rpf (matched 100% via 00011cfbf000.pso outfit hashes),
- its primary LOD0 head drawable, bespoke face albedo/normal hashes, eye drawable, body rig,
  and representative clothing drawables from that archetype's 0DRP.

Writes LIBRARY/ped_archetypes.sqlite3.

    python engine/ped_archetypes.py"""
import collections
from concurrent.futures import ThreadPoolExecutor
import json
import os
import re
import sqlite3
import struct
import zlib

from decode_pso import Decoder, sections_of  # noqa: E402
from face_presets import (  # noqa: E402
    CATALOG_ROOT, DRAWABLE_TAGS, HEAD_CATEGORY, PACKS_RPF, PACKS_SPANS,
    parse_0drp_blocks, ptr, read_tags,
)
from script_symbol_names import murmur64  # noqa: E402
from script_tree import _name_hash  # noqa: E402
from paths import GAME, INDEX, LIBRARY, ENGINE

ARCHETYPES_PSO = INDEX / "packs.rpf/00011d1ff000.pso"
OUTFITS_PSO = INDEX / "packs.rpf/00011cfbf000.pso"
HAND_VAR_PSO = INDEX / "packs.rpf/00011cb99000.pso"
FEET_VAR_PSO = INDEX / "packs.rpf/00011cb92000.pso"
FACE_VAR_PSO = INDEX / "packs.rpf/00011cba4000.pso"
BASE_VAR_PSO = INDEX / "packs.rpf/00011cc90000.pso"
BODYSHAPE_PSO = INDEX / "anim.rpf/000106153000.pso"
INVENTORY_DB = LIBRARY / "inventory.sqlite3"
TEXTURES_DB = LIBRARY / "textures.sqlite3"
OUT_DB = LIBRARY / "ped_archetypes.sqlite3"

HAND_CATEGORY = 0x3817131D
FEET_CATEGORY = 0x44925BFD
EYES_CATEGORY = 0xEA24B45E
TEETH_CATEGORY = 0x96EDAE5C
SKIN_CATEGORIES = {HAND_CATEGORY, FEET_CATEGORY, 0x9D283797, 0xC8BC4E01, 0x2A7A13A4}
IGNORE_SKIN_ALBEDOS = {"", "0000000000000000", "d2288f458ac8c7e2", "1e29578a23d839f3"}
SKIP_CLOTHING_PREFIXES = (
    "hair", "beard", "brows", "lashes", "armpithair", "bodyhair", "chesthair",
    "genitalhair", "stubble", "eyecap", "scextrahiddenpoly", "teeth", "hand_", "feet_",
    "bodygenitals", "bodynipples", "fingernails", "toenails",
)
RIG_RE = re.compile(r"_(f[a-z]{1,2}\d|m[a-z]{1,2}\d)_")
TOK_RE = re.compile(rb"[A-Za-z][A-Za-z0-9_]{2,56}")
PED_PFX_RE = re.compile(
    rb"\b(?:cs_|ig_|a_f_|a_m_|s_f_|s_m_|g_f_|g_m_|u_f_|u_m_|mp_f_|mp_m_|a_c_|hc_|sc_|player_|ex_|cti_|m_|cr[abcfimr]_)[A-Za-z0-9_]{2,54}\b",
    re.I,
)

# Visually verified clean skin textures in ttdb_peds_0..3_3.ttpf mapped by (gender, avgRGB)
# Excludes dismemberment/gore stump textures (7365f054, 36af4a6b, d77eaee1, 7c09b1de, 2c7f049e, 1e08a810)
# and guarantees female peds never receive male chest/nipple textures (and vice versa).
VERIFIED_CLEAN_HAND = {
    "F": [
        ((191, 159, 148), "5e2549973f2e4663", "Very Fair (Female)"),
        ((181, 137, 123), "27d85c83d55837f1", "Fair / Caucasian (Female)"),
        ((170, 126, 100), "09a57311dddec1fa", "Warm Tan (Female)"),
        ((168, 125, 99),  "5933afeb09e086b3", "Warm Tan Aged (Female)"),
        ((156, 118, 93),  "6a602971d0ad81ab", "Olive Aged (Female)"),
        ((155, 121, 98),  "73b0adca2de58beb", "Olive / Latina (Female)"),
        ((155, 120, 98),  "61210a63220f6879", "Olive / Lucia (Female)"),
        ((99, 69, 57),    "d1301a5592bf22b6", "Dark Brown (Female)"),
        ((98, 68, 56),    "3dd1029c27687f16", "Dark Brown Aged (Female)"),
        ((76, 55, 45),    "958e810aa52a64b2", "Deep Dark (Female)"),
    ],
    "M": [
        ((192, 159, 147), "8e98c5fb0689057f", "Very Fair (Male)"),
        ((180, 136, 122), "0bee707e0b7523ef", "Fair / Caucasian (Male)"),
        ((178, 135, 121), "32ea8e71cf08449c", "Fair Hairy (Male)"),
        ((178, 134, 121), "cceffdf7ceb8fae4", "Fair Aged (Male)"),
        ((173, 129, 111), "c17045096fb434eb", "Medium Fair (Male)"),
        ((172, 130, 113), "702e9063be82161d", "Medium Fair / Jason (Male)"),
        ((170, 127, 100), "b9667b40e0d354d3", "Warm Tan (Male)"),
        ((168, 125, 98),  "151f9aee179548eb", "Warm Tan Aged (Male)"),
        ((155, 121, 98),  "96f2c7a594e9a3e5", "Olive / Latino (Male)"),
        ((154, 120, 97),  "788d82330e4c6e7d", "Olive Aged (Male)"),
        ((99, 69, 57),    "fac2d7272f698768", "Dark Brown (Male)"),
        ((76, 55, 46),    "9400d0b4d1bc8f1b", "Deep Dark (Male)"),
    ],
}

VERIFIED_CLEAN_FEET = {
    "F": [
        ((184, 154, 147), "ccedb4ae4a2c1a73", "Very Fair (Female)"),
        ((173, 133, 121), "613a80072abe54c4", "Fair / Caucasian (Female)"),
        ((171, 132, 122), "17d32d189317e6b9", "Fair Aged (Female)"),
        ((160, 122, 99),  "53daad3cb9e23528", "Warm Tan (Female)"),
        ((159, 121, 98),  "09eb16041d46fbbb", "Warm Tan Aged (Female)"),
        ((150, 115, 94),  "e14fda608bef9ef5", "Medium Tan (Female)"),
        ((145, 113, 94),  "06c4f867d482a5bf", "Olive / Latina (Female)"),
        ((144, 111, 93),  "684784bdaaf9dd3c", "Olive / Lucia (Female)"),
        ((94, 66, 56),    "7e4ad11f38c0f381", "Dark Brown (Female)"),
        ((93, 65, 55),    "343150b3c068636d", "Dark Brown Aged (Female)"),
        ((75, 54, 46),    "b713edaf1da41d7c", "Deep Dark (Female)"),
    ],
    "M": [
        ((184, 153, 146), "d05f926a841d68ca", "Very Fair (Male)"),
        ((173, 133, 122), "149f4a09284aaaa8", "Fair / Caucasian (Male)"),
        ((171, 132, 120), "b88c085b73275d7d", "Fair Aged (Male)"),
        ((160, 122, 99),  "337d40a9e3c87a70", "Warm Tan (Male)"),
        ((160, 118, 101), "c1ea3112fbc3488c", "Medium Fair / Jason (Male)"),
        ((159, 121, 98),  "09eb16041d46fbbb", "Warm Tan Aged (Male)"),
        ((149, 117, 97),  "e9f857ca3a4437aa", "Olive / Latino (Male)"),
        ((148, 116, 96),  "dac68494338f6bd5", "Olive Aged (Male)"),
        ((94, 66, 56),    "b5ff690016fc006b", "Dark Brown (Male)"),
        ((94, 65, 55),    "8659c80916049f0e", "Dark Brown Aged (Male)"),
        ((76, 55, 47),    "f929b3ba5670d965", "Deep Dark (Male)"),
    ],
}

# Verified male genital (bodygenitalsgen_mr1_*) textures in ttdb_peds with measured avgRGB
MALE_GENITAL_SKINS = [
    ((179, 144, 133), "d27599939e20342f"),
    ((165, 124, 112), "bce4d91da23689c7"),
    ((144, 105, 80),  "270a1487560e55ce"),
    ((132, 101, 80),  "0a5e0ae4e524948b"),
    ((86, 58, 49),    "35598434464541b0"),
    ((67, 48, 41),    "46424e1f69765c64"),
]

# All 34 cracked RAGE 9 BodyShapePreset morph channel names (lowercase JOAAT -> canonical name)
_MORPH_CHANNEL_NAMES = [
    ("GCBreasts", "gcbreasts"),
    ("GCButt", "gcbutt"),
    ("GCCleavage", "gccleavage"),
    ("gchipwidth", "gchipwidth"),
    ("gchipheight", "gchipheight"),
    ("gcbreastheight", "gcbreastheight"),
    ("gcchestheight", "gcchestheight"),
    ("gcbreastspacing", "gcbreastspacing"),
    ("gcmusclebiceps", "gcmusclebiceps"),
    ("gcmuscletriceps", "gcmuscletriceps"),
    ("gcbellyround", "gcbellyround"),
] + [
    (pfx + part, pfx + part)
    for pfx in ("gcweight", "gcmuscle")
    for part in ("back", "belly", "butt", "calf", "chest", "forearm", "hip", "muffin", "neck", "shoulder", "thigh", "upperarm")
]
from char_outfits import resolve_eye_texture  # noqa: E402
from glb_morph_bake import NEWLY_CRACKED_MORPH_NAMES  # noqa: E402

MORPH_HASH_TO_NAME = {_name_hash(low) & 0xFFFFFFFF: canon for canon, low in _MORPH_CHANNEL_NAMES}
MORPH_HASH_TO_NAME.update(NEWLY_CRACKED_MORPH_NAMES)
MORPH_HASH_TO_NAME[0xB0EAE798] = "0xb0eae798"


def load_var_pso_rgb(path):
    """Parse a 0x1edee47e variation PSO into {tex_hash_hex: (r, g, b)} from 0x28689f25 +80."""
    if not path.is_file():
        return {}
    s, _ = sections_of(path.read_bytes())
    dec = Decoder(s, {})
    out = {}
    for th, bdata, *_ in dec.blocks:
        if th == 0x00000101 and len(bdata) >= 1552 and (len(bdata) % 1552 == 0):
            for k in range(len(bdata) // 1552):
                chunk = bdata[k * 1552:(k + 1) * 1552]
                ctrl = chunk[:16]
                for si in range(16):
                    if ctrl[si] == 0x80:
                        continue
                    slot = chunk[16 + si * 96:16 + (si + 1) * 96]
                    k64 = struct.unpack_from("<Q", slot, 0)[0]
                    if not k64:
                        continue
                    r, g, b, _ = struct.unpack_from("<BBBB", slot, 88)
                    if r or g or b:
                        out[f"{k64:016x}"] = (r, g, b)
    return out


def load_bodyshape_morphs():
    """Load all 3,443 BodyShapePreset morph records from anim.rpf/000106153000.pso."""
    if not BODYSHAPE_PSO.is_file():
        return {}
    s, _ = sections_of(BODYSHAPE_PSO.read_bytes())
    dec = Decoder(s, {})
    ab = {i + 1: (blk[0], blk[1]) for i, blk in enumerate(dec.blocks)}
    morph_db = {}
    for _bnum, (th, bdata) in ab.items():
        if th == 0x00000101 and len(bdata) >= 784 and (len(bdata) % 784 == 0):
            for k in range(len(bdata) // 784):
                chunk = bdata[k * 784:(k + 1) * 784]
                ctrl = chunk[:16]
                for si in range(16):
                    if ctrl[si] == 0x80:
                        continue
                    slot = chunk[16 + si * 48:16 + (si + 1) * 48]
                    key_u32 = struct.unpack_from("<I", slot, 0)[0]
                    if not key_u32:
                        continue
                    c_arr = struct.unpack_from("<I", slot, 16)[0]
                    p_arr = struct.unpack_from("<Q", slot, 24)[0]
                    rb, ro = ptr(p_arr)
                    chans = {}
                    if c_arr and rb in ab:
                        adata = ab[rb][1]
                        for pi in range(c_arr):
                            if ro + (pi + 1) * 8 > len(adata):
                                break
                            pp = struct.unpack_from("<Q", adata, ro + pi * 8)[0]
                            pb, po = ptr(pp)
                            if pb in ab and ab[pb][0] == 0x6E384CC5:
                                pdata = ab[pb][1]
                                if po + 20 <= len(pdata):
                                    _, nh, fval = struct.unpack_from("<IIf", pdata, po + 8)
                                    nm = MORPH_HASH_TO_NAME.get(nh)
                                    if nm:
                                        chans[nm] = round(float(fval), 4)
                    if chans:
                        morph_db[key_u32] = chans
    return morph_db


def _closest_skin(rgb, palette):
    r, g, b = rgb
    best = min(palette, key=lambda x: (r - x[0][0]) ** 2 + (g - x[0][1]) ** 2 + (b - x[0][2]) ** 2)
    return best[1]


def resolve_skin_albedo(raw_hash, gender, part, var_rgb, base_rgb, tex_info, fallback_rgb=None):
    """Resolve a 0DRP hand/feet albedo hash to a verified clean local ttdb_peds texture hash + avgRGB."""
    palette = VERIFIED_CLEAN_HAND[gender] if part == "hand" else VERIFIED_CLEAN_FEET[gender]
    rgb = var_rgb.get(raw_hash) or base_rgb.get(raw_hash) or fallback_rgb
    # Direct hit in local textures.sqlite3 (e.g. Lucia, Jason, skeleton/special peds)
    if raw_hash and raw_hash in tex_info and "SRGB" in (tex_info[raw_hash][2] or ""):
        return raw_hash, rgb
    # Map via verified gender-matched clean skin palette using exact avgRGB from 0x1edee47e
    if rgb:
        return _closest_skin(rgb, palette), rgb
    return "", None


def load_archetypes():
    s, _ = sections_of(ARCHETYPES_PSO.read_bytes())
    d = Decoder(s, {})
    blk2 = d.blocks[1][1]
    out = []
    for idx in range(len(blk2) // 384):
        rec = blk2[idx * 384:(idx + 1) * 384]
        h8 = struct.unpack_from("<I", rec, 8)[0]
        h_short = struct.unpack_from("<I", rec, 0x174)[0]
        hscale = struct.unpack_from("<f", rec, 316)[0]
        if not (0.75 <= hscale <= 1.25):
            hscale = 1.0
        out.append((idx, h8, h_short, round(float(hscale), 4)))
    return out


def load_outfits():
    s, _ = sections_of(OUTFITS_PSO.read_bytes())
    d = Decoder(s, {})
    blks = {i + 1: (k, data) for i, (k, data, _) in enumerate(d.blocks)}
    b1 = blks[1][1]
    arch_to_outfits = {}
    outfit_to_arch = {}
    for i in range(len(b1) // 24):
        k, cnt, _cap, p = struct.unpack_from("<IxxxxIIQ", b1, i * 24)
        if not k or not cnt:
            continue
        tb, to = ptr(p)
        if tb not in blks:
            continue
        data = blks[tb][1]
        outs = []
        for j in range(cnt):
            if to + (j + 1) * 32 > len(data):
                break
            _slot_h, out_h = struct.unpack_from("<II", data, to + j * 32)
            if out_h:
                outs.append(out_h)
                outfit_to_arch[out_h] = k
        arch_to_outfits[k] = outs
    return arch_to_outfits, outfit_to_arch


def harvest_names(arch_set, short_set, all_models):
    found_arch = {}
    found_short = {}

    def _try(w):
        wl = w.lower()
        h = _name_hash(wl) & 0xFFFFFFFF
        if h in arch_set and h not in found_arch:
            found_arch[h] = wl
        if h in short_set and h not in found_short:
            found_short[h] = wl

    for nm in all_models:
        nl = nm.lower()
        _try(nl)
        for m in re.finditer(
            r"^(cs_[a-z0-9_]+?|hc_cs_[a-z0-9_]+?|hc_[a-z0-9_]+?|u_[mf]_[a-z0-9_]+?|sc_[a-z0-9_]+?|"
            r"player_story\d+_\d+|a_c_[a-z0-9_]+?|cr[abcfimr]_[a-z0-9_]+?)_"
            r"(?:head|body|legacy|hair|beard|brows|lashes|jacket|shirt|tshirt|pants|shorts|skirt|"
            r"dress|shoes|boots|hat|bra|undies|necklace|earring|watch|glasses|shades|belt|bag|"
            r"tanktop|hoodie|sweatshirt|vest|coat|fullbody|teeth|00)",
            nl,
        ):
            b = m.group(1)
            _try(b)
            if b.startswith("cs_"):
                _try("ig_" + b[3:])
                _try(b[3:])
            elif b.startswith("hc_cs_"):
                _try(b[3:])
                _try("ig_" + b[6:])
                _try(b[6:])

    for jp in (
        LIBRARY / "names.json",              # learned from the game's own files by the names stage (names.py)
        ENGINE / "script_symbol_names.json",
    ):
        if jp.is_file():
            for m in TOK_RE.finditer(jp.read_bytes()):
                raw = m.group(0)
                # The same filters the archive scan below applies: a token is tried when it is at most 36
                # characters long, or when it looks like a ped name (PED_PFX_RE).
                if len(raw) > 36 and not PED_PFX_RE.fullmatch(raw):
                    continue
                _try(raw.decode("ascii"))

    def scan_rpf(rel_rpf):
        rpf_p, sp_p = GAME / rel_rpf, INDEX / rel_rpf / "deflate-spans.bin"
        if not rpf_p.is_file() or not sp_p.is_file():
            return {}, {}
        spans = sp_p.read_bytes()
        rows = [struct.unpack_from("<QQQII", spans, i * 32) for i in range(len(spans) // 32)]
        la, ls = {}, {}
        with open(rpf_p, "rb") as f:
            for off, clen, ilen, _a, _b in rows:
                if ilen > 4_000_000:
                    continue
                f.seek(off)
                try:
                    buf = zlib.decompress(f.read(clen))
                except Exception:
                    continue
                for m in PED_PFX_RE.finditer(buf):
                    wl = m.group(0).decode("ascii").lower()
                    h = _name_hash(wl) & 0xFFFFFFFF
                    if h in arch_set:
                        la[h] = wl
                if len(buf) <= 262144:
                    for m in TOK_RE.finditer(buf):
                        raw = m.group(0)
                        if len(raw) > 36:
                            continue
                        wl = raw.decode("ascii").lower()
                        h = _name_hash(wl) & 0xFFFFFFFF
                        if h in arch_set:
                            la[h] = wl
                        if h in short_set:
                            ls[h] = wl
                        if "_" in wl:
                            for part in wl.split("_"):
                                if 3 <= len(part) <= 24:
                                    hp = _name_hash(part) & 0xFFFFFFFF
                                    if hp in short_set:
                                        ls[hp] = part
        return la, ls

    targets = [
        "common.rpf",
        "data.rpf",
        "pack/story000/story000.rpf",
        "pack/core_restofworld/core_restofworld.rpf",
    ]
    with ThreadPoolExecutor(max_workers=4) as ex:
        for la, ls in ex.map(scan_rpf, targets):
            for h, w in la.items():
                found_arch.setdefault(h, w)
            for h, w in ls.items():
                found_short.setdefault(h, w)

    for _sh, sw in list(found_short.items()):
        for pfx in (
            "", "cs_", "ig_", "hc_", "hc_cs_", "sc_",
            "u_f_y_", "u_f_m_", "u_f_o_", "u_m_y_", "u_m_m_", "u_m_o_",
            "a_f_y_", "a_f_m_", "a_f_o_", "a_m_y_", "a_m_m_", "a_m_o_",
            "s_f_y_", "s_f_m_", "s_m_y_", "s_m_m_", "g_m_y_", "g_m_m_", "g_f_y_",
        ):
            c = pfx + sw
            hc = _name_hash(c) & 0xFFFFFFFF
            if hc in arch_set and hc not in found_arch:
                found_arch[hc] = c

    return found_arch, found_short


def scan_all_0drps(outfit_to_arch, j32_models, m64_models, tex_info, morph_db):
    spans = PACKS_SPANS.read_bytes()
    srows = [struct.unpack_from("<QQQII", spans, i * 32) for i in range(len(spans) // 32)]
    n_s = len(srows)
    out_set = set(outfit_to_arch.keys())

    def worker(ci):
        lo, hi = ci * 4000, min(n_s, (ci + 1) * 4000)
        results = []
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
                rk, blks = parse_0drp_blocks(buf)
                if rk == CATALOG_ROOT and blks:
                    votes = collections.Counter()
                    for _bnum, (bk, bdata) in blks.items():
                        if bk in (0x37E52277, 0xF795E8BA):
                            continue
                        for o in range(0, len(bdata) - 3, 4):
                            v = struct.unpack_from("<I", bdata, o)[0]
                            if v in out_set:
                                votes[outfit_to_arch[v]] += 1
                    arch_h = votes.most_common(1)[0][0] if votes else 0

                    # Parse all presets with parent_id inheritance so hc_* / cs_* child presets resolve.
                    presets = []
                    by_id = {}
                    for _bnum, (bk, data) in blks.items():
                        if bk != 0xF795E8BA:
                            continue
                        for idx in range(len(data) // 88):
                            r = data[idx * 88:(idx + 1) * 88]
                            pid, parent_id = struct.unpack_from("<II", r, 0)
                            c1 = struct.unpack_from("<I", r, 8)[0]
                            p1 = struct.unpack_from("<Q", r, 16)[0]
                            cat = struct.unpack_from("<I", r, 24)[0]
                            p2 = struct.unpack_from("<Q", r, 48)[0]
                            c2 = struct.unpack_from("<H", r, 56)[0]
                            tags = read_tags(blks, p2, c2)
                            rb, ro = ptr(p1)
                            states = []
                            if c1 and rb in blks and blks[rb][0] == 0x37E52277:
                                rdata = blks[rb][1]
                                for si in range(c1):
                                    if ro + (si + 1) * 184 > len(rdata):
                                        break
                                    h0, alb, nrm, _ = struct.unpack_from("<QQQQ", rdata, ro + si * 184 + 8)
                                    sp2 = struct.unpack_from("<Q", rdata, ro + si * 184 + 168)[0]
                                    sc2 = struct.unpack_from("<H", rdata, ro + si * 184 + 176)[0]
                                    stags = read_tags(blks, sp2, sc2) if sc2 else {}
                                    states.append((h0, alb, nrm, stags))
                            ent = (pid, parent_id, cat, tags, states)
                            presets.append(ent)
                            by_id[pid] = ent

                    head_cands = []
                    eye_cands = []
                    teeth_cands = []
                    body_cands = []
                    clothing_cands = []
                    animal_cands = []
                    hand_skin_cands = []
                    feet_skin_cands = []
                    morphs = {}

                    parent_ids = {par for _, par, _, _, _ in presets if par}

                    def _first_valid_skin(sts):
                        a_out, n_out = "", ""
                        for _h0, alb, nrm, _stags in sts:
                            ah = f"{alb:016x}" if alb else ""
                            nh = f"{nrm:016x}" if nrm else ""
                            if ah not in IGNORE_SKIN_ALBEDOS and not a_out:
                                a_out = ah
                            if nh and nh != "0000000000000000" and not n_out:
                                n_out = nh
                        return a_out, n_out

                    for pid, parent_id, cat, tags, states in presets:
                        # 1. BodyShapePreset morph presets (0x6b3fb5a6 / 0x36313cc7 -> anim.rpf/000106153000.pso)
                        for mk_tag in (0x6B3FB5A6, 0x36313CC7):
                            for mv in tags.get(mk_tag, ()):
                                mdict = morph_db.get(mv)
                                if mdict:
                                    morphs.update(mdict)

                        # 2. Leaf-resolved upper (HAND_CATEGORY) & lower (FEET_CATEGORY) body skin presets
                        if cat in (HAND_CATEGORY, FEET_CATEGORY):
                            own_a, own_n = _first_valid_skin(states)
                            ra, rn = own_a, own_n
                            cur, depth = parent_id, 0
                            while cur and cur in by_id and depth < 6:
                                _, ppar, _, _, psts = by_id[cur]
                                pa_h, pn_h = _first_valid_skin(psts)
                                if not ra and pa_h:
                                    ra = pa_h
                                if not rn and pn_h:
                                    rn = pn_h
                                cur = ppar
                                depth += 1
                            if ra or rn:
                                is_leaf = 1 if pid not in parent_ids else 0
                                has_own = 1 if own_a else 0
                                rec_skin = (is_leaf, depth, has_own, ra, rn)
                                if cat == HAND_CATEGORY:
                                    hand_skin_cands.append(rec_skin)
                                else:
                                    feet_skin_cands.append(rec_skin)

                        dmodels = [m for tk in DRAWABLE_TAGS for v in tags.get(tk, ()) for m in j32_models.get(v, ())]
                        if not dmodels and parent_id:
                            cur, dep = parent_id, 0
                            while cur and cur in by_id and dep < 5:
                                _, ppar, _, ptags, _ = by_id[cur]
                                pm = [m for tk in DRAWABLE_TAGS for v in ptags.get(tk, ()) for m in j32_models.get(v, ())]
                                if pm:
                                    dmodels = pm
                                    break
                                cur = ppar
                                dep += 1
                        eye_inherited_alb = ""
                        if cat == EYES_CATEGORY:
                            cur_e, dep_e = pid, 0
                            while cur_e and cur_e in by_id and dep_e < 6 and not eye_inherited_alb:
                                _, epar, _, _, ests = by_id[cur_e]
                                for _eh0, ealb, _enrm, _estags in ests:
                                    cand_eye = resolve_eye_texture(ealb, tex_info)
                                    if cand_eye:
                                        eye_inherited_alb = cand_eye
                                        break
                                cur_e = epar
                                dep_e += 1
                        for h0, alb, nrm, stags in states:
                            smodels = list(dmodels)
                            for tk in DRAWABLE_TAGS:
                                for v in stags.get(tk, ()):
                                    smodels.extend(j32_models.get(v, ()))
                            if not smodels and h0 in m64_models:
                                smodels = list(m64_models[h0])
                            if not smodels:
                                continue
                            alb_h = f"{alb:016x}" if alb else ""
                            nrm_h = f"{nrm:016x}" if nrm else ""
                            ai = tex_info.get(alb_h)
                            ni = tex_info.get(nrm_h)
                            for nm in smodels:
                                nl = nm.lower()
                                if nl.startswith(("cr", "a_c_")) and "_fur_" not in nl:
                                    animal_cands.append(nm)
                                elif cat == HEAD_CATEGORY or (
                                    re.search(r"(?:^|_)head_(?:f|m)[a-z]{1,2}\d", nl)
                                    and not re.search(r"_(?:eyes|eyecap|earring|eyelashes|lashes|teeth)(?:_|$)", nl)
                                ):
                                    is_base = "_base_" in nl
                                    is_named = nl.startswith(("cs_", "hc_", "u_", "sc_", "player_"))
                                    a_px = (ai[0] * ai[1]) if ai else 0
                                    a_srgb = 1 if (ai and "SRGB" in ai[2]) else 0
                                    n_px = (ni[0] * ni[1]) if ni else 0
                                    score = (0 if is_base else 1, 1 if is_named else 0, a_srgb, a_px, n_px)
                                    head_cands.append((score, nm, alb_h if ai else "", nrm_h if ni else ""))
                                elif cat == EYES_CATEGORY or re.search(r"_eyes(?:_|$)", nl):
                                    valid_eye = resolve_eye_texture(alb_h, tex_info) or eye_inherited_alb
                                    eye_cands.append((1 if valid_eye else 0, nm, valid_eye))
                                elif cat == TEETH_CATEGORY or "teeth" in nl:
                                    teeth_cands.append(nm)
                                elif cat in SKIN_CATEGORIES or nl.startswith(("hand_", "feet_", "bodygenitals", "bodynipples")) or "_bodyupper" in nl or "_bodylower" in nl:
                                    body_cands.append(nm)
                                elif not any(nl.startswith(p) or f"_{p}" in nl for p in SKIP_CLOTHING_PREFIXES):
                                    clothing_cands.append((cat, nm))

                    results.append((
                        off, arch_h, head_cands, eye_cands, teeth_cands, body_cands,
                        clothing_cands, animal_cands, hand_skin_cands, feet_skin_cands, morphs,
                    ))
                i = j + 1
        return results

    all_res = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        for res in ex.map(worker, range((n_s + 3999) // 4000)):
            all_res.extend(res)
    return all_res


def classify_archetype_group(idx, name):
    nl = name.lower()
    if nl.startswith("player_story000_00"):
        return "Protagonists", "Lucia"
    if nl.startswith("player_story000_01"):
        return "Protagonists", "Jason"
    if nl.startswith("player_"):
        return "Protagonists", "Player wardrobe"
    if nl.startswith("cs_") or (960 <= idx <= 1379):
        return "Characters", "Story characters"
    if nl.startswith("u_"):
        return "Characters", "Unique characters"
    if nl.startswith("sc_"):
        return "Characters", "Special & creature-like characters"
    if nl.startswith(("cr", "a_c_")) or (614 <= idx <= 874):
        return "Animals", "Animals"
    return "Characters", "Ped archetypes (world & ambient)"


def fallback_name(idx, h8, short_name, head_name):
    if 960 <= idx <= 1379:
        pfx = "cs"
    elif 614 <= idx <= 874:
        pfx = "cr"
    elif idx < 614:
        pfx = "a_ped"
    elif 1410 <= idx <= 1500:
        pfx = "ex_ped"
    elif 1500 < idx <= 1650:
        pfx = "g_ped"
    elif 1900 <= idx <= 2018:
        pfx = "m_ped"
    elif 2021 <= idx <= 2550:
        pfx = "s_ped"
    else:
        pfx = "ped"
    if short_name and re.match(r"^[a-z][a-z0-9_]{1,24}$", short_name):
        return f"{pfx}_{short_name}_{h8:08x}"
    # Include rig/ethnicity hint for unnamed world/story archetypes so they are immediately legible
    if head_name:
        hm = re.search(r"((?:fr|mr|fb|mb|fo|mo|fc|mc|mi|mk)\d)_(cau|lat|afr|asi|gen|nat|base)", head_name.lower())
        if hm:
            return f"{pfx}_{hm.group(1)}_{hm.group(2)}_{h8:08x}"
    return f"{pfx}_{h8:08x}"


def main():
    inv = sqlite3.connect(INVENTORY_DB.as_uri() + "?mode=ro", uri=True)
    tx = sqlite3.connect(TEXTURES_DB.as_uri() + "?mode=ro", uri=True)
    tex_info = {
        h: (w, hh, fmt or "")
        for h, w, hh, fmt in tx.execute("select hash, width, height, format from texture")
    }
    all_models = [
        r[0]
        for r in inv.execute(
            "select distinct name from model where name is not null and status='indexed' "
            "and category in ('Protagonists','Characters','Clothing & accessories','Animals')"
        )
    ]
    j32_models = collections.defaultdict(list)
    m64_models = collections.defaultdict(list)
    for m in all_models:
        j32_models[_name_hash(m) & 0xFFFFFFFF].append(m)
        m64_models[murmur64(m.encode())].append(m)

    hand_var = load_var_pso_rgb(HAND_VAR_PSO)
    feet_var = load_var_pso_rgb(FEET_VAR_PSO)
    face_var = load_var_pso_rgb(FACE_VAR_PSO)
    base_var = load_var_pso_rgb(BASE_VAR_PSO)
    morph_db = load_bodyshape_morphs()

    archetypes = load_archetypes()
    arch_set = {h8 for _, h8, _, _ in archetypes}
    short_set = {hs for _, _, hs, _ in archetypes if hs}
    arch_to_outfits, outfit_to_arch = load_outfits()
    found_arch, found_short = harvest_names(arch_set, short_set, all_models)
    drp_rows = scan_all_0drps(outfit_to_arch, j32_models, m64_models, tex_info, morph_db)

    by_arch = {}
    for (
        off, arch_h, head_cands, eye_cands, teeth_cands, body_cands,
        clothing_cands, animal_cands, hand_skin_cands, feet_skin_cands, morphs,
    ) in drp_rows:
        if not arch_h:
            continue
        # Try cracking unnamed arch_h from bespoke drawable prefixes inside its own 0DRP!
        if arch_h not in found_arch:
            for _, nm, _, _ in head_cands:
                m = re.match(r"^(?:hc_)?(cs_[a-z0-9_]+?|u_[mf]_[a-z0-9_]+?|sc_[a-z0-9_]+?)_head_", nm.lower())
                if m:
                    cand = m.group(1)
                    if (_name_hash(cand) & 0xFFFFFFFF) == arch_h:
                        found_arch[arch_h] = cand
                        break
        by_arch[arch_h] = (
            off, head_cands, eye_cands, teeth_cands, body_cands,
            clothing_cands, animal_cands, hand_skin_cands, feet_skin_cands, morphs,
        )

    def _pick_skin_cand(cands, var_rgb, face_rgb):
        """Pick the best leaf skin candidate; break multi-leaf ties (e.g. Lucia/Jason) by face avgRGB proximity."""
        if not cands:
            return "", ""
        def _key(c):
            is_leaf, depth, has_own, ra, rn = c
            rgb = var_rgb.get(ra) or base_var.get(ra)
            in_db = 1 if (ra and ra in tex_info and "SRGB" in (tex_info[ra][2] or "")) else 0
            # Negative squared color distance to face_rgb (higher = closer match!)
            f_prox = -((rgb[0] - face_rgb[0]) ** 2 + (rgb[1] - face_rgb[1]) ** 2 + (rgb[2] - face_rgb[2]) ** 2) if (face_rgb and rgb) else 0
            # Prefer leaf & deeper override first; when multiple leaves exist at same level, face proximity + in_db breaks ties
            return (is_leaf, depth, f_prox if face_rgb else 0, in_db, has_own)
        best = max(cands, key=_key)
        return best[3], best[4]

    rows_out = []
    stats = collections.Counter()
    for idx, h8, h_short, hscale in archetypes:
        drp = by_arch.get(h8)
        drp_off = drp[0] if drp else 0
        head_name, face_alb, face_nrm = "", "", ""
        eye_name, eye_tex = "", ""
        teeth_name = ""
        body_rig = ""
        body_meshes = []
        clothing_meshes = []
        is_animal = False
        upper_alb, upper_nrm = "", ""
        lower_alb, lower_nrm = "", ""
        genital_alb = ""
        skin_rgb_str = ""
        morphs_out = {}

        if drp:
            (
                _, head_cands, eye_cands, teeth_cands, body_cands,
                clothing_cands, animal_cands, hand_skin_cands, feet_skin_cands, morphs,
            ) = drp
            if head_cands:
                head_cands.sort(key=lambda x: x[0], reverse=True)
                _, head_name, face_alb, face_nrm = head_cands[0]
                # If best head lacked normal or albedo, borrow from another state of the same head in this 0DRP
                for _, hn, ha, hnr in head_cands:
                    if hn == head_name:
                        if not face_alb and ha:
                            face_alb = ha
                        if not face_nrm and hnr:
                            face_nrm = hnr
            if eye_cands:
                eye_cands.sort(key=lambda x: (x[0], 1 if head_name and x[1].lower().startswith(head_name.lower().removesuffix("_full")) else 0), reverse=True)
                _, eye_name, eye_tex = eye_cands[0]
            if teeth_cands:
                teeth_name = teeth_cands[0]
            # Detect body rig from head, eyes, or body parts
            for probe in [head_name, eye_name] + body_cands + [c[1] for c in clothing_cands]:
                if not probe:
                    continue
                rm = RIG_RE.search(probe.lower())
                if rm:
                    body_rig = rm.group(1)
                    break
            # Keep bespoke full-body meshes (e.g. sc_alien_bodyupper/lower, cs_farmm_bodyupper/lower)
            bespoke_bodies = sorted({b for b in body_cands if "_bodyupper" in b.lower() or "_bodylower" in b.lower()})
            body_meshes = bespoke_bodies
            # Deduplicate clothing per category hash (1 representative garment per slot, capped at 12)
            seen_cat = set()
            for c_cat, c_nm in clothing_cands:
                if c_cat not in seen_cat:
                    seen_cat.add(c_cat)
                    clothing_meshes.append(c_nm)
                    if len(clothing_meshes) >= 12:
                        break
            if not head_name and animal_cands:
                is_animal = True
                # Pick animal body drawable
                body_pick = next((a for a in animal_cands if "_body_" in a.lower()), animal_cands[0])
                head_name = body_pick

            if not is_animal and body_rig:
                gender = "F" if body_rig.startswith("f") else "M"
                face_rgb = face_var.get(face_alb) or base_var.get(face_alb)
                raw_ha, raw_hn = _pick_skin_cand(hand_skin_cands, hand_var, face_rgb)
                raw_fa, raw_fn = _pick_skin_cand(feet_skin_cands, feet_var, face_rgb)
                upper_alb, up_rgb = resolve_skin_albedo(raw_ha, gender, "hand", hand_var, base_var, tex_info, fallback_rgb=face_rgb)
                lower_alb, lo_rgb = resolve_skin_albedo(raw_fa, gender, "feet", feet_var, base_var, tex_info, fallback_rgb=up_rgb or face_rgb)
                if raw_hn and raw_hn in tex_info:
                    upper_nrm = raw_hn
                if raw_fn and raw_fn in tex_info:
                    lower_nrm = raw_fn
                ref_rgb = up_rgb or lo_rgb or face_rgb
                if ref_rgb:
                    skin_rgb_str = f"{ref_rgb[0]},{ref_rgb[1]},{ref_rgb[2]}"
                    if gender == "M":
                        genital_alb = _closest_skin(ref_rgb, MALE_GENITAL_SKINS)
                if morphs:
                    # Drop near-zero channels to keep JSON compact
                    morphs_out = {k: v for k, v in sorted(morphs.items()) if abs(v) > 1e-4}

        resolved_name = found_arch.get(h8, "")
        short_name = found_short.get(h_short, "")
        name_is_exact = 1 if resolved_name else 0
        display_name = resolved_name or fallback_name(idx, h8, short_name, head_name)
        cat, sub = classify_archetype_group(idx, display_name)
        if is_animal:
            cat, sub = "Animals", "Animals"
        n_outfits = len(arch_to_outfits.get(h8, ()))

        rows_out.append((
            idx, h8, f"{h8:08x}", display_name, name_is_exact, short_name,
            cat, sub, drp_off, n_outfits,
            head_name, face_alb, face_nrm, eye_name, eye_tex, teeth_name,
            body_rig, json.dumps(body_meshes), json.dumps(clothing_meshes),
            upper_alb, upper_nrm, lower_alb, lower_nrm, genital_alb, skin_rgb_str,
            json.dumps(morphs_out) if morphs_out else "", hscale,
        ))
        stats[(cat, sub, bool(head_name))] += 1

    tmp = OUT_DB.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    db = sqlite3.connect(tmp)
    db.executescript("""
        create table ped_archetype(
            idx integer primary key,
            arch_hash integer unique,
            arch_hex text,
            name text,
            name_exact integer,
            short_name text,
            category text,
            subcategory text,
            drp_offset integer,
            outfit_count integer,
            head_drawable text,
            face_albedo text,
            face_normal text,
            eye_drawable text,
            eye_texture text,
            teeth_drawable text,
            body_rig text,
            body_drawables text,
            clothing_drawables text,
            upper_skin_albedo text,
            upper_skin_normal text,
            lower_skin_albedo text,
            lower_skin_normal text,
            genital_skin_albedo text,
            skin_rgb text,
            body_morphs text,
            height_scale real
        );
        create index idx_pa_name on ped_archetype(name);
        create index idx_pa_head on ped_archetype(head_drawable);
    """)
    db.executemany(
        "insert into ped_archetype values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        rows_out,
    )
    db.commit()
    db.close()
    os.replace(tmp, OUT_DB)

    exact_cnt = sum(r[4] for r in rows_out)
    with_head = sum(1 for r in rows_out if r[10])
    with_skin = sum(1 for r in rows_out if r[19] or r[21])
    with_morph = sum(1 for r in rows_out if r[25])
    print(
        f"Wrote {len(rows_out):,} ped archetypes -> {OUT_DB.name} | exact names: {exact_cnt:,} "
        f"| with 3D head/body: {with_head:,} | with body skin: {with_skin:,} | with body morphs: {with_morph:,}"
    )
    for k, v in sorted(stats.items()):
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
