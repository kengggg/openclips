"""asyncio facade over the blocking API.

Every ``Camera`` call runs on a single-thread executor so calls stay
serialised (the transport is not safe for concurrent reads), and events are
delivered as an async iterator on the event loop::

    async with AsyncConnection(address, store=PairingStore()) as cam:
        state = cam.state
        sessions = await cam.list_sessions()
        async for name, args in cam.events(EVENT_STATE):
            ...
"""

from __future__ import annotations

import asyncio
import functools
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor

from .camera import EVENT_DISCONNECTED, EVENT_NOTIFICATION, EVENT_STATE, Camera
from .connection import ConnectionManager

_NOT_WRAPPED = {"on", "off", "keepalive", "start_keepalive", "stop_keepalive", "close"}


class AsyncCamera:
    """Wrap a :class:`Camera`; blocking methods become coroutines."""

    def __init__(self, camera: Camera, loop: asyncio.AbstractEventLoop | None = None):
        self._camera = camera
        self._loop = loop or asyncio.get_event_loop()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="openclips-aio")

    @property
    def camera(self) -> Camera:
        return self._camera

    @property
    def state(self):
        return self._camera.state

    def __getattr__(self, name):
        attr = getattr(self._camera, name)
        if not callable(attr) or name in _NOT_WRAPPED or name.startswith("_"):
            return attr

        @functools.wraps(attr)
        async def call(*args, **kwargs):
            return await self._loop.run_in_executor(self._executor, functools.partial(attr, *args, **kwargs))

        return call

    async def events(self, *names: str) -> AsyncIterator[tuple[str, tuple]]:
        """Yield ``(event_name, args)`` for the named events (default: all)."""
        names = names or (EVENT_STATE, EVENT_NOTIFICATION, EVENT_DISCONNECTED)
        queue: asyncio.Queue = asyncio.Queue()
        handlers = []
        for n in names:

            def make(n):
                def handler(*args):
                    self._loop.call_soon_threadsafe(queue.put_nowait, (n, args))

                return handler

            h = make(n)
            self._camera.on(n, h)
            handlers.append((n, h))
        try:
            while True:
                yield await queue.get()
        finally:
            for n, h in handlers:
                self._camera.off(n, h)

    async def aclose(self) -> None:
        await self._loop.run_in_executor(self._executor, self._camera.close)
        self._executor.shutdown(wait=False)


class AsyncConnection:
    """``async with`` wrapper around :class:`ConnectionManager`."""

    def __init__(self, *args, **kwargs):
        self._mgr = ConnectionManager(*args, **kwargs)
        self._cam: AsyncCamera | None = None

    @property
    def manager(self) -> ConnectionManager:
        return self._mgr

    async def connect(self) -> AsyncCamera:
        loop = asyncio.get_running_loop()
        cam = await loop.run_in_executor(None, self._mgr.connect)
        self._cam = AsyncCamera(cam, loop)
        return self._cam

    async def close(self) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._mgr.close)
        if self._cam is not None:
            self._cam._executor.shutdown(wait=False)

    async def __aenter__(self) -> AsyncCamera:
        return await self.connect()

    async def __aexit__(self, *exc):
        await self.close()
        return False
