# SPDX-License-Identifier: GPL-3.0-or-later
"""Index embedded drawable roots in RPF9 dictionaries using their paired hash and pointer tables."""
import re
import struct


from drawables import read_stream, word
from script_tree import _name_hash

PTR = struct.Struct("<Q")
NAME = re.compile(rb"([a-z0-9_-]+)\.#dr\x00")
UNIT = 64 << 20
BASE = 0x50000000


def addr(data, field):
    return PTR.unpack_from(data,field)[0]-BASE


def supported(header):
    if len(header)<64 or header[:8]!=bytes(8) or not 0 <= addr(header,8) < 32<<20:
        return False
    if header[16:24]!=bytes(8):
        return False
    hashes, roots = addr(header,32),addr(header,48)
    count, capacity = struct.unpack_from("<HH",header,40)
    return (0 <= hashes < 32<<20 and 0 <= roots < 32<<20 and 0<count<=capacity<=65535
            and header[40:44]==header[56:60])


def read_dictionary(src, offset):
    system, next_block = read_stream(src,offset+16)
    if not supported(system):
        raise ValueError("unsupported dictionary")
    def grow(want):
        nonlocal next_block
        if not 0 < want <= 128<<20:
            raise ValueError("invalid dictionary pointer")
        while len(system) < want:
            extra,next_block=read_stream(src,next_block)
            system.extend(extra)
    n=struct.unpack_from("<H",system,40)[0]
    ht,rt=addr(system,32),addr(system,48)
    grow(max(ht+n*4,rt+n*8))
    raw=[(word(system,ht+4*k),addr(system,rt+8*k)) for k in range(n)]
    grow(max(root+176 for h,root in raw))
    named=[]
    for h,root in raw:
        if root<0:
            raise ValueError("invalid drawable pointer")
        name_at=addr(system,root+168)
        grow(name_at+1)
        if system.find(b"\0",name_at,min(name_at+512,len(system)))<0:
            grow(name_at+512)
        name=NAME.match(system,name_at)
        if not name:
            continue
        text=name[1].decode()
        if h==_name_hash(text):
            named.append((h,root,text))
    return system,next_block,named
