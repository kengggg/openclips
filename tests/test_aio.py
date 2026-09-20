import asyncio
import os

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
