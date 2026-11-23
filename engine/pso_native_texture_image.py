# SPDX-License-Identifier: GPL-3.0-or-later
"""Preview a texture from source-linked native RPF dictionary records."""

from functools import lru_cache
from math import ceil
from pathlib import Path
import zlib

from ttpf_format import (DXGI, SWIZZLE3D, block_dims, decode_pixels, decode_values, display_range,
                         is_hdr, png_bytes, surface_layout, unswizzle, unswizzle3)


@lru_cache(maxsize=3)
def _inflate(path, stamp, offset, size, inflated):
    with path.open("rb") as f:
        f.seek(offset)
        payload = f.read(size)
    d = zlib.decompressobj()
    data = d.decompress(payload, inflated + 1)
    if len(data) != inflated or not d.eof or d.unused_data:
        raise ValueError("Native texture GPU stream differs from its physical index")
    return data


def _read(root, record, start, size):
    if start < 0 or size < 1 or start + size > record["allocationBytes"]:
        raise ValueError("Requested mip exceeds the declared GPU allocation")
    start += record["gpuOffset"]
    end = start + size
    path = Path(root) / record["package"]
    stamp = path.stat().st_mtime_ns
    cursor = 0
    parts = []
    for offset, packed, inflated in record.get("gpuStreams", ()):
        following = cursor + inflated
        if following > start and cursor < end:
            blob = _inflate(path, stamp, offset, packed, inflated)
            parts.append(blob[max(0, start - cursor):min(inflated, end - cursor)])
        cursor = following
        if cursor >= end:
            break
    if sum(map(len, parts)) != size:
        raise ValueError("GPU stream coverage is unavailable for this texture")
    return b"".join(parts)


def _volume_slice(root, record, level, layer, elem, px):
    # AMD GFX10 4KB_S macro-tiled layout. Each group of bd slices has its own
    # mip chain in reverse size order and one shared tail, up to five levels.
    (bw, bh, bd), order = SWIZZLE3D[elem, 12]
    levels = record["mips"]
    dims = [(ceil(max(1, record["width"] >> i) / px), ceil(max(1, record["height"] >> i) / px))
            for i in range(levels)]
    tail = next((i for i, (w, h) in enumerate(dims) if levels > 1 and levels - i <= 5
                 and w <= bw and h <= bh // 2), levels)
    sizes = [ceil(w / bw) * ceil(h / bh) * 4096 for w, h in dims[:tail]]
    tail_bytes = 4096 if tail < levels else 0
    stride = sum(sizes) + tail_bytes
    if stride * ceil(record["depth"] / bd) != record["allocationBytes"]:
        raise ValueError("Volume mip layout differs from the declared GPU allocation")
    start = stride * (layer // bd)
    ew, eh = dims[level]
    if level < tail:
        data = _read(root, record, start + tail_bytes + sum(sizes[level + 1:]), sizes[level])
        return unswizzle3(data, ew, eh, bd, elem, 12)[layer % bd]

    # For 4 KiB thick tiles, the five consecutive tail roots occupy 256-byte
    # microblocks (0,2), (1,1), (1,0), (0,1), (0,0).
    index = 4 - (level - tail)
    bits = order.split()[:8 - (elem.bit_length() - 1)]
    mx = ((index >> 1) & 1) * (1 << sum(bit.startswith("x") for bit in bits))
    my = ((index & 1) | ((index >> 1) & 2)) * (1 << sum(bit.startswith("y") for bit in bits))
    data = _read(root, record, start, 4096)
    sheet = unswizzle3(data, bw, bh, bd, elem, 12)[layer % bd]
    return b"".join(sheet[((my + y) * bw + mx) * elem:((my + y) * bw + mx + ew) * elem]
                    for y in range(eh))


def native_png(root, record, level=0, slice_index=0, thumbnail=False):
    """Render a source mip and face/layer/depth slice, or explain why it is unavailable."""
    if not 0 <= level < record["mips"]:
        raise ValueError("Mip level outside this texture")
    volume = record["dimension"] == 3
    depth = max(1, record["depth"] >> level) if volume else record["depth"]
    if not 0 <= slice_index < depth:
        raise ValueError("Slice outside this mip level")
    tile = record["tileHint"]
    if tile not in (0, 5) or record["dimension"] not in (0, 1, 2, 3):
        raise ValueError("Unsupported native texture layout")
    fmt = record["dxgi"]
    spec = DXGI.get(fmt)
    if not spec or not spec[3] or record["elementSize"] != spec[1]:
        raise ValueError("Unsupported native texture format or element size")
    _, elem, px, _ = spec
    w, h = max(1, record["width"] >> level), max(1, record["height"] >> level)
    if tile == 0:
        # Linear GPU textures store each row at a 256-byte pitch.
        row_bytes = ceil(w / px) * elem
        rows = ceil(h / px)
        pitch = ceil(row_bytes / 256) * 256
        stride = pitch * rows
        if record["mips"] != 1 or stride * depth != record["allocationBytes"]:
            raise ValueError("Linear texture pitch differs from the declared GPU allocation")
        raw = _read(root, record, stride * slice_index, stride)
        linear = b"".join(raw[y*pitch:y*pitch + row_bytes] for y in range(rows))
    elif volume:
        linear = _volume_slice(root, record, level, slice_index, elem, px)
    else:
        stride, chain = surface_layout(record["width"], record["height"], elem, px, 12, record["mips"])
        if stride * depth != record["allocationBytes"]:
            raise ValueError("Mip chain differs from the declared GPU allocation")
        mip = chain[level]
        if not mip["located"]:
            raise ValueError("Mip lies outside its expected tail slot")
        data = _read(root, record, slice_index * stride + mip["offset"], mip["size"])
        if mip["inTail"]:
            bw, bh = block_dims(elem, 12)
            tile = unswizzle(data, bw, bh, elem, 12)
            start = (mip["tailY"] * bw + mip["tailX"]) * elem
            linear = b"".join(tile[start + y*bw*elem:start + (y*bw + mip["ew"])*elem]
                              for y in range(mip["eh"]))
        else:
            linear = unswizzle(data, mip["ew"], mip["eh"], elem, 12)
    black, exposure = 0.0, 1.0
    if is_hdr(fmt):
        black, exposure = display_range(fmt, decode_values(linear, fmt, w, h))
    image = decode_pixels(linear, fmt, w, h, exposure, black)
    if thumbnail:
        image.thumbnail((160, 160))
    return png_bytes(image)
