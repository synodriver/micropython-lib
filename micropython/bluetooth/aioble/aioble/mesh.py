# MicroPython aioble Mesh adapter. MIT license.
# Optional: importing ordinary aioble never requires the native blemesh module.
import asyncio
import blemesh

from .core import ensure_active, register_irq_handler

_node = None


class MeshStoppedError(Exception):
    pass


class MeshOverflowError(Exception):
    pass


def _shutdown():
    global _node
    if _node is not None:
        node, _node = _node, None
        node._active = False
        node._flag.set()


register_irq_handler(None, _shutdown)


class Node:
    """One PB-ADV node with Configuration, Health and one Vendor model.

    send() queues a message; SENT is local completion, not a remote ACK.
    One task consumes recv()/the async iterator. Pair request/response messages
    in the application using its own transaction identifiers.
    """

    def __init__(self, uuid, *, company_id, model_id=1):
        self._uuid = bytes(uuid)
        self._company_id = company_id
        self._model_id = model_id
        self._flag = asyncio.ThreadSafeFlag()
        self._active = False
        self._receiving = False
        self._dropped = 0

    def _notify(self, _):
        self._flag.set()

    def start(self):
        global _node
        if _node is not None:
            raise ValueError("A Mesh node is already active")
        if blemesh.status()["state"] != 0:
            raise ValueError("The native Mesh node is already in use")
        flag = asyncio.ThreadSafeFlag()
        ensure_active()
        _node = self
        blemesh.irq(self._notify)
        try:
            blemesh.start(self._uuid, company_id=self._company_id, model_id=self._model_id)
        except:
            # Preserve a failed native node for explicit cleanup/recovery if
            # deinitialisation itself fails. Never start a second host.
            try:
                blemesh.stop()
            finally:
                blemesh.irq(None)
                _node = None
            raise
        # A flag is also a session token. Old waiters retain the old flag,
        # already signalled by stop/shutdown, and cannot consume new events.
        self._flag = flag
        self._active = True
        self._receiving = None
        self._dropped = 0
        return self

    def stop(self):
        global _node
        if _node is self:
            blemesh.stop()
            blemesh.irq(None)
            _node = None
        self._active = False
        self._flag.set()

    def status(self):
        self._check_active()
        return blemesh.status()

    def _check_active(self):
        if not self._active or _node is not self:
            raise MeshStoppedError()

    def send(self, dst, data, *, net_idx, app_idx, opcode=0, ttl=7):
        self._check_active()
        blemesh.send(dst, data, net_idx=net_idx, app_idx=app_idx, opcode=opcode, ttl=ttl)

    async def recv(self, timeout_ms=None):
        self._check_active()
        if self._receiving:
            raise ValueError("Only one Mesh receiver is supported")
        session = self._flag
        self._receiving = session
        try:
            if timeout_ms is None:
                return await self._recv(session)
            return await asyncio.wait_for_ms(self._recv(session), timeout_ms)
        finally:
            if self._receiving is session:
                self._receiving = None

    async def _recv(self, session):
        while True:
            if session is not self._flag:
                raise MeshStoppedError()
            self._check_active()
            status = blemesh.status()
            if status["state"] != 2:
                raise MeshStoppedError()
            if status["dropped"] != self._dropped:
                self._dropped = status["dropped"]
                raise MeshOverflowError(self._dropped)
            item = blemesh.recv()
            if item is not None:
                return item
            await session.wait()

    async def __aenter__(self):
        return self.start()

    async def __aexit__(self, exc_type, exc, tb):
        self.stop()

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return await self.recv()
        except MeshStoppedError:
            raise StopAsyncIteration
