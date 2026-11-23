# SPDX-License-Identifier: GPL-3.0-or-later
"""Per-character named outfits, per-garment material/tint overrides, and eye texture resolution.

Decodes RAGE 9 / GTA VI character outfit & eye appearance data from:
- `INDEX/packs.rpf/00011cfbf000.pso` (root `0x890aca98`):
  Maps each ped archetype hash -> ordered list of `(slot_hash, outfit_hash, is_random_spawn, weight)`.
- `packs.rpf` compiled `0DRP` ped catalogues (2,709 files, root `0x8dcb4cf5`) and the global
  wardrobe catalogue `INDEX/packs.rpf/00011ccb2000.pso`:
  - `0xa9d57751` (80 B): outfit definitions (`outfit_id`, `parent_outfit_id`, array of `0x67e54cb2`
    `(ref_id, state_id)` pairs supporting recursive sub-outfits & inheritance).
  - `0xf795e8ba` (88 B): part presets (`pid`, `parent_id`, `category`, `u28` tint palette hash e.g.
    `0xa7c8d8e0` = `ped_tint_generic_clean`, `b32..b34` legacy tint row indices 0..254 [255=unset],
    `u36` MUV 8-slot garment preset hash -> `data.rpf/00001b95b000.pso` / `clothing_presets.sqlite3`,
    plus tags & state array).
  - `0x37e52277` (184 B): per-state drawable/material overrides (`state_id`, drawable `murmur64` h0,
    `albedo` hash, `normal` hash, `h3` aux hash, state tags).
- `INDEX/packs.rpf/*.pso` (`0x1edee47e` variation PSOs, 80 files):
  Provides exact average RGB colors (`0x28689f25 +80`) for 11,625 texture hashes as tint fallback
  on legacy non-MUV garments.
- `LIBRARY/textures.sqlite3` & `LIBRARY/clothing_presets.sqlite3`:
  Verifies decodable sRGB eye & garment textures and resolves MUV preset swatches/labels.

Standalone module — Wave 2 wires these helpers into `ped_archetypes.py`, `model_service.py`,
`glb_textured.py`, and the viewer UI.
"""

from __future__ import annotations

import collections
from concurrent.futures import ThreadPoolExecutor
import glob
import json
from pathlib import Path
import re
import sqlite3
import struct
import threading
import zlib


from decode_pso import Decoder, sections_of  # noqa: E402
from face_presets import (  # noqa: E402
    CATALOG_ROOT,
    DRAWABLE_TAGS,
    HEAD_CATEGORY,
    PACKS_RPF,
    PACKS_SPANS,
    PRESETS_PSO,
    parse_0drp_blocks,
    ptr,
    read_tags,
)
from script_symbol_names import murmur64  # noqa: E402
from script_tree import _name_hash  # noqa: E402
from paths import INDEX, LIBRARY, ENGINE

# Paths --------------------------------------------------------------------------------------------

OUTFITS_PSO = INDEX / "packs.rpf/00011cfbf000.pso"
PACKS_PSO_DIR = INDEX / "packs.rpf"
INVENTORY_DB = LIBRARY / "inventory.sqlite3"
TEXTURES_DB = LIBRARY / "textures.sqlite3"
CLOTHING_PRESETS_DB = LIBRARY / "clothing_presets.sqlite3"
PED_ARCH_DB = LIBRARY / "ped_archetypes.sqlite3"

# Category & state constants -----------------------------------------------------------------------

EYES_CATEGORY = 0xEA24B45E
TEETH_CATEGORY = 0x96EDAE5C
HAND_CATEGORY = 0x3817131D
FEET_CATEGORY = 0x44925BFD
HAIR_CATEGORY = 0x864B03AE
BROWS_CATEGORY = 0x7C50AD43
LASHES_CATEGORY = 0x83A664E7
FINGERNAILS_CATEGORY = 0xB7F3ECA1
TOENAILS_CATEGORY = 0xC9469A2B
DEFAULT_STATE_ID = 0x44E21C90

SKIN_CATEGORIES = {
    HAND_CATEGORY,
    FEET_CATEGORY,
    0x9D283797,
    0xC8BC4E01,
    0x2A7A13A4,
}
NON_GARMENT_CATEGORIES = SKIN_CATEGORIES | {
    HEAD_CATEGORY,
    EYES_CATEGORY,
    TEETH_CATEGORY,
    HAIR_CATEGORY,
    BROWS_CATEGORY,
    LASHES_CATEGORY,
    FINGERNAILS_CATEGORY,
    TOENAILS_CATEGORY,
}

# Blank / decal placeholder hashes that must never be emitted as garment or eye albedos
PLACEHOLDER_TEXTURES = {
    "",
    "0000000000000000",
    "2e67967223889f9d",  # 512x512 BC1_UNORM all-black MUV blank decal placeholder
    "d2288f458ac8c7e2",  # shared blank skin/clothing placeholder
    "1e29578a23d839f3",  # shared blank placeholder
}

SKIP_GARMENT_PREFIXES = (
    "hair", "beard", "brows", "lashes", "armpithair", "bodyhair", "chesthair",
    "genitalhair", "leghair", "stubble", "eyecap", "scextrahiddenpoly", "teeth",
    "hand_", "feet_", "head_", "bodygenitals", "bodynipples", "fingernails",
    "toenails", "invisible_",
)
HAIR_PREFIXES = ("hair", "beard")
NAIL_PREFIXES = ("fingernails", "toenails")

# Base female/male eye presets in 00011ccb2000.pso (child overrides take priority over these)
BASE_EYE_PRESET_IDS = {0xC850C527, 0xE2A58B99}

# Known cracked legacy tint palette names (0xf795e8ba +28)
KNOWN_TINT_PALETTES = {
    0xA7C8D8E0: "ped_tint_generic_clean",
}

# Verified 512x512 BC7_UNORM_SRGB human iris textures in textures.sqlite3 with measured iris RGB
# Ordered by usage frequency across the 2,714 GTA VI ped archetypes.
VERIFIED_EYE_PRESETS = (
    {
        "hash": "047a0ccd9ec53d59",
        "label": "Medium Brown",
        "hex": "#574431",
        "rigs": ("fr1", "mr1", "mb1", "mo1"),
        "count": 945,
    },
    {
        "hash": "794f6d1c8c269f94",
        "label": "Dark Brown",
        "hex": "#52331a",
        "rigs": ("fr1", "mr1", "fc1", "fo1", "mc1", "mo1"),
        "count": 815,
    },
    {
        "hash": "307eedd0f1687fbc",
        "label": "Warm Brown / Hazel",
        "hex": "#645039",
        "rigs": ("fr1", "mr1", "fo1", "mi1", "mk1"),
        "count": 466,
    },
    {
        "hash": "bee188857a5fbf11",
        "label": "Light Brown / Amber",
        "hex": "#7d6247",
        "rigs": ("fr1", "mr1", "mb1"),
        "count": 101,
    },
    {
        "hash": "429453b7a2eb9980",
        "label": "Warm Brown (Lucia)",
        "hex": "#64503a",
        "rigs": ("fr1", "mr1"),
        "count": 24,
    },
    {
        "hash": "39e23c8d94a8e2a1",
        "label": "Blue-Grey",
        "hex": "#606c6a",
        "rigs": ("fr1", "mr1", "fb1", "mb1", "mo1"),
        "count": 21,
    },
    {
        "hash": "1955d5c1bb8a433b",
        "label": "Slate Grey",
        "hex": "#616865",
        "rigs": ("fr1", "mr1"),
        "count": 13,
    },
    {
        "hash": "8bd44f831a9c0af5",
        "label": "Deep Espresso",
        "hex": "#3d2e20",
        "rigs": ("fr1", "mr1"),
        "count": 13,
    },
    {
        "hash": "f4840ef42714d070",
        "label": "Steel Blue",
        "hex": "#5b6a76",
        "rigs": ("fr1", "mr1"),
        "count": 8,
    },
    {
        "hash": "242b967b64e4e425",
        "label": "Grey-Green / Hazel (Jason)",
        "hex": "#61645c",
        "rigs": ("mr1", "mb1", "fr1"),
        "count": 6,
    },
    {
        "hash": "84a890b1c4906038",
        "label": "Ice Blue-Grey",
        "hex": "#646b70",
        "rigs": ("mr1", "fr1"),
        "count": 4,
    },
    {
        "hash": "acfb3becebe4ffa9",
        "label": "Green",
        "hex": "#5d7062",
        "rigs": ("fr1", "mr1"),
        "count": 2,
    },
    {
        "hash": "8328e89d3833a52b",
        "label": "Muted Grey",
        "hex": "#5b5d58",
        "rigs": ("fr1", "mr1"),
        "count": 2,
    },
    {
        "hash": "306fbb5e62012af7",
        "label": "Chestnut Brown",
        "hex": "#523924",
        "rigs": ("mr1", "fr1"),
        "count": 1,
    },
    {
        "hash": "1bd1dc1127e153bb",
        "label": "Bloodshot Amber",
        "hex": "#845c4d",
        "rigs": ("mr1", "fr1"),
        "count": 1,
    },
)
VERIFIED_EYE_HASHES = {e["hash"] for e in VERIFIED_EYE_PRESETS}

# Garment naming & slot classification -------------------------------------------------------------

_GARMENT_LABELS = (
    ("bracelet", ("Bracelet", 5, "Accessory")),
    ("brabikini", ("Bikini Top", 1, "Top")),
    ("brasports", ("Sports Bra", 1, "Top")),
    ("brastrapless", ("Strapless Bra", 1, "Top")),
    ("brabalcony", ("Balcony Bra", 1, "Top")),
    ("brafullcup", ("Bra", 1, "Top")),
    ("bra", ("Bra", 1, "Top")),
    ("undiesgstring", ("G-String", 2, "Bottom")),
    ("undiesswimming", ("Swim Bottoms", 2, "Bottom")),
    ("undiesbikini", ("Bikini Bottoms", 2, "Bottom")),
    ("undiesboxers", ("Boxers", 2, "Bottom")),
    ("undiesbriefs", ("Briefs", 2, "Bottom")),
    ("undies", ("Underwear", 2, "Bottom")),
    ("lingeriebodysuit", ("Bodysuit", 0, "Fullbody")),
    ("fullbodyromper", ("Romper", 0, "Fullbody")),
    ("fullbodyjumpsuit", ("Jumpsuit", 0, "Fullbody")),
    ("fullbodyswim", ("Swimsuit", 0, "Fullbody")),
    ("fullbody", ("Fullbody", 0, "Fullbody")),
    ("dress", ("Dress", 0, "Fullbody")),
    ("hoodiezip", ("Zip Hoodie", 1, "Top")),
    ("hoodie", ("Hoodie", 1, "Top")),
    ("sweatshirt", ("Sweatshirt", 1, "Top")),
    ("jacketbomber", ("Bomber Jacket", 1, "Outerwear")),
    ("jackettrack", ("Track Jacket", 1, "Outerwear")),
    ("jacketcardigan", ("Cardigan", 1, "Outerwear")),
    ("jacket", ("Jacket", 1, "Outerwear")),
    ("coat", ("Coat", 1, "Outerwear")),
    ("vest", ("Vest", 1, "Outerwear")),
    ("blazer", ("Blazer", 1, "Outerwear")),
    ("cardigan", ("Cardigan", 1, "Outerwear")),
    ("tanktopcropped", ("Cropped Tank", 1, "Top")),
    ("tanktop", ("Tank Top", 1, "Top")),
    ("tshirtcrop", ("Crop Tee", 1, "Top")),
    ("tshirt", ("T-Shirt", 1, "Top")),
    ("shirtcrop", ("Crop Shirt", 1, "Top")),
    ("shirtcuban", ("Cuban Shirt", 1, "Top")),
    ("shirtpolo", ("Polo", 1, "Top")),
    ("shirt", ("Shirt", 1, "Top")),
    ("basetops", ("Sports Top", 1, "Top")),
    ("top", ("Top", 1, "Top")),
    ("shortsjeans", ("Jean Shorts", 2, "Bottom")),
    ("shortstrack", ("Track Shorts", 2, "Bottom")),
    ("shortsswim", ("Swim Shorts", 2, "Bottom")),
    ("shortstactical", ("Cargo Shorts", 2, "Bottom")),
    ("shortscargo", ("Cargo Shorts", 2, "Bottom")),
    ("shortswork", ("Work Shorts", 2, "Bottom")),
    ("shorts", ("Shorts", 2, "Bottom")),
    ("basebottoms", ("Leggings", 2, "Bottom")),
    ("pantsleggings", ("Leggings", 2, "Bottom")),
    ("pantsjeans", ("Jeans", 2, "Bottom")),
    ("pantstrack", ("Track Pants", 2, "Bottom")),
    ("pantscargo", ("Cargo Pants", 2, "Bottom")),
    ("pantsdress", ("Dress Pants", 2, "Bottom")),
    ("pants", ("Pants", 2, "Bottom")),
    ("skirt", ("Skirt", 2, "Bottom")),
    ("shoesflipflops", ("Flip-Flops", 3, "Footwear")),
    ("shoessneakers", ("Sneakers", 3, "Footwear")),
    ("shoesheels", ("Heels", 3, "Footwear")),
    ("shoesboots", ("Boots", 3, "Footwear")),
    ("shoesloafers", ("Loafers", 3, "Footwear")),
    ("shoessandals", ("Sandals", 3, "Footwear")),
    ("shoescanvas", ("Sneakers", 3, "Footwear")),
    ("shoes", ("Shoes", 3, "Footwear")),
    ("boots", ("Boots", 3, "Footwear")),
    ("socks", ("Socks", 4, "Footwear")),
    ("shadesaviators", ("Aviators", 4, "Accessory")),
    ("shades", ("Sunglasses", 4, "Accessory")),
    ("specs", ("Glasses", 4, "Accessory")),
    ("glasses", ("Glasses", 4, "Accessory")),
    ("hatcap", ("Cap", 4, "Accessory")),
    ("hatbucket", ("Bucket Hat", 4, "Accessory")),
    ("hat", ("Hat", 4, "Accessory")),
    ("watch", ("Watch", 5, "Accessory")),
    ("necklace", ("Necklace", 5, "Accessory")),
    ("rings", ("Rings", 5, "Accessory")),
    ("piercings", ("Piercings", 5, "Accessory")),
    ("earring", ("Earrings", 5, "Accessory")),
    ("belt", ("Belt", 5, "Accessory")),
    ("bag", ("Bag", 5, "Accessory")),
    ("backpack", ("Backpack", 5, "Accessory")),
    ("gloves", ("Gloves", 5, "Accessory")),
)

_CHAR_PREFIX_RE = re.compile(
    r"^(?:player_story\d+_\d+|cs_[a-z0-9_]+?|hc_cs_[a-z0-9_]+?|hc_[a-z0-9_]+?|u_[mf]_[a-z0-9_]+?|sc_[a-z0-9_]+?|s\d+)_"
)
_GENERIC_SLOT_RE = re.compile(r"^(?:outfit|out|var|look|set)_\d+$", re.I)


def _classify_garment(drawable_name: str) -> tuple[str, int, str]:
    """Return (short_label, headline_priority, slot_category) for a garment drawable."""
    nl = drawable_name.lower()
    # Strip character prefix like player_story000_00_ / cs_mandy_ / s01_
    stripped = _CHAR_PREFIX_RE.sub("", nl)
    for token in (stripped, nl):
        for pfx, info in _GARMENT_LABELS:
            if token.startswith(pfx):
                return info
    return ("Garment", 6, "Clothing")


# Feature #2: Per-Character Eye Textures -----------------------------------------------------------

def resolve_eye_texture(raw_hash: str | int | None, tex_info: dict | None) -> str:
    """Verify a raw eye texture hash is a decodable sRGB texture in `textures.sqlite3`.

    Accepts either a 16-char hex string or integer murmur64 hash.
    `tex_info` maps `{hash_hex: (width, height, format, ...)}` (or `{int_hash: ...}`).
    Returns normalized 16-char lowercase hex hash, or `""` if invalid / missing / non-sRGB.
    """
    if raw_hash is None:
        return ""
    if isinstance(raw_hash, int):
        if raw_hash <= 0:
            return ""
        h = f"{raw_hash:016x}"
    else:
        h = str(raw_hash).strip().lower().removeprefix("0x")
        if not h:
            return ""
        if len(h) < 16:
            h = h.zfill(16)
    if h in PLACEHOLDER_TEXTURES:
        return ""
    if tex_info is None:
        return h if h in VERIFIED_EYE_HASHES else ""

    meta = tex_info.get(h)
    if meta is None:
        try:
            meta = tex_info.get(int(h, 16))
        except ValueError:
            meta = None
    if not meta:
        return ""
    # meta is typically (width, height, format) or dict
    if isinstance(meta, dict):
        w = int(meta.get("width") or 0)
        hh = int(meta.get("height") or 0)
        fmt = str(meta.get("format") or "").upper()
    else:
        w = int(meta[0] or 0)
        hh = int(meta[1] or 0)
        fmt = str(meta[2] or "").upper()
    # Reject linear masks (e.g. BC4_UNORM demon eye mask 052d86792cab8a72) and tiny proxies
    if "SRGB" not in fmt or w < 128 or hh < 128:
        return ""
    return h


def eye_presets_for_rig(rig: str | None = None, tex_info: dict | None = None) -> list[dict]:
    """List verified decodable iris presets available for a body rig (e.g. `'fr1'`, `'mr1'`).

    All 15 verified 512x512 BC7_UNORM_SRGB iris textures work on both female (`fr1`/`fo1`/`fb1`)
    and male (`mr1`/`mo1`/`mb1`) eye meshes because GTA VI shares the standard eye UV layout;
    presets native to the requested rig are prioritized first.
    """
    rig_l = (rig or "").strip().lower()
    out = []
    for entry in VERIFIED_EYE_PRESETS:
        h = entry["hash"]
        if tex_info is not None and not resolve_eye_texture(h, tex_info):
            continue
        is_rig_match = (not rig_l) or (rig_l in entry["rigs"])
        out.append({
            "hash": h,
            "label": entry["label"],
            "hex": entry["hex"],
            "count": entry["count"],
            "rigNative": is_rig_match,
        })
    out.sort(key=lambda x: (0 if x["rigNative"] else 1, -x["count"], x["label"]))
    return out


# Feature #3: Outfit Definitions & Catalogue Parsers -----------------------------------------------

def _build_builtin_slot_names(slot_hashes: set[int]) -> dict[int, str]:
    """Crack common `outfit_XX` / semantic slot names from fast local dictionaries (no RPF scan)."""
    found: dict[int, str] = {}

    def _try(word: str):
        wl = word.lower()
        h = _name_hash(wl) & 0xFFFFFFFF
        if h in slot_hashes and h not in found:
            found[h] = wl

    for n in range(0, 128):
        for pfx in ("outfit_", "outfit", "out_", "var_", "look_", "set_", "default_"):
            _try(f"{pfx}{n}")
            _try(f"{pfx}{n:02d}")
            _try(f"{pfx}{n:03d}")

    for base in (
        "default", "casual", "formal", "swim", "swimwear", "bikini", "beach",
        "club", "party", "workout", "gym", "sport", "sports", "sleep", "pajamas",
        "home", "lounge", "work", "uniform", "winter", "summer", "rain", "cold",
        "hot", "night", "day", "street", "fancy", "dress", "suit", "tux", "naked",
        "nude", "underwear", "lingerie", "robbery", "heist", "prison", "inmate",
        "hospital", "gown", "towel", "robe", "motel", "pool", "boat", "yacht",
        "biker", "cop", "police", "guard", "security", "base", "empty", "main",
        "prologue", "intro", "finale", "epilogue", "mission", "cutscene", "freeroam",
    ):
        _try(base)
        for n in range(1, 25):
            _try(f"{base}_{n}")
            _try(f"{base}_{n:02d}")
            _try(f"outfit_{base}_{n:02d}")
            _try(f"outfit_{base}")

    tok_re = re.compile(rb"[A-Za-z][A-Za-z0-9_]{1,64}")
    for jp in (
        LIBRARY / "names.json",              # learned from the game's own files by the names stage (names.py)
        ENGINE / "script_symbol_names.json",
    ):
        if jp.is_file():
            for m in tok_re.finditer(jp.read_bytes()):
                _try(m.group(0).decode("ascii"))
    return found


def load_outfit_definitions(outfits_pso: Path = OUTFITS_PSO, crack_names: bool = True) -> dict:
    """Parse `packs.rpf/00011cfbf000.pso` into per-archetype structured outfit lists.

    Returns:
        {
            "arch_to_outfits": {
                arch_hash_u32: [
                    {"slot_hash": u32, "outfit_hash": u32, "slot_name": str,
                     "is_random_spawn": bool, "weight": float},
                    ...
                ]
            },
            "outfit_to_arch": {outfit_hash_u32: arch_hash_u32},
            "slot_names": {slot_hash_u32: str},
        }
    """
    path = Path(outfits_pso)
    if not path.is_file():
        return {"arch_to_outfits": {}, "outfit_to_arch": {}, "slot_names": {}}

    s, _ = sections_of(path.read_bytes())
    dec = Decoder(s, {})
    blks = {i + 1: (k, data) for i, (k, data, *_) in enumerate(dec.blocks)}
    b1 = blks[1][1]

    raw_arch: dict[int, list[tuple[int, int, bool, float]]] = {}
    outfit_to_arch: dict[int, int] = {}
    all_slot_h: set[int] = set()

    for i in range(len(b1) // 24):
        arch_h, cnt, _cap, p = struct.unpack_from("<IxxxxIIQ", b1, i * 24)
        if not arch_h or not cnt:
            continue
        tb, to = ptr(p)
        if tb not in blks:
            continue
        data = blks[tb][1]
        entries = []
        for j in range(cnt):
            if to + (j + 1) * 32 > len(data):
                break
            slot_h, out_h, _acnt, _acap, _aptr, b24, weight = struct.unpack_from(
                "<IIIIQIf", data, to + j * 32
            )
            if out_h:
                all_slot_h.add(slot_h)
                entries.append((slot_h, out_h, bool(b24), round(float(weight), 4)))
                outfit_to_arch[out_h] = arch_h
        if entries:
            raw_arch[arch_h] = entries

    slot_names = _build_builtin_slot_names(all_slot_h) if crack_names else {}

    arch_to_outfits: dict[int, list[dict]] = {}
    for arch_h, entries in raw_arch.items():
        arch_to_outfits[arch_h] = [
            {
                "slot_hash": slot_h,
                "outfit_hash": out_h,
                "slot_name": slot_names.get(slot_h, ""),
                "is_random_spawn": is_rand,
                "weight": weight,
            }
            for slot_h, out_h, is_rand, weight in entries
        ]

    return {
        "arch_to_outfits": arch_to_outfits,
        "outfit_to_arch": outfit_to_arch,
        "slot_names": slot_names,
    }


def parse_drp_catalogue(blocks: dict) -> tuple[dict, dict]:
    """Parse `(presets_by_id, outfits_by_id)` from a `0DRP` or `00011ccb2000.pso` block dict.

    `presets_by_id[pid]`:
        `(parent_id, category, tint_palette_u28, (b32, b33, b34, b35), muv_u36, tags, states)`
        where `states[state_id] = (h0_m64, alb_m64, nrm_m64, h3_m64, stags)`.
    `outfits_by_id[oid]`:
        `(parent_outfit_id, [(ref_id, state_id), ...])`.
    """
    presets: dict[int, tuple] = {}
    outfits: dict[int, tuple] = {}

    for _bn, (bk, data) in blocks.items():
        if bk == 0xF795E8BA:
            for idx in range(len(data) // 88):
                r = data[idx * 88:(idx + 1) * 88]
                pid, par, c1, _u12, p1, cat, u28 = struct.unpack_from("<IIIIQII", r, 0)
                b32, b33, b34, b35 = struct.unpack_from("<BBBB", r, 32)
                u36 = struct.unpack_from("<I", r, 36)[0]
                p2 = struct.unpack_from("<Q", r, 48)[0]
                c2 = struct.unpack_from("<H", r, 56)[0]
                tags = read_tags(blocks, p2, c2) if c2 else {}
                rb, ro = ptr(p1)
                states: dict[int, tuple] = {}
                if c1 and rb in blocks and blocks[rb][0] == 0x37E52277:
                    rdata = blocks[rb][1]
                    for si in range(c1):
                        if ro + (si + 1) * 184 > len(rdata):
                            break
                        sid = struct.unpack_from("<I", rdata, ro + si * 184)[0]
                        h0, alb, nrm, h3 = struct.unpack_from("<QQQQ", rdata, ro + si * 184 + 8)
                        sp2 = struct.unpack_from("<Q", rdata, ro + si * 184 + 168)[0]
                        sc2 = struct.unpack_from("<H", rdata, ro + si * 184 + 176)[0]
                        stags = read_tags(blocks, sp2, sc2) if sc2 else {}
                        states[sid] = (h0, alb, nrm, h3, stags)
                presets[pid] = (par, cat, u28, (b32, b33, b34, b35), u36, tags, states)
        elif bk == 0xA9D57751:
            for idx in range(len(data) // 80):
                r = data[idx * 80:(idx + 1) * 80]
                oid, opar = struct.unpack_from("<II", r, 0)
                ecnt, _ecap, eptr = struct.unpack_from("<IIQ", r, 56)
                eb, eo = ptr(eptr)
                refs = []
                if ecnt and eb in blocks and blocks[eb][0] == 0x67E54CB2:
                    edata = blocks[eb][1]
                    for ei in range(ecnt):
                        if eo + (ei + 1) * 8 > len(edata):
                            break
                        refs.append(struct.unpack_from("<II", edata, eo + ei * 8))
                outfits[oid] = (opar, refs)

    return presets, outfits


def load_global_catalogue(presets_pso: Path = PRESETS_PSO) -> tuple[dict, dict]:
    """Load the shared global wardrobe catalogue `packs.rpf/00011ccb2000.pso`."""
    path = Path(presets_pso)
    if not path.is_file():
        return {}, {}
    s, _ = sections_of(path.read_bytes())
    dec = Decoder(s, {})
    blks = {i + 1: (k, data) for i, (k, data, *_) in enumerate(dec.blocks)}
    return parse_drp_catalogue(blks)


def load_muv_metadata(clothing_db: Path = CLOTHING_PRESETS_DB) -> dict[int, dict]:
    """Load `{preset_id_u32: {hex, label, swatches, hasPattern, primaryColor}}` from `clothing_presets.sqlite3`."""
    path = Path(clothing_db)
    if not path.is_file():
        return {}
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    out: dict[int, dict] = {}
    try:
        for pid, phex, lbl, sw_s, has_pat in conn.execute(
            "SELECT preset_id, preset_hex, label, swatches, has_pattern FROM muv_preset"
        ):
            sw = json.loads(sw_s) if sw_s else []
            out[int(pid)] = {
                "hex": phex,
                "label": lbl or "",
                "swatches": sw,
                "hasPattern": bool(has_pat),
                "primaryColor": sw[0] if sw else "",
            }
    finally:
        conn.close()
    return out


def load_all_variation_rgb(packs_pso_dir: Path = PACKS_PSO_DIR) -> dict[str, tuple[int, int, int]]:
    """Load `{tex_hash_hex: (r, g, b)}` from all 80 `0x1edee47e` variation PSOs in `packs.rpf`."""
    pdir = Path(packs_pso_dir)
    if not pdir.is_dir():
        return {}
    needle = struct.pack("<I", 0x1EDEE47E)
    out: dict[str, tuple[int, int, int]] = {}
    for p in sorted(glob.glob(str(pdir / "*.pso"))):
        b = Path(p).read_bytes()
        if needle not in b:
            continue
        try:
            s, _ = sections_of(b)
            dec = Decoder(s, {})
        except Exception:
            continue
        if dec.blocks[dec.root - 1][0] != 0x1EDEE47E:
            continue
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
                        r, g, bb, _a = struct.unpack_from("<BBBB", slot, 88)
                        if r or g or bb:
                            out[f"{k64:016x}"] = (r, g, bb)
    return out


def build_drawable_lookup(inv_by_name) -> tuple[dict[int, list[str]], dict[int, list[str]]]:
    """Build `(j32_models, m64_models)` hash maps from an iterable/dict of drawable names."""
    j32_models: dict[int, list[str]] = collections.defaultdict(list)
    m64_models: dict[int, list[str]] = collections.defaultdict(list)
    if isinstance(inv_by_name, dict):
        # Preserve canonical casing if values are dicts with "name", else use keys
        names = [
            v["name"] if isinstance(v, dict) and v.get("name") else str(k)
            for k, v in inv_by_name.items()
        ]
    else:
        names = [str(n) for n in inv_by_name if n]
    for nm in names:
        j32_models[_name_hash(nm) & 0xFFFFFFFF].append(nm)
        m64_models[murmur64(nm.encode())].append(nm)
    return j32_models, m64_models


# Core Per-Ped Outfit & Override Extraction --------------------------------------------------------

def _resolve_outfit_tree(
    oid: int,
    local_outfits: dict,
    global_outfits: dict,
    depth: int = 0,
    seen: set[int] | None = None,
) -> list[tuple[int, int, int]]:
    """Flatten an outfit's recursive sub-outfit & `parent_outfit_id` tree into `[(pid, sid, depth)]`.

    Parent/sub-outfit references appear before direct child references so direct entries override
    inherited ones when deduplicating by category or drawable.
    """
    if seen is None:
        seen = set()
    if not oid or oid in seen or depth > 6:
        return []
    seen.add(oid)
    rec = local_outfits.get(oid) or global_outfits.get(oid)
    if not rec:
        return []
    opar, refs = rec
    flat: list[tuple[int, int, int]] = []
    if opar:
        flat.extend(_resolve_outfit_tree(opar, local_outfits, global_outfits, depth + 1, seen))
    for ref_id, state_id in refs:
        if ref_id in local_outfits or ref_id in global_outfits:
            flat.extend(_resolve_outfit_tree(ref_id, local_outfits, global_outfits, depth + 1, seen))
        else:
            flat.append((ref_id, state_id, depth))
    return flat


def _resolve_preset_chain(
    pid: int,
    sid: int,
    local_presets: dict,
    global_presets: dict,
    j32_models: dict,
    m64_models: dict,
) -> dict | None:
    """Resolve a `(pid, sid)` preset walking `parent_id` across local `0DRP` -> global catalogue."""
    chain = []
    cur = pid
    seen: set[int] = set()
    while cur and cur not in seen and len(chain) < 10:
        seen.add(cur)
        p = local_presets.get(cur) or global_presets.get(cur)
        if not p:
            break
        chain.append((cur, p))
        cur = p[0]
    if not chain:
        return None

    cat = chain[0][1][1]
    u36 = 0
    u28 = 0
    tint_rows = None
    alb = 0
    nrm = 0
    h3 = 0
    models: list[str] = []

    # Child overrides parent -> walk child-to-parent taking first non-empty attribute
    for _cid, (par, pcat, pu28, fl, pu36, tags, states) in chain:
        if not cat and pcat:
            cat = pcat
        if not u36 and pu36:
            u36 = pu36
        if not u28 and pu28:
            u28 = pu28
        if tint_rows is None and fl[:3] != (255, 255, 255) and fl[:3] != (0, 0, 0):
            tint_rows = [int(fl[0]), int(fl[1]), int(fl[2])]

        st = states.get(sid)
        if st is None and states:
            st = states.get(DEFAULT_STATE_ID) or next(iter(states.values()))
        if st:
            h0, sa, sn, sh3, stags = st
            if not alb and sa:
                alb = sa
            if not nrm and sn:
                nrm = sn
            if not h3 and sh3:
                h3 = sh3
            if not models:
                for tk in DRAWABLE_TAGS:
                    for v in stags.get(tk, ()):
                        models.extend(j32_models.get(v, ()))
                if not models and h0 in m64_models:
                    models.extend(m64_models[h0])
        if not models:
            for tk in DRAWABLE_TAGS:
                for v in tags.get(tk, ()):
                    models.extend(j32_models.get(v, ()))

    return {
        "pid": pid,
        "cat": cat,
        "u36": u36,
        "u28": u28,
        "tint_rows": tint_rows,
        "alb": alb,
        "nrm": nrm,
        "h3": h3,
        "models": list(dict.fromkeys(models)),
    }


def _build_override_dict(
    res: dict,
    tex_info: dict,
    muv_meta: dict,
    var_rgb: dict,
) -> dict:
    """Build clean per-garment material override metadata for an outfit part or `part_overrides`."""
    u36 = res["u36"]
    muv = muv_meta.get(u36) if u36 else None

    alb_raw = f"{res['alb']:016x}" if res["alb"] else ""
    nrm_raw = f"{res['nrm']:016x}" if res["nrm"] else ""

    # Only keep genuine non-placeholder sRGB albedos present in textures.sqlite3
    alb_hex = ""
    if alb_raw and alb_raw not in PLACEHOLDER_TEXTURES:
        ai = tex_info.get(alb_raw)
        if ai:
            fmt = str(ai["format"] if isinstance(ai, dict) else ai[2] or "").upper()
            if "SRGB" in fmt:
                alb_hex = alb_raw

    nrm_hex = nrm_raw if (nrm_raw and nrm_raw not in PLACEHOLDER_TEXTURES and nrm_raw in tex_info) else ""

    tint_palette = f"{res['u28']:08x}" if res["u28"] else ""
    tint_rows = res["tint_rows"]
    tint_row = None
    if tint_rows:
        for r in tint_rows:
            if r < 255:
                tint_row = r
                break

    tint_color = ""
    if muv and muv.get("primaryColor"):
        tint_color = muv["primaryColor"]
    elif alb_hex and alb_hex in var_rgb:
        # Only trust variation PSO avgRGB on verified sRGB color albedos — never on linear
        # tint-zone masks (where R/G/B encode mask weights rather than color!).
        r, g, b = var_rgb[alb_hex]
        tint_color = f"#{r:02x}{g:02x}{b:02x}"

    ov: dict = {
        "albedo": alb_hex,
        "normal": nrm_hex,
        "tintRow": tint_row,
        "tintColor": tint_color,
    }
    if tint_rows:
        ov["tintRows"] = tint_rows
    if tint_palette:
        ov["tintPalette"] = tint_palette
    if muv:
        ov["muvPreset"] = muv["hex"]
        ov["muvLabel"] = muv["label"]
        ov["swatches"] = muv["swatches"]
        ov["hasPattern"] = muv["hasPattern"]
    return ov


def _make_outfit_label(index: int, slot_name: str, parts: list[dict]) -> str:
    """Create a concise, recognizable display label for an outfit."""
    if slot_name and not _GENERIC_SLOT_RE.match(slot_name):
        pretty = slot_name.replace("_", " ").strip().title()
        if pretty:
            return pretty

    if not parts:
        return f"Outfit {index + 1}"

    # Sort garments by visual headline priority (Fullbody > Top/Bikini > Bottom > Footwear > Accessory)
    ranked = []
    for p in parts:
        short_lbl, prio, _slot = _classify_garment(p["drawable"])
        color_tag = ""
        if p.get("muvLabel"):
            color_tag = p["muvLabel"].split(" · ")[0].strip()
        ranked.append((prio, short_lbl, color_tag))
    ranked.sort(key=lambda x: x[0])

    # Pick up to 2 headline pieces (plus footwear if bikini/underwear set)
    chosen_labels = []
    seen_lbls: set[str] = set()
    primary_color = ""
    for prio, lbl, ctag in ranked:
        if prio > 4 and chosen_labels:
            continue
        if lbl in seen_lbls:
            continue
        seen_lbls.add(lbl)
        if not primary_color and ctag and ctag != "Default":
            primary_color = ctag
        chosen_labels.append(lbl)
        if len(chosen_labels) >= 2:
            break

    base = " & ".join(chosen_labels) if chosen_labels else f"Outfit {index + 1}"
    if primary_color:
        return f"{base} ({primary_color})"
    return base


def extract_ped_outfits(
    drp_blocks,
    outfit_defs: dict,
    inv_by_name,
    tex_info: dict,
    *,
    arch_hash: int = 0,
    global_cat: tuple[dict, dict] | None = None,
    muv_meta: dict | None = None,
    var_rgb: dict | None = None,
    drawable_lookup: tuple[dict, dict] | None = None,
    max_outfits: int = 64,
) -> dict:
    """Extract structured named outfits, per-garment `part_overrides`, and verified eye texture for a ped.

    Args:
        drp_blocks: Either the `blocks` dict returned by `parse_0drp_blocks(buf)` OR a pre-parsed
            `(local_presets, local_outfits)` tuple from `parse_drp_catalogue(blocks)`.
        outfit_defs: Result of `load_outfit_definitions()`.
        inv_by_name: Iterable/set/dict of valid indexed drawable names from `inventory.sqlite3`
            (optional if `drawable_lookup` is supplied).
        tex_info: `{hash_hex: (width, height, format, ...)}` from `textures.sqlite3`.
        arch_hash: Ped archetype u32 hash (if 0, inferred via `outfit_defs["outfit_to_arch"]`).
        global_cat: Optional `(global_presets, global_outfits)` from `load_global_catalogue()`.
        muv_meta: Optional MUV preset metadata from `load_muv_metadata()`.
        var_rgb: Optional texture avgRGB dict from `load_all_variation_rgb()`.
        drawable_lookup: Optional prebuilt `(j32_models, m64_models)` from `build_drawable_lookup()`
            to avoid rebuilding hashes per ped during batch sweeps.
        max_outfits: Cap on number of distinct outfits returned (default 64; all unique garments across
            ALL outfits still populate `part_overrides`!).

    Returns:
        {
            "outfits": [{"id", "slotHash", "name", "label", "weight", "parts", "hair", "nails"}, ...],
            "part_overrides": {drawable_lower: {"albedo", "normal", "tintRow", "tintColor", ...}},
            "eye_texture": str,       # verified 16-char sRGB iris hash or ""
            "eye_drawable": str,      # matched eye mesh name or ""
            "hair_drawables": [str],  # distinct hair drawables referenced across outfits
        }
    """
    if isinstance(drp_blocks, tuple) and len(drp_blocks) == 2:
        local_presets, local_outfits = drp_blocks
    else:
        local_presets, local_outfits = parse_drp_catalogue(drp_blocks or {})

    global_presets, global_outfits = global_cat if global_cat is not None else ({}, {})
    muv_meta = muv_meta or {}
    var_rgb = var_rgb or {}

    if drawable_lookup is not None:
        j32_models, m64_models = drawable_lookup
    else:
        j32_models, m64_models = build_drawable_lookup(inv_by_name or ())

    # Infer arch_hash from local_outfits if caller didn't pass it
    arch_to_outfits = outfit_defs.get("arch_to_outfits", {}) if outfit_defs else {}
    outfit_to_arch = outfit_defs.get("outfit_to_arch", {}) if outfit_defs else {}
    if not arch_hash and local_outfits and outfit_to_arch:
        votes = collections.Counter()
        for oid in local_outfits:
            ah = outfit_to_arch.get(oid)
            if ah:
                votes[ah] += 1
        if votes:
            arch_hash = votes.most_common(1)[0][0]

    pso_entries = arch_to_outfits.get(arch_hash, []) if arch_hash else []
    if not pso_entries and local_outfits:
        # Fallback: enumerate top-level local outfits not referenced as sub-outfits
        referenced_subs = {
            ref_id
            for _opar, refs in local_outfits.values()
            for ref_id, _sid in refs
            if ref_id in local_outfits
        }
        pso_entries = [
            {"slot_hash": 0, "outfit_hash": oid, "slot_name": "", "is_random_spawn": True, "weight": 1.0}
            for oid in local_outfits
            if oid not in referenced_subs
        ]

    outfits_out: list[dict] = []
    part_overrides: dict[str, dict] = {}
    override_freq: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    override_payload: dict[tuple[str, str], dict] = {}
    hair_drawables: list[str] = []
    seen_hair: set[str] = set()
    eye_candidates: list[tuple[int, str, str]] = []  # (priority, eye_drawable, eye_tex_hex)

    seen_outfit_sigs: set[tuple] = set()
    seen_oid: set[int] = set()

    for idx_o, entry in enumerate(pso_entries):
        oid = entry["outfit_hash"]
        slot_h = entry["slot_hash"]
        slot_name = entry.get("slot_name") or ""

        flat_refs = _resolve_outfit_tree(oid, local_outfits, global_outfits)
        if not flat_refs:
            continue

        # Merge parts so direct outfit entries override inherited sub-outfit entries of the same category
        direct_cats: set[int] = set()
        resolved_items: list[tuple[int, dict]] = []
        for pid, sid, depth in flat_refs:
            res = _resolve_preset_chain(pid, sid, local_presets, global_presets, j32_models, m64_models)
            if not res or not res["models"]:
                continue
            if depth == 0 and res["cat"]:
                direct_cats.add(res["cat"])
            resolved_items.append((depth, res))

        parts_by_drawable: dict[str, dict] = {}
        outfit_hair: list[str] = []
        outfit_nails: list[str] = []

        for depth, res in resolved_items:
            cat = res["cat"]
            drawable = res["models"][0]
            dl = drawable.lower()

            # Eye texture capture (prefer outfit-assigned verified sRGB eye texture)
            if cat == EYES_CATEGORY or re.search(r"_eyes(?:_|$)", dl):
                ev = resolve_eye_texture(res["alb"], tex_info)
                if ev:
                    prio = (2 if idx_o == 0 else 1) + (1 if res["pid"] not in BASE_EYE_PRESET_IDS else 0)
                    eye_candidates.append((prio, drawable, ev))
                continue

            if cat in SKIN_CATEGORIES or cat in (HEAD_CATEGORY, TEETH_CATEGORY):
                continue

            # If a direct ref replaced an inherited category, skip the inherited part
            if depth > 0 and cat and cat in direct_cats:
                continue

            ov = _build_override_dict(res, tex_info, muv_meta, var_rgb)

            # Hair & Nails tracked separately on the outfit AND recorded in part_overrides
            if cat == HAIR_CATEGORY or dl.startswith(HAIR_PREFIXES):
                if drawable not in outfit_hair:
                    outfit_hair.append(drawable)
                if dl not in seen_hair:
                    seen_hair.add(dl)
                    hair_drawables.append(drawable)
                part_overrides.setdefault(dl, ov)
                continue
            if cat in (FINGERNAILS_CATEGORY, TOENAILS_CATEGORY) or dl.startswith(NAIL_PREFIXES):
                if drawable not in outfit_nails:
                    outfit_nails.append(drawable)
                part_overrides.setdefault(dl, ov)
                continue

            if cat in NON_GARMENT_CATEGORIES or any(dl.startswith(p) or f"_{p}" in dl for p in SKIP_GARMENT_PREFIXES):
                continue

            _short_lbl, _prio, slot_cat = _classify_garment(drawable)
            part_obj = {
                "drawable": drawable,
                "slot": slot_cat,
                "category": f"{cat:08x}" if cat else "",
                **ov,
            }
            parts_by_drawable[dl] = part_obj

            # Track frequency of each colorway per drawable so part_overrides picks the character's
            # primary/most-canonical colorway for every garment they own!
            ov_key = json.dumps(ov, sort_keys=True, separators=(",", ":"))
            override_freq[dl][ov_key] += 2 if idx_o == 0 else 1
            override_payload[(dl, ov_key)] = ov

        parts_list = list(parts_by_drawable.values())
        if not parts_list:
            continue

        # Deduplicate identical outfits (e.g. duplicate PSO pointers or identical garment+preset sets)
        if oid in seen_oid:
            continue
        sig = tuple(
            sorted(
                (p["drawable"].lower(), p.get("muvPreset", ""), p.get("albedo", ""), p.get("tintRow"))
                for p in parts_list
            )
        )
        if sig in seen_outfit_sigs:
            continue
        seen_oid.add(oid)
        seen_outfit_sigs.add(sig)

        if len(outfits_out) < max_outfits:
            out_idx = len(outfits_out)
            out_name = slot_name or f"outfit_{out_idx + 1:02d}"
            out_label = _make_outfit_label(out_idx, slot_name, parts_list)
            outfits_out.append({
                "id": f"{oid:08x}",
                "slotHash": f"{slot_h:08x}" if slot_h else "",
                "name": out_name,
                "label": out_label,
                "weight": entry.get("weight", 1.0),
                "parts": parts_list,
                "hair": outfit_hair,
                "nails": outfit_nails,
            })

    # Finalize fallback part_overrides: for every garment seen in any outfit, pick highest-frequency
    # (and earliest-outfit) colorway, and also sweep any non-outfit local_presets so toggling ANY
    # garment in the ped's 0DRP uses that ped's real in-game colorway!
    for dl, ctr in override_freq.items():
        best_key = ctr.most_common(1)[0][0]
        part_overrides[dl] = override_payload[(dl, best_key)]

    # Sweep local_presets for any additional garments/eyes not referenced by a PSO outfit
    parent_ids = {p[0] for p in local_presets.values() if p[0]}
    for pid, p in local_presets.items():
        cat = p[1]
        if cat == EYES_CATEGORY:
            res = _resolve_preset_chain(pid, DEFAULT_STATE_ID, local_presets, global_presets, j32_models, m64_models)
            if res and res["models"]:
                ev = resolve_eye_texture(res["alb"], tex_info)
                if ev:
                    is_leaf = 1 if pid not in parent_ids else 0
                    not_base = 1 if pid not in BASE_EYE_PRESET_IDS else 0
                    eye_candidates.append((is_leaf + not_base - 2, res["models"][0], ev))
            continue
        if cat in NON_GARMENT_CATEGORIES:
            continue
        # Only fill part_overrides if not already populated from an outfit
        res = _resolve_preset_chain(pid, DEFAULT_STATE_ID, local_presets, global_presets, j32_models, m64_models)
        if not res or not res["models"]:
            continue
        dl = res["models"][0].lower()
        if dl in part_overrides or any(dl.startswith(pr) or f"_{pr}" in dl for pr in SKIP_GARMENT_PREFIXES):
            continue
        ov = _build_override_dict(res, tex_info, muv_meta, var_rgb)
        if ov.get("muvPreset") or ov.get("albedo") or ov.get("tintColor") or ov.get("tintRow") is not None:
            part_overrides[dl] = ov

    # Disambiguate duplicate outfit labels (e.g. two "Cropped Tank & Jean Shorts (Red)" -> append #2)
    lbl_counts = collections.Counter(o["label"] for o in outfits_out)
    if any(c > 1 for c in lbl_counts.values()):
        lbl_seen: dict[str, int] = collections.Counter()
        for o in outfits_out:
            lb = o["label"]
            if lbl_counts[lb] > 1:
                lbl_seen[lb] += 1
                o["label"] = f"{lb} #{lbl_seen[lb]}"

    eye_drawable, eye_texture = "", ""
    if eye_candidates:
        eye_candidates.sort(key=lambda x: x[0], reverse=True)
        _, eye_drawable, eye_texture = eye_candidates[0]

    return {
        "outfits": outfits_out,
        "part_overrides": part_overrides,
        "eye_texture": eye_texture,
        "eye_drawable": eye_drawable,
        "hair_drawables": hair_drawables,
    }


# Lazy Runtime Helper (for on-demand single-character lookup in model_service.py) ------------------

_RUNTIME_LOCK = threading.Lock()
_RUNTIME_CTX: dict | None = None


def _get_runtime_ctx() -> dict:
    global _RUNTIME_CTX
    with _RUNTIME_LOCK:
        if _RUNTIME_CTX is not None:
            return _RUNTIME_CTX
        spans = PACKS_SPANS.read_bytes()
        srows = [struct.unpack_from("<QQQII", spans, i * 32) for i in range(len(spans) // 32)]
        off_to_idx = {r[0]: idx for idx, r in enumerate(srows)}

        inv = sqlite3.connect(INVENTORY_DB.as_uri() + "?mode=ro", uri=True)
        all_models = [
            r[0]
            for r in inv.execute(
                "SELECT DISTINCT name FROM model WHERE name IS NOT NULL AND status='indexed' "
                "AND category IN ('Protagonists','Characters','Clothing & accessories','Animals')"
            )
        ]
        inv.close()

        tx = sqlite3.connect(TEXTURES_DB.as_uri() + "?mode=ro", uri=True)
        tex_info = {
            h: (w, hh, fmt or "")
            for h, w, hh, fmt in tx.execute("SELECT hash, width, height, format FROM texture")
        }
        tx.close()

        _RUNTIME_CTX = {
            "srows": srows,
            "off_to_idx": off_to_idx,
            "drawable_lookup": build_drawable_lookup(all_models),
            "tex_info": tex_info,
            "outfit_defs": load_outfit_definitions(),
            "global_cat": load_global_catalogue(),
            "muv_meta": load_muv_metadata(),
            "var_rgb": load_all_variation_rgb(),
        }
        return _RUNTIME_CTX


def load_0drp_blocks_at_offset(drp_offset: int, srows: list | None = None, off_to_idx: dict | None = None):
    """Decompress and parse a single `0DRP` catalogue from `packs.rpf` at `drp_offset`."""
    if srows is None or off_to_idx is None:
        ctx = _get_runtime_ctx()
        srows = ctx["srows"]
        off_to_idx = ctx["off_to_idx"]
    i = off_to_idx.get(int(drp_offset))
    if i is None:
        return 0, {}
    with open(PACKS_RPF, "rb") as f:
        off, clen, *_ = srows[i]
        f.seek(off)
        buf = bytearray(zlib.decompress(f.read(clen)))
        j = i
        while (
            j + 1 < len(srows)
            and ((srows[j][0] + srows[j][1] + 15) & -16) == srows[j + 1][0]
            and len(buf) % 65536 == 0
        ):
            j += 1
            f.seek(srows[j][0])
            buf.extend(zlib.decompress(f.read(srows[j][1])))
    rk, blks = parse_0drp_blocks(buf)
    if rk != CATALOG_ROOT or not blks:
        return 0, {}
    return rk, blks


def extract_ped_outfits_by_offset(drp_offset: int, arch_hash: int = 0, max_outfits: int = 64) -> dict:
    """On-demand extraction of outfits, `part_overrides`, and verified eye texture for one ped."""
    ctx = _get_runtime_ctx()
    rk, blks = load_0drp_blocks_at_offset(drp_offset, ctx["srows"], ctx["off_to_idx"])
    if not rk:
        return {"outfits": [], "part_overrides": {}, "eye_texture": "", "eye_drawable": "", "hair_drawables": []}
    return extract_ped_outfits(
        blks,
        ctx["outfit_defs"],
        None,
        ctx["tex_info"],
        arch_hash=arch_hash,
        global_cat=ctx["global_cat"],
        muv_meta=ctx["muv_meta"],
        var_rgb=ctx["var_rgb"],
        drawable_lookup=ctx["drawable_lookup"],
        max_outfits=max_outfits,
    )


# CLI Self-Test & Full Coverage Sweep --------------------------------------------------------------

def _self_test():
    ctx = _get_runtime_ctx()
    pa = sqlite3.connect(PED_ARCH_DB.as_uri() + "?mode=ro", uri=True)
    rows = pa.execute(
        "SELECT idx, arch_hash, name, drp_offset, outfit_count, eye_texture, body_rig FROM ped_archetype"
    ).fetchall()
    pa.close()
    by_name = {r[2]: r for r in rows}

    print("=== TARGET CHARACTERS VERIFICATION ===")
    for nm in ("cs_mandy", "player_story000_00", "player_story000_01", "cs_boobie_ike", "cs_ali", "cs_brian_heder", "cs_cal"):
        r = by_name.get(nm)
        if not r:
            continue
        _idx, arch_h, _name, drp_off, pso_cnt, db_eye, rig = r
        res = extract_ped_outfits_by_offset(drp_off, arch_h, max_outfits=64)
        outs = res["outfits"]
        pov = res["part_overrides"]
        print(
            f"\n[{nm}] rig={rig} pso_outfits={pso_cnt} -> distinct_outfits={len(outs)} "
            f"| part_overrides={len(pov)} | eye={res['eye_texture']} (db={db_eye})"
        )
        show_n = len(outs) if nm == "cs_mandy" else min(3, len(outs))
        for oi, o in enumerate(outs[:show_n]):
            p_desc = []
            for p in o["parts"]:
                tag = p.get("muvPreset") or (p["albedo"][:8] if p.get("albedo") else (f"tint:{p['tintRow']}" if p.get("tintRow") is not None else "base"))
                col = p.get("tintColor") or ""
                p_desc.append(f"{p['drawable']} [{tag} {col}]".strip())
            print(f"  #{oi+1:2d} id={o['id']} ({o['name']}) -> \"{o['label']}\" ({len(o['parts'])} parts)")
            for pd in p_desc:
                print(f"      - {pd}")

    print("\n=== EYE PRESETS SAMPLE (fr1 / mr1) ===")
    for rig in ("fr1", "mr1"):
        ep = eye_presets_for_rig(rig, ctx["tex_info"])
        print(f"  {rig}: {len(ep)} verified sRGB iris presets -> top 5: {[(x['hash'][:8], x['label'], x['hex']) for x in ep[:5]]}")

    print("\n=== FULL 2,714 ARCHETYPE COVERAGE SWEEP ===")
    srows = ctx["srows"]
    off_to_idx = ctx["off_to_idx"]

    def _worker(chunk):
        st = collections.Counter()
        with open(PACKS_RPF, "rb") as f:
            for _idx, arch_h, _nm, drp_off, _pso_cnt, db_eye, _rig in chunk:
                st["total_archetypes"] += 1
                db_eye_ok = resolve_eye_texture(db_eye, ctx["tex_info"])
                if db_eye_ok:
                    st["eye_db_verified"] += 1
                if not drp_off or drp_off not in off_to_idx:
                    continue
                i = off_to_idx[drp_off]
                off, clen, *_ = srows[i]
                f.seek(off)
                buf = bytearray(zlib.decompress(f.read(clen)))
                j = i
                while (
                    j + 1 < len(srows)
                    and ((srows[j][0] + srows[j][1] + 15) & -16) == srows[j + 1][0]
                    and len(buf) % 65536 == 0
                ):
                    j += 1
                    f.seek(srows[j][0])
                    buf.extend(zlib.decompress(f.read(srows[j][1])))
                rk, blks = parse_0drp_blocks(buf)
                if rk != CATALOG_ROOT or not blks:
                    continue
                res = extract_ped_outfits(
                    blks,
                    ctx["outfit_defs"],
                    None,
                    ctx["tex_info"],
                    arch_hash=arch_h,
                    global_cat=ctx["global_cat"],
                    muv_meta=ctx["muv_meta"],
                    var_rgb=ctx["var_rgb"],
                    drawable_lookup=ctx["drawable_lookup"],
                    max_outfits=64,
                )
                if res["outfits"]:
                    st["peds_with_outfits"] += 1
                    st["total_distinct_outfits"] += len(res["outfits"])
                if res["part_overrides"]:
                    st["peds_with_part_overrides"] += 1
                    st["total_part_overrides"] += len(res["part_overrides"])
                if res["eye_texture"] or db_eye_ok:
                    st["peds_with_verified_eye"] += 1
                if res["eye_texture"]:
                    st["peds_with_outfit_eye"] += 1
                for o in res["outfits"]:
                    for p in o["parts"]:
                        st["outfit_parts_total"] += 1
                        if p.get("muvPreset"):
                            st["parts_with_muv"] += 1
                        if p.get("albedo"):
                            st["parts_with_albedo"] += 1
                        if p.get("normal"):
                            st["parts_with_normal"] += 1
                        if p.get("tintColor"):
                            st["parts_with_tintColor"] += 1
                        if p.get("tintRow") is not None:
                            st["parts_with_tintRow"] += 1
                        if p.get("muvPreset") or p.get("albedo") or p.get("tintColor") or p.get("tintRow") is not None:
                            st["parts_with_any_override"] += 1
        return st

    chunks = [rows[k::8] for k in range(8)]
    tot = collections.Counter()
    with ThreadPoolExecutor(max_workers=8) as ex:
        for st in ex.map(_worker, chunks):
            tot.update(st)
    for k, v in sorted(tot.items()):
        print(f"  {k:28s}: {v:,}")


if __name__ == "__main__":
    _self_test()
