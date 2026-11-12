"""Offline D4 admission experiment; not adapter isolation or a production quota."""
import asyncio


class BoundedBackend:
    """Limit live searches through this instance on one event loop.

    The permit stays held until the backend coroutine really finishes, including
    when it suppresses cancellation after the caller's deadline. Saturation fails
    closed without queuing work. One instance cannot control other instances,
    processes, detached children, or a backend that blocks the event loop.
    """
    def __init__(self, backend, limit=1):
        if type(limit) is not int or not 1 <= limit <= 32:
            raise ValueError("invalid admission limit")
        self._backend = backend
        self._limit = limit
        self._active = 0
        self._loop = None

    async def search(self, query):
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
        elif self._loop is not loop:
            raise RuntimeError("backend bound to another loop")
        if self._active >= self._limit:
            raise RuntimeError("retrieval capacity unavailable")
        self._active += 1
        try:
            return await self._backend.search(query)
        finally:
            self._active -= 1
