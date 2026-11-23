# SPDX-License-Identifier: GPL-3.0-or-later
"""Build a self-contained, Blender-ready glTF 2.0 binary (.glb) with embedded PNG textures, PBR materials,
multi-part assemblies, instanced vehicle wheels, and baked procedural/tint shader modes."""
import io
import json
import struct
import numpy as np
from PIL import Image

BASE_COLOR = "c8e8c282"
OVERLAY = "0df47048"
DIRT = "7485dafb"
BUMP = "1e77bf4c"
SPECULAR = "c6f46e79"
HEAD_SHADER = "379efcef"
EYE_SHADER = "89a1cf72"
WATER_STENCIL_SHADER = "fc34f08b"
PROCEDURAL_MUV_SHADERS = {
    "d5ed50f4", "39ddb9ca", "7574b09b", "ea8d09d0",
    "73055d06", "c78a3e26", "9880a0c9", "1c43a65b",
    "6f3c7994", "1df84fef",
}
VEHICLE_GLASS_SHADERS = {"fa9cc069", "bd71bdbf", "389206d1", "31212453"}
VEHICLE_PAINT_SHADERS = {"4aa38282", "476ac73a", "03b27476", "469ebe36", "4d2b05e7", "921b19e5", "4dc0ac6a", "e0166a30", "f25d4644", "4f1964cc", "f3ad0561", "0b11a70e", "4ea57893", "87493dca", "e810a19b"}
VEHICLE_WHEEL_SHADERS = {"c5cdbdd2", "570fcace"}
VEHICLE_TYRE_SHADERS = {"99ad6fc9", "b994d670", "42ac0460"}
DIFFUSE_ROLES = {"Base color", "Layer 0 diffuse", "Interior diffuse", "Alt base color",
                 "Font diffuse", "Uber layer 0 diffuse"}
NORMAL_ROLES = {"Bump / normal", "Bump / normal (unaliased)", "Layer 0 normal", "Layer 1 normal",
                "Alt bump / normal", "Font normal", "Uber layer 0 bump"}
CAR_PAINTS = [
    [0.16, 0.27, 0.45], [0.58, 0.13, 0.14], [0.85, 0.86, 0.88], [0.18, 0.20, 0.23],
    [0.14, 0.34, 0.24], [0.68, 0.42, 0.13], [0.46, 0.49, 0.53], [0.26, 0.22, 0.38],
]


def _vehicle_paint_color(name):
    s = (name or "").lower()
    h = 2166136261
    for ch in s:
        h = ((h ^ ord(ch)) * 16777619) & 0xFFFFFFFF
    return list(CAR_PAINTS[h % len(CAR_PAINTS)])


def _animal_mask_palette(name):
    n = (name or "").lower()
    if "antler" in n or "horn" in n:
        return [[0.52, 0.44, 0.33], [0.34, 0.26, 0.18], [0.85, 0.81, 0.73]]
    if "deer_agh" in n:
        return [[0.34, 0.35, 0.24], [0.44, 0.38, 0.24], [0.74, 0.75, 0.66]]
    if "deer" in n:
        return [[0.48, 0.33, 0.20], [0.64, 0.38, 0.19], [0.92, 0.90, 0.86]]
    if "cattle_holstein" in n:
        return [[0.14, 0.13, 0.13], [0.95, 0.94, 0.91], [0.16, 0.15, 0.15]]
    if "horse" in n:
        return [[0.44, 0.26, 0.15], [0.18, 0.14, 0.12], [0.90, 0.88, 0.84]]
    if "cat_" in n:
        return [[0.48, 0.43, 0.38], [0.22, 0.20, 0.18], [0.90, 0.88, 0.85]]
    if "goat" in n:
        return [[0.46, 0.35, 0.25], [0.20, 0.17, 0.15], [0.88, 0.85, 0.80]]
    if "sheep" in n:
        return [[0.78, 0.74, 0.67], [0.56, 0.48, 0.40], [0.88, 0.85, 0.79]]
    if "mouse" in n:
        return [[0.45, 0.41, 0.37], [0.33, 0.29, 0.26], [0.80, 0.72, 0.70]]
    if n.startswith("crf_"):
        return [[0.36, 0.43, 0.41], [0.58, 0.49, 0.35], [0.86, 0.89, 0.91]]
    return [[0.46, 0.36, 0.26], [0.60, 0.44, 0.30], [0.90, 0.88, 0.84]]


def _tex_hash(t):
    return (t.get("effectiveHash") or t.get("textureHash")) if t else None


def _terrain_layers(material):
    sh = material.get("shaderHash", "")
    if (sh in VEHICLE_GLASS_SHADERS or sh in VEHICLE_PAINT_SHADERS
            or sh in VEHICLE_WHEEL_SHADERS or sh in VEHICLE_TYRE_SHADERS):
        return None
    texs = material.get("textures", ())
    usable = lambda t: bool(t.get("sources")) and t["sources"][0].get("dimension") == "2D" and t["sources"][0].get("height", 0) > 1
    usable_mask = lambda t: usable(t) and t["sources"][0].get("width", 0) >= 16

    uber_params = ("ce7b968e", "d883aa9e", "32b9df09", "3d10f3b7")
    uber_roles = ("Uber layer 0 diffuse", "Uber layer 1 diffuse", "Uber layer 2 diffuse", "Uber layer 3 diffuse")
    uber_layers = [
        next((t for t in texs if usable(t) and (t["parameter"] == p or t.get("role") == r)), None)
        for p, r in zip(uber_params, uber_roles)
    ]
    first_u = next((x for x in uber_layers if x), None)
    if first_u and sum(1 for x in uber_layers if x) >= 2:
        if not uber_layers[0]:
            uber_layers[0] = first_u
        mask = next((t for t in texs if usable_mask(t) and (t["parameter"] == "c075f9b6" or t.get("role") == "Uber terrain lookup")), None)
        tint = next((t for t in texs if usable_mask(t) and "SRGB" in t["sources"][0].get("format", "")
                     and (t["parameter"] == "3fb7964f" or t.get("role") == "Uber terrain tint")), None)
        return {"slot": uber_layers[0], "texMode": 5, "layers": uber_layers, "maskSlot": mask, "tintSlot": tint}

    std_params = ("346394ea", "7d4f12e2", "7d072b80", "")
    std_roles = ("Layer 0 diffuse", "Layer 1 diffuse", "Layer 2 diffuse", "Layer 3 diffuse")
    std_layers = [
        next((t for t in texs if usable(t) and (t.get("role") == r or (p and t["parameter"] == p))), None)
        for p, r in zip(std_params, std_roles)
    ]
    first_s = next((x for x in std_layers if x), None)
    if first_s and sum(1 for x in std_layers if x) >= 2:
        if not std_layers[0]:
            std_layers[0] = first_s
        mask = next((t for t in texs if usable_mask(t) and (t["parameter"] == "38ebcdb3" or t.get("role") == "Layer blend control")), None)
        return {"slot": std_layers[0], "texMode": 5, "layers": std_layers, "maskSlot": mask, "tintSlot": None}
    return None


def _default_slot(material, model_name="", category=""):
    sh = material.get("shaderHash", "")
    if sh in VEHICLE_GLASS_SHADERS:
        return None
    terr = _terrain_layers(material)
    if terr:
        return terr
    usable = lambda t: (bool(t.get("sources")) and t["sources"][0].get("dimension") == "2D"
                        and t["sources"][0].get("height", 0) > 1 and not t["sources"][0].get("isTintPalette"))
    texs = material.get("textures", ())
    is_veh_2lyr = sh in VEHICLE_PAINT_SHADERS or sh in VEHICLE_WHEEL_SHADERS or sh in VEHICLE_TYRE_SHADERS
    base = next((t for t in texs if t["parameter"] == BASE_COLOR and usable(t)), None)
    if base:
        fmt = base["sources"][0].get("format", "")
        if sh == EYE_SHADER:
            return {"slot": base, "texMode": 4}
        if sh == HEAD_SHADER and "SRGB" not in fmt:
            return {"slot": base, "texMode": 3}
        if category == "Animals" and "SRGB" not in fmt:
            return {"slot": base, "texMode": 2, "palette": _animal_mask_palette(model_name)}
        if is_veh_2lyr and "SRGB" not in fmt:
            return {"slot": base, "texMode": 1}
        return {"slot": base, "texMode": 0}
    named = next((t for t in texs if usable(t) and t.get("role") in DIFFUSE_ROLES), None)
    if named:
        if is_veh_2lyr and "SRGB" not in named["sources"][0].get("format", ""):
            return {"slot": named, "texMode": 1}
        return {"slot": named, "texMode": 0}
    is_veh_paint = sh in VEHICLE_PAINT_SHADERS
    color = [t for t in texs if usable(t) and t["parameter"] not in (OVERLAY, DIRT, "2c8d7182")
             and "SRGB" in t["sources"][0].get("format", "")
             and (not is_veh_paint or (t["sources"][0]["width"] > 4 and t["sources"][0]["height"] > 4))]
    if color:
        color.sort(key=lambda t: t["sources"][0]["width"] * t["sources"][0]["height"], reverse=True)
        return {"slot": color[0], "texMode": 0}
    if not is_veh_paint:
        ov = next((t for t in texs if t["parameter"] == OVERLAY and usable(t)
                   and "SRGB" in t["sources"][0].get("format", "")
                   and t["sources"][0]["width"] > 4 and t["sources"][0]["height"] > 4), None)
        if ov:
            return {"slot": ov, "texMode": 0}
    spec = next((t for t in texs if t["parameter"] == SPECULAR and usable(t) and t["sources"][0]["width"] >= 32), None)
    if (spec and sh != HEAD_SHADER and not is_veh_paint
            and (sh in PROCEDURAL_MUV_SHADERS or not any(t["parameter"] == BASE_COLOR and usable(t) for t in texs))):
        return {"slot": spec, "texMode": 1}
    return None


def _default_normal_slot(material):
    if material.get("shaderHash") == EYE_SHADER:
        return None
    usable = lambda t: bool(t.get("sources")) and t["sources"][0].get("dimension") == "2D" and t["sources"][0]["width"] >= 8
    texs = material.get("textures", ())
    bump = next((t for t in texs if t["parameter"] == BUMP and usable(t)), None)
    if bump:
        return bump
    named = next((t for t in texs if usable(t) and t.get("role") in NORMAL_ROLES), None)
    if named:
        return named
    return next((t for t in texs if usable(t) and "BC5" in t["sources"][0].get("format", "")), None)


def _srgb_to_linear(c):
    return [float((max(0.0, min(1.0, x)) ** 2.2)) for x in c]


def _parse_glb(blob):
    magic, ver, length, jlen, jtype = struct.unpack_from("<4sIIII", blob, 0)
    if magic != b"glTF":
        raise ValueError("Invalid cached GLB")
    doc = json.loads(blob[20:20 + jlen].decode("utf-8"))
    bstart = 20 + jlen + 8
    blen = struct.unpack_from("<I", blob, 20 + jlen)[0]
    return doc, memoryview(blob)[bstart:bstart + blen]


def _bake_base_png(raw_png, tex_mode, tint, palette):
    if tex_mode == 0:
        return raw_png
    im = Image.open(io.BytesIO(raw_png)).convert("RGBA")
    arr = np.asarray(im, dtype=np.float32) / 255.0
    r, g, b, a = arr[..., 0], arr[..., 1], arr[..., 2], arr[..., 3]
    if tex_mode == 1:
        col = np.asarray(tint or [0.68, 0.65, 0.60], dtype=np.float32)
        rgb = np.clip(col[None, None, :] * (0.38 + 0.72 * g[..., None]), 0.0, 1.0)
    elif tex_mode == 2:
        pal = palette or [[0.46, 0.34, 0.23], [0.62, 0.42, 0.25], [0.90, 0.88, 0.84]]
        p0, p1, p2 = [np.asarray(p, dtype=np.float32) for p in pal]
        c = p0[None, None, :] * (1.0 - r[..., None]) + p1[None, None, :] * r[..., None]
        c = c * (1.0 - b[..., None]) + p2[None, None, :] * b[..., None]
        rgb = np.clip(c * np.clip(g[..., None] * 2.15, 0.12, 1.55), 0.0, 1.0)
    elif tex_mode == 3:
        col = np.asarray(tint or [0.76, 0.57, 0.47], dtype=np.float32)
        pore = np.clip(g * 0.65 + r * 0.35, 0.0, 1.0)[..., None]
        warmth = (r - g)[..., None] * np.array([0.055, -0.015, -0.025], dtype=np.float32)
        rgb = np.clip(col[None, None, :] * (0.76 + 0.28 * pore) + warmth, 0.0, 1.0)
    elif tex_mode == 4:
        sclera = np.array([0.92, 0.90, 0.89], dtype=np.float32)
        rgb = np.clip(sclera[None, None, :] * (1.0 - a[..., None]) + arr[..., :3] * a[..., None], 0.0, 1.0)
    else:
        return raw_png
    buf = io.BytesIO()
    if tex_mode != 4 and float(a.min()) < 0.99:
        rgba = np.concatenate((rgb, a[..., None]), axis=-1)
        Image.fromarray((rgba * 255.0 + 0.5).astype(np.uint8), mode="RGBA").save(buf, format="PNG")
    else:
        Image.fromarray((rgb * 255.0 + 0.5).astype(np.uint8), mode="RGB").save(buf, format="PNG")
    return buf.getvalue()


def _analyze_rgba(png_bytes):
    """Inspect decoded PNG bytes for RAGE magenta placeholders and binary-alpha cutouts."""
    try:
        im = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
        arr = np.asarray(im, dtype=np.uint8).reshape(-1, 4)
        n = len(arr)
        if n == 0:
            return False, False
        step = max(1, n // 4096)
        sub = arr[::step]
        mag_hits = int(((sub[:, 0] > 200) & (sub[:, 1] < 60) & (sub[:, 2] > 200)).sum())
        is_magenta = (mag_hits * 4) >= len(sub)

        a = arr[::4, 3]
        total = len(a)
        if total == 0:
            return is_magenta, False
        clear = int((a < 16).sum())
        solid = int((a > 239).sum())
        clear_frac = clear / total
        binary_frac = (clear + solid) / total
        is_cutout = clear_frac > 0.02 and binary_frac > 0.85
        return is_magenta, is_cutout
    except Exception:
        return False, False


def _bake_terrain_png(lib, model_id, terr_spec, max_tex):
    """Composite 2..4 terrain diffuse layers using macro lookup/control mask (UV0) or detail blend (UV1)."""
    layer_hashes = [_tex_hash(s) for s in terr_spec["layers"]]
    mask_hash = _tex_hash(terr_spec.get("maskSlot"))
    tint_hash = _tex_hash(terr_spec.get("tintSlot"))
    size = min(max_tex, 1024)

    def _load_rgb(h, target_wh=None, tile=1):
        if not h:
            return None
        raw, _ = lib.texture(h, size if tile == 1 else max(128, size // 2), model_id=model_id)
        im = Image.open(io.BytesIO(raw)).convert("RGB")
        if tile > 1 and target_wh:
            tw = max(16, target_wh[0] // tile)
            th = max(16, target_wh[1] // tile)
            small = np.asarray(im.resize((tw, th), Image.Resampling.BILINEAR), dtype=np.float32) / 255.0
            tiled = np.tile(small, (tile, tile, 1))
            return tiled[:target_wh[1], :target_wh[0], :]
        if target_wh and im.size != target_wh:
            im = im.resize(target_wh, Image.Resampling.BILINEAR)
        return np.asarray(im, dtype=np.float32) / 255.0

    if mask_hash:
        mask_raw, _ = lib.texture(mask_hash, size, model_id=model_id)
        mask_im = Image.open(io.BytesIO(mask_raw)).convert("RGBA")
        wh = mask_im.size
        m_arr = np.asarray(mask_im, dtype=np.float32) / 255.0
        l0 = _load_rgb(layer_hashes[0], wh, tile=8)
        l1 = _load_rgb(layer_hashes[1], wh, tile=8)
        l2 = _load_rgb(layer_hashes[2], wh, tile=8)
        l3 = _load_rgb(layer_hashes[3], wh, tile=8)
        # If R is constant/unused while G & B encode 2x2 bilinear corner weights, use bilinear blending.
        r_std = float(m_arr[..., 0].std())
        g_std = float(m_arr[..., 1].std())
        b_std = float(m_arr[..., 2].std())
        if r_std < 0.015 and g_std > 0.03 and b_std > 0.03 and l1 is not None:
            wb = m_arr[..., 2:3]
            wg = m_arr[..., 1:2]
            near = l0 * (1.0 - wb) + l1 * wb
            far_a = l2 if l2 is not None else l0
            far_b = l3 if l3 is not None else (l2 if l2 is not None else l1)
            far = far_a * (1.0 - wb) + far_b * wb
            out = near * (1.0 - wg) + far * wg
        else:
            out = l0.copy()
            for lk, ch in ((l1, 0), (l2, 1), (l3, 2)):
                if lk is not None:
                    w = m_arr[..., ch:ch + 1]
                    out = out * (1.0 - w) + lk * w
        if tint_hash:
            try:
                t_arr = _load_rgb(tint_hash, wh, tile=1)
                if t_arr is not None:
                    out = np.clip(out * (0.55 + 0.75 * t_arr), 0.0, 1.0)
            except Exception:
                pass
        buf = io.BytesIO()
        Image.fromarray((np.clip(out, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8), mode="RGB").save(buf, format="PNG")
        return buf.getvalue(), False  # Uses macro UV0

    l0_raw, _ = lib.texture(layer_hashes[0], size, model_id=model_id)
    l0_im = Image.open(io.BytesIO(l0_raw)).convert("RGB")
    wh = l0_im.size
    out = np.asarray(l0_im, dtype=np.float32) / 255.0
    weights = (0.30, 0.18, 0.12)
    for k, w in zip((1, 2, 3), weights):
        if layer_hashes[k]:
            try:
                lk = _load_rgb(layer_hashes[k], wh, tile=1)
                if lk is not None:
                    out = out * (1.0 - w) + lk * w
            except Exception:
                pass
    buf = io.BytesIO()
    Image.fromarray((np.clip(out, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8), mode="RGB").save(buf, format="PNG")
    return buf.getvalue(), True  # Uses tiled detail UV1 when present


def _bake_normal_png(raw_png, fmt_str):
    """Ensure tangent-space normal maps have a valid unit Z channel (handles BC5 and XY-packed BC1/BC7)."""
    im = Image.open(io.BytesIO(raw_png)).convert("RGB")
    arr = np.asarray(im, dtype=np.float32) / 255.0
    is_bc5 = "BC5" in (fmt_str or "")
    b_ch = arr[..., 2]
    b_mean = float(b_ch.mean())
    b_std = float(b_ch.std())
    needs_z = is_bc5 or b_mean < 0.55 or (b_std < 0.005 and b_mean < 0.78)
    if not needs_z:
        return raw_png
    nx = arr[..., 0] * 2.0 - 1.0
    ny = arr[..., 1] * 2.0 - 1.0
    nz = np.sqrt(np.maximum(0.0, 1.0 - nx * nx - ny * ny))
    out = np.empty_like(arr, dtype=np.uint8)
    out[..., 0] = (arr[..., 0] * 255.0 + 0.5).astype(np.uint8)
    out[..., 1] = (arr[..., 1] * 255.0 + 0.5).astype(np.uint8)
    out[..., 2] = ((nz * 0.5 + 0.5) * 255.0 + 0.5).astype(np.uint8)
    buf = io.BytesIO()
    Image.fromarray(out, mode="RGB").save(buf, format="PNG")
    return buf.getvalue()


def _load_tint_palette_rgb(lib, model_id, tint_pal_meta):
    """Load a 1D tint palette texture as an (W, 3) float32 linear RGB array for glTF COLOR_0 baking."""
    if not tint_pal_meta or not tint_pal_meta.get("hash"):
        return None
    try:
        raw_png, _ = lib.texture(tint_pal_meta["hash"], 4096, model_id=model_id)
        im = Image.open(io.BytesIO(raw_png)).convert("RGB")
        arr = np.asarray(im, dtype=np.float32)[0] / 255.0  # shape (W, 3), sRGB
        return np.power(np.clip(arr, 0.0, 1.0), 2.2).astype(np.float32)
    except Exception:
        return None


def _bake_color0_bytes(sdoc, sbin, col_aidx, prim_extras, pal_rgb, row_len, row):
    """Return ('copy', None), ('omit', None), or ('replace', (bytes, count)) for glTF COLOR_0 attribute."""
    ext = prim_extras or {}
    has_rgb = bool(ext.get("hasColorRGB"))
    has_alpha = bool(ext.get("hasVertexAlpha"))
    if pal_rgb is not None and len(pal_rgb) > 0:
        acc = sdoc["accessors"][col_aidx]
        bv = sdoc["bufferViews"][acc["bufferView"]]
        off = bv.get("byteOffset", 0) + acc.get("byteOffset", 0)
        cnt = int(acc["count"])
        raw = np.frombuffer(sbin, dtype="<f4", count=cnt * 4, offset=off).reshape(cnt, 4)
        idx = np.clip(np.round(raw[:, 2] * 255.0).astype(np.int32), 0, 255)
        w = len(pal_rgb)
        r = max(0, int(row))
        entry = r * max(1, int(row_len)) + idx
        fallback = np.clip(idx, 0, w - 1)
        entry = np.where((entry >= 0) & (entry < w), entry, fallback)
        out = np.empty((cnt, 4), dtype="<f4")
        out[:, :3] = pal_rgb[entry]
        out[:, 3] = raw[:, 3] if has_alpha else 1.0
        return "replace", (out.tobytes(), cnt)
    if has_rgb:
        return "copy", None
    if has_alpha:
        acc = sdoc["accessors"][col_aidx]
        bv = sdoc["bufferViews"][acc["bufferView"]]
        off = bv.get("byteOffset", 0) + acc.get("byteOffset", 0)
        cnt = int(acc["count"])
        raw = np.frombuffer(sbin, dtype="<f4", count=cnt * 4, offset=off).reshape(cnt, 4)
        out = np.ones((cnt, 4), dtype="<f4")
        out[:, 3] = raw[:, 3]
        return "replace", (out.tobytes(), cnt)
    return "omit", None


def build_textured_glb(lib, main_id, lod=0, part_ids=(), wheel_spec=None, overrides=None, max_tex=1024,
                       include_rig=False, anim_dict_id=None, anim_clip_hash=None, anim_root_motion=False,
                       anatomy_tex=None, muv_presets=None, part_overrides=None,
                       body_morphs=None, height_scale=1.0, is_male=None, use_rage9_rig=False):
    """Assemble a complete glTF 2.0 binary (.glb) with embedded PNG textures, PBR materials, and optional skeleton/animation."""
    overrides = overrides or {}
    anatomy_tex = anatomy_tex or {}
    muv_presets = muv_presets or {}
    part_overrides = part_overrides or {}
    out_bin = bytearray()
    out_doc = {
        "asset": {"version": "2.0", "generator": "Lineup (textured glTF 2.0)"},
        "scene": 0,
        "scenes": [{"nodes": []}],
        "nodes": [],
        "meshes": [],
        "materials": [],
        "textures": [],
        "images": [],
        "samplers": [{"magFilter": 9729, "minFilter": 9987, "wrapS": 10497, "wrapT": 10497}],
        "buffers": [],
        "bufferViews": [],
        "accessors": [],
    }
    used_clearcoat = False
    image_cache = {}
    mesh_cache = {}

    master_skel = None
    combined_skel = None
    if include_rig:
        try:
            master_skel = lib.skeleton_for_model(main_id)
            if master_skel and master_skel.get("joints"):
                combined_skel = dict(master_skel)
                combined_skel["joints"] = list(master_skel["joints"])
        except Exception:
            master_skel = None
            combined_skel = None

    def _add_bytes(blob, target=None):
        out_bin.extend(b"\x00" * ((-len(out_bin)) % 4))
        off = len(out_bin)
        out_bin.extend(blob)
        bv = {"buffer": 0, "byteOffset": off, "byteLength": len(blob)}
        if target is not None:
            bv["target"] = target
        idx = len(out_doc["bufferViews"])
        out_doc["bufferViews"].append(bv)
        return idx

    def _embed_png(cache_key, png_bytes, name):
        if cache_key in image_cache:
            return image_cache[cache_key]
        bv_idx = _add_bytes(png_bytes)
        img_idx = len(out_doc["images"])
        out_doc["images"].append({"bufferView": bv_idx, "mimeType": "image/png", "name": name})
        tex_idx = len(out_doc["textures"])
        out_doc["textures"].append({"sampler": 0, "source": img_idx, "name": name})
        image_cache[cache_key] = tex_idx
        return tex_idx

    def _build_material(m, item_meta, mat_override=None, has_uv1=False):
        nonlocal used_clearcoat
        sh = m.get("shaderHash", "")
        rpass = m.get("renderPass") or ("stencil" if sh == WATER_STENCIL_SHADER else "opaque")
        model_id = item_meta["id"]
        model_name, category = item_meta["name"], item_meta["category"]
        nm = (model_name or "").lower()
        pick = _default_slot(m, model_name, category)
        default_vis = (sh != WATER_STENCIL_SHADER and rpass != "stencil"
                       and not (rpass == "decal" and not pick and not m.get("tintColor")))
        visible = m.get("visible", default_vis)
        if mat_override and "visible" in mat_override:
            visible = bool(mat_override["visible"])
        if not visible:
            return None
        nslot = _default_normal_slot(m)
        is_rim = sh in VEHICLE_WHEEL_SHADERS or nm.startswith("rim_")
        is_tyre = sh in VEHICLE_TYRE_SHADERS or nm.startswith("tyre_")
        tex_mode = pick["texMode"] if pick else 0
        mat_mode = 2 if sh in VEHICLE_GLASS_SHADERS else (
            1 if ((sh in VEHICLE_PAINT_SHADERS and (not pick or tex_mode == 0)) or is_rim) else 0
        )
        active_hash = _tex_hash(pick["slot"]) if pick else None
        norm_hash = _tex_hash(nslot) if nslot else None
        norm_fmt = nslot["sources"][0].get("format", "") if nslot and nslot.get("sources") else "BC5"
        tint = m.get("tintColor")
        if not tint:
            if is_tyre and (not pick or tex_mode == 1):
                tint = [0.15, 0.15, 0.16]
            elif is_rim and (not pick or tex_mode == 1):
                tint = [0.76, 0.78, 0.82]
            elif category == "Vehicles" and sh in VEHICLE_PAINT_SHADERS:
                if not pick:
                    tint = _vehicle_paint_color(model_name)
                elif tex_mode == 1:
                    tint = [0.44, 0.46, 0.50]
        pal = pick.get("palette") if pick else None

        if mat_override:
            if "tex" in mat_override:
                active_hash = mat_override["tex"]
            if "texMode" in mat_override:
                tex_mode = int(mat_override["texMode"])
            if "norm" in mat_override:
                norm_hash = mat_override["norm"]
            if mat_override.get("tint") or mat_override.get("tintColor"):
                tint = mat_override.get("tint") or mat_override.get("tintColor")
            if mat_override.get("pal") or mat_override.get("palette"):
                pal = mat_override.get("pal") or mat_override.get("palette")

        if mat_mode == 2:
            gmat = {
                "name": f"{model_name}_mat{m['index']}_glass",
                "alphaMode": "BLEND",
                "doubleSided": True,
                "pbrMetallicRoughness": {
                    "baseColorFactor": [0.06, 0.10, 0.14, 0.36],
                    "metallicFactor": 0.0,
                    "roughnessFactor": 0.06,
                },
            }
            out_doc["materials"].append(gmat)
            return len(out_doc["materials"]) - 1

        pbr = {
            "metallicFactor": 0.25 if mat_mode == 1 else 0.0,
            "roughnessFactor": 0.16 if mat_mode == 1 else (0.08 if rpass == "water" else (0.12 if tex_mode == 4 else 0.65)),
        }
        gmat = {
            "name": f"{model_name}_mat{m['index']}",
            "doubleSided": True,
            "pbrMetallicRoughness": pbr,
        }
        if rpass == "cutout":
            gmat["alphaMode"] = "MASK"
            gmat["alphaCutoff"] = 0.4
        elif rpass in ("alpha", "decal", "water"):
            gmat["alphaMode"] = "BLEND"

        detail_uv1 = False
        if tex_mode == 5 and pick and pick.get("layers"):
            try:
                tkey = ("terrain", tuple(_tex_hash(s) for s in pick["layers"]),
                        _tex_hash(pick.get("maskSlot")), _tex_hash(pick.get("tintSlot")))
                baked, use_uv1 = _bake_terrain_png(lib, model_id, pick, max_tex)
                tex_idx = _embed_png(tkey, baked, f"{model_name}_m{m['index']}_terrain")
                tref = {"index": tex_idx}
                if use_uv1 and has_uv1:
                    tref["texCoord"] = 1
                    detail_uv1 = True
                pbr["baseColorTexture"] = tref
                if mat_override and mat_override.get("tint"):
                    pbr["baseColorFactor"] = [*_srgb_to_linear(mat_override["tint"]), 1.0]
            except Exception:
                active_hash = _tex_hash(pick["slot"])
                tex_mode = 0

        # RAGE 9 MUV clothing: bake authentic 8-slot fabric/pattern/metal albedo when preset is available
        muv_preset = None
        if not (mat_override and "tex" in mat_override):
            if mat_override and mat_override.get("muvPreset"):
                muv_preset = mat_override["muvPreset"]
            elif m.get("isMuv") or m.get("muvPreset"):
                muv_preset = (
                    muv_presets.get(str(model_id))
                    or muv_presets.get(nm)
                    or m.get("muvPreset")
                )
        if muv_preset and "baseColorTexture" not in pbr:
            try:
                from clothing_presets import get_store as get_clothing_store
                from model_service import CACHE as _MUV_CACHE
                mres = max(256, min(max_tex, 1024))
                mbaked = get_clothing_store().bake_muv_albedo(
                    lib, model_id, muv_preset, max_res=mres, mat_idx=m["index"], cache_dir=_MUV_CACHE,
                )
                mkey = ("muv", model_id, m["index"], muv_preset, mres)
                mtex_idx = _embed_png(mkey, mbaked, f"{model_name}_m{m['index']}_muv_{muv_preset}")
                pbr["baseColorTexture"] = {"index": mtex_idx}
            except Exception:
                pass

        if "baseColorTexture" not in pbr:
            if active_hash:
                try:
                    raw_png, _ = lib.texture(active_hash, max_tex, model_id=model_id)
                    is_mag, is_cut = _analyze_rgba(raw_png)
                    if is_mag and not (mat_override and "tex" in mat_override):
                        if rpass == "decal" and not (mat_override and mat_override.get("visible") is True):
                            return None
                        # Drop magenta placeholder texture on non-decal meshes; fall back to neutral tint.
                        if tint:
                            pbr["baseColorFactor"] = [*_srgb_to_linear(tint), 1.0]
                        else:
                            pbr["baseColorFactor"] = [0.50, 0.51, 0.49, 1.0]
                    else:
                        if is_cut and rpass in ("alpha", "decal") and tex_mode == 0:
                            gmat["alphaMode"] = "MASK"
                            gmat["alphaCutoff"] = 0.5
                        skin_gain = mat_override.get("skinGain") if mat_override else None
                        if skin_gain and len(skin_gain) == 3 and any(abs(float(g) - 1.0) > 0.002 for g in skin_gain):
                            skin_gain_t = tuple(round(float(g), 4) for g in skin_gain)
                        else:
                            skin_gain_t = None
                        cache_key = ("base", active_hash, tex_mode,
                                     tuple(round(x, 4) for x in tint) if tint and tex_mode in (1, 3) else None,
                                     tuple(tuple(round(c, 4) for c in z) for z in pal) if pal and tex_mode == 2 else None,
                                     skin_gain_t)
                        baked = _bake_base_png(raw_png, tex_mode, tint, pal)
                        if skin_gain_t is not None:
                            try:
                                from skin_harmonize import apply_skin_balance_to_png
                                baked = apply_skin_balance_to_png(baked, skin_gain_t)
                            except Exception:
                                pass
                        tex_idx = _embed_png(cache_key, baked, f"{model_name}_m{m['index']}_{active_hash}")
                        pbr["baseColorTexture"] = {"index": tex_idx}
                        if mat_override and mat_override.get("tint") and tex_mode == 0:
                            pbr["baseColorFactor"] = [*_srgb_to_linear(mat_override["tint"]), 1.0]
                except Exception:
                    if tint:
                        pbr["baseColorFactor"] = [*_srgb_to_linear(tint), 1.0]
            elif rpass == "water":
                pbr["baseColorFactor"] = [0.07, 0.20, 0.26, 0.62]
            elif tint:
                pbr["baseColorFactor"] = [*_srgb_to_linear(tint), 1.0]
            else:
                pbr["baseColorFactor"] = [0.50, 0.51, 0.49, 1.0]

        if norm_hash:
            try:
                raw_npng, _ = lib.texture(norm_hash, max_tex, model_id=model_id)
                nkey = ("norm", norm_hash, "BC5" in norm_fmt)
                nbaked = _bake_normal_png(raw_npng, norm_fmt)
                ntex_idx = _embed_png(nkey, nbaked, f"{model_name}_m{m['index']}_n_{norm_hash}")
                nref = {"index": ntex_idx}
                if detail_uv1 and has_uv1:
                    nref["texCoord"] = 1
                gmat["normalTexture"] = nref
            except Exception:
                pass

        if mat_mode == 1:
            used_clearcoat = True
            gmat["extensions"] = {
                "KHR_materials_clearcoat": {
                    "clearcoatFactor": 0.85,
                    "clearcoatRoughnessFactor": 0.06,
                }
            }
        out_doc["materials"].append(gmat)
        return len(out_doc["materials"]) - 1

    def _anatomy_overrides_for(item_meta):
        pnm = (item_meta.get("name") or "").lower()
        pid_s = str(item_meta.get("id", ""))
        entry = {}
        # 1. Per-part garment/outfit overrides (muvPreset, albedo, normal, tintColor, tintRow)
        pov = part_overrides.get(pid_s) or part_overrides.get(pnm)
        if isinstance(pov, dict):
            if pov.get("muvPreset"):
                entry["muvPreset"] = pov["muvPreset"]
            elif pov.get("albedo") or pov.get("tex"):
                entry["tex"] = pov.get("albedo") or pov.get("tex")
                entry["texMode"] = 0
            if pov.get("normal") or pov.get("norm"):
                entry["norm"] = pov.get("normal") or pov.get("norm")
            if pov.get("tintColor") or pov.get("tint"):
                entry["tint"] = pov.get("tintColor") or pov.get("tint")
            if pov.get("tintRow") is not None:
                entry["tintRow"] = int(pov["tintRow"])

        # 2. Anatomy skin / eye / collar-waist seam harmonization overrides
        if anatomy_tex:
            ov_alb = None
            ov_norm = None
            ov_gain = None
            if pnm.startswith("hand_"):
                ov_alb = anatomy_tex.get("upperAlbedo") or None
                ov_norm = anatomy_tex.get("upperNormal") or None
                ov_gain = anatomy_tex.get("upperGain") or None
            elif pnm.startswith("bodynipplesgen_"):
                ov_alb = anatomy_tex.get("upperAlbedo") or None
                ov_gain = anatomy_tex.get("upperGain") or None
            elif pnm.startswith("feet_"):
                ov_alb = anatomy_tex.get("lowerAlbedo") or None
                ov_norm = anatomy_tex.get("lowerNormal") or None
                ov_gain = anatomy_tex.get("lowerGain") or anatomy_tex.get("upperGain") or None
            elif pnm.startswith("bodygenitalsgen_"):
                ov_alb = anatomy_tex.get("genitalAlbedo") or (anatomy_tex.get("lowerAlbedo") if "_fr1_" in pnm else None) or None
                ov_norm = anatomy_tex.get("genitalNormal") or (anatomy_tex.get("lowerNormal") if "_fr1_" in pnm else None) or None
                ov_gain = anatomy_tex.get("lowerGain") or anatomy_tex.get("upperGain") or None
            elif "_eyes" in pnm or pnm.endswith("eyes"):
                ov_alb = anatomy_tex.get("eyeAlbedo") or None
            if ov_alb:
                entry["tex"] = ov_alb
                entry["texMode"] = 4 if ("_eyes" in pnm or pnm.endswith("eyes")) else 0
            if ov_norm:
                entry["norm"] = ov_norm
            if ov_gain and len(ov_gain) == 3:
                entry["skinGain"] = [float(x) for x in ov_gain]
        if not entry:
            return None
        return {str(m["index"]): dict(entry) for m in item_meta.get("materials") or ()}

    def _import_model_mesh(model_id, use_lod=0, mat_overrides=None, use_anatomy_tex=False):
        item_meta = lib.item(model_id, lod=use_lod)
        if use_anatomy_tex and not mat_overrides:
            mat_overrides = _anatomy_overrides_for(item_meta)
        ckey = (model_id, use_lod, json.dumps(mat_overrides, sort_keys=True) if mat_overrides else "")
        if ckey in mesh_cache:
            return mesh_cache[ckey]
        raw_glb = lib.glb(model_id, lod=use_lod)
        sdoc, sbin = _parse_glb(raw_glb)
        snode = sdoc["nodes"][sdoc["scenes"][sdoc.get("scene", 0)]["nodes"][0]]
        smesh = sdoc["meshes"][snode["mesh"]]
        uv1_shaders = {
            (prim.get("extras") or {}).get("shaderIndex", 0)
            for prim in smesh["primitives"]
            if "TEXCOORD_1" in prim.get("attributes", {})
        }
        mat_map = {}
        tint_pal_map = {}
        for m in item_meta["materials"]:
            ov = (mat_overrides or {}).get(str(m["index"]))
            mat_map[m["index"]] = _build_material(m, item_meta, ov, has_uv1=(m["index"] in uv1_shaders))
            tp_meta = m.get("tintPalette")
            if tp_meta:
                pal_rgb = _load_tint_palette_rgb(lib, model_id, tp_meta)
                if pal_rgb is not None:
                    t_row = int(ov.get("tintRow", 0)) if ov and "tintRow" in ov else 0
                    tint_pal_map[m["index"]] = (pal_rgb, tp_meta["rowLength"], t_row)
        bv_remap = {}
        acc_remap = {}

        def _copy_acc(aidx):
            if aidx in acc_remap:
                return acc_remap[aidx]
            acc = dict(sdoc["accessors"][aidx])
            old_bv = acc["bufferView"]
            if old_bv not in bv_remap:
                sbv = sdoc["bufferViews"][old_bv]
                boff = sbv.get("byteOffset", 0)
                blen = sbv["byteLength"]
                bv_remap[old_bv] = _add_bytes(sbin[boff:boff + blen], sbv.get("target"))
            acc["bufferView"] = bv_remap[old_bv]
            new_aidx = len(out_doc["accessors"])
            out_doc["accessors"].append(acc)
            acc_remap[aidx] = new_aidx
            return new_aidx

        remapped_skin = None
        if combined_skel is not None:
            try:
                remapped_skin = lib.remapped_skin_for_model(model_id, lod=use_lod, skeleton=combined_skel)
                if remapped_skin and remapped_skin.get("extraJoints"):
                    combined_skel["joints"].extend(remapped_skin["extraJoints"])
            except Exception:
                remapped_skin = None
        skin_by_prim = {p["primIndex"]: p for p in remapped_skin["primitives"]} if remapped_skin else {}
        mesh_skinned = False

        out_prims = []
        for pi, prim in enumerate(smesh["primitives"]):
            sh_idx = (prim.get("extras") or {}).get("shaderIndex", 0)
            gmat_idx = mat_map.get(sh_idx)
            if gmat_idx is None and sh_idx in mat_map:
                continue
            if gmat_idx is None:
                out_doc["materials"].append({
                    "name": f"{item_meta['name']}_default",
                    "doubleSided": True,
                    "pbrMetallicRoughness": {"baseColorFactor": [0.5, 0.5, 0.5, 1.0], "metallicFactor": 0.0, "roughnessFactor": 0.7},
                })
                gmat_idx = len(out_doc["materials"]) - 1
                mat_map[sh_idx] = gmat_idx
            new_attrs = {}
            tp_info = tint_pal_map.get(sh_idx)
            for k, v in prim["attributes"].items():
                if k == "COLOR_0":
                    pal_rgb, rlen, trow = tp_info if tp_info else (None, 1, 0)
                    act, payload = _bake_color0_bytes(sdoc, sbin, v, prim.get("extras"), pal_rgb, rlen, trow)
                    if act == "omit":
                        continue
                    if act == "replace":
                        cbytes, ccnt = payload
                        cbv = _add_bytes(cbytes, 34962)
                        cacc = len(out_doc["accessors"])
                        out_doc["accessors"].append({"bufferView": cbv, "componentType": 5126, "count": ccnt, "type": "VEC4"})
                        new_attrs[k] = cacc
                        continue
                new_attrs[k] = _copy_acc(v)

            sk_p = skin_by_prim.get(pi)
            pos_count = sdoc["accessors"][prim["attributes"]["POSITION"]]["count"]
            if sk_p and sk_p.get("skinned") and sk_p["vertices"] == pos_count:
                j0_bv = _add_bytes(sk_p["j0"].tobytes(), 34962)
                new_attrs["JOINTS_0"] = len(out_doc["accessors"])
                out_doc["accessors"].append({"bufferView": j0_bv, "componentType": 5123, "count": pos_count, "type": "VEC4"})
                w0_bv = _add_bytes(sk_p["w0"].tobytes(), 34962)
                new_attrs["WEIGHTS_0"] = len(out_doc["accessors"])
                out_doc["accessors"].append({"bufferView": w0_bv, "componentType": 5126, "count": pos_count, "type": "VEC4"})
                if sk_p.get("hasSkin8"):
                    j1_bv = _add_bytes(sk_p["j1"].tobytes(), 34962)
                    new_attrs["JOINTS_1"] = len(out_doc["accessors"])
                    out_doc["accessors"].append({"bufferView": j1_bv, "componentType": 5123, "count": pos_count, "type": "VEC4"})
                    w1_bv = _add_bytes(sk_p["w1"].tobytes(), 34962)
                    new_attrs["WEIGHTS_1"] = len(out_doc["accessors"])
                    out_doc["accessors"].append({"bufferView": w1_bv, "componentType": 5126, "count": pos_count, "type": "VEC4"})
                mesh_skinned = True

            new_prim = {"attributes": new_attrs, "indices": _copy_acc(prim["indices"]), "material": gmat_idx}
            if "extras" in prim:
                new_prim["extras"] = prim["extras"]
            out_prims.append(new_prim)
        if not out_prims:
            res = (None, snode.get("translation"), item_meta, False)
            mesh_cache[ckey] = res
            return res
        mesh_idx = len(out_doc["meshes"])
        out_doc["meshes"].append({"name": item_meta["name"], "primitives": out_prims})
        res = (mesh_idx, snode.get("translation"), item_meta, mesh_skinned)
        mesh_cache[ckey] = res
        return res

    skinned_node_indices = []

    # 1. Main model
    main_mesh, main_trans, main_meta, main_skinned = _import_model_mesh(main_id, use_lod=lod, mat_overrides=overrides)
    if main_mesh is not None:
        n = {"name": main_meta["name"], "mesh": main_mesh}
        if main_trans:
            n["translation"] = main_trans
        out_doc["nodes"].append(n)
        nidx = len(out_doc["nodes"]) - 1
        out_doc["scenes"][0]["nodes"].append(nidx)
        if main_skinned:
            skinned_node_indices.append(nidx)

    # 2. Assembled parts / outfit layers
    seen_parts = {main_id}
    for pid in part_ids:
        if pid in seen_parts:
            continue
        seen_parts.add(pid)
        try:
            pmesh, ptrans, pmeta, pskinned = _import_model_mesh(pid, use_lod=0, use_anatomy_tex=True)
        except Exception:
            continue
        if pmesh is not None:
            pn = {"name": pmeta["name"], "mesh": pmesh}
            if ptrans:
                pn["translation"] = ptrans
            out_doc["nodes"].append(pn)
            nidx = len(out_doc["nodes"]) - 1
            out_doc["scenes"][0]["nodes"].append(nidx)
            if pskinned:
                skinned_node_indices.append(nidx)

    # 2b. Clean watertight body-cull seam deduplication (hand_*_nude_cull vs feet_*_nude_cull / nipples / fr1 genitals)
    #     Split the 258-tri waist overlap along the V=1.0/0.0 UV atlas boundary so hand keeps the 124
    #     upper-waist torso-UV triangles (v >= 0.85) and feet keeps the 134 lower-waist pelvis-UV triangles (v <= 0.15).
    def _node_tri_centroids(node):
        mesh = out_doc["meshes"][node["mesh"]]
        off = np.array(node.get("translation") or [0.0, 0.0, 0.0], dtype=np.float32)
        cents = []
        for rp in mesh["primitives"]:
            pacc = out_doc["accessors"][rp["attributes"]["POSITION"]]
            pbv = out_doc["bufferViews"][pacc["bufferView"]]
            po = pbv.get("byteOffset", 0) + pacc.get("byteOffset", 0)
            rpos = np.frombuffer(out_bin, dtype="<f4", count=pacc["count"] * 3, offset=po).reshape(-1, 3).copy() + off
            iacc = out_doc["accessors"][rp["indices"]]
            ibv = out_doc["bufferViews"][iacc["bufferView"]]
            io_ = ibv.get("byteOffset", 0) + iacc.get("byteOffset", 0)
            idtype = "<u2" if iacc["componentType"] == 5123 else "<u4"
            ridx = np.frombuffer(out_bin, dtype=idtype, count=iacc["count"], offset=io_).reshape(-1, 3).copy()
            cents.append(rpos[ridx].mean(axis=1))
        return np.vstack(cents) if cents else None

    def _cull_node_against(target_node, ref_specs):
        ref_clouds = []
        for rcloud, tol, v_min, v_max in ref_specs:
            if rcloud is not None and len(rcloud):
                ref_clouds.append((rcloud, float(tol), v_min, v_max))
        if not ref_clouds:
            return
        tmesh = out_doc["meshes"][target_node["mesh"]]
        t_off = np.array(target_node.get("translation") or [0.0, 0.0, 0.0], dtype=np.float32)
        for tp in tmesh["primitives"]:
            pacc = out_doc["accessors"][tp["attributes"]["POSITION"]]
            pbv = out_doc["bufferViews"][pacc["bufferView"]]
            po = pbv.get("byteOffset", 0) + pacc.get("byteOffset", 0)
            tpos = np.frombuffer(out_bin, dtype="<f4", count=pacc["count"] * 3, offset=po).reshape(-1, 3).copy() + t_off
            iacc = out_doc["accessors"][tp["indices"]]
            ibv = out_doc["bufferViews"][iacc["bufferView"]]
            io_ = ibv.get("byteOffset", 0) + iacc.get("byteOffset", 0)
            idtype = "<u2" if iacc["componentType"] == 5123 else "<u4"
            tidx = np.frombuffer(out_bin, dtype=idtype, count=iacc["count"], offset=io_).reshape(-1, 3).copy()
            tv = None
            if "TEXCOORD_0" in tp["attributes"]:
                uacc = out_doc["accessors"][tp["attributes"]["TEXCOORD_0"]]
                ubv = out_doc["bufferViews"][uacc["bufferView"]]
                uo = ubv.get("byteOffset", 0) + uacc.get("byteOffset", 0)
                tuv = np.frombuffer(out_bin, dtype="<f4", count=uacc["count"] * 2, offset=uo).reshape(-1, 2).copy()
                tv = tuv[tidx, 1].mean(axis=1)
            tc = tpos[tidx].mean(axis=1)
            drop = np.zeros(len(tidx), dtype=bool)
            for rcloud, tol, v_min, v_max in ref_clouds:
                cand = np.ones(len(tidx), dtype=bool)
                if v_min is not None:
                    cand &= (tv >= v_min) if tv is not None else False
                if v_max is not None:
                    cand &= (tv <= v_max) if tv is not None else False
                cidx = np.flatnonzero(cand)
                if not len(cidx):
                    continue
                for start_i in range(0, len(cidx), 400):
                    sl = cidx[start_i:start_i + 400]
                    chunk = tc[sl]
                    dmin = np.linalg.norm(chunk[:, None, :] - rcloud[None, :, :], axis=-1).min(axis=1)
                    drop[sl] |= (dmin <= tol)
            if drop.any():
                kept = np.ascontiguousarray(tidx[~drop].ravel())
                new_bv = _add_bytes(kept.tobytes(), 34963)
                new_acc = {
                    "bufferView": new_bv,
                    "componentType": iacc["componentType"],
                    "count": int(len(kept)),
                    "type": "SCALAR",
                }
                out_doc["accessors"].append(new_acc)
                tp["indices"] = len(out_doc["accessors"]) - 1

    import re as _re
    nodes_now = [n for n in out_doc["nodes"] if "mesh" in n]
    hand_nodes = [n for n in nodes_now if _re.match(r"^hand_[a-z0-9]+_000_nude_cull$", n["name"].lower())]
    feet_nodes = [n for n in nodes_now if _re.match(r"^feet_[a-z0-9]+_000_nude_cull$", n["name"].lower())]
    nip_nodes = [n for n in nodes_now if n["name"].lower().startswith("bodynipplesgen_")]
    fr1_gen_nodes = [n for n in nodes_now if n["name"].lower().startswith("bodygenitalsgen_fr1_")]
    # Snapshot original triangle centroids before mutating either mesh's index buffer.
    hand_clouds = [_node_tri_centroids(hn) for hn in hand_nodes]
    feet_clouds = [_node_tri_centroids(fn) for fn in feet_nodes]
    nip_clouds = [_node_tri_centroids(nn) for nn in nip_nodes]
    fr1_gen_clouds = [_node_tri_centroids(gn) for gn in fr1_gen_nodes]
    for hn in hand_nodes:
        refs = [(fc, 0.015, None, 0.15) for fc in feet_clouds] + [(nc, 0.0025, None, None) for nc in nip_clouds]
        _cull_node_against(hn, refs)
    for fn in feet_nodes:
        refs = [(hc, 0.015, 0.85, None) for hc in hand_clouds] + [(gc, 0.0016, None, None) for gc in fr1_gen_clouds]
        _cull_node_against(fn, refs)

    # 3. Instanced vehicle wheels (rim + tyre at each axle TRS)
    if wheel_spec:
        ws = main_meta.get("wheelSetup")
        axles = (wheel_spec.get("axles") if isinstance(wheel_spec, dict) else None) or (ws.get("axles") if ws else None) or []
        rim_id = int(wheel_spec.get("rimId") or (ws.get("defaultRimId") if ws else 0) or 0)
        tyre_id = int(wheel_spec.get("tyreId") or (ws.get("defaultTyreId") if ws else 0) or 0)
        if axles and (rim_id or tyre_id):
            rim_mesh, _, rim_meta, _ = _import_model_mesh(rim_id, 0) if rim_id else (None, None, None, False)
            tyre_mesh, _, tyre_meta, _ = _import_model_mesh(tyre_id, 0) if tyre_id else (None, None, None, False)
            tyre_box = tyre_meta.get("box") if tyre_meta else None
            tyre_unit_r = (max(tyre_box[1][1] - tyre_box[0][1], tyre_box[1][2] - tyre_box[0][2]) * 0.5) if tyre_box else 0.7886
            tyre_unit_w = (tyre_box[1][0] - tyre_box[0][0]) if tyre_box else 0.5458
            rim_box = rim_meta.get("box") if rim_meta else None
            rim_unit_r = (max(rim_box[1][1] - rim_box[0][1], rim_box[1][2] - rim_box[0][2]) * 0.5) if rim_box else 0.5
            rim_r_fix = 0.5 / max(rim_unit_r, 0.1)
            for ax in axles:
                sr = float(ax["radius"]) / max(tyre_unit_r, 0.1)
                sw = float(ax["width"]) / max(tyre_unit_w, 0.05)
                rot = [0.0, 1.0, 0.0, 0.0] if ax.get("right") else [0.0, 0.0, 0.0, 1.0]
                pos = list(ax["position"])
                if rim_mesh is not None:
                    out_doc["nodes"].append({
                        "name": f"{ax['bone']}_rim",
                        "mesh": rim_mesh,
                        "translation": pos,
                        "rotation": rot,
                        "scale": [round(sw, 5), round(sr * rim_r_fix, 5), round(sr * rim_r_fix, 5)],
                    })
                    out_doc["scenes"][0]["nodes"].append(len(out_doc["nodes"]) - 1)
                if tyre_mesh is not None:
                    out_doc["nodes"].append({
                        "name": f"{ax['bone']}_tyre",
                        "mesh": tyre_mesh,
                        "translation": pos,
                        "rotation": rot,
                        "scale": [round(sw, 5), round(sr, 5), round(sr, 5)],
                    })
                    out_doc["scenes"][0]["nodes"].append(len(out_doc["nodes"]) - 1)

    # 4. Skeleton, skin, and optional animation clip (glTF 2.0 Y-up)
    if combined_skel is not None and skinned_node_indices:
        import character_rig as cr
        joints = combined_skel["joints"]
        joint_node_indices = []
        root_joint_nodes = []
        ibm_list = []

        # Create one glTF node per joint (RAGE Z-up TRS -> glTF Y-up TRS)
        base_node_idx = len(out_doc["nodes"])
        for ji, j in enumerate(joints):
            t_yup = cr.rage_to_yup_vec3(j["trans"])
            q_yup = cr.rage_to_yup_quat(j["rot"])
            s_yup = [float(j["scale"][0]), float(j["scale"][2]), float(j["scale"][1])]
            jnode = {"name": j["name"]}
            if any(abs(v) > 1e-7 for v in t_yup):
                jnode["translation"] = [round(v, 7) for v in t_yup]
            if any(abs(v - d0) > 1e-7 for v, d0 in zip(q_yup, (0.0, 0.0, 0.0, 1.0))):
                qn = float(np.linalg.norm(q_yup)) or 1.0
                jnode["rotation"] = [round(v / qn, 7) for v in q_yup]
            if any(abs(v - 1.0) > 1e-6 for v in s_yup):
                jnode["scale"] = [round(v, 7) for v in s_yup]
            out_doc["nodes"].append(jnode)
            joint_node_indices.append(base_node_idx + ji)

            # Inverse bind matrix converted to glTF Y-up: C * IBM_rage * C^T (column-major)
            ibm_rage = np.array(j["ibmCol"], dtype=np.float64).reshape(4, 4).T
            ibm_yup = cr.rage_to_yup_mat4(ibm_rage)
            ibm_list.append(ibm_yup.T.astype("<f4"))

        for ji, j in enumerate(joints):
            par = j["parent"]
            nidx = joint_node_indices[ji]
            if 0 <= par < len(joints):
                out_doc["nodes"][joint_node_indices[par]].setdefault("children", []).append(nidx)
            else:
                root_joint_nodes.append(nidx)

        # Ground-standing wrapper node: inherit exact translation from primary skinned mesh node
        # (e.g. [0, +1.0, 0] for human bipeds, exact ground lift for animals) before stripping it from mesh nodes.
        off_yup = None
        for sn_idx in skinned_node_indices:
            tr = out_doc["nodes"][sn_idx].get("translation")
            if tr and any(abs(v) > 1e-6 for v in tr):
                off_yup = [float(v) for v in tr]
                break
        if off_yup is None:
            off_yup = list(combined_skel.get("defaultOffsetYup") or [0.0, 0.0, 0.0])
        rig_root_node = {
            "name": f"{combined_skel.get('name', 'character')}_rig",
            "children": root_joint_nodes,
        }
        if any(abs(v) > 1e-6 for v in off_yup):
            rig_root_node["translation"] = [float(v) for v in off_yup]
        rig_root_idx = len(out_doc["nodes"])
        out_doc["nodes"].append(rig_root_node)
        out_doc["scenes"][0]["nodes"].append(rig_root_idx)

        ibm_bytes = np.stack(ibm_list, axis=0).tobytes()
        ibm_bv = _add_bytes(ibm_bytes)
        ibm_acc = len(out_doc["accessors"])
        out_doc["accessors"].append({
            "bufferView": ibm_bv,
            "componentType": 5126,
            "count": len(joints),
            "type": "MAT4",
        })
        out_doc["skins"] = [{
            "name": combined_skel.get("name", "skeleton"),
            "joints": joint_node_indices,
            "skeleton": rig_root_idx,
            "inverseBindMatrices": ibm_acc,
        }]
        # Per glTF 2.0 spec, bind skinned mesh nodes to skin 0 and strip redundant mesh translation
        # (the rig_root_node translation lifts both skeleton and skinned mesh cleanly onto y=0).
        for sn_idx in skinned_node_indices:
            out_doc["nodes"][sn_idx]["skin"] = 0
            out_doc["nodes"][sn_idx].pop("translation", None)

        # Optional embedded animation clip
        if anim_dict_id is not None and anim_clip_hash is not None:
            try:
                clip_data = lib.anim.decode_clip_tracks(int(anim_dict_id), int(anim_clip_hash), joints)
                n_frames = int(clip_data["frames"])
                dur = float(clip_data["duration"])
                if n_frames >= 1:
                    times = (np.linspace(0.0, dur, n_frames, dtype="<f4") if n_frames > 1
                             else np.array([0.0], dtype="<f4"))
                    t_bv = _add_bytes(times.tobytes())
                    t_acc = len(out_doc["accessors"])
                    out_doc["accessors"].append({
                        "bufferView": t_bv,
                        "componentType": 5126,
                        "count": n_frames,
                        "type": "SCALAR",
                        "min": [float(times[0])],
                        "max": [float(times[-1])],
                    })
                    samplers = []
                    channels = []

                    def _add_channel(target_node, path_name, arr_f32, vtype):
                        bv = _add_bytes(np.ascontiguousarray(arr_f32, dtype="<f4").tobytes())
                        acc = len(out_doc["accessors"])
                        out_doc["accessors"].append({
                            "bufferView": bv,
                            "componentType": 5126,
                            "count": int(len(arr_f32)),
                            "type": vtype,
                        })
                        s_idx = len(samplers)
                        samplers.append({"input": t_acc, "interpolation": "LINEAR", "output": acc})
                        channels.append({"sampler": s_idx, "target": {"node": target_node, "path": path_name}})

                    for jidx, arr in sorted(clip_data["rotTracks"].items()):
                        # (x, y, z, w) RAGE Z-up -> (x, z, -y, w) glTF Y-up, normalized
                        q_yup = np.column_stack((arr[:, 0], arr[:, 2], -arr[:, 1], arr[:, 3])).astype("<f4")
                        qn = np.maximum(np.linalg.norm(q_yup, axis=1, keepdims=True), 1e-8)
                        q_yup /= qn
                        _add_channel(joint_node_indices[jidx], "rotation", q_yup, "VEC4")

                    for jidx, arr in sorted(clip_data["transTracks"].items()):
                        t_yup = np.column_stack((arr[:, 0], arr[:, 2], -arr[:, 1])).astype("<f4")
                        _add_channel(joint_node_indices[jidx], "translation", t_yup, "VEC3")

                    for jidx, arr in sorted(clip_data["scaleTracks"].items()):
                        s_yup = np.column_stack((arr[:, 0], arr[:, 2], arr[:, 1])).astype("<f4")
                        _add_channel(joint_node_indices[jidx], "scale", s_yup, "VEC3")

                    if anim_root_motion and clip_data.get("moverPos") is not None:
                        mp = clip_data["moverPos"]
                        mp_yup = np.column_stack((mp[:, 0] + off_yup[0], mp[:, 2] + off_yup[1], -mp[:, 1] + off_yup[2])).astype("<f4")
                        _add_channel(rig_root_idx, "translation", mp_yup, "VEC3")
                    if anim_root_motion and clip_data.get("moverRot") is not None:
                        mr = clip_data["moverRot"]
                        mr_yup = np.column_stack((mr[:, 0], mr[:, 2], -mr[:, 1], mr[:, 3])).astype("<f4")
                        mr_yup /= np.maximum(np.linalg.norm(mr_yup, axis=1, keepdims=True), 1e-8)
                        _add_channel(rig_root_idx, "rotation", mr_yup, "VEC4")

                    if channels:
                        out_doc["animations"] = [{
                            "name": clip_data["name"],
                            "channels": channels,
                            "samplers": samplers,
                        }]
            except Exception:
                pass

    # 5. Bake character body proportions (BodyShapePreset morphs) & height_scale into skinned GLB
    if combined_skel is not None and skinned_node_indices:
        hs_val = float(height_scale) if height_scale is not None else 1.0
        has_morphs = isinstance(body_morphs, dict) and any(abs(float(v)) > 1e-4 for v in body_morphs.values())
        if has_morphs or abs(hs_val - 1.0) > 1e-4:
            try:
                from glb_morph_bake import bake_morphs_into_glb
                if is_male is None:
                    skel_nm = (combined_skel.get("name") or "").lower()
                    body_tok = ((main_meta.get("characterAssembly") or {}).get("body") or "").lower()
                    eff_is_male = (
                        skel_nm.startswith(("m", "male")) or "_mr1" in skel_nm or "_mb1" in skel_nm
                        or body_tok.startswith("m")
                    )
                else:
                    eff_is_male = bool(is_male)
                bake_morphs_into_glb(
                    out_doc,
                    out_bin,
                    combined_skel,
                    body_morphs=body_morphs if has_morphs else None,
                    height_scale=hs_val,
                    is_male=eff_is_male,
                    use_rage9_rig=bool(use_rage9_rig),
                )
            except Exception:
                pass

    if used_clearcoat:
        out_doc["extensionsUsed"] = ["KHR_materials_clearcoat"]
    if not out_doc["textures"]:
        for k in ("textures", "images", "samplers"):
            out_doc.pop(k, None)
    out_bin.extend(b"\x00" * ((-len(out_bin)) % 4))
    out_doc["buffers"] = [{"byteLength": len(out_bin)}]
    head = json.dumps(out_doc, separators=(",", ":"), allow_nan=False).encode("utf-8")
    head += b" " * ((-len(head)) % 4)
    total_len = 28 + len(head) + len(out_bin)
    return (
        struct.pack("<4sIIII", b"glTF", 2, total_len, len(head), 0x4E4F534A)
        + head
        + struct.pack("<II", len(out_bin), 0x004E4942)
        + bytes(out_bin)
    )
