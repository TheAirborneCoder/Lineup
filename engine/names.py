# SPDX-License-Identifier: GPL-3.0-or-later
"""Learn names from your own copy of the game: WORK/library/names.json.

The game's data files store most names only as 32-bit hashes (structure types, field names, enum values, resource
and archetype names). The same words appear in clear text elsewhere in the game: as tags, attributes and text in
the XML members, and as NUL-terminated strings inside the metadata (PSO) members. This stage

  1. collects the hashes that need a name: every structure type, field name, enum and enum value hash in the
     indexed PSO members, and every value of a hash-typed field in their data blocks;
  2. collects candidate words from the indexed XML and PSO members and from script_symbol_names.json (this
     project's own list);
  3. hashes every candidate as written and in lower case with the game's hash (script_tree._name_hash) and keeps
     the ones that land on a wanted hash;
  4. records, for each name kept, whether the word also occurs as a string inside a PSO member that holds the hash
     ("same-member") or only turned up elsewhere ("elsewhere").

A 32-bit hash is hit by chance now and then: a test with a deliberately wrong hash function lands about 100 spurious
names among the 12,000 found, and "same-member" is the evidence that is hard to get by accident. Nothing here ships
a name list. A hash that two different words reach is left out. names.json keeps its {"names": {...}} map and adds
an {"evidence": {...}} map; readers that find no names.json fall back to hash-only labels.

    python engine/names.py          (needs the index stage first)
"""
import hashlib
import json
import re
import time

import numpy as np

import archive_index
from decode_pso import Decoder, sections_of
from paths import ENGINE, INDEX, LIBRARY
from script_tree import _name_hash

OUT = LIBRARY / "names.json"
HASH32 = 0x4A
STRUCTURE = 0x05
ARRAY_ELEMENT = 0x100
WORD = re.compile(rb"[A-Za-z_][A-Za-z0-9_]{1,80}")
STRING = re.compile(rb"(?<![a-zA-Z_0-9./-])[a-zA-Z_][a-zA-Z0-9_./-]{3,150}\x00")
VALUE = re.compile(rb'(?:="|>)([A-Za-z_][A-Za-z0-9_./ -]{2,100})(?:"|<)')


def hash_offsets(schemas, key, base=0, depth=0):
    """Byte offsets, inside one record of structure `key`, of every hash-typed field (nested structures too)."""
    stride, _, fields, ends = schemas[key]
    found = []
    for name, offset, word, ref in fields:
        if name == ARRAY_ELEMENT:
            continue
        code, room = word & 0xFF, ends[offset] - offset
        if code == HASH32 and room >= 4:
            found.append(base + offset)
        elif code == STRUCTURE and ref in schemas and depth < 8 and schemas[ref][0] <= room:
            found.extend(hash_offsets(schemas, ref, base + offset, depth + 1))
    return found


def pso_members():
    """The indexed PSO member files of every archive."""
    return sorted(INDEX.rglob("*.pso"))


def wanted_hashes(files, progress=None):
    """{hash: kind}. Kinds: type, field, enum, enum value, hash field value."""
    wanted = {}
    values = []
    per_file = []
    failed = 0
    for i, path in enumerate(files):
        if progress:
            progress(i, len(files), "wanted hashes")
        try:
            blob = path.read_bytes()
            decoder = Decoder(sections_of(blob)[0], {})
        except (ValueError, KeyError, OSError, IndexError):
            failed += 1
            per_file.append(None)
            continue
        mine = []
        for key, (_stride, _version, fields, _ends) in decoder.schemas.items():
            mine.append(key)
            wanted.setdefault(key, "type")
            for name, *_ in fields:
                if name != ARRAY_ELEMENT:
                    mine.append(name)
                    wanted.setdefault(name, "field")
        for key, entries in decoder.enums.items():
            mine.append(key)
            wanted.setdefault(key, "enum")
            for names in entries.values():
                for name in names:
                    mine.append(name)
                    wanted.setdefault(name, "enum value")
        first = len(values)
        for key, data, _ in decoder.blocks:
            if key == HASH32:
                values.append(np.frombuffer(data[:len(data) // 4 * 4], "<u4"))
            elif key in decoder.schemas:
                stride = decoder.schemas[key][0]
                if len(data) % stride:
                    continue
                rows = np.frombuffer(data, np.uint8).reshape(-1, stride)
                for offset in hash_offsets(decoder.schemas, key):
                    values.append(np.ascontiguousarray(rows[:, offset:offset + 4]).view("<u4").ravel())
        per_file.append(np.unique(np.concatenate([np.array(mine, np.uint32), *values[first:]])))
    if values:
        for value in np.unique(np.concatenate(values)).tolist():
            wanted.setdefault(value, "hash field value")
    wanted.pop(0, None)
    return wanted, failed, per_file


def candidate_words(files, progress=None):
    """Words from the XML members, the PSO strings and this project's own symbol list."""
    words = set()
    xml = [p for archive in archive_index.character_archives() for p in archive_index.xml_members(archive)]
    for i, path in enumerate(xml):
        if progress:
            progress(i, len(xml), "XML members")
        try:
            data = path.read_bytes()
        except OSError:
            continue
        words.update(m.group(0).decode("ascii") for m in WORD.finditer(data))
        for m in VALUE.finditer(data):
            text = m.group(1).decode("ascii").strip()
            words.add(text)
            words.update(text.split("/"))
    for i, path in enumerate(files):
        if progress:
            progress(i, len(files), "PSO strings")
        try:
            data = path.read_bytes()
        except OSError:
            continue
        for m in STRING.finditer(data):
            text = m.group(0)[:-1].decode("ascii")
            words.add(text)
            words.update(text.split("/"))
    try:
        words.update(json.loads((ENGINE / "script_symbol_names.json").read_text("utf-8")))
    except (OSError, ValueError):
        pass
    return {w for w in words if w and w.isascii()}


def member_evidence(files, per_file, found):
    """{hash: "same-member" | "elsewhere"} for the names found. Same-member: the word also occurs as a string in a
    PSO member that holds the hash."""
    hashes = np.array(sorted(found), np.uint32)
    holders = {}
    for i, vals in enumerate(per_file):
        if vals is None or not len(hashes):
            continue
        position = np.searchsorted(vals, hashes)
        position[position == len(vals)] = 0
        for h in hashes[vals[position] == hashes].tolist():
            holders.setdefault(h, []).append(i)
    strings = {}

    def words_of(i):
        if i not in strings:
            seen = set()
            try:
                for m in STRING.finditer(files[i].read_bytes()):
                    text = m.group(0)[:-1].decode("ascii").lower()
                    seen.add(text)
                    seen.update(text.split("/"))
            except OSError:
                pass
            strings[i] = seen
        return strings[i]
    return {h: ("same-member" if any(found[h].lower() in words_of(i) for i in holders.get(h, ())) else "elsewhere")
            for h in found}


def match(words, wanted):
    """{hash: word} for words (as written or in lower case) that reach a wanted hash; ambiguous hashes are dropped."""
    found, clash = {}, set()
    for word in sorted(words):
        for form in {word, word.lower()}:
            h = _name_hash(form)
            if h not in wanted:
                continue
            if h in found and found[h] != form:
                clash.add(h)
            found.setdefault(h, form)
    for h in clash:
        del found[h]
    return found, len(clash)


def build(progress=None):
    """Write names.json. Returns a signature of its content (so an unchanged result does not rebuild its readers)."""
    t0 = time.monotonic()
    files = pso_members()
    wanted, failed, per_file = wanted_hashes(files, progress)
    words = candidate_words(files, progress)
    found, clashes = match(words, wanted)
    evidence = member_evidence(files, per_file, found)
    same = sum(1 for v in evidence.values() if v == "same-member")
    kinds = {}
    for h in wanted:
        row = kinds.setdefault(wanted[h], [0, 0])
        row[0] += 1
        row[1] += h in found
    result = {
        "sources": ["Words found in this copy of the game (XML and PSO members) and script_symbol_names.json, "
                    "each tested against the game's own 32-bit name hash."],
        "stats": {"psoMembers": len(files), "psoUnreadable": failed, "candidateWords": len(words),
                  "wanted": len(wanted), "named": len(found), "sameMember": same, "elsewhere": len(found) - same,
                  "ambiguousDropped": clashes,
                  "byKind": {k: {"wanted": v[0], "named": v[1]} for k, v in sorted(kinds.items())},
                  "seconds": round(time.monotonic() - t0, 1)},
        "names": {f"{h:08x}": found[h] for h in sorted(found)},
        "evidence": {f"{h:08x}": evidence[h] for h in sorted(found)},
    }
    LIBRARY.mkdir(parents=True, exist_ok=True)
    text = json.dumps(result, indent=1, sort_keys=False)
    tmp = OUT.with_suffix(".tmp")
    tmp.write_text(text, "utf-8")
    tmp.replace(OUT)
    print(f"    names: {len(found)} of {len(wanted)} wanted hashes named from {len(words)} candidate words "
          f"({same} also occur in the member that holds the hash)", flush=True)
    return hashlib.sha256(json.dumps([result["names"], result["evidence"]], sort_keys=True).encode()).hexdigest()[:20]


def load():
    """{hash (int): name} from names.json, or {} when it is missing."""
    try:
        return {int(k, 16): v for k, v in json.loads(OUT.read_text("utf-8"))["names"].items()}
    except (OSError, ValueError, KeyError):
        return {}


if __name__ == "__main__":
    build()
