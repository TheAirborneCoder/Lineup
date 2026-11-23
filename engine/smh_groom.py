# SPDX-License-Identifier: GPL-3.0-or-later
"""Decoder for RAGE "smh" strand-hair groom assets (hair, beards, brows, lashes, body hair, animal fur).

A hair or fur item's drawable is a one-triangle stub. The hair itself is a *groom LOD resource*: a small
system page plus one GPU buffer that the smh_* compute shaders read through a header of byte offsets.
The buffer holds guides (the vertices of a scalp triangle mesh, each with a polyline of nodes, skin
weights and attributes) and per-triangle strand counts. Strands are not stored: the game's
smh_gen_*_CS_InterpolateStrands shader scatters them over the scalp triangles and blends three guides.

What is exact here and what is not:
  exact     the asset layout: header, guides, node positions, lengths, skin, triangles, strand counts,
            region ids, and the integer random sequences that pick a strand's triangle and barycentric point
  modelled  strands in the rest pose: barycentric blend of three guides, trimmed to the strand's length.
            Run-time inputs the asset does not store are left out: the style record (clump, curl and
            frizz amounts, region mask), the length texture, skinning and simulation.

    python smh_groom.py info packs.rpf 0xf129d000
    python smh_groom.py glb packs.rpf 0xf129d000 out.glb [--strands 6000]
"""
from __future__ import annotations

import argparse
import json
import mmap
import struct
import zlib
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from paths import archive_path

SYS_BASE = 0x50000000
GPU_BASE = 0x60000000
TYPE_ID = 0x7D8F3DB3          # system page +0x1c in every groom LOD resource
PREFIX = bytes(16) + b"\x78"  # native resource: 16 zero bytes, then the zlib system page
U32 = np.uint32

# GPU header: byte offset of the field -> (name, bytes per element, element domain).
# Domains: G = per guide, N = per node (guide-major), T = per triangle.
ARRAYS = {
    0x00: ("attr_a", 4, "G"),      # 3 threshold bytes + 0xff (gen: compared with a random number)
    0x04: ("attr_b", 4, "G"),      # 3 threshold bytes
    0x08: ("uv", 4, "G"),          # two half floats: root UV
    0x0C: ("attr_c", 4, "G"),      # four bytes (64,128,191,255 in most assets)
    0x10: ("bone_index", 8, "G"),  # eight palette indices
    0x14: ("bone_weight", 8, "G"),  # eight weights, normalised by their sum
    0x18: ("ext_18", 12, "G"),     # optional
    0x1C: ("ext_1c", 8, "G"),      # optional
    0x24: ("ext_24", 4, "G"),      # optional
    0x28: ("direction", 4, "G"),   # optional: three bytes, 2*b/255-1 = a direction per guide
    0x30: ("length", 2, "G"),      # half float: guide arc length in metres
    0x34: ("node_4", 4, "N"),      # optional
    0x38: ("node_2", 2, "N"),      # optional
    0x40: ("triangle", 6, "T"),    # three u16 guide indices
    0x44: ("position", 8, "N"),    # half x, y, z, w
    0x48: ("tri_count", 4, "T"),   # strands per triangle for each strand class
    0x4C: ("tri_region", 1, "T"),  # region id, 0 = none
}
TAIL_OFFSET = 0x20                 # optional variable-size block (not decoded)
# Header 0x2c is not an offset: 0xffffffff in most assets, 0x0001ffff in 39.
STRAND_CLASS_SEED = (0x7C, 0x80)   # header offset of the per-class seed word


class GroomError(ValueError):
    pass


def inflate(src, pos):
    """One zlib stream at pos -> (bytes, offset of the next 16-byte boundary)."""
    decoder = zlib.decompressobj()
    out = bytearray()
    while not decoder.eof:
        chunk = src[pos:pos + 65536]
        if not chunk or len(out) > 64 << 20:
            raise GroomError("incomplete resource stream")
        out.extend(decoder.decompress(chunk))
        pos += len(chunk) - len(decoder.unused_data)
    return bytes(out), (pos + 15) & -16


def _sys_ptr(sysd, at):
    value = struct.unpack_from("<Q", sysd, at)[0] - SYS_BASE
    if not 0 <= value < len(sysd):
        raise GroomError(f"system pointer at {at:#x} outside the page")
    return value


def system_page_is_groom(sysd):
    """Cheap test on the first inflated bytes of a resource's system page."""
    if len(sysd) < 0xE0 or sysd[:8] != bytes(8) or struct.unpack_from("<I", sysd, 0x1C)[0] != TYPE_ID:
        return False
    desc = struct.unpack_from("<Q", sysd, 0x70)[0] - SYS_BASE
    if not 0 < desc <= len(sysd) - 0x20:
        return False
    words, stride, flag = struct.unpack_from("<IHH", sysd, desc + 8)
    return stride == 4 and flag == 8 and struct.unpack_from("<Q", sysd, desc + 0x18)[0] == GPU_BASE and words >= 40


@dataclass
class Groom:
    archive: str
    offset: int
    system: bytes = field(repr=False)
    buffer: bytes = field(repr=False)
    header: tuple = field(repr=False)
    guides: int = 0
    triangles_n: int = 0
    nodes: int = 0
    nodes_per_guide: int = 0
    flags: int = 0
    box: np.ndarray = None           # [2,3] metres, min and max (system page)
    strand_totals: tuple = (0, 0)    # system page: strands of class 0 and class 1
    regions: list = field(default_factory=list)
    arrays: dict = field(default_factory=dict, repr=False)
    present: dict = field(default_factory=dict, repr=False)
    tail: bytes = field(default=b"", repr=False)

    # --- decoded views -------------------------------------------------------------------------
    @property
    def position(self):
        """[guides, nodes_per_guide, 3] float32 rest positions, metres, skeleton-root space (Z up)."""
        return self.arrays["position"][..., :3]

    @property
    def node_w(self):
        return self.arrays["position"][..., 3]

    @property
    def triangle(self):
        return self.arrays["triangle"]

    @property
    def tri_count(self):
        return self.arrays["tri_count"]

    @property
    def tri_region(self):
        return self.arrays["tri_region"]

    @property
    def length(self):
        return self.arrays["length"]

    @property
    def uv(self):
        return self.arrays["uv"]

    @property
    def length_random(self):
        """(amount, minimum factor): header +0x60, two half floats."""
        return tuple(float(x) for x in np.frombuffer(self.buffer, "<f2", 2, 0x60))

    def seed(self, strand_class=0):
        return self.header[STRAND_CLASS_SEED[strand_class] // 4]

    def bone_weights(self):
        w = self.arrays["bone_weight"].astype(np.float32)
        return w / np.maximum(w.sum(1, keepdims=True), 1e-6)

    def summary(self):
        seg = np.linalg.norm(np.diff(self.position, axis=1), axis=2).sum(1)
        return {
            "archive": self.archive, "offset": hex(self.offset), "guides": self.guides,
            "nodesPerGuide": self.nodes_per_guide, "triangles": self.triangles_n, "flags": hex(self.flags),
            "box": self.box.round(5).tolist(), "strandTotals": list(self.strand_totals),
            "strandsFromTriangles": self.tri_count.sum(0).astype(int).tolist(),
            "regions": self.regions, "bufferBytes": len(self.buffer),
            "guideLengthM": [round(float(seg.min()), 5), round(float(seg.mean()), 5), round(float(seg.max()), 5)],
            "storedLengthMaxErrorM": round(float(np.abs(seg - self.length).max()), 6),
            "lengthRandom": [round(v, 4) for v in self.length_random],
            "optionalArrays": sorted(n for n, p in self.present.items() if p and n.startswith(("ext", "node_", "direction"))),
            "tailBytes": len(self.tail),
        }


def parse(system, buffer, archive="", offset=0):
    """Decode one groom LOD from its inflated system page and GPU buffer."""
    if len(buffer) < 0xA0:
        raise GroomError("asset buffer shorter than its header")
    h = struct.unpack_from("<40I", buffer)
    guides, tris = h[0x58 // 4] & 0xFFFF, h[0x58 // 4] >> 16
    nodes, ppg = h[0x54 // 4] >> 8, h[0x5C // 4] & 0xFF
    if not guides or not tris or not ppg or nodes != guides * ppg:
        raise GroomError(f"inconsistent counts: {guides} guides x {ppg} nodes != {nodes}")
    count = {"G": guides, "N": nodes, "T": tris}
    raw, present = {}, {}
    for at, (name, size, domain) in ARRAYS.items():
        start = h[at // 4]
        present[name] = bool(start)
        if not start:
            continue
        end = start + size * count[domain]
        if start < 0xA0 or end > len(buffer):
            raise GroomError(f"array {name} ({start:#x}..{end:#x}) outside the {len(buffer)}-byte buffer")
        raw[name] = (start, count[domain])
    for needed in ("position", "triangle", "tri_count", "length", "bone_index", "bone_weight", "uv"):
        if needed not in raw:
            raise GroomError(f"asset has no {needed} array")

    def view(name, dtype, width):
        start, n = raw[name]
        return np.frombuffer(buffer, dtype, n * width, start).reshape(n, width)

    arrays = {
        "position": view("position", "<f2", 4).astype(np.float32).reshape(guides, ppg, 4),
        "triangle": view("triangle", "<u2", 3),
        "tri_count": view("tri_count", "u1", 4),
        "tri_region": view("tri_region", "u1", 1)[:, 0] if "tri_region" in raw else np.zeros(tris, np.uint8),
        "length": view("length", "<f2", 1)[:, 0].astype(np.float32),
        "uv": view("uv", "<f2", 2).astype(np.float32),
        "bone_index": view("bone_index", "u1", 8),
        "bone_weight": view("bone_weight", "u1", 8),
    }
    for name, width in (("attr_a", 4), ("attr_b", 4), ("attr_c", 4), ("ext_18", 12), ("ext_1c", 8),
                        ("ext_24", 4), ("direction", 4), ("node_4", 4), ("node_2", 2)):
        if name in raw:
            arrays[name] = view(name, "u1", width)
    if arrays["triangle"].max() >= guides:
        raise GroomError("triangle references a guide past the guide count")
    if not np.isfinite(arrays["position"]).all():
        raise GroomError("non-finite node position")
    tail = b""
    if h[TAIL_OFFSET // 4]:
        # 0x3c is a further optional array (seen in 39 assets, element size unknown): it bounds the tail too.
        starts = sorted([s for s, _ in raw.values()] + [h[0x3C // 4]])
        after = [s for s in starts if s > h[TAIL_OFFSET // 4]]
        tail = buffer[h[TAIL_OFFSET // 4]:after[0] if after else len(buffer)]

    box = np.array([struct.unpack_from("<3f", system, 0x30), struct.unpack_from("<3f", system, 0x40)], np.float32)
    totals = struct.unpack_from("<II", system, 0xD0)
    regions = []
    n_reg = struct.unpack_from("<I", system, 0xA8)[0]
    if 0 < n_reg <= 64 and struct.unpack_from("<Q", system, 0xB0)[0]:
        at = _sys_ptr(system, 0xB0)
        for k in range(n_reg):
            if at + 12 * k + 12 > len(system):
                break
            n_tri, n_guide, n_s0, pad, rid, n_s1 = struct.unpack_from("<HHHBBI", system, at + 12 * k)
            regions.append({"id": rid, "triangles": n_tri, "guides": n_guide, "strands": [n_s0, n_s1]})
    return Groom(archive, offset, bytes(system), bytes(buffer), h, guides, tris, nodes, ppg, h[0x50 // 4],
                 box, totals, regions, arrays, present, tail)


def read(src, offset, archive=""):
    """Read the groom LOD resource whose 16-byte zero prefix starts at offset."""
    system, pos = inflate(src, offset + 16)
    if not system_page_is_groom(system):
        raise GroomError("not a groom LOD resource")
    desc = _sys_ptr(system, 0x70)
    size = struct.unpack_from("<I", system, desc + 8)[0] * 4
    gpu = bytearray()
    while len(gpu) < size:
        if src[pos:pos + 1] != b"x":
            raise GroomError("asset buffer stream missing")
        chunk, pos = inflate(src, pos)
        gpu.extend(chunk)
    return parse(system, bytes(gpu[:size]), archive, offset)


_maps = {}


def open_archive(name):
    """Shared read-only memory map of an archive in the game folder."""
    try:
        path = archive_path(name)
    except ValueError as exc:
        raise GroomError(str(exc)) from None
    if name not in _maps:
        handle = path.open("rb")
        _maps[name] = (handle, mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ))
    return _maps[name][1]


def load(archive, offset):
    return read(open_archive(archive), offset, archive)


def scan(src, start=0, stop=None, chunk=64 << 20):
    """Yield the offset of every groom LOD resource in an archive (16-aligned zero prefix + zlib page)."""
    stop = len(src) if stop is None else stop
    for base in range(start // 16 * 16, stop, chunk):
        block = src[base:base + chunk + 17]
        at = -1
        while True:
            at = block.find(PREFIX, at + 1)
            if at < 0 or at >= chunk:
                break
            if at % 16:
                continue
            try:
                head = zlib.decompressobj().decompress(src[base + at + 16:base + at + 16 + 4096], 4096)
            except zlib.error:
                continue
            if system_page_is_groom(head):
                yield base + at


# --- strand generation (port of smh_gen_1_CS_InterpolateStrands, FSSM pc 136-1504 and 4116-4460) ----------

def _mul24(a, b):
    """v_mad_u32_u24 product: low 24 bits of each operand, 32-bit result."""
    return (a & U32(0xFFFFFF)) * (b & U32(0xFFFFFF))


def _pcg(state):
    """pc 4116-4264: PCG output hash folded to 24 bits, as a float in [0, 1)."""
    v = state * U32(0x2C9277B5) + U32(0xAC564B05)
    w = ((v >> ((v >> U32(28)) + U32(4))) ^ v) * U32(0x108EF2D9)
    w = (w >> U32(22)) ^ (w & U32(0xFFFFFF))
    return w.astype(np.float32) * np.float32(2.0 ** -24)


def scatter(groom, count=None, strand_class=0, density_scale=None, region_mask=None):
    """Strand roots exactly as the shader picks them.

    count           strands to generate (thread ids 0..count-1). Default: the asset's class total.
    density_scale   acceptance = saturate(trianglesStrands * density_scale). The game supplies this per
                    instance; the default makes the densest triangle always accepted.
    region_mask     iterable of region ids to cull (the style record's 256-bit mask). Default: none.
    Returns dict: tri, culled, bary [n,3], seed (per-strand hash input for later random numbers).
    """
    per_tri = groom.tri_count[:, strand_class].astype(np.float32)
    if count is None:
        count = int(groom.strand_totals[strand_class]) or int(per_tri.sum())
    if density_scale is None:
        density_scale = 1.0 / max(float(per_tri.max()), 1.0)
    blocked = np.zeros(256, bool)
    for rid in region_mask or ():
        blocked[int(rid) & 255] = True
    blocked[0] = False
    T = U32(groom.triangles_n)
    with np.errstate(over="ignore"):
        t = np.arange(count, dtype=U32)
        v3 = t * U32(0x19660D) + U32(0xEF3EFB11)
        tri = np.zeros(count, U32)
        v20 = np.zeros(count, U32)
        culled = np.zeros(count, bool)
        searching = np.ones(count, bool)
        for attempt in range(4):
            s0 = U32(attempt) * U32(0x19660D) + U32(0x86945BED)
            v11 = s0 * U32(0x153E39CE) + v3
            v5 = v11 * U32(0x31CD17D1) + U32(0x153E39CE)
            v9 = v11 >> U32(16)
            v6 = v5 * v11 + U32(0x31CD17D1)
            v7 = v6 * v5
            v5 = v5 ^ (v5 >> U32(16))
            v6 = v6 ^ (v6 >> U32(16))
            v7 = v7 + s0
            v7 = v7 ^ (v7 >> U32(16))
            v8 = (v9 ^ v11) + v7 * v5
            v10 = v8 * v6 + v5
            pick = (v8 >> U32(16)) % T
            v5 = v10 * v8 + v6
            v6 = v5 * v10 + v7
            masked = blocked[groom.tri_region[pick]]
            tri[searching] = pick[searching]
            culled[searching] = masked[searching]
            live = searching & ~masked
            v20[live] = (v5 >> U32(8))[live]
            u = (v6 & U32(0xFFFFFF)).astype(np.float32) * np.float32(2.0 ** -24)
            accept = live & (u < np.clip(per_tri[pick] * np.float32(density_scale), 0.0, 1.0))
            searching &= ~accept
        # Barycentric point (pc 1204-1504).
        seed = U32(0x3C1) * (tri & U32(0xFFFF)) + U32(groom.seed(strand_class)) * U32(0x8A30D) + v20 * U32(31)
        k = U32(0xC083C5)
        x1 = _mul24(k, seed) + U32(0x3C6EF35F)
        x2 = _mul24(k, x1) + U32(0x3C6EF35F)
        x1 = _mul24(k, x2) + x1
        x2 = _mul24(k, x1) + x2
        x1 = x1 ^ (x1 >> U32(12))
        x2 = x2 ^ (x2 >> U32(12))
        x1 = _mul24(k, x2) + x1
        a = (x1 & U32(0xFFFFFF)) ^ (x1 >> U32(12))
        x1 = _mul24(k, x1) + x2
        b = (x1 & U32(0xFFFFFF)) ^ (x1 >> U32(12))
    u1 = a.astype(np.float32) * np.float32(2.0 ** -24)
    u2 = b.astype(np.float32) * np.float32(2.0 ** -24)
    r = np.sqrt(u1)
    bary = np.stack((1.0 - r, (1.0 - u2) * r, u2 * r), axis=1).astype(np.float32)
    return {"tri": tri.astype(np.int64), "culled": culled, "bary": bary, "seed": seed}


def strands(groom, count=None, strand_class=0, density_scale=None, region_mask=None, max_strands=None):
    """Rest-pose strand polylines.

    Returns dict: points [n, nodes_per_guide, 3] (trimmed strands repeat their tip), live [n] node counts
    that are distinct, length [n] metres, uv [n,2], tri [n], bary [n,3].
    """
    roots = scatter(groom, count, strand_class, density_scale, region_mask)
    keep = ~roots["culled"]
    if max_strands is not None and int(keep.sum()) > max_strands:
        # Thread ids are independent, so the first ids are an unbiased subset.
        keep &= np.cumsum(keep) <= max_strands
    tri, bary, seed = roots["tri"][keep], roots["bary"][keep], roots["seed"][keep]
    idx = groom.triangle[tri].astype(np.int64)
    pos = groom.position
    blend = (bary[:, 0, None, None] * pos[idx[:, 0]] + bary[:, 1, None, None] * pos[idx[:, 1]]
             + bary[:, 2, None, None] * pos[idx[:, 2]])
    guide_len = (bary * groom.length[idx]).sum(1)
    amount, low = groom.length_random
    with np.errstate(over="ignore"):
        factor = low + _pcg(seed) * (1.0 - low)
    target = np.maximum(guide_len * (1.0 + (factor - 1.0) * amount), 0.0)
    # Walk the blended guide, clamping each segment to the length that is left (pc 5584-5650).
    seg = np.diff(blend, axis=1)
    seg_len = np.maximum(np.linalg.norm(seg, axis=2), 2.0 ** -23)
    before = np.concatenate((np.zeros((len(seg), 1), np.float32), np.cumsum(seg_len, axis=1)[:, :-1]), axis=1)
    use = np.clip(target[:, None] - before, 0.0, seg_len)
    use[use < 1e-9] = 0.0
    points = np.concatenate((blend[:, :1], blend[:, :1] + np.cumsum(seg / seg_len[..., None] * use[..., None], axis=1)), axis=1)
    live = 1 + (use > 0).sum(1)
    uv = (bary[:, :, None] * groom.uv[idx]).sum(1)
    return {"points": points.astype(np.float32), "live": live, "length": use.sum(1), "uv": uv.astype(np.float32),
            "tri": tri, "bary": bary}


# --- export -------------------------------------------------------------------------------------------

def y_up(a):
    out = a[..., [0, 2, 1]].copy()
    out[..., 2] *= -1
    return out


def tubes(points, live=None, radius=0.0004, tip=0.25, sides=3):
    """Triangle tubes around polylines [n, k, 3] -> (vertices, normals, indices uint32)."""
    n, k, _ = points.shape
    if n == 0:
        return np.zeros((0, 3), np.float32), np.zeros((0, 3), np.float32), np.zeros(0, np.uint32)
    tangent = np.gradient(points, axis=1)
    norm = np.linalg.norm(tangent, axis=2, keepdims=True)
    tangent = np.where(norm > 1e-9, tangent / np.maximum(norm, 1e-9), np.array([0, 0, 1], np.float32))
    ref = np.where(np.abs(tangent[..., 2:3]) < 0.9, np.array([0, 0, 1], np.float32), np.array([1, 0, 0], np.float32))
    side = np.cross(tangent, ref)
    side /= np.maximum(np.linalg.norm(side, axis=2, keepdims=True), 1e-9)
    up = np.cross(tangent, side)
    taper = (1.0 - (1.0 - tip) * np.linspace(0.0, 1.0, k, dtype=np.float32))[None, :, None] * radius
    ang = np.arange(sides, dtype=np.float32) * (2.0 * np.pi / sides)
    ring = np.cos(ang)[None, None, :, None] * side[:, :, None, :] + np.sin(ang)[None, None, :, None] * up[:, :, None, :]
    verts = (points[:, :, None, :] + ring * taper[..., None]).reshape(-1, 3)
    normals = ring.reshape(-1, 3)
    base = (np.arange(n, dtype=np.uint32)[:, None] * (k * sides) + np.arange(k - 1, dtype=np.uint32)[None, :] * sides)
    if live is not None:
        segments = np.arange(k - 1)[None, :] < (np.asarray(live)[:, None] - 1)
    else:
        segments = np.ones((n, k - 1), bool)
    base = base[segments]
    quads = []
    for s in range(sides):
        a, b = base + s, base + (s + 1) % sides
        quads.append(np.stack((a, b, a + sides, b, b + sides, a + sides), axis=1))
    return verts.astype(np.float32), normals.astype(np.float32), np.concatenate(quads, axis=1).ravel().astype(np.uint32)


class _Glb:
    def __init__(self):
        self.data = bytearray()
        self.doc = {"asset": {"version": "2.0", "generator": "Lineup smh groom decoder"}, "scene": 0,
                    "scenes": [{"nodes": [0]}], "nodes": [], "meshes": [], "buffers": [], "bufferViews": [],
                    "accessors": []}

    def add(self, array, kind, component, bounds=False):
        array = np.ascontiguousarray(array, "<f4" if component == 5126 else "<u4")
        self.data.extend(bytes(-len(self.data) % 4))
        self.doc["bufferViews"].append({"buffer": 0, "byteOffset": len(self.data), "byteLength": array.nbytes})
        self.data.extend(array.tobytes())
        acc = {"bufferView": len(self.doc["bufferViews"]) - 1, "componentType": component,
               "count": len(array), "type": kind}
        if bounds and len(array):
            acc.update(min=array.min(0).tolist(), max=array.max(0).tolist())
        self.doc["accessors"].append(acc)
        return len(self.doc["accessors"]) - 1

    def blob(self):
        self.data.extend(bytes(-len(self.data) % 4))
        self.doc["buffers"] = [{"byteLength": len(self.data)}]
        head = json.dumps(self.doc, separators=(",", ":"), allow_nan=False).encode()
        head += b" " * (-len(head) % 4)
        return (struct.pack("<4sIIII", b"glTF", 2, 28 + len(head) + len(self.data), len(head), 0x4E4F534A) + head
                + struct.pack("<II", len(self.data), 0x004E4942) + bytes(self.data))


PART_NAMES = ("Scalp triangles", "Guides", "Strands (class 0)", "Strands (class 1)")


def to_glb(groom, max_strands=6000, guide_radius=0.0006, strand_radius=0.00035, scalp=True, guides=True,
           classes=(0, 1), curves=False, vertex_budget=360000, engine="shader", class1_shader=None, style=False):
    """GLB bytes plus a metadata dict. Parts are primitives with extras.shaderIndex 0..3 (PART_NAMES).

    curves=False: strands and guides are thin triangle tubes, which every triangle viewer draws.
    curves=True:  strands and guides are glTF line strips (mode 3), one primitive per part.
    engine="shader": strands come from executing the game's smh_gen shaders (smh_gen.py). engine="port":
    the older hand port strands() (class 0 roots exact, class 1 approximate).
    class1_shader: smh_gen_N to use for class 1 instead of the one system page +0x12d selects.
    style: apply the asset's curl profile (smh_style.py, the InterpolateStrandStyle pass) to shader strands,
    with smh_gen's own frame, flip and weight bits; the run-time amount is taken as full (1.0) and profile set 0.
    """
    glb = _Glb()
    prims, stats = [], {"strands": {}, "shown": {}, "shader": {}, "style": None}
    name = f"groom_{groom.offset:x}"

    def add_mesh(part, verts, normals, index, mode=None):
        if not len(index):
            return
        attr = {"POSITION": glb.add(y_up(verts), "VEC3", 5126, True)}
        if normals is not None:
            attr["NORMAL"] = glb.add(y_up(normals), "VEC3", 5126)
        prim = {"attributes": attr, "indices": glb.add(index, "SCALAR", 5125),
                "extras": {"shaderIndex": part, "part": PART_NAMES[part]}}
        if mode is not None:
            prim["mode"] = mode
        prims.append(prim)

    def add_lines(part, points, live=None):
        n, k, _ = points.shape
        live = np.full(n, k) if live is None else np.asarray(live)
        seg = np.arange(k - 1)[None, :] < (live[:, None] - 1)
        a = (np.arange(n, dtype=np.uint32)[:, None] * k + np.arange(k - 1, dtype=np.uint32)[None, :])[seg]
        add_mesh(part, points.reshape(-1, 3), None, np.stack((a, a + 1), axis=1).ravel(), mode=1)

    if scalp:
        root = groom.position[:, 0]
        tri = groom.triangle.astype(np.int64)
        face = np.cross(root[tri[:, 1]] - root[tri[:, 0]], root[tri[:, 2]] - root[tri[:, 0]])
        vn = np.zeros_like(root)
        for c in range(3):
            np.add.at(vn, tri[:, c], face)
        vn /= np.maximum(np.linalg.norm(vn, axis=1, keepdims=True), 1e-12)
        add_mesh(0, root, vn, tri.astype(np.uint32).ravel())
    if guides:
        if curves:
            add_lines(1, groom.position)
        else:
            add_mesh(1, *tubes(groom.position, None, guide_radius))
    for cls in classes:
        budget = max_strands if curves else min(max_strands, max(200, vertex_budget // (groom.nodes_per_guide * 3)))
        if engine == "shader":
            import smh_gen
            total = int(groom.strand_totals[cls])
            stats["strands"][cls] = total
            if not total:
                continue
            s = smh_gen.strands(groom, cls, max_strands=budget, variant=class1_shader if cls == 1 else None)
            stats["shader"][cls] = s["variant"]
            if not len(s["points"]):
                continue
            if style:
                import smh_style
                st = smh_style.apply_style(groom, s, cls, points_out=s["points"].shape[1], amount=1.0, set_index=0)
                if st["profile"] is not None:
                    s = {"points": st["points"][~st["culled"]],
                         "live": np.full(int((~st["culled"]).sum()), st["points"].shape[1], np.int64)}
                    stats["style"] = {"profile": st["profile"], "assumed": st["assumed"]}
        else:
            total = int(groom.strand_totals[cls]) or int(groom.tri_count[:, cls].sum())
            stats["strands"][cls] = total
            if not total or not groom.tri_count[:, cls].any():
                continue
            s = strands(groom, total, cls, max_strands=budget)
        stats["shown"][cls] = int(len(s["points"]))
        if curves:
            add_lines(2 + cls, s["points"], s["live"])
        else:
            add_mesh(2 + cls, *tubes(s["points"], s["live"], strand_radius))
    if not prims:
        raise GroomError("nothing to export")
    glb.doc["meshes"].append({"name": name, "primitives": prims})
    glb.doc["nodes"].append({"name": name, "mesh": 0})
    glb.doc["extras"] = {"archive": groom.archive, "member": hex(groom.offset), "groom": groom.summary(),
                         "strandTotals": stats["strands"], "strandsShown": stats["shown"],
                         "engine": engine, "shaders": {str(k): v for k, v in stats["shader"].items()},
                         "style": stats["style"],
                         "note": "Tube radii are display sizes, not the game's strand width."}
    return glb.blob(), glb.doc["extras"]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for cmd in ("info", "glb"):
        p = sub.add_parser(cmd)
        p.add_argument("archive")
        p.add_argument("offset", type=lambda s: int(s, 0))
        if cmd == "glb":
            p.add_argument("output", type=Path)
            p.add_argument("--strands", type=int, default=6000, help="most strands per class to write")
            p.add_argument("--curves", action="store_true", help="write line strips instead of tubes")
    p = sub.add_parser("scan")
    p.add_argument("archive")
    args = parser.parse_args()
    if args.command == "scan":
        for offset in scan(open_archive(args.archive)):
            print(hex(offset))
        return
    groom = load(args.archive, args.offset)
    if args.command == "info":
        print(json.dumps(groom.summary(), indent=2))
    else:
        blob, meta = to_glb(groom, max_strands=args.strands, curves=args.curves)
        args.output.write_bytes(blob)
        print(json.dumps({"bytes": len(blob), "strandTotals": meta["strandTotals"], "strandsShown": meta["strandsShown"]}))


if __name__ == "__main__":
    main()
