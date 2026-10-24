# MicroPython aioble module
# MIT license; Copyright (c) 2026

from micropython import const, schedule

import asyncio

from .core import ble, _ble5_method, _FragmentBuffer, register_irq_handler, log_error, PeriodicSyncLostError
from .device import Device, DeviceTimeout


_IRQ_PERIODIC_SYNC = const(43)
_IRQ_PERIODIC_REPORT = const(44)
_IRQ_PERIODIC_SYNC_LOST = const(45)
_PAYLOAD_MAX_LEN = const(1650)
_CLEANUP_RETRY_MS = const(250)
_SCHEDULE_ERRORS = (RuntimeError, MemoryError)
_STOP_ERRORS = (OSError, MemoryError)

_pending_sync = None
_syncs = {}


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
            sync._enqueue(status, rssi, tx_power, payload)
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
    def __init__(self, device, sid, max_queue=32, max_queue_bytes=8192):
        for limit in (max_queue, max_queue_bytes):
            if not isinstance(limit, int) or limit <= 0:
                raise ValueError("periodic limits must be positive integers")
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
        self._queue_bytes = 0
        self._max_queue = max_queue
        self._max_queue_bytes = max_queue_bytes
        self._fragments = None
        self._cleanup_task = None
        self._abandoned = False
        self._error = None
        self._stop_requested = False
        self._stop_scheduled = False
        # Bind while memory is available, before an IRQ needs cleanup.
        self._stop_callback = self._scheduled_stop

    def _clear(self):
        self._queue.clear()
        self._queue_bytes = 0
        self._fragments = None

    def _request_stop(self):
        # A delayed callback must never stop a new sync reusing this handle.
        if self._handle is not None and _syncs.get(self._handle) is self:
            if not self._stop_requested:
                ble.gap_periodic_sync_stop(self._handle)
                self._stop_requested = True

    def _schedule_stop(self):
        if self._stop_scheduled or self._stop_requested or not self.is_synced():
            return
        self._stop_scheduled = True
        try:
            schedule(self._stop_callback, None)
        except _SCHEDULE_ERRORS:
            # Retry on a later report or explicit close, without escaping IRQ.
            self._stop_scheduled = False

    def _scheduled_stop(self, _):
        self._stop_scheduled = False
        try:
            self._request_stop()
        except _STOP_ERRORS:
            # Keep ownership until a real loss/stop IRQ; later reports retry.
            pass

    def _fail(self, message):
        if self._error is None:
            self._error = message
        self._abandoned = True
        self._clear()
        self._report_event.set()
        self._schedule_stop()

    def _enqueue(self, status, rssi, tx_power, payload):
        if self._abandoned:
            self._schedule_stop()
            return
        if (
            len(self._queue) >= self._max_queue
            or self._queue_bytes + len(payload) > self._max_queue_bytes
        ):
            self._fail("periodic queue limit exceeded")
            return
        try:
            self._queue.append((status, rssi, tx_power, bytes(payload)))
            self._queue_bytes += len(payload)
        except MemoryError:
            self._fail("periodic buffer allocation failed")
            return
        self._report_event.set()

    def _lost(self, reason):
        self._handle = None
        self._closed = True
        self.reason = reason
        self._abandoned = True
        self._clear()
        self._event.set()
        self._report_event.set()

    def is_synced(self):
        return self._handle is not None

    async def close(self, timeout_ms=1000):
        self._abandoned = True
        self._clear()
        if not self.is_synced():
            return
        try:
            self._request_stop()
            with DeviceTimeout(None, timeout_ms):
                while not self._closed:
                    await self._event.wait()
        except BaseException:
            self._schedule_stop()
            raise

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
                        self._request_stop()
                    except _STOP_ERRORS as error:
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
        try:
            return await self._next_report()
        except MemoryError:
            self._fail("periodic buffer allocation failed")
            raise

    async def _next_report(self):
        while True:
            if self._error:
                raise MemoryError(self._error)
            if self._closed:
                raise PeriodicSyncLostError(self.reason)
            while self._queue:
                try:
                    status, rssi, tx_power, payload = self._queue.pop(0)
                except IndexError:
                    # An IRQ/scheduled callback can clear the queue at a VM safe point.
                    if self._abandoned:
                        break
                    raise
                self._queue_bytes = max(0, self._queue_bytes - len(payload))
                if self._abandoned:
                    break
                fragments = self._fragments
                if fragments is None and status == 1:
                    fragments = _FragmentBuffer(_PAYLOAD_MAX_LEN)
                valid = (
                    fragments.append(payload)
                    if fragments is not None
                    else len(payload) <= _PAYLOAD_MAX_LEN
                )
                if status == 1:
                    self._fragments = fragments
                else:
                    self._fragments = None
                    if status == 0:
                        if not valid:
                            status = 2
                        elif fragments is not None:
                            payload = fragments.finish()
                if self._abandoned:
                    self._clear()
                    break
                if status == 1:
                    continue
                report = PeriodicReport(status, rssi, tx_power, payload if status == 0 else None)
                if self._abandoned:
                    self._clear()
                    break
                return report
            if self._error or self._closed:
                continue
            await self._report_event.wait()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_traceback):
        try:
            await self.close()
        except Exception:
            # close() has arranged a retry; preserve an exception from the body.
            if exc_type is None:
                raise


async def periodic_sync(
    device,
    *,
    sid=None,
    skip=0,
    timeout_ms=10000,
    sync_timeout_ms=10000,
    max_queue=32,
    max_queue_bytes=8192,
):
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
    sync = PeriodicSync(device, sid, max_queue, max_queue_bytes)
    _pending_sync = sync
    submitted = False
    try:
        create(device.addr_type, device.addr, sid=sid, skip=skip, timeout_ms=sync_timeout_ms)
        submitted = True
        with DeviceTimeout(None, timeout_ms):
            while sync._status is None and not sync._closed:
                await sync._event.wait()
        if sync._closed or sync._error:
            # Reports can overflow before this creator resumes. A scheduled
            # stop may already have closed the sync; keep the original error.
            if sync._error:
                raise MemoryError(sync._error)
            raise PeriodicSyncLostError(sync.reason)
        if sync._status:
            raise OSError(sync._status)
        return sync
    except BaseException:
        if submitted and (sync._status is None or sync.is_synced()) and not sync._closed:
            # Retain the pending slot until the cancellation completion arrives.
            sync._abandoned = True
            sync._clear()
            sync._cleanup_task = asyncio.create_task(sync._cancel_create())
        raise
    finally:
        if not sync._cleanup_task and _pending_sync is sync:
            _pending_sync = None
