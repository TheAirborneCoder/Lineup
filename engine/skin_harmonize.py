# SPDX-License-Identifier: GPL-3.0-or-later
"""Collar / neck (and waist) seam color & shading harmonization between GTA VI head and body skin textures.

Derived directly from 3D-matched seam boundary geometry between:
  - head_* (shader 379efcef, universal 8,983-vert RAGE 9 head topology across fr1/mr1/fo1/mo1/fb1/mb1/fc1/mc1/mi1)
  - hand_*_000_nude_cull (shader 210fbc6a, upper torso/arms/neck collar)
  - feet_*_000_nude_cull (shader 210fbc6a, lower torso/legs/waist)

At the neck seam, 29 boundary vertices match at 0.00000 m separation with 1.0000 normal dot product.
Each of the 24 seam-adjacent triangles around the 360-degree neck ring is sampled at barycentric
coordinates (0.35, 0.35, 0.30) — strictly inside the valid UV island (~2-4 mm from the UV edge)
to eliminate UV padding / dilation artifacts while staying directly across the physical seam.

Color-space convention:
  - The viewer's WebGL fragment shader (viewer/model-view.js) multiplies texture sRGB by `color`
    (`base = t.rgb * color`) BEFORE `toLinear(base) = pow(base, 2.2)`.
  - Exported glTF 2.0 PNGs (glb_textured.py) store sRGB `baseColorTexture`.
  - Therefore `gain` / `gain_srgb` is an sRGB RGB multiplier [r, g, b] so WebGL live preview and
    baked GLB export match identically. Because `(x * g)^2.2 == x^2.2 * g^2.2` below clipping,
    `gain_linear = [g ** 2.2 for g in gain_srgb]` is also returned for linear-space consumers.
"""
from __future__ import annotations

import io
import json
import math
import struct
import threading
from contextlib import closing
from typing import Any, Iterable, Sequence

import numpy as np
from PIL import Image

from paths import LIBRARY

# ---------------------------------------------------------------------------
# 3D-paired neck seam UV masks (glTF convention: u right, v down).
# Each entry: (azimuth_deg, (head_u, head_v), (body_u, body_v)), 24 per rig.
# They are derived from the user's own meshes the first time they are needed (derive_seam_masks_from_meshes) and
# cached in LIBRARY/neck_seams.json; nothing from the game is stored in this file. The pair of meshes per rig:
#   fr1: head_fr1_cau_150_y_full <-> hand_fr1_000_nude_cull
#   mr1: player_story000_01_head_mr1_cau_000_m <-> hand_mr1_000_nude_cull
# The UV layout is identical across fo1, mo1, fb1, mb1, fc1, mc1, mi1.
# ---------------------------------------------------------------------------
NECK_SEAM_MODELS = {
    "fr1": ("head_fr1_cau_150_y_full", "hand_fr1_000_nude_cull"),
    "mr1": ("player_story000_01_head_mr1_cau_000_m", "hand_mr1_000_nude_cull"),
}
NECK_SEAM_CACHE = LIBRARY / "neck_seams.json"
NECK_SEAM_VERSION = 1
_NECK_LOCK = threading.RLock()
_NECK_UV_ARRAYS: dict[str, tuple[np.ndarray, np.ndarray]] = {}


def neck_seam_arrays(lib: Any, rig_key: str) -> tuple[np.ndarray, np.ndarray]:
    """(head UVs, body UVs) of the paired neck seam points for a rig, derived and cached on first use."""
    with _NECK_LOCK:
        if rig_key in _NECK_UV_ARRAYS:
            return _NECK_UV_ARRAYS[rig_key]
        pairs = None
        try:
            stored = json.loads(NECK_SEAM_CACHE.read_text("utf-8"))
            if stored.get("version") == NECK_SEAM_VERSION:
                pairs = stored.get("pairs", {}).get(rig_key)
        except (OSError, ValueError):
            stored = {}
        if not pairs:
            head_name, hand_name = NECK_SEAM_MODELS[rig_key]
            ids = []
            with closing(lib.db()) as db:
                for name in (head_name, hand_name):
                    row = db.execute("select id from model where name=? order by is_rep desc, id limit 1", (name,)).fetchone()
                    if row is None:
                        raise LookupError(f"model {name} is not in the inventory")
                    ids.append(int(row[0]))
            pairs = derive_seam_masks_from_meshes(lib, ids[0], ids[1])
            if not pairs:
                raise ValueError("no neck seam found between the head and the upper body meshes")
            try:
                cache = {"version": NECK_SEAM_VERSION, "pairs": dict(stored.get("pairs", {})) if isinstance(stored, dict) else {}}
                cache["pairs"][rig_key] = pairs
                NECK_SEAM_CACHE.parent.mkdir(parents=True, exist_ok=True)
                tmp = NECK_SEAM_CACHE.with_suffix(".tmp")
                tmp.write_text(json.dumps(cache), "utf-8")
                tmp.replace(NECK_SEAM_CACHE)
            except OSError:
                pass
        arrays = (np.array([p[1] for p in pairs], dtype=np.float32), np.array([p[2] for p in pairs], dtype=np.float32))
        _NECK_UV_ARRAYS[rig_key] = arrays
        return arrays


# Precomputed 3D-paired waist seam UV masks: (azimuth_deg, (upper_hand_u, v), (lower_feet_u, v))
WAIST_SEAM_UV_PAIRS: dict[str, tuple[tuple[float, tuple[float, float], tuple[float, float]], ...]] = {
    "fr1": (
        (-161.9, (0.0943, 0.0467), (0.0922, 0.0622)),
        (-144.8, (0.1314, 0.0367), (0.1323, 0.0499)),
        (-122.9, (0.1844, 0.0272), (0.1797, 0.0365)),
        (-106.0, (0.2295, 0.0287), (0.2148, 0.0359)),
        (-89.3,  (0.2682, 0.0310), (0.2622, 0.0422)),
        (-70.1,  (0.3205, 0.0301), (0.3126, 0.0399)),
        (-52.7,  (0.3762, 0.0319), (0.3670, 0.0401)),
        (-35.2,  (0.4299, 0.0476), (0.4338, 0.0679)),
        (-15.9,  (0.4728, 0.0442), (0.4705, 0.0606)),
        (-5.6,   (0.4936, 0.0420), (0.4869, 0.0558)),
        (35.2,   (0.5711, 0.0476), (0.5672, 0.0679)),
        (52.7,   (0.6248, 0.0319), (0.6340, 0.0401)),
        (70.1,   (0.6806, 0.0301), (0.6885, 0.0399)),
        (89.3,   (0.7329, 0.0309), (0.7388, 0.0422)),
        (106.0,  (0.7716, 0.0287), (0.7863, 0.0359)),
        (122.9,  (0.8167, 0.0272), (0.8214, 0.0365)),
        (144.8,  (0.8697, 0.0367), (0.8687, 0.0499)),
        (161.9,  (0.9068, 0.0467), (0.9088, 0.0621)),
        (176.1,  (0.9302, 0.0371), (0.9349, 0.0484)),
    ),
    "mr1": (
        (-176.6, (0.0707, 0.0370), (0.0662, 0.0497)),
        (-161.8, (0.0943, 0.0467), (0.0963, 0.0620)),
        (-145.3, (0.1314, 0.0367), (0.1370, 0.0504)),
        (-124.8, (0.1844, 0.0272), (0.1835, 0.0376)),
        (-109.2, (0.2295, 0.0287), (0.2178, 0.0373)),
        (-94.0,  (0.2682, 0.0310), (0.2632, 0.0436)),
        (-75.0,  (0.3205, 0.0301), (0.3129, 0.0404)),
        (-56.1,  (0.3762, 0.0319), (0.3670, 0.0401)),
        (-38.3,  (0.4299, 0.0476), (0.4338, 0.0679)),
        (-16.5,  (0.4728, 0.0442), (0.4705, 0.0606)),
        (5.6,    (0.5074, 0.0420), (0.5141, 0.0558)),
        (27.5,   (0.5568, 0.0498), (0.5436, 0.0704)),
        (47.3,   (0.5912, 0.0381), (0.6041, 0.0447)),
        (65.5,   (0.6541, 0.0300), (0.6620, 0.0391)),
        (84.6,   (0.7065, 0.0303), (0.7132, 0.0434)),
        (103.3,  (0.7610, 0.0321), (0.7632, 0.0448)),
        (115.1,  (0.7911, 0.0248), (0.7936, 0.0341)),
        (135.2,  (0.8434, 0.0309), (0.8425, 0.0429)),
        (153.8,  (0.8950, 0.0463), (0.8827, 0.0612)),
        (169.9,  (0.9223, 0.0404), (0.9228, 0.0539)),
    ),
}

# Precomputed numpy arrays for fast vectorized sampling (the waist table; the neck arrays are built on first use)
_WAIST_UV_ARRAYS: dict[str, tuple[np.ndarray, np.ndarray]] = {
    rig: (
        np.array([p[1] for p in pairs], dtype=np.float32),
        np.array([p[2] for p in pairs], dtype=np.float32),
    )
    for rig, pairs in WAIST_SEAM_UV_PAIRS.items()
}

# Default clamp & safety parameters
DEFAULT_CLAMP_MIN = 0.80
DEFAULT_CLAMP_MAX = 1.25
DEFAULT_MAX_CHROMA_SPREAD = 0.14
DEFAULT_DEADBAND = 0.012
DEFAULT_SAMPLE_SIZE = 128

# Shading harmonization metadata discovered from shader inspection:
# ~55% of head meshes (379efcef) resolve shared specTex 9f9bc17e41f082b2 (sp.g=0.599 -> rough=0.572)
# while hand_*_000_nude_cull (210fbc6a) has no resolved specTex (fallback rough=0.540).
SHARED_HEAD_SPECTEX_HASH = "9f9bc17e41f082b2"
HEAD_WITH_SPECTEX_NECK_ROUGH = 0.572
BODY_NO_SPECTEX_ROUGH = 0.540
RECOMMENDED_UNIFIED_SKIN_ROUGH = 0.555

# Thread-safe in-memory cache: (tex_hash, rig_key, region, sample_size) -> np.ndarray shape (N, 3) float32 sRGB [0,1]
_SAMPLE_CACHE_LOCK = threading.RLock()
_SAMPLE_CACHE: dict[tuple[str, str, str, int], np.ndarray] = {}
_SAMPLE_CACHE_MAX = 4096


def normalize_rig(body_rig: str | None) -> str:
    """Map any RAGE 9 humanoid body rig token (fr1/fo1/fb1/fc1/mr1/mo1/mb1/mc1/mk1/mi1) to 'fr1' or 'mr1'."""
    r = (body_rig or "").strip().lower()
    if r.startswith("f"):
        return "fr1"
    return "mr1"


def clear_sample_cache() -> None:
    """Clear the in-memory UV ring color sample cache."""
    with _SAMPLE_CACHE_LOCK:
        _SAMPLE_CACHE.clear()


def _sample_bilinear_srgb(img_arr: np.ndarray, uvs: np.ndarray) -> np.ndarray:
    """Sample float32 sRGB [0,1] image (H, W, 3) at glTF UVs (u right, v down) using bilinear interpolation."""
    h, w, _ = img_arr.shape
    u = np.clip(uvs[:, 0], 0.0, 1.0) * (w - 1)
    v = np.clip(uvs[:, 1], 0.0, 1.0) * (h - 1)
    x0 = np.floor(u).astype(np.int32)
    y0 = np.floor(v).astype(np.int32)
    x1 = np.minimum(x0 + 1, w - 1)
    y1 = np.minimum(y0 + 1, h - 1)
    fx = (u - x0)[:, None]
    fy = (v - y0)[:, None]
    return (
        (img_arr[y0, x0] * (1.0 - fx) + img_arr[y0, x1] * fx) * (1.0 - fy)
        + (img_arr[y1, x0] * (1.0 - fx) + img_arr[y1, x1] * fx) * fy
    ).astype(np.float32)


def _srgb_to_linear_arr(rgb: np.ndarray) -> np.ndarray:
    """Convert sRGB [0,1] array to linear light using model-view.js gamma 2.2."""
    return np.power(np.clip(rgb, 0.0, 1.0), 2.2).astype(np.float32)


def _srgb_to_lab(rgb: np.ndarray) -> np.ndarray:
    """Convert sRGB [0,1] (..., 3) to CIE L*a*b* (D65)."""
    c = np.clip(np.asarray(rgb, dtype=np.float64), 0.0, 1.0)
    lin = np.where(c <= 0.04045, c / 12.92, np.power((c + 0.055) / 1.055, 2.4))
    m = np.array(
        [
            [0.4124564, 0.3575761, 0.1804375],
            [0.2126729, 0.7151522, 0.0721750],
            [0.0193339, 0.1191920, 0.9503041],
        ],
        dtype=np.float64,
    )
    xyz = (lin @ m.T) / np.array([0.95047, 1.00000, 1.08883], dtype=np.float64)
    eps = 216.0 / 24389.0
    kap = 24389.0 / 27.0
    f = np.where(xyz > eps, np.cbrt(np.maximum(xyz, 0.0)), (kap * xyz + 16.0) / 116.0)
    L = 116.0 * f[..., 1] - 16.0
    a = 500.0 * (f[..., 0] - f[..., 1])
    b = 200.0 * (f[..., 1] - f[..., 2])
    return np.stack([L, a, b], axis=-1)


def delta_e_76(srgb_a: Sequence[float], srgb_b: Sequence[float]) -> float:
    """Compute perceptual CIE76 Delta-E between two sRGB [0,1] triples."""
    lab_a = _srgb_to_lab(np.asarray(srgb_a, dtype=np.float64))
    lab_b = _srgb_to_lab(np.asarray(srgb_b, dtype=np.float64))
    return float(np.linalg.norm(lab_a - lab_b))


def _load_ring_samples(
    lib: Any,
    tex_hash: str,
    rig_key: str,
    region: str,
    uvs: np.ndarray,
    *,
    sample_size: int = DEFAULT_SAMPLE_SIZE,
    model_id: int = 0,
) -> np.ndarray:
    """Decode a skin texture at `sample_size` and return cached (N, 3) float32 sRGB [0,1] ring samples."""
    ckey = (tex_hash, rig_key, region, int(sample_size))
    with _SAMPLE_CACHE_LOCK:
        hit = _SAMPLE_CACHE.get(ckey)
        if hit is not None:
            return hit
    png_bytes, _ = lib.texture(tex_hash, int(sample_size), model_id=int(model_id or 0))
    im = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    arr = np.asarray(im, dtype=np.float32) / 255.0
    samples = _sample_bilinear_srgb(arr, uvs)
    with _SAMPLE_CACHE_LOCK:
        if len(_SAMPLE_CACHE) >= _SAMPLE_CACHE_MAX:
            _SAMPLE_CACHE.clear()
        _SAMPLE_CACHE[ckey] = samples
    return samples


def _filter_valid_skin_pairs(
    ref_samples: np.ndarray,
    src_samples: np.ndarray,
    min_luma: float = 0.08,
    max_chroma: float = 0.55,
) -> np.ndarray:
    """Return boolean mask filtering out dark ink/tattoo/shadow/saturated non-skin outliers."""
    luma_w = np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)
    ref_l = ref_samples @ luma_w
    src_l = src_samples @ luma_w
    ref_c = ref_samples.max(axis=1) - ref_samples.min(axis=1)
    src_c = src_samples.max(axis=1) - src_samples.min(axis=1)
    mask = (ref_l >= min_luma) & (src_l >= min_luma) & (ref_c <= max_chroma) & (src_c <= max_chroma)
    # Also exclude magenta placeholder pixels (R>0.78, G<0.24, B>0.78)
    not_mag_ref = ~((ref_samples[:, 0] > 0.78) & (ref_samples[:, 1] < 0.24) & (ref_samples[:, 2] > 0.78))
    not_mag_src = ~((src_samples[:, 0] > 0.78) & (src_samples[:, 1] < 0.24) & (src_samples[:, 2] > 0.78))
    mask = mask & not_mag_ref & not_mag_src
    if int(mask.sum()) < 6:
        return np.ones(len(ref_samples), dtype=bool)
    return mask


def _clamp_gain_with_chroma_safety(
    raw_gain: np.ndarray,
    clamp_min: float,
    clamp_max: float,
    max_chroma_spread: float,
) -> tuple[np.ndarray, bool]:
    """Clamp per-channel RGB gain to [clamp_min, clamp_max] and bound per-channel chromatic spread."""
    g = np.asarray(raw_gain, dtype=np.float64)
    g_clamped = np.clip(g, clamp_min, clamp_max)
    clamped = bool(np.any(np.abs(g_clamped - g) > 1e-6))

    if max_chroma_spread > 0.0:
        g_geo = float(math.pow(max(g_clamped[0] * g_clamped[1] * g_clamped[2], 1e-9), 1.0 / 3.0))
        lo = max(clamp_min, g_geo * (1.0 - max_chroma_spread))
        hi = min(clamp_max, g_geo * (1.0 + max_chroma_spread))
        g_safe = np.clip(g_clamped, lo, hi)
        if np.any(np.abs(g_safe - g_clamped) > 1e-6):
            clamped = True
        g_clamped = g_safe

    return g_clamped.astype(np.float64), clamped


def _paired_median_delta_e(ref_v: np.ndarray, src_v: np.ndarray) -> float:
    """Compute median per-edge CIE76 Delta-E across all paired seam triangles."""
    lab_ref = _srgb_to_lab(ref_v)
    lab_src = _srgb_to_lab(src_v)
    per_edge = np.linalg.norm(lab_ref - lab_src, axis=-1)
    return float(np.median(per_edge))


def _compute_paired_balance(
    ref_samples: np.ndarray,
    src_samples: np.ndarray,
    rig_key: str,
    *,
    clamp_min: float = DEFAULT_CLAMP_MIN,
    clamp_max: float = DEFAULT_CLAMP_MAX,
    max_chroma_spread: float = DEFAULT_MAX_CHROMA_SPREAD,
    deadband: float = DEFAULT_DEADBAND,
) -> dict[str, Any]:
    """Core paired-seam color balance calculation from (N,3) reference and source sRGB samples."""
    valid = _filter_valid_skin_pairs(ref_samples, src_samples)
    ref_v = ref_samples[valid]
    src_v = src_samples[valid]

    # Paired per-edge sRGB ratios (immune to front/back baked AO gradient when both sides share it)
    ratios = ref_v / np.maximum(src_v, 0.02)
    raw_gain = np.median(ratios, axis=0).astype(np.float64)

    ref_med = np.median(ref_v, axis=0).astype(np.float64)
    src_med = np.median(src_v, axis=0).astype(np.float64)
    de_before = delta_e_76(ref_med, src_med)
    paired_de_before = _paired_median_delta_e(ref_v, src_v)

    gain_safe, clamped = _clamp_gain_with_chroma_safety(
        raw_gain, clamp_min, clamp_max, max_chroma_spread
    )

    cand_med_after = np.clip(src_med * gain_safe, 0.0, 1.0)
    cand_de_after = delta_e_76(ref_med, cand_med_after)
    cand_paired_de_after = _paired_median_delta_e(ref_v, np.clip(src_v * gain_safe[None, :], 0.0, 1.0))

    # Monotonic do-no-harm + sub-threshold deadband guard:
    # Skip identity/noise adjustments that do not meaningfully reduce seam Delta-E.
    is_tiny = deadband > 0.0 and de_before < 1.0 and float(np.max(np.abs(gain_safe - 1.0))) < max(deadband, 0.02)
    worsens_seam = (cand_de_after > de_before + 0.02) and (cand_paired_de_after >= paired_de_before - 0.02)
    if is_tiny or worsens_seam:
        gain_out = np.array([1.0, 1.0, 1.0], dtype=np.float64)
        src_after = src_med
        de_after = de_before
        paired_de_after = paired_de_before
        needs_balance = False
    else:
        gain_out = gain_safe
        src_after = cand_med_after
        de_after = cand_de_after
        paired_de_after = cand_paired_de_after
        needs_balance = bool(float(np.max(np.abs(gain_out - 1.0))) >= 1e-3)

    gain_list = [round(float(x), 4) for x in gain_out]
    gain_lin_list = [round(float(math.pow(max(x, 0.0), 2.2)), 4) for x in gain_out]
    raw_list = [round(float(x), 4) for x in raw_gain]

    return {
        "gain": gain_list,
        "gain_srgb": gain_list,
        "gain_linear": gain_lin_list,
        "raw_gain_srgb": raw_list,
        "head_srgb": [int(round(x * 255.0)) for x in ref_med],
        "body_srgb": [int(round(x * 255.0)) for x in src_med],
        "body_after_srgb": [int(round(x * 255.0)) for x in src_after],
        "head_linear": [round(float(x), 4) for x in _srgb_to_linear_arr(ref_med)],
        "body_linear": [round(float(x), 4) for x in _srgb_to_linear_arr(src_med)],
        "deltaE_before": round(de_before, 2),
        "deltaE_after": round(de_after, 2),
        "paired_deltaE_before": round(paired_de_before, 2),
        "paired_deltaE_after": round(paired_de_after, 2),
        "clamped": clamped,
        "needs_balance": needs_balance,
        "severe_mismatch": bool(de_before >= 8.0),
        "rig": rig_key,
        "valid_pairs": int(valid.sum()),
    }


def _identity_result(rig_key: str, reason: str = "") -> dict[str, Any]:
    out = {
        "gain": [1.0, 1.0, 1.0],
        "gain_srgb": [1.0, 1.0, 1.0],
        "gain_linear": [1.0, 1.0, 1.0],
        "raw_gain_srgb": [1.0, 1.0, 1.0],
        "head_srgb": [0, 0, 0],
        "body_srgb": [0, 0, 0],
        "body_after_srgb": [0, 0, 0],
        "head_linear": [0.0, 0.0, 0.0],
        "body_linear": [0.0, 0.0, 0.0],
        "deltaE_before": 0.0,
        "deltaE_after": 0.0,
        "paired_deltaE_before": 0.0,
        "paired_deltaE_after": 0.0,
        "clamped": False,
        "needs_balance": False,
        "severe_mismatch": False,
        "rig": rig_key,
        "valid_pairs": 0,
    }
    if reason:
        out["reason"] = reason
    return out


def compute_skin_color_balance(
    lib: Any,
    face_albedo_hash: str,
    upper_albedo_hash: str,
    body_rig: str,
    face_model_id: int = 0,
    *,
    clamp_min: float = DEFAULT_CLAMP_MIN,
    clamp_max: float = DEFAULT_CLAMP_MAX,
    max_chroma_spread: float = DEFAULT_MAX_CHROMA_SPREAD,
    deadband: float = DEFAULT_DEADBAND,
    sample_size: int = DEFAULT_SAMPLE_SIZE,
) -> dict[str, Any]:
    """Compute safe, clamped sRGB & linear RGB gain multipliers to harmonize body collar to head neck.

    Parameters
    ----------
    lib : ModelLibrary
        Active ModelLibrary instance providing `.texture(hash, max_size, model_id=...)`.
    face_albedo_hash : str
        16-hex-char texture hash of the head's sRGB albedo (`c8e8c282`).
    upper_albedo_hash : str
        16-hex-char texture hash of the upper body (`hand_*`) sRGB albedo.
    body_rig : str
        Body rig string (e.g. 'fr1', 'mr1', 'fo1', 'mo1', 'fb1', 'mb1').
    face_model_id : int, optional
        Model ID for embedded TXD lookup fallback when loading `face_albedo_hash`.
    clamp_min, clamp_max : float
        Hard safety bounds for per-channel sRGB gain (default [0.80, 1.25]).
    max_chroma_spread : float
        Max fractional deviation of any channel from geometric mean luminance gain (default 0.14).
    deadband : float
        Return exact [1.0, 1.0, 1.0] when deltaE_before < 0.6 and max(|g - 1|) < deadband.
    sample_size : int
        Max mip dimension to decode for ring sampling (default 128; ~3 ms cold, <0.05 ms cached).

    Returns
    -------
    dict
        Contains `gain` (sRGB `[r, g, b]`), `gain_linear`, `deltaE_before`, `deltaE_after`,
        `head_srgb`, `body_srgb`, `body_after_srgb`, `clamped`, `needs_balance`, `severe_mismatch`.
    """
    rig_key = normalize_rig(body_rig)
    if not face_albedo_hash or not upper_albedo_hash:
        return _identity_result(rig_key, "missing_texture_hash")
    if face_albedo_hash == upper_albedo_hash:
        return _identity_result(rig_key, "identical_texture")

    try:
        head_uvs, body_uvs = neck_seam_arrays(lib, rig_key)
        head_samples = _load_ring_samples(
            lib, face_albedo_hash, rig_key, "head_neck", head_uvs,
            sample_size=sample_size, model_id=face_model_id,
        )
        body_samples = _load_ring_samples(
            lib, upper_albedo_hash, rig_key, "body_collar", body_uvs,
            sample_size=sample_size, model_id=0,
        )
    except Exception as exc:
        return _identity_result(rig_key, f"texture_load_failed:{exc}")

    return _compute_paired_balance(
        head_samples,
        body_samples,
        rig_key,
        clamp_min=clamp_min,
        clamp_max=clamp_max,
        max_chroma_spread=max_chroma_spread,
        deadband=deadband,
    )


def compute_waist_color_balance(
    lib: Any,
    upper_albedo_hash: str,
    lower_albedo_hash: str,
    body_rig: str,
    upper_gain: Sequence[float] | None = None,
    *,
    clamp_min: float = DEFAULT_CLAMP_MIN,
    clamp_max: float = DEFAULT_CLAMP_MAX,
    max_chroma_spread: float = DEFAULT_MAX_CHROMA_SPREAD,
    deadband: float = DEFAULT_DEADBAND,
    sample_size: int = DEFAULT_SAMPLE_SIZE,
) -> dict[str, Any]:
    """Compute safe sRGB & linear RGB gain for lower body (`feet_*`) to match (harmonized) `hand_*` at the waist.

    If `upper_gain` is supplied (e.g. from `compute_skin_color_balance`), the upper waist samples
    are multiplied by `upper_gain` first so the lower body seamlessly matches the harmonized torso.
    """
    rig_key = normalize_rig(body_rig)
    if not upper_albedo_hash or not lower_albedo_hash:
        return _identity_result(rig_key, "missing_texture_hash")

    upper_uvs, lower_uvs = _WAIST_UV_ARRAYS[rig_key]
    try:
        upper_samples = _load_ring_samples(
            lib, upper_albedo_hash, rig_key, "upper_waist", upper_uvs,
            sample_size=sample_size,
        )
        lower_samples = _load_ring_samples(
            lib, lower_albedo_hash, rig_key, "lower_waist", lower_uvs,
            sample_size=sample_size,
        )
    except Exception as exc:
        return _identity_result(rig_key, f"texture_load_failed:{exc}")

    if upper_gain is not None:
        ug = np.asarray(upper_gain, dtype=np.float32).reshape(1, 3)
        upper_samples = np.clip(upper_samples * ug, 0.0, 1.0)

    return _compute_paired_balance(
        upper_samples,
        lower_samples,
        rig_key,
        clamp_min=clamp_min,
        clamp_max=clamp_max,
        max_chroma_spread=max_chroma_spread,
        deadband=deadband,
    )


def recommend_clean_upper_skin(
    lib: Any,
    face_albedo_hash: str,
    body_rig: str,
    candidate_hashes: Iterable[str],
    face_model_id: int = 0,
    *,
    sample_size: int = DEFAULT_SAMPLE_SIZE,
) -> dict[str, Any] | None:
    """Pick the candidate upper_skin_albedo hash with minimum neck-collar Delta-E to `face_albedo_hash`.

    Useful when a ped archetype has `severe_mismatch=True` (wrong skin-tone bin assigned in 0DRP).
    """
    rig_key = normalize_rig(body_rig)
    best: dict[str, Any] | None = None
    for cand in candidate_hashes:
        if not cand:
            continue
        res = compute_skin_color_balance(
            lib, face_albedo_hash, cand, rig_key, face_model_id=face_model_id,
            sample_size=sample_size,
        )
        if res.get("valid_pairs", 0) == 0:
            continue
        if best is None or res["deltaE_before"] < best["deltaE_before"]:
            best = dict(res, upper_albedo_hash=cand)
    return best


def apply_skin_balance_to_png(
    png_bytes: bytes,
    gain_rgb: Sequence[float] | None,
    *,
    is_linear: bool = False,
) -> bytes:
    """Bake an RGB skin balance multiplier into a PNG texture (for glb_textured.py exports).

    Parameters
    ----------
    png_bytes : bytes
        Input encoded PNG bytes (RGB or RGBA, sRGB).
    gain_rgb : sequence of 3 floats or None
        Per-channel RGB gain multiplier. By default interpreted in sRGB space (matching
        `model-view.js` `base = t.rgb * color`). Pass `is_linear=True` if supplying linear gain.
    is_linear : bool
        If True, converts `gain_rgb` via `g ** (1 / 2.2)` before multiplying sRGB texels.

    Returns
    -------
    bytes
        Balanced PNG bytes (unchanged passthrough if gain is identity within 1e-3).
    """
    if not png_bytes or not gain_rgb or len(gain_rgb) < 3:
        return png_bytes
    g = np.array([float(gain_rgb[0]), float(gain_rgb[1]), float(gain_rgb[2])], dtype=np.float32)
    if is_linear:
        g = np.power(np.maximum(g, 1e-4), 1.0 / 2.2).astype(np.float32)
    if float(np.max(np.abs(g - 1.0))) < 1e-3:
        return png_bytes

    im = Image.open(io.BytesIO(png_bytes))
    has_alpha = im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info)
    if has_alpha:
        im = im.convert("RGBA")
        arr = np.asarray(im, dtype=np.float32)
        arr[..., :3] = np.clip(arr[..., :3] * g[None, None, :], 0.0, 255.0)
        out_im = Image.fromarray((arr + 0.5).astype(np.uint8), mode="RGBA")
    else:
        im = im.convert("RGB")
        arr = np.asarray(im, dtype=np.float32)
        arr = np.clip(arr * g[None, None, :], 0.0, 255.0)
        out_im = Image.fromarray((arr + 0.5).astype(np.uint8), mode="RGB")

    buf = io.BytesIO()
    out_im.save(buf, format="PNG")
    return buf.getvalue()


def derive_seam_masks_from_meshes(
    lib: Any,
    head_model_id: int,
    hand_model_id: int,
    *,
    inset: float = 0.30,
    midpoint_tol: float = 0.025,
) -> list[tuple[float, tuple[float, float], tuple[float, float]]]:
    """Re-derive 3D-paired neck seam UV sample points directly from cached head and hand GLBs."""
    def _load_prim(mid: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        blob = lib.glb(mid, lod=0)
        magic, _ver, _length, jlen, _jtype = struct.unpack_from("<4sIIII", blob, 0)
        if magic != b"glTF":
            raise ValueError(f"Invalid GLB for model {mid}")
        doc = json.loads(blob[20:20 + jlen].decode("utf-8"))
        bstart = 20 + jlen + 8
        blen = struct.unpack_from("<I", blob, 20 + jlen)[0]
        mv = memoryview(blob)[bstart:bstart + blen]
        dtype_map = {5120: "<i1", 5121: "<u1", 5122: "<i2", 5123: "<u2", 5125: "<u4", 5126: "<f4"}
        ncomp_map = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4}

        def _acc(idx: int) -> np.ndarray:
            a = doc["accessors"][idx]
            bv = doc["bufferViews"][a["bufferView"]]
            off = bv.get("byteOffset", 0) + a.get("byteOffset", 0)
            nc = ncomp_map[a["type"]]
            arr = np.frombuffer(mv, dtype=dtype_map[a["componentType"]], count=a["count"] * nc, offset=off)
            return arr.reshape(a["count"], nc) if nc > 1 else arr.copy()

        prim = doc["meshes"][0]["primitives"][0]
        attrs = prim["attributes"]
        return (
            _acc(attrs["POSITION"]).astype(np.float32),
            _acc(attrs["TEXCOORD_0"]).astype(np.float32),
            _acc(prim["indices"]).astype(np.int32).reshape(-1, 3),
        )

    hpos, huv, htris = _load_prim(head_model_id)
    bpos, buv, btris = _load_prim(hand_model_id)
    qa = np.round(hpos * 1000.0).astype(np.int64)
    qb = np.round(bpos * 1000.0).astype(np.int64)
    sb = set(map(tuple, qb))
    sa = set(map(tuple, qa))
    va = {i for i, q in enumerate(qa) if tuple(q) in sb}
    vb = {i for i, q in enumerate(qb) if tuple(q) in sa}

    def _edge_samples(pos: np.ndarray, uv: np.ndarray, tris: np.ndarray, sset: set[int]):
        out = []
        w = (1.0 - inset) * 0.5
        for t in tris:
            sv = [int(v) for v in t if int(v) in sset]
            if len(sv) != 2:
                continue
            iv = [int(v) for v in t if int(v) not in sset][0]
            if max(abs(uv[sv[0], 0] - uv[sv[1], 0]), abs(uv[sv[0], 0] - uv[iv, 0])) > 0.25:
                continue
            if max(abs(uv[sv[0], 1] - uv[sv[1], 1]), abs(uv[sv[0], 1] - uv[iv, 1])) > 0.25:
                continue
            uv_in = w * uv[sv[0]] + w * uv[sv[1]] + inset * uv[iv]
            mid = 0.5 * (pos[sv[0]] + pos[sv[1]])
            out.append((mid, uv_in))
        return out

    ea = _edge_samples(hpos, huv, htris, va)
    eb = _edge_samples(bpos, buv, btris, vb)
    pairs = []
    for ma, uva in ea:
        best = min(eb, key=lambda x: float(np.linalg.norm(ma - x[0])))
        if float(np.linalg.norm(ma - best[0])) < midpoint_tol:
            ang = round(float(math.degrees(math.atan2(float(ma[0]), float(ma[2])))), 1)
            pairs.append((
                ang,
                (round(float(uva[0]), 5), round(float(uva[1]), 5)),
                (round(float(best[1][0]), 5), round(float(best[1][1]), 5)),
            ))
    pairs.sort(key=lambda x: x[0])
    return pairs
