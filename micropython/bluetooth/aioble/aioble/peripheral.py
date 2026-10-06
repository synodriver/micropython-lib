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
    ble5_features,
)
from .device import Device, DeviceConnection, DeviceTimeout


_IRQ_CENTRAL_CONNECT = const(1)
_IRQ_CENTRAL_DISCONNECT = const(2)
_IRQ_ADV_COMPLETE_EXT = const(42)


_ADV_TYPE_FLAGS = const(0x01)
_ADV_TYPE_NAME = const(0x09)
_ADV_TYPE_UUID16_COMPLETE = const(0x3)
_ADV_TYPE_UUID32_COMPLETE = const(0x5)
_ADV_TYPE_UUID128_COMPLETE = const(0x7)
_ADV_TYPE_UUID16_MORE = const(0x2)
_ADV_TYPE_UUID32_MORE = const(0x4)
_ADV_TYPE_UUID128_MORE = const(0x6)
_ADV_TYPE_APPEARANCE = const(0x19)
_ADV_TYPE_MANUFACTURER = const(0xFF)

_ADV_PAYLOAD_MAX_LEN = const(31)
_CLEANUP_RETRY_MS = const(250)


_advertisers = {}
# IRQ 1 has no instance ID. Keep connections and their possible advertisers
# until IRQ 42 supplies it, including advertisers whose caller was cancelled.
_pending_connections = {}
_unclaimed_connections = {}
_abandoned_handles = set()
_cleanup_event = None
_cleanup_task = None


class _Advertiser:
    def __init__(self, connectable):
        self.connectable = connectable
        self.event = asyncio.ThreadSafeFlag()
        self.connection = None
        self.reason = None
        self.conn_handle = None
        self.abandoned = False
        self.removed = False


def _validate_instance(instance):
    if not isinstance(instance, int):
        raise TypeError("Advertising instance must be an integer")
    features = ble5_features()
    if not 1 <= instance < features["advertising_instances"]:
        raise ValueError("Invalid advertising instance")
    return features


def _waiting_connection(advertiser):
    if advertiser.connection is not None or advertiser.reason not in (None, 0):
        return False
    return (
        advertiser.reason == 0 and advertiser.connection is None and advertiser.connectable
    ) or any(advertiser in candidates for _, candidates in _pending_connections.values())


def _stop_advertiser(instance, advertiser, remove=True):
    if instance:
        ble.gap_advertise_ext_stop(instance, remove=remove)
    else:
        ble.gap_advertise(None)
    advertiser.removed = remove


async def _cleanup(event):
    global _cleanup_task
    try:
        while True:
            await event.wait()
            while ble.active():
                retry = False
                for handle, (connection, candidates) in tuple(_pending_connections.items()):
                    if connection._conn_handle is not None:
                        connection._run_task()
                    if not any(
                        not a.abandoned
                        and a.connection is None
                        and (a.conn_handle is None or a.conn_handle == handle)
                        for a in candidates
                    ):
                        _unclaimed_connections.setdefault(connection, False)
                for instance, advertiser in tuple(_advertisers.items()):
                    if not advertiser.abandoned:
                        continue
                    if advertiser.connection:
                        _unclaimed_connections.setdefault(advertiser.connection, False)
                    # Removing the controller instance now could suppress the late
                    # completion IRQ needed to distinguish concurrent advertisers.
                    if _waiting_connection(advertiser):
                        continue
                    try:
                        if not advertiser.removed:
                            _stop_advertiser(instance, advertiser)
                    except OSError as error:
                        log_error("Advertising cleanup; retrying", error)
                        retry = True
                    else:
                        if _advertisers.get(instance, None) is advertiser:
                            del _advertisers[instance]
                for connection, requested in tuple(_unclaimed_connections.items()):
                    if connection._conn_handle is None:
                        del _unclaimed_connections[connection]
                        continue
                    connection._run_task()
                    retry = True
                    if (
                        DeviceConnection._connected.get(connection._conn_handle, None)
                        is not connection
                    ):
                        connection._event.set()
                        continue
                    if not requested:
                        try:
                            ble.gap_disconnect(connection._conn_handle)
                        except OSError as error:
                            log_error(
                                "Disconnect unclaimed peripheral connection; retrying", error
                            )
                        else:
                            _unclaimed_connections[connection] = True
                if not retry:
                    break
                try:
                    await asyncio.wait_for_ms(event.wait(), _CLEANUP_RETRY_MS)
                except asyncio.TimeoutError:
                    pass
    finally:
        # A stopped worker may finish after stop() has created a new one.
        if _cleanup_event is event:
            _cleanup_task = None


def _ensure_cleanup_task():
    global _cleanup_event, _cleanup_task
    if _cleanup_task is None or _cleanup_task.done():
        _cleanup_event = asyncio.ThreadSafeFlag()
        _cleanup_task = asyncio.create_task(_cleanup(_cleanup_event))
        # A previous worker may have exited with cleanup still outstanding.
        _cleanup_event.set()


def _abandon_advertiser(instance, advertiser):
    advertiser.abandoned = True
    if ble.active() and _advertisers.get(instance, None) is advertiser:
        if advertiser.connection:
            _unclaimed_connections.setdefault(advertiser.connection, False)
        _ensure_cleanup_task()
        _cleanup_event.set()


def _peripheral_irq(event, data):
    if event == _IRQ_CENTRAL_CONNECT:
        conn_handle, addr_type, addr = data

        # Create, initialise, and register the device.
        device = Device(addr_type, bytes(addr))
        connection = DeviceConnection(device)
        connection._conn_handle = conn_handle
        DeviceConnection._connected[conn_handle] = connection
        # Retained cleanup records keep their original connection even when
        # its handle is reused after disconnection.
        candidates = tuple(
            a for a in _advertisers.values() if a.connectable and a.connection is None
        )
        _pending_connections[conn_handle] = (connection, candidates)

        # Signal advertise() to return the connected device.
        if (
            (advertiser := _advertisers.get(0, None))
            and advertiser.connectable
            and advertiser.connection is None
        ):
            advertiser.connection = connection
            advertiser.event.set()
            _pending_connections.pop(conn_handle, None)
        for instance, advertiser in _advertisers.items():
            if (
                instance
                and advertiser.connection is None
                and advertiser.conn_handle == conn_handle
            ):
                advertiser.connection = connection
                advertiser.event.set()
                _pending_connections.pop(conn_handle, None)
        if conn_handle in _abandoned_handles:
            _abandoned_handles.discard(conn_handle)
            _pending_connections.pop(conn_handle, None)
            _unclaimed_connections[connection] = False
        if _cleanup_event:
            _cleanup_event.set()

    elif event == _IRQ_ADV_COMPLETE_EXT:
        instance, reason, conn_handle = data
        if advertiser := _advertisers.get(instance, None):
            advertiser.reason = reason
            advertiser.conn_handle = conn_handle
            if (
                not reason
                and advertiser.connection is None
                and (pending := _pending_connections.pop(conn_handle, None))
            ):
                advertiser.connection = pending[0]
            if reason or advertiser.connection or not advertiser.connectable:
                advertiser.event.set()
        elif not reason and conn_handle != 0xFFFF:
            if pending := _pending_connections.pop(conn_handle, None):
                _unclaimed_connections[pending[0]] = False
            else:
                _abandoned_handles.add(conn_handle)
        if _cleanup_event:
            _cleanup_event.set()

    elif event == _IRQ_CENTRAL_DISCONNECT:
        conn_handle, _, _ = data
        if connection := DeviceConnection._connected.get(conn_handle, None):
            # Tell the device_task that it should terminate.
            connection._event.set()
        if _cleanup_event:
            _cleanup_event.set()


def _peripheral_shutdown():
    global _cleanup_event, _cleanup_task
    connections = set(_unclaimed_connections)
    connections.update(connection for connection, _ in _pending_connections.values())
    for advertiser in _advertisers.values():
        advertiser.reason = -1
        advertiser.event.set()
        if advertiser.connection:
            connections.add(advertiser.connection)
    _advertisers.clear()
    # A connect IRQ may have arrived before the cleanup task ran.
    for connection in connections:
        if connection._conn_handle is not None:
            connection._run_task()
            connection._event.set()
    _pending_connections.clear()
    _unclaimed_connections.clear()
    _abandoned_handles.clear()
    if _cleanup_task:
        _cleanup_task.cancel()
    _cleanup_task = None
    _cleanup_event = None


register_irq_handler(_peripheral_irq, _peripheral_shutdown)


# Advertising payloads are repeated packets of the following form:
#   1 byte data length (N + 1)
#   1 byte type (see constants below)
#   N bytes type-specific data
def _append(adv_data, resp_data, adv_type, value, max_len=_ADV_PAYLOAD_MAX_LEN, overflow=True):
    if len(value) > 254:
        raise ValueError("Advertising field too long")
    data = struct.pack("BB", len(value) + 1, adv_type) + value

    if len(data) + len(adv_data) <= max_len:
        adv_data += data
        return resp_data

    if overflow and len(data) + (len(resp_data) if resp_data else 0) <= max_len:
        if not resp_data:
            # Overflow into resp_data for the first time.
            resp_data = bytearray()
        resp_data += data
        return resp_data

    raise ValueError("Advertising payload too long")


async def advertise(  # noqa: PLR0913
    interval_us,
    adv_data=None,
    resp_data=None,
    connectable=True,
    limited_disc=False,
    br_edr=False,
    name=None,
    services=None,
    appearance=0,
    manufacturer=None,
    timeout_ms=None,
    *,
    extended=False,
    instance=1,
    scannable=False,
    primary_phy=1,
    secondary_phy=1,
    sid=0,
):
    ensure_active()
    advertise_ext = _ble5_method("gap_advertise_ext") if extended else None
    key = instance if extended else 0
    features = _validate_instance(instance) if extended else None
    if extended and connectable and scannable:
        raise ValueError("Extended advertising cannot be connectable and scannable")
    if key in _advertisers:
        raise ValueError("Advertising instance already in use")
    # The legacy connect IRQ has no advertising instance identifier.
    if connectable and any(a.connectable for i, a in _advertisers.items() if not i or not key):
        raise ValueError("Legacy and extended connectable advertising cannot overlap")
    max_len = features["max_adv_data_len"] if extended else _ADV_PAYLOAD_MAX_LEN
    if extended and connectable:
        max_len = min(max_len, 251)

    if (not extended and not adv_data and not resp_data) or (
        extended and adv_data is None and resp_data is None
    ):
        # If the user didn't manually specify adv_data / resp_data then
        # construct them from the kwargs. Keep adding fields to adv_data,
        # overflowing to resp_data if necessary.
        # TODO: Try and do better bin-packing than just concatenating in
        # order?

        adv_data = bytearray()

        resp_data = _append(
            adv_data,
            resp_data,
            _ADV_TYPE_FLAGS,
            struct.pack("B", (0x01 if limited_disc else 0x02) + (0x18 if br_edr else 0x04)),
            max_len,
            not extended,
        )

        # Services are prioritised to go in the advertising data because iOS supports
        # filtering scan results by service only, so services must come first.
        if services:
            for uuid_len, code in (
                (2, _ADV_TYPE_UUID16_COMPLETE),
                (4, _ADV_TYPE_UUID32_COMPLETE),
                (16, _ADV_TYPE_UUID128_COMPLETE),
            ):
                if uuids := [bytes(uuid) for uuid in services if len(bytes(uuid)) == uuid_len]:
                    resp_data = _append(
                        adv_data, resp_data, code, b"".join(uuids), max_len, not extended
                    )

        if name:
            resp_data = _append(adv_data, resp_data, _ADV_TYPE_NAME, name, max_len, not extended)

        if appearance:
            # See org.bluetooth.characteristic.gap.appearance.xml
            resp_data = _append(
                adv_data,
                resp_data,
                _ADV_TYPE_APPEARANCE,
                struct.pack("<H", appearance),
                max_len,
                not extended,
            )

        if manufacturer:
            resp_data = _append(
                adv_data,
                resp_data,
                _ADV_TYPE_MANUFACTURER,
                struct.pack("<H", manufacturer[0]) + manufacturer[1],
                max_len,
                not extended,
            )
        if extended and scannable:
            resp_data, adv_data = adv_data, b""

    if extended:
        # Do not reuse cached data from an earlier mode on this instance.
        adv_data = b"" if adv_data is None else adv_data
        resp_data = b"" if resp_data is None else resp_data

    advertiser = _Advertiser(connectable)
    _ensure_cleanup_task()
    _advertisers[key] = advertiser
    started = False
    result = None
    try:
        if extended:
            advertise_ext(
                interval_us,
                adv_data=adv_data,
                resp_data=resp_data,
                instance=instance,
                connectable=connectable,
                scannable=scannable,
                primary_phy=primary_phy,
                secondary_phy=secondary_phy,
                sid=sid,
            )
        else:
            ble.gap_advertise(
                interval_us, adv_data=adv_data, resp_data=resp_data, connectable=connectable
            )
        started = True
        # Allow optional timeout for a central to connect to us (or just to stop advertising).
        with DeviceTimeout(None, timeout_ms):
            while advertiser.connection is None and advertiser.reason is None:
                await advertiser.event.wait()
            # Completion may precede the connect IRQ.
            while advertiser.reason == 0 and advertiser.connection is None and connectable:
                await advertiser.event.wait()

        # Get the newly connected connection to the central and start a task
        # to wait for disconnection.
        if not advertiser.connection or advertiser.reason not in (None, 0):
            raise OSError(advertiser.reason)
        if not advertiser.connection.is_connected():
            raise OSError(-1)
        # This mirrors what connecting to a central does.
        advertiser.connection._run_task()
        result = advertiser.connection
    except asyncio.CancelledError:
        # Something else cancelled this task (to manually stop advertising).
        pass
    except BaseException:
        if extended and not started and ble.active():
            # A failed start may already have configured the controller instance.
            try:
                _stop_advertiser(key, advertiser, remove=not _waiting_connection(advertiser))
            except OSError:
                pass
        raise
    finally:
        exited = False
        try:
            if started and ble.active() and _advertisers.get(key, None) is advertiser:
                _stop_advertiser(key, advertiser, remove=not _waiting_connection(advertiser))
            exited = True
        finally:
            if result is None or not exited:
                _abandon_advertiser(key, advertiser)
            if (result is not None and exited) or (
                advertiser.removed and not _waiting_connection(advertiser)
            ):
                if _advertisers.get(key, None) is advertiser:
                    del _advertisers[key]
    # All fallible exit work has completed; the caller can now own the connection.
    return result


class periodic_advertise:
    def __init__(
        self,
        interval_us,
        adv_data=None,
        *,
        instance=1,
        sid=0,
        discovery_data=b"",
        extended_interval_us=100000,
        primary_phy=1,
        secondary_phy=1,
    ):
        self._interval_us = interval_us
        self._adv_data = adv_data
        self._instance = instance
        self._params = {
            "adv_data": discovery_data,
            "resp_data": b"",
            "instance": instance,
            "sid": sid,
            "connectable": False,
            "scannable": False,
            "primary_phy": primary_phy,
            "secondary_phy": secondary_phy,
        }
        self._extended_interval_us = extended_interval_us
        self._advertiser = None

    async def __aenter__(self):
        advertise_ext = _ble5_method("gap_advertise_ext")
        periodic = _ble5_method("gap_periodic_advertise")
        _validate_instance(self._instance)
        if self._instance in _advertisers:
            raise ValueError("Advertising instance already in use")
        self._advertiser = _Advertiser(False)
        _ensure_cleanup_task()
        _advertisers[self._instance] = self._advertiser
        try:
            advertise_ext(self._extended_interval_us, **self._params)
            periodic(self._interval_us, self._adv_data, instance=self._instance)
        except BaseException:
            try:
                if ble.active() and _advertisers.get(self._instance, None) is self._advertiser:
                    _stop_advertiser(self._instance, self._advertiser)
            except OSError:
                _abandon_advertiser(self._instance, self._advertiser)
            else:
                if _advertisers.get(self._instance, None) is self._advertiser:
                    del _advertisers[self._instance]
            raise
        return self

    async def __aexit__(self, exc_type, exc_val, exc_traceback):
        if (
            self._advertiser is None
            or _advertisers.get(self._instance, None) is not self._advertiser
        ):
            return
        try:
            if ble.active():
                _stop_advertiser(self._instance, self._advertiser)
        except OSError:
            _abandon_advertiser(self._instance, self._advertiser)
            raise
        else:
            if _advertisers.get(self._instance, None) is self._advertiser:
                del _advertisers[self._instance]
