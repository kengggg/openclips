#!/usr/bin/env python3
"""Skeleton for porting openclips to another BLE stack.

Implement the three Transport methods on top of your platform's GATT API,
then everything in `openclips.Camera` works unchanged. Requirements:

* write(): one Write Request per frame, MTU 512, never long/prepared writes
* read_indication(): return one complete indication value or None on timeout
* subscribe to indications on INDICATE_UUID before returning from __init__
"""

from openclips import Camera, Transport
from openclips.constants import INDICATE_UUID, WRITE_UUID  # noqa: F401


class MyTransport(Transport):
    def __init__(self, address: str):
        # connect, discover, enable indications on INDICATE_UUID
        raise NotImplementedError

    def write(self, frame: bytes) -> None:
        # write `frame` to WRITE_UUID with response
        raise NotImplementedError

    def read_indication(self, timeout: float = 1.0):
        # block up to `timeout` seconds for the next indication value
        raise NotImplementedError

    def close(self) -> None:
        pass


if __name__ == "__main__":
    cam = Camera(MyTransport("AA:BB:CC:DD:EE:FF"), pairing_key=bytes.fromhex("..." * 0))
    print(cam.resume().as_dict())
