# SPDX-License-Identifier: GPL-3.0-or-later
"""BC6H block rebuild for bcpack.py (numpy). Clean-room; the research notes behind it are not part of this repository.

Lanes 4 4 8 8 8 5 5 5 6 6 6 8 8 8 8 5, two elements A and B per block:
    lane 15       A = mode number - 1 (0..13, the format's 14 modes in their published order), B = partition
    lanes 0, 1    reference distances as in BC7: d_i = lane 1, d_e = (d_i + unzig(lane 0)) mod 256; values split over
                  the elements with the even bits in A and the odd bits in B
    lanes 2-4     base endpoint w of r, g, b (16 bits over A and B); lanes 5-7 field x (10 bits); lanes 8-10 field y in
                  A and field z in B (6 bits each): zig-zag differences from the referenced block's state
    lanes 11-14   indices, one pixel row per lane, A = pixels x 0-1, B = x 2-3; a symbol interleaves two zig-zag
                  differences (first pixel in the odd bits; in row 3 the second)

State a block leaves, raw bits left-aligned: W 16 bits, X 10, Y 6, Z 6 per channel; indices in 4 bits per pixel. A block
predicts each field with a right shift to the field's width and adds the difference modulo the width. Without a
reference every field starts in the middle of its scale (W 0x8000, X 0x200, Y and Z 0x20, indices 8). A one-region
block leaves Z = the top six bits of x and Y = the top six bits of w cut to the delta width, offset by half that range
in transformed modes. In modes 13 and 14 BCPack numbers the high bits of w (10-11, 10-15) in the order the block
stores them, i.e. reversed.

References before the first block read a built-in history of 256 endpoint states and 256 index states
(bcpack_data/bc6h.json), fitted against neighbour mips of the props and vfx packs; none is pinned exactly."""

import json
import os

import numpy as np

import bc6h

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bcpack_data")
CODE = {1: 0x00, 2: 0x01, 3: 0x02, 4: 0x06, 5: 0x0A, 6: 0x0E, 7: 0x12, 8: 0x16, 9: 0x1A, 10: 0x1E,
        11: 0x03, 12: 0x07, 13: 0x0B, 14: 0x0F}
NONE = np.array([0x8000, 0x200, 0x20, 0x20], np.int64)
TRANS = np.zeros(15, np.int64)
BASE = np.zeros(15, np.int64)
DELTA = np.zeros((15, 3), np.int64)
REGIONS = np.ones(15, np.int64)
for _m, _c in CODE.items():
    _t, _b, _dr, _dg, _db, _r = bc6h._INFO[_c]
    TRANS[_m], BASE[_m], DELTA[_m], REGIONS[_m] = _t, _b, (_dr, _dg, _db), _r
_table = None


def load_table():
    """(E [256, 3, 4], I [256, 16], endpoint entry class [256], index entry class [256]); class 0 = no value,
    1 = fitted against reliable references, 2 = last resort (only separately filtered mips as reference)."""
    global _table
    if _table is None:
        path = os.path.join(DATA, "bc6h.json")
        if not os.path.exists(path):
            raise ValueError("BC6H rebuild data (bcpack_data/bc6h.json) is not present")
        with open(path) as f:
            raw = json.load(f)
        _table = (np.array(raw["endpointHistory"], np.int64), np.array(raw["indexHistory"], np.int64),
                  np.array(raw["endpointClass"], np.int64), np.array(raw["indexClass"], np.int64))
    return _table


def _unzig(v):
    return np.where(v & 1, -((v + 1) >> 1), v >> 1)


def _inter(a, b, n):
    d = np.zeros_like(a)
    for k in range(n):
        d |= ((a >> k) & 1) << (2 * k) | ((b >> k) & 1) << (2 * k + 1)
    return d


def _deint(s, odd):
    v = np.zeros_like(s)
    for k in range(4):
        v |= ((s >> (2 * k + odd)) & 1) << k
    return v


def _stream_w(mode, w):
    """BCPack's base endpoint -> the format's: mode 13 swaps bits 10 and 11, mode 14 reverses bits 10..15."""
    low = w & 0x3FF
    m13 = low | (((w >> 10) & 1) << 11) | (((w >> 11) & 1) << 10)
    m14 = low.copy()
    for b in range(10, 16):
        m14 |= ((w >> b) & 1) << (25 - b)
    return np.where(mode == 13, m13, np.where(mode == 14, m14, w))


def _chain(d):
    n = len(d)
    root = np.zeros(n, np.int64)
    level = np.zeros(n, np.int64)
    dl = d.tolist()
    rl, ll = [0] * n, [0] * n
    for i in range(n):
        v = dl[i]
        if v == 0:
            rl[i] = -1
        elif v > i:
            rl[i] = 256 + i - v
        else:
            rl[i] = rl[i - v]
            ll[i] = ll[i - v] + 1
    root[:] = rl
    level[:] = ll
    return root, level


def _levels(level):
    order = np.argsort(level, kind="stable")
    cuts = np.nonzero(np.diff(level[order]))[0] + 1
    return np.split(order, cuts)


def rebuild(lanes, nblocks):
    """BC6H blocks from the sixteen lanes. Returns (bytes, info): info has the endpoint and index history entries the
    chunk reads with the number of blocks that hang on each, and the number of anchor pixels whose index came out
    with its top bit set (a sign of a wrong index entry)."""
    E, I, _, _ = load_table()
    S = np.array(lanes, np.int64)
    A, B = S[:, 0::2], S[:, 1::2]
    n = nblocks
    if A.shape[1] != n:
        raise ValueError("BC6H lane length does not match the block count")
    if (A[15] > 13).any():
        raise ValueError("BC6H mode symbol outside 0..13")
    mode = A[15] + 1
    part = np.minimum(B[15], 31)
    reg = REGIONS[mode]
    base = BASE[mode][:, None]
    D = DELTA[mode]
    trans = TRANS[mode][:, None]
    two = (reg == 2)[:, None]
    di = _inter(A[1], B[1], 4)
    de = (di + _unzig(_inter(A[0], B[0], 4))) & 255
    dz = np.zeros((n, 3, 4), np.int64)
    for c in range(3):
        dz[:, c, 0] = _unzig(_inter(A[2 + c], B[2 + c], 8))
        dz[:, c, 1] = _unzig(_inter(A[5 + c], B[5 + c], 5))
        dz[:, c, 2] = _unzig(A[8 + c])
        dz[:, c, 3] = _unzig(B[8 + c])
    eroot, elevel = _chain(de)
    iroot, ilevel = _chain(di)

    # endpoint fields, level by level along the reference chains
    F = np.zeros((n, 3, 4), np.int64)
    ST = np.zeros((n, 3, 4), np.int64)
    mw = (1 << base) - 1
    mD = (1 << D) - 1
    sh = np.maximum(6 - D, 0)
    for i in _levels(elevel):
        d = de[i]
        first = (d == 0) | (d > i)
        ref = np.where(first[:, None, None],
                       np.where((d == 0)[:, None, None], NONE[None, None, :], E[np.clip(eroot[i], 0, 255)]),
                       ST[np.where(first, 0, i - d)])
        w = ((ref[:, :, 0] >> (16 - base[i])) + dz[i, :, 0]) & mw[i]
        x = ((ref[:, :, 1] >> (10 - D[i])) + dz[i, :, 1]) & mD[i]
        y = ((ref[:, :, 2] >> sh[i]) + dz[i, :, 2]) & mD[i]
        z = ((ref[:, :, 3] >> sh[i]) + dz[i, :, 3]) & mD[i]
        x10 = x << (10 - D[i])
        v = (w & mD[i]) ^ np.where(trans[i] == 1, 1 << (D[i] - 1), 0)
        low_y = np.where(D[i] >= 6, v >> np.maximum(D[i] - 6, 0), v << sh[i])
        ST[i, :, 0] = w << (16 - base[i])
        ST[i, :, 1] = x10
        ST[i, :, 2] = np.where(two[i], y << sh[i], low_y)
        ST[i, :, 3] = np.where(two[i], z << sh[i], x10 >> 4)
        F[i, :, 0] = w
        F[i, :, 1] = x
        F[i, :, 2] = np.where(two[i], y, 0)
        F[i, :, 3] = np.where(two[i], z, 0)

    # indices
    ibits = np.where(reg == 2, 3, 4)[:, None]
    idiff = np.zeros((n, 16), np.int64)
    for p in range(16):
        yy, xx = p >> 2, p & 3
        first = (xx & 1) == 0
        odd = int(first) if yy < 3 else int(not first)
        idiff[:, p] = _unzig(_deint((A if xx < 2 else B)[11 + yy], odd))
    IX = np.zeros((n, 16), np.int64)
    SI = np.zeros((n, 16), np.int64)
    for i in _levels(ilevel):
        d = di[i]
        first = (d == 0) | (d > i)
        ref = np.where(first[:, None], np.where((d == 0)[:, None], 8, I[np.clip(iroot[i], 0, 255)]),
                       SI[np.where(first, 0, i - d)])
        IX[i] = ((ref >> (4 - ibits[i])) + idiff[i]) & ((1 << ibits[i]) - 1)
        SI[i] = IX[i] << (4 - ibits[i])

    # blocks
    F[:, :, 0] = _stream_w(mode[:, None], F[:, :, 0])
    lo = np.zeros(n, np.uint64)
    hi = np.zeros(n, np.uint64)
    _, anchors = bc6h._partitions()
    second = np.where(reg == 2, anchors[part], -1)
    top = np.where(reg == 2, 4, 8)
    violations = int(((IX[:, 0] & top) != 0).sum() + ((reg == 2) & ((IX[np.arange(n), np.maximum(second, 0)] & 4) != 0)).sum())
    names = {"rw": (0, 0), "gw": (1, 0), "bw": (2, 0), "rx": (0, 1), "gx": (1, 1), "bx": (2, 1),
             "ry": (0, 2), "gy": (1, 2), "by": (2, 2), "rz": (0, 3), "gz": (1, 3), "bz": (2, 3)}
    for m in np.unique(mode):
        rows = np.nonzero(mode == m)[0]
        code = CODE[int(m)]
        plan, header = bc6h._plan(code)
        l = np.full(len(rows), code, np.uint64)
        h = np.zeros(len(rows), np.uint64)
        for name, b, pos in plan:
            if name == "d":
                bit = ((part[rows] >> b) & 1).astype(np.uint64)
            else:
                c, q = names[name]
                bit = ((F[rows, c, q] >> b) & 1).astype(np.uint64)
            if pos < 64:
                l |= bit << np.uint64(pos)
            else:
                h |= bit << np.uint64(pos - 64)
        wb = 3 if REGIONS[int(m)] == 2 else 4
        at = np.full(len(rows), header, np.int64)
        for p in range(16):
            nb = np.where((p == 0) | (second[rows] == p), wb - 1, wb)
            val = IX[rows, p] & ((1 << nb) - 1)
            for k in range(wb):
                bit = np.where(k < nb, (val >> k) & 1, 0).astype(np.uint64)
                pos = at + k
                l |= np.where(pos < 64, bit << np.minimum(pos, 63).astype(np.uint64), np.uint64(0))
                h |= np.where(pos >= 64, bit << np.clip(pos - 64, 0, 63).astype(np.uint64), np.uint64(0))
            at += nb
        lo[rows] = l
        hi[rows] = h
    out = np.empty((n, 2), "<u8")
    out[:, 0] = lo
    out[:, 1] = hi
    info = {"endpointEntries": dict(zip(*[a.tolist() for a in np.unique(eroot[eroot >= 0], return_counts=True)])),
            "indexEntries": dict(zip(*[a.tolist() for a in np.unique(iroot[iroot >= 0], return_counts=True)])),
            "anchorViolations": violations}
    return out.tobytes(), info
