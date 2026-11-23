# SPDX-License-Identifier: GPL-3.0-or-later
"""The `names` stage's matching and the PSO type-code tables, on synthetic data."""
import struct
import unittest

import _setup
import decode_pso
import names
from script_tree import _name_hash

SCRATCH = _setup.SCRATCH  # importing _setup puts engine/ on sys.path and uses throwaway folders


class Matching(unittest.TestCase):
    def test_a_word_is_kept_when_it_hits_a_wanted_hash_as_written_or_in_lower_case(self):
        wanted = {_name_hash("Archetype"): "field", _name_hash("cs_alex"): "hash field value"}
        found, clashes = names.match({"Archetype", "CS_Alex", "unrelated"}, wanted)
        self.assertEqual(found, {_name_hash("Archetype"): "Archetype", _name_hash("cs_alex"): "cs_alex"})
        self.assertEqual(clashes, 0)

    def test_a_hash_reached_by_two_different_words_is_dropped(self):
        h = _name_hash("alpha")
        # Two different spellings of one hash cannot be told apart: simulate by patching the hash function.
        original = names._name_hash
        names._name_hash = lambda text: h if text in ("alpha", "beta") else 0
        try:
            found, clashes = names.match({"alpha", "beta"}, {h: "field"})
        finally:
            names._name_hash = original
        self.assertEqual((found, clashes), ({}, 1))

    def test_hash_offsets_finds_nested_hash_fields(self):
        # structure 1: hash32 at +0, a nested structure 2 at +8 whose hash32 is at +4
        schemas = {
            1: (16, 0, [(10, 0, 0x4A, 0), (11, 8, 0x05, 2)], {0: 8, 8: 16}),
            2: (8, 0, [(12, 4, 0x4A, 0)], {4: 8}),
        }
        self.assertEqual(names.hash_offsets(schemas, 1), [0, 12])


class TypeTables(unittest.TestCase):
    def test_flag_and_vector_codes(self):
        self.assertEqual(decode_pso.FORMATS[0x63], "B")
        self.assertEqual(decode_pso.TYPE_NAMES[0x63], "flags8")
        self.assertEqual(decode_pso.TYPE_NAMES[0x33], "vector3_in_16")

    def test_format_widths(self):
        widths = {code: struct.calcsize("<" + fmt) for code, fmt in decode_pso.FORMATS.items()}
        self.assertEqual(widths[0x01], 1)
        self.assertEqual(widths[0x21], 4)
        self.assertEqual(widths[0x4A], 4)
        self.assertEqual(widths[0x33], 12)
        self.assertEqual(widths[0x34], 16)

    def test_every_format_code_has_a_label(self):
        for code in decode_pso.FORMATS:
            self.assertIn(code, decode_pso.TYPE_NAMES)

    def test_a_field_with_less_room_than_its_format_is_returned_raw(self):
        decoder = decode_pso.Decoder.__new__(decode_pso.Decoder)
        decoder.names, decoder.schemas, decoder.enums, decoder.blocks = {}, {}, {}, []
        self.assertEqual(decoder.value(b"\x05", 0x15, 0), {"raw_hex": "05"})        # u32 with 1 byte of room
        self.assertEqual(decoder.value(b"\x05", 0x63, 0), 5)                        # flags8 fits in 1 byte
        self.assertEqual(decoder.value(bytes([0, 0, 0x80, 0x3F]), 0x21, 0), 1.0)


if __name__ == "__main__":
    unittest.main()
