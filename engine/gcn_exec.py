# SPDX-License-Identifier: GPL-3.0-or-later
"""A small executor for the AMD GCN/RDNA (gfx10) compute listings (see docs/strand-shaders.md; a research tool).

It runs one disassembled compute program for many threads at once: every thread is a lane of one wide wave,
with an exec mask, per-lane vector registers and scalar registers that hold either a uint32 or a lane mask.
That is exact for these programs as long as scalar values are uniform, which holds for the smh shaders
(scalars come from per-instance records and constants).

Only the instructions used by the smh_gen_* InterpolateStrands shaders are implemented; anything else raises.
Memory: scalar pointer regions (descriptor tables, heap), buffer resources addressed by descriptor dword 0,
the program image for pc-relative tables, and a texture callback. See run_program().
"""
import re
import struct

import numpy as np

M32 = 0xFFFFFFFF
F32 = np.float32
FLOAT_CONST = {"0.5": 0x3F000000, "-0.5": 0xBF000000, "1.0": 0x3F800000, "-1.0": 0xBF800000, "2.0": 0x40000000,
               "-2.0": 0xC0000000, "4.0": 0x40800000, "-4.0": 0xC0800000, "0.15915494": 0x3E22F983}
SEL = {"DWORD": (0, 32), "WORD_0": (0, 16), "WORD_1": (16, 16), "BYTE_0": (0, 8), "BYTE_1": (8, 8),
       "BYTE_2": (16, 8), "BYTE_3": (24, 8)}
IMAGE_BASE = 0x7F000000


class ExecError(RuntimeError):
    pass


def f(u):
    return np.asarray(u, np.uint32).view(F32)


def u(x):
    return np.asarray(x, F32).view(np.uint32)


def h2f(bits):
    return np.asarray(np.asarray(bits, np.uint32) & 0xFFFF, np.uint16).view(np.float16).astype(F32)


def f2h(x):
    return np.asarray(x, F32).astype(np.float16).view(np.uint16).astype(np.uint32)


class Instr:
    __slots__ = ("pc", "op", "args", "mods", "text")

    def __init__(self, pc, op, args, mods, text):
        self.pc, self.op, self.args, self.mods, self.text = pc, op, args, mods, text


def parse_listing(path):
    prog = {}
    for line in open(path, encoding="utf-8", errors="replace"):
        if line.startswith(";"):
            continue
        m = re.match(r"(\d+) (\S+)\s*(.*)", line.rstrip("\n"))
        if not m:
            continue
        pc, op, rest = int(m.group(1)), m.group(2), m.group(3)
        if op in ("VOP3", "Undecoded") or "Warning" in rest:
            prog[pc] = Instr(pc, op, [], {}, line.strip())
            continue
        mods = {}
        for mm in re.finditer(r"(\w+):(\[[^\]]*\])", rest):
            mods[mm.group(1)] = [int(x) for x in mm.group(2)[1:-1].split(",")]
        rest = re.sub(r"(\w+):(\[[^\]]*\])", "", rest)
        parts = [p.strip() for p in rest.split(",")]
        args = []
        for i, p in enumerate(parts):
            toks = p.split()
            if not toks:
                continue
            args.append(toks[0])
            for t in toks[1:]:
                if ":" in t:
                    k, v = t.split(":", 1)
                    mods[k] = v
                else:
                    mods[t] = True
        prog[pc] = Instr(pc, op, args, mods, line.strip())
    return prog


class Machine:
    def __init__(self, n, program_bytes=b""):
        self.n = n
        self.V = np.zeros((256, n), np.uint32)
        self.S = {}
        self.exec = np.ones(n, bool)
        self.vcc = np.zeros(n, bool)
        self.scc = False
        self.smem = []          # (base, bytes) scalar-addressable regions
        self.buffers = {}       # descriptor id -> bytearray
        self.stores = []
        self.image = program_bytes
        self.texture = None     # callback(desc_words, coords list of float arrays, dmask) -> list of float arrays
        self.trace = None

    # ---------------- register access ----------------
    def sget(self, name):
        if name in ("vcc_lo", "vcc"):
            return self.vcc.copy()
        if name in ("exec_lo", "exec"):
            return self.exec.copy()
        if name == "null":
            return 0
        if name == "src_scc":
            return int(self.scc)
        m = re.fullmatch(r"s(\d+)", name)
        if m:
            return self.S.get(int(m.group(1)), 0)
        raise ExecError("scalar operand " + name)

    def sset(self, name, val):
        if name in ("vcc_lo", "vcc"):
            self.vcc = self._as_mask(val)
        elif name in ("exec_lo", "exec"):
            self.exec = self._as_mask(val)
        elif name == "null":
            pass
        else:
            m = re.fullmatch(r"s(\d+)", name)
            if not m:
                raise ExecError("scalar dest " + name)
            self.S[int(m.group(1))] = val if isinstance(val, np.ndarray) else int(val) & M32

    def _as_mask(self, val):
        if isinstance(val, np.ndarray):
            return val.astype(bool)
        if int(val) & M32 == M32:
            return np.ones(self.n, bool)
        if int(val) == 0:
            return np.zeros(self.n, bool)
        raise ExecError("scalar value used as a lane mask")

    def sregs(self, name):
        m = re.fullmatch(r"s\[(\d+):(\d+)\]", name)
        if m:
            return list(range(int(m.group(1)), int(m.group(2)) + 1))
        m = re.fullmatch(r"s(\d+)", name)
        if m:
            return [int(m.group(1))]
        raise ExecError("scalar range " + name)

    def vregs(self, name):
        m = re.fullmatch(r"v\[(\d+):(\d+)\]", name)
        if m:
            return list(range(int(m.group(1)), int(m.group(2)) + 1))
        m = re.fullmatch(r"v(\d+)", name)
        if m:
            return [int(m.group(1))]
        raise ExecError("vector range " + name)

    def literal(self, tok):
        if tok in FLOAT_CONST:
            return FLOAT_CONST[tok]
        try:
            v = int(tok, 0)
        except ValueError:
            raise ExecError("operand " + tok)
        return v & M32

    def scalar(self, tok):
        """A scalar source as an int (masks are returned as arrays)."""
        if re.fullmatch(r"s\d+|vcc_lo|vcc|exec_lo|exec|null|src_scc", tok):
            return self.sget(tok)
        return self.literal(tok)

    def vsrc(self, tok):
        """A source as a uint32 lane array, with float modifiers split off: (array, neg, abs)."""
        neg = abs_ = False
        if tok in FLOAT_CONST:
            return np.full(self.n, FLOAT_CONST[tok], np.uint32), False, False
        if tok.startswith("-") and not re.fullmatch(r"-(0x[0-9a-f]+|\d+)", tok):
            neg, tok = True, tok[1:]
        if tok.startswith("|") and tok.endswith("|"):
            abs_, tok = True, tok[1:-1]
        if tok.startswith("v"):
            arr = self.V[self.vregs(tok)[0]].copy()
        else:
            s = self.scalar(tok)
            if isinstance(s, np.ndarray):
                arr = s.astype(np.uint32)
            else:
                arr = np.full(self.n, s, np.uint32)
        return arr, neg, abs_

    def vint(self, tok):
        a, neg, abs_ = self.vsrc(tok)
        if neg or abs_:
            raise ExecError("float modifier on integer op: " + tok)
        return a.astype(np.uint64) & M32

    def vflt(self, tok):
        a, neg, abs_ = self.vsrc(tok)
        x = f(a).copy()
        if abs_:
            x = np.abs(x)
        if neg:
            x = -x
        return x

    def vwrite(self, ins, dest, val, *, float_out=False):
        if float_out:
            x = np.asarray(val, F32)
            if ins.mods.get("clamp"):
                x = np.clip(np.nan_to_num(x, nan=0.0), 0.0, 1.0).astype(F32)
            if ins.mods.get("div") == "2":
                x = (x * F32(0.5)).astype(F32)
            val = u(x)
        val = np.asarray(val).astype(np.uint64) & M32
        reg = self.vregs(dest)[0]
        sel = ins.mods.get("dst_sel")
        if sel and sel != "DWORD":
            lo, width = SEL[sel]
            keep = self.V[reg].astype(np.uint64) if ins.mods.get("dst_unused") == "UNUSED_PRESERVE" else np.zeros(self.n, np.uint64)
            field = ((1 << width) - 1) << lo
            val = (keep & ~np.uint64(field)) | ((val << np.uint64(lo)) & np.uint64(field))
        self.V[reg] = np.where(self.exec, val.astype(np.uint32), self.V[reg])

    def sdwa(self, ins, idx, arr):
        sel = ins.mods.get("src%d_sel" % idx)
        if not sel or sel == "DWORD":
            return arr
        lo, width = SEL[sel]
        return (arr >> np.uint64(lo)) & np.uint64((1 << width) - 1)

    # ---------------- memory ----------------
    def smem_read(self, addr, count):
        for base, data in self.smem:
            if base <= addr < base + len(data):
                off = addr - base
                return list(struct.unpack_from("<%dI" % count, bytes(data[off:off + 4 * count]).ljust(4 * count, b"\0")))
        raise ExecError("scalar load outside memory at %#x" % addr)

    def buf_words(self, desc_regs):
        return [self.S.get(r, 0) for r in desc_regs]

    def buf_read(self, desc, byte_addr, nwords):
        data = self.buffers.get(desc[0] & M32)
        if data is None:
            raise ExecError("unknown buffer descriptor %#x" % desc[0])
        out = []
        size = len(data)
        arr = np.frombuffer(bytes(data) + b"\0" * 16, np.uint8)
        for k in range(nwords):
            a = byte_addr.astype(np.int64) + 4 * k
            ok = (a >= 0) & (a + 4 <= size)
            a = np.where(ok, a, 0)
            w = (arr[a].astype(np.uint32) | (arr[a + 1].astype(np.uint32) << 8) | (arr[a + 2].astype(np.uint32) << 16)
                 | (arr[a + 3].astype(np.uint32) << 24))
            out.append(np.where(ok, w, 0).astype(np.uint32))
        return out

    def mubuf_address(self, ins, desc):
        vaddr, soff = ins.args[1], ins.args[3] if len(ins.args) > 3 else "0"
        base = np.zeros(self.n, np.int64)
        imm = int(ins.mods.get("offset", "0"), 0)
        so = self.scalar(soff) if soff != "0" else 0
        if ins.mods.get("idxen"):
            stride = (desc[1] >> 16) & 0x3FFF
            base = self.V[self.vregs(vaddr)[0]].astype(np.int64) * stride
        elif ins.mods.get("offen"):
            base = self.V[self.vregs(vaddr)[0]].astype(np.int64)
        return base + int(so) + imm

    # ---------------- execution ----------------
    def run(self, prog, start=0, limit=5_000_000):
        pcs = sorted(prog)
        pc = start
        steps = 0
        while True:
            ins = prog.get(pc)
            if ins is None:
                raise ExecError("no instruction at pc %d" % pc)
            steps += 1
            if steps > limit:
                raise ExecError("step limit")
            if self.trace:
                self.trace(self, ins)
            nxt = self.step(ins)
            if nxt == "end":
                return
            if nxt is None:
                i = pcs.index(pc)
                pc = pcs[i + 1]
            else:
                pc = nxt

    def branch_target(self, ins):
        simm = int(ins.args[0], 0)
        if simm >= 0x8000:
            simm -= 0x10000
        return ins.pc + 4 + 4 * simm

    def step(self, ins):
        op, a = ins.op, ins.args
        if op in ("VOP3",) and ins.pc < 24:
            return None
        if op.startswith("s_waitcnt") or op in ("s_nop", "v_nop", "s_inst_prefetch"):
            return None
        if op == "s_endpgm":
            return "end"
        if op == "s_setreg_imm32_b32":
            return None
        h = getattr(self, "op_" + op, None)
        if h is None:
            raise ExecError("unimplemented %s (pc %d): %s" % (op, ins.pc, ins.text))
        return h(ins, a)

    # ---- branches ----
    def op_s_branch(self, ins, a):
        return self.branch_target(ins)

    def op_s_cbranch_execz(self, ins, a):
        return self.branch_target(ins) if not self.exec.any() else None

    def op_s_cbranch_scc0(self, ins, a):
        return self.branch_target(ins) if not self.scc else None

    def op_s_cbranch_scc1(self, ins, a):
        return self.branch_target(ins) if self.scc else None

    def op_s_cbranch_vccz(self, ins, a):
        return self.branch_target(ins) if not self.vcc.any() else None

    def op_s_cbranch_vccnz(self, ins, a):
        return self.branch_target(ins) if self.vcc.any() else None

    # ---- scalar ALU ----
    def _s2(self, ins, fn, set_scc=None):
        x, y = self.scalar(ins.args[1]), self.scalar(ins.args[2])
        if isinstance(x, np.ndarray) or isinstance(y, np.ndarray):
            r = fn(self._as_mask(x), self._as_mask(y))
            self.sset(ins.args[0], r)
            self.scc = bool(np.asarray(r).any())
            return None
        r = fn(x, y) & M32
        self.sset(ins.args[0], r)
        if set_scc == "nz":
            self.scc = r != 0
        return None

    def op_s_mov_b32(self, ins, a):
        self.sset(a[0], self.scalar(a[1]))

    op_s_movk_i32 = op_s_mov_b32

    def op_s_add_i32(self, ins, a):
        x, y = self.scalar(a[1]), self.scalar(a[2])
        self.sset(a[0], (x + y) & M32)

    def op_s_add_u32(self, ins, a):
        x, y = self.scalar(a[1]), self.scalar(a[2])
        r = x + y
        self.sset(a[0], r & M32)
        self.scc = r > M32

    def op_s_addc_u32(self, ins, a):
        x, y = self.scalar(a[1]), self.scalar(a[2])
        r = x + y + int(self.scc)
        self.sset(a[0], r & M32)
        self.scc = r > M32

    def op_s_addk_i32(self, ins, a):
        k = self.literal(a[1])
        if k >= 0x8000 and k < 0x10000:
            k -= 0x10000
        self.sset(a[0], (self.scalar(a[0]) + k) & M32)

    def op_s_sub_i32(self, ins, a):
        self.sset(a[0], (self.scalar(a[1]) - self.scalar(a[2])) & M32)

    def op_s_mul_i32(self, ins, a):
        self.sset(a[0], (self.scalar(a[1]) * self.scalar(a[2])) & M32)

    def op_s_mulk_i32(self, ins, a):
        self.sset(a[0], (self.scalar(a[0]) * self.literal(a[1])) & M32)

    def op_s_mul_hi_u32(self, ins, a):
        self.sset(a[0], (self.scalar(a[1]) * self.scalar(a[2])) >> 32)

    def op_s_lshl_b32(self, ins, a):
        r = (self.scalar(a[1]) << (self.scalar(a[2]) & 31)) & M32
        self.sset(a[0], r)
        self.scc = r != 0

    def op_s_lshr_b32(self, ins, a):
        r = self.scalar(a[1]) >> (self.scalar(a[2]) & 31)
        self.sset(a[0], r)
        self.scc = r != 0

    def op_s_bfe_u32(self, ins, a):
        x, c = self.scalar(a[1]), self.scalar(a[2])
        off, width = c & 31, (c >> 16) & 0x7F
        r = (x >> off) & ((1 << width) - 1) if width else 0
        self.sset(a[0], r)
        self.scc = r != 0

    def op_s_and_b32(self, ins, a):
        return self._s2(ins, lambda x, y: x & y, "nz")

    def op_s_or_b32(self, ins, a):
        return self._s2(ins, lambda x, y: x | y, "nz")

    def op_s_xor_b32(self, ins, a):
        x, y = self.scalar(a[1]), self.scalar(a[2])
        if isinstance(x, np.ndarray) or isinstance(y, np.ndarray):
            r = self._as_mask(x) ^ self._as_mask(y)
            self.sset(a[0], r)
            self.scc = bool(r.any())
            return None
        r = (x ^ y) & M32
        self.sset(a[0], r)
        self.scc = r != 0

    def op_s_andn2_b32(self, ins, a):
        return self._s2(ins, lambda x, y: x & ~y if isinstance(x, np.ndarray) else x & (~y & M32), "nz")

    def op_s_and_saveexec_b32(self, ins, a):
        src = self._as_mask(self.scalar(a[1]))
        old = self.exec.copy()
        self.exec = src & old
        self.sset(a[0], old)
        self.scc = bool(self.exec.any())

    def op_s_or_saveexec_b32(self, ins, a):
        src = self._as_mask(self.scalar(a[1]))
        old = self.exec.copy()
        self.exec = src | old
        self.sset(a[0], old)
        self.scc = bool(self.exec.any())

    def op_s_cselect_b32(self, ins, a):
        self.sset(a[0], self.scalar(a[1]) if self.scc else self.scalar(a[2]))

    def _scmp(self, ins, fn, signed=False):
        x, y = self.scalar(ins.args[0]), self.scalar(ins.args[1])
        if signed:
            x = x - (1 << 32) if x & 0x80000000 else x
            y = y - (1 << 32) if y & 0x80000000 else y
        self.scc = bool(fn(x, y))

    def op_s_cmp_eq_u32(self, ins, a):
        self._scmp(ins, lambda x, y: x == y)

    def op_s_cmp_lg_u32(self, ins, a):
        self._scmp(ins, lambda x, y: x != y)

    def op_s_cmp_gt_u32(self, ins, a):
        self._scmp(ins, lambda x, y: x > y)

    def op_s_cmp_lt_u32(self, ins, a):
        self._scmp(ins, lambda x, y: x < y)

    def op_s_cmp_ge_i32(self, ins, a):
        self._scmp(ins, lambda x, y: x >= y, True)

    def op_s_cmp_le_i32(self, ins, a):
        self._scmp(ins, lambda x, y: x <= y, True)

    def op_s_cmp_lt_i32(self, ins, a):
        self._scmp(ins, lambda x, y: x < y, True)

    def op_s_bitcmp1_b32(self, ins, a):
        self.scc = bool((self.scalar(a[0]) >> (self.scalar(a[1]) & 31)) & 1)

    def op_s_getpc_b64(self, ins, a):
        regs = self.sregs(a[0])
        addr = IMAGE_BASE + ins.pc + 4
        self.S[regs[0]] = addr & M32
        self.S[regs[1]] = addr >> 32

    # ---- scalar memory ----
    def _sload(self, ins, a, count):
        dst = self.sregs(a[0])
        ptr = self.sregs(a[1])
        base = self.S.get(ptr[0], 0) | (self.S.get(ptr[1], 0) << 32)
        off = self.scalar(a[2])
        words = self.smem_read(base + off, count)
        for r, w in zip(dst, words):
            self.S[r] = w

    def op_s_load_dwordx2(self, ins, a):
        self._sload(ins, a, 2)

    def op_s_load_dwordx4(self, ins, a):
        self._sload(ins, a, 4)

    def op_s_load_dwordx8(self, ins, a):
        self._sload(ins, a, 8)

    def _sbuf(self, ins, a, count):
        dst = self.sregs(a[0])
        desc = self.buf_words(self.sregs(a[1]))
        off = self.scalar(a[2])
        words = self.buf_read(desc, np.array([off], np.int64), count)
        for r, w in zip(dst, words):
            self.S[r] = int(w[0])

    def op_s_buffer_load_dword(self, ins, a):
        self._sbuf(ins, a, 1)

    def op_s_buffer_load_dwordx2(self, ins, a):
        self._sbuf(ins, a, 2)

    def op_s_buffer_load_dwordx4(self, ins, a):
        self._sbuf(ins, a, 4)

    def op_s_buffer_load_dwordx8(self, ins, a):
        self._sbuf(ins, a, 8)

    # ---- vector memory ----
    def _vload(self, ins, a, count, fmt=False):
        desc = self.buf_words(self.sregs(a[2]))
        addr = self.mubuf_address(ins, desc)
        words = self.buf_read(desc, addr, count)
        for r, w in zip(self.vregs(a[0]), words):
            self.V[r] = np.where(self.exec, w, self.V[r])

    def op_buffer_load_dword(self, ins, a):
        self._vload(ins, a, 1)

    def op_buffer_load_dwordx2(self, ins, a):
        self._vload(ins, a, 2)

    def op_buffer_load_dwordx3(self, ins, a):
        self._vload(ins, a, 3)

    def op_buffer_load_dwordx4(self, ins, a):
        self._vload(ins, a, 4)

    def op_buffer_load_format_x(self, ins, a):
        self._vload(ins, a, 1, fmt=True)

    def _vstore(self, ins, a, count):
        desc = self.buf_words(self.sregs(a[2]))
        addr = self.mubuf_address(ins, desc)
        regs = self.vregs(a[0])
        data = self.buffers[desc[0] & M32]
        view = np.frombuffer(data, np.uint8)
        lanes = np.nonzero(self.exec)[0]
        for k in range(count):
            p = addr[lanes] + 4 * k
            ok = (p >= 0) & (p + 4 <= len(data)) & (p % 4 == 0)
            if not ok.all() and ((p % 4 != 0) & (p >= 0)).any():
                raise ExecError("unaligned store")
            words = view[: len(data) // 4 * 4].view(np.uint32)
            words[(p[ok] // 4).astype(np.int64)] = self.V[regs[k]][lanes[ok]]
        self.stores.append((ins.pc, count, int(self.exec.sum())))

    def op_buffer_store_dword(self, ins, a):
        self._vstore(ins, a, 1)

    def op_buffer_store_dwordx3(self, ins, a):
        self._vstore(ins, a, 3)

    def op_buffer_store_dwordx4(self, ins, a):
        self._vstore(ins, a, 4)

    def op_global_load_dwordx2(self, ins, a):
        src = self.vregs(a[1])
        addr = self.V[src[0]].astype(np.int64) | (self.V[src[1]].astype(np.int64) << 32)
        off = addr - IMAGE_BASE
        img = np.frombuffer(self.image + b"\0" * 16, np.uint8)
        for k, r in enumerate(self.vregs(a[0])):
            p = np.clip(off + 4 * k, 0, len(self.image))
            w = img[p].astype(np.uint32) | (img[p + 1].astype(np.uint32) << 8) | (img[p + 2].astype(np.uint32) << 16) | (img[p + 3].astype(np.uint32) << 24)
            self.V[r] = np.where(self.exec, w, self.V[r])

    def op_image_sample_lz(self, ins, a):
        coords = [f(self.V[r]) for r in self.vregs(a[1])]
        desc = self.buf_words(self.sregs(a[2]))
        out = self.texture(desc, coords, ins.mods.get("dmask"))
        for r, x in zip(self.vregs(a[0]), out):
            self.V[r] = np.where(self.exec, u(np.asarray(x, F32) + np.zeros(self.n, F32)), self.V[r])

    # ---- vector ALU: integer ----
    def _vint2(self, ins, fn):
        x = self.sdwa(ins, 0, self.vint(ins.args[1]))
        y = self.sdwa(ins, 1, self.vint(ins.args[2]))
        self.vwrite(ins, ins.args[0], fn(x, y))

    def op_v_mov_b32_e32(self, ins, a):
        self.vwrite(ins, a[0], self.vint(a[1]))

    def op_v_add_nc_u32_e32(self, ins, a):
        self._vint2(ins, lambda x, y: x + y)

    op_v_add_nc_u32_sdwa = op_v_add_nc_u32_e32

    def op_v_sub_nc_u32_e32(self, ins, a):
        self._vint2(ins, lambda x, y: x - y)

    op_v_sub_nc_u32_sdwa = op_v_sub_nc_u32_e32

    def op_v_subrev_nc_u32_e32(self, ins, a):
        self._vint2(ins, lambda x, y: y - x)

    def op_v_and_b32_e32(self, ins, a):
        self._vint2(ins, lambda x, y: x & y)

    op_v_and_b32_sdwa = op_v_and_b32_e32

    def op_v_or_b32_e32(self, ins, a):
        self._vint2(ins, lambda x, y: x | y)

    op_v_or_b32_sdwa = op_v_or_b32_e32

    def op_v_xor_b32_e32(self, ins, a):
        self._vint2(ins, lambda x, y: x ^ y)

    op_v_xor_b32_sdwa = op_v_xor_b32_e32

    def op_v_xnor_b32_e32(self, ins, a):
        self._vint2(ins, lambda x, y: ~(x ^ y) & np.uint64(M32))

    def op_v_lshlrev_b32_e32(self, ins, a):
        self._vint2(ins, lambda x, y: y << (x & np.uint64(31)))

    op_v_lshlrev_b32_e64 = op_v_lshlrev_b32_e32
    op_v_lshlrev_b32_sdwa = op_v_lshlrev_b32_e32

    def op_v_lshrrev_b32_e32(self, ins, a):
        self._vint2(ins, lambda x, y: y >> (x & np.uint64(31)))

    def op_v_mul_lo_u32(self, ins, a):
        self._vint2(ins, lambda x, y: x * y)

    def op_v_mul_hi_u32(self, ins, a):
        self._vint2(ins, lambda x, y: (x * y) >> np.uint64(32))

    def op_v_mul_u32_u24_e32(self, ins, a):
        self._vint2(ins, lambda x, y: (x & np.uint64(0xFFFFFF)) * (y & np.uint64(0xFFFFFF)))

    op_v_mul_u32_u24_sdwa = op_v_mul_u32_u24_e32

    def op_v_max_u32_sdwa(self, ins, a):
        self._vint2(ins, np.maximum)

    def op_v_min_u32_e32(self, ins, a):
        self._vint2(ins, np.minimum)

    def op_v_max_i32_e32(self, ins, a):
        def mx(x, y):
            xs = x.astype(np.int64) - ((x >> np.uint64(31)) << np.uint64(32)).astype(np.int64)
            ys = y.astype(np.int64) - ((y >> np.uint64(31)) << np.uint64(32)).astype(np.int64)
            return (np.maximum(xs, ys) & M32).astype(np.uint64)
        self._vint2(ins, mx)

    def _vint3(self, ins, fn):
        x, y, z = (self.vint(t) for t in ins.args[1:4])
        self.vwrite(ins, ins.args[0], fn(x, y, z))

    def op_v_add3_u32(self, ins, a):
        self._vint3(ins, lambda x, y, z: x + y + z)

    def op_v_xad_u32(self, ins, a):
        self._vint3(ins, lambda x, y, z: (x ^ y) + z)

    def op_v_lshl_add_u32(self, ins, a):
        self._vint3(ins, lambda x, y, z: ((x << (y & np.uint64(31))) & np.uint64(M32)) + z)

    def op_v_lshl_or_b32(self, ins, a):
        self._vint3(ins, lambda x, y, z: ((x << (y & np.uint64(31))) & np.uint64(M32)) | z)

    def op_v_and_or_b32(self, ins, a):
        self._vint3(ins, lambda x, y, z: (x & y) | z)

    def op_v_or3_b32(self, ins, a):
        self._vint3(ins, lambda x, y, z: x | y | z)

    def op_v_mad_u32_u24(self, ins, a):
        self._vint3(ins, lambda x, y, z: (x & np.uint64(0xFFFFFF)) * (y & np.uint64(0xFFFFFF)) + z)

    def op_v_bfe_u32(self, ins, a):
        def bfe(x, o, w):
            o = o & np.uint64(31)
            w = w & np.uint64(31)
            return np.where(w == 0, np.uint64(0), (x >> o) & ((np.uint64(1) << w) - np.uint64(1)))
        self._vint3(ins, bfe)

    def op_v_bfrev_b32_e32(self, ins, a):
        x = self.vint(a[1]).astype(np.uint32)
        r = np.zeros_like(x)
        for i in range(32):
            r |= ((x >> np.uint32(i)) & np.uint32(1)) << np.uint32(31 - i)
        self.vwrite(ins, a[0], r)

    def _carry_out(self, ins, dst_mask_name, mask):
        if dst_mask_name in ("vcc_lo", "vcc"):
            self.vcc = np.where(self.exec, mask, self.vcc)
        else:
            old = self.sget(dst_mask_name)
            old = old if isinstance(old, np.ndarray) else np.zeros(self.n, bool)
            self.sset(dst_mask_name, np.where(self.exec, mask, old))

    def op_v_add_co_u32(self, ins, a):
        # v_add_co_u32 vD, sCarry, src0, src1
        x, y = self.vint(a[2]), self.vint(a[3])
        r = x + y
        self.vwrite(ins, a[0], r)
        self._carry_out(ins, a[1], r > M32)

    def op_v_add_co_ci_u32_e32(self, ins, a):
        # e32: vD, vcc, src0, src1, vcc
        x, y = self.vint(a[2]), self.vint(a[3])
        c = self.vcc.astype(np.uint64)
        r = x + y + c
        self.vwrite(ins, a[0], r)
        self._carry_out(ins, a[1], r > M32)

    def op_v_add_co_ci_u32_e64(self, ins, a):
        x, y = self.vint(a[2]), self.vint(a[3])
        cin = self.sget(a[4])
        c = (cin if isinstance(cin, np.ndarray) else np.full(self.n, bool(cin))).astype(np.uint64)
        r = x + y + c
        self.vwrite(ins, a[0], r)
        self._carry_out(ins, a[1], r > M32)

    def op_v_cndmask_b32_e32(self, ins, a):
        x, y = self.vsrc(a[1])[0], self.vsrc(a[2])[0]
        m = self.vcc if len(a) < 4 else self._as_mask(self.scalar(a[3]))
        self.vwrite(ins, a[0], np.where(m, y, x))

    def op_v_cndmask_b32_e64(self, ins, a):
        x, nx, ax = self.vsrc(a[1])
        y, ny, ay = self.vsrc(a[2])
        if nx or ax or ny or ay:
            xf, yf = f(x), f(y)
            if ax: xf = np.abs(xf)
            if nx: xf = -xf
            if ay: yf = np.abs(yf)
            if ny: yf = -yf
            x, y = u(xf), u(yf)
        m = self._as_mask(self.scalar(a[3]))
        self.vwrite(ins, a[0], np.where(m, y, x))

    def op_v_readfirstlane_b32(self, ins, a):
        x = self.vint(a[1])
        lanes = np.nonzero(self.exec)[0]
        self.sset(a[0], int(x[lanes[0]]) if len(lanes) else 0)

    # ---- comparisons ----
    def _vcmp(self, ins, fn, kind):
        args = ins.args
        cmpx = ins.op.startswith("v_cmpx")
        if cmpx and len(args) == 2:
            dst, srcs = None, args
        else:
            dst, srcs = args[0], args[1:3]
        if kind == "f":
            x, y = self.vflt(srcs[0]), self.vflt(srcs[1])
        else:
            x, y = self.vint(srcs[0]), self.vint(srcs[1])
        r = fn(x, y) & self.exec
        if cmpx:
            self.exec = r
        elif dst in ("vcc_lo", "vcc"):
            self.vcc = r
        else:
            self.sset(dst, r)
        return None

    def _cmp_handlers():
        table = {"eq": np.equal, "ne": np.not_equal, "gt": np.greater, "ge": np.greater_equal, "lt": np.less,
                 "le": np.less_equal}
        out = {}
        for name, fn in table.items():
            for kind in ("u32", "f32"):
                for enc in ("e32", "e64"):
                    for x in ("cmp", "cmpx"):
                        out["op_v_%s_%s_%s_%s" % (x, name, kind, enc)] = (fn, "f" if kind == "f32" else "u")
        return out

    def op_v_cmp_nlt_f32_e32(self, ins, a):
        return self._vcmp(ins, lambda x, y: ~(x < y), "f")

    # ---- vector ALU: float ----
    def _f1(self, ins, fn):
        x = self.vflt(ins.args[1])
        with np.errstate(all="ignore"):
            r = fn(x)
        self.vwrite(ins, ins.args[0], np.asarray(r, F32), float_out=True)

    def _f2(self, ins, fn):
        x, y = self.vflt(ins.args[1]), self.vflt(ins.args[2])
        with np.errstate(all="ignore"):
            r = fn(x, y)
        self.vwrite(ins, ins.args[0], np.asarray(r, F32), float_out=True)

    def op_v_add_f32_e32(self, ins, a):
        self._f2(ins, lambda x, y: (x + y).astype(F32))

    op_v_add_f32_e64 = op_v_add_f32_e32

    def op_v_sub_f32_e32(self, ins, a):
        self._f2(ins, lambda x, y: (x - y).astype(F32))

    op_v_sub_f32_e64 = op_v_sub_f32_e32

    def op_v_mul_legacy_f32_e32(self, ins, a):
        self._f2(ins, lambda x, y: np.where((x == 0) | (y == 0), F32(0), x * y).astype(F32))

    op_v_mul_legacy_f32_e64 = op_v_mul_legacy_f32_e32

    def op_v_max_f32_e32(self, ins, a):
        self._f2(ins, lambda x, y: np.fmax(x, y).astype(F32))

    op_v_max_f32_e64 = op_v_max_f32_e32

    def op_v_min_f32_e32(self, ins, a):
        self._f2(ins, lambda x, y: np.fmin(x, y).astype(F32))

    def op_v_rcp_f32_e32(self, ins, a):
        self._f1(ins, lambda x: (F32(1) / x).astype(F32))

    op_v_rcp_iflag_f32_e32 = op_v_rcp_f32_e32

    def op_v_sqrt_f32_e32(self, ins, a):
        self._f1(ins, lambda x: np.sqrt(x).astype(F32))

    def op_v_rsq_f32_e32(self, ins, a):
        self._f1(ins, lambda x: (F32(1) / np.sqrt(x)).astype(F32))

    def op_v_sin_f32_e32(self, ins, a):
        self._f1(ins, lambda x: np.sin(x.astype(np.float64) * 2 * np.pi).astype(F32))

    def op_v_cos_f32_e32(self, ins, a):
        self._f1(ins, lambda x: np.cos(x.astype(np.float64) * 2 * np.pi).astype(F32))

    def op_v_fract_f32_e32(self, ins, a):
        self._f1(ins, lambda x: (x - np.floor(x)).astype(F32))

    def op_v_trunc_f32_e32(self, ins, a):
        self._f1(ins, lambda x: np.trunc(x).astype(F32))

    def _fma(self, x, y, z):
        return (x.astype(np.float64) * y.astype(np.float64) + z.astype(np.float64)).astype(F32)

    def op_v_fma_f32(self, ins, a):
        x, y, z = (self.vflt(t) for t in a[1:4])
        self.vwrite(ins, a[0], self._fma(x, y, z), float_out=True)

    def op_v_mad_f32(self, ins, a):
        x, y, z = (self.vflt(t) for t in a[1:4])
        with np.errstate(all="ignore"):
            r = ((x * y).astype(F32) + z).astype(F32)
        self.vwrite(ins, a[0], r, float_out=True)

    def op_v_fmac_f32_e32(self, ins, a):
        x, y = self.vflt(a[1]), self.vflt(a[2])
        d = f(self.V[self.vregs(a[0])[0]])
        self.vwrite(ins, a[0], self._fma(x, y, d), float_out=True)

    op_v_fmac_f32_e64 = op_v_fmac_f32_e32

    def op_v_mac_f32_e32(self, ins, a):
        x, y = self.vflt(a[1]), self.vflt(a[2])
        d = f(self.V[self.vregs(a[0])[0]])
        with np.errstate(all="ignore"):
            r = ((x * y).astype(F32) + d).astype(F32)
        self.vwrite(ins, a[0], r, float_out=True)

    op_v_mac_f32_e64 = op_v_mac_f32_e32

    def op_v_fma_mix_f32(self, ins, a):
        sel = ins.mods.get("op_sel", [0, 0, 0])
        hi = ins.mods.get("op_sel_hi", [0, 0, 0])
        vals = []
        for i, tok in enumerate(a[1:4]):
            raw, neg, abs_ = self.vsrc(tok)
            if hi[i]:
                bits = (raw >> np.uint32(16)) if sel[i] else raw
                x = h2f(bits)
            else:
                x = f(raw).copy()
            if abs_:
                x = np.abs(x)
            if neg:
                x = -x
            vals.append(x)
        self.vwrite(ins, a[0], self._fma(*vals), float_out=True)

    # ---- conversions ----
    def op_v_cvt_f32_u32_e32(self, ins, a):
        x = self.sdwa(ins, 0, self.vint(a[1]))
        self.vwrite(ins, a[0], x.astype(F32), float_out=True)

    op_v_cvt_f32_u32_sdwa = op_v_cvt_f32_u32_e32

    def op_v_cvt_f32_i32_e32(self, ins, a):
        x = self.vint(a[1]).astype(np.int64)
        x = np.where(x >= 2 ** 31, x - 2 ** 32, x)
        self.vwrite(ins, a[0], x.astype(F32), float_out=True)

    def _cvt_to_int(self, ins, signed):
        x = self.vflt(ins.args[1]).astype(np.float64)
        x = np.nan_to_num(x, nan=0.0)
        if signed:
            r = np.clip(np.trunc(x), -2 ** 31, 2 ** 31 - 1).astype(np.int64) & M32
        else:
            r = np.clip(np.trunc(x), 0, M32).astype(np.int64)
        self.vwrite(ins, ins.args[0], r.astype(np.uint64))

    def op_v_cvt_u32_f32_e32(self, ins, a):
        self._cvt_to_int(ins, False)

    op_v_cvt_u32_f32_sdwa = op_v_cvt_u32_f32_e32

    def op_v_cvt_i32_f32_e32(self, ins, a):
        self._cvt_to_int(ins, True)

    def _cvt_byte(self, ins, k):
        x = self.sdwa(ins, 0, self.vint(ins.args[1]))
        self.vwrite(ins, ins.args[0], ((x >> np.uint64(8 * k)) & np.uint64(255)).astype(F32), float_out=True)

    def op_v_cvt_f32_ubyte0_e32(self, ins, a):
        self._cvt_byte(ins, 0)

    op_v_cvt_f32_ubyte0_sdwa = op_v_cvt_f32_ubyte0_e32

    def op_v_cvt_f32_ubyte1_e32(self, ins, a):
        self._cvt_byte(ins, 1)

    def op_v_cvt_f32_ubyte2_e32(self, ins, a):
        self._cvt_byte(ins, 2)

    def op_v_cvt_f32_ubyte3_e32(self, ins, a):
        self._cvt_byte(ins, 3)

    def op_v_cvt_f32_f16_e32(self, ins, a):
        raw, neg, abs_ = self.vsrc(a[1])
        raw = self.sdwa(ins, 0, raw.astype(np.uint64))
        sel = ins.mods.get("op_sel")
        if sel and sel[0]:
            raw = raw >> np.uint64(16)
        x = h2f(raw)
        if abs_:
            x = np.abs(x)
        if neg:
            x = -x
        self.vwrite(ins, a[0], x, float_out=True)

    op_v_cvt_f32_f16_e64 = op_v_cvt_f32_f16_e32
    op_v_cvt_f32_f16_sdwa = op_v_cvt_f32_f16_e32

    def op_v_cvt_f16_f32_e32(self, ins, a):
        x = self.vflt(a[1])
        self.vwrite(ins, a[0], f2h(x))

    op_v_cvt_f16_f32_sdwa = op_v_cvt_f16_f32_e32


for _name, (_fn, _kind) in Machine._cmp_handlers().items():
    def _make(fn, kind):
        return lambda self, ins, a: self._vcmp(ins, fn, kind)
    if not hasattr(Machine, _name):
        setattr(Machine, _name, _make(_fn, _kind))
