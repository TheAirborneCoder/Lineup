#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Measure the PSO field type codes from your own copy of the game.

Reads the PSO members that the index stage wrote (WORK/index/**/*.pso) and, for every type code that occurs in a
schema field, measures

  uses / members   how many fields use the code, in how many files (and how often it declares an array element)
  width            two bounds from the files themselves: the gap from the field's offset to the next field's offset
                   (or the structure stride) is an upper bound, and the highest byte that is ever non-zero in the
                   field's data is a lower bound; where they meet the width is exact
  checks           what the data allows: floats are finite and plausible, bool fields hold only 0 or 1, hash fields
                   hit names the `names` stage learned, enum fields hold members of the enum they reference, vectors
                   are finite
  kind             the best-supported reading of those measurements

and compares the result with decode_pso.FORMATS and decode_pso.TYPE_NAMES. Nothing here ships game data.

    python tools/verify_pso_types.py [--work PATH] [--json FILE] [--limit N]

Exit status 1 when a measurement contradicts the tables.
"""
import argparse
import collections
import json
import struct
import sys
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "engine"))

NATURAL = (1, 2, 4, 8, 12, 16, 24, 32, 64)
SAMPLE = 400_000          # values kept per code for the value checks


class Stat:
    def __init__(self):
        self.uses = 0
        self.members = set()
        self.element_uses = 0
        self.block_keys = 0
        self.gaps = collections.Counter()
        self.alignment = collections.Counter()      # offset modulo the smallest gap seen so far, collected at the end
        self.offsets = collections.Counter()
        self.used_by_gap = collections.defaultdict(int)   # gap -> highest non-zero byte + 1 in fields with that gap
        self.values = 0
        self.arrays = collections.defaultdict(list)
        self.enum_ref_fields = 0
        self.enum_hits = 0
        self.enum_total = 0


def natural_up(n):
    return next((w for w in NATURAL if w >= n), n)


def collect(files, names, limit=None):
    from decode_pso import Decoder, sections_of
    stats = collections.defaultdict(Stat)
    read = skipped = 0
    for path in files[:limit]:
        try:
            decoder = Decoder(sections_of(path.read_bytes())[0], {})
        except (ValueError, KeyError, IndexError, OSError, struct.error):
            skipped += 1
            continue
        read += 1
        member = f"{path.parent.name}/{path.name}"
        for key, (stride, _version, fields, ends) in decoder.schemas.items():
            for name, offset, word, ref in fields:
                code = word & 0xFF
                s = stats[code]
                if name == 0x100:
                    s.element_uses += 1
                    continue
                s.uses += 1
                s.members.add(member)
                s.gaps[ends[offset] - offset] += 1
                s.offsets[offset] += 1
        for key, data, _ in decoder.blocks:
            if key < 0x100:
                stats[key].block_keys += 1
                continue
            if key not in decoder.schemas:
                continue
            stride, _version, fields, ends = decoder.schemas[key]
            if not stride or len(data) % stride:
                continue
            rows = np.frombuffer(data, np.uint8).reshape(-1, stride)
            seen = set()
            for name, offset, word, ref in fields:
                if name == 0x100 or offset in seen:
                    continue
                seen.add(offset)
                code, span = word & 0xFF, min(ends[offset] - offset, 64)
                s = stats[code]
                if s.values >= SAMPLE:
                    continue
                cells = rows[:, offset:offset + span]
                used = np.flatnonzero(cells.any(axis=0))
                gap = ends[offset] - offset
                if len(used):
                    s.used_by_gap[gap] = max(s.used_by_gap[gap], int(used[-1]) + 1)
                s.values += len(cells)
                s.arrays["cells"].append((cells[:2000].copy(), span, ref, decoder.enums.get(ref)))
    return stats, read, skipped


# Widths the measurements above fix exactly for these codes; the XML cross-check reads values with them.
READ = {0x01: "u1", 0x10: "i1", 0x11: "u1", 0x12: "i2", 0x13: "u2", 0x14: "i4", 0x15: "u4", 0x21: "f4",
        0x4a: "u4", 0x60: "u1", 0x62: "u4"}


def xml_classes(archives):
    """{class name: {field tag: set of value strings}} from <Item type="Class"> elements of the indexed XML members."""
    import xml.etree.ElementTree as ET
    import archive_index
    classes = {}
    for archive in archives:
        for path in archive_index.xml_members(archive):
            try:
                root = ET.parse(path).getroot()
            except (ET.ParseError, OSError):
                continue
            for item in root.iter("Item"):
                kind = item.get("type")
                if kind:
                    fields = classes.setdefault(kind, {})
                    for child in item:
                        if child.get("value") is not None:
                            fields.setdefault(child.tag, set()).add(child.get("value"))
    return classes


def xml_crosscheck(files, classes):
    """Per type code: [XML values checked, XML values found among the PSO values of the same class and field].

    A PSO structure is matched to an XML class when the hash of the class name is the structure's type hash; a PSO
    field is matched to an XML child when the hash of its tag is the field's name hash. The PSO values are read with
    the measured width of the code, so an agreeing value is independent evidence for that width and kind."""
    from decode_pso import Decoder, sections_of
    from script_tree import _name_hash
    by_hash = {_name_hash(c): c for c in classes}
    result = collections.defaultdict(lambda: [0, 0])
    classes_hit = set()
    domain = collections.defaultdict(set)
    gathered = {}
    for path in files:
        try:
            decoder = Decoder(sections_of(path.read_bytes())[0], {})
        except (ValueError, KeyError, IndexError, OSError, struct.error):
            continue
        for key, data, _ in decoder.blocks:
            cls = by_hash.get(key)
            if cls is None or key not in decoder.schemas:
                continue
            stride, _version, fields, ends = decoder.schemas[key]
            if not stride or len(data) % stride:
                continue
            rows = np.frombuffer(data, np.uint8).reshape(-1, stride)
            tags = {_name_hash(t): t for t in classes[cls]}
            for name, offset, word, ref in fields:
                tag, code = tags.get(name), word & 0xFF
                if tag is None or code not in READ or ends[offset] - offset < int(READ[code][1]):
                    continue
                kind, n = READ[code][0], int(READ[code][1])
                column = np.ascontiguousarray(rows[:, offset:offset + n]).view(f"<{kind}{n}").ravel()
                entry = gathered.setdefault((cls, tag, code), [set(), set(), decoder.enums.get(ref)])
                entry[0].update(column.tolist())
                entry[1].add(cls)
                if code in (0x60, 0x62) and decoder.enums.get(ref):
                    entry[2] = decoder.enums[ref]
    for (cls, tag, code), (values, _c, enum) in gathered.items():
        classes_hit.add(cls)
        domain[code].update(values)
        for text in classes[cls][tag]:
            result[code][0] += 1
            try:
                if code == 0x21:
                    want = float(text)
                    found = any(abs(v - want) <= 1e-5 * max(1.0, abs(want)) for v in values)
                elif code == 0x01:
                    found = {"true": 1, "false": 0}.get(text.lower(), -1) in values
                elif code == 0x4a:
                    found = _name_hash(text) in values or _name_hash(text.lower()) in values
                elif code in (0x60, 0x62):
                    hashes = {_name_hash(text), _name_hash(text.lower())}
                    found = any(hashes & set(enum.get(v, ())) for v in values) if enum else False
                else:
                    bits = int(READ[code][1]) * 8
                    found = (int(text) & ((1 << bits) - 1)) in {v & ((1 << bits) - 1) for v in values}
            except ValueError:
                continue
            result[code][1] += bool(found)
    return result, classes_hit, {c: len(v) for c, v in domain.items()}


def evidence(code, s, names):
    """Measured width bounds and value checks for one code. Returns a dict."""
    upper = min(s.gaps) if s.gaps else None
    # Only the fields that fill the smallest gap say how wide the value is; in a wider gap the bytes after the value
    # may belong to something else.
    lower = s.used_by_gap.get(upper) or None
    width = natural_up(lower) if lower else None
    spill = max((u for g, u in s.used_by_gap.items() if g > (upper or 0)), default=0)
    out = {"upper": upper, "lower": lower, "width": width, "spill": spill if spill > (lower or 0) else None,
           "exact": bool(lower and upper and natural_up(lower) == upper),
           "checks": {}}
    if not width or not s.arrays["cells"]:
        return out
    w = min(width, 16)
    chunks = [c for c, span, _ref, _enum in s.arrays["cells"] if c.shape[1] >= w]
    if not chunks:
        return out
    cells = np.concatenate([c[:, :w] for c in chunks])
    checks = out["checks"]
    n = len(cells)
    checks["samples"] = n
    if w in (1, 2, 4, 8):
        v = np.ascontiguousarray(cells).view(f"<u{w}").ravel()
        checks["zero"] = float((v == 0).mean())
        checks["bool"] = float((v <= 1).mean())
        checks["topBit"] = float((v >> (8 * w - 1)).astype(bool).mean())
        checks["allOnes"] = float((v == (1 << (8 * w)) - 1).mean())
        checks["distinct"] = int(len(np.unique(v)))
        if w == 4:
            f = v.view("<f4")
            finite = np.isfinite(f)
            plausible = finite & ((v == 0) | ((np.abs(f) >= 1e-9) & (np.abs(f) <= 1e9) & (v >= 0x00800000)))
            nz = v != 0
            checks["finiteFloat"] = float(finite.mean())
            checks["plausibleFloat"] = float(plausible[nz].mean()) if nz.any() else None
            if names:
                nonzero = v[nz]
                checks["namedHash"] = float(np.isin(nonzero, np.fromiter(names, np.uint32)).mean()) if len(nonzero) else None
        # enum membership, where the field points at an enum table
        member = total = 0
        for c, span, ref, enum in s.arrays["cells"]:
            if enum and c.shape[1] >= w and w in (1, 2, 4, 8):
                vv = np.ascontiguousarray(c[:, :w]).view(f"<u{w}").ravel()
                members = np.fromiter((k & ((1 << (8 * w)) - 1) for k in enum), np.uint64)
                member += int(np.isin(vv.astype(np.uint64), members).sum())
                total += len(vv)
        if total:
            checks["enumMember"] = member / total
            checks["enumFields"] = total
    elif w % 4 == 0:
        f = np.ascontiguousarray(cells).view("<f4")
        a = np.abs(np.nan_to_num(f, nan=1e30, posinf=1e30))
        normal = (f == 0) | ((a >= 1e-9) & (a <= 1e9))
        checks["finiteFloat"] = float(np.isfinite(f).all(axis=1).mean())
        checks["vectorPlausible"] = float(normal.all(axis=1).mean())
    return out


def infer_kind(code, s, e):
    c = e["checks"]
    width = e["width"]
    if code == 0x05:
        return "structure (inline record)"
    if code in (0x07, 0x08):
        return "pointer-sized value"
    if not width:
        return "no data" if not s.values else "all-zero data"
    if width == 1 and c.get("bool", 0) >= 0.999 and c.get("distinct", 9) <= 2:
        return "bool (only 0/1)"
    if c.get("enumMember", 0) >= 0.98 and c.get("enumFields", 0) >= 20:
        return f"enum{width * 8} (values are members of the referenced enum)"
    if width == 4 and (c.get("namedHash") or 0) >= 0.003:
        return "hash32 (values hit learned names)"
    if width == 4 and (c.get("plausibleFloat") or 0) >= 0.9 and c.get("finiteFloat", 0) >= 0.999 and c.get("distinct", 0) > 8:
        return "float32"
    if width in (12, 16) and c.get("vectorPlausible", 0) >= 0.99:
        return f"vector of {width // 4} float32 (all lanes finite, normal)"
    if width in (12, 16, 24, 64) and c.get("vectorPlausible", 1) < 0.99:
        return f"{width}-byte composite (lanes are not all floats)"
    if width in (1, 2, 4, 8):
        flavour = ("signed or unsigned (top bit used)" if c.get("topBit", 0) > 0 else "unsigned-looking (top bit never set)")
        return f"{width * 8}-bit integer, {flavour}"
    return f"{width}-byte block"


def compare(stats, evid, kinds, formats, type_names):
    """Disagreements between measurements and the tables in decode_pso."""
    problems = []
    for code, fmt in formats.items():
        e = evid.get(code)
        if not e:
            problems.append((code, "in FORMATS but never used by a schema field"))
            continue
        w = struct.calcsize("<" + fmt)
        if e["upper"] is not None and w > e["upper"]:
            problems.append((code, f"FORMATS reads {w} bytes but a field of this code has only {e['upper']} bytes of room"))
        # A label such as vector3_in_16 states that the slot is wider than the part that is read.
        if e["lower"] is not None and w < e["lower"] and "_in_" not in type_names.get(code, ""):
            problems.append((code, f"FORMATS reads {w} bytes but data is non-zero up to byte {e['lower']}"))
    for code in type_names:
        if code not in evid or (not stats[code].uses and not stats[code].block_keys and not stats[code].element_uses):
            problems.append((code, "in TYPE_NAMES but never occurs"))
    return problems


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--work", help="the work folder (default: your saved one)")
    ap.add_argument("--json", help="write the measurements to this file")
    ap.add_argument("--limit", type=int, help="only the first N PSO members")
    args = ap.parse_args(argv)
    import paths
    paths.configure(None, args.work)
    import archive_index
    import decode_pso
    files = sorted(paths.INDEX.rglob("*.pso"))
    if not files:
        print(f"No PSO members under {paths.INDEX}. Run: python lineup.py --build --stages index")
        return 2
    try:
        names = {int(k, 16) for k in json.loads((paths.LIBRARY / "names.json").read_text("utf-8"))["names"]}
    except (OSError, ValueError, KeyError):
        names = set()
    print(f"{len(files)} PSO members; {len(names)} learned names for the hash check")
    stats, read, skipped = collect(files, names, args.limit)
    print(f"read {read}, skipped {skipped}\n")
    evid = {code: evidence(code, s, names) for code, s in stats.items()}
    kinds = {code: infer_kind(code, stats[code], evid[code]) for code in evid}

    header = f"{'code':>5} {'fields':>7} {'files':>6} {'elem':>5} {'upper':>5} {'lower':>5} {'width':>6}  {'table':<28} measured kind / checks"
    print(header)
    print("-" * len(header))
    for code in sorted(stats):
        s, e = stats[code], evid[code]
        table = ""
        if code in decode_pso.TYPE_NAMES:
            table = decode_pso.TYPE_NAMES[code]
            if code in decode_pso.FORMATS:
                table += f" ({struct.calcsize('<' + decode_pso.FORMATS[code])} B)"
        c = e["checks"]
        spill = f" spill>{e['spill']}" if e.get("spill") else ""
        brief = spill + " " + ", ".join(f"{k}={v:.3f}" if isinstance(v, float) else f"{k}={v}" for k, v in c.items()
                          if k in ("bool", "plausibleFloat", "namedHash", "enumMember", "finiteFloat", "topBit"))
        width = f"{e['width']}{'=' if e['exact'] else '~'}" if e["width"] else "-"
        print(f"{code:#5x} {s.uses:7d} {len(s.members):6d} {s.element_uses:5d} {e['upper'] if e['upper'] else '-':>5} "
              f"{e['lower'] if e['lower'] else '-':>5} {width:>6}  {table:<28} {kinds[code]}" + (f"  [{brief}]" if brief else ""))
    print("\nwidth: '=' the data and the layout agree on one width, '~' the layout only bounds it.")
    print("Block keys below 0x100 (primitive blocks): "
          + ", ".join(f"{k:#x} x{s.block_keys}" for k, s in sorted(stats.items()) if s.block_keys))

    classes = xml_classes(archive_index.character_archives())
    shared, hit, domain = xml_crosscheck(files, classes)
    print(f"\nXML cross-check: {len(classes)} classes in <Item type=...> elements, {len(hit)} of them are also PSO structures")
    print("  code   XML values checked   found in the PSO values of the same class and field   distinct PSO values")
    for code in sorted(shared):
        checked, found = shared[code]
        print(f"  {code:#5x}  {checked:18d}   {found:10d} ({100 * found / max(1, checked):5.1f}%)"
              f"{'':36}{domain.get(code, 0):8d}")

    problems = compare(stats, evid, kinds, decode_pso.FORMATS, decode_pso.TYPE_NAMES)
    print()
    if problems:
        print("Measurements that disagree with decode_pso.FORMATS / TYPE_NAMES:")
        for code, text in problems:
            print(f"  {code:#x}: {text}")
    else:
        print("Every entry of decode_pso.FORMATS and TYPE_NAMES is consistent with the measurements.")
    missing = sorted(c for c in stats if c not in decode_pso.TYPE_NAMES and (stats[c].uses or stats[c].element_uses))
    print(f"Codes that occur but have no TYPE_NAMES entry ({len(missing)}): " + " ".join(f"{c:#x}" for c in missing))
    if args.json:
        Path(args.json).write_text(json.dumps({
            f"{c:#x}": {"uses": stats[c].uses, "files": len(stats[c].members), "elementUses": stats[c].element_uses,
                        "gaps": dict(stats[c].gaps), "evidence": evid[c], "kind": kinds[c]} for c in sorted(stats)},
            indent=1, default=str), "utf-8")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
