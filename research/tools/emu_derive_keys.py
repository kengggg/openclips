#!/usr/bin/env python3
"""Emulate deriveSessionKeys (0x2c6688) from SecureSession native lib.
State layout (from disasm):
  [0]      role (1=app)
  [1..11]  our nonce (16B)
  [0x11]    our-nonce-set flag
  [0x12..22] peer nonce (16B)
  [0x22]    peer-nonce-set flag
  [0x23]    handshake-complete flag
  [0x28]    ptr to pairing key (32B)
deriveSessionKeys(x0=state, x8=out48):
  nonce32 = (role==1) ? our||peer : peer||our
  HKDF: init via 0x2c67b8/0x2c67e0 allocs, then 0x2c71e0(buf, label29,
        pairing_key32, nonce32, 32, 48, out48)
We hook the allocs by pre-mapping memory. Instead of hooking malloc, we
point calls 0x2c67b8 (writes alloc'd ptr into [x8]) to scratch memory."""
import os
import struct, hashlib, json
from unicorn import *
from unicorn.arm64_const import *

SO = os.environ.get("CLIPS_NATIVE_LIB", "liblinks_port_android_jni_native_library.so")
data = open(SO, "rb").read()

uc = Uc(UC_ARCH_ARM64, UC_MODE_LITTLE_ENDIAN)
uc.mem_map(0, 0x800000)
uc.mem_write(0, data[:0x800000])
STACK = 0x7f000000
uc.mem_map(STACK - 0x200000, 0x200000)
HEAP = 0x10000000
uc.mem_map(HEAP, 0x1000000)
RET = 0x12340000
uc.mem_map(RET & ~0xFFF, 0x2000)

# sections
e_shoff = struct.unpack_from("<Q", data, 0x28)[0]
e_shentsize, e_shnum, e_shstrndx = struct.unpack_from("<HHH", data, 0x3A)
def sh(i):
    off = e_shoff + i * e_shentsize
    return struct.unpack_from("<IIQQQQIIQQ", data, off)
_, _, _, _, shstr_off, *_ = sh(e_shstrndx)
sections = {}
for i in range(e_shnum):
    name_off, typ, flags, addr, offset, size, link, info, align, entsize = sh(i)
    end = data.index(b"\x00", shstr_off + name_off)
    nm = data[shstr_off + name_off:end].decode()
    sections[nm] = (addr, offset, size)

# apply R_AARCH64_RELATIVE relocs
addr, offset, size = sections[".rela.dyn"]
for pos in range(offset, offset + size, 24):
    r_offset, r_info, r_addend = struct.unpack_from("<QQq", data, pos)
    if (r_info & 0xFFFFFFFF) == 0x403 and r_offset < 0x800000:
        uc.mem_write(r_offset, struct.pack("<Q", r_addend))

# PLT hooks (libc) — same as emu_f3
dynsym_addr, dynsym_off, dynsym_size = sections[".dynsym"]
dynstr_addr, dynstr_off, _ = sections[".dynstr"]
_, rela_plt_off, rela_plt_size = sections[".rela.plt"]
MAGIC_BASE = 0x20000000
uc.mem_map(MAGIC_BASE, 0x10000)
got_to_magic = {}

def make_impl(name):
    def impl(uc_, address, size, ud=None):
        impl.n = getattr(impl, 'n', 0)
        x0 = uc_.reg_read(UC_ARM64_REG_X0)
        x1 = uc_.reg_read(UC_ARM64_REG_X1)
        x2 = uc_.reg_read(UC_ARM64_REG_X2)
        if name == "memcpy":
            uc_.mem_write(x0, bytes(uc_.mem_read(x1, x2)))
            uc_.reg_write(UC_ARM64_REG_X0, x0)
        elif name == "memmove":
            tmp = bytes(uc_.mem_read(x1, x2))
            uc_.mem_write(x0, tmp)
            uc_.reg_write(UC_ARM64_REG_X0, x0)
        elif name == "memset":
            uc_.mem_write(x0, bytes([x1 & 0xFF]) * x2)
            uc_.reg_write(UC_ARM64_REG_X0, x0)
        elif name == "memcmp":
            a = bytes(uc_.mem_read(x0, x2))
            b = bytes(uc_.mem_read(x1, x2))
            r = 0 if a == b else 1
            uc_.reg_write(UC_ARM64_REG_X0, r & 0xFFFFFFFFFFFFFFFF)
        elif name == "malloc":
            a = HEAP + 0x300000 + impl.n
            impl.n += (x0 + 15) & ~15
            uc_.reg_write(UC_ARM64_REG_X0, a)
        elif name == "calloc":
            a = HEAP + 0x300000 + impl.n
            impl.n += (x0 * x1 + 15) & ~15
            uc_.mem_write(a, b"\x00" * (x0 * x1))
            uc_.reg_write(UC_ARM64_REG_X0, a)
        elif name == "free":
            pass
        elif name == "realloc":
            old = bytes(uc_.mem_read(x0, 1024)) if x0 else b""
            a = HEAP + 0x300000 + impl.n
            impl.n += (x2 + 15) & ~15
            uc_.mem_write(a, old[:x2])
            uc_.reg_write(UC_ARM64_REG_X0, a)
        elif name == "abort":
            raise RuntimeError("abort()")
        elif name == "__errno":
            uc_.reg_write(UC_ARM64_REG_X0, HEAP + 0x90000)
        else:
            uc_.reg_write(UC_ARM64_REG_X0, 0)
        lr = uc_.reg_read(UC_ARM64_REG_LR)
        uc_.reg_write(UC_ARM64_REG_PC, lr)
    return impl

for pos in range(rela_plt_off, rela_plt_off + rela_plt_size, 24):
    r_offset, r_info, r_addend = struct.unpack_from("<QQq", data, pos)
    sym_idx = r_info >> 32
    sym_off = dynsym_off + sym_idx * 24
    st_name = struct.unpack_from("<I", data, sym_off)[0]
    end = data.index(b"\x00", dynstr_off + st_name)
    name = data[dynstr_off + st_name:end].decode()
    magic = MAGIC_BASE + len(got_to_magic) * 16
    got_to_magic[r_offset] = (magic, name)
    uc.mem_write(r_offset, struct.pack("<Q", magic))

for got_off, (magic, name) in got_to_magic.items():
    uc.hook_add(UC_HOOK_CODE, make_impl(name), begin=magic, end=magic + 4)

# ---- remove the alloc hooks; let calloc do its thing, then track buffers ----
calloc_tracker = {"n": 0}
_orig_calloc_impls = None
def calloc_tracker_wrap(name):
    def impl(uc_, address, size, ud=None):
        x0 = uc_.reg_read(UC_ARM64_REG_X0)
        x1 = uc_.reg_read(UC_ARM64_REG_X1)
        a = HEAP + 0x300000 + calloc_tracker["n"]
        calloc_tracker["n"] += (x0 * x1 + 15) & ~15
        uc_.mem_write(a, b"\x00" * (x0 * x1))
        uc_.reg_write(UC_ARM64_REG_X0, a)
        uc_.reg_write(UC_ARM64_REG_PC, uc_.reg_read(UC_ARM64_REG_LR))
    return impl

st = json.load(open(os.environ.get("CLIPS_SESSION_STATE", "session_state.json")))
PK = bytes.fromhex(st["pairing_key"])
ON = bytes.fromhex(st["our_nonce"])
LN = bytes.fromhex(st["lens_nonce"])

OBJ = HEAP + 0x60000
KEYPTR = HEAP + 0x1000
OUT48 = HEAP + 0x2000
uc.mem_write(KEYPTR, PK)
uc.mem_write(OBJ, b"\x00" * 0x40)
buf = bytearray(b"\x00" * 0x40)
buf[0] = 1              # role = app
buf[1:17] = ON          # our nonce
buf[0x11] = 1           # our nonce set
buf[0x12:0x22] = LN     # peer nonce
buf[0x22] = 1           # peer nonce set
buf[0x23] = 1           # handshake done
uc.mem_write(OBJ, bytes(buf))
uc.mem_write(OBJ + 0x28, struct.pack("<Q", KEYPTR))

uc.reg_write(UC_ARM64_REG_SP, STACK - 0x1000)
uc.reg_write(UC_ARM64_REG_X0, OBJ)
uc.reg_write(UC_ARM64_REG_X8, OUT48)
uc.reg_write(UC_ARM64_REG_LR, RET)
uc.reg_write(UC_ARM64_REG_TPIDR_EL0, HEAP + 0x80000)
uc.mem_write(HEAP + 0x80000 + 0x28, b"\x00" * 8)  # TLS canary slot

trace = []
hmac_calls = []
def hook_code(uc_, address, size, ud=None):
    trace.append(address)
    if address == 0x2c9854:
        x0 = uc_.reg_read(UC_ARM64_REG_X0)
        x1 = uc_.reg_read(UC_ARM64_REG_X1)
        w2 = uc_.reg_read(UC_ARM64_REG_W2)
        try:
            d = bytes(uc_.mem_read(x1, w2)).hex()
        except Exception:
            d = "?"
        hmac_calls.append(("update", x0, x1, w2, d))
        print(f"  HMAC-update ctx={x0:#x} len={w2} data={d}")
    if address == 0x2c9884:
        x0 = uc_.reg_read(UC_ARM64_REG_X0)
        hmac_calls.append(("final", x0))
        print(f"  HMAC-final ctx={x0:#x}")
    if address in (0x2c6728, 0x2c672c, 0x2c6768, 0x2c6788, 0x2c6700, 0x2c6704):
        print(f"  KEYPOINT {address:#x} insns={len(trace)}")
    if len(trace) > 2_000_000:
        uc_.emu_stop()
h = uc.hook_add(UC_HOOK_CODE, hook_code)
def hook_mem(uc_, access, address, size, value, ud=None):
    print(f"MEM-INVALID access={access} addr={address:#x} pc={uc_.reg_read(UC_ARM64_REG_PC):#x}")
    return False
hm = uc.hook_add(UC_HOOK_MEM_INVALID, hook_mem)
try:
    uc.emu_start(0x2c6688, RET, timeout=60_000_000, count=200_000_000)
except UcError as e:
    print("EMU ERR:", e, "pc:", hex(uc.reg_read(UC_ARM64_REG_PC)))
    print("total insns:", len(trace))
    raise SystemExit(1)
finally:
    uc.hook_del(h); uc.hook_del(hm)

out_ptr = struct.unpack_from("<Q", uc.mem_read(OUT48, 8))[0]
print("OUT48 slot now holds ptr:", hex(out_ptr))
if out_ptr:
    out = bytes(uc.mem_read(out_ptr, 48))
    print("session keys 48B:", out.hex())
    print("first 32:", out[:32].hex())
    print("last 16:", out[32:].hex())
    json.dump({"keys48": out.hex(), "ptr": out_ptr},
              open("session_keys.json", "w"))
else:
    out = bytes(uc.mem_read(OUT48, 48))
    print("OUT48 raw:", out.hex())