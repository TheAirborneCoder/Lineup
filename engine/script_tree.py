# SPDX-License-Identifier: GPL-3.0-or-later
"""Read the node trees and attribute pools in the script.rpf metadata PSOs."""

import math
import struct

from decode_pso import Decoder, require, sections_of


ROOT_TYPE = 0xDE396FE2
ATTRIBUTE_TYPE = 0x85B4C631
NODE_TYPE = 0x01393259
XYZ_TYPE = 0xAEB623D8
NO_INDEX = 0xFFFF
NODE = struct.Struct("<I5H2B")
ATTRIBUTE = struct.Struct("<IIIBB2s")


def _name_hash(label):
    # These identifiers are case-sensitive (e.g. valueHash and parentIndex).
    result = 0
    for byte in label.encode("ascii"):
        result = ((result + byte) * 1025) & 0xFFFFFFFF
        result ^= result >> 6
    result = (result * 9) & 0xFFFFFFFF
    return ((result ^ (result >> 11)) * 32769) & 0xFFFFFFFF


def verified_attribute_value(attr, tree):
    """Return typed display text only when its exact spelling reproduces the source value hash."""
    kind, index = attr["contentType"], attr["contentIndex"]
    if kind == 1:
        vector = tree["xyzPool"][index].get("float32Interpretation")
        if vector is None:
            return None
        label, text = "Float32 XYZ", ",".join(f"{n:.6f}" for n in vector)
    elif kind == 2:
        value, = struct.unpack("<f", struct.pack("<I", index))
        if not math.isfinite(value):
            return None
        label, text = "Float32", f"{value:.6f}"
    elif kind == 3:
        value, = struct.unpack("<i", struct.pack("<I", index))
        label, text = "Signed int32", str(value)
    elif kind == 4 and index in (0, 1):
        label, text = "Boolean", ("false", "true")[index]
    else:
        return None
    if _name_hash(text) == int(attr["valueHash"]["hash"], 16):
        return {"type": label, "text": text}
    return None


# Published spellings, each tested against the uint32 key at the start of a forest root.
ROOT_NAMES = {f"0x{_name_hash(name):08x}": {"name": name, "source": "published spelling"}
              for name in ("airmule", "banshee", "metromule", "hauler", "buffalo", "elegy", "phoenix")}


# Display candidates only when the spelling reproduces the exact stored hash.
_CANDIDATES = {_name_hash(word): word for word in (
    "root", "data", "item", "items", "points", "tags", "tag", "scene", "set",
    "locations", "layout", "sceneposition", "attributes", "dataNodes", "name",
    "valueHash", "contentType", "contentIndex", "parentIndex", "childIndex",
    "siblingIndex", "previousSiblingIndex", "attributeStart", "attributeCount",
    "flags", "heading", "position", "pitch", "text", "rotation", "scale", "cost",
    "radius", "faction", "id", "type", "priority", "offset", "x", "y", "z",
    "back", "gender", "exhaust", "location", "bumper_f", "category", "none",
    "bumper_r", "interior", "handlebars", "settings", "bonnet", "right",
)}


def _label(key, names):
    digits = f"{key:08x}"
    out = {"hash": "0x" + digits}
    supplied = names.get(key) or names.get(digits)
    if supplied is not None:
        out["suppliedName"] = supplied
    if key in _CANDIDATES and supplied != _CANDIDATES[key]:
        out["candidateName"] = _CANDIDATES[key]
    return out


def _array(d, root, offset, kind, width, name):
    count, capacity, pointer = struct.unpack_from("<IIQ", root, offset)
    require(count <= capacity, f"{name} count exceeds capacity")
    info = {"count": count, "capacity": capacity, "pointerBits": f"0x{pointer:016x}"}
    if not pointer and not count:
        return info, b""
    target = d.pointer(pointer)
    require(target and "block" in target, f"Invalid {name} pointer")
    block, start = target["block"], target["offset"]
    actual, packed, _ = d.blocks[block - 1]
    require(actual == kind and start % width == 0 and start + count * width <= len(packed),
            f"Invalid {name} record range")
    info.update(block=block, startRecord=start // width)
    return info, packed[start:start + count * width]


def _check_nodes(nodes, attributes):
    roots = [row["record"] for row in nodes if row["parent"] is None]
    require(len(roots) == 1, "Expected one script tree root")
    root = roots[0]
    require(nodes[root]["previousSibling"] is None and nodes[root]["nextSibling"] is None,
            "Root has a sibling")
    pending = [root]
    seen = {root}
    while pending:
        parent = pending.pop()
        node = nodes[parent]
        child, previous = node["firstChild"], None
        while child is not None:
            require(child not in seen and child < len(nodes), "Invalid or repeated script child")
            row = nodes[child]
            require(row["parent"] == parent and row["previousSibling"] == previous,
                    "Inconsistent script child or sibling reference")
            seen.add(child)
            pending.append(child)
            previous, child = child, row["nextSibling"]
        count = node["attributeCount"]
        if count:
            start = node["attributeStart"]
            require(start is not None and start + count <= len(attributes),
                    "Script attribute span outside the array")
            for index in range(start, start + count):
                require(attributes[index]["node"] is None, "Overlapping script attribute spans")
                attributes[index]["node"] = parent
    require(len(seen) == len(nodes), "Script tree contains unreachable nodes")
    require(all(row["node"] is not None for row in attributes), "Unowned script attribute")
    return root


def decode_tree(data, names):
    """Return the validated native ordinals and typed values for one verified PSO."""
    sections, checksum = sections_of(data)
    d = Decoder(sections, {})
    root_type, root, _ = d.blocks[d.root - 1]
    require(root_type == ROOT_TYPE and len(root) == 80, "Unsupported script tree root")
    specs = [
        ("attributes", 0x749DBB9E, ATTRIBUTE_TYPE, ATTRIBUTE.size),
        ("strings", 0x5C2411E5, 0x44, 16),
        ("xyz", 0xD54B8545, XYZ_TYPE, 12),
        ("int64", 0x4880F687, 0x16, 8),
        ("nodes", 0x2260B8BE, NODE_TYPE, NODE.size),
    ]
    arrays, packed = {}, {}
    for i, (name, key, kind, width) in enumerate(specs):
        info, chunk = _array(d, root, 16 * i, kind, width, name)
        info["field"] = _label(key, names)
        arrays[name], packed[name] = info, chunk

    nodes = []
    for i, (key, parent, child, nxt, prev, start, count, flag) in enumerate(NODE.iter_unpack(packed["nodes"])):
        links = [None if n == NO_INDEX else n for n in (parent, child, nxt, prev, start)]
        nodes.append({
            "record": i, "name": _label(key, names), "parent": links[0], "firstChild": links[1],
            "nextSibling": links[2], "previousSibling": links[3], "attributeStart": links[4],
            "attributeCount": count, "flag": flag,
        })

    attributes = []
    for i, (key, val, index, kind, flag, tail) in enumerate(ATTRIBUTE.iter_unpack(packed["attributes"])):
        row = {"record": i, "node": None, "name": _label(key, names), "valueHash": _label(val, names),
               "contentIndex": index, "contentType": kind, "flag": flag, "reservedHex": tail.hex()}
        pool = {1: "xyz", 5: "int64"}.get(kind)
        if pool:
            require(index < arrays[pool]["count"], f"Script attribute {i} exceeds its {pool} pool")
            row.update(pool=pool, poolRecord=index)
        attributes.append(row)
    root_node = _check_nodes(nodes, attributes)

    xyz = []
    for i, signed in enumerate(struct.iter_unpack("<3i", packed["xyz"])):
        floats = struct.unpack_from("<3f", packed["xyz"], i * 12)
        row = {"record": i, "int32": list(signed)}
        if all(map(math.isfinite, floats)):
            row["float32Interpretation"] = list(floats)
        xyz.append(row)
    ints = [{"record": i, "bits": f"0x{n:016x}",
             "signedDecimal": str(n - (1 << 64) if n >> 63 else n)}
            for i, (n,) in enumerate(struct.iter_unpack("<Q", packed["int64"]))]
    strings = []
    for p in range(0, len(packed["strings"]), 16):
        chunk = packed["strings"][p:p + 16]
        strings.append({"record": p // 16, "value": d.value(chunk, 0x44, 0), "rawHex": chunk.hex()})
    return {
        "rootType": f"0x{ROOT_TYPE:08x}", "checksum": f"0x{checksum:08x}", "rootNode": root_node,
        "nodeBlock": arrays["nodes"].get("block"), "attributeBlock": arrays["attributes"].get("block"),
        "counts": {key: info["count"] for key, info in arrays.items()}, "arrays": arrays,
        "nameResolution": {"suppliedName": "names.json",
                           "candidateName": "Exact case-sensitive Jenkins hash of a candidate label"},
        "nodes": nodes, "attributes": attributes, "xyzPool": xyz, "int64Pool": ints, "stringPool": strings,
    }
