# MicroPython aioble module
# MIT license; Copyright (c) 2021 Jim Mussared

from micropython import const

import bluetooth
import struct

import asyncio

from .core import (
    ensure_active,
    ble,
    log_info,
    log_error,
    log_warn,
    register_irq_handler,
    _ble5_method,
)
from .device import Device, DeviceConnection, DeviceTimeout


_IRQ_SCAN_RESULT = const(5)
_IRQ_SCAN_DONE = const(6)
_IRQ_SCAN_RESULT_EXT = const(41)
_ADV_EXT_PAYLOAD_MAX_LEN = const(1650)
_PROP_CONNECTABLE = const(1)
_PROP_SCAN_RESPONSE = const(8)

_IRQ_PERIPHERAL_CONNECT = const(7)
_IRQ_PERIPHERAL_DISCONNECT = const(8)

_ADV_IND = const(0)
_ADV_DIRECT_IND = const(1)
_ADV_SCAN_IND = const(2)
_ADV_NONCONN_IND = const(3)
_SCAN_RSP = const(4)

_ADV_TYPE_FLAGS = const(0x01)
_ADV_TYPE_NAME = const(0x09)
_ADV_TYPE_SHORT_NAME = const(0x08)
_ADV_TYPE_UUID16_INCOMPLETE = const(0x2)
_ADV_TYPE_UUID16_COMPLETE = const(0x3)
_ADV_TYPE_UUID32_INCOMPLETE = const(0x4)
_ADV_TYPE_UUID32_COMPLETE = const(0x5)
_ADV_TYPE_UUID128_INCOMPLETE = const(0x6)
_ADV_TYPE_UUID128_COMPLETE = const(0x7)
_ADV_TYPE_APPEARANCE = const(0x19)
_ADV_TYPE_MANUFACTURER = const(0xFF)


# Keep track of the active scanner so IRQs can be delivered to it.
_active_scanner = None


# Keep the request object until it is delivered or cancellation cleanup ends.
_connecting = {}
_UNCLAIMED_RETRY_MS = const(250)


async def _cleanup_connect(connection):
    try:
        while (
            not connection._connect_done
            and ble.active()
            and _connecting.get(connection.device, None) is connection
        ):
            try:
                ble.gap_connect(None)
            except OSError as error:
                log_error("Cancel outgoing connection; retrying", error)
            if not connection._connect_done:
                try:
                    await asyncio.wait_for_ms(
                        connection._connect_event.wait(), _UNCLAIMED_RETRY_MS
                    )
                except asyncio.TimeoutError:
                    pass
        while (
            connection._conn_handle is not None
            and ble.active()
            and _connecting.get(connection.device, None) is connection
        ):
            connection._run_task()
            # The old device task may still be consuming a disconnect when the
            # controller reuses its handle. Never terminate the replacement.
            if DeviceConnection._connected.get(connection._conn_handle, None) is not connection:
                connection._event.set()
                await connection._task
                break
            try:
                if ble.gap_disconnect(connection._conn_handle) is False:
                    connection._event.set()
            except OSError as error:
                log_error("Disconnect unclaimed central connection; retrying", error)
                await asyncio.sleep_ms(_UNCLAIMED_RETRY_MS)
            else:
                await connection._task
    finally:
        if _connecting.get(connection.device, None) is connection:
            del _connecting[connection.device]
        connection._cleanup_task = None


def _central_irq(event, data):
    # Send results and done events to the active scanner instance.
    if event == _IRQ_SCAN_RESULT:
        addr_type, addr, adv_type, rssi, adv_data = data
        if not _active_scanner or _active_scanner._extended:
            return
        _active_scanner._queue.append((addr_type, bytes(addr), adv_type, rssi, bytes(adv_data)))
        _active_scanner._event.set()
    elif event == _IRQ_SCAN_RESULT_EXT:
        if not _active_scanner or not _active_scanner._extended:
            return
        # Address/data memoryviews are only valid during this IRQ.
        addr_type, properties, primary_phy, secondary_phy, sid, periodic_interval, data_status = (
            data[:7]
        )
        addr = bytes(data[7])
        rssi, tx_power = data[8:10]
        adv_data = bytes(data[10])
        _active_scanner._queue.append(
            (
                addr_type,
                properties,
                primary_phy,
                secondary_phy,
                sid,
                periodic_interval,
                data_status,
                addr,
                rssi,
                tx_power,
                adv_data,
            )
        )
        _active_scanner._event.set()
    elif event == _IRQ_SCAN_DONE:
        if not _active_scanner:
            return
        _active_scanner._done = True
        _active_scanner._event.set()
        _active_scanner._done_event.set()

    # Peripheral connect must be in response to a pending connection, so find
    # it in the pending connection set.
    elif event == _IRQ_PERIPHERAL_CONNECT:
        conn_handle, addr_type, addr = data

        for d, connection in _connecting.items():
            if d.addr_type == addr_type and d.addr == addr:
                # Register ownership before waking connect() so cancellation
                # cannot leave an established connection without a listener.
                connection._conn_handle = conn_handle
                connection._connect_done = True
                DeviceConnection._connected[conn_handle] = connection
                connection._connect_event.set()
                break

    # Find the active device connection for this connection handle.
    elif event == _IRQ_PERIPHERAL_DISCONNECT:
        conn_handle, addr_type, _ = data
        if addr_type == 0xFF:
            # NimBLE's failed/cancelled attempt has no valid peer address or
            # handle. Only one controller initiation can be pending.
            for connection in _connecting.values():
                if not connection._connect_done:
                    connection._connect_done = True
                    connection._connect_event.set()
                    break
        elif connection := DeviceConnection._connected.get(conn_handle, None):
            # Tell the device_task that it should terminate.
            connection._event.set()


def _central_shutdown():
    global _active_scanner
    if _active_scanner:
        _active_scanner._done = True
        _active_scanner._event.set()
        _active_scanner._done_event.set()
    _active_scanner = None
    for connection in _connecting.values():
        connection._connect_done = True
        connection._connect_event.set()
        if connection._conn_handle is not None:
            connection._run_task()
            connection._event.set()
    _connecting.clear()


register_irq_handler(_central_irq, _central_shutdown)


# Cancel an in-progress scan.
async def _cancel_pending():
    if _active_scanner:
        await _active_scanner.cancel()


# Start connecting to a peripheral.
# Call device.connect() rather than using method directly.
async def _connect(
    connection, timeout_ms, scan_duration_ms, min_conn_interval_us, max_conn_interval_us, phys=None
):
    device = connection.device
    # Enable BLE and cancel in-progress scans.
    ensure_active()
    connect_ext = _ble5_method("gap_connect_ext") if phys is not None else None
    await _cancel_pending()

    if device in _connecting or any(not c._connect_done for c in _connecting.values()):
        raise ValueError("Connection or cancellation already pending")

    # Allow the connected IRQ to find the device by address.
    connection._connect_event = asyncio.ThreadSafeFlag()
    connection._connect_done = False
    connection._cleanup_task = None
    _connecting[device] = connection

    submitted = False
    try:
        with DeviceTimeout(None, timeout_ms):
            if connect_ext:
                connect_ext(
                    device.addr_type,
                    device.addr,
                    2000 if scan_duration_ms is None else scan_duration_ms,
                    min_conn_interval_us=0
                    if min_conn_interval_us is None
                    else min_conn_interval_us,
                    max_conn_interval_us=0
                    if max_conn_interval_us is None
                    else max_conn_interval_us,
                    phys=phys,
                )
            else:
                ble.gap_connect(
                    device.addr_type,
                    device.addr,
                    scan_duration_ms,
                    min_conn_interval_us,
                    max_conn_interval_us,
                )
            submitted = True

            # Wait for the connected IRQ.
            await connection._connect_event.wait()
            if not connection.is_connected() or _connecting.get(device, None) is not connection:
                raise OSError(-1)
            connection._run_task()
    except BaseException:
        if submitted or connection._conn_handle is not None:
            connection._cleanup_task = asyncio.create_task(_cleanup_connect(connection))
        raise
    finally:
        if connection._cleanup_task is None and _connecting.get(device, None) is connection:
            del _connecting[device]


# Represents a single device that has been found during a scan. The scan
# iterator will return the same ScanResult instance multiple times as its data
# changes (i.e. changing RSSI or advertising data).
class ScanResult:
    def __init__(self, device, sid=None):
        self.device = device
        self.adv_data = None
        self.resp_data = None
        self.rssi = None
        self.connectable = False
        self.sid = sid
        self.properties = None
        self.primary_phy = None
        self.secondary_phy = None
        self.periodic_interval = 0
        self.tx_power = None
        self.data_status = 0
        self._fragments = {}

    # New scan result available, return true if it changes our state.
    def _update(self, adv_type, rssi, adv_data):
        updated = False

        if rssi != self.rssi:
            self.rssi = rssi
            updated = True

        if adv_type in (_ADV_IND, _ADV_NONCONN_IND):
            if adv_data != self.adv_data:
                self.adv_data = adv_data
                self.connectable = adv_type == _ADV_IND
                updated = True
        elif adv_type == _ADV_SCAN_IND:
            if adv_data != self.adv_data and self.resp_data:
                updated = True
            self.adv_data = adv_data
        elif adv_type == _SCAN_RSP and adv_data:
            if adv_data != self.resp_data:
                self.resp_data = adv_data
                updated = True

        return updated

    def _update_extended(self, report):
        (
            _,
            properties,
            primary_phy,
            secondary_phy,
            _,
            periodic_interval,
            status,
            _,
            rssi,
            tx_power,
            payload,
        ) = report
        response = bool(properties & _PROP_SCAN_RESPONSE)
        fragments = self._fragments.pop(response, b"")
        payload = fragments + payload if fragments is not None else None
        if status == 1:
            self._fragments[response] = (
                payload
                if payload is not None and len(payload) <= _ADV_EXT_PAYLOAD_MAX_LEN
                else None
            )
            return False
        if payload is None or len(payload) > _ADV_EXT_PAYLOAD_MAX_LEN:
            status = 2

        field = "resp_data" if response else "adv_data"
        payload = payload if status == 0 else None
        metadata = (
            properties,
            primary_phy,
            secondary_phy,
            periodic_interval,
            rssi,
            tx_power,
            status,
        )
        previous = (
            self.properties,
            self.primary_phy,
            self.secondary_phy,
            self.periodic_interval,
            self.rssi,
            self.tx_power,
            self.data_status,
        )
        updated = metadata != previous or getattr(self, field) != payload
        (
            self.properties,
            self.primary_phy,
            self.secondary_phy,
            self.periodic_interval,
            self.rssi,
            self.tx_power,
            self.data_status,
        ) = metadata
        self.connectable = bool(properties & _PROP_CONNECTABLE)
        setattr(self, field, payload)
        return updated

    def __str__(self):
        return "Scan result: {} {}".format(self.device, self.rssi)

    # Gets all the fields for the specified types.
    def _decode_field(self, *adv_type):
        # Advertising payloads are repeated packets of the following form:
        #   1 byte data length (N + 1)
        #   1 byte type (see constants below)
        #   N bytes type-specific data
        for payload in (self.adv_data, self.resp_data):
            if not payload:
                continue
            i = 0
            while i + 1 < len(payload):
                end = i + payload[i] + 1
                if not payload[i] or end > len(payload):
                    break
                if payload[i + 1] in adv_type:
                    yield payload[i + 2 : end]
                i = end

    # Returns the value of the complete (or shortened) advertised name, if available.
    def name(self):
        for n in self._decode_field(_ADV_TYPE_NAME, _ADV_TYPE_SHORT_NAME):
            return str(n, "utf-8") if n else ""

    # Generator that enumerates the service UUIDs that are advertised.
    def services(self):
        for uuid_len, codes in (
            (2, (_ADV_TYPE_UUID16_INCOMPLETE, _ADV_TYPE_UUID16_COMPLETE)),
            (4, (_ADV_TYPE_UUID32_INCOMPLETE, _ADV_TYPE_UUID32_COMPLETE)),
            (16, (_ADV_TYPE_UUID128_INCOMPLETE, _ADV_TYPE_UUID128_COMPLETE)),
        ):
            for u in self._decode_field(*codes):
                for i in range(0, len(u), uuid_len):
                    yield bluetooth.UUID(u[i : i + uuid_len])

    # Generator that returns (manufacturer_id, data) tuples.
    def manufacturer(self, filter=None):
        for u in self._decode_field(_ADV_TYPE_MANUFACTURER):
            if len(u) < 2:
                continue
            m = struct.unpack("<H", u[0:2])[0]
            if filter is None or m == filter:
                yield (m, u[2:])


# Use with:
# async with aioble.scan(...) as scanner:
#   async for result in scanner:
#     ...
class scan:
    def __init__(
        self,
        duration_ms,
        interval_us=None,
        window_us=None,
        active=False,
        *,
        extended=False,
        phys=None,
    ):
        self._queue = []
        self._event = asyncio.ThreadSafeFlag()
        self._done_event = asyncio.ThreadSafeFlag()
        self._done = False

        # Keep track of what we've already seen.
        self._results = set()

        # Ideally we'd start the scan here and avoid having to save these
        # values, but we need to stop any previous scan first via awaiting
        # _cancel_pending(), but __init__ isn't async.
        self._duration_ms = duration_ms
        self._interval_us = interval_us or 1280000
        self._window_us = window_us or 11250
        self._active = active
        self._extended = extended or phys is not None
        self._phys = 1 if phys is None else phys

    async def __aenter__(self):
        global _active_scanner
        ensure_active()
        scan_ext = _ble5_method("gap_scan_ext") if self._extended else None
        await _cancel_pending()
        _active_scanner = self
        try:
            if scan_ext:
                scan_ext(
                    self._duration_ms,
                    interval_us=self._interval_us,
                    window_us=self._window_us,
                    active=self._active,
                    phys=self._phys,
                )
            else:
                ble.gap_scan(self._duration_ms, self._interval_us, self._window_us, self._active)
        except BaseException:
            _active_scanner = None
            self._done = True
            raise
        return self

    async def __aexit__(self, exc_type, exc_val, exc_traceback):
        # Cancel the current scan if we're still the active scanner. This will
        # happen if the loop breaks early before the scan duration completes.
        if _active_scanner == self:
            await self.cancel()

    def __aiter__(self):
        assert _active_scanner == self
        return self

    async def __anext__(self):
        global _active_scanner

        if _active_scanner != self:
            # The scan has been canceled (e.g. a connection was initiated).
            raise StopAsyncIteration

        while True:
            if _active_scanner != self:
                raise StopAsyncIteration
            while self._queue:
                # Extended fragments must be consumed in arrival order.
                report = self._queue.pop(0) if self._extended else self._queue.pop()
                if self._extended:
                    addr_type, _, _, _, sid, _, _, addr, _, _, _ = report
                else:
                    addr_type, addr, adv_type, rssi, adv_data = report
                    sid = None

                # Try to find an existing ScanResult for this device.
                for r in self._results:
                    if r.device.addr_type == addr_type and r.device.addr == addr and r.sid == sid:
                        result = r
                        break
                else:
                    # New device, create a new Device & ScanResult.
                    device = Device(addr_type, addr)
                    result = ScanResult(device, sid)
                    self._results.add(result)

                # Add the new information from this event.
                updated = (
                    result._update_extended(report)
                    if self._extended
                    else result._update(adv_type, rssi, adv_data)
                )
                if updated:
                    # It's new information, so re-yield this result.
                    return result

            if self._done:
                # _IRQ_SCAN_DONE event was fired.
                _active_scanner = None
                raise StopAsyncIteration

            # Wait for either done or result IRQ.
            await self._event.wait()

    # Cancel any in-progress scan. We need to do this before starting any other operation.
    async def cancel(self):
        global _active_scanner
        if _active_scanner != self:
            return
        if not self._done:
            if self._extended:
                ble.gap_scan_ext(None)
            else:
                ble.gap_scan(None)
        while not self._done:
            await self._done_event.wait()
        if _active_scanner == self:
            _active_scanner = None
