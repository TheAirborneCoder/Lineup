# SPDX-License-Identifier: GPL-3.0-or-later
"""tools/check_no_game_data.py against planted files, and against the repository as it stands."""
import pathlib
import tempfile
import unittest

import _setup
import check_no_game_data as checker


class Planted(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)

    def plant(self, name, data):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def reasons(self, name, data):
        path = self.plant(name, data)
        return " | ".join(checker.check_file(path, self.root))

    def test_clean_text_files_pass(self):
        self.assertEqual(self.reasons("engine/a.py", b"print('hi')\n"), "")
        self.assertEqual(self.reasons("README.md", "café — text\n".encode("utf-8")), "")

    def test_blocked_extensions(self):
        for name in ("a.rpf", "b.ttpf", "c.pso", "d.sqlite3", "e.glb", "f.png", "g.dds", "h.bin", "i.s", "j.fssm"):
            self.assertIn("blocked file type", self.reasons(name, b"text"), name)

    def test_magic_numbers_even_under_an_innocent_name(self):
        cases = {"9FPR": "archive", "RDS5": "stream", "PSIN": "metadata", "ETPF": "texture pack",
                 "SQLite format 3\x00": "SQLite", "glTF": "glTF", "DDS ": "DDS", "\x89PNG\r\n\x1a\n": "PNG"}
        for magic, word in cases.items():
            self.assertIn(word, self.reasons("notes.txt", magic.encode("latin-1") + b" trailing text"), magic)

    def test_binary_and_zlib_data_are_refused(self):
        self.assertIn("not a text file", self.reasons("blob.dat", b"\x00\x01\x02\x03" * 100))
        self.assertIn("zlib", self.reasons("blob2.dat", b"\x78\x9c" + bytes(range(1, 255)) * 3))

    def test_the_logo_is_the_only_image_and_is_text(self):
        self.assertEqual(self.reasons("assets/logo.svg", b"<svg xmlns='http://www.w3.org/2000/svg'/>"), "")
        self.assertIn("blocked", self.reasons("assets/other.png", b"x"))

    def test_json_must_be_on_the_allow_list(self):
        self.assertIn("allow-list", self.reasons("dump.json", b"{}"))
        self.assertEqual(self.reasons("engine/bcpack_data/tables.json", b"{}"), "")

    def test_size_limits(self):
        self.assertIn("limit", self.reasons("big.txt", b"a" * (checker.SIZE_LIMIT + 1)))
        self.assertEqual(self.reasons("ok.txt", b"a" * (checker.SIZE_LIMIT - 1)), "")
        cap = checker.ALLOWED_DATA["engine/bcpack_data/tables.json"][1]
        self.assertIn("permits", self.reasons("engine/bcpack_data/tables.json", b" " * (cap + 1)))

    def test_whole_tree_and_given_files(self):
        self.plant("ok.py", b"x = 1\n")
        bad = self.plant("data/x.rpf", b"abc")
        self.assertEqual([p for p, _ in checker.check(root=self.root)], [str(bad)])
        self.assertEqual(checker.check([self.root / "ok.py"], self.root), [])
        self.assertEqual(checker.main([str(self.root / "ok.py")]), 0)
        self.assertEqual(checker.main([str(bad)]), 1)

    def test_every_allow_list_entry_has_a_reason(self):
        for name, (reason, limit) in checker.ALLOWED_DATA.items():
            self.assertTrue(reason.strip(), name)
            self.assertGreater(limit, 0)


class Repository(unittest.TestCase):
    def test_the_repository_holds_no_game_data(self):
        self.assertEqual(checker.check(), [])

    def test_every_bcpack_data_json_is_on_the_allow_list(self):
        for path in (_setup.ENGINE / "bcpack_data").glob("*.json"):
            self.assertIn(f"engine/bcpack_data/{path.name}", checker.ALLOWED_DATA)


if __name__ == "__main__":
    unittest.main()
