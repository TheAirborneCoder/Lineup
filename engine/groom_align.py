# SPDX-License-Identifier: GPL-3.0-or-later
"""Second stage of the groom name link: hair items and story characters, where an item owns several grooms.

groom_names.py calls link(). The block between the proven chops and lashes families holds, in name order,
the animals, the cs_* story characters, genitalhairfull_* and hair*. For the part from the first cs_ groom on:

  * each groom gets the set of (region, rig) labels it can be, from its guide roots and its skinning bones;
  * each item has one label from its name;
  * an alignment gives every item one or more consecutive grooms that can carry its label, in order;
  * a groom is named only when *every* alignment gives it to the same item. Otherwise it gets a hint with
    the items it can still belong to.

Split-chain rule: some assets are stored between another asset's LOD 0 and its later LODs. Entries are in name
order, so the asset in between has a name that starts with the outer asset's name, and both belong to the same
item: no item boundary may fall between them. The rule assumes that the LODs of one asset share its name as a
prefix. Nothing contradicts it: no pair lies in a family with one groom per item, no pair crosses a boundary the
labels fix, and where both grooms of a pair were already named they have the same item. A name that needs the
rule says so in its evidence.

Labels are permissive on purpose. A label is removed from a groom only on clear evidence, because a wrongly
removed label can force a wrong name, while a kept one only leaves more grooms open:
  region  s scalp, f face, e eye (brows, lashes), b body or fur, k child head, i infant head
  rig     M or F, from the fit of the roots to the adult head spheres. Story characters have their own
          heights (cs_roxy's hair sits at male head height), so their scalp items accept either rig.
  bones   bone names that occur in the 182 beard grooms but in no groom that reaches behind the ears on a
          head sphere mark a groom as face; bones with the opposite record mark it as scalp.
A geometric "front of the face, so it is a beard" rule was tried and dropped: it made the hair block
impossible to align (a female fringe sits where a male face would be).
"""
import bisect
import collections
import re
import sqlite3
import struct

import numpy as np

import smh_groom  # noqa: E402
from paths import LIBRARY

ARCHIVE = "packs.rpf"
DB = LIBRARY / "grooms.sqlite3"
HEAD = {"M": (np.array([0.0, 0.0, 0.721]), 0.091), "F": (np.array([0.0, -0.011, 0.624]), 0.087)}
EYE = {"M": 0.720, "F": 0.620}
HEAD_LABELS = {"sM", "sF", "fM", "eM", "eF"}
ALL_LABELS = HEAD_LABELS | {"b", "k", "i"}
BASIS = "archive order; every alignment consistent with region, rig and bone labels gives this item"
BASIS_SPLIT = BASIS + ", given that an asset stored inside another asset's LOD chain belongs to the same item"
PAGE_AT, PAGE_END = 0xB8, 0x150     # part of each system page kept for groom_likely.py (hashes, style fields)


def features(assets):
    """Root statistics, head-sphere residuals and skinning bone hashes for each groom."""
    src = smh_groom.open_archive(ARCHIVE)
    rows = []
    for a in assets:
        groom = smh_groom.read(src, a["offset"], ARCHIVE)
        roots = groom.position[:, 0, :].astype(np.float64)
        row = dict(a, lo=roots.min(0), hi=roots.max(0), mean=roots.mean(0))
        for rig, (centre, radius) in HEAD.items():
            row["res" + rig] = float(np.abs(np.linalg.norm(roots - centre, axis=1) - radius).mean())
        n = struct.unpack_from("<I", groom.system, 0x98)[0]
        at = struct.unpack_from("<Q", groom.system, 0xA0)[0] - smh_groom.SYS_BASE
        row["bones"] = set(struct.unpack_from(f"<{n}I", groom.system, at)) if 0 < n <= 256 and 0 < at < len(groom.system) else set()
        row["page"] = groom.system[PAGE_AT:PAGE_END].ljust(PAGE_END - PAGE_AT, bytes(1))
        rows.append(row)
    return rows


def geometry_labels(f):
    lo, hi, mean = f["lo"], f["hi"], f["mean"]
    if hi[2] < 0.30:
        return {"i" if hi[2] < -0.2 else "k", "b"}   # infant or child head, or lower body
    if lo[2] < 0.42 or max(-lo[0], hi[0]) > 0.16:
        return {"b"}                                 # reaches below the neck or is wider than a head
    out = set()
    small = hi[2] - lo[2] <= 0.06 and max(-lo[0], hi[0]) <= 0.085 and hi[1] <= -0.05 and f["longest"] <= 0.04
    for rig in "MF":
        if small and abs((lo[2] + hi[2]) / 2 - EYE[rig]) <= 0.035:
            out.add("e" + rig)
    if f["resM"] < 0.022:
        out.add("sM")
    if hi[1] <= 0.012 and mean[1] <= -0.035 and hi[2] <= 0.74 and lo[2] >= 0.49:
        out.add("fM")                                # never behind the ears, below the temple
    if f["resF"] < 0.022 and hi[2] <= 0.725:
        out.add("sF")
    return out or set(HEAD_LABELS)


def bone_sets(rows, beard_ords):
    """(face-only, scalp-only) bone hashes. Scalp evidence: a groom on a head sphere that reaches behind the ears."""
    beard, scalp = set(), set()
    for f in rows:
        if f["ord"] in beard_ords:
            beard |= f["bones"]
        elif f["hi"][1] > 0.03 and f["lo"][2] > 0.45 and min(f["resM"], f["resF"]) < 0.016:
            scalp |= f["bones"]
    return beard - scalp, scalp - beard


def groom_labels(f, face_bones, scalp_bones):
    labels = geometry_labels(f)
    if labels & HEAD_LABELS:
        face, scalp = bool(f["bones"] & face_bones), bool(f["bones"] & scalp_bones)
        trimmed = set(labels)
        if face and not scalp:
            trimmed -= {"sM", "sF"}
        elif scalp and not face:
            trimmed -= {"fM", "eM", "eF"}
        labels = trimmed or labels
    return labels


def item_labels(name):
    rig = re.search(r"_([fm])([a-z]{1,2})1_", name)
    kind = re.search(r"(?:^|_)(hair|wigs|beard|chin|chops|mustache|stubble|brows|eyebrows|lashes|bodyhair|genitalhair)",
                     name)
    if not rig or not kind:
        return set(ALL_LABELS)
    if rig.group(2) in ("k", "i"):
        return {rig.group(2)}
    sex, kind = rig.group(1).upper(), kind.group(1)
    if "hairaccessories" in name:
        return set(ALL_LABELS)
    story = name.startswith("cs_")
    if kind in ("hair", "wigs"):
        return {"sM", "sF"} if story else {"s" + sex}
    if kind in ("beard", "chin", "chops", "mustache", "stubble"):
        return {"fM"}
    if kind in ("brows", "eyebrows", "lashes"):
        return {"eM", "eF"} if story else {"e" + sex}
    return {"b"}


def split_inside(archive=ARCHIVE):
    """ord of an asset stored inside another asset's LOD chain -> ord of that outer asset."""
    with sqlite3.connect(DB) as db:
        assets = db.execute("select id, ord, offset from asset where archive=? order by ord", (archive,)).fetchall()
        lods = collections.defaultdict(list)
        for a, off in db.execute("select asset, offset from lod where archive=? order by asset, lod", (archive,)):
            lods[a].append(off)
    first = [a[2] for a in assets]
    inside = {}
    for ident, order, _ in assets:
        offs = lods[ident]
        for k in range(bisect.bisect_right(first, offs[0]), bisect.bisect_left(first, offs[-1])):
            inside[assets[k][1]] = order
    return inside


def boundaries(rows, inside):
    """(allowed, pairs): allowed[k] is False where an item boundary before groom k would part a split pair."""
    pos = {f["ord"]: k for k, f in enumerate(rows)}
    allowed = np.ones(len(rows) + 1, bool)
    pairs = 0
    for inner, outer in inside.items():
        if inner in pos and outer in pos:
            allowed[pos[outer] + 1:pos[inner] + 1] = False
            pairs += 1
    return allowed, pairs


def align(item_sets, groom_sets, optional=(), allowed=None):
    """possible[i, a]: item i owns groom a in at least one alignment. optional items may own no groom.

    allowed[k], when given, says whether an item boundary may sit before groom k (k = 0..m).
    """
    n, m = len(item_sets), len(groom_sets)
    free = np.ones(m + 1, bool) if allowed is None else np.asarray(allowed, bool)
    ok = np.array([[bool(it & gs) for gs in groom_sets] for it in item_sets])
    opt = np.zeros(n, bool)
    opt[list(optional)] = True
    idx = np.arange(m + 1)

    def run_start(row):
        start = np.zeros(m + 1, np.int64)
        start[1:] = np.maximum.accumulate(np.where(~row, np.arange(m), -1)) + 1
        return start

    def step(row, state):
        pre = np.concatenate(([0], np.cumsum(state)))
        take = (pre[idx] - pre[np.minimum(run_start(row), idx)]) > 0
        take[0] = False
        return take

    fwd = np.zeros((n + 1, m + 1), bool)
    fwd[0, 0] = True
    for i in range(n):
        fwd[i + 1] = (step(ok[i], fwd[i]) | (fwd[i] if opt[i] else False)) & free
    bwd = np.zeros((n + 1, m + 1), bool)
    bwd[n, m] = True
    for i in range(n - 1, -1, -1):
        bwd[i] = (step(ok[i][::-1], bwd[i + 1][::-1])[::-1] | (bwd[i + 1] if opt[i] else False)) & free
    possible = np.zeros((n, m), bool)
    if fwd[n, m]:
        pos = np.arange(m)
        for i in range(n):
            row = ok[i]
            pre = np.concatenate(([0], np.cumsum(fwd[i])))
            left = (pre[pos + 1] - pre[run_start(row)[1:]]) > 0
            back = bwd[i + 1][::-1]
            preb = np.concatenate(([0], np.cumsum(back)))
            right = ((preb[pos + 1] - preb[run_start(row[::-1])[1:]]) > 0)[::-1]
            possible[i] = row & left & right
    return bool(fwd[n, m]), possible


def link(items, assets, names, hints, context=None):
    """Add proven names and candidate hints for the cs_*, genitalhairfull_* and hair* grooms. Returns report lines.

    context, a dict, receives what groom_likely.py needs: seq, sets, optional, rows, labels and possible.
    """
    by_name = {v[0]: assets_id for assets_id, v in names.items()}
    order = {a["id"]: k for k, a in enumerate(assets)}
    chops = [order[i] for n, i in by_name.items() if n.startswith("chopsmutton_")]
    lashes = [order[i] for i, v in names.items() if v[0].startswith("lashes")]
    if not chops or not lashes:
        return ["second stage skipped: the chops and lashes anchors are not linked"]
    rows = features(assets[max(chops) + 1:min(lashes)])
    base = max(chops) + 1
    plain = [geometry_labels(f) for f in rows]
    head = np.array([bool(s & HEAD_LABELS) for s in plain])
    # The story block starts where head-sized grooms begin for good: ten in a row after the animals.
    start = next((k for k in range(len(rows) - 10) if head[k:k + 10].all()), None)
    if start is None:
        return ["second stage skipped: no run of head grooms after the animals"]
    beard_ords = {a["ord"] for a in assets if "beard* items" in (hints.get(a["id"]) or "")}
    all_rows = features([a for a in assets if a["ord"] in beard_ords]) + rows
    face_bones, scalp_bones = bone_sets(all_rows, beard_ords)
    rows = rows[start:]
    grooms = [groom_labels(f, face_bones, scalp_bones) for f in rows]

    seq, sets, optional = [], [], []
    for item in items:
        n = item["name"]
        if n.startswith("cs_"):
            seq.append(None)                      # fur on a story character's clothes sorts between their items
            sets.append({"b"})
            optional.append(len(seq) - 1)
            seq.append(n)
            sets.append(item_labels(n))
            if "eyebrows" in n or "hairaccessories" in n:
                optional.append(len(seq) - 1)     # no groom of their own where their names sort
        elif n.startswith("genitalhairfull_"):
            seq.append(n)
            sets.append({"b"})
        elif n.startswith("hair"):
            seq.append(n)
            sets.append(item_labels(n))
    seq.append(None)                              # grooms of non-stub items between hair* and lashes*
    sets.append(set(ALL_LABELS))
    optional.append(len(seq) - 1)
    feasible, plain_possible = align(sets, grooms, optional)
    if not feasible:
        return [f"second stage: no alignment exists for {len(rows)} grooms from archive position {base + start + 1}"]
    allowed, pairs = boundaries(rows, split_inside())
    feasible, possible = align(sets, grooms, optional, allowed)
    rule = "kept"
    if not feasible:      # the split-chain rule contradicts the labels: drop the rule rather than the labels
        allowed, pairs, possible, rule = np.ones(len(rows) + 1, bool), 0, plain_possible, "DROPPED, it contradicts the labels"
    if context is not None:
        context.update(seq=seq, sets=sets, optional=optional, rows=rows, labels=grooms, possible=possible,
                       allowed=allowed, pairs=pairs)
    real = np.array([s is not None for s in seq])
    plain_count = plain_possible.sum(0)
    plain_complete = {i for i in np.nonzero(real)[0]
                      if plain_possible[i].any() and (plain_count[plain_possible[i]] == 1).all()}
    count = possible.sum(0)
    # An item is complete when every groom it can own can only be its own. Its name is then safe even if a
    # neighbouring item turns out to have no groom. A groom that is merely the only candidate at the edge of a
    # run of like items is not named: it would be wrong if that edge item has no groom of its own, which
    # happens (bodyhairgenitals_*, cs_brian_heder_eyebrowsangle). It gets the candidate as a hint instead.
    complete = {i for i in np.nonzero(real)[0] if possible[i].any() and (count[possible[i]] == 1).all()}
    named = edge = hinted = 0
    for k, f in enumerate(rows):
        owners = np.nonzero(possible[:, k])[0]
        cands = [seq[i] for i in owners if real[i]]
        if count[k] == 1 and owners[0] in complete:
            names[f["id"]] = (seq[owners[0]], BASIS if owners[0] in plain_complete else BASIS_SPLIT)
            named += 1
        elif count[k] == 1 and real[owners[0]]:
            hints[f["id"]] = f"{cands[0]}, if every item next to it in name order has a groom of its own"
            edge += 1
        elif cands:
            other = " or an item that is not a hair stub" if len(cands) < len(owners) else ""
            hints[f["id"]] = (f"one of {len(cands)} items ({cands[0]} .. {cands[-1]}){other}" if len(cands) > 1
                              else f"{cands[0]}{other}")
            hinted += 1
    return [f"story characters and hair: {len(rows)} grooms from archive position {base + start + 1}, "
            f"{int(real.sum())} items; {named} grooms named for {len(complete)} complete items, "
            f"{edge} grooms with a single candidate, {hinted} with several (median {int(np.median(count))} items)",
            f"split-chain rule {rule}: {pairs} pairs; {len(complete - plain_complete)} of the complete items need it; "
            f"candidates per groom fall from {plain_count.mean():.1f} to {count.mean():.1f} on average"]
