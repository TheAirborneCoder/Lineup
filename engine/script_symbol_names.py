# SPDX-License-Identifier: GPL-3.0-or-later
"""Verify a script type-5 spelling against its two stored hashes.

The pool uses the low word of Austin Appleby's MurmurHash3_x64_128, seed zero.
Reference: https://github.com/aappleby/smhasher/blob/master/src/MurmurHash3.cpp
"""

import argparse
from collections import Counter
from functools import cache
import hashlib
import json
from pathlib import Path
import struct
from paths import ENGINE


MASK = (1 << 64) - 1
C1 = 0x87C37B91114253D5
C2 = 0x4CF5AD432745937F


def _rotl(word, shift):
    return ((word << shift) | (word >> (64 - shift))) & MASK


def _mix(word, first, rotate, second):
    return _rotl(word * first & MASK, rotate) * second & MASK


def _finalize(word):
    word = ((word ^ (word >> 33)) * 0xFF51AFD7ED558CCD) & MASK
    word = ((word ^ (word >> 33)) * 0xC4CEB9FE1A85EC53) & MASK
    return word ^ (word >> 33)


def murmur64(data: bytes) -> int:
    """Lower 64-bit word of MurmurHash3 x64_128 over these exact bytes."""
    low = high = 0
    end = len(data) - len(data) % 16
    for first, second in struct.iter_unpack("<QQ", memoryview(data)[:end]):
        low ^= _mix(first, C1, 31, C2)
        low = ((_rotl(low, 27) + high) * 5 + 0x52DCE729) & MASK
        high ^= _mix(second, C2, 33, C1)
        high = ((_rotl(high, 31) + low) * 5 + 0x38495AB5) & MASK
    tail = data[end:]
    low ^= _mix(int.from_bytes(tail[:8], "little"), C1, 31, C2)
    high ^= _mix(int.from_bytes(tail[8:], "little"), C2, 33, C1)
    low ^= len(data)
    high ^= len(data)
    low = (low + high) & MASK
    high = (high + low) & MASK
    return (_finalize(low) + _finalize(high)) & MASK


@cache
def _spellings():
    from script_tree import _name_hash

    names = json.loads((ENGINE / "script_symbol_names.json").read_text("utf-8"))
    pairs = {}
    for name in names:
        pair = (_name_hash(name), murmur64(name.encode("ascii")))
        if pair in pairs:
            raise ValueError(f"Duplicate script hash pair for {name!r}")
        pairs[pair] = name
    return pairs


def verified_pool_name(attr, tree):
    """Return a double-hash-verified spelling for a selected nonzero type-5 value."""
    if attr.get("contentType") != 5 or attr.get("pool") != "int64":
        return None
    index = attr["poolRecord"]
    pool = tree["int64Pool"]
    if not 0 <= index < len(pool):
        return None
    both = int(attr["valueHash"]["hash"], 16), int(pool[index]["bits"], 16)
    return _spellings().get(both)


def coverage(folder):
    from script_tree import _name_hash, decode_tree

    names = _spellings()
    pairs = Counter()
    members = matched_members = 0
    manifest = json.loads((folder / "manifest.json").read_bytes())
    for member in manifest["members"]:
        if member["kind"] != "pso":
            continue
        path = folder / member["path"]
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != member["sha256"]:
            raise ValueError(f"Source digest differs for {path.name}")
        tree = decode_tree(data, {})
        members += 1
        used = set()
        for attr in tree["attributes"]:
            if attr["contentType"] != 5:
                continue
            pair = int(attr["valueHash"]["hash"], 16), int(tree["int64Pool"][attr["poolRecord"]]["bits"], 16)
            pairs[pair] += 1
            name = verified_pool_name(attr, tree)
            if name is not None:
                if (_name_hash(name), murmur64(name.encode("ascii"))) != pair:
                    raise ValueError(f"Invalid name at {path.name}, attribute {attr['record']}")
                used.add(name)
        matched_members += bool(used)
    matched = pairs.keys() & names.keys()
    if not members:
        raise ValueError(f"No script PSOs in {folder}/manifest.json")
    if names.keys() - pairs.keys():
        raise ValueError("The candidate file contains names absent from the source archive")
    return {"members": members, "membersWithVerifiedNames": matched_members,
            "attributeUses": sum(pairs.values()), "uniquePairs": len(pairs),
            "zeroPairUses": pairs[(0, 0)], "verifiedNames": len(matched),
            "verifiedAttributeUses": sum(pairs[key] for key in matched)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--extracted", type=Path, default=None)
    print(json.dumps(coverage(parser.parse_args().extracted), indent=2))
