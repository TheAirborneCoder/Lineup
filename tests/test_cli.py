# SPDX-License-Identifier: GPL-3.0-or-later
"""lineup.py as a user runs it (help, a wrong game folder, a wrong version) and the launcher scripts' syntax."""
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

import _setup

LINEUP = _setup.REPO / "lineup.py"


def run(*args, env=None):
    environment = {k: v for k, v in os.environ.items() if not k.startswith("LINEUP_")}
    environment.update(env or {})
    return subprocess.run([sys.executable, str(LINEUP), *args], capture_output=True, text=True, timeout=120,
                          env=environment)


class Cli(unittest.TestCase):
    def test_help_lists_every_documented_option(self):
        result = run("--help")
        self.assertEqual(result.returncode, 0)
        for option in ("--game", "--work", "--find", "--pick", "--check", "--build", "--stages", "--rebuild",
                       "--status", "--port", "--no-browser", "--force"):
            self.assertIn(option, result.stdout)

    def test_a_missing_game_folder_is_a_message_not_a_traceback(self):
        with tempfile.TemporaryDirectory() as t:
            result = run("--game", str(pathlib.Path(t) / "nope"), "--work", t, "--check")
        self.assertEqual(result.returncode, 2)
        self.assertIn("not found", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_the_wrong_version_lists_the_problems_and_builds_nothing(self):
        with tempfile.TemporaryDirectory() as t:
            game, work = pathlib.Path(t, "game"), pathlib.Path(t, "work")
            game.mkdir()
            (game / "MicrosoftGame.Config").write_text('<Game><Identity Version="9.9.9.9"/></Game>')
            (game / "packs.rpf").write_bytes(b"")
            result = run("--game", str(game), "--work", str(work), "--build")
            self.assertEqual(result.returncode, 2)
            self.assertIn("9.9.9.9", result.stdout)
            self.assertIn("data.rpf: missing", result.stdout)
            self.assertIn("--force", result.stdout)
            self.assertFalse(work.exists(), "nothing may be written to the work folder")

    def test_an_unknown_stage_is_refused(self):
        with tempfile.TemporaryDirectory() as t:
            result = run("--game", t, "--work", t, "--stages", "nonsense", "--force")
        self.assertEqual(result.returncode, 2)


class Launchers(unittest.TestCase):
    def test_posix_launchers_parse_as_sh(self):
        sh = shutil.which("sh")
        if sh is None:
            self.skipTest("no sh on this system")
        for name in ("launch.sh", "Launch.command"):
            result = subprocess.run([sh, "-n", str(_setup.REPO / name)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, f"{name}: {result.stderr}")

    def test_line_endings(self):
        self.assertIn(b"\r\n", (_setup.REPO / "Launch.bat").read_bytes())
        for name in ("launch.sh", "Launch.command"):
            self.assertNotIn(b"\r", (_setup.REPO / name).read_bytes(), name)

    def test_the_launchers_pass_arguments_to_lineup(self):
        for name, marker in (("Launch.bat", "%*"), ("launch.sh", '"$@"')):
            self.assertIn(marker, (_setup.REPO / name).read_text("utf-8"), name)

    def test_requirements_name_only_numpy_and_pillow(self):
        lines = [ln.split(">=")[0].strip().lower() for ln in (_setup.REPO / "requirements.txt").read_text().splitlines()
                 if ln.strip() and not ln.startswith("#")]
        self.assertEqual(sorted(lines), ["numpy", "pillow"])


if __name__ == "__main__":
    unittest.main()
