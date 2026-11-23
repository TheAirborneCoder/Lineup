# SPDX-License-Identifier: GPL-3.0-or-later
"""engine/native_build.py compiles the two C files with whatever compiler the system has.

Without a compiler the test is skipped, unless LINEUP_REQUIRE_COMPILER=1 is set (the CI does that, which is how the
gcc and clang paths get exercised)."""
import ctypes
import os
import pathlib
import tempfile
import unittest

import _setup
import native_build
import paths

SCRATCH = _setup.SCRATCH  # importing _setup puts engine/ on sys.path and uses throwaway folders


class Native(unittest.TestCase):
    def test_sources_exist_and_have_no_msvc_only_headers(self):
        for base in native_build.SOURCES:
            text = (native_build.DATA / f"{base}.c").read_text("utf-8")
            self.assertNotIn("<windows.h>", text)
            self.assertNotIn("<intrin.h>", text)
            self.assertIn("EXPORT", text)

    def test_compile_into_a_temporary_folder(self):
        compiler = native_build.find_compiler()
        if compiler is None:
            if os.environ.get("LINEUP_REQUIRE_COMPILER") == "1":
                self.fail("LINEUP_REQUIRE_COMPILER=1 but no C compiler was found")
            self.skipTest("no C compiler on this system")
        # a loaded library cannot be deleted on Windows, so cleanup errors are ignored
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as t:
            saved = paths.NATIVE
            paths.NATIVE = pathlib.Path(t) / "native"
            native_build.paths.NATIVE = paths.NATIVE
            messages = []
            try:
                result = native_build.build(force=True, say=messages.append)
                self.assertEqual(result["status"], "built", result.get("error") or messages)
                for base, symbol in native_build.EXPORTS.items():
                    library = ctypes.CDLL(str(paths.NATIVE / (base + native_build.suffix())))
                    self.assertTrue(hasattr(library, symbol), f"{base} does not export {symbol}")
                # a second call finds everything in place and does not compile again
                self.assertEqual(native_build.build(say=messages.append)["status"], "built")
            finally:
                paths.NATIVE = saved
                native_build.paths.NATIVE = saved

    def test_no_compiler_is_not_an_error(self):
        saved = native_build.find_compiler
        native_build.find_compiler = lambda: None
        with tempfile.TemporaryDirectory() as t:
            old = paths.NATIVE
            paths.NATIVE = pathlib.Path(t)
            messages = []
            try:
                result = native_build.build(force=True, say=messages.append)
            finally:
                paths.NATIVE = old
                native_build.find_compiler = saved
        self.assertEqual(result["status"], "no compiler")
        self.assertEqual(len(messages), 1)
        self.assertIn("more slowly", messages[0])


if __name__ == "__main__":
    unittest.main()
