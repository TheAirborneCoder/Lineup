# SPDX-License-Identifier: GPL-3.0-or-later
"""Index what Lineup needs inside the game's archives.

Only plain files that are already readable on disk are opened, and only for reading. Three indexes are
written per archive under WORK/index/<archive>/:

    deflate-spans.bin    every checksum-verified zlib stream that starts on a 16-byte boundary
                         (<QQQII rows: packed offset, packed length, inflated length, Adler-32, first 4 bytes)
    *.pso, members.json  the PSIN metadata members (ped archetypes, outfits, wardrobe, decoration catalogues)
    xml/*.xml            the XML members (body shapes, status effects, damage tables)

Each index keeps a receipt with the archive's size and modification time and is skipped when both still match.
"""
import hashlib
import json
import mmap
import os
import re
import struct
import tempfile
import xml.etree.ElementTree as ET
import zlib
from collections import Counter
from pathlib import Path

import numpy as np

from decode_pso import Decoder, sections_of
from paths import GAME, INDEX
from rpf9_psin import BLOCK, decode_block, psin_checksum, recover

ROW = struct.Struct("<QQQII")
HEADER = struct.Struct("<16I")
WINDOW = 32 << 20
ZLIB_STARTS = [0x0178, 0x5E78, 0x9C78, 0xDA78]
SKIP = ("_bvh4", "_script", "core_licenses")
BASE_ARCHIVES = ("packs.rpf", "data.rpf", "common.rpf", "anim.rpf")


def full_rescan():
    return os.environ.get("LINEUP_RESCAN") == "1"


def character_archives():
    """The archives that hold characters, their catalogues, textures' owners and animations."""
    names = [n for n in BASE_ARCHIVES if (GAME / n).is_file()]
    names += sorted(p.relative_to(GAME).as_posix() for p in (GAME / "pack").glob("*/*.rpf")
                    if not any(s in p.name for s in SKIP))
    return names


def align(offset, unit):
    return (offset + unit - 1) // unit * unit


def write_json(destination, value):
    try:
        if json.loads(destination.read_bytes()) == value:
            return
    except (FileNotFoundError, ValueError):
        pass
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=destination.parent, delete=False, encoding="utf-8") as file:
        temporary = Path(file.name)
        try:
            json.dump(value, file, separators=(",", ":"))
        except BaseException:
            file.close()
            temporary.unlink(missing_ok=True)
            raise
    try:
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def is_rpf_header(words, remaining):
    return (words[0] == int.from_bytes(b"9FPR", "little")
            and HEADER.size <= words[1] <= remaining and words[1] % 16 == 0
            and words[2] <= words[1] and words[3] <= remaining - words[1]
            and words[13:16] == (0, 0, 0))


def psin(source, start):
    """One PSIN member: (bytes, end offset, block count). Checks the whole-file and per-block checksums."""
    result = recover(source, start)
    if result is None:
        raise ValueError(f"Invalid PSIN at {start:#x}")
    data, count = result
    if psin_checksum(data) != struct.unpack_from(">I", data, len(data) - 8)[0]:
        raise ValueError(f"Whole-file PSIN checksum mismatch at {start:#x}")
    position = start
    for i in range(count):
        expected = data[i * BLOCK:(i + 1) * BLOCK]
        try:
            block, end = decode_block(source, position)
        except (ValueError, zlib.error):
            end = position + len(expected)
            block = source[position:end]
        if block != expected:
            raise ValueError(f"PSIN block mismatch at {position:#x}")
        position = align(end, 16)
        if any(source[end:position]):
            raise ValueError(f"Nonzero PSIN alignment at {end:#x}")
    return data, position, count


def _stamp(path):
    st = path.stat()
    return [st.st_size, st.st_mtime_ns]


# --- verified zlib spans -------------------------------------------------------------------------------------


def scan_spans(path, key, progress=None):
    folder = INDEX / key
    folder.mkdir(parents=True, exist_ok=True)
    receipt = folder / "physical-inventory.json"
    index = folder / "deflate-spans.bin"
    stamp = _stamp(path)
    if not full_rescan() and receipt.exists() and index.exists():
        old = json.loads(receipt.read_bytes())
        if old["sourceStat"] == stamp and index.stat().st_size == ROW.size * old["streams"]:
            return old
    headers = []
    total = count = inflated = covered = 0
    fingerprint = hashlib.sha256()
    with (path.open("rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as src,
          tempfile.NamedTemporaryFile(dir=folder, delete=False) as out):
        for base in range(0, len(src), WINDOW):
            if progress:
                progress(base, len(src))
            data = src[base:base + WINDOW]
            fingerprint.update(data)
            if len(data) >= HEADER.size:
                at = data.find(b"9FPR")
                while at >= 0:
                    pos = base + at
                    if pos % 16 == 0 and pos + HEADER.size <= len(src):
                        h = HEADER.unpack_from(src, pos)
                        if is_rpf_header(h, len(src) - pos):
                            headers.append([pos, h[1], h[3], h[9]])
                    at = data.find(b"9FPR", at + 1)
            words = np.frombuffer(data, dtype="<u2", count=len(data) // 16 * 8)[::8]
            for i in np.flatnonzero(np.isin(words, ZLIB_STARTS)):
                pos = base + int(i) * 16
                if pos < covered:
                    continue
                cursor = pos
                size = 0
                prefix = b""
                d = zlib.decompressobj()
                try:
                    while not d.eof:
                        chunk = src[cursor:cursor + 69632]
                        if not chunk:
                            raise ValueError("EOF")
                        raw = d.decompress(chunk, 1 << 20)
                        prefix = (prefix + raw[:4])[:4]
                        size += len(raw)
                        cursor += len(chunk) - max(len(d.unused_data), len(d.unconsumed_tail))
                except (zlib.error, ValueError):
                    continue
                covered = cursor
                length = cursor - pos
                total += length
                inflated += size
                count += 1
                out.write(ROW.pack(pos, length, size, int.from_bytes(src[cursor - 4:cursor], "big"),
                                   int.from_bytes(prefix, "little")))
        temporary = out.name
    if _stamp(path) != stamp:
        os.unlink(temporary)
        raise ValueError(f"The archive changed while it was being read: {key}")
    os.replace(temporary, index)
    result = dict(package=key, sourceStat=stamp, sha256=fingerprint.hexdigest(), headers=headers, streams=count,
                  storedStreamBytes=total, inflatedBytes=inflated,
                  bytesOutsideVerifiedStreams=stamp[0] - total, index=index.name, layout="<QQQII")
    write_json(receipt, result)
    return result


def read_spans(key):
    """[(offset, stored, inflated, adler, first word)] of an indexed archive."""
    return list(ROW.iter_unpack((INDEX / key / "deflate-spans.bin").read_bytes()))


# --- PSIN metadata members -----------------------------------------------------------------------------------


def scan_members(path, key, progress=None):
    folder = INDEX / key
    folder.mkdir(parents=True, exist_ok=True)
    receipt = folder / "members.json"
    stamp = _stamp(path)
    if not full_rescan() and receipt.exists():
        old = json.loads(receipt.read_bytes())
        if old["sourceStat"] == stamp and all((folder / m["file"]).exists() for m in old["members"]):
            return old
    members, rejected = [], []
    with path.open("rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as source:
        window = 16 << 20
        covered = 0
        for base in range(0, len(source), window):
            if progress:
                progress(base, len(source))
            buf = f.read(window)
            pairs = np.frombuffer(buf[:len(buf) // 16 * 16], dtype="<u2")[::8]
            for i in np.flatnonzero(np.isin(pairs, [0x5350, *ZLIB_STARTS])):
                at = base + 16 * int(i)
                if at < covered:
                    continue
                probe = source[at:at + 512]
                if probe[:4] != b"PSIN":
                    try:
                        prefix = zlib.decompressobj().decompress(probe, 4)
                    except zlib.error:
                        continue
                    if prefix != b"PSIN":
                        continue
                try:
                    blob, end, _ = psin(source, at)
                    d = Decoder(sections_of(blob)[0], {})
                except (ValueError, zlib.error, struct.error, KeyError, IndexError, UnicodeError) as exc:
                    rejected.append([at, str(exc)])
                    continue
                covered = end
                name = f"{at:012x}.pso"
                (folder / name).write_bytes(blob)
                members.append({"file": name, "offset": at, "root": hex(d.blocks[d.root - 1][0])})
    result = {"package": key, "sourceStat": stamp, "members": members, "rejected": rejected}
    write_json(receipt, result)
    return result


def members_by_root(key):
    """{root type hash (int): [member file path, ...]} of an indexed archive."""
    folder = INDEX / key
    found = {}
    try:
        receipt = json.loads((folder / "members.json").read_bytes())
    except (OSError, ValueError):
        return found
    for m in receipt["members"]:
        found.setdefault(int(m["root"], 16), []).append(folder / m["file"])
    return found


# --- XML members ---------------------------------------------------------------------------------------------


def _recover_xml(f, at):
    data = bytearray()
    pos = at
    for _ in range(1024):
        block, end = decode_block(f, pos)
        data.extend(block)
        if len(block) != 65536:
            raw = bytes(data).rstrip(b"\x00")
            ET.fromstring(raw)
            return raw
        pos = (end + 15) & ~15
    raise ValueError("XML member exceeds supported size")


def scan_xml(path, key, progress=None):
    """XML members that start with a declaration."""
    folder = INDEX / key / "xml"
    folder.mkdir(parents=True, exist_ok=True)
    receipt = folder / "index.json"
    stamp = _stamp(path)
    if not full_rescan() and receipt.exists():
        old = json.loads(receipt.read_bytes())
        if old["stat"] == stamp and all((folder / f).is_file() for f in old.get("files", ())):
            return
    errors, roots, files = Counter(), Counter(), []
    with path.open("rb") as reader, path.open("rb") as probe:
        for base in range(0, stamp[0], WINDOW):
            if progress:
                progress(base, stamp[0])
            data = reader.read(WINDOW)
            pairs = np.frombuffer(data[:len(data) // 16 * 16], dtype="<u2")[::8]
            for i in np.flatnonzero(np.isin(pairs, ZLIB_STARTS)):
                start = 16 * int(i)
                chunk = data[start:start + 256]
                if len(chunk) < 256:
                    probe.seek(base + start)
                    chunk = probe.read(256)
                try:
                    prefix = zlib.decompressobj().decompress(chunk, 32)
                except zlib.error:
                    continue
                if not prefix.lstrip(b"\xef\xbb\xbf \t\n\r").startswith(b"<?xml"):
                    continue
                try:
                    raw = _recover_xml(probe, base + start)
                    root = ET.fromstring(raw)
                    roots[root.tag] += 1
                    files.append(f"{base + start:012x}.xml")
                    (folder / f"{base + start:012x}.xml").write_bytes(raw)
                except (ET.ParseError, ValueError, zlib.error) as e:
                    errors[type(e).__name__] += 1
    write_json(receipt, {"stat": stamp, "files": files, "roots": roots, "errors": errors})


def supplement_xml(path, key):
    """Declaration-free or larger single-stream XML members, read from the verified spans."""
    folder = INDEX / key
    span_index = folder / "deflate-spans.bin"
    receipt = folder / "xml/index.json"
    if not span_index.exists() or not receipt.exists():
        return
    current = json.loads(receipt.read_bytes())
    physical = json.loads((folder / "physical-inventory.json").read_bytes())
    if current.get("physicalSource") == physical["sha256"] and not full_rescan():
        return
    spans = {offset: (stored, size, first)
             for offset, stored, size, _, first in ROW.iter_unpack(span_index.read_bytes())}
    files = set(current["files"])
    roots = Counter(current["roots"])
    with path.open("rb") as f:
        for offset, (stored, size, first) in spans.items():
            filename = f"{offset:012x}.xml"
            head = first.to_bytes(4, "little").lstrip()
            if filename in files or not head.startswith((b"<", b"\xef\xbb\xbf", b"\xff\xfe", b"\xfe\xff")):
                continue
            if size > 32 << 20:
                continue
            f.seek(offset)
            probe = zlib.decompressobj().decompress(f.read(min(stored, 512)), 128)
            for enc in ("utf-8-sig", "utf-16"):
                try:
                    text = probe.decode(enc, errors="ignore").lstrip()
                except UnicodeError:
                    continue
                if re.match(r"<(?:\?xml|[A-Za-z_][\w:.-]*(?:\s|/?>))", text):
                    break
            else:
                continue
            f.seek(offset)
            raw = zlib.decompress(f.read(stored))
            end = offset + stored
            while True:
                try:
                    root = ET.fromstring(raw.rstrip(b"\x00") if enc == "utf-8-sig" else raw)
                except ET.ParseError as exc:
                    nxt = (end + 15) & -16
                    if exc.code not in (3, 5, 6) or nxt not in spans or len(raw) + spans[nxt][1] > 32 << 20:
                        break
                    length = spans[nxt][0]
                    f.seek(nxt)
                    raw += zlib.decompress(f.read(length))
                    end = nxt + length
                    continue
                (folder / "xml" / filename).write_bytes(raw)
                files.add(filename)
                roots[root.tag] += 1
                break
    current.update(files=sorted(files), roots=roots, physicalSource=physical["sha256"])
    write_json(receipt, current)


def xml_members(key):
    folder = INDEX / key / "xml"
    try:
        return [folder / name for name in json.loads((folder / "index.json").read_bytes())["files"]]
    except (OSError, ValueError, KeyError):
        return []
