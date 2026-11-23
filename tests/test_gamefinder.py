# SPDX-License-Identifier: GPL-3.0-or-later
"""The game finder and the version check, against fake folder trees (no real game files)."""
import pathlib
import tempfile
import unittest
from unittest import mock

import _setup
import gamefinder

SCRATCH = _setup.SCRATCH  # importing _setup puts engine/ on sys.path and uses throwaway folders

CONFIG = '<Game configVersion="1"><Identity Name="x" Publisher="y" Version="{version}" /></Game>'


def make_game(folder, version="1.0.2.1", sizes=None):
    folder = pathlib.Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "MicrosoftGame.Config").write_text(CONFIG.format(version=version), "utf-8")
    for name, size in (sizes or gamefinder.EXPECTED_SIZES).items():
        (folder / name).write_bytes(b"\0" * size)
    return folder


class Finder(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.small = {"packs.rpf": 10, "data.rpf": 20, "common.rpf": 30, "anim.rpf": 40}
        patch = mock.patch.object(gamefinder, "EXPECTED_SIZES", self.small)
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(self.tmp.cleanup)

    def search(self, *roots):
        with mock.patch.object(gamefinder, "search_roots", return_value=list(roots)):
            return gamefinder.find()

    def test_a_game_folder_needs_both_marker_files(self):
        folder = make_game(self.root / "Games" / "game", sizes=self.small)
        self.assertTrue(gamefinder.is_game_folder(folder))
        (folder / "packs.rpf").unlink()
        self.assertFalse(gamefinder.is_game_folder(folder))
        self.assertFalse(gamefinder.is_game_folder(self.root / "missing"))

    def test_finds_a_copy_two_folders_deep(self):
        make_game(self.root / "Downloads" / "Some Game", sizes=self.small)
        found = self.search(self.root)
        self.assertEqual([p.name for p in found], ["Some Game"])

    def test_does_not_look_deeper_than_two_folders(self):
        make_game(self.root / "a" / "b" / "c" / "game", sizes=self.small)
        self.assertEqual(self.search(self.root), [])

    def test_lists_several_copies_and_skips_system_folders(self):
        make_game(self.root / "one", sizes=self.small)
        make_game(self.root / "Games" / "two", sizes=self.small)
        make_game(self.root / "AppData" / "hidden", sizes=self.small)
        names = sorted(p.name for p in self.search(self.root))
        self.assertEqual(names, ["one", "two"])

    def test_nothing_found(self):
        (self.root / "empty").mkdir()
        self.assertEqual(self.search(self.root), [])

    def test_the_search_never_reads_file_contents(self):
        make_game(self.root / "g", sizes=self.small)
        with mock.patch("builtins.open", side_effect=AssertionError("opened a file")), \
                mock.patch.object(pathlib.Path, "read_bytes", side_effect=AssertionError("read a file")), \
                mock.patch.object(pathlib.Path, "read_text", side_effect=AssertionError("read a file")):
            self.assertEqual(len(self.search(self.root)), 1)


class Version(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.small = {"packs.rpf": 10, "data.rpf": 20, "common.rpf": 30, "anim.rpf": 40}
        patch = mock.patch.object(gamefinder, "EXPECTED_SIZES", self.small)
        patch.start()
        self.addCleanup(patch.stop)
        self.folder = pathlib.Path(self.tmp.name) / "g"

    def test_matching_copy(self):
        make_game(self.folder, sizes=self.small)
        self.assertEqual(gamefinder.game_version(self.folder), "1.0.2.1")
        self.assertEqual(gamefinder.check(self.folder), (True, []))

    def test_every_problem_is_listed_at_once(self):
        make_game(self.folder, version="1.0.9.9", sizes={"packs.rpf": 11, "data.rpf": 20})
        ok, problems = gamefinder.check(self.folder)
        self.assertFalse(ok)
        text = "\n".join(problems)
        self.assertIn("version 1.0.9.9", text)
        self.assertIn("packs.rpf: 11 bytes", text)
        self.assertIn("common.rpf: missing", text)
        self.assertIn("anim.rpf: missing", text)
        self.assertNotIn("data.rpf", text)

    def test_not_a_folder_and_not_a_game(self):
        self.assertEqual(gamefinder.check(self.folder)[0], False)
        self.folder.mkdir()
        ok, problems = gamefinder.check(self.folder)
        self.assertFalse(ok)
        self.assertTrue(any("MicrosoftGame.Config" in p for p in problems))

    def test_version_is_read_with_or_without_a_namespace(self):
        make_game(self.folder, sizes=self.small)
        (self.folder / "MicrosoftGame.Config").write_text(
            '<Game xmlns="http://example.invalid/ns"><Identity Version="2.0.0.0"/></Game>', "utf-8")
        self.assertEqual(gamefinder.game_version(self.folder), "2.0.0.0")
        (self.folder / "MicrosoftGame.Config").write_text('<Game><Identity Version="3.1"', "utf-8")   # broken XML
        self.assertEqual(gamefinder.game_version(self.folder), "3.1")

    def test_the_supported_version_and_sizes_are_the_documented_ones(self):
        self.assertEqual(gamefinder.SUPPORTED_VERSION, "1.0.2.1")


if __name__ == "__main__":
    unittest.main()
