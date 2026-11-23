# SPDX-License-Identifier: GPL-3.0-or-later
"""Find standalone, dictionary and fragment drawable names in an archive."""

import mmap
import re
import struct
import zlib
from collections import Counter

import drawables as d
from index_dictionaries import supported, read_dictionary
from find_models import inspect
from paths import GAME

PREFIX = bytes(16) + b"\x78"
FT = re.compile(rb"([a-z0-9_-]+)\.#ft\x00")
PTR = struct.Struct("<Q")
CHUNK = 64 << 20


def find_file(path, writer):
    counts = Counter()
    with (
        path.open("rb") as handle,
        mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as src,
    ):
        for base in range(0, len(src), CHUNK):
            block = src[base : base + CHUNK + 16]
            start = 0
            while True:
                at = block.find(PREFIX, start)
                if at < 0:
                    break
                start = at + 1
                if at >= CHUNK or at % 16:
                    continue
                offset = at + base
                try:
                    head = zlib.decompressobj().decompress(
                        src[offset + 16 : offset + 2064], 184
                    )
                except zlib.error:
                    continue
                if (
                    len(head) < 64
                    or head[:8] != bytes(8)
                    or not 0x50000000 <= PTR.unpack_from(head, 8)[0] < 0x58000000
                ):
                    continue
                counts["native_headers"] += 1
                archive = path.relative_to(GAME).as_posix()
                try:
                    if supported(head):
                        _, _, entries = read_dictionary(src, offset)
                        for h, root, name in entries:
                            writer.writerow(
                                (
                                    archive,
                                    hex(offset),
                                    hex(root),
                                    f"{h:08x}",
                                    name,
                                    "dictionary",
                                )
                            )
                        counts["dictionary"] += len(entries)
                    elif len(head) >= 176 and all(
                        0x50000000 <= PTR.unpack_from(head, i)[0] < 0x52000000
                        for i in (16, 80, 168)
                    ):
                        # Keep offsets local so inspect() does not traverse more than this resource.
                        result = inspect(src[offset : offset + (1 << 20)], 0)
                        if result:
                            name, _ = result
                            writer.writerow(
                                (
                                    archive,
                                    hex(offset),
                                    "0x0",
                                    f"{d._name_hash(name):08x}",
                                    name,
                                    "standalone",
                                )
                            )
                            counts["standalone"] += 1
                    elif (
                        len(head) >= 176
                        and 0x50000000 <= PTR.unpack_from(head, 24)[0] < 0x54000000
                        and head[16:20] != b"0DRP"
                    ):
                        system, gpu = d.read_stream(src, offset + 16)
                        hits = list(FT.finditer(system))
                        if len(hits) != 1:
                            continue
                        root = PTR.unpack_from(system, 24)[0] - 0x50000000
                        if root + 176 > len(system):
                            continue
                        try:
                            d.model_geometry(system, root)
                        except (ValueError, IndexError, struct.error):
                            continue
                        name = hits[0][1].decode()
                        writer.writerow(
                            (
                                archive,
                                hex(offset),
                                hex(root),
                                f"{d._name_hash(name):08x}",
                                name,
                                "fragment",
                            )
                        )
                        counts["fragment"] += 1
                except (ValueError, struct.error, zlib.error, IndexError) as exc:
                    counts["rejected_" + type(exc).__name__] += 1
        return counts
