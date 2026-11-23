# SPDX-License-Identifier: GPL-3.0-or-later
"""Bake RAGE 9 body proportions & height into rigged glTF 2.0 / GLB exports.

Standalone backend module for:
- Feature #5: Baking character body morphs (27+ GC* channels) & height_scale
  directly into skinned mesh vertices (POSITION, NORMAL, TANGENT) and updating
  the exported glTF skeleton joint rest pose + inverseBindMatrices so rest-pose
  skinning is identity (W_rest @ IBM == I) and Blender/Maya animations play
  cleanly without double-applying morphs or snapping back.
- Feature #6a: Exact RAGE 9 RIG0 procedural body-shape expression graph loader
  and evaluator (`LOD0_Body_Shapes_Female` & `LOD0_Body_Shapes_Male` from
  `anim.rpf`), complete GC-channel -> RIG0 scalar hash mapping (including
  channel `0xb0eae798` -> `0x8a7d0c78` driving 13 female breast/chest bones),
  and 14 newly cracked GTA VI `gc*` channel names.
"""
from __future__ import annotations

import math
import struct
import zlib
from typing import Any

import numpy as np
from paths import GAME, INDEX

ANIM_RPF = GAME / "anim.rpf"
ANIM_SPANS = INDEX / "anim.rpf" / "deflate-spans.bin"

# RAGE Z-up -> glTF Y-up orthogonal basis conversion: (x, y, z) -> (x, z, -y)
C_YUP = np.array(
    [
        [1.0, 0.0, 0.0, 0.0],
        [0.0, 0.0, 1.0, 0.0],
        [0.0, -1.0, 0.0, 0.0],
        [0.0, 0.0, 0.0, 1.0],
    ],
    dtype=np.float64,
)
C_YUP_T = C_YUP.T

# Newly cracked GTA VI morph channel names from 000106153000.pso (lowercase JOAAT)
NEWLY_CRACKED_MORPH_NAMES: dict[int, str] = {
    0x035B1A7B: "gcage",
    0x78C868AF: "gcbelly",
    0xC4492466: "gcbodybuilder",
    0x09AD0893: "gcchild",
    0xF9CA516F: "gcdir",
    0x277CE8EB: "gcheels",
    0xA519DF9B: "gcpear",
    0x093A28CC: "gcspeed",
    0x33770C39: "gcthigh",
    0xD3B7DF66: "gcweight",
    0x5A016131: "gcchestposition",
    0x8590C258: "gchandsize",
    0xE7B0939B: "gcheadscale",
    0xED59C0F6: "gcfootsize",
}

# Verified GC channel -> RIG0 scalar input hashes from anim.rpf/000106153000.pso (blk#8 0xe498d0f4).
# Each entry maps canonical channel name (lowercased for lookup) -> {"+": rig_hash, "-": rig_hash}.
GC_CHANNEL_TO_RIG0: dict[str, dict[str, int]] = {
    "gcbreasts":        {"+": 0x377B0101, "-": 0x48F90678},
    "gcbutt":           {"+": 0x5A502AE2},
    "gccleavage":       {"+": 0xF9DC7B4C, "-": 0xC7717EDF},
    "gcbellyround":     {"+": 0xA1AEDE2A},
    "gcbreastheight":   {"+": 0xF9BC636E, "-": 0x12B6DD5F},
    "gcbreastspacing":  {"+": 0xB3E7142F, "-": 0x36C800C8},
    "gcchestheight":    {"+": 0xA064C82B, "-": 0x9B89A2B0},
    "gchipheight":      {"+": 0xA64A2B22, "-": 0xEE16A69C},
    "gchipwidth":       {"+": 0x6C83D62B},
    "gcheadscale":      {"+": 0x9C978205},
    # Uncracked hash 0xb0eae798 (571 peds, 87% female): breast volume/projection/lower-breast
    # fullness morph driving 13 female breast/chest bones in LOD0_Body_Shapes_Female!
    "0xb0eae798":       {"+": 0x8A7D0C78, "-": 0xB2EFCB7F},
    "gcmuscleback":     {"+": 0x7BA939D2},
    "gcmusclebelly":    {"+": 0xD2D829E2},
    "gcmusclebiceps":   {"+": 0xE3E6F882},
    "gcmusclebutt":     {"+": 0xAA02F4EE},
    "gcmusclecalf":     {"+": 0x6A3A3AF1},
    "gcmusclechest":    {"+": 0x3A21E790},
    "gcmuscleforearm":  {"+": 0x36ED82E6},
    "gcmusclehip":      {"+": 0x1E0D9D2B},
    "gcmusclemuffin":   {"+": 0x506B5EBB},
    "gcmuscleneck":     {"+": 0x670EDEEB},
    "gcmuscleshoulder": {"+": 0x0BD0C264},
    "gcmusclethigh":    {"+": 0x15F21DFD},
    "gcmuscletriceps":  {"+": 0xDD1A11D2},
    "gcmuscleupperarm": {"+": 0xBD15772F},
    "gcweightback":     {"+": 0x0CC59918, "-": 0x655A230D},
    "gcweightbelly":    {"+": 0x317A0EE6, "-": 0x337059B9},
    "gcweightbutt":     {"+": 0x616B260F, "-": 0xA4443235},
    "gcweightcalf":     {"+": 0x9BB196E1, "-": 0x4C244729},
    "gcweightchest":    {"+": 0x3FCD2976, "-": 0x4ED58768},
    "gcweightforearm":  {"+": 0x0580B09B, "-": 0xE7334890},
    "gcweighthip":      {"+": 0xBF1E04C0, "-": 0xD143934B},
    "gcweightmuffin":   {"+": 0x26D96B2F, "-": 0x31942254},
    "gcweightneck":     {"+": 0xB8074759, "-": 0xB7F3AD56},
    "gcweightshoulder": {"+": 0xD4D23C1E, "-": 0x24670AFD},
    "gcweightthigh":    {"+": 0x67554B34, "-": 0xA45A32E1},
    "gcweightupperarm": {"+": 0x07A48486, "-": 0x66402B72},
}

_RIG0_CACHE: dict[bool, dict[str, Any] | None] = {}


# ---------------------------------------------------------------------------
# Quaternion & matrix helpers (pure NumPy, float64)
# ---------------------------------------------------------------------------

def _clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else (hi if v > hi else v)


def _quat_normalize(q: np.ndarray) -> np.ndarray:
    n = float(np.linalg.norm(q))
    if n < 1e-12:
        return np.array([0.0, 0.0, 0.0, 1.0], dtype=np.float64)
    return q / n


def _quat_mul(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    ax, ay, az, aw = float(a[0]), float(a[1]), float(a[2]), float(a[3])
    bx, by, bz, bw = float(b[0]), float(b[1]), float(b[2]), float(b[3])
    return np.array(
        [
            aw * bx + ax * bw + ay * bz - az * by,
            aw * by - ax * bz + ay * bw + az * bx,
            aw * bz + ax * by - ay * bx + az * bw,
            aw * bw - ax * bx - ay * by - az * bz,
        ],
        dtype=np.float64,
    )


def _quat_slerp_from_identity(dq: np.ndarray, w: float) -> np.ndarray:
    """Compute slerp(identity, dq, w) for scalar weight w >= 0."""
    if abs(w) < 1e-7:
        return np.array([0.0, 0.0, 0.0, 1.0], dtype=np.float64)
    q = _quat_normalize(np.asarray(dq, dtype=np.float64))
    if q[3] < 0.0:
        q = -q
    cos_half = _clamp(float(q[3]), -1.0, 1.0)
    half_angle = math.acos(cos_half)
    if half_angle < 1e-7:
        return np.array([0.0, 0.0, 0.0, 1.0], dtype=np.float64)
    sin_half = math.sin(half_angle)
    axis = q[:3] / sin_half
    new_half = half_angle * w
    s = math.sin(new_half)
    return np.array([axis[0] * s, axis[1] * s, axis[2] * s, math.cos(new_half)], dtype=np.float64)


def _trs_mat4(t: np.ndarray, q: np.ndarray, s: np.ndarray) -> np.ndarray:
    """Build standard row-major 4x4 TRS matrix (v' = M @ v) from t(3), q(xyzw), s(3)."""
    qn = _quat_normalize(q)
    qx, qy, qz, qw = float(qn[0]), float(qn[1]), float(qn[2]), float(qn[3])
    sx, sy, sz = float(s[0]), float(s[1]), float(s[2])
    xx, yy, zz = qx * qx, qy * qy, qz * qz
    xy, xz, yz = qx * qy, qx * qz, qy * qz
    wx, wy, wz = qw * qx, qw * qy, qw * qz
    M = np.empty((4, 4), dtype=np.float64)
    M[0, 0] = (1.0 - 2.0 * (yy + zz)) * sx
    M[1, 0] = (2.0 * (xy + wz)) * sx
    M[2, 0] = (2.0 * (xz - wy)) * sx
    M[3, 0] = 0.0
    M[0, 1] = (2.0 * (xy - wz)) * sy
    M[1, 1] = (1.0 - 2.0 * (xx + zz)) * sy
    M[2, 1] = (2.0 * (yz + wx)) * sy
    M[3, 1] = 0.0
    M[0, 2] = (2.0 * (xz + wy)) * sz
    M[1, 2] = (2.0 * (yz - wx)) * sz
    M[2, 2] = (1.0 - 2.0 * (xx + yy)) * sz
    M[3, 2] = 0.0
    M[0, 3] = float(t[0])
    M[1, 3] = float(t[1])
    M[2, 3] = float(t[2])
    M[3, 3] = 1.0
    return M


def _mat3_to_quat(R: np.ndarray) -> np.ndarray:
    """Convert a proper 3x3 rotation matrix to unit quaternion [x, y, z, w]."""
    tr = float(R[0, 0] + R[1, 1] + R[2, 2])
    if tr > 0.0:
        s = math.sqrt(tr + 1.0) * 2.0
        w = 0.25 * s
        x = (R[2, 1] - R[1, 2]) / s
        y = (R[0, 2] - R[2, 0]) / s
        z = (R[1, 0] - R[0, 1]) / s
    elif R[0, 0] > R[1, 1] and R[0, 0] > R[2, 2]:
        s = math.sqrt(max(1e-12, 1.0 + R[0, 0] - R[1, 1] - R[2, 2])) * 2.0
        w = (R[2, 1] - R[1, 2]) / s
        x = 0.25 * s
        y = (R[0, 1] + R[1, 0]) / s
        z = (R[0, 2] + R[2, 0]) / s
    elif R[1, 1] > R[2, 2]:
        s = math.sqrt(max(1e-12, 1.0 + R[1, 1] - R[0, 0] - R[2, 2])) * 2.0
        w = (R[0, 2] - R[2, 0]) / s
        x = (R[0, 1] + R[1, 0]) / s
        y = 0.25 * s
        z = (R[1, 2] + R[2, 1]) / s
    else:
        s = math.sqrt(max(1e-12, 1.0 + R[2, 2] - R[0, 0] - R[1, 1])) * 2.0
        w = (R[1, 0] - R[0, 1]) / s
        x = (R[0, 2] + R[2, 0]) / s
        y = (R[1, 2] + R[2, 1]) / s
        z = 0.25 * s
    q = _quat_normalize(np.array([x, y, z, w], dtype=np.float64))
    if q[3] < 0.0:
        q = -q
    return q


def _polar_rotation(A: np.ndarray) -> np.ndarray:
    """Extract nearest proper orthogonal 3x3 rotation matrix R (det=+1) from A via SVD."""
    U, _sig, Vt = np.linalg.svd(A)
    R = U @ Vt
    if np.linalg.det(R) < 0.0:
        U[:, -1] *= -1.0
        R = U @ Vt
    return R


def _cofactor_3x3_batch(A: np.ndarray) -> np.ndarray:
    """Vectorized 3x3 cofactor matrix for (V, 3, 3) linear transforms (exact normal matrix up to scale)."""
    cof = np.empty_like(A)
    a00, a01, a02 = A[:, 0, 0], A[:, 0, 1], A[:, 0, 2]
    a10, a11, a12 = A[:, 1, 0], A[:, 1, 1], A[:, 1, 2]
    a20, a21, a22 = A[:, 2, 0], A[:, 2, 1], A[:, 2, 2]
    cof[:, 0, 0] = a11 * a22 - a12 * a21
    cof[:, 0, 1] = -(a10 * a22 - a12 * a20)
    cof[:, 0, 2] = a10 * a21 - a11 * a20
    cof[:, 1, 0] = -(a01 * a22 - a02 * a21)
    cof[:, 1, 1] = a00 * a22 - a02 * a20
    cof[:, 1, 2] = -(a00 * a21 - a01 * a20)
    cof[:, 2, 0] = a01 * a12 - a02 * a11
    cof[:, 2, 1] = -(a00 * a12 - a02 * a10)
    cof[:, 2, 2] = a00 * a11 - a01 * a10
    return cof


# ---------------------------------------------------------------------------
# Feature #6a: Exact RAGE 9 RIG0 Body_Shapes expression graph parser
# ---------------------------------------------------------------------------

def load_rage9_body_shapes(is_male: bool = False) -> dict[str, Any] | None:
    """Parse Rockstar's exact RAGE 9 `LOD0_Body_Shapes_{Female,Male}` RIG0 graph from anim.rpf.

    Returns a cached dict with:
      - `name`: ASCII rig path
      - `n_regs`: register count
      - `bone_in_regs`: dict[bone_hash, dict[track ('T'|'R'|'S'), reg_idx]]
      - `scalar_in_regs`: dict[scalar_hash, reg_idx]
      - `ops`: decoded VM instruction list (`0x85` clamp, `0x46` mul, `0x81` remap, `0x86` blend-delta)
    Returns None if `anim.rpf` or `deflate-spans.bin` is unavailable.
    """
    key = bool(is_male)
    if key in _RIG0_CACHE:
        return _RIG0_CACHE[key]
    if not ANIM_RPF.is_file() or not ANIM_SPANS.is_file():
        _RIG0_CACHE[key] = None
        return None
    try:
        raw_sp = ANIM_SPANS.read_bytes()
        with open(ANIM_RPF, "rb") as f:
            off6, clen6, _, _, _ = struct.unpack_from("<QQQII", raw_sp, 130346 * 32)
            f.seek(off6)
            d6 = zlib.decompress(f.read(clen6))
            off7, clen7, _, _, _ = struct.unpack_from("<QQQII", raw_sp, 130347 * 32)
            f.seek(off7)
            d7 = zlib.decompress(f.read(clen7))
        joined = d6 + d7
        target = b"LOD0_Body_Shapes_Male" if key else b"LOD0_Body_Shapes_Female"
        pos = joined.find(target)
        if pos < 0:
            _RIG0_CACHE[key] = None
            return None
        r0 = joined.rfind(b"RIG0", 0, pos)
        rig_pos = joined.rfind(b"RIG\x02", max(0, r0 - 512), r0)
        if r0 < 0 or rig_pos < 0:
            _RIG0_CACHE[key] = None
            return None
        _, rig_size = struct.unpack_from("<II", joined, rig_pos)
        rig0 = joined[r0:r0 + rig_size]
        n_sec = struct.unpack_from("<I", rig0, 0x10)[0]
        secs = struct.unpack_from(f"<{n_sec}I", rig0, 0x14)
        sec0 = rig0[secs[0]:secs[1]]
        sec1 = rig0[secs[1]:secs[2]]
        sec3 = rig0[secs[3]:secs[4]]
        sec5 = rig0[secs[5]:]
        rig_name = sec5[12:].split(b"\x00", 1)[0].decode("ascii", "ignore")

        _, _, cnt1 = struct.unpack_from("<III", sec1, 0)
        scalar_in_regs: dict[int, int] = {}
        bone_reg_to_dof: dict[int, tuple[int, str]] = {}
        bone_in_regs: dict[int, dict[str, int]] = {}
        track_names = {0: "T", 1: "R", 2: "S"}
        for k in range(cnt1):
            b0, b1, reg, hval = struct.unpack_from("<BBHI", sec1, 12 + k * 8)
            if b1 == 1:
                scalar_in_regs[hval] = reg
            else:
                tr = track_names.get(b0, "T")
                bone_reg_to_dof[reg] = (hval, tr)
                bone_in_regs.setdefault(hval, {})[tr] = reg

        op_words = [struct.unpack_from("<I", sec0, 12 + k * 4)[0] for k in range((len(sec0) - 12) // 4)]
        n_regs = int(op_words[0])
        payload = sec3[8:]
        raw_ops = []
        for w in op_words[1:]:
            if w == 0xFF000000:
                break
            raw_ops.append((w >> 24, w & 0xFFFFFF))

        ops: list[dict[str, Any]] = []
        for idx, (op, off) in enumerate(raw_ops):
            end = raw_ops[idx + 1][1] if idx + 1 < len(raw_ops) else len(payload)
            chunk = payload[off:end]
            if op == 0x85 and len(chunk) >= 12:
                dst, src = struct.unpack_from("<HH", chunk, 0)
                lo, hi = struct.unpack_from("<ff", chunk, 4)
                ops.append({"op": 0x85, "dst": dst, "src": src, "lo": lo, "hi": hi})
            elif op == 0x46 and len(chunk) >= 6:
                a, b, dst = struct.unpack_from("<HHH", chunk, 0)
                ops.append({"op": 0x46, "a": a, "b": b, "dst": dst})
            elif op == 0x81 and len(chunk) >= 8:
                dst, src = struct.unpack_from("<HH", chunk, 0)
                ops.append({"op": 0x81, "dst": dst, "src": src})
            elif op == 0x86 and len(chunk) >= 8:
                nT, nS, nR, _pad = struct.unpack_from("<HHHH", chunk, 0)
                tot = nT + nS + nR
                if len(chunk) != 8 + 6 * tot + 12 * nT + 12 * nS + 16 * nR:
                    continue
                data_off = 8 + 6 * tot
                t_entries = []
                for i in range(nT):
                    d, w, sr = struct.unpack_from("<HHH", chunk, 8 + i * 6)
                    vec = struct.unpack_from("<3f", chunk, data_off + i * 12)
                    t_entries.append((d, w, sr, vec))
                s_off = data_off + nT * 12
                s_entries = []
                for i in range(nS):
                    d, w, sr = struct.unpack_from("<HHH", chunk, 8 + (nT + i) * 6)
                    vec = struct.unpack_from("<3f", chunk, s_off + i * 12)
                    s_entries.append((d, w, sr, vec))
                r_off = s_off + nS * 12
                r_entries = []
                for i in range(nR):
                    d, w, sr = struct.unpack_from("<HHH", chunk, 8 + (nT + nS + i) * 6)
                    q = struct.unpack_from("<4f", chunk, r_off + i * 16)
                    r_entries.append((d, w, sr, q))
                ops.append({"op": 0x86, "T": t_entries, "S": s_entries, "R": r_entries})

        parsed = {
            "name": rig_name,
            "n_regs": n_regs,
            "bone_in_regs": bone_in_regs,
            "bone_reg_to_dof": bone_reg_to_dof,
            "scalar_in_regs": scalar_in_regs,
            "ops": ops,
        }
        _RIG0_CACHE[key] = parsed
        return parsed
    except Exception:
        _RIG0_CACHE[key] = None
        return None


def eval_rage9_body_shapes(
    joints: list[dict],
    body_morphs: dict[str, float] | None,
    is_male: bool = False,
) -> dict[int, dict[str, np.ndarray]]:
    """Evaluate Rockstar's exact RAGE 9 RIG0 Body_Shapes VM for the given skeleton & body_morphs.

    Returns dict[joint_index, {"T": new_t(3), "S": new_s(3), "R": new_q(4)}] in RAGE Z-up space
    for every joint whose DOF is written by the expression graph.
    """
    rig = load_rage9_body_shapes(is_male=is_male)
    if rig is None or not body_morphs:
        return {}

    # Normalize morph keys (case-insensitive, supporting master 'weight'/'muscle' offsets)
    cm_low = {str(k).lower(): float(v) for k, v in body_morphs.items() if v is not None}
    w_all = cm_low.get("weight", 0.0)
    m_all = cm_low.get("muscle", 0.0)

    # Populate scalar registers from body_morphs via GC_CHANNEL_TO_RIG0
    scalar_regs: dict[int, float] = {}
    s_in = rig["scalar_in_regs"]
    for gc_key, sign_map in GC_CHANNEL_TO_RIG0.items():
        val = cm_low.get(gc_key, 0.0)
        if gc_key.startswith("gcweight") and gc_key != "gcweight":
            val = _clamp(val + w_all, -1.0, 1.35)
        elif gc_key.startswith("gcmuscle"):
            val = _clamp(val + m_all, -0.35, 1.35)
        if abs(val) < 1e-6:
            continue
        if val > 0.0 and "+" in sign_map:
            r = s_in.get(sign_map["+"])
            if r is not None:
                scalar_regs[r] = val
        elif val < 0.0 and "-" in sign_map:
            r = s_in.get(sign_map["-"])
            if r is not None:
                scalar_regs[r] = -val

    if not scalar_regs:
        return {}

    # Initialize bone DOF registers from skeleton bind pose
    j_by_hash = {int(j["hash"]) & 0xFFFFFFFF: idx for idx, j in enumerate(joints) if "hash" in j}
    vec_regs: dict[int, np.ndarray] = {}
    quat_regs: dict[int, np.ndarray] = {}
    for bh, dof_map in rig["bone_in_regs"].items():
        ji = j_by_hash.get(bh)
        if ji is None:
            continue
        j = joints[ji]
        if "T" in dof_map:
            vec_regs[dof_map["T"]] = np.array(j["trans"], dtype=np.float64)
        if "S" in dof_map:
            vec_regs[dof_map["S"]] = np.array(j["scale"], dtype=np.float64)
        if "R" in dof_map:
            quat_regs[dof_map["R"]] = _quat_normalize(np.array(j["rot"], dtype=np.float64))

    # Execute VM ops sequentially
    touched_regs: set[int] = set()
    for op in rig["ops"]:
        code = op["op"]
        if code == 0x85:
            scalar_regs[op["dst"]] = _clamp(scalar_regs.get(op["src"], 0.0), op["lo"], op["hi"])
        elif code == 0x46:
            scalar_regs[op["dst"]] = scalar_regs.get(op["a"], 0.0) * scalar_regs.get(op["b"], 0.0)
        elif code == 0x81:
            scalar_regs[op["dst"]] = _clamp(scalar_regs.get(op["src"], 0.0), 0.0, 1.0)
        elif code == 0x86:
            for d, w_reg, sr, vec in op["T"]:
                w = scalar_regs.get(w_reg, 0.0)
                if abs(w) > 1e-6 and sr in vec_regs:
                    vec_regs[d] = vec_regs[sr] + w * np.array(vec, dtype=np.float64)
                    touched_regs.add(d)
            for d, w_reg, sr, vec in op["S"]:
                w = scalar_regs.get(w_reg, 0.0)
                if abs(w) > 1e-6 and sr in vec_regs:
                    vec_regs[d] = vec_regs[sr] + w * np.array(vec, dtype=np.float64)
                    touched_regs.add(d)
            for d, w_reg, sr, qv in op["R"]:
                w = scalar_regs.get(w_reg, 0.0)
                if abs(w) > 1e-6 and sr in quat_regs:
                    dq = _quat_slerp_from_identity(np.array(qv, dtype=np.float64), w)
                    # Verified via L/R mirror symmetry (BM_L_PectoralisLo err=0.00019 vs 0.62):
                    # RAGE 9 applies rotation deltas by right-multiplication q_out = q_in * slerp(I, dq, w).
                    quat_regs[d] = _quat_normalize(_quat_mul(quat_regs[sr], dq))
                    touched_regs.add(d)

    out: dict[int, dict[str, np.ndarray]] = {}
    reg_to_dof = rig["bone_reg_to_dof"]
    for reg in touched_regs:
        dof = reg_to_dof.get(reg)
        if dof is None:
            continue
        bh, tr = dof
        ji = j_by_hash.get(bh)
        if ji is None:
            continue
        entry = out.setdefault(ji, {})
        if tr == "T":
            entry["T"] = vec_regs[reg]
        elif tr == "S":
            entry["S"] = np.clip(vec_regs[reg], 0.05, 3.0)
        elif tr == "R":
            entry["R"] = quat_regs[reg]
    return out


# ---------------------------------------------------------------------------
# Feature #5: Exact model-view.js `_evalBoneMorph` port (100% parity)
# ---------------------------------------------------------------------------

def eval_bone_morph(
    bone_name: str,
    char_morph: dict[str, Any] | None,
    is_male: bool | None = None,
) -> tuple[float, float, float, float, float, float] | None:
    """Evaluate RAGE 9 local-axis BM_*/SEC_* bone scale & translation deltas.

    Matches `viewer/model-view.js` `_evalBoneMorph(name, cm)` (L2406-2559)
    identically so exported GLBs match Lineup's 3D viewport 100%.

    Returns `(sx, sy, sz, dx, dy, dz)` in RAGE Z-up local bone space, or `None`
    if the bone is unaffected.
    """
    if not char_morph:
        return None
    cm = char_morph

    def _get(key: str) -> float:
        v = cm.get(key)
        if v is None:
            # Allow case-insensitive fallback (e.g. 'gcbreasts' vs 'GCBreasts')
            kl = key.lower()
            for ck, cv in cm.items():
                if isinstance(ck, str) and ck.lower() == kl:
                    v = cv
                    break
        return float(v) if v is not None else 0.0

    sx = 1.0
    sy = 1.0
    sz = 1.0
    dx = 0.0
    dy = 0.0
    dz = 0.0
    w_all = _get("weight")
    m_all = _get("muscle")

    def w_ch(k: str) -> float:
        return _clamp(_get(k) + w_all, -1.0, 1.35)

    def m_ch(k: str) -> float:
        return _clamp(_get(k) + m_all, -0.35, 1.35)

    if is_male is None:
        is_male = (float(cm.get("defaultHeight") or 1.72)) > 1.77

    name = bone_name
    if name in ("BM_L_Breast", "BM_R_Breast"):
        if is_male:
            s = _get("GCBreasts") * 0.24 + w_ch("gcweightchest") * 0.14 + m_ch("gcmusclechest") * 0.12
        else:
            s = _get("GCBreasts") * 0.55 + w_ch("gcweightchest") * 0.16
        c = _get("GCCleavage") * 0.016
        sp = _get("gcbreastspacing") * 0.018
        bh = _get("gcbreastheight") * 0.018 + _get("gcchestheight") * 0.012
        sgn = 1.0 if name.startswith("BM_L_") else -1.0
        if is_male:
            sx += s * 0.25
            sy += s * 0.42
            sz += s * 0.36
            dx += bh * 0.5
            dy += s * 0.012
        else:
            sx += s * 0.52
            sy += s * 0.85
            sz += s * 0.55
            dx += bh
            dy += s * 0.024
            dz += sgn * (sp - c)
    elif name in ("SEC_L_Full_Breast", "SEC_R_Full_Breast", "SEC_L_Mid_Breast", "SEC_R_Mid_Breast"):
        s = 0.0 if is_male else _get("GCBreasts") * 0.25
        sx += s * 0.28
        sy += s * 0.45
        sz += s * 0.28
    elif name in ("BM_L_Top_Breast", "BM_R_Top_Breast", "BM_L_Bot_Breast", "BM_R_Bot_Breast"):
        s = _get("GCBreasts") * (0.12 if is_male else 0.25)
        sx += s * 0.20
        sy += s * 0.42
        sz += s * 0.25
    elif name == "BM_Cleavage":
        c = _get("GCCleavage")
        dy += c * 0.014
        sz -= c * 0.16
    elif "Pectoralis" in name:
        mv = m_ch("gcmusclechest") + (max(0.0, _get("GCBreasts")) * 0.45 if is_male else 0.0)
        wv = w_ch("gcweightchest")
        sx += mv * 0.14 + wv * 0.10
        sy += mv * 0.28 + wv * 0.20
        sz += mv * 0.18 + wv * 0.12
        dy += mv * 0.012 + max(0.0, wv) * 0.010
    elif name in ("BM_L_Butt", "BM_R_Butt"):
        b = _get("GCButt") * 0.55 + w_ch("gcweightbutt") * 0.45 + m_ch("gcmusclebutt") * 0.42
        hw = _get("gchipwidth")
        hh = _get("gchipheight")
        sgn = 1.0 if name.startswith("BM_L_") else -1.0
        sx += b * 0.28
        sy += b * 0.85
        sz += b * 0.48 + hw * 0.24
        dx -= m_ch("gcmusclebutt") * 0.008 - max(0.0, w_ch("gcweightbutt")) * 0.004 + hh * 0.012
        dy -= b * 0.052
        dz -= sgn * (hw * 0.022 + b * 0.010)
    elif name in ("BM_L_Pelvis", "BM_R_Pelvis"):
        wv = w_ch("gcweighthip")
        mv = m_ch("gcmusclehip")
        hw = _get("gchipwidth")
        hh = _get("gchipheight")
        b = _get("GCButt") * 0.35 + w_ch("gcweightbutt") * 0.20 + m_ch("gcmusclebutt") * 0.15
        sgn = 1.0 if name.startswith("BM_L_") else -1.0
        sy += wv * 0.22 + mv * 0.10 + b * 0.18
        sz += hw * 0.34 + wv * 0.24 + mv * 0.12
        dx -= hh * 0.014
        dz -= sgn * (hw * 0.026 + wv * 0.010)
    elif name in ("BM_pelvisRear", "BM_pelvisRearLo"):
        wv = w_ch("gcweighthip")
        hw = _get("gchipwidth")
        b = _get("GCButt") * 0.45 + w_ch("gcweightbutt") * 0.32 + m_ch("gcmusclebutt") * 0.28
        sy += b * 0.36 + wv * 0.14
        sz += hw * 0.22 + wv * 0.18 + b * 0.18
        dy -= b * 0.018
    elif name in ("BM_pelvis", "BM_pelvisFront"):
        wv = w_ch("gcweighthip")
        mv = m_ch("gcmusclehip")
        hw = _get("gchipwidth")
        sy += wv * 0.16 + _get("GCButt") * 0.08
        sz += hw * 0.16 + wv * 0.16 + mv * 0.08
    elif name.startswith("BM_Stomach"):
        wb = w_ch("gcweightbelly")
        wm = w_ch("gcweightmuffin")
        mb = m_ch("gcmusclebelly")
        br = _get("gcbellyround")
        is_side = ("_L_" in name) or ("_R_" in name)
        v = (wm * 0.28 + wb * 0.16) if is_side else (wb * 0.34 + wm * 0.12)
        sx += v * 0.12
        sy += v * 0.72 + br * 0.42 + mb * 0.10
        sz += v * 0.52 + wm * 0.22 + mb * 0.08
        dy += max(0.0, v + br * 0.5) * 0.042
    elif ("Spine0" in name) or ("Spine1" in name):
        wm = w_ch("gcweightmuffin")
        wb = w_ch("gcweightback")
        mb = m_ch("gcmuscleback")
        mm = m_ch("gcmusclemuffin")
        sy += wm * 0.18 + wb * 0.14 + mb * 0.08
        sz += wm * 0.22 + wb * 0.12 + mb * 0.08 + mm * 0.10
    elif any(k in name for k in ("Spine2", "Spine3", "Spine4", "Spine5")):
        wb = w_ch("gcweightback")
        wc = w_ch("gcweightchest")
        mb = m_ch("gcmuscleback")
        sy += wb * 0.14 + wc * 0.14 + mb * 0.16
        sz += wb * 0.14 + wc * 0.12 + mb * 0.20
    elif any(k in name for k in ("Deltoid", "Clavicle", "Scapula", "Trap")):
        ws = w_ch("gcweightshoulder")
        ms = m_ch("gcmuscleshoulder")
        mb = m_ch("gcmuscleback")
        sx += ws * 0.08 + ms * 0.16
        sy += ws * 0.16 + ms * 0.24
        sz += ws * 0.16 + ms * 0.26 + (mb * 0.16 if "Trap" in name else 0.0)
    elif "Bicep" in name:
        wu = w_ch("gcweightupperarm")
        mu = m_ch("gcmuscleupperarm")
        mb = m_ch("gcmusclebiceps")
        sy += wu * 0.22 + mu * 0.26 + mb * 0.24
        sz += wu * 0.22 + mu * 0.26 + mb * 0.24
    elif "Tricep" in name:
        wu = w_ch("gcweightupperarm")
        mu = m_ch("gcmuscleupperarm")
        mt = m_ch("gcmuscletriceps")
        sy += wu * 0.22 + mu * 0.26 + mt * 0.24
        sz += wu * 0.22 + mu * 0.26 + mt * 0.24
    elif "UpperArm" in name:
        wu = w_ch("gcweightupperarm")
        mu = m_ch("gcmuscleupperarm")
        sy += wu * 0.20 + mu * 0.26
        sz += wu * 0.20 + mu * 0.26
    elif ("Forearm" in name) or ("Brachioradialis" in name):
        wf = w_ch("gcweightforearm")
        mf = m_ch("gcmuscleforearm")
        sy += wf * 0.18 + mf * 0.24
        sz += wf * 0.18 + mf * 0.24
    elif any(k in name for k in ("Thigh", "Quadriceps", "Hamstrings")):
        wt = w_ch("gcweightthigh")
        mt = m_ch("gcmusclethigh")
        hw = _get("gchipwidth")
        sy += wt * 0.30 + mt * 0.28
        sz += wt * 0.30 + mt * 0.26 + hw * 0.14
    elif "Calf" in name:
        wc = w_ch("gcweightcalf")
        mc = m_ch("gcmusclecalf")
        sy += wc * 0.26 + mc * 0.28
        sz += wc * 0.24 + mc * 0.26
    elif any(k in name for k in ("Neck", "Throat", "Mastoid")):
        wn = w_ch("gcweightneck")
        mn = m_ch("gcmuscleneck")
        sy += wn * 0.16 + mn * 0.20
        sz += wn * 0.16 + mn * 0.22
    elif ("Cheek" in name) or (name == "BM_Chin"):
        wn = w_ch("gcweightneck")
        sx += wn * 0.10
        sy += wn * 0.16
        sz += wn * 0.14
    else:
        return None

    if (
        abs(sx - 1.0) < 1e-4
        and abs(sy - 1.0) < 1e-4
        and abs(sz - 1.0) < 1e-4
        and abs(dx) < 1e-5
        and abs(dy) < 1e-5
        and abs(dz) < 1e-5
    ):
        return None
    return (
        _clamp(sx, 0.55, 1.85),
        _clamp(sy, 0.55, 1.85),
        _clamp(sz, 0.55, 1.85),
        dx,
        dy,
        dz,
    )


# ---------------------------------------------------------------------------
# Skeleton matrix evaluation (RAGE Z-up -> glTF Y-up)
# ---------------------------------------------------------------------------

def _topo_order(joints: list[dict]) -> list[int]:
    """Return joint indices ordered so every parent appears before its children."""
    n = len(joints)
    visited = [0] * n  # 0=unvisited, 1=visiting, 2=done
    order: list[int] = []

    def dfs(i: int) -> None:
        if visited[i] == 2:
            return
        visited[i] = 1
        par = int(joints[i].get("parent", -1))
        if 0 <= par < n and visited[par] == 0:
            dfs(par)
        visited[i] = 2
        order.append(i)

    for i in range(n):
        if visited[i] == 0:
            dfs(i)
    return order


def compute_morphed_skin_matrices(
    joints: list[dict],
    body_morphs: dict[str, Any] | None = None,
    height_scale: float = 1.0,
    is_male: bool = False,
    *,
    use_rage9_rig: bool = False,
) -> dict[str, Any]:
    """Evaluate morphed pose & glTF Y-up skinning matrices for a character skeleton.

    Matches `model-view.js` L2339-2400 identically by default (`use_rage9_rig=False`),
    or evaluates Rockstar's exact `LOD0_Body_Shapes_{Female,Male}` RIG0 expression
    graph when `use_rage9_rig=True`.

    Returns a dict with:
      - `skin_matrices_yup`: (N, 4, 4) float64, glTF Y-up vertex skin matrices M_i = W_morphed_yup_i @ IBM_orig_yup_i
      - `world_morphed_yup`: (N, 4, 4) float64, full morphed world matrices (with scale) in glTF Y-up
      - `rest_world_yup`: (N, 4, 4) float64, clean rigid (unit-scale) rest world matrices at morphed pivots
      - `rest_local_trans_yup`: (N, 3) float64, clean local rest translations for glTF joint nodes
      - `rest_local_rot_yup`: (N, 4) float64, clean local rest unit quaternions (xyzw) for glTF joint nodes
      - `new_ibm_yup`: (N, 4, 4) float64, inverse bind matrices `inv(rest_world_yup)`
      - `height_scale`: float, clamped height scale
      - `has_effect`: bool, True if any joint deviates from bind pose
    """
    n = len(joints)
    hs = _clamp(float(height_scale if height_scale is not None else 1.0), 0.78, 1.26)
    has_hs = abs(hs - 1.0) > 1e-4

    rig_overrides: dict[int, dict[str, np.ndarray]] = {}
    if use_rage9_rig and body_morphs:
        rig_overrides = eval_rage9_body_shapes(joints, body_morphs, is_male=is_male)

    loc_rage = np.empty((n, 4, 4), dtype=np.float64)
    has_effect = has_hs

    for i, j in enumerate(joints):
        t = np.array(j["trans"], dtype=np.float64)
        q = _quat_normalize(np.array(j["rot"], dtype=np.float64))
        s = np.array(j["scale"], dtype=np.float64)
        par = int(j.get("parent", -1))

        if use_rage9_rig and i in rig_overrides:
            ov = rig_overrides[i]
            if "T" in ov:
                t = ov["T"]
            if "S" in ov:
                s = ov["S"]
            if "R" in ov:
                q = ov["R"]
            has_effect = True
        elif not use_rage9_rig and body_morphs:
            grp = j.get("group", "")
            bn = j.get("name", "")
            if grp in ("BM", "SEC") or bn.startswith(("BM_", "SEC_")):
                m = eval_bone_morph(bn, body_morphs, is_male=is_male)
                if m is not None:
                    s = s * np.array([m[0], m[1], m[2]], dtype=np.float64)
                    t = t + np.array([m[3], m[4], m[5]], dtype=np.float64)
                    has_effect = True

        if par < 0 and has_hs:
            s = s * hs

        loc_rage[i] = _trs_mat4(t, q, s)

    # Hierarchical world matrices in RAGE Z-up
    order = _topo_order(joints)
    world_rage = np.empty((n, 4, 4), dtype=np.float64)
    for i in order:
        par = int(joints[i].get("parent", -1))
        if 0 <= par < n:
            world_rage[i] = world_rage[par] @ loc_rage[i]
        else:
            world_rage[i] = loc_rage[i]

    # Skinning matrices: M_rage = world_rage @ IBM_orig_rage, then convert to glTF Y-up: C @ M @ C^T
    skin_yup = np.empty((n, 4, 4), dtype=np.float64)
    world_yup = np.empty((n, 4, 4), dtype=np.float64)
    rest_world_yup = np.empty((n, 4, 4), dtype=np.float64)

    for i, j in enumerate(joints):
        ibm_rage = np.array(j["ibmCol"], dtype=np.float64).reshape(4, 4).T
        m_rage = world_rage[i] @ ibm_rage
        skin_yup[i] = C_YUP @ m_rage @ C_YUP_T
        wy = C_YUP @ world_rage[i] @ C_YUP_T
        world_yup[i] = wy

        # Build clean rigid (unit-scale, zero-shear) rest world matrix at the exact morphed joint pivot.
        # This ensures:
        # 1) Every joint sits at its exact morphed & height-scaled anatomical pivot in Blender/Maya.
        # 2) Root height scale is cleanly baked into joint translations (SKEL_ROOT scale == [1,1,1]).
        # 3) BM_* scales are baked into the mesh and removed from rest node scales so animations
        #    and Alt+S never shear, double-apply, or snap back.
        rw = np.eye(4, dtype=np.float64)
        rw[:3, :3] = _polar_rotation(wy[:3, :3])
        rw[:3, 3] = wy[:3, 3]
        rest_world_yup[i] = rw

    # Compute local rigid rest TRS relative to parent rest world & exact new IBMs
    rest_local_trans = np.zeros((n, 3), dtype=np.float64)
    rest_local_rot = np.zeros((n, 4), dtype=np.float64)
    new_ibm_yup = np.empty((n, 4, 4), dtype=np.float64)

    for i in order:
        par = int(joints[i].get("parent", -1))
        if 0 <= par < n:
            loc_rest = np.linalg.inv(rest_world_yup[par]) @ rest_world_yup[i]
        else:
            loc_rest = rest_world_yup[i]
        rest_local_trans[i] = loc_rest[:3, 3]
        rest_local_rot[i] = _mat3_to_quat(loc_rest[:3, :3])
        new_ibm_yup[i] = np.linalg.inv(rest_world_yup[i])

    return {
        "skin_matrices_yup": skin_yup,
        "world_morphed_yup": world_yup,
        "rest_world_yup": rest_world_yup,
        "rest_local_trans_yup": rest_local_trans,
        "rest_local_rot_yup": rest_local_rot,
        "new_ibm_yup": new_ibm_yup,
        "height_scale": hs,
        "has_effect": has_effect,
    }


# ---------------------------------------------------------------------------
# glTF 2.0 Accessor / BufferView I/O helpers
# ---------------------------------------------------------------------------

_COMP_DTYPE = {
    5120: "<i1",
    5121: "<u1",
    5122: "<i2",
    5123: "<u2",
    5125: "<u4",
    5126: "<f4",
}
_TYPE_COUNT = {
    "SCALAR": 1,
    "VEC2": 2,
    "VEC3": 3,
    "VEC4": 4,
    "MAT2": 4,
    "MAT3": 9,
    "MAT4": 16,
}


def _read_accessor(doc: dict, bin_buf: bytearray | bytes, acc_idx: int) -> np.ndarray:
    acc = doc["accessors"][acc_idx]
    bv = doc["bufferViews"][acc["bufferView"]]
    dt = np.dtype(_COMP_DTYPE[acc["componentType"]])
    ncomp = _TYPE_COUNT[acc["type"]]
    count = int(acc["count"])
    offset = int(bv.get("byteOffset", 0)) + int(acc.get("byteOffset", 0))
    stride = int(bv.get("byteStride", 0))
    item_bytes = dt.itemsize * ncomp
    if stride and stride != item_bytes:
        raw = np.frombuffer(bin_buf, dtype=np.uint8, count=stride * (count - 1) + item_bytes, offset=offset)
        out = np.empty((count, ncomp), dtype=dt)
        for i in range(count):
            out[i] = np.frombuffer(raw[i * stride : i * stride + item_bytes], dtype=dt, count=ncomp)
        arr = out
    else:
        arr = np.frombuffer(bin_buf, dtype=dt, count=count * ncomp, offset=offset).copy()
        if ncomp > 1:
            arr = arr.reshape(count, ncomp)
    if acc.get("normalized"):
        ct = acc["componentType"]
        if ct == 5121:
            arr = arr.astype(np.float64) / 255.0
        elif ct == 5123:
            arr = arr.astype(np.float64) / 65535.0
        elif ct == 5120:
            arr = np.maximum(arr.astype(np.float64) / 127.0, -1.0)
        elif ct == 5122:
            arr = np.maximum(arr.astype(np.float64) / 32767.0, -1.0)
    return arr


def _append_accessor(
    doc: dict,
    bin_buf: bytearray,
    arr: np.ndarray,
    vtype: str,
    component_type: int = 5126,
    target: int | None = None,
    with_minmax: bool = False,
) -> int:
    pad = (-len(bin_buf)) % 4
    if pad:
        bin_buf.extend(b"\x00" * pad)
    byte_off = len(bin_buf)
    raw = np.ascontiguousarray(arr, dtype=_COMP_DTYPE[component_type]).tobytes()
    bin_buf.extend(raw)
    bv: dict[str, Any] = {
        "buffer": 0,
        "byteOffset": byte_off,
        "byteLength": len(raw),
    }
    if target is not None:
        bv["target"] = target
    bv_idx = len(doc["bufferViews"])
    doc["bufferViews"].append(bv)
    acc: dict[str, Any] = {
        "bufferView": bv_idx,
        "componentType": component_type,
        "count": int(arr.shape[0]),
        "type": vtype,
    }
    if with_minmax and len(arr):
        acc["min"] = [float(x) for x in arr.min(axis=0)]
        acc["max"] = [float(x) for x in arr.max(axis=0)]
    acc_idx = len(doc["accessors"])
    doc["accessors"].append(acc)
    return acc_idx


# ---------------------------------------------------------------------------
# Main GLB Morph Baking Entrypoint
# ---------------------------------------------------------------------------

def bake_morphs_into_glb(
    out_doc: dict,
    out_bin: bytearray,
    combined_skel: dict | None,
    body_morphs: dict[str, Any] | None = None,
    height_scale: float = 1.0,
    is_male: bool = False,
    *,
    use_rage9_rig: bool = False,
) -> dict[str, Any]:
    """Bake body_morphs and height_scale directly into a glTF document & binary buffer in-place.

    - For every skinned primitive (`JOINTS_0`/`WEIGHTS_0`, optional `JOINTS_1`/`WEIGHTS_1`):
      bakes LBS-morphed `POSITION` (updating accessor `min`/`max`), cofactor-transformed
      & renormalized `NORMAL`, and re-orthogonalized `TANGENT` (if present) into new
      bufferViews appended to `out_bin` (zero aliasing risk).
    - Updates every joint node's glTF `translation`/`rotation` (with clean unit `scale=[1,1,1]`)
      and recomputes `inverseBindMatrices` directly from the written joint node transforms so
      `W_rest_i @ IBM_i == I` (max error < 1e-6) and animations play cleanly in Blender/Maya.
    - Scales `rig_root_node["translation"]` (and any animation translation tracks) by
      `height_scale` so feet remain planted on y=0.
    - Scales unskinned meshes (if any) when `height_scale != 1.0`.

    Returns a summary stats dict (`baked`, `skinned_prims`, `unskinned_prims`, `joints_updated`,
    `max_rest_ibm_err`, `height_scale`).
    """
    hs = _clamp(float(height_scale if height_scale is not None else 1.0), 0.78, 1.26)
    has_hs = abs(hs - 1.0) > 1e-4
    has_morphs = bool(body_morphs and any(abs(float(v)) > 1e-5 for v in body_morphs.values() if isinstance(v, (int, float))))

    if not has_hs and not has_morphs:
        return {
            "baked": False,
            "skinned_prims": 0,
            "unskinned_prims": 0,
            "joints_updated": 0,
            "max_rest_ibm_err": 0.0,
            "height_scale": hs,
        }

    nodes = out_doc.get("nodes") or []
    meshes = out_doc.get("meshes") or []
    skins = out_doc.get("skins") or []

    skinned_mesh_indices: set[int] = set()
    unskinned_mesh_indices: set[int] = set()
    unskinned_node_indices: list[int] = []
    for ni, node in enumerate(nodes):
        if "mesh" not in node:
            continue
        mi = int(node["mesh"])
        if "skin" in node:
            skinned_mesh_indices.add(mi)
        else:
            unskinned_mesh_indices.add(mi)
            unskinned_node_indices.append(ni)

    skinned_prims = 0
    unskinned_prims = 0
    joints_updated = 0
    max_rest_ibm_err = 0.0

    # 1. Bake skinned meshes + update skeleton & inverseBindMatrices
    if combined_skel is not None and skins and skinned_mesh_indices:
        joints = combined_skel["joints"]
        n_joints = len(joints)
        eval_res = compute_morphed_skin_matrices(
            joints,
            body_morphs=body_morphs,
            height_scale=hs,
            is_male=is_male,
            use_rage9_rig=use_rage9_rig,
        )
        if eval_res["has_effect"]:
            skin_mats = eval_res["skin_matrices_yup"]  # (N, 4, 4) float64

            for mi in sorted(skinned_mesh_indices):
                mesh = meshes[mi]
                for prim in mesh.get("primitives") or []:
                    attrs = prim.get("attributes") or {}
                    if "POSITION" not in attrs or "JOINTS_0" not in attrs or "WEIGHTS_0" not in attrs:
                        continue
                    pos = _read_accessor(out_doc, out_bin, attrs["POSITION"]).astype(np.float64)
                    j0 = np.clip(_read_accessor(out_doc, out_bin, attrs["JOINTS_0"]).astype(np.int64), 0, n_joints - 1)
                    w0 = _read_accessor(out_doc, out_bin, attrs["WEIGHTS_0"]).astype(np.float64)

                    has_set1 = ("JOINTS_1" in attrs) and ("WEIGHTS_1" in attrs)
                    if has_set1:
                        j1 = np.clip(_read_accessor(out_doc, out_bin, attrs["JOINTS_1"]).astype(np.int64), 0, n_joints - 1)
                        w1 = _read_accessor(out_doc, out_bin, attrs["WEIGHTS_1"]).astype(np.float64)
                        w_sum = w0.sum(axis=1) + w1.sum(axis=1)
                    else:
                        j1 = w1 = None
                        w_sum = w0.sum(axis=1)

                    # Normalize non-zero weight rows if slightly off 1.0
                    renorm = (w_sum > 1e-6) & (np.abs(w_sum - 1.0) > 1e-4)
                    if np.any(renorm):
                        inv_s = 1.0 / w_sum[renorm, None]
                        w0[renorm] *= inv_s
                        if has_set1 and w1 is not None:
                            w1[renorm] *= inv_s

                    # Linear blend skinning matrix per vertex: B[v] = sum_k w_k * M_yup[j_k]
                    B = np.zeros((len(pos), 4, 4), dtype=np.float64)
                    for k in range(4):
                        B += w0[:, k, None, None] * skin_mats[j0[:, k]]
                    if has_set1 and j1 is not None and w1 is not None:
                        for k in range(4):
                            B += w1[:, k, None, None] * skin_mats[j1[:, k]]

                    # Fallback for any zero-weight vertices: apply uniform height scale
                    zero_w = w_sum <= 1e-6
                    if np.any(zero_w):
                        B[zero_w] = np.diag([hs, hs, hs, 1.0])

                    # Bake POSITION
                    pos_new = np.einsum("vij,vj->vi", B[:, :3, :3], pos) + B[:, :3, 3]
                    attrs["POSITION"] = _append_accessor(
                        out_doc,
                        out_bin,
                        pos_new.astype("<f4"),
                        "VEC3",
                        component_type=5126,
                        target=34962,
                        with_minmax=True,
                    )

                    # Bake NORMAL via exact 3x3 cofactor matrix (handles non-uniform BM_* scale accurately)
                    n_new_unit = None
                    if "NORMAL" in attrs:
                        nrm = _read_accessor(out_doc, out_bin, attrs["NORMAL"]).astype(np.float64)
                        cof = _cofactor_3x3_batch(B[:, :3, :3])
                        n_raw = np.einsum("vij,vj->vi", cof, nrm)
                        n_len = np.linalg.norm(n_raw, axis=1, keepdims=True)
                        bad = n_len[:, 0] < 1e-8
                        if np.any(bad):
                            n_raw[bad] = nrm[bad]
                            n_len = np.linalg.norm(n_raw, axis=1, keepdims=True)
                            n_len[n_len < 1e-8] = 1.0
                        n_new_unit = n_raw / n_len
                        attrs["NORMAL"] = _append_accessor(
                            out_doc,
                            out_bin,
                            n_new_unit.astype("<f4"),
                            "VEC3",
                            component_type=5126,
                            target=34962,
                            with_minmax=False,
                        )

                    # Bake TANGENT (VEC4: xyz transformed & Gram-Schmidt re-orthogonalized, w handedness kept)
                    if "TANGENT" in attrs:
                        tan = _read_accessor(out_doc, out_bin, attrs["TANGENT"]).astype(np.float64)
                        t_xyz = np.einsum("vij,vj->vi", B[:, :3, :3], tan[:, :3])
                        if n_new_unit is not None:
                            t_xyz = t_xyz - np.sum(t_xyz * n_new_unit, axis=1, keepdims=True) * n_new_unit
                        t_len = np.linalg.norm(t_xyz, axis=1, keepdims=True)
                        bad_t = t_len[:, 0] < 1e-8
                        if np.any(bad_t):
                            t_xyz[bad_t] = tan[bad_t, :3]
                            t_len = np.linalg.norm(t_xyz, axis=1, keepdims=True)
                            t_len[t_len < 1e-8] = 1.0
                        t_new = np.empty_like(tan)
                        t_new[:, :3] = t_xyz / t_len
                        t_new[:, 3] = tan[:, 3]
                        attrs["TANGENT"] = _append_accessor(
                            out_doc,
                            out_bin,
                            t_new.astype("<f4"),
                            "VEC4",
                            component_type=5126,
                            target=34962,
                            with_minmax=False,
                        )

                    skinned_prims += 1

            # Update skin 0 joint nodes & recompute inverseBindMatrices directly from the written node TRS
            skin = skins[0]
            joint_node_indices = list(skin.get("joints") or [])
            if len(joint_node_indices) == n_joints:
                r_trans = eval_res["rest_local_trans_yup"]
                r_rot = eval_res["rest_local_rot_yup"]
                order = _topo_order(joints)

                for ji, nidx in enumerate(joint_node_indices):
                    jnode = nodes[nidx]
                    tv = [round(float(x), 7) for x in r_trans[ji]]
                    if any(abs(v) > 1e-7 for v in tv):
                        jnode["translation"] = tv
                    else:
                        jnode.pop("translation", None)

                    qv = _quat_normalize(r_rot[ji])
                    if any(abs(float(v) - d0) > 1e-7 for v, d0 in zip(qv, (0.0, 0.0, 0.0, 1.0))):
                        jnode["rotation"] = [round(float(v), 7) for v in qv]
                    else:
                        jnode.pop("rotation", None)

                    # Clean unit rest scale so Blender/Maya animations never shear or snap back
                    jnode.pop("scale", None)
                    joints_updated += 1

                # Reconstruct exact world matrices from the rounded glTF node transforms so
                # W_rest @ IBM == I holds to float32 precision (< 1e-6) after JSON round-trip!
                w_exact = np.empty((n_joints, 4, 4), dtype=np.float64)
                for ji in order:
                    jnode = nodes[joint_node_indices[ji]]
                    t_v = np.array(jnode.get("translation") or (0.0, 0.0, 0.0), dtype=np.float64)
                    q_v = np.array(jnode.get("rotation") or (0.0, 0.0, 0.0, 1.0), dtype=np.float64)
                    s_v = np.array(jnode.get("scale") or (1.0, 1.0, 1.0), dtype=np.float64)
                    loc_m = _trs_mat4(t_v, q_v, s_v)
                    par = int(joints[ji].get("parent", -1))
                    if 0 <= par < n_joints:
                        w_exact[ji] = w_exact[par] @ loc_m
                    else:
                        w_exact[ji] = loc_m

                ibm_cols = np.empty((n_joints, 4, 4), dtype="<f4")
                eye4 = np.eye(4, dtype=np.float64)
                for ji in range(n_joints):
                    ibm_m = np.linalg.inv(w_exact[ji])
                    ibm_cols[ji] = ibm_m.T.astype("<f4")
                    err = float(np.max(np.abs(w_exact[ji] @ ibm_cols[ji].astype(np.float64).T - eye4)))
                    if err > max_rest_ibm_err:
                        max_rest_ibm_err = err

                skin["inverseBindMatrices"] = _append_accessor(
                    out_doc,
                    out_bin,
                    ibm_cols.reshape(n_joints, 16),
                    "MAT4",
                    component_type=5126,
                    target=None,
                    with_minmax=False,
                )

                # Scale rig_root_node ground offset by height_scale so feet remain planted on y=0
                rig_root_idx = skin.get("skeleton")
                if has_hs and rig_root_idx is not None and 0 <= int(rig_root_idx) < len(nodes):
                    rnode = nodes[int(rig_root_idx)]
                    if "translation" in rnode:
                        rnode["translation"] = [round(float(v) * hs, 7) for v in rnode["translation"]]

                # Scale embedded animation translation tracks by height_scale so strides match taller/shorter rig
                if has_hs and out_doc.get("animations"):
                    scaled_nodes = set(joint_node_indices)
                    if rig_root_idx is not None:
                        scaled_nodes.add(int(rig_root_idx))
                    for anim in out_doc["animations"]:
                        samplers = anim.get("samplers") or []
                        for ch in anim.get("channels") or []:
                            tgt = ch.get("target") or {}
                            if tgt.get("path") == "translation" and tgt.get("node") in scaled_nodes:
                                samp = samplers[int(ch["sampler"])]
                                arr = _read_accessor(out_doc, out_bin, int(samp["output"])).astype(np.float64)
                                samp["output"] = _append_accessor(
                                    out_doc,
                                    out_bin,
                                    (arr * hs).astype("<f4"),
                                    "VEC3",
                                    component_type=5126,
                                    target=None,
                                    with_minmax=False,
                                )

    # 2. Handle unskinned meshes when height_scale != 1.0
    if has_hs and unskinned_mesh_indices:
        # Only bake meshes not already baked as skinned meshes
        pure_unskinned = unskinned_mesh_indices - skinned_mesh_indices
        for mi in sorted(pure_unskinned):
            mesh = meshes[mi]
            for prim in mesh.get("primitives") or []:
                attrs = prim.get("attributes") or {}
                if "POSITION" not in attrs:
                    continue
                pos = _read_accessor(out_doc, out_bin, attrs["POSITION"]).astype(np.float64)
                pos_new = pos * hs
                attrs["POSITION"] = _append_accessor(
                    out_doc,
                    out_bin,
                    pos_new.astype("<f4"),
                    "VEC3",
                    component_type=5126,
                    target=34962,
                    with_minmax=True,
                )
                unskinned_prims += 1
        for ni in unskinned_node_indices:
            node = nodes[ni]
            if "translation" in node:
                node["translation"] = [round(float(v) * hs, 7) for v in node["translation"]]

    # Keep buffers[0].byteLength in sync if already populated
    if out_doc.get("buffers"):
        pad = (-len(out_bin)) % 4
        if pad:
            out_bin.extend(b"\x00" * pad)
        out_doc["buffers"][0]["byteLength"] = len(out_bin)

    return {
        "baked": (skinned_prims > 0 or unskinned_prims > 0),
        "skinned_prims": skinned_prims,
        "unskinned_prims": unskinned_prims,
        "joints_updated": joints_updated,
        "max_rest_ibm_err": max_rest_ibm_err,
        "height_scale": hs,
    }
