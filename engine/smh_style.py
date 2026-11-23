# SPDX-License-Identifier: GPL-3.0-or-later
"""Strand styling for smh grooms: port of smh_modify_CS_InterpolateStrandStyle.

The game generates strands in smh_gen_*_CS_InterpolateStrands, then this pass re-samples every strand along
its arc length and offsets each new point in the strand's local frame by a per-groom *style profile*. The
profile is a 2D curve stored in the asset (header word +0x20, the block that smh_groom.py calls "tail"). For
curly items it is a helix of up to about 2 cm radius (hairafro: about 1 cm, 3 to 9 turns), for straight
items a sub-millimetre to few-millimetre wobble.

    import smh_groom, smh_style
    g = smh_groom.load("packs.rpf", 0x105202000)
    s = smh_groom.strands(g, max_strands=4000)
    out = smh_style.apply_style(g, s)          # out["points"] [n, P_out, 3]

Exact (read from the shader and checked against the data):
  profile record layout and decoding, point count K (system page +0x130 byte 0) and number of profile sets
  (+0x130 byte 1), arc-length re-sampling with the f16 strand length, binary search, linear interpolation of
  position, central-difference tangent, Gram-Schmidt of the frame vector against the tangent, the binormal
  T x N, the flip of the first axis, the offset scaling 0.8 mm + (r - 0.8 mm) * amount, amount =
  min(1 - weight/3, instance amount).

Not in the asset (run-time inputs, each with a flagged default you can override):
  points_out   instance +0x54 bits 24-31, the re-sampled point count. Default: K (assumed)
  amount       instance +0x148 byte 0 / 255. Default 1.0 (assumed: full style)
  set_index    root constant that picks one of the asset's profile sets. Default 0 (assumed)
  frame        per-point frame vector N written by smh_gen (14-bit octahedral, bits 16-29 of point dword 3).
               Default: a rotation-minimising frame built here (APPROXIMATION, see frame_mode)
  flip         per-strand bit 4 of point 0 dword 3, a random bit from smh_gen. Default False (assumed)
  weight_bits  per-point 2-bit value in bits 30-31 of dword 3 (smh_gen: 3 * blended pose-record value + 0.5;
               the pose value is computed at run time by smh_pose UpdatePosePosition, rest value unknown).
               Default 0 (assumed). With weight 3 or amount 0 every curl shrinks to the 0.8 mm floor, so
               the defaults give the most curl the profile allows, an upper bound
frame, flip and weight_bits are also taken from the strands dict (keys "frame", "flip", "weight_bits")
when the strand generator supplies them.
"""
from __future__ import annotations

import struct

import numpy as np

MIN_OFFSET = np.float32(0.0008)       # 0x3a51b717: offsets up to 0.8 mm are never scaled down
CULL_LENGTH = np.float32(1e-4)        # 0x38d1b717: strands this short are culled
TWO_PI_255 = np.float32(2.0 * np.pi / 255.0)


def profile_info(groom):
    """The asset's style profile, or None. Returns dict K, sets, records [sets] of (ranges[4], xyz[K,3] int)."""
    h = struct.unpack_from("<64I", groom.buffer)
    start = h[0x20 // 4]
    if not start:
        return None
    word = struct.unpack_from("<I", groom.system, 0x130)[0] if len(groom.system) >= 0x134 else 0
    k, sets = word & 0xFF, (word >> 8) & 0xFF
    if not k:
        return None
    sets = max(sets, 1)
    stride = 8 + 4 * k                 # InterpolateStrandStyle: (instance +0x54 bits 8-15) * 4 + 8
    records = []
    for i in range(sets):
        at = start + i * stride
        if at + stride > len(groom.buffer):
            break
        rng = np.frombuffer(groom.buffer, "<f2", 4, at).astype(np.float32)      # xmin, ymin, xmax, ymax (m)
        d = np.frombuffer(groom.buffer, "<u4", k, at + 8).astype(np.int64)
        xyz = np.stack((d & 0x3FF, (d >> 10) & 0x3FF, d >> 20), axis=1)        # 10, 10, 12 bits
        records.append((rng, xyz))
    return {"K": k, "sets": sets, "stride": stride, "offset": start, "records": records}


def profile_curve(groom, set_index=0, samples=None):
    """Decoded profile in metres: x, y offsets and z = arc fraction, at the K control points (or resampled)."""
    info = profile_info(groom)
    if info is None:
        return None
    rng, xyz = info["records"][set_index]
    x = rng[0] + (rng[2] - rng[0]) * np.float32(1 / 1023) * xyz[:, 0]
    y = rng[1] + (rng[3] - rng[1]) * np.float32(1 / 1023) * xyz[:, 1]
    z = xyz[:, 2] / np.float32(4095)
    return np.stack((x, y, z), axis=1).astype(np.float32)


def _eval_profile(rng, xyz, points_out):
    """Profile at output point j (pc 604-880): t = j/(P-1), linear between control points."""
    k = len(xyz)
    j = np.arange(points_out, dtype=np.float32)
    if k >= 2:
        t = np.clip(j / np.float32(max(points_out - 1, 1)), 0, 1)
        seg = np.minimum(k - 2, (t * (k - 1)).astype(np.int64))
        fr = np.clip(t * (k - 1) - seg, 0, 1)
        a, b = xyz[seg].astype(np.float32), xyz[seg + 1].astype(np.float32)
        v = a + (b - a) * fr[:, None]
    else:
        v = np.repeat(xyz[:1].astype(np.float32), points_out, axis=0)
    ox = rng[0] + (rng[2] - rng[0]) * np.float32(1 / 1023) * v[:, 0]
    oy = rng[1] + (rng[3] - rng[1]) * np.float32(1 / 1023) * v[:, 1]
    zf = v[:, 2] * np.float32(1 / 4095)
    return ox, oy, zf


def _unit(v, eps=1e-20):
    return v / np.sqrt(np.maximum((v * v).sum(-1, keepdims=True), eps))


def _tangents(p):
    """Per-point direction p[k+1] - p[k-1] with clamped indices, as the style shader builds it."""
    n = p.shape[1]
    nxt = p[:, np.minimum(np.arange(n) + 1, n - 1)]
    prv = p[:, np.maximum(np.arange(n) - 1, 0)]
    return _unit(nxt - prv)


def default_frame(groom, s, frame_mode="transport", twist=True):
    """APPROXIMATE per-point frame vector N [n, P, 3] standing in for the one smh_gen writes.

    What smh_gen does (read, not ported here; the gen port owns it): root vector from the optional per-guide
    3-byte array at header +0xbc (used when header +0xa8 == 3; 2*b/255-1, blended over the three guides),
    skinned, then carried along the strand and rotated about the tangent by an angle interpolated from the
    four bytes of ext_24 (header +0x24, angle = b*2pi/255 - pi) at arc fraction 0, 1/3, 2/3, 1.
    Here: the same root vector when the asset has +0xbc (else the projection of world Z, or X), carried by
    projection (a rotation-minimising frame), and the ext_24 twist applied with a right-handed sign (the sign
    and the carrying method are not verified).
    """
    p = s["points"].astype(np.float32)
    n, k, _ = p.shape
    T = _tangents(p)
    h = struct.unpack_from("<64I", groom.buffer)
    idx = groom.triangle[s["tri"]].astype(np.int64)
    bary = s["bary"].astype(np.float32)
    n0 = None
    if h[0xA8 // 4] == 3 and h[0xBC // 4]:
        raw = np.frombuffer(groom.buffer, "u1", groom.guides * 4, h[0xBC // 4]).reshape(-1, 4)[:, :3]
        val = (raw[idx].astype(np.float32) * bary[:, :, None]).sum(1) / np.float32(255)
        n0 = val * 2 - 1
    if n0 is None:
        up = np.where(np.abs(T[:, 0, 2:3]) < 0.95, np.array([0, 0, 1], np.float32), np.array([1, 0, 0], np.float32))
        n0 = up
    N = np.empty_like(p)
    cur = _unit(n0 - (n0 * T[:, 0]).sum(-1, keepdims=True) * T[:, 0])
    N[:, 0] = cur
    for i in range(1, k):
        t = T[:, i]
        nxt = cur - (cur * t).sum(-1, keepdims=True) * t
        bad = (nxt * nxt).sum(-1) < 1e-12
        nxt[bad] = cur[bad]
        cur = _unit(nxt)
        N[:, i] = cur
    if twist and groom.present.get("ext_24"):
        ang = (groom.arrays["ext_24"][idx].astype(np.float32) * TWO_PI_255 - np.float32(np.pi))   # [n,3,4]
        ang = (ang * bary[:, :, None]).sum(1)                                                       # [n,4]
        seg = np.linalg.norm(np.diff(p, axis=1), axis=2)
        cum = np.concatenate((np.zeros((n, 1), np.float32), np.cumsum(seg, 1)), 1)
        u = np.clip(cum / np.maximum(cum[:, -1:], 1e-9), 0, 1) * 3
        lo = np.minimum(u.astype(np.int64), 3)
        hi = np.minimum(lo + 1, 3)
        a = np.take_along_axis(ang, lo, 1) + (np.take_along_axis(ang, hi, 1) - np.take_along_axis(ang, lo, 1)) * (u - lo)
        c, sn = np.cos(a)[..., None], np.sin(a)[..., None]
        N = c * N + sn * np.cross(T, N)
    return N.astype(np.float32)


def apply_style(groom, s, strand_class=0, points_out=None, amount=1.0, set_index=0, frame=None, flip=None,
                weight_bits=None, frame_mode="transport", twist=True, enable=True):
    """Styled strands from smh_groom.strands() output.

    strand_class  accepted for symmetry; the style pass treats both classes alike (one thread per point of
                  the class 1 + class 0 strands, same profile).
    Returns dict: points [n, P_out, 3], length [n] source length (f16 rounded), target [n, P_out] arc
    position sampled for each output point, culled [n], profile (K, sets, set), assumed [list of the
    defaults that were used].
    """
    p = np.asarray(s["points"], np.float32)
    n, pin, _ = p.shape
    # Gen-side per-point fields, when the strand generator provides them (exact inputs):
    #   s["frame"] [n, P_in, 3] frame vector N, s["flip"] [n] bool, s["weight_bits"] [n, P_in] 0..3.
    frame = s.get("frame") if frame is None else frame
    flip = s.get("flip") if flip is None else flip
    weight_bits = s.get("weight_bits") if weight_bits is None else weight_bits
    info = profile_info(groom)
    assumed = []
    if info is None or not enable:
        return {"points": p.copy(), "length": None, "target": None, "culled": np.zeros(n, bool),
                "profile": None, "assumed": ["no style profile in this asset" if info is None else "style disabled"]}
    if points_out is None:
        points_out = info["K"]
        assumed.append("points_out = K")
    if amount == 1.0:
        assumed.append("instance amount (+0x148) = 1.0")
    if set_index >= len(info["records"]):
        raise ValueError(f"set {set_index} not in the asset ({len(info['records'])} sets)")
    if info["sets"] > 1 and set_index == 0:
        assumed.append("profile set 0 of %d" % info["sets"])
    rng, xyz = info["records"][set_index]

    # Source arc length per point, as smh_gen stores it: f32 running sum, written as f16 (point 0 slot holds
    # flags, so its length reads as 0).
    seg = np.linalg.norm(np.diff(p, axis=1), axis=2)
    cum = np.concatenate((np.zeros((n, 1), np.float32), np.cumsum(seg, 1, dtype=np.float32)), 1)
    L = cum.astype(np.float16).astype(np.float32)
    L[:, 0] = 0
    total = L[:, -1]
    culled = ~(total > CULL_LENGTH) if pin > 1 else np.ones(n, bool)

    ox, oy, zf = _eval_profile(rng, xyz, points_out)
    target = (total * np.float32(1 / 4095))[:, None] * (zf * 4095)[None, :]        # v6 = L/4095 * z

    # Binary search: largest i with L[i] < target, clamped to [0, P_in-1] (pc 884-1032).
    i = np.clip((L[:, None, :] < target[:, :, None]).sum(2) - 1, 0, pin - 1)
    ip, im = np.minimum(i + 1, pin - 1), np.maximum(i - 1, 0)
    take = lambda a, j: np.take_along_axis(a, j, 1)
    Li, Lp = take(L, i), take(L, ip)
    den = Lp - Li
    with np.errstate(divide="ignore", invalid="ignore"):
        fr = np.where(den > 0, (target - Li) / den, 0.0)
    fr = np.clip(np.nan_to_num(fr), 0, 1).astype(np.float32)[..., None]
    gi = lambda a, j: np.take_along_axis(a, j[..., None].repeat(a.shape[-1], -1), 1)
    P = gi(p, i) + (gi(p, ip) - gi(p, i)) * fr
    T = _unit(gi(p, ip) - gi(p, im))

    if frame is None:
        frame = default_frame(groom, s, frame_mode, twist)
        assumed.append("frame approximated (not smh_gen's)")
    frame = np.asarray(frame, np.float32)
    Ni, Np = _unit(gi(frame, i)), _unit(gi(frame, ip))
    N = Ni + (Np - Ni) * fr
    N = _unit(N)
    N = _unit(N - (N * T).sum(-1, keepdims=True) * T)
    B = np.cross(T, N)

    if weight_bits is None:
        wb = np.zeros((n, points_out), np.float32)
        assumed.append("weight bits = 0")
    else:
        wb = np.asarray(weight_bits, np.float32)
        wb = take(wb, i) + (take(wb, ip) - take(wb, i)) * fr[..., 0]
    w = np.minimum(1 - wb / 3, np.float32(amount))

    r = np.hypot(ox, oy)[None, :]
    with np.errstate(divide="ignore", invalid="ignore"):
        scale = np.clip(np.where(r > 0, (MIN_OFFSET + (r - MIN_OFFSET) * w) / r, 1.0), 0, 1)
    sx, sy = (ox[None, :] * scale)[..., None], (oy[None, :] * scale)[..., None]
    if flip is None:
        sign = np.ones((n, 1, 1), np.float32)
        assumed.append("flip bit = 0")
    else:
        sign = np.where(np.asarray(flip, bool), -1.0, 1.0).astype(np.float32)[:, None, None]
    out = P + sx * sign * N + sy * B
    return {"points": out.astype(np.float32), "length": total, "target": target, "culled": culled,
            "profile": {"K": info["K"], "sets": info["sets"], "set": set_index,
                        "radius_max_m": float(r.max()) if r.size else 0.0},
            "assumed": assumed}
