# SPDX-License-Identifier: GPL-3.0-or-later
"""Recover PSIN files and raw zlib streams from the observed Xbox RPF9 layouts."""

import argparse
from collections import Counter
import os
from pathlib import Path
import struct
import sys
import tempfile
import time
import zlib


PAGE = 4096
BLOCK = 65536
SCAN_WINDOW = 8 * 1024 * 1024
MAX_OBJECT = 128 * 1024 * 1024


def candidates(scanner, start, stop, include_shifted=False, include_psin=False):
    """Scan sequentially with bounded memory, checking 4 KiB boundaries."""
    base = start // PAGE * PAGE
    shifts = (0, 16) if include_shifted else (0,)
    scanner.seek(base)
    next_update = time.monotonic() + 30
    while base < stop:
        window = scanner.read(min(SCAN_WINDOW, stop - base + 3))
        if not window:
            return
        matches = []
        for shift in shifts:
            for page, first in enumerate(window[shift::PAGE]):
                if first != 0x78 and not (include_psin and first == ord("P")):
                    continue
                i = page * PAGE + shift
                if not start <= base + i < stop or i + 1 >= len(window):
                    continue
                if shift and any(window[i - shift:i]):
                    continue
                if include_psin and window[i:i + 4] == b"PSIN":
                    matches.append(base + i)
                    continue
                if first != 0x78:
                    continue
                flag = window[i + 1]
                if not flag & 0x20 and (0x7800 + flag) % 31 == 0:
                    matches.append(base + i)
        yield from sorted(matches)
        base += len(window)
        if time.monotonic() >= next_update:
            percent = min(100, 100 * (base - start) / (stop - start))
            print(f"Scanned {percent:.1f}% of requested range.", file=sys.stderr)
            next_update = time.monotonic() + 30


def decode_block(reader, offset):
    # Each observed stream expands to at most 64 KiB. An extra page
    # accommodates the overhead of an incompressible DEFLATE stream.
    reader.seek(offset)
    packed = reader.read(BLOCK + PAGE)
    stream = zlib.decompressobj()
    data = stream.decompress(packed, BLOCK + 1)
    if len(data) > BLOCK or not stream.eof:
        raise ValueError("unsupported or incomplete compressed block")
    consumed = len(packed) - len(stream.unused_data)
    return data, offset + consumed


def psin_checksum(data):
    """PSO signed-byte Jenkins checksum with the CHKS size/hash fields cleared."""
    value = 0x3FAC7125
    for part in (memoryview(data)[:-12], memoryview(bytes(8)), memoryview(data)[-4:]):
        for byte in part.cast("b"):
            value = ((value + byte) * 1025) & 0xffffffff
            value ^= value >> 6
    value = (value * 9) & 0xffffffff
    value ^= value >> 11
    return (value * 32769) & 0xffffffff


def recover(reader, offset):
    # Probe only the prefix before spending time on unrelated zlib streams.
    reader.seek(offset)
    probe = reader.read(512)
    raw_start = probe.startswith(b"PSIN")
    if not raw_start:
        try:
            if zlib.decompressobj().decompress(probe, 4) != b"PSIN":
                return None
        except zlib.error:
            return None

    data = bytearray()
    cursor = 0
    blocks = 0
    used_raw = False
    while True:
        try:
            block, end = decode_block(reader, offset)
            raw = False
        except (ValueError, zlib.error):
            if blocks == 0 and not raw_start:
                raise
            reader.seek(offset)
            block = reader.read(BLOCK)
            end = offset + len(block)
            raw = used_raw = True
        if len(data) + len(block) > MAX_OBJECT:
            raise ValueError("object exceeds configured size limit")
        data.extend(block)
        blocks += 1
        while cursor + 8 <= len(data):
            tag, size = struct.unpack_from(">4sI", data, cursor)
            if not tag.isalnum() or size < 8 or cursor + size > MAX_OBJECT:
                raise ValueError("unsupported section layout")
            if cursor + size > len(data):
                break
            if tag == b"CHKS":
                file_end = cursor + size
                if size != 20 or (not raw and file_end != len(data)):
                    raise ValueError("unexpected CHKS layout")
                if raw:
                    del data[file_end:]
                if struct.unpack_from(">I", data, cursor + 8)[0] != len(data):
                    raise ValueError("CHKS total size mismatch")
                if used_raw and struct.unpack_from(">I", data, cursor + 12)[0] != psin_checksum(data):
                    raise ValueError("whole-file CHKS checksum mismatch")
                return data, blocks
            cursor += size
        if len(block) != BLOCK:
            raise ValueError("no complete terminal CHKS section")
        offset = (end + 15) // 16 * 16
        reader.seek(end)
        if any(reader.read(offset - end)):
            raise ValueError("unexpected block padding")


def number(value):
    return int(value, 0)


def save_psin(path, data, resume):
    if resume and path.exists():
        if path.stat().st_size == len(data) and path.read_bytes() == data:
            return False
        raise ValueError(f"existing file differs from decoded bytes: {path}")
    out = tempfile.NamedTemporaryFile(dir=path.parent, suffix=".part", delete=False)
    tmp = Path(out.name)
    try:
        with out:
            out.write(data)
        if os.name == "nt":
            tmp.rename(path)  # Windows rename fails if the destination exists.
        else:
            os.link(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return True


def inflate_stream(reader, offset, destination):
    """Return compressed end and decoded size only after zlib validates the trailer."""
    reader.seek(offset)
    stream = zlib.decompressobj()
    total = 0
    while not stream.eof:
        packed = reader.read(32768)
        if not packed:
            raise ValueError("incomplete compressed stream")
        while packed and not stream.eof:
            data = stream.decompress(packed, 1024 * 1024)
            packed = stream.unconsumed_tail
            total += len(data)
            if destination is not None:
                destination.write(data)
    return reader.tell() - len(stream.unused_data), total


def recover_streams(scanner, reader, start, stop, output):
    count = 0
    consumed_until = 0
    for first in candidates(scanner, start, stop, include_shifted=True):
        if first < consumed_until:
            continue
        offset = first
        while offset < stop:
            name = f"{offset:012x}.bin"
            try:
                if output:
                    temporary = output / (name + ".part")
                    with temporary.open("xb") as out:
                        end, size = inflate_stream(reader, offset, out)
                    temporary.rename(output / name)
                else:
                    end, size = inflate_stream(reader, offset, None)
            except (ValueError, zlib.error):
                if output:
                    temporary.unlink()
                break
            print(f"0x{offset:012x}  {end-offset:10d} packed  {size:10d} decoded  {name}")
            count += 1
            consumed_until = end
            offset = (end + 15) // 16 * 16
            reader.seek(end)
            if any(reader.read(offset - end)):
                break
            head = reader.read(2)
            if len(head) != 2 or head[0] != 0x78 or int.from_bytes(head, "big") % 31:
                break
    print(f"{count} checksum-verified zlib streams.", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--mode", choices=("psin", "streams"), default="psin",
                        help="recover complete PSIN objects or separate raw streams at page and page+16 boundaries")
    parser.add_argument("--output", type=Path, help="save files to a new folder (default: list only)")
    parser.add_argument("--resume", action="store_true", help="verify existing PSIN files and add missing ones")
    parser.add_argument("--start", type=number, default=0, help="first candidate offset, decimal or hex")
    parser.add_argument("--stop", type=number, help="exclusive end offset for candidate starts")
    args = parser.parse_args()
    if args.resume and (not args.output or args.mode != "psin"):
        parser.error("--resume requires --output and --mode psin")
    length = args.archive.stat().st_size
    stop = length if args.stop is None else min(length, args.stop)
    if not 0 <= args.start < stop:
        parser.error("empty or invalid scan range")
    with args.archive.open("rb") as scanner, args.archive.open("rb") as reader:
        if scanner.read(4) != b"9FPR":
            parser.error("expected an RPF9 archive")
        if args.output:
            try:
                args.output.mkdir(parents=True, exist_ok=args.resume)
            except FileExistsError:
                parser.error(f"output path already exists: {args.output}; choose a new folder")
        if args.mode == "streams":
            recover_streams(scanner, reader, args.start, stop, args.output)
            return
        found = 0
        added = 0
        skipped = Counter()
        for offset in candidates(scanner, args.start, stop, include_psin=True):
            try:
                result = recover(reader, offset)
            except (ValueError, zlib.error) as error:
                skipped[str(error)] += 1
                print(f"0x{offset:012x}: {error}", file=sys.stderr)
                continue
            if result is None:
                continue
            data, blocks = result
            name = f"{offset:012x}.pso"
            if args.output:
                added += save_psin(args.output / name, data, args.resume)
            print(f"0x{offset:012x}  {len(data):10d} bytes  {blocks:5d} blocks  {name}")
            found += 1
    print(f"{found} complete PSIN objects. {sum(skipped.values())} PSIN candidates skipped.", file=sys.stderr)
    for reason, count in skipped.items():
        print(f"  {count}: {reason}", file=sys.stderr)
    if args.output:
        print(f"{added} added, {found - added} existing files verified.", file=sys.stderr)
    if skipped:
        sys.exit(1)


if __name__ == "__main__":
    main()
