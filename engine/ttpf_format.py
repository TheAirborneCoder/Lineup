# SPDX-License-Identifier: GPL-3.0-or-later
"""Read RDS5 texture packs (data/ttdb/**/*.ttpf). Python standard library, numpy and Pillow.

Container
    0x00  "RDS5"
    0x04  u32  chunk-table entries (data chunks, one terminator, then zero fill up to the index)
    0x08  u40  virtual (decompressed) stream size, then u8 0x16 (format version)
    0x10  u32  ETPF index offset in 256-byte units
    0x18  u64  chunk entries:
          bits  0..23  physical offset in 256-byte units
          bits 24..29  codec: 0x20 zlib; 0x00 stored; otherwise (BCn << 2) BCPack (BC1, BC3, BC4, BC5, BC6H, BC7)
          bits 30..31  virtual offset bits 12..13
          bits 32..51  virtual offset bits 14..33
          bits 52..63  per-chunk value (unresolved, kept raw)
    The terminator entry points at end of file and at the virtual stream size.

Virtual stream
    [0, first chunk) is the ETPF index, stored uncompressed.
    The chunks decompress back to back. Textures follow in index order of their location.

ETPF index
    "ETPF", u32 version (634), u32 count, u32 size field
    count x 16 bytes: u32 shape, u32 format, u64 location (bits 0..20 = payload page of 4 KiB)
    count x u64 name hashes (ascending; entry i has hash i)
    an unresolved region up to 16 + size field
    count x 32 bytes per entry (unresolved digest; not SHA-256/SHA3/BLAKE2s of the payload)

Descriptor
    shape   bits 0..13 width - 1, bits 14..27 height - 1, bits 28..31 declared levels
    format  bits 0..2 mode bits; bits 3..9 DXGI value; bits 10..20 slices - 1; bits 21..23 dimension
            (1 = 2D, 2 = cube, 3 = volume, 5 = 2D array); bits 24..26 tile mode; bit 27 LOD-group layout;
            bit 28 a flag that does not change the layout
    The texture holds declared levels + mode bits mips (at least one).

Texture payload
    A surface is GFX10: with more than one mip, the mips that fit half a swizzle block share one mip-tail
    block at offset 0 and the larger mips follow from smallest to largest, each padded to whole swizzle
    blocks. A one-mip surface has no tail: mip 0 starts at offset 0. Tail mip i sits at byte offset
    block/2^(i+1) until that reaches 2 KiB, then at 1536, 1280, 1024, ...
    Swizzle blocks are 64 KiB when the tile mode is 0 and 4 KiB for tile modes 1..3.
    Cube maps store one complete surface per face, face after face. 2D arrays are stored mip by mip: first the
    mip-tail block of every slice, then each larger mip of every slice, from the smallest mip to the largest
    (mip agreement in 125 of 126 arrays with mips: about 1 against 60 for the other order; arrayorder.py).
    LOD-group layout (cube maps; tile mode G = 1..4 is then the number of groups, blocks are 4 KiB): G groups
    from the smallest to the full size. Group g holds the cube at width >> (G - 1 - g) with that many fewer
    mips, as six face surfaces one after another. A group of 128 KiB or more is padded to whole 64 KiB units.
    The smaller groups repeat the lower mips of the full-size group.
    Volume textures (format bits 10..20 hold depth - 1) use thick blocks: one 4 KiB block holds 8 x 8 x 8
    eight-byte pixels or 8 x 16 x 16 two-byte pixels (the block shapes of AMD's public addrlib), blocks run in
    x, then y, then z, and depth that does not fill the last block layer is padding.
    These rules reproduce the space every texture occupies in all packs (352,461 descriptors).
"""

import bisect
import hashlib
import io
import math
import mmap
import os
import struct
import threading
import zlib
from functools import lru_cache
from pathlib import Path

import numpy as np

MAGIC = b"RDS5"
PAGE = 256
CODEC_ZLIB = 0x20
CODEC_STORED = 0x00

# DXGI code -> (name, element bytes, element pixels, Pillow decode)
DXGI = {
    2: ("R32G32B32A32_FLOAT", 16, 1, ("hdr", "f32", 4)),
    10: ("R16G16B16A16_FLOAT", 8, 1, ("hdr", "f16", 4)),
    11: ("R16G16B16A16_UNORM", 8, 1, ("u16", 4)),
    24: ("R10G10B10A2_UNORM", 4, 1, ("rgb10a2",)),
    28: ("R8G8B8A8_UNORM", 4, 1, ("raw", "RGBA", "RGBA")),
    29: ("R8G8B8A8_UNORM_SRGB", 4, 1, ("raw", "RGBA", "RGBA")),
    34: ("R16G16_FLOAT", 4, 1, ("hdr", "f16", 2)),
    41: ("R32_FLOAT", 4, 1, ("hdr", "f32", 1)),
    49: ("R8G8_UNORM", 2, 1, ("rg8",)),
    54: ("R16_FLOAT", 2, 1, ("hdr", "f16", 1)),
    56: ("R16_UNORM", 2, 1, ("u16", 1)),
    61: ("R8_UNORM", 1, 1, ("raw", "L")),
    65: ("A8_UNORM", 1, 1, ("raw", "L")),
    71: ("BC1_UNORM", 8, 4, ("bcn", 1, "RGBA")),
    72: ("BC1_UNORM_SRGB", 8, 4, ("bcn", 1, "RGBA")),
    74: ("BC2_UNORM", 16, 4, ("bcn", 2, "RGBA")),
    75: ("BC2_UNORM_SRGB", 16, 4, ("bcn", 2, "RGBA")),
    77: ("BC3_UNORM", 16, 4, ("bcn", 3, "RGBA")),
    78: ("BC3_UNORM_SRGB", 16, 4, ("bcn", 3, "RGBA")),
    80: ("BC4_UNORM", 8, 4, ("bcn", 4, "L")),
    81: ("BC4_SNORM", 8, 4, None),
    83: ("BC5_UNORM", 16, 4, ("bcn", 5, "RGB")),
    84: ("BC5_SNORM", 16, 4, None),
    87: ("B8G8R8A8_UNORM", 4, 1, ("raw", "RGBA", "BGRA")),
    91: ("B8G8R8A8_UNORM_SRGB", 4, 1, ("raw", "RGBA", "BGRA")),
    95: ("BC6H_UF16", 16, 4, ("hdr", "bc6h", False)),
    96: ("BC6H_SF16", 16, 4, ("hdr", "bc6h", True)),
    98: ("BC7_UNORM", 16, 4, ("bcn", 7, "RGBA")),
    99: ("BC7_UNORM_SRGB", 16, 4, ("bcn", 7, "RGBA")),
}

# Element order inside one swizzle block, least significant address bit first. Each entry was
# confirmed by rendering textures of that class and by mip-to-mip agreement (see --verify).
SWIZZLE = {
    (16, 16): "x0 y0 x1 y1 y2 x2 y3 x3 y4 x4 y5 x5",
    (16, 12): "y0 y1 x0 x1 y2 x2 y3 x3",
    (8, 16): "x0 y0 x1 x2 y1 y2 x3 y3 x4 y4 x5 y5 x6",
    (8, 12): "x0 y0 y1 x1 x2 y2 x3 y3 x4",
    # 4-byte pixels: found by smoothness over every axis-ordered interleaving, then told from its mirror image
    # by multi-block textures (smoother in 119 of 120) and by mip agreement (0.2 to 1.0 against 5.6 to 13.4).
    (4, 16): "x0 x1 y0 y1 x2 y2 y3 x3 y4 x4 y5 x5 y6 x6",
    (4, 12): "x0 x1 y0 y1 y2 x2 y3 x3 y4 x4",
    # 2-byte pixels (R16_FLOAT): smoothest of every axis-ordered interleaving in 42 of 61 and 21 of 25 blocks,
    # roughness 1.04 against 1.38 and 1.00 against 1.29 for the runner-up (research script, not shipped).
    (2, 16): "x0 x1 x2 y0 y1 y2 x3 y3 x4 y4 x5 y5 x6 y6 x7",
    (2, 12): "x0 x1 x2 y0 y1 y2 x3 y3 x4 y4 x5",
    # 1-byte elements (2026-11-22): A8 texture 566 of ttdb_weapons stores its 4x4 pixels at x + 16*y (x0 x1 y0 y1).
    # 4 KiB: smoothest of all 924 axis-ordered orders in 50 of 61 blocks (research script, not shipped), and mip 1
    # equals mip 0 box-reduced to 0.08 levels on average against 31+ for every other order of the high bits
    # (swizmip1.py, 40 textures). 64 KiB: the distance each address bit moves (mean pixel change per flipped bit)
    # groups the bits by level; of the 256 x/y assignments, this one gives mip agreement 0.12 (next best 6.5),
    # best in 20 of 21 textures. Its low 12 bits differ from the 4 KiB order.
    (1, 12): "x0 x1 x2 x3 y0 y1 y2 y3 y4 x4 y5 x5",
    (1, 16): "x0 x1 x2 y1 y0 y2 x3 y3 y4 x4 y5 x5 y6 x6 y7 x7",
}
# Coordinate bits confirmed by data for each class; None means every bit is confirmed.
CONFIRMED = {}
# Volume textures: (element bytes, block log2) -> (block width, height, depth in elements, element order).
# Found by smoothness over every block shape that fills the occupied size and every axis-ordered order
# (research script, not shipped); the two-byte order also by the padding bit (z3) and by the step size doubling
# from bit to bit along each axis (volcheck.py).
SWIZZLE3D = {
    # GFX10_SW_4K_S3_PATINFO in AMD's gfx10SwizzlePattern.h.
    (1, 12): ((16, 16, 16), "x0 x1 z0 y0 z1 y1 x2 z2 y2 x3 z3 y3"),
    (4, 12): ((8, 16, 8), "x0 y0 z0 y1 x1 z1 y2 x2 z2 y3"),
    (16, 12): ((4, 8, 8), "z0 y0 x0 z1 y1 x1 z2 y2"),
    (8, 12): ((8, 8, 8), "x0 z0 y0 x1 z1 y1 x2 z2 y2"),
    (2, 12): ((8, 16, 16), "x0 z0 y0 z1 y1 x1 z2 y2 x2 z3 y3"),
}

DIMENSIONS = {1: "2D", 2: "cube", 3: "volume", 5: "2D array"}
GROUP_UNIT = 1 << 16          # a LOD group of GROUP_PAD_FROM bytes or more is padded to whole units of this size
GROUP_PAD_FROM = 32 << 12

FIELD_HELP = {
    "nameHash": "64-bit name hash from the ETPF hash array. Original names are not stored in the pack.",
    "width": "Shape bits 0..13 plus one.",
    "height": "Shape bits 14..27 plus one.",
    "declaredLevels": "Shape bits 28..31. Added to the mode bits it gives the number of mips the texture holds.",
    "dxgi": "Format bits 3..9: a DXGI_FORMAT value.",
    "modeBits": "Format bits 0..2. Added to the declared levels it gives the number of mips: 0 on small textures, 1 and 3 on larger ones.",
    "mipCount": "Declared levels + mode bits, at least 1. Slots of the mip-tail block past this count hold no data.",
    "slices": "Format bits 10..20 plus one: cube faces (6), array slices, or the depth of a volume texture. A face or array slice is a complete surface with its own mips; the depth slices of a volume share its blocks.",
    "depth": "Volume textures: number of depth slices (format bits 10..20 plus one).",
    "volumeBlock": "Volume textures: pixels one 4 KiB block holds, as width x height x depth. Blocks run in x, then y, then z.",
    "display": "Float formats: how Studio shows the values. Picture = (value - black) x exposure, clipped to 0..1. The DDS holds the exact values.",
    "dimension": "Format bits 21..23: 1 = 2D, 2 = cube, 3 = volume, 5 = 2D array.",
    "formatBits10": "Format bits 10..23 as stored: slices - 1 in the low 11 bits, then the dimension. A plain 2D texture reads 0x800, a cube 0x1005.",
    "layoutClass": "Format bits 24..28 as stored: tile mode, +8 for the LOD-group layout, +16 for a flag that does not change the layout.",
    "tileMode": "Format bits 24..26. 0 = 64 KiB swizzle blocks; 1, 2 and 3 = 4 KiB blocks. In the LOD-group layout it is the number of groups.",
    "lodGroups": "LOD-group layout (format bit 27): the payload holds this many copies of the cube from the smallest to the full size, each with its own mips. The smaller groups repeat the lower mips of the full-size one. 0 = one surface.",
    "sliceStride": "Cube maps: bytes from one face of the full-size surface to the next. 2D arrays are stored mip by mip, so their slices have no single stride.",
    "sliceOrder": "How the slices are stored: 'surface' = one complete surface (all mips) per face, face after face (cube maps); 'mip' = mip by mip, each mip holding every slice (2D arrays).",
    "surfaceBase": "Byte offset of the full-size surface inside the payload (after the smaller LOD groups).",
    "hdr": "HDR format: the pixels are real values, not 0..255. Studio shows value x exposure clipped to 0..1; the exposure brings the 99.9th percentile of the measured mip to 1. The DDS holds the exact values.",
    "layoutWord": "Location bits 21..63 (unresolved). Zero for 64 KiB layouts; varies with shape and format otherwise.",
    "virtualOffset": "Location bits 0..20 x 4096: start of the payload in the decompressed stream (21 bits, so packs can exceed 4 GiB).",
    "virtualSize": "Distance to the next payload. It must equal the size computed from the descriptor.",
}


def align(value, unit):
    return (value + unit - 1) // unit * unit


def block_dims(elem_bytes, block_log2):
    e = block_log2 - int(math.log2(elem_bytes))
    return 1 << ((e + 1) // 2), 1 << (e // 2)


def tail_dims(bw, bh, block_log2):
    return (bw, bh >> 1) if block_log2 & 1 else (bw >> 1, bh)


@lru_cache(maxsize=16)
def swizzle_maps(elem_bytes, block_log2):
    order = SWIZZLE[(elem_bytes, block_log2)].split()
    n = 1 << len(order)
    a = np.arange(n)
    x = np.zeros(n, np.int64)
    y = np.zeros(n, np.int64)
    for bit, axis in enumerate(order):
        value = (a >> bit) & 1
        if axis[0] == "x":
            x |= value << int(axis[1:])
        else:
            y |= value << int(axis[1:])
    return x, y


@lru_cache(maxsize=8)
def swizzle_maps3(elem_bytes, block_log2):
    """(x, y, z) of every element of one thick block, in address order."""
    order = SWIZZLE3D[(elem_bytes, block_log2)][1].split()
    n = 1 << len(order)
    a = np.arange(n)
    coords = {axis: np.zeros(n, np.int64) for axis in "xyz"}
    for bit, name in enumerate(order):
        coords[name[0]] |= ((a >> bit) & 1) << int(name[1:])
    return coords["x"], coords["y"], coords["z"]


def unswizzle3(data, width, height, depth, elem_bytes, block_log2):
    """Volume payload -> one linear payload per depth slice."""
    (bw, bh, bd), _ = SWIZZLE3D[(elem_bytes, block_log2)]
    nbx, nby, nbz = math.ceil(width / bw), math.ceil(height / bh), math.ceil(depth / bd)
    per_block = (1 << block_log2) // elem_bytes
    need = nbx * nby * nbz << block_log2
    if len(data) < need:
        raise ValueError("Volume payload shorter than its blocks")
    arr = np.frombuffer(data, np.uint8, need).reshape(nbx * nby * nbz, per_block, elem_bytes)
    x, y, z = swizzle_maps3(elem_bytes, block_log2)
    b = np.arange(nbx * nby * nbz)
    out = np.empty((nbz * bd, nby * bh, nbx * bw, elem_bytes), np.uint8)
    out[(b // (nbx * nby))[:, None] * bd + z[None], (b // nbx % nby)[:, None] * bh + y[None],
        (b % nbx)[:, None] * bw + x[None]] = arr
    return [np.ascontiguousarray(out[k, :height, :width]).tobytes() for k in range(depth)]


def tail_offset(index, block_log2):
    """Byte offset of the index-th mip inside the tail block: halves down to 2 KiB, then 256-byte steps."""
    j = block_log2 - 12
    if index <= j:
        return 1 << (block_log2 - 1 - index)
    step = 7 - (index - j)
    if step < 0:
        raise ValueError("Mip tail holds more levels than its slots")
    return 256 * step


def surface_layout(width, height, elem_bytes, px, block_log2, nmips=1):
    """Return (total bytes, mips) of one surface that holds nmips levels.

    Each mip: level, offset, size, element and pixel sizes, tail placement. A one-mip surface has no mip
    tail: mip 0 starts at offset 0. With more mips, those that fit the tail dimensions share the tail block
    at offset 0 and the larger ones follow from smallest to largest."""
    bw, bh = block_dims(elem_bytes, block_log2)
    tw, th = tail_dims(bw, bh, block_log2)
    block = 1 << block_log2
    chain = []
    for level in range(max(1, nmips)):
        w, h = max(1, width >> level), max(1, height >> level)
        chain.append(((w + px - 1) // px, (h + px - 1) // px, w, h))
    first_tail = len(chain)
    if len(chain) > 1:
        first_tail = next((i for i, (ew, eh, _, _) in enumerate(chain) if ew <= tw and eh <= th), len(chain))
    offset = block if first_tail < len(chain) else 0
    mips = [None] * len(chain)
    for i in range(first_tail - 1, -1, -1):
        ew, eh, w, h = chain[i]
        size = math.ceil(ew / bw) * math.ceil(eh / bh) * block
        mips[i] = dict(level=i, offset=offset, size=size, ew=ew, eh=eh, width=w, height=h, inTail=False, located=True)
        offset += size
    for i in range(first_tail, len(chain)):
        ew, eh, w, h = chain[i]
        entry = dict(level=i, offset=0, size=block, ew=ew, eh=eh, width=w, height=h, inTail=True, located=False)
        if (elem_bytes, block_log2) in SWIZZLE:
            try:
                byte = tail_offset(i - first_tail, block_log2)
            except ValueError:
                byte = None
            if byte is not None:
                xs, ys = swizzle_maps(elem_bytes, block_log2)
                element = byte // elem_bytes
                tx, ty = int(xs[element]), int(ys[element])
                if tx + ew <= bw and ty + eh <= bh:
                    entry.update(located=True, tailByteOffset=byte, tailX=tx, tailY=ty)
        mips[i] = entry
    return offset, mips


def unswizzle(data, ew, eh, elem_bytes, block_log2):
    bw, bh = block_dims(elem_bytes, block_log2)
    x, y = swizzle_maps(elem_bytes, block_log2)
    n = bw * bh
    bpr, bpc = math.ceil(ew / bw), math.ceil(eh / bh)
    need = bpr * bpc * n * elem_bytes
    if len(data) < need:
        raise ValueError("Mip payload shorter than its swizzle blocks")
    arr = np.frombuffer(data, np.uint8, need).reshape(bpr * bpc, n, elem_bytes)
    out = np.empty((bpc * bh, bpr * bw, elem_bytes), np.uint8)
    b = np.arange(bpr * bpc)
    rows = (b // bpr)[:, None] * bh + y[None, :]
    cols = (b % bpr)[:, None] * bw + x[None, :]
    out[rows, cols] = arr
    return out[:eh, :ew].tobytes()


def is_hdr(dxgi):
    spec = DXGI.get(dxgi)
    return bool(spec and spec[3] and spec[3][0] == "hdr")


def decode_values(linear, dxgi, width, height):
    """Float pixel values [height, width, channels] of a float format: BC6H (3 channels, decoded by bc6h.py),
    R16_FLOAT (1), R16G16B16A16_FLOAT and R32G32B32A32_FLOAT (4)."""
    kind = DXGI[dxgi][3]
    if kind[1] == "bc6h":
        import bc6h

        return bc6h.decode(linear, width, height, kind[2])
    dtype = np.float16 if kind[1] == "f16" else np.float32
    return np.frombuffer(linear, dtype, width * height * kind[2]).reshape(height, width, kind[2]).astype(np.float32)


def display_range(dxgi, values):
    """(black, exposure) for showing float values [..., channels]: picture = (value - black) x exposure, clipped
    to 0..1. Values that already fill 0..1 are shown as they are. Otherwise the 99.9th percentile of the brightest
    channel becomes white; with negative values the 0.1th percentile of the darkest channel becomes black."""
    kind = DXGI[dxgi][3]
    if kind[1] == "bc6h":
        import bc6h

        return 0.0, bc6h.exposure_for(values.reshape(-1, values.shape[-2], values.shape[-1]))
    finite = np.nan_to_num(values, nan=0.0, posinf=65504.0, neginf=-65504.0).reshape(-1, values.shape[-1])
    if not finite.size:
        return 0.0, 1.0
    top = float(np.percentile(finite.max(axis=1), 99.9))
    low = float(np.percentile(finite.min(axis=1), 0.1))
    black = low if low < 0 else 0.0
    if top <= black:
        return black, 1.0
    if black == 0.0 and 0.25 <= top <= 1.0:
        return 0.0, 1.0
    return black, 1.0 / (top - black)


def decode_pixels(linear, dxgi, width, height, exposure=None, black=0.0):
    """Decode a linear mip to a Pillow image. Raises ValueError for unsupported formats.

    Float formats are shown as (value - black) x exposure clipped to 0..1 (exposure 1 when none is given):
    one channel as grey, three as RGB, four as RGBA with the fourth channel scaled like the others."""
    from PIL import Image

    spec = DXGI.get(dxgi)
    if not spec or not spec[3]:
        raise ValueError(f"No pixel decoder for DXGI format {dxgi}")
    if spec[3][0] == "hdr":
        values = decode_values(linear, dxgi, width, height)
        finite = np.nan_to_num(values, nan=0.0, posinf=65504.0, neginf=0.0 if spec[3][1] == "bc6h" else -65504.0)
        shown = (np.clip((finite - black) * (exposure or 1.0), 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)
        if shown.shape[2] == 2:
            return Image.fromarray(np.dstack((shown, np.zeros_like(shown[..., 0]))))
        if shown.shape[2] == 1:
            return Image.fromarray(shown[..., 0], "L")
        return Image.fromarray(shown, "RGB" if shown.shape[2] == 3 else "RGBA")
    if spec[3][0] == "u16":
        channels = spec[3][1]
        wide = np.frombuffer(linear, "<u2").reshape(height, width, channels)
        shown = ((wide.astype(np.uint32) * 255 + 32767) // 65535).astype(np.uint8)
        return Image.fromarray(shown[..., 0] if channels == 1 else shown)
    if spec[3][0] == "rg8":
        rg = np.frombuffer(linear, np.uint8).reshape(height, width, 2)
        return Image.fromarray(np.dstack((rg, np.zeros_like(rg[..., 0]))))
    if spec[3][0] == "rgb10a2":
        words = np.frombuffer(linear, "<u4").reshape(height, width)
        rgb = [(((words >> shift) & 1023) * 255 + 511) // 1023 for shift in (0, 10, 20)]
        return Image.fromarray(np.stack([*rgb, (words >> 30) * 85], axis=2).astype(np.uint8))
    if spec[3][0] == "raw":
        return Image.frombytes(spec[3][1], (width, height), linear, "raw", spec[3][-1])
    _, n, mode = spec[3][:3]
    return Image.frombytes(mode, (width, height), linear, "bcn", (n,) + tuple(spec[3][3:]))


def dds_bytes(dxgi, width, height, mips, cube=False, volume=False):
    """A DX10 DDS. mips is a list of linear mip payloads (mip 0 first), or one such list per array slice or
    cube face (slice after slice, as DDS stores arrays). volume: the slices are the depth slices of a
    one-mip volume texture and the file is a DDS volume."""
    slices = mips if isinstance(mips[0], (list, tuple)) else [mips]
    first = slices[0]
    _, elem, px, _ = DXGI[dxgi]
    block = px > 1
    pitch = max(1, (width + px - 1) // px) * elem
    if volume and len(first) != 1:
        raise ValueError("A volume DDS with more than one mip is not supported")
    flags = 0x1 | 0x2 | 0x4 | 0x1000 | 0x20000 | (0x80000 if block else 0x8) | (0x800000 if volume else 0)
    header = struct.pack("<4s7I44x", b"DDS ", 124, flags, height, width,
                         len(first[0]) if block else pitch, len(slices) if volume else 0, len(first))
    pixel = struct.pack("<2I4s5I", 32, 0x4, b"DX10", 0, 0, 0, 0, 0)
    complex_surface = len(first) > 1 or len(slices) > 1
    caps = struct.pack("<4I4x", 0x1000 | (0x400000 if len(first) > 1 else 0) | (0x8 if complex_surface else 0),
                       0x200000 if volume else 0xFE00 if cube else 0, 0, 0)
    dx10 = struct.pack("<5I", dxgi, 4 if volume else 3, 0x4 if cube else 0,
                       1 if volume else len(slices) // 6 if cube else len(slices), 0)
    return header + pixel + caps + dx10 + b"".join(b"".join(s) for s in slices)


def slice_sheet(images, cube=False, gap=0):
    """One picture of all slices: a horizontal cross for a cube (+X -X +Y -Y +Z -Z order), a grid for an array."""
    from PIL import Image

    w, h = images[0].size
    if cube and len(images) == 6:
        cells = {0: (2, 1), 1: (0, 1), 2: (1, 0), 3: (1, 2), 4: (1, 1), 5: (3, 1)}
        cols, rows = 4, 3
    else:
        cols = math.ceil(math.sqrt(len(images)))
        rows = math.ceil(len(images) / cols)
        cells = {k: (k % cols, k // cols) for k in range(len(images))}
    sheet = Image.new("RGBA", (cols * w + (cols - 1) * gap, rows * h + (rows - 1) * gap), (0, 0, 0, 0))
    for k, image in enumerate(images):
        x, y = cells[k]
        sheet.paste(image.convert("RGBA"), (x * (w + gap), y * (h + gap)))
    return sheet


class Chunk:
    __slots__ = ("index", "phys", "phys_end", "virt", "virt_end", "codec", "extra")

    def __init__(self, index, phys, phys_end, virt, virt_end, codec, extra):
        self.index, self.phys, self.phys_end = index, phys, phys_end
        self.virt, self.virt_end, self.codec, self.extra = virt, virt_end, codec, extra

    @property
    def codec_name(self):
        if self.codec == CODEC_ZLIB:
            return "zlib"
        if self.codec == CODEC_STORED:
            return "stored"
        return f"BCPack BC{self.codec >> 2}"


def bcpack_module():
    try:
        import bcpack
    except ImportError:
        return None
    return bcpack


def bcpack_decoder():
    """The BCPack decode function, only when bcpack.py declares AVAILABLE = True."""
    module = bcpack_module()
    if module is None or not getattr(module, "AVAILABLE", False):
        return None
    return getattr(module, "decode", None)


class TtpfArchive:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.file = open(self.path, "rb")
        self.size = os.fstat(self.file.fileno()).st_size
        self.map = mmap.mmap(self.file.fileno(), 0, access=mmap.ACCESS_READ)
        self.lock = threading.Lock()
        self.bcpack = bcpack_decoder()
        module = bcpack_module() if self.bcpack is not None else None
        self.bcpack_provisional = getattr(module, "decode_provisional", None)
        self.provisional = {}       # chunk index -> unconfirmed built-in table entries its decode relied on
        self._parse()

    def close(self):
        self.map.close()
        self.file.close()

    def _parse(self):
        m = self.map
        if m[:4] != MAGIC:
            raise ValueError(f"{self.path.name}: not an RDS5 texture pack")
        self.entry_count, = struct.unpack_from("<I", m, 4)
        raw_size, = struct.unpack_from("<Q", m, 8)
        self.virtual_size = raw_size & 0xFFFFFFFFFF
        self.version = raw_size >> 56
        self.header_flags = (raw_size >> 40) & 0xFFFF
        self.etpf_offset = struct.unpack_from("<I", m, 0x10)[0] * PAGE
        self.header_reserved, = struct.unpack_from("<I", m, 0x14)
        table_end = 0x18 + 8 * self.entry_count
        words = []
        for i in range(self.entry_count):
            at = 0x18 + 8 * i
            if at + 8 > self.etpf_offset:
                break
            words.append(struct.unpack_from("<II", m, at))
        chunks = []
        terminator = None
        for i, (lo, hi) in enumerate(words):
            phys = (lo & 0xFFFFFF) * PAGE
            virt = ((lo >> 30) << 12) | ((hi & 0xFFFFF) << 14)
            if phys >= self.size:
                terminator = (i, phys, virt)
                break
            chunks.append([i, phys, virt, (lo >> 24) & 0x3F, hi >> 20])
        if terminator is None or terminator[1] != self.size or terminator[2] != self.virtual_size:
            raise ValueError(f"{self.path.name}: chunk table terminator does not close the archive")
        trailing = m[0x18 + 8 * (terminator[0] + 1):min(table_end, self.etpf_offset)]
        if any(trailing):
            raise ValueError(f"{self.path.name}: nonzero bytes after the chunk table terminator")
        self.chunks = []
        for k, (i, phys, virt, codec, extra) in enumerate(chunks):
            nxt = chunks[k + 1] if k + 1 < len(chunks) else [None, self.size, self.virtual_size]
            self.chunks.append(Chunk(i, phys, nxt[1], virt, nxt[2], codec, extra))
        self.chunk_starts = [c.virt for c in self.chunks]
        if any(m[table_end:self.etpf_offset]) and table_end < self.etpf_offset:
            raise ValueError(f"{self.path.name}: nonzero bytes between chunk table and index")
        first = self.chunks[0]
        stored = first.phys - self.etpf_offset
        if first.virt > stored or any(m[self.etpf_offset + first.virt:first.phys]):
            raise ValueError(f"{self.path.name}: index size differs from the first chunk's virtual offset")
        # The index occupies the virtual stream up to the first chunk; the file may pad it further.
        self.index_bytes = bytes(m[self.etpf_offset:self.etpf_offset + first.virt])
        self.index_padding = stored - first.virt
        e = self.index_bytes
        if e[:4] != b"ETPF":
            raise ValueError(f"{self.path.name}: ETPF index missing")
        self.index_version, self.count, self.index_size_field = struct.unpack_from("<III", e, 4)
        n = self.count
        self.entries = [struct.unpack_from("<IIQ", e, 16 + 16 * i) for i in range(n)]
        self.hashes = struct.unpack_from(f"<{n}Q", e, 16 + 16 * n)
        self.middle_offset = 16 + 24 * n
        self.digest_offset = 16 + self.index_size_field
        self.digests = [e[self.digest_offset + 32 * i:self.digest_offset + 32 * i + 32] for i in range(n)]
        self.index_used = self.digest_offset + 32 * n
        if self.index_used > len(e) or any(e[self.index_used:]):
            raise ValueError(f"{self.path.name}: unexpected bytes after the ETPF digest array")
        self.textures = [self._texture(i) for i in range(n)]
        order = sorted(range(n), key=lambda i: self.textures[i]["virtualOffset"])
        for k, i in enumerate(order):
            end = self.textures[order[k + 1]]["virtualOffset"] if k + 1 < n else self.virtual_size
            t = self.textures[i]
            t["virtualSize"] = end - t["virtualOffset"]
            t["sizeMatches"] = t["virtualSize"] == t["computedSize"]
        if order and self.textures[order[0]]["virtualOffset"] != len(e):
            raise ValueError(f"{self.path.name}: first texture does not follow the index")

    def _texture(self, i):
        shape, fmt, loc = self.entries[i]
        dxgi = (fmt >> 3) & 0x7F
        layout_class = fmt >> 24
        tile, grouped = layout_class & 7, bool(layout_class & 8)
        dimension = (fmt >> 21) & 7
        slices = ((fmt >> 10) & 0x7FF) + 1
        count = max(1, (shape >> 28) + (fmt & 7))
        spec = DXGI.get(dxgi)
        t = dict(index=i, nameHash=f"{self.hashes[i]:016x}", shapeWord=f"0x{shape:08x}", formatWord=f"0x{fmt:08x}",
                 locationWord=f"0x{loc:016x}", width=(shape & 0x3FFF) + 1, height=((shape >> 14) & 0x3FFF) + 1,
                 declaredLevels=shape >> 28, dxgi=dxgi, format=spec[0] if spec else f"DXGI_{dxgi}",
                 modeBits=fmt & 7, mipCount=count, slices=slices,
                 dimension=DIMENSIONS.get(dimension, f"type {dimension}"),
                 formatBits10=f"0x{(fmt >> 10) & 0x3FFF:x}", layoutClass=layout_class, tileMode=tile,
                 lodGroups=tile if grouped else 0, layoutWord=f"0x{loc >> 21:x}",
                 virtualOffset=(loc & 0x1FFFFF) << 12, digest=self.digests[i].hex())
        reason = None
        block_log2 = 12 if grouped or tile else 16
        if not spec:
            reason = f"DXGI format {dxgi} is not in the reader's format table"
        elif dimension not in DIMENSIONS or layout_class >> 5 or grouped and not 1 <= tile <= 4:
            reason = "Descriptor uses layout bits the reader has not met"
        elif dimension == 3 and (grouped or count != 1 or (spec[1], block_log2) not in SWIZZLE3D):
            reason = (f"Volume texture with {count} mips of {spec[1]}-byte elements in {1 << block_log2}-byte blocks: "
                      "only one-mip volumes of 2- and 8-byte pixels in 4 KiB blocks occur in the packs and are resolved")
        if reason:
            t.update(computedSize=None, blockSize=None, mips=[], surfaceBase=0, sliceStride=0, lodGroupTable=[],
                     layoutNote=reason)
            return t
        if dimension == 3:
            # Thick blocks: the depth slices share the blocks, so the whole volume is one payload.
            (bw, bh, bd), order = SWIZZLE3D[(spec[1], block_log2)]
            w, h = t["width"], t["height"]
            total = math.ceil(w / bw) * math.ceil(h / bh) * math.ceil(slices / bd) << block_log2
            mips = [dict(level=0, offset=0, size=total, ew=w, eh=h, width=w, height=h, inTail=False, located=True)]
            t.update(computedSize=total, blockSize=1 << block_log2, mips=mips, surfaceBase=0, sliceStride=0,
                     lodGroupTable=[], swizzle=order, depth=slices, volumeBlock=f"{bw} x {bh} x {bd}")
            return t
        groups, base = [], 0
        if grouped:
            # Smallest group first; the full-size group is the last one and is the texture's own surface.
            for g in range(tile):
                k = tile - 1 - g
                w, h = max(1, t["width"] >> k), max(1, t["height"] >> k)
                stride, mips = surface_layout(w, h, spec[1], spec[2], block_log2, max(1, count - k))
                used = stride * slices
                size = align(used, GROUP_UNIT) if used >= GROUP_PAD_FROM else used
                groups.append(dict(group=g, firstMip=k, width=w, height=h, mips=len(mips), offset=base,
                                   sliceStride=stride, used=used, size=size))
                base += size
            total, base = base, groups[-1]["offset"]
        else:
            stride, mips = surface_layout(t["width"], t["height"], spec[1], spec[2], block_log2, count)
            total = stride * slices
        for m in mips:
            m["offset"] += base
        t.update(computedSize=total, blockSize=1 << block_log2, mips=mips, surfaceBase=base, sliceStride=stride,
                 lodGroupTable=groups, swizzle=SWIZZLE.get((spec[1], block_log2)),
                 sliceOrder="mip" if dimension == 5 and slices > 1 else "surface")
        return t

    # Virtual stream -------------------------------------------------------------------------
    def chunk_range(self, start, end):
        i = bisect.bisect_right(self.chunk_starts, start) - 1
        j = bisect.bisect_left(self.chunk_starts, end)
        return self.chunks[max(i, 0):j]

    def chunk_payload(self, chunk):
        return bytes(self.map[chunk.phys:chunk.phys_end])

    @lru_cache(maxsize=48)
    def chunk_data(self, index):
        chunk = self.chunks[index]
        raw = self.map[chunk.phys:chunk.phys_end]
        size = chunk.virt_end - chunk.virt
        if chunk.codec == CODEC_STORED:
            data = bytes(raw[:size])
            if len(data) != size or any(raw[size:]):
                raise ValueError(f"Chunk {chunk.index}: stored chunk does not match its virtual extent")
            return data
        if chunk.codec == CODEC_ZLIB:
            stream = zlib.decompressobj()
            data = stream.decompress(raw)
            if not stream.eof or len(data) != size or any(stream.unused_data):
                raise ValueError(f"Chunk {chunk.index}: zlib stream does not match its virtual extent")
            return data
        if self.bcpack is None:
            raise LookupError(f"Chunk {chunk.index} uses {chunk.codec_name}; no BCPack decoder is installed")
        try:
            data = self.bcpack(bytes(raw), chunk.codec, size)
        except (ValueError, IndexError, KeyError) as error:
            data = None
            if self.bcpack_provisional is not None:
                try:
                    data, entries = self.bcpack_provisional(bytes(raw), chunk.codec, size)
                    self.provisional[chunk.index] = entries
                except (ValueError, IndexError, KeyError):
                    data = None
            if data is None:
                raise LookupError(f"Chunk {chunk.index} ({chunk.codec_name}): decoder refused it: {error}") from None
        if len(data) != size:
            raise LookupError(f"Chunk {chunk.index} ({chunk.codec_name}): decoder output size differs from its extent")
        return data

    def mip_start(self, texture, level, slice=0):
        """Virtual offset of one mip of one slice (for a tail mip: of that slice's tail block)."""
        if not 0 <= slice < texture["slices"]:
            raise ValueError("Slice outside the texture")
        mip = texture["mips"][level]
        if texture.get("sliceOrder") == "mip":
            # 2D arrays: every part of the surface (tail block, then each mip) holds all slices one after another
            return texture["virtualOffset"] + texture["slices"] * mip["offset"] + slice * mip["size"]
        return texture["virtualOffset"] + mip["offset"] + slice * texture["sliceStride"]

    def mip_chunks(self, texture, level):
        """Chunks that hold this mip, over every slice."""
        size = texture["mips"][level]["size"]
        seen = {}
        for s in range(texture["slices"]):
            start = self.mip_start(texture, level, s)
            for c in self.chunk_range(start, start + size):
                seen[c.index] = c
        return [seen[k] for k in sorted(seen)]

    def mip_codecs(self, texture, level):
        return {c.codec_name for c in self.mip_chunks(texture, level)}

    def read(self, start, size):
        """Return (bytes, blocking chunks). Bytes is None when any covering chunk cannot be decoded."""
        if start < len(self.index_bytes):
            raise ValueError("Range overlaps the index")
        out = bytearray()
        blocked = []
        for chunk in self.chunk_range(start, start + size):
            try:
                data = self.chunk_data(chunk.index)
            except LookupError:
                blocked.append(chunk)
                continue
            lo, hi = max(start, chunk.virt), min(start + size, chunk.virt_end)
            out += data[lo - chunk.virt:hi - chunk.virt]
        if blocked:
            return None, blocked
        if len(out) != size:
            raise ValueError("Virtual range is not covered by chunks")
        return bytes(out), []

    # Textures -------------------------------------------------------------------------------
    def mip_linear(self, texture, level, slice=0):
        """Linear (unswizzled) payload of one mip of one slice, or raise LookupError/ValueError with the reason."""
        if texture.get("depth"):
            return self._volume(texture)[slice]
        return self._linear(texture, texture["mips"][level], self.mip_start(texture, level, slice))

    def _volume(self, texture):
        """Linear payloads of every depth slice of a volume texture (kept for the texture read last)."""
        cached = getattr(self, "_volume_cache", None)
        if cached is None or cached[0] != texture["index"]:
            data, blocked = self.read(texture["virtualOffset"], texture["computedSize"])
            if data is None:
                raise LookupError(", ".join(f"chunk {c.index} ({c.codec_name})" for c in blocked))
            spec = DXGI[texture["dxgi"]]
            cached = (texture["index"], unswizzle3(data, texture["width"], texture["height"], texture["depth"], spec[1],
                                                   texture["blockSize"].bit_length() - 1))
            self._volume_cache = cached
        return cached[1]

    def group_mips(self, texture, group):
        """Mips of one LOD group's surface; offsets are relative to a slice of that group."""
        g = texture["lodGroupTable"][group]
        spec = DXGI[texture["dxgi"]]
        return surface_layout(g["width"], g["height"], spec[1], spec[2], texture["blockSize"].bit_length() - 1, g["mips"])[1]

    def group_linear(self, texture, group, level, slice=0):
        """Linear payload of mip `level` (counted inside the group) of one slice of a LOD group."""
        g = texture["lodGroupTable"][group]
        mip = self.group_mips(texture, group)[level]
        return self._linear(texture, mip, texture["virtualOffset"] + g["offset"] + slice * g["sliceStride"] + mip["offset"])

    def _linear(self, texture, mip, start):
        spec = DXGI.get(texture["dxgi"])
        if not mip["located"]:
            raise ValueError("Offset of this mip inside the mip-tail block is unresolved")
        key = (spec[1], texture["blockSize"].bit_length() - 1)
        if key not in SWIZZLE:
            raise ValueError(f"Swizzle order for {spec[1]}-byte elements in {texture['blockSize']}-byte blocks is unconfirmed")
        confirmed = CONFIRMED.get(key)
        if confirmed is not None:
            needed = {f"x{b}" for b in range((mip["ew"] - 1).bit_length())} | {f"y{b}" for b in range((mip["eh"] - 1).bit_length())}
            if mip["inTail"] or not needed <= confirmed:
                raise ValueError(f"Swizzle bits {sorted(needed - confirmed)} for {spec[1]}-byte elements are unconfirmed")
        data, blocked = self.read(start, mip["size"])
        if data is None:
            raise LookupError(", ".join(f"chunk {c.index} ({c.codec_name})" for c in blocked))
        if not mip["inTail"]:
            return unswizzle(data, mip["ew"], mip["eh"], spec[1], key[1])
        bw, bh = block_dims(spec[1], key[1])
        whole = np.frombuffer(unswizzle(data, bw, bh, spec[1], key[1]), np.uint8).reshape(bh, bw, spec[1])
        x, y = mip["tailX"], mip["tailY"]
        return np.ascontiguousarray(whole[y:y + mip["eh"], x:x + mip["ew"]]).tobytes()

    def mip_image(self, texture, level, slice=0, exposure=None, black=0.0):
        mip = texture["mips"][level]
        return decode_pixels(self.mip_linear(texture, level, slice), texture["dxgi"], mip["width"], mip["height"],
                             exposure, black)

    def texture_chunks(self, texture):
        end = texture["virtualOffset"] + (texture["computedSize"] or texture["virtualSize"])
        return self.chunk_range(texture["virtualOffset"], end)


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while block := f.read(8 << 20):
            h.update(block)
    return h.hexdigest()


def png_bytes(image):
    buf = io.BytesIO()
    image.save(buf, "PNG", compress_level=3)
    return buf.getvalue()
