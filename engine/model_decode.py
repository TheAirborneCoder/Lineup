# SPDX-License-Identifier: GPL-3.0-or-later
"""Decode one native model resource (dictionary entry, standalone drawable or fragment) to a GLB.

Uses the drawable decoders (drawables.py) unchanged and adds
per-material texture references, so every archive and resource kind goes through one entry point.
"""
import mmap
import struct
import threading
import zlib
from pathlib import Path

import drawables as d  # noqa: E402
from index_dictionaries import read_dictionary  # noqa: E402
from fragment_pages import complete_system  # noqa: E402
from paths import archive_path

_lock = threading.Lock()
_maps = {}


def archive(name):
    """Read-only memory map of a local archive, shared across calls."""
    path = archive_path(name)
    with _lock:
        if name not in _maps:
            f = path.open("rb")
            _maps[name] = (f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ))
        return _maps[name][1]


def _pass_name(pass_mask):
    if pass_mask & (16 | 32):
        return "stencil"
    if pass_mask & 8:
        return "cutout"
    if pass_mask & 4:
        return "decal"
    if pass_mask & 2:
        return "alpha"
    if pass_mask & 64:
        return "water"
    return "opaque"


def _tex_name_at(system, tp):
    if tp + 0x38 > len(system):
        return None
    raw = struct.unpack_from("<Q", system, tp + 0x30)[0]
    if (raw >> 28) != 5:
        return None
    no = raw - 0x50000000
    if no < 0 or no >= len(system):
        return None
    buf = system[no:min(len(system), no + 136)]
    if len(buf) >= 8 and buf[:8] == b"\x00" * 8:
        buf = buf[8:]
    end = buf.find(0)
    if not 4 <= end < 128:
        return None
    raw_s = buf[:end]
    if not all(0x30 <= c <= 0x39 or 0x41 <= c <= 0x5A or 0x61 <= c <= 0x7A or c in (0x5F, 0x2D, 0x2E) for c in raw_s):
        return None
    return bytes(raw_s).decode("ascii", errors="ignore")


def tint_palette_layout(width):
    """Compute RageForge row-packing layout (rowLength, rows) for a 1D tint palette texture."""
    w = max(1, int(width))
    whole = 1 << (w.bit_length() - 1)
    extra = w - whole
    row_len = max(1, min(whole, 256 >> min(extra, 8)))
    rows = max(1, whole // row_len)
    return row_len, rows


def _embedded_entry(system, tp, idx=0, fallback_seed=""):
    if tp < 0 or tp + 0x48 > len(system):
        return None
    gfx_raw = struct.unpack_from("<Q", system, tp + 0x18)[0]
    if (gfx_raw >> 28) != 6:
        return None
    from ttpf_format import DXGI
    w, h, depth = struct.unpack_from("<HHH", system, tp + 0x38)
    dxgi = int(system[tp + 0x3f])
    tile = int(system[tp + 0x40])
    mips = max(1, int(system[tp + 0x42]))
    spec = DXGI.get(dxgi)
    is_tint = (h == 1 and 1 <= w <= 4096 and dxgi in (28, 29) and tile == 0)
    if not is_tint and not (4 <= w <= 8192 and 4 <= h <= 8192 and spec and spec[3] and tile in (0, 5)):
        return None
    ref_raw = struct.unpack_from("<Q", system, tp + 0x30)[0]
    tex_hash = None
    if (ref_raw >> 28) == 5:
        ref = d.pointer(system, tp + 0x30)
        if ref + 16 <= len(system):
            cand = struct.unpack_from("<Q", system, ref + 8)[0]
            if cand and (is_tint or _tex_name_at(system, tp) is None):
                tex_hash = f"{cand:016x}"
    tname = ("tint_palette" if is_tint else (_tex_name_at(system, tp) or f"embedded_{idx:02d}"))
    if not tex_hash:
        tex_hash = f"eb{d._name_hash(f'{fallback_seed}:{tname}:{tp}'):08x}{tp & 0xFFFFFF:06x}"
    ent = {
        "tp": tp,
        "idx": idx,
        "hash": tex_hash,
        "name": tname,
        "gfxOffset": gfx_raw - 0x60000000,
        "width": int(w),
        "height": int(h),
        "depth": max(1, int(depth)),
        "dxgi": dxgi,
        "format": spec[0] if spec else "R8G8B8A8_UNORM",
        "tile": tile,
        "layout": "Tint Palette" if is_tint else ("GFX10 4KB_S" if tile == 5 else "Linear"),
        "mips": 1 if is_tint else mips,
    }
    if is_tint:
        row_len, rows = tint_palette_layout(w)
        ent["isTintPalette"] = True
        ent["tintRowLength"] = row_len
        ent["tintRows"] = rows
    return ent


def embedded_textures(system, root=0, seed=""):
    """Extract embedded grcTexture descriptors from group TXD (group + 8), direct shader slots, and 1D tint palettes."""
    group = d.pointer(system, root + 0x10)
    by_tp = {}
    txd_raw = struct.unpack_from("<Q", system, group + 8)[0]
    if (txd_raw >> 28) == 5:
        txd = d.pointer(system, group + 8)
        if txd + 0x3a <= len(system) and struct.unpack_from("<Q", system, txd + 0x30)[0]:
            cnt = struct.unpack_from("<H", system, txd + 0x38)[0]
            if 0 < cnt <= 2048:
                arr = d.pointer(system, txd + 0x30)
                for k in range(cnt):
                    if not struct.unpack_from("<Q", system, arr + 8 * k)[0]:
                        continue
                    tp = d.pointer(system, arr + 8 * k)
                    ent = _embedded_entry(system, tp, idx=k, fallback_seed=seed)
                    if ent:
                        by_tp[tp] = ent
    array = d.pointer(system, group + 16)
    count = struct.unpack_from("<H", system, group + 24)[0]
    for i in range(count):
        shader = d.pointer(system, array + 8 * i)
        if not (struct.unpack_from("<Q", system, shader + 16)[0] and struct.unpack_from("<Q", system, shader + 32)[0]):
            continue
        table = d.pointer(system, shader + 32)
        slots = d.pointer(system, shader + 16)
        for j in range(system[table + 1]):
            _, offset = struct.unpack_from("<II", system, table + 8 + 8 * j)
            if offset != 4 * j or not struct.unpack_from("<Q", system, slots + 8 * j)[0]:
                continue
            tp = d.pointer(system, slots + 8 * j)
            if tp not in by_tp:
                ent = _embedded_entry(system, tp, idx=len(by_tp), fallback_seed=seed)
                if ent:
                    by_tp[tp] = ent
    # Scan CPU memory for standalone 1D RGBA8 tint palettes (h==1, DXGI 28/29) linked via u64 reference key.
    seen_hashes = {e["hash"] for e in by_tp.values()}
    sys_len = len(system)
    for tp in range(0, sys_len - 0x47, 8):
        if tp in by_tp:
            continue
        if system[tp + 0x3a] != 1 or system[tp + 0x3b] != 0:
            continue
        dxgi = system[tp + 0x3f]
        if dxgi not in (28, 29) or system[tp + 0x40] != 0:
            continue
        w = struct.unpack_from("<H", system, tp + 0x38)[0]
        if not (1 <= w <= 4096):
            continue
        gfx_raw = struct.unpack_from("<Q", system, tp + 0x18)[0]
        ref_raw = struct.unpack_from("<Q", system, tp + 0x30)[0]
        if (gfx_raw >> 28) != 6 or (ref_raw >> 28) != 5:
            continue
        ent = _embedded_entry(system, tp, idx=len(by_tp), fallback_seed=seed)
        if ent and ent["hash"] not in seen_hashes:
            by_tp[tp] = ent
            seen_hashes.add(ent["hash"])
    return list(by_tp.values())


def shader_refs(system, root=0, seed=""):
    """Material list of a drawable: shader hash, render pass byte (shader+0x3c), and texture parameters."""
    group = d.pointer(system, root + 0x10)
    array = d.pointer(system, group + 16)
    count = struct.unpack_from("<H", system, group + 24)[0]
    out = []
    for i in range(count):
        shader = d.pointer(system, array + 8 * i)
        if system[shader + 4: shader + 8] != b"atem":
            raise ValueError("unrecognized shader record")
        pass_mask = int(system[shader + 0x3c]) if shader + 0x3d <= len(system) else 1
        refs, params, unresolved = [], {}, 0
        # A material without texture parameters stores null slot and table pointers.
        if struct.unpack_from("<Q", system, shader + 16)[0] and struct.unpack_from("<Q", system, shader + 32)[0]:
            table = d.pointer(system, shader + 32)
            slots = d.pointer(system, shader + 16)
            n_tex = system[table + 1]
            for j in range(n_tex):
                key, offset = struct.unpack_from("<II", system, table + 8 + 8 * j)
                if offset != 4 * j or not struct.unpack_from("<Q", system, slots + 8 * j)[0]:
                    continue
                try:
                    tex = d.pointer(system, slots + 8 * j)
                    emb = _embedded_entry(system, tex, idx=j, fallback_seed=seed)
                    if emb:
                        refs.append({"slot": j, "parameter": f"{key:08x}", "textureHash": emb["hash"], "embedded": True})
                        continue
                    k_val = struct.unpack_from("<Q", system, tex + 0x10)[0] if tex + 0x18 <= len(system) else 0
                    ref = d.pointer(system, tex + 0x30)
                    if d.word(system, ref + 16) != 6 and k_val != 0x980:
                        continue
                    h64 = struct.unpack_from("<Q", system, ref + 8)[0]
                    if not h64:
                        continue
                    refs.append({"slot": j, "parameter": f"{key:08x}", "textureHash": f"{h64:016x}"})
                except (ValueError, struct.error):
                    unresolved += 1
            # Extract per-material constant buffer float/vector parameters (entries n_tex..n_tot-1).
            try:
                cbuf_raw = struct.unpack_from("<Q", system, shader + 8)[0]
                n_tot = struct.unpack_from("<H", system, table + 4)[0]
                if cbuf_raw and n_tot > n_tex and n_tot <= 256:
                    cbuf = d.pointer(system, shader + 8)
                    cbuf_size = slots - cbuf
                    if 8 < cbuf_size <= 4096 and cbuf + cbuf_size <= len(system):
                        import math as _math
                        for j in range(n_tex, n_tot):
                            if table + 8 + 8 * (j + 1) > len(system):
                                break
                            key, desc = struct.unpack_from("<II", system, table + 8 + 8 * j)
                            kind = desc & 0xFF
                            off = (desc >> 8) & 0xFFF
                            blen = (desc >> 20) & 0xFFF
                            if kind == 3 and 4 <= blen <= 64 and (blen & 3) == 0 and (8 + off + blen) <= cbuf_size:
                                vals = struct.unpack_from(f"<{blen // 4}f", system, cbuf + 8 + off)
                                if all(_math.isfinite(v) for v in vals):
                                    params[f"{key:08x}"] = [round(v, 5) for v in vals]
            except (ValueError, struct.error):
                pass
        out.append({
            "index": i,
            "shaderHash": f"{d.word(system, shader):08x}",
            "passMask": pass_mask,
            "renderPass": _pass_name(pass_mask),
            "textures": refs,
            **({"params": params} if params else {}),
            **({"unresolvedTextureRefs": unresolved} if unresolved else {}),
        })
    return out


def live_shader_info(arc, member, root, name, kind):
    """Fast CPU-only read of live shader render passes, 0x980 slots, and embedded TXD metadata."""
    seed = f"{arc}:{member:x}:{root:x}:{name}"
    src, system, start, froot, actual = read_resource(arc, member, root, name, kind)
    (mats, emb), _, _ = with_pages(
        lambda s, _: (shader_refs(s, froot, seed=seed), embedded_textures(s, froot, seed=seed)),
        src, system, start,
    )
    return mats, emb


def decode_embedded_png(arc, member, root, name, kind, target_hash, max_size=1024):
    """Untile and decode an embedded drawable/TXD texture directly from the resource GPU stream to PNG."""
    from math import ceil
    from ttpf_format import (DXGI, block_dims, decode_pixels, decode_values, display_range,
                             is_hdr, png_bytes, surface_layout, unswizzle)
    seed = f"{arc}:{member:x}:{root:x}:{name}"
    src, system, start, froot, _ = read_resource(arc, member, root, name, kind)
    (_, _, emb_list), system, start = with_pages(
        lambda s, _: (model_geometry_lod(s, froot, 0), shader_refs(s, froot, seed=seed), embedded_textures(s, froot, seed=seed)),
        src, system, start,
    )
    ent = next((e for e in emb_list if e["hash"] == target_hash), None)
    if not ent:
        raise LookupError("Embedded texture hash not found in drawable")
    fmt = ent["dxgi"]
    _, elem, px, _ = DXGI[fmt]
    w0, h0, mips = ent["width"], ent["height"], ent["mips"]
    gfx_off = ent["gfxOffset"]
    starts = sorted({e["gfxOffset"] for e in emb_list})
    pos_idx = starts.index(gfx_off)
    next_off = starts[pos_idx + 1] if pos_idx + 1 < len(starts) else None

    if ent.get("isTintPalette"):
        need_end = gfx_off + w0 * 4
    elif ent["tile"] == 0:
        row_bytes = ceil(w0 / px) * elem
        rows = ceil(h0 / px)
        pitch = ceil(row_bytes / 256) * 256
        need_end = gfx_off + (next_off - gfx_off if next_off is not None else pitch * rows)
    else:
        stride, chain = surface_layout(w0, h0, elem, px, 12, mips)
        need_end = gfx_off + (next_off - gfx_off if next_off is not None else stride)

    parts_lod0 = None
    try:
        parts_lod0 = model_geometry_lod(system, froot, 0)
    except Exception:
        pass
    first_v = parts_lod0[0][3][0] if parts_lod0 else None

    def _inflate_gpu(start_off):
        buf = bytearray()
        pos_p = start_off
        target_len = max(need_end, (first_v + 4) if first_v is not None else 0)
        while len(buf) < target_len and pos_p < len(src) and src[pos_p:pos_p + 2] in ZLIB_HEADERS:
            chunk, pos_p = d.read_stream(src, pos_p)
            buf.extend(chunk)
        return buf

    gpu = _inflate_gpu(start)
    if first_v is not None and (first_v + 4 > len(gpu) or struct.unpack_from("<I", gpu, first_v)[0] not in (0x4042, 0x3063)):
        cur_sys, cur_gpu = system, start
        for _ in range(4):
            nxt = _consume_trailing_cpu(src, cur_sys, cur_gpu)
            if nxt is None:
                break
            cur_sys, cur_gpu = nxt
            cand = _inflate_gpu(cur_gpu)
            if first_v + 4 <= len(cand) and struct.unpack_from("<I", cand, first_v)[0] in (0x4042, 0x3063):
                gpu = cand
                break
    if gfx_off >= len(gpu):
        raise ValueError("Embedded texture offset lies past inflated GPU stream")
    if ent.get("isTintPalette"):
        from PIL import Image
        raw = bytearray(gpu[gfx_off:gfx_off + w0 * 4])
        if len(raw) < w0 * 4:
            raw.extend(b"\xff" * (w0 * 4 - len(raw)))
        for k in range(3, w0 * 4, 4):
            raw[k] = 255
        image = Image.frombytes("RGBA", (w0, 1), bytes(raw))
        return png_bytes(image), 0
    alloc_end = min(len(gpu), next_off) if next_off is not None else len(gpu)
    alloc = memoryview(gpu)[gfx_off:alloc_end]

    if ent["tile"] == 0:
        level = 0
        w, h = w0, h0
        row_bytes = ceil(w / px) * elem
        rows = ceil(h / px)
        pitch = ceil(row_bytes / 256) * 256
        if len(alloc) >= pitch * rows:
            linear = b"".join(bytes(alloc[y * pitch:y * pitch + row_bytes]) for y in range(rows))
        else:
            raw = bytes(alloc[:row_bytes * rows]).ljust(row_bytes * rows, b"\x00")
            linear = raw
    else:
        stride, chain = surface_layout(w0, h0, elem, px, 12, mips)
        if len(alloc) < stride and mips > 1:
            mips = 1
            stride, chain = surface_layout(w0, h0, elem, px, 12, 1)
        levels = [i for i in range(mips) if max(max(1, w0 >> i), max(1, h0 >> i)) <= max_size] or [mips - 1]
        level = levels[0]
        mip = chain[level]
        w, h = max(1, w0 >> level), max(1, h0 >> level)
        src_bytes = bytes(alloc) if len(alloc) >= stride else bytes(alloc).ljust(stride, b"\x00")
        data = src_bytes[mip["offset"]:mip["offset"] + mip["size"]]
        if mip["inTail"]:
            bw, bh = block_dims(elem, 12)
            tile_sheet = unswizzle(data, bw, bh, elem, 12)
            tstart = (mip["tailY"] * bw + mip["tailX"]) * elem
            linear = b"".join(
                tile_sheet[tstart + y * bw * elem:tstart + (y * bw + mip["ew"]) * elem]
                for y in range(mip["eh"])
            )
        else:
            linear = unswizzle(data, mip["ew"], mip["eh"], elem, 12)

    black, exposure = 0.0, 1.0
    if is_hdr(fmt):
        black, exposure = display_range(fmt, decode_values(linear, fmt, w, h))
    image = decode_pixels(linear, fmt, w, h, exposure, black)
    return png_bytes(image.convert("RGBA")), level


def _all_lods(src, system, start, root):
    """Bring in CPU pages for every LOD of a drawable (pages can follow the first stream)."""
    pointers = bytes(system[root + 0x50: root + 0x70])
    try:
        for level in range(4):
            entry = pointers[level * 8: level * 8 + 8]
            if any(entry):
                system[root + 0x50: root + 0x58] = entry
                system, start = complete_system(src, system, start, root)
    finally:
        system[root + 0x50: root + 0x70] = pointers
    return system, start


PAGE = 65536
ZLIB_HEADERS = (b"\x78\x01", b"\x78\x5e", b"\x78\x9c", b"\x78\xda")


def _uncompressed_tail(src, start):
    """Return length of an uncompressed 16-byte-aligned trailing CPU page before the next zlib stream, or 0."""
    for off in range(16, 4096, 16):
        if src[start + off:start + off + 2] in ZLIB_HEADERS:
            try:
                d.read_stream(src, start + off)
                return off
            except Exception:
                continue
    return 0


def with_pages(fn, src, system, start, limit=256):
    """Call fn(system, start); on a pointer past the loaded CPU data, append continuation pages and retry.

    Accepts zlib continuation pages when the loaded system ends on a full 64 KiB page, and small
    uncompressed trailing CPU pages (<= 4 KiB) directly preceding the next valid zlib stream.
    """
    system = bytearray(system)
    for _ in range(limit):
        try:
            return fn(system, start), system, start
        except ValueError as exc:
            if "pointer outside system page" not in str(exc) or len(system) % PAGE:
                raise
            if src[start:start + 2] in ZLIB_HEADERS:
                extra, start = d.read_stream(src, start)
                system.extend(extra)
                continue
            tail = _uncompressed_tail(src, start)
            if tail:
                system.extend(src[start:start + tail])
                start += tail
                continue
            raise
    raise ValueError("system continuation limit reached")


def lod_levels(system, root):
    return [i for i in range(4) if any(system[root + 0x50 + 8 * i: root + 0x58 + 8 * i])]


def model_geometry_lod(system, root, lod=0):
    """Read geometry descriptors for a specific LOD level (0..3) without mutating the caller's buffer."""
    if lod == 0:
        return d.model_geometry(system, root)
    if not 0 <= lod < 4:
        raise ValueError(f"invalid LOD level {lod}")
    entry = system[root + 0x50 + 8 * lod: root + 0x58 + 8 * lod]
    if not any(entry):
        raise ValueError(f"LOD {lod} not present")
    buf = bytearray(system)
    buf[root + 0x50: root + 0x58] = entry
    return d.model_geometry(buf, root)


def decode_part_full(vertex, indices):
    """Decode one native meshlet geometry part with secondary UVs, vertex alpha, and terrain blend weights."""
    import numpy as np
    header = np.frombuffer(vertex, dtype="<u4", count=23)
    first = int(header[0])
    if first not in (0x4042, 0x3063):
        raise ValueError("unsupported vertex format")
    starts = (header >> 8) * 2
    p = int(starts[0])
    stride = 8 if first == 0x4042 else 12
    after = starts[starts > p]
    size = (int(after.min()) if len(after) else len(vertex)) - p
    if size % stride or size <= 0:
        raise ValueError("invalid vertex array")
    count = size // stride
    if first == 0x4042:
        q = d.vectors(vertex, p, "<u2", count, 4)[:, 1:].astype("f4")
        q[:, :2] *= 256
        base = np.frombuffer(vertex, "<f4", 3, 0x60)
        step = np.frombuffer(vertex, "<f4", 3, 0x70)
        pos = q * step + base
        mode = d.word(vertex, 0x6c)
        if mode not in (0, 1, 2):
            raise ValueError("unknown quantizer axis")
        pos = pos[:, [(2, 1, 0), (1, 2, 0), (0, 1, 2)][mode]]
    else:
        pos = d.vectors(vertex, p, "<f4", count, 3)
    if not np.isfinite(pos).all():
        raise ValueError("nonfinite vertices")
    n, _, tri_count, remap_count = struct.unpack_from("<HHII", indices)
    tri_pos = 12 + 16 * n
    remap_pos = tri_pos + tri_count * 4
    if not n or remap_pos + remap_count * 2 > len(indices):
        raise ValueError("invalid index array")
    meshlets = d.vectors(indices, 12, "<u2", n, 8)
    bases = meshlets[:, 2].astype("u4") | (((meshlets[:, 0].astype("u4") >> 14) & 1) << 16)
    tri = np.frombuffer(indices, "<u4", tri_count, tri_pos)
    remap = np.frombuffer(indices, "<u2", remap_count, remap_pos)
    polys = remap[bases[tri >> 21, None] + ((tri[:, None] >> np.array([0, 7, 14], "u4")) & 127)]
    if polys.max() >= count:
        raise ValueError("triangle exceeds vertex count")
    a, b, c = polys.T
    polys = polys[(a != b) & (b != c) & (a != c)]
    if not len(polys):
        raise ValueError("empty drawable geometry")
    normals = None
    nfmt = int(header[2] & 255)
    if nfmt in (0x21, 0x27) and int(starts[2]) + count * 4 <= len(vertex):
        raw = np.frombuffer(vertex, "<u4", count, int(starts[2]))
        xy10 = np.stack((raw & 1023, (raw >> 10) & 1023), axis=1).astype("f4") * (2.0 / 1023.0) - 1.0
        if nfmt == 0x21:
            # Bit 31 selects 10+10-bit octahedral normal (1) vs 8+8-bit octahedral normal + 15-bit tangent frame (0).
            xy8 = np.stack((raw & 255, (raw >> 8) & 255), axis=1).astype("f4") * (2.0 / 255.0) - 1.0
            xy = np.where(((raw >> 31) != 0)[:, None], xy10, xy8)
        else:
            xy = xy10
        z = 1.0 - np.abs(xy).sum(axis=1)
        xy[z < 0] = (1.0 - np.abs(xy[z < 0, ::-1])) * np.where(xy[z < 0] < 0, -1.0, 1.0)
        normals = np.column_stack((xy, z))
        normals /= np.maximum(np.linalg.norm(normals, axis=1, keepdims=True), 1e-9)
    if normals is not None:
        a, b, c = polys.T
        cross = np.cross(pos[b] - pos[a], pos[c] - pos[a])
        flip = np.sum(cross * (normals[a] + normals[b] + normals[c]), axis=1) < 0
        polys[flip] = polys[flip][:, [0, 2, 1]]

    def _read_uv(slot):
        if slot >= len(header):
            return None
        ufmt = int(header[slot] & 255)
        st = int(starts[slot])
        if ufmt == 0x28 and st + count * 4 <= len(vertex):
            arr = d.vectors(vertex, st, "<i2", count, 2).astype("f4") / 8192.0
            return arr if np.isfinite(arr).all() else None
        if ufmt == 0x42 and st + count * 8 <= len(vertex):
            arr = d.vectors(vertex, st, "<f4", count, 2)
            return arr if np.isfinite(arr).all() else None
        return None

    uv = _read_uv(3)
    uv1 = None
    for s in (4, 14, 15):
        uv1 = _read_uv(s)
        if uv1 is not None:
            break

    colors = None
    has_rgb = False
    has_alpha = False
    has_tint_idx = False
    if (header[1] & 255) == 0x26 and int(starts[1]) + count * 4 <= len(vertex):
        raw_c = np.frombuffer(vertex, "<u1", count * 4, int(starts[1])).reshape(count, 4)
        rgb_ok = int(raw_c[:, :3].max()) > 0
        alpha_ok = int(raw_c[:, 3].max()) > 0 and int(raw_c[:, 3].min()) < 254
        if rgb_ok or alpha_ok:
            colors = raw_c.astype("f4") / 255.0
            if not alpha_ok:
                colors[:, 3] = 1.0
            has_rgb = (uv is None and rgb_ok)
            has_alpha = alpha_ok
            has_tint_idx = True

    blend_weights = None
    if len(header) > 12 and (header[12] & 255) == 0x26 and int(starts[12]) + count * 4 <= len(vertex):
        raw_w = np.frombuffer(vertex, "<u1", count * 4, int(starts[12])).reshape(count, 4)
        if int(raw_w.max()) > 0:
            blend_weights = raw_w.astype("f4") / 255.0

    return pos, normals, uv, uv1, colors, blend_weights, has_rgb, has_alpha, has_tint_idx, polys


def decode_part(vertex, indices):
    """Backwards-compatible 5-tuple wrapper over decode_part_full."""
    pos, normals, uv, _, colors, _, has_rgb, _, _, polys = decode_part_full(vertex, indices)
    return pos, normals, uv, (colors if has_rgb else None), polys


def read_resource(arc, member, root, name, kind):
    """CPU system bytes, start of the GPU stream and the drawable root for one model."""
    src = archive(arc)
    if kind == "dictionary":
        system, start, entries = read_dictionary(src, member)
        if not any(r == root and n == name for _, r, n in entries):
            raise ValueError("dictionary entry differs from the index")
    elif kind == "standalone":
        found, system, start = d.read_system(src, member)
        if found != name:
            raise ValueError("drawable name differs from the index")
        root = 0
    elif kind == "fragment":
        from index_fragments import read_fragment
        plain, embedded, froot, _, _, system, start = read_fragment(src, member)
        if name not in (plain, embedded):
            # The package scanner's name pattern can capture stray bytes before the real name.
            if not name.endswith(embedded):
                raise ValueError("fragment name differs from the index")
            name = embedded
        root = froot
    else:
        raise ValueError(f"unknown resource kind {kind}")
    return src, system, start, root, name


def export_lod(src, member, out, name, system, start, root, arc, lod=0):
    """Export one LOD of a drawable to GLB, accepting a rigid skeleton-root box offset when needed."""
    import numpy as np
    parts = model_geometry_lod(system, root, lod)
    required = max(at + size for *_, vert, ind in parts for at, size in (vert, ind))
    gpu = bytearray()
    p = start
    while len(gpu) < required:
        if src[p:p + 1] != b"x":
            raise ValueError("unresolved graphics stream")
        chunk, p = d.read_stream(src, p)
        gpu.extend(chunk)
    glb = d.Glb()
    prims, lows, highs, verts, tris = [], [], [], 0, 0
    for model, geometry, shader, (v, nv), (i, ni) in parts:
        pos, norm, uv, uv1, col, blend_w, has_rgb, has_alpha, has_tint_idx, faces = decode_part_full(
            memoryview(gpu)[v:v + nv], memoryview(gpu)[i:i + ni]
        )
        lows.append(pos.min(0))
        highs.append(pos.max(0))
        verts += len(pos)
        tris += len(faces)
        attr = {"POSITION": glb.array(d.yup(pos), "VEC3", 5126, True)}
        if norm is not None:
            attr["NORMAL"] = glb.array(d.yup(norm), "VEC3", 5126)
        if uv is not None:
            attr["TEXCOORD_0"] = glb.array(uv, "VEC2", 5126)
        if uv1 is not None:
            attr["TEXCOORD_1"] = glb.array(uv1, "VEC2", 5126)
        if col is not None:
            attr["COLOR_0"] = glb.array(col, "VEC4", 5126)
        if blend_w is not None:
            attr["COLOR_1"] = glb.array(blend_w, "VEC4", 5126)
        p_extras = {"model": model, "geometry": geometry, "shaderIndex": shader}
        if has_rgb:
            p_extras["hasColorRGB"] = True
        if has_alpha:
            p_extras["hasVertexAlpha"] = True
        if has_tint_idx:
            p_extras["hasTintIndex"] = True
        if blend_w is not None:
            p_extras["hasBlendWeights"] = True
        prims.append({"attributes": attr, "indices": glb.array(faces.ravel(), "SCALAR", 5123), "extras": p_extras})
    bounds = np.vstack((np.min(lows, 0), np.max(highs, 0))).astype(np.float64)
    box = np.array([struct.unpack_from("<3f", system, root + b) for b in (48, 64)], dtype=np.float64)
    excess = max(float(np.max(box[0] - bounds[0])), float(np.max(bounds[1] - box[1])), 0.0)
    offset, dims_error = None, None
    is_biped = any(t in name.lower() for t in ("_fr1_", "_mr1_", "_fb1_", "_mb1_", "_fo1_", "_mo1_", "_fc1_", "_mc1_"))
    extent = float(np.max(bounds[1] - bounds[0]))
    mid_x = float((bounds[0][0] + bounds[1][0]) * 0.5)
    mid_z = float((bounds[0][2] + bounds[1][2]) * 0.5)
    at_root_stub = bool(is_biped and extent < 0.12 and mid_z < -0.85 and abs(mid_x) < 0.12)
    if np.isfinite(box).all():
        dims_error = float(np.abs(np.diff(box, axis=0) - np.diff(bounds, axis=0)).max())
        if is_biped and bounds[0][2] < box[0][2] - 0.5:
            # Human biped rigs share an exact SKEL_ROOT at (0, 0, +1.0m), even when fr1 heads store an mr1 template box.
            cand = np.array([0.0, 0.0, 1.0], dtype=np.float64)
            if max(float(np.max(box[0] - (bounds[0] + cand))), float(np.max((bounds[1] + cand) - box[1])), 0.0) <= 0.15:
                offset = cand
        # Skinned characters/animals keep vertices relative to the skeleton root while box is in standing space.
        if offset is None and dims_error <= 0.03 and float(np.linalg.norm(box[0] - bounds[0])) > 0.002:
            offset = box[0] - bounds[0]
        if offset is None and excess > 0.3 and bounds[0][2] < -0.2 and box[0][2] >= -0.15:
            cand = np.array([0.0, 0.0, float(box[1][2] - bounds[1][2])], dtype=np.float64)
            if max(float(np.max(box[0] - (bounds[0] + cand))), float(np.max((bounds[1] + cand) - box[1])), 0.0) <= 0.08:
                offset = cand
    if offset is None and excess > max(0.3, float(np.linalg.norm(box[1] - box[0])) * 0.05):
        raise ValueError("geometry exceeds stored bounds and its dimensions differ from the drawable box")
    glb.doc["meshes"].append({"name": name, "primitives": prims})
    node = {"name": name, "mesh": 0}
    if offset is not None:
        node["translation"] = d.yup(offset[None, :])[0].tolist()
    glb.doc["nodes"].append(node)
    glb.doc["scenes"][0]["nodes"] = [0]
    extras = {"archive": arc, "member": hex(member), "nameHash": hex(d._name_hash(name)), "lod": lod}
    if offset is not None:
        extras["boundsOffset"] = offset.tolist()
        extras["boundsDimensionsErrorM"] = dims_error
    if at_root_stub:
        extras["atRootStub"] = True
    glb.doc["extras"] = extras
    out.mkdir(parents=True, exist_ok=True)
    suffix = f".lod{lod}" if lod else ""
    file = f"{name}.{member:x}{suffix}.glb"
    glb.write(out / file)
    info = {"name": name, "member": hex(member), "archetypeHash": d._name_hash(name), "vertices": verts,
            "triangles": tris, "parts": len(parts),
            "geometryBounds": (bounds + offset if offset is not None else bounds).tolist(),
            "drawableBounds": box.tolist(), "file": file, "lod": lod}
    if offset is not None:
        info["boundsOffset"] = offset.tolist()
        info["boundsDimensionsErrorM"] = dims_error
    if at_root_stub:
        info["atRootStub"] = True
    return info


def _consume_trailing_cpu(src, sysdata, gpu):
    """Consume an unreferenced trailing CPU page when sysdata ends on a 64 KiB boundary."""
    if len(sysdata) % PAGE:
        return None
    if src[gpu:gpu + 2] in ZLIB_HEADERS:
        chunk, nxt = d.read_stream(src, gpu)
        if len(chunk) < PAGE or struct.unpack_from("<I", chunk, 0)[0] not in (0x4042, 0x3063):
            out = bytearray(sysdata)
            out.extend(chunk)
            return out, nxt
    else:
        tail = _uncompressed_tail(src, gpu)
        if tail:
            out = bytearray(sysdata)
            out.extend(src[gpu:gpu + tail])
            return out, gpu + tail
    return None


def decode(arc, member, root, name, kind, out_dir, lod=0):
    """Write <name>.<member>[.lodN].glb under out_dir and return geometry, LOD and material metadata."""
    seed = f"{arc}:{member:x}:{root:x}:{name}"
    src, system, start, root, name = read_resource(arc, member, root, name, kind)
    # Load every CPU page the drawable's tables reference before decoding.
    try:
        _, system, start = with_pages(lambda s, _: (model_geometry_lod(s, root, lod), shader_refs(s, root, seed=seed)),
                                      src, system, start)
    except (ValueError, IndexError, struct.error, zlib.error) as exc:
        raise ValueError(f"system pages: {exc}")
    attempts = []
    for completion in (False, True):
        try:
            sysdata, gpu = (_all_lods(src, bytearray(system), start, root) if completion else (system, start))
            tag = "with continuation pages" if completion else "direct"
            try:
                info = export_lod(src, member, Path(out_dir), name, sysdata, gpu, root, arc, lod=lod)
            except ValueError as exc:
                if str(exc) not in ("unsupported vertex format", "invalid vertex array", "invalid index array"):
                    raise
                # Fragment/drawable CPU data can end with an unreferenced trailing page (e.g. bone names)
                # right after a 64 KiB page boundary before the GPU stream begins.
                cur_sys, cur_gpu = sysdata, gpu
                info = None
                for step in range(4):
                    nxt = _consume_trailing_cpu(src, cur_sys, cur_gpu)
                    if nxt is None:
                        break
                    cur_sys, cur_gpu = nxt
                    try:
                        info = export_lod(src, member, Path(out_dir), name, cur_sys, cur_gpu, root, arc, lod=lod)
                        tag = f"{tag} (+{step + 1} trailing CPU page{'s' if step else ''})"
                        break
                    except ValueError:
                        continue
                if info is None:
                    raise
            info["materials"] = shader_refs(sysdata, root, seed=seed)
            info["lodLevels"] = lod_levels(sysdata, root)
            info["kind"] = kind
            info["root"] = hex(root)
            info["decodePath"] = tag
            return info
        except (ValueError, IndexError, struct.error, zlib.error, EOFError) as exc:
            attempts.append(f"{'pages' if completion else 'direct'}: {exc}")
    raise ValueError("; ".join(attempts))


WHEEL_BONE_RE = d.re.compile(r"^wheel_(?:lf|rf|lr|rr|lm\d+|rm\d+|lr_inr|rr_inr|lf_inr|rf_inr|f|r)$")


def vehicle_wheels(arc, member, root, name, kind):
    """Extract native RAGE wheel axle positions, widths, and tire radii from crSkeletonData + phBoundComposite."""
    import numpy as np
    try:
        src, system, start, root, name = read_resource(arc, member, root, name, kind)
        system = bytearray(system)
        def _need(off):
            nonlocal start
            while off > len(system) and start < len(src) and src[start:start + 1] == b"x":
                chunk, start = d.read_stream(src, start)
                system.extend(chunk)
        _need(root + 0x70)
        skel_ptr = struct.unpack_from("<Q", system, root + 0x18)[0]
        if (skel_ptr >> 28) != 5:
            return []
        skel = skel_ptr - 0x50000000
        _need(skel + 0x70)
        nbones = struct.unpack_from("<H", system, skel + 0x6c)[0]
        if not 1 <= nbones <= 1024:
            return []
        bones_ptr = struct.unpack_from("<Q", system, skel + 0x28)[0]
        inv_ptr = struct.unpack_from("<Q", system, skel + 0x30)[0]
        if (bones_ptr >> 28) != 5 or (inv_ptr >> 28) != 5:
            return []
        bones = bones_ptr - 0x50000000
        inv_mats = inv_ptr - 0x50000000
        _need(max(bones + nbones * 0x50, inv_mats + nbones * 64))
        max_nptr = 0
        for i in range(nbones):
            np_raw = struct.unpack_from("<Q", system, bones + i * 0x50 + 0x30)[0]
            if (np_raw >> 28) == 5:
                max_nptr = max(max_nptr, np_raw - 0x50000000 + 64)
        if max_nptr:
            _need(max_nptr)
        cylinders = []
        if kind == "fragment" and (struct.unpack_from("<Q", system, 0x10)[0] >> 28) == 5:
            arch = struct.unpack_from("<Q", system, 0x10)[0] - 0x50000000
            _need(arch + 0x30)
            cp = struct.unpack_from("<Q", system, arch + 0x20)[0]
            if (cp >> 28) == 5:
                comp = cp - 0x50000000
                _need(comp + 0xa8)
                if system[comp + 0x10] == 6:
                    nc = struct.unpack_from("<H", system, comp + 0x98)[0]
                    bp = struct.unpack_from("<Q", system, comp + 0x70)[0] - 0x50000000
                    mt = struct.unpack_from("<Q", system, comp + 0x78)[0] - 0x50000000
                    _need(max(bp + nc * 8, mt + nc * 64))
                    for i in range(min(nc, 256)):
                        cptr = struct.unpack_from("<Q", system, bp + i * 8)[0]
                        if (cptr >> 28) != 5:
                            continue
                        boff = cptr - 0x50000000
                        _need(boff + 0x40)
                        if boff + 0x40 <= len(system) and system[boff + 0x10] == 8:
                            bx = struct.unpack_from("<8f", system, boff + 0x20)
                            sz = [abs(bx[4 + k] - bx[k]) for k in range(3)]
                            rad = sz[1] * 0.5
                            if rad > 0.12 and abs(sz[1] - sz[2]) < 0.05 and sz[0] < sz[1] * 1.6:
                                cpos = struct.unpack_from("<3f", system, mt + i * 64 + 48)
                                cylinders.append((np.array(cpos, dtype=np.float64), float(sz[0]), float(rad)))
        box_min_z = struct.unpack_from("<f", system, root + 56)[0]
        wheels = []
        for i in range(nbones):
            np_raw = struct.unpack_from("<Q", system, bones + i * 0x50 + 0x30)[0]
            if (np_raw >> 28) != 5:
                continue
            noff = np_raw - 0x50000000
            end = system.find(b"\x00", noff, min(len(system), noff + 48))
            if end <= noff:
                continue
            bname = system[noff:end].decode("ascii", "replace")
            if not WHEEL_BONE_RE.match(bname):
                continue
            raw = np.frombuffer(system, "<f4", 16, inv_mats + i * 64).reshape(4, 4)
            m = np.eye(4, dtype=np.float64)
            m[:3, :3] = raw[:3, :3].T
            m[:3, 3] = raw[3, :3]
            pos = np.linalg.inv(m)[:3, 3]
            if not np.isfinite(pos).all():
                continue
            best = None
            for cpos, cw, cr in cylinders:
                if float(np.linalg.norm(cpos - pos)) < 0.16:
                    if best is None or cr > best[1]:
                        best = (cw, cr)
            if best:
                width, radius = best
            else:
                est_r = float(pos[2] - box_min_z)
                radius = est_r if 0.18 <= est_r <= 1.5 else 0.34
                width = round(radius * 0.72, 4)
            is_right = ("_r" in bname and not bname.endswith("_lr") and "_lr_" not in bname) or float(pos[0]) > 0.08
            wheels.append({
                "bone": bname,
                "position": [round(float(pos[0]), 4), round(float(pos[2]), 4), round(float(-pos[1]), 4)],
                "ragePosition": [round(float(v), 4) for v in pos],
                "radius": round(float(radius), 4),
                "width": round(float(width), 4),
                "right": bool(is_right),
            })
        return wheels
    except Exception:
        return []

