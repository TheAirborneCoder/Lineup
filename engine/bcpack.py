# SPDX-License-Identifier: GPL-3.0-or-later
"""BCPack (Xbox hardware texture codec) decoder for .ttpf chunks.

Clean-room: the format below was worked out from the game's own packs, the public rANS patent
US 11,234,023 and public BCn documentation. No GDK or SDK material is used.
The research notes and scripts that established each rule are the author's private notes and are not part of this repository.

Chunk kind = (chunk_lo32 >> 24) & 0x3f. 0x20 = zlib; BCPack uses BCn << 2 (0x04 BC1, 0x0c BC3, 0x10 BC4, 0x14 BC5,
0x18 BC6H, 0x1c BC7).

Stream. Several rANS decoders ("lanes") run in lockstep and share one byte stream; each emits one
symbol per element. Per lane the stream is a chain of fragments:

    hdr0 = z:2 | h3:1 | flush:1 | q:4        symbol width = lane default - z, q = probability table
    hdr1 [, hdr2]                            data bytes: hdr1 + 1, or hdr1 + 256*hdr2 + 257 with h3

rANS: 32-bit state, lower limit L = 2**24, bytes merged while state < L, a symbol decoded while
state > L, total 65536, symbol 0 owns [1, f0). A flushed fragment ends when the state is back at L
(its trailing zero bytes are not stored: zeros are shifted in once the bytes are used up) and the
next fragment starts from four fresh state bytes. A fragment without flush hands its state on.

Lane array cycle. Lanes act in index order. A lane holding a symbol whose output slot is still full
waits. A lane with an empty one-byte input buffer asks for a byte; eight bytes are handed out per
cycle in lane order and a lane that gets none freezes for the cycle. Otherwise the lane releases its
held symbol, merges one byte if state < L and decodes the next symbol if state > L. When every lane
has a symbol in its slot the element is complete.

Tables. Width w has w+1 per-symbol frequencies (position 0 = symbol 0, position p covers
[2**(p-1), 2**p)): an exponential CDF sampled at 2**(p-1)/2**w, rounded to an integer, then to the
nearest multiple of 2**(p-1) with ties to even, every symbol keeping frequency >= 1.
bcpack_data/tables.json holds them.

BC1 (12 lanes, widths 8 8 5 6 5 5 6 5 8 8 8 8, one element per block):
    lane 0      distance d back to the reference block, zig-zag of the signed byte (0 = none)
    lane 1      always 0
    lanes 2-7   R0 G0 B0 R1 G1 B1 as zig-zag differences from the reference block, modulo 32/64
    lanes 8-11  the 2x2 pixel quadrants TL TR BL BR, each as the rank of its four 2-bit indices in a
                fixed 256-entry table (bcpack_data/bc1_index.json)
    A reference before the first block reads a built-in history of 256 endpoint pairs
    (bcpack_data/bc1_presets.json). decode() uses only confirmed entries and refuses a chunk that
    needs another one. decode_provisional() also uses the best-supported value of an unconfirmed
    entry and reports which ones, so the caller can label the result.

BC7 (16 lanes, widths 4 4 8 8 8 8 8 7 7 5 6 8 8 8 8 5, two elements A and B per block):
    An 8-bit value split over the two elements has its even bits in A and its odd bits in B.
    lane 15     signature: the cell (A, B), ranked by ((A+1)*(B+1), A), selects mode, partition,
                rotation and index selection (bcpack_data/bc7.json, "config")
    lane 1      d_i, distance back to the block the indices are predicted from (0 = none)
    lane 0      zig-zag signed difference: d_e = (d_i + it) mod 256 is the distance to the block the
                endpoints are predicted from (0 = none). BC1 follows the same rule with d_i = 0.
    lanes 2-10  endpoints as zig-zag differences modulo the field size. Subset 0: lanes 2 3 4 5 =
                R G B A; subset 1: lanes 6 7 8 9; subset 2: lane 10 = R, 5 = G, 9 = B. Element A is
                endpoint 0, B endpoint 1. Channels are true channels (the rotation of modes 4 and 5
                is undone); red carries the p-bit as its lowest bit. A block leaves each value
                left-aligned in 8 bits (alpha 0 when the mode has none, a missing subset = subset 0)
                and the next block predicts with stored >> (8 - bits).
    lanes 11-14 indices, one pixel row per lane, element A = pixels x 0-1, B = x 2-3. A symbol
                interleaves two zig-zag differences (first pixel in the odd bits; in row 3 the second),
                modulo the index size, predicted from the reference block's indices left-aligned in
                4 bits. Modes 4 and 5: lanes 11-14 are the 3-bit set (mode 4) or the colour set
                (mode 5), lanes 6 7 8 10 the other set (zero in blocks without one).
    The all-zero block is stored as mode 0, partition 0 with every symbol zero.
    References before the first block read a built-in history of 256 blocks (endpoints and
    indices, bcpack_data/bc7.json). Its values are fitted from the packs: decode() uses only values
    that two or more chunks agree on, decode_provisional() also single-chunk fits and reports them.
    decode_provisional() also accepts an endpoint value of which only the leading bits are known (at least
    four): the missing low bits are taken at the middle of their range and the bound is reported.

BC4 (lanes 8 8 8 8 + 8 x 6 bits, one element per block)
    lane 0     endpoint reference distance, zig-zag modulo 256 as in BC1 (lane 1 is always 0);
               distance 0 = no reference: both endpoints are predicted from 128
    lanes 2, 3 alpha0, alpha1 as zig-zag differences modulo 256 from the referenced block
    lanes 4 + 2r, 5 + 2r   left and right pixel pair of row r: the rank of the pair (left index, right index)
               in a fixed table of all 64 pairs (bcpack_data/bc4.json)
    References before the first block read a built-in history of 256 endpoint pairs. The entries known so far
    are fitted from the next mip of single chunks, so decode() refuses them and decode_provisional() reports them.

BC5 (lanes 4 4 8 8 + 8 x 6 bits, two elements A and B per block; bcpack_data/bc5.json)
    lanes 0, 1 the reference distances, split over the elements as in BC7 (even bits in A, odd bits in B). Lane 1,
               the index reference, is always 0. Distance 0 = no reference: all four endpoints are predicted from 128
    lane 2     red endpoints, lane 3 green endpoints, as zig-zag differences modulo 256 from the referenced block;
               element A = endpoint 0, B = endpoint 1
    lanes 4-7  the red block's pixel rows 0..3, lanes 8-11 the green block's; element A = the left pixel pair,
               B = the right pair, each the rank of (left index, right index) in a fixed table of all 64 pairs
               (another table than BC4's)
    References before the first block read a built-in history of 256 entries (r0, r1, g0, g1). Values pinned by
    flat-area blocks are confirmed; the others are pooled fits against neighbour mips (within about 1 of the true
    value where that could be checked), so decode() refuses them and decode_provisional() reports them.

BC3 (lanes 4 4 8 6 6 6 6 5 6 5 8 8, two elements A and B per block; bcpack_data/bc3.json)
    lanes 0, 1    the reference distances, split over the elements (lane 1, the index reference, is always 0)
    lane 2        alpha endpoints as zig-zag differences modulo 256 from the referenced block, A = alpha0, B = alpha1;
                  no reference predicts 128
    lanes 3-6     alpha pixel rows 0..3, A = the left pixel pair, B = the right pair: the rank of
                  (left index, right index) in BC3's own table of all 64 pairs
    lanes 7, 8, 9 colour R, G, B (5, 6, 5 bits) as zig-zag differences modulo 32 / 64, A = colour 0, B = colour 1;
                  no reference predicts 0
    lanes 10, 11  colour indices as 2x2 quadrants (10: A = top left, B = top right; 11: bottom left, bottom right):
                  the rank of the quadrant's four 2-bit indices in BC3's own 256-entry table. That table is learned
                  from reference pixels; symbols with thin support are reported as provisional.
    References before the first block read a built-in history of 256 entries (alpha0, alpha1, R0, G0, B0, R1, G1,
    B1), fitted against neighbour mips: decode() refuses them and decode_provisional() reports them.

BC6H (lanes 4 4 8 8 8 5 5 5 6 6 6 8 8 8 8 5, two elements per block): rebuilt by bcpack_bc6h.py, data in
    bcpack_data/bc6h.json (a fitted built-in history of 256 endpoint and 256 index states, so every chunk is
    provisional). Exact on 855 of 855 blocks whose content is known. Rules (from the author's research notes, not part of this repository):
    lane 15       A = mode number - 1 (0..13), B = partition
    lanes 0, 1    reference distances as in BC7
    lanes 2-4     the stored base endpoint w of r, g, b; lanes 5-7 the stored field x; lanes 8-10 fields y (A) and
                  z (B): zig-zag differences, values split over the elements with the even bits in A
    lanes 11-14   indices by the BC7 rule; a block without an index reference predicts 8 (left-aligned in 4 bits)
    Between blocks every field is kept as raw bits, left-aligned (w 16 bits, x 10, y and z 6); a block without an
    endpoint reference starts every field in the middle of its scale; in mode 13 BCPack's w has bits 10 and 11 swapped
    against the format's, in mode 14 bits 10..15 reversed. A one-region block leaves z = the top 6 bits of x and y =
    the top 6 bits of w (offset by half the range in transformed modes). Signed (SF16) chunks have no neighbour mips:
    their table entries rest on "no negative value" and "mode 14 blocks are black"; the extractor checks them.
"""

import argparse
import ctypes
import glob
import json
import os
import struct
import sys
import threading

from paths import NATIVE

L = 1 << 24
MAXW = 10
BUDGET = 8
VZMAX = 8
# ttpf_format only routes chunks through decode() when this is True.
AVAILABLE = True
KINDS = {0x04: "BC1", 0x0C: "BC3", 0x10: "BC4", 0x14: "BC5", 0x18: "BC6H", 0x1C: "BC7"}
ELEMENT = {0x04: 8, 0x0C: 8, 0x10: 8, 0x14: 8, 0x18: 8, 0x1C: 8}   # bytes per lane element (BC3, BC5, BC7: two elements per block)
BLOCK = {0x04: 8, 0x0C: 16, 0x10: 8, 0x14: 16, 0x18: 16, 0x1C: 16}
LANES = {0x04: (8, 8, 5, 6, 5, 5, 6, 5, 8, 8, 8, 8),
         0x0C: (4, 4, 8, 6, 6, 6, 6, 5, 6, 5, 8, 8),
         0x10: (8, 8, 8, 8, 6, 6, 6, 6, 6, 6, 6, 6),
         0x14: (4, 4, 8, 8, 6, 6, 6, 6, 6, 6, 6, 6),
         0x18: (4, 4, 8, 8, 8, 5, 5, 5, 6, 6, 6, 8, 8, 8, 8, 5),
         0x1C: (4, 4, 8, 8, 8, 8, 8, 7, 7, 5, 6, 8, 8, 8, 8, 5)}
REBUILD = {0x04, 0x0C, 0x10, 0x14, 0x18, 0x1C}                    # kinds whose blocks can be rebuilt from the symbols
DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bcpack_data")


def _library_paths(base):
    """Where a native helper may be: the work folder's native/ first (native_build.py puts it there), then next to the tables."""
    suffix = ".dll" if sys.platform == "win32" else ".dylib" if sys.platform == "darwin" else ".so"
    return [os.path.join(folder, base + suffix) for folder in (NATIVE, DATA)]


ERRORS = {1: "probability table for this width and q is not known", 2: "a fragment did not end at its home state",
          3: "input ran out", 4: "symbol outside its width", 5: "symbol width out of range", 6: "unsupported lane count"}


class _Store:
    """Lazy data: probability tables, the BC1 index table, the BC1 preset history, the native library."""
    tables = None
    native = False
    native_tables = None
    bc1_index = None
    bc1_presets = None
    bc7 = None
    bc7_native = False
    bc4 = None
    bc5 = None
    bc3 = None

    @classmethod
    def load_bc3(cls):
        """(alpha pair table [64][2], colour quadrant table [256], symbols of it that are well supported,
        confirmed history entries, unconfirmed ones)."""
        if cls.bc3 is None:
            path = os.path.join(DATA, "bc3.json")
            if not os.path.exists(path):
                raise ValueError("BC3 rebuild data (bcpack_data/bc3.json) is not present")
            with open(path) as f:
                raw = json.load(f)
            solid = {int(k): tuple(v["value"]) for k, v in raw["history"].items() if v["confirmed"]}
            loose = {int(k): tuple(v["value"]) for k, v in raw["history"].items() if not v["confirmed"]}
            cls.bc3 = ([tuple(p) for p in raw["alphaSymbolToPair"]], raw["colourSymbolToQuadrant"],
                       set(raw["colourSymbolsSupported"]), solid, loose)
        return cls.bc3

    @classmethod
    def load_bc5(cls):
        """(index-pair table [64][2], confirmed built-in entries (r0, r1, g0, g1), unconfirmed ones)."""
        if cls.bc5 is None:
            path = os.path.join(DATA, "bc5.json")
            if not os.path.exists(path):
                raise ValueError("BC5 rebuild data (bcpack_data/bc5.json) is not present")
            with open(path) as f:
                raw = json.load(f)
            solid = {int(k): tuple(v["value"]) for k, v in raw["history"].items() if all(v["confirmed"])}
            loose = {int(k): tuple(v["value"]) for k, v in raw["history"].items() if not all(v["confirmed"])}
            cls.bc5 = ([tuple(p) for p in raw["symbolToPair"]], solid, loose)
        return cls.bc5

    @classmethod
    def load_tables(cls):
        if cls.tables is None:
            with open(os.path.join(DATA, "tables.json")) as f:
                raw = json.load(f)
            cls.tables = {tuple(map(int, k.split(","))): v for k, v in raw.items()}
        return cls.tables

    @classmethod
    def load_native(cls):
        if cls.native is False:
            cls.native = None
            for path in _library_paths("bcpack_native"):
                if os.path.exists(path):
                    try:
                        lib = ctypes.CDLL(path)
                        lib.bcpack_lanes.restype = ctypes.c_int
                        cls.native = lib
                        break
                    except OSError:
                        pass
        return cls.native

    @classmethod
    def load_bc7(cls):
        if cls.bc7 is None:
            path = os.path.join(DATA, "bc7.json")
            if not os.path.exists(path):
                raise ValueError("BC7 rebuild data (bcpack_data/bc7.json) is not present")
            with open(path) as f:
                raw = json.load(f)
            cells = sorted(((a, b) for a in range(32) for b in range(16)), key=lambda ab: ((ab[0] + 1) * (ab[1] + 1), ab[0]))
            rank_of = [-1] * 512
            for rank, (a, b) in enumerate(cells[:len(raw["config"])]):
                rank_of[a * 16 + b] = rank
            raw["rank_of"] = rank_of
            raw["inferred"] = set(raw["configInferred"])
            raw["last_resort"] = {(k, j) for k, j in raw.get("historyEndpointLastResort", [])}
            # A value known to d < 8 leading bits: put the unknown low bits at the middle of their range. Readers of
            # d bits or fewer are not affected (they shift the low bits away).
            for row, depth in zip(raw["historyEndpoints"], raw["historyEndpointBits"]):
                for j, d in enumerate(depth):
                    if 0 < d < 8:
                        row[j] = (row[j] >> (8 - d) << (8 - d)) | (1 << (7 - d))
            cls.bc7 = raw
        return cls.bc7

    @classmethod
    def load_bc7_native(cls):
        if cls.bc7_native is False:
            cls.bc7_native = None
            for path in _library_paths("bcpack_bc7"):
                if os.path.exists(path):
                    try:
                        lib = ctypes.CDLL(path)
                        lib.bc7_rebuild.restype = ctypes.c_int
                        cls.bc7_native = lib
                        break
                    except OSError:
                        pass
        return cls.bc7_native

    @classmethod
    def load_bc4(cls):
        """(index-pair table [64][2], confirmed built-in endpoint entries, unconfirmed ones, entries whose value is a
        last resort: a mip fit on fewer than 8 blocks, or block-edge continuity only, so their blocks can be visibly off)."""
        if cls.bc4 is None:
            path = os.path.join(DATA, "bc4.json")
            if not os.path.exists(path):
                raise ValueError("BC4 rebuild data (bcpack_data/bc4.json) is not present")
            with open(path) as f:
                raw = json.load(f)
            solid = {int(k): tuple(v["value"]) for k, v in raw["history"].items() if v.get("confirmed")}
            loose = {int(k): tuple(v["value"]) for k, v in raw["history"].items() if not v.get("confirmed")}
            last = {int(k) for k, v in raw["history"].items() if v.get("lastResort") and not v.get("confirmed")}
            cls.bc4 = ([tuple(p) for p in raw["symbolToPair"]], solid, loose, last)
        return cls.bc4

    @classmethod
    def load_bc1(cls):
        if cls.bc1_index is None:
            with open(os.path.join(DATA, "bc1_presets.json")) as f:
                entries = json.load(f)["entries"]
            cls.bc1_presets = {int(k): v["value"] for k, v in entries.items() if v.get("solid")}
            cls.bc1_provisional = {int(k): v["value"] for k, v in entries.items() if not v.get("solid")}
            with open(os.path.join(DATA, "bc1_index.json")) as f:
                cls.bc1_index = json.load(f)["symbolToQuadrant"]
        return cls.bc1_index, cls.bc1_presets


class _Model:
    __slots__ = ("w", "freq", "base")

    def __init__(self, w, freq):
        self.w, self.freq, self.base = w, freq, []
        acc = 0
        for p, f in enumerate(freq):
            self.base.append(acc)
            acc += f * (1 if p == 0 else 1 << (p - 1))
        if acc != 65536:
            raise ValueError("Probability table does not sum to 65536")

    def lookup(self, cf):
        p = self.w
        while p > 0 and cf < self.base[p]:
            p -= 1
        return 0 if p == 0 else (1 << (p - 1)) + (cf - self.base[p]) // self.freq[p]

    def advance(self, s, x):
        p = s.bit_length()
        f = self.freq[p]
        c = self.base[p] + f * (s - (0 if p == 0 else 1 << (p - 1)))
        if c == 0:
            f, c = f - 1, 1
        return f * (x >> 16) + (x & 0xFFFF) - c


def _lanes_python(raw, widths, nsym):
    """Reference implementation of the lane array (slow). Returns one symbol list per lane and bytes used."""
    tables = _Store.load_tables()
    models = {}
    nl = len(widths)
    HDR0, HDR1, HDR2, PROC = range(4)
    ctrl = [HDR0] * nl
    out_rem = [nsym] * nl
    w = [0] * nl
    q = [0] * nl
    h3 = [0] * nl
    flush = [0] * nl
    done = [nsym <= 0] * nl
    full = [False] * nl
    held = [False] * nl
    vz = [0] * nl
    inbuf = [0] * nl
    sym = [0] * nl
    in_rem = [0] * nl
    p0 = [0] * nl
    p1 = [0] * nl
    model = [None] * nl
    slotfull = [False] * nl
    slotval = [0] * nl
    stall = [False] * nl
    out = [[] for _ in range(nl)]
    pos, nraw = 0, len(raw)

    def fail(code, lane):
        raise ValueError(f"lane {lane} (width {w[lane]}, q {q[lane]}) at input byte {pos}: {ERRORS[code]}")

    while True:
        if all(done[i] and not slotfull[i] for i in range(nl)):
            return out, pos
        left = BUDGET
        for i in range(nl):
            if done[i]:
                stall[i] = True
                continue
            stall[i] = held[i] and slotfull[i]
            if stall[i]:
                continue
            if ctrl[i] == PROC:
                p0[i] = model[i].advance(sym[i], p1[i]) if held[i] else p1[i]
            if not full[i] and (ctrl[i] != PROC or in_rem[i] > 0):
                if left == 0:
                    stall[i] = True
                    continue
                if pos >= nraw:
                    fail(3, i)
                inbuf[i] = raw[pos]
                pos += 1
                full[i] = True
                left -= 1
            if held[i]:
                slotfull[i] = True
                slotval[i] = sym[i]
                held[i] = False
        for i in range(nl):
            if done[i] or stall[i]:
                continue
            c = ctrl[i]
            if c == HDR0:
                if not full[i]:
                    continue
                h = inbuf[i]
                q[i] = h & 15
                flush[i] = (h >> 4) & 1
                h3[i] = (h >> 5) & 1
                w[i] = widths[i] - (h >> 6)
                full[i] = False
                ctrl[i] = HDR1
                vz[i] = 0
            elif c == HDR1:
                if not full[i]:
                    continue
                full[i] = False
                if not 1 <= w[i] <= MAXW:
                    fail(5, i)
                key = (w[i], q[i])
                if key not in models:
                    if key not in tables:
                        fail(1, i)
                    models[key] = _Model(w[i], tables[key])
                model[i] = models[key]
                if h3[i]:
                    in_rem[i] = inbuf[i]
                    ctrl[i] = HDR2
                else:
                    in_rem[i] = inbuf[i] + 1
                    ctrl[i] = PROC
            elif c == HDR2:
                if not full[i]:
                    continue
                in_rem[i] = ((inbuf[i] << 8) | (in_rem[i] & 0xFF)) + 257
                ctrl[i] = PROC
                full[i] = False
            else:
                st = p0[i]
                if st < L:
                    if in_rem[i] > 0:
                        if full[i]:
                            st = (st << 8) | inbuf[i]
                            in_rem[i] -= 1
                            full[i] = False
                            if in_rem[i] == 0 and flush[i]:
                                while st < L and st != 0 and vz[i] < VZMAX:
                                    st <<= 8
                                    vz[i] += 1
                    elif flush[i]:
                        if st == 0 or vz[i] >= VZMAX:
                            fail(2, i)
                        while st < L and st != 0 and vz[i] < VZMAX:
                            st <<= 8
                            vz[i] += 1
                    else:
                        p1[i] = st
                        if out_rem[i] > 0:
                            ctrl[i] = HDR0
                        else:
                            done[i] = True
                        continue
                nxt = st > L and out_rem[i] > 0
                if nxt:
                    sym[i] = model[i].lookup(st & 0xFFFF)
                    out_rem[i] -= 1
                    if sym[i] >> w[i]:
                        fail(4, i)
                held[i] = nxt
                p1[i] = st
                if in_rem[i] == 0:
                    if flush[i]:
                        if st == L:
                            if out_rem[i] > 0:
                                ctrl[i] = HDR0
                                p0[i] = 0
                                p1[i] = 0
                            else:
                                done[i] = True
                        elif st > L and not nxt:
                            fail(2, i)
                    elif out_rem[i] == 0 and not nxt:
                        done[i] = True
                elif st == L or (out_rem[i] == 0 and not nxt and st > L):
                    fail(2, i)
        ready, anyfull = True, False
        for i in range(nl):
            if slotfull[i]:
                anyfull = True
            elif not (done[i] and not held[i]):
                ready = False
                break
        if ready and anyfull:
            for i in range(nl):
                if slotfull[i]:
                    out[i].append(slotval[i])
                    slotfull[i] = False
        for i in range(nl):
            if done[i] and held[i] and not slotfull[i]:
                slotfull[i] = True
                slotval[i] = sym[i]
                held[i] = False


_NATIVE_FIRST = threading.Lock()
_native_warm = set()


def _first_native_call(kind, call):
    """The first call into each native library runs alone, so that the C code's static tables and the tables built
    here are filled before any other thread uses them. Later calls run without the lock."""
    if kind in _native_warm:
        return call()
    with _NATIVE_FIRST:
        result = call()
        _native_warm.add(kind)
        return result


def _lanes_native(lib, raw, widths, nsym):
    return _first_native_call("lanes", lambda: _lanes_native_call(lib, raw, widths, nsym))


def _lanes_native_call(lib, raw, widths, nsym):
    if _Store.native_tables is None:
        tables = _Store.load_tables()
        freq = (ctypes.c_int32 * ((MAXW + 1) * 16 * (MAXW + 1)))()
        known = (ctypes.c_uint8 * ((MAXW + 1) * 16))()
        for (tw, tq), f in tables.items():
            known[tw * 16 + tq] = 1
            for p, v in enumerate(f):
                freq[(tw * 16 + tq) * (MAXW + 1) + p] = v
        _Store.native_tables = (freq, known)
    freq, known = _Store.native_tables
    nl = len(widths)
    out = (ctypes.c_uint16 * (nl * max(nsym, 1)))()
    info = (ctypes.c_int32 * 8)()
    code = lib.bcpack_lanes(raw, len(raw), nl, (ctypes.c_int * nl)(*widths), nsym, freq, known, out, info)
    if code:
        raise ValueError(f"lane {info[2]} (width {info[3]}, q {info[4]}) at input byte {info[1]}: {ERRORS.get(code, code)}")
    flat = list(out)
    return [flat[i * nsym:(i + 1) * nsym] for i in range(nl)], info[1]


def lane_symbols(raw, kind, out_size, force_python=False):
    """Entropy layer: one list of symbols per lane, and the number of input bytes used."""
    if kind not in KINDS:
        raise ValueError(f"Not a BCPack chunk kind: {kind:#x}")
    if out_size % ELEMENT[kind]:
        raise ValueError("Chunk size is not a whole number of elements")
    nsym = out_size // ELEMENT[kind]
    lib = None if force_python else _Store.load_native()
    raw = bytes(raw)
    lanes, used = (_lanes_native(lib, raw, LANES[kind], nsym) if lib is not None
                   else _lanes_python(raw, LANES[kind], nsym))
    if any(raw[used:]):
        raise ValueError("Data left over after the last lane finished")
    return lanes, used


def _bc1_blocks(lanes, nblocks, provisional=None):
    """BC1 blocks from the twelve lanes. `provisional`: a set that collects the unconfirmed history
    entries used; when it is None such entries are refused."""
    index, presets = _Store.load_bc1()
    if any(lanes[1]):
        raise ValueError("BC1 lane 1 is not zero; this case has not been seen and is not understood")
    out = bytearray(nblocks * 8)
    hist = [None] * nblocks
    mods = (32, 64, 32, 32, 64, 32)
    l0 = lanes[0]
    cols = lanes[2:8]
    q0, q1, q2, q3 = lanes[8:12]
    for i in range(nblocks):
        v = l0[i]
        d = 256 - ((v + 1) >> 1) if v & 1 else v >> 1
        if d == 0:
            base = (0, 0, 0, 0, 0, 0)
        elif i - d >= 0:
            base = hist[i - d]
        else:
            base = presets.get(256 + i - d)
            if base is None and provisional is not None and 256 + i - d in _Store.bc1_provisional:
                base = _Store.bc1_provisional[256 + i - d]
                provisional.add(256 + i - d)
            if base is None:
                raise ValueError(f"block {i} needs entry {256 + i - d} of the built-in endpoint history, which is not pinned yet")
        e = []
        for k in range(6):
            s = cols[k][i]
            dz = -((s + 1) >> 1) if s & 1 else s >> 1
            e.append((base[k] + dz) % mods[k])
        hist[i] = e
        c0 = (e[0] << 11) | (e[1] << 5) | e[2]
        c1 = (e[3] << 11) | (e[4] << 5) | e[5]
        a, b, c, dd = index[q0[i]], index[q1[i]], index[q2[i]], index[q3[i]]
        # quadrant byte: pixels TL, TR, BL, BR of the quadrant in bits 0-1, 2-3, 4-5, 6-7
        row0 = (a & 15) | ((b & 15) << 4)
        row1 = (a >> 4) | ((b >> 4) << 4)
        row2 = (c & 15) | ((dd & 15) << 4)
        row3 = (c >> 4) | ((dd >> 4) << 4)
        struct.pack_into("<HHBBBB", out, i * 8, c0, c1, row0, row1, row2, row3)
    return bytes(out)


# BC4 ---------------------------------------------------------------------------------------------
def _bc4_blocks(lanes, nblocks, provisional=None):
    """BC4 blocks from the twelve lanes. `provisional`: a set that collects labels of the unconfirmed built-in
    endpoint entries used; when it is None such entries are refused."""
    pairs, solid, loose, last = _Store.load_bc4()
    if any(lanes[1]):
        raise ValueError("BC4 lane 1 is not zero; this case has not been seen and is not understood")
    out = bytearray(nblocks * 8)
    hist = [None] * nblocks
    l0, l2, l3 = lanes[0], lanes[2], lanes[3]
    rows = lanes[4:12]
    for i in range(nblocks):
        v = l0[i]
        d = 256 - ((v + 1) >> 1) if v & 1 else v >> 1
        if d == 0:
            base = (128, 128)                       # no reference: both endpoints are predicted from 128
        elif i - d >= 0:
            base = hist[i - d]
        else:
            entry = 256 + i - d
            base = solid.get(entry)
            if base is None and provisional is not None and entry in loose:
                base = loose[entry]
                if entry in last:
                    provisional.add(f"BC4 endpoint history {entry}: a last-resort value from a thin reference "
                                    "(values can be visibly off)")
                else:
                    provisional.add(f"BC4 endpoint history {entry}")
            if base is None:
                known = "is not confirmed yet" if entry in loose else "is not known"
                raise ValueError(f"block {i} needs entry {entry} of the built-in BC4 endpoint history, which {known}")
        a0 = (base[0] + _unzig(l2[i])) & 255
        a1 = (base[1] + _unzig(l3[i])) & 255
        hist[i] = (a0, a1)
        bits = 0
        for r in range(4):
            left, right = pairs[rows[2 * r][i]], pairs[rows[2 * r + 1][i]]
            bits |= (left[0] | (left[1] << 3) | (right[0] << 6) | (right[1] << 9)) << (12 * r)
        out[i * 8] = a0
        out[i * 8 + 1] = a1
        out[i * 8 + 2:i * 8 + 8] = bits.to_bytes(6, "little")
    return bytes(out)


# BC3 ---------------------------------------------------------------------------------------------
_B3_MODS = (256, 256, 32, 64, 32, 32, 64, 32)     # alpha0 alpha1 R0 G0 B0 R1 G1 B1
_B3_NONE = (128, 128, 0, 0, 0, 0, 0, 0)           # predictors of a block without a reference


def _bc3_blocks(lanes, nblocks, provisional=None):
    """BC3 blocks from the twelve lanes (two elements per block). `provisional`: a set that collects labels of
    what the result relied on without confirmation; when it is None those are refused."""
    pairs, quads, supported, solid, loose = _Store.load_bc3()
    if any(lanes[1]):
        raise ValueError("BC3 lane 1 (index reference) is not zero; this case has not been seen and is not understood")
    out = bytearray(nblocks * 16)
    hist = [None] * nblocks
    lost = [False] * nblocks
    l0, alpha = lanes[0], lanes[2]
    rows = lanes[3:7]
    lr, lg, lb = lanes[7], lanes[8], lanes[9]
    top, bottom = lanes[10], lanes[11]
    unknown = set()
    weak = 0
    mods = _B3_MODS
    for i in range(nblocks):
        a, b = 2 * i, 2 * i + 1
        d = _unzig(_inter8(l0[a], l0[b])) & 255
        if d == 0:
            base = _B3_NONE
        elif i - d >= 0:
            base = hist[i - d]
            lost[i] = lost[i - d]
        else:
            entry = 256 + i - d
            base = solid.get(entry)
            if base is None and entry in loose:
                if provisional is None:
                    raise ValueError(f"block {i} needs entry {entry} of the built-in BC3 history, which is not confirmed yet")
                base = loose[entry]
                provisional.add(f"BC3 history {entry}")
            if base is None:
                if provisional is None:
                    raise ValueError(f"block {i} needs entry {entry} of the built-in BC3 history, which is not known")
                base, lost[i] = _B3_NONE, True
                unknown.add(entry)
        dz = (alpha[a], alpha[b], lr[a], lg[a], lb[a], lr[b], lg[b], lb[b])
        e = tuple((base[k] + _unzig(dz[k])) % mods[k] for k in range(8))
        hist[i] = e
        bits = 0
        for r in range(4):
            left, right = pairs[rows[r][a]], pairs[rows[r][b]]
            bits |= (left[0] | (left[1] << 3) | (right[0] << 6) | (right[1] << 9)) << (12 * r)
        at = i * 16
        out[at] = e[0]
        out[at + 1] = e[1]
        out[at + 2:at + 8] = bits.to_bytes(6, "little")
        c0 = (e[2] << 11) | (e[3] << 5) | e[4]
        c1 = (e[5] << 11) | (e[6] << 5) | e[7]
        sa, sb, sc, sd = top[a], top[b], bottom[a], bottom[b]
        weak += (sa not in supported) + (sb not in supported) + (sc not in supported) + (sd not in supported)
        qa, qb, qc, qd = quads[sa], quads[sb], quads[sc], quads[sd]
        # quadrant byte: pixels TL, TR, BL, BR of the quadrant in bits 0-1, 2-3, 4-5, 6-7
        struct.pack_into("<HHBBBB", out, at + 8, c0, c1, (qa & 15) | ((qb & 15) << 4), (qa >> 4) | ((qb >> 4) << 4),
                         (qc & 15) | ((qd & 15) << 4), (qc >> 4) | ((qd >> 4) << 4))
    if unknown:
        total = sum(lost)
        if total > _loose_limit(nblocks):
            names = ", ".join(str(k) for k in sorted(unknown)[:6]) + (", ..." if len(unknown) > 6 else "")
            raise ValueError(f"{total} of {nblocks} blocks start from built-in BC3 history entries that are not known yet ({names})")
        provisional.add(f"BC3 history {', '.join(map(str, sorted(unknown)))} not known: {total} blocks undetermined")
    if weak:
        if provisional is None:
            raise ValueError(f"{weak} colour quadrants use symbols of the BC3 quadrant table that are not well supported yet")
        provisional.add(f"BC3 colour quadrant table: {weak} of {4 * nblocks} quadrants use symbols with thin support")
    return bytes(out)


# BC5 ---------------------------------------------------------------------------------------------
def _loose_limit(nblocks):
    """Provisional decode: at most one block in 1,024 (and at least two) may be undetermined."""
    return max(2, nblocks // 1024)


def _bc5_blocks(lanes, nblocks, provisional=None):
    """BC5 blocks from the twelve lanes (two elements per block). `provisional`: a set that collects labels of
    the unconfirmed built-in entries used; when it is None such entries are refused."""
    pairs, solid, loose = _Store.load_bc5()
    if any(lanes[1]):
        raise ValueError("BC5 lane 1 (index reference) is not zero; this case has not been seen and is not understood")
    out = bytearray(nblocks * 16)
    hist = [None] * nblocks
    lost = [False] * nblocks                        # the chain starts at an entry with no value at all
    l0, red, green = lanes[0], lanes[2], lanes[3]
    rows = lanes[4:12]
    unknown = set()
    for i in range(nblocks):
        a, b = 2 * i, 2 * i + 1
        d = _unzig(_inter8(l0[a], l0[b])) & 255
        if d == 0:
            base = (128, 128, 128, 128)             # no reference: every endpoint is predicted from 128
        elif i - d >= 0:
            base = hist[i - d]
            lost[i] = lost[i - d]
        else:
            entry = 256 + i - d
            base = solid.get(entry)
            if base is None and entry in loose:
                if provisional is None:
                    raise ValueError(f"block {i} needs entry {entry} of the built-in BC5 endpoint history, which is not confirmed yet")
                base = loose[entry]
                provisional.add(f"BC5 endpoint history {entry}")
            if base is None:
                if provisional is None:
                    raise ValueError(f"block {i} needs entry {entry} of the built-in BC5 endpoint history, which is not known")
                base, lost[i] = (128, 128, 128, 128), True
                unknown.add(entry)
        e = ((base[0] + _unzig(red[a])) & 255, (base[1] + _unzig(red[b])) & 255,
             (base[2] + _unzig(green[a])) & 255, (base[3] + _unzig(green[b])) & 255)
        hist[i] = e
        at = i * 16
        for ch in range(2):
            bits = 0
            for r in range(4):
                left, right = pairs[rows[4 * ch + r][a]], pairs[rows[4 * ch + r][b]]
                bits |= (left[0] | (left[1] << 3) | (right[0] << 6) | (right[1] << 9)) << (12 * r)
            out[at + 8 * ch] = e[2 * ch]
            out[at + 8 * ch + 1] = e[2 * ch + 1]
            out[at + 8 * ch + 2:at + 8 * ch + 8] = bits.to_bytes(6, "little")
    if unknown:
        total = sum(lost)
        if total > _loose_limit(nblocks):
            names = ", ".join(str(k) for k in sorted(unknown)[:6]) + (", ..." if len(unknown) > 6 else "")
            raise ValueError(f"{total} of {nblocks} blocks start from built-in BC5 history entries that are not known yet ({names})")
        provisional.add(f"BC5 endpoint history {', '.join(map(str, sorted(unknown)))} not known: {total} blocks undetermined")
    return bytes(out)


# BC6H --------------------------------------------------------------------------------------------
def _bc6h_blocks(lanes, nblocks, provisional=None):
    """BC6H blocks from the sixteen lanes (bcpack_bc6h.py). No built-in history value is pinned exactly, so decode()
    only returns chunks that read none; decode_provisional() reports what was read."""
    import bcpack_bc6h
    _, _, eclass, iclass = bcpack_bc6h.load_table()
    data, info = bcpack_bc6h.rebuild(lanes, nblocks)
    ends, idxs = info["endpointEntries"], info["indexEntries"]
    if provisional is None:
        if ends or idxs:
            k = next(iter(ends or idxs))
            raise ValueError(f"needs entry {k} of the built-in BC6H history, which is not confirmed yet")
        return data
    lost = sum(c for k, c in ends.items() if eclass[k] == 0) + sum(c for k, c in idxs.items() if iclass[k] == 0)
    missing = sorted({f"endpoint {k}" for k in ends if eclass[k] == 0} | {f"index {k}" for k in idxs if iclass[k] == 0})
    if lost > _loose_limit(nblocks):
        raise ValueError(f"{lost} of {nblocks} blocks start from built-in BC6H history entries that are not known yet "
                         f"({', '.join(missing[:6])}{', ...' if len(missing) > 6 else ''})")
    if info["anchorViolations"] > _loose_limit(nblocks):
        raise ValueError(f"{info['anchorViolations']} anchor pixels of {nblocks} blocks came out with their top index bit set "
                         "(a built-in BC6H index value is wrong)")
    if missing:
        provisional.add(f"BC6H history {', '.join(missing)} not known: {lost} blocks undetermined")
    if info["anchorViolations"]:
        provisional.add(f"BC6H: {info['anchorViolations']} anchor pixels with an inconsistent index")
    for name, entries, cls in (("endpoint", ends, eclass), ("index", idxs, iclass)):
        fitted = sorted(k for k in entries if cls[k] == 1)
        rough = sorted(k for k in entries if cls[k] == 2)
        if fitted:
            provisional.add(f"BC6H {name} history {', '.join(map(str, fitted))}")
        if rough:
            provisional.add(f"BC6H {name} history {', '.join(map(str, rough))}: last-resort values from a rough reference "
                            "(values can be visibly off)")
    return data


# BC7 ---------------------------------------------------------------------------------------------
_B7_NS = (3, 2, 3, 2, 1, 1, 1, 2)
_B7_CB = (4, 6, 5, 7, 5, 7, 7, 5)
_B7_AB = (0, 0, 0, 0, 6, 8, 7, 5)
_B7_PK = (1, 2, 0, 1, 0, 0, 1, 1)                 # 0 none, 1 p-bit per endpoint, 2 shared per subset
_B7_IB = (3, 3, 2, 2, 3, 2, 4, 2)                 # index bits of the set in lanes 11-14
_B7_PB = (4, 6, 6, 6, 0, 0, 0, 6)
_B7_LANE = ((2, 3, 4, 5), (6, 7, 8, 9), (10, 5, 9, -1))
_B7_ANCHOR, _B7_PBIT, _B7_SIGNATURE = 8, 16, 32
_B7_LOOSE = 1024      # provisional decode: at most one block in this many (and at least two) may be undetermined
_B7_MIN_DEPTH = 4     # provisional decode: an endpoint value counts as usable when this many leading bits are known


def _bc7_loose_limit(nblocks):
    return max(2, nblocks // _B7_LOOSE)


def _unzig(v):
    return -((v + 1) >> 1) if v & 1 else v >> 1


def _inter8(a, b):
    d = 0
    for k in range(4):
        d |= ((a >> k) & 1) << (2 * k) | ((b >> k) & 1) << (2 * k + 1)
    return d


def _deint(s, odd):
    v = 0
    for k in range(4):
        v |= ((s >> (2 * k + odd)) & 1) << k
    return v


def _bc7_field_bits(mode, rot):
    """Bits a block of this mode reads from a stored byte, for true channels R G B A (0 = none)."""
    w = [_B7_CB[mode]] * 3 + [_B7_AB[mode]]
    if rot:
        w[rot - 1], w[3] = _B7_AB[mode], _B7_CB[mode]
    return [w[c] + (1 if (_B7_PK[mode] and c == 0) else 0) if w[c] else 0 for c in range(4)]


def _bc7_check(lanes, nblocks, table, provisional):
    """Check that every built-in value and signature the chunk uses is known well enough. Raises ValueError,
    or adds labels to `provisional` (a set) when that is allowed. Returns the (kind, entry) pairs whose value
    is not known to the depth the chunk reads (only when `provisional` is given; otherwise that raises)."""
    cfg, rank_of = table["config"], table["rank_of"]
    l0, l1, l15 = lanes[0], lanes[1], lanes[15]
    ranks = set()
    for a, b in set(zip(l15[0::2], l15[1::2])):
        rank = rank_of[a * 16 + b] if a < 32 and b < 16 else -1
        if rank < 0 or cfg[rank][0] > 7 or cfg[rank][1] == 255:
            raise ValueError(f"signature ({a}, {b}) is not in the BC7 table")
        ranks.add(rank)
    for rank in sorted(ranks & table["inferred"]):
        if provisional is None:
            raise ValueError(f"signature rank {rank}: its partition is placed by order only, not confirmed yet")
        provisional.add(f"signature {rank}")
    need_e, need_i, unknown = {}, {}, []
    for i in range(min(nblocks, 256)):
        rank = rank_of[l15[2 * i] * 16 + l15[2 * i + 1]]
        mode, part, rot, isel = cfg[rank]
        di = _inter8(l1[2 * i], l1[2 * i + 1])
        de = (di + _unzig(_inter8(l0[2 * i], l0[2 * i + 1]))) & 255
        if de > i:
            bits = _bc7_field_bits(mode, rot)
            row = need_e.setdefault(256 + i - de, [[0] * 4 for _ in range(3)])
            for sub in range(_B7_NS[mode]):
                for c in range(4):
                    row[sub][c] = max(row[sub][c], bits[c])
        if di > i:
            row = need_i.setdefault(256 + i - di, [0, 0])
            row[0] = max(row[0], _B7_IB[mode])
            if mode in (4, 5):
                row[1] = 2
    for kinds, need, bits_t, conf_t in (("endpoint", need_e, table["historyEndpointBits"], table["historyEndpointConfirmedBits"]),
                                        ("index", need_i, table["historyIndexBits"], table["historyIndexConfirmedBits"])):
        for k, want in sorted(need.items()):
            if kinds == "endpoint":
                slots = [(sub * 8 + e * 4 + c, want[sub][c]) for sub in range(3) for e in range(2) for c in range(4) if want[sub][c]]
            else:
                slots = [(st * 16 + p, want[st]) for st in range(2) for p in range(16) if want[st]]
            weak = missing = False
            shallow = 8
            for j, b in slots:
                if bits_t[k][j] < b:
                    missing = True
                    shallow = min(shallow, bits_t[k][j])
                weak |= conf_t[k][j] < b
            if missing:
                if provisional is None:
                    raise ValueError(f"needs entry {k} of the built-in {kinds} history, which is not known")
                if kinds == "endpoint" and shallow >= _B7_MIN_DEPTH:
                    # only low bits are missing: they are taken at the middle of their range (see load_bc7)
                    provisional.add(f"endpoint history {k}: only the top {shallow} bits are known "
                                    f"(colours can be off by up to {1 << (7 - shallow)}/255)")
                else:
                    unknown.append((kinds, k))
            elif weak:
                if provisional is None:
                    raise ValueError(f"needs entry {k} of the built-in {kinds} history, which is not confirmed yet")
                provisional.add(f"{kinds} history {k}")
            if kinds == "endpoint" and provisional is not None and any((k, j) in table["last_resort"] for j, _ in slots):
                # a value taken from a single rough fit (bc7weak.py): the colour can be visibly wrong, not a step off
                provisional.add(f"endpoint history {k}: a last-resort value from a rough reference (colours can be visibly off)")
    return unknown


def _bc7_rebuild_python(lanes, n, table):
    """Reference implementation of the rebuild (same result as bcpack_bc7.c). Returns (bytes, status list)."""
    cfg, rank_of = table["config"], table["rank_of"]
    p2, p3, a2, a3a, a3b = table["partition2"], table["partition3"], table["anchor2"], table["anchor3a"], table["anchor3b"]
    pre_e, pre_i = table["historyEndpoints"], table["historyIndices"]
    out = bytearray(16 * n)
    status = [0] * n
    eps = [None] * n
    ixs = [None] * n
    zero_e, zero_i = [0] * 24, [0] * 32
    rows1, rows2 = (11, 12, 13, 14), (6, 7, 8, 10)
    for i in range(n):
        A = [lane[2 * i] for lane in lanes]
        B = [lane[2 * i + 1] for lane in lanes]
        rank = rank_of[A[15] * 16 + B[15]] if A[15] < 32 and B[15] < 16 else -1
        if rank < 0 or cfg[rank][0] > 7:
            status[i] = _B7_SIGNATURE
            eps[i], ixs[i] = zero_e, zero_i
            continue
        mode, part, rot, isel = cfg[rank]
        st = 0
        di = _inter8(A[1], B[1])
        de = (di + _unzig(_inter8(A[0], B[0]))) & 255
        rep = zero_e if de == 0 else (eps[i - de] if i >= de else pre_e[256 + i - de])
        rix = zero_i if di == 0 else (ixs[i - di] if i >= di else pre_i[256 + i - di])
        ns, cb, ab, pk = _B7_NS[mode], _B7_CB[mode], _B7_AB[mode], _B7_PK[mode]
        w = [cb, cb, cb, ab]
        if rot:
            w[rot - 1], w[3] = ab, cb
        val = [[[0] * 4, [0] * 4] for _ in range(3)]
        sep = [0] * 24
        pb = [[0, 0], [0, 0], [0, 0]]
        for sub in range(ns):
            for c in range(4):
                lane = _B7_LANE[sub][c]
                if not w[c] or lane < 0:
                    continue
                bits = w[c] + (1 if (pk and c == 0) else 0)
                for e in range(2):
                    v = ((rep[sub * 8 + e * 4 + c] >> (8 - bits)) + _unzig(B[lane] if e else A[lane])) & ((1 << bits) - 1)
                    val[sub][e][c] = v
                    sep[sub * 8 + e * 4 + c] = v << (8 - bits)
            if pk == 1:
                pb[sub] = [val[sub][0][0] & 1, val[sub][1][0] & 1]
            elif pk == 2:
                pb[sub] = [val[sub][0][0] & 1] * 2
                if (val[sub][1][0] & 1) != pb[sub][0]:
                    st |= _B7_PBIT
        for sub in range(ns, 3):
            sep[sub * 8:sub * 8 + 8] = sep[0:8]
        eps[i] = sep
        b1 = _B7_IB[mode]
        two = mode in (4, 5)
        s1, s2, six = [0] * 16, [0] * 16, [0] * 32
        for p in range(16):
            y, x = p >> 2, p & 3
            el, first = x >> 1, not (x & 1)
            odd = int(first) if y < 3 else int(not first)
            sym = B[rows1[y]] if el else A[rows1[y]]
            s1[p] = ((rix[p] >> (4 - b1)) + _unzig(_deint(sym, odd))) & ((1 << b1) - 1)
            six[p] = s1[p] << (4 - b1)
            if two:
                sym = B[rows2[y]] if el else A[rows2[y]]
                s2[p] = ((rix[16 + p] >> 2) + _unzig(_deint(sym, odd))) & 3
                six[16 + p] = s2[p] << 2
        ixs[i] = six
        if mode == 0 and part == 0 and not any(sep) and not any(s1):
            status[i] = st
            continue                                  # the all-zero block
        sm = p2[part] if ns == 2 else p3[part] if ns == 3 else None
        anchors = (0,) if ns == 1 else (0, a2[part]) if ns == 2 else (0, a3a[part], a3b[part])
        if mode == 4:
            pri, ib, sec, ib2 = s2, 2, s1, 3
        elif mode == 5:
            pri, ib, sec, ib2 = s1, 2, s2, 2
        else:
            pri, ib, sec, ib2 = s1, b1, None, 0
        acc, pos = 0, 0

        def put(v, nbits):
            nonlocal acc, pos
            acc |= (v & ((1 << nbits) - 1)) << pos
            pos += nbits

        put(1 << mode, mode + 1)
        put(part, _B7_PB[mode])
        if mode in (4, 5):
            put(rot, 2)
        if mode == 4:
            put(isel, 1)
        for c in range(3):
            tc = 3 if (rot and c == rot - 1) else c
            for sub in range(ns):
                for e in range(2):
                    v = val[sub][e][tc]
                    put(v >> 1 if (pk and tc == 0) else v, cb)
        if ab:
            tc = rot - 1 if rot else 3
            for sub in range(ns):
                for e in range(2):
                    v = val[sub][e][tc]
                    put(v >> 1 if (pk and tc == 0) else v, ab)
        if pk == 1:
            for sub in range(ns):
                put(pb[sub][0], 1)
                put(pb[sub][1], 1)
        elif pk == 2:
            for sub in range(ns):
                put(pb[sub][0], 1)
        for p in range(16):
            anchor = p == anchors[sm[p] if sm else 0]
            if anchor and pri[p] >> (ib - 1):
                st |= _B7_ANCHOR
            put(pri[p], ib - 1 if anchor else ib)
        if sec is not None:
            for p in range(16):
                if p == 0 and sec[p] >> (ib2 - 1):
                    st |= _B7_ANCHOR
                put(sec[p], ib2 - 1 if p == 0 else ib2)
        if pos != 128:
            st |= _B7_SIGNATURE
        out[16 * i:16 * i + 16] = acc.to_bytes(16, "little")
        status[i] = st
    return bytes(out), status


class _Bc7Params(ctypes.Structure):
    _fields_ = [(name, ctypes.c_void_p) for name in ("cfg", "rank_of", "p2", "p3", "a2", "a3a", "a3b", "pre_ep", "pre_ep_ok", "pre_ix", "pre_ix_ok")]
    _fields_ += [("missing_last", ctypes.c_int32), ("m5_swap", ctypes.c_int32)]


def _bc7_rebuild_native(lib, lanes, n, table):
    return _first_native_call("bc7", lambda: _bc7_rebuild_native_call(lib, lanes, n, table))


def _bc7_rebuild_native_call(lib, lanes, n, table):
    if "native" not in table:
        def arr(ctype, values):
            return (ctype * len(values))(*values)
        flat = lambda rows: [v for row in rows for v in row]
        table["native"] = dict(
            cfg=arr(ctypes.c_uint8, flat(table["config"]) + [255, 255, 0, 0] * (286 - len(table["config"]))),
            rank_of=arr(ctypes.c_int16, table["rank_of"]),
            p2=arr(ctypes.c_uint8, flat(table["partition2"])), p3=arr(ctypes.c_uint8, flat(table["partition3"])),
            a2=arr(ctypes.c_uint8, table["anchor2"]), a3a=arr(ctypes.c_uint8, table["anchor3a"]), a3b=arr(ctypes.c_uint8, table["anchor3b"]),
            pre_ep=arr(ctypes.c_uint8, flat(table["historyEndpoints"])), pre_ix=arr(ctypes.c_uint8, flat(table["historyIndices"])),
            ok=arr(ctypes.c_uint8, [1] * 256))
    c = table["native"]
    addr = ctypes.addressof
    params = _Bc7Params(addr(c["cfg"]), addr(c["rank_of"]), addr(c["p2"]), addr(c["p3"]), addr(c["a2"]), addr(c["a3a"]), addr(c["a3b"]),
                        addr(c["pre_ep"]), addr(c["ok"]), addr(c["pre_ix"]), addr(c["ok"]), 0, 1)
    syms = (ctypes.c_uint16 * (32 * n))()
    for k, lane in enumerate(lanes):
        syms[k * 2 * n:(k + 1) * 2 * n] = lane
    out = (ctypes.c_uint8 * (16 * n))()
    status = (ctypes.c_uint8 * n)()
    roots = (ctypes.c_int16 * (2 * n))()
    lib.bc7_rebuild(syms, ctypes.c_int(n), ctypes.byref(params), out, status,
                    ctypes.cast(roots, ctypes.c_void_p), ctypes.c_void_p(ctypes.addressof(roots) + 2 * n), None)
    return bytes(out), list(status)


def _bc7_blocks(lanes, nblocks, provisional=None, force_python=False):
    """BC7 blocks from the sixteen lanes. `provisional`: a set that collects labels of unconfirmed built-in
    values and signatures the chunk relied on; when it is None those are refused."""
    table = _Store.load_bc7()
    unknown = _bc7_check(lanes, nblocks, table, provisional)
    if unknown:
        # blocks whose chain starts at such an entry are rebuilt with the missing bits as zero: they are undetermined
        l0, l1 = lanes[0], lanes[1]
        er, ir = [0] * nblocks, [0] * nblocks
        for i in range(nblocks):
            di = _inter8(l1[2 * i], l1[2 * i + 1])
            de = (di + _unzig(_inter8(l0[2 * i], l0[2 * i + 1]))) & 255
            er[i] = -1 if de == 0 else (256 + i - de if de > i else er[i - de])
            ir[i] = -1 if di == 0 else (256 + i - di if di > i else ir[i - di])
        counts = [(kinds, k, (er if kinds == "endpoint" else ir).count(k)) for kinds, k in unknown]
        total = sum(c for _, _, c in counts)
        if total > _bc7_loose_limit(nblocks):
            names = ", ".join(f"{kinds} {k}" for kinds, k, _ in counts[:6]) + (", ..." if len(counts) > 6 else "")
            raise ValueError(f"{total} of {nblocks} blocks start from built-in history entries that are not known yet ({names})")
        for kinds, k, count in counts:
            provisional.add(f"{kinds} history {k} not known: {count} blocks undetermined")
    lib = None if force_python else _Store.load_bc7_native()
    data, status = _bc7_rebuild_native(lib, lanes, nblocks, table) if lib is not None else _bc7_rebuild_python(lanes, nblocks, table)
    bad = [i for i, st in enumerate(status) if st]
    if bad:
        what = "an anchor index or shared p-bit came out inconsistent" if status[bad[0]] & (_B7_ANCHOR | _B7_PBIT) else "a signature is outside the table"
        if provisional is None or not (status[bad[0]] & (_B7_ANCHOR | _B7_PBIT)) or not provisional or len(bad) > _bc7_loose_limit(nblocks):
            raise ValueError(f"{len(bad)} of {nblocks} blocks failed the consistency check (first: block {bad[0]}, {what})")
        provisional.add(f"consistency check failed in {len(bad)} blocks")
    return data


def _blocks(kind, lanes, out_size, provisional=None):
    if kind == 0x04:
        return _bc1_blocks(lanes, out_size // 8, provisional)
    if kind == 0x0C:
        return _bc3_blocks(lanes, out_size // 16, provisional)
    if kind == 0x10:
        return _bc4_blocks(lanes, out_size // 8, provisional)
    if kind == 0x14:
        return _bc5_blocks(lanes, out_size // 16, provisional)
    if kind == 0x18:
        return _bc6h_blocks(lanes, out_size // 16, provisional)
    return _bc7_blocks(lanes, out_size // 16, provisional)


def decode(raw, kind, out_size):
    """Return out_size bytes of BC blocks, or raise ValueError when the chunk cannot be decoded exactly."""
    if kind not in KINDS:
        raise ValueError(f"Not a BCPack chunk kind: {kind:#x}")
    if kind not in REBUILD:
        raise ValueError(f"{KINDS[kind]} block rebuild is not known yet (its lane array does decode)")
    lanes, _ = lane_symbols(raw, kind, out_size)
    return _blocks(kind, lanes, out_size)


def decode_provisional(raw, kind, out_size):
    """Like decode(), but also accepts built-in history values that are not confirmed yet.
    Returns (bytes, sorted list of what was relied on: entry numbers for BC1, labels for BC7).
    Blocks that depend on such a value can be slightly off; the caller must label the result as provisional."""
    if kind not in REBUILD:
        raise ValueError(f"{KINDS.get(kind, kind)} block rebuild is not known yet (its lane array does decode)")
    lanes, _ = lane_symbols(raw, kind, out_size)
    used = set()
    data = _blocks(kind, lanes, out_size, used)
    return data, sorted(used)


def first_fragments(raw):
    """First fragment header of the eight lanes that open the stream."""
    if len(raw) < 24:
        raise ValueError("BCPack payload shorter than its first header rows")
    out = []
    for col in range(8):
        h0 = raw[col]
        hdr3 = bool(h0 & 0x20)
        n = raw[8 + col] + 256 * raw[16 + col] + 257 if hdr3 else raw[8 + col] + 1
        out.append({"lane": col, "widthReduction": h0 >> 6, "threeByteHeader": hdr3,
                    "flush": bool(h0 & 0x10), "table": h0 & 15, "inputBytes": n})
    return out


def describe(raw, kind, out_size):
    """Metadata for a BCPack chunk: codec, block count, payload length, first headers, decode status."""
    if kind not in KINDS:
        raise ValueError(f"Not a BCPack chunk kind: {kind:#x}")
    payload = raw.rstrip(b"\0")
    info = {"codec": "BCPack", "format": KINDS[kind], "blocks": out_size // BLOCK[kind], "lanes": len(LANES[kind]),
            "laneWidths": list(LANES[kind]), "payloadBytes": len(payload), "firstFragments": first_fragments(payload)}
    lanes = None
    try:
        lanes, used = lane_symbols(raw, kind, out_size)
        info["laneArray"] = "decoded"
        info["inputBytesUsed"] = used
    except ValueError as error:
        info["laneArray"] = f"failed: {error}"
    if kind not in REBUILD:
        info["decodable"] = False
        info["reason"] = f"{KINDS[kind]} block rebuild is not known yet"
    elif lanes is None:
        info["decodable"] = False
        info["reason"] = info["laneArray"]
    else:
        try:
            _blocks(kind, lanes, out_size)
            info["decodable"] = True
        except ValueError as error:
            used = set()
            try:
                _blocks(kind, lanes, out_size, used)
                info["decodable"] = True
                info["provisionalEntries"] = sorted(used)
                info["reason"] = ("decoded with unconfirmed built-in values: "
                                  f"{', '.join(map(str, sorted(used)))}; dependent blocks can be slightly off")
            except ValueError:
                info["decodable"] = False
                info["reason"] = str(error)
    return info


def chunks(path):
    """Yield (chunk index, kind, payload, uncompressed size) for BCPack chunks of a .ttpf."""
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        head = f.read(0x18)
        if head[:4] != b"RDS5":
            raise ValueError(f"{path}: not an RDS5 .ttpf")
        count = struct.unpack_from("<I", head, 4)[0]
        table = f.read(8 * count)
        entries = [struct.unpack_from("<II", table, 8 * i) for i in range(count)]
        phys = [(lo & 0xFFFFFF) * 256 for lo, hi in entries]
        virt = [((lo >> 30) << 12) | ((hi & 0xFFFFF) << 14) for lo, hi in entries]
        for i in range(count - 1):
            kind = (entries[i][0] >> 24) & 0x3F
            if kind not in KINDS or phys[i] >= size:
                continue
            end = min(phys[i + 1], size) if phys[i + 1] > phys[i] else size
            f.seek(phys[i])
            yield i, kind, f.read(end - phys[i]), virt[i + 1] - virt[i]


def main():
    parser = argparse.ArgumentParser(description="Decode or inspect the BCPack chunks of .ttpf files.")
    parser.add_argument("paths", nargs="*", help=".ttpf files or folders")
    parser.add_argument("--selftest", action="store_true",
                        help="also run the pure-Python lane decoder on two chunks per format and compare")
    args = parser.parse_args()
    files = []
    for p in args.paths:
        files += sorted(glob.glob(os.path.join(p, "*.ttpf"))) if os.path.isdir(p) else [p]
    total = {}
    for path in files:
        for index, kind, payload, size in chunks(path):
            row = total.setdefault(KINDS[kind], {"chunks": 0, "laneArray": 0, "decoded": 0, "provisional": 0, "reasons": {}})
            row["chunks"] += 1
            try:
                lanes, _ = lane_symbols(payload, kind, size)
                row["laneArray"] += 1
                if args.selftest and row["laneArray"] <= 2:
                    slow, _ = lane_symbols(payload, kind, size, force_python=True)
                    if slow != lanes:
                        raise SystemExit(f"{path} chunk {index}: native and Python lane decoders disagree")
                if kind in REBUILD:
                    try:
                        exact = _blocks(kind, lanes, size)
                        row["decoded"] += 1
                        if args.selftest and kind == 0x1C and row["decoded"] <= 2 and _Store.load_bc7_native() is not None:
                            if _bc7_blocks(lanes, size // 16, force_python=True) != exact:
                                raise SystemExit(f"{path} chunk {index}: native and Python BC7 rebuilds disagree")
                    except ValueError:
                        _blocks(kind, lanes, size, set())
                        row["provisional"] += 1
                else:
                    row["reasons"]["block rebuild not known yet"] = row["reasons"].get("block rebuild not known yet", 0) + 1
            except ValueError as error:
                key = str(error).split(" at input byte")[0]
                key = "needs a built-in history entry that is not known" if "history" in key else key[:90]
                row["reasons"][key] = row["reasons"].get(key, 0) + 1
    print(json.dumps({"nativeLibrary": _Store.load_native() is not None, "formats": total}, indent=2))


if __name__ == "__main__":
    main()
