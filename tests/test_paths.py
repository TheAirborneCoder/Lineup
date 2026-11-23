# SPDX-License-Identifier: GPL-3.0-or-later
"""The three places (GAME, WORK, ENGINE) and the settings file."""
import os
import pathlib
import tempfile
import unittest

import _setup
import paths


class Folders(unittest.TestCase):
    def test_configure_sets_globals_and_environment(self):
        with tempfile.TemporaryDirectory() as t:
            game, work = pathlib.Path(t, "g"), pathlib.Path(t, "w")
            game.mkdir()
            paths.configure(game, work)
            try:
                self.assertEqual(paths.GAME, game.resolve())
                self.assertEqual(paths.INDEX, work.resolve() / "index")
                self.assertEqual(paths.LIBRARY, work.resolve() / "library")
                self.assertEqual(paths.CACHE, work.resolve() / "cache")
                self.assertEqual(paths.NATIVE, work.resolve() / "native")
                # worker processes read the same folders from the environment
                self.assertEqual(os.environ["LINEUP_GAME"], str(game.resolve()))
                self.assertEqual(os.environ["LINEUP_WORK"], str(work.resolve()))
            finally:
                paths.configure(_setup.GAME, _setup.WORK)

    def test_ensure_work_creates_the_folders(self):
        paths.ensure_work()
        for folder in (paths.INDEX, paths.LIBRARY, paths.CACHE, paths.NATIVE):
            self.assertTrue(folder.is_dir(), folder)

    def test_archive_path_stays_inside_the_game_folder(self):
        (paths.GAME / "packs.rpf").write_bytes(b"")
        self.assertEqual(paths.archive_path("packs.rpf"), (paths.GAME / "packs.rpf").resolve())
        for bad in ("../outside.rpf", "packs.txt", "/etc/passwd.rpf", "..\\..\\x.rpf"):
            with self.assertRaises(ValueError, msg=bad):
                paths.archive_path(bad)

    def test_game_archives_lists_posix_names(self):
        (paths.GAME / "pack" / "story000").mkdir(parents=True, exist_ok=True)
        (paths.GAME / "pack" / "story000" / "story000.rpf").write_bytes(b"")
        (paths.GAME / "packs.rpf").write_bytes(b"")
        found = paths.game_archives()
        self.assertIn("pack/story000/story000.rpf", found)
        self.assertIn("packs.rpf", found)

    def test_settings_round_trip(self):
        with tempfile.TemporaryDirectory() as t:
            saved = paths.SETTINGS
            paths.SETTINGS = pathlib.Path(t) / "sub" / "settings.json"
            try:
                self.assertEqual(paths.load_settings(), {})
                paths.save_settings(game="C:/x", work=None)
                paths.save_settings(work="D:/y")
                self.assertEqual(paths.load_settings(), {"game": "C:/x", "work": "D:/y"})
                paths.SETTINGS.write_text("not json", "utf-8")
                self.assertEqual(paths.load_settings(), {})
            finally:
                paths.SETTINGS = saved


if __name__ == "__main__":
    unittest.main()
