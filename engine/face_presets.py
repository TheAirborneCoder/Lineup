# SPDX-License-Identifier: GPL-3.0-or-later
"""Match character drawables to face/texture presets from the game's appearance catalogues (packs.rpf, 2026-11-22).

Sources (both use the same 0x8dcb4cf5 appearance-catalogue schema):
- Standalone PSIN PSO: packs.rpf/00011ccb2000.pso (player/shop wardrobe catalogue, 19,827 presets).
- Resource-packed 0DRP files inside packs.rpf: 2,709 compiled ped catalogues (one per ped archetype,
  indexed in INDEX/packs.rpf/deflate-spans.bin). Each 0DRP begins with a standard RSC8
  header (0x50000000 virtual base), magic b'0DRP' at +0x10, root block index at +0x20, block table
  pointer at +0x38, and block count (u16) at +0x4c; internal block/offset pointers use the same
  (block = p & 0xfffff, offset = p >> 20) encoding as PSIN PSOs.

Preset layout:
- Entries of type 0xf795e8ba (88 bytes): id (+0), parent_id (+4), state count (+8), state ptr (+16)
  -> 184-byte 0x37e52277 records (drawable murmur64 h0 at +0x08, albedo at +0x10, normal at +0x18),
  category (+24; 0x378ad10c = head), and tags array (+48 ptr, +56 count u16) -> 24-byte tag map
  records (synthetic block id 0x100..0x120 depending on schema order; u32 key, pad, u32 count,
  u32 cap, u64 ptr -> u32 values in block 0x4a).

Matching (all direct game data; nothing inferred or borrowed):
1. 'drawable tag': preset (or state) tags 0x4bfd27dd / 0x1c114e61 directly name a drawable (joaat32).
2. 'drawable tag (child)': preset has no drawable tag of its own, but an ancestor via parent_id
   within the same catalogue directly names a drawable (used by e.g. hc_* heads whose parent preset
   has zero hashes and whose child preset holds the bespoke face albedo and normal).
3. 'state drawable': head-category (0x378ad10c) preset with no drawable tag whose state h0 directly
   names a head drawable (murmur64; e.g. base heads, demon heads, formaldehyde creature heads).

Writes LIBRARY/face_presets.sqlite3: match(model, preset, albedo, normal, how), deduplicated
per (model, albedo, normal, how) and ordered best-first (direct > child > state drawable, local
highest-resolution sRGB albedo first, then local normal).

    python engine/face_presets.py"""
import collections
from concurrent.futures import ThreadPoolExecutor
import os
import sqlite3
import struct
import zlib

from decode_pso import Decoder, sections_of  # noqa: E402
from script_symbol_names import murmur64  # noqa: E402
from script_tree import _name_hash  # noqa: E402
from paths import GAME, INDEX, LIBRARY

SYS = 0x50000000
CATALOG_ROOT = 0x8DCB4CF5
HEAD_CATEGORY = 0x378AD10C
BODY_SKIN_CATEGORIES = {0x3817131D, 0x44925BFD, 0x9D283797}  # hand_* (torso/arms), feet_* (legs/feet), hand_*_fp
DRAWABLE_TAGS = (0x4BFD27DD, 0x1C114E61)

PRESETS_PSO = INDEX / "packs.rpf/00011ccb2000.pso"
PACKS_RPF = GAME / "packs.rpf"
PACKS_SPANS = INDEX / "packs.rpf/deflate-spans.bin"


def ptr(p):
    return p & 0xFFFFF, p >> 20


def read_tags(blocks, p2, c2):
    tb, to = ptr(p2)
    tags = {}
    if not c2 or tb not in blocks:
        return tags
    bk, tdata = blocks[tb]
    if not (0x100 <= bk <= 0x120):
        return tags
    for j in range(c2):
        if to + 24 * j + 24 > len(tdata):
            break
        tk, tc, _tcap, tpp = struct.unpack_from("<IxxxxIIQ", tdata, to + 24 * j)
        if tc and tc <= 64:
            ub, uo = ptr(tpp)
            if ub in blocks and blocks[ub][0] == 0x4A:
                udata = blocks[ub][1]
                tags[tk] = [
                    struct.unpack_from("<I", udata, uo + 4 * q)[0]
                    for q in range(tc)
                    if uo + 4 * q + 4 <= len(udata)
                ]
    return tags


def parse_0drp_blocks(buf):
    if len(buf) < 0x50 or buf[16:20] != b"0DRP":
        return None, None
    rblk = struct.unpack_from("<I", buf, 0x20)[0]
    bp = struct.unpack_from("<Q", buf, 0x38)[0] - SYS
    nb = struct.unpack_from("<H", buf, 0x4C)[0]
    if not (0 < nb <= 4096) or not (0 <= bp and bp + nb * 16 <= len(buf)):
        return None, None
    blocks = {}
    view = memoryview(buf)
    for i in range(nb):
        key, size, bptr = struct.unpack_from("<IIQ", buf, bp + i * 16)
        boff = bptr - SYS
        if 0 <= boff and boff + size <= len(buf):
            blocks[i + 1] = (key, view[boff:boff + size])
    return (blocks[rblk][0] if rblk in blocks else 0), blocks


def extract_matches(blocks, source_label, j32_models, m64_models):
    presets = []
    by_id = {}
    for bnum, (bk, data) in blocks.items():
        if bk != 0xF795E8BA:
            continue
        for idx in range(len(data) // 88):
            r = data[idx * 88:(idx + 1) * 88]
            pid, parent_id = struct.unpack_from("<II", r, 0)
            c1 = struct.unpack_from("<I", r, 8)[0]
            p1 = struct.unpack_from("<Q", r, 16)[0]
            cat = struct.unpack_from("<I", r, 24)[0]
            p2 = struct.unpack_from("<Q", r, 48)[0]
            c2 = struct.unpack_from("<H", r, 56)[0]
            tags = read_tags(blocks, p2, c2)
            rb, ro = ptr(p1)
            states = []
            if c1 and rb in blocks and blocks[rb][0] == 0x37E52277:
                rdata = blocks[rb][1]
                for si in range(c1):
                    if ro + (si + 1) * 184 > len(rdata):
                        break
                    h0, alb, nrm, _h3 = struct.unpack_from("<QQQQ", rdata, ro + si * 184 + 8)
                    sp2 = struct.unpack_from("<Q", rdata, ro + si * 184 + 168)[0]
                    sc2 = struct.unpack_from("<H", rdata, ro + si * 184 + 176)[0]
                    stags = read_tags(blocks, sp2, sc2) if sc2 else {}
                    states.append((h0, alb, nrm, stags))
            entry = (f"{source_label}:{bnum}:{idx}", pid, parent_id, cat, tags, states)
            presets.append(entry)
            by_id[pid] = entry

    out = []
    for preset_id, _pid, parent_id, cat, tags, states in presets:
        dmodels = {
            m
            for tk in DRAWABLE_TAGS
            for v in tags.get(tk, ())
            for m in j32_models.get(v, ())
        }
        for h0, alb, nrm, stags in states:
            if not alb and not nrm:
                continue
            smodels = set(dmodels)
            for tk in DRAWABLE_TAGS:
                for v in stags.get(tk, ()):
                    smodels.update(j32_models.get(v, ()))
            if smodels:
                for m in smodels:
                    out.append((m, preset_id, alb, nrm, "drawable tag"))
                continue
            # Check ancestor preset via parent_id within the same catalogue.
            cur, depth, matched_child = parent_id, 0, False
            while cur and cur in by_id and depth < 5:
                _, _, ppar, _, ptags, _ = by_id[cur]
                pmodels = {
                    m
                    for tk in DRAWABLE_TAGS
                    for v in ptags.get(tk, ())
                    for m in j32_models.get(v, ())
                }
                if pmodels:
                    for m in pmodels:
                        out.append((m, preset_id, alb, nrm, "drawable tag (child)"))
                    matched_child = True
                    break
                cur = ppar
                depth += 1
            if matched_child:
                continue
            # Any preset whose state h0 directly names a drawable (murmur64) in the model inventory.
            if h0 in m64_models:
                for m in m64_models[h0]:
                    out.append((m, preset_id, alb, nrm, "state drawable"))
    return out


def scan_pso(j32_models, m64_models):
    s, _ = sections_of(PRESETS_PSO.read_bytes())
    d = Decoder(s, {})
    blocks = {i + 1: (k, data) for i, (k, data, _) in enumerate(d.blocks)}
    return extract_matches(blocks, "00011ccb2000", j32_models, m64_models)


def scan_packs_0drp(j32_models, m64_models):
    spans = PACKS_SPANS.read_bytes()
    rows = [struct.unpack_from("<QQQII", spans, i * 32) for i in range(len(spans) // 32)]
    n = len(rows)

    def worker(chunk_idx):
        lo = chunk_idx * 4000
        hi = min(n, lo + 4000)
        hits = []
        catalogs = 0
        with open(PACKS_RPF, "rb") as f:
            i = lo
            while i < hi:
                off, clen, _ilen, _a, _b = rows[i]
                f.seek(off)
                try:
                    head = zlib.decompressobj().decompress(f.read(min(clen, 128)), 32)
                except Exception:
                    i += 1
                    continue
                if len(head) < 24 or head[16:20] != b"0DRP":
                    i += 1
                    continue
                f.seek(off)
                buf = bytearray(zlib.decompress(f.read(clen)))
                j = i
                while (
                    j + 1 < n
                    and ((rows[j][0] + rows[j][1] + 15) & -16) == rows[j + 1][0]
                    and len(buf) % 65536 == 0
                ):
                    j += 1
                    f.seek(rows[j][0])
                    buf.extend(zlib.decompress(f.read(rows[j][1])))
                rk, blocks = parse_0drp_blocks(buf)
                if rk == CATALOG_ROOT and blocks:
                    catalogs += 1
                    hits.extend(extract_matches(blocks, f"0x{off:x}", j32_models, m64_models))
                i = j + 1
        return catalogs, hits

    all_hits = []
    total_catalogs = 0
    with ThreadPoolExecutor(max_workers=8) as ex:
        for cats, hits in ex.map(worker, range((n + 3999) // 4000)):
            total_catalogs += cats
            all_hits.extend(hits)
    return total_catalogs, all_hits


def main():
    inv = sqlite3.connect(LIBRARY / "inventory.sqlite3")
    tx = sqlite3.connect(LIBRARY / "textures.sqlite3")
    tex_info = {
        int(h, 16): (w, hh, fmt)
        for h, w, hh, fmt in tx.execute("select hash, width, height, format from texture")
    }
    models = [
        r[0]
        for r in inv.execute(
            "select distinct name from model where name is not null and "
            "category in ('Characters','Protagonists','Clothing & accessories','Animals')"
        )
    ]
    j32_models = collections.defaultdict(list)
    m64_models = collections.defaultdict(list)
    for m in models:
        j32_models[_name_hash(m) & 0xFFFFFFFF].append(m)
        m64_models[murmur64(m.encode())].append(m)

    pso_hits = scan_pso(j32_models, m64_models)
    n_0drp, drp_hits = scan_packs_0drp(j32_models, m64_models)

    # Deduplicate per (model, albedo, normal, how), preferring the standalone PSO id if present,
    # and skipping tiny <32px fur/groom proxy textures from state-drawable matches.
    dedup = {}
    for m, preset_id, alb, nrm, how in pso_hits + drp_hits:
        if how == "state drawable":
            ai = tex_info.get(alb)
            ni = tex_info.get(nrm)
            a_ok = ai and max(ai[0], ai[1]) >= 32
            n_ok = ni and max(ni[0], ni[1]) >= 32
            if not a_ok and not n_ok:
                continue
        dedup.setdefault((m, alb, nrm, how), preset_id)

    how_rank = {"drawable tag": 0, "drawable tag (child)": 1, "state drawable": 2}

    def sort_key(item):
        (m, alb, nrm, how), preset_id = item
        ai = tex_info.get(alb)
        ni = tex_info.get(nrm)
        return (
            m,
            how_rank.get(how, 9),
            0 if ai else 1,
            -(ai[0] * ai[1]) if ai else 0,
            0 if (ai and "SRGB" in ai[2]) else 1,
            0 if ni else 1,
            -(ni[0] * ni[1]) if ni else 0,
            0 if preset_id.startswith("00011ccb2000:") else 1,
            f"{alb:016x}",
            f"{nrm:016x}",
            preset_id,
        )

    rows = [
        (m, preset_id, f"{alb:016x}", f"{nrm:016x}", how)
        for (m, alb, nrm, how), preset_id in sorted(dedup.items(), key=sort_key)
    ]

    out = LIBRARY / "face_presets.sqlite3"
    tmp = out.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    db = sqlite3.connect(tmp)
    db.execute("create table match(model text, preset text, albedo text, normal text, how text)")
    db.execute("create index idx_match_model on match(model)")
    db.executemany("insert into match values (?,?,?,?,?)", rows)
    db.commit()
    db.close()
    os.replace(tmp, out)

    by_model = collections.defaultdict(list)
    for m, _pid, alb, nrm, how in rows:
        by_model[m].append((int(alb, 16), int(nrm, 16), how))
    local_alb = sum(1 for vs in by_model.values() if any(a in tex_info for a, _, _ in vs))
    local_1024 = sum(1 for vs in by_model.values() if any(a in tex_info and tex_info[a][0] >= 512 for a, _, _ in vs))
    local_nrm = sum(1 for vs in by_model.values() if any(n in tex_info for _, n, _ in vs))
    how_best = collections.Counter(vs[0][2] for vs in by_model.values())
    print(
        f"sources: 00011ccb2000.pso + {n_0drp:,} packs.rpf 0DRP catalogues | "
        f"models matched: {len(by_model):,} ({len(rows):,} unique preset rows) | "
        f"local albedo: {local_alb:,} (>=512: {local_1024:,}) | local normal: {local_nrm:,} | "
        f"by how: {dict(how_best)}",
        flush=True,
    )


if __name__ == "__main__":
    main()
