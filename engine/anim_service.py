# SPDX-License-Identifier: GPL-3.0-or-later
"""GTA VI (RAGE 9) multi-archive animation clip, blendspace (.motion), and motion matching
(.motionmatcher) indexer, search service, and keyframe decoder.

Supports:
  - Multi-archive indexing across anim.rpf, pack/story000/story000.rpf,
    pack/shared_minigame000/shared_minigame000.rpf, and pack/core_restofworld/core_restofworld.rpf
    using fast deflate-spans.bin lookup (with raw zlib scan fallback).
  - Exact per-skeleton bone-hash matching for all humans AND all 270+ animal species.
  - Channel types 0..5, 7, 8, and 10 (second-order zigzag delta2_float with 64-frame chunks
    and 8-lane bitstreams) + multi-animation layer merging.
  - Parametric 0D..5D blendspaces (.motion) with child clip resolution & time-warp curves.
  - Motion Matching databases (.motionmatcher, Mode 1 AoS & Mode 3 SoA+AABB) with candidate
    clip resolution across all indexed archives.
"""
from __future__ import annotations

import collections
import concurrent.futures
import json
import math
import mmap
import os
import re
import sqlite3
import struct
import threading
import time
import zlib
from contextlib import closing
from pathlib import Path

import numpy as np
from paths import GAME, INDEX, LIBRARY


ANIM_RPF = GAME / "anim.rpf"
ANIM_DB = LIBRARY / "anim_clips.sqlite3"
SPANS_ROOT = INDEX

ANIM_ARCHIVES = [
    "anim.rpf",
    "pack/story000/story000.rpf",
    "pack/shared_minigame000/shared_minigame000.rpf",
    "pack/core_restofworld/core_restofworld.rpf",
]

ARCHIVE_LABELS = {
    "anim.rpf": "anim.rpf",
    "pack/story000/story000.rpf": "story000.rpf",
    "pack/shared_minigame000/shared_minigame000.rpf": "shared_minigame000.rpf",
    "pack/core_restofworld/core_restofworld.rpf": "core_restofworld.rpf",
}

SYSTEM = 0x50000000
ZHDR = re.compile(rb"\x78[\x01\x5e\x9c\xda]")

TRACK_ROT = 0x98E1750C    # joaat32("bonerotation")
TRACK_TRANS = 0xF791A95F  # joaat32("bonetranslation")
TRACK_SCALE = 0x7AAA9F60  # joaat32("bonescale")


def joaat32(text: str) -> int:
    h = 0
    for c in text.lower().encode("latin-1", "replace"):
        h = (h + c) & 0xFFFFFFFF
        h = (h + (h << 10)) & 0xFFFFFFFF
        h ^= h >> 6
    h = (h + (h << 3)) & 0xFFFFFFFF
    h ^= h >> 11
    return (h + (h << 15)) & 0xFFFFFFFF


class DecodeError(ValueError):
    pass


class NotClipDictionary(DecodeError):
    pass


class System:
    """Joined system pages of one clip dictionary resource."""

    def __init__(self, data: bytes):
        self.d = data

    def u16(self, at: int) -> int:
        return struct.unpack_from("<H", self.d, at)[0]

    def u32(self, at: int) -> int:
        return struct.unpack_from("<I", self.d, at)[0]

    def u64(self, at: int) -> int:
        return struct.unpack_from("<Q", self.d, at)[0]

    def f32(self, at: int) -> float:
        return struct.unpack_from("<f", self.d, at)[0]

    def ptr(self, at: int) -> int | None:
        if at < 0 or at + 8 > len(self.d):
            raise DecodeError(f"field at 0x{at:x} outside pages")
        q = self.u64(at)
        if q == 0:
            return None
        if not SYSTEM <= q < SYSTEM + len(self.d):
            raise DecodeError(f"pointer 0x{q:x} at 0x{at:x} outside pages")
        return q - SYSTEM

    def string(self, at: int) -> str | None:
        if at < 0 or at + 16 > len(self.d):
            raise DecodeError(f"string at 0x{at:x} outside pages")
        tag = self.d[at + 15]
        if tag & 0x80:
            n = tag & 0x7F
            if n > 15:
                raise DecodeError(f"inline string length {n}")
            return self.d[at:at + n].decode("latin-1", "replace")
        p = self.ptr(at)
        if p is None:
            return None
        n = self.u16(at + 8)
        if p + n > len(self.d):
            raise DecodeError("string past pages")
        return self.d[p:p + n].decode("latin-1", "replace")

    def array(self, at: int):
        p = self.ptr(at)
        return (p, self.u16(at + 8)) if p is not None else (None, 0)

    def atmap(self, at: int):
        buckets, n = self.ptr(at), self.u32(at + 8)
        if buckets is None:
            return []
        if n > 4096:
            raise DecodeError(f"atMap has {n} buckets")
        out, seen = [], set()
        for i in range(n):
            e = self.ptr(buckets + 8 * i)
            while e is not None:
                if e in seen:
                    raise DecodeError("atMap cycle")
                seen.add(e)
                out.append((self.u64(e), self.ptr(e + 8)))
                e = self.ptr(e + 16)
        return out


# ---------------------------------------------------------------------------
# Structure parsers
# ---------------------------------------------------------------------------
def parse_animation(s: System, at: int) -> dict:
    seqs, nseq = s.array(at + 0x50)
    tracks, ntr = s.array(at + 0x60)
    return {
        "offset": at,
        "frames": s.u16(at + 0x24),
        "duration": s.f32(at + 0x28),
        "sequences": [s.ptr(seqs + 8 * i) for i in range(nseq)] if seqs is not None else [],
        "tracks": [struct.unpack_from("<3I", s.d, tracks + 12 * i) for i in range(ntr)] if tracks is not None else [],
    }


def parse_clip(s: System, at: int) -> dict:
    kind = s.u32(at + 0x20)
    if kind >= 16:
        name_at, tag = s.u64(at + 0x30), s.d[at + 0x3F]
        if SYSTEM <= name_at < SYSTEM + len(s.d) or (tag & 0x80 and (tag & 0x7F) <= 15):
            return {"kind": "motion", "offset": at, "name": s.string(at + 0x30), "duration": 0.0, "animations": []}
        return {"kind": "motionmatcher", "offset": at, "name": s.string(at + 0x38), "duration": 0.0, "animations": []}
    clip = {"kind": "clip", "offset": at, "name": s.string(at + 0x28), "duration": s.f32(at + 0x78), "animations": []}
    lst = s.ptr(at + 0x68)
    if lst is not None:
        entries, n = s.array(lst)
        if entries is not None:
            for i in range(n):
                e = entries + 0x38 * i
                clip["animations"].append({
                    "animation": s.ptr(e),
                    "hash": s.u32(e + 8),
                    "start": s.f32(e + 0x28),
                    "end": s.f32(e + 0x2C),
                    "rate": s.f32(e + 0x30),
                })
    return clip


def parse_dictionary(s: System) -> dict:
    refs_word = s.u64(0x70)
    if refs_word and not SYSTEM <= refs_word < SYSTEM + len(s.d):
        raise NotClipDictionary("rig" if s.d[0x70:0x73] == b"RIG" else "other")
    out = {"name": s.string(0x38), "hash": s.u32(0x68), "clips": [], "packs": [], "anims_by_off": {}}
    for key, value in s.atmap(0x48):
        if value is not None:
            clip = parse_clip(s, value)
            clip["hash"] = key & 0xFFFFFFFF
            out["clips"].append(clip)
    refs, n = s.array(0x70)
    for i in range(n):
        pack = s.ptr(refs + 16 * i)
        if pack is None:
            continue
        anims = []
        for _k, v in s.atmap(pack + 0x48):
            if v is not None:
                a = parse_animation(s, v)
                anims.append(a)
                out["anims_by_off"][v] = a
        out["packs"].append({"name": s.string(pack + 0x38), "hash": s.u32(pack + 0x68), "animations": anims})
    return out


# ---------------------------------------------------------------------------
# Keyframe channel decoders (types 0..5, 7, 8, 10 delta2_float)
# ---------------------------------------------------------------------------
def _zigzag(u: int) -> int:
    return (u >> 1) if not (u & 1) else -((u + 1) >> 1)


def _bits(blob: bytes, pos: int, k: int) -> int:
    if not k:
        return 0
    o = pos >> 3
    v = int.from_bytes(blob[o:o + (((pos & 7) + k + 7) >> 3)], "little")
    return (v >> (pos & 7)) & ((1 << k) - 1)


def decode_delta2(data: memoryview, at: int, nf: int):
    words, packed, quantum, offset = struct.unpack_from("<IIff", data, at)
    c1, c2, c3 = packed & 255, (packed >> 8) & 255, (packed >> 16) & 255
    size = 4 * words
    if size < 16 or at + size > len(data):
        raise DecodeError("type-10 channel runs past sequence")
    blob = bytes(data[at + 16:at + size])
    nbits = 8 * len(blob)
    nc, ng = (nf + 63) // 64, (nf + 6) // 8
    starts = [_bits(blob, nc * c1 + i * c2, c2) for i in range(nc)]
    pos = nc * (c1 + c2)
    widths = []
    for _ in range(ng):
        o = pos >> 3
        byte = blob[o] >> (pos & 7) if o < len(blob) else 0
        if byte:
            k = (byte & -byte).bit_length() - 1
        else:
            k = 8 - (pos & 7)
            o += 1
            while o < len(blob) and not blob[o]:
                k += 8
                o += 1
            if o >= len(blob):
                raise DecodeError("type-10 width runs past channel")
            k += (blob[o] & -blob[o]).bit_length() - 1
        pos += k + 1
        widths.append(k)
    lane_at = nbits - 256 * c3
    if lane_at < pos or sum(widths) > 32 * c3:
        raise DecodeError("type-10 lane layout mismatch")
    lw = struct.unpack_from(f"<{8 * c3}I", blob, lane_at // 8)
    lanes = [b"".join(struct.pack("<I", lw[w * 8 + j]) for w in range(c3)) for j in range(8)]
    raw, lp = [], 0
    for k in widths:
        for j in range(8):
            raw.append(_bits(lanes[j], lp, k))
        lp += k
    values = [0.0] * nf
    for ci in range(nc):
        x, v, f0 = starts[ci], 0, ci * 64
        values[f0] = x * quantum + offset
        for j in range(64):
            f = f0 + j + 1
            if f >= nf:
                break
            v += _zigzag(raw[f0 + j])
            x += v
            if j == 63:
                break
            values[f] = x * quantum + offset
    return values, size


def decode_sequence(s: System, at: int):
    h = struct.unpack_from("<5I5H2B", s.d, at)
    length, fo, nf, fl = h[1], h[3], h[6], h[7]
    data = memoryview(s.d)[at + 0x20:at + 0x20 + length]
    lists_at = fo + fl * nf
    if lists_at + 22 > len(data):
        raise DecodeError("sequence counts past data")
    counts = struct.unpack_from("<11H", data, lists_at)
    lists, p = [], lists_at + 22
    for c in counts:
        lists.append([(x >> 2, x & 3) for x in struct.unpack_from(f"<{c}H", data, p)])
        p += 2 * ((c + 3) // 4 * 4)
    p, channels, frame_fields = 0, [], []
    for t, c in enumerate(counts):
        if c and t in (6, 9):
            raise DecodeError(f"unsupported channel type {t}")
        for i in range(c):
            track, comp = lists[t][i]
            value = None
            if t in (0, 1):
                value = struct.unpack_from("<3f", data, p)
                p += 12
            elif t == 2:
                value = struct.unpack_from("<f", data, p)[0]
                p += 4
            elif t == 3:
                frame_fields.append((len(channels), 32, "raw", None))
            elif t == 4:
                bits, quantum, offset = struct.unpack_from("<Iff", data, p)
                p += 12
                frame_fields.append((len(channels), bits, quantum, offset))
            elif t == 5:
                fbits, vbits, words, quantum, offset = struct.unpack_from("<IIIff", data, p)
                table_bytes = bytes(data[p + 20:p + 20 + 4 * words])
                p += 20 + 4 * words
                n = (32 * words) // vbits if vbits else 1
                table = [(_bits(table_bytes, j * vbits, vbits) if vbits else 0) * quantum + offset for j in range(n)]
                frame_fields.append((len(channels), fbits, "table", table))
            elif t == 10:
                value, size = decode_delta2(data, p, nf)
                p += size
            channels.append([t, track, comp, value])
    if p != fo:
        raise DecodeError(f"channel defs end at 0x{p:x} != 0x{fo:x}")
    if frame_fields:
        columns = {i: [] for i, *_ in frame_fields}
        for f in range(nf):
            row = int.from_bytes(data[fo + f * fl:fo + (f + 1) * fl], "little")
            for i, bits, quantum, offset in frame_fields:
                x = row & ((1 << bits) - 1)
                row >>= bits
                if quantum == "raw":
                    columns[i].append(struct.unpack("<f", struct.pack("<I", x))[0])
                elif quantum == "table":
                    columns[i].append(offset[x])
                else:
                    columns[i].append(x * quantum + offset)
        for i, col in columns.items():
            channels[i][3] = col
    return nf, channels


def assemble_tracks(nf: int, channels: list, ntracks: int) -> dict[int, list[tuple]]:
    by = collections.defaultdict(list)
    for t, track, comp, v in channels:
        if track < ntracks:
            by[track].append((t, comp, v))
    out = {}
    for track, chs in by.items():
        whole = [c for c in chs if c[0] in (0, 1)]
        if whole:
            t, _comp, v = whole[0]
            if t == 0:
                x, y, z = v
                r = max(0.0, 1.0 - x * x - y * y - z * z)
                v = (x, y, z, math.sqrt(r))
            out[track] = [tuple(v)] * nf
            continue
        comps, rebuild, normalise = {}, None, False
        for t, comp, v in chs:
            if t == 7:
                rebuild = comp
            elif t == 8:
                normalise = True
            else:
                comps[comp] = v if isinstance(v, list) else [v] * nf
        n = 3 if rebuild is not None else (max(comps) + 1 if comps else 0)
        frames = []
        for f in range(nf):
            vec = [comps[c][f] if c in comps else 0.0 for c in range(n)]
            if rebuild is not None:
                r = max(0.0, 1.0 - sum(a * a for a in vec))
                vec.insert(rebuild, math.sqrt(r))
            if rebuild is not None or normalise:
                norm = math.sqrt(sum(a * a for a in vec)) or 1.0
                vec = [a / norm for a in vec]
            frames.append(tuple(vec))
        out[track] = frames
    return out


def decode_animation(s: System, anim: dict):
    tracks, total = {}, 0
    for si, at in enumerate(anim["sequences"]):
        if at is None:
            continue
        nf, channels = decode_sequence(s, at)
        part = assemble_tracks(nf, channels, len(anim["tracks"]))
        for k, frames in part.items():
            have = tracks.setdefault(k, [frames[0]] * total if frames else [])
            have.extend(frames[1:] if si else frames)
        total += nf - (1 if si else 0)
        for k, have in tracks.items():
            if len(have) < total:
                have.extend([have[-1]] * (total - len(have)))
    return tracks, total


# ---------------------------------------------------------------------------
# Clip categorization & dictionary page grouping
# ---------------------------------------------------------------------------
def _is_root(head: bytes) -> bool:
    return (len(head) >= 0x40 and head[:8] == b"\x00" * 8
            and head[0x10:0x38] == b"\x00" * 0x28
            and (struct.unpack_from("<Q", head, 8)[0] >> 28) == 5)


def _named_root_hash(page: bytes):
    if len(page) < 0x6C or not _is_root(page):
        return None
    s = System(page)
    try:
        name = s.string(0x38)
    except DecodeError:
        return None
    stored = s.u32(0x68)
    return stored if name and joaat32(name) == stored else None


def _local_packs(data: bytes):
    if len(data) < 0x7A:
        return None
    refs, count = struct.unpack_from("<QH", data, 0x70)
    if not count or refs < SYSTEM:
        return set()
    start = refs - SYSTEM
    end = start + count * 16
    if end > len(data):
        return None
    return {(ptr, nh) for ptr, nh in struct.iter_unpack("<QI4x", memoryview(data)[start:end]) if ptr}


def _short_clip_name(name: str | None, fallback_hash: int) -> str:
    if not name:
        return f"clip_0x{fallback_hash:08x}"
    s = name
    if s.startswith("pack:/"):
        s = s[6:]
    for ext in (".clip", ".motion", ".motionmatcher"):
        if s.endswith(ext):
            s = s[:-len(ext)]
            break
    return s


_ANIMAL_KEYWORDS = (
    "creature", "animal", "cra_", "crb_", "crc_", "crf_", "cri_", "crm_", "crr_",
    "dog", "cat_", "@cat", "gator", "alligator", "croc", "horse", "deer", "bird",
    "shark", "cow", "pig", "boar", "fish", "crab", "frog", "iguana", "racoon",
    "raccoon", "bear", "panther", "cougar", "bobcat", "tiger", "lion", "snake",
    "chicken", "dolphin", "whale", "seagull", "pelican", "heron", "egret", "crane",
    "duck", "goose", "owl", "hawk", "eagle", "vulture", "parrot", "macaw", "flamingo",
    "turtle", "tortoise", "lizard", "manatee", "manta", "stingray", "otter", "skunk",
    "opossum", "squirrel", "rabbit", "rat_", "@rat", "bat_", "fox", "coyote", "wolf",
    "goat", "sheep", "rooster", "turkey", "armadillo", "lobster", "shrimp", "spider",
    "beetle", "butterfly", "dragonfly", "mosquito", "pelican", "pigeon", "crow",
)


def categorize_clip(dict_name: str, clip_name: str, archive: str = "anim.rpf") -> str:
    s = f"{dict_name} {clip_name}".lower()
    if any(k in s for k in _ANIMAL_KEYWORDS):
        return "Animals"
    if any(k in s for k in ("facial", "viseme", "blink", "mood", "lipsync", "head_look", "face_")):
        return "Facial"
    if any(k in s for k in ("cutscene", "cs_", "ig_", "mocap", "story", "mission")) or "story" in archive.lower():
        return "Cutscene"
    if any(k in s for k in ("veh@", "vehicle", "car_", "bike_", "boat_", "heli", "plane", "drive", "steering", "seat_")):
        return "Vehicles"
    if any(k in s for k in ("combat", "melee", "weapon", "gun@", "rifle", "pistol", "shoot", "aim_", "reload", "cover@", "punch", "fight", "reaction@", "hit_react", "death", "ragdoll")):
        return "Combat"
    if any(k in s for k in ("move_", "move@", "loco", "walk", "run_", "sprint", "jog", "strafe", "climb", "swim", "jump", "vault", "ladder", "crouch", "stealth", "turn_")):
        return "Locomotion"
    if any(k in s for k in ("sit_", "seat", "chair", "bench", "couch", "bed_", "sleep", "lean_", "prop_", "bar_", "drink", "smoke", "eat_", "phone", "cellphone", "coffee")):
        return "Sit & Props"
    if any(k in s for k in ("dance", "club", "wave", "gesture", "emote", "clap", "cheer", "taunt", "greet", "salute", "talk", "convo", "chat", "point_", "laugh", "cry", "react")):
        return "Gestures"
    if any(k in s for k in ("idle", "stand", "ambient", "fidget", "breathe", "guard", "wait")):
        return "Idle"
    return "Other"


def _pack_u32_set(hashes: set[int]) -> bytes | None:
    if not hashes:
        return None
    s = sorted(int(h) & 0xFFFFFFFF for h in hashes)
    return struct.pack(f"<{len(s)}I", *s)


def _unpack_u32_set(blob: bytes | None) -> frozenset[int]:
    if not blob:
        return frozenset()
    n = len(blob) // 4
    if n == 0:
        return frozenset()
    return frozenset(struct.unpack_from(f"<{n}I", blob, 0))


def _extract_dict_entries(
    archive: str,
    off: int,
    segs: list,
    joined: bytes,
    female_hashes: set[int],
    male_hashes: set[int],
):
    """Parse one joined clip dictionary into (dict_row, clips_rows, blendspaces_rows, mm_rows)."""
    import blendspace as bs_mod
    import motion_matcher as mm_mod

    try:
        sys_buf = System(joined)
        d_parsed = parse_dictionary(sys_buf)
    except (DecodeError, struct.error, IndexError):
        return None

    dname = d_parsed["name"] or f"dict_0x{d_parsed['hash']:08x}"
    dhash = int(d_parsed["hash"]) & 0xFFFFFFFF
    anims_by_off = d_parsed["anims_by_off"]

    playable_clips = []
    blendspaces_out = []
    mm_out = []
    dict_bone_hashes: set[int] = set()
    best_f = 0
    best_m = 0
    has_mm_clip = False

    bs_sys = None
    for clip in d_parsed["clips"]:
        kind = clip["kind"]
        if kind == "clip":
            if not clip["animations"]:
                continue
            # Gather ALL local animations for this clip so layered bone tracks are captured
            local_anims = []
            for aref in clip["animations"]:
                aoff = aref.get("animation")
                if aoff is not None and aoff in anims_by_off:
                    local_anims.append(anims_by_off[aoff])
            if not local_anims:
                continue
            # Primary animation = one with most tracks (and >= 1 frame)
            primary = max(local_anims, key=lambda a: (len(a["tracks"]), a["frames"]))
            if primary["frames"] < 1 or not primary["tracks"]:
                continue
            bone_hashes: set[int] = set()
            for arec in local_anims:
                for tr in arec["tracks"]:
                    if tr[1] in (TRACK_ROT, TRACK_TRANS, TRACK_SCALE) and tr[0] != 0:
                        bone_hashes.add(int(tr[0]) & 0xFFFFFFFF)
            if not bone_hashes:
                continue
            dict_bone_hashes.update(bone_hashes)
            mf = len(bone_hashes & female_hashes)
            mm = len(bone_hashes & male_hashes)
            if mf > best_f:
                best_f = mf
            if mm > best_m:
                best_m = mm
            cname = clip["name"] or ""
            short_n = _short_clip_name(cname, clip["hash"])
            dur = float(clip["duration"]) if math.isfinite(clip["duration"]) and clip["duration"] > 0 else float(primary["duration"])
            if not math.isfinite(dur) or dur < 0:
                dur = 0.0
            cat = categorize_clip(dname, short_n, archive=archive)
            playable_clips.append((
                off,
                cname,
                short_n,
                int(clip["hash"]) & 0xFFFFFFFF,
                round(dur, 4),
                int(primary["frames"]),
                len(bone_hashes),
                mf,
                mm,
                cat,
            ))
        elif kind == "motion":
            if bs_sys is None:
                bs_sys = bs_mod.SystemPage(joined)
            coff = clip["offset"]
            if not bs_mod.is_motion_clip(bs_sys, coff):
                continue
            try:
                parsed_bs = bs_mod.parse_motion_clip(bs_sys, coff, dname)
            except Exception:
                continue
            cname = parsed_bs["name"]
            short_n = _short_clip_name(cname, clip["hash"])
            axes_names = [ax["name"] or ax["hash"] for ax in parsed_bs.get("axes", [])]
            domain = bs_mod._classify_domain(dname)
            cat = categorize_clip(dname, short_n, archive=archive)
            sub_cnt = len(parsed_bs.get("subMotions", [])) if parsed_bs["variantType"] == 2 else 1
            blendspaces_out.append((
                off,
                cname,
                short_n,
                int(clip["hash"]) & 0xFFFFFFFF,
                int(parsed_bs["variantType"]),
                int(parsed_bs["dimensions"]),
                int(parsed_bs["nodeType"]),
                sub_cnt,
                int(parsed_bs["sampleCount"]),
                int(parsed_bs["childCount"]),
                json.dumps(axes_names, separators=(",", ":")),
                domain,
                cat,
            ))
        elif kind == "motionmatcher":
            has_mm_clip = True

    if has_mm_clip:
        try:
            mms = mm_mod.parse_dictionary_motionmatchers(joined, resolver=None, include_poses=False)
        except Exception:
            mms = []
        for mm_rec in mms:
            rf = mm_rec.get("rigFamily") or {}
            st = mm_rec.get("stats") or {}
            bhashes = {int(b["hash"], 16) for b in mm_rec.get("bones", []) if b.get("hash")}
            dict_bone_hashes.update(bhashes)
            mm_out.append((
                off,
                mm_rec["name"],
                mm_rec["shortName"],
                int(mm_rec["hash"], 16) & 0xFFFFFFFF,
                int(mm_rec["mode"]),
                mm_rec["modeName"],
                rf.get("category", "human"),
                rf.get("domain", ""),
                rf.get("clade", ""),
                rf.get("species", ""),
                int(st.get("boneCount", 0)),
                int(st.get("poseCount", 0)),
                int(st.get("candidateCount", 0)),
                int(st.get("intervalCount", 0)),
                int(st.get("featureDim", 0)),
                int(st.get("channelCount", 0)),
                _pack_u32_set(bhashes),
            ))

    if not playable_clips and not blendspaces_out and not mm_out:
        return None

    if playable_clips:
        dict_cat = playable_clips[0][9]
    elif blendspaces_out:
        dict_cat = blendspaces_out[0][12]
    else:
        dict_cat = "Animals" if mm_out[0][6] == "animal" else categorize_clip(dname, "", archive=archive)

    dict_row = (
        archive,
        off,
        json.dumps(segs, separators=(",", ":")),
        dname,
        dhash,
        len(playable_clips),
        len(blendspaces_out),
        len(mm_out),
        best_f,
        best_m,
        dict_cat,
        _pack_u32_set(dict_bone_hashes),
    )
    return dict_row, playable_clips, blendspaces_out, mm_out


def _scan_spans_worker(args):
    """Fast worker using INDEX/<archive>/deflate-spans.bin (32-byte <QQQII records)."""
    archive, rpf_path, spans_path, lo, hi, female_hashes, male_hashes = args
    raw = Path(spans_path).read_bytes()
    n_spans = len(raw) // 32
    spans = list(struct.iter_unpack("<QQQII", raw[:n_spans * 32]))
    hi = min(hi, n_spans)

    dicts_out = []
    clips_out = []
    bs_out = []
    mm_out = []

    with open(rpf_path, "rb") as fh, mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as m:
        size = len(m)
        taken = set()
        for i in range(lo, hi):
            if i in taken:
                continue
            off, comp, uncomp, _, first4 = spans[i]
            if first4 != 0 or uncomp < 0x80:
                continue
            try:
                data = zlib.decompress(m[off:off + comp])
            except zlib.error:
                continue
            if not _is_root(data):
                continue

            pm = struct.unpack_from("<Q", data, 8)[0] - SYSTEM
            parts = [i]
            page_bytes = [data]
            k = i

            def _next_adj(idx_a, idx_b):
                oa, sa = spans[idx_a][0], spans[idx_a][1]
                return spans[idx_b][0] == ((oa + sa + 15) & ~15)

            def _separate_root(page, offset):
                nh = _named_root_hash(page)
                if nh is None:
                    return None
                packs = _local_packs(data)
                if packs is None and len(page_bytes) > 1:
                    packs = _local_packs(b"".join(page_bytes))
                return packs is None or (SYSTEM + offset, nh) not in packs

            count_at = pm + 8
            counts = data[count_at:count_at + 2]
            end_len = len(data)
            while len(counts) < 2 and len(parts) < 255 and k + 1 < n_spans:
                if not _next_adj(k, k + 1):
                    break
                no, nc = spans[k + 1][0], spans[k + 1][1]
                try:
                    f_page = zlib.decompress(m[no:no + nc])
                except zlib.error:
                    break
                start_len = end_len
                end_len += len(f_page)
                if _is_root(f_page) and (len(parts) >= 4 or any(f_page[0x38:0x40])
                                         or struct.unpack_from("<Q", f_page, 8)[0] - SYSTEM + 10 <= len(f_page)):
                    if _separate_root(f_page, start_len) is not False:
                        break
                k += 1
                parts.append(k)
                page_bytes.append(f_page)
                if end_len > count_at:
                    counts += f_page[max(0, count_at - start_len):count_at + 2 - start_len]

            if len(counts) != 2:
                continue
            pages, graphics = counts[0], counts[1]
            if graphics != 0 or pages == 0:
                continue
            while len(parts) < pages and k + 1 < n_spans and _next_adj(k, k + 1):
                no, nc = spans[k + 1][0], spans[k + 1][1]
                try:
                    f_page = zlib.decompress(m[no:no + nc])
                except zlib.error:
                    break
                if _is_root(f_page) and _separate_root(f_page, end_len):
                    break
                end_len += len(f_page)
                k += 1
                parts.append(k)
                page_bytes.append(f_page)

            joined = b"".join(page_bytes)
            segs = [(spans[j][0], spans[j][1], 0) for j in parts]

            # Handle small unlisted tail page (zlib < 64B or 16B raw) when 1 page short
            if len(parts) == pages - 1:
                last_off, last_stored = spans[k][0], spans[k][1]
                end_pos = last_off + last_stored
                at = (end_pos + 15) & ~15
                if at + 16 <= size and m[end_pos:at] == bytes(at - end_pos):
                    tail_seg = None
                    tail_bytes = None
                    if ZHDR.match(m, at):
                        z = zlib.decompressobj()
                        chunk_b = m[at:min(size, at + 4096)]
                        try:
                            pg = z.decompress(chunk_b)
                            if z.eof:
                                tail_bytes = pg
                                tail_seg = (at, len(chunk_b) - len(z.unused_data), 0)
                        except zlib.error:
                            pass
                    if tail_seg is None:
                        tail_bytes = bytes(m[at:at + 16])
                        tail_seg = (at, 16, 1)
                    try:
                        parse_dictionary(System(joined + tail_bytes))
                        joined = joined + tail_bytes
                        segs.append(tail_seg)
                    except (DecodeError, struct.error, IndexError):
                        continue
                else:
                    continue
            elif len(parts) != pages:
                continue

            taken.update(parts)
            extracted = _extract_dict_entries(archive, off, segs, joined, female_hashes, male_hashes)
            if extracted is not None:
                drow, crows, brows, mrows = extracted
                dicts_out.append(drow)
                clips_out.extend(crows)
                bs_out.extend(brows)
                mm_out.extend(mrows)

    return archive, dicts_out, clips_out, bs_out, mm_out


def _scan_chunk_worker(args):
    """Fallback worker: raw zlib scan for an archive without deflate-spans.bin."""
    archive, rpf_path, chunk_start, chunk_end, overlap_end, female_hashes, male_hashes = args
    dicts_out = []
    clips_out = []
    bs_out = []
    mm_out = []

    with open(rpf_path, "rb") as fh, mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as m:
        size = len(m)
        scan_end = min(size, overlap_end)
        streams = []
        pos = chunk_start
        while pos < scan_end:
            hit = ZHDR.search(m, pos, scan_end)
            if not hit:
                break
            s = hit.start()
            if s >= chunk_end and len(streams) > 0:
                prev_off, prev_stored, _ = streams[-1]
                if s > ((prev_off + prev_stored + 15) & ~15) + 16:
                    break
            d_obj = zlib.decompressobj()
            chunks, p = [], s
            try:
                while not d_obj.eof and p < size:
                    blk = m[p:min(p + (1 << 20), size)]
                    chunks.append(d_obj.decompress(blk))
                    p += len(blk)
                    if d_obj.unused_data or d_obj.eof:
                        break
                out = b"".join(chunks) + d_obj.flush()
            except zlib.error:
                pos = s + 1
                continue
            if not d_obj.eof or len(out) < 64:
                pos = s + 1
                continue
            used = p - s - len(d_obj.unused_data)
            streams.append((s, used, out))
            pos = s + used

        taken = set()
        n_streams = len(streams)
        for i in range(n_streams):
            if i in taken:
                continue
            off, stored, data = streams[i]
            if off >= chunk_end:
                break
            if not _is_root(data):
                continue
            pm = struct.unpack_from("<Q", data, 8)[0] - SYSTEM
            parts = [i]
            k = i

            def _next_adj(idx_a, idx_b):
                oa, sa, _ = streams[idx_a]
                ob, _, _ = streams[idx_b]
                return ob == ((oa + sa + 15) & ~15)

            def _separate_root(page, offset):
                nh = _named_root_hash(page)
                if nh is None:
                    return None
                packs = _local_packs(data)
                if packs is None and len(parts) > 1:
                    prefix = b"".join(streams[j][2] for j in parts)
                    packs = _local_packs(prefix)
                return packs is None or (SYSTEM + offset, nh) not in packs

            count_at = pm + 8
            counts = data[count_at:count_at + 2]
            end_len = len(data)
            while len(counts) < 2 and len(parts) < 255 and k + 1 < n_streams:
                if not _next_adj(k, k + 1):
                    break
                f_page = streams[k + 1][2]
                start_len = end_len
                end_len += len(f_page)
                if _is_root(f_page) and (len(parts) >= 4 or any(f_page[0x38:0x40])
                                         or struct.unpack_from("<Q", f_page, 8)[0] - SYSTEM + 10 <= len(f_page)):
                    if _separate_root(f_page, start_len) is not False:
                        break
                k += 1
                parts.append(k)
                if end_len > count_at:
                    counts += f_page[max(0, count_at - start_len):count_at + 2 - start_len]
            if len(counts) != 2:
                continue
            pages, graphics = counts[0], counts[1]
            if graphics != 0 or pages == 0:
                continue
            while len(parts) < pages and k + 1 < n_streams and _next_adj(k, k + 1):
                f_page = streams[k + 1][2]
                if _is_root(f_page) and _separate_root(f_page, end_len):
                    break
                end_len += len(f_page)
                k += 1
                parts.append(k)

            joined = b"".join(streams[j][2] for j in parts)
            segs = [(streams[j][0], streams[j][1], 0) for j in parts]

            if len(parts) == pages - 1:
                last_off, last_stored, _ = streams[k]
                end_pos = last_off + last_stored
                at = (end_pos + 15) & ~15
                if at + 16 <= size and m[end_pos:at] == bytes(at - end_pos):
                    tail_seg = None
                    tail_bytes = None
                    if ZHDR.match(m, at):
                        z = zlib.decompressobj()
                        chunk_b = m[at:min(size, at + 4096)]
                        try:
                            pg = z.decompress(chunk_b)
                            if z.eof:
                                tail_bytes = pg
                                tail_seg = (at, len(chunk_b) - len(z.unused_data), 0)
                        except zlib.error:
                            pass
                    if tail_seg is None:
                        tail_bytes = bytes(m[at:at + 16])
                        tail_seg = (at, 16, 1)
                    try:
                        parse_dictionary(System(joined + tail_bytes))
                        joined = joined + tail_bytes
                        segs.append(tail_seg)
                    except (DecodeError, struct.error, IndexError):
                        continue
                else:
                    continue
            elif len(parts) != pages:
                continue

            taken.update(parts)
            extracted = _extract_dict_entries(archive, off, segs, joined, female_hashes, male_hashes)
            if extracted is not None:
                drow, crows, brows, mrows = extracted
                dicts_out.append(drow)
                clips_out.extend(crows)
                bs_out.extend(brows)
                mm_out.extend(mrows)

    return archive, dicts_out, clips_out, bs_out, mm_out


def build_index(
    archives: list[str] | None = None,
    db_path: Path = ANIM_DB,
    female_hashes: set[int] | None = None,
    male_hashes: set[int] | None = None,
    workers: int | None = None,
    progress_cb=None,
) -> dict:
    """Scan all configured RPF9 archives in parallel and build anim_clips.sqlite3."""
    import motion_matcher as mm_mod

    t0 = time.monotonic()
    if female_hashes is None or male_hashes is None:
        female_hashes, male_hashes = _default_ped_bone_hashes()

    arch_list = [a for a in (archives or ANIM_ARCHIVES) if (GAME / a).is_file()]
    if not arch_list:
        raise FileNotFoundError(f"No animation RPF archives found under {GAME}")

    span_tasks = []
    chunk_tasks = []
    for arch in arch_list:
        rpf_path = GAME / arch
        spans_path = SPANS_ROOT / arch / "deflate-spans.bin"
        if spans_path.is_file():
            n_spans = spans_path.stat().st_size // 32
            step = max(1500, n_spans // 32)
            for lo in range(0, n_spans, step):
                span_tasks.append((arch, str(rpf_path), str(spans_path), lo, min(n_spans, lo + step), female_hashes, male_hashes))
        else:
            file_size = rpf_path.stat().st_size
            with open(rpf_path, "rb") as fh:
                hdr = fh.read(8)
            data_start = struct.unpack_from("<I", hdr, 4)[0] if len(hdr) >= 8 else 0
            if not 0 <= data_start < file_size:
                data_start = 0
            chunk_size = 32 << 20
            overlap = 2 << 20
            pos = data_start
            while pos < file_size:
                cend = min(file_size, pos + chunk_size)
                oend = min(file_size, cend + overlap)
                chunk_tasks.append((arch, str(rpf_path), pos, cend, oend, female_hashes, male_hashes))
                pos = cend

    n_workers = workers or min(16, max(1, (os.cpu_count() or 4) // 2))
    tmp_db = db_path.with_suffix(".sqlite3.tmp")
    if tmp_db.exists():
        tmp_db.unlink()

    total_tasks = len(span_tasks) + len(chunk_tasks)
    with closing(sqlite3.connect(str(tmp_db))) as con:
        con.execute("PRAGMA journal_mode=OFF")
        con.execute("PRAGMA synchronous=OFF")
        con.execute("""
            CREATE TABLE dict (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                archive TEXT NOT NULL,
                offset INTEGER NOT NULL,
                segments TEXT NOT NULL,
                name TEXT NOT NULL,
                hash INTEGER NOT NULL,
                clip_count INTEGER NOT NULL,
                motion_count INTEGER NOT NULL DEFAULT 0,
                mm_count INTEGER NOT NULL DEFAULT 0,
                best_f INTEGER NOT NULL,
                best_m INTEGER NOT NULL,
                category TEXT NOT NULL,
                bone_hashes BLOB,
                UNIQUE(archive, offset)
            )
        """)
        con.execute("""
            CREATE TABLE clip (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dict_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                short_name TEXT NOT NULL,
                hash INTEGER NOT NULL,
                duration REAL NOT NULL,
                frames INTEGER NOT NULL,
                bones INTEGER NOT NULL,
                match_f INTEGER NOT NULL,
                match_m INTEGER NOT NULL,
                category TEXT NOT NULL
            )
        """)
        con.execute("""
            CREATE TABLE blendspace (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dict_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                short_name TEXT NOT NULL,
                hash INTEGER NOT NULL,
                variant_type INTEGER NOT NULL,
                dimensions INTEGER NOT NULL,
                node_type INTEGER NOT NULL,
                sub_count INTEGER NOT NULL,
                sample_count INTEGER NOT NULL,
                child_count INTEGER NOT NULL,
                axes_json TEXT NOT NULL,
                domain TEXT NOT NULL,
                category TEXT NOT NULL
            )
        """)
        con.execute("""
            CREATE TABLE motion_matcher (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dict_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                short_name TEXT NOT NULL,
                hash INTEGER NOT NULL,
                mode INTEGER NOT NULL,
                mode_name TEXT NOT NULL,
                rig_category TEXT NOT NULL,
                rig_domain TEXT NOT NULL,
                rig_clade TEXT NOT NULL,
                rig_species TEXT NOT NULL,
                bone_count INTEGER NOT NULL,
                pose_count INTEGER NOT NULL,
                candidate_count INTEGER NOT NULL,
                resolved_count INTEGER NOT NULL DEFAULT 0,
                interval_count INTEGER NOT NULL,
                feature_dim INTEGER NOT NULL,
                channel_count INTEGER NOT NULL,
                bone_hashes BLOB
            )
        """)

        dict_id_by_key: dict[tuple[str, int], int] = {}
        total_dicts = 0
        total_clips = 0
        total_bs = 0
        total_mm = 0
        done_i = 0

        def _ingest(result):
            nonlocal total_dicts, total_clips, total_bs, total_mm, done_i
            arch, dicts_out, clips_out, bs_out, mm_out = result
            for row in dicts_out:
                key = (row[0], row[1])
                if key in dict_id_by_key:
                    continue
                cur = con.execute(
                    """INSERT INTO dict(
                           archive, offset, segments, name, hash,
                           clip_count, motion_count, mm_count,
                           best_f, best_m, category, bone_hashes
                       ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    row,
                )
                dict_id_by_key[key] = cur.lastrowid
                total_dicts += 1
            if clips_out:
                clip_batch = []
                for crow in clips_out:
                    did = dict_id_by_key.get((arch, crow[0]))
                    if did is not None:
                        clip_batch.append((did, *crow[1:]))
                if clip_batch:
                    con.executemany(
                        """INSERT INTO clip(
                               dict_id, name, short_name, hash, duration,
                               frames, bones, match_f, match_m, category
                           ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                        clip_batch,
                    )
                    total_clips += len(clip_batch)
            if bs_out:
                bs_batch = []
                for brow in bs_out:
                    did = dict_id_by_key.get((arch, brow[0]))
                    if did is not None:
                        bs_batch.append((did, *brow[1:]))
                if bs_batch:
                    con.executemany(
                        """INSERT INTO blendspace(
                               dict_id, name, short_name, hash, variant_type,
                               dimensions, node_type, sub_count, sample_count,
                               child_count, axes_json, domain, category
                           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        bs_batch,
                    )
                    total_bs += len(bs_batch)
            if mm_out:
                mm_batch = []
                for mrow in mm_out:
                    did = dict_id_by_key.get((arch, mrow[0]))
                    if did is not None:
                        mm_batch.append((did, *mrow[1:]))
                if mm_batch:
                    con.executemany(
                        """INSERT INTO motion_matcher(
                               dict_id, name, short_name, hash, mode, mode_name,
                               rig_category, rig_domain, rig_clade, rig_species,
                               bone_count, pose_count, candidate_count, interval_count,
                               feature_dim, channel_count, bone_hashes
                           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        mm_batch,
                    )
                    total_mm += len(mm_batch)
            done_i += 1
            if progress_cb:
                progress_cb(done_i, total_tasks, total_dicts, total_clips, total_bs, total_mm)

        with concurrent.futures.ProcessPoolExecutor(max_workers=n_workers) as pool:
            if span_tasks:
                for res in pool.map(_scan_spans_worker, span_tasks):
                    _ingest(res)
            if chunk_tasks:
                for res in pool.map(_scan_chunk_worker, chunk_tasks):
                    _ingest(res)

        con.execute("CREATE INDEX idx_clip_cat ON clip(category, match_f DESC, frames DESC)")
        con.execute("CREATE INDEX idx_clip_name ON clip(short_name)")
        con.execute("CREATE INDEX idx_clip_dict ON clip(dict_id)")
        con.execute("CREATE INDEX idx_clip_dict_hash ON clip(dict_id, hash)")
        con.execute("CREATE INDEX idx_dict_name ON dict(name)")
        con.execute("CREATE INDEX idx_dict_hash ON dict(hash)")
        con.execute("CREATE INDEX idx_dict_archive ON dict(archive)")
        con.execute("CREATE INDEX idx_bs_dim ON blendspace(dimensions, domain)")
        con.execute("CREATE INDEX idx_bs_dict ON blendspace(dict_id)")
        con.execute("CREATE INDEX idx_mm_cat ON motion_matcher(rig_category, mode)")
        con.commit()

        # Fast post-pass: resolve exact candidate counts for all motion_matcher rows
        if total_mm > 0:
            resolver = mm_mod.ClipResolver(tmp_db)
            mm_dicts = con.execute(
                """SELECT DISTINCT d.id, d.archive, d.segments
                   FROM dict d JOIN motion_matcher m ON m.dict_id = d.id"""
            ).fetchall()
            open_fhs = {}
            try:
                updates = []
                for did, arch, segs_json in mm_dicts:
                    if arch not in open_fhs:
                        open_fhs[arch] = (GAME / arch).open("rb")
                    fh = open_fhs[arch]
                    parts = []
                    for off, stored, is_raw in json.loads(segs_json):
                        fh.seek(off)
                        raw_b = fh.read(stored)
                        parts.append(raw_b if is_raw else zlib.decompressobj().decompress(raw_b))
                    joined = b"".join(parts)
                    for parsed in mm_mod.parse_dictionary_motionmatchers(joined, resolver=resolver, include_poses=False):
                        h_int = int(parsed["hash"], 16) & 0xFFFFFFFF
                        rc = int(parsed["stats"]["resolvedCandidateCount"])
                        updates.append((rc, did, h_int))
                if updates:
                    con.executemany(
                        "UPDATE motion_matcher SET resolved_count=? WHERE dict_id=? AND hash=?",
                        updates,
                    )
                    con.commit()
            finally:
                for fh in open_fhs.values():
                    fh.close()

    os.replace(tmp_db, db_path)
    elapsed = round(time.monotonic() - t0, 2)
    return {
        "archives": arch_list,
        "dictionaries": total_dicts,
        "clips": total_clips,
        "blendspaces": total_bs,
        "motionMatchers": total_mm,
        "seconds": elapsed,
        "db": str(db_path),
    }


def _default_ped_bone_hashes() -> tuple[set[int], set[int]]:
    """Read female and male master skeleton bone hashes from LIBRARY/inventory.sqlite3."""
    import character_rig as cr
    inv = LIBRARY / "inventory.sqlite3"
    f_set, m_set = set(), set()
    if not inv.is_file():
        return f_set, m_set
    with closing(sqlite3.connect(str(inv))) as con:
        con.row_factory = sqlite3.Row
        for sname, target in (("ambient_femalebody", f_set), ("ambient_malebody", m_set)):
            r = con.execute("SELECT archive, member, root, name, kind FROM model WHERE name=? LIMIT 1", (sname,)).fetchone()
            if r:
                sk = cr.read_skeleton(r["archive"], int(r["member"]), int(r["root"]), r["name"], r["kind"])
                if sk:
                    for j in sk["joints"]:
                        target.add(int(j["hash"]))
                        target.add(joaat32(j["name"]))
    return f_set, m_set


# ---------------------------------------------------------------------------
# Runtime service: multi-archive search, skeleton matching, blendspaces, MM
# ---------------------------------------------------------------------------
class AnimService:
    def __init__(self, rpf_path: Path = ANIM_RPF, db_path: Path = ANIM_DB):
        self.rpf_path = rpf_path
        self.db_path = db_path
        self._lock = threading.Lock()
        self._fhs: dict[str, object] = {}
        self._mmaps: dict[str, mmap.mmap] = {}
        self._clip_cache = collections.OrderedDict()
        self._bs_cache = collections.OrderedDict()
        self._mm_cache = collections.OrderedDict()
        self._dict_bones: list[tuple[int, str, str, str, frozenset[int]]] | None = None
        self._skel_match_cache = collections.OrderedDict()
        self._clip_resolver = None
        self._schema_checked = False
        self._has_v2_schema = False
        self._indexing = False
        self._index_progress = None

    def _mmap(self, archive: str = "anim.rpf") -> mmap.mmap:
        arch_norm = archive.replace("\\", "/") if archive else "anim.rpf"
        with self._lock:
            mm = self._mmaps.get(arch_norm)
            if mm is None:
                p = self.rpf_path if arch_norm == "anim.rpf" else (GAME / arch_norm)
                if not p.is_file():
                    raise FileNotFoundError(f"Archive {arch_norm} not present in workspace")
                fh = p.open("rb")
                mm = mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ)
                self._fhs[arch_norm] = fh
                self._mmaps[arch_norm] = mm
            return mm

    def _check_schema(self):
        if self._schema_checked or not self.db_path.is_file():
            return
        with closing(sqlite3.connect(str(self.db_path))) as con:
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            cols = {r[1] for r in con.execute("PRAGMA table_info(dict)")} if "dict" in tables else set()
            self._has_v2_schema = ("blendspace" in tables and "motion_matcher" in tables and "archive" in cols)
        self._schema_checked = True

    def available(self) -> bool:
        return self.db_path.is_file() and self.rpf_path.is_file()

    def _get_resolver(self):
        with self._lock:
            if self._clip_resolver is None:
                import motion_matcher as mm_mod
                self._clip_resolver = mm_mod.ClipResolver(self.db_path)
            return self._clip_resolver

    def _ensure_dict_bones(self):
        if self._dict_bones is not None:
            return self._dict_bones
        self._check_schema()
        if not self._has_v2_schema:
            self._dict_bones = []
            return self._dict_bones
        entries = []
        with closing(sqlite3.connect(str(self.db_path))) as con:
            for did, dname, arch, cat, blob in con.execute(
                "SELECT id, name, archive, category, bone_hashes FROM dict WHERE bone_hashes IS NOT NULL"
            ):
                bset = _unpack_u32_set(blob)
                if bset:
                    entries.append((int(did), (dname or "").lower(), arch, cat, bset))
        with self._lock:
            self._dict_bones = entries
        return entries

    _BROAD_CLADE_TOKENS = frozenset({
        "mammal", "mammals", "bird", "birds", "reptile", "reptiles", "fish",
        "amphibian", "amphibians", "crustacean", "crustaceans", "insect", "insects",
        "creature", "creatures", "animal", "animals",
    })

    def _match_dicts_for_skeleton(
        self,
        skeleton_id: int,
        skeleton_hashes: set[int],
        species_tokens: list[str] | None = None,
        rig_kind: str = "animal",
    ) -> dict[int, tuple[int, int, float]]:
        """Return `{dict_id: (species_hit, overlap_bones, precision)}` for compatible dicts."""
        toks = tuple(t.lower() for t in (species_tokens or []) if t and len(t) >= 3)
        specific_toks = tuple(t for t in toks if t not in self._BROAD_CLADE_TOKENS)
        broad_toks = tuple(t for t in toks if t in self._BROAD_CLADE_TOKENS)
        ckey = (int(skeleton_id), toks, rig_kind)
        with self._lock:
            hit = self._skel_match_cache.get(ckey)
            if hit is not None:
                self._skel_match_cache.move_to_end(ckey)
                return hit

        entries = self._ensure_dict_bones()
        skel_set = set(int(h) & 0xFFFFFFFF for h in skeleton_hashes)
        min_ov = 4 if len(skel_set) >= 12 else max(2, len(skel_set) // 3)
        matched: dict[int, tuple[int, int, float]] = {}

        for did, dlow, _arch, cat, bset in entries:
            ov = len(skel_set & bset)
            if ov < min_ov:
                continue
            prec = ov / max(1, len(bset))
            if specific_toks and any(t in dlow for t in specific_toks):
                sp_hit = 2
            elif broad_toks and any(t in dlow for t in broad_toks):
                sp_hit = 1
            else:
                sp_hit = 0
            if rig_kind == "animal":
                # Keep animal clip lists focused on genuine creature/species dicts, excluding human rigs
                if not (sp_hit >= 2 or cat == "Animals" or prec >= 0.72):
                    continue
            matched[did] = (sp_hit, ov, round(prec, 4))

        with self._lock:
            self._skel_match_cache[ckey] = matched
            self._skel_match_cache.move_to_end(ckey)
            while len(self._skel_match_cache) > 64:
                self._skel_match_cache.popitem(last=False)
        return matched

    def status(self) -> dict:
        if self.available():
            self._check_schema()
            with closing(sqlite3.connect(str(self.db_path))) as con:
                nd = con.execute("SELECT COUNT(*) FROM dict").fetchone()[0]
                nc = con.execute("SELECT COUNT(*) FROM clip").fetchone()[0]
                cats = con.execute("SELECT category, COUNT(*) FROM clip GROUP BY category ORDER BY COUNT(*) DESC").fetchall()
                nbs = con.execute("SELECT COUNT(*) FROM blendspace").fetchone()[0] if self._has_v2_schema else 0
                nmm = con.execute("SELECT COUNT(*) FROM motion_matcher").fetchone()[0] if self._has_v2_schema else 0
                archs = []
                if self._has_v2_schema:
                    archs = [
                        {"name": a, "label": ARCHIVE_LABELS.get(a, a), "clips": int(c)}
                        for a, c in con.execute(
                            """SELECT d.archive, COUNT(c.id)
                               FROM clip c JOIN dict d ON d.id = c.dict_id
                               GROUP BY d.archive ORDER BY COUNT(c.id) DESC"""
                        ).fetchall()
                    ]
            return {
                "ready": True,
                "indexing": False,
                "dictionaries": nd,
                "clips": nc,
                "blendspaces": nbs,
                "motionMatchers": nmm,
                "archives": archs,
                "categories": [{"name": c, "count": n} for c, n in cats],
            }
        return {
            "ready": False,
            "indexing": self._indexing,
            "hasRpf": self.rpf_path.is_file(),
            "progress": self._index_progress,
            "dictionaries": 0,
            "clips": 0,
            "blendspaces": 0,
            "motionMatchers": 0,
            "archives": [],
            "categories": [],
        }

    def search(
        self,
        q: str = "",
        category: str = "",
        rig: str = "female",
        archive: str = "",
        skeleton_id: int | None = None,
        skeleton_hashes: set[int] | None = None,
        species_tokens: list[str] | None = None,
        limit: int = 60,
        offset: int = 0,
    ) -> dict:
        if not self.available():
            return {"ready": False, "items": [], "total": 0, "categories": [], "archives": []}
        self._check_schema()
        limit = max(1, min(200, int(limit)))
        offset = max(0, int(offset))
        match_col = "c.match_m" if rig == "male" else "c.match_f"

        # Per-skeleton exact bone-overlap matching for animals / custom rigs
        use_skel_match = bool(
            self._has_v2_schema
            and skeleton_id is not None
            and skeleton_hashes
            and rig not in ("female", "male")
        )

        if use_skel_match:
            matched_dicts = self._match_dicts_for_skeleton(
                int(skeleton_id), skeleton_hashes, species_tokens=species_tokens, rig_kind=rig
            )
            if not matched_dicts:
                return {"ready": True, "total": 0, "items": [], "categories": [], "archives": []}

            # Rank dicts first, query clips inside matched dicts
            where = ["c.duration > 0.05", "c.frames >= 2"]
            params: list = []
            if archive and archive != "All":
                where.append("d.archive = ?")
                params.append(archive)
            if category and category != "All":
                where.append("c.category = ?")
                params.append(category)
            if q:
                for tok in [t.strip().lower() for t in q.split() if t.strip()][:4]:
                    where.append("(LOWER(c.short_name) LIKE ? OR LOWER(d.name) LIKE ?)")
                    like = f"%{tok}%"
                    params.extend([like, like])

            # Chunk IN-clauses cleanly via a temp table on this connection
            with closing(sqlite3.connect(str(self.db_path))) as con:
                con.row_factory = sqlite3.Row
                con.execute("CREATE TEMP TABLE _md (dict_id INTEGER PRIMARY KEY, sp INTEGER, ov INTEGER, prec REAL)")
                con.executemany(
                    "INSERT INTO _md(dict_id, sp, ov, prec) VALUES (?,?,?,?)",
                    [(did, sp, ov, prec) for did, (sp, ov, prec) in matched_dicts.items()],
                )
                where_sql = " AND ".join(where)
                total = con.execute(
                    f"""SELECT COUNT(*) FROM clip c
                        JOIN dict d ON d.id = c.dict_id
                        JOIN _md m ON m.dict_id = c.dict_id
                        WHERE {where_sql}""",
                    params,
                ).fetchone()[0]
                rows = con.execute(
                    f"""SELECT c.id, c.dict_id, d.name AS dict_name, d.archive, c.short_name, c.hash,
                               c.duration, c.frames, c.bones, m.ov AS matched_bones, m.sp AS sp_hit,
                               m.prec AS prec, c.category
                        FROM clip c
                        JOIN dict d ON d.id = c.dict_id
                        JOIN _md m ON m.dict_id = c.dict_id
                        WHERE {where_sql}
                        ORDER BY m.sp DESC,
                                 (CASE WHEN c.duration BETWEEN 0.3 AND 15.0 THEN 0 ELSE 1 END),
                                 m.prec DESC, m.ov DESC, c.frames DESC, c.short_name ASC
                        LIMIT ? OFFSET ?""",
                    [*params, limit, offset],
                ).fetchall()
                cats = con.execute(
                    """SELECT c.category, COUNT(*)
                       FROM clip c JOIN _md m ON m.dict_id = c.dict_id
                       WHERE c.duration > 0.05 AND c.frames >= 2
                       GROUP BY c.category ORDER BY COUNT(*) DESC"""
                ).fetchall()
                archs = con.execute(
                    """SELECT d.archive, COUNT(*)
                       FROM clip c JOIN dict d ON d.id = c.dict_id JOIN _md m ON m.dict_id = c.dict_id
                       WHERE c.duration > 0.05 AND c.frames >= 2
                       GROUP BY d.archive ORDER BY COUNT(*) DESC"""
                ).fetchall()

            items = [{
                "id": r["id"],
                "dictId": r["dict_id"],
                "dictName": r["dict_name"],
                "archive": r["archive"],
                "archiveLabel": ARCHIVE_LABELS.get(r["archive"], r["archive"]),
                "name": r["short_name"],
                "clipHash": f"0x{r['hash']:08x}",
                "duration": round(float(r["duration"]), 3),
                "frames": int(r["frames"]),
                "bones": int(r["bones"]),
                "matchedBones": int(r["matched_bones"]),
                "speciesMatch": bool(r["sp_hit"]),
                "category": r["category"],
            } for r in rows]
            return {
                "ready": True,
                "total": int(total),
                "items": items,
                "categories": [{"name": c, "count": n} for c, n in cats],
                "archives": [{"name": a, "label": ARCHIVE_LABELS.get(a, a), "count": n} for a, n in archs],
            }

        # Standard fast indexed path (human female/male or unfiltered)
        where = ["c.duration > 0.05", "c.frames >= 2"]
        params = []
        if category and category != "All":
            where.append("c.category = ?")
            params.append(category)
        if archive and archive != "All" and self._has_v2_schema:
            where.append("d.archive = ?")
            params.append(archive)
        if rig in ("female", "male"):
            where.append(f"{match_col} >= 10")
        elif rig == "animal":
            where.append("c.category = 'Animals'")
        if q:
            tokens = [t.strip().lower() for t in q.split() if t.strip()]
            for tok in tokens[:4]:
                where.append("(LOWER(c.short_name) LIKE ? OR LOWER(d.name) LIKE ?)")
                like = f"%{tok}%"
                params.extend([like, like])

        where_sql = " AND ".join(where)
        arch_col = "d.archive" if self._has_v2_schema else "'anim.rpf' AS archive"
        with closing(sqlite3.connect(str(self.db_path))) as con:
            con.row_factory = sqlite3.Row
            total = con.execute(
                f"SELECT COUNT(*) FROM clip c JOIN dict d ON d.id = c.dict_id WHERE {where_sql}",
                params,
            ).fetchone()[0]
            rows = con.execute(
                f"""SELECT c.id, c.dict_id, d.name AS dict_name, {arch_col}, c.short_name, c.hash,
                           c.duration, c.frames, c.bones, c.match_f, c.match_m, c.category
                    FROM clip c JOIN dict d ON d.id = c.dict_id
                    WHERE {where_sql}
                    ORDER BY (CASE WHEN c.duration BETWEEN 0.4 AND 12.0 THEN 0 ELSE 1 END),
                             {match_col} DESC, c.frames DESC, c.short_name ASC
                    LIMIT ? OFFSET ?""",
                [*params, limit, offset],
            ).fetchall()
            cats = con.execute(
                f"""SELECT c.category, COUNT(*) FROM clip c
                    WHERE c.duration > 0.05 AND c.frames >= 2
                    {"AND " + match_col + " >= 10" if rig in ("female", "male") else ""}
                    GROUP BY c.category ORDER BY COUNT(*) DESC"""
            ).fetchall()
            archs = []
            if self._has_v2_schema:
                archs = con.execute(
                    f"""SELECT d.archive, COUNT(*) FROM clip c JOIN dict d ON d.id = c.dict_id
                        WHERE c.duration > 0.05 AND c.frames >= 2
                        {"AND " + match_col + " >= 10" if rig in ("female", "male") else ""}
                        GROUP BY d.archive ORDER BY COUNT(*) DESC"""
                ).fetchall()

        items = [{
            "id": r["id"],
            "dictId": r["dict_id"],
            "dictName": r["dict_name"],
            "archive": r["archive"],
            "archiveLabel": ARCHIVE_LABELS.get(r["archive"], r["archive"]),
            "name": r["short_name"],
            "clipHash": f"0x{r['hash']:08x}",
            "duration": round(float(r["duration"]), 3),
            "frames": int(r["frames"]),
            "bones": int(r["bones"]),
            "matchedBones": int(r["match_m"] if rig == "male" else r["match_f"]),
            "category": r["category"],
        } for r in rows]
        return {
            "ready": True,
            "total": int(total),
            "items": items,
            "categories": [{"name": c, "count": n} for c, n in cats],
            "archives": [{"name": a, "label": ARCHIVE_LABELS.get(a, a), "count": n} for a, n in archs],
        }

    # -----------------------------------------------------------------------
    # Blendspaces (.motion) search & full on-demand parsing
    # -----------------------------------------------------------------------
    def search_blendspaces(
        self,
        q: str = "",
        dims: str = "",
        domain: str = "",
        rig: str = "",
        species_tokens: list[str] | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        if not self.available():
            return {"ready": False, "items": [], "total": 0, "domains": [], "dims": []}
        self._check_schema()
        if not self._has_v2_schema:
            return {"ready": False, "items": [], "total": 0, "domains": [], "dims": []}

        limit = max(1, min(200, int(limit)))
        offset = max(0, int(offset))
        where = ["1=1"]
        params: list = []

        if dims and dims != "All":
            if dims == "3+":
                where.append("b.dimensions >= 3")
            elif dims.isdigit():
                where.append("b.dimensions = ?")
                params.append(int(dims))
        if domain and domain != "All":
            where.append("b.domain = ?")
            params.append(domain)
        elif rig == "animal" and not q:
            where.append("b.domain = 'animal/creature'")

        if q:
            for tok in [t.strip().lower() for t in q.split() if t.strip()][:4]:
                where.append("(LOWER(b.short_name) LIKE ? OR LOWER(d.name) LIKE ? OR LOWER(b.axes_json) LIKE ?)")
                like = f"%{tok}%"
                params.extend([like, like, like])

        toks = [t.lower() for t in (species_tokens or []) if t and len(t) >= 3 and t.lower() not in self._BROAD_CLADE_TOKENS][:6]
        order_parts = []
        sp_params: list = []
        if toks:
            clauses = []
            for t in toks:
                clauses.append("LOWER(d.name) LIKE ? OR LOWER(b.short_name) LIKE ?")
                sp_params.extend([f"%{t}%", f"%{t}%"])
            order_parts.append(f"(CASE WHEN {' OR '.join(clauses)} THEN 0 ELSE 1 END)")
        order_parts.append("(CASE WHEN b.dimensions IN (2, 1) THEN 0 ELSE 1 END)")
        order_parts.append("b.sample_count DESC")
        order_parts.append("b.short_name ASC")
        order_sql = ", ".join(order_parts)

        where_sql = " AND ".join(where)
        with closing(sqlite3.connect(str(self.db_path))) as con:
            con.row_factory = sqlite3.Row
            total = con.execute(
                f"SELECT COUNT(*) FROM blendspace b JOIN dict d ON d.id = b.dict_id WHERE {where_sql}",
                params,
            ).fetchone()[0]
            rows = con.execute(
                f"""SELECT b.id, b.dict_id, d.name AS dict_name, d.archive, b.name, b.short_name, b.hash,
                           b.variant_type, b.dimensions, b.node_type, b.sub_count, b.sample_count,
                           b.child_count, b.axes_json, b.domain, b.category
                    FROM blendspace b JOIN dict d ON d.id = b.dict_id
                    WHERE {where_sql}
                    ORDER BY {order_sql}
                    LIMIT ? OFFSET ?""",
                [*params, *sp_params, limit, offset],
            ).fetchall()
            doms = con.execute(
                "SELECT domain, COUNT(*) FROM blendspace GROUP BY domain ORDER BY COUNT(*) DESC"
            ).fetchall()
            dim_counts = con.execute(
                "SELECT dimensions, COUNT(*) FROM blendspace GROUP BY dimensions ORDER BY dimensions ASC"
            ).fetchall()

        items = [{
            "id": r["id"],
            "dictId": r["dict_id"],
            "dictName": r["dict_name"],
            "archive": r["archive"],
            "name": r["short_name"],
            "fullName": r["name"],
            "hash": f"0x{r['hash']:08x}",
            "variantType": int(r["variant_type"]),
            "dimensions": int(r["dimensions"]),
            "nodeType": int(r["node_type"]),
            "subCount": int(r["sub_count"]),
            "sampleCount": int(r["sample_count"]),
            "childCount": int(r["child_count"]),
            "axes": json.loads(r["axes_json"]),
            "domain": r["domain"],
            "category": r["category"],
        } for r in rows]
        return {
            "ready": True,
            "total": int(total),
            "items": items,
            "domains": [{"name": d, "count": n} for d, n in doms],
            "dims": [{"dim": int(dm), "count": n} for dm, n in dim_counts],
        }

    def get_blendspace(self, bs_id: int) -> dict:
        bs_id = int(bs_id)
        with self._lock:
            hit = self._bs_cache.get(bs_id)
            if hit is not None:
                self._bs_cache.move_to_end(bs_id)
                return hit

        import blendspace as bs_mod
        with closing(sqlite3.connect(str(self.db_path))) as con:
            row = con.execute(
                """SELECT b.id, b.dict_id, b.hash, b.domain, b.category, d.name, d.hash AS dhash, d.archive
                   FROM blendspace b JOIN dict d ON d.id = b.dict_id
                   WHERE b.id=?""",
                (bs_id,),
            ).fetchone()
        if not row:
            raise LookupError(f"Unknown blendspace id {bs_id}")
        _id, dict_id, clip_hash, domain, category, dname, dhash, archive = row

        _dname2, blob = self._inflate_dict_bytes(dict_id)
        bs_sys = bs_mod.SystemPage(blob)
        target_off = None
        for key, val in bs_sys.atmap(0x48):
            if (key & 0xFFFFFFFF) == (int(clip_hash) & 0xFFFFFFFF) and bs_mod.is_motion_clip(bs_sys, val):
                target_off = val
                break
        if target_off is None:
            raise LookupError(f"Blendspace 0x{clip_hash:08x} not found in dictionary {dname}")

        parsed = bs_mod.parse_motion_clip(bs_sys, target_off, dname)
        resolver = self._get_resolver()

        def _enrich_node(node_obj: dict):
            resolved_samples = 0
            for samp in node_obj.get("samples", []):
                ch_hex = samp.get("clipHash")
                dh_hex = samp.get("dictHash")
                ch_int = int(ch_hex, 16) if ch_hex else 0
                dh_int = int(dh_hex, 16) if dh_hex else 0
                if dh_int == 0:
                    dh_int = int(dhash) & 0xFFFFFFFF
                res = resolver.resolve(ch_int, dh_int, dh_int)
                if not res["resolved"] and dh_int != (int(dhash) & 0xFFFFFFFF):
                    # Try blendspace's own dictionary as fallback
                    alt = resolver.resolve(ch_int, int(dhash) & 0xFFFFFFFF, int(dhash) & 0xFFFFFFFF)
                    if alt["resolved"]:
                        res = alt
                samp["dictId"] = res["dictId"]
                samp["dictName"] = res["dictName"]
                samp["clipName"] = res["clipName"]
                samp["clipShortName"] = res["clipShortName"] or (ch_hex or "clip")
                samp["clipDurationResolved"] = res["clipDuration"]
                samp["clipFrames"] = res["clipFrames"]
                samp["resolved"] = res["resolved"]
                if res["resolved"]:
                    resolved_samples += 1
            node_obj["resolvedSamples"] = resolved_samples
            # For 2D nodes with sparse/empty grid triangles, provide Delaunay fallback triangles for clean canvas rendering
            if node_obj.get("dimensions") == 2:
                cmap = {
                    s["sampleIndex"]: s["coords"]
                    for s in node_obj.get("samples", [])
                    if len(s.get("coords", [])) >= 2
                }
                if len(cmap) >= 3 and not node_obj.get("triangles"):
                    node_obj["triangles"] = bs_mod._delaunay_2d(cmap)
                    node_obj["trianglesFallback"] = True

        if parsed["variantType"] == 1:
            _enrich_node(parsed)
        else:
            for sm in parsed.get("subMotions", []):
                _enrich_node(sm)
            # Re-sync promoted top-level samples/triangles from primary subMotion
            primary = max(parsed["subMotions"], key=lambda sm: sm["dimensions"])
            parsed["samples"] = primary["samples"]
            parsed["triangles"] = primary["triangles"]
            parsed["resolvedSamples"] = primary["resolvedSamples"]

        parsed["id"] = bs_id
        parsed["dictId"] = int(dict_id)
        parsed["archive"] = archive
        parsed["domain"] = domain
        parsed["category"] = category

        with self._lock:
            self._bs_cache[bs_id] = parsed
            self._bs_cache.move_to_end(bs_id)
            while len(self._bs_cache) > 48:
                self._bs_cache.popitem(last=False)
        return parsed

    # -----------------------------------------------------------------------
    # Motion Matching (.motionmatcher) search & on-demand parsing
    # -----------------------------------------------------------------------
    def search_motion_matchers(
        self,
        q: str = "",
        rig_category: str = "",
        mode: str = "",
        skeleton_hashes: set[int] | None = None,
        species_tokens: list[str] | None = None,
        limit: int = 80,
        offset: int = 0,
    ) -> dict:
        if not self.available():
            return {"ready": False, "items": [], "total": 0, "categories": []}
        self._check_schema()
        if not self._has_v2_schema:
            return {"ready": False, "items": [], "total": 0, "categories": []}

        limit = max(1, min(250, int(limit)))
        offset = max(0, int(offset))
        where = ["1=1"]
        params: list = []
        if rig_category and rig_category != "All":
            where.append("m.rig_category = ?")
            params.append(rig_category.lower())
        if mode and mode.isdigit():
            where.append("m.mode = ?")
            params.append(int(mode))
        if q:
            for tok in [t.strip().lower() for t in q.split() if t.strip()][:4]:
                where.append(
                    "(LOWER(m.short_name) LIKE ? OR LOWER(d.name) LIKE ? "
                    "OR LOWER(m.rig_species) LIKE ? OR LOWER(m.rig_clade) LIKE ?)"
                )
                like = f"%{tok}%"
                params.extend([like, like, like, like])

        where_sql = " AND ".join(where)
        with closing(sqlite3.connect(str(self.db_path))) as con:
            con.row_factory = sqlite3.Row
            rows = con.execute(
                f"""SELECT m.id, m.dict_id, d.name AS dict_name, d.archive, m.name, m.short_name, m.hash,
                           m.mode, m.mode_name, m.rig_category, m.rig_domain, m.rig_clade, m.rig_species,
                           m.bone_count, m.pose_count, m.candidate_count, m.resolved_count,
                           m.interval_count, m.feature_dim, m.channel_count, m.bone_hashes
                    FROM motion_matcher m JOIN dict d ON d.id = m.dict_id
                    WHERE {where_sql}""",
                params,
            ).fetchall()
            cats = con.execute(
                "SELECT rig_category, COUNT(*) FROM motion_matcher GROUP BY rig_category ORDER BY COUNT(*) DESC"
            ).fetchall()

        skel_set = set(int(h) & 0xFFFFFFFF for h in skeleton_hashes) if skeleton_hashes else None
        toks = [t.lower() for t in (species_tokens or []) if t and len(t) >= 3]
        spec_toks = [t for t in toks if t not in self._BROAD_CLADE_TOKENS]
        broad_toks = [t for t in toks if t in self._BROAD_CLADE_TOKENS]

        scored = []
        for r in rows:
            ov = 0
            prec = 0.0
            sp_hit = 0
            if skel_set:
                bset = _unpack_u32_set(r["bone_hashes"])
                ov = len(skel_set & bset)
                prec = ov / max(1, len(bset))
            if toks:
                hay = f"{r['dict_name']} {r['rig_species']} {r['rig_clade']} {r['short_name']}".lower()
                if spec_toks and any(t in hay for t in spec_toks):
                    sp_hit = 2
                elif broad_toks and any(t in hay for t in broad_toks):
                    sp_hit = 1
            scored.append((sp_hit, prec, ov, r))

        if skel_set or toks:
            scored.sort(key=lambda x: (-x[0], -round(x[1], 3), -x[2], -int(x[3]["resolved_count"]), -int(x[3]["pose_count"]), x[3]["short_name"]))
        else:
            scored.sort(key=lambda x: (-int(x[3]["mode"]), -int(x[3]["pose_count"]), x[3]["dict_name"], x[3]["short_name"]))

        total = len(scored)
        page = scored[offset:offset + limit]
        items = [{
            "id": int(r["id"]),
            "dictId": int(r["dict_id"]),
            "dictName": r["dict_name"],
            "archive": r["archive"],
            "name": r["short_name"],
            "fullName": r["name"],
            "hash": f"0x{r['hash']:08x}",
            "mode": int(r["mode"]),
            "modeName": r["mode_name"],
            "rigCategory": r["rig_category"],
            "rigDomain": r["rig_domain"],
            "rigClade": r["rig_clade"],
            "rigSpecies": r["rig_species"],
            "boneCount": int(r["bone_count"]),
            "poseCount": int(r["pose_count"]),
            "candidateCount": int(r["candidate_count"]),
            "resolvedCount": int(r["resolved_count"]),
            "intervalCount": int(r["interval_count"]),
            "featureDim": int(r["feature_dim"]),
            "channelCount": int(r["channel_count"]),
            "matchedBones": int(ov),
            "boneOverlap": round(prec, 3),
            "speciesMatch": bool(sp_hit),
        } for sp_hit, prec, ov, r in page]

        return {
            "ready": True,
            "total": total,
            "items": items,
            "categories": [{"name": c, "count": n} for c, n in cats],
        }

    def get_motion_matcher(self, mm_id: int, include_poses: bool = False) -> dict:
        mm_id = int(mm_id)
        ckey = (mm_id, bool(include_poses))
        with self._lock:
            hit = self._mm_cache.get(ckey)
            if hit is not None:
                self._mm_cache.move_to_end(ckey)
                return hit

        import motion_matcher as mm_mod
        with closing(sqlite3.connect(str(self.db_path))) as con:
            row = con.execute(
                """SELECT m.id, m.dict_id, m.hash, d.name, d.archive
                   FROM motion_matcher m JOIN dict d ON d.id = m.dict_id
                   WHERE m.id=?""",
                (mm_id,),
            ).fetchone()
        if not row:
            raise LookupError(f"Unknown motion matcher id {mm_id}")
        _id, dict_id, clip_hash, dname, archive = row

        _dname2, blob = self._inflate_dict_bytes(dict_id)
        resolver = self._get_resolver()
        mms = mm_mod.parse_dictionary_motionmatchers(blob, resolver=resolver, include_poses=include_poses)
        target_hex = f"0x{int(clip_hash) & 0xFFFFFFFF:08x}"
        found = next((m for m in mms if m["hash"].lower() == target_hex), mms[0] if mms else None)
        if found is None:
            raise LookupError(f"Motion matcher {target_hex} not found in dictionary {dname}")

        found["id"] = mm_id
        found["dictId"] = int(dict_id)
        found["archive"] = archive

        with self._lock:
            self._mm_cache[ckey] = found
            self._mm_cache.move_to_end(ckey)
            while len(self._mm_cache) > 24:
                self._mm_cache.popitem(last=False)
        return found

    # -----------------------------------------------------------------------
    # Dictionary inflation & keyframe decoding (multi-archive + multi-layer)
    # -----------------------------------------------------------------------
    def _inflate_dict_bytes(self, dict_id: int) -> tuple[str, bytes]:
        self._check_schema()
        with closing(sqlite3.connect(str(self.db_path))) as con:
            if self._has_v2_schema:
                row = con.execute("SELECT name, segments, archive FROM dict WHERE id=?", (int(dict_id),)).fetchone()
            else:
                r0 = con.execute("SELECT name, segments FROM dict WHERE id=?", (int(dict_id),)).fetchone()
                row = (r0[0], r0[1], "anim.rpf") if r0 else None
            if not row:
                raise LookupError(f"Unknown clip dictionary id {dict_id}")
        dname, segs_json, archive = row
        segs = json.loads(segs_json)
        mm = self._mmap(archive)
        parts = []
        for off, stored, is_raw in segs:
            raw_b = mm[off:off + stored]
            parts.append(bytes(raw_b) if is_raw else zlib.decompressobj().decompress(raw_b))
        return dname, b"".join(parts)

    @staticmethod
    def _resample_frames(arr: np.ndarray, target_frames: int, is_quat: bool = False) -> np.ndarray:
        src_n = len(arr)
        if src_n == target_frames:
            return arr
        if target_frames <= 1 or src_n <= 1:
            return np.repeat(arr[0:1], max(1, target_frames), axis=0).astype("<f4")
        t = np.linspace(0.0, src_n - 1, target_frames, dtype=np.float64)
        i0 = np.floor(t).astype(np.int64)
        i1 = np.minimum(src_n - 1, i0 + 1)
        a = (t - i0)[:, None]
        if is_quat:
            q0 = arr[i0].astype(np.float64)
            q1 = arr[i1].astype(np.float64)
            dots = np.sum(q0 * q1, axis=1, keepdims=True)
            q1 = np.where(dots < 0.0, -q1, q1)
            q = (1.0 - a) * q0 + a * q1
            norms = np.linalg.norm(q, axis=1, keepdims=True)
            q = q / np.where(norms > 1e-8, norms, 1.0)
            for fi in range(1, len(q)):
                if float(np.dot(q[fi - 1], q[fi])) < 0.0:
                    q[fi] = -q[fi]
            return q.astype("<f4")
        return ((1.0 - a) * arr[i0] + a * arr[i1]).astype("<f4")

    def decode_clip_tracks(self, dict_id: int, clip_hash: int, skeleton_joints: list[dict]) -> dict:
        """Decode one clip (across all archives & animation layers) and map tracks onto skeleton_joints."""
        cache_key = (int(dict_id), int(clip_hash), len(skeleton_joints), skeleton_joints[0]["hash"] if skeleton_joints else 0)
        with self._lock:
            cached = self._clip_cache.get(cache_key)
            if cached is not None:
                self._clip_cache.move_to_end(cache_key)
                return cached

        dname, blob = self._inflate_dict_bytes(dict_id)
        sys_buf = System(blob)
        d_parsed = parse_dictionary(sys_buf)

        clip = next((c for c in d_parsed["clips"] if c["hash"] == int(clip_hash) and c["kind"] == "clip"), None)
        if clip is None:
            raise LookupError(f"Clip hash 0x{clip_hash:08x} not found in dictionary {dname}")

        local_pairs = []
        for aref in clip["animations"]:
            aoff = aref.get("animation")
            if aoff is not None and aoff in d_parsed["anims_by_off"]:
                local_pairs.append((d_parsed["anims_by_off"][aoff], aref))
        if not local_pairs:
            raise LookupError(f"Clip {clip['name']} has no local animation payload")

        # Primary layer = local animation with most tracks & frames
        local_pairs.sort(key=lambda pair: (len(pair[0]["tracks"]), pair[0]["frames"]), reverse=True)
        anim_rec, anim_ref = local_pairs[0]

        decoded_tracks, total_frames = decode_animation(sys_buf, anim_rec)
        if total_frames < 1:
            raise DecodeError("animation decoded 0 frames")

        anim_dur = float(anim_rec["duration"]) if anim_rec["duration"] > 0 else max(0.033, (total_frames - 1) / 30.0)
        anim_fps = (total_frames - 1) / anim_dur if (total_frames > 1 and anim_dur > 1e-6) else 30.0
        rate = float(anim_ref["rate"]) if (anim_ref and math.isfinite(anim_ref["rate"]) and anim_ref["rate"] > 1e-4) else 1.0
        t_start = float(anim_ref["start"]) if (anim_ref and math.isfinite(anim_ref["start"]) and anim_ref["start"] >= 0.0) else 0.0
        t_end = float(anim_ref["end"]) if (anim_ref and math.isfinite(anim_ref["end"]) and anim_ref["end"] > t_start) else anim_dur
        t_start = max(0.0, min(anim_dur, t_start))
        t_end = max(t_start, min(anim_dur, t_end))

        f_start = max(0, min(total_frames - 1, int(round(t_start * anim_fps))))
        f_end = max(f_start, min(total_frames - 1, int(round(t_end * anim_fps))))
        sliced_frames = f_end - f_start + 1

        span = (t_end - t_start) / rate
        clip_dur = span if span > 1e-4 else (float(clip["duration"]) if clip["duration"] > 0 else max(0.033, (sliced_frames - 1) / 30.0))
        if not math.isfinite(clip_dur) or clip_dur <= 0:
            clip_dur = max(0.033, (sliced_frames - 1) / 30.0)
        fps = (sliced_frames - 1) / clip_dur if sliced_frames > 1 else 30.0

        joint_by_hash: dict[int, int] = {}
        for j in skeleton_joints:
            joint_by_hash.setdefault(int(j["hash"]), int(j["index"]))
            joint_by_hash.setdefault(joaat32(j["name"]), int(j["index"]))

        rot_tracks: dict[int, np.ndarray] = {}
        trans_tracks: dict[int, np.ndarray] = {}
        scale_tracks: dict[int, np.ndarray] = {}
        mover_pos: np.ndarray | None = None
        mover_rot: np.ndarray | None = None
        matched_bones = set()
        total_tracks_all = 0

        def _ingest_layer(arec, aref, d_tracks, t_frames, is_primary: bool):
            nonlocal mover_pos, mover_rot, total_tracks_all
            total_tracks_all += len(arec["tracks"])
            if is_primary:
                fs, fe = f_start, f_end
            else:
                ldur = float(arec["duration"]) if arec["duration"] > 0 else max(0.033, (t_frames - 1) / 30.0)
                lfps = (t_frames - 1) / ldur if (t_frames > 1 and ldur > 1e-6) else 30.0
                ls = float(aref["start"]) if (aref and math.isfinite(aref["start"]) and aref["start"] >= 0.0) else 0.0
                le = float(aref["end"]) if (aref and math.isfinite(aref["end"]) and aref["end"] > ls) else ldur
                ls = max(0.0, min(ldur, ls))
                le = max(ls, min(ldur, le))
                fs = max(0, min(t_frames - 1, int(round(ls * lfps))))
                fe = max(fs, min(t_frames - 1, int(round(le * lfps))))

            for tr_idx, (bone_hash, ttype, _fmt) in enumerate(arec["tracks"]):
                frames_all = d_tracks.get(tr_idx)
                if not frames_all:
                    continue
                frames = frames_all[fs:fe + 1]
                if not frames:
                    continue
                if bone_hash == 0:
                    if is_primary:
                        if ttype == TRACK_TRANS and len(frames[0]) >= 3:
                            mp = np.array([f[:3] for f in frames], dtype="<f4")
                            mover_pos = mp - mp[0:1]
                        elif ttype == TRACK_ROT and len(frames[0]) >= 4:
                            mr = np.array([f[:4] for f in frames], dtype="<f4")
                            for fi in range(1, len(mr)):
                                if float(np.dot(mr[fi - 1], mr[fi])) < 0.0:
                                    mr[fi] = -mr[fi]
                            mover_rot = mr
                    continue
                jidx = joint_by_hash.get(int(bone_hash))
                if jidx is None:
                    continue
                if ttype == TRACK_ROT and len(frames[0]) >= 4:
                    if not is_primary and jidx in rot_tracks:
                        continue
                    arr = np.array([f[:4] for f in frames], dtype="<f4")
                    for fi in range(1, len(arr)):
                        if float(np.dot(arr[fi - 1], arr[fi])) < 0.0:
                            arr[fi] = -arr[fi]
                    if len(arr) != sliced_frames:
                        arr = self._resample_frames(arr, sliced_frames, is_quat=True)
                    rot_tracks[jidx] = arr
                    matched_bones.add(jidx)
                elif ttype == TRACK_TRANS and len(frames[0]) >= 3:
                    if not is_primary and jidx in trans_tracks:
                        continue
                    arr = np.array([f[:3] for f in frames], dtype="<f4")
                    if len(arr) != sliced_frames:
                        arr = self._resample_frames(arr, sliced_frames, is_quat=False)
                    trans_tracks[jidx] = arr
                    matched_bones.add(jidx)
                elif ttype == TRACK_SCALE and len(frames[0]) >= 3:
                    if not is_primary and jidx in scale_tracks:
                        continue
                    arr = np.array([f[:3] for f in frames], dtype="<f4")
                    if len(arr) != sliced_frames:
                        arr = self._resample_frames(arr, sliced_frames, is_quat=False)
                    scale_tracks[jidx] = arr
                    matched_bones.add(jidx)

        _ingest_layer(anim_rec, anim_ref, decoded_tracks, total_frames, is_primary=True)
        for sec_rec, sec_ref in local_pairs[1:]:
            try:
                sec_tracks, sec_frames = decode_animation(sys_buf, sec_rec)
                if sec_frames >= 1:
                    _ingest_layer(sec_rec, sec_ref, sec_tracks, sec_frames, is_primary=False)
            except Exception:
                continue

        result = {
            "dictId": int(dict_id),
            "dictName": dname,
            "name": _short_clip_name(clip["name"], clip["hash"]),
            "clipHash": f"0x{clip['hash']:08x}",
            "duration": round(clip_dur, 5),
            "frames": int(sliced_frames),
            "fps": round(fps, 3),
            "totalTracks": total_tracks_all,
            "matchedBones": len(matched_bones),
            "rotTracks": rot_tracks,
            "transTracks": trans_tracks,
            "scaleTracks": scale_tracks,
            "moverPos": mover_pos,
            "moverRot": mover_rot,
        }
        with self._lock:
            self._clip_cache[cache_key] = result
            self._clip_cache.move_to_end(cache_key)
            while len(self._clip_cache) > 64:
                self._clip_cache.popitem(last=False)
        return result

    def pack_anim_binary(self, decoded: dict) -> bytes:
        """Pack decoded clip tracks into compact binary for WebGL2.

        Format: [u32 header_len][utf8 json header (4B padded)][Float32Array payload blocks].
        """
        chunks = []
        byte_off = 0

        def _add_block(arr: np.ndarray) -> int:
            nonlocal byte_off
            b = np.ascontiguousarray(arr, dtype="<f4").tobytes()
            off = byte_off
            chunks.append(b)
            byte_off += len(b)
            return off

        rot_meta = []
        for jidx in sorted(decoded["rotTracks"]):
            rot_meta.append([int(jidx), _add_block(decoded["rotTracks"][jidx])])
        trans_meta = []
        for jidx in sorted(decoded["transTracks"]):
            trans_meta.append([int(jidx), _add_block(decoded["transTracks"][jidx])])
        scale_meta = []
        for jidx in sorted(decoded["scaleTracks"]):
            scale_meta.append([int(jidx), _add_block(decoded["scaleTracks"][jidx])])

        mover_pos_off = _add_block(decoded["moverPos"]) if decoded["moverPos"] is not None else -1
        mover_rot_off = _add_block(decoded["moverRot"]) if decoded["moverRot"] is not None else -1

        header = {
            "dictId": decoded["dictId"],
            "dictName": decoded["dictName"],
            "name": decoded["name"],
            "clipHash": decoded["clipHash"],
            "duration": decoded["duration"],
            "frames": decoded["frames"],
            "fps": decoded["fps"],
            "totalTracks": decoded["totalTracks"],
            "matchedBones": decoded["matchedBones"],
            "rot": rot_meta,
            "trans": trans_meta,
            "scale": scale_meta,
            "moverPosOffset": mover_pos_off,
            "moverRotOffset": mover_rot_off,
        }
        hb = json.dumps(header, separators=(",", ":")).encode("utf-8")
        hb += b" " * ((-len(hb)) % 4)
        return struct.pack("<I", len(hb)) + hb + b"".join(chunks)


if __name__ == "__main__":
    def _prog(done, total, nd, nc, nbs, nmm):
        if done % 10 == 0 or done == total:
            print(f"[{done}/{total}] {nd} dicts | {nc} clips | {nbs} blendspaces | {nmm} motion matchers", flush=True)
    res = build_index(progress_cb=_prog)
    print("Built animation index:", json.dumps(res, indent=2))
