# SPDX-License-Identifier: GPL-3.0-or-later
"""Read native resources page by page and find the drawables they hold.

A resource starts with 16 zero bytes followed by zlib-compressed CPU ("system") pages. Pointers inside the
pages are virtual addresses based at 0x50000000. discover() recognises the three layouts that carry drawable
names: a dictionary (paired hash and pointer tables), a standalone drawable, and a fragment whose drawable
sits behind the pointer at +0x18.
"""

import mmap
import re
import struct
import threading
import zlib

BASE = 0x50000000
MAX_SYSTEM = 128 << 20
PTR = struct.Struct("<Q")
PAIR = struct.Struct("<HH")
NAME = re.compile(rb"([a-zA-Z0-9_+.#-]{1,255})\.#(?:dr|ft)\x00")
NATIVE_ERRORS = (ValueError, IndexError, struct.error, zlib.error)
_maps = {}
_maps_lock = threading.Lock()


def source(path):
    with _maps_lock:
        if path not in _maps:
            with path.open("rb") as stream:
                _maps[path] = mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ)
        return _maps[path]


class Pages:
    """Read consecutive native system pages as far as a validated pointer needs."""

    def __init__(self, src, member):
        self.src = src
        self.pos = member + 16
        self.data = bytearray()

    def grow(self, end):
        if end < 0 or end > MAX_SYSTEM:
            raise ValueError("pointer outside supported CPU address range")
        while len(self.data) < end:
            if self.src[self.pos : self.pos + 1] != b"x":
                raise ValueError("native CPU page continuation missing")
            decoder = zlib.decompressobj()
            while not decoder.eof:
                piece = self.src[self.pos : self.pos + 32768]
                if not piece:
                    raise ValueError("truncated CPU page")
                chunk = decoder.decompress(piece, MAX_SYSTEM + 1 - len(self.data))
                self.data.extend(chunk)
                if len(self.data) > MAX_SYSTEM:
                    raise ValueError("oversized CPU resource")
                self.pos += len(piece) - len(decoder.unused_data)
            self.pos = (self.pos + 15) & -16

    def ptr(self, at, size):
        self.grow(at + 8)
        addr = PTR.unpack_from(self.data, at)[0] - BASE
        if not 0 <= addr < MAX_SYSTEM:
            raise ValueError("unsupported CPU pointer")
        self.grow(addr + size)
        return addr


def discover(src, member):
    """Identify additional typed drawable roots in indexed physical RPF streams."""
    off = member + 16
    if src[member:off] != bytes(16):
        return []
    prefix = zlib.decompressobj().decompress(src[off : off + 2048], 176)
    if len(prefix) < 64 or prefix[:8] != bytes(8):
        return []

    def ptr(at):
        return PTR.unpack_from(prefix, at)[0] - BASE

    if not 0 <= ptr(8) < MAX_SYSTEM:
        return []
    pages = Pages(src, member)
    if (
        prefix[16:24] == bytes(8)
        and 0 <= ptr(32) < MAX_SYSTEM
        and 0 <= ptr(48) < MAX_SYSTEM
    ):
        n, cap = PAIR.unpack_from(prefix, 40)
        if not 0 < n <= cap or prefix[40:44] != prefix[56:60]:
            return []
        table = pages.ptr(48, 8 * n)
        roots = [(pages.ptr(table + 8 * i, 176), "dictionary") for i in range(n)]
    elif len(prefix) == 176 and 0 <= ptr(168) < MAX_SYSTEM:
        roots = []
        if 0 <= ptr(16) < MAX_SYSTEM and 0 <= ptr(80) < MAX_SYSTEM:
            roots.append((0, "standalone"))
        if 0 <= ptr(24) < MAX_SYSTEM:
            roots.append((ptr(24), "fragment"))
    else:
        return []
    found = []
    for root, kind in roots:
        try:
            n = pages.ptr(root + 168, 1)
            end = pages.data.find(0, n, n + 261)
            if end < 0:
                pages.grow(n + 261)
            match = NAME.match(pages.data, n)
            if match:
                found.append((root, match[1].decode("ascii"), kind))
        except NATIVE_ERRORS:
            continue
    return found
