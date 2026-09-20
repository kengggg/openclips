#!/usr/bin/env python3
"""Decode protobuf-javalite encoded schema strings (gzu format) from the
Google Clips APK. Format reverse-engineered from gzk.java (schema parser).

Encoding: sequence of "13-bit varint" values encoded as chars:
  - char < 0xD800: value = char
  - char >= 0xD800: 13 bits per char (mask 0x1FFF), terminated by char < 0xD800
Header: 10 values (flags, x0, ... counts ...)
Per field: [field_number][packed_bits] and if type_code >= 51: [objarr_index]
packed bits: type = low 8 bits; 0x100=?, 0x200=?, 0x400=oneof, 0x800=?
"""
import sys, json

def read_13bit_varints(s):
    """Decode the whole string into a list of ints."""
    vals = []
    i = 0
    n = len(s)
    while i < n:
        c = ord(s[i])
        i += 1
        if c < 0xD800:
            vals.append(c)
        else:
            r = c & 0x1FFF
            shift = 13
            while i < n:
                c2 = ord(s[i])
                i += 1
                if c2 < 0xD800:
                    r = (c2 << shift) | r
                    break
                r |= (c2 & 0x1FFF) << shift
                shift += 13
            vals.append(r)
    return vals

# FieldType ordinals (protobuf-javalite old-style FieldType enum)
PRIM = {0:'double',1:'float',2:'int64',3:'uint64',4:'int32',5:'fixed64',6:'fixed32',
        7:'bool',8:'string',9:'group',10:'message',11:'bytes',12:'uint32',13:'enum',
        14:'sfixed32',15:'sfixed64',16:'sint32',17:'sint64'}

def decode(schema_str, objarr):
    vals = read_13bit_varints(schema_str)
    pos = 0
    def rd():
        nonlocal pos
        v = vals[pos]; pos += 1
        return v
    flags = rd()          # v0: bit0 = checkUtf8 mode? etc
    v1 = rd()             # if 0 -> no fields
    if v1 == 0:
        return {'fields': []}
    v2 = rd()             # -> i6
    v3 = rd()             # combined with v2: total = v3 + (v2<<1)
    v4 = rd()             # i8
    v5 = rd()             # i65/i10
    v6 = rd()             # i9 = per-field objArr consumption counter max
    v7 = rd()             # i5
    v8 = rd()             #
    v9 = rd()             # iCharAt (last header val)
    header = dict(flags=flags, v1=v1, v2=v2, v3=v3, v4=v4, v5=v5, v6=v6, v7=v7, v8=v8, v9=v9,
                  total_fields=v3 + (v2 << 1))
    fields = []
    objidx = 0
    while pos < len(vals):
        number = rd()
        packed = rd()
        tcode = packed & 0xFF
        f = dict(number=number, packed=packed, type_code=tcode,
                 oneof=bool(packed & 0x400),
                 flag_0x100=bool(packed & 0x100),
                 flag_0x200=bool(packed & 0x200),
                 flag_0x800=bool(packed & 0x800))
        if tcode >= 51:
            idx = rd()
            f['obj_index'] = idx
            name = objarr[idx * 2] if idx * 2 < len(objarr) else '?'
            f['java_field'] = name
            base = tcode - 51
            # per gzk: base 9 or 17 -> message-ish (class ref consumed)
            if base == 9 or base == 17:
                cls = objarr[objidx] if objidx < len(objarr) else '?'
                f['class'] = str(cls)
                f['objarr_pos'] = objidx
                objidx += 1
            elif base == 12 and (flags & 1):
                f['class'] = str(objarr[objidx]) if objidx < len(objarr) else '?'
                objidx += 1
        else:
            name = objarr[objidx] if objidx < len(objarr) else '?'
            f['java_field'] = name
            f['objarr_pos'] = objidx
            objidx += 1
            if tcode in (27, 49):
                f['extra'] = str(objarr[objidx]) if objidx < len(objarr) else '?'
                objidx += 1
            elif tcode in (12, 30, 44) and (flags & 1):
                f['extra'] = str(objarr[objidx]) if objidx < len(objarr) else '?'
                objidx += 1
            elif tcode == 50:
                objidx += 1
                if packed & 0x800:
                    objidx += 1
        fields.append(f)
    return dict(header=header, fields=fields)

# ============ Ground truth test: Request (hir) ============
REQ_STR = "\u00016\u0001\u0002\u0001ࠀ6\u0000\u0000\u0000\u0001<\u0000\u0002<\u0000\u0003<\u0000\u0004<\u0000\u0005<\u0000\u0006<\u0000\u0007<\u0000\b<\u0000\t<\u0000\n<\u0000\u000b<\u0000\f<\u0000\r<\u0000\u000e<\u0000\u000f<\u0000\u0010<\u0000\u0011<\u0000\u0012<\u0000\u0013<\u0000\u0014<\u0000\u0015<\u0000\u0016<\u0000\u001b<\u0000\u001c<\u0000\u001d<\u0000\u001e<\u0000\u001f<\u0000 <\u0000!<\u0000\"<\u0000#<\u0000$<\u0000%<\u0000&\u000b\u0000'<\u0000(<\u0000)<\u0000*<\u0000+<\u0000,<\u0000-<\u0000.<\u0000/<\u00000<\u00001<\u00002<\u00003<\u00004<\u00005<\u00006<\u00007<\u00008<\u00009<\u0000ࠀ<\u0000"
REQ_OBJ = ["d", "c", "a", "b", "hip", "hil", "hgy", "hgn", "hcw", "hgq", "hdn", "hkb",
           "hgt", "hcz", "hhg", "hhb", "hfv", "hdv", "hjm", "hfm", "hfp", "hjp", "hke",
           "hdy", "hdk", "hjy", "hgh", "hcq", "hjj", "hjg", "hgk", "hct", "hev", "hey",
           "hfe", "hgb", "hkq", "e", "hee", "hcb", "hfy", "hjv", "hdq", "hcm", "hiu",
           "hjs", "hge", "hdh", "hhv", "hiz", "hcf", "hhy", "hep", "hfs", "hfh", "hho",
           "hfb", "heb"]

if __name__ == '__main__':
    result = decode(REQ_STR, REQ_OBJ)
    h = result['header']
    print("HEADER:", json.dumps(h))
    print(f"\n{len(result['fields'])} fields decoded:")
    for f in result['fields']:
        print(f"  #{f['number']:3d} type={f['type_code']:3d} {'ONEOF' if f['oneof'] else '    '} java={f.get('java_field','?'):3s} class={f.get('class','-')}")