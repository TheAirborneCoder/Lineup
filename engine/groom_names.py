# SPDX-License-Identifier: GPL-3.0-or-later
"""Tie groom assets to named hair items where the archive order proves the link.

    python engine/groom_names.py          # groom_index.py runs it after every rebuild

Groom resources carry no name, no metadata lists an item's grooms, and the archive file tables are
encrypted. What is known:
  * the groom archive inside packs.rpf stores its entries in item-name order (the order of the stub drawables);
  * all rigs share one root offset, so `groom z - rig head height` puts every rig's head at the same place.
A family is linked only when the evidence leaves one answer:
  exact   one groom per item. A run of consecutive grooms must pass the family's body-region test, each at the
          head-relative height of *its own* item's rig, the run must be exactly as long as the item list, and it
          must be the only such run in the archive.
  runs    items may own several grooms. Items and grooms are both cut into runs of one rig (fr1, mr1, mk1, mi1).
          The two run sequences must be identical and unique in the archive. A run with as many grooms as items
          is linked one to one, a run with a single item gives that item all its grooms, anything else stays open.
  family  several grooms per item and no rig changes to separate them: the grooms only get a family hint.
Everything else stays unnamed.
"""
import itertools
import json
import re
import sqlite3
import sys
from contextlib import closing
from paths import LIBRARY

DB = LIBRARY / "grooms.sqlite3"
INVENTORY = LIBRARY / "inventory.sqlite3"
ARCHIVE = "packs.rpf"
BASIS = {"exact": "archive order; one groom per item; count, rig height and body region verified",
         "runs": "archive order; rig runs match the item list exactly; body region verified"}
# Height of the stub triangle in the head of each rig, minus the 1 m root offset the grooms use.
# Read from the stub drawables' boxes: mr1 1.736, fr1 1.626, mk1 0.974, mi1 0.563 (big and old rigs = regular).
RIG_HEAD = {"mr1": 0.736, "mb1": 0.736, "mo1": 0.736, "fr1": 0.626, "fb1": 0.626, "fo1": 0.626,
            "mk1": -0.026, "fk1": -0.026, "mi1": -0.437, "fi1": -0.437}


CLASS_BY_NAME = (("brows", "Brows and lashes"), ("lashes", "Brows and lashes"), ("armpithair", "Body hair"),
                 ("chesthair", "Body hair"), ("leghair", "Body hair"), ("chingoatee", "Facial hair"),
                 ("chopsmutton", "Facial hair"))


def rig_of(name):
    m = re.search(r"_([fm][a-z]{1,2}1)_", name)
    return m.group(1) if m else None


def _rel(asset, head):
    (x0, y0, z0), (x1, y1, z1) = asset["box"]
    return x0, x1, y0, y1, z0 - head, z1 - head


def gate_armpit(a, head):
    # Armpit height below the head differs by sex (0.33 m female, 0.38 m male), so the window is per rig.
    x0, x1, y0, y1, z0, z1 = _rel(a, head)
    expected = -0.379 if head > 0.7 else -0.328
    return x1 - x0 >= 0.30 and z1 - z0 <= 0.09 and abs((z0 + z1) / 2 - expected) <= 0.022


def gate_eye(a, head):
    """Brows and lashes: a small patch at eye level on the front of the head."""
    x0, x1, y0, y1, z0, z1 = _rel(a, head)
    return (max(-x0, x1) <= 0.09 and z1 - z0 <= 0.07 and y1 <= -0.04 and a["longest"] <= 0.04
            and -0.07 <= (z0 + z1) / 2 <= 0.02)


def gate_chest(a, head):
    x0, x1, y0, y1, z0, z1 = _rel(a, head)
    return z1 - z0 >= 0.35 and -0.62 <= (z0 + z1) / 2 <= -0.38


def gate_lower_face(a, head):
    x0, x1, y0, y1, z0, z1 = _rel(a, head)
    return max(-x0, x1) <= 0.13 and y1 <= 0.03 and z1 - z0 <= 0.2 and -0.16 <= (z0 + z1) / 2 <= -0.06


def gate_face(a, head):
    x0, x1, y0, y1, z0, z1 = _rel(a, head)
    return max(-x0, x1) <= 0.14 and y1 <= 0.07 and -0.30 <= (z0 + z1) / 2 <= 0.0


def gate_legs(a, head):
    x0, x1, y0, y1, z0, z1 = _rel(a, head)
    return z1 - z0 >= 0.6 and z1 <= -0.6 and z0 <= -1.5 and 0.35 <= x1 - x0 <= 0.7


# In archive (= name) order.
FAMILIES = [
    ("exact", ("armpithairfull_",), gate_armpit),
    ("family", ("beard",), gate_face),
    ("exact", ("brows",), gate_eye),
    ("exact", ("chesthairfull_",), gate_chest),
    ("exact", ("chingoatee_", "chopsmutton_"), gate_lower_face),
    ("runs", ("lashes",), gate_eye),
    ("exact", ("leghairfull_",), gate_legs),
]
NOT_ATTEMPTED = ("bodyhair*: 11 items, 10 to 14 grooms, no bodyhairgenitals groom in place",
                 "mustache*: 7 items, 8 grooms", "stubbleregular: position not isolated",
                 "player_*, u_*, sc_*, wigs* (after mustache*): grooms of non-stub items sit between them",
                 "crb_*, crf_*, crm_* (animals): several grooms per item, one rig per species")


def load():
    with closing(sqlite3.connect(INVENTORY.as_uri() + "?mode=ro", uri=True)) as db:
        items = [{"name": n, "rig": rig_of(n)} for (n,) in db.execute(
            "select distinct name from model where vbytes > 0 and vbytes <= 200 and archive = ? order by name",
            (ARCHIVE,))]
    with closing(sqlite3.connect(DB)) as db:
        assets = [{"id": i, "ord": o, "offset": off, "box": json.loads(b), "longest": lg}
                  for i, o, off, b, lg in db.execute(
                      "select id, ord, offset, box, longest from asset where archive = ? order by ord", (ARCHIVE,))]
    return items, assets


def _gated_runs(assets, gate, heads, start):
    """Maximal runs of consecutive grooms that pass the gate for exactly one of the family's rig heights."""
    labels = []
    for a in assets:
        passing = [h for h in heads if gate(a, h)]
        labels.append(passing[0] if len(passing) == 1 else None)
    runs, k = [], start
    while k < len(assets):
        if labels[k] is None:
            k += 1
            continue
        end = k
        while end < len(assets) and labels[end] is not None:
            end += 1
        runs.append((k, end, labels[k:end]))
        k = end
    return runs


def link(items, assets):
    names, hints, report = {}, {}, []
    cursor = 0
    for kind, prefixes, gate in FAMILIES:
        members = [i for i in items if i["name"].startswith(prefixes)]
        label = "/".join(p.rstrip("_") for p in prefixes)
        if not members or any(m["rig"] not in RIG_HEAD for m in members):
            report.append((label, kind, len(members), None, "no items, or a rig without a known head height"))
            continue
        n = len(members)
        heads = [RIG_HEAD[m["rig"]] for m in members]
        runs = _gated_runs(assets, gate, sorted(set(heads)), cursor)
        if kind == "family":
            if not runs:
                report.append((label, kind, n, None, "no groom passes the region test"))
                continue
            start, end, _ = runs[0]
            text = f"one of the {n} {label}* items ({members[0]['name']} .. {members[-1]['name']})"
            for k in range(start, end):
                hints[assets[k]["id"]] = f"{text}, groom {k - start + 1} of {end - start} in that block"
            report.append((label, kind, n, start, f"{end - start} grooms labelled with the family"))
            cursor = end
            continue
        if kind == "exact":
            fits = [(s, e) for s, e, lab in runs if e - s == n and lab == heads]
        else:
            want = [h for h, _ in itertools.groupby(heads)]
            fits = [(s, e) for s, e, lab in runs if [h for h, _ in itertools.groupby(lab)] == want]
        if len(fits) != 1:
            report.append((label, kind, n, None, f"{len(fits)} candidate runs, need exactly one"))
            continue
        start, end = fits[0]
        if kind == "exact":
            for k, item in enumerate(members):
                names[assets[start + k]["id"]] = (item["name"], BASIS["exact"])
            report.append((label, kind, n, start, f"linked, {n} grooms"))
        else:
            labels = next(lab for s, e, lab in runs if s == start)
            item_runs = [list(g) for _, g in itertools.groupby(members, key=lambda m: RIG_HEAD[m["rig"]])]
            pos, linked, open_items = start, 0, 0
            for group, (_, lab_run) in zip(item_runs, itertools.groupby(labels)):
                m = len(list(lab_run))
                if m == len(group):
                    for k, item in enumerate(group):
                        names[assets[pos + k]["id"]] = (item["name"], BASIS["runs"])
                    linked += len(group)
                elif len(group) == 1:
                    for k in range(m):
                        names[assets[pos + k]["id"]] = (group[0]["name"], BASIS["runs"])
                    linked += 1
                else:
                    text = f"one of {len(group)} items ({group[0]['name']} .. {group[-1]['name']})"
                    for k in range(m):
                        hints[assets[pos + k]["id"]] = text
                    open_items += len(group)
                pos += m
            report.append((label, kind, n, start, f"linked {linked} items over {end - start} grooms"
                           + (f", {open_items} items open" if open_items else "")))
        cursor = end
    return names, hints, report


def main():
    items, assets = load()
    names, hints, report = link(items, assets)
    import groom_align
    import groom_likely
    context, likely = {}, {}
    stage2 = groom_align.link(items, assets, names, hints, context)
    stage2 += groom_likely.link(context, names, hints, likely)
    with closing(sqlite3.connect(DB)) as db, db:
        columns = {r[1] for r in db.execute("pragma table_info(asset)")}
        for column, kind in (("hint", "text"), ("likely", "text"), ("likely_p", "real"), ("grp", "integer"),
                             ("edge_p", "real")):
            if column not in columns:
                db.execute(f"alter table asset add column {column} {kind}")
        db.execute("update asset set name = null, name_basis = null, hint = null, likely = null, likely_p = null,"
                   " grp = null, edge_p = null")
        db.executemany("update asset set likely = ?, likely_p = ?, grp = ?, edge_p = ? where id = ?",
                       [(n, round(p, 4), g, round(e, 4), i) for i, (n, p, g, e) in likely.items()])
        db.executemany("update asset set name = ?, name_basis = ? where id = ?",
                       [(n, basis, i) for i, (n, basis) in names.items()])
        db.executemany("update asset set hint = ? where id = ?", [(h, i) for i, h in hints.items()])
        # A proven name beats the box heuristic for the placement class (child and infant rigs sit low).
        for prefix, cls in CLASS_BY_NAME:
            db.execute("update asset set class = ? where name like ?", (cls, prefix + "%"))
        db.execute("update asset set class = 'Scalp hair' where name like 'hair%'"
                   " or (name like 'cs%' and instr(name, '_hair') > 0)")
    for label, kind, n, start, outcome in report:
        where = "" if start is None else f" at archive position {start + 1}"
        print(f"{label:28s} {n:4d} items  {kind:6s} {outcome}{where}")
    for line in stage2:
        print(line)
    for line in NOT_ATTEMPTED:
        print("not linked:", line)
    linked_items = len({n for n, _ in names.values()})
    print(f"{len(names)} grooms named for {linked_items} of {len(items)} hair and fur items; "
          f"{len(hints)} grooms with a family hint; {len(assets)} grooms in {ARCHIVE}.")


if __name__ == "__main__":
    sys.exit(main())
