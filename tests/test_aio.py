import asyncio
import os

import pytest
from fake_lens import FakeLens

from openclips import constants as C
from openclips.aio import AsyncCamera, AsyncConnection
from openclips.camera import EVENT_STATE, Camera
from openclips.pb import pb_uint

SID = 1789825799491786000


def test_async_camera_wraps_blocking_calls_and_events():
    async def main():
        lens = FakeLens(pairing_key=os.urandom(32), sessions={SID: [1, 2]})
        cam = AsyncCamera(Camera(lens, lens.pairing_key))
        state = await cam.resume()
        assert state.system_state_name == "IDLE"
        assert cam.state is cam.camera.state  # properties pass through
        s = await cam.list_sessions()
        assert s["session_ids"] == [SID]
        moments = await cam.moments(SID)
        assert [m.moment_id for m in moments] == [1, 2]

        async def first_event():
            async for name, args in cam.events(EVENT_STATE):
                return name, args

        lens.push_notification(2, pb_uint(1, C.SYSTEM_STATE_CAPTURE))
        ev_task = asyncio.create_task(first_event())
        await cam.poll(0.2)
        name, (state, changes) = await asyncio.wait_for(ev_task, 2)
        assert name == EVENT_STATE and changes["system_state"] == (2, 4)
        assert cam.camera._listeners[EVENT_STATE] == []  # generator cleanup removed the handler
        await cam.aclose()
        assert lens.closed

    asyncio.run(main())


def test_async_connection_context_manager():
    async def main():
        key = os.urandom(32)
        lens = FakeLens(pairing_key=key, sessions={7: [1]})

        def factory(addr):
            lens.reopen()
            return lens

        async with AsyncConnection("A", pairing_key=key, transport_factory=factory, keepalive=False) as cam:
            assert (await cam.list_sessions())["session_ids"] == [7]
            assert lens.time_synced_to is not None
        assert lens.closed

    asyncio.run(main())


def test_async_connection_shares_executor_and_wakes_events():
    async def main():
        key = os.urandom(32)
        lens = FakeLens(pairing_key=key, sessions={7: [1]})

        def factory(addr):
            lens.reopen()
            return lens

        conn = AsyncConnection("A", pairing_key=key, transport_factory=factory, keepalive=False)
        cam = await conn.connect()
        assert cam._executor is conn._executor
        assert await conn.connect() is cam
        task = asyncio.create_task(_drain_one(cam))
        await asyncio.sleep(0.05)
        await conn.close()
        await asyncio.wait_for(task, 2)
        with pytest.raises(RuntimeError):
            await cam.list_sessions()
        await conn.close()
        assert lens.closed

    asyncio.run(main())


async def _drain_one(cam):
    async for _ in cam.events(EVENT_STATE):
        break


def test_event_overflow_ends_subscription(monkeypatch):
    async def main():
        import openclips.aio as aio
        from openclips.errors import EventOverflow

        monkeypatch.setattr(aio, "EVENT_QUEUE_LIMIT", 2)
        lens = FakeLens(pairing_key=os.urandom(32))
        cam = AsyncCamera(Camera(lens, lens.pairing_key))
        await cam.resume()
        seen = []

        async def consume():
            try:
                async for _ in cam.events(EVENT_STATE):
                    await asyncio.sleep(0.2)
            except EventOverflow:
                seen.append("overflow")

        task = asyncio.create_task(consume())
        await asyncio.sleep(0.05)
        for i in range(8):
            lens.push_notification(2, pb_uint(1, C.SYSTEM_STATE_CAPTURE if i % 2 else C.SYSTEM_STATE_IDLE))
        await cam.poll(0.2)
        await asyncio.wait_for(task, 3)
        assert seen == ["overflow"]
        await cam.aclose()

    asyncio.run(main())


def test_state_event_is_snapshot():
    async def main():
        lens = FakeLens(pairing_key=os.urandom(32))
        cam = AsyncCamera(Camera(lens, lens.pairing_key))
        await cam.resume()
        held = []

        async def first():
            async for name, args in cam.events(EVENT_STATE):
                held.append(args[0].system_state)
                return

        task = asyncio.create_task(first())
        await asyncio.sleep(0.02)
        lens.push_notification(2, pb_uint(1, C.SYSTEM_STATE_CAPTURE))
        await cam.poll(0.2)
        await asyncio.wait_for(task, 2)
        cam.camera.state.system_state = 99
        assert held[0] == C.SYSTEM_STATE_CAPTURE
        await cam.aclose()

    asyncio.run(main())
