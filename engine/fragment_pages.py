# SPDX-License-Identifier: GPL-3.0-or-later
"""Locate all CPU-side tables referenced by the primary fragment drawable's first LOD."""

import struct
import zlib

BASE = 0x50000000
LIMIT = 64 << 20
PAGE = 65536
U64 = struct.Struct("<Q")
COUNTS = struct.Struct("<HH")


def complete_system(source, system, following, root):
    """Return the CPU bytes and aligned start of GPU streams after following typed references.

    A continuation is read only when a supported system table's declared span reaches
    past a complete 64 KiB page. Non-system pointers and unsupported framing are errors.
    """

    def ensure(at, size):
        nonlocal following
        end = at + size
        if at < 0 or size < 0 or end > LIMIT:
            raise ValueError(
                "fragment system table is outside the supported address space"
            )
        while len(system) < end:
            if not system or len(system) % PAGE:
                raise ValueError(
                    "fragment system table reaches past a partial final page"
                )
            if source[following : following + 2] not in (
                b"\x78\x01",
                b"\x78\x5e",
                b"\x78\x9c",
                b"\x78\xda",
            ):
                raise ValueError("referenced fragment system continuation is not zlib")
            dec = zlib.decompressobj()
            initial = len(system)
            while not dec.eof:
                packed = source[following : following + PAGE]
                if not packed:
                    raise EOFError("incomplete fragment system continuation")
                system.extend(dec.decompress(packed, LIMIT + 1 - len(system)))
                if len(system) > LIMIT:
                    raise ValueError("fragment system continuation exceeds size limit")
                following += len(packed) - len(dec.unused_data)
            aligned = (following + 15) & -16
            if any(source[following:aligned]) or len(system) == initial:
                raise ValueError("invalid fragment continuation boundary")
            following = aligned

    def table(field, length):
        ensure(field, 8)
        at = U64.unpack_from(system, field)[0] - BASE
        ensure(at, length)
        return at

    def count(field):
        ensure(field, 4)
        n, capacity = COUNTS.unpack_from(system, field)
        if not 0 < n <= capacity <= 4096:
            raise ValueError("invalid fragment LOD array dimensions")
        return n

    lod = table(root + 0x50, 16)
    num_models = count(lod + 8)
    models = table(lod, 8 * num_models)
    for i in range(num_models):
        model = table(models + 8 * i, 8 + 0x20) + 8
        num_geometries = count(model + 8)
        entries = table(model, num_geometries * 8)
        table(model + 24, num_geometries * 2)
        for j in range(num_geometries):
            geometry = table(entries + 8 * j, 0x48)
            table(geometry + 8, 0x20)
            holder = table(geometry + 0x40, 8)
            table(holder, 0x20)
    return system, following
