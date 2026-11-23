# SPDX-License-Identifier: GPL-3.0-or-later
"""Helper-bone placement in engine/character_rig.py, with small synthetic skeletons (no game data)."""
import unittest

import numpy as np

import _setup
import character_rig as cr

SCRATCH = _setup.SCRATCH  # importing _setup puts engine/ on sys.path and uses throwaway folders


def joint(index, name, parent, attach=0, at=(0.0, 0.0, 0.0)):
    world = np.eye(4)
    world[:3, 3] = at
    return {"index": index, "name": name, "hash": cr.joaat32(name), "attachHash": attach, "parent": parent,
            "rot": [0.0, 0.0, 0.0, 1.0], "trans": list(at), "scale": [1.0, 1.0, 1.0],
            "worldCol": [float(v) for v in world.T.ravel()],
            "ibmCol": [float(v) for v in np.linalg.inv(world).T.ravel()],
            "bindPosRage": list(at), "bindPosYup": list(at), "group": "OTHER"}


def skeleton(*chain):
    """A skeleton from (name, parent index) pairs."""
    return {"id": 1, "joints": [joint(i, name, parent, at=(0.0, 0.0, 0.1 * i)) for i, (name, parent) in enumerate(chain)]}


DEFAULT = skeleton(("SKEL_ROOT", -1), ("SKEL_Spine0", 0), ("SKEL_Neck_1", 1), ("SKEL_Head", 2), ("PH_Hat", 3))


class AttachThroughDefaultMaster(unittest.TestCase):
    def test_goes_up_to_the_first_joint_the_target_has(self):
        target = skeleton(("SKEL_ROOT", -1), ("SKEL_Spine0", 0), ("SKEL_Head", 1))
        got = cr.attach_through_default_master(cr.joaat32("PH_Hat"), DEFAULT["joints"], target["joints"])
        self.assertEqual(got, 2)                                    # PH_Hat is absent; its parent SKEL_Head is there

    def test_skips_several_missing_ancestors(self):
        target = skeleton(("SKEL_ROOT", -1), ("SKEL_Spine0", 0))      # no neck, no head
        self.assertEqual(cr.attach_through_default_master(cr.joaat32("PH_Hat"), DEFAULT["joints"], target["joints"]), 1)

    def test_the_attach_bone_itself_counts_when_the_target_has_it(self):
        target = skeleton(("SKEL_ROOT", -1), ("PH_Hat", 0))
        self.assertEqual(cr.attach_through_default_master(cr.joaat32("PH_Hat"), DEFAULT["joints"], target["joints"]), 1)

    def test_falls_back_to_the_root(self):
        target = skeleton(("A_ROOT", -1), ("B", 0))                  # shares nothing with the default
        for hash_, default in ((cr.joaat32("PH_Hat"), DEFAULT["joints"]),   # nothing shared on the way up
                               (0, DEFAULT["joints"]),                      # no attach hash
                               (cr.joaat32("PH_Hat"), None),                # no default skeleton
                               (cr.joaat32("PH_Hat"), []),
                               (cr.joaat32("NOT_THERE"), DEFAULT["joints"])):   # attach bone unknown to the default
            self.assertEqual(cr.attach_through_default_master(hash_, default, target["joints"]), 0)

    def test_the_root_is_the_first_joint_without_a_parent(self):
        target = skeleton(("A", 0), ("B_ROOT", -1), ("C", 1))
        self.assertEqual(cr.attach_through_default_master(0, None, target["joints"]), 1)

    def test_a_loop_in_the_default_skeleton_ends(self):
        loop = skeleton(("X", 1), ("Y", 0))
        target = skeleton(("SKEL_ROOT", -1))
        self.assertEqual(cr.attach_through_default_master(cr.joaat32("X"), loop["joints"], target["joints"]), 0)

    def test_a_bone_is_found_by_its_name_hash_when_the_stored_hash_differs(self):
        odd = skeleton(("SKEL_ROOT", -1), ("SKEL_Head", 0), ("PH_Hat", 1))
        odd["joints"][2]["hash"] = 12345
        target = skeleton(("SKEL_ROOT", -1), ("SKEL_Head", 0))
        self.assertEqual(cr.attach_through_default_master(cr.joaat32("PH_Hat"), odd["joints"], target["joints"]), 1)


def helper_part():
    """A part whose own skeleton holds one helper bone attached to PH_Hat, one vertex weighted to it."""
    helper = joint(0, "HatBandRoot", -1, attach=cr.joaat32("PH_Hat"), at=(0.0, 0.1, 0.0))
    return {"name": "hatband", "lod": 0, "ownSkeleton": {"joints": [helper]},
            "primitives": [{"primIndex": 0, "skinned": True, "palette": [helper["hash"]], "vertices": 1,
                            "j0": np.zeros((1, 4), np.uint8), "j1": np.zeros((1, 4), np.uint8),
                            "w0": np.array([[255, 0, 0, 0]], np.uint8), "w1": np.zeros((1, 4), np.uint8),
                            "maxInfluences": 4}]}


class RemapUsesIt(unittest.TestCase):
    def test_a_helper_follows_the_ancestor_found_through_the_default_master(self):
        target = skeleton(("SKEL_ROOT", -1), ("SKEL_Spine0", 0), ("SKEL_Head", 1))
        with_default = cr.remap_part_skin(helper_part(), target, default_skeleton=DEFAULT)
        self.assertEqual(len(with_default["extraJoints"]), 1)
        self.assertEqual(with_default["extraJoints"][0]["parent"], 2)

    def test_without_a_default_skeleton_the_helper_hangs_off_the_root(self):
        target = skeleton(("SKEL_ROOT", -1), ("SKEL_Spine0", 0), ("SKEL_Head", 1))
        self.assertEqual(cr.remap_part_skin(helper_part(), target)["extraJoints"][0]["parent"], 0)

    def test_a_target_that_has_the_attach_bone_does_not_need_the_default(self):
        target = skeleton(("SKEL_ROOT", -1), ("SKEL_Head", 0), ("PH_Hat", 1))
        result = cr.remap_part_skin(helper_part(), target)
        self.assertEqual(result["extraJoints"][0]["parent"], 2)
        self.assertEqual(result["primitives"][0]["skinned"], True)

    def test_the_token_tables_are_gone(self):
        for name in ("infer_ped_parent", "_normalise_tokens", "_alias_tokens", "nearest_master_joint"):
            self.assertFalse(hasattr(cr, name), name)


if __name__ == "__main__":
    unittest.main()
