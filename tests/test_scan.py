import asyncio

import pytest

from openclips.scan import scan_async


class FakeScanner:
    def __init__(self):
        self.started = 0
        self.stopped = 0

    async def start(self):
        self.started += 1

    async def stop(self):
        self.stopped += 1


def test_scan_stops_after_start_even_when_cancelled():
    async def main():
        s = FakeScanner()
        await scan_async(0.01, scanner=s)
        assert s.started == 1 and s.stopped == 1

        s2 = FakeScanner()

        async def lingering():
            await scan_async(30, scanner=s2)

        t = asyncio.create_task(lingering())
        await asyncio.sleep(0.05)
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
        assert s2.started == 1 and s2.stopped == 1

    asyncio.run(main())
