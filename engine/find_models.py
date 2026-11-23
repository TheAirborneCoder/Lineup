# SPDX-License-Identifier: GPL-3.0-or-later
"""Index named drawable roots at 4096-byte archive boundaries, using their name pointer."""

import re
import struct
import zlib

NAME = re.compile(rb"([a-z0-9_-]+)\.#dr\0")
PTR = struct.Struct("<Q")
UNIT = 64 << 20


def inspect(block, off):
    # Decode only the bytes that reach root +0xa8, then the pointed-to name.
    pos = off + 16
    prefix = zlib.decompressobj().decompress(block[pos : pos + 2048], 0xB0)
    if len(prefix) < 0xB0 or prefix[16:20] == b"0DRP" or prefix[:8] != bytes(8):
        return
    if any(
        not 0x50000000 <= PTR.unpack_from(prefix, i)[0] < 0x52000000
        for i in (8, 16, 0x50, 0xA8)
    ):
        return
    nameoff = PTR.unpack_from(prefix, 0xA8)[0] - 0x50000000
    logical = 0
    while pos < len(block):
        obj = zlib.decompressobj()
        want = nameoff - logical + 256
        raw = bytearray()
        while not obj.eof and len(raw) < want:
            piece = block[pos : pos + 65536]
            if not piece:
                return
            raw.extend(obj.decompress(piece, want - len(raw)))
            pos += len(piece) - len(obj.unused_data) - len(obj.unconsumed_tail)
        if nameoff < logical + len(raw):
            hit = NAME.match(raw, nameoff - logical)
            if hit:
                return hit[1].decode(), nameoff
            return
        pos = (pos + 15) & -16
        if block[pos : pos + 1] != b"x":
            return
        logical += len(raw)
