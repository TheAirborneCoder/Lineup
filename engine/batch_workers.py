# SPDX-License-Identifier: GPL-3.0-or-later
"""Multi-core worker helpers for parallel GLB decoding and direct GPU-compressed BCn (GTEX) extraction."""

import os
from pathlib import Path
import struct
from paths import GAME


# DXGI codes supported by WebGL2 compressed texture extensions (uploaded as UNORM to match sRGB shader pipeline):
#   bc1: 71 (BC1_UNORM), 72 (BC1_UNORM_SRGB)
#   bc2: 74 (BC2_UNORM), 75 (BC2_UNORM_SRGB)
#   bc3: 77 (BC3_UNORM), 78 (BC3_UNORM_SRGB)
#   bc5: 83 (BC5_UNORM)
#   bc7: 98 (BC7_UNORM), 99 (BC7_UNORM_SRGB)
DXGI_TO_CAP = {
    71: "bc1", 72: "bc1",
    74: "bc2", 75: "bc2",
    77: "bc3", 78: "bc3",
    83: "bc5",
    98: "bc7", 99: "bc7",
}
ALL_GPU_CAPS = frozenset(DXGI_TO_CAP.values())

GTEX_MAGIC = b"GTEX"
GTEX_KIND_BCN = 1
GTEX_KIND_PNG = 2

FLAG_MAGENTA = 1
FLAG_CUTOUT = 2
FLAG_BILINEAR_MASK = 4

# Per-worker-process TtpfArchive LRU (avoids re-parsing .ttpf index headers on every texture)
import threading as _threading
_WORKER_ARCHIVES = {}
_WORKER_ARC_LOCK = _threading.RLock()
_WORKER_PER_ARC_LOCKS = {}


def _worker_archive(rel_path):
    from ttpf_format import TtpfArchive
    with _WORKER_ARC_LOCK:
        arc = _WORKER_ARCHIVES.get(rel_path)
        if arc is not None:
            return arc, _WORKER_PER_ARC_LOCKS[rel_path]
        lk = _WORKER_PER_ARC_LOCKS.setdefault(rel_path, _threading.RLock())
    with lk:
        with _WORKER_ARC_LOCK:
            arc = _WORKER_ARCHIVES.get(rel_path)
            if arc is not None:
                return arc, lk
        arc = TtpfArchive(GAME / rel_path)
        with _WORKER_ARC_LOCK:
            if len(_WORKER_ARCHIVES) >= 96:
                oldest = next(iter(_WORKER_ARCHIVES))
                try:
                    _WORKER_ARCHIVES.pop(oldest).close()
                except Exception:
                    pass
            _WORKER_ARCHIVES[rel_path] = arc
            return arc, lk


def analyze_rgba_flags(image):
    """Match ModelView.analyzeImage() heuristics (magenta placeholder, binary alpha cutout, bilinear terrain mask)."""
    import numpy as np
    im = image.convert("RGBA")
    if im.width > 64 or im.height > 64:
        im = im.resize((min(64, im.width), min(64, im.height)))
    arr = np.asarray(im, dtype=np.uint8)
    if arr.size == 0:
        return 0
    r = arr[..., 0]
    g = arr[..., 1]
    b = arr[..., 2]
    a = arr[..., 3]
    n = int(r.size)
    flags = 0
    mag_hits = int(np.count_nonzero((r > 200) & (g < 60) & (b > 200)))
    if mag_hits * 4 >= n:
        flags |= FLAG_MAGENTA
    clear = int(np.count_nonzero(a < 16))
    solid = int(np.count_nonzero(a > 239))
    if (clear / n) > 0.02 and ((clear + solid) / n) > 0.85:
        flags |= FLAG_CUTOUT
    rf = r.astype(np.float32) * (1.0 / 255.0)
    gf = g.astype(np.float32) * (1.0 / 255.0)
    bf = b.astype(np.float32) * (1.0 / 255.0)
    if float(rf.std()) < 0.015 and float(gf.std()) > 0.03 and float(bf.std()) > 0.03:
        flags |= FLAG_BILINEAR_MASK
    return flags


def pack_gtex_bcn(dxgi, flags, mips):
    """Pack raw linear BCn mip chain into GTEX binary container. mips = [(w, h, bytes), ...]."""
    w0, h0 = mips[0][0], mips[0][1]
    parts = [struct.pack("<4sBBHHHHH", GTEX_MAGIC, GTEX_KIND_BCN, flags, dxgi, w0, h0, len(mips), 0)]
    for w, h, data in mips:
        parts.append(struct.pack("<HHI", w, h, len(data)))
        parts.append(data)
    return b"".join(parts)


def pack_gtex_png(png_data, width=0, height=0, flags=0):
    """Wrap PNG fallback bytes inside GTEX binary container."""
    return struct.pack("<4sBBHHHHHI", GTEX_MAGIC, GTEX_KIND_PNG, flags, 0, width, height, 1, 0, len(png_data)) + png_data


def _write_atomic(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    try:
        tmp.write_bytes(data)
        tmp.replace(path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


def extract_ttpf_gtex(arc, tex_idx, max_size, allowed_caps, cache_path=None):
    """Extract a GPU-ready GTEX payload from an open TtpfArchive without full-res CPU BCn decompression."""
    from ttpf_format import DXGI, decode_pixels, png_bytes
    t = arc.textures[tex_idx]
    dxgi = t["dxgi"]
    spec = DXGI.get(dxgi)
    cap = DXGI_TO_CAP.get(dxgi)
    mips_meta = t["mips"]
    levels = [i for i, m in enumerate(mips_meta) if max(m["width"], m["height"]) <= max_size] or [len(mips_meta) - 1]
    start_lvl = levels[0]

    # Fast GPU-compressed path: raw unswizzled BC1/BC2/BC3/BC5/BC7 mip chain directly from TTPF
    if cap and cap in allowed_caps and spec and not t.get("depth"):
        top_m = mips_meta[start_lvl]
        w0, h0 = top_m["width"], top_m["height"]
        if w0 >= 4 and h0 >= 4 and (w0 % 4 == 0) and (h0 % 4 == 0):
            elem = spec[1]
            chain = []
            for lvl in range(start_lvl, len(mips_meta)):
                mm = mips_meta[lvl]
                mw, mh = mm["width"], mm["height"]
                # Stop before non-4x4 or unlocated tail mips (WebGL2 TEXTURE_MAX_LEVEL makes partial chains complete)
                if mw < 4 or mh < 4 or (mw % 4 != 0) or (mh % 4 != 0):
                    break
                expected = ((mw + 3) // 4) * ((mh + 3) // 4) * elem
                try:
                    raw = arc.mip_linear(t, lvl, 0)
                except Exception:
                    if not chain:
                        break
                    break
                if len(raw) != expected:
                    if not chain:
                        break
                    break
                chain.append((mw, mh, raw))
            if chain:
                # Compute lightweight heuristic flags from the smallest mip in the chain (typically 4x4..32x32, <0.2ms!)
                flags = 0
                if cap != "bc5":  # Normal maps never need magenta/cutout/mask checks
                    probe = chain[-1]
                    for pw, ph, pdata in reversed(chain):
                        if max(pw, ph) >= 16:
                            probe = (pw, ph, pdata)
                            break
                    try:
                        im = decode_pixels(probe[2], dxgi, probe[0], probe[1])
                        flags = analyze_rgba_flags(im)
                    except Exception:
                        flags = 0
                payload = pack_gtex_bcn(dxgi, flags, chain)
                if cache_path:
                    _write_atomic(cache_path, payload)
                return payload

    # Fallback: decode single mip to PNG and wrap in GTEX_KIND_PNG
    errors = []
    for level in levels:
        try:
            picture = arc.mip_image(t, level, 0)
            flags = analyze_rgba_flags(picture)
            blob = png_bytes(picture.convert("RGBA"))
            payload = pack_gtex_png(blob, picture.width, picture.height, flags)
            if cache_path:
                _write_atomic(cache_path, payload)
            return payload
        except Exception as exc:
            errors.append(f"mip {level}: {exc}")
    raise ValueError("No decodable mip: " + "; ".join(errors[:3]))


def decode_glb_worker(job):
    """Top-level picklable worker: decode one RAGE drawable/fragment to GLB on a worker core."""
    from model_decode import decode
    ident, lod, archive, member, root, name, kind, folder_str, source = job
    folder = Path(folder_str)
    try:
        info = decode(archive, member, root, name, kind, folder, lod=lod)
        rel = f"{folder.name}/{info['file']}"
        keep = {k: info[k] for k in (
            "geometryBounds", "drawableBounds", "boundsOffset", "boundsDimensionsErrorM",
            "fragmentBoundsCheck", "decodePath", "lodLevels", "lod", "atRootStub",
        ) if k in info}
        data = (folder / info["file"]).read_bytes()
        return (ident, lod, True, rel, info["vertices"], info["triangles"], info["parts"], keep, source, data)
    except Exception as exc:
        return (ident, lod, False, str(exc)[:500], 0, 0, 0, None, source, b"")


def encode_gtex_ttpf_worker(job):
    """Top-level picklable worker: extract GTEX (BCn or PNG fallback) from a TTPF pack on a worker core."""
    key, rel_path, tex_idx, max_size, caps_tuple, cache_path = job
    try:
        arc, lk = _worker_archive(rel_path)
        with lk:
            data = extract_ttpf_gtex(arc, tex_idx, max_size, frozenset(caps_tuple), cache_path=cache_path)
        return (key, data)
    except Exception:
        return (key, b"")
