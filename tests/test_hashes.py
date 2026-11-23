# SPDX-License-Identifier: GPL-3.0-or-later
"""The two name hashes, checked against independent implementations written out in this file."""
import random
import struct
import unittest

import _setup
from script_symbol_names import murmur64
from script_tree import _name_hash

SCRATCH = _setup.SCRATCH  # importing _setup puts engine/ on sys.path and uses throwaway folders

M32 = 0xFFFFFFFF
M64 = (1 << 64) - 1


def one_at_a_time(data):
    """Bob Jenkins' one-at-a-time hash, as published (case-sensitive; the game's names are hashed as written)."""
    h = 0
    for byte in data:
        h = (h + byte) & M32
        h = (h + (h << 10)) & M32
        h ^= h >> 6
    h = (h + (h << 3)) & M32
    h ^= h >> 11
    return (h + (h << 15)) & M32


def rotl(x, r):
    return ((x << r) | (x >> (64 - r))) & M64


def fmix(k):
    k ^= k >> 33
    k = (k * 0xFF51AFD7ED558CCD) & M64
    k ^= k >> 33
    k = (k * 0xC4CEB9FE1A85EC53) & M64
    return k ^ (k >> 33)


def murmur3_x64_128_low(data):
    """MurmurHash3_x64_128 with seed 0, as published; returns the first 64-bit half."""
    c1, c2 = 0x87C37B91114253D5, 0x4CF5AD432745937F
    h1 = h2 = 0
    blocks = len(data) // 16
    for i in range(blocks):
        k1, k2 = struct.unpack_from("<QQ", data, i * 16)
        h1 ^= rotl(k1 * c1 & M64, 31) * c2 & M64
        h1 = (rotl(h1, 27) + h2) * 5 + 0x52DCE729 & M64
        h2 ^= rotl(k2 * c2 & M64, 33) * c1 & M64
        h2 = (rotl(h2, 31) + h1) * 5 + 0x38495AB5 & M64
    tail = data[blocks * 16:]
    k1 = int.from_bytes(tail[:8], "little")
    k2 = int.from_bytes(tail[8:], "little")
    if len(tail) > 8:
        h2 ^= rotl(k2 * c2 & M64, 33) * c1 & M64
    if tail:
        h1 ^= rotl(k1 * c1 & M64, 31) * c2 & M64
    h1 ^= len(data)
    h2 ^= len(data)
    h1 = (h1 + h2) & M64
    h2 = (h2 + h1) & M64
    h1, h2 = fmix(h1), fmix(h2)
    return (h1 + h2) & M64


class NameHash(unittest.TestCase):
    def test_published_vectors(self):
        # Values published for the one-at-a-time hash.
        self.assertEqual(one_at_a_time(b"a"), 0xCA2E9442)
        self.assertEqual(one_at_a_time(b"The quick brown fox jumps over the lazy dog"), 0x519E91F5)

    def test_matches_the_reference_implementation(self):
        rng = random.Random(7)
        for _ in range(500):
            text = "".join(rng.choice("abcXYZ_0189") for _ in range(rng.randrange(0, 40)))
            self.assertEqual(_name_hash(text), one_at_a_time(text.encode("ascii")), text)

    def test_empty_and_case(self):
        self.assertEqual(_name_hash(""), 0)
        self.assertNotEqual(_name_hash("Head"), _name_hash("head"))


class Murmur(unittest.TestCase):
    def test_matches_the_reference_implementation(self):
        rng = random.Random(11)
        for length in list(range(0, 40)) + [63, 64, 65, 200]:
            data = bytes(rng.randrange(256) for _ in range(length))
            self.assertEqual(murmur64(data), murmur3_x64_128_low(data), length)

    def test_empty_is_zero(self):
        self.assertEqual(murmur64(b""), 0)


if __name__ == "__main__":
    unittest.main()
