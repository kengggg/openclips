#!/usr/bin/env python3
"""Convert extracted FileDescriptorProtos to .proto source files."""
import sys, os
from google.protobuf import descriptor_pb2
from google.protobuf.descriptor_pool import DescriptorPool
from google.protobuf import message_factory

def write_proto_text(fdp):
    """Serialize FDP back to a FileDescriptorSet and use protoc to print .proto."""
    pool = DescriptorPool()
    # Add dependencies first (descriptor.proto is built-in)
    try:
        pool.Add(fdp)
    except Exception as e:
        return None
    # Build a FileDescriptorSet for protoc
    return fdp

if __name__ == '__main__':
    indir, outdir = sys.argv[1], sys.argv[2]
    os.makedirs(outdir, exist_ok=True)
    files = sorted(os.listdir(indir))
    fds = descriptor_pb2.FileDescriptorSet()
    for fn in files:
        if not fn.endswith('.fdesc'):
            continue
        with open(os.path.join(indir, fn), 'rb') as f:
            fdp = descriptor_pb2.FileDescriptorProto()
            fdp.ParseFromString(f.read())
            fds.file.add().CopyFrom(fdp)
    # Write set and run protoc --decode to print .proto text
    setpath = os.path.join(outdir, '_set.binpb')
    with open(setpath, 'wb') as f:
        f.write(fds.SerializeToString())
    print(f"wrote FileDescriptorSet with {len(fds.file)} files: {setpath}")
