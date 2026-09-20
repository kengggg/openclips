"""asyncio facade over the blocking API.

Every ``Camera`` call runs on a single-thread executor so calls stay
serialised (the transport is not safe for concurrent reads), and events are
delivered as an async iterator on the event loop::

    async with AsyncConnection(address, store=PairingStore()) as cam:
        state = cam.state
        sessions = await cam.list_sessions()
        async for name, args in cam.events(EVENT_STATE):
            ...

``AsyncConnection`` owns one worker thread. Its ``AsyncCamera`` shares that
executor so connect, RPCs, and close cannot overlap. A standalone
``AsyncCamera`` owns its own executor. Cancelling an await cancels the
future, not the blocking work already running on that thread; close waits
for in-flight work up to ``shutdown_timeout`` (default 8 s) then shuts the
pool down. Event queues are bounded (256); overflow ends the subscription
with :class:`~openclips.errors.EventOverflow`. State snapshots are copied
at emission.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor

from .camera import EVENT_DISCONNECTED, EVENT_NOTIFICATION, EVENT_STATE, Camera
from .connection import ConnectionManager
from .errors import EventOverflow

logger = logging.getLogger(__name__)

_NOT_WRAPPED = {"on", "off", "keepalive", "start_keepalive", "stop_keepalive", "close"}
EVENT_QUEUE_LIMIT = 256
DEFAULT_SHUTDOWN_TIMEOUT = 8.0


class AsyncCamera:
    """Wrap a :class:`Camera`; blocking methods become coroutines.

    Pass ``executor`` to share an owner’s worker (``AsyncConnection``).
    Omit it to own a one-thread pool that ``aclose()`` shuts down.
    """

    def __init__(
        self,
        camera: Camera,
        loop: asyncio.AbstractEventLoop | None = None,
        executor: ThreadPoolExecutor | None = None,
        *,
        shutdown_timeout: float = DEFAULT_SHUTDOWN_TIMEOUT,
    ):
        self._camera = camera
        self._loop = loop or asyncio.get_event_loop()
        self._owned_executor = executor is None
        self._executor = executor or ThreadPoolExecutor(max_workers=1, thread_name_prefix="openclips-aio")
        self._shutdown_timeout = shutdown_timeout
        self._closed = False
        self._event_queues: list[asyncio.Queue] = []

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
            if self._closed:
                raise RuntimeError("AsyncCamera is closed")
            return await self._loop.run_in_executor(self._executor, functools.partial(attr, *args, **kwargs))

        return call

    async def events(self, *names: str) -> AsyncIterator[tuple[str, tuple]]:
        """Yield ``(event_name, args)`` for the named events (default: all).

        The queue holds at most ``EVENT_QUEUE_LIMIT`` items. Overflow raises
        :class:`EventOverflow` and unregisters handlers. Close/disconnect
        enqueues ``None`` so the iterator ends.
        """
        names = names or (EVENT_STATE, EVENT_NOTIFICATION, EVENT_DISCONNECTED)
        queue: asyncio.Queue = asyncio.Queue(maxsize=EVENT_QUEUE_LIMIT)
        self._event_queues.append(queue)
        closed = False
        handlers = []

        def _put(item) -> None:
            if closed:
                return
            try:
                queue.put_nowait(item)
            except asyncio.QueueFull:
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    queue.put_nowait(EventOverflow("async event queue overflow"))
                except asyncio.QueueFull:
                    pass

        for n in names:

            def make(n):
                def handler(*args):
                    self._loop.call_soon_threadsafe(_put, (n, args))

                return handler

            h = make(n)
            self._camera.on(n, h)
            handlers.append((n, h))
        try:
            while True:
                item = await queue.get()
                if item is None:
                    return
                if isinstance(item, EventOverflow):
                    raise item
                yield item
        finally:
            closed = True
            if queue in self._event_queues:
                self._event_queues.remove(queue)
            for n, h in handlers:
                try:
                    self._camera.off(n, h)
                except Exception:
                    pass

    def _wake_event_queues(self) -> None:
        for q in list(self._event_queues):
            try:
                q.put_nowait(None)
            except asyncio.QueueFull:
                try:
                    q.get_nowait()
                    q.put_nowait(None)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._wake_event_queues()
        try:
            await asyncio.wait_for(
                self._loop.run_in_executor(self._executor, self._camera.close),
                timeout=self._shutdown_timeout,
            )
        except (asyncio.TimeoutError, TimeoutError):
            logger.debug("AsyncCamera close exceeded shutdown_timeout")
        finally:
            if self._owned_executor:
                self._executor.shutdown(wait=False, cancel_futures=True)


class AsyncConnection:
    """``async with`` wrapper around :class:`ConnectionManager`.

    Owns one worker thread used for connect, camera RPCs, and manager close.
    """

    def __init__(self, *args, shutdown_timeout: float = DEFAULT_SHUTDOWN_TIMEOUT, **kwargs):
        self._mgr = ConnectionManager(*args, **kwargs)
        self._cam: AsyncCamera | None = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="openclips-aio")
        self._shutdown_timeout = shutdown_timeout
        self._closed = False

    @property
    def manager(self) -> ConnectionManager:
        return self._mgr

    async def connect(self) -> AsyncCamera:
        if self._closed:
            raise RuntimeError("AsyncConnection is closed")
        if self._cam is not None and self._mgr.connected:
            return self._cam
        loop = asyncio.get_running_loop()
        cam = await loop.run_in_executor(self._executor, self._mgr.connect)
        self._cam = AsyncCamera(cam, loop, executor=self._executor, shutdown_timeout=self._shutdown_timeout)
        return self._cam

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._cam is not None:
            self._cam._closed = True
            self._cam._wake_event_queues()
        loop = asyncio.get_running_loop()
        try:
            await asyncio.wait_for(
                loop.run_in_executor(self._executor, self._mgr.close),
                timeout=self._shutdown_timeout,
            )
        except (asyncio.TimeoutError, TimeoutError):
            logger.debug("AsyncConnection close exceeded shutdown_timeout")
        finally:
            if self._cam is not None:
                self._cam._closed = True
            self._executor.shutdown(wait=False, cancel_futures=True)

    async def __aenter__(self) -> AsyncCamera:
        try:
            return await self.connect()
        except Exception:
            await self.close()
            raise

    async def __aexit__(self, *exc):
        await self.close()
        return False
