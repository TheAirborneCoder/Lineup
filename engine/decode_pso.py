# SPDX-License-Identifier: GPL-3.0-or-later
"""Decode the observed Xbox RPF9 PSO layout into JSON. Python standard library only."""

import argparse
import json
import math
import os
from pathlib import Path
import struct
import sys
import tempfile

from rpf9_psin import psin_checksum


SECTIONS = ["PSIN", "LSCH", "LENM", "LSNS", "LGID", "LRSC", "CHKS"]
# Field type codes of the PSO schemas. FORMATS gives the little-endian struct format of the scalar codes (vectors are
# stored in 16-byte slots; the first 12 or 16 bytes are read), TYPE_NAMES a label for each code the decoder
# understands (vector3 sits in a 16-byte slot whose fourth lane is not read; 0x63 fields are one byte wide). Both
# come from the PSO members of the game itself: `python tools/verify_pso_types.py` measures, for
# every code in your copy, how many bytes its fields occupy (from the schema layout and from the data) and whether
# the values behave as the label says (booleans are 0 or 1, floats are finite and plausible, hashes hit learned
# names, enum values are members of their enum). A field with less room than its format is returned as raw bytes,
# and codes that occur in the files but are not listed here are returned as raw bytes too.
FORMATS = {0x01: "B", 0x10: "b", 0x11: "B", 0x12: "h", 0x13: "H",
           0x14: "i", 0x15: "I", 0x21: "f", 0x33: "3f", 0x34: "4f",
           0x4a: "I", 0x60: "B", 0x62: "i", 0x63: "B", 0x64: "H", 0x65: "I"}
TYPE_NAMES = {0x01: "bool", 0x05: "structure", 0x07: "pointer", 0x10: "int8",
              0x11: "uint8", 0x12: "int16", 0x13: "uint16", 0x14: "int32",
              0x15: "uint32", 0x21: "float32", 0x33: "vector3_in_16", 0x34: "vector4",
              0x40: "fixed_string", 0x44: "string", 0x4a: "hash32", 0x50: "fixed_array",
              0x52: "array", 0x5c: "array32", 0x60: "enum8", 0x62: "enum32",
              0x63: "flags8", 0x64: "flags16", 0x65: "flags32"}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def hash_id(value):
    return f"0x{value:08x}"


def raw(data):
    return {"raw_hex": data.hex()}


def string_value(data):
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return raw(data)


def sections_of(blob):
    sections = {}
    offset = 0
    while offset < len(blob):
        tag, size = struct.unpack_from(">4sI", blob, offset)
        tag = tag.decode("ascii")
        require(size >= 8 and offset + size <= len(blob) and tag not in sections,
                f"invalid section at {offset:#x}")
        sections[tag] = blob[offset + 8:offset + size]
        offset += size
    require(list(sections) == SECTIONS, "unsupported PSO section layout")
    trailer = sections["CHKS"]
    require(len(trailer) == 12, "invalid CHKS size")
    size, checksum = struct.unpack_from(">II", trailer)
    require(size == len(blob), "CHKS length mismatch")
    require(checksum == psin_checksum(blob), "CHKS checksum mismatch")
    return sections, checksum


def read_schemas(data, count):
    cursor = 32 * count
    schemas = {}
    for i in range(count):
        key, version = struct.unpack_from("<II", data, i * 32)
        stride, _, n = struct.unpack_from("<IHH", data, i * 32 + 24)
        require(stride > 0 and key not in schemas, "invalid structure descriptor")
        fields = [struct.unpack_from("<4I", data, cursor + 16*j) for j in range(n)]
        cursor += n * 16
        # 0x100 declares the element type of an array. It does not occupy storage.
        starts = sorted({offset for name, offset, _, _ in fields if name != 0x100})
        require(not starts or starts[-1] < stride, "field offset outside record")
        ends = dict(zip(starts, starts[1:] + [stride]))
        schemas[key] = (stride, version, fields, ends)
    require(cursor == len(data), "LSCH length mismatch")
    return schemas


def read_enums(data, count):
    cursor = 24 * count
    enums = {}
    for i in range(count):
        key = struct.unpack_from("<I", data, 24*i)[0]
        n = struct.unpack_from("<I", data, 24*i + 16)[0]
        entries = {}
        for _ in range(n):
            value, name = struct.unpack_from("<qI", data, cursor)
            entries.setdefault(value, []).append(name)
            cursor += 16
        enums[key] = entries
    require(cursor == len(data), "LENM length mismatch")
    return enums


def read_blocks(data, count):
    blocks = []
    cursor = 16 * count + 8  # the first data byte aligns to 16 from the start of PSIN
    for i in range(count):
        key, size = struct.unpack_from("<II", data, 16*i)
        require(cursor + size <= len(data), "PSIN block outside section")
        blocks.append((key, data[cursor:cursor + size], cursor + 8))
        cursor += (size + 15) & ~15
    require(cursor - len(data) in range(16), "PSIN block lengths do not cover section")
    return blocks


def lsns_segments(data):
    require(len(data) >= 4, "Truncated LSNS segment count")
    n, = struct.unpack_from("<I", data)
    cursor = 4 + 8*n
    require(cursor <= len(data), "Truncated LSNS segment directory")
    segments = []
    for i in range(n):
        key, size = struct.unpack_from("<II", data, 4+i*8)
        segments.append((key, cursor, size))
        cursor += size
    require(cursor == len(data), "LSNS length mismatch")
    return segments


def auxiliary(sections):
    data = sections["LSNS"]
    segments = [{"id": hash_id(key), "raw_hex": data[start:start+size].hex()}
                for key, start, size in lsns_segments(data)]
    gids = sections["LGID"]
    if gids:
        count, = struct.unpack_from("<I", gids)
        require(len(gids) == 4 + count*16, "LGID length mismatch")
    return {"LSNS": segments, "LGID": [gids[i:i+16].hex() for i in range(4, len(gids), 16)],
            "LRSC": raw(sections["LRSC"])}


class Decoder:
    format_hash = staticmethod(hash_id)

    def __init__(self, sections, names):
        lr = sections["LRSC"]
        require(len(lr) == 80, "unsupported LRSC descriptor")
        self.root, = struct.unpack_from("<I", lr, 16)
        ns, ne, nb = struct.unpack_from("<HHI", lr, 56)
        self.schemas = read_schemas(sections["LSCH"], ns)
        self.enums = read_enums(sections["LENM"], ne)
        self.blocks = read_blocks(sections["PSIN"], nb)
        require(1 <= self.root <= nb, "root block outside directory")
        self.names = names

    def label(self, key):
        return self.names.get(key, hash_id(key))

    def pointer(self, bits):
        if not bits:
            return None
        block, offset = bits & 0xfffff, bits >> 20
        if 1 <= block <= len(self.blocks) and offset < len(self.blocks[block-1][1]):
            return {"block": block, "offset": offset}
        return {"unresolved_pointer": hex(bits)}

    def value(self, data, word, ref, fields=(), depth=0):
        code = word & 0xff
        fmt = FORMATS.get(code)
        if fmt and struct.calcsize("<" + fmt) <= len(data):
            values = struct.unpack_from("<" + fmt, data)
            if any(isinstance(v, float) and not math.isfinite(v) for v in values):
                return raw(data)
            v = values[0]
            if code == 1 and v in (0, 1):
                return bool(v)
            if code == 0x4a:
                return self.format_hash(v)
            if code in (0x60, 0x62) and v in self.enums.get(ref, {}):
                return {"value": v, "symbols": [self.label(h) for h in self.enums[ref][v]]}
            return list(values) if len(values) > 1 else v
        if code == 5 and ref in self.schemas and depth < 16:
            stride = self.schemas[ref][0]
            if stride <= len(data):
                return self.record(ref, data[:stride], depth + 1)
        if code == 7 and len(data) >= 8:
            return self.pointer(struct.unpack_from("<Q", data)[0])
        if code == 0x40 and 0 < ref <= len(data):
            return string_value(data[:ref].split(b"\0", 1)[0])
        if code == 0x44 and len(data) >= 16:
            if data[15] & 0x80:
                n = data[15] & 0x7f
                if n <= 15:
                    return string_value(data[:n])
            else:
                bits, n = struct.unpack_from("<QH", data)
                ptr = self.pointer(bits)
                if ptr is None and n == 0:
                    return None
                if ptr and "block" in ptr:
                    _, target, _ = self.blocks[ptr["block"]-1]
                    end = ptr["offset"] + n
                    if end <= len(target):
                        return string_value(target[ptr["offset"]:end])
        if code in (0x52, 0x5c) and len(data) >= 16:
            if code == 0x52:
                bits, count, capacity = struct.unpack_from("<QHH", data)
            else:
                count, capacity, bits = struct.unpack_from("<IIQ", data)
            return {"count": count, "capacity": capacity, "target": self.pointer(bits)}
        if code == 0x50 and word >> 16 < len(fields) and ref and depth < 16:
            marker, _, element, key = fields[word >> 16]
            stride = self.element_size(element & 0xff, key)
            if marker == 0x100 and stride and ref * stride <= len(data):
                return [self.value(data[i:i+stride], element, key, fields, depth+1)
                        for i in range(0, ref*stride, stride)]
        return raw(data)

    def element_size(self, code, key):
        if code == 5 and key in self.schemas:
            return self.schemas[key][0]
        if code == 7:
            return 8
        if code in FORMATS:
            return struct.calcsize("<" + FORMATS[code])
        return 0

    def record(self, key, data, depth=0):
        _, _, fields, ends = self.schemas[key]
        result = {}
        for name, offset, word, ref in fields:
            if name != 0x100:
                label = self.label(name)
                require(label not in result, f"duplicate record field {label}")
                result[label] = self.value(data[offset:ends[offset]], word, ref, fields, depth)
        return result

    def decoded_block(self, number, key, data, offset):
        result = {"block": number, "type": self.label(key), "type_id": hash_id(key),
                  "file_offset": offset, "size": len(data)}
        if key in self.schemas:
            stride = self.schemas[key][0]
            require(len(data) % stride == 0, "partial structure record")
            result["records"] = [self.record(key, data[i:i+stride]) for i in range(0, len(data), stride)]
        else:
            stride = self.element_size(key, 0)
            # Primitive blocks can include padding between array slices. Preserve the bytes.
            result.update(raw(data))
            if stride and len(data) % stride == 0:
                result["values"] = [self.value(data[i:i+stride], key, 0) for i in range(0, len(data), stride)]
        return result

    def schema_json(self):
        result = {}
        for key, (stride, version, fields, _) in self.schemas.items():
            descriptors = []
            for name, off, word, ref in fields:
                descriptors.append({"name": "array_element" if name == 0x100 else self.label(name),
                                    "name_id": hash_id(name), "offset": off,
                                    "type": TYPE_NAMES.get(word & 255, "unresolved"),
                                    "type_word": hex(word), "reference": hash_id(ref)})
            result[hash_id(key)] = {"name": self.label(key), "stride": stride,
                                    "schema_key": hash_id(version), "fields": descriptors}
        return result


def decode(path, names):
    return decode_bytes(path.read_bytes(), names, path.name)


def decode_bytes(blob, names, source_name):
    sections, checksum = sections_of(blob)
    decoder = Decoder(sections, names)
    return {"source": source_name, "checksum": hash_id(checksum), "root_block": decoder.root,
            "schemas": decoder.schema_json(),
            "enums": {hash_id(k): {str(v): [{"hash": hash_id(h), "name": decoder.label(h)} for h in keys]
                                   for v, keys in entries.items()} for k, entries in decoder.enums.items()},
            "blocks": [decoder.decoded_block(i, *b) for i, b in enumerate(decoder.blocks, 1)],
            "auxiliary": auxiliary(sections)}


def write_json(path, obj):
    # Existing user files are never replaced. Publish only complete JSON.
    out = tempfile.NamedTemporaryFile(dir=path.parent, delete=False, mode="w", encoding="utf-8")
    tmp = Path(out.name)
    try:
        with out:
            json.dump(obj, out, indent=2, allow_nan=False)
            out.write("\n")
        if os.name == "nt":
            tmp.rename(path)
        else:
            os.link(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="one PSO file or folder of PSO files")
    parser.add_argument("--output", required=True, type=Path, help="new JSON file or batch output folder")
    args = parser.parse_args()
    # Names learned from your own game files (python lineup.py --build --stages names); without them labels stay hashes.
    from paths import LIBRARY
    try:
        names = {int(k, 16): v for k, v in json.loads((LIBRARY / "names.json").read_text("utf-8"))["names"].items()}
    except (OSError, ValueError, KeyError):
        names = {}
    batch = args.input.is_dir()
    if batch:
        files = sorted(args.input.glob("*.pso"))
        if not files:
            parser.error("no PSO files in input folder")
        args.output.mkdir(parents=True, exist_ok=True)
    else:
        files = [args.input]
        args.output.parent.mkdir(parents=True, exist_ok=True)
    failures = 0
    for i, source in enumerate(files, 1):
        target = args.output / (source.stem + ".json") if batch else args.output
        try:
            if target.exists():
                raise FileExistsError(f"output already exists: {target}")
            write_json(target, decode(source, names))
        except (OSError, ValueError, struct.error) as error:
            failures += 1
            print(f"{source.name}: {error}", file=sys.stderr)
        if batch and i % 1000 == 0:
            print(f"Processed {i}/{len(files)}", file=sys.stderr)
    print(f"{len(files) - failures} decoded, {failures} failed.", file=sys.stderr)
    return bool(failures)


if __name__ == "__main__":
    sys.exit(main())
