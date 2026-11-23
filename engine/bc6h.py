# SPDX-License-Identifier: GPL-3.0-or-later
"""BC6H block decoder (numpy): half-float RGB from 16-byte blocks, unsigned (UF16) and signed (SF16).

Pillow decodes BC6H only to clamped 8-bit, which turns HDR data black or white. This decoder returns the
real values so a viewer can pick an exposure. Its output is checked against Pillow wherever the values lie
inside 0..1 (python bc6h.py <pack.ttpf>).

Layout of every mode follows the format's published bit tables: two-bit modes 00 and 01, five-bit modes
00010 .. 11110 (two regions) and 00011, 00111, 01011, 01111 (one region)."""

import json
import os

import numpy as np

# Field order per mode, least significant stream bit first, after the mode bits.
# "rw10" = red of endpoint w, 10 bits (bit 0 first); "gy4" = bit 4 of green of endpoint y; "rw:10-11" = bits
# stored high bit first (11 then 10); "d" = the five partition bits.
_LAYOUT = {
    0x00: "gy4 by4 bz4 rw10 gw10 bw10 rx5 gz4 gy4_ gx5 bz0 gz4_ bx5 bz1 by4_ ry5 bz2 rz5 bz3 d",
    0x01: "gy5 gz4 gz5 rw7 bz0 bz1 by4 gw7 by5 bz2 gy4 bw7 bz3 bz5 bz4 rx6 gy4_ gx6 gz4_ bx6 by4_ ry6 rz6 d",
    0x02: "rw10 gw10 bw10 rx5 rw:10 gy4_ gx4 gw:10 bz0 gz4_ bx4 bw:10 bz1 by4_ ry5 bz2 rz5 bz3 d",
    0x06: "rw10 gw10 bw10 rx4 rw:10 gz4 gy4_ gx5 gw:10 gz4_ bx4 bw:10 bz1 by4_ ry4 bz0 bz2 rz4 gy4 bz3 d",
    0x0A: "rw10 gw10 bw10 rx4 rw:10 by4 gy4_ gx4 gw:10 bz0 gz4_ bx5 bw:10 by4_ ry4 bz1 bz2 rz4 bz4 bz3 d",
    0x0E: "rw9 by4 gw9 gy4 bw9 bz4 rx5 gz4 gy4_ gx5 bz0 gz4_ bx5 bz1 by4_ ry5 bz2 rz5 bz3 d",
    0x12: "rw8 gz4 by4 gw8 bz2 gy4 bw8 bz3 bz4 rx6 gy4_ gx5 bz0 gz4_ bx5 bz1 by4_ ry6 rz6 d",
    0x16: "rw8 bz0 by4 gw8 gy5 gy4 bw8 gz5 bz4 rx5 gz4 gy4_ gx6 gz4_ bx5 bz1 by4_ ry5 bz2 rz5 bz3 d",
    0x1A: "rw8 bz1 by4 gw8 by5 gy4 bw8 bz5 bz4 rx5 gz4 gy4_ gx5 bz0 gz4_ bx6 by4_ ry5 bz2 rz5 bz3 d",
    0x1E: "rw6 gz4 bz0 bz1 by4 gw6 gy5 by5 bz2 gy4 bw6 gz5 bz3 bz5 bz4 rx6 gy4_ gx6 gz4_ bx6 by4_ ry6 rz6 d",
    0x03: "rw10 gw10 bw10 rx10 gx10 bx10",
    0x07: "rw10 gw10 bw10 rx9 rw:10 gx9 gw:10 bx9 bw:10",
    0x0B: "rw10 gw10 bw10 rx8 rw:11-10 gx8 gw:11-10 bx8 bw:11-10",
    0x0F: "rw10 gw10 bw10 rx4 rw:15-10 gx4 gw:15-10 bx4 bw:15-10",
}
# mode -> (transformed, base bits, delta bits r, g, b, regions)
_INFO = {
    0x00: (1, 10, 5, 5, 5, 2), 0x01: (1, 7, 6, 6, 6, 2), 0x02: (1, 11, 5, 4, 4, 2), 0x06: (1, 11, 4, 5, 4, 2),
    0x0A: (1, 11, 4, 4, 5, 2), 0x0E: (1, 9, 5, 5, 5, 2), 0x12: (1, 8, 6, 5, 5, 2), 0x16: (1, 8, 5, 6, 5, 2),
    0x1A: (1, 8, 5, 5, 6, 2), 0x1E: (0, 6, 6, 6, 6, 2), 0x03: (0, 10, 10, 10, 10, 1), 0x07: (1, 11, 9, 9, 9, 1),
    0x0B: (1, 12, 8, 8, 8, 1), 0x0F: (1, 16, 4, 4, 4, 1),
}
_W3 = np.array([0, 9, 18, 27, 37, 46, 55, 64], np.int64)
_W4 = np.array([0, 4, 9, 13, 17, 21, 26, 30, 34, 38, 43, 47, 51, 55, 60, 64], np.int64)
_tables = None


def _partitions():
    """Two-region partition masks [32][16] and second anchor positions [32]: the first 32 BC7 two-subset shapes."""
    global _tables
    if _tables is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bcpack_data", "bc7.json")
        with open(path, encoding="utf-8") as f:
            t = json.load(f)
        _tables = (np.array(t["partition2"][:32], np.int64), np.array(t["anchor2"][:32], np.int64))
    return _tables


def _plan(mode):
    """List of (field, bit of the field, stream position) for the header of a mode, and the header length."""
    pos = 2 if mode < 2 else 5
    out = []
    for token in _LAYOUT[mode].split():
        if token == "d":
            out += [("d", b, pos + b) for b in range(5)]
            pos += 5
        elif ":" in token:
            name, span = token.split(":")
            hi, lo = (int(x) for x in span.split("-")) if "-" in span else (int(span), int(span))
            for b in range(hi, lo - 1, -1):          # stored from the high bit down
                out.append((name, b, pos))
                pos += 1
        elif token.endswith("_"):                     # "gy4_": the low n bits of the field
            name, n = token[:2], int(token[2:-1])
            out += [(name, b, pos + b) for b in range(n)]
            pos += n
        else:
            name, n = token[:2], int(token[2:])
            if name[1] in "wx" or name in ("ry", "rz"):
                out += [(name, b, pos + b) for b in range(n)]     # a whole field of n bits
                pos += n
            else:                                     # "gy4", "bz0": a single bit of the field
                out.append((name, n, pos))
                pos += 1
    return out, pos


_PLANS = {}


def decode(data, width, height, signed=False):
    """Half-float RGB [height, width, 3] (float32) from BC6H blocks stored row by row."""
    bw, bh = (width + 3) // 4, (height + 3) // 4
    raw = np.frombuffer(data, np.uint8, bw * bh * 16).reshape(-1, 16)
    lo = raw[:, :8].copy().view("<u8")[:, 0]
    hi = raw[:, 8:].copy().view("<u8")[:, 0]

    def bit(pos, rows):
        if pos < 64:
            return ((lo[rows] >> np.uint64(pos)) & np.uint64(1)).astype(np.int64)
        return ((hi[rows] >> np.uint64(pos - 64)) & np.uint64(1)).astype(np.int64)

    def bits(pos, count, rows):
        value = np.zeros(len(rows), np.int64)
        for k in range(count):
            value |= bit(pos + k, rows) << k
        return value

    first = raw[:, 0].astype(np.int64)
    modes = np.where((first & 3) < 2, first & 3, first & 31)
    out = np.zeros((len(raw), 16, 3), np.int64)
    parts, anchors = _partitions()
    for mode in np.unique(modes):
        rows = np.nonzero(modes == mode)[0]
        mode = int(mode)
        if mode not in _INFO:                       # reserved modes decode to zero
            continue
        if mode not in _PLANS:
            _PLANS[mode] = _plan(mode)
        plan, header = _PLANS[mode]
        transformed, base, dr, dg, db, regions = _INFO[mode]
        field = {}
        for name, b, pos in plan:
            field[name] = field.get(name, 0) | (bit(pos, rows) << b)
        zero = np.zeros(len(rows), np.int64)
        delta = {"r": dr, "g": dg, "b": db}
        ends = np.zeros((len(rows), 4, 3), np.int64)             # endpoints w, x, y, z
        mask = (1 << base) - 1
        for c, ch in enumerate("rgb"):
            w = field.get(ch + "w", zero)
            if signed:
                w = np.where(w >> (base - 1), w - (1 << base), w)
            ends[:, 0, c] = w
            for e, name in enumerate("xyz", 1):
                v = field.get(ch + name, zero)
                if e > 1 and regions == 1:
                    continue
                if transformed:
                    v = np.where(v >> (delta[ch] - 1), v - (1 << delta[ch]), v)
                    v = (v + w) & mask
                    if signed:
                        v = np.where(v >> (base - 1), v - (1 << base), v)
                elif signed:
                    v = np.where(v >> (delta[ch] - 1), v - (1 << delta[ch]), v)
                ends[:, e, c] = v
        # unquantize
        if signed:
            if base < 16:
                sign = ends < 0
                a = np.abs(ends)
                u = np.where(a == 0, 0, np.where(a >= (1 << (base - 1)) - 1, 0x7FFF, ((a << 15) + 0x4000) >> (base - 1)))
                ends = np.where(sign, -u, u)
        elif base < 15:
            ends = np.where(ends == 0, 0, np.where(ends == mask, 0xFFFF, ((ends << 15) + 0x4000) >> (base - 1)))
        # indices
        if regions == 2:
            shape = field["d"]
            region = parts[shape]                                 # [n, 16]
            second = anchors[shape]
            width_bits, weights = 3, _W3
        else:
            region = np.zeros((len(rows), 16), np.int64)
            second = np.full(len(rows), -1, np.int64)
            width_bits, weights = 4, _W4
        index = np.zeros((len(rows), 16), np.int64)
        pos = np.full(len(rows), header, np.int64)
        for p in range(16):
            n = np.where((p == 0) | (second == p), width_bits - 1, width_bits)
            value = np.zeros(len(rows), np.int64)
            for k in range(width_bits):
                at = pos + k
                low = (lo[rows] >> np.minimum(at, 63).astype(np.uint64)) & np.uint64(1)
                high = (hi[rows] >> np.clip(at - 64, 0, 63).astype(np.uint64)) & np.uint64(1)
                b = np.where(at < 64, low, high).astype(np.int64)
                value |= np.where(k < n, b, 0) << k
            index[:, p] = value
            pos += n
        w = weights[index]                                         # [n, 16]
        e0 = np.take_along_axis(ends, (region * 2)[:, :, None], axis=1)        # [n, 16, 3]
        e1 = np.take_along_axis(ends, (region * 2 + 1)[:, :, None], axis=1)
        out[rows] = (e0 * (64 - w[:, :, None]) + e1 * w[:, :, None] + 32) >> 6
    # to half-float bit patterns
    if signed:
        mag = (np.abs(out) * 31) >> 5
        half = np.where(out < 0, mag | 0x8000, mag).astype(np.uint16)
    else:
        half = ((out * 31) >> 6).astype(np.uint16)
    values = half.view(np.float16).astype(np.float32).reshape(bh, bw, 4, 4, 3)
    return np.ascontiguousarray(values.transpose(0, 2, 1, 3, 4).reshape(bh * 4, bw * 4, 3)[:height, :width])


def exposure_for(values):
    """A display scale for HDR pixels: 1.0 when they already fill 0..1, otherwise the scale that brings the
    99.9th percentile of the brightest channel to 1."""
    finite = np.nan_to_num(values, nan=0.0, posinf=65504.0, neginf=0.0)
    top = float(np.percentile(finite.max(axis=2), 99.9)) if finite.size else 0.0
    if top <= 0:
        return 1.0
    if 0.25 <= top <= 1.0:
        return 1.0
    return 1.0 / top


def to_image(values, exposure=1.0):
    """8-bit RGB picture: values times exposure, clipped to 0..1 (no tone curve)."""
    from PIL import Image

    finite = np.nan_to_num(values, nan=0.0, posinf=65504.0, neginf=0.0)
    return Image.fromarray((np.clip(finite * exposure, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8), "RGB")


if __name__ == "__main__":
    import sys
    from PIL import Image

    from ttpf_format import TtpfArchive

    arc = TtpfArchive(sys.argv[1])
    blocks = worst = compared = 0
    exact = 0
    for t in arc.textures:
        if t["dxgi"] not in (95, 96) or not t["mips"]:
            continue
        for m in t["mips"]:
            if m["width"] < 4 or m["height"] < 4:
                continue
            try:
                linear = arc.mip_linear(t, m["level"])
            except (LookupError, ValueError):
                continue
            signed = t["dxgi"] == 96
            mine = decode(linear, m["width"], m["height"], signed)
            ref = np.asarray(Image.frombytes("RGB", (m["width"], m["height"]), linear, "bcn", (6, "BC6HS" if signed else "BC6H")), np.int64)
            inside = (mine >= 0).all(axis=2) & (mine <= 1).all(axis=2)
            got = (np.clip(mine, 0, 1) * 255.0).astype(np.int64)
            d = np.abs(got - ref)[inside]
            if d.size:
                worst = max(worst, int(d.max()))
                exact += int((d.max(axis=1) == 0).sum())
                compared += int(inside.sum())
            blocks += len(linear) // 16
    print(f"{blocks:,} blocks; {compared:,} pixels inside 0..1 compared with Pillow: {exact:,} equal, largest difference {worst}")
