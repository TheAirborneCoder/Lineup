# SPDX-License-Identifier: GPL-3.0-or-later
"""RAGE 9 character skeleton, 8-influence vertex skinning, and garment helper-bone decoder.

Implements the verified crSkeletonData, grmModel bone-hash palette, 4/8-influence vertex skin stream
(0x25 u8x4, 0x8C packed u32x4 bits 0..11, u16x4, and rigid fallback), and drawable-local garment
helper-bone parenting rules measured on GTA VI (Xbox Series 1.0.2.1).
"""
from __future__ import annotations

import json
import math
import re
import struct
from collections import Counter

import numpy as np

import model_decode as md
import drawables as d

# RAGE Z-up -> glTF Y-up basis matrix: (x, y, z) -> (x, z, -y).
C_YUP = np.array([
    [1.0, 0.0, 0.0, 0.0],
    [0.0, 0.0, 1.0, 0.0],
    [0.0, -1.0, 0.0, 0.0],
    [0.0, 0.0, 0.0, 1.0],
], dtype=np.float64)
C_YUP_T = C_YUP.T.copy()

# Master ped skeleton fragments indexed in LIBRARY/inventory.sqlite3.
MASTER_RIG_TOKENS = {
    "fr1": "ambient_femalebody",
    "fo1": "ambient_femalebody",
    "fsk1": "ambient_femalebody",
    "fc1": "ambient_femalebody",
    "mr1": "ambient_malebody",
    "mo1": "ambient_malebody",
    "msk1": "ambient_malebody",
    "mc1": "ambient_malebody",
    "fb1": "ambient_femalebb",
    "mb1": "ambient_malebb",
    "mk1": "ambient_kidbody",
    "fk1": "ambient_kidbody",
    "mi1": "ambient_infantbody",
    "fi1": "ambient_infantbody",
    "fxxl": "ambient_femalexxl",
    "mxxl": "ambient_malexxl",
}

_RIG_TOKEN_RE = re.compile(
    r"(?:^|_)(fr1|mr1|fb1|mb1|fo1|mo1|fc1|mc1|fsk1|msk1|mk1|fk1|mi1|fi1|fxxl|mxxl)(?:_|$)",
    re.I,
)


def joaat32(text: str) -> int:
    """Jenkins one-at-a-time over lowercased Latin-1 bytes (RAGE bone/clip hash)."""
    h = 0
    for b in text.lower().encode("latin-1", "replace"):
        h = (h + b) & 0xFFFFFFFF
        h = (h + (h << 10)) & 0xFFFFFFFF
        h ^= h >> 6
    h = (h + (h << 3)) & 0xFFFFFFFF
    h ^= h >> 11
    return (h + (h << 15)) & 0xFFFFFFFF


def bone_group(name: str) -> str:
    """Classify a RAGE 9 bone name into its structural subsystem."""
    u = (name or "").upper()
    if u.startswith("SKEL_"):
        return "SKEL"
    if u.startswith(("FB_", "FACIAL_")):
        return "FB"
    if u.startswith("BM_"):
        return "BM"
    if u.startswith("MH_"):
        return "MH"
    if u.startswith(("PH_", "IK_")):
        return "PH"
    if u.startswith("RB_"):
        return "RB"
    if u.startswith("SEC_"):
        return "SEC"
    if u.startswith("GM_") or "_GM_" in u:
        return "GM"
    return "OTHER"


def quat_to_mat3(q) -> np.ndarray:
    """Unit quaternion (x, y, z, w) -> 3x3 column-vector rotation matrix."""
    x, y, z, w = float(q[0]), float(q[1]), float(q[2]), float(q[3])
    n = math.sqrt(x * x + y * y + z * z + w * w)
    if n > 1e-12:
        x /= n
        y /= n
        z /= n
        w /= n
    else:
        x, y, z, w = 0.0, 0.0, 0.0, 1.0
    return np.array([
        [1.0 - 2.0 * (y * y + z * z), 2.0 * (x * y - z * w),       2.0 * (x * z + y * w)],
        [2.0 * (x * y + z * w),       1.0 - 2.0 * (x * x + z * z), 2.0 * (y * z - x * w)],
        [2.0 * (x * z - y * w),       2.0 * (y * z + x * w),       1.0 - 2.0 * (x * x + y * y)],
    ], dtype=np.float64)


def mat3_to_quat(m: np.ndarray) -> tuple[float, float, float, float]:
    """3x3 orthonormal rotation matrix -> unit quaternion (x, y, z, w)."""
    tr = float(m[0, 0] + m[1, 1] + m[2, 2])
    if tr > 0.0:
        s = math.sqrt(tr + 1.0) * 2.0
        w = 0.25 * s
        x = (m[2, 1] - m[1, 2]) / s
        y = (m[0, 2] - m[2, 0]) / s
        z = (m[1, 0] - m[0, 1]) / s
    elif m[0, 0] > m[1, 1] and m[0, 0] > m[2, 2]:
        s = math.sqrt(max(1e-12, 1.0 + m[0, 0] - m[1, 1] - m[2, 2])) * 2.0
        w = (m[2, 1] - m[1, 2]) / s
        x = 0.25 * s
        y = (m[0, 1] + m[1, 0]) / s
        z = (m[0, 2] + m[2, 0]) / s
    elif m[1, 1] > m[2, 2]:
        s = math.sqrt(max(1e-12, 1.0 + m[1, 1] - m[0, 0] - m[2, 2])) * 2.0
        w = (m[0, 2] - m[2, 0]) / s
        x = (m[0, 1] + m[1, 0]) / s
        y = 0.25 * s
        z = (m[1, 2] + m[2, 1]) / s
    else:
        s = math.sqrt(max(1e-12, 1.0 + m[2, 2] - m[0, 0] - m[1, 1])) * 2.0
        w = (m[1, 0] - m[0, 1]) / s
        x = (m[0, 2] + m[2, 0]) / s
        y = (m[1, 2] + m[2, 1]) / s
        z = 0.25 * s
    n = math.sqrt(x * x + y * y + z * z + w * w)
    if n > 1e-12:
        return (x / n, y / n, z / n, w / n)
    return (0.0, 0.0, 0.0, 1.0)


def trs_col(t, q, s) -> np.ndarray:
    """Compose (trans, rot_xyzw, scale) into a 4x4 column-vector matrix."""
    M = np.eye(4, dtype=np.float64)
    R = quat_to_mat3(q)
    M[:3, :3] = R * np.asarray(s, dtype=np.float64)[None, :]
    M[:3, 3] = np.asarray(t, dtype=np.float64)
    return M


def decompose_trs_col(M: np.ndarray) -> tuple[list[float], list[float], list[float]]:
    """Decompose a 4x4 affine column-vector matrix into (trans, rot_xyzw, scale)."""
    t = [float(M[0, 3]), float(M[1, 3]), float(M[2, 3])]
    c0 = M[:3, 0]
    c1 = M[:3, 1]
    c2 = M[:3, 2]
    sx = float(np.linalg.norm(c0)) or 1.0
    sy = float(np.linalg.norm(c1)) or 1.0
    sz = float(np.linalg.norm(c2)) or 1.0
    R = np.column_stack((c0 / sx, c1 / sy, c2 / sz))
    if float(np.linalg.det(R)) < 0.0:
        sz = -sz
        R[:, 2] = -R[:, 2]
    q = mat3_to_quat(R)
    return t, [float(v) for v in q], [sx, sy, sz]


def rage_to_yup_mat4(M: np.ndarray) -> np.ndarray:
    """Convert a 4x4 column-vector matrix from RAGE Z-up to glTF Y-up: C * M * C^T."""
    return C_YUP @ M @ C_YUP_T


def rage_to_yup_vec3(v) -> list[float]:
    return [float(v[0]), float(v[2]), -float(v[1])]


def rage_to_yup_quat(q) -> list[float]:
    """Convert quaternion (x, y, z, w) from RAGE Z-up to glTF Y-up (conjugation by C)."""
    return [float(q[0]), float(q[2]), -float(q[1]), float(q[3])]


def infer_master_rig_name(model_name: str, category: str = "", body_rig: str = "", assembly_kind: str = "") -> str | None:
    """Resolve the master skeleton fragment name for a biped character or clothing drawable."""
    if body_rig:
        rig_l = body_rig.lower()
        if rig_l in MASTER_RIG_TOKENS:
            return MASTER_RIG_TOKENS[rig_l]
    nm = (model_name or "").lower()
    if nm.startswith("ambient_") and ("body" in nm or "bb" in nm or "xxl" in nm):
        return nm
    m = _RIG_TOKEN_RE.search(nm)
    if m:
        return MASTER_RIG_TOKENS.get(m.group(1).lower())
    # Named story protagonists / heads without explicit rig token in name
    if nm.startswith(("player_story000", "cs_lucia", "ig_lucia", "hc_lucia")):
        return "ambient_femalebody"
    if nm.startswith(("player_story001", "cs_jason", "ig_jason", "hc_jason")):
        return "ambient_malebody"
    if assembly_kind == "Female":
        return "ambient_femalebody"
    if assembly_kind == "Male":
        return "ambient_malebody"
    if category in ("Protagonists", "Characters", "Clothing & accessories"):
        if "_female" in nm or "female_" in nm or "_f_" in nm:
            return "ambient_femalebody"
        if "_male" in nm or "male_" in nm or "_m_" in nm:
            return "ambient_malebody"
    return None


def _parse_cr_skeleton_data(system: bytes | bytearray, root: int) -> dict | None:
    """Parse crSkeletonData pointed to by drawable root + 0x18. Returns None if absent."""
    if root + 0x20 > len(system):
        return None
    sk_raw = struct.unpack_from("<Q", system, root + 0x18)[0]
    if (sk_raw >> 28) != 5:
        return None
    sk = d.pointer(system, root + 0x18)
    if sk + 0x70 > len(system):
        raise IndexError("skeleton header crosses CPU page")
    count = struct.unpack_from("<H", system, sk + 0x6C)[0]
    if count == 0 or count > 4096:
        return None
    rec_raw = struct.unpack_from("<Q", system, sk + 0x28)[0]
    ibm_raw = struct.unpack_from("<Q", system, sk + 0x30)[0]
    par_raw = struct.unpack_from("<Q", system, sk + 0x40)[0]
    if (rec_raw >> 28) != 5 or (ibm_raw >> 28) != 5 or (par_raw >> 28) != 5:
        return None
    rec_off = d.pointer(system, sk + 0x28)
    ibm_off = d.pointer(system, sk + 0x30)
    par_off = d.pointer(system, sk + 0x40)
    if rec_off + count * 0x50 > len(system) or ibm_off + count * 64 > len(system) or par_off + count * 2 > len(system):
        raise IndexError("skeleton arrays cross CPU page")

    joints = []
    worlds = []
    max_bind_err = 0.0
    groups = Counter()

    for i in range(count):
        base = rec_off + i * 0x50
        rot = struct.unpack_from("<4f", system, base + 0x00)
        trans = struct.unpack_from("<3f", system, base + 0x10)
        bhash = struct.unpack_from("<I", system, base + 0x1C)[0]
        scale = struct.unpack_from("<3f", system, base + 0x20)
        attach_hash = struct.unpack_from("<I", system, base + 0x2C)[0]
        name_raw = struct.unpack_from("<Q", system, base + 0x30)[0]
        bname = ""
        if (name_raw >> 28) == 5:
            no = d.pointer(system, base + 0x30)
            if no >= len(system):
                raise IndexError("bone name crosses CPU page")
            end = system.find(0, no, min(len(system), no + 192))
            if end < 0 and no + 192 > len(system):
                raise IndexError("bone name string crosses CPU page")
            if end > no:
                bname = bytes(system[no:end]).decode("ascii", "replace")
        if not bname:
            bname = f"bone_0x{bhash:08x}"
        elif bhash == 0:
            bhash = joaat32(bname)

        par = struct.unpack_from("<H", system, par_off + i * 2)[0]
        if par == 0xFFFF or par >= count or par == i:
            par = -1

        # RAGE stores Mat34V row-by-row (last column is 0 padding); transpose to column-vector 4x4 with [3,3]=1.
        raw_m = np.frombuffer(system, "<f4", 16, ibm_off + i * 64).reshape(4, 4).astype(np.float64)
        ibm_col = raw_m.T.copy()
        ibm_col[3, :] = [0.0, 0.0, 0.0, 1.0]

        if not np.isfinite(rot).all() or not np.isfinite(trans).all() or not np.isfinite(scale).all():
            rot, trans, scale = (0.0, 0.0, 0.0, 1.0), (0.0, 0.0, 0.0), (1.0, 1.0, 1.0)
        loc = trs_col(trans, rot, scale)
        world = loc if par < 0 else (worlds[par] @ loc if par < len(worlds) else loc)
        worlds.append(world)

        det3 = float(np.linalg.det(ibm_col[:3, :3]))
        if not np.isfinite(ibm_col).all() or abs(det3) < 1e-12:
            # Fallback to exact inverse of composed bind world if stored matrix is degenerate.
            ibm_col = np.linalg.inv(world)
        else:
            err = float(np.max(np.abs(world @ ibm_col - np.eye(4))))
            if err > max_bind_err:
                max_bind_err = err

        grp = bone_group(bname)
        groups[grp] += 1
        bind_pos_rage = world[:3, 3]
        joints.append({
            "index": i,
            "name": bname,
            "hash": int(bhash),
            "attachHash": int(attach_hash),
            "parent": int(par),
            "group": grp,
            "rot": [float(v) for v in rot],
            "trans": [float(v) for v in trans],
            "scale": [float(v) for v in scale],
            "ibmCol": [float(v) for v in ibm_col.T.ravel()],  # column-major 16 floats (OpenGL/glTF order)
            "worldCol": [float(v) for v in world.T.ravel()],   # column-major 16 floats in RAGE Z-up
            "bindPosRage": [float(bind_pos_rage[0]), float(bind_pos_rage[1]), float(bind_pos_rage[2])],
            "bindPosYup": rage_to_yup_vec3(bind_pos_rage),
        })

    # Re-resolve any forward parent references (rare, but safe).
    if any(j["parent"] > j["index"] for j in joints):
        computed = [None] * count
        def _world_of(idx, depth=0):
            if computed[idx] is not None:
                return computed[idx]
            j = joints[idx]
            loc = trs_col(j["trans"], j["rot"], j["scale"])
            if j["parent"] < 0 or depth > count:
                computed[idx] = loc
            else:
                computed[idx] = _world_of(j["parent"], depth + 1) @ loc
            return computed[idx]
        max_bind_err = 0.0
        for i, j in enumerate(joints):
            w = _world_of(i)
            ibm_m = np.array(j["ibmCol"], dtype=np.float64).reshape(4, 4).T
            max_bind_err = max(max_bind_err, float(np.max(np.abs(w @ ibm_m - np.eye(4)))))
            j["worldCol"] = [float(v) for v in w.T.ravel()]
            j["bindPosRage"] = [float(w[0, 3]), float(w[1, 3]), float(w[2, 3])]
            j["bindPosYup"] = rage_to_yup_vec3(w[:3, 3])

    return {
        "boneCount": count,
        "bindError": round(max_bind_err, 8),
        "groups": dict(groups),
        "joints": joints,
    }


def read_skeleton(arc: str, member: int, root: int, name: str, kind: str) -> dict | None:
    """Read and validate crSkeletonData for a drawable or fragment resource."""
    src, system, start, froot, actual = md.read_resource(arc, member, root, name, kind)
    skel, _, _ = md.with_pages(lambda s, _: _parse_cr_skeleton_data(s, froot), src, system, start)
    if skel is None:
        return None
    skel["name"] = actual
    skel["kind"] = kind
    # Check if this is a standing human biped rig (feet around Z = -1.0m in bind space)
    low = (actual or "").lower()
    is_animal_prefix = bool(re.match(r"^(?:cra|crb|crc|crf|cri|crm|crr|a_c)_", low))
    min_z = min((j["bindPosRage"][2] for j in skel["joints"]), default=0.0)
    joint_names = {j["name"] for j in skel["joints"]}
    skel["isBiped"] = bool(
        not is_animal_prefix
        and "SKEL_Pelvis" in joint_names
        and ("SKEL_L_Hand" in joint_names or "SKEL_L_Clavicle" in joint_names)
        and min_z < -0.4
    )
    if skel["isBiped"]:
        skel["defaultOffsetYup"] = [0.0, 1.0, 0.0]
    elif min_z < -0.05:
        skel["defaultOffsetYup"] = [0.0, round(-min_z, 4), 0.0]
    else:
        skel["defaultOffsetYup"] = [0.0, 0.0, 0.0]
    return skel


_ANIMAL_CLADE_MAP = {
    "cra": "amphibian",
    "crb": "bird",
    "crc": "crustacean",
    "crf": "fish",
    "cri": "insect",
    "crm": "mammal",
    "crr": "reptile",
}


def animal_species_tokens(name: str, subcat: str = "") -> list[str]:
    """Extract ordered search/matching tokens (most specific first) for an animal model/skeleton name."""
    low = (name or "").lower()
    m = re.match(r"^(cra|crb|crc|crf|cri|crm|crr)_([a-z0-9]+)(?:_([a-z0-9]+))?", low)
    tokens: list[str] = []
    if m:
        clade = _ANIMAL_CLADE_MAP.get(m.group(1), "")
        sp = m.group(2)
        sub = m.group(3) or ""
        if sub and sub not in ("m", "f", "00", "01", "02", "generic", "standard", "legendary"):
            tokens.append(f"{sub}_{sp}")
            tokens.append(f"{sp}_{sub}")
        if sp:
            tokens.append(sp)
            # Common alias mappings in GTA VI anim.rpf paths
            aliases = {
                "alligator": ["gator", "alligator", "croc"],
                "crocodile": ["crocodile", "alligator", "gator"],
                "caiman": ["alligator", "crocodile", "caiman"],
                "toad": ["frog", "toad"],
                "chicken": ["chicken", "rooster", "hen", "bird"],
                "rooster": ["chicken", "rooster", "bird"],
                "cougar": ["panther", "cougar", "puma", "big_cat"],
                "panther": ["panther", "cougar", "big_cat"],
                "tiger": ["bengal_tiger", "tiger", "big_cat"],
                "bobcat": ["bobcat", "lynx", "cat"],
                "dog": ["dog", "canine"],
                "wolf": ["wolf", "dog"],
                "boar": ["boar", "pig"],
                "pig": ["pig", "boar"],
                "deer": ["deer", "buck", "doe"],
                "cattle": ["cow", "cattle", "bull"],
                "cow": ["cow", "cattle"],
                "whale": ["whale", "orca", "cetacean"],
                "dolphin": ["dolphin", "whale"],
                "shark": ["shark", "fish"],
                "turtle": ["turtle", "tortoise"],
                "tortoise": ["tortoise", "turtle"],
                "snake": ["snake", "python", "rattlesnake"],
                "python": ["python", "snake"],
                "iguana": ["iguana", "lizard"],
                "monitor": ["monitor", "lizard", "iguana"],
            }
            for a in aliases.get(sp, ()):
                if a not in tokens:
                    tokens.append(a)
        if clade and clade not in tokens:
            tokens.append(clade)
    elif low.startswith("a_c_"):
        parts = low[4:].split("_")
        if parts and parts[0]:
            tokens.append(parts[0])
    if subcat:
        sc = subcat.lower().split()[0]
        if sc and sc not in tokens:
            tokens.append(sc)
    return tokens


def has_skeleton(arc: str, member: int, root: int, name: str, kind: str) -> int:
    """Fast check returning bone count if resource has crSkeletonData, else 0."""
    try:
        src, system, start, froot, _ = md.read_resource(arc, member, root, name, kind)
        def _count(s, _):
            if froot + 0x20 > len(s):
                return 0
            sk_raw = struct.unpack_from("<Q", s, froot + 0x18)[0]
            if (sk_raw >> 28) != 5:
                return 0
            sk = d.pointer(s, froot + 0x18)
            if sk + 0x70 > len(s):
                raise IndexError("skeleton header crosses page")
            cnt = struct.unpack_from("<H", s, sk + 0x6C)[0]
            return cnt if 0 < cnt <= 4096 else 0
        cnt, _, _ = md.with_pages(_count, src, system, start)
        return int(cnt)
    except Exception:
        return 0


def _read_model_palettes(system: bytes | bytearray, root: int, lod: int = 0) -> list[list[int]]:
    """Read per-grmModel local bone hash palettes (u32 joaat32) for a given LOD."""
    lod_ptr_off = root + 0x50 + 8 * lod
    if lod_ptr_off + 8 > len(system):
        return []
    lod_raw = struct.unpack_from("<Q", system, lod_ptr_off)[0]
    if (lod_raw >> 28) != 5:
        return []
    lod_off = d.pointer(system, lod_ptr_off)
    if lod_off + 10 > len(system):
        raise IndexError("lod header crosses page")
    m_arr_raw = struct.unpack_from("<Q", system, lod_off)[0]
    m_cnt = struct.unpack_from("<H", system, lod_off + 8)[0]
    if (m_arr_raw >> 28) != 5 or m_cnt == 0 or m_cnt > 256:
        return []
    m_arr = d.pointer(system, lod_off)
    if m_arr + 8 * m_cnt > len(system):
        raise IndexError("model array crosses page")
    palettes = []
    for mi in range(m_cnt):
        mp = d.pointer(system, m_arr + 8 * mi)
        if mp + 0x30 > len(system):
            raise IndexError("grmModel header crosses page")
        local_bones = struct.unpack_from("<H", system, mp + 0x14)[0]
        pal_raw = struct.unpack_from("<Q", system, mp + 0x28)[0]
        if local_bones > 0 and local_bones <= 2048 and (pal_raw >> 28) == 5:
            pal_off = d.pointer(system, mp + 0x28)
            if pal_off + local_bones * 4 > len(system):
                raise IndexError("bone palette crosses page")
            pal = list(struct.unpack_from(f"<{local_bones}I", system, pal_off))
        else:
            pal = []
        palettes.append(pal)
    return palettes


def _decode_skin_streams_from_vbuf(vertex: memoryview, count: int, local_bones: int):
    """Extract raw 8-influence joint indices (u16) and weights (u8) from a mesh-block vertex buffer."""
    header = np.frombuffer(vertex, dtype="<u4", count=23)
    starts = (header >> 8) * 2
    fmts = header & 0xFF

    def _read_weights(slot: int):
        fmt = int(fmts[slot])
        st = int(starts[slot])
        if fmt == 0x26 and st > 0 and st + count * 4 <= len(vertex):
            return np.frombuffer(vertex, dtype="<u1", count=count * 4, offset=st).reshape(count, 4).copy()
        return None

    def _read_joints(slot: int):
        fmt = int(fmts[slot])
        st = int(starts[slot])
        if fmt == 0x25 and st > 0 and st + count * 4 <= len(vertex):
            return np.frombuffer(vertex, dtype="<u1", count=count * 4, offset=st).reshape(count, 4).astype(np.uint16), False
        if fmt == 0x8C and st > 0 and st + count * 16 <= len(vertex):
            # Packed u32x4 layout: bone index is bits 0..11 of each u32.
            words = np.frombuffer(vertex, dtype="<u4", count=count * 4, offset=st).reshape(count, 4)
            return (words & 0xFFF).astype(np.uint16), True
        if fmt in (0x4B, 0x4C) and st > 0 and st + count * 8 <= len(vertex):
            return np.frombuffer(vertex, dtype="<u2", count=count * 4, offset=st).reshape(count, 4).copy(), False
        return None, False

    w0 = _read_weights(6)
    w1 = _read_weights(7)
    j0, packed0 = _read_joints(8)
    j1, packed1 = _read_joints(9)

    # Rigid single-bone binding fallback when palette has >=1 bone and no per-vertex skin slots exist.
    if w0 is None and j0 is None:
        if local_bones >= 1:
            j0 = np.zeros((count, 4), dtype=np.uint16)
            w0 = np.zeros((count, 4), dtype=np.uint8)
            w0[:, 0] = 255
            j1 = np.zeros((count, 4), dtype=np.uint16)
            w1 = np.zeros((count, 4), dtype=np.uint8)
            return j0, j1, w0, w1, 1, False
        return None

    if j0 is None:
        return None
    if w0 is None:
        # Single-bone rigid index stream without explicit weight stream -> weight 1.0 on slot 0.
        w0 = np.zeros((count, 4), dtype=np.uint8)
        w0[:, 0] = 255

    has_8 = (w1 is not None and j1 is not None and int(w1.max()) > 0)
    if j1 is None:
        j1 = np.zeros((count, 4), dtype=np.uint16)
    if w1 is None:
        w1 = np.zeros((count, 4), dtype=np.uint8)

    max_inf = 8 if has_8 else 4
    return j0, j1, w0, w1, max_inf, bool(packed0 or packed1)


def read_part_skin(arc: str, member: int, root: int, name: str, kind: str, lod: int = 0) -> dict | None:
    """Read local bone palettes, own skeleton (if any), and 8-influence skin streams for a drawable LOD."""
    src, system, start, froot, actual = md.read_resource(arc, member, root, name, kind)

    def _gather(s, _):
        parts = md.model_geometry_lod(s, froot, lod)
        palettes = _read_model_palettes(s, froot, lod)
        own_skel = _parse_cr_skeleton_data(s, froot)
        return parts, palettes, own_skel

    (parts, palettes, own_skel), system, start = md.with_pages(_gather, src, system, start)
    if not parts:
        return None

    # Check if any model has a bone palette or own skeleton
    has_any_palette = any(bool(p) for p in palettes)
    if not has_any_palette and own_skel is None:
        return None

    required = max(at + size for *_, vert, ind in parts for at, size in (vert, ind))
    gpu = bytearray()
    p = start
    while len(gpu) < required:
        if src[p:p + 1] != b"x":
            # Try consuming trailing CPU pages if needed (same pattern as model_decode.decode)
            nxt = md._consume_trailing_cpu(src, system, start)
            if nxt is None:
                raise ValueError("unresolved graphics stream while reading skin")
            system, start = nxt
            gpu = bytearray()
            p = start
            continue
        chunk, p = d.read_stream(src, p)
        gpu.extend(chunk)

    prims = []
    any_skinned = False
    for pi, (model_idx, geom_idx, shader_idx, (v, nv), (i, ni)) in enumerate(parts):
        vbuf = memoryview(gpu)[v:v + nv]
        header = np.frombuffer(vbuf, dtype="<u4", count=23)
        first = int(header[0])
        if first not in (0x4042, 0x3063):
            continue
        starts = (header >> 8) * 2
        p0 = int(starts[0])
        stride = 8 if first == 0x4042 else 12
        after = starts[starts > p0]
        size = (int(after.min()) if len(after) else len(vbuf)) - p0
        vcount = size // stride

        pal = palettes[model_idx] if model_idx < len(palettes) else []
        if not pal and own_skel is not None:
            pal = [j["hash"] for j in own_skel["joints"]]

        decoded = _decode_skin_streams_from_vbuf(vbuf, vcount, len(pal))
        if decoded is None:
            prims.append({
                "primIndex": pi,
                "model": model_idx,
                "geometry": geom_idx,
                "vertices": vcount,
                "skinned": False,
                "palette": pal,
            })
            continue
        j0, j1, w0, w1, max_inf, packed = decoded
        any_skinned = True
        prims.append({
            "primIndex": pi,
            "model": model_idx,
            "geometry": geom_idx,
            "vertices": vcount,
            "skinned": True,
            "palette": pal,
            "j0": j0,
            "j1": j1,
            "w0": w0,
            "w1": w1,
            "maxInfluences": max_inf,
            "packedJoints": packed,
        })

    if not any_skinned:
        return None
    return {
        "name": actual,
        "lod": lod,
        "ownSkeleton": own_skel,
        "primitives": prims,
    }


# ---------------------------------------------------------------------------
# Parent for a helper bone that has neither an own-skeleton parent nor its attach bone in the target skeleton
# ---------------------------------------------------------------------------
def _skeleton_root(joints: list[dict]) -> int:
    return next((j["index"] for j in joints if j["parent"] < 0), 0)


def attach_through_default_master(attach_hash: int, default_joints: list[dict] | None, master_joints: list[dict]) -> int:
    """Index in `master_joints` of the first joint, going up from the helper's attach bone, that the target master has.

    The attach bone is looked up in the item's own default master skeleton (`default_joints`) and its parents are
    followed until one has a hash (or a name hash) found in `master_joints`. With no default skeleton, no attach
    hash, an attach bone the default lacks, or nothing shared on the way up, the target's root joint is returned."""
    root = _skeleton_root(master_joints)
    if not attach_hash or not default_joints:
        return root
    target: dict[int, int] = {}
    for j in master_joints:
        target.setdefault(int(j["hash"]), j["index"])
        target.setdefault(joaat32(j["name"]), j["index"])
    start = next((j for j in default_joints if int(j["hash"]) == attach_hash or joaat32(j["name"]) == attach_hash), None)
    seen = set()
    while start is not None and start["index"] not in seen:
        seen.add(start["index"])
        for key in (int(start["hash"]), joaat32(start["name"])):
            if key in target:
                return target[key]
        parent = start["parent"]
        start = default_joints[parent] if 0 <= parent < len(default_joints) else None
    return root


def remap_part_skin(part_skin: dict, master_skel: dict, default_skeleton: dict | None = None) -> dict:
    """Remap a part's local bone palettes onto master_skel, appending component/facial/garment helper bones if needed.

    `default_skeleton` is the item's own default master skeleton; it is only used to place a helper bone whose attach
    bone `master_skel` does not have (see attach_through_default_master).

    Returns normalized float32/uint16 arrays per primitive plus any `extraJoints` to append to
    master_skel's joint array (starting at index `len(master_skel['joints'])`).
    """
    master_joints = master_skel["joints"]
    base_count = len(master_joints)

    by_hash: dict[int, int] = {}
    by_name: dict[str, int] = {}
    for j in master_joints:
        by_hash.setdefault(int(j["hash"]), j["index"])
        by_hash.setdefault(joaat32(j["name"]), j["index"])
        by_name.setdefault(j["name"].lower(), j["index"])

    own_skel = part_skin.get("ownSkeleton")
    own_by_hash: dict[int, dict] = {}
    own_joints = own_skel["joints"] if own_skel else []
    for oj in own_joints:
        own_by_hash.setdefault(int(oj["hash"]), oj)
        own_by_hash.setdefault(joaat32(oj["name"]), oj)

    # Track appended component/helper joints so multiple primitives in the same drawable share them.
    extra_joints: list[dict] = []
    extra_by_hash: dict[int, int] = {}
    extra_is_attach_local: dict[int, bool] = {}
    all_worlds_col: list[np.ndarray] = [
        np.array(j["worldCol"], dtype=np.float64).reshape(4, 4).T for j in master_joints
    ]

    def _resolve_hash(h: int, depth: int = 0) -> int:
        if h in by_hash:
            return by_hash[h]
        if h in extra_by_hash:
            return extra_by_hash[h]
        oj = own_by_hash.get(h)
        if oj is not None:
            oname = oj["name"]
            if oname.lower() in by_name:
                idx = by_name[oname.lower()]
                by_hash[h] = idx
                return idx
            # Determine parent index in combined joint space
            par_idx = None
            opar = oj["parent"]
            has_own_parent = (0 <= opar < len(own_joints) and opar != oj["index"] and depth < 32)
            if has_own_parent:
                par_hash = own_joints[opar]["hash"]
                par_name = own_joints[opar]["name"]
                if par_hash in by_hash:
                    par_idx = by_hash[par_hash]
                elif par_name.lower() in by_name:
                    par_idx = by_name[par_name.lower()]
                elif par_hash in extra_by_hash:
                    par_idx = extra_by_hash[par_hash]
                else:
                    par_idx = _resolve_hash(par_hash, depth + 1)

            attach_hash = int(oj.get("attachHash") or 0)
            if par_idx is None:
                # Native RAGE 9 crBoneData +0x2C stores the exact master-rig attachment parent bone hash
                # (e.g. BM_Head for FACIAL_C_FacialRoot/_eyes, PH_Hat for HatBandRoot, SKEL_Spine5 for Backpack_Root,
                # RB_L_Thigh01/SKEL_Spine4/etc. for GM_* garment helpers, SKEL_Head for animal head feathers).
                if attach_hash and attach_hash in by_hash:
                    par_idx = by_hash[attach_hash]
                elif attach_hash and attach_hash in extra_by_hash:
                    par_idx = extra_by_hash[attach_hash]
                else:
                    # The attach bone is not in this master: attach where its nearest ancestor in the default master is.
                    par_idx = attach_through_default_master(
                        attach_hash, default_skeleton["joints"] if default_skeleton else None, master_joints)

            # Secondary component skeletons with non-zero crBoneData +0x2C store their bind pose in the
            # attachment parent bone's local space, while mesh vertices live in character bind space.
            # Composing par_world @ local_m converts both bind world and IBM into true character space,
            # so eyes/garments/accessories follow the parent bone rigidly AND pivot at their true center.
            own_world = np.array(oj["worldCol"], dtype=np.float64).reshape(4, 4).T
            own_ibm = np.array(oj["ibmCol"], dtype=np.float64).reshape(4, 4).T
            par_world = all_worlds_col[par_idx]
            is_attach_local = bool(attach_hash) or extra_is_attach_local.get(par_idx, False)

            if is_attach_local:
                local_m = trs_col(oj["trans"], oj["rot"], oj["scale"]) if has_own_parent else own_world
                true_world = par_world @ local_m
                true_ibm = np.linalg.inv(true_world)
            else:
                local_m = np.linalg.inv(par_world) @ own_world
                true_world = own_world
                true_ibm = own_ibm

            loc_t, loc_q, loc_s = decompose_trs_col(local_m)
            new_idx = base_count + len(extra_joints)
            bind_pos_rage = true_world[:3, 3]
            bg = bone_group(oname)
            ej = {
                "index": new_idx,
                "name": oname,
                "hash": int(h),
                "attachHash": attach_hash,
                "parent": int(par_idx),
                "group": bg if bg != "OTHER" else "GM",
                "rot": loc_q,
                "trans": loc_t,
                "scale": loc_s,
                "ibmCol": [float(v) for v in true_ibm.T.ravel()],
                "worldCol": [float(v) for v in true_world.T.ravel()],
                "bindPosRage": [float(bind_pos_rage[0]), float(bind_pos_rage[1]), float(bind_pos_rage[2])],
                "bindPosYup": rage_to_yup_vec3(bind_pos_rage),
            }
            extra_joints.append(ej)
            all_worlds_col.append(true_world)
            extra_by_hash[h] = new_idx
            extra_is_attach_local[new_idx] = is_attach_local
            return new_idx

        # Unknown hash not in own skeleton or master skeleton -> attach rigidly to root (0) as fallback
        return 0

    remapped_prims = []
    max_inf_overall = 0
    any_packed = False
    total_local_bones = 0

    for prim in part_skin["primitives"]:
        if not prim["skinned"]:
            remapped_prims.append({
                "primIndex": prim["primIndex"],
                "vertices": prim["vertices"],
                "skinned": False,
            })
            continue
        pal = prim["palette"]
        total_local_bones = max(total_local_bones, len(pal))
        if not pal:
            local_to_joint = np.array([0], dtype=np.uint16)
        else:
            local_to_joint = np.array([_resolve_hash(h) for h in pal], dtype=np.uint16)

        vcount = prim["vertices"]
        j_all = np.concatenate([prim["j0"], prim["j1"]], axis=1).astype(np.int64)
        w_all = np.concatenate([prim["w0"], prim["w1"]], axis=1).astype(np.float64)
        totals = w_all.sum(axis=1)

        # Handle any degenerate zero-weight vertices gracefully (bind to joint 0 with weight 1.0)
        zero_verts = totals <= 0.0
        if bool(zero_verts.any()):
            w_all[zero_verts, 0] = 1.0
            j_all[zero_verts, 0] = 0
            totals = w_all.sum(axis=1)

        sum_min = float(totals.min()) / 255.0
        sum_max = float(totals.max()) / 255.0
        w_all /= totals[:, None]

        # Zero-weight joint slots can hold arbitrary indices, so they are masked to 0 before the palette lookup
        zero_slots = w_all <= 0.0
        j_all[zero_slots] = 0
        # Clamp any out-of-palette non-zero index safely to 0
        oob = j_all >= len(local_to_joint)
        if bool(oob.any()):
            j_all[oob] = 0
        j_mapped = local_to_joint[j_all]
        j_mapped[zero_slots] = 0

        has8 = bool(prim["maxInfluences"] > 4 and float(w_all[:, 4:].max()) > 1e-6)
        eff_inf = 8 if has8 else 4
        max_inf_overall = max(max_inf_overall, eff_inf)
        any_packed = any_packed or bool(prim.get("packedJoints"))

        remapped_prims.append({
            "primIndex": prim["primIndex"],
            "vertices": vcount,
            "skinned": True,
            "hasSkin8": has8,
            "maxInfluences": eff_inf,
            "localBones": len(pal),
            "sumMin": round(sum_min, 4),
            "sumMax": round(sum_max, 4),
            "j0": j_mapped[:, :4].astype("<u2"),
            "j1": j_mapped[:, 4:].astype("<u2"),
            "w0": w_all[:, :4].astype("<f4"),
            "w1": w_all[:, 4:].astype("<f4"),
        })

    return {
        "name": part_skin["name"],
        "lod": part_skin["lod"],
        "masterBoneCount": base_count,
        "extraJoints": extra_joints,
        "localBones": total_local_bones,
        "maxInfluences": max_inf_overall,
        "packedJoints": any_packed,
        "primitives": remapped_prims,
    }


def pack_skin_binary(remapped: dict) -> bytes:
    """Pack remapped skin streams into a compact binary response for WebGL2.

    Format: [u32 header_len][utf8 json header (padded to 4B)][raw buffers per skinned primitive:
    j0 (u16x4), w0_u16 (u16x4 norm), and if hasSkin8: j1 (u16x4), w1_u16 (u16x4 norm)].
    """
    meta_prims = []
    chunks = []
    byte_off = 0
    for p in remapped["primitives"]:
        if not p["skinned"]:
            meta_prims.append({"primIndex": p["primIndex"], "vertices": p["vertices"], "skinned": False})
            continue
        j0_b = p["j0"].tobytes()
        w0_u16 = np.clip(np.round(p["w0"] * 65535.0), 0, 65535).astype("<u2").tobytes()
        entry = {
            "primIndex": p["primIndex"],
            "vertices": p["vertices"],
            "skinned": True,
            "hasSkin8": p["hasSkin8"],
            "maxInfluences": p["maxInfluences"],
            "localBones": p["localBones"],
            "offset": byte_off,
        }
        chunks.append(j0_b)
        chunks.append(w0_u16)
        byte_off += len(j0_b) + len(w0_u16)
        if p["hasSkin8"]:
            j1_b = p["j1"].tobytes()
            w1_u16 = np.clip(np.round(p["w1"] * 65535.0), 0, 65535).astype("<u2").tobytes()
            chunks.append(j1_b)
            chunks.append(w1_u16)
            byte_off += len(j1_b) + len(w1_u16)
        meta_prims.append(entry)

    header_obj = {
        "name": remapped["name"],
        "lod": remapped["lod"],
        "masterBoneCount": remapped["masterBoneCount"],
        "localBones": remapped["localBones"],
        "maxInfluences": remapped["maxInfluences"],
        "packedJoints": remapped["packedJoints"],
        "extraJoints": remapped["extraJoints"],
        "primitives": meta_prims,
    }
    header_bytes = json.dumps(header_obj, separators=(",", ":")).encode("utf-8")
    pad = (-len(header_bytes)) % 4
    header_bytes += b" " * pad
    return struct.pack("<I", len(header_bytes)) + header_bytes + b"".join(chunks)
