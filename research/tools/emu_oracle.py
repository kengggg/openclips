#!/usr/bin/env python3
"""Unicorn-based oracle for the Clips native crypto library.
Exposes:
  F(key)                 -> 32B "PRK" (SETKEY via 0x2c9854 + final 0x2c9884)
  derive_keys48(PK, on, peer, role) -> keys48 (via 0x2c6688 full)
Verified against: run6 session_state.json.
NOTE: F does NOT equal standard HMAC (see notes); always use this oracle.
"""
import os
import struct

SO = os.environ.get("CLIPS_NATIVE_LIB", "liblinks_port_android_jni_native_library.so")

class Oracle:
    def __init__(self):
        import unicorn.arm64_const as uc_const
        self.UCR = uc_const
        data = open(SO, "rb").read()
        from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_LITTLE_ENDIAN, UC_HOOK_CODE
        uc = Uc(UC_ARCH_ARM64, UC_MODE_LITTLE_ENDIAN)
        uc.mem_map(0, 0x800000); uc.mem_write(0, data[:0x800000])
        STACK = 0x7f000000; uc.mem_map(STACK - 0x200000, 0x200000)
        HEAP = 0x10000000; uc.mem_map(HEAP, 0x1000000)
        RET = 0x12340000; uc.mem_map(RET & ~0xFFF, 0x2000)
        self.uc = uc; self.STACK = STACK; self.HEAP = HEAP; self.RET = RET
        e_shoff = struct.unpack_from("<Q", data, 0x28)[0]
        e_shentsize, e_shnum, e_shstrndx = struct.unpack_from("<HHH", data, 0x3A)
        def sh(i):
            off = e_shoff + i*e_shentsize
            return struct.unpack_from("<IIQQQQIIQQ", data, off)
        _,_,_,_,shstr_off,*_ = sh(e_shstrndx)
        sections = {}
        for i in range(e_shnum):
            no, typ, fl, addr, off, sz, lk, inf, al, es = sh(i)
            end = data.index(b"\x00", shstr_off+no)
            nm = data[shstr_off+no:end].decode()
            sections[nm] = (addr, off, sz)
        addr, off, sz = sections[".rela.dyn"]
        for pos in range(off, off+sz, 24):
            r_offset, r_info, r_addend = struct.unpack_from("<QQq", data, pos)
            if (r_info & 0xFFFFFFFF) == 0x403 and r_offset < 0x800000:
                uc.mem_write(r_offset, struct.pack("<Q", r_addend))
        dynsym_addr, dynsym_off, _ = sections[".dynsym"]
        dynstr_addr, dynstr_off, _ = sections[".dynstr"]
        _, rela_plt_off, rela_plt_size = sections[".rela.plt"]
        MAGIC_BASE = 0x20000000; uc.mem_map(MAGIC_BASE, 0x10000)
        allocs = []
        R = self.UCR
        def make_impl(name):
            def impl(uc_, address, size, ud=None):
                x0 = uc_.reg_read(R.UC_ARM64_REG_X0); x1 = uc_.reg_read(R.UC_ARM64_REG_X1); x2 = uc_.reg_read(R.UC_ARM64_REG_X2)
                if name == "memcpy":
                    uc_.mem_write(x0, bytes(uc_.mem_read(x1, x2))); uc_.reg_write(R.UC_ARM64_REG_X0, x0)
                elif name == "memmove":
                    t = bytes(uc_.mem_read(x1, x2)); uc_.mem_write(x0, t); uc_.reg_write(R.UC_ARM64_REG_X0, x0)
                elif name == "memset":
                    uc_.mem_write(x0, bytes([x1 & 0xFF]) * x2); uc_.reg_write(R.UC_ARM64_REG_X0, x0)
                elif name == "malloc":
                    a = HEAP + 0x300000 + sum(s for _,s in allocs); allocs.append((a, x0)); uc_.reg_write(R.UC_ARM64_REG_X0, a)
                elif name == "calloc":
                    n = x0*x1
                    a = HEAP + 0x300000 + sum(s for _,s in allocs); allocs.append((a, n)); uc_.mem_write(a, b"\x00"*n); uc_.reg_write(R.UC_ARM64_REG_X0, a)
                elif name == "free": pass
                elif name == "__errno": uc_.reg_write(R.UC_ARM64_REG_X0, self.HEAP + 0x90000)
                else: uc_.reg_write(R.UC_ARM64_REG_X0, 0)
                uc_.reg_write(R.UC_ARM64_REG_PC, uc_.reg_read(R.UC_ARM64_REG_LR))
            return impl
        for pos in range(rela_plt_off, rela_plt_off+rela_plt_size, 24):
            r_offset, r_info, r_addend = struct.unpack_from("<QQq", data, pos)
            sym_idx = r_info >> 32
            sym_off = dynsym_off + sym_idx*24
            st_name = struct.unpack_from("<I", data, sym_off)[0]
            end = data.index(b"\x00", dynstr_off+st_name)
            nm = data[dynstr_off+st_name:end].decode()
            magic = MAGIC_BASE + (r_offset % 0x10000)
            uc.mem_write(r_offset, struct.pack("<Q", magic))
            uc.hook_add(UC_HOOK_CODE, make_impl(nm), begin=magic, end=magic+4)
        self.allocs = allocs
        self.CTX = HEAP + 0x30
        self.DATA = HEAP + 0x1000
        self.OBJ = HEAP + 0x60000
        self.KEYPTR = HEAP + 0x1000 + 0x1000
        self.OUT48 = HEAP + 0x4000

    def _regs(self, **kw):
        from unicorn.arm64_const import UC_ARM64_REG_SP, UC_ARM64_REG_LR, UC_ARM64_REG_TPIDR_EL0, UC_ARM64_REG_X0, UC_ARM64_REG_X1, UC_ARM64_REG_X2, UC_ARM64_REG_X8
        self.uc.reg_write(UC_ARM64_REG_SP, self.STACK - 0x1000)
        self.uc.reg_write(UC_ARM64_REG_LR, self.RET)
        self.uc.reg_write(UC_ARM64_REG_TPIDR_EL0, self.HEAP + 0x80000)
        for k, v in kw.items():
            self.uc.reg_write(getattr(self.UCR, "UC_ARM64_REG_" + k), v)

    def F(self, key):
        """SETKEY(key) then final → digest32."""
        from unicorn.arm64_const import UC_ARM64_REG_X0, UC_ARM64_REG_X1, UC_ARM64_REG_W2
        uc = self.uc
        uc.mem_write(self.CTX, b"\x00"*0x140)
        uc.mem_write(self.DATA, key)
        self._regs(X0=self.CTX, X1=self.DATA)
        uc.reg_write(UC_ARM64_REG_W2, len(key))
        uc.emu_start(0x2c9854, self.RET, timeout=30_000_000, count=100_000_000)
        self._regs(X0=self.CTX)
        uc.emu_start(0x2c9884, self.RET, timeout=30_000_000, count=100_000_000)
        x0 = uc.reg_read(UC_ARM64_REG_X0)
        return bytes(uc.mem_read(x0, 32))

    def MAC(self, key, msg):
        """SETKEY(key); update(msg); final → digest32 (the lib's MAC)."""
        R = self.UCR
        uc = self.uc
        uc.mem_write(self.CTX, b"\x00"*0x140)
        uc.mem_write(self.DATA, key)
        uc.mem_write(self.DATA + 0x400, msg)
        self._regs(X0=self.CTX, X1=self.DATA)
        uc.reg_write(R.UC_ARM64_REG_W2, len(key))
        uc.emu_start(0x2c9854, self.RET, timeout=30_000_000, count=100_000_000)
        # update(msg) via vtable[8] = 0x2c9974
        self._regs(X0=self.CTX, X1=self.DATA + 0x400)
        uc.reg_write(R.UC_ARM64_REG_W2, len(msg))
        uc.emu_start(0x2c9974, self.RET, timeout=30_000_000, count=100_000_000)
        # final
        self._regs(X0=self.CTX)
        uc.emu_start(0x2c9884, self.RET, timeout=30_000_000, count=100_000_000)
        x0 = uc.reg_read(R.UC_ARM64_REG_X0)
        return bytes(uc.mem_read(x0, 32))

    def derive_keys48(self, PK, our16, peer16, role=1):
        """Full 0x2c6688: returns keys48 bytes."""
        from unicorn.arm64_const import UC_ARM64_REG_X0, UC_ARM64_REG_X8
        uc = self.uc
        del self.allocs[:]
        uc.mem_write(self.KEYPTR, PK)
        buf = bytearray(b"\x00"*0x40)
        buf[0] = role; buf[1:17] = our16; buf[0x11] = 1
        buf[0x12:0x22] = peer16; buf[0x22] = 1; buf[0x23] = 1
        uc.mem_write(self.OBJ, bytes(buf))
        uc.mem_write(self.OBJ+0x28, struct.pack("<Q", self.KEYPTR))
        self._regs(X0=self.OBJ)
        uc.reg_write(UC_ARM64_REG_X8, self.OUT48)
        uc.emu_start(0x2c6688, self.RET, timeout=60_000_000, count=200_000_000)
        for a, s in self.allocs:
            if s == 48:
                return bytes(uc.mem_read(a, 48))
        return None

if __name__ == "__main__":
    import json, hmac, hashlib
    o = Oracle()
    # sanity: F vs HMAC
    r = o.F(b"A"*20)
    ref = hmac.new(b"A"*20, b"", hashlib.sha256).hexdigest()
    print("F(A*20):", r.hex())
    print("HMAC ref:", ref)
    # run6 keys
    st = json.load(open(os.environ.get("CLIPS_SESSION_STATE", "session_state.json")))
    PK = bytes.fromhex(st["pairing_key"]); ON = bytes.fromhex(st["our_nonce"]); LN = bytes.fromhex(st["lens_nonce"])
    k = o.derive_keys48(PK, ON, LN, 1)
    print("app keys48:", k.hex() if k else None)
    kl = o.derive_keys48(PK, LN, ON, 2)
    print("lens keys48:", kl.hex() if kl else None)
