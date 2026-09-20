#!/usr/bin/env python3
"""Extract ALL embedded protobuf FileDescriptorProtos from liblinks .so (v3).

v2 failed because greedy ParseFromString overruns into trailing garbage.
v3 hand-parses the wire format field-by-field; when an unknown/garbage tag
appears we stop there — that's the exact end of the descriptor blob.
"""
import sys, os, re
from google.protobuf import descriptor_pb2

KNOWN_TOP_FIELDS = {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12}  # FileDescriptorProto fields

def read_varint(d, p):
    r = 0
    s = 0
    while p < len(d):
        b = d[p]
        r |= (b & 0x7F) << s
        p += 1
        if not (b & 0x80):
            return r, p
        s += 7
        if s > 70:
            return None, None
    return None, None

def parse_fields(d):
    """Yield (field, wt, value_bytes_or_int, end_pos). Stop at garbage/unknown."""
    p = 0
    fields = []
    while p < len(d):
        tag, p2 = read_varint(d, p)
        if tag is None:
            break
        field, wt = tag >> 3, tag & 7
        if field == 0 or wt in (3, 4, 6, 7) or field > 500:
            break
        if wt == 0:
            val, p3 = read_varint(d, p2)
            if val is None:
                break
            fields.append((field, wt, val))
            p = p3
        elif wt == 2:
            ln, p3 = read_varint(d, p2)
            if ln is None or p3 + ln > len(d):
                break
            fields.append((field, wt, d[p3:p3 + ln]))
            p = p3 + ln
        else:
            break
    return fields, p

def extract_fdp_at(data, idx):
    """Hand-parse an FDP at idx; return (FileDescriptorProto, exact_len) or None."""
    fields, consumed = parse_fields(data[idx:idx + 65536])
    if not fields:
        return None, 0
    # The name field must be field 1 with .proto name
    name_field = None
    for f, wt, v in fields:
        if f == 1 and wt == 2:
            name_field = v
            break
    if name_field is None or not name_field.endswith(b'.proto'):
        return None, 0
    # Trim: find the exact end = position after last KNOWN field that parses.
    # Walk fields in order; keep only consecutive known fields from the start.
    valid = []
    for f, wt, v in fields:
        if f in KNOWN_TOP_FIELDS:
            valid.append((f, wt, v))
        else:
            break
    # Compute end offset by re-scanning only the valid prefix
    p = 0
    for f, wt, v in valid:
        tag, p2 = read_varint(data[idx:idx + 65536], p)
        if wt == 0:
            _, p = read_varint(data[idx:idx + 65536], p2)
        else:
            ln, p3 = read_varint(data[idx:idx + 65536], p2)
            p = p3 + ln
    blob = data[idx:idx + p]
    fdp = descriptor_pb2.FileDescriptorProto()
    try:
        fdp.ParseFromString(blob)
    except Exception:
        return None, 0
    if not fdp.name or not (fdp.message_type or fdp.enum_type or fdp.service or fdp.extension):
        return None, 0
    # Round-trip sanity: reserialization should be same length (canonical)
    if len(fdp.SerializeToString()) != len(blob):
        # tolerate minor difference but flag
        pass
    return fdp, p

def main(path, outdir):
    data = open(path, 'rb').read()
    # Find every \x0a<varint><name>.proto candidate
    candidates = set()
    for m in re.finditer(rb'[A-Za-z0-9_./\-]{5,}\.proto', data):
        j = m.start()
        # search backward for 0x0a + varint that lands exactly on j
        for back in range(1, 6):
            s = j - back
            if s < 0:
                break
            if data[s] != 0x0A:
                continue
            ln, after = read_varint(data, s + 1)
            if ln is not None and after == j and after + ln == m.end():
                candidates.add(s)
    print(f"{len(candidates)} anchor candidates")
    seen = {}
    for idx in sorted(candidates):
        fdp, length = extract_fdp_at(data, idx)
        if fdp is None:
            continue
        if fdp.name in seen:
            continue
        seen[fdp.name] = fdp
        print(f"  OK {fdp.name}: msgs={len(fdp.message_type)} enums={len(fdp.enum_type)} svcs={len(fdp.service)} len={length}")

    os.makedirs(outdir, exist_ok=True)
    for name, fdp in sorted(seen.items()):
        rel = name.replace('/', '__') + '.fdesc'
        with open(os.path.join(outdir, rel), 'wb') as f:
            f.write(fdp.SerializeToString())
    print(f"\nwrote {len(seen)} descriptors to {outdir}")

if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2])