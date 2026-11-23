# SPDX-License-Identifier: GPL-3.0-or-later
"""Index native fragment drawable and name pointers in the local RPF9 member frames."""

import re
import struct
import zlib


BASE = 0x50000000
LIMIT = 64 << 20
UNIT = 64 << 20
PREFIX = bytes(16) + b"x"
TEXT = re.compile(rb"([a-zA-Z0-9_-]{1,255})\.#ft\x00")
BARE = re.compile(rb"([a-zA-Z0-9_-]{1,255})\x00")


def ptr(buf, pos):
    return struct.unpack_from("<Q", buf, pos)[0] - BASE


def shape(header):
    return (
        len(header) >= 0xB0
        and header[:8] == bytes(8)
        and 0 <= ptr(header, 8) < LIMIT
        and 0 <= ptr(header, 0x18) < LIMIT
        and 0 <= ptr(header, 0xA8) < LIMIT
        and header[16:20] != b"0DRP"
    )


def read_fragment(source, offset):
    """Return the embedded drawable name, system bytes and start of following stream.

    This loader follows the system-page pointer (+8), fragment drawable (+0x18)
    and both name pointers. The GPU descriptors and mesh indices are verified
    by the separate drawables.export_system decoder when geometry is requested.
    """
    data = bytearray()
    pos = offset + 16

    def grow(need):
        nonlocal pos
        if need > LIMIT or need < 0:
            raise ValueError("pointer outside supported system range")
        while len(data) < need:
            if pos >= len(source):
                raise EOFError("input window ended")
            if source[pos : pos + 1] != b"x":
                raise ValueError("expected compressed system continuation")
            dec = zlib.decompressobj()
            while not dec.eof:
                piece = source[pos : pos + 65536]
                if not piece:
                    raise EOFError("input window ended")
                data.extend(dec.decompress(piece, LIMIT + 1 - len(data)))
                if len(data) > LIMIT:
                    raise ValueError("system extent exceeds limit")
                pos += len(piece) - len(dec.unused_data)
            pos = (pos + 15) & -16

    grow(0xB0)
    if not shape(data):
        raise ValueError("unsupported fragment header")
    root = ptr(data, 0x18)
    grow(max(root + 0xB0, ptr(data, 8) + 1, ptr(data, 0xA8) + 1))
    nam = ptr(data, root + 0xA8)
    frag = ptr(data, 0xA8)
    if not 0 <= nam < LIMIT:
        raise ValueError("no embedded name pointer")
    grow(nam + 1)
    for start in (frag, nam):
        if data.find(0, start, min(len(data), start + 260)) < 0:
            grow(start + 260)
    fmt = TEXT.match(data, nam)
    plain = BARE.match(data, frag)
    if not fmt or not plain:
        raise ValueError("name shape")
    if not (0 <= ptr(data, root + 0x50) < LIMIT):
        raise ValueError("no LOD table")
    return plain[1].decode(), fmt[1].decode(), root, frag, nam, data, pos
