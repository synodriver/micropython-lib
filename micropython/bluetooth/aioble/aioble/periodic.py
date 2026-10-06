# MicroPython aioble module
# MIT license; Copyright (c) 2026

from micropython import const

import asyncio

from .core import ble, _ble5_method, register_irq_handler, log_error
from .device import Device, DeviceTimeout


_IRQ_PERIODIC_SYNC = const(43)
_IRQ_PERIODIC_REPORT = const(44)
_IRQ_PERIODIC_SYNC_LOST = const(45)
_PAYLOAD_MAX_LEN = const(1650)
_CLEANUP_RETRY_MS = const(250)

_pending_sync = None
_syncs = {}


class PeriodicSyncLostError(Exception):
    def __init__(self, reason):
        self.reason = reason


def _periodic_irq(event, data):
    if event == _IRQ_PERIODIC_SYNC:
        if sync := _pending_sync:
            status, handle, sid, interval, phy, addr_type, addr = data
            sync._status = status
            if not status:
                sync.device = Device(addr_type, bytes(addr))
                sync.sid = sid
                sync.periodic_interval = interval
                sync.phy = phy
                sync._handle = handle
                _syncs[handle] = sync
            sync._event.set()
    elif event == _IRQ_PERIODIC_REPORT:
        handle, status, rssi, tx_power, payload = data
        if sync := _syncs.get(handle, None):
            if not sync._abandoned:
                sync._queue.append((status, rssi, tx_power, bytes(payload)))
                sync._report_event.set()
    elif event == _IRQ_PERIODIC_SYNC_LOST:
        handle, reason = data
        if sync := _syncs.pop(handle, None):
            sync._lost(reason)


def _periodic_shutdown():
    global _pending_sync
    if _pending_sync:
        _pending_sync._lost(-1)
    for sync in _syncs.values():
        sync._lost(-1)
    _syncs.clear()
    _pending_sync = None


register_irq_handler(_periodic_irq, _periodic_shutdown)


class PeriodicReport:
    def __init__(self, data_status, rssi, tx_power, adv_data):
        self.data_status = data_status
        self.rssi = rssi
        self.tx_power = tx_power
        self.adv_data = adv_data


class PeriodicSync:
    def __init__(self, device, sid):
        self.device = device
        self.sid = sid
        self.periodic_interval = 0
        self.phy = None
        self._handle = None
        self._status = None
        self._closed = False
        self.reason = None
        self._event = asyncio.ThreadSafeFlag()
        self._report_event = asyncio.ThreadSafeFlag()
        self._queue = []
        self._fragments = b""
        self._cleanup_task = None
        self._abandoned = False

    def _lost(self, reason):
        self._handle = None
        self._closed = True
        self.reason = reason
        self._event.set()
        self._report_event.set()

    def is_synced(self):
        return self._handle is not None

    async def close(self, timeout_ms=1000):
        if not self.is_synced():
            return
        ble.gap_periodic_sync_stop(self._handle)
        with DeviceTimeout(None, timeout_ms):
            while not self._closed:
                await self._event.wait()

    async def _cancel_create(self):
        global _pending_sync
        try:
            if self._status is None and not self._closed:
                try:
                    ble.gap_periodic_sync(None)
                except OSError:
                    # Completion may already be queued by the controller.
                    pass
                while self._status is None and not self._closed:
                    await self._event.wait()
            if self.is_synced():
                logged_error = False
                while self.is_synced() and not self._closed:
                    try:
                        ble.gap_periodic_sync_stop(self._handle)
                    except OSError as error:
                        if not logged_error:
                            log_error("Periodic sync cleanup; retrying", error)
                            logged_error = True
                        await asyncio.sleep_ms(_CLEANUP_RETRY_MS)
                    else:
                        while self.is_synced() and not self._closed:
                            await self._event.wait()
            if _pending_sync is self:
                _pending_sync = None
        finally:
            self._cleanup_task = None

    def __aiter__(self):
        return self

    async def __anext__(self):
        while True:
            if self._closed:
                raise PeriodicSyncLostError(self.reason)
            while self._queue:
                status, rssi, tx_power, payload = self._queue.pop(0)
                payload = self._fragments + payload if self._fragments is not None else None
                if status == 1:
                    self._fragments = (
                        payload
                        if payload is not None and len(payload) <= _PAYLOAD_MAX_LEN
                        else None
                    )
                    continue
                self._fragments = b""
                if status == 0 and (payload is None or len(payload) > _PAYLOAD_MAX_LEN):
                    status = 2
                return PeriodicReport(status, rssi, tx_power, payload if status == 0 else None)
            await self._report_event.wait()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_traceback):
        await self.close()


async def periodic_sync(device, *, sid=None, skip=0, timeout_ms=10000, sync_timeout_ms=10000):
    global _pending_sync
    create = _ble5_method("gap_periodic_sync")
    if hasattr(device, "device"):
        if sid is None:
            sid = device.sid
        device = device.device
    if sid is None or not 0 <= sid <= 15:
        raise ValueError("Periodic sync requires an advertising SID")
    if _pending_sync:
        raise ValueError("Periodic sync creation already pending")
    sync = PeriodicSync(device, sid)
    _pending_sync = sync
    submitted = False
    try:
        create(device.addr_type, device.addr, sid=sid, skip=skip, timeout_ms=sync_timeout_ms)
        submitted = True
        with DeviceTimeout(None, timeout_ms):
            while sync._status is None and not sync._closed:
                await sync._event.wait()
        if sync._closed:
            raise PeriodicSyncLostError(sync.reason)
        if sync._status:
            raise OSError(sync._status)
        return sync
    except BaseException:
        if submitted and (sync._status is None or sync.is_synced()) and not sync._closed:
            # Retain the pending slot until the cancellation completion arrives.
            sync._abandoned = True
            sync._queue.clear()
            sync._cleanup_task = asyncio.create_task(sync._cancel_create())
        raise
    finally:
        if not sync._cleanup_task and _pending_sync is sync:
            _pending_sync = None
