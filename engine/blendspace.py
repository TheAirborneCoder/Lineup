# SPDX-License-Identifier: GPL-3.0-or-later
"""GTA VI `.motion` Parametric Blendspace Parser (0D / 1D / 2D / 3D / 4D / 5D).

Standalone reverse-engineered binary parser for GTA VI RAGE `.motion` clips stored inside
clip dictionaries (`anim.rpf`, `story000.rpf`, etc.).

Supports:
  - Both variant modes:
      +0x58 == 1: Single parametric motion (inline sub-motion at +0x120..+0x140)
      +0x58 == 2: Multi-variation composite motion (+0x120 array of 0x38-byte sub-motions
                  + +0x130 array of variation selector nodes, types 1/2/4)
  - All 6 parameter node types:
      Type 1 (0D): Constant / single sample
      Type 3 (1D): Sorted 1D sample axis (+0x20 `{u32 sampleIdx, f32 value}`) -> segments
      Type 4 (2D): Regular 2D barycentric lookup grid (24B cells: 4 corners x 3 packed u16)
                   -> recovers sub-mm 2D sample coords & explicit triangles
      Type 5 (3D): Regular 3D barycentric lookup grid (64B cells: 8 corners x 4 packed u16)
                   + optional explicit +0x20 tetrahedra (exact float32 vertices on 130 nodes!)
                   -> recovers 3D sample coords & tetrahedra
      Type 6 (4D): 1D slice array of M Type-5 3D grids (+0x20/+0x30/+0x40) -> 4D sample coords
      Type 8 (5D): 2D Type-4 outer grid (+0x40..+0xA0) indexing M inner 3D grids -> 5D coords
  - Full sample-to-child indirection via +0x90 (`{u32 childIndex, f32 weight, f32 rate}`) -> +0x60
    (`{clipHash, dictHash}`)
  - Full +0x70 piecewise-linear child time-warping curves (`childDuration`, `blendDuration`,
    `[[t_child, t_blend], ...]`)
  - 89 verified axis parameter names (85.75% of all 51,574 axis occurrences) including RAGE
    dot-component vector parameters (`rotation.z`, `velocity.x/y/z`, `angularvelocity.x/y/z`,
    `desiredvelocity.x/w`, `togoal.x/y/z`, `initialrotation.x/z`, etc.)

CLI usage:
  python blendspace_parser.py --validate
  python blendspace_parser.py --dump ta_run_fwd.motion --limit 1
"""

from __future__ import annotations

import argparse
import collections
import concurrent.futures
import json
import math
import mmap
import os
import struct
import time
import zlib
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import numpy as np

from paths import GAME, INDEX

SYSTEM_BASE = 0x50000000

DEFAULT_ANIM_RPF = GAME / "anim.rpf"
DEFAULT_ANIM_SPANS = INDEX / "anim.rpf" / "deflate-spans.bin"
DEFAULT_STORY_RPF = GAME / "pack" / "story000" / "story000.rpf"
DEFAULT_STORY_SPANS = INDEX / "pack" / "story000" / "story000.rpf" / "deflate-spans.bin"

# Parameter node type -> dimensionality
NODE_TYPE_TO_DIM = {
    1: 0,
    3: 1,
    4: 2,
    5: 3,
    6: 4,
    8: 5,
}

# 89 verified joaat32(lowercase) axis parameter names covering 44,224 / 51,574 (85.75%) occurrences
KNOWN_AXIS_NAMES: Dict[int, str] = {
    0xCB2A07DA: "rotation.z",
    0x6F50198B: "rotationz",
    0x15DE02B1: "yawangle",
    0xE1150E95: "desiredvelocity.w",
    0xD4907132: "initialspeedxy",
    0x772452C5: "angletotarget",
    0xFD5EDF96: "velocity.y",
    0xB62412B1: "angularvelocity.z",
    0x09A9782B: "velocity.x",
    0xC822BF3B: "surfaceincline",
    0x1E2277F5: "facingdirection",
    0x9A7513D7: "facing_desireddirection",
    0xF5FF52DD: "angletoobject",
    0xF997622B: "speed",
    0xA4639899: "verticalheight",
    0x9AE65ED6: "translationy",
    0x88ACBA63: "translationx",
    0xC5B23230: "initialrotation.z",
    0xE8A6FFF9: "speedxy",
    0xB88DF3F2: "togoal.x",
    0xF16B65AC: "togoal.y",
    0x853987CF: "leanangle",
    0x50F90A63: "inputx",
    0x75ADDCDF: "rotation.x",
    0xA07D821C: "verticaloffset",
    0xCB2448C6: "roll",
    0xEC75D72C: "lateraloffset",
    0x3190E501: "pedleanx",
    0xC733E068: "backoffset",
    0xC5F90EC8: "togoal.z",
    0x3F5A8094: "pedleany",
    0xB270E0B3: "verticalbodyheight",
    0x52FDF336: "height",
    0xA81AEBB1: "depthoffset",
    0x561CAB3E: "velocityxyangle",
    0x68875B95: "headingoffset",
    0xFD3C3275: "pedalrotationrate",
    0x3F4BB8CC: "pitch",
    0x64D03B24: "rotation.y",
    0x1074F838: "waterheight",
    0xBCA972D6: "depth",
    0x809A226D: "pitchangle",
    0x93822282: "stairincline",
    0x3E202238: "angularaccelerationz",
    0xEA73A60B: "blend",
    0x0F3805B5: "angle",
    0xD99DDA0D: "mbr",
    0x0A536111: "desiredvelocity.x",
    0xC849A928: "legintensity",
    0xAC040D40: "desireddirection",
    0x9C28615A: "translationz",
    0xB08D53F2: "pitchtotarget",
    0xD773554F: "angularvelocity.x",
    0xC0B78F38: "currentstamina",
    0x5960D98B: "initialrotation.x",
    0x8933D82B: "targetheight",
    0x5A739D92: "moveorientation",
    0x34DF9828: "direction",
    0xE0E46831: "angularvelocity.y",
    0x007E0813: "stridelength",
    0x6427B055: "movingdirection",
    0x2325A549: "turn",
    0xD2B58014: "agitation",
    0x58314E50: "angularvelocityz",
    0xAD4EE4DF: "accely",
    0x24FA826A: "facingup",
    0xC3226EB8: "inputy",
    0x1A0B8727: "carryweight",
    0xE714625C: "excitement",
    0x4E920047: "velocityy",
    0x28CEA232: "initialvelocityy",
    0x7CDF5E9D: "velocity.z",
    0xAFF26621: "effort",
    0x87D9D8EB: "tired",
    0x07E40964: "xdirection",
    0xF91694D5: "parachutepitch",
    0x0EB9C71D: "leftbrake",
    0xC502D63E: "rightbrake",
    0x43F5CF44: "heading",
    0x2B1FFB24: "targetheading",
    0x8225B9A4: "targetdistance",
    0x549BCEB3: "finalheadingoffset",
    0x816CE5FC: "velocityx",
    0xEC249284: "initialspeed",
    0x0546C5AB: "fallheight",
    0x5848B383: "steer_phase",
    0xCF237743: "boost",
    0xBBBADB27: "parachuteroll",
    0x93BC2FB3: "ydirection",
}

_ANGLE_KEYWORDS = ("angle", "rotation", "heading", "direction", "yaw", "pitch", "roll", "orientation")


def joaat32(text: str) -> int:
    h = 0
    for c in text.lower().encode("latin-1", "replace"):
        h = (h + c) & 0xFFFFFFFF
        h = (h + (h << 10)) & 0xFFFFFFFF
        h ^= h >> 6
    h = (h + (h << 3)) & 0xFFFFFFFF
    h ^= h >> 11
    return (h + (h << 15)) & 0xFFFFFFFF


def _is_cyclic_axis(name: Optional[str], vmin: Optional[float], vmax: Optional[float]) -> bool:
    if vmin is None or vmax is None:
        return False
    span = vmax - vmin
    is_angle_name = bool(name and any(k in name for k in _ANGLE_KEYWORDS))
    if not is_angle_name:
        return False
    # Full circle in radians (~2*pi or > 2*pi wrapped) or degrees (~360)
    if 6.1 <= span <= 13.0 and abs(vmin) >= 2.5 and abs(vmax) >= 2.5:
        return True
    if 350.0 <= span <= 370.0 and vmin <= -170.0 and vmax >= 170.0:
        return True
    return False


class SystemPage:
    """Bounds-checked reader for a RAGE virtual system-page buffer (base 0x50000000)."""

    __slots__ = ("d", "n")

    def __init__(self, data: bytes):
        self.d = data
        self.n = len(data)

    def u8(self, at: int) -> int:
        return self.d[at]

    def u16(self, at: int) -> int:
        return struct.unpack_from("<H", self.d, at)[0]

    def u32(self, at: int) -> int:
        return struct.unpack_from("<I", self.d, at)[0]

    def i32(self, at: int) -> int:
        return struct.unpack_from("<i", self.d, at)[0]

    def u64(self, at: int) -> int:
        return struct.unpack_from("<Q", self.d, at)[0]

    def f32(self, at: int) -> float:
        return struct.unpack_from("<f", self.d, at)[0]

    def is_ptr(self, q: int) -> bool:
        return SYSTEM_BASE <= q < SYSTEM_BASE + self.n

    def ptr(self, at: int) -> Optional[int]:
        if at < 0 or at + 8 > self.n:
            return None
        q = self.u64(at)
        if q == 0:
            return None
        if self.is_ptr(q):
            return q - SYSTEM_BASE
        raise ValueError(f"Invalid system pointer 0x{q:x} at 0x{at:x}")

    def string(self, at: int) -> Optional[str]:
        """Read RAGE atString (SSO inline if byte[+0x0F] has high bit set, else ptr)."""
        if at < 0 or at + 16 > self.n:
            return None
        tag = self.d[at + 15]
        if tag & 0x80:
            length = tag & 0x7F
            if length <= 15:
                return self.d[at : at + length].split(b"\x00", 1)[0].decode("latin-1")
        p = self.ptr(at)
        if p is None:
            return None
        end = self.d.find(b"\x00", p)
        if end < 0:
            end = min(p + 256, self.n)
        return self.d[p:end].decode("latin-1")

    def array(self, at: int) -> Tuple[Optional[int], int]:
        """Read RAGE atArray header `{u64 ptr, u16 count, u16 capacity}`."""
        p = self.ptr(at)
        if p is None:
            return None, 0
        return p, self.u16(at + 8)

    def atmap(self, at: int) -> List[Tuple[int, Optional[int]]]:
        buckets, n = self.ptr(at), self.u32(at + 8)
        if buckets is None or n > 4096:
            return []
        out: List[Tuple[int, Optional[int]]] = []
        seen = set()
        for i in range(n):
            e = self.ptr(buckets + 8 * i)
            while e is not None:
                if e in seen:
                    break
                seen.add(e)
                out.append((self.u64(e), self.ptr(e + 8)))
                e = self.ptr(e + 16)
        return out


# ---------------------------------------------------------------------------
# Archive Clip-Dictionary Iterator (deflate-spans.bin)
# ---------------------------------------------------------------------------

def _is_clip_dict_root(head: bytes) -> bool:
    return (
        len(head) >= 0x40
        and head[:8] == b"\x00" * 8
        and head[0x10:0x38] == b"\x00" * 0x28
        and (struct.unpack_from("<Q", head, 8)[0] >> 28) == 5
    )


def _named_root_hash(page: bytes) -> Optional[int]:
    if len(page) < 0x6C or not _is_clip_dict_root(page):
        return None
    s = SystemPage(page)
    try:
        name = s.string(0x38)
    except Exception:
        return None
    stored = s.u32(0x68)
    return stored if name and joaat32(name) == stored else None


def _local_packs(data: bytes) -> Optional[set]:
    if len(data) < 0x7A:
        return None
    refs, count = struct.unpack_from("<QH", data, 0x70)
    if not count or refs < SYSTEM_BASE:
        return set()
    start = refs - SYSTEM_BASE
    end = start + count * 16
    if end > len(data):
        return None
    return {(ptr, nh) for ptr, nh in struct.iter_unpack("<QI4x", memoryview(data)[start:end]) if ptr}


def load_deflate_spans(spans_path: Path) -> List[Tuple[int, int, int, int, int]]:
    raw = spans_path.read_bytes()
    n = len(raw) // 32
    return list(struct.iter_unpack("<QQQII", raw[: n * 32]))


def iter_clip_dicts_range(
    rpf_path: Path,
    spans: List[Tuple[int, int, int, int, int]],
    start_idx: int = 0,
    end_idx: Optional[int] = None,
) -> Iterable[Tuple[int, bytes]]:
    """Yield `(span_offset, joined_system_page_bytes)` for clip dictionaries in `[start_idx, end_idx)`."""
    n = len(spans)
    hi = n if end_idx is None else min(n, end_idx)
    with open(rpf_path, "rb") as fh, mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as m:
        taken = set()
        for i in range(start_idx, hi):
            if i in taken:
                continue
            off, comp, uncomp, _, first4 = spans[i]
            if first4 != 0 or uncomp < 0x80:
                continue
            try:
                data = zlib.decompress(m[off : off + comp])
            except Exception:
                continue
            if not _is_clip_dict_root(data):
                continue
            parts = [i]
            k = i

            def _next_adj(ia: int, ib: int) -> bool:
                oa, ca = spans[ia][0], spans[ia][1]
                return spans[ib][0] == ((oa + ca + 15) & ~15)

            def _separate_root(page: bytes, offset: int) -> Optional[bool]:
                nh = _named_root_hash(page)
                if nh is None:
                    return None
                packs = _local_packs(data)
                if packs is None and len(parts) > 1:
                    prefix = data + b"".join(zlib.decompress(m[spans[j][0] : spans[j][0] + spans[j][1]]) for j in parts[1:])
                    packs = _local_packs(prefix)
                return packs is None or (SYSTEM_BASE + offset, nh) not in packs

            total_len = len(data)
            while k + 1 < n and _next_adj(k, k + 1) and len(parts) < 64:
                nxt_off, nxt_comp, nxt_uncomp, _, _ = spans[k + 1]
                try:
                    page = zlib.decompress(m[nxt_off : nxt_off + nxt_comp])
                except Exception:
                    break
                if _separate_root(page, total_len):
                    break
                parts.append(k + 1)
                taken.add(k + 1)
                total_len += len(page)
                k += 1

            joined = (
                data
                if len(parts) == 1
                else data + b"".join(zlib.decompress(m[spans[j][0] : spans[j][0] + spans[j][1]]) for j in parts[1:])
            )
            yield off, joined


# ---------------------------------------------------------------------------
# Grid Extraction & Coordinate Recovery Solver (2D / 3D / 4D / 5D)
# ---------------------------------------------------------------------------

def _extract_grid_2d(
    s: SystemPage, cells_ptr: int, hdr_off: int
) -> Tuple[Tuple[int, int], Tuple[float, float], List[Tuple[float, float]], Dict[Tuple[int, int], Tuple[Tuple[float, float], Dict[int, float]]]]:
    """Extract unique 2D grid vertices from a Type-4 regular grid header (0x50 bytes at `hdr_off`)."""
    ix0, ix1, iy0, iy1, _iz0, _iz1 = struct.unpack_from("<6i", s.d, hdr_off + 0x08)
    xmin, xmax, ymin, ymax = struct.unpack_from("<4f", s.d, hdr_off + 0x20)
    dx, dy = struct.unpack_from("<2f", s.d, hdr_off + 0x38)
    nx, ny = ix1 - ix0, iy1 - iy0
    verts: Dict[Tuple[int, int], Tuple[Tuple[float, float], Dict[int, float]]] = {}
    for cy in range(ny):
        for cx in range(nx):
            ci = cx + cy * nx
            base = cells_ptr + ci * 24
            for corner, (ox, oy) in enumerate(((0, 0), (1, 0), (0, 1), (1, 1))):
                key = (cx + ox, cy + oy)
                if key in verts:
                    continue
                ws = struct.unpack_from("<3H", s.d, base + corner * 6)
                wmap: Dict[int, float] = {}
                wsum = 0.0
                for wv in ws:
                    si = wv >> 7
                    wi = wv & 0x7F
                    if wi > 0:
                        wmap[si] = wmap.get(si, 0.0) + wi
                        wsum += wi
                if wsum > 0:
                    inv = 1.0 / wsum
                    for k in wmap:
                        wmap[k] *= inv
                pos = (xmin + key[0] * dx, ymin + key[1] * dy)
                verts[key] = (pos, wmap)
    return (nx, ny), (dx, dy), [(xmin, xmax), (ymin, ymax)], verts


def _extract_grid_3d(
    s: SystemPage, cells_ptr: int, hdr_off: int
) -> Tuple[Tuple[int, int, int], Tuple[float, float, float], List[Tuple[float, float]], Dict[Tuple[int, int, int], Tuple[Tuple[float, float, float], Dict[int, float]]]]:
    """Extract unique 3D grid vertices from a Type-5/6/8 3D grid header (0x50 bytes at `hdr_off`)."""
    plane = s.u32(hdr_off + 0x04)
    ix0, ix1, iy0, iy1, iz0, iz1 = struct.unpack_from("<6i", s.d, hdr_off + 0x08)
    xmin, xmax, ymin, ymax, zmin, zmax = struct.unpack_from("<6f", s.d, hdr_off + 0x20)
    dx, dy, dz = struct.unpack_from("<3f", s.d, hdr_off + 0x38)
    nx, ny, nz = ix1 - ix0, iy1 - iy0, iz1 - iz0
    verts: Dict[Tuple[int, int, int], Tuple[Tuple[float, float, float], Dict[int, float]]] = {}
    for cz in range(nz):
        for cy in range(ny):
            for cx in range(nx):
                ci = cx + cy * nx + cz * plane
                base = cells_ptr + ci * 64
                for corner in range(8):
                    ox = corner & 1
                    oy = (corner >> 1) & 1
                    oz = (corner >> 2) & 1
                    key = (cx + ox, cy + oy, cz + oz)
                    if key in verts:
                        continue
                    ws = struct.unpack_from("<4H", s.d, base + corner * 8)
                    wmap: Dict[int, float] = {}
                    wsum = 0.0
                    for wv in ws:
                        si = wv >> 7
                        wi = wv & 0x7F
                        if wi > 0:
                            wmap[si] = wmap.get(si, 0.0) + wi
                            wsum += wi
                    if wsum > 0:
                        inv = 1.0 / wsum
                        for k in wmap:
                            wmap[k] *= inv
                    pos = (xmin + key[0] * dx, ymin + key[1] * dy, zmin + key[2] * dz)
                    verts[key] = (pos, wmap)
    return (nx, ny, nz), (dx, dy, dz), [(xmin, xmax), (ymin, ymax), (zmin, zmax)], verts


def solve_regular_grid_coords(
    sample_ids: List[int],
    dim: int,
    sbounds: List[Tuple[float, float]],
    verts: Dict[Tuple[int, ...], Tuple[Tuple[float, ...], Dict[int, float]]],
) -> Tuple[Dict[int, List[float]], List[Tuple[int, ...]], str, Optional[float]]:
    """Recover exact coordinates for `sample_ids` from regular-grid barycentric weights.

    Algorithm (v3 — exact strict-interior first, then topological interior facets/peaks):
      1. Handle trivial counts (0, 1, 2 samples) analytically.
      2. Step 1a: Solve strictly interior vertices (`len(w) == dim + 1`) alone via truncated SVD;
         keep only nullspace-free (`well_det`) samples for sub-mm accuracy.
      3. Step 1b: If any samples remain unsolved, add topologically interior mid-rows (shared by
         >= 2 simplices or bracketed across opposite grid neighbors) and isolated interior pure
         peaks (`w == 1.0` surrounded on all sides by `w < 1.0`) — ONLY assigning unsolved samples.
      4. Step 2: Iterative single-unknown back-substitution on guaranteed-interior equations.
      5. Step 3: For exterior convex-hull corners with no strict interior rows, pick the innermost
         pure vertex (closest to centroid of non-pure vertices containing that sample).
      6. Step 4: Degenerate/collinear fallback + clamp to `sbounds`.

    Returns `(coords_by_id, simplices, method_name, max_strict_residual)`.
    """
    n = len(sample_ids)
    if n == 0:
        return {}, [], "empty", None

    id_to_local = {sid: idx for idx, sid in enumerate(sample_ids)}
    coords = np.zeros((n, dim), dtype=np.float64)
    solved = np.zeros(n, dtype=bool)

    if n == 1:
        for d in range(dim):
            coords[0, d] = 0.5 * (sbounds[d][0] + sbounds[d][1])
        return {sample_ids[0]: coords[0].tolist()}, [], "single", 0.0

    if n == 2:
        s0, s1 = sample_ids[0], sample_ids[1]
        pts, w1s = [], []
        for pos, wmap in verts.values():
            w0, w1 = wmap.get(s0, 0.0), wmap.get(s1, 0.0)
            if w0 + w1 > 0.5:
                pts.append(pos)
                w1s.append(w1)
        if pts:
            pts_arr = np.array(pts, dtype=np.float64)
            w1_arr = np.array(w1s, dtype=np.float64)
            w1_c = w1_arr - np.mean(w1_arr)
            for d in range(dim):
                lo, hi = sbounds[d]
                if hi - lo < 1e-7:
                    coords[0, d] = coords[1, d] = 0.5 * (lo + hi)
                else:
                    cov = float(np.mean((pts_arr[:, d] - np.mean(pts_arr[:, d])) * w1_c))
                    if abs(cov) < 1e-9:
                        coords[0, d] = coords[1, d] = 0.5 * (lo + hi)
                    elif cov > 0:
                        coords[0, d], coords[1, d] = lo, hi
                    else:
                        coords[0, d], coords[1, d] = hi, lo
        else:
            for d in range(dim):
                coords[0, d], coords[1, d] = sbounds[d]
        return {s0: coords[0].tolist(), s1: coords[1].tolist()}, [(s0, s1)], "two_sample_exact", 0.0

    # Classify grid vertices
    simplices = set()
    strict_rows = []
    mid_rows = []
    pure_verts: Dict[int, List[Tuple[Tuple[int, ...], Tuple[float, ...]]]] = collections.defaultdict(list)
    facet_counts: collections.Counter = collections.Counter()

    for key, (pos, wmap) in verts.items():
        if not wmap:
            continue
        k = len(wmap)
        if k == dim + 1:
            simp = tuple(sorted(wmap.keys()))
            simplices.add(simp)
            strict_rows.append((key, pos, wmap))
        elif k >= 2:
            mid_rows.append((key, pos, wmap))
        elif k == 1:
            si = next(iter(wmap.keys()))
            pure_verts[si].append((key, pos))

    for simp in simplices:
        for i in range(len(simp)):
            facet_counts[simp[:i] + simp[i + 1 :]] += 1
        if dim == 3:
            for i in range(len(simp)):
                for j in range(i + 1, len(simp)):
                    facet_counts[(simp[i], simp[j])] += 1

    def _is_interior_mid(key: Tuple[int, ...], wmap: Dict[int, float]) -> bool:
        supp = tuple(sorted(wmap.keys()))
        if facet_counts.get(supp, 0) >= 2:
            return True
        supp_set = set(supp)
        for d in range(dim):
            km = list(key); km[d] -= 1
            kp = list(key); kp[d] += 1
            nm = verts.get(tuple(km))
            np_ = verts.get(tuple(kp))
            if nm is None or np_ is None:
                continue
            sm = set(nm[1].keys())
            sp = set(np_[1].keys())
            if (sm - supp_set) and (sp - supp_set) and sm != sp:
                return True
        return False

    def _is_interior_pure(key: Tuple[int, ...], si: int) -> bool:
        for d in range(dim):
            for delta in (-1, 1):
                kn = list(key); kn[d] += delta
                nb = verts.get(tuple(kn))
                if nb is None or nb[1].get(si, 0.0) >= 0.999:
                    return False
        return True

    def _fit_rows(rows_w: List[Tuple[Tuple[float, ...], Dict[int, float], float]]):
        if not rows_w:
            return None, set()
        present = sorted({si for _p, wm, _rw in rows_w for si in wm if si in id_to_local})
        if not present:
            return None, set()
        pmap = {si: j for j, si in enumerate(present)}
        m, ncols = len(rows_w), len(present)
        A = np.zeros((m, ncols), dtype=np.float64)
        B = np.zeros((m, dim), dtype=np.float64)
        for r, (pos, wm, rw) in enumerate(rows_w):
            sq = math.sqrt(rw)
            for si, wv in wm.items():
                j = pmap.get(si)
                if j is not None:
                    A[r, j] = wv * sq
            for d in range(dim):
                B[r, d] = pos[d] * sq
        U, S, Vt = np.linalg.svd(A, full_matrices=False)
        thresh = max(1e-4, 1e-4 * (S[0] if len(S) else 1.0))
        null_mask = S < thresh
        if np.any(null_mask):
            V_null = Vt[null_mask, :]
            col_null_norm = np.linalg.norm(V_null, axis=0)
            well_det = {present[j] for j in range(ncols) if col_null_norm[j] < 1e-2}
        else:
            well_det = set(present)
        S_inv = np.zeros_like(S)
        ok = S >= thresh
        S_inv[ok] = 1.0 / S[ok]
        X = (Vt.T * S_inv) @ (U.T @ B)
        return {present[j]: X[j] for j in range(ncols)}, well_det

    strict_eqs = [(pos, wm, 1.0) for _k, pos, wm in strict_rows]

    # Step 1a: Solve strict interior vertices alone first (sub-mm precision!)
    sol1a, well1a = _fit_rows(strict_eqs)
    if sol1a is not None:
        for si in well1a:
            loc = id_to_local[si]
            coords[loc] = sol1a[si]
            solved[loc] = True

    # Build full guaranteed-interior equation set for remaining samples
    eqs = list(strict_eqs)
    if not np.all(solved):
        for key, pos, wm in mid_rows:
            if _is_interior_mid(key, wm):
                eqs.append((pos, wm, 0.5))
        for si, plist in pure_verts.items():
            if si not in id_to_local:
                continue
            for key, pos in plist:
                if _is_interior_pure(key, si):
                    eqs.append((pos, {si: 1.0}, 1.0))

        # Step 1b: Solve combined guaranteed-interior equations, keeping well1a fixed
        if len(eqs) > len(strict_eqs):
            sol1b, well1b = _fit_rows(eqs)
            if sol1b is not None:
                for si in well1b:
                    loc = id_to_local[si]
                    if not solved[loc]:
                        coords[loc] = sol1b[si]
                        solved[loc] = True

    # Step 2: Iterative single-unknown back-substitution on guaranteed-interior equations
    if not np.all(solved) and eqs:
        for _ in range(6):
            changed = False
            per_si: Dict[int, List[Tuple[np.ndarray, float]]] = collections.defaultdict(list)
            for pos, wm, rw in eqs:
                unsolved_in_row = [si for si in wm if si in id_to_local and not solved[id_to_local[si]]]
                if len(unsolved_in_row) == 1:
                    u = unsolved_in_row[0]
                    wu = wm[u]
                    if wu >= 0.05:
                        rhs = np.array(pos, dtype=np.float64)
                        for si, wv in wm.items():
                            if si != u and si in id_to_local:
                                rhs -= wv * coords[id_to_local[si]]
                        per_si[u].append((rhs / wu, wu * wu * rw))
            for u, ests in per_si.items():
                wsum = sum(wt for _, wt in ests)
                coords[id_to_local[u]] = sum(pt * wt for pt, wt in ests) / wsum
                solved[id_to_local[u]] = True
                changed = True
            if not changed or np.all(solved):
                break

    # Step 3: Exterior boundary hull corners with pure vertices -> innermost pure vertex
    if not np.all(solved):
        for loc, si in enumerate(sample_ids):
            if solved[loc]:
                continue
            if pure_verts[si]:
                non_pure = [pos for _k, (pos, wm) in verts.items() if 0.0 < wm.get(si, 0.0) < 1.0]
                if non_pure:
                    centroid = np.mean(np.array(non_pure, dtype=np.float64), axis=0)
                    best_p = min(
                        (p for _k, p in pure_verts[si]),
                        key=lambda p: float(np.linalg.norm(np.array(p, dtype=np.float64) - centroid)),
                    )
                    coords[loc] = best_p
                else:
                    coords[loc] = np.mean([p for _k, p in pure_verts[si]], axis=0)
                solved[loc] = True

    # Step 4: Degenerate / lower-rank fallback (e.g., collinear 2D or planar 3D)
    if not np.all(solved):
        def _has_ident_nb(key: Tuple[int, ...], wm: Dict[int, float]) -> bool:
            for d in range(dim):
                for delta in (-1, 1):
                    kn = list(key); kn[d] += delta
                    nb = verts.get(tuple(kn))
                    if nb is not None and nb[1] == wm:
                        return True
            return False

        extra = list(eqs) + [(pos, wm, 0.1) for key, pos, wm in mid_rows if not _has_ident_nb(key, wm)]
        sol3, _ = _fit_rows(extra)
        if sol3 is not None:
            for loc, si in enumerate(sample_ids):
                if not solved[loc] and si in sol3:
                    coords[loc] = sol3[si]
                    solved[loc] = True
        for loc, si in enumerate(sample_ids):
            if not solved[loc]:
                best_w, best_p = -1.0, None
                for _k, (pos, wm) in verts.items():
                    w = wm.get(si, 0.0)
                    if w > best_w:
                        best_w, best_p = w, pos
                if best_p is not None:
                    coords[loc] = best_p
                else:
                    for d in range(dim):
                        coords[loc, d] = 0.5 * (sbounds[d][0] + sbounds[d][1])
                solved[loc] = True

    # Clamp to sample bounds
    for d in range(dim):
        lo, hi = sbounds[d]
        if hi >= lo:
            coords[:, d] = np.clip(coords[:, d], lo, hi)

    # Measure max reconstruction residual on strict interior vertices
    max_res: Optional[float] = None
    if strict_rows:
        mr = 0.0
        for _k, pos, wm in strict_rows:
            pred = np.zeros(dim, dtype=np.float64)
            ok_all = True
            for si, wv in wm.items():
                loc = id_to_local.get(si)
                if loc is None:
                    ok_all = False
                    break
                pred += wv * coords[loc]
            if ok_all:
                err = float(np.linalg.norm(pred - np.array(pos, dtype=np.float64)))
                if err > mr:
                    mr = err
        max_res = mr

    out_coords = {sid: [round(float(x), 6) for x in coords[loc]] for loc, sid in enumerate(sample_ids)}
    return out_coords, sorted(simplices), "grid_lsq_v3", max_res


# ---------------------------------------------------------------------------
# Core `.motion` Binary Parser
# ---------------------------------------------------------------------------

def _parse_axis_descs(
    s: SystemPage, arr_off: int, ranges: Optional[List[Tuple[float, float]]] = None
) -> List[Dict[str, Any]]:
    p, n = s.array(arr_off)
    axes: List[Dict[str, Any]] = []
    if p is None:
        return axes
    for i in range(n):
        h = s.u32(p + 8 * i)
        param_idx = s.u16(p + 8 * i + 4)
        comp_idx = s.u16(p + 8 * i + 6)
        name = KNOWN_AXIS_NAMES.get(h)
        vmin, vmax = (ranges[i] if ranges and i < len(ranges) else (None, None))
        axes.append(
            {
                "name": name,
                "hash": f"0x{h:08x}",
                "hashInt": h,
                "paramIndex": param_idx,
                "componentIndex": comp_idx,
                "min": round(float(vmin), 6) if vmin is not None else None,
                "max": round(float(vmax), 6) if vmax is not None else None,
                "cyclic": _is_cyclic_axis(name, vmin, vmax),
            }
        )
    return axes


def _parse_children_and_curves(s: SystemPage, clip_off: int) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Parse +0x60 child clip refs and +0x70 time-warping curves."""
    p60, n60 = s.array(clip_off + 0x60)
    children: List[Dict[str, Any]] = []
    if p60 is not None:
        for i in range(n60):
            base = p60 + 16 * i
            ch = s.u32(base + 8)
            dh = s.u32(base + 12)
            children.append(
                {
                    "childIndex": i,
                    "clipHash": f"0x{ch:08x}",
                    "clipHashInt": ch,
                    "dictHash": f"0x{dh:08x}",
                    "dictHashInt": dh,
                }
            )

    p70, n70 = s.array(clip_off + 0x70)
    curves: List[Dict[str, Any]] = []
    if p70 is not None:
        for i in range(n70):
            base = p70 + 0x28 * i
            cdur = s.f32(base + 0x00)
            bdur = s.f32(base + 0x04)
            cp, cn = s.array(base + 0x08)
            pts: List[List[float]] = []
            if cp is not None and cn > 0:
                for k in range(cn):
                    tc, tb = struct.unpack_from("<2f", s.d, cp + 8 * k)
                    pts.append([round(tc, 6), round(tb, 6)])
            curves.append(
                {
                    "childDuration": round(cdur, 6),
                    "blendDuration": round(bdur, 6),
                    "timeCurve": pts,
                }
            )
    return children, curves


def _parse_sample_table(s: SystemPage, clip_off: int) -> List[List[Dict[str, Any]]]:
    """Parse +0x90 sample indirection table: each sample -> list of `{childIndex, weight, rate}`."""
    p90, n90 = s.array(clip_off + 0x90)
    table: List[List[Dict[str, Any]]] = []
    if p90 is None:
        return table
    for i in range(n90):
        sp, sn = s.array(p90 + 16 * i)
        items: List[Dict[str, Any]] = []
        if sp is not None:
            for k in range(sn):
                ci, wt, rate = struct.unpack_from("<Iff", s.d, sp + 12 * k)
                items.append(
                    {
                        "childIndex": int(ci),
                        "weight": round(float(wt), 6),
                        "rate": round(float(rate), 6),
                    }
                )
        table.append(items)
    return table


def _parse_param_node(
    s: SystemPage,
    node: int,
    sample_start: int,
    sample_count_hint: int,
    sample_table: List[List[Dict[str, Any]]],
    children: List[Dict[str, Any]],
    curves: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Parse a single parameter node (Type 1, 3, 4, 5, 6, or 8) and link samples to child clips."""
    nt = s.u32(node + 8)
    dim = NODE_TYPE_TO_DIM.get(nt)
    if dim is None:
        raise ValueError(f"Unknown parameter node type {nt} at 0x{node:x}")

    coords_by_si: Dict[int, List[float]] = {}
    segments: List[List[int]] = []
    triangles: List[List[int]] = []
    tetrahedra: List[List[int]] = []
    axes: List[Dict[str, Any]] = []
    grid_info: Dict[str, Any] = {}
    coord_method = "exact"
    max_residual: Optional[float] = 0.0
    nsamp = sample_count_hint

    if nt == 1:
        # 0D constant / single-sample node
        nsamp = max(sample_count_hint, 1 if sample_start < len(sample_table) else 0)
        for si in range(nsamp):
            coords_by_si[si] = []
        coord_method = "0d_constant"

    elif nt == 3:
        # 1D sorted axis
        vmin, vmax = s.f32(node + 0x30), s.f32(node + 0x34)
        axes = _parse_axis_descs(s, node + 0x10, [(vmin, vmax)])
        p20, n20 = s.array(node + 0x20)
        ordered: List[Tuple[float, int]] = []
        if p20 is not None:
            for i in range(n20):
                si = s.u32(p20 + 8 * i)
                val = s.f32(p20 + 8 * i + 4)
                coords_by_si[si] = [round(float(val), 6)]
                ordered.append((val, si))
        ordered.sort(key=lambda x: x[0])
        for i in range(len(ordered) - 1):
            segments.append([ordered[i][1], ordered[i + 1][1]])
        nsamp = max(sample_count_hint, (max(coords_by_si) + 1) if coords_by_si else 0)
        coord_method = "1d_exact"

    elif nt == 4:
        # 2D regular grid
        nsamp = s.u32(node + 0x90)
        sbounds = [
            (s.f32(node + 0x94), s.f32(node + 0x98)),
            (s.f32(node + 0x9C), s.f32(node + 0xA0)),
        ]
        axes = _parse_axis_descs(s, node + 0x10, sbounds)
        p30, _n30 = s.array(node + 0x30)
        gdims, _steps, gbounds, verts = _extract_grid_2d(s, p30, node + 0x40)
        coords_by_si, simps, coord_method, max_residual = solve_regular_grid_coords(
            list(range(nsamp)), 2, sbounds, verts
        )
        triangles = [list(t) for t in simps if len(t) == 3]
        if not triangles and len(simps) == 1 and len(simps[0]) == 2:
            segments = [list(simps[0])]
        grid_info = {"gridDims": list(gdims), "gridBounds": [[round(a, 6), round(b, 6)] for a, b in gbounds]}

    elif nt == 5:
        # 3D regular grid + optional explicit +0x20 tetrahedra!
        nsamp = s.u32(node + 0x90)
        sbounds = [
            (s.f32(node + 0x94), s.f32(node + 0x98)),
            (s.f32(node + 0x9C), s.f32(node + 0xA0)),
            (s.f32(node + 0xA4), s.f32(node + 0xA8)),
        ]
        axes = _parse_axis_descs(s, node + 0x10, sbounds)
        p30, _n30 = s.array(node + 0x30)
        gdims, _steps, gbounds, verts = _extract_grid_3d(s, p30, node + 0x40)
        tp, tn = s.array(node + 0x20)
        gt_coords: Dict[int, List[float]] = {}
        if tp is not None and tn > 0:
            for ti in range(tn):
                base = tp + 0x70 * ti
                tet = [s.u16(base + 2 * k) for k in range(4)]
                tetrahedra.append(tet)
                for k, vi in enumerate(tet):
                    if vi not in gt_coords:
                        x = s.f32(base + 0x10 + 16 * k)
                        y = s.f32(base + 0x14 + 16 * k)
                        z = s.f32(base + 0x18 + 16 * k)
                        gt_coords[vi] = [round(x, 6), round(y, 6), round(z, 6)]
        if len(gt_coords) >= nsamp and nsamp > 0:
            coords_by_si = {si: gt_coords[si] for si in range(nsamp) if si in gt_coords}
            coord_method = "3d_explicit_tets"
            max_residual = 0.0
        else:
            rec_coords, simps, coord_method, max_residual = solve_regular_grid_coords(
                list(range(nsamp)), 3, sbounds, verts
            )
            if gt_coords:
                rec_coords.update(gt_coords)
                coord_method = "3d_explicit_tets_plus_grid"
            coords_by_si = rec_coords
            if not tetrahedra:
                tetrahedra = [list(t) for t in simps if len(t) == 4]
        grid_info = {
            "gridDims": list(gdims),
            "gridBounds": [[round(a, 6), round(b, 6)] for a, b in gbounds],
            "hasExplicitTets": bool(tp is not None and tn > 0),
        }

    elif nt == 6:
        # 4D: 1D array of M Type-5 3D grids
        nsamp = s.u32(node + 0x50)
        sbounds = [(s.f32(node + 0x54 + 8 * i), s.f32(node + 0x58 + 8 * i)) for i in range(4)]
        axes = _parse_axis_descs(s, node + 0x10, sbounds)
        p20, n20 = s.array(node + 0x20)
        p30, _n30 = s.array(node + 0x30)
        p40, n40 = s.array(node + 0x40)
        w_vals = [s.f32(p40 + 4 * i) for i in range(n40)] if p40 is not None else []
        accum_xyz: Dict[int, Tuple[np.ndarray, float]] = {}
        accum_w: Dict[int, Tuple[float, float]] = {}
        worst_res: Optional[float] = None
        for mi in range(n20):
            gp, _gn = s.array(p20 + 16 * mi)
            if gp is None:
                continue
            _gdims, _steps, gbounds, verts = _extract_grid_3d(s, gp, p30 + 0x50 * mi)
            active = sorted({si for _p, wm in verts.values() for si in wm if si < nsamp})
            if not active:
                continue
            sc, _simps, _m, mres = solve_regular_grid_coords(active, 3, sbounds[:3], verts)
            if mres is not None and (worst_res is None or mres > worst_res):
                worst_res = mres
            # Count per-sample presence in this slice for weighting across slices
            cnts: collections.Counter = collections.Counter()
            for _p, wm in verts.values():
                wt_bonus = 4.0 if len(wm) == 4 else 1.0
                for si, wv in wm.items():
                    if si < nsamp:
                        cnts[si] += wv * wt_bonus
            wm_val = w_vals[mi] if mi < len(w_vals) else 0.0
            for si in active:
                wt = max(cnts[si], 1e-3)
                vec = np.array(sc[si], dtype=np.float64)
                if si in accum_xyz:
                    old_v, old_w = accum_xyz[si]
                    accum_xyz[si] = (old_v + vec * wt, old_w + wt)
                    old_wv, old_ww = accum_w[si]
                    accum_w[si] = (old_wv + wm_val * wt, old_ww + wt)
                else:
                    accum_xyz[si] = (vec * wt, wt)
                    accum_w[si] = (wm_val * wt, wt)
        for si in range(nsamp):
            if si in accum_xyz:
                xyz = accum_xyz[si][0] / accum_xyz[si][1]
                wv = accum_w[si][0] / accum_w[si][1]
                coords_by_si[si] = [round(float(xyz[0]), 6), round(float(xyz[1]), 6), round(float(xyz[2]), 6), round(float(wv), 6)]
            else:
                coords_by_si[si] = [round(0.5 * (lo + hi), 6) for lo, hi in sbounds]
        coord_method = "4d_slice_3d_lsq"
        max_residual = worst_res
        grid_info = {"numSlices": n20, "sliceAxis3Values": [round(float(v), 6) for v in w_vals]}

    elif nt == 8:
        # 5D: 2D Type-4 outer grid (+0x40..+0xA0) indexing M inner 3D grids (+0x20, +0x30)
        nsamp = s.u32(node + 0xA0)
        sbounds = [(s.f32(node + 0xA4 + 8 * i), s.f32(node + 0xA8 + 8 * i)) for i in range(5)]
        axes = _parse_axis_descs(s, node + 0x10, sbounds)
        p20, n20 = s.array(node + 0x20)
        p30, _n30 = s.array(node + 0x30)
        p40, _n40 = s.array(node + 0x40)
        # Solve outer 2D grid for slice coordinates (axes 3, 4)
        ogdims, _osteps, ogbounds, overts = _extract_grid_2d(s, p40, node + 0x50)
        slice_coords, _osimps, _om, ores = solve_regular_grid_coords(list(range(n20)), 2, sbounds[3:5], overts)
        accum_xyz = {}
        accum_uv: Dict[int, Tuple[np.ndarray, float]] = {}
        worst_res = ores
        for mi in range(n20):
            gp, _gn = s.array(p20 + 16 * mi)
            if gp is None:
                continue
            _gdims, _steps, _gb, verts = _extract_grid_3d(s, gp, p30 + 0x50 * mi)
            active = sorted({si for _p, wm in verts.values() for si in wm if si < nsamp})
            if not active:
                continue
            sc, _simps, _m, mres = solve_regular_grid_coords(active, 3, sbounds[:3], verts)
            if mres is not None and (worst_res is None or mres > worst_res):
                worst_res = mres
            cnts = collections.Counter()
            for _p, wm in verts.values():
                wt_bonus = 4.0 if len(wm) == 4 else 1.0
                for si, wv in wm.items():
                    if si < nsamp:
                        cnts[si] += wv * wt_bonus
            uv_vec = np.array(slice_coords.get(mi, [0.5 * (sbounds[3][0] + sbounds[3][1]), 0.5 * (sbounds[4][0] + sbounds[4][1])]), dtype=np.float64)
            for si in active:
                wt = max(cnts[si], 1e-3)
                vec = np.array(sc[si], dtype=np.float64)
                if si in accum_xyz:
                    old_v, old_w = accum_xyz[si]
                    accum_xyz[si] = (old_v + vec * wt, old_w + wt)
                    old_uv, old_uw = accum_uv[si]
                    accum_uv[si] = (old_uv + uv_vec * wt, old_uw + wt)
                else:
                    accum_xyz[si] = (vec * wt, wt)
                    accum_uv[si] = (uv_vec * wt, wt)
        for si in range(nsamp):
            if si in accum_xyz:
                xyz = accum_xyz[si][0] / accum_xyz[si][1]
                uv = accum_uv[si][0] / accum_uv[si][1]
                coords_by_si[si] = [round(float(x), 6) for x in (xyz[0], xyz[1], xyz[2], uv[0], uv[1])]
            else:
                coords_by_si[si] = [round(0.5 * (lo + hi), 6) for lo, hi in sbounds]
        coord_method = "5d_outer2d_inner3d_lsq"
        max_residual = worst_res
        grid_info = {
            "numSlices": n20,
            "outerGridDims": list(ogdims),
            "outerGridBounds": [[round(a, 6), round(b, 6)] for a, b in ogbounds],
        }

    # Assemble sample records linking local sampleIndex -> +0x90[sample_start + si] -> +0x60 & +0x70
    samples_out: List[Dict[str, Any]] = []
    for si in range(nsamp):
        g_si = sample_start + si
        entries = sample_table[g_si] if g_si < len(sample_table) else []
        primary = entries[0] if entries else None
        ci = primary["childIndex"] if primary is not None else (si if si < len(children) else None)
        ch_rec = children[ci] if (ci is not None and ci < len(children)) else None
        cv_rec = curves[ci] if (ci is not None and ci < len(curves)) else None
        s_obj: Dict[str, Any] = {
            "sampleIndex": si,
            "globalSampleIndex": g_si,
            "childIndex": ci,
            "dictHash": ch_rec["dictHash"] if ch_rec else None,
            "clipHash": ch_rec["clipHash"] if ch_rec else None,
            "weight": primary["weight"] if primary else 1.0,
            "rate": primary["rate"] if primary else 1.0,
            "coords": coords_by_si.get(si, []),
            "childDuration": cv_rec["childDuration"] if cv_rec else None,
            "blendDuration": cv_rec["blendDuration"] if cv_rec else None,
            "timeCurve": cv_rec["timeCurve"] if cv_rec else [],
        }
        if len(entries) > 1:
            extras = []
            for ent in entries[1:]:
                eci = ent["childIndex"]
                ech = children[eci] if eci < len(children) else None
                extras.append(
                    {
                        "childIndex": eci,
                        "dictHash": ech["dictHash"] if ech else None,
                        "clipHash": ech["clipHash"] if ech else None,
                        "weight": ent["weight"],
                        "rate": ent["rate"],
                    }
                )
            s_obj["extraChildren"] = extras
        samples_out.append(s_obj)

    return {
        "dimensions": dim,
        "nodeType": nt,
        "axes": axes,
        "samples": samples_out,
        "segments": segments,
        "triangles": triangles,
        "tetrahedra": tetrahedra,
        "coordMethod": coord_method,
        "maxResidual": round(float(max_residual), 6) if max_residual is not None else None,
        "gridInfo": grid_info,
    }


def _parse_selectors(s: SystemPage, clip_off: int) -> List[Dict[str, Any]]:
    sp, sn = s.array(clip_off + 0x130)
    out: List[Dict[str, Any]] = []
    if sp is None:
        return out
    for j in range(sn):
        sel = s.ptr(sp + 8 * j)
        if sel is None:
            continue
        st = s.u32(sel + 8)
        axes = _parse_axis_descs(s, sel + 0x10)
        p20, n20 = s.array(sel + 0x20)
        rec: Dict[str, Any] = {"selectorType": st, "axes": axes}
        if st == 4 and p20 is not None:
            groups = []
            for i in range(n20):
                hp, hn = s.array(p20 + 16 * i)
                hashes = [f"0x{s.u32(hp + 4 * k):08x}" for k in range(hn)] if hp is not None else []
                groups.append(hashes)
            rec["tagGroups"] = groups
        elif st == 1 and p20 is not None:
            groups_f = []
            for i in range(n20):
                fp, fn = s.array(p20 + 16 * i)
                vals = [round(s.f32(fp + 4 * k), 6) for k in range(fn)] if fp is not None else []
                groups_f.append(vals)
            rec["thresholdGroups"] = groups_f
        elif st == 2 and p20 is not None:
            rec["bucketCount"] = n20
        out.append(rec)
    return out


def is_motion_clip(s: SystemPage, clip_off: int) -> bool:
    if clip_off is None or clip_off + 0x140 > s.n:
        return False
    # .motion clips have a valid system pointer at +0x20 (descriptor block) and variantType in {1, 2} at +0x58
    if not s.is_ptr(s.u64(clip_off + 0x20)):
        return False
    if s.u32(clip_off + 0x58) not in (1, 2):
        return False
    name_at = s.u64(clip_off + 0x30)
    tag = s.d[clip_off + 0x3F]
    if not (s.is_ptr(name_at) or ((tag & 0x80) and (tag & 0x7F) <= 15)):
        return False
    try:
        nm = s.string(clip_off + 0x30) or ""
    except Exception:
        return False
    return nm.endswith(".motion")


def parse_motion_clip(s: SystemPage, clip_off: int, dict_name: str = "") -> Dict[str, Any]:
    """Parse a `.motion` clip at `clip_off` inside `SystemPage` `s` into a clean JSON-serializable dict."""
    pack_name = s.string(clip_off + 0x30) or ""
    clean_name = pack_name[6:] if pack_name.startswith("pack:/") else pack_name
    clip_hash = s.u32(clip_off + 0x28)
    dict_hash = s.u32(clip_off + 0x2C)
    variant_type = s.u32(clip_off + 0x58)
    total_params = s.u32(clip_off + 0x5C)

    children, curves = _parse_children_and_curves(s, clip_off)
    sample_table = _parse_sample_table(s, clip_off)
    _p80, n80 = s.array(clip_off + 0x80)

    if variant_type == 1:
        node = s.ptr(clip_off + 0x138)
        if node is None:
            raise ValueError(f"Null parameter node in single motion {dict_name}::{pack_name}")
        reg_ptr = s.ptr(clip_off + 0x130)
        reg_type = s.u32(reg_ptr + 8) if reg_ptr is not None else None
        parsed_node = _parse_param_node(s, node, 0, len(sample_table), sample_table, children, curves)
        return {
            "name": clean_name,
            "packName": pack_name,
            "dictName": dict_name,
            "hash": f"0x{clip_hash:08x}",
            "dictHash": f"0x{dict_hash:08x}",
            "variantType": 1,
            "totalParams": total_params,
            "childCount": len(children),
            "sampleCount": len(sample_table),
            "dimensions": parsed_node["dimensions"],
            "nodeType": parsed_node["nodeType"],
            "axes": parsed_node["axes"],
            "samples": parsed_node["samples"],
            "segments": parsed_node["segments"],
            "triangles": parsed_node["triangles"],
            "tetrahedra": parsed_node["tetrahedra"],
            "coordMethod": parsed_node["coordMethod"],
            "maxResidual": parsed_node["maxResidual"],
            "gridInfo": parsed_node["gridInfo"],
            "regCurveType": reg_type,
            "moverCurveCount": n80,
        }

    elif variant_type == 2:
        ap, an = s.array(clip_off + 0x120)
        if ap is None or an == 0:
            raise ValueError(f"Empty +0x120 sub-motion array in variantType=2 {dict_name}::{pack_name}")
        sub_motions: List[Dict[str, Any]] = []
        for si in range(an):
            ebase = ap + 0x38 * si
            reg_ptr = s.ptr(ebase + 0x10)
            reg_type = s.u32(reg_ptr + 8) if reg_ptr is not None else None
            node = s.ptr(ebase + 0x18)
            if node is None:
                raise ValueError(f"Null sub-motion node[{si}] in {dict_name}::{pack_name}")
            c_start = s.u32(ebase + 0x20)
            c_count = s.u32(ebase + 0x24)
            s_start = s.u32(ebase + 0x28)
            s_count = s.u32(ebase + 0x2C)
            pnode = _parse_param_node(s, node, s_start, s_count, sample_table, children, curves)
            pnode["subIndex"] = si
            pnode["childStart"] = c_start
            pnode["childCount"] = c_count
            pnode["sampleStart"] = s_start
            pnode["sampleCount"] = s_count
            pnode["regCurveType"] = reg_type
            sub_motions.append(pnode)

        selectors = _parse_selectors(s, clip_off)
        # Promote primary (highest-dimension, else first) sub-motion fields to top-level for uniform access
        primary = max(sub_motions, key=lambda sm: sm["dimensions"])
        return {
            "name": clean_name,
            "packName": pack_name,
            "dictName": dict_name,
            "hash": f"0x{clip_hash:08x}",
            "dictHash": f"0x{dict_hash:08x}",
            "variantType": 2,
            "totalParams": total_params,
            "childCount": len(children),
            "sampleCount": len(sample_table),
            "dimensions": primary["dimensions"],
            "nodeType": primary["nodeType"],
            "axes": primary["axes"],
            "samples": primary["samples"],
            "segments": primary["segments"],
            "triangles": primary["triangles"],
            "tetrahedra": primary["tetrahedra"],
            "coordMethod": primary["coordMethod"],
            "maxResidual": primary["maxResidual"],
            "gridInfo": primary["gridInfo"],
            "subMotions": sub_motions,
            "selectors": selectors,
            "moverCurveCount": n80,
        }

    else:
        raise ValueError(f"Unsupported variantType (+0x58) = {variant_type} in {dict_name}::{pack_name}")


def parse_clip_dict_motions(joined_page: bytes) -> List[Dict[str, Any]]:
    """Parse all `.motion` clips in a joined clip dictionary buffer."""
    s = SystemPage(joined_page)
    dict_name = s.string(0x38) or ""
    out: List[Dict[str, Any]] = []
    for _key, val in s.atmap(0x48):
        if is_motion_clip(s, val):
            out.append(parse_motion_clip(s, val, dict_name))
    return out


# ---------------------------------------------------------------------------
# Runtime Blendspace Evaluator (1D & 2D barycentric)
# ---------------------------------------------------------------------------

def _delaunay_2d(cmap: Dict[int, List[float]]) -> List[List[int]]:
    """Pure-Python Bowyer-Watson 2D Delaunay triangulation for gap-filling coarse grids."""
    items = [(sid, float(p[0]), float(p[1])) for sid, p in cmap.items() if len(p) >= 2]
    if len(items) < 3:
        return []
    xs = [p[1] for p in items]
    ys = [p[2] for p in items]
    minx, maxx = min(xs), max(xs)
    miny, maxy = min(ys), max(ys)
    dmax = max(maxx - minx, maxy - miny, 1.0)
    midx, midy = (minx + maxx) * 0.5, (miny + maxy) * 0.5

    coords: Dict[int, Tuple[float, float]] = {sid: (x, y) for sid, x, y in items}
    coords[-1] = (midx - 20.0 * dmax, midy - dmax)
    coords[-2] = (midx, midy + 20.0 * dmax)
    coords[-3] = (midx + 20.0 * dmax, midy - dmax)
    tris: List[Tuple[int, int, int]] = [(-1, -2, -3)]

    def _in_circumcircle(tri: Tuple[int, int, int], px: float, py: float) -> bool:
        ax, ay = coords[tri[0]]
        bx, by = coords[tri[1]]
        cx, cy = coords[tri[2]]
        det = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)
        if abs(det) < 1e-14:
            return False
        if det < 0:
            bx, by, cx, cy = cx, cy, bx, by
        dax, day = ax - px, ay - py
        dbx, dby = bx - px, by - py
        dcx, dcy = cx - px, cy - py
        val = (
            (dax * dax + day * day) * (dbx * dcy - dcx * dby)
            - (dbx * dbx + dby * dby) * (dax * dcy - dcx * day)
            + (dcx * dcx + dcy * dcy) * (dax * dby - dbx * day)
        )
        return val > 1e-12

    for sid, px, py in items:
        bad = [t for t in tris if _in_circumcircle(t, px, py)]
        if not bad:
            continue
        edge_count: Dict[Tuple[int, int], int] = {}
        for t in bad:
            for e in ((t[0], t[1]), (t[1], t[2]), (t[2], t[0])):
                ke = (e[0], e[1]) if e[0] < e[1] else (e[1], e[0])
                edge_count[ke] = edge_count.get(ke, 0) + 1
        tris = [t for t in tris if t not in bad]
        for (e0, e1), cnt in edge_count.items():
            if cnt == 1:
                tris.append((e0, e1, sid))

    out: List[List[int]] = []
    for t in tris:
        if t[0] < 0 or t[1] < 0 or t[2] < 0:
            continue
        ax, ay = coords[t[0]]
        bx, by = coords[t[1]]
        cx, cy = coords[t[2]]
        if abs((bx - ax) * (cy - ay) - (by - ay) * (cx - ax)) < 1e-10:
            continue
        out.append(sorted(t))
    out.sort()
    return out


def _eval_triangles_2d(
    tris: List[List[int]], cmap: Dict[int, List[float]], px: float, py: float
) -> Tuple[Optional[List[Tuple[int, float]]], Optional[List[int]], Optional[Tuple[float, float, float]], float]:
    best_tri: Optional[List[int]] = None
    best_bary: Optional[Tuple[float, float, float]] = None
    best_neg = -1e30
    for tri in tris:
        a, b, c = cmap.get(tri[0]), cmap.get(tri[1]), cmap.get(tri[2])
        if not (a and b and c):
            continue
        det = (b[1] - c[1]) * (a[0] - c[0]) + (c[0] - b[0]) * (a[1] - c[1])
        if abs(det) < 1e-12:
            continue
        l0 = ((b[1] - c[1]) * (px - c[0]) + (c[0] - b[0]) * (py - c[1])) / det
        l1 = ((c[1] - a[1]) * (px - c[0]) + (a[0] - c[0]) * (py - c[1])) / det
        l2 = 1.0 - l0 - l1
        mn = min(l0, l1, l2)
        if mn >= -1e-6:
            ws = [max(0.0, l0), max(0.0, l1), max(0.0, l2)]
            s_w = sum(ws)
            return [(tri[i], ws[i] / s_w) for i in range(3) if ws[i] > 1e-6], tri, (l0, l1, l2), mn
        if mn > best_neg:
            best_neg = mn
            best_tri = tri
            best_bary = (l0, l1, l2)
    return None, best_tri, best_bary, best_neg


def evaluate_blendspace(blendspace: Dict[str, Any], params: List[float]) -> List[Tuple[int, float]]:
    """Evaluate blend weights `[(sampleIndex, weight), ...]` for a parsed 0D / 1D / 2D blendspace."""
    dim = blendspace["dimensions"]
    samples = blendspace["samples"]
    if not samples:
        return []
    if dim == 0 or len(samples) == 1:
        return [(samples[0]["sampleIndex"], 1.0)]

    if dim == 1:
        x = float(params[0])
        pts = sorted((s["coords"][0], s["sampleIndex"]) for s in samples if len(s["coords"]) >= 1)
        if not pts:
            return []
        if x <= pts[0][0]:
            return [(pts[0][1], 1.0)]
        if x >= pts[-1][0]:
            return [(pts[-1][1], 1.0)]
        for i in range(len(pts) - 1):
            x0, s0 = pts[i]
            x1, s1 = pts[i + 1]
            if x0 <= x <= x1:
                if x1 - x0 < 1e-9:
                    return [(s0, 1.0)]
                t = (x - x0) / (x1 - x0)
                return [(s0, 1.0 - t), (s1, t)]
        return [(pts[-1][1], 1.0)]

    if dim == 2:
        px, py = float(params[0]), float(params[1])
        cmap = {s["sampleIndex"]: s["coords"] for s in samples if len(s["coords"]) >= 2}
        # Exact sample vertex hit
        for sid, c in cmap.items():
            if (c[0] - px) ** 2 + (c[1] - py) ** 2 < 1e-12:
                return [(sid, 1.0)]
        # 1) Search GTA VI grid-observed triangles first (preserves authored diagonals)
        hit, best_tri, best_bary, best_neg = _eval_triangles_2d(blendspace.get("triangles", []), cmap, px, py)
        if hit is not None:
            return hit
        # 2) Gap fallback: Delaunay triangulation over all recovered samples
        del_tris = _delaunay_2d(cmap)
        hit2, d_tri, d_bary, d_neg = _eval_triangles_2d(del_tris, cmap, px, py)
        if hit2 is not None:
            return hit2
        if d_tri is not None and d_neg > best_neg:
            best_tri, best_bary = d_tri, d_bary
        # 3) Outside convex hull: project to nearest triangle edge/vertex
        if best_tri is not None and best_bary is not None:
            ws = [max(0.0, w) for w in best_bary]
            s_w = sum(ws)
            if s_w > 1e-9:
                return [(best_tri[i], ws[i] / s_w) for i in range(3) if ws[i] > 1e-6]
        # Nearest sample fallback
        nearest = min(cmap.items(), key=lambda kv: (kv[1][0] - px) ** 2 + (kv[1][1] - py) ** 2)[0]
        return [(nearest, 1.0)]

    raise NotImplementedError(f"evaluate_blendspace currently supports 0D/1D/2D (got {dim}D)")


# ---------------------------------------------------------------------------
# Parallel Full-Archive Validation
# ---------------------------------------------------------------------------

def _classify_domain(dict_name: str) -> str:
    dl = dict_name.lower()
    if "@creatures@" in dl or any(k in dl for k in ("@dog@", "@horse@", "@shark@", "@dolphin@", "@bird@", "@bear@", "@alligator@", "@deer@", "@cat@", "@pig@", "@cow@", "@frog@", "@mantaray@", "@crane@", "@gator@")):
        return "animal/creature"
    if "@vehicles@" in dl or "@bicycle@" in dl or "@boat@" in dl or "@bike@" in dl:
        return "vehicle/mount"
    if "@scripted@" in dl or "story@" in dl:
        return "scripted/story"
    return "human/gameplay"


def _validate_worker(args: Tuple[str, str, int, int]) -> Dict[str, Any]:
    rpf_str, spans_str, lo, hi = args
    rpf_path, spans_path = Path(rpf_str), Path(spans_str)
    spans = load_deflate_spans(spans_path)

    stats: Dict[str, Any] = {
        "dicts": 0,
        "clips_ok": 0,
        "clips_err": 0,
        "errors": [],
        "by_variant": collections.Counter(),
        "by_clip_dim": collections.Counter(),
        "by_node_type": collections.Counter(),
        "by_domain": collections.Counter(),
        "by_domain_dim": collections.Counter(),
        "axis_total": 0,
        "axis_resolved": 0,
        "t4_residuals": [],
        "t5_explicit": 0,
        "t5_grid_only": 0,
        "t5_grid_residuals": [],
        "t6_residuals": [],
        "t8_residuals": [],
        "samples_total": 0,
        "samples_valid_coords": 0,
        "curves_total": 0,
        "curves_piecewise": 0,
        "curves_linear_stretch": 0,
        "curves_identity": 0,
        "curves_empty": 0,
        "curves_rate_scaled_samples": 0,
        "json_check_ok": 0,
    }

    for _off, joined in iter_clip_dicts_range(rpf_path, spans, lo, hi):
        stats["dicts"] += 1
        s = SystemPage(joined)
        try:
            dname = s.string(0x38) or ""
            entries = s.atmap(0x48)
        except Exception:
            continue
        domain = _classify_domain(dname)
        for _key, val in entries:
            if not is_motion_clip(s, val):
                continue
            try:
                parsed = parse_motion_clip(s, val, dname)
                if stats["clips_ok"] % 50 == 0 or parsed["dimensions"] >= 4:
                    json.dumps(parsed)
                    stats["json_check_ok"] += 1
            except Exception as exc:
                stats["clips_err"] += 1
                if len(stats["errors"]) < 10:
                    stats["errors"].append(f"{dname}: {exc}")
                continue

            stats["clips_ok"] += 1
            vt = parsed["variantType"]
            stats["by_variant"][vt] += 1
            stats["by_clip_dim"][parsed["dimensions"]] += 1
            stats["by_domain"][domain] += 1
            stats["by_domain_dim"][(domain, parsed["dimensions"])] += 1

            nodes_list = [parsed] if vt == 1 else parsed["subMotions"]
            for nd in nodes_list:
                nt = nd["nodeType"]
                dim = nd["dimensions"]
                stats["by_node_type"][nt] += 1
                for ax in nd["axes"]:
                    stats["axis_total"] += 1
                    if ax["name"] is not None:
                        stats["axis_resolved"] += 1
                mr = nd["maxResidual"]
                if nt == 4 and mr is not None:
                    stats["t4_residuals"].append(mr)
                elif nt == 5:
                    if nd["gridInfo"].get("hasExplicitTets"):
                        stats["t5_explicit"] += 1
                    else:
                        stats["t5_grid_only"] += 1
                        if mr is not None:
                            stats["t5_grid_residuals"].append(mr)
                elif nt == 6 and mr is not None:
                    stats["t6_residuals"].append(mr)
                elif nt == 8 and mr is not None:
                    stats["t8_residuals"].append(mr)

                for samp in nd["samples"]:
                    stats["samples_total"] += 1
                    c = samp["coords"]
                    if len(c) == dim and all(math.isfinite(x) for x in c):
                        stats["samples_valid_coords"] += 1
                    if abs(samp["rate"] - 1.0) > 1e-4:
                        stats["curves_rate_scaled_samples"] += 1

            for sel in parsed.get("selectors", []):
                for ax in sel.get("axes", []):
                    stats["axis_total"] += 1
                    if ax["name"] is not None:
                        stats["axis_resolved"] += 1

            _ch, cvs = _parse_children_and_curves(s, val)
            for cv in cvs:
                stats["curves_total"] += 1
                pts = cv["timeCurve"]
                if len(pts) == 0:
                    stats["curves_empty"] += 1
                elif len(pts) > 2:
                    stats["curves_piecewise"] += 1
                elif len(pts) == 2 and abs(pts[0][0] - pts[0][1]) < 1e-4 and abs(pts[1][0] - pts[1][1]) < 1e-4:
                    stats["curves_identity"] += 1
                else:
                    stats["curves_linear_stretch"] += 1

    return stats


def validate_archive(rpf_path: Path, spans_path: Path, workers: int = 8) -> Dict[str, Any]:
    t0 = time.time()
    spans = load_deflate_spans(spans_path)
    n = len(spans)
    chunk_sz = max(1, n // workers)
    ranges = []
    cur = 0
    while cur < n:
        nxt = min(n, cur + chunk_sz)
        while nxt < n and spans[nxt][0] == ((spans[nxt - 1][0] + spans[nxt - 1][1] + 15) & ~15):
            nxt += 1
        ranges.append((str(rpf_path), str(spans_path), cur, nxt))
        cur = nxt

    merged: Dict[str, Any] = {
        "archive": rpf_path.name,
        "dicts": 0,
        "clips_ok": 0,
        "clips_err": 0,
        "errors": [],
        "by_variant": collections.Counter(),
        "by_clip_dim": collections.Counter(),
        "by_node_type": collections.Counter(),
        "by_domain": collections.Counter(),
        "by_domain_dim": collections.Counter(),
        "axis_total": 0,
        "axis_resolved": 0,
        "t4_residuals": [],
        "t5_explicit": 0,
        "t5_grid_only": 0,
        "t5_grid_residuals": [],
        "t6_residuals": [],
        "t8_residuals": [],
        "samples_total": 0,
        "samples_valid_coords": 0,
        "curves_total": 0,
        "curves_piecewise": 0,
        "curves_linear_stretch": 0,
        "curves_identity": 0,
        "curves_empty": 0,
        "curves_rate_scaled_samples": 0,
        "json_check_ok": 0,
    }

    if len(ranges) == 1 or workers <= 1:
        parts = [_validate_worker(r) for r in ranges]
    else:
        with concurrent.futures.ProcessPoolExecutor(max_workers=min(workers, len(ranges))) as ex:
            parts = list(ex.map(_validate_worker, ranges))

    for p in parts:
        for k in ("dicts", "clips_ok", "clips_err", "axis_total", "axis_resolved", "t5_explicit", "t5_grid_only",
                  "samples_total", "samples_valid_coords", "curves_total", "curves_piecewise", "curves_linear_stretch",
                  "curves_identity", "curves_empty", "curves_rate_scaled_samples", "json_check_ok"):
            merged[k] += p[k]
        for k in ("by_variant", "by_clip_dim", "by_node_type", "by_domain", "by_domain_dim"):
            merged[k] += p[k]
        for k in ("errors", "t4_residuals", "t5_grid_residuals", "t6_residuals", "t8_residuals"):
            merged[k].extend(p[k])

    merged["elapsed_s"] = round(time.time() - t0, 2)
    return merged


def _print_validation_report(res: Dict[str, Any]) -> None:
    tot = res["clips_ok"] + res["clips_err"]
    pct_ok = 100.0 * res["clips_ok"] / tot if tot else 0.0
    pct_samp = 100.0 * res["samples_valid_coords"] / res["samples_total"] if res["samples_total"] else 0.0
    pct_ax = 100.0 * res["axis_resolved"] / res["axis_total"] if res["axis_total"] else 0.0
    print(f"\n================ {res['archive']} ({res['elapsed_s']:.2f}s) ================")
    print(f"Clip dicts scanned: {res['dicts']:,}")
    print(f"Total .motion clips: {tot:,} | Parsed OK: {res['clips_ok']:,} ({pct_ok:.2f}%) | Errors: {res['clips_err']}")
    print(f"JSON serialization spot-checks passed: {res['json_check_ok']:,}")
    print(f"By variantType (+0x58): {dict(sorted(res['by_variant'].items()))}")
    print(f"By clip max dimensions: {dict(sorted(res['by_clip_dim'].items()))}")
    print(f"By parameter nodeType (all sub-nodes): {dict(sorted(res['by_node_type'].items()))}")
    print(f"Total samples with finite valid coordinates: {res['samples_valid_coords']:,} / {res['samples_total']:,} ({pct_samp:.2f}%)")
    print(f"Axis names resolved: {res['axis_resolved']:,} / {res['axis_total']:,} ({pct_ax:.2f}%)")
    print(f"Type-5 3D nodes: {res['t5_explicit']} explicit +0x20 tets (exact 0.0 err), {res['t5_grid_only']} grid-only")
    for label, key in [("Type-4 (2D)", "t4_residuals"), ("Type-5 grid-only (3D)", "t5_grid_residuals"),
                       ("Type-6 (4D)", "t6_residuals"), ("Type-8 (5D)", "t8_residuals")]:
        vals = res[key]
        if vals:
            a = np.array(vals)
            print(f"  {label:22s} strict-interior max residual (n={len(a):,}): median={np.median(a):.5f}, p95={np.percentile(a,95):.5f}, max={np.max(a):.5f}")
    ct = max(1, res["curves_total"])
    print(
        f"Time-warping curves (+0x70): total={res['curves_total']:,} | "
        f"piecewise(>2pts)={res['curves_piecewise']:,} ({100.0*res['curves_piecewise']/ct:.1f}%) | "
        f"linear_stretch(2pts)={res['curves_linear_stretch']:,} ({100.0*res['curves_linear_stretch']/ct:.1f}%) | "
        f"identity={res['curves_identity']:,} ({100.0*res['curves_identity']/ct:.1f}%) | "
        f"empty={res['curves_empty']:,}"
    )
    print(f"Rate-scaled samples (rate != 1.0): {res['curves_rate_scaled_samples']:,} / {res['samples_total']:,} ({100.0*res['curves_rate_scaled_samples']/max(1,res['samples_total']):.1f}%)")
    print("Domain breakdown (human vs animal vs vehicle vs scripted):")
    for dom, cnt in res["by_domain"].most_common():
        dims = {d: res["by_domain_dim"][(dom, d)] for d in range(6) if res["by_domain_dim"][(dom, d)]}
        print(f"  {dom:16s}: {cnt:6,d} clips  (dims={dims})")
    if res["errors"]:
        print("Errors (first 5):", res["errors"][:5])


def dump_matching(rpf_path: Path, spans_path: Path, query: str, limit: int = 1) -> None:
    spans = load_deflate_spans(spans_path)
    ql = query.lower()
    found = 0
    for _off, joined in iter_clip_dicts_range(rpf_path, spans):
        s = SystemPage(joined)
        try:
            dname = s.string(0x38) or ""
            entries = s.atmap(0x48)
        except Exception:
            continue
        for _key, val in entries:
            if not is_motion_clip(s, val):
                continue
            mname = s.string(val + 0x30) or ""
            full = f"{dname}::{mname}".lower()
            if ql in full:
                parsed = parse_motion_clip(s, val, dname)
                print(json.dumps(parsed, indent=2))
                found += 1
                if found >= limit:
                    return


def main() -> None:
    ap = argparse.ArgumentParser(description=".motion Parametric Blendspace Parser")
    ap.add_argument("--rpf", type=Path, default=DEFAULT_ANIM_RPF)
    ap.add_argument("--spans", type=Path, default=DEFAULT_ANIM_SPANS)
    ap.add_argument("--validate", action="store_true", help="Validate across all .motion clips in anim.rpf + story000.rpf")
    ap.add_argument("--workers", type=int, default=min(12, os.cpu_count() or 4))
    ap.add_argument("--dump", type=str, default=None, help="Dump JSON of .motion clips matching substring")
    ap.add_argument("--limit", type=int, default=1)
    args = ap.parse_args()

    if args.dump:
        dump_matching(args.rpf, args.spans, args.dump, args.limit)
        return

    if args.validate:
        r_anim = validate_archive(DEFAULT_ANIM_RPF, DEFAULT_ANIM_SPANS, workers=args.workers)
        _print_validation_report(r_anim)
        if DEFAULT_STORY_RPF.exists() and DEFAULT_STORY_SPANS.exists():
            r_story = validate_archive(DEFAULT_STORY_RPF, DEFAULT_STORY_SPANS, workers=min(4, args.workers))
            _print_validation_report(r_story)
        return

    ap.print_help()


if __name__ == "__main__":
    main()
