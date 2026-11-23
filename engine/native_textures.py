# SPDX-License-Identifier: GPL-3.0-or-later
"""Index the textures stored inside model resources (embedded texture dictionaries), from the span index."""
import gzip
import json
import os
from pathlib import Path
import struct
import tempfile
import zlib

from script_tree import _name_hash
from ttpf_format import DXGI
from paths import GAME, INDEX

SPAN = struct.Struct("<QQQII")
SYS = 0x50000000
GPU = 0x60000000
VERSION = 4
LIMIT = 256 << 20
U16 = struct.Struct("<H")
U32 = struct.Struct("<I")
U64 = struct.Struct("<Q")


def word(data, at):
    return U32.unpack_from(data, at)[0]


def pointer(data, at, base=SYS):
    return U64.unpack_from(data, at)[0] - base


def adjacent(rows, i):
    return i + 1 < len(rows) and (rows[i][0] + rows[i][1] + 15) & -16 == rows[i + 1][0]


def dictionary(head):
    return (len(head) >= 64 and head[:8] == bytes(8) and 0 <= pointer(head, 8) < LIMIT
            and head[16:24] == bytes(8) and head[40:44] == head[56:60]
            and U16.unpack_from(head, 40)[0] > 0 and 0 <= pointer(head, 32) < LIMIT
            and 0 <= pointer(head, 48) < LIMIT)


class Window:
    """Coalesce nearby reads on the large mechanical-disk package installation."""
    def __init__(self, stream):
        self.stream = stream
        self.base = 0
        self.data = b""

    def read(self, at, size):
        if at < self.base or at + size > self.base + len(self.data):
            self.base = at & -4096
            self.stream.seek(self.base)
            self.data = self.stream.read(max(8 << 20, at + size - self.base))
        pos = at - self.base
        return self.data[pos:pos + size]


class Spans:
    def __init__(self, data):
        self.data = data

    def __len__(self):
        return len(self.data) // SPAN.size

    def __getitem__(self, n):
        return SPAN.unpack_from(self.data, n * SPAN.size)


def system_resource(src, spans, i, page_map):
    """Follow the system/graphics page counts in the page-map record."""
    last = i - 1
    data = bytearray()

    def add():
        nonlocal last
        if last >= i and not adjacent(spans, last):
            raise ValueError("Non-contiguous CPU pages")
        last += 1
        at, length, inflated, *_ = spans[last]
        if len(data) + inflated > LIMIT:
            raise ValueError("Oversize CPU resource")
        data.extend(zlib.decompress(src.read(at, length)))

    while len(data) < page_map + 16:
        add()
    system_count, graphic_count = data[page_map + 8:page_map + 10]
    if (data[page_map:page_map + 8] != bytes(8) or data[page_map + 10:page_map + 16] != bytes(6)
            or not system_count):
        raise ValueError("Unrecognized native page map")
    # Compression chunks can split an allocation page. A partial final CPU
    # block ends the system section even when the page map has further entries;
    # a 64 KiB-aligned following span belongs to the GPU bulk section instead.
    while (last - i + 1 < system_count and len(data) % 65536 == 0 and adjacent(spans, last)
           and spans[last + 1][2] < 65536):
        add()
    gpu = []
    if (graphic_count and last - i + 1 < system_count and len(data) % 65536 == 0
            and not adjacent(spans, last) and last + 1 < len(spans)):
        # Small 16-byte CPU tail stored uncompressed between the first CPU span and GPU bulk.
        gap_at = (spans[last][0] + spans[last][1] + 15) & -16
        if spans[last + 1][0] == gap_at + 16:
            data.extend(src.read(gap_at, 16))
            last += 1
            gpu.append(spans[last][:3])
    while graphic_count and adjacent(spans, last):
        last += 1
        gpu.append(spans[last][:3])
    return data, gpu, last


def dictionary_entries(data, pos):
    if pos < 0 or pos + 64 > len(data):
        return []
    if (data[pos:pos + 8] != bytes(8) or data[pos + 16:pos + 24] != bytes(8)
            or data[pos + 40:pos + 44] != data[pos + 56:pos + 60]):
        return []
    hashes, entries = pointer(data, pos + 32), pointer(data, pos + 48)
    count = U16.unpack_from(data, pos + 40)[0]
    if not count or not 0 <= hashes <= len(data) - 4 * count or not 0 <= entries <= len(data) - 8 * count:
        return []
    return [(pointer(data, entries + 8 * j), word(data, hashes + 4 * j), j, count) for j in range(count)]


def embedded_textures(data, gpu, package, off):
    gpu_size = sum(row[2] for row in gpu)
    result = {}
    # The root dictionary supplies hashes even for layouts with a different GPU flag.
    dictionaries = {0: dictionary_entries(data, 0)}
    sources = {entry for entry, *_ in dictionaries[0]}
    marker = bytes.fromhex("09000000000000")
    at = data.find(marker)
    while at >= 0:
        obj = at - 17
        if obj >= 0 and obj % 8 == 0 and data[at - 1] in (0x80, 0x82):
            sources.add(obj)
        at = data.find(marker, at + 1)
    for entry in sorted(sources):
        texture = texture_at(data, entry)
        if texture is None:
            continue
        name_at = pointer(data, entry + 48) + 8
        name = name_hash = ""
        if 8 <= name_at < len(data):
            # Type 6 anchors hold 64-bit texture IDs; the bytes are not a string.
            anchor = U64.unpack_from(data, name_at - 8)[0]
            linked = anchor - SYS - 5
            if anchor == 0:
                end = data.find(b"\0", name_at, name_at + 1024)
                if end > name_at:
                    try:
                        name = data[name_at:end].decode("utf8")
                    except UnicodeDecodeError:
                        pass
                    if name.isprintable():
                        name_hash = f"{_name_hash(name.lower()):08x}"
            elif 0 <= linked <= len(data) - 4 and word(data, linked) == 6 and name_at + 8 <= len(data):
                name_hash = f"{U64.unpack_from(data, name_at)[0]:016x}"
                name = name_hash
        if not name_hash:
            spec = DXGI.get(texture["dxgi"])
            if (not spec or spec[1] != texture["elementSize"] or texture["dimension"] not in range(6)
                    or texture["width"] > 16384 or texture["height"] > 16384 or texture["mips"] > 16
                    or pointer(data, entry + 40) != entry + 0x50
                    or word(data, entry + 16) not in (0x980, 0x982)
                    or texture["gpuOffset"] + texture["allocationBytes"] > gpu_size):
                continue
            name = f"Unnamed texture at 0x{entry:x}"
        rec = dict(texture, name=name, nameHash=name_hash, **location(package, off, entry))
        if gpu and texture["gpuOffset"] + texture["allocationBytes"] <= gpu_size:
            rec.update(gpuStream=gpu[0][0], gpuStreams=gpu, gpuBytes=gpu_size)
        result[entry] = rec
    if not result:
        return [], 0
    # Embedded dictionaries omit the root's own page map. Locate the dictionary
    # ownership field and require both array pointers, counts and matching stored hashes.
    flag = (1).to_bytes(8, "little")
    pos = data.find(flag, 24)
    while pos >= 0:
        root = pos - 24
        if root % 8 == 0 and root not in dictionaries:
            entries = dictionary_entries(data, root)
            if entries:
                dictionaries[root] = entries
        pos = data.find(flag, pos + 8)
    matched = set()
    for root, entries in dictionaries.items():
        for entry, stored, index, count in entries:
            rec = result.get(entry)
            if rec and rec["nameHash"] and int(rec["nameHash"], 16) == stored:
                matched.add(root)
                rec.update(dictionaryOffset=root, entry=index, dictionaryEntries=count, dictionaryHashVerified=True)
    extras = []
    for root in sorted(matched):
        for entry, stored, index, count in dictionaries[root]:
            rec = result.get(entry)
            if rec:
                if not rec["nameHash"]:
                    rec.update(name=f"{stored:08x}", nameHash=f"{stored:08x}", dictionaryOffset=root,
                               entry=index, dictionaryEntries=count, storedDictionaryHash=True)
                continue
            r = location(package, off, entry)
            r.update(id=f"{package}@{off:x}/dict{root:x}-slot{index}", kind="Unresolved texture dictionary slot",
                     name=f"{stored:08x}", nameHash=f"{stored:08x}", dictionaryOffset=root, entry=index,
                     dictionaryEntries=count, storedDictionaryHash=True, width=None, height=None, format="Unresolved")
            extras.append(r)
    return [*result.values(), *extras], len(matched)


def location(package, offset, descriptor):
    return {"id": f"{package}@{offset:x}/{descriptor:x}", "kind": "Native RPF texture",
            "package": package, "offset": offset, "descriptor": descriptor}


def texture_at(data, pos):
    if pos < 0 or len(data) < pos + 0x50 or data[pos:pos + 8] != bytes(8):
        return None
    gpu = pointer(data, pos + 24, GPU)
    if not (0 <= gpu < 1 << 28 and 0 <= pointer(data, pos + 40) <= len(data) - 16):
        return None
    width, height, layers, dimension, dxgi, tile_hint, _, mips = struct.unpack_from("<HHHBBBBB", data, pos + 56)
    elements = word(data, pos + 8)
    element_size = U16.unpack_from(data, pos + 12)[0]
    if not width or not height or not layers or not mips or not elements or element_size not in (1, 2, 4, 8, 16):
        return None
    return dict(width=width, height=height, depth=layers, mips=mips, dimension=dimension, dxgi=dxgi,
                format=DXGI.get(dxgi, (f"DXGI_{dxgi}",))[0], tileHint=tile_hint, gpuOffset=gpu,
                allocationBytes=elements * element_size, elementSize=element_size)


def build_pack(package):
    path = GAME / package
    directory = INDEX / package
    inventory = directory / "physical-inventory.json"
    binary = directory / "deflate-spans.bin"
    stat = path.stat()
    stamp = [stat.st_size, stat.st_mtime_ns]
    result = dict(package=package, streams=0, rows=0, dictionaries=0, headerCandidates=0,
                  sourceStat=stamp, records=[], version=VERSION, status="Physical span inventory missing")
    if not inventory.is_file() or not binary.is_file():
        return result
    receipt = json.loads(inventory.read_bytes())
    if receipt["sourceStat"] != stamp or binary.stat().st_size != SPAN.size * receipt["streams"]:
        result["status"] = "Physical span inventory differs from source"
        return result
    index_stamp = [inventory.stat().st_mtime_ns, binary.stat().st_mtime_ns]
    cache = directory / f"studio-native-textures-v{VERSION}.json.gz"
    material_spans = directory / "atem-records.bin"
    if cache.is_file():
        with gzip.open(cache, "rb") as stream:
            prior = json.load(stream)
        if (prior.get("version") == VERSION and prior.get("sourceStat") == stamp
                and prior.get("indexStamp") == index_stamp and material_spans.is_file()
                and material_spans.stat().st_size == 12 * prior.get("materialMarkers", -1)):
            return prior
    result.update(indexStamp=index_stamp, streams=receipt["streams"], headers=len(receipt["headers"]),
                  bytesOutsideVerifiedStreams=receipt["bytesOutsideVerifiedStreams"],
                  status="Verified compressed spans scanned")
    unknown = 0
    systems = 0
    material_offsets = bytearray()
    issues = []
    shader_spans = 0
    with path.open("rb") as file:
        src = Window(file)
        data = binary.read_bytes()
        spans = Spans(data)
        consumed = -1
        for i, (at, length, size, _, prefix) in enumerate(SPAN.iter_unpack(data)):
            if i <= consumed or prefix or size < 64:
                continue
            head = zlib.decompressobj().decompress(src.read(at, min(length, 1024)), 64)
            if len(head) < 64 or head[:8] != bytes(8):
                continue
            page_map = pointer(head, 8)
            if not 0 <= page_map <= LIMIT - 16 or page_map % 8:
                continue
            result["headerCandidates"] += 1
            try:
                system, gpu, last = system_resource(src, spans, i, page_map)
            except (ValueError, IndexError, struct.error, zlib.error) as exc:
                issues.append({"offset": at, "issue": str(exc), "dictionaryShape": dictionary(head)})
                unknown += 1
                continue
            consumed = last
            systems += 1
            mark = system.find(b"atem")
            if mark >= 0:
                shader_spans += 1
            while mark >= 0:
                if mark >= 4 and mark % 4 == 0:
                    material_offsets.extend(struct.pack("<QI", at, mark - 4))
                mark = system.find(b"atem", mark + 4)
            textures, dict_count = embedded_textures(system, gpu, package, at)
            result["records"].extend(textures)
            result["dictionaries"] += dict_count
    result.update(rows=len(result["records"]), unresolvedCandidateStreams=unknown, nativeResources=systems,
                  issues=issues, materialMarkerSpans=shader_spans, materialMarkers=len(material_offsets) // 12)
    if (path.stat().st_size, path.stat().st_mtime_ns) != tuple(stamp):
        raise ValueError(f"Package changed during texture inventory: {package}")
    with tempfile.NamedTemporaryFile(dir=directory, delete=False) as temp:
        temp.write(material_offsets)
    os.replace(temp.name, material_spans)
    with tempfile.NamedTemporaryFile(dir=directory, delete=False) as temp:
        temp_name = temp.name
        with gzip.GzipFile(fileobj=temp, mode="wb", compresslevel=1) as stream:
            stream.write(json.dumps(result, separators=(",", ":")).encode())
    try:
        os.replace(temp_name, cache)
    finally:
        Path(temp_name).unlink(missing_ok=True)
    return result
