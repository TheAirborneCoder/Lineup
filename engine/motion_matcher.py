# SPDX-License-Identifier: GPL-3.0-or-later
"""Standalone parser for GTA VI (RAGE 9) `.motionmatcher` clips inside `crClipDictionary` containers.

Supports parsing from:
  1. Joined decompressed `crClipDictionary` bytes (`parse_dictionary_motionmatchers` / `parse_motionmatcher`)
  2. Direct extraction from `anim.rpf` via `INDEX/anim.rpf/deflate-spans.bin` (`load_from_anim_rpf`)

Resolves candidate clip references against the animation index (LIBRARY/anim_clips.sqlite3) using:
  - Direct `+0x90` `(dictHash, clipHash)` external references (36/180 databases)
  - Direct `+0xA0` SwissTable `(dictHash, clipHash)` entries
  - Derived `+0xA0` SwissTable `joaat32("clipset@" + dict_name)` -> `dict` mappings

CLI usage:
  python motion_matcher_parser.py --test-all [--dump-dir <dir>] [--catalog <json>]
  python motion_matcher_parser.py --bin <dict.bin> [--clip <substring>] [--include-poses] [--out <file.json>]
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import struct
import zlib
from pathlib import Path
from typing import Any

from paths import GAME, INDEX, LIBRARY

SYSTEM_BASE = 0x50000000
DEFAULT_ANIM_DB = LIBRARY / "anim_clips.sqlite3"
DEFAULT_ANIM_RPF = GAME / "anim.rpf"
DEFAULT_SPANS_BIN = INDEX / "anim.rpf" / "deflate-spans.bin"

CHANNEL_FLAG_POSITION = 0x1
CHANNEL_FLAG_VELOCITY = 0x2
CHANNEL_FLAG_ORIENTATION_6D = 0x8

MODE_POSE_MATCHER_AOS = 1
MODE_MOTION_MATCHER_SOA_AABB = 3


def joaat32(text: str) -> int:
    """Standard RAGE lowercase Jenkins one-at-a-time 32-bit hash."""
    h = 0
    for b in text.lower().encode("ascii", errors="ignore"):
        h = (h + b) & 0xFFFFFFFF
        h = (h + ((h << 10) & 0xFFFFFFFF)) & 0xFFFFFFFF
        h ^= h >> 6
    h = (h + ((h << 3) & 0xFFFFFFFF)) & 0xFFFFFFFF
    h ^= h >> 11
    h = (h + ((h << 15) & 0xFFFFFFFF)) & 0xFFFFFFFF
    return h


class _Reader:
    """Minimal RAGE 9 System-segment (`0x5xxxxxxx`) virtual-pointer binary reader."""

    __slots__ = ("d", "n")

    def __init__(self, data: bytes):
        self.d = data
        self.n = len(data)

    def ptr(self, off: int) -> int | None:
        if off < 0 or off + 8 > self.n:
            return None
        v = struct.unpack_from("<Q", self.d, off)[0]
        if v == 0:
            return None
        if (v >> 28) != 5:
            return None
        p = v & 0x0FFFFFFF
        return p if p < self.n else None

    def u8(self, off: int) -> int:
        return self.d[off]

    def u16(self, off: int) -> int:
        return struct.unpack_from("<H", self.d, off)[0]

    def i16(self, off: int) -> int:
        return struct.unpack_from("<h", self.d, off)[0]

    def u32(self, off: int) -> int:
        return struct.unpack_from("<I", self.d, off)[0]

    def u64(self, off: int) -> int:
        return struct.unpack_from("<Q", self.d, off)[0]

    def f32(self, off: int) -> float:
        return struct.unpack_from("<f", self.d, off)[0]

    def arr32(self, off: int) -> tuple[int, int, int | None]:
        """16-byte RAGE 9 motionmatcher array descriptor `{u32 count, u32 capacity, T* ptr}`."""
        cnt, cap = struct.unpack_from("<II", self.d, off)
        return cnt, cap, self.ptr(off + 8)

    def cstr(self, off: int | None) -> str:
        if off is None or off < 0 or off >= self.n:
            return ""
        end = self.d.find(b"\x00", off)
        if end == -1:
            end = self.n
        return self.d[off:end].decode("ascii", errors="replace")

    def at_string(self, off: int) -> str:
        """Read RAGE `atString` (`{char* ptr, u16 len, u16 cap}`) at `off`."""
        p = self.ptr(off)
        if p is None:
            return ""
        ln = self.u16(off + 8)
        if ln and p + ln <= self.n:
            raw = self.d[p : p + ln]
            z = raw.find(b"\x00")
            if z != -1:
                raw = raw[:z]
            return raw.decode("ascii", errors="replace")
        return self.cstr(p)


class ClipResolver:
    """Resolves `(dictHash, clipHash)` and `(clipSetHash, clipHash)` against `anim_clips.sqlite3`."""

    def __init__(self, db_path: Path | str | None = DEFAULT_ANIM_DB):
        self.dicts_by_hash: dict[int, list[tuple[int, str, int]]] = {}
        self.dicts_by_clipset_hash: dict[int, list[tuple[int, str, int]]] = {}
        self.clip_by_pair: dict[tuple[int, int], tuple[str, str, float, int]] = {}
        self.clip_short_names: dict[int, str] = {}

        if db_path is None:
            return
        p = Path(db_path)
        if not p.exists():
            return

        con = sqlite3.connect(str(p))
        try:
            for did, dname, dhash in con.execute("SELECT id, name, hash FROM dict ORDER BY clip_count DESC, id ASC"):
                dh = int(dhash) & 0xFFFFFFFF
                rec = (did, dname, dh)
                self.dicts_by_hash.setdefault(dh, []).append(rec)
                self.dicts_by_clipset_hash.setdefault(joaat32("clipset@" + dname), []).append(rec)
            for did, cname, sname, chash, dur, frames in con.execute(
                "SELECT dict_id, name, short_name, hash, duration, frames FROM clip"
            ):
                ch = int(chash) & 0xFFFFFFFF
                self.clip_by_pair[(did, ch)] = (cname, sname, float(dur), int(frames))
                if ch not in self.clip_short_names:
                    self.clip_short_names[ch] = sname or cname
        finally:
            con.close()

    def resolve(
        self,
        clip_hash: int,
        clipset_hash: int,
        ext_dict_hash: int | None = None,
    ) -> dict[str, Any]:
        candidates: list[tuple[str, list[tuple[int, str, int]]]] = []
        if ext_dict_hash is not None and ext_dict_hash in self.dicts_by_hash:
            candidates.append(("ext_ref_0x90", self.dicts_by_hash[ext_dict_hash]))
        if clipset_hash in self.dicts_by_hash:
            candidates.append(("swisstable_direct_dict", self.dicts_by_hash[clipset_hash]))
        if clipset_hash in self.dicts_by_clipset_hash:
            candidates.append(("swisstable_clipset_prefix", self.dicts_by_clipset_hash[clipset_hash]))

        did: int | None = None
        dict_name: str | None = None
        dict_hash: int | None = ext_dict_hash
        method = "unresolved"
        clip_name: str | None = None
        clip_short: str | None = None
        duration: float | None = None
        frames: int | None = None
        resolved = False

        # First pass: find a (dict_id, clip_hash) pair that actually has the playable clip
        for m_name, recs in candidates:
            for c_did, c_dname, c_dh in recs:
                if (c_did, clip_hash) in self.clip_by_pair:
                    did, dict_name, dict_hash = c_did, c_dname, c_dh
                    clip_name, clip_short, duration, frames = self.clip_by_pair[(c_did, clip_hash)]
                    method = m_name
                    resolved = True
                    break
            if resolved:
                break

        if not resolved:
            if candidates:
                method, recs = candidates[0]
                did, dict_name, dict_hash = recs[0]
            if clip_hash in self.clip_short_names:
                clip_short = self.clip_short_names[clip_hash]

        return {
            "dictId": did,
            "dictHash": f"0x{dict_hash:08x}" if dict_hash is not None else None,
            "dictName": dict_name,
            "clipName": clip_name,
            "clipShortName": clip_short,
            "clipDuration": round(duration, 4) if duration is not None else None,
            "clipFrames": frames,
            "resolved": resolved,
            "resolutionMethod": method if resolved else (f"{method}_clip_missing" if did is not None else "unresolved"),
        }


def _classify_rig_family(dict_name: str, bones: list[dict[str, Any]]) -> dict[str, Any]:
    bone_names = [b["name"] for b in bones]
    bc = len(bones)
    top = (dict_name or "").split("@")[0]

    if top == "gameplay_creatures":
        parts = dict_name.split("@")
        domain = parts[1] if len(parts) > 1 else "unknown"
        clade = parts[2] if len(parts) > 2 else "unknown"
        species = "@".join(parts[3:]) if len(parts) > 3 else domain
        return {
            "category": "animal",
            "domain": domain,
            "clade": f"{domain}/{clade}",
            "species": species,
            "boneCount": bc,
            "hasSpineRoot": "SKEL_Spine_Root" in bone_names,
            "anatomyTags": _anatomy_tags(bone_names),
        }

    if top == "interaction_ped":
        parts = dict_name.split("@")
        target = parts[1] if len(parts) > 1 else dict_name
        if "human" in target.lower():
            return {
                "category": "human",
                "domain": "interaction_ped",
                "clade": "human/biped",
                "species": "human",
                "boneCount": bc,
                "hasSpineRoot": "SKEL_Spine_Root" in bone_names,
                "anatomyTags": _anatomy_tags(bone_names),
            }
        return {
            "category": "animal",
            "domain": "interaction_ped",
            "clade": "animal/pickup_dead",
            "species": target,
            "boneCount": bc,
            "hasSpineRoot": "SKEL_Spine_Root" in bone_names,
            "anatomyTags": _anatomy_tags(bone_names),
        }

    species = "human"
    if "lucia" in (dict_name or "").lower():
        species = "human_lucia"
    elif "jason" in (dict_name or "").lower():
        species = "human_jason"
    elif "female" in (dict_name or "").lower() or top == "mech_loco_f":
        species = "human_female"
    elif "male" in (dict_name or "").lower() or top == "mech_loco_m":
        species = "human_male"

    return {
        "category": "human",
        "domain": top or "human",
        "clade": "human/biped",
        "species": species,
        "boneCount": bc,
        "hasSpineRoot": "SKEL_Spine_Root" in bone_names,
        "anatomyTags": _anatomy_tags(bone_names),
    }


def _anatomy_tags(bone_names: list[str]) -> list[str]:
    tags = []
    if any("Wing" in b for b in bone_names):
        tags.append("wings")
    if any("Tail" in b for b in bone_names):
        tags.append("tail")
    if any("Fin" in b or "Flipper" in b or "Fluke" in b for b in bone_names):
        tags.append("fins")
    if any("LegA" in b for b in bone_names):
        tags.append("arthropod_legs")
    if any("Clavicle" in b or "Forearm" in b or "Hand" in b for b in bone_names):
        tags.append("upper_body")
    if any("Thigh" in b or "Foot" in b for b in bone_names):
        tags.append("lower_body")
    if any("PH_" in b for b in bone_names):
        tags.append("prop_helpers")
    return tags


def _parse_embedded_skeleton(r: _Reader, sk_off: int | None) -> tuple[list[dict[str, Any]], dict[int, str], bool]:
    if sk_off is None:
        return [], {}, False
    bone_count = r.u16(sk_off + 0x6C)
    rec_off = r.ptr(sk_off + 0x28)
    par_off = r.ptr(sk_off + 0x40)
    trie_off = r.ptr(sk_off + 0x48)
    bones: list[dict[str, Any]] = []
    bone_map: dict[int, str] = {}
    if rec_off is None:
        return bones, bone_map, trie_off is not None

    for i in range(bone_count):
        bo = rec_off + i * 0x50
        rot = struct.unpack_from("<4f", r.d, bo + 0x00)
        trans = struct.unpack_from("<3f", r.d, bo + 0x10)
        bhash = r.u32(bo + 0x1C)
        scale = struct.unpack_from("<3f", r.d, bo + 0x20)
        bname = r.cstr(r.ptr(bo + 0x30))
        parent = r.i16(par_off + i * 2) if par_off is not None else -1
        bone_map[bhash] = bname
        bones.append({
            "index": i,
            "name": bname,
            "hash": f"0x{bhash:08x}",
            "parent": parent,
            "defaultRot": [round(x, 6) for x in rot],
            "defaultTrans": [round(x, 6) for x in trans],
            "defaultScale": [round(x, 6) for x in scale],
        })
    return bones, bone_map, trie_off is not None


def _parse_alignment_space(r: _Reader, p30: int | None, bone_map: dict[int, str]) -> dict[str, Any] | None:
    if p30 is None:
        return None
    atype = r.u32(p30 + 0x08)
    if atype == 1:
        quat = struct.unpack_from("<4f", r.d, p30 + 0x10)
        trans = struct.unpack_from("<3f", r.d, p30 + 0x20)
        scale = struct.unpack_from("<3f", r.d, p30 + 0x30)
        bcnt, _, bptr = r.arr32(p30 + 0x40)
        wcnt, _, wptr = r.arr32(p30 + 0x50)
        dof_flags = r.u32(p30 + 0x60)
        bones_list = []
        if bptr is not None:
            for i in range(bcnt):
                bh = r.u32(bptr + i * 4)
                bones_list.append({"hash": f"0x{bh:08x}", "name": bone_map.get(bh)})
        weights = [round(r.f32(wptr + i * 4), 6) for i in range(wcnt)] if wptr is not None else []
        return {
            "type": 1,
            "typeName": "weighted_multi_bone",
            "referenceRot": [round(x, 6) for x in quat],
            "referenceTrans": [round(x, 6) for x in trans],
            "referenceScale": [round(x, 6) for x in scale],
            "bones": bones_list,
            "weights": weights,
            "dofFlags": dof_flags,
        }
    if atype == 2:
        bh_a = r.u32(p30 + 0x10)
        bh_b = r.u32(p30 + 0x14)
        wa = r.f32(p30 + 0x18)
        wb = r.f32(p30 + 0x1C)
        return {
            "type": 2,
            "typeName": "two_bone_midpoint",
            "boneA": {"hash": f"0x{bh_a:08x}", "name": bone_map.get(bh_a)},
            "boneB": {"hash": f"0x{bh_b:08x}", "name": bone_map.get(bh_b)},
            "weightA": round(wa, 6),
            "weightB": round(wb, 6),
        }
    return {"type": atype, "typeName": f"unknown_{atype}"}


def _decode_channel_subfeatures(flags: int) -> list[dict[str, Any]]:
    """Decode Type 1 bone channel bitmask in verified feature-vector order: Position -> Orientation6D -> Velocity."""
    parts = []
    rel = 0
    if flags & CHANNEL_FLAG_POSITION:
        parts.append({"kind": "position", "offset": rel, "dim": 3})
        rel += 3
    if flags & CHANNEL_FLAG_ORIENTATION_6D:
        parts.append({"kind": "orientation_6d", "offset": rel, "dim": 6})
        rel += 6
    if flags & CHANNEL_FLAG_VELOCITY:
        parts.append({"kind": "velocity", "offset": rel, "dim": 3})
        rel += 3
    return parts


def _parse_swisstable_clips(r: _Reader, off: int) -> dict[int, tuple[int, int]]:
    """Parse `+0xA0` SwissTable (`0x110`-byte groups: 16B H2 ctrl + 16*16B `{clip_hash, clipset_hash, idx, pad}`)."""
    num_groups, count, ptr = r.arr32(off)
    out: dict[int, tuple[int, int]] = {}
    if ptr is None:
        return out
    for g in range(num_groups):
        goff = ptr + g * 0x110
        ctrl = r.d[goff : goff + 16]
        for slot in range(16):
            if ctrl[slot] < 0x80:
                h0, h1, idx, _ = struct.unpack_from("<4I", r.d, goff + 16 + slot * 16)
                out[idx] = (h0, h1)
    return out


def _parse_swisstable_channels(r: _Reader, off: int) -> dict[int, int]:
    """Parse `p20+0x48` SwissTable (`0x90`-byte groups: 16B H2 ctrl + 16*8B `{chan_hash, chan_idx}`)."""
    num_groups, count, ptr = r.arr32(off)
    out: dict[int, int] = {}
    if ptr is None:
        return out
    for g in range(num_groups):
        goff = ptr + g * 0x90
        ctrl = r.d[goff : goff + 16]
        for slot in range(16):
            if ctrl[slot] < 0x80:
                ch_hash, ch_idx = struct.unpack_from("<II", r.d, goff + 16 + slot * 8)
                out[ch_idx] = ch_hash
    return out


def _decode_feature_matrix(
    r: _Reader,
    p20: int,
    mode: int,
    n_poses: int,
    feature_dim: int,
    perm: list[int] | None,
) -> list[list[float]]:
    """Decode all `n_poses x feature_dim` fp16 feature vectors in canonical (unpermuted) dimension order."""
    ce8, _, pe8 = r.arr32(p20 + 0xE8)
    if pe8 is None or n_poses == 0 or feature_dim == 0:
        return []

    if mode == MODE_POSE_MATCHER_AOS:
        bpp = r.u32(p20 + 0x100)
        stride_f16 = bpp * 16
        mat = []
        for p in range(n_poses):
            vals = struct.unpack_from(f"<{stride_f16}e", r.d, pe8 + p * bpp * 32)
            mat.append([float(vals[d]) for d in range(feature_dim)])
        return mat

    if mode == MODE_MOTION_MATCHER_SOA_AABB:
        half_dim = r.u16(p20 + 0x12C)
        n_pg = math.ceil(n_poses / 8)
        assert perm is not None and len(perm) == feature_dim
        mat = [[0.0] * feature_dim for _ in range(n_poses)]
        for pg in range(n_pg):
            for dp in range(half_dim):
                blk = struct.unpack_from("<16e", r.d, pe8 + (pg * half_dim + dp) * 32)
                d0 = perm[dp * 2]
                d1 = perm[dp * 2 + 1] if (dp * 2 + 1) < feature_dim else -1
                for lane in range(8):
                    p = pg * 8 + lane
                    if p < n_poses:
                        mat[p][d0] = float(blk[lane])
                        if d1 >= 0:
                            mat[p][d1] = float(blk[8 + lane])
        return mat

    raise ValueError(f"Unsupported motionmatcher search mode: {mode}")


def _format_guid(raw16: bytes) -> str:
    d1, d2, d3 = struct.unpack_from("<IHH", raw16, 0)
    d4 = raw16[8:10].hex()
    d5 = raw16[10:16].hex()
    return f"{d1:08x}-{d2:04x}-{d3:04x}-{d4}-{d5}"


def parse_motionmatcher(
    raw_dict_bytes: bytes,
    clip_offset: int,
    dict_name: str = "",
    dict_hash: int | None = None,
    clip_hash: int | None = None,
    resolver: ClipResolver | None = None,
    include_poses: bool = False,
) -> dict[str, Any]:
    """Parse a single `.motionmatcher` object at `clip_offset` inside joined `crClipDictionary` bytes."""
    r = _Reader(raw_dict_bytes)
    co = clip_offset

    name = r.at_string(co + 0x38)
    short_name = name
    if short_name.startswith("pack:/"):
        short_name = short_name[6:]
    if short_name.endswith(".motionmatcher"):
        short_name = short_name[:-14]

    if clip_hash is None:
        clip_hash = joaat32(short_name)
    if dict_hash is None and dict_name:
        dict_hash = joaat32(dict_name)

    guid = _format_guid(r.d[co + 0x28 : co + 0x38])
    p20 = r.ptr(co + 0x20)
    if p20 is None:
        raise ValueError(f"Null database pointer at +0x20 for clip {name!r} @ 0x{co:x}")

    # Top-level candidate arrays
    c50, _, p50 = r.arr32(co + 0x50)  # N_intervals x 0x18B descriptors
    c60, _, p60 = r.arr32(co + 0x60)  # (N_intervals + 1) x u32 pose CSR offsets
    c70, _, p70 = r.arr32(co + 0x70)  # (N_candidates + 1) x u16 interval CSR offsets
    c90, _, p90 = r.arr32(co + 0x90)  # optional N_candidates x 16B external clip refs
    cb0, _, pb0 = r.arr32(co + 0xB0)  # N_candidates x u8 is_looping flags
    a0_map = _parse_swisstable_clips(r, co + 0xA0)

    pose_csr = [r.u32(p60 + i * 4) for i in range(c60)] if p60 is not None else []
    interval_csr = [r.u16(p70 + i * 2) for i in range(c70)] if p70 is not None else []
    loop_flags = list(r.d[pb0 : pb0 + cb0]) if pb0 is not None else [0] * cb0

    intervals = []
    if p50 is not None:
        for i in range(c50):
            start_t, fps, end_t, cidx, u10, u14 = struct.unpack_from("<fffIII", r.d, p50 + i * 0x18)
            ps = pose_csr[i] if i < len(pose_csr) else 0
            pe = pose_csr[i + 1] if (i + 1) < len(pose_csr) else ps
            intervals.append({
                "intervalIndex": i,
                "candidateIndex": cidx,
                "startTime": round(start_t, 6),
                "sampleRateHz": round(fps, 4),
                "endTime": round(end_t, 6),
                "poseStart": ps,
                "poseEnd": pe,
                "poseCount": pe - ps,
            })

    # Sub-object p20: skeleton, alignment space, channels, weights, tags, search index
    bones, bone_map, has_trie = _parse_embedded_skeleton(r, r.ptr(p20 + 0x28))
    alignment_space = _parse_alignment_space(r, r.ptr(p20 + 0x30), bone_map)
    chan_hash_map = _parse_swisstable_channels(r, p20 + 0x48)

    mode = r.u32(p20 + 0xC8)
    if mode == MODE_POSE_MATCHER_AOS:
        mode_name = "pose_matcher_aos"
        n_poses = r.u32(p20 + 0xF8)
        feature_dim = r.u32(p20 + 0xFC)
    elif mode == MODE_MOTION_MATCHER_SOA_AABB:
        mode_name = "motion_matcher_soa_aabb"
        n_poses = r.u32(p20 + 0x128)
        feature_dim = r.u16(p20 + 0x12E)
    else:
        raise ValueError(f"Unknown motionmatcher mode {mode} in {name!r}")

    # Weights
    cb0_w, _, pb0_w = r.arr32(p20 + 0xB0)
    total_weight = r.f32(p20 + 0xC0)
    weight_scale = r.f32(p20 + 0xC4)
    norm_weights = (
        [float(x) for x in struct.unpack_from(f"<{cb0_w * 8}f", r.d, pb0_w)][:feature_dim]
        if pb0_w is not None
        else []
    )
    raw_weights = [round(w * total_weight, 5) for w in norm_weights]

    # Feature channels
    cc, _, cp = r.arr32(p20 + 0x38)
    channels = []
    trajectory_channels = []
    if cp is not None:
        for i in range(cc):
            chp = r.ptr(cp + i * 8)
            if chp is None:
                continue
            foff = r.u32(chp + 0x08)
            dim = r.u16(chp + 0x0C)
            ctype = r.u16(chp + 0x0E)
            ch_w_raw = raw_weights[foff : foff + dim]
            ch_w_norm = [round(x, 6) for x in norm_weights[foff : foff + dim]]
            ch_hash = chan_hash_map.get(i)
            if ctype == 1:
                bh = r.u32(chp + 0x18)
                flags = r.u32(chp + 0x1C)
                entry = {
                    "index": i,
                    "type": 1,
                    "typeName": "bone_pose",
                    "channelHash": f"0x{ch_hash:08x}" if ch_hash is not None else None,
                    "featureOffset": foff,
                    "dim": dim,
                    "boneHash": f"0x{bh:08x}",
                    "boneName": bone_map.get(bh),
                    "flags": flags,
                    "subFeatures": _decode_channel_subfeatures(flags),
                    "rawWeights": ch_w_raw,
                    "normalizedWeights": ch_w_norm,
                }
                channels.append(entry)
            elif ctype == 5:
                h1 = r.u32(chp + 0x18)
                h1b = r.u32(chp + 0x1C)
                h2 = r.u32(chp + 0x24)
                dt = r.f32(chp + 0x38)
                u3c = r.u32(chp + 0x3C)
                semantic = (
                    "target_stop_position" if u3c == 0x201
                    else "target_heading_direction" if u3c == 0x214
                    else "target_position_and_orientation_6d" if u3c == 0x24B
                    else f"trajectory_flags_0x{u3c:x}"
                )
                entry = {
                    "index": i,
                    "type": 5,
                    "typeName": "trajectory_target",
                    "channelHash": f"0x{ch_hash:08x}" if ch_hash is not None else None,
                    "featureOffset": foff,
                    "dim": dim,
                    "semantic": semantic,
                    "primaryHash": f"0x{h1:08x}" if h1 else None,
                    "secondaryHash": f"0x{h1b:08x}" if h1b else None,
                    "auxHash": f"0x{h2:08x}" if h2 else None,
                    "sampleDeltaSec": round(dt, 4),
                    "flags": f"0x{u3c:x}",
                    "rawWeights": ch_w_raw,
                    "normalizedWeights": ch_w_norm,
                }
                channels.append(entry)
                trajectory_channels.append(entry)
            else:
                channels.append({
                    "index": i,
                    "type": ctype,
                    "typeName": f"unknown_{ctype}",
                    "featureOffset": foff,
                    "dim": dim,
                    "rawWeights": ch_w_raw,
                })

    # Extra metadata & pose tags
    c68, _, p68 = r.arr32(p20 + 0x68)
    extra_hashes = [f"0x{r.u32(p68 + i * 4):08x}" for i in range(c68)] if p68 is not None else []
    schema_hash = r.u32(p20 + 0x7C)
    simd_stride = r.u16(p20 + 0x80)
    raw_float_bytes = r.u16(p20 + 0x82)

    c90p, _, p90p = r.arr32(p20 + 0x90)
    pose_tags = [r.u32(p90p + i * 4) for i in range(c90p)] if p90p is not None else []

    # Search index metadata
    ce8, _, pe8 = r.arr32(p20 + 0xE8)
    perm: list[int] | None = None
    if mode == MODE_POSE_MATCHER_AOS:
        bpp = r.u32(p20 + 0x100)
        search_index: dict[str, Any] = {
            "mode": mode,
            "layout": "aos_fp16_simd",
            "blocksPerPose": bpp,
            "blockCount": ce8,
            "matrixBytes": ce8 * 32,
        }
    else:
        half_dim = r.u16(p20 + 0x12C)
        cf8, _, pf8 = r.arr32(p20 + 0xF8)
        c108, _, p108 = r.arr32(p20 + 0x108)
        c118, _, p118 = r.arr32(p20 + 0x118)
        perm = [r.u32(pf8 + i * 4) for i in range(cf8)] if pf8 is not None else []
        cluster_sizes = [r.u32(p118 + i * 4) for i in range(c118)] if p118 is not None else []
        aabb_levels = []
        if p108 is not None:
            for lvl in range(c108):
                min_c, _, _ = r.arr32(p108 + lvl * 32)
                max_c, _, _ = r.arr32(p108 + lvl * 32 + 16)
                aabb_levels.append({
                    "level": lvl,
                    "minBlocks": min_c,
                    "maxBlocks": max_c,
                    "simdBoxes8": min_c // half_dim if half_dim else 0,
                    "bytes": (min_c + max_c) * 32,
                })
        search_index = {
            "mode": mode,
            "layout": "soa_fp16_simd_aabb",
            "halfDim": half_dim,
            "poseGroups8": math.ceil(n_poses / 8),
            "blockCount": ce8,
            "matrixBytes": ce8 * 32,
            "dimPermutation": perm,
            "clusterSizes": cluster_sizes,
            "aabbLevels": aabb_levels,
        }

    # Build candidate clip records
    candidates = []
    resolved_count = 0
    for idx in range(cb0):
        ch_a0, cs_a0 = a0_map.get(idx, (0, 0))
        ext_dh: int | None = None
        if p90 is not None and idx < c90:
            _, ch90, dh90 = struct.unpack_from("<QII", r.d, p90 + idx * 16)
            ext_dh = dh90
            if ch_a0 == 0:
                ch_a0 = ch90

        iv_start = interval_csr[idx] if idx < len(interval_csr) else idx
        iv_end = interval_csr[idx + 1] if (idx + 1) < len(interval_csr) else iv_start + 1
        cand_ivs = intervals[iv_start:iv_end]
        p_start = cand_ivs[0]["poseStart"] if cand_ivs else 0
        p_end = cand_ivs[-1]["poseEnd"] if cand_ivs else 0
        cand_tags = sorted(set(pose_tags[p_start:p_end])) if pose_tags else []

        res = (
            resolver.resolve(ch_a0, cs_a0, ext_dh)
            if resolver is not None
            else {
                "dictId": None,
                "dictHash": f"0x{ext_dh:08x}" if ext_dh is not None else None,
                "dictName": None,
                "clipName": None,
                "clipShortName": None,
                "clipDuration": None,
                "clipFrames": None,
                "resolved": False,
                "resolutionMethod": "no_resolver",
            }
        )
        if res["resolved"]:
            resolved_count += 1

        candidates.append({
            "index": idx,
            "clipHash": f"0x{ch_a0:08x}",
            "clipSetHash": f"0x{cs_a0:08x}",
            **res,
            "isLooping": bool(loop_flags[idx]),
            "startTime": cand_ivs[0]["startTime"] if cand_ivs else 0.0,
            "endTime": cand_ivs[-1]["endTime"] if cand_ivs else -1.0,
            "sampleRateHz": cand_ivs[0]["sampleRateHz"] if cand_ivs else 0.0,
            "poseStart": p_start,
            "poseEnd": p_end,
            "poseCount": p_end - p_start,
            "poseTagMasks": [f"0x{t:08x}" for t in cand_tags],
            "intervals": cand_ivs,
        })

    # Decode feature matrix (for per-channel stats and optional full pose export)
    mat = _decode_feature_matrix(r, p20, mode, n_poses, feature_dim, perm)
    if mat:
        for ch in channels:
            foff, dim = ch["featureOffset"], ch["dim"]
            mins = [round(min(row[foff + k] for row in mat), 4) for k in range(dim)]
            maxs = [round(max(row[foff + k] for row in mat), 4) for k in range(dim)]
            ch["valueRange"] = {"min": mins, "max": maxs}

    # Mode 1 per-pose alignment transforms (p20 + 0xA0)
    ca0_tr, _, pa0_tr = r.arr32(p20 + 0xA0)
    poses_out: list[dict[str, Any]] | None = None
    if include_poses:
        poses_out = []
        for p in range(n_poses):
            item: dict[str, Any] = {
                "poseIndex": p,
                "tagMask": f"0x{pose_tags[p]:08x}" if p < len(pose_tags) else "0x00000000",
                "features": [round(x, 5) for x in mat[p]] if p < len(mat) else [],
            }
            if pa0_tr is not None and p < ca0_tr:
                q = struct.unpack_from("<4f", r.d, pa0_tr + p * 0x30)
                t = struct.unpack_from("<3f", r.d, pa0_tr + p * 0x30 + 0x10)
                sc = struct.unpack_from("<3f", r.d, pa0_tr + p * 0x30 + 0x20)
                item["alignmentTransform"] = {
                    "rot": [round(x, 5) for x in q],
                    "trans": [round(x, 5) for x in t],
                    "scale": [round(x, 5) for x in sc],
                }
            poses_out.append(item)

    rig_family = _classify_rig_family(dict_name, bones)

    result: dict[str, Any] = {
        "name": name,
        "shortName": short_name,
        "dictName": dict_name,
        "hash": f"0x{clip_hash:08x}",
        "dictHash": f"0x{dict_hash:08x}" if dict_hash is not None else None,
        "guid": guid,
        "mode": mode,
        "modeName": mode_name,
        "rigFamily": rig_family,
        "bones": bones,
        "hasFrontCodedBoneTrie": has_trie,
        "alignmentSpace": alignment_space,
        "features": {
            "schemaHash": f"0x{schema_hash:08x}",
            "featureDim": feature_dim,
            "simdByteStride": simd_stride,
            "rawFloatBytes": raw_float_bytes,
            "totalWeight": round(total_weight, 5),
            "weightScale": round(weight_scale, 5),
            "extraHashes": extra_hashes,
            "channels": channels,
            "trajectoryChannels": trajectory_channels,
        },
        "searchIndex": search_index,
        "candidates": candidates,
        "stats": {
            "poseCount": n_poses,
            "candidateCount": len(candidates),
            "intervalCount": len(intervals),
            "resolvedCandidateCount": resolved_count,
            "featureDim": feature_dim,
            "channelCount": len(channels),
            "boneCount": len(bones),
            "hasExternalRefs0x90": p90 is not None,
            "hasAlignmentTransforms": pa0_tr is not None,
            "dictionaryBytes": len(raw_dict_bytes),
            "featureMatrixBytes": search_index["matrixBytes"],
            "uniquePoseTagMasks": sorted(f"0x{t:08x}" for t in set(pose_tags)),
        },
    }
    if poses_out is not None:
        result["poses"] = poses_out
    return result


def parse_dictionary_motionmatchers(
    raw_dict_bytes: bytes,
    resolver: ClipResolver | None = None,
    include_poses: bool = False,
) -> list[dict[str, Any]]:
    """Find and parse all `.motionmatcher` clips inside a joined `crClipDictionary` buffer."""
    r = _Reader(raw_dict_bytes)
    dict_name = r.at_string(0x38)
    dict_hash = r.u32(0x68)

    # crClipDictionary atMap at +0x48: `{ptr buckets, u16 n_buckets, u16 n_entries}`
    buckets_ptr = r.ptr(0x48)
    n_buckets = r.u16(0x50)
    if buckets_ptr is None or n_buckets == 0:
        return []

    out = []
    seen = set()
    for b in range(n_buckets):
        node = r.ptr(buckets_ptr + b * 8)
        while node is not None and node not in seen:
            seen.add(node)
            chash = r.u32(node + 0x00)
            clip_off = r.ptr(node + 0x08)
            nxt = r.ptr(node + 0x10)
            if clip_off is not None:
                cname = r.at_string(clip_off + 0x38)
                if cname.endswith(".motionmatcher"):
                    out.append(
                        parse_motionmatcher(
                            raw_dict_bytes,
                            clip_off,
                            dict_name=dict_name,
                            dict_hash=dict_hash,
                            clip_hash=chash,
                            resolver=resolver,
                            include_poses=include_poses,
                        )
                    )
            node = nxt
    out.sort(key=lambda x: x["name"])
    return out


def load_dictionary_from_rpf(
    segments: list[tuple[int, int]],
    rpf_path: Path | str = DEFAULT_ANIM_RPF,
) -> bytes:
    """Decompress and join consecutive deflate spans from an RPF9 archive (`[(offset, stored_len), ...]`)."""
    chunks = []
    with open(rpf_path, "rb") as rf:
        for off, slen in segments:
            rf.seek(off)
            raw_c = rf.read(slen)
            try:
                chunks.append(zlib.decompress(raw_c, -15))
            except zlib.error:
                chunks.append(zlib.decompress(raw_c))
    return b"".join(chunks)


def main() -> None:
    ap = argparse.ArgumentParser(description="Parse .motionmatcher clips to structured JSON.")
    ap.add_argument("--bin", type=Path, help="Path to a joined crClipDictionary .bin dump")
    ap.add_argument("--clip", type=str, default="", help="Filter clip name substring")
    ap.add_argument("--include-poses", action="store_true", help="Include full decoded fp16 pose vectors")
    ap.add_argument("--out", type=Path, help="Write JSON output to file")
    ap.add_argument("--db", type=Path, default=DEFAULT_ANIM_DB, help="Path to anim_clips.sqlite3")
    ap.add_argument("--test-all", action="store_true", help="Parse and validate all 180 .motionmatcher dumps")
    ap.add_argument(
        "--dump-dir",
        type=Path,
        default=Path(__file__).resolve().parent / "mm_dump" / "anim",
        help="Directory of joined .motionmatcher .bin dumps (for --test-all)",
    )
    args = ap.parse_args()

    resolver = ClipResolver(args.db)

    if args.test_all:
        bins = sorted(args.dump_dir.glob("*.bin"))
        total_clips = 0
        total_poses = 0
        total_cands = 0
        total_resolved = 0
        human_clips = 0
        animal_clips = 0
        mode1 = 0
        mode3 = 0
        for bp in bins:
            raw = bp.read_bytes()
            items = parse_dictionary_motionmatchers(raw, resolver=resolver, include_poses=False)
            assert len(items) > 0, f"No .motionmatcher found in {bp.name}"
            for it in items:
                # Verify strict JSON serializability
                _ = json.dumps(it)
                total_clips += 1
                total_poses += it["stats"]["poseCount"]
                total_cands += it["stats"]["candidateCount"]
                total_resolved += it["stats"]["resolvedCandidateCount"]
                if it["rigFamily"]["category"] == "human":
                    human_clips += 1
                else:
                    animal_clips += 1
                if it["mode"] == MODE_POSE_MATCHER_AOS:
                    mode1 += 1
                else:
                    mode3 += 1
        print(
            f"PASS: Parsed {total_clips} .motionmatcher clips across {len(bins)} dictionaries "
            f"(Mode1={mode1}, Mode3={mode3}, Human={human_clips}, Animal={animal_clips}, "
            f"Poses={total_poses:,}, Candidates={total_cands:,}, Resolved={total_resolved:,}/{total_cands:,} "
            f"[{100.0 * total_resolved / max(1, total_cands):.1f}%])"
        )
        return

    if not args.bin:
        ap.error("Specify --bin <file.bin> or --test-all")

    items = parse_dictionary_motionmatchers(
        args.bin.read_bytes(),
        resolver=resolver,
        include_poses=args.include_poses,
    )
    if args.clip:
        items = [x for x in items if args.clip.lower() in x["name"].lower()]
    payload = items[0] if len(items) == 1 else items
    text = json.dumps(payload, indent=2)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
        print(f"Wrote {len(items)} item(s) to {args.out}")
    else:
        print(text[:4000])


if __name__ == "__main__":
    main()
