# SPDX-License-Identifier: GPL-3.0-or-later
"""Character-native tattoos & facial/body makeup overlays for GTA VI (RAGE 9).

Resolves per-ped tattoos, body decorations, and facial/body makeup overlays from:
  1. Per-ped 0DRP catalogues in ``packs.rpf`` (indexed via ``ped_archetypes.sqlite3.drp_offset``):
     - ``0xf795e8ba`` cosmetic presets (tag ``0x2d497cdb = 0xb9b40a57`` ("cosmetic"))
     - Domain tag ``0xbc242c1e``: ``0x43d760cb`` ("tattoo") vs ``0x53a1ec2c`` ("makeup")
     - Decoration ID tag ``0xd9f1087b`` -> u32 IDs into ``data.rpf/00001ae00000.pso``
     - Outfit list ``0xa9d57751`` (80B records) -> ``0x67e54cb2`` (8B ``(preset_id, flag)`` entries)
       separating permanent base tattoos (``Outfit[0..1]``) and primary makeup looks from
       alternate scene/mission outfits (including 41-layer dirt/bruise damage looks).
  2. Global decoration catalog ``INDEX/data.rpf/00001ae00000.pso``:
     - Block 11 (``0x40e79f52``, 7,509 records): target slot (``0xb53667be``), base 3x4 UV affine
       matrix (``0x6e8249bf``), and layer array pointer (``0xef888d7e``)
     - Blocks 1..5 (``0x77f0121d``, 13,281 records): per-layer texture slots (``0x5adaff50``),
       HSVA 3-channel tint palette (``0x0d23ee2d``), opacity (``0xc88b9e6d``), bilateral mirror
       bitmask (``0x4b621302 & 0x400``), material/gloss overrides (``0x10c4c2d8``), and sub 3x4
       UV affine matrix (``0x8b424621``)
     - Block 9 (``0x91501997``, 85 records): composite multi-decoration sets (e.g. Boobie Ike's
       26-layer upper-body tattoo set, Brian Heder's 13-layer upper-body tattoo set) resolving
       into Block 7 (``0x0000004a``) child ``0x40e79f52`` IDs -> 100.0% resolution across all
       2,694 ped 0DRPs.
  3. ``LIBRARY/textures.sqlite3`` (``decorations_0..3_3.ttpf``, 6,929 textures).

Standalone module designed for Wave 2 integration into ``ped_archetypes.py``, ``model_service.py``,
``glb_textured.py``, and ``viewer/char-effects.js``.
"""
from __future__ import annotations

import collections
import colorsys
from contextlib import closing
import io
import math
from pathlib import Path
import sqlite3
import struct
import threading
import zlib
from typing import Any


import decode_pso  # noqa: E402
from face_presets import (  # noqa: E402
    PACKS_RPF,
    PACKS_SPANS,
    parse_0drp_blocks,
    ptr,
    read_tags,
)
from paths import INDEX, LIBRARY

DECORATIONS_PSO = INDEX / "data.rpf/00001ae00000.pso"
TEXTURES_DB = LIBRARY / "textures.sqlite3"
PED_ARCH_DB = LIBRARY / "ped_archetypes.sqlite3"

# ---------------------------------------------------------------------------
# RAGE 9 Decoration / Cosmetic Tag & Category Constants
# ---------------------------------------------------------------------------
TAG_DECORATION_ID = 0xD9F1087B    # u32[] -> 0x40e79f52 or 0x91501997 (0xacec22be)
TAG_COSMETIC_DOMAIN = 0xBC242C1E  # 0x43d760cb ("tattoo") or 0x53a1ec2c ("makeup")
TAG_SLOT_TYPE = 0x2D497CDB        # 0xb9b40a57 ("cosmetic")

DOMAIN_TATTOO = 0x43D760CB        # joaat("tattoo")
DOMAIN_MAKEUP = 0x53A1EC2C        # joaat("makeup")
SLOT_COSMETIC = 0xB9B40A57        # joaat("cosmetic")

# Decoration target body slot (0x40e79f52 field 0xb53667be @ offset 108)
DCAT_HEAD = 0x378AD10C
DCAT_UPPER = 0x3817131D
DCAT_LOWER = 0x44925BFD

SKIN_TARGET_BY_DCAT: dict[int, str] = {
    DCAT_HEAD: "head",
    DCAT_UPPER: "upper",
    DCAT_LOWER: "lower",
}

# 0x77f0121d layer bitmask (0x4b621302 @ offset 208)
LAYER_FLAG_MIRROR_U = 0x00000400  # Bilateral horizontal mirror across U = 0.5

# Makeup preset category hashes (0xf795e8ba cat @ offset 24 when domain == "makeup")
MAKEUP_KIND_BY_CAT: dict[int, tuple[str, str]] = {
    0x9F7CE58F: ("lipstick", "Lipstick"),
    0x424F80A0: ("eyeliner", "Eyeliner"),
    0x8CBFFD53: ("eyeshadow", "Eyeshadow"),
    0x83973CBA: ("blush", "Blush & Contour"),
    0xD45CC3CE: ("fullMakeup", "Full Face Look"),
    0xA7557DF9: ("bodyShimmer", "Body Shimmer"),
}

# Known high-frequency tattoo zone hashes (0xf795e8ba cat @ offset 24 when domain == "tattoo")
TATTOO_ZONE_BY_CAT: dict[int, str] = {
    0x7FEA21F5: "Upper Arm / Wrist",
    0xB3024C9F: "Forearm / Wrist",
    0x69057228: "Upper Body / Torso",
    0x2E3FF261: "Upper Body / Arm",
    0x089B9D09: "Arm / Shoulder",
    0xF3A56B84: "Torso / Chest",
    0x9AB1C4DE: "Back / Torso",
    0x822A4D8C: "Upper Body",
    0x533A38D4: "Arm / Shoulder",
    0x5A52CDB7: "Hand / Wrist",
    0x766EF65A: "Full Upper Sleeve / Torso",
    0xB7E33546: "Full Body",
    0x413DA039: "Lower Body / Leg",
    0x66DFDB06: "Thigh / Leg",
    0xED13B364: "Calf / Leg",
    0x509345D4: "Leg / Shin",
    0x312FA0F1: "Lower Leg / Shin",
    0x44EB5200: "Head / Face",
    0x12926679: "Neck / Head",
    0xEB4099EE: "Neck / Face",
    0x399FB475: "Face / Cheek",
    0xE232C2D2: "Neck / Jaw",
}

TARGET_DEFAULT_ZONE: dict[str, str] = {
    "head": "Head / Neck",
    "upper": "Upper Body",
    "lower": "Lower Body",
}

# Caches (protected by _LOCK)
_LOCK = threading.RLock()
_CATALOG_CACHE: dict[str, Any] | None = None
_SPANS_CACHE: tuple[list[tuple[int, int, int, int, int]], dict[int, int]] | None = None
_MAKEUP_PRESET_INDEX_CACHE: list[dict[str, Any]] | None = None


def _ro_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Color & RAGE 9 Tint-Mask Decoding
# ---------------------------------------------------------------------------
def hsva_u32_to_rgba(val: int) -> tuple[int, int, int, int]:
    """Decode RAGE 9 ``0x0d23ee2d`` packed HSVA u32 -> ``(r, g, b, a)`` in 0..255.

    Byte layout (little-endian): ``byte0 = H``, ``byte1 = S``, ``byte2 = V``, ``byte3 = A``
    (where ``A > 0`` indicates the tint channel is active; ``A == 0`` means unused/disabled).
    """
    v = int(val) & 0xFFFFFFFF
    h = (v & 0xFF) / 255.0
    s = ((v >> 8) & 0xFF) / 255.0
    val_f = ((v >> 16) & 0xFF) / 255.0
    a = (v >> 24) & 0xFF
    rf, gf, bf = colorsys.hsv_to_rgb(h, s, val_f)
    return (
        max(0, min(255, int(round(rf * 255.0)))),
        max(0, min(255, int(round(gf * 255.0)))),
        max(0, min(255, int(round(bf * 255.0)))),
        a,
    )


def hsva_u32_to_hex(val: int) -> str:
    r, g, b, _ = hsva_u32_to_rgba(val)
    return f"#{r:02x}{g:02x}{b:02x}"


def decode_tint_palette(raw_tints: tuple[int, int, int] | list[int]) -> tuple[list[dict[str, Any]], str]:
    """Decode a 3-entry ``0x0d23ee2d`` HSVA palette into structured tints + ``tintMode``.

    Returns ``(tints_list, tint_mode)`` where:
      - ``tint_mode == "mask"`` when at least one tint has top byte ``A > 0``:
        RAGE 9 texture encodes ``A`` = opacity mask, ``R`` = blend toward ``t1``,
        ``B`` = blend toward ``t2``, ``G`` = luminance shading centered at ``0.5``.
      - ``tint_mode == "direct_rgb"`` when all 3 tints have ``A == 0``:
        texture is already authored as direct full-color sRGB + Alpha.
    """
    entries: list[dict[str, Any]] = []
    any_enabled = False
    for idx in range(3):
        raw = int(raw_tints[idx]) if idx < len(raw_tints) else 0
        r, g, b, a = hsva_u32_to_rgba(raw)
        enabled = a > 0
        if enabled:
            any_enabled = True
        entries.append({
            "slot": idx,
            "enabled": enabled,
            "hex": f"#{r:02x}{g:02x}{b:02x}",
            "rgb": [r, g, b],
            "alpha": round(a / 255.0, 4),
            "hsva": f"0x{raw & 0xFFFFFFFF:08x}",
        })
    return entries, ("mask" if any_enabled else "direct_rgb")


def color_label(hex_col: str, is_tattoo: bool = False) -> str:
    """Return a concise human-readable swatch label for UI display."""
    h = (hex_col or "#000000").lstrip("#")
    if len(h) != 6:
        return "Ink" if is_tattoo else "Natural"
    r, g, b = int(h[0:2], 16) / 255.0, int(h[2:4], 16) / 255.0, int(h[4:6], 16) / 255.0
    hue, sat, val = colorsys.rgb_to_hsv(r, g, b)
    hd = hue * 360.0
    if val < 0.16:
        return "Black Ink" if is_tattoo else "Jet Black"
    if sat < 0.14:
        if val > 0.80:
            return "White"
        if val > 0.45:
            return "Grey"
        return "Charcoal Ink" if is_tattoo else "Charcoal"
    if is_tattoo:
        if 150 <= hd <= 245 and val < 0.55:
            return "Faded Blue-Black Ink" if sat < 0.55 else "Teal-Blue Ink"
        if hd < 20 or hd >= 340:
            return "Red Ink"
        if 20 <= hd < 55:
            return "Sepia Ink"
        if 55 <= hd < 150:
            return "Green Ink"
        return "Colored Ink"
    # Makeup color words
    if hd < 12 or hd >= 342:
        if val < 0.45:
            return "Deep Crimson"
        if sat < 0.45 and val > 0.65:
            return "Soft Rose"
        return "Berry Red" if hd >= 342 else "Crimson"
    if hd < 32:
        if val < 0.42:
            return "Dark Espresso"
        if sat < 0.42:
            return "Warm Nude"
        return "Coral Bronze" if val > 0.65 else "Terracotta"
    if hd < 58:
        return "Gold Shimmer" if val > 0.72 else "Warm Bronze"
    if hd < 155:
        return "Emerald"
    if hd < 245:
        return "Sapphire"
    if hd < 300:
        return "Plum"
    return "Rose Pink" if val > 0.62 else "Mauve"


def tint_overlay_rgba(
    rgba_u8,
    tints: list[dict[str, Any]],
    tint_mode: str = "mask",
    opacity: float = 1.0,
):
    """Apply RAGE 9 3-tint channel mask decoding to a decoded ``(H, W, 4) uint8`` RGBA array.

    For ``tint_mode == "direct_rgb"``, scales alpha by ``opacity`` and returns RGBA directly.
    For ``tint_mode == "mask"``, decodes:
      - ``base_rgb = t0`` (primary tint)
      - ``rgb = lerp(base_rgb, t1, R)`` if ``t1.enabled``
      - ``rgb = lerp(rgb, t2, B)`` if ``t2.enabled``
      - ``rgb = clamp(rgb * (0.5 + G), 0, 1)`` (G = 0.5 -> neutral 1.0x shading)
      - ``alpha = A * opacity``
    """
    import numpy as np

    arr = np.asarray(rgba_u8, dtype=np.float32) / 255.0
    if arr.ndim != 3 or arr.shape[2] < 4:
        return rgba_u8
    op = max(0.0, min(1.0, float(opacity)))
    if tint_mode != "mask" or not tints:
        out = arr.copy()
        out[:, :, 3] = np.clip(out[:, :, 3] * op, 0.0, 1.0)
        return np.clip(out * 255.0 + 0.5, 0, 255).astype(np.uint8)

    def _rgb01(idx: int) -> tuple[float, float, float, bool]:
        if idx < len(tints):
            t = tints[idx]
            if isinstance(t, dict):
                rgb = t.get("rgb")
                en = bool(t.get("enabled", True))
                if rgb and len(rgb) == 3:
                    return (rgb[0] / 255.0, rgb[1] / 255.0, rgb[2] / 255.0, en)
                hx = (t.get("hex") or "#000000").lstrip("#")
                if len(hx) == 6:
                    return (int(hx[0:2], 16) / 255.0, int(hx[2:4], 16) / 255.0, int(hx[4:6], 16) / 255.0, en)
            elif isinstance(t, str):
                hx = t.lstrip("#")
                if len(hx) == 6:
                    return (int(hx[0:2], 16) / 255.0, int(hx[2:4], 16) / 255.0, int(hx[4:6], 16) / 255.0, True)
        return (0.0, 0.0, 0.0, False)

    t0r, t0g, t0b, _ = _rgb01(0)
    t1r, t1g, t1b, t1_en = _rgb01(1)
    t2r, t2g, t2b, t2_en = _rgb01(2)

    ch_r = arr[:, :, 0]
    ch_g = arr[:, :, 1]
    ch_b = arr[:, :, 2]
    ch_a = arr[:, :, 3]

    out = np.empty_like(arr)
    out[:, :, 0] = t0r
    out[:, :, 1] = t0g
    out[:, :, 2] = t0b

    if t1_en:
        w1 = ch_r
        out[:, :, 0] = out[:, :, 0] * (1.0 - w1) + t1r * w1
        out[:, :, 1] = out[:, :, 1] * (1.0 - w1) + t1g * w1
        out[:, :, 2] = out[:, :, 2] * (1.0 - w1) + t1b * w1
    if t2_en:
        w2 = ch_b
        out[:, :, 0] = out[:, :, 0] * (1.0 - w2) + t2r * w2
        out[:, :, 1] = out[:, :, 1] * (1.0 - w2) + t2g * w2
        out[:, :, 2] = out[:, :, 2] * (1.0 - w2) + t2b * w2

    # G channel encodes subtle luminance/shading centered at 0.50 (neutral = 1.0x)
    shade = np.clip(0.5 + ch_g, 0.25, 1.75)
    out[:, :, 0] = np.clip(out[:, :, 0] * shade, 0.0, 1.0)
    out[:, :, 1] = np.clip(out[:, :, 1] * shade, 0.0, 1.0)
    out[:, :, 2] = np.clip(out[:, :, 2] * shade, 0.0, 1.0)
    out[:, :, 3] = np.clip(ch_a * op, 0.0, 1.0)
    return np.clip(out * 255.0 + 0.5, 0, 255).astype(np.uint8)


def bake_overlay_layer_png(
    raw_png_bytes: bytes,
    tints: list[dict[str, Any]],
    tint_mode: str = "mask",
    opacity: float = 1.0,
) -> bytes:
    """Decode raw texture PNG bytes, apply RAGE 9 tint-mask decoding, and re-encode as RGBA PNG."""
    import numpy as np
    from PIL import Image

    im = Image.open(io.BytesIO(raw_png_bytes)).convert("RGBA")
    arr = np.asarray(im, dtype=np.uint8)
    tinted = tint_overlay_rgba(arr, tints, tint_mode=tint_mode, opacity=opacity)
    buf = io.BytesIO()
    Image.fromarray(tinted, mode="RGBA").save(buf, format="PNG", compress_level=1)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# 2D UV Affine Math (Exact Reflection + Bilateral Mirror Support)
# ---------------------------------------------------------------------------
def _mul_affine2d(
    m1: tuple[float, ...],
    m2: tuple[float, ...],
) -> tuple[float, float, float, float, float, float]:
    """Compose two RAGE 3x4 row-vector affine UV matrices: ``uv_tex = [u v 1] * (M1 * M2)``."""
    a1, b1, c1, d1, tx1, ty1 = m1[0], m1[1], m1[4], m1[5], m1[8], m1[9]
    a2, b2, c2, d2, tx2, ty2 = m2[0], m2[1], m2[4], m2[5], m2[8], m2[9]
    return (
        a1 * a2 + b1 * c2,
        a1 * b2 + b1 * d2,
        c1 * a2 + d1 * c2,
        c1 * b2 + d1 * d2,
        tx1 * a2 + ty1 * c2 + tx2,
        tx1 * b2 + ty1 * d2 + ty2,
    )


def _decompose_uv_affine(
    a: float, b: float, c: float, d: float, tx: float, ty: float,
) -> dict[str, Any]:
    """Invert ``uv_mesh -> uv_tex`` into exact ``uv_tex -> uv_mesh`` placement.

    Correctly separates horizontal reflection (``det < 0`` -> ``flipU = True``) from rotation
    so ``ctx.translate(cx, cy); ctx.rotate(angle); ctx.scale(flipU ? -1 : 1, 1)`` reproduces
    the exact RAGE 9 affine mapping without turning mirrored eye makeup upside-down.
    """
    det = a * d - b * c
    if abs(det) < 1e-8 or not math.isfinite(det):
        return {
            "u": 0.5, "v": 0.5, "su": 0.22, "sv": 0.22,
            "angle": 0.0, "flipU": False, "rawU": 0.5, "rawV": 0.5,
        }
    ia = d / det
    ib = -b / det
    ic = -c / det
    id_ = a / det
    rx, ry = 0.5 - tx, 0.5 - ty
    raw_u = rx * ia + ry * ic
    raw_v = rx * ib + ry * id_
    su = math.hypot(ia, ib)
    sv = math.hypot(ic, id_)
    flip_u = det < 0.0
    if flip_u:
        # With scale(-1, 1) applied after rotate(ang), image +X maps to (-cos, -sin)
        ang = math.atan2(-ib, -ia)
    else:
        ang = math.atan2(ib, ia)

    if not (math.isfinite(raw_u) and math.isfinite(raw_v) and 1e-4 <= su <= 4.0 and 1e-4 <= sv <= 4.0):
        return {
            "u": 0.5, "v": 0.5, "su": 0.22, "sv": 0.22,
            "angle": 0.0, "flipU": False, "rawU": 0.5, "rawV": 0.5,
        }

    # Wrap UDIM/repeat coordinates > 1.0 or < 0.0 into [0, 1] UV space (e.g. Jason U=1.295 -> 0.295,
    # Brian Heder V=1.674 -> 0.674) while keeping exact full-sheet centers near 0.5 untouched.
    u_wrapped = raw_u % 1.0 if (raw_u < -0.05 or raw_u > 1.05) else max(0.0, min(1.0, raw_u))
    v_wrapped = raw_v % 1.0 if (raw_v < -0.05 or raw_v > 1.05) else max(0.0, min(1.0, raw_v))
    return {
        "u": round(u_wrapped, 4),
        "v": round(v_wrapped, 4),
        "su": round(min(1.5, max(0.005, su)), 4),
        "sv": round(min(1.5, max(0.005, sv)), 4),
        "angle": round(ang, 4),
        "flipU": bool(flip_u),
        "rawU": round(raw_u, 4),
        "rawV": round(raw_v, 4),
    }


def _decode_mat_override(c10: tuple[int, ...]) -> tuple[float, float]:
    """Extract normalized ``(gloss, shimmer)`` hints from ``0x77f0121d`` field ``0x10c4c2d8``."""
    gloss = 0.0
    shimmer = 0.0
    if len(c10) >= 3 and c10[2]:
        # c10[2] non-zero on makeup layers; lower byte encodes specular/gloss intensity when active
        c2 = int(c10[2]) & 0xFFFFFFFF
        if (c2 >> 24) & 0xFF:
            # Byte 0 or Byte 1 carries roughness/gloss modulation
            gloss = round(min(1.0, ((c2 & 0xFF) / 255.0) * 0.75 + (((c2 >> 8) & 0xFF) / 255.0) * 0.25), 3)
    if len(c10) >= 5 and c10[4]:
        c4 = int(c10[4]) & 0xFFFFFFFF
        if (c4 >> 24) & 0xFF:
            shimmer = round(min(1.0, (c4 & 0xFF) / 255.0), 3)
    return gloss, shimmer


# ---------------------------------------------------------------------------
# Fast Binary Catalog Loader for data.rpf/00001ae00000.pso
# ---------------------------------------------------------------------------
def _load_decorations_tex_info() -> dict[str, tuple[int, int, str]]:
    if not TEXTURES_DB.is_file():
        return {}
    out: dict[str, tuple[int, int, str]] = {}
    with closing(_ro_db(TEXTURES_DB)) as db:
        for r in db.execute(
            "SELECT t.hash, t.width, t.height, t.format "
            "FROM texture t JOIN pack p ON p.id = t.pack "
            "WHERE p.path LIKE '%decorations%'"
        ):
            out[r["hash"]] = (int(r["width"]), int(r["height"]), str(r["format"] or ""))
    return out


def load_decoration_catalog() -> dict[str, Any]:
    """Load & cache the complete RAGE 9 decoration catalog from ``data.rpf/00001ae00000.pso``.

    Uses direct binary struct unpacking over ``decode_pso.Decoder`` blocks (~120ms cold load).
    Returns a dict with:
      - ``decorations``: ``{dec_id_u32: dec_record}`` (all 7,509 ``0x40e79f52`` records)
      - ``composites``: ``{comp_id_u32: [child_dec_id_u32, ...]}`` (all 85 Block 9 sets)
      - ``tex_info``: ``{hash_hex: (width, height, format)}`` (6,929 ``decorations_*`` textures)
      - ``skin_dec_ids``: set of ``dec_id_u32`` targeting ``head`` / ``upper`` / ``lower``
    """
    global _CATALOG_CACHE
    with _LOCK:
        if _CATALOG_CACHE is not None:
            return _CATALOG_CACHE

    tex_info = _load_decorations_tex_info()
    if not DECORATIONS_PSO.is_file():
        empty = {"decorations": {}, "composites": {}, "tex_info": tex_info, "skin_dec_ids": set()}
        with _LOCK:
            _CATALOG_CACHE = empty
        return empty

    secs, _ = decode_pso.sections_of(DECORATIONS_PSO.read_bytes())
    dec = decode_pso.Decoder(secs, {})
    raw_blks: dict[int, tuple[int, bytes]] = {
        i + 1: (k, data) for i, (k, data, _) in enumerate(dec.blocks)
    }

    # Index Block 9 (0x91501997, 24B records) -> Block 7 (0x0000004a) child dec_id arrays
    composites: dict[int, list[int]] = {}
    for _bn, (bk, data) in raw_blks.items():
        if bk != 0x91501997:
            continue
        for off in range(0, len(data) - 23, 24):
            cid = struct.unpack_from("<I", data, off)[0]
            cnt, _cap, pb = struct.unpack_from("<IIQ", data, off + 8)
            tb, toff = ptr(pb)
            if cnt and tb in raw_blks and toff + cnt * 4 <= len(raw_blks[tb][1]):
                composites[cid] = list(struct.unpack_from(f"<{cnt}I", raw_blks[tb][1], toff))

    # Parse Block 11 (0x40e79f52, 224B records) + linked 0x77f0121d (320B) layers
    decorations: dict[int, dict[str, Any]] = {}
    skin_dec_ids: set[int] = set()
    ident_m = (1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0)

    for _bn, (bk, data) in raw_blks.items():
        if bk != 0x40E79F52:
            continue
        for off in range(0, len(data) - 223, 224):
            dec_id, ca54, dec_mode, dec_flags = struct.unpack_from("<IIII", data, off + 4)
            fade_f = struct.unpack_from("<f", data, off + 20)[0]
            base_m = struct.unpack_from("<12f", data, off + 32)
            dcat = struct.unpack_from("<I", data, off + 108)[0]
            l_cnt, _l_cap, l_ptr = struct.unpack_from("<IIQ", data, off + 160)

            target = SKIN_TARGET_BY_DCAT.get(dcat, "clothing")
            if target != "clothing":
                skin_dec_ids.add(dec_id)

            tb, toff = ptr(l_ptr)
            layers: list[dict[str, Any]] = []
            if l_cnt and tb in raw_blks and raw_blks[tb][0] == 0x77F0121D:
                ldata = raw_blks[tb][1]
                for li in range(l_cnt):
                    lo = toff + li * 320
                    if lo + 320 > len(ldata):
                        break
                    slots = struct.unpack_from("<5Q", ldata, lo + 8)
                    alb_u64 = slots[0]
                    if not alb_u64:
                        continue
                    alb = f"{alb_u64:016x}"
                    nrm = f"{slots[1]:016x}" if slots[1] else ""
                    mat_tex = f"{slots[2]:016x}" if slots[2] else ""
                    ti = tex_info.get(alb)
                    has_tex = ti is not None
                    if nrm and nrm not in tex_info:
                        nrm = ""

                    c10 = struct.unpack_from("<5I", ldata, lo + 48)
                    raw_tints = struct.unpack_from("<3I", ldata, lo + 68)
                    l_flags = struct.unpack_from("<I", ldata, lo + 208)[0]
                    op1, op2 = struct.unpack_from("<ff", ldata, lo + 212)
                    l_mode = struct.unpack_from("<I", ldata, lo + 220)[0]
                    sub_m = struct.unpack_from("<12f", ldata, lo + 224)

                    if not math.isfinite(op1) or op1 < 0.0:
                        op1 = 0.0
                    if not math.isfinite(op2) or op2 <= 0.0:
                        op2 = 1.0
                    eff_op = max(0.0, min(1.0, op1 * op2))

                    if any(abs(v) > 1e-6 for v in sub_m[:6]):
                        aff = _mul_affine2d(base_m, sub_m)
                    elif any(abs(v) > 1e-6 for v in base_m[:6]):
                        aff = (base_m[0], base_m[1], base_m[4], base_m[5], base_m[8], base_m[9])
                    else:
                        aff = (ident_m[0], ident_m[1], ident_m[4], ident_m[5], ident_m[8], ident_m[9])

                    uv = _decompose_uv_affine(*aff)
                    uv["mirrorU"] = bool(l_flags & LAYER_FLAG_MIRROR_U)

                    tints, tint_mode = decode_tint_palette(raw_tints)
                    gloss, shimmer = _decode_mat_override(c10)

                    layers.append({
                        "layerIndex": li,
                        "albedo": alb,
                        "normal": nrm,
                        "matTex": mat_tex if mat_tex in tex_info else "",
                        "hasTexture": has_tex,
                        "width": ti[0] if ti else 0,
                        "height": ti[1] if ti else 0,
                        "format": ti[2] if ti else "",
                        "opacity": round(eff_op, 4),
                        "tints": tints,
                        "tintMode": tint_mode,
                        "primaryHex": tints[0]["hex"] if tint_mode == "mask" else "#ffffff",
                        "gloss": gloss,
                        "shimmer": shimmer,
                        "flags": l_flags,
                        "mode": l_mode,
                        "uv": uv,
                    })

            decorations[dec_id] = {
                "decId": dec_id,
                "decHex": f"0x{dec_id:08x}",
                "dcat": dcat,
                "target": target,
                "ca54": ca54,
                "decMode": dec_mode,
                "decFlags": dec_flags,
                "fade": round(max(0.0, min(1.0, fade_f)) if math.isfinite(fade_f) else 0.0, 4),
                "layers": layers,
            }

    cat = {
        "decorations": decorations,
        "composites": composites,
        "tex_info": tex_info,
        "skin_dec_ids": skin_dec_ids,
    }
    with _LOCK:
        _CATALOG_CACHE = cat
    return cat


def load_tattoo_catalog(target: str = "") -> list[dict[str, Any]]:
    """Return all skin-target tattoo decorations from ``00001ae00000.pso`` with accurate HSVA tints & UV math.

    Optional ``target`` filters to ``"head"``, ``"upper"``, or ``"lower"``.
    Formatted to match ``viewer/char-effects.js``'s tattoo browser & UV baker schema.
    """
    cat = load_decoration_catalog()
    out: list[dict[str, Any]] = []
    seen_albedo: set[str] = set()
    for dec_id, rec in cat["decorations"].items():
        tgt = rec["target"]
        if tgt == "clothing":
            continue
        if target and tgt != target:
            continue
        # ca54 == 1 denotes tattoos (whereas 3/5/7/21 are makeup/damage overlays)
        if rec["ca54"] != 1:
            continue
        for lay in rec["layers"]:
            if not lay["hasTexture"] or lay["opacity"] <= 0.01:
                continue
            alb = lay["albedo"]
            if alb in seen_albedo:
                continue
            seen_albedo.add(alb)
            uv = lay["uv"]
            tint_hex = lay["primaryHex"]
            out.append({
                "index": len(out),
                "id": rec["decHex"],
                "target": tgt,
                "zone": TARGET_DEFAULT_ZONE.get(tgt, "Body"),
                "albedo": alb,
                "normal": lay["normal"],
                "width": lay["width"],
                "height": lay["height"],
                "format": lay["format"],
                "opacity": lay["opacity"],
                "fade": rec["fade"],
                "tint": tint_hex,
                "tints": [t["hex"] for t in lay["tints"] if t["enabled"]],
                "tintPalette": lay["tints"],
                "tintMode": lay["tintMode"],
                "u": uv["u"],
                "v": uv["v"],
                "su": uv["su"],
                "sv": uv["sv"],
                "angle": uv["angle"],
                "flipU": uv["flipU"],
                "mirrorU": uv["mirrorU"],
            })
            break
    return out


# ---------------------------------------------------------------------------
# 0DRP Loading Helper (On-Demand Single Ped, ~2ms)
# ---------------------------------------------------------------------------
def _load_spans_index() -> tuple[list[tuple[int, int, int, int, int]], dict[int, int]]:
    global _SPANS_CACHE
    with _LOCK:
        if _SPANS_CACHE is not None:
            return _SPANS_CACHE
    raw = PACKS_SPANS.read_bytes()
    srows = [struct.unpack_from("<QQQII", raw, i * 32) for i in range(len(raw) // 32)]
    off_to_idx = {r[0]: i for i, r in enumerate(srows)}
    val = (srows, off_to_idx)
    with _LOCK:
        _SPANS_CACHE = val
    return val


def load_0drp_blocks_at_offset(drp_offset: int) -> dict[int, tuple[int, bytes]]:
    """Decompress and parse a single ped's 0DRP block table from ``packs.rpf`` at ``drp_offset``."""
    if not drp_offset or not PACKS_RPF.is_file() or not PACKS_SPANS.is_file():
        return {}
    srows, off_to_idx = _load_spans_index()
    idx = off_to_idx.get(int(drp_offset))
    if idx is None:
        return {}
    n_s = len(srows)
    with open(PACKS_RPF, "rb") as f:
        off, clen, *_ = srows[idx]
        f.seek(off)
        buf = bytearray(zlib.decompress(f.read(clen)))
        j = idx
        while (
            j + 1 < n_s
            and ((srows[j][0] + srows[j][1] + 15) & -16) == srows[j + 1][0]
            and len(buf) % 65536 == 0
        ):
            j += 1
            f.seek(srows[j][0])
            buf.extend(zlib.decompress(f.read(srows[j][1])))
    _rk, blks = parse_0drp_blocks(buf)
    return blks or {}


# ---------------------------------------------------------------------------
# Core Per-Ped Extraction (0DRP -> Tattoos & Facial/Body Overlays)
# ---------------------------------------------------------------------------
def _resolve_dec_ids(raw_ids: list[int] | tuple[int, ...], catalog: dict[str, Any]) -> list[int]:
    """Expand raw ``0xd9f1087b`` decoration IDs, including Block 9 (``0x91501997``) composite sets."""
    decs = catalog["decorations"]
    comps = catalog["composites"]
    out: list[int] = []
    seen: set[int] = set()
    for did in raw_ids:
        if did in decs:
            if did not in seen:
                seen.add(did)
                out.append(did)
        elif did in comps:
            for cid in comps[did]:
                if cid in decs and cid not in seen:
                    seen.add(cid)
                    out.append(cid)
    return out


def _parse_0drp_outfits(blks: dict[int, tuple[int, bytes]]) -> list[tuple[int, list[int]]]:
    """Parse ``0xa9d57751`` (80B outfit records) -> ``[(outfit_id, [preset_ids...]), ...]``."""
    outfits: list[tuple[int, list[int]]] = []
    for _bn, (bk, data) in blks.items():
        if bk != 0xA9D57751:
            continue
        for oi in range(len(data) // 80):
            rec = data[oi * 80:(oi + 1) * 80]
            oid = struct.unpack_from("<I", rec, 0)[0]
            # 0x67e54cb2 array count sits at offset 56 (u32), pointer at offset 64 (u64)
            cnt = struct.unpack_from("<I", rec, 56)[0]
            qv = struct.unpack_from("<Q", rec, 64)[0]
            pb, poff = ptr(qv)
            pids: list[int] = []
            if 0 < cnt < 512 and pb in blks and blks[pb][0] == 0x67E54CB2:
                bd = blks[pb][1]
                for ei in range(cnt):
                    eo = poff + ei * 8
                    if eo + 8 > len(bd):
                        break
                    pids.append(struct.unpack_from("<I", bd, eo)[0])
            outfits.append((oid, pids))
    return outfits


def _is_damage_makeup_dec(dec_rec: dict[str, Any]) -> bool:
    """Detect mission dirt/bruise/bloody-face overlays (``ca54 == 3`` or >= 25 stacked layers)."""
    return dec_rec["ca54"] == 3 or len(dec_rec["layers"]) >= 25


def _infer_makeup_kind(cat: int, target: str, dec_rec: dict[str, Any]) -> tuple[str, str]:
    if cat in MAKEUP_KIND_BY_CAT:
        kind, label = MAKEUP_KIND_BY_CAT[cat]
        if kind == "fullMakeup" and _is_damage_makeup_dec(dec_rec):
            return ("damageOverlay", "Dirt & Bruise Look")
        return (kind, label)
    if target == "upper":
        return ("bodyShimmer", "Body Shimmer")
    if target == "lower":
        return ("bodyOverlay", "Body Overlay")
    return ("faceOverlay", "Face Overlay")


def _build_tattoo_entries(
    pid: int,
    cat: int,
    dec_ids: list[int],
    catalog: dict[str, Any],
    outfit_default: bool,
) -> list[dict[str, Any]]:
    decs = catalog["decorations"]
    out: list[dict[str, Any]] = []
    for did in dec_ids:
        drec = decs.get(did)
        if not drec or drec["target"] == "clothing":
            continue
        tgt = drec["target"]
        zone = TATTOO_ZONE_BY_CAT.get(cat, TARGET_DEFAULT_ZONE.get(tgt, "Body"))
        for lay in drec["layers"]:
            if not lay["hasTexture"] or lay["opacity"] <= 0.01:
                continue
            uv = lay["uv"]
            tint_hex = lay["primaryHex"]
            swatch = color_label(tint_hex, is_tattoo=True) if lay["tintMode"] == "mask" else "Full Color"
            label = f"{zone} Tattoo ({swatch})"
            enabled_tints = [t["hex"] for t in lay["tints"] if t["enabled"]]
            fade = drec["fade"] if drec["fade"] > 0 else 0.10
            out.append({
                "id": f"{drec['decHex']}:{lay['layerIndex']}",
                "decId": drec["decHex"],
                "presetId": f"0x{pid:08x}",
                "layerIndex": lay["layerIndex"],
                "target": tgt,
                "zone": zone,
                "zoneHash": f"0x{cat:08x}",
                "label": label,
                "hash": lay["albedo"],
                "albedo": lay["albedo"],
                "normal": lay["normal"],
                "width": lay["width"],
                "height": lay["height"],
                "format": lay["format"],
                "opacity": lay["opacity"],
                "fade": fade,
                "scaleMult": 1.0,
                "offU": 0.0,
                "offV": 0.0,
                "rotDeg": 0.0,
                "tint": tint_hex,
                "tints": lay["tints"],
                "tintHexes": enabled_tints,
                "tintMode": lay["tintMode"],
                "blendMode": "tattoo",
                "outfitDefault": bool(outfit_default),
                "uvTransform": dict(uv),
                # Direct drop-in object matching viewer/char-effects.js ST.appliedTattoos[i].tattoo
                "tattoo": {
                    "id": drec["decHex"],
                    "albedo": lay["albedo"],
                    "normal": lay["normal"],
                    "width": lay["width"],
                    "height": lay["height"],
                    "format": lay["format"],
                    "tint": tint_hex,
                    "tints": enabled_tints,
                    "tintPalette": lay["tints"],
                    "tintMode": lay["tintMode"],
                    "u": uv["u"],
                    "v": uv["v"],
                    "su": uv["su"],
                    "sv": uv["sv"],
                    "angle": uv["angle"],
                    "flipU": uv["flipU"],
                    "mirrorU": uv["mirrorU"],
                },
            })
    return out


def _build_overlay_entries(
    pid: int,
    cat: int,
    dec_ids: list[int],
    catalog: dict[str, Any],
    outfit_default: bool,
) -> list[dict[str, Any]]:
    decs = catalog["decorations"]
    out: list[dict[str, Any]] = []
    for did in dec_ids:
        drec = decs.get(did)
        if not drec or drec["target"] == "clothing":
            continue
        tgt = drec["target"]
        kind, kind_label = _infer_makeup_kind(cat, tgt, drec)
        is_damage = _is_damage_makeup_dec(drec)
        for lay in drec["layers"]:
            if not lay["hasTexture"] or lay["opacity"] <= 0.01:
                continue
            uv = lay["uv"]
            enabled_tints = [t["hex"] for t in lay["tints"] if t["enabled"]]
            color_hex = lay["primaryHex"]
            display_hex = color_hex
            if lay["tintMode"] == "mask" and enabled_tints:
                # If slot 0 is pure/near black (e.g. contour shadow) and slot 1/2 carries blush/rose tint, use it for the label swatch
                r0, g0, b0 = lay["tints"][0]["rgb"]
                if max(r0, g0, b0) < 42 and len(enabled_tints) > 1:
                    display_hex = max(
                        (t for t in lay["tints"] if t["enabled"]),
                        key=lambda t: max(t["rgb"]),
                    )["hex"]
            swatch = color_label(display_hex, is_tattoo=False) if lay["tintMode"] == "mask" else "Natural"
            label = f"{kind_label} ({swatch})"
            blend_mode = "multiply" if is_damage else "source-over"
            out.append({
                "id": f"{drec['decHex']}:{lay['layerIndex']}",
                "decId": drec["decHex"],
                "presetId": f"0x{pid:08x}",
                "layerIndex": lay["layerIndex"],
                "kind": kind,
                "kindLabel": kind_label,
                "categoryHash": f"0x{cat:08x}",
                "target": tgt,
                "label": label,
                "hash": lay["albedo"],
                "normal": lay["normal"],
                "width": lay["width"],
                "height": lay["height"],
                "format": lay["format"],
                "color": display_hex,
                "tints": lay["tints"],
                "tintHexes": enabled_tints,
                "tintMode": lay["tintMode"],
                "opacity": lay["opacity"],
                "blendMode": blend_mode,
                "gloss": lay["gloss"],
                "shimmer": lay["shimmer"],
                "outfitDefault": bool(outfit_default),
                "isDamageLook": bool(is_damage),
                "uvTransform": dict(uv),
            })
    return out


def extract_ped_tattoos_and_overlays(
    blks: dict[int, tuple[int, bytes]],
    arch_hash: int = 0,
    ped_name: str = "",
    include_alternates: bool = True,
    catalog: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Extract a ped's character-native tattoos & facial/body makeup overlays from parsed 0DRP blocks.

    Parameters
    ----------
    blks:
        Parsed 0DRP blocks dict ``{block_num: (block_type_u32, bytes)}`` from ``parse_0drp_blocks``.
    arch_hash:
        Optional ped archetype Jenkins u32 hash.
    ped_name:
        Optional ped display name (e.g. ``"cs_mandy"``).
    include_alternates:
        When True (default), also returns ``allFaceOverlays`` and ``makeupLooks`` for alternate
        wardrobe/scene makeup looks so Wave 2 UI can offer 1-click native look switching.
    catalog:
        Optional pre-loaded result of ``load_decoration_catalog()`` (avoids lock lookup in batch loops).
    """
    if catalog is None:
        catalog = load_decoration_catalog()

    # 1. Collect all cosmetic skin presets (0xf795e8ba with tag 0xd9f1087b and domain tattoo/makeup)
    tattoo_presets: dict[int, tuple[int, list[int]]] = {}   # pid -> (cat, [dec_ids])
    makeup_presets: dict[int, tuple[int, list[int]]] = {}   # pid -> (cat, [dec_ids])
    decs = catalog["decorations"]

    for _bn, (bk, data) in blks.items():
        if bk != 0xF795E8BA:
            continue
        for i in range(len(data) // 88):
            r = data[i * 88:(i + 1) * 88]
            pid = struct.unpack_from("<I", r, 0)[0]
            cat = struct.unpack_from("<I", r, 24)[0]
            p2 = struct.unpack_from("<Q", r, 48)[0]
            c2 = struct.unpack_from("<H", r, 56)[0]
            if not c2:
                continue
            tags = read_tags(blks, p2, c2)
            raw_dids = tags.get(TAG_DECORATION_ID)
            if not raw_dids:
                continue
            bc24 = tags.get(TAG_COSMETIC_DOMAIN, ())
            is_tattoo = DOMAIN_TATTOO in bc24
            is_makeup = DOMAIN_MAKEUP in bc24
            if not is_tattoo and not is_makeup:
                continue
            resolved = [
                did for did in _resolve_dec_ids(raw_dids, catalog)
                if decs[did]["target"] != "clothing"
            ]
            if not resolved:
                continue
            if is_tattoo:
                tattoo_presets[pid] = (cat, resolved)
            else:
                makeup_presets[pid] = (cat, resolved)

    # 2. Parse 0xa9d57751 -> 0x67e54cb2 outfits to distinguish default vs alternate overlays
    outfits = _parse_0drp_outfits(blks)

    # Permanent/base tattoos live in Outfit[0] (or Outfit[1] for protagonists like Jason).
    # For ambient/crowd peds that put tattoos across outfits without Outfit[0], fall back to the
    # first outfit that carries tattoos (or all tattoo presets if 0xa9d57751 has none).
    default_tat_pids: list[int] = []
    seen_tat_pids: set[int] = set()
    for oi, (_oid, pids) in enumerate(outfits[:2]):
        for p in pids:
            if p in tattoo_presets and p not in seen_tat_pids:
                seen_tat_pids.add(p)
                default_tat_pids.append(p)
    if not default_tat_pids and tattoo_presets:
        # Check remaining outfits for the first outfit carrying tattoos
        for _oid, pids in outfits[2:]:
            found = [p for p in pids if p in tattoo_presets]
            if found:
                for p in found:
                    if p not in seen_tat_pids:
                        seen_tat_pids.add(p)
                        default_tat_pids.append(p)
                break
    if not default_tat_pids and tattoo_presets:
        # Fallback: include first preset per unique zone category (avoids stacking duplicate alt variants)
        seen_cats: set[int] = set()
        for p, (cat, _dids) in tattoo_presets.items():
            if cat not in seen_cats:
                seen_cats.add(cat)
                seen_tat_pids.add(p)
                default_tat_pids.append(p)

    # Collect unique makeup looks across all outfits in 0xa9d57751
    look_order: list[tuple[tuple[int, ...], int, int]] = []  # (sorted_pids_key, first_outfit_idx, first_oid)
    look_freq: collections.Counter[tuple[int, ...]] = collections.Counter()
    look_first: dict[tuple[int, ...], tuple[int, int, list[int]]] = {}
    for oi, (oid, pids) in enumerate(outfits):
        mk_in_outfit = [p for p in pids if p in makeup_presets]
        if not mk_in_outfit:
            continue
        # Preserve preset order while deduplicating identical sets
        uniq_ordered = list(dict.fromkeys(mk_in_outfit))
        key = tuple(sorted(uniq_ordered))
        look_freq[key] += 1
        if key not in look_first:
            look_first[key] = (oi, oid, uniq_ordered)
            look_order.append((key, oi, oid))

    # Also capture any makeup presets not referenced in 0xa9d57751
    referenced_mk_pids = {p for key in look_first for p in key}
    orphan_mk_pids = [p for p in makeup_presets if p not in referenced_mk_pids]
    if orphan_mk_pids and not look_order:
        # Group non-damage orphan presets by category (first per category) as a default look
        seen_cats = set()
        picked = []
        for p in orphan_mk_pids:
            cat, dids = makeup_presets[p]
            if any(_is_damage_makeup_dec(decs[d]) for d in dids):
                continue
            if cat not in seen_cats:
                seen_cats.add(cat)
                picked.append(p)
        if picked:
            key = tuple(sorted(picked))
            look_first[key] = (0, 0, picked)
            look_freq[key] = 1
            look_order.append((key, 0, 0))

    # Pick the ped's default active makeup look:
    # - Never auto-pick a damage/dirt/bruise look (ca54 == 3 or >= 25 layers) as default!
    # - Prefer a clean makeup look appearing in early outfits (Outfit[0..3], like cs_mandy's Outfit[2]!),
    #   or for peds with <= 12 outfits (NPCs), the earliest/most-common clean makeup look.
    # - For protagonists with 200+ outfits whose Outfit[0..14] are clean (Lucia, Jason), keep default
    #   face clean and expose all glam looks in makeupLooks!
    def _look_is_damage(pids_list: list[int]) -> bool:
        return all(
            any(_is_damage_makeup_dec(decs[d]) for d in makeup_presets[p][1])
            for p in pids_list
        )

    default_look_key: tuple[int, ...] | None = None
    early_cutoff = 4 if len(outfits) > 20 else max(4, len(outfits))
    for key, oi, _oid in look_order:
        _, _, ordered_pids = look_first[key]
        if _look_is_damage(ordered_pids):
            continue
        if oi < early_cutoff:
            default_look_key = key
            break

    default_mk_pids = set(look_first[default_look_key][2]) if default_look_key is not None else set()

    # 3. Build structured tattoo entries
    tattoos_out: list[dict[str, Any]] = []
    for p in default_tat_pids:
        cat, dids = tattoo_presets[p]
        tattoos_out.extend(_build_tattoo_entries(p, cat, dids, catalog, outfit_default=True))
    if include_alternates:
        for p, (cat, dids) in tattoo_presets.items():
            if p in seen_tat_pids:
                continue
            tattoos_out.extend(_build_tattoo_entries(p, cat, dids, catalog, outfit_default=False))

    # 4. Build structured face/body overlay entries & grouped makeupLooks
    default_overlays: list[dict[str, Any]] = []
    all_overlays: list[dict[str, Any]] = []
    overlays_by_pid: dict[int, list[dict[str, Any]]] = {}

    for p, (cat, dids) in makeup_presets.items():
        is_def = p in default_mk_pids
        ents = _build_overlay_entries(p, cat, dids, catalog, outfit_default=is_def)
        overlays_by_pid[p] = ents
        all_overlays.extend(ents)

    if default_look_key is not None:
        for p in look_first[default_look_key][2]:
            default_overlays.extend(overlays_by_pid.get(p, ()))

    makeup_looks: list[dict[str, Any]] = []
    for li, (key, oi, oid) in enumerate(look_order):
        _, _, ordered_pids = look_first[key]
        is_def = (key == default_look_key)
        is_dmg = _look_is_damage(ordered_pids)
        kind_labels: list[str] = []
        look_overlays: list[dict[str, Any]] = []
        for p in ordered_pids:
            ents = overlays_by_pid.get(p, [])
            look_overlays.extend(ents)
            for e in ents:
                kl = e["kindLabel"]
                if kl not in kind_labels:
                    kind_labels.append(kl)
        if not look_overlays:
            continue
        summary = " + ".join(kind_labels[:4])
        prefix = "Default Look" if is_def else ("Damage / Dirt" if is_dmg else f"Look {li + 1}")
        makeup_looks.append({
            "id": f"look_{li}",
            "outfitIndex": oi,
            "outfitId": f"0x{oid:08x}",
            "outfitCount": int(look_freq[key]),
            "label": f"{prefix}: {summary}" if summary else prefix,
            "isDefault": is_def,
            "isDamageLook": is_dmg,
            "presetIds": [f"0x{p:08x}" for p in ordered_pids],
            "overlayCount": len(look_overlays),
            "overlays": look_overlays,
        })

    # Include ungrouped orphan presets as an extra look if any exist
    if orphan_mk_pids and look_order:
        orphan_ents = [e for p in orphan_mk_pids for e in overlays_by_pid.get(p, ())]
        if orphan_ents:
            makeup_looks.append({
                "id": f"look_{len(makeup_looks)}",
                "outfitIndex": -1,
                "outfitId": "0x00000000",
                "outfitCount": 0,
                "label": "Additional Character Overlays",
                "isDefault": False,
                "isDamageLook": all(e["isDamageLook"] for e in orphan_ents),
                "presetIds": [f"0x{p:08x}" for p in orphan_mk_pids],
                "overlayCount": len(orphan_ents),
                "overlays": orphan_ents,
            })

    default_tat_count = sum(1 for t in tattoos_out if t["outfitDefault"])
    res: dict[str, Any] = {
        "pedName": ped_name,
        "archHash": f"0x{int(arch_hash) & 0xFFFFFFFF:08x}" if arch_hash else "",
        "tattoos": tattoos_out,
        "faceOverlays": default_overlays,
        "counts": {
            "tattoos": len(tattoos_out),
            "defaultTattoos": default_tat_count,
            "faceOverlays": len(default_overlays),
            "allFaceOverlays": len(all_overlays),
            "makeupLooks": len(makeup_looks),
            "tattooPresets": len(tattoo_presets),
            "makeupPresets": len(makeup_presets),
        },
    }
    if include_alternates:
        res["allFaceOverlays"] = all_overlays
        res["makeupLooks"] = makeup_looks
    return res


def extract_ped_overlays_by_drp_offset(
    drp_offset: int,
    arch_hash: int = 0,
    ped_name: str = "",
    include_alternates: bool = True,
) -> dict[str, Any]:
    """Load a ped's 0DRP from ``packs.rpf`` at ``drp_offset`` and extract tattoos & overlays."""
    blks = load_0drp_blocks_at_offset(drp_offset)
    if not blks:
        return {
            "pedName": ped_name,
            "archHash": f"0x{int(arch_hash) & 0xFFFFFFFF:08x}" if arch_hash else "",
            "tattoos": [],
            "faceOverlays": [],
            "allFaceOverlays": [] if include_alternates else [],
            "makeupLooks": [] if include_alternates else [],
            "counts": {
                "tattoos": 0, "defaultTattoos": 0, "faceOverlays": 0,
                "allFaceOverlays": 0, "makeupLooks": 0, "tattooPresets": 0, "makeupPresets": 0,
            },
        }
    return extract_ped_tattoos_and_overlays(
        blks,
        arch_hash=arch_hash,
        ped_name=ped_name,
        include_alternates=include_alternates,
    )


def extract_ped_overlays_for_archetype(
    ped_name_or_hash: str | int,
    include_alternates: bool = True,
) -> dict[str, Any]:
    """Look up a ped in ``ped_archetypes.sqlite3`` by name, head drawable, or arch_hash and extract overlays."""
    if not PED_ARCH_DB.is_file():
        return extract_ped_overlays_by_drp_offset(0, ped_name=str(ped_name_or_hash))
    row = None
    with closing(_ro_db(PED_ARCH_DB)) as db:
        if isinstance(ped_name_or_hash, int):
            row = db.execute(
                "SELECT name, arch_hash, drp_offset FROM ped_archetype WHERE arch_hash=? LIMIT 1",
                (ped_name_or_hash & 0xFFFFFFFF,),
            ).fetchone()
        else:
            key = str(ped_name_or_hash or "").strip()
            kl = key.lower()
            if kl.startswith("0x"):
                try:
                    ah = int(kl, 16)
                    row = db.execute(
                        "SELECT name, arch_hash, drp_offset FROM ped_archetype WHERE arch_hash=? LIMIT 1",
                        (ah,),
                    ).fetchone()
                except ValueError:
                    row = None
            if row is None and kl:
                row = db.execute(
                    "SELECT name, arch_hash, drp_offset FROM ped_archetype "
                    "WHERE lower(name)=? OR lower(head_drawable)=? LIMIT 1",
                    (kl, kl),
                ).fetchone()
    if row is None or not row["drp_offset"]:
        return extract_ped_overlays_by_drp_offset(0, ped_name=str(ped_name_or_hash))
    return extract_ped_overlays_by_drp_offset(
        int(row["drp_offset"]),
        arch_hash=int(row["arch_hash"]),
        ped_name=str(row["name"]),
        include_alternates=include_alternates,
    )


# ---------------------------------------------------------------------------
# Global Browsable Makeup & Skin Overlay Presets Catalog (from 00001ae00000.pso)
# ---------------------------------------------------------------------------
def _classify_makeup_dec_heuristic(rec: dict[str, Any]) -> tuple[str, str]:
    """Classify a non-tattoo ``0x40e79f52`` skin decoration into a makeup/overlay kind from UV & metadata."""
    if rec["target"] == "upper":
        return ("bodyShimmer", "Body Shimmer")
    if rec["target"] == "lower":
        return ("bodyOverlay", "Body Overlay")
    if _is_damage_makeup_dec(rec):
        return ("damageOverlay", "Dirt & Bruise")
    layers = [l for l in rec["layers"] if l["hasTexture"] and l["opacity"] > 0.01]
    if not layers:
        return ("faceOverlay", "Face Overlay")
    l0 = layers[0]
    u, v = l0["uv"]["u"], l0["uv"]["v"]
    su, sv = l0["uv"]["su"], l0["uv"]["sv"]
    # Lipstick sits squarely on mouth UV center ~(0.50, 0.50), size ~(0.08..0.18)
    if len(layers) <= 2 and 0.45 <= u <= 0.55 and 0.45 <= v <= 0.55 and 0.05 <= su <= 0.22 and 0.05 <= sv <= 0.22:
        return ("lipstick", "Lipstick")
    # Eye makeup sits around U ~ 0.37..0.44 or 0.56..0.63, V ~ 0.27..0.36
    if ((0.35 <= u <= 0.45) or (0.55 <= u <= 0.65)) and 0.25 <= v <= 0.37 and su <= 0.22:
        # Eyeliner is typically dark/high-opacity single or 2-layer; eyeshadow is softer/colorful
        p_hex = l0["primaryHex"].lstrip("#")
        r0, g0, b0 = int(p_hex[0:2], 16), int(p_hex[2:4], 16), int(p_hex[4:6], 16)
        lum = (r0 + g0 + b0) / (3.0 * 255.0)
        if lum < 0.18 and l0["opacity"] >= 0.70 and len(layers) <= 2:
            return ("eyeliner", "Eyeliner")
        return ("eyeshadow", "Eyeshadow")
    # Full-face blush/contour/complexion has su >= 0.75, sv >= 0.75 and <= 3 layers
    if su >= 0.75 and sv >= 0.75 and len(layers) <= 3:
        return ("blush", "Blush & Contour")
    return ("fullMakeup", "Full Face Look")


def _load_makeup_preset_index() -> list[dict[str, Any]]:
    global _MAKEUP_PRESET_INDEX_CACHE
    with _LOCK:
        if _MAKEUP_PRESET_INDEX_CACHE is not None:
            return _MAKEUP_PRESET_INDEX_CACHE

    cat = load_decoration_catalog()
    presets: list[dict[str, Any]] = []
    for dec_id, rec in cat["decorations"].items():
        if rec["target"] == "clothing" or rec["ca54"] == 1:
            continue
        valid_layers = [l for l in rec["layers"] if l["hasTexture"] and l["opacity"] > 0.01]
        if not valid_layers:
            continue
        kind, kind_label = _classify_makeup_dec_heuristic(rec)
        l0 = valid_layers[0]
        swatch = color_label(l0["primaryHex"], is_tattoo=False) if l0["tintMode"] == "mask" else "Natural"
        presets.append({
            "id": rec["decHex"],
            "kind": kind,
            "kindLabel": kind_label,
            "target": rec["target"],
            "label": f"{kind_label} · {swatch}",
            "color": l0["primaryHex"],
            "previewHash": l0["albedo"],
            "layerCount": len(valid_layers),
            "isDamageLook": _is_damage_makeup_dec(rec),
            "layers": [
                {
                    "id": f"{rec['decHex']}:{l['layerIndex']}",
                    "decId": rec["decHex"],
                    "layerIndex": l["layerIndex"],
                    "kind": kind,
                    "kindLabel": kind_label,
                    "target": rec["target"],
                    "hash": l["albedo"],
                    "normal": l["normal"],
                    "width": l["width"],
                    "height": l["height"],
                    "format": l["format"],
                    "color": l["primaryHex"],
                    "tints": l["tints"],
                    "tintHexes": [t["hex"] for t in l["tints"] if t["enabled"]],
                    "tintMode": l["tintMode"],
                    "opacity": l["opacity"],
                    "blendMode": "multiply" if _is_damage_makeup_dec(rec) else "source-over",
                    "gloss": l["gloss"],
                    "shimmer": l["shimmer"],
                    "uvTransform": dict(l["uv"]),
                }
                for l in valid_layers
            ],
        })

    # Stable, pleasant ordering: lipstick -> eyeliner -> eyeshadow -> blush -> fullMakeup -> bodyShimmer -> damageOverlay
    order = {
        "lipstick": 0, "eyeliner": 1, "eyeshadow": 2, "blush": 3,
        "fullMakeup": 4, "bodyShimmer": 5, "bodyOverlay": 6, "damageOverlay": 7, "faceOverlay": 8,
    }
    presets.sort(key=lambda p: (order.get(p["kind"], 9), p["label"], p["id"]))
    with _LOCK:
        _MAKEUP_PRESET_INDEX_CACHE = presets
    return presets


def list_overlay_preset_kinds() -> list[dict[str, Any]]:
    """Return available browsable makeup/skin-overlay preset categories with counts."""
    idx = _load_makeup_preset_index()
    counts: collections.Counter[tuple[str, str]] = collections.Counter()
    for p in idx:
        counts[(p["kind"], p["kindLabel"])] += 1
    order = {
        "lipstick": 0, "eyeliner": 1, "eyeshadow": 2, "blush": 3,
        "fullMakeup": 4, "bodyShimmer": 5, "bodyOverlay": 6, "damageOverlay": 7, "faceOverlay": 8,
    }
    return [
        {"kind": k, "label": lbl, "count": cnt}
        for (k, lbl), cnt in sorted(counts.items(), key=lambda kv: order.get(kv[0][0], 9))
    ]


def get_overlay_presets(
    kind: str = "",
    q: str = "",
    offset: int = 0,
    limit: int = 36,
    include_damage: bool = True,
) -> dict[str, Any]:
    """Search & paginate browsable makeup/skin-overlay presets backed by ``textures.sqlite3``."""
    all_p = _load_makeup_preset_index()
    kl = (kind or "").strip()
    ql = (q or "").strip().lower().removeprefix("0x")
    filt = []
    for p in all_p:
        if not include_damage and p["isDamageLook"]:
            continue
        if kl and p["kind"] != kl:
            continue
        if ql and ql not in p["label"].lower() and ql not in p["id"].lower() and ql not in p["previewHash"]:
            continue
        filt.append(p)
    off = max(0, int(offset))
    lim = max(1, min(120, int(limit)))
    return {
        "total": len(filt),
        "offset": off,
        "limit": lim,
        "kinds": list_overlay_preset_kinds(),
        "items": filt[off:off + lim],
    }


def get_overlay_preset_by_id(dec_id: str | int) -> dict[str, Any] | None:
    """Look up a single browsable makeup/skin-overlay preset by decoration ID (e.g. ``'0x9c6eaace'``)."""
    want = f"0x{int(dec_id, 16) & 0xFFFFFFFF:08x}" if isinstance(dec_id, str) else f"0x{int(dec_id) & 0xFFFFFFFF:08x}"
    for p in _load_makeup_preset_index():
        if p["id"] == want:
            return p
    return None
