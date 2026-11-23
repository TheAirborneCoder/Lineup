# SPDX-License-Identifier: GPL-3.0-or-later
"""The receipt logic of the build runner, with stand-in stages that touch no game file."""
import unittest

import _setup
import build
import paths

SCRATCH = _setup.SCRATCH  # importing _setup puts engine/ on sys.path and uses throwaway folders


class Fake:
    """Three stages a -> b -> c; each records that it ran and writes one output file."""

    def __init__(self):
        self.ran = []
        self.inputs = {"a": "1", "b": "1", "c": "1"}
        self.content = {}
        self.fail = None

    def stage(self, name, needs=(), version=1):
        def run(ctx):
            if self.fail == name:
                raise RuntimeError("boom")
            self.ran.append(name)
            (paths.LIBRARY / f"{name}.out").write_text("x")
            return self.content.get(name)
        return build.Stage(name, name.upper(), run, needs=needs, version=version,
                           inputs=lambda: self.inputs[name], outputs=lambda: [paths.LIBRARY / f"{name}.out"])


class Receipts(unittest.TestCase):
    def setUp(self):
        self.saved = build.STAGES, build.STAGE_NAMES
        paths.ensure_work()
        for stale in (paths.WORK / "build.json", *paths.LIBRARY.glob("*.out")):
            stale.unlink(missing_ok=True)
        self.fake = Fake()
        self.versions = {"a": 1, "b": 1, "c": 1}
        self.install()

    def tearDown(self):
        build.STAGES, build.STAGE_NAMES = self.saved

    def install(self):
        f = self.fake
        build.STAGES = {"a": f.stage("a", version=self.versions["a"]),
                        "b": f.stage("b", ("a",), self.versions["b"]),
                        "c": f.stage("c", ("b",), self.versions["c"])}
        build.STAGE_NAMES = ("a", "b", "c")

    def run_all(self, wanted=None, rebuild=()):
        self.fake.ran.clear()
        build.run_stages(wanted, rebuild, log=lambda *_: None)
        return list(self.fake.ran)

    def test_first_run_builds_everything_in_order_then_skips(self):
        self.assertEqual(self.run_all(), ["a", "b", "c"])
        self.assertEqual(self.run_all(), [])

    def test_a_new_stage_version_reruns_it_and_what_reads_it(self):
        self.run_all()
        self.versions["b"] = 2
        self.install()
        self.assertEqual(self.run_all(), ["b", "c"])

    def test_changed_game_files_rerun_the_stage_and_everything_after(self):
        self.run_all()
        self.fake.inputs["a"] = "2"
        self.assertEqual(self.run_all(), ["a", "b", "c"])

    def test_a_later_stage_input_does_not_touch_earlier_stages(self):
        self.run_all()
        self.fake.inputs["c"] = "2"
        self.assertEqual(self.run_all(), ["c"])

    def test_forced_rebuild_cascades(self):
        self.run_all()
        self.assertEqual(self.run_all(rebuild=["b"]), ["b", "c"])

    def test_unchanged_content_stops_the_cascade(self):
        self.fake.content["a"] = "same-content"
        self.run_all()
        self.assertEqual(self.run_all(rebuild=["a"]), ["a"])
        self.fake.content["a"] = "other-content"
        self.assertEqual(self.run_all(rebuild=["a"]), ["a", "b", "c"])

    def test_a_missing_output_reruns_the_stage(self):
        self.run_all()
        (paths.LIBRARY / "b.out").unlink()
        self.assertEqual(self.run_all(), ["b"])

    def test_wanted_stages_pull_in_what_they_need(self):
        self.assertEqual(build.order(["c"]), ["a", "b", "c"])
        self.assertEqual(build.order(["a"]), ["a"])
        self.assertEqual(self.run_all(["b"]), ["a", "b"])

    def test_a_failing_stage_keeps_the_receipts_of_the_earlier_ones(self):
        self.fake.fail = "b"
        with self.assertRaises(RuntimeError):
            self.run_all()
        self.assertIn("a", build.load_receipt()["stages"])
        self.assertNotIn("b", build.load_receipt()["stages"])
        self.fake.fail = None
        self.assertEqual(self.run_all(), ["b", "c"])

    def test_status_names_the_reason(self):
        self.assertEqual(build.state(build.STAGES["a"], build.load_receipt()), "not built")
        self.run_all()
        self.assertEqual(build.state(build.STAGES["a"], build.load_receipt()), "done")
        self.fake.inputs["a"] = "2"
        self.assertEqual(build.state(build.STAGES["a"], build.load_receipt()), "game files changed")

    def test_clock(self):
        self.assertEqual(build.clock(3725), "1:02:05")


class RealStages(unittest.TestCase):
    def test_the_stage_table_is_consistent(self):
        names = build.STAGE_NAMES
        self.assertEqual(set(names), set(build.STAGES))
        for position, name in enumerate(names):
            for need in build.STAGES[name].needs:
                self.assertLess(names.index(need), position, f"{name} must come after {need}")
        self.assertEqual(names[:2], ("index", "names"))


if __name__ == "__main__":
    unittest.main()
