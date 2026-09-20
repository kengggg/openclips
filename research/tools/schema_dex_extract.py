#!/usr/bin/env python3
"""Full javalite schema extractor: pulls descriptor strings + Object[] class
lists straight from the Google Clips DEX, then decodes every message into a
field table, producing a complete .proto reconstruction.

DEX parsing: minimal string_id lookup + searching code items is hard; instead
we exploit that jadx already gave us `a(int,Object)` case-2 sources with the
Object[] inline. The descriptor STRING though must come from DEX (jadx
truncates). We locate each class's string via its unique prefix in the DEX
using the (truncated) jadx string as prefix anchor.
"""
import sys, os, re, json, struct

def read_uleb128(d, p):
    r = 0; s = 0
    while True:
        b = d[p]; p += 1
        r |= (b & 0x7F) << s
        if not (b & 0x80):
            return r, p
        s += 7

def decode_mutf8(d, start, endp):
    out = []
    i = start
    while i < endp:
        b = d[i]
        if b < 0x80:
            out.append(chr(b)); i += 1
        elif (b & 0xE0) == 0xC0:
            out.append(chr(((b & 0x1F) << 6) | (d[i+1] & 0x3F))); i += 2
        elif (b & 0xF0) == 0xE0:
            out.append(chr(((b & 0x0F) << 12) | ((d[i+1] & 0x3F) << 6) | (d[i+2] & 0x3F))); i += 3
        else:
            raise ValueError(f"bad mutf8 byte {b:#x} at {i}")
    return ''.join(out)

def read_13bit_varints(s):
    vals = []
    i = 0
    n = len(s)
    while i < n:
        c = ord(s[i]); i += 1
        if c < 0xD800:
            vals.append(c)
        else:
            r = c & 0x1FFF
            shift = 13
            while i < n:
                c2 = ord(s[i]); i += 1
                if c2 < 0xD800:
                    r = (c2 << shift) | r
                    break
                r |= (c2 & 0x1FFF) << shift
                shift += 13
            vals.append(r)
    return vals

# FieldType for tc < 51 (javalite FieldType ordinals)
FT = {0:'double',1:'float',2:'int64',3:'uint64',4:'int32',5:'fixed64',6:'fixed32',
      7:'bool',8:'string',9:'group',10:'message',11:'bytes',12:'uint32',13:'enum',
      14:'sfixed32',15:'sfixed64',16:'sint32',17:'sint64'}
# tc 18..49: "repeated"/packed variants; 27=repeated message?, 49=?, 30,44...
# From gzk: tc==27 or 49 → objarr class ref consumed (enum/message list?).
#           tc==12/30/44 with flags&1 → class ref (enum?)
#           tc==50 → special (map?) consumes 2 refs
# We map observed ones pragmatically.
SPECIAL = {18:'repeated_double',19:'repeated_float',20:'repeated_int64',21:'repeated_uint64',
           22:'repeated_int32',23:'repeated_fixed64',24:'repeated_fixed32',25:'repeated_bool',
           26:'repeated_string',27:'repeated_message',28:'repeated_bytes',29:'repeated_uint32',
           30:'repeated_enum',31:'repeated_sfixed32',32:'repeated_sfixed64',33:'repeated_sint32',
           34:'repeated_sint64',35:'packed_fixed64',36:'packed_fixed32',37:'packed_int64',
           38:'packed_uint64',39:'packed_int32',40:'packed_uint32',41:'packed_sfixed32',
           42:'packed_sfixed64',43:'packed_sint32',44:'packed_sint64',45:'packed_double',
           46:'packed_float',47:'packed_bool',48:'packed_enum',49:'group/message-ref',
           50:'map-entry'}

def decode_schema(s):
    vals = read_13bit_varints(s)
    if len(vals) < 2:
        return None
    flags = vals[0]
    v1 = vals[1]
    if v1 == 0:
        return {'flags': flags, 'fields': []}
    if len(vals) < 10:
        return None
    v2, v3, v4, v5, v6, v7, v8, v9 = vals[2:10]
    header = dict(flags=flags, v1=v1, v2=v2, v3=v3, v4=v4, v5=v5, v6=v6, v7=v7, v8=v8, v9=v9)
    pos = 10
    fields = []
    while pos + 1 < len(vals):
        num = vals[pos]; packed = vals[pos+1]; pos += 2
        tc = packed & 0xFF
        f = dict(num=num, tc=tc, packed=packed, oneof=bool(packed & 0x400))
        if tc >= 51:
            f['objidx'] = vals[pos] if pos < len(vals) else None; pos += 1
        elif flags & 1 and tc <= 17:
            f['presence'] = vals[pos] if pos < len(vals) else None; pos += 1
        fields.append(f)
    return dict(header=header, fields=fields, leftover=len(vals)-pos)

def dex_string_at_prefix(dex, prefix_bytes):
    """Find full MUTF-8 string starting with prefix_bytes; return decoded string."""
    idx = dex.find(prefix_bytes)
    if idx < 0:
        return None
    end = idx
    n = len(dex)
    while end < n:
        if dex[end] == 0:
            # real NUL only if not part of C0 80 encoding
            if end == 0 or dex[end-1] != 0xC0:
                break
        end += 1
    return decode_mutf8(dex, idx, end)

def java_string_to_bytes(java_literal):
    """Convert jadx-printed java string literal (with \\uXXXX escapes, possibly
    truncated) into the byte prefix for DEX search. Handles truncation: use
    whatever complete escapes are present."""
    def repl(m):
        return chr(int(m.group(1), 16))
    s = re.sub(r'\\u([0-9a-fA-F]{4})', repl, java_literal)
    for esc, ch in [('\\b', 8), ('\\f', 12), ('\\r', 13), ('\\t', 9), ('\\n', 10)]:
        s = s.replace(esc, chr(ch))
    # encode to mutf8 bytes (NUL = C0 80!)
    out = bytearray()
    for ch in s:
        c = ord(ch)
        if c == 0:
            out += b'\xc0\x80'
        elif c < 0x80:
            out.append(c)
        elif c < 0x800:
            out.append(0xC0 | (c >> 6)); out.append(0x80 | (c & 0x3F))
        else:
            out.append(0xE0 | (c >> 12)); out.append(0x80 | ((c >> 6) & 0x3F)); out.append(0x80 | (c & 0x3F))
    return bytes(out)

def extract_all_messages(src_root, dex_path):
    dex = open(dex_path, 'rb').read()
    results = {}
    for dirpath, _, files in os.walk(src_root):
        for fn in files:
            if not fn.endswith('.java'):
                continue
            path = os.path.join(dirpath, fn)
            try:
                src = open(path, encoding='utf-8', errors='replace').read()
            except Exception:
                continue
            m = re.search(r'new gzu\(\w+, "((?:[^"\\]|\\.)*)", new Object\[\]\{([^}]*)\}\)', src)
            if not m:
                continue
            lit, objarr_src = m.group(1), m.group(2)
            # class name from package+file
            rel = os.path.relpath(path, src_root)
            cls = os.path.splitext(rel)[0].replace(os.sep, '.')
            # objarr entries
            entries = []
            for part in re.finditer(r'"((?:[^"\\]|\\.)*)"|(\w+)\.class', objarr_src):
                if part.group(1) is not None:
                    entries.append(('str', part.group(1)))
                else:
                    entries.append(('cls', part.group(2)))
            # get full string from dex
            prefix = java_string_to_bytes(lit)
            # trim prefix to last complete escape boundary: use up to 16 bytes min
            if len(prefix) < 4:
                continue
            full = dex_string_at_prefix(dex, prefix[:min(len(prefix), 40)])
            if full is None and len(prefix) > 8:
                full = dex_string_at_prefix(dex, prefix[:8])
            schema = decode_schema(full) if full else None
            if schema:
                results[cls] = dict(java_class=cls, objarr=entries, schema=schema,
                                    truncated_in_java=(len(lit) < len(full) if full else None),
                                    full_len=len(full) if full else 0)
    return results

if __name__ == '__main__':
    src_root = sys.argv[1]  # jadx sources root
    dex_path = sys.argv[2]
    out_json = sys.argv[3]
    res = extract_all_messages(src_root, dex_path)
    with open(out_json, 'w') as f:
        json.dump(res, f, indent=1)
    print(f"extracted {len(res)} message schemas -> {out_json}")
    # summary
    multi = [(k, len(v['schema']['fields'])) for k, v in res.items() if len(v['schema']['fields']) > 0]
    print(f"{len(multi)} with fields")