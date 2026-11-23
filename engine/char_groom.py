# SPDX-License-Identifier: GPL-3.0-or-later
"""RAGE 9 Character Hair, Eyebrows, Eyelashes, Beards, Body Hair & Strand Groom engine.

Standalone backend module for Lineup:
  - Verified GTA VI hair color palette extracted from `packs.rpf/00011dab3000.pso`
    (paired tip/root palette `0x1e64f584` + master palette `0xa7c8d8e0` and variants).
  - Extracts each ped's default and alternate hair/brows/lashes/beard/bodyhair presets
    and hair color from its `0DRP` ped catalogue (`0xf795e8ba` presets, `0x37e52277`
    states, `0xa9d57751` outfits, `0x67e54cb2` outfit-item pairs).
  - Resolves `0DRP` groom drawables to both `grooms.sqlite3` `.grm` strand assets
    (exact `name`, multi-groom `likely`/`grp` runs, alphabetical `hint` block ranges,
    and prefix/style/rig fallbacks) and polygonal hair/lash/accessory meshes in
    `inventory.sqlite3`.
  - Builds per-rig groom & color catalogs (`groom_catalog_for_rig`) for interactive
    character customization.
  - Decodes single or multi-groom `.grm` strand assets (`decode_groom_strands_for_model`)
    into compact, cached WebGL-ready buffers aligned to character standing skeleton
    Y-up space (`[x, z + 1.0, -y]`, with optional `head_box` anchoring).
"""

from __future__ import annotations

import base64
import collections
import functools
import hashlib
import json
import math
import re
import sqlite3
import struct
import threading
import zlib
from contextlib import closing
from pathlib import Path
from typing import Any, Sequence

import numpy as np


import face_presets as fp  # noqa: E402
import smh_gen  # noqa: E402
import smh_groom  # noqa: E402
import smh_style  # noqa: E402
from decode_pso import Decoder, sections_of  # noqa: E402
from paths import INDEX, LIBRARY, CACHE

GROOMS_DB = LIBRARY / "grooms.sqlite3"
INVENTORY_DB = LIBRARY / "inventory.sqlite3"
PEDS_DB = LIBRARY / "ped_archetypes.sqlite3"
PALETTE_PSO = INDEX / "packs.rpf" / "00011dab3000.pso"
STRAND_CACHE_DIR = CACHE / "char_grooms"

# ---------------------------------------------------------------------------
# 1. Verified RAGE 9 Hair Color Palettes (from 00011dab3000.pso)
# ---------------------------------------------------------------------------

PALETTE_HASH_HAIR_PAIRED = 0x1E64F584  # 128 tip/primary [0..127] + 128 paired root/shadow [128..255]
PALETTE_HASH_MASTER = 0xA7C8D8E0       # 256-entry master hair/brow/lash/beard palette
PALETTE_HASH_MUTED = 0xC64CF5B1        # desaturated companion of 0xa7c8d8e0
PALETTE_HASH_VIBRANT = 0x385E3F0D      # richer companion of 0xa7c8d8e0
PALETTE_HASH_EMPTY = 0xFCEF23E3        # empty / baked-color marker

# Curated 48-color hair palette. The curation (id, label, group, melanin, redness, dye) is this project's; the
# colors themselves are read from your copy: palette 0x1e64f584 of INDEX/packs.rpf/00011dab3000.pso, where
# `tip = pal[idx]` and `root = pal[idx + 128]`. Without that member a neutral grey ramp stands in (see
# `hair_color_palette()`), and the API says so.
_PALETTE_SPECS = [
    # id, label, group, idx_1e64f584, melanin, redness, dye
    ("jet_black", "Jet Black", "Black", 8, 0.98, 0.04, False),
    ("natural_black", "Natural Black", "Black", 9, 0.94, 0.02, False),
    ("soft_black", "Soft Black", "Black", 10, 0.90, 0.06, False),
    ("espresso_black", "Espresso Black", "Black", 22, 0.92, 0.14, False),

    ("deep_espresso", "Deep Espresso", "Brown", 11, 0.84, 0.12, False),
    ("dark_ash_brown", "Dark Ash Brown", "Brown", 13, 0.80, 0.10, False),
    ("dark_chocolate", "Dark Chocolate", "Brown", 24, 0.78, 0.22, False),
    ("rich_brunette", "Rich Brunette", "Brown", 26, 0.68, 0.26, False),
    ("chestnut_brown", "Chestnut Brown", "Brown", 38, 0.66, 0.30, False),
    ("warm_brunette", "Warm Brunette", "Brown", 28, 0.58, 0.28, False),
    ("medium_brown", "Medium Brown", "Brown", 44, 0.54, 0.30, False),
    ("golden_brown", "Golden Brown", "Brown", 46, 0.48, 0.32, False),
    ("light_caramel", "Light Caramel", "Brown", 52, 0.38, 0.28, False),

    ("dirty_blonde", "Dirty Blonde", "Blonde", 77, 0.42, 0.26, False),
    ("ash_blonde", "Ash Blonde", "Blonde", 70, 0.30, 0.18, False),
    ("sandy_blonde", "Sandy Blonde", "Blonde", 56, 0.28, 0.26, False),
    ("honey_blonde", "Honey Blonde", "Blonde", 79, 0.24, 0.26, False),
    ("warm_honey", "Warm Honey", "Blonde", 75, 0.22, 0.32, False),
    ("sun_kissed_blonde", "Sun-Kissed Blonde", "Blonde", 80, 0.16, 0.26, False),
    ("golden_blonde", "Golden Blonde", "Blonde", 73, 0.12, 0.30, False),
    ("champagne_blonde", "Champagne Blonde", "Blonde", 60, 0.18, 0.22, False),
    ("ash_platinum", "Ash Platinum", "Blonde", 64, 0.14, 0.08, False),
    ("platinum_blonde", "Platinum Blonde", "Blonde", 62, 0.08, 0.10, False),
    ("bleached_blonde", "Bleached Blonde", "Blonde", 63, 0.04, 0.06, False),

    ("deep_mahogany", "Deep Mahogany", "Red & Auburn", 88, 0.70, 0.56, False),
    ("dark_auburn", "Dark Auburn", "Red & Auburn", 89, 0.66, 0.68, False),
    ("rich_auburn", "Rich Auburn", "Red & Auburn", 92, 0.54, 0.80, False),
    ("copper_auburn", "Copper Auburn", "Red & Auburn", 95, 0.42, 0.64, False),
    ("natural_ginger", "Natural Ginger", "Red & Auburn", 97, 0.44, 0.88, False),
    ("strawberry_copper", "Strawberry Copper", "Red & Auburn", 103, 0.36, 0.62, False),
    ("golden_amber", "Golden Amber", "Red & Auburn", 85, 0.38, 0.54, False),
    ("strawberry_blonde", "Strawberry Blonde", "Red & Auburn", 83, 0.24, 0.52, False),

    ("charcoal_gray", "Charcoal Gray", "Gray & White", 6, 0.60, 0.00, False),
    ("salt_and_pepper", "Salt & Pepper", "Gray & White", 4, 0.42, 0.00, False),
    ("steel_gray", "Steel Gray", "Gray & White", 3, 0.28, 0.00, False),
    ("silver_gray", "Silver", "Gray & White", 1, 0.12, 0.00, False),
    ("snow_white", "Snow White", "Gray & White", 0, 0.02, 0.00, False),

    ("crimson_red", "Crimson Red", "Dyed", 126, 0.50, 0.95, True),
    ("scarlet_red", "Scarlet Red", "Dyed", 125, 0.36, 0.98, True),
    ("flame_orange", "Flame Orange", "Dyed", 122, 0.24, 0.95, True),
    ("rose_pink", "Rose Pink", "Dyed", 108, 0.20, 0.60, True),
    ("neon_magenta", "Neon Magenta", "Dyed", 106, 0.22, 0.50, True),
    ("deep_plum", "Deep Plum", "Dyed", 105, 0.65, 0.35, True),
    ("twilight_violet", "Twilight Violet", "Dyed", 104, 0.68, 0.20, True),
    ("midnight_blue", "Midnight Blue", "Dyed", 111, 0.72, 0.05, True),
    ("ocean_teal", "Ocean Teal", "Dyed", 109, 0.52, 0.05, True),
    ("emerald_green", "Emerald Green", "Dyed", 112, 0.52, 0.05, True),
    ("neon_lime", "Neon Lime", "Dyed", 116, 0.42, 0.15, True),
]


def _rgb_hex(rgb: Sequence[int]) -> str:
    return f"#{int(rgb[0]):02x}{int(rgb[1]):02x}{int(rgb[2]):02x}"


def _fallback_rgb(melanin: float) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    """Neutral grey tip and root for a preset when the game's palette is not available (darker with more melanin)."""
    tip = int(round(255 * (1.0 - float(melanin))))
    root = int(round(tip * 0.7))
    return (tip, tip, tip), (root, root, root)


@functools.lru_cache(maxsize=1)
def _hair_palette_state() -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], bool]:
    cols = load_rage_palettes().get(PALETTE_HASH_HAIR_PAIRED) or []
    from_game = len(cols) >= 256
    palette = []
    for cid, label, group, idx, melanin, redness, dye in _PALETTE_SPECS:
        if from_game:
            tip_rgb, root_rgb = tuple(cols[idx]), tuple(cols[idx + 128])
        else:
            tip_rgb, root_rgb = _fallback_rgb(melanin)
        palette.append({
            "id": cid,
            "label": label,
            "group": group,
            "paletteHash": f"0x{PALETTE_HASH_HAIR_PAIRED:08x}",
            "paletteIndex": idx,
            "rootRgb": list(root_rgb),
            "tipRgb": list(tip_rgb),
            "rootHex": _rgb_hex(root_rgb),
            "tipHex": _rgb_hex(tip_rgb),
            "melanin": melanin,
            "redness": redness,
            "dye": dye,
            "colorSource": "game palette" if from_game else "fallback grey ramp",
        })
    return palette, {c["id"]: c for c in palette}, from_game


def hair_color_palette() -> list[dict[str, Any]]:
    """The 48 presets with tip/root colors from the user's palette (grey ramp when it is missing)."""
    return _hair_palette_state()[0]


def hair_color_by_id() -> dict[str, dict[str, Any]]:
    return _hair_palette_state()[1]


def hair_colors_from_game() -> bool:
    return _hair_palette_state()[2]


def __getattr__(name: str):
    """HAIR_COLOR_PALETTE and HAIR_COLOR_BY_ID stay importable; they are built on first use."""
    if name == "HAIR_COLOR_PALETTE":
        return hair_color_palette()
    if name == "HAIR_COLOR_BY_ID":
        return hair_color_by_id()
    raise AttributeError(name)

# Verified story / protagonist character default hair color IDs (for characters whose
# 0DRP hair presets leave palette indices at 0/255 runtime-inherit).
STORY_CHARACTER_HAIR_COLORS: dict[str, str] = {
    "cs_mandy": "honey_blonde",
    "player_story000_00": "dark_chocolate",       # Lucia
    "player_story000_01": "golden_brown",         # Jason
    "cs_boobie_ike": "jet_black",
    "cs_ali": "soft_black",
    "cs_alicia": "deep_espresso",
    "cs_brian_heder": "salt_and_pepper",
    "cs_cal": "rich_brunette",
    "cs_raul_bautista": "dark_ash_brown",
    "cs_dre_quan": "jet_black",
    "cs_bae_luxe": "deep_espresso",
    "cs_roxy": "chestnut_brown",
    "cs_lori_heder": "honey_blonde",
    "cs_dimez": "jet_black",
    "cs_real_dimez": "jet_black",
}

# ---------------------------------------------------------------------------
# 2. Verified RAGE 9 0DRP Groom Category Hashes (JOAAT32)
# ---------------------------------------------------------------------------

GROOM_CATEGORY_HASHES: dict[int, tuple[str, str]] = {
    # hash -> (canonical_slot, sub_slot)
    0x864B03AE: ("hair", "hair"),
    0x50FA3613: ("hair", "wigs"),
    0x37FFDDAF: ("hair", "legacyhair"),
    0x7C50AD43: ("brows", "brows"),
    0x0242A48F: ("brows", "brows"),
    0x83A664E7: ("lashes", "lashes"),
    0x637BC818: ("lashes", "lashes"),
    0x44D2709F: ("beard", "beard"),
    0x1A19EE85: ("beard", "mustache"),
    0xF693CF81: ("beard", "chops"),
    0x129C4311: ("beard", "stubble"),
    0xA228CC57: ("bodyhair", "bodyhair"),
    0x775184B7: ("bodyhair", "chesthair"),
    0x904BB7CE: ("bodyhair", "leghair"),
    0x13CF4B00: ("bodyhair", "armpithair"),
    0x8C8F5410: ("bodyhair", "genitalhair"),
    0x5438003C: ("accessory", "hairaccessories"),
}

GROOM_SLOTS = ("hair", "brows", "lashes", "beard", "bodyhair")

# Expected standing RAGE Z-up (pelvis-relative) head height centers by rig
_RIG_HEAD_Z = {
    "fr1": 0.62,
    "mr1": 0.72,
    "fc1": 0.45,
    "mc1": 0.45,
    "mk1": 0.35,
    "mi1": 0.25,
}

# Expected strand length (meters) for length token l01..l09
_LENGTH_METERS = {
    "l01": 0.015,
    "l02": 0.035,
    "l03": 0.065,
    "l04": 0.100,
    "l05": 0.145,
    "l06": 0.200,
    "l07": 0.260,
    "l08": 0.330,
    "l09": 0.420,
}

# ---------------------------------------------------------------------------
# 3. Lazy Cached Indices (Palettes, Inventory, Grooms, 0DRP Spans)
# ---------------------------------------------------------------------------

_LOCK = threading.RLock()
_PALETTES_CACHE: dict[int, list[tuple[int, int, int]]] | None = None
_INV_CACHE: dict[str, Any] | None = None
_GROOM_INDEX_CACHE: dict[str, Any] | None = None
_RIG_CATALOG_CACHE: dict[str, dict[str, Any]] = {}
_SPANS_CACHE: tuple[list[tuple[int, int, int, int, int]], dict[int, int]] | None = None
_STRAND_MEM_CACHE: collections.OrderedDict[str, dict[str, Any]] = collections.OrderedDict()
_STRAND_MEM_MAX = 48


def load_rage_palettes() -> dict[int, list[tuple[int, int, int]]]:
    """Load all 22 RGBA8 color palettes from `00011dab3000.pso` (cached)."""
    global _PALETTES_CACHE
    with _LOCK:
        if _PALETTES_CACHE is not None:
            return _PALETTES_CACHE
        pals: dict[int, list[tuple[int, int, int]]] = {}
        if PALETTE_PSO.exists():
            raw = PALETTE_PSO.read_bytes()
            dec = Decoder(sections_of(raw)[0], {})
            if len(dec.blocks) >= 10:
                colors_blk = dec.blocks[8][1]
                meta_blk = dec.blocks[9][1]
                for i in range(len(meta_blk) // 24):
                    ph, _u4, ptr, cnt, _cap = struct.unpack_from("<IIQHH", meta_blk, i * 24)
                    # PSO block pointer stores byte offset in upper bits (ptr >> 20) for 256*4 steps
                    bo = ((ptr & 0xFFFFFFFF) >> 20) if (ptr & 0xFFFFF) < 0x1000 else ((ptr >> 12) & 0xFFFFFFFF)
                    cols: list[tuple[int, int, int]] = []
                    if ptr and cnt and bo + cnt * 4 <= len(colors_blk):
                        for j in range(cnt):
                            r, g, b, _a = struct.unpack_from("<BBBB", colors_blk, bo + j * 4)
                            cols.append((r, g, b))
                    pals[ph] = cols
        _PALETTES_CACHE = pals
        return pals


def nearest_hair_color_preset(rgb: Sequence[int]) -> dict[str, Any]:
    """Find the closest preset in `HAIR_COLOR_PALETTE` to an RGB triple."""
    r0, g0, b0 = float(rgb[0]), float(rgb[1]), float(rgb[2])
    palette = hair_color_palette()
    best = palette[0]
    best_d = 1e18
    for p in palette:
        tr, tg, tb = p["tipRgb"]
        # Perceptual redmean-weighted Euclidean distance
        rm = 0.5 * (r0 + tr)
        dr, dg, db = r0 - tr, g0 - tg, b0 - tb
        d = (2.0 + rm / 256.0) * dr * dr + 4.0 * dg * dg + (2.0 + (255.0 - rm) / 256.0) * db * db
        if d < best_d:
            best_d = d
            best = p
    return best


def resolve_palette_color(
    palette_hash: int,
    idx0: int,
    idx1: int = 255,
    idx2: int = 255,
) -> dict[str, Any] | None:
    """Resolve a RAGE 9 `(palette_hash, b0, b1, b2)` tuple to exact tip/root RGBs."""
    if not palette_hash or palette_hash == PALETTE_HASH_EMPTY:
        return None
    if idx0 in (0, 255) and idx1 in (0, 255) and idx2 in (0, 255):
        return None
    pals = load_rage_palettes()
    cols = pals.get(palette_hash)
    if not cols:
        return None

    def _pick(ix: int) -> tuple[int, int, int] | None:
        if 0 <= ix < len(cols) and ix != 255:
            return cols[ix]
        return None

    c0 = _pick(idx0) or _pick(idx1) or _pick(idx2)
    if c0 is None:
        return None

    if palette_hash == PALETTE_HASH_HAIR_PAIRED:
        # Palette 0x1e64f584 pairs tip=cols[i] with root=cols[i+128] for i < 128.
        tip = c0
        if idx0 < 128 and idx0 + 128 < len(cols):
            root = cols[idx0 + 128]
        else:
            root = tuple(max(0, int(v * 0.68)) for v in tip)
        accent = _pick(idx1) if (idx1 not in (idx0, 255)) else None
        if accent and idx1 != idx0:
            # Multi-tone dyed ped hair (e.g. b0=primary, b1=tip/highlight)
            root = tip if idx0 >= 128 else cols[idx0 + 128]
            tip = accent
    else:
        tip = c0
        c1 = _pick(idx1) if idx1 not in (idx0, 255) else None
        if c1:
            root = c0
            tip = c1
        else:
            root = tuple(max(0, int(v * 0.68)) for v in tip)

    nearest = nearest_hair_color_preset(tip)
    return {
        "id": nearest["id"],
        "label": nearest["label"],
        "group": nearest["group"],
        "paletteHash": f"0x{palette_hash:08x}",
        "paletteIndex": int(idx0),
        "secondaryIndex": int(idx1) if idx1 != 255 else None,
        "accentIndex": int(idx2) if idx2 != 255 else None,
        "rootRgb": list(root),
        "tipRgb": list(tip),
        "rootHex": _rgb_hex(root),
        "tipHex": _rgb_hex(tip),
        "melanin": nearest["melanin"],
        "redness": nearest["redness"],
        "dye": nearest["dye"],
        "source": "0drp_palette",
    }


def infer_default_hair_color(
    ped_name: str = "",
    head_drawable: str = "",
    skin_rgb: str | Sequence[int] | None = None,
    drp_colors: Sequence[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Determine the best default hair color for a character.

    Priority:
      1. Explicit non-zero/non-255 RAGE 9 palette color from the ped's `0DRP`.
      2. Curated verified story/protagonist character hair color (`STORY_CHARACTER_HAIR_COLORS`).
      3. Head archetype age/ethnicity (`_o_`, `_afr_`, `_asi_`, `_lat_`, `_cau_`) + `skin_rgb`.
    """
    if drp_colors:
        # Prefer natural-looking primary hair palette color if present
        c = dict(drp_colors[0])
        return c

    pn = (ped_name or "").lower().strip()
    if pn in STORY_CHARACTER_HAIR_COLORS:
        cid = STORY_CHARACTER_HAIR_COLORS[pn]
        out = dict(hair_color_by_id()[cid])
        out["source"] = "story_default"
        return out

    hd = (head_drawable or "").lower()
    # Parse age token: _o_ (elder/old), _m_ (middle), _y_ (young)
    is_old = bool(re.search(r"_o(?:_|$)", hd)) or "_o_" in pn or pn.startswith(("a_f_o_", "a_m_o_", "u_f_o_", "u_m_o_"))
    eth = ""
    for e in ("afr", "asi", "lat", "cau", "ind", "mid", "mix"):
        if f"_{e}_" in hd or f"_{e}_" in pn:
            eth = e
            break

    # Deterministic stable selector from ped/head name so each ambient ped is consistent
    seed_bytes = hashlib.md5((pn or hd or "default").encode("utf-8")).digest()
    pick = seed_bytes[0]

    # Parse skin luminance if available
    skin_lum = 140.0
    if isinstance(skin_rgb, str) and "," in skin_rgb:
        try:
            sr, sg, sb = (int(x.strip()) for x in skin_rgb.split(",")[:3])
            skin_lum = 0.299 * sr + 0.587 * sg + 0.114 * sb
        except Exception:
            pass
    elif isinstance(skin_rgb, (list, tuple)) and len(skin_rgb) >= 3:
        skin_lum = 0.299 * float(skin_rgb[0]) + 0.587 * float(skin_rgb[1]) + 0.114 * float(skin_rgb[2])

    if is_old:
        pool = ["salt_and_pepper", "steel_gray", "silver_gray", "charcoal_gray", "snow_white"]
        cid = pool[pick % len(pool)]
    elif eth == "afr" or skin_lum < 95:
        pool = ["jet_black", "natural_black", "soft_black", "espresso_black"]
        cid = pool[pick % len(pool)]
    elif eth == "asi":
        pool = ["jet_black", "soft_black", "espresso_black", "deep_espresso"]
        cid = pool[pick % len(pool)]
    elif eth == "lat":
        pool = ["deep_espresso", "dark_chocolate", "soft_black", "rich_brunette", "dark_ash_brown"]
        cid = pool[pick % len(pool)]
    elif eth == "cau" or skin_lum >= 132:
        pool = [
            "dark_chocolate", "rich_brunette", "chestnut_brown", "golden_brown",
            "dirty_blonde", "honey_blonde", "sandy_blonde", "warm_brunette",
            "copper_auburn", "dark_auburn", "ash_blonde",
        ]
        cid = pool[pick % len(pool)]
    else:
        pool = ["dark_chocolate", "deep_espresso", "rich_brunette", "soft_black", "chestnut_brown"]
        cid = pool[pick % len(pool)]

    out = dict(hair_color_by_id()[cid])
    out["source"] = "archetype_inferred"
    return out


# ---------------------------------------------------------------------------
# 4. Drawable Name Parsing & Human-Readable Labels
# ---------------------------------------------------------------------------

_STEM_RE = re.compile(r"^(.+?_(?:fr1|mr1|mk1|mi1|fc1|mc1)_\d{3})(?:_|$)", re.I)
_RIG_RE = re.compile(r"_(fr1|mr1|mk1|mi1|fc1|mc1)_", re.I)
_LEN_RE = re.compile(r"_(l0[1-9])(?:_|$)", re.I)
_TEX_RE = re.compile(r"_(straight|wavy|curly|coily|braided|locs|dreads|bald)(?:_|$)", re.I)

_STYLE_TOKEN_RE = re.compile(
    r"(?:^|_)(hair[a-z0-9]+|brows[a-z0-9]+|lashes[a-z0-9]+|beard[a-z0-9]+|mustache[a-z0-9]+|"
    r"stubble[a-z0-9]+|chops[a-z0-9]+|chingoatee[a-z0-9]*|bodyhair[a-z0-9]+|chesthair[a-z0-9]+|"
    r"leghair[a-z0-9]+|armpithair[a-z0-9]+|genitalhair[a-z0-9]+|legacyhair[a-z0-9]*|wigs?[a-z0-9]*)"
    r"_(fr1|mr1|mk1|mi1|fc1|mc1)_(\d{3})",
    re.I,
)

_GROOM_PREFIX_TO_SLOT = (
    ("hairacc", "accessory"),
    ("hair", "hair"),
    ("legacyhair", "hair"),
    ("wig", "hair"),
    ("brows", "brows"),
    ("eyebrow", "brows"),
    ("lashes", "lashes"),
    ("eyelash", "lashes"),
    ("beard", "beard"),
    ("mustache", "beard"),
    ("stubble", "beard"),
    ("chops", "beard"),
    ("chingoatee", "beard"),
    ("bodyhair", "bodyhair"),
    ("chesthair", "bodyhair"),
    ("leghair", "bodyhair"),
    ("armpithair", "bodyhair"),
    ("genitalhair", "bodyhair"),
)

_STYLE_PRETTY = {
    "hairafro": "Afro",
    "hairbob": "Bob",
    "hairbraids": "Braids",
    "hairbun": "Bun",
    "hairbuzzcut": "Buzz Cut",
    "haircaesar": "Caesar",
    "haircornrows": "Cornrows",
    "haircrewcut": "Crew Cut",
    "haircrop": "Crop",
    "haircurls": "Curls",
    "hairdreadlocks": "Dreadlocks",
    "hairdutchbraid": "Dutch Braid",
    "hairfade": "Fade",
    "hairfingerwave": "Finger Wave",
    "hairflattop": "Flat Top",
    "hairflow": "Flow",
    "hairfohawk": "Faux Hawk",
    "hairfrenchbraid": "French Braid",
    "hairhalfbun": "Half Bun",
    "hairhighfade": "High Fade",
    "hairlob": "Lob",
    "hairlong": "Long",
    "hairlowfade": "Low Fade",
    "hairmidfade": "Mid Fade",
    "hairmohawk": "Mohawk",
    "hairmullet": "Mullet",
    "hairpixie": "Pixie",
    "hairpompadour": "Pompadour",
    "hairponytail": "Ponytail",
    "hairquiff": "Quiff",
    "hairreceding": "Receding",
    "hairshag": "Shag",
    "hairshort": "Short",
    "hairslickback": "Slick Back",
    "hairspiky": "Spiky",
    "hairtaper": "Taper",
    "hairtopknot": "Top Knot",
    "hairtwists": "Twists",
    "hairundercut": "Undercut",
    "hairwaves": "Waves",
    "browsangle": "Angled Brows",
    "browsflat": "Flat Brows",
    "browsround": "Rounded Brows",
    "browsarch": "Arched Brows",
    "lashesnatural": "Natural Lashes",
    "lashescat": "Cat-Eye Lashes",
    "lashesdoll": "Doll Lashes",
    "lashesopen": "Open Lashes",
    "lasheswispy": "Wispy Lashes",
    "beardbandholz": "Bandholz Beard",
    "beardbox": "Boxed Beard",
    "beardchops": "Mutton Chops",
    "bearddutch": "Dutch Beard",
    "beardfull": "Full Beard",
    "beardgoatee": "Goatee",
    "beardstache": "Beardstache",
    "beardstubble": "Stubble",
    "beardvandyke": "Van Dyke",
    "mustachechevron": "Chevron Mustache",
    "chopsmutton": "Mutton Chops",
    "stubbleregular": "Regular Stubble",
    "chingoatee": "Chin Goatee",
    "armpithairfull": "Armpit Hair",
    "chesthairfull": "Chest Hair",
    "leghairfull": "Leg Hair",
    "bodyhairfull": "Body Hair",
    "bodyhairgenitals": "Body Hair (Lower)",
    "genitalhairfull": "Lower Body Hair",
}


def item_stem(name: str) -> str:
    if not name:
        return ""
    nm = name.lower()
    m = _STEM_RE.match(nm)
    return m.group(1) if m else nm


def parse_groom_drawable(name: str) -> dict[str, Any]:
    """Parse rig, slot, style, texture, length, and human-friendly label from a drawable name."""
    nm = (name or "").lower()
    m = _STYLE_TOKEN_RE.search(nm)
    rig_m = _RIG_RE.search(nm)
    len_m = _LEN_RE.search(nm)
    tex_m = _TEX_RE.search(nm)

    raw_style = m.group(1) if m else ""
    rig = (m.group(2) if m else (rig_m.group(1) if rig_m else "")).lower()
    num = m.group(3) if m else ""
    length_code = len_m.group(1).lower() if len_m else ""
    texture = tex_m.group(1).lower() if tex_m else ""

    slot = "hair"
    check = raw_style or nm
    matched_pref = False
    for pref, sl in _GROOM_PREFIX_TO_SLOT:
        if check.startswith(pref):
            slot = sl
            matched_pref = True
            break
    if not matched_pref:
        if "hairacc" in nm:
            slot = "accessory"
        elif "eyelash" in nm or "lashes" in nm:
            slot = "lashes"
        elif "eyebrow" in nm or "brows" in nm:
            slot = "brows"
        elif any(t in nm for t in ("beard", "mustache", "stubble", "chops", "chingoatee")):
            slot = "beard"
        elif any(t in nm for t in ("bodyhair", "chesthair", "leghair", "armpithair", "genitalhair")):
            slot = "bodyhair"

    # Character prefix (e.g. cs_mandy, player_story000_00, cs_boobie_ike)
    char_prefix = ""
    if m and m.start() > 0:
        char_prefix = nm[: m.start()].rstrip("_")
    elif not m and nm.startswith(("cs_", "player_story", "u_m_", "u_f_", "sc_")):
        hm = re.search(r"_head_", nm)
        if hm:
            char_prefix = nm[: hm.start()].rstrip("_")

    pretty_style = _STYLE_PRETTY.get(raw_style)
    if not pretty_style and raw_style:
        # Strip leading category word for clean display
        for pfx in ("hair", "brows", "lashes", "beard", "mustache", "bodyhair", "chesthair", "leghair", "armpithair"):
            if raw_style.startswith(pfx) and len(raw_style) > len(pfx):
                pretty_style = f"{raw_style[len(pfx):].title()} {pfx.title()}"
                break
        if not pretty_style:
            pretty_style = raw_style.replace("_", " ").title()
    elif not pretty_style:
        if slot == "lashes":
            pretty_style = "Eyelashes"
        elif slot == "brows":
            pretty_style = "Eyebrows"

    parts = []
    if char_prefix:
        cp = char_prefix.replace("player_story000_00", "Lucia").replace("player_story000_01", "Jason")
        cp = re.sub(r"^(?:cs|u_[mf]_[ymo]|sc)_", "", cp).replace("_", " ").title()
        parts.append(cp)
    parts.append(pretty_style or nm)
    if num and (not char_prefix or num != "000"):
        parts.append(f"#{int(num)}")
    if texture:
        parts.append(f"({texture.title()})")
    label = " ".join(p for p in parts if p)

    return {
        "drawable": name,
        "stem": item_stem(name),
        "slot": slot,
        "rig": rig,
        "style": raw_style,
        "number": num,
        "lengthCode": length_code,
        "lengthMeters": _LENGTH_METERS.get(length_code),
        "texture": texture,
        "charPrefix": char_prefix,
        "label": label,
    }


# ---------------------------------------------------------------------------
# 5. Inventory & Grooms.sqlite3 Linkage Engine
# ---------------------------------------------------------------------------

def _load_inventory_index(inv_by_name: dict[str, Any] | None = None) -> dict[str, Any]:
    global _INV_CACHE
    with _LOCK:
        if _INV_CACHE is not None:
            return _INV_CACHE
        h2n: dict[int, str] = {}
        by_name: dict[str, dict[str, Any]] = {}
        sorted_groom_names: list[str] = []
        poly_groom_names: list[dict[str, Any]] = []

        if inv_by_name is not None:
            for k, v in inv_by_name.items():
                nm = v["name"] if isinstance(v, dict) and "name" in v else str(k)
                nml = nm.lower()
                vbytes = (v.get("vbytes") if isinstance(v, dict) else 0) or 0
                cat = (v.get("category") if isinstance(v, dict) else "") or ""
                box = v.get("box") if isinstance(v, dict) else None
                rec = {"name": nm, "vbytes": int(vbytes), "category": cat, "box": box}
                by_name[nml] = rec
                h2n[fp.murmur64(nml.encode())] = nm
                if _STYLE_TOKEN_RE.search(nml):
                    sorted_groom_names.append(nml)
                    if int(vbytes) > 200:
                        poly_groom_names.append(rec)
        elif INVENTORY_DB.exists():
            with closing(sqlite3.connect(INVENTORY_DB.as_uri() + "?mode=ro", uri=True)) as db:
                for nm, cat, vbytes, box in db.execute("SELECT name, category, vbytes, box FROM model"):
                    nml = nm.lower()
                    rec = {"name": nm, "vbytes": int(vbytes or 0), "category": cat or "", "box": box}
                    by_name[nml] = rec
                    h2n[fp.murmur64(nml.encode())] = nm
                    if _STYLE_TOKEN_RE.search(nml) or "eyelash" in nml:
                        sorted_groom_names.append(nml)
                        if (vbytes or 0) > 200:
                            poly_groom_names.append(rec)

        sorted_groom_names.sort()
        _INV_CACHE = {
            "h2n": h2n,
            "by_name": by_name,
            "sorted_groom_names": sorted_groom_names,
            "poly_groom_names": poly_groom_names,
        }
        return _INV_CACHE


_HINT_RANGE_RE = re.compile(
    r"one of(?: the)? (\d+) [^\(]*\(([a-z0-9_]+)\s*\.\.\s*([a-z0-9_]+)\)",
    re.I,
)
_HINT_GROOM_POS_RE = re.compile(r"groom (\d+) of (\d+)", re.I)


def _parse_box(box_val: Any) -> list[list[float]] | None:
    if not box_val:
        return None
    if isinstance(box_val, str):
        try:
            box_val = json.loads(box_val)
        except Exception:
            return None
    if isinstance(box_val, (list, tuple)) and len(box_val) == 2 and len(box_val[0]) == 3:
        return [[float(x) for x in box_val[0]], [float(x) for x in box_val[1]]]
    return None


def _groom_z_center(row: dict[str, Any]) -> float:
    b = row.get("_box")
    if b:
        return 0.5 * (b[0][2] + b[1][2])
    return 0.65


def _load_groom_index(grooms_db: sqlite3.Connection | Path | str | None = None) -> dict[str, Any]:
    global _GROOM_INDEX_CACHE
    with _LOCK:
        if _GROOM_INDEX_CACHE is not None and grooms_db is None:
            return _GROOM_INDEX_CACHE

        own_conn = False
        if isinstance(grooms_db, sqlite3.Connection):
            db = grooms_db
            db.row_factory = sqlite3.Row
        else:
            db_path = Path(grooms_db) if grooms_db else GROOMS_DB
            if not db_path.exists():
                return {
                    "rows_by_id": {},
                    "by_exact": {},
                    "by_exact_stem": {},
                    "by_likely": {},
                    "by_likely_stem": {},
                    "by_grp": {},
                    "hint_blocks": [],
                    "by_style_rig": {},
                    "all_rows": [],
                }
            db = sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True)
            db.row_factory = sqlite3.Row
            own_conn = True

        try:
            all_rows = []
            rows_by_id: dict[int, dict[str, Any]] = {}
            by_exact: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
            by_exact_stem: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
            by_likely: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
            by_likely_stem: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
            by_grp: dict[tuple[str, int], list[dict[str, Any]]] = collections.defaultdict(list)
            by_style_rig: dict[tuple[str, str, str], list[tuple[str, list[int], float]]] = collections.defaultdict(list)

            # Group hint ranges (archive, start_item, end_item) -> list of grooms in ord order
            hint_range_groups: dict[tuple[str, str, str], list[dict[str, Any]]] = collections.defaultdict(list)
            # Single-item mentions in hint (e.g. `cs_ali_hairdreadlocks_...`)
            by_hint_exact: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)

            for r in db.execute(
                "SELECT id, archive, offset, ord, lods, class, guides, nodes_per_guide, triangles, "
                "strands0, strands1, box, longest, bytes, regions, name, name_basis, hint, likely, likely_p, grp "
                "FROM asset ORDER BY archive, ord"
            ):
                d = dict(r)
                d["_box"] = _parse_box(d.get("box"))
                d["_strands"] = int(d.get("strands0") or 0) + int(d.get("strands1") or 0)
                all_rows.append(d)
                rows_by_id[d["id"]] = d

                if d.get("grp") is not None:
                    by_grp[(d["archive"], int(d["grp"]))].append(d)

                nm = (d.get("name") or "").lower()
                if nm:
                    by_exact[nm].append(d)
                    by_exact_stem[item_stem(nm)].append(d)

                lk = (d.get("likely") or "").lower()
                if lk:
                    by_likely[lk].append(d)
                    by_likely_stem[item_stem(lk)].append(d)

                h = d.get("hint") or ""
                if h and not nm and not lk:
                    hl = h.lower()
                    rm = _HINT_RANGE_RE.search(hl)
                    if rm:
                        s_item, e_item = rm.group(2), rm.group(3)
                        hint_range_groups[(d["archive"], s_item, e_item)].append(d)
                    else:
                        # Single-item hint like "cs_ali_hairdreadlocks_fr1_000_..., if every item..."
                        for tok in re.findall(r"[a-z0-9_]+_(?:fr1|mr1|mk1|mi1|fc1|mc1)_\d{3}[a-z0-9_]*", hl):
                            by_hint_exact[tok].append(d)
                            by_hint_exact[item_stem(tok)].append(d)

            # Build (slot, style, rig) -> list of (item_name, groom_ids, max_longest) for smart fallbacks
            seen_item_keys: set[str] = set()
            for nm, rows in list(by_exact.items()) + list(by_likely.items()):
                if nm in seen_item_keys:
                    continue
                seen_item_keys.add(nm)
                meta = parse_groom_drawable(nm)
                # Expand full grp if from likely
                gids = _expand_group_ids(rows, by_grp)
                max_len = max((rows_by_id[g]["longest"] or 0.0) for g in gids) if gids else 0.0
                by_style_rig[(meta["slot"], meta["style"], meta["rig"])].append((nm, gids, max_len))
                by_style_rig[(meta["slot"], "", meta["rig"])].append((nm, gids, max_len))

            # Precompute hint range blocks with their sorted inventory items
            inv = _load_inventory_index()
            sorted_inv = inv["sorted_groom_names"]
            hint_blocks = []
            for (arch, s_item, e_item), grows in hint_range_groups.items():
                # Find all inventory items alphabetically in [s_item, e_item]
                s_lo, e_hi = (s_item, e_item) if s_item <= e_item else (e_item, s_item)
                items_in_range = [x for x in sorted_inv if s_lo <= x <= e_hi]
                if not items_in_range:
                    items_in_range = [s_lo] if s_lo == e_hi else [s_lo, e_hi]
                grows_sorted = sorted(grows, key=lambda x: x["ord"])
                hint_blocks.append({
                    "archive": arch,
                    "start": s_lo,
                    "end": e_hi,
                    "items": items_in_range,
                    "item_index": {it: i for i, it in enumerate(items_in_range)},
                    "grooms": grows_sorted,
                })

            idx = {
                "rows_by_id": rows_by_id,
                "by_exact": by_exact,
                "by_exact_stem": by_exact_stem,
                "by_likely": by_likely,
                "by_likely_stem": by_likely_stem,
                "by_grp": by_grp,
                "by_hint_exact": by_hint_exact,
                "hint_blocks": hint_blocks,
                "by_style_rig": by_style_rig,
                "all_rows": all_rows,
            }
            if grooms_db is None:
                _GROOM_INDEX_CACHE = idx
            return idx
        finally:
            if own_conn:
                db.close()


def _expand_group_ids(rows: list[dict[str, Any]], by_grp: dict[tuple[str, int], list[dict[str, Any]]]) -> list[int]:
    """Expand groom rows to include all sibling sub-grooms in the same `grp` run."""
    if not rows:
        return []
    # If all rows are named, return their exact IDs in archive/ord order
    if any(r.get("name") for r in rows):
        return [r["id"] for r in sorted(rows, key=lambda x: (x["archive"], x["ord"]))]
    # Pick the highest-confidence group run
    best_grp: tuple[str, int] | None = None
    best_score = -1.0
    for r in rows:
        if r.get("grp") is not None:
            sc = float(r.get("likely_p") or 0.0)
            if sc > best_score:
                best_score = sc
                best_grp = (r["archive"], int(r["grp"]))
    if best_grp is not None and best_grp in by_grp:
        return [g["id"] for g in sorted(by_grp[best_grp], key=lambda x: x["ord"])]
    return [r["id"] for r in sorted(rows, key=lambda x: (x["archive"], x["ord"]))]


def _groom_matches_slot_class(g: dict[str, Any], slot: str, rig: str) -> bool:
    """Return True if groom `g`'s heuristic class is physically compatible with `slot` and `rig`."""
    cls = g.get("class") or ""
    if slot == "hair":
        if cls == "Scalp hair":
            return True
        # Female fr1 short hair has lower Z (~0.66..0.72) and is occasionally labeled 'Facial hair'
        if cls == "Facial hair" and rig == "fr1":
            b = g.get("_box")
            if b and b[1][2] >= 0.65:
                return True
        return False
    if slot == "beard":
        return cls == "Facial hair"
    if slot in ("brows", "lashes"):
        return cls == "Brows and lashes"
    if slot == "bodyhair":
        return cls in ("Body hair", "Fur and other")
    return True


def _match_hint_range(drawable_lower: str, meta: dict[str, Any], gidx: dict[str, Any]) -> tuple[list[int], bool]:
    """Match an item across ALL containing `(start .. end)` alphabetical hint blocks in `grooms.sqlite3`.

    Returns `(groom_ids, is_high_confidence)` where `is_high_confidence` is True when the
    item is an exact endpoint of the range or inside a tight (<=4 item) class-matched block.
    """
    rig = meta["rig"]
    slot = meta["slot"]
    want_z = _RIG_HEAD_Z.get(rig)
    want_len = meta.get("lengthMeters")

    scored: list[tuple[float, bool, dict[str, Any]]] = []
    for blk in gidx["hint_blocks"]:
        if not (blk["start"] <= drawable_lower <= blk["end"]):
            continue
        is_endpoint = (drawable_lower == blk["start"]) or (drawable_lower == blk["end"])
        items = blk["items"]
        grows = blk["grooms"]
        n_items = max(1, len(items))
        n_grooms = len(grows)
        pos = blk["item_index"].get(drawable_lower, -1)
        if pos < 0:
            pos = sum(1 for x in items if x < drawable_lower)

        center = (pos + 0.5) * n_grooms / n_items
        window = max(3, int(math.ceil(n_grooms / n_items * 2.5)))
        if is_endpoint:
            # Endpoint items can sit at either boundary of the block
            lo, hi = 0, n_grooms
        else:
            lo = max(0, int(center) - window)
            hi = min(n_grooms, int(center) + window + 1)
        candidates = grows[lo:hi] or grows

        span_pen = 0.06 * min(n_items, 30)
        ep_bonus = -1.6 if is_endpoint else 0.0

        for k, g in enumerate(candidates, start=lo):
            if not _groom_matches_slot_class(g, slot, rig):
                continue
            dist_ord = abs(k - center) / max(1.0, float(max(window, n_grooms)))
            empty_pen = 2.5 if g.get("_strands", 0) <= 0 else 0.0
            # Slightly prefer richer primary grooms over tiny stray tufts
            rich_bonus = -0.25 if g.get("_strands", 0) >= 1000 else 0.0
            z_pen = 0.0
            if want_z is not None and slot in ("hair", "brows", "lashes", "beard"):
                zc = _groom_z_center(g)
                z_pen = abs(zc - want_z) * 3.0
            len_pen = 0.0
            if want_len is not None and g.get("longest"):
                len_pen = abs(float(g["longest"]) - want_len) * 1.5
            score = dist_ord + span_pen + ep_bonus + empty_pen + rich_bonus + z_pen + len_pen
            high_conf = is_endpoint or (n_items <= 4)
            scored.append((score, high_conf, g))

    if not scored:
        return [], False

    scored.sort(key=lambda t: t[0])
    _, high_conf, chosen = scored[0]

    if chosen.get("grp") is not None:
        return _expand_group_ids([chosen], gidx["by_grp"]), high_conf
    return [chosen["id"]], high_conf


def resolve_drawable_to_groom(
    drawable: str,
    inv_index: dict[str, Any] | None = None,
    groom_index: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve a single 0DRP groom drawable name to strand groom ID(s) and/or polygonal mesh."""
    inv = inv_index or _load_inventory_index()
    gidx = groom_index or _load_groom_index()
    dl = (drawable or "").lower()
    st = item_stem(dl)
    meta = parse_groom_drawable(drawable)

    inv_rec = inv["by_name"].get(dl)
    has_poly = bool(inv_rec and inv_rec.get("vbytes", 0) > 200)

    groom_ids: list[int] = []
    match_type = "none"
    matched_item = drawable

    # Stage 1: Exact `name` match in grooms.sqlite3
    if dl in gidx["by_exact"]:
        groom_ids = _expand_group_ids(gidx["by_exact"][dl], gidx["by_grp"])
        match_type = "exact"
    elif st in gidx["by_exact_stem"]:
        groom_ids = _expand_group_ids(gidx["by_exact_stem"][st], gidx["by_grp"])
        match_type = "exact_stem"
    # Stage 2: `likely` / `grp` multi-groom match
    elif dl in gidx["by_likely"]:
        groom_ids = _expand_group_ids(gidx["by_likely"][dl], gidx["by_grp"])
        match_type = "likely"
    elif st in gidx["by_likely_stem"]:
        groom_ids = _expand_group_ids(gidx["by_likely_stem"][st], gidx["by_grp"])
        match_type = "likely_stem"
    # Stage 3: Single-item hint match
    elif dl in gidx["by_hint_exact"]:
        groom_ids = _expand_group_ids(gidx["by_hint_exact"][dl][:1], gidx["by_grp"])
        match_type = "hint_exact"
    elif st in gidx["by_hint_exact"]:
        groom_ids = _expand_group_ids(gidx["by_hint_exact"][st][:1], gidx["by_grp"])
        match_type = "hint_exact"
    else:
        # Stage 3b vs Stage 4: Check high-confidence class-matched hint_range vs prefix_stripped
        range_ids, range_high_conf = _match_hint_range(dl, meta, gidx)
        if range_ids and range_high_conf:
            groom_ids = range_ids
            match_type = "hint_range"
        else:
            # Try stripping character prefix (e.g. `player_story000_00_browsangle_fr1_000` -> `browsangle_fr1_000`
            # or `cs_brian_heder_hairreceding_mr1_000` -> `hairreceding_mr1_000`)
            if meta["charPrefix"] and meta["style"] and meta["rig"]:
                generic_stem = f"{meta['style']}_{meta['rig']}_{meta['number'] or '000'}"
                if generic_stem in gidx["by_exact_stem"]:
                    groom_ids = _expand_group_ids(gidx["by_exact_stem"][generic_stem], gidx["by_grp"])
                    match_type = "prefix_stripped"
                    matched_item = generic_stem
                elif generic_stem in gidx["by_likely_stem"]:
                    groom_ids = _expand_group_ids(gidx["by_likely_stem"][generic_stem], gidx["by_grp"])
                    match_type = "prefix_stripped"
                    matched_item = generic_stem
            if not groom_ids and range_ids:
                groom_ids = range_ids
                match_type = "hint_range"

    # Stage 5: Style + Rig + Length nearest fallback (for any custom hair/brow/lash/beard)
    if not groom_ids and not has_poly and meta["rig"]:
        pool = (
            gidx["by_style_rig"].get((meta["slot"], meta["style"], meta["rig"]))
            or gidx["by_style_rig"].get((meta["slot"], "", meta["rig"]))
        )
        if pool:
            want_len = meta.get("lengthMeters")
            if want_len is not None:
                best = min(pool, key=lambda t: abs(t[2] - want_len))
            else:
                best = pool[0]
            matched_item, groom_ids = best[0], list(best[1])
            match_type = "style_fallback"

    strand_count = sum(
        gidx["rows_by_id"][gid]["_strands"]
        for gid in groom_ids
        if gid in gidx["rows_by_id"]
    )
    kind = "strand" if groom_ids else ("mesh" if has_poly else "none")
    if groom_ids and has_poly:
        kind = "strand+mesh"

    return {
        **meta,
        "kind": kind,
        "matchType": match_type,
        "matchedItem": matched_item,
        "groomIds": groom_ids,
        "strandCount": int(strand_count),
        "hasPolyMesh": has_poly,
        "polyMeshDrawable": drawable if has_poly else None,
    }


# ---------------------------------------------------------------------------
# 6. 0DRP Decompression & Extraction (`extract_drp_groom_data`)
# ---------------------------------------------------------------------------

def _load_spans() -> tuple[list[tuple[int, int, int, int, int]], dict[int, int]]:
    global _SPANS_CACHE
    with _LOCK:
        if _SPANS_CACHE is not None:
            return _SPANS_CACHE
        raw = fp.PACKS_SPANS.read_bytes()
        spans = [struct.unpack_from("<QQQII", raw, i) for i in range(0, len(raw), 32)]
        by_off = {r[0]: idx for idx, r in enumerate(spans)}
        _SPANS_CACHE = (spans, by_off)
        return _SPANS_CACHE


def read_0drp_bytes(drp_offset: int) -> bytes | None:
    """Decompress a ped's `0DRP` catalogue from `packs.rpf` using `deflate-spans.bin`."""
    if not drp_offset:
        return None
    spans, by_off = _load_spans()
    idx = by_off.get(int(drp_offset))
    if idx is None:
        return None
    out = bytearray()
    with open(fp.PACKS_RPF, "rb") as f:
        j = idx
        while j < len(spans):
            off, clen, _ilen, _a, _b = spans[j]
            f.seek(off)
            chunk = zlib.decompress(f.read(clen))
            out.extend(chunk)
            if len(chunk) < 65536 or j + 1 >= len(spans):
                break
            nxt_expected = (off + clen + 15) & -16
            if spans[j + 1][0] != nxt_expected:
                break
            j += 1
    return bytes(out)


def _extract_from_0drp_blocks(
    blocks: dict[int, tuple[int, memoryview]],
    inv_index: dict[str, Any],
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    """Parse all groom presets, outfit usage counts, and palette colors from 0DRP blocks."""
    h2n = inv_index["h2n"]
    presets_by_id: dict[int, dict[str, Any]] = {}

    # 1. Read all 0xf795e8ba preset entries (88 bytes each)
    for _bid, (k, d) in blocks.items():
        if k != 0xF795E8BA:
            continue
        for i in range(len(d) // 88):
            o = i * 88
            pid, parent_id, scnt, _pad, sptr, cat, pal, b0, b1, b2, b3, _muv = struct.unpack_from(
                "<IIIIQIIBBBBI", d, o
            )
            drawables: list[str] = []
            if sptr and scnt:
                sbid, sbo = fp.ptr(sptr)
                if sbid in blocks:
                    sbuf = blocks[sbid][1]
                    for j in range(min(scnt, 8)):
                        so = sbo + j * 184
                        if so + 16 > len(sbuf):
                            break
                        h0 = struct.unpack_from("<Q", sbuf, so + 8)[0]
                        nm = h2n.get(h0)
                        if nm and "hiddenpoly" not in nm.lower() and nm not in drawables:
                            drawables.append(nm)
            presets_by_id[pid] = {
                "pid": pid,
                "parent": parent_id,
                "cat": cat,
                "pal": pal,
                "b": (b0, b1, b2, b3),
                "drawables": drawables,
            }

    # 2. Count how many outfits (`0xa9d57751` -> `0x67e54cb2`) reference each preset
    outfit_counts: collections.Counter[int] = collections.Counter()
    first_outfit_rank: dict[int, int] = {}
    outfit_idx = 0
    for _bid, (k, d) in blocks.items():
        if k != 0xA9D57751:
            continue
        for oi in range(len(d) // 80):
            o = oi * 80
            icnt = struct.unpack_from("<H", d, o + 56)[0]
            iptr = struct.unpack_from("<Q", d, o + 64)[0]
            if iptr and icnt:
                ibid, ibo = fp.ptr(iptr)
                if ibid in blocks:
                    ibuf = blocks[ibid][1]
                    for j in range(icnt):
                        io = ibo + j * 8
                        if io + 8 > len(ibuf):
                            break
                        ref_pid, _ref_sid = struct.unpack_from("<II", ibuf, io)
                        outfit_counts[ref_pid] += 1
                        if ref_pid not in first_outfit_rank:
                            first_outfit_rank[ref_pid] = outfit_idx
            outfit_idx += 1

    # 3. Resolve parent->child inheritance for drawables, categories & hair colors
    by_slot: dict[str, dict[str, dict[str, Any]]] = {s: {} for s in GROOM_SLOTS}
    drp_colors: list[dict[str, Any]] = []
    seen_color_keys: set[tuple[int, int, int, int]] = set()

    for pid, pr in presets_by_id.items():
        cat_info = GROOM_CATEGORY_HASHES.get(pr["cat"])
        if not cat_info:
            continue
        slot, sub_slot = cat_info
        if slot not in by_slot:
            continue

        # Walk parent chain (max depth 3) for drawables & palette
        drawables = list(pr["drawables"])
        pal = pr["pal"]
        b0, b1, b2, _b3 = pr["b"]
        cur = pr
        for _ in range(3):
            par_id = cur["parent"]
            if not par_id or par_id not in presets_by_id:
                break
            par = presets_by_id[par_id]
            if not drawables and par["drawables"]:
                drawables = list(par["drawables"])
            if (not pal or (b0 in (0, 255) and b1 in (0, 255))) and par["pal"]:
                pal = par["pal"]
                if b0 in (0, 255):
                    b0, b1, b2, _ = par["b"]
            cur = par

        # Record explicit 0DRP hair palette colors
        if slot in ("hair", "beard", "brows") and pal and (b0 not in (0, 255) or b1 not in (0, 255)):
            ck = (pal, b0, b1, b2)
            if ck not in seen_color_keys:
                c = resolve_palette_color(pal, b0, b1, b2)
                if c:
                    seen_color_keys.add(ck)
                    drp_colors.append(c)

        if not drawables:
            continue
        dname = drawables[0]
        key = dname.lower()
        oc = outfit_counts.get(pid, 0)
        rank = first_outfit_rank.get(pid, 9999)

        existing = by_slot[slot].get(key)
        if existing is None:
            by_slot[slot][key] = {
                "drawable": dname,
                "subSlot": sub_slot,
                "outfitCount": oc,
                "firstOutfit": rank,
                "presetIds": [f"0x{pid:08x}"],
            }
        else:
            existing["outfitCount"] += oc
            existing["firstOutfit"] = min(existing["firstOutfit"], rank)
            existing["presetIds"].append(f"0x{pid:08x}")

    out_lists: dict[str, list[dict[str, Any]]] = {}
    for slot, items_map in by_slot.items():
        items = list(items_map.values())
        # Rank defaults:
        #   1. Used in outfits (`outfitCount > 0`) first
        #   2. Higher `outfitCount` first
        #   3. Earlier `firstOutfit` index
        #   4. Character-specific items (`cs_*`/`player_story*`) before generic on ties
        items.sort(
            key=lambda it: (
                0 if it["outfitCount"] > 0 else 1,
                -it["outfitCount"],
                it["firstOutfit"],
                0 if it["drawable"].lower().startswith(("cs_", "player_story")) else 1,
                it["drawable"].lower(),
            )
        )
        out_lists[slot] = items

    return out_lists, drp_colors


def _fallback_default_for_rig(slot: str, rig: str, gidx: dict[str, Any], inv: dict[str, Any]) -> dict[str, Any] | None:
    """Guaranteed clean default groom for a rig when a ped's 0DRP omits brows/lashes/hair."""
    r = (rig or "fr1").lower()
    if r not in ("fr1", "mr1"):
        r = "fr1" if r.startswith("f") else "mr1"
    defaults = {
        ("brows", "fr1"): "browsangle_fr1_000_medium_n_full",
        ("brows", "mr1"): "browsangle_mr1_000_medium_n_full",
        ("lashes", "fr1"): "lashesnatural_fr1_000_full",
        ("lashes", "mr1"): "lashesnatural_mr1_000_full",
        ("hair", "fr1"): "hairlob_fr1_008_wavy_l09_non_sm_p_full",
        ("hair", "mr1"): "hairbob_mr1_014_straight_l06_non_sb_p_full",
    }
    dn = defaults.get((slot, r))
    if not dn:
        return None
    res = resolve_drawable_to_groom(dn, inv_index=inv, groom_index=gidx)
    res["subSlot"] = slot
    res["outfitCount"] = 0
    res["isRigFallback"] = True
    return res


def extract_drp_groom_data(
    drp_parts: Any,
    *,
    ped_name: str = "",
    rig: str = "",
    head_drawable: str = "",
    skin_rgb: str | Sequence[int] | None = None,
    inv_by_name: dict[str, Any] | None = None,
    grooms_db: sqlite3.Connection | Path | str | None = None,
    include_rig_fallbacks: bool = True,
) -> dict[str, Any]:
    """Extract default and all available groom variants + default hair color from a ped's 0DRP.

    `drp_parts` may be:
      - `bytes` / `bytearray` / `memoryview`: raw decompressed `0DRP` blob
      - `dict[int, tuple[int, Any]]`: parsed `0DRP` blocks from `face_presets.parse_0drp_blocks`
      - `list` / `tuple`: pre-extracted drawable names (`str`) or part dicts
        (`{"drawable": ..., "category": ..., "paletteHash": ..., "paletteIndex": ...}`)
      - `None`: returns rig-appropriate defaults if `include_rig_fallbacks=True`.
    """
    inv = _load_inventory_index(inv_by_name)
    gidx = _load_groom_index(grooms_db)

    slot_raw: dict[str, list[dict[str, Any]]] = {s: [] for s in GROOM_SLOTS}
    drp_colors: list[dict[str, Any]] = []

    if isinstance(drp_parts, (bytes, bytearray, memoryview)):
        _, blocks = fp.parse_0drp_blocks(bytes(drp_parts))
        if blocks:
            slot_raw, drp_colors = _extract_from_0drp_blocks(blocks, inv)
    elif isinstance(drp_parts, dict) and drp_parts and isinstance(next(iter(drp_parts.keys())), int):
        slot_raw, drp_colors = _extract_from_0drp_blocks(drp_parts, inv)
    elif isinstance(drp_parts, (list, tuple)):
        seen: dict[str, set[str]] = {s: set() for s in GROOM_SLOTS}
        for entry in drp_parts:
            if isinstance(entry, str):
                dn = entry
                meta = parse_groom_drawable(dn)
                sl = meta["slot"]
                if sl in slot_raw and dn.lower() not in seen[sl]:
                    seen[sl].add(dn.lower())
                    slot_raw[sl].append({"drawable": dn, "subSlot": sl, "outfitCount": 1, "firstOutfit": 0})
            elif isinstance(entry, dict):
                dn = entry.get("drawable") or entry.get("name") or ""
                if not dn:
                    continue
                cat_h = entry.get("categoryHash")
                if isinstance(cat_h, int) and cat_h in GROOM_CATEGORY_HASHES:
                    sl, sub = GROOM_CATEGORY_HASHES[cat_h]
                else:
                    sl = entry.get("slot") or parse_groom_drawable(dn)["slot"]
                    sub = entry.get("subSlot") or sl
                if sl in slot_raw and dn.lower() not in seen[sl]:
                    seen[sl].add(dn.lower())
                    slot_raw[sl].append({
                        "drawable": dn,
                        "subSlot": sub,
                        "outfitCount": int(entry.get("outfitCount", 1)),
                        "firstOutfit": int(entry.get("firstOutfit", 0)),
                    })
                ph = entry.get("paletteHash")
                pi = entry.get("paletteIndex")
                if isinstance(ph, int) and isinstance(pi, int):
                    c = resolve_palette_color(ph, pi, int(entry.get("secondaryIndex", 255)))
                    if c:
                        drp_colors.append(c)

    # Resolve every extracted drawable to strand groom ID(s) / polygonal mesh
    variants: dict[str, list[dict[str, Any]]] = {s: [] for s in GROOM_SLOTS}
    inferred_rig = (rig or "").lower()
    for slot in GROOM_SLOTS:
        for item in slot_raw[slot]:
            resolved = resolve_drawable_to_groom(item["drawable"], inv_index=inv, groom_index=gidx)
            resolved["subSlot"] = item.get("subSlot") or slot
            resolved["outfitCount"] = item.get("outfitCount", 0)
            if item.get("presetIds"):
                resolved["presetIds"] = item["presetIds"]
            if not inferred_rig and resolved.get("rig"):
                inferred_rig = resolved["rig"]
            variants[slot].append(resolved)

    if not inferred_rig:
        m = _RIG_RE.search((head_drawable or ped_name or "").lower())
        inferred_rig = m.group(1) if m else "fr1"

    # Build defaults per slot
    defaults: dict[str, Any] = {
        "hair": variants["hair"][0] if variants["hair"] else None,
        "brows": variants["brows"][0] if variants["brows"] else None,
        "lashes": variants["lashes"][0] if variants["lashes"] else None,
        "beard": variants["beard"][0] if variants["beard"] else None,
        "bodyhair": [],
    }
    # For bodyhair, pick one default per distinct subSlot (chesthair, leghair, armpithair, genitalhair, bodyhair)
    seen_bh_sub: set[str] = set()
    for bh in variants["bodyhair"]:
        ss = bh.get("subSlot") or "bodyhair"
        if ss not in seen_bh_sub:
            seen_bh_sub.add(ss)
            defaults["bodyhair"].append(bh)

    if include_rig_fallbacks:
        for req_slot in ("brows", "lashes"):
            if defaults[req_slot] is None and inferred_rig in ("fr1", "mr1"):
                fb = _fallback_default_for_rig(req_slot, inferred_rig, gidx, inv)
                if fb:
                    defaults[req_slot] = fb

    default_color = infer_default_hair_color(
        ped_name=ped_name,
        head_drawable=head_drawable,
        skin_rgb=skin_rgb,
        drp_colors=drp_colors,
    )

    return {
        "pedName": ped_name or None,
        "rig": inferred_rig,
        "defaultColor": default_color,
        "drpColors": drp_colors,
        "defaults": defaults,
        "variants": variants,
    }


def extract_ped_groom_data(
    ped_name: str,
    *,
    include_rig_fallbacks: bool = True,
) -> dict[str, Any]:
    """Convenience helper: look up a ped in `ped_archetypes.sqlite3` and extract its groom data."""
    if not PEDS_DB.exists():
        raise FileNotFoundError(f"Missing {PEDS_DB}")
    with closing(sqlite3.connect(PEDS_DB.as_uri() + "?mode=ro", uri=True)) as db:
        row = db.execute(
            "SELECT name, drp_offset, body_rig, head_drawable, skin_rgb FROM ped_archetype WHERE lower(name)=lower(?)",
            (ped_name,),
        ).fetchone()
    if not row:
        raise KeyError(f"Unknown ped archetype: {ped_name}")
    name, drp_off, rig, head_drawable, skin_rgb = row
    buf = read_0drp_bytes(drp_off) if drp_off else None
    return extract_drp_groom_data(
        buf,
        ped_name=name,
        rig=rig or "",
        head_drawable=head_drawable or "",
        skin_rgb=skin_rgb,
        include_rig_fallbacks=include_rig_fallbacks,
    )


# ---------------------------------------------------------------------------
# 7. Per-Rig Groom & Hair Color Catalog (`groom_catalog_for_rig`)
# ---------------------------------------------------------------------------

def groom_catalog_for_rig(
    rig: str,
    inv_by_name: dict[str, Any] | None = None,
    grooms_db: sqlite3.Connection | Path | str | None = None,
) -> dict[str, Any]:
    """Return all available hair, brows, lashes, beard & bodyhair variants for `rig` + `HAIR_COLOR_PALETTE`.

    Combines:
      - Named & `likely`/`grp`-grouped strand grooms from `grooms.sqlite3` filtered to `rig`
      - Alphabetical `hint`-range strand grooms (beards, bodyhair, custom `cs_*` hairs) for `rig`
      - Polygonal hair/lash/accessory meshes (`vbytes > 200`) from `inventory.sqlite3` for `rig`
    """
    rig_norm = (rig or "fr1").lower().strip()
    use_cache = inv_by_name is None and grooms_db is None
    with _LOCK:
        if use_cache and rig_norm in _RIG_CATALOG_CACHE:
            return _RIG_CATALOG_CACHE[rig_norm]

    inv = _load_inventory_index(inv_by_name)
    gidx = _load_groom_index(grooms_db)

    by_slot: dict[str, list[dict[str, Any]]] = {s: [] for s in GROOM_SLOTS}
    accessories: list[dict[str, Any]] = []
    seen_keys: set[str] = set()

    # 1. All inventory groom drawables for this rig (covers named, likely/grp, hint-range, and poly meshes!)
    for nml in inv["sorted_groom_names"]:
        meta = parse_groom_drawable(nml)
        if meta["rig"] != rig_norm:
            continue
        if "hiddenpoly" in nml:
            continue
        slot = meta["slot"]
        rec = inv["by_name"][nml]
        drawable = rec["name"]

        # Deduplicate by stem unless different length/texture variants exist
        dedup_key = f"{slot}:{nml}"
        if dedup_key in seen_keys:
            continue
        seen_keys.add(dedup_key)

        resolved = resolve_drawable_to_groom(drawable, inv_index=inv, groom_index=gidx)
        if resolved["kind"] == "none":
            continue
        # Only list authentic matches (exact, likely, hint, prefix_stripped, or poly mesh) in the catalog
        if resolved["matchType"] == "style_fallback" and not resolved["hasPolyMesh"]:
            continue

        entry = {
            "id": nml,
            "drawable": drawable,
            "label": resolved["label"],
            "slot": slot,
            "style": resolved["style"],
            "texture": resolved["texture"],
            "lengthCode": resolved["lengthCode"],
            "charPrefix": resolved["charPrefix"],
            "kind": resolved["kind"],
            "matchType": resolved["matchType"],
            "groomIds": resolved["groomIds"],
            "strandCount": resolved["strandCount"],
            "hasPolyMesh": resolved["hasPolyMesh"],
        }
        if slot == "accessory":
            accessories.append(entry)
        elif slot in by_slot:
            by_slot[slot].append(entry)

    # 2. Also include any named/likely grooms in grooms.sqlite3 whose item wasn't in inventory.sqlite3
    for nm, rows in list(gidx["by_exact"].items()) + list(gidx["by_likely"].items()):
        meta = parse_groom_drawable(nm)
        if meta["rig"] != rig_norm:
            continue
        slot = meta["slot"]
        dedup_key = f"{slot}:{nm}"
        if dedup_key in seen_keys or slot not in by_slot:
            continue
        seen_keys.add(dedup_key)
        gids = _expand_group_ids(rows, gidx["by_grp"])
        if not gids:
            continue
        sc = sum(gidx["rows_by_id"][g]["_strands"] for g in gids if g in gidx["rows_by_id"])
        by_slot[slot].append({
            "id": nm,
            "drawable": nm,
            "label": meta["label"],
            "slot": slot,
            "style": meta["style"],
            "texture": meta["texture"],
            "lengthCode": meta["lengthCode"],
            "charPrefix": meta["charPrefix"],
            "kind": "strand",
            "matchType": "exact" if nm in gidx["by_exact"] else "likely",
            "groomIds": gids,
            "strandCount": int(sc),
            "hasPolyMesh": False,
        })

    # Sort each slot nicely: generic styles alphabetically first, then character-specific, then label
    for slot in by_slot:
        by_slot[slot].sort(key=lambda e: (1 if e["charPrefix"] else 0, e["style"], e["label"]))
    accessories.sort(key=lambda e: e["label"])

    catalog = {
        "rig": rig_norm,
        "counts": {s: len(by_slot[s]) for s in GROOM_SLOTS} | {"accessories": len(accessories)},
        "hair": by_slot["hair"],
        "brows": by_slot["brows"],
        "lashes": by_slot["lashes"],
        "beard": by_slot["beard"],
        "bodyhair": by_slot["bodyhair"],
        "accessories": accessories,
        "colors": hair_color_palette(),
        "colorsFromGame": hair_colors_from_game(),
    }
    if not catalog["colorsFromGame"]:
        catalog["colorsNote"] = ("The hair palette member (packs.rpf 00011dab3000.pso) was not found in the index, "
                                 "so the preset colors are a neutral grey ramp.")
    if use_cache:
        with _LOCK:
            _RIG_CATALOG_CACHE[rig_norm] = catalog
    return catalog


# ---------------------------------------------------------------------------
# 8. Strand Groom Decoder for Standing Character Models
# ---------------------------------------------------------------------------

# Compact binary magic header for `decode_groom_strands_for_model(..., binary=True)`:
#   magic[4] = b"STR1"
#   u32 vertexCount    (number of vec4(x, y, z, t) vertices)
#   u32 indexCount     (number of u32 GL_LINES segment indices)
#   u32 strandCount    (total strands decoded)
#   f32[6] bounds      (xmin, ymin, zmin, xmax, ymax, zmax in standing Y-up space)
#   f32[3] alignOffset (dx, dy, dz applied after +1.0m standing shift)
#   -> 52 bytes header, followed by:
#      Float32Array[vertexCount * 4]  interleaved (x, y, z, t_root_to_tip)
#      Uint32Array[indexCount]        GL_LINES segment index pairs
_BINARY_HEADER = struct.Struct("<4sIII6f3f")


def _generate_groom_polylines(
    archive: str,
    offset: int,
    budget: int,
    apply_style: bool = True,
) -> tuple[list[np.ndarray], list[np.ndarray], dict[str, Any]]:
    """Decode one `.grm` LOD into lists of `(N, K, 3)` RAGE Z-up point arrays and `(N,)` live counts."""
    groom = smh_groom.load(archive, offset)
    pts_list: list[np.ndarray] = []
    live_list: list[np.ndarray] = []
    meta = {
        "guides": int(len(groom.position)),
        "nodesPerGuide": int(groom.nodes_per_guide),
        "totals": [int(groom.strand_totals[0]), int(groom.strand_totals[1])],
        "shown": [0, 0],
    }

    t0, t1 = int(groom.strand_totals[0]), int(groom.strand_totals[1])
    total_all = t0 + t1
    if total_all > 0:
        for cls in (0, 1):
            tc = int(groom.strand_totals[cls])
            if tc <= 0:
                continue
            cls_budget = max(64, int(round(budget * tc / total_all)))
            try:
                s = smh_gen.strands(groom, cls, max_strands=cls_budget)
                pts = s["points"]
                live = s["live"]
                if len(pts) and apply_style:
                    st = smh_style.apply_style(
                        groom, s, cls, points_out=pts.shape[1], amount=1.0, set_index=0
                    )
                    if st["profile"] is not None:
                        keep = ~st["culled"]
                        if keep.any():
                            pts = st["points"][keep]
                            live = np.full(int(keep.sum()), pts.shape[1], dtype=np.int64)
            except Exception:
                s = smh_groom.strands(groom, tc, cls, max_strands=cls_budget)
                pts, live = s["points"], s["live"]

            if len(pts):
                pts_list.append(np.asarray(pts, dtype=np.float32))
                live_list.append(np.asarray(live, dtype=np.int64))
                meta["shown"][cls] = int(len(pts))

    # Fallback to guide curves if a tiny/legacy groom generated 0 strands
    if not pts_list and len(groom.position):
        gpos = np.asarray(groom.position, dtype=np.float32)
        if len(gpos) > budget:
            idx = np.linspace(0, len(gpos) - 1, budget, dtype=np.int64)
            gpos = gpos[idx]
        pts_list.append(gpos)
        live_list.append(np.full(len(gpos), gpos.shape[1], dtype=np.int64))
        meta["fallbackGuides"] = int(len(gpos))

    return pts_list, live_list, meta


_RIG_EYE_Y = {
    "fr1": 1.603,
    "mr1": 1.703,
    "fc1": 1.320,
    "mc1": 1.360,
}


def _is_generic_padded_head_box(hb_min: np.ndarray, hb_max: np.ndarray) -> bool:
    """Detect RAGE's shared padded drawable header box `[[-0.094,-0.134,1.507],[0.094,0.096,1.816]]`.

    Both `fr1` (female, crown ~1.72m) and `mr1` (male, crown ~1.82m) share this identical
    padded header bounding box in `inventory.sqlite3`, so aligning an `fr1` groom against
    it would falsely shift female hair/brows/lashes up by +10 cm!
    """
    h = float(hb_max[1] - hb_min[1])
    return abs(h - 0.3095) < 0.025 and abs(float(hb_max[1]) - 1.8164) < 0.02


def _compute_standing_alignment(
    pts_yup: np.ndarray,
    head_box: Sequence[Sequence[float]] | None,
    slot: str = "hair",
    *,
    groom_rig: str = "",
    target_rig: str = "",
    align_offset: Sequence[float] | None = None,
) -> tuple[float, float, float]:
    """Compute optional alignment offset from standing Y-up groom coords (`z_rage + 1.0`).

    Native-rig grooms (`fr1` on `fr1`, `mr1` on `mr1`) are already authored in exact
    pelvis-relative bone coordinates, so `(x, z_rage + 1.0, -y)` aligns hair, brows,
    eyelashes, and beards to sub-millimeter precision with `(0, 0, 0)` extra offset.

    An extra offset is applied ONLY when:
      1. Caller passes an explicit `align_offset=(dx, dy, dz)`.
      2. Cross-rig usage (`groom_rig != target_rig`, e.g. `fr1` <-> `mr1`: exact ±0.100m).
      3. Caller passes a real vertex-computed `head_box` (NOT RAGE's padded 0.31m header box)
         whose crown/eye line differs from the groom by > 5.5cm.
    """
    if align_offset is not None and len(align_offset) == 3:
        return (round(float(align_offset[0]), 5), round(float(align_offset[1]), 5), round(float(align_offset[2]), 5))

    if slot == "bodyhair" or len(pts_yup) == 0:
        return (0.0, 0.0, 0.0)

    gr = (groom_rig or "").lower().strip()
    tr = (target_rig or "").lower().strip()
    if gr and tr and gr != tr and gr in _RIG_EYE_Y and tr in _RIG_EYE_Y:
        return (0.0, round(_RIG_EYE_Y[tr] - _RIG_EYE_Y[gr], 5), 0.0)

    if head_box is None:
        return (0.0, 0.0, 0.0)
    try:
        hb_min = np.asarray(head_box[0], dtype=np.float64)
        hb_max = np.asarray(head_box[1], dtype=np.float64)
        if hb_min.shape != (3,) or hb_max.shape != (3,):
            return (0.0, 0.0, 0.0)
    except Exception:
        return (0.0, 0.0, 0.0)

    # Auto-detect if caller passed RAGE Z-up box (z in [1.3..2.0], y in [-0.2..0.2])
    if hb_max[2] > 1.0 and abs(hb_max[1]) < 0.4:
        y0, y1 = hb_min[2], hb_max[2]
        z0, z1 = -hb_max[1], -hb_min[1]
        hb_min = np.array([hb_min[0], y0, z0], dtype=np.float64)
        hb_max = np.array([hb_max[0], y1, z1], dtype=np.float64)

    # Never shift against RAGE's generic padded drawable header box
    if _is_generic_padded_head_box(hb_min, hb_max):
        return (0.0, 0.0, 0.0)

    head_cx = 0.5 * (hb_min[0] + hb_max[0])
    head_top = hb_max[1]
    head_h = max(0.15, hb_max[1] - hb_min[1])

    g_min = pts_yup.min(axis=0)
    g_max = pts_yup.max(axis=0)
    g_cx = 0.5 * (g_min[0] + g_max[0])

    dx = (head_cx - g_cx) if abs(head_cx - g_cx) > 0.02 else 0.0

    if slot == "brows":
        target_y = head_top - 0.39 * head_h
        cur_y = 0.5 * (g_min[1] + g_max[1])
        thresh = 0.055
    elif slot == "lashes":
        target_y = head_top - 0.45 * head_h
        cur_y = 0.5 * (g_min[1] + g_max[1])
        thresh = 0.055
    elif slot == "beard":
        target_y = hb_min[1] + 0.25 * head_h
        cur_y = 0.5 * (g_min[1] + g_max[1])
        thresh = 0.075
    else:
        target_y = head_top
        cur_y = g_max[1]
        thresh = 0.055

    raw_dy = float(target_y - cur_y)
    dy = raw_dy if abs(raw_dy) > thresh else 0.0
    return (round(dx, 5), round(dy, 5), 0.0)


def decode_groom_strands_for_model(
    groom_id: int | Sequence[int],
    head_box: Sequence[Sequence[float]] | None = None,
    max_strands: int = 4000,
    *,
    lod: int = 0,
    slot: str = "hair",
    groom_rig: str = "",
    target_rig: str = "",
    align_offset: Sequence[float] | None = None,
    style: bool = True,
    binary: bool = False,
    grooms_db: sqlite3.Connection | Path | str | None = None,
) -> bytes | dict[str, Any]:
    """Decode `.grm` strand groom(s) into a WebGL-ready buffer aligned to character standing space.

    Parameters
    ----------
    groom_id : int or Sequence[int]
        One groom `asset.id` from `grooms.sqlite3` or a list of sibling sub-groom IDs
        (e.g. a multi-groom hairstyle group such as Mandy's `[15865, 15866, 15867, 15868, 15869]`).
    head_box : [[xmin, ymin, zmin], [xmax, ymax, zmax]] | None
        Optional vertex-computed target head bounding box (standing Y-up; auto-converts RAGE Z-up)
        for scaled / non-standard peds. Native `fr1`/`mr1` grooms need no `head_box`.
    max_strands : int
        Total strand budget distributed proportionally across all sub-grooms (default 4000).
    lod : int
        Preferred LOD index (0 = highest detail; gracefully clamps to available LODs).
    slot : str
        Groom slot (`hair`, `brows`, `lashes`, `beard`, `bodyhair`) used for alignment heuristics.
    style : bool
        Apply RAGE 9 `smh_style` curl/wave pass (default True).
    binary : bool
        If True, returns raw compact `STR1` binary `bytes` (52-byte header + Float32 `[x,y,z,t]`
        + Uint32 `GL_LINES` indices).
        If False (default), returns a JSON-serializable dict with base64-encoded buffers,
        `vertexCount`, `indexCount`, `strandCount`, `bounds`, `alignOffset`, and `groomIds`.
    """
    if isinstance(groom_id, (list, tuple)):
        ids = [int(x) for x in groom_id]
    else:
        ids = [int(groom_id)]
    if not ids:
        raise ValueError("groom_id must contain at least one groom asset id")

    max_strands = max(64, min(60000, int(max_strands)))

    # Cache key for raw standing (+1.0m Y-up) decode BEFORE optional head_box tweak
    cache_key = f"{'_'.join(str(i) for i in ids)}.l{int(lod)}.s{max_strands}.st{int(bool(style))}"

    cached = _STRAND_MEM_CACHE.get(cache_key)
    if cached is None:
        disk_path = STRAND_CACHE_DIR / f"{cache_key}.str1"
        if disk_path.exists():
            raw_blob = disk_path.read_bytes()
            magic, vcnt, icnt, scnt, bx0, by0, bz0, bx1, by1, bz1, _ox, _oy, _oz = _BINARY_HEADER.unpack_from(
                raw_blob, 0
            )
            if magic == b"STR1":
                v_end = _BINARY_HEADER.size + vcnt * 16
                verts_f32 = np.frombuffer(raw_blob, dtype="<f4", count=vcnt * 4, offset=_BINARY_HEADER.size).reshape(
                    vcnt, 4
                ).copy()
                idx_u32 = np.frombuffer(raw_blob, dtype="<u4", count=icnt, offset=v_end).copy()
                cached = {
                    "verts": verts_f32,
                    "indices": idx_u32,
                    "strandCount": int(scnt),
                    "groomIds": ids,
                    "subMeta": [],
                }
                with _LOCK:
                    _STRAND_MEM_CACHE[cache_key] = cached
                    _STRAND_MEM_CACHE.move_to_end(cache_key)
                    while len(_STRAND_MEM_CACHE) > _STRAND_MEM_MAX:
                        _STRAND_MEM_CACHE.popitem(last=False)

    if cached is None:
        # Query LOD offsets for all requested groom IDs
        own_conn = False
        if isinstance(grooms_db, sqlite3.Connection):
            db = grooms_db
            db.row_factory = sqlite3.Row
        else:
            db_path = Path(grooms_db) if grooms_db else GROOMS_DB
            db = sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True)
            db.row_factory = sqlite3.Row
            own_conn = True

        try:
            targets = []
            for gid in ids:
                asset = db.execute("SELECT id, archive, strands0, strands1 FROM asset WHERE id=?", (gid,)).fetchone()
                if not asset:
                    raise LookupError(f"Unknown groom asset id: {gid}")
                lods = db.execute("SELECT lod, archive, offset FROM lod WHERE asset=? ORDER BY lod", (gid,)).fetchall()
                if not lods:
                    raise LookupError(f"Groom {gid} has no LOD rows")
                pick_lod = lods[min(max(0, int(lod)), len(lods) - 1)]
                targets.append({
                    "id": gid,
                    "archive": pick_lod["archive"],
                    "offset": int(pick_lod["offset"]),
                    "weight": max(1, int(asset["strands0"] or 0) + int(asset["strands1"] or 0)),
                })
        finally:
            if own_conn:
                db.close()

        total_weight = sum(t["weight"] for t in targets)
        all_verts_list: list[np.ndarray] = []
        all_idx_list: list[np.ndarray] = []
        v_cursor = 0
        strand_total = 0
        sub_meta = []

        for t in targets:
            sub_budget = max(64, int(round(max_strands * t["weight"] / total_weight)))
            pts_chunks, live_chunks, sm = _generate_groom_polylines(
                t["archive"], t["offset"], sub_budget, apply_style=style
            )
            sub_meta.append({"id": t["id"], **sm})

            for pts_rage, live in zip(pts_chunks, live_chunks):
                n, k, _ = pts_rage.shape
                if n == 0 or k < 2:
                    continue
                # Transform RAGE Z-up pelvis -> standing glTF Y-up: (x, z + 1.0, -y)
                pts_yup = np.empty((n, k, 3), dtype=np.float32)
                pts_yup[..., 0] = pts_rage[..., 0]
                pts_yup[..., 1] = pts_rage[..., 2] + 1.0
                pts_yup[..., 2] = -pts_rage[..., 1]

                # Compute root-to-tip parameter t in [0.0, 1.0] per vertex using each strand's live count
                denom = np.maximum(live - 1, 1).astype(np.float32)[:, None]
                t_param = np.clip(np.arange(k, dtype=np.float32)[None, :] / denom, 0.0, 1.0)

                verts = np.empty((n, k, 4), dtype=np.float32)
                verts[..., :3] = pts_yup
                verts[..., 3] = t_param

                # Build GL_LINES segment indices only for live nodes
                seg_mask = np.arange(k - 1)[None, :] < (live[:, None] - 1)
                a = (
                    np.arange(n, dtype=np.uint32)[:, None] * np.uint32(k)
                    + np.arange(k - 1, dtype=np.uint32)[None, :]
                )[seg_mask] + np.uint32(v_cursor)
                if len(a) == 0:
                    continue
                seg_idx = np.empty(len(a) * 2, dtype=np.uint32)
                seg_idx[0::2] = a
                seg_idx[1::2] = a + np.uint32(1)

                all_verts_list.append(verts.reshape(-1, 4))
                all_idx_list.append(seg_idx)
                v_cursor += n * k
                strand_total += n

        if all_verts_list:
            verts_f32 = np.concatenate(all_verts_list, axis=0)
            idx_u32 = np.concatenate(all_idx_list, axis=0)
        else:
            verts_f32 = np.zeros((0, 4), dtype=np.float32)
            idx_u32 = np.zeros((0,), dtype=np.uint32)

        cached = {
            "verts": verts_f32,
            "indices": idx_u32,
            "strandCount": int(strand_total),
            "groomIds": ids,
            "subMeta": sub_meta,
        }

        # Persist raw standing buffer to disk cache
        try:
            STRAND_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            if len(verts_f32):
                bmin = verts_f32[:, :3].min(axis=0).tolist()
                bmax = verts_f32[:, :3].max(axis=0).tolist()
            else:
                bmin, bmax = [0.0, 0.0, 0.0], [0.0, 0.0, 0.0]
            hdr = _BINARY_HEADER.pack(
                b"STR1",
                int(len(verts_f32)),
                int(len(idx_u32)),
                int(strand_total),
                bmin[0], bmin[1], bmin[2],
                bmax[0], bmax[1], bmax[2],
                0.0, 0.0, 0.0,
            )
            disk_path = STRAND_CACHE_DIR / f"{cache_key}.str1"
            tmp = disk_path.with_suffix(".tmp")
            tmp.write_bytes(hdr + verts_f32.astype("<f4", copy=False).tobytes() + idx_u32.astype("<u4", copy=False).tobytes())
            tmp.replace(disk_path)
        except Exception:
            pass

        with _LOCK:
            _STRAND_MEM_CACHE[cache_key] = cached
            _STRAND_MEM_CACHE.move_to_end(cache_key)
            while len(_STRAND_MEM_CACHE) > _STRAND_MEM_MAX:
                _STRAND_MEM_CACHE.popitem(last=False)

    verts_out = cached["verts"]
    idx_out = cached["indices"]
    align_off = _compute_standing_alignment(
        verts_out[:, :3],
        head_box,
        slot=slot,
        groom_rig=groom_rig,
        target_rig=target_rig,
        align_offset=align_offset,
    )
    if any(abs(v) > 1e-6 for v in align_off) and len(verts_out):
        verts_out = verts_out.copy()
        verts_out[:, 0] += np.float32(align_off[0])
        verts_out[:, 1] += np.float32(align_off[1])
        verts_out[:, 2] += np.float32(align_off[2])

    if len(verts_out):
        bmin = [round(float(x), 5) for x in verts_out[:, :3].min(axis=0)]
        bmax = [round(float(x), 5) for x in verts_out[:, :3].max(axis=0)]
    else:
        bmin, bmax = [0.0, 0.0, 0.0], [0.0, 0.0, 0.0]

    v_bytes = verts_out.astype("<f4", copy=False).tobytes()
    i_bytes = idx_out.astype("<u4", copy=False).tobytes()

    if binary:
        hdr = _BINARY_HEADER.pack(
            b"STR1",
            int(len(verts_out)),
            int(len(idx_out)),
            int(cached["strandCount"]),
            bmin[0], bmin[1], bmin[2],
            bmax[0], bmax[1], bmax[2],
            align_off[0], align_off[1], align_off[2],
        )
        return hdr + v_bytes + i_bytes

    return {
        "format": "STR1",
        "groomIds": list(cached["groomIds"]),
        "slot": slot,
        "vertexCount": int(len(verts_out)),
        "indexCount": int(len(idx_out)),
        "segmentCount": int(len(idx_out) // 2),
        "strandCount": int(cached["strandCount"]),
        "bounds": [bmin, bmax],
        "alignOffset": list(align_off),
        "vertexLayout": "float32x4(x,y,z,t_root_to_tip)",
        "indexLayout": "uint32_gl_lines",
        "positionsT_f32_b64": base64.b64encode(v_bytes).decode("ascii"),
        "indices_u32_b64": base64.b64encode(i_bytes).decode("ascii"),
        "byteSize": len(v_bytes) + len(i_bytes),
    }


__all__ = [
    "hair_color_palette",
    "hair_color_by_id",
    "hair_colors_from_game",
    "GROOM_CATEGORY_HASHES",
    "GROOM_SLOTS",
    "PALETTE_HASH_HAIR_PAIRED",
    "PALETTE_HASH_MASTER",
    "STORY_CHARACTER_HAIR_COLORS",
    "load_rage_palettes",
    "resolve_palette_color",
    "nearest_hair_color_preset",
    "infer_default_hair_color",
    "parse_groom_drawable",
    "resolve_drawable_to_groom",
    "read_0drp_bytes",
    "extract_drp_groom_data",
    "extract_ped_groom_data",
    "groom_catalog_for_rig",
    "decode_groom_strands_for_model",
]
