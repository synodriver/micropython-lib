"""Host regressions for aioble's ESP32 BLE 5 adapter, using a simulated BLE API."""

import asyncio
import importlib
from pathlib import Path
import sys
import types
import unittest


class ThreadSafeFlag:
    def __init__(self):
        self._event = asyncio.Event()
        self._waiting = False

    def set(self):
        self._event.set()

    async def wait(self):
        if self._waiting:
            raise RuntimeError("ThreadSafeFlag only supports one waiting task")
        self._waiting = True
        try:
            await self._event.wait()
            self._event.clear()
        finally:
            self._waiting = False


async def sleep_ms(ms):
    await asyncio.sleep(ms / 1000)


async def wait_for_ms(awaitable, ms):
    return await asyncio.wait_for(awaitable, ms / 1000)


class FakeBLE:
    def __init__(self, modern):
        self.modern = modern
        self.enabled = False
        self.handler = None
        self.calls = []
        self.fail = {}
        self.tx_power_level = 9
        self.fail_by_instance = {}
        self.complete_sync_on_cancel = True
        self.defer_scan_stop = False
        self.defer_connect = False
        self.defer_indicate = False
        self.defer_disconnect = False
        self.complete_connect_on_cancel = True
        self.extended_instances = set()
        self.periodic_instances = set()

    def irq(self, handler):
        self.handler = handler

    def emit(self, event, data):
        self.handler(event, data)

    def active(self, value=None):
        if value is not None:
            self.enabled = value
            if not value:
                self.extended_instances.clear()
                self.periodic_instances.clear()
        return self.enabled

    def ble5_features(self):
        if not self.modern:
            raise AssertionError("Unsupported method should be absent")
        return {
            "phys": 7,
            "extended_advertising": True,
            "periodic_advertising": True,
            "tx_power_set": True,
            "tx_power_get": True,
            "advertising_instances": 3,
            "max_adv_data_len": 1650,
        }

    def __getattribute__(self, name):
        if name == "ble5_features" and not object.__getattribute__(self, "modern"):
            raise AttributeError(name)
        return object.__getattribute__(self, name)

    def __getattr__(self, name):
        extended = (
            name.endswith("_ext")
            or "periodic" in name
            or "phy" in name
            or name in ("gap_set_tx_power", "gap_get_tx_power")
        )
        if not name.startswith(("gap_", "gattc_", "gatts_", "l2cap_", "config")) or (
            extended and not self.modern
        ):
            raise AttributeError(name)

        def call(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            if name in ("gap_advertise_ext", "gap_advertise_ext_stop", "gap_periodic_advertise"):
                instance = (
                    args[0] if name == "gap_advertise_ext_stop" else kwargs.get("instance", 1)
                )
                if not isinstance(instance, int):
                    raise TypeError("Instance must be an integer")
                if not 1 <= instance < self.ble5_features()["advertising_instances"]:
                    raise OSError(22)
                if instance in self.fail_by_instance.get(name, {}):
                    failures = self.fail_by_instance[name][instance]
                    error = failures.pop(0)
                    if not failures:
                        del self.fail_by_instance[name][instance]
                    raise error
            if name in self.fail:
                if isinstance(self.fail[name], list):
                    error = self.fail[name].pop(0)
                    if not self.fail[name]:
                        del self.fail[name]
                    raise error
                raise self.fail.pop(name)
            if name == "gap_get_tx_power":
                return self.tx_power_level
            if name == "gap_advertise_ext":
                self.extended_instances.add(instance)
            elif name == "gap_periodic_advertise":
                self.periodic_instances.add(instance)
            elif name == "gap_advertise_ext_stop":
                self.periodic_instances.discard(instance)
                if kwargs.get("remove", False):
                    self.extended_instances.discard(instance)
            elif name in ("gap_scan", "gap_scan_ext") and args[0] is None:
                if self.defer_scan_stop:
                    asyncio.get_running_loop().call_soon(self.emit, 6, ())
                else:
                    self.emit(6, ())
            elif name == "gap_connect" and args[0] is not None:
                if not self.defer_connect:
                    asyncio.get_running_loop().call_soon(self.emit, 7, (10, args[0], args[1]))
            elif name == "gap_connect" and args[0] is None:
                if self.complete_connect_on_cancel:
                    asyncio.get_running_loop().call_soon(self.emit, 8, (0xFFFF, 0xFF, b"\0" * 6))
            elif name == "gap_connect_ext":
                if not self.defer_connect:
                    asyncio.get_running_loop().call_soon(self.emit, 7, (10, args[0], args[1]))
            elif name == "gap_disconnect" and not self.defer_disconnect:
                asyncio.get_running_loop().call_soon(self.emit, 2, (args[0], 0, b"abcdef"))
                asyncio.get_running_loop().call_soon(self.emit, 8, (args[0], 0, b"abcdef"))
            elif name == "gap_phy":
                return 1, 2
            elif name == "gap_set_phy" and args[0] is not None:
                asyncio.get_running_loop().call_soon(self.emit, 40, (args[0], 0, 2, 2))
            elif name == "gap_periodic_sync" and args[0] is None:
                if self.complete_sync_on_cancel:
                    asyncio.get_running_loop().call_soon(
                        self.emit, 43, (1, 0, 0, 0, 0, 0, b"\0" * 6)
                    )
            elif name == "gap_periodic_sync_stop":
                asyncio.get_running_loop().call_soon(self.emit, 45, (args[0], 0))
            elif name == "gattc_exchange_mtu":
                asyncio.get_running_loop().call_soon(self.emit, 21, (args[0], 64))
            elif name == "gattc_discover_services":
                asyncio.get_running_loop().call_soon(self.emit, 9, (args[0], 1, 10, 0x180F))
                asyncio.get_running_loop().call_soon(self.emit, 10, (args[0], 0))
            elif name == "gattc_discover_characteristics":
                asyncio.get_running_loop().call_soon(self.emit, 11, (args[0], 10, 2, 0x0A, 0x2A19))
                asyncio.get_running_loop().call_soon(self.emit, 12, (args[0], 0))
            elif name == "gattc_discover_descriptors":
                asyncio.get_running_loop().call_soon(self.emit, 13, (args[0], 3, 0x2902))
                asyncio.get_running_loop().call_soon(self.emit, 14, (args[0], 0))
            elif name == "gattc_read":
                asyncio.get_running_loop().call_soon(self.emit, 15, (args[0], args[1], b"read"))
                asyncio.get_running_loop().call_soon(self.emit, 16, (args[0], args[1], 0))
            elif name == "gattc_write" and args[3]:
                asyncio.get_running_loop().call_soon(self.emit, 17, (args[0], args[1], 0))
            elif name == "gatts_indicate" and not self.defer_indicate:
                asyncio.get_running_loop().call_soon(self.emit, 20, (args[0], args[1], 0))
            elif name == "gap_pair":
                asyncio.get_running_loop().call_soon(
                    self.emit, 28, (args[0], True, True, True, 16)
                )
            elif name == "l2cap_send":
                return True
            elif name == "l2cap_recvinto":
                return 0 if args[2] is None else 1
            elif name == "l2cap_disconnect":
                asyncio.get_running_loop().call_soon(self.emit, 24, (args[0], args[1], 22, 0))

        return call


class BLE5Tests(unittest.IsolatedAsyncioTestCase):
    def load_aioble(self, modern=True):
        for name in list(sys.modules):
            if name == "aioble" or name.startswith("aioble."):
                del sys.modules[name]
        self.ble = FakeBLE(modern)
        bluetooth = types.ModuleType("bluetooth")
        bluetooth.BLE = lambda: self.ble
        micropython = types.ModuleType("micropython")
        micropython.const = lambda value: value
        sys.modules["bluetooth"] = bluetooth
        sys.modules["micropython"] = micropython
        self.aioble = importlib.import_module("aioble")
        self.central = importlib.import_module("aioble.central")
        self.peripheral = importlib.import_module("aioble.peripheral")
        self.periodic = importlib.import_module("aioble.periodic")
        return self.aioble

    async def asyncSetUp(self):
        self.load_aioble()

    async def asyncTearDown(self):
        self.aioble.stop()
        await asyncio.sleep(0)

    async def settle(self):
        for _ in range(12):
            await asyncio.sleep(0)

    def report(self, sid=1, status=0, payload=b"\x04\x09MPY", properties=1):
        return (
            0,
            properties,
            1,
            2,
            sid,
            80,
            status,
            memoryview(b"abcdef"),
            -40,
            3,
            memoryview(payload),
        )

    async def test_legacy_scan_and_connect(self):
        async with self.aioble.scan(1000) as scanner:
            self.ble.emit(41, self.report())
            self.ble.emit(5, (0, b"abcdef", 0, -40, b"\x04\x09MPY"))
            result = await scanner.__anext__()
            self.assertEqual(result.name(), "MPY")
            self.assertIsNone(result.sid)
        connection = await result.device.connect()
        self.assertEqual(self.ble.calls[-1][0], "gap_connect")
        await connection.disconnect()

    async def test_set_tx_power_enables_ble_and_forwards_arguments(self):
        self.assertFalse(self.ble.enabled)
        self.assertIsNone(
            self.aioble.set_tx_power(self.aioble.TX_POWER_TYPE_DEFAULT, 0, self.aioble.TX_POWER_N0)
        )
        self.assertTrue(self.ble.enabled)
        self.assertEqual(self.ble.calls[-1], ("gap_set_tx_power", (0, 0, 8), {}))
        self.assertTrue(self.aioble.ble5_features()["tx_power_set"])
        for power_type, handle, power_level in ((1, 2, 11), (2, 0, 0), (3, 0, 15), (4, 513, 9)):
            self.aioble.set_tx_power(power_type, handle, power_level)
            self.assertEqual(
                self.ble.calls[-1], ("gap_set_tx_power", (power_type, handle, power_level), {})
            )

    async def test_set_tx_power_preserves_sdk_errors(self):
        error = OSError(95)
        self.ble.fail["gap_set_tx_power"] = error
        with self.assertRaises(OSError) as result:
            self.aioble.set_tx_power(self.aioble.TX_POWER_TYPE_ADV, 1, self.aioble.TX_POWER_P9)
        self.assertIs(result.exception, error)
        self.aioble.set_tx_power(self.aioble.TX_POWER_TYPE_ADV, 1, self.aioble.TX_POWER_P9)

    async def test_get_tx_power_enables_ble_and_forwards_arguments(self):
        self.assertFalse(self.ble.enabled)
        self.assertEqual(
            self.aioble.get_tx_power(self.aioble.TX_POWER_TYPE_DEFAULT, 0),
            self.aioble.TX_POWER_P3,
        )
        self.assertTrue(self.ble.enabled)
        self.assertEqual(self.ble.calls[-1], ("gap_get_tx_power", (0, 0), {}))
        for power_type, handle in ((1, 2), (2, 0), (3, 0), (4, 513)):
            self.assertEqual(self.aioble.get_tx_power(power_type, handle), self.aioble.TX_POWER_P3)
            self.assertEqual(self.ble.calls[-1], ("gap_get_tx_power", (power_type, handle), {}))

    async def test_get_tx_power_missing_on_old_or_intermediate_firmware(self):
        for modern in (False, True):
            self.load_aioble(modern)
            if modern:
                self.ble.gap_get_tx_power = None
                features = self.ble.ble5_features()
                del features["tx_power_get"]
                self.ble.ble5_features = lambda: features
            with self.assertRaises(NotImplementedError):
                self.aioble.get_tx_power(0, 0)
            self.assertFalse(self.ble.enabled)
            self.assertEqual(self.ble.calls, [])
            self.assertFalse(self.aioble.ble5_features().get("tx_power_get", False))

    async def test_get_tx_power_preserves_results_and_errors(self):
        self.assertTrue(self.aioble.ble5_features()["tx_power_get"])
        connection = await self.aioble.Device(0, b"abcdef").connect()
        for level in (0, 3, 15, None):
            self.ble.tx_power_level = level
            self.assertEqual(self.aioble.get_tx_power(0, 0), level)
            self.assertEqual(connection.get_tx_power(), level)
        for call in (lambda: self.aioble.get_tx_power(0, 0), connection.get_tx_power):
            error = OSError(19)
            self.ble.fail["gap_get_tx_power"] = error
            with self.assertRaises(OSError) as result:
                call()
            self.assertIs(result.exception, error)
        await connection.disconnect()

    async def test_set_tx_power_missing_on_old_or_intermediate_firmware(self):
        for modern in (False, True):
            self.load_aioble(modern)
            if modern:
                self.ble.gap_set_tx_power = None
            else:
                self.assertFalse(self.aioble.ble5_features()["tx_power_set"])
            with self.assertRaises(NotImplementedError):
                self.aioble.set_tx_power(0, 0, 8)
            self.assertFalse(self.ble.enabled)
            self.assertEqual(self.ble.calls, [])

    async def test_connection_set_tx_power_and_disconnect(self):
        connection = await self.aioble.Device(0, b"abcdef").connect()
        handle = connection._conn_handle
        self.assertIsNone(connection.set_tx_power(self.aioble.TX_POWER_P3))
        self.assertEqual(self.ble.calls[-1], ("gap_set_tx_power", (4, handle, 9), {}))
        await connection.disconnect()
        calls = len(self.ble.calls)
        with self.assertRaises(self.aioble.DeviceDisconnectedError):
            connection.set_tx_power(self.aioble.TX_POWER_N0)
        self.assertEqual(len(self.ble.calls), calls)

    async def test_connection_get_tx_power_and_disconnect(self):
        connection = await self.aioble.Device(0, b"abcdef").connect()
        handle = connection._conn_handle
        self.assertEqual(connection.get_tx_power(), self.aioble.TX_POWER_P3)
        self.assertEqual(self.ble.calls[-1], ("gap_get_tx_power", (4, handle), {}))
        await connection.disconnect()
        calls = len(self.ble.calls)
        with self.assertRaises(self.aioble.DeviceDisconnectedError):
            connection.get_tx_power()
        self.assertEqual(len(self.ble.calls), calls)

    async def test_connection_get_tx_power_on_old_firmware(self):
        self.load_aioble(False)
        connection = await self.aioble.Device(0, b"abcdef").connect()
        calls = len(self.ble.calls)
        with self.assertRaises(NotImplementedError):
            connection.get_tx_power()
        self.assertEqual(len(self.ble.calls), calls)
        await connection.disconnect()

    async def test_connection_set_tx_power_on_old_firmware(self):
        self.load_aioble(False)
        connection = await self.aioble.Device(0, b"abcdef").connect()
        calls = len(self.ble.calls)
        with self.assertRaises(NotImplementedError):
            connection.set_tx_power(self.aioble.TX_POWER_P3)
        self.assertEqual(len(self.ble.calls), calls)
        await connection.disconnect()

    async def test_old_firmware_import_and_explicit_requests(self):
        self.load_aioble(False)
        self.assertFalse(self.aioble.ble5_features()["extended_advertising"])
        async with self.aioble.scan(100):
            pass
        with self.assertRaises(NotImplementedError):
            async with self.aioble.scan(100, extended=True):
                pass
        with self.assertRaises(NotImplementedError):
            await self.aioble.advertise(100000, extended=True)
        device = self.aioble.Device(0, b"abcdef")
        with self.assertRaises(NotImplementedError):
            await device.connect(phys=4)
        self.assertIsNone(device._connection)
        connection = await device.connect()
        await connection.disconnect()

    async def test_extended_scan_copies_memoryviews_and_filters_legacy(self):
        async with self.aioble.scan(100, phys=5) as scanner:
            self.ble.emit(5, (0, b"abcdef", 0, -40, b"legacy"))
            payload = bytearray(b"\x04\x09MPY")
            addr = bytearray(b"abcdef")
            report = self.report(payload=payload)
            self.ble.emit(41, (*report[:7], memoryview(addr), *report[8:]))
            payload[:] = b"x" * len(payload)
            addr[:] = b"XXXXXX"
            result = await scanner.__anext__()
            self.assertEqual(result.name(), "MPY")
            self.assertEqual(result.device.addr, b"abcdef")
            self.assertEqual((result.sid, result.primary_phy, result.secondary_phy), (1, 1, 2))
            self.assertEqual(
                (result.periodic_interval, result.data_status, result.rssi, result.tx_power),
                (80, 0, -40, 3),
            )
            self.assertEqual(self.ble.calls[0][2]["phys"], 5)
            self.assertEqual(len(scanner._queue), 0)
        self.assertEqual(self.ble.calls[-1][0], "gap_scan_ext")

    async def test_extended_fragments_and_sid_are_independent(self):
        async with self.aioble.scan(100, extended=True) as scanner:
            self.ble.emit(41, self.report(sid=1, status=1, payload=b"\x04\x09"))
            self.ble.emit(41, self.report(sid=2, payload=b"\x04\x09TWO"))
            self.ble.emit(41, self.report(sid=1, payload=b"ONE"))
            two = await scanner.__anext__()
            one = await scanner.__anext__()
            self.assertEqual((two.sid, two.name()), (2, "TWO"))
            self.assertEqual((one.sid, one.name()), (1, "ONE"))
            self.ble.emit(41, self.report(sid=1, status=2, payload=b"broken"))
            truncated = await scanner.__anext__()
            self.assertEqual(truncated.data_status, 2)
            self.assertIsNone(truncated.adv_data)
            self.ble.emit(41, self.report(sid=1, payload=b"\x04\x09RSP", properties=9))
            response = await scanner.__anext__()
            self.assertEqual(response.resp_data, b"\x04\x09RSP")

    async def test_oversized_fragment_chain_is_not_complete(self):
        async with self.aioble.scan(100, extended=True) as scanner:
            self.ble.emit(41, self.report(status=1, payload=b"a" * 1650))
            self.ble.emit(41, self.report(status=1, payload=b"b"))
            self.ble.emit(41, self.report(payload=b"suffix"))
            result = await scanner.__anext__()
            self.assertEqual(result.data_status, 2)
            self.assertIsNone(result.adv_data)

    async def test_scan_start_failure_does_not_leave_active_scanner(self):
        self.ble.fail["gap_scan_ext"] = OSError(22)
        with self.assertRaises(OSError):
            async with self.aioble.scan(100, extended=True):
                pass
        self.assertIsNone(self.central._active_scanner)
        async with self.aioble.scan(100):
            pass

    async def test_switch_scan_with_iterator_waiting_and_deferred_stop(self):
        old = await self.aioble.scan(0, extended=True).__aenter__()
        waiter = asyncio.create_task(old.__anext__())
        await asyncio.sleep(0)
        self.ble.defer_scan_stop = True
        async with self.aioble.scan(100) as new:
            with self.assertRaises(StopAsyncIteration):
                await waiter
            self.assertIs(self.central._active_scanner, new)

    async def test_extended_connect_and_phy(self):
        device = self.aioble.Device(0, b"abcdef")
        connection = await device.connect(phys=4)
        name, args, kwargs = self.ble.calls[-1]
        self.assertEqual(name, "gap_connect_ext")
        self.assertEqual(args[2], 2000)
        self.assertEqual(kwargs["phys"], 4)
        self.assertEqual(kwargs["min_conn_interval_us"], 0)
        self.assertEqual(connection.phy(), (1, 2))
        self.assertEqual(await connection.set_phy(3, 3), (2, 2))
        self.aioble.set_default_phy(3, 3)
        self.assertEqual(self.ble.calls[-1][1], (None, 3, 3))
        await connection.disconnect()

    async def test_extended_advertise_payload_and_connection(self):
        task = asyncio.create_task(
            self.aioble.advertise(100000, name=b"N" * 50, extended=True, secondary_phy=2, sid=3)
        )
        await asyncio.sleep(0)
        name, _, kwargs = self.ble.calls[-1]
        self.assertEqual(name, "gap_advertise_ext")
        self.assertGreater(len(kwargs["adv_data"]), 31)
        self.assertEqual(kwargs["resp_data"], b"")
        self.ble.emit(1, (12, 0, memoryview(b"abcdef")))
        self.assertFalse(task.done())
        self.ble.emit(42, (1, 0, 12))
        connection = await task
        self.assertEqual(connection._conn_handle, 12)
        self.assertEqual(self.ble.calls[-1], ("gap_advertise_ext_stop", (1,), {"remove": True}))
        await connection.disconnect()

    async def test_old_disconnect_task_preserves_reused_handle_mapping(self):
        first = asyncio.create_task(self.aioble.advertise(100000, extended=True, instance=1))
        await asyncio.sleep(0)
        self.ble.emit(1, (20, 0, b"aaaaaa"))
        self.ble.emit(42, (1, 0, 20))
        old_connection = await first
        second = asyncio.create_task(self.aioble.advertise(100000, extended=True, instance=2))
        await asyncio.sleep(0)
        # Reuse the handle before the old device task consumes its disconnect.
        self.ble.emit(2, (20, 0, b"aaaaaa"))
        self.ble.emit(1, (20, 0, b"bbbbbb"))
        self.ble.emit(42, (2, 0, 20))
        new_connection = await second
        await self.settle()
        self.assertFalse(old_connection.is_connected())
        self.assertIs(self.peripheral.DeviceConnection._connected.get(20), new_connection)
        self.assertEqual(await new_connection.set_phy(2, 2, timeout_ms=100), (2, 2))
        await new_connection.disconnect(timeout_ms=100)
        self.assertFalse(new_connection.is_connected())
        self.assertNotIn(20, self.peripheral.DeviceConnection._connected)

    async def prepare_handle_reuse(self):
        first = asyncio.create_task(self.aioble.advertise(100000, extended=True, instance=1))
        await asyncio.sleep(0)
        self.ble.emit(1, (20, 0, b"aaaaaa"))
        self.ble.emit(42, (1, 0, 20))
        old_connection = await first
        second = asyncio.create_task(self.aioble.advertise(100000, extended=True, instance=2))
        await asyncio.sleep(0)
        return old_connection, second

    def reuse_handle(self, completion_first=False):
        self.ble.emit(2, (20, 0, b"aaaaaa"))
        if completion_first:
            self.ble.emit(42, (2, 0, 20))
            self.ble.emit(1, (20, 0, b"bbbbbb"))
        else:
            self.ble.emit(1, (20, 0, b"bbbbbb"))
            self.ble.emit(42, (2, 0, 20))

    async def assert_replacement_survives(self, old_connection, second):
        new_connection = await second
        await self.settle()
        self.assertIsNone(old_connection._conn_handle)
        self.assertIsNone(old_connection.device._connection)
        self.assertIs(self.peripheral.DeviceConnection._connected.get(20), new_connection)
        self.assertTrue(new_connection.is_connected())
        self.assertNotIn(("gap_disconnect", (20,), {}), self.ble.calls)
        self.assertEqual(await new_connection.set_phy(2, 2, timeout_ms=100), (2, 2))
        await new_connection.disconnect(timeout_ms=100)
        self.assertFalse(new_connection.is_connected())
        self.assertNotIn(20, self.peripheral.DeviceConnection._connected)

    async def assert_old_disconnect_preserves_replacement(self, completion_first):
        old_connection, second = await self.prepare_handle_reuse()
        self.reuse_handle(completion_first)
        # Call without yielding to the old device task after its disconnect IRQ.
        await old_connection.disconnect(timeout_ms=100)
        self.assertNotIn(("gap_disconnect", (20,), {}), self.ble.calls)
        self.assertIsNone(old_connection._conn_handle)
        await self.assert_replacement_survives(old_connection, second)

    async def test_old_disconnect_preserves_replacement_connect_first(self):
        await self.assert_old_disconnect_preserves_replacement(False)

    async def test_old_disconnect_preserves_replacement_completion_first(self):
        await self.assert_old_disconnect_preserves_replacement(True)

    async def assert_old_central_disconnect_preserves_replacement(self, phys):
        old_connection = await self.aioble.Device(0, b"aaaaaa").connect(phys=phys)
        self.ble.defer_connect = True
        device = self.aioble.Device(0, b"bbbbbb")
        task = asyncio.create_task(device.connect(phys=phys))
        await asyncio.sleep(0)
        self.ble.emit(8, (10, 0, b"aaaaaa"))
        self.ble.emit(7, (10, 0, b"bbbbbb"))
        await old_connection.disconnect(timeout_ms=100)
        self.assertNotIn(("gap_disconnect", (10,), {}), self.ble.calls)
        self.assertIsNone(old_connection._conn_handle)
        new_connection = await task
        self.assertIs(self.peripheral.DeviceConnection._connected.get(10), new_connection)
        self.assertTrue(new_connection.is_connected())
        self.assertEqual(await new_connection.set_phy(2, 2, timeout_ms=100), (2, 2))
        await new_connection.disconnect(timeout_ms=100)
        self.assertFalse(new_connection.is_connected())

    async def test_old_central_disconnect_preserves_legacy_replacement(self):
        await self.assert_old_central_disconnect_preserves_replacement(None)

    async def test_old_central_disconnect_preserves_extended_replacement(self):
        await self.assert_old_central_disconnect_preserves_replacement(4)

    async def test_reconnect_replaces_stale_device_cache(self):
        old_connection, second = await self.prepare_handle_reuse()
        device = old_connection.device
        self.reuse_handle()
        reconnected = await device.connect(phys=4)
        self.assertIsNot(reconnected, old_connection)
        self.assertIs(device._connection, reconnected)
        self.assertIsNone(old_connection._conn_handle)
        self.assertTrue(reconnected.is_connected())
        await reconnected.disconnect()
        await self.assert_replacement_survives(old_connection, second)

    async def test_reused_handle_still_cancels_old_operation(self):
        old_connection, second = await self.prepare_handle_reuse()

        async def wait_for_old_connection():
            with old_connection.timeout(100):
                await asyncio.Event().wait()

        task = asyncio.create_task(wait_for_old_connection())
        await asyncio.sleep(0)
        self.reuse_handle()
        await old_connection.disconnect(timeout_ms=100)
        with self.assertRaises(self.aioble.DeviceDisconnectedError):
            await task
        self.assertEqual(old_connection._timeouts, [])
        await self.assert_replacement_survives(old_connection, second)

    async def test_central_cleanup_finishes_old_task_after_handle_reuse(self):
        release_old_task = asyncio.Event()
        connection_type = self.peripheral.DeviceConnection
        original = connection_type.device_task

        async def delayed_device_task(connection):
            if connection.device.addr == b"aaaaaa":
                await connection._event.wait()
                await release_old_task.wait()
                connection._event.set()
            await original(connection)

        connection_type.device_task = delayed_device_task
        device = self.aioble.Device(0, b"aaaaaa")
        task = asyncio.create_task(device.connect(phys=4))
        await asyncio.sleep(0)
        old_connection = device._connection
        self.ble.fail["gap_disconnect"] = OSError(12)
        asyncio.get_running_loop().call_soon(task.cancel)
        with self.assertRaises(asyncio.CancelledError):
            await task
        await self.settle()
        self.ble.emit(8, (10, 0, b"aaaaaa"))
        new_connection = await self.aioble.Device(0, b"bbbbbb").connect(phys=4)
        try:
            await asyncio.sleep(0.3)
            self.assertIsNotNone(old_connection._cleanup_task)
            self.assertIs(self.central._connecting.get(device), old_connection)
            self.assertEqual(self.ble.calls.count(("gap_disconnect", (10,), {})), 1)
            self.assertTrue(new_connection.is_connected())
        finally:
            release_old_task.set()
        await self.settle()
        self.assertIsNone(old_connection._conn_handle)
        self.assertIsNone(old_connection._cleanup_task)
        self.assertNotIn(device, self.central._connecting)
        self.assertIs(connection_type._connected.get(10), new_connection)
        self.assertEqual(await new_connection.set_phy(2, 2), (2, 2))
        await new_connection.disconnect()

    async def test_stale_phy_mtu_and_timeout_reject_reused_handle(self):
        old_connection, second = await self.prepare_handle_reuse()
        self.reuse_handle()
        calls = len(self.ble.calls)
        self.assertFalse(old_connection.is_connected())
        with self.assertRaises(self.aioble.DeviceDisconnectedError):
            old_connection.phy()
        with self.assertRaises(self.aioble.DeviceDisconnectedError):
            old_connection.set_tx_power(self.aioble.TX_POWER_P3)
        with self.assertRaises(self.aioble.DeviceDisconnectedError):
            old_connection.get_tx_power()
        with self.assertRaises(self.aioble.DeviceDisconnectedError):
            await old_connection.set_phy(2, 2, timeout_ms=10)
        with self.assertRaises(ValueError):
            await old_connection.exchange_mtu(128, timeout_ms=10)
        with self.assertRaises(self.aioble.DeviceDisconnectedError):
            with old_connection.timeout(10):
                pass
        self.assertEqual(len(self.ble.calls), calls)
        self.assertEqual(old_connection._timeouts, [])
        await self.assert_replacement_survives(old_connection, second)

    async def test_stale_gatt_discovery_rejects_reused_handle(self):
        client = importlib.import_module("aioble.client")
        old_connection, second = await self.prepare_handle_reuse()
        service = client.ClientService(old_connection, 1, 10, 0x180F)
        characteristic = client.ClientCharacteristic(service, 10, 2, 0x0A, 0x2A19)
        discoveries = (
            old_connection.services(),
            service.characteristics(),
            characteristic.descriptors(),
        )
        self.reuse_handle()
        calls = len(self.ble.calls)
        for discovery in discoveries:
            with self.assertRaises(self.aioble.DeviceDisconnectedError):
                with self.peripheral.DeviceTimeout(None, 50):
                    await discovery.__anext__()
        self.assertEqual(len(self.ble.calls), calls)
        self.assertIsNone(old_connection._discover)
        await self.assert_replacement_survives(old_connection, second)

    async def test_stale_gatt_values_reject_reused_handle(self):
        client = importlib.import_module("aioble.client")
        old_connection, second = await self.prepare_handle_reuse()
        service = client.ClientService(old_connection, 1, 10, 0x180F)
        characteristic = client.ClientCharacteristic(service, 10, 2, 0x3E, 0x2A19)
        descriptor = client.ClientDescriptor(characteristic, 3, 0x2902)
        self.reuse_handle()
        calls = len(self.ble.calls)
        for value in (characteristic, descriptor):
            with self.assertRaises(self.aioble.DeviceDisconnectedError):
                await value.read(timeout_ms=10)
            for response in (False, True):
                with self.assertRaises(self.aioble.DeviceDisconnectedError):
                    await value.write(b"write", response=response, timeout_ms=10)
        with self.assertRaises(self.aioble.DeviceDisconnectedError):
            await characteristic.notified(timeout_ms=10)
        with self.assertRaises(self.aioble.DeviceDisconnectedError):
            await characteristic.indicated(timeout_ms=10)
        with self.assertRaises(self.aioble.DeviceDisconnectedError):
            await characteristic.subscribe()
        self.assertEqual(len(self.ble.calls), calls)
        self.assertEqual(old_connection._characteristics, {})
        await self.assert_replacement_survives(old_connection, second)

    async def test_stale_server_updates_reject_reused_handle(self):
        server = importlib.import_module("aioble.server")
        characteristic = server.Characteristic(
            server.Service(0x180F), 0x2A19, notify=True, indicate=True
        )
        characteristic._value_handle = 2
        old_connection, second = await self.prepare_handle_reuse()
        self.reuse_handle()
        calls = len(self.ble.calls)
        with self.assertRaises(self.aioble.DeviceDisconnectedError):
            characteristic.notify(old_connection, b"notify")
        with self.assertRaises(ValueError):
            await characteristic.indicate(old_connection, b"indicate", timeout_ms=10)
        self.assertEqual(len(self.ble.calls), calls)
        await self.assert_replacement_survives(old_connection, second)

    async def test_stale_pairing_rejects_reused_handle(self):
        sys.modules["micropython"].schedule = lambda callback, arg: None
        importlib.import_module("aioble.security")
        old_connection, second = await self.prepare_handle_reuse()
        self.reuse_handle()
        calls = len(self.ble.calls)
        with self.assertRaises(self.aioble.DeviceDisconnectedError):
            await old_connection.pair(timeout_ms=10)
        self.assertEqual(len(self.ble.calls), calls)
        self.assertIsNone(old_connection._pair_event)
        await self.assert_replacement_survives(old_connection, second)

    async def test_stale_l2cap_operations_reject_reused_handle(self):
        l2cap = importlib.import_module("aioble.l2cap")
        old_connection, second = await self.prepare_handle_reuse()
        channel = l2cap.L2CAPChannel(old_connection)
        channel._cid = 4
        channel.our_mtu = channel.peer_mtu = 32
        channel._data_ready = True
        self.reuse_handle()
        calls = len(self.ble.calls)
        with self.assertRaises(l2cap.L2CAPDisconnectedError):
            channel.available()
        with self.assertRaises(l2cap.L2CAPDisconnectedError):
            await channel.recvinto(bytearray(8), timeout_ms=10)
        with self.assertRaises(l2cap.L2CAPDisconnectedError):
            await channel.send(b"send", timeout_ms=10)
        with self.assertRaises(l2cap.L2CAPDisconnectedError):
            await channel.flush(timeout_ms=10)
        await channel.disconnect(timeout_ms=10)
        with self.assertRaises(ValueError):
            await old_connection.l2cap_connect(22, 32, timeout_ms=10)
        with self.assertRaises(ValueError):
            await old_connection.l2cap_accept(22, 32, timeout_ms=10)
        self.assertEqual(len(self.ble.calls), calls)
        await self.assert_replacement_survives(old_connection, second)

    async def test_valid_gatt_pair_and_mtu_operations(self):
        sys.modules["micropython"].schedule = lambda callback, arg: None
        importlib.import_module("aioble.security")
        client = importlib.import_module("aioble.client")
        server = importlib.import_module("aioble.server")
        connection = await self.aioble.Device(0, b"abcdef").connect(phys=4)
        self.assertEqual(await connection.exchange_mtu(64, timeout_ms=100), 64)
        await connection.pair(timeout_ms=100)
        self.assertTrue(connection.encrypted)
        service = client.ClientService(connection, 1, 10, 0x180F)
        characteristic = client.ClientCharacteristic(service, 10, 2, 0x0E, 0x2A19)
        self.assertEqual(await characteristic.read(timeout_ms=100), b"read")
        await characteristic.write(b"write", response=True, timeout_ms=100)
        await characteristic.write(b"write", response=False)
        server_characteristic = server.Characteristic(
            server.Service(0x180F), 0x2A19, notify=True, indicate=True
        )
        server_characteristic._register(2)
        server_characteristic.notify(connection, b"notify")
        await server_characteristic.indicate(connection, b"indicate", timeout_ms=100)
        await connection.disconnect()

    async def test_valid_l2cap_operations(self):
        l2cap = importlib.import_module("aioble.l2cap")
        connection = await self.aioble.Device(0, b"abcdef").connect(phys=4)
        channel = l2cap.L2CAPChannel(connection)
        channel._cid = 4
        channel.our_mtu = channel.peer_mtu = 32
        channel._data_ready = True
        self.assertTrue(channel.available())
        self.assertEqual(await channel.recvinto(bytearray(8), timeout_ms=100), 1)
        await channel.send(b"send", timeout_ms=100)
        await channel.flush(timeout_ms=100)
        await channel.disconnect(timeout_ms=100)
        self.assertIsNone(channel._cid)
        await connection.disconnect()

    async def test_valid_gatt_discovery_and_descriptor_operations(self):
        sys.modules["bluetooth"].UUID = lambda value: value
        connection = await self.aioble.Device(0, b"abcdef").connect(phys=4)
        service = await connection.service(0x180F)
        self.assertIs(service.connection, connection)
        characteristic = await service.characteristic(0x2A19)
        descriptor = await characteristic.descriptor(0x2902)
        self.assertIs(descriptor.characteristic, characteristic)
        self.assertEqual(await descriptor.read(timeout_ms=100), b"read")
        await descriptor.write(b"write", timeout_ms=100)
        self.assertIsNone(connection._discover)
        await connection.disconnect()

    async def prepare_indication_connections(self):
        server = importlib.import_module("aioble.server")
        characteristic = server.Characteristic(server.Service(0x180F), 0x2A19, indicate=True)
        characteristic._register(2)
        first = asyncio.create_task(self.aioble.advertise(100000, extended=True, instance=1))
        second = asyncio.create_task(self.aioble.advertise(100000, extended=True, instance=2))
        await asyncio.sleep(0)
        self.ble.emit(1, (20, 0, b"aaaaaa"))
        self.ble.emit(42, (1, 0, 20))
        self.ble.emit(1, (21, 0, b"bbbbbb"))
        self.ble.emit(42, (2, 0, 21))
        a, b = await asyncio.gather(first, second)
        self.ble.defer_indicate = True
        return characteristic, a, b

    async def assert_late_indication_does_not_complete_other_connection(self, cancel):
        characteristic, a, b = await self.prepare_indication_connections()
        if cancel:
            first = asyncio.create_task(characteristic.indicate(a, b"first", timeout_ms=100))
            await asyncio.sleep(0)
            first.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first
        else:
            with self.assertRaises(asyncio.TimeoutError):
                await characteristic.indicate(a, b"first", timeout_ms=1)
        second = asyncio.create_task(characteristic.indicate(b, b"second", timeout_ms=100))
        await asyncio.sleep(0)
        self.assertIs(characteristic._indicate_connection, b)
        # A's late confirmation must not raise in IRQ or wake B's waiter.
        self.ble.emit(20, (20, 2, 0))
        await self.settle()
        self.assertFalse(second.done())
        self.assertIsNone(characteristic._indicate_status)
        self.ble.emit(20, (21, 2, 0))
        await second
        self.assertIsNone(characteristic._indicate_connection)
        self.assertTrue(a.is_connected())
        self.assertTrue(b.is_connected())
        self.assertEqual(await a.set_phy(2, 2, timeout_ms=100), (2, 2))
        await a.disconnect()
        await b.disconnect()

    async def test_late_indication_after_timeout_does_not_complete_other_connection(self):
        await self.assert_late_indication_does_not_complete_other_connection(False)

    async def test_late_indication_after_cancel_does_not_complete_other_connection(self):
        await self.assert_late_indication_does_not_complete_other_connection(True)

    async def test_indication_matching_error_survives_unrelated_completions(self):
        characteristic, a, b = await self.prepare_indication_connections()
        task = asyncio.create_task(characteristic.indicate(b, b"data", timeout_ms=100))
        await asyncio.sleep(0)
        self.ble.emit(20, (20, 2, 7))
        self.ble.emit(20, (99, 2, 7))
        self.ble.emit(20, (21, 99, 7))
        await self.settle()
        self.assertFalse(task.done())
        self.assertIsNone(characteristic._indicate_status)
        self.ble.emit(20, (21, 2, 5))
        with self.assertRaises(self.aioble.GattError) as error:
            await task
        self.assertEqual(error.exception._status, 5)
        self.assertIsNone(characteristic._indicate_connection)
        # No waiter is active, so both peers' subsequent completions are ignored.
        self.ble.emit(20, (20, 2, 0))
        self.ble.emit(20, (21, 2, 0))
        self.ble.defer_indicate = False
        await characteristic.indicate(a, b"retry", timeout_ms=100)
        await characteristic.indicate(b, b"retry", timeout_ms=100)
        await a.disconnect()
        await b.disconnect()

    async def assert_cancel_after_indication_completion_does_not_affect_next(self, status):
        characteristic, a, b = await self.prepare_indication_connections()
        first = asyncio.create_task(characteristic.indicate(a, b"first", timeout_ms=100))
        await asyncio.sleep(0)
        self.ble.emit(20, (20, 2, 0))
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        self.assertIsNone(characteristic._indicate_connection)
        second = asyncio.create_task(characteristic.indicate(b, b"second", timeout_ms=100))
        await self.settle()
        self.assertFalse(second.done())
        self.assertIs(characteristic._indicate_connection, b)
        self.assertIsNone(characteristic._indicate_status)
        self.ble.emit(20, (21, 2, status))
        if status:
            with self.assertRaises(self.aioble.GattError) as error:
                await second
            self.assertEqual(error.exception._status, status)
        else:
            await second
        self.ble.defer_indicate = False
        await characteristic.indicate(a, b"retry", timeout_ms=100)
        await a.disconnect()
        await b.disconnect()

    async def test_cancel_after_indication_completion_keeps_next_wait_pending(self):
        await self.assert_cancel_after_indication_completion_does_not_affect_next(0)

    async def test_cancel_after_indication_completion_preserves_next_error(self):
        await self.assert_cancel_after_indication_completion_does_not_affect_next(5)

    async def assert_indication_retry_waits_for_late_completion(self, cancel):
        server = importlib.import_module("aioble.server")
        characteristic = server.Characteristic(server.Service(0x180F), 0x2A19, indicate=True)
        characteristic._register(2)
        connection = await self.aioble.Device(0, b"abcdef").connect(phys=4)
        self.ble.defer_indicate = True

        if cancel:
            first = asyncio.create_task(
                characteristic.indicate(connection, b"first", timeout_ms=100)
            )
            await asyncio.sleep(0)
            first.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first
        else:
            with self.assertRaises(asyncio.TimeoutError):
                await characteristic.indicate(connection, b"first", timeout_ms=1)

        second = asyncio.create_task(
            characteristic.indicate(connection, b"second", timeout_ms=100)
        )
        with self.assertRaises(ValueError):
            await second
        self.assertEqual(
            [call for call in self.ble.calls if call[0] == "gatts_indicate"],
            [("gatts_indicate", (10, 2, b"first"), {})],
        )

        # The old completion clears the abandoned controller request, but does
        # not create a completion for a later operation.
        self.ble.emit(20, (10, 2, 0))
        second = asyncio.create_task(
            characteristic.indicate(connection, b"second", timeout_ms=100)
        )
        await asyncio.sleep(0)
        self.assertFalse(second.done())
        self.assertEqual(
            [call for call in self.ble.calls if call[0] == "gatts_indicate"],
            [
                ("gatts_indicate", (10, 2, b"first"), {}),
                ("gatts_indicate", (10, 2, b"second"), {}),
            ],
        )
        self.ble.emit(20, (10, 2, 0))
        await second
        await connection.disconnect()

    async def test_indication_retry_after_timeout_waits_for_old_completion(self):
        await self.assert_indication_retry_waits_for_late_completion(False)

    async def test_indication_retry_after_cancel_waits_for_old_completion(self):
        await self.assert_indication_retry_waits_for_late_completion(True)

    async def abandon_indication(self, characteristic, connection, cancel):
        if cancel:
            task = asyncio.create_task(characteristic.indicate(connection, timeout_ms=None))
            await self.settle()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        else:
            with self.assertRaises(asyncio.TimeoutError):
                await characteristic.indicate(connection, timeout_ms=1)

    async def test_indication_tracks_all_abandoned_connections(self):
        characteristic, a, b = await self.prepare_indication_connections()
        for cancel_a in (False, True):
            for cancel_b in (False, True):
                with self.subTest(cancel_a=cancel_a, cancel_b=cancel_b):
                    await self.abandon_indication(characteristic, a, cancel_a)
                    await self.abandon_indication(characteristic, b, cancel_b)
                    calls = len(self.ble.calls)
                    for connection in (a, b):
                        with self.assertRaises(ValueError):
                            await characteristic.indicate(connection, timeout_ms=1)
                    self.assertEqual(len(self.ble.calls), calls)
                    # A's final error releases A only; B must remain blocked.
                    self.ble.emit(20, (20, 2, 5))
                    retry = asyncio.create_task(characteristic.indicate(a, timeout_ms=None))
                    await self.settle()
                    self.ble.emit(20, (21, 2, 0))
                    await self.settle()
                    self.assertFalse(retry.done())
                    self.ble.emit(20, (20, 2, 0))
                    await retry
        await a.disconnect()
        await b.disconnect()

    async def test_indication_serializes_characteristics_on_same_connection(self):
        characteristic, a, b = await self.prepare_indication_connections()
        server = importlib.import_module("aioble.server")
        other = server.Characteristic(server.Service(0x180F), 0x2A1A, indicate=True)
        other._register(3)
        first = asyncio.create_task(characteristic.indicate(a, timeout_ms=None))
        await self.settle()
        calls = len(self.ble.calls)
        with self.assertRaises(ValueError):
            await other.indicate(a, timeout_ms=1)
        self.assertEqual(len(self.ble.calls), calls)
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        with self.assertRaises(ValueError):
            await other.indicate(a, timeout_ms=1)
        self.ble.emit(20, (20, 2, 0))
        retry = asyncio.create_task(other.indicate(a, timeout_ms=None))
        await self.settle()
        self.ble.emit(20, (20, 2, 0))
        await self.settle()
        self.assertFalse(retry.done())
        self.ble.emit(20, (20, 3, 0))
        await retry
        await a.disconnect()
        await b.disconnect()

    async def prepare_disconnect_connection(self, central):
        if central:
            return await self.aioble.Device(0, b"abcdef").connect(phys=4), 8
        task = asyncio.create_task(self.aioble.advertise(100000, extended=True, instance=1))
        await asyncio.sleep(0)
        self.ble.emit(1, (20, 0, b"abcdef"))
        self.ble.emit(42, (1, 0, 20))
        return await task, 2

    async def test_indication_disconnect_releases_abandoned_requests(self):
        server = importlib.import_module("aioble.server")
        characteristic = server.Characteristic(server.Service(0x180F), 0x2A19, indicate=True)
        characteristic._register(2)
        self.ble.defer_indicate = True
        for central in (False, True):
            for cancel in (False, True):
                with self.subTest(central=central, cancel=cancel):
                    connection, irq = await self.prepare_disconnect_connection(central)
                    handle = connection._conn_handle
                    await self.abandon_indication(characteristic, connection, cancel)
                    self.assertIn(connection, server._pending_indications)
                    self.ble.emit(irq, (handle, 0, b"abcdef"))
                    self.assertNotIn(connection, server._pending_indications)
                    await connection.disconnected(timeout_ms=100)
                    self.ble.emit(20, (handle, 2, 0))
        self.assertEqual(server._pending_indications, {})

    async def test_indication_disconnect_and_handle_reuse_preserve_new_request(self):
        server = importlib.import_module("aioble.server")
        characteristic = server.Characteristic(server.Service(0x180F), 0x2A19, indicate=True)
        characteristic._register(2)
        old, second = await self.prepare_handle_reuse()
        self.ble.defer_indicate = True
        task = asyncio.create_task(characteristic.indicate(old, timeout_ms=None))
        await self.settle()
        self.reuse_handle()
        self.assertNotIn(old, server._pending_indications)
        new = await second
        with self.assertRaises(self.aioble.DeviceDisconnectedError):
            await task
        retry = asyncio.create_task(characteristic.indicate(new, timeout_ms=None))
        await self.settle()
        self.assertFalse(retry.done())
        self.assertTrue(new.is_connected())
        self.ble.emit(20, (20, 2, 0))
        await retry
        await new.disconnect()

    async def test_indication_stop_clears_all_requests_before_restart(self):
        characteristic, a, b = await self.prepare_indication_connections()
        server = importlib.import_module("aioble.server")
        await self.abandon_indication(characteristic, a, False)
        await self.abandon_indication(characteristic, b, True)
        self.assertEqual(len(server._pending_indications), 2)
        self.aioble.stop()
        self.assertEqual(server._pending_indications, {})
        # FakeBLE.active(False) does not synthesize the stack's disconnect IRQs.
        self.ble.emit(2, (20, 0, b"aaaaaa"))
        self.ble.emit(2, (21, 0, b"bbbbbb"))
        await self.settle()
        new, _ = await self.prepare_disconnect_connection(False)
        characteristic._register(2)
        retry = asyncio.create_task(characteristic.indicate(new, timeout_ms=None))
        await self.settle()
        self.assertFalse(retry.done())
        self.ble.emit(20, (20, 2, 0))
        await retry
        await new.disconnect()

    async def test_indication_submission_failure_allows_retry(self):
        characteristic, a, b = await self.prepare_indication_connections()
        self.ble.fail["gatts_indicate"] = OSError(12)
        with self.assertRaises(OSError):
            await characteristic.indicate(a)
        self.ble.defer_indicate = False
        await characteristic.indicate(a)
        await a.disconnect()
        await b.disconnect()

    async def test_indication_failed_connect_irq_does_not_release_pending_request(self):
        characteristic, a, b = await self.prepare_indication_connections()
        await self.abandon_indication(characteristic, a, True)
        # A failed outgoing attempt does not identify a real disconnected peer.
        self.ble.emit(8, (20, 0xFF, b"\x00" * 6))
        calls = len(self.ble.calls)
        with self.assertRaises(ValueError):
            await characteristic.indicate(a, timeout_ms=1)
        self.assertEqual(len(self.ble.calls), calls)
        self.ble.emit(20, (20, 2, 0))
        self.ble.defer_indicate = False
        await characteristic.indicate(a)
        await a.disconnect()
        await b.disconnect()

    async def test_indication_synchronous_completion_allows_retry(self):
        characteristic, a, b = await self.prepare_indication_connections()
        self.ble.gatts_indicate = lambda handle, value, data: self.ble.emit(20, (handle, value, 0))
        await characteristic.indicate(a)
        await characteristic.indicate(a)
        await characteristic.indicate(b)
        await a.disconnect()
        await b.disconnect()

    async def test_indication_different_connections_can_use_different_characteristics(self):
        characteristic, a, b = await self.prepare_indication_connections()
        server = importlib.import_module("aioble.server")
        other = server.Characteristic(server.Service(0x180F), 0x2A1A, indicate=True)
        other._register(3)
        first = asyncio.create_task(characteristic.indicate(a, timeout_ms=None))
        second = asyncio.create_task(other.indicate(b, timeout_ms=None))
        await self.settle()
        self.assertFalse(first.done())
        self.assertFalse(second.done())
        self.ble.emit(20, (21, 3, 0))
        await second
        self.assertFalse(first.done())
        self.ble.emit(20, (20, 2, 0))
        await first
        await a.disconnect()
        await b.disconnect()

    async def assert_disconnect_wait_keeps_cleanup_alive(self, central, cancel, disconnect=False):
        connection, irq = await self.prepare_disconnect_connection(central)
        handle = connection._conn_handle
        cleanup = connection._task
        self.ble.defer_disconnect = True

        async def remote_operation():
            with connection.timeout(None):
                await asyncio.Event().wait()

        operation = asyncio.create_task(remote_operation())
        await asyncio.sleep(0)
        method = connection.disconnect if disconnect else connection.disconnected
        if cancel:
            waiter = asyncio.create_task(method(timeout_ms=None))
            await asyncio.sleep(0)
            waiter.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await waiter
        else:
            with self.assertRaises(asyncio.TimeoutError):
                await method(timeout_ms=1)
        self.assertFalse(cleanup.done())
        self.assertTrue(connection.is_connected())
        self.assertIs(connection.device._connection, connection)
        self.assertFalse(operation.done())
        self.assertEqual(
            self.ble.calls.count(("gap_disconnect", (handle,), {})), 1 if disconnect else 0
        )
        # Real peer disconnection still has a live owner after the public wait ends.
        self.ble.emit(irq, (handle, 0, b"abcdef"))
        await self.settle()
        self.assertTrue(cleanup.done())
        self.assertFalse(cleanup.cancelled())
        self.assertIsNone(connection._conn_handle)
        self.assertIsNone(connection.device._connection)
        self.assertNotIn(handle, self.peripheral.DeviceConnection._connected)
        with self.assertRaises(self.aioble.DeviceDisconnectedError):
            await operation
        self.assertEqual(connection._timeouts, [])
        await connection.disconnected(timeout_ms=100)

    async def test_central_disconnected_timeout_keeps_cleanup_alive(self):
        await self.assert_disconnect_wait_keeps_cleanup_alive(True, False)

    async def test_peripheral_disconnected_timeout_keeps_cleanup_alive(self):
        await self.assert_disconnect_wait_keeps_cleanup_alive(False, False)

    async def test_central_disconnected_cancel_keeps_cleanup_alive(self):
        await self.assert_disconnect_wait_keeps_cleanup_alive(True, True)

    async def test_peripheral_disconnected_cancel_keeps_cleanup_alive(self):
        await self.assert_disconnect_wait_keeps_cleanup_alive(False, True)

    async def test_central_disconnect_timeout_keeps_cleanup_alive(self):
        await self.assert_disconnect_wait_keeps_cleanup_alive(True, False, True)

    async def test_peripheral_disconnect_timeout_keeps_cleanup_alive(self):
        await self.assert_disconnect_wait_keeps_cleanup_alive(False, False, True)

    async def test_central_disconnect_cancel_keeps_cleanup_alive(self):
        await self.assert_disconnect_wait_keeps_cleanup_alive(True, True, True)

    async def test_peripheral_disconnect_cancel_keeps_cleanup_alive(self):
        await self.assert_disconnect_wait_keeps_cleanup_alive(False, True, True)

    async def test_disconnected_waiters_cancel_independently(self):
        connection, irq = await self.prepare_disconnect_connection(True)
        handle = connection._conn_handle
        first = asyncio.create_task(connection.disconnected(timeout_ms=1))
        second = asyncio.create_task(connection.disconnected())
        third = asyncio.create_task(connection.disconnected())
        await asyncio.sleep(0)
        second.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await second
        with self.assertRaises(asyncio.TimeoutError):
            await first
        self.assertFalse(connection._task.done())
        self.assertFalse(third.done())
        self.assertTrue(connection.is_connected())
        self.assertNotIn(("gap_disconnect", (handle,), {}), self.ble.calls)
        self.ble.emit(irq, (handle, 0, b"abcdef"))
        await third
        self.assertIsNone(connection._conn_handle)
        self.assertIsNone(connection.device._connection)
        self.assertNotIn(handle, self.peripheral.DeviceConnection._connected)

    async def assert_concurrent_connect_rejected_until_delivery(self, phys, success_first):
        self.ble.defer_connect = True
        device = self.aioble.Device(0, b"abcdef")
        task = asyncio.create_task(device.connect(phys=phys))
        await asyncio.sleep(0)
        connection = device._connection
        if success_first:
            self.ble.emit(7, (10, 0, b"abcdef"))
        calls = len(self.ble.calls)
        with self.assertRaises(ValueError):
            await device.connect(phys=phys)
        self.assertEqual(len(self.ble.calls), calls)
        self.assertIs(device._connection, connection)
        self.assertIsNone(connection._task)
        self.assertFalse(task.done())
        if not success_first:
            self.ble.emit(7, (10, 0, b"abcdef"))
        self.assertIs(await task, connection)
        self.assertTrue(connection.is_connected())
        self.assertIsNotNone(connection._task)
        # Once delivered, connect remains idempotent and does not issue commands.
        calls = len(self.ble.calls)
        self.assertIs(await device.connect(phys=phys), connection)
        self.assertEqual(len(self.ble.calls), calls)
        await connection.disconnect(timeout_ms=100)

    async def test_concurrent_legacy_connect_before_irq_is_rejected(self):
        await self.assert_concurrent_connect_rejected_until_delivery(None, False)

    async def test_concurrent_extended_connect_before_irq_is_rejected(self):
        await self.assert_concurrent_connect_rejected_until_delivery(4, False)

    async def test_concurrent_legacy_connect_after_irq_is_rejected(self):
        await self.assert_concurrent_connect_rejected_until_delivery(None, True)

    async def test_concurrent_extended_connect_after_irq_is_rejected(self):
        await self.assert_concurrent_connect_rejected_until_delivery(4, True)

    async def test_concurrent_connect_during_scan_stop_is_rejected(self):
        self.ble.defer_scan_stop = True
        for phys in (None, 4):
            with self.subTest(phys=phys):
                async with self.aioble.scan(0):
                    device = self.aioble.Device(0, b"abcdef")
                    task = asyncio.create_task(device.connect(phys=phys))
                    await asyncio.sleep(0)
                    connection = device._connection
                    self.assertTrue(device._connect_pending)
                    self.assertNotIn(device, self.central._connecting)
                    calls = len(self.ble.calls)
                    with self.assertRaises(ValueError):
                        await device.connect(phys=phys)
                    self.assertEqual(len(self.ble.calls), calls)
                    self.assertIs(device._connection, connection)
                    self.assertIs(await task, connection)
                    self.assertFalse(device._connect_pending)
                    await connection.disconnect()

    async def test_connect_start_error_releases_pending_guard(self):
        for phys in (None, 4):
            with self.subTest(phys=phys):
                device = self.aioble.Device(0, b"abcdef")
                self.ble.fail["gap_connect" if phys is None else "gap_connect_ext"] = OSError(12)
                with self.assertRaises(OSError):
                    await device.connect(phys=phys)
                self.assertFalse(device._connect_pending)
                self.assertIsNone(device._connection)
                self.assertNotIn(device, self.central._connecting)
                connection = await device.connect(phys=phys)
                self.assertTrue(connection.is_connected())
                self.assertFalse(device._connect_pending)
                await connection.disconnect()

    async def test_connect_cancel_releases_pending_guard(self):
        for phys in (None, 4):
            with self.subTest(phys=phys):
                self.ble.defer_connect = True
                device = self.aioble.Device(0, b"abcdef")
                task = asyncio.create_task(device.connect(phys=phys))
                await asyncio.sleep(0)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertFalse(device._connect_pending)
                self.assertIsNone(device._connection)
                await self.settle()
                self.assertNotIn(device, self.central._connecting)
                self.ble.defer_connect = False
                connection = await device.connect(phys=phys)
                self.assertTrue(connection.is_connected())
                await connection.disconnect()

    async def test_connect_timeout_releases_pending_guard(self):
        for phys in (None, 4):
            with self.subTest(phys=phys):
                self.ble.defer_connect = True
                device = self.aioble.Device(0, b"abcdef")
                with self.assertRaises(asyncio.TimeoutError):
                    await device.connect(phys=phys, timeout_ms=1)
                self.assertFalse(device._connect_pending)
                self.assertIsNone(device._connection)
                await self.settle()
                self.assertNotIn(device, self.central._connecting)
                self.ble.defer_connect = False
                connection = await device.connect(phys=phys)
                self.assertTrue(connection.is_connected())
                await connection.disconnect()

    async def assert_cancelled_connect_after_success_is_cleaned(self, phys=None):
        device = self.aioble.Device(0, b"abcdef")
        task = asyncio.create_task(device.connect(phys=phys))
        await asyncio.sleep(0)
        connection = device._connection
        asyncio.get_running_loop().call_soon(task.cancel)
        with self.assertRaises(asyncio.CancelledError):
            await task
        await self.settle()
        self.assertIn(("gap_disconnect", (10,), {}), self.ble.calls)
        self.assertFalse(connection.is_connected())
        self.assertIsNone(device._connection)
        self.assertNotIn(10, self.peripheral.DeviceConnection._connected)
        new_connection = await device.connect(phys=phys)
        self.assertIsNot(new_connection, connection)
        await new_connection.disconnect()

    async def test_unclaimed_cleanup_does_not_disconnect_reused_handle(self):
        release_old_task = asyncio.Event()
        connection_type = self.peripheral.DeviceConnection
        original = connection_type.device_task

        async def delayed_device_task(connection):
            if connection.device.addr == b"aaaaaa":
                await connection._event.wait()
                await release_old_task.wait()
                connection._event.set()
            await original(connection)

        connection_type.device_task = delayed_device_task
        first = asyncio.create_task(self.aioble.advertise(100000, extended=True, instance=1))
        await asyncio.sleep(0)
        self.ble.emit(1, (20, 0, b"aaaaaa"))
        self.ble.emit(42, (1, 0, 20))
        old_connection = self.peripheral._advertisers[1].connection
        self.ble.fail["gap_disconnect"] = OSError(12)
        first.cancel()
        self.assertIsNone(await first)
        await self.settle()
        second = asyncio.create_task(self.aioble.advertise(100000, extended=True, instance=2))
        await asyncio.sleep(0)
        self.ble.emit(2, (20, 0, b"aaaaaa"))
        self.ble.emit(1, (20, 0, b"bbbbbb"))
        self.ble.emit(42, (2, 0, 20))
        new_connection = await second
        # Hold old object cleanup until after the disconnect retry becomes due.
        try:
            await asyncio.sleep(0.3)
            self.assertEqual(self.ble.calls.count(("gap_disconnect", (20,), {})), 1)
            self.assertTrue(new_connection.is_connected())
            self.assertIs(connection_type._connected.get(20), new_connection)
        finally:
            release_old_task.set()
        await self.settle()
        self.assertFalse(old_connection.is_connected())
        self.assertIsNone(old_connection._conn_handle)
        # The worker drops its record on the next retry after device_task ends.
        await asyncio.sleep(0.3)
        self.assertNotIn(old_connection, self.peripheral._unclaimed_connections)
        self.assertEqual(await new_connection.set_phy(2, 2), (2, 2))
        await new_connection.disconnect()

    async def test_cancelled_legacy_connect_after_success_is_cleaned(self):
        await self.assert_cancelled_connect_after_success_is_cleaned()

    async def test_cancelled_extended_connect_after_success_is_cleaned(self):
        await self.assert_cancelled_connect_after_success_is_cleaned(phys=4)

    async def test_cancelled_connect_disconnect_errors_are_retried(self):
        for phys in (None, 4):
            with self.subTest(phys=phys):
                device = self.aioble.Device(0, b"abcdef")
                task = asyncio.create_task(device.connect(phys=phys))
                await asyncio.sleep(0)
                connection = device._connection
                self.ble.fail["gap_disconnect"] = [OSError(12)] * 2
                asyncio.get_running_loop().call_soon(task.cancel)
                with self.assertRaises(asyncio.CancelledError):
                    await task
                await self.settle()
                self.assertIs(self.peripheral.DeviceConnection._connected.get(10), connection)
                self.assertIsNone(device._connection)
                with self.assertRaises(ValueError):
                    await device.connect(phys=phys)
                self.assertIsNone(device._connection)
                self.assertIs(self.central._connecting.get(device), connection)
                await asyncio.sleep(0.55)
                self.assertFalse(connection.is_connected())
                self.assertNotIn(10, self.peripheral.DeviceConnection._connected)
                self.assertNotIn(device, self.central._connecting)

    async def test_cancel_failed_then_late_success_is_cleaned(self):
        self.ble.defer_connect = True
        self.ble.complete_connect_on_cancel = False
        for phys in (None, 4):
            with self.subTest(phys=phys):
                device = self.aioble.Device(0, b"abcdef")
                task = asyncio.create_task(device.connect(phys=phys))
                await asyncio.sleep(0)
                connection = device._connection
                self.ble.fail["gap_connect"] = OSError(12)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                await self.settle()
                self.assertIs(self.central._connecting.get(device), connection)
                self.assertIsNone(device._connection)
                with self.assertRaises(ValueError):
                    await device.connect(phys=phys)
                self.ble.emit(7, (10, 0, b"abcdef"))
                await self.settle()
                self.assertFalse(connection.is_connected())
                self.assertIsNone(connection._cleanup_task)
                self.assertNotIn(10, self.peripheral.DeviceConnection._connected)
                self.assertNotIn(device, self.central._connecting)
        self.assertEqual(self.ble.calls.count(("gap_disconnect", (10,), {})), 2)

    async def test_cancel_completion_without_address_preserves_other_connection(self):
        active = await self.aioble.Device(0, b"aaaaaa").connect()
        self.ble.defer_connect = True
        self.ble.complete_connect_on_cancel = False
        device = self.aioble.Device(0, b"bbbbbb")
        task = asyncio.create_task(device.connect(phys=4))
        await asyncio.sleep(0)
        connection = device._connection
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await self.settle()
        # A failed initiation's handle and address have no connection identity.
        self.ble.emit(8, (10, 0xFF, b"\0" * 6))
        await self.settle()
        self.assertIsNone(connection._cleanup_task)
        self.assertNotIn(device, self.central._connecting)
        self.assertTrue(active.is_connected())
        self.assertIs(self.peripheral.DeviceConnection._connected.get(10), active)
        await active.disconnect()

    async def test_cancel_connect_errors_retry_until_cancel_completion(self):
        self.ble.defer_connect = True
        device = self.aioble.Device(0, b"abcdef")
        task = asyncio.create_task(device.connect())
        await asyncio.sleep(0)
        connection = device._connection
        self.ble.fail["gap_connect"] = [OSError(12)] * 2
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.55)
        self.assertIsNone(connection._cleanup_task)
        self.assertNotIn(device, self.central._connecting)
        self.assertIsNone(device._connection)
        self.assertEqual(self.ble.calls.count(("gap_connect", (None,), {})), 3)

    async def test_connect_timeout_then_late_success_is_cleaned(self):
        self.ble.defer_connect = True
        self.ble.complete_connect_on_cancel = False
        device = self.aioble.Device(0, b"abcdef")
        task = asyncio.create_task(device.connect(phys=4, timeout_ms=1))
        await asyncio.sleep(0)
        connection = device._connection
        with self.assertRaises(asyncio.TimeoutError):
            await task
        self.ble.emit(7, (10, 0, b"abcdef"))
        await self.settle()
        self.assertFalse(connection.is_connected())
        self.assertIsNone(connection._cleanup_task)
        self.assertIsNone(device._connection)
        self.assertNotIn(device, self.central._connecting)
        self.assertNotIn(10, self.peripheral.DeviceConnection._connected)

    async def test_success_then_disconnect_before_connect_resumes_is_cleaned(self):
        self.ble.defer_connect = True
        device = self.aioble.Device(0, b"abcdef")
        task = asyncio.create_task(device.connect(phys=4))
        await asyncio.sleep(0)
        connection = device._connection
        self.ble.emit(7, (10, 0, b"abcdef"))
        self.ble.emit(8, (10, 0, b"abcdef"))
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await self.settle()
        self.assertFalse(connection.is_connected())
        self.assertIsNone(device._connection)
        self.assertNotIn(10, self.peripheral.DeviceConnection._connected)

    async def test_stop_during_central_cleanup_preserves_restarted_connection(self):
        device = self.aioble.Device(0, b"abcdef")
        task = asyncio.create_task(device.connect(phys=4))
        await asyncio.sleep(0)
        old_connection = device._connection
        self.ble.fail["gap_disconnect"] = OSError(12)
        asyncio.get_running_loop().call_soon(task.cancel)
        with self.assertRaises(asyncio.CancelledError):
            await task
        await self.settle()
        self.aioble.stop()
        new_connection = await self.aioble.Device(0, b"bbbbbb").connect(phys=4)
        disconnects = self.ble.calls.count(("gap_disconnect", (10,), {}))
        await asyncio.sleep(0.3)
        self.assertFalse(old_connection.is_connected())
        self.assertIsNone(old_connection._cleanup_task)
        self.assertTrue(new_connection.is_connected())
        self.assertIs(self.peripheral.DeviceConnection._connected.get(10), new_connection)
        self.assertEqual(self.ble.calls.count(("gap_disconnect", (10,), {})), disconnects)
        await new_connection.disconnect()

    async def test_stop_after_connect_success_prevents_delivery(self):
        self.ble.defer_connect = True
        device = self.aioble.Device(0, b"abcdef")
        task = asyncio.create_task(device.connect(phys=4))
        await asyncio.sleep(0)
        connection = device._connection
        self.ble.emit(7, (10, 0, b"abcdef"))
        self.aioble.stop()
        with self.assertRaises(OSError):
            await task
        await self.settle()
        self.assertFalse(connection.is_connected())
        self.assertIsNone(device._connection)
        self.assertNotIn(10, self.peripheral.DeviceConnection._connected)
        self.assertNotIn(device, self.central._connecting)

    async def test_multiple_extended_advertisers_route_by_instance(self):
        first = asyncio.create_task(self.aioble.advertise(100000, extended=True, instance=1))
        second = asyncio.create_task(self.aioble.advertise(100000, extended=True, instance=2))
        await asyncio.sleep(0)
        self.ble.emit(1, (20, 0, b"aaaaaa"))
        self.ble.emit(1, (21, 0, b"bbbbbb"))
        self.ble.emit(42, (2, 0, 20))
        self.ble.emit(42, (1, 0, 21))
        a, b = await asyncio.gather(first, second)
        self.assertEqual((a.device.addr, b.device.addr), (b"bbbbbb", b"aaaaaa"))
        await a.disconnect()
        await b.disconnect()

    async def test_advertising_completion_before_connection(self):
        task = asyncio.create_task(self.aioble.advertise(100000, extended=True))
        await asyncio.sleep(0)
        self.ble.emit(42, (1, 0, 15))
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        self.ble.emit(1, (15, 0, b"abcdef"))
        connection = await task
        await connection.disconnect()

    async def test_legacy_advertising_and_overlap_restriction(self):
        task = asyncio.create_task(self.aioble.advertise(100000, name=b"MPY"))
        await asyncio.sleep(0)
        self.assertEqual(self.ble.calls[-1][0], "gap_advertise")
        with self.assertRaises(ValueError):
            await self.aioble.advertise(100000, extended=True)
        self.ble.emit(1, (16, 0, b"abcdef"))
        connection = await task
        self.assertEqual(self.ble.calls[-1][0], "gap_advertise")
        await connection.disconnect()

    async def test_advertising_start_failure_removes_partial_instance(self):
        self.ble.fail["gap_advertise_ext"] = OSError(22)
        with self.assertRaises(OSError):
            await self.aioble.advertise(100000, extended=True)
        self.assertEqual(self.ble.calls[-1][0], "gap_advertise_ext_stop")
        self.assertEqual(self.peripheral._advertisers, {})

    async def test_legacy_broadcaster_does_not_take_extended_connection(self):
        legacy = asyncio.create_task(self.aioble.advertise(100000, connectable=False))
        extended = asyncio.create_task(self.aioble.advertise(100000, extended=True))
        await asyncio.sleep(0)
        self.ble.emit(1, (18, 0, b"abcdef"))
        self.ble.emit(42, (1, 0, 18))
        connection = await extended
        self.assertFalse(legacy.done())
        legacy.cancel()
        await legacy
        await connection.disconnect()

    async def test_extended_payload_field_and_connectable_limits(self):
        with self.assertRaises(ValueError):
            await self.aioble.advertise(100000, extended=True, name=b"x" * 255)
        with self.assertRaises(ValueError):
            await self.aioble.advertise(100000, extended=True, name=b"x" * 250)
        task = asyncio.create_task(
            self.aioble.advertise(100000, extended=True, connectable=False, name=b"x" * 250)
        )
        await asyncio.sleep(0)
        self.assertGreater(len(self.ble.calls[-1][2]["adv_data"]), 251)
        task.cancel()
        await task

    async def test_decode_field_does_not_parse_padding_or_short_field(self):
        result = self.central.ScanResult(self.aioble.Device(0, b"abcdef"))
        result.adv_data = b"\x09\x09MPY"
        self.assertIsNone(result.name())
        result.adv_data = b"\0\x04\x09MPY"
        self.assertIsNone(result.name())

    async def test_advertising_timeout_cancellation_and_mode_validation(self):
        with self.assertRaises(asyncio.TimeoutError):
            await self.aioble.advertise(100000, extended=True, timeout_ms=1)
        self.assertEqual(self.peripheral._advertisers, {})
        task = asyncio.create_task(
            self.aioble.advertise(
                100000, extended=True, connectable=False, scannable=True, name=b"N" * 50
            )
        )
        await asyncio.sleep(0)
        _, _, kwargs = self.ble.calls[-1]
        self.assertEqual(kwargs["adv_data"], b"")
        self.assertGreater(len(kwargs["resp_data"]), 31)
        task.cancel()
        await task
        self.assertEqual(self.peripheral._advertisers, {})
        with self.assertRaises(ValueError):
            await self.aioble.advertise(100000, extended=True, scannable=True)

    async def test_cancelled_advertise_disconnects_unclaimed_connection(self):
        task = asyncio.create_task(self.aioble.advertise(100000, extended=True))
        await asyncio.sleep(0)
        self.ble.emit(1, (12, 0, b"abcdef"))
        self.ble.emit(42, (1, 0, 12))
        task.cancel()
        self.assertIsNone(await task)
        await self.settle()
        self.assertIn(("gap_disconnect", (12,), {}), self.ble.calls)
        self.assertNotIn(12, self.peripheral.DeviceConnection._connected)

    async def test_cancelled_advertise_between_connect_and_completion(self):
        task = asyncio.create_task(self.aioble.advertise(100000, extended=True))
        await asyncio.sleep(0)
        self.ble.emit(1, (12, 0, b"abcdef"))
        task.cancel()
        self.assertIsNone(await task)
        self.assertIn(1, self.peripheral._advertisers)
        self.assertIn(("gap_advertise_ext_stop", (1,), {"remove": False}), self.ble.calls)
        # IRQ 42 still belongs to the cancelled invocation, never a new one.
        with self.assertRaises(ValueError):
            await self.aioble.advertise(100000, extended=True)
        self.ble.emit(42, (1, 0, 12))
        await self.settle()
        self.assertIn(("gap_disconnect", (12,), {}), self.ble.calls)
        self.assertNotIn(12, self.peripheral.DeviceConnection._connected)
        self.assertEqual(self.peripheral._advertisers, {})
        self.assertEqual(self.peripheral._pending_connections, {})

    async def test_cancelled_advertise_between_completion_and_connect(self):
        task = asyncio.create_task(self.aioble.advertise(100000, extended=True))
        await asyncio.sleep(0)
        self.ble.emit(42, (1, 0, 13))
        task.cancel()
        self.assertIsNone(await task)
        self.assertIn(1, self.peripheral._advertisers)
        self.ble.emit(1, (13, 0, b"abcdef"))
        await self.settle()
        self.assertIn(("gap_disconnect", (13,), {}), self.ble.calls)
        self.assertNotIn(13, self.peripheral.DeviceConnection._connected)
        self.assertEqual(self.peripheral._advertisers, {})

    async def test_cancelled_instance_does_not_disconnect_other_advertiser(self):
        cancelled = asyncio.create_task(self.aioble.advertise(100000, extended=True, instance=1))
        active = asyncio.create_task(self.aioble.advertise(100000, extended=True, instance=2))
        await asyncio.sleep(0)
        self.ble.emit(1, (20, 0, b"aaaaaa"))
        self.ble.emit(1, (21, 0, b"bbbbbb"))
        cancelled.cancel()
        self.assertIsNone(await cancelled)
        await self.settle()
        self.assertFalse(any(name == "gap_disconnect" for name, _, _ in self.ble.calls))
        self.ble.emit(42, (2, 0, 20))
        connection = await active
        self.ble.emit(42, (1, 0, 21))
        await self.settle()
        self.assertIn(("gap_disconnect", (21,), {}), self.ble.calls)
        self.assertNotIn(("gap_disconnect", (20,), {}), self.ble.calls)
        self.assertTrue(connection.is_connected())
        self.assertEqual(self.peripheral._advertisers, {})
        await connection.disconnect()

    async def prepare_abandoned_advertiser_with_released_handle(self):
        old_task = asyncio.create_task(self.aioble.advertise(100000, extended=True, instance=1))
        await asyncio.sleep(0)
        advertiser = self.peripheral._advertisers[1]
        self.ble.fail_by_instance["gap_advertise_ext_stop"] = {1: [OSError(12)] * 100}
        self.ble.emit(1, (20, 0, b"aaaaaa"))
        self.ble.emit(42, (1, 0, 20))
        with self.assertRaises(OSError):
            await old_task
        old_connection = advertiser.connection
        await self.settle()
        self.assertFalse(old_connection.is_connected())
        self.assertNotIn(20, self.peripheral.DeviceConnection._connected)
        self.assertIs(self.peripheral._advertisers[1], advertiser)
        self.assertTrue(advertiser.abandoned)
        return advertiser, old_connection

    async def assert_reused_handle_keeps_new_instance_connection(self, connect_first):
        (
            old_advertiser,
            old_connection,
        ) = await self.prepare_abandoned_advertiser_with_released_handle()
        new_task = asyncio.create_task(self.aioble.advertise(100000, extended=True, instance=2))
        await asyncio.sleep(0)
        new_advertiser = self.peripheral._advertisers[2]
        if connect_first:
            self.ble.emit(1, (20, 0, b"bbbbbb"))
            await self.settle()
            self.assertIs(old_advertiser.connection, old_connection)
            self.ble.emit(42, (2, 0, 20))
        else:
            self.ble.emit(42, (2, 0, 20))
            await asyncio.sleep(0)
            self.ble.emit(1, (20, 0, b"bbbbbb"))
        new_connection = await new_task
        await self.settle()
        self.assertEqual(new_connection.device.addr, b"bbbbbb")
        self.assertTrue(new_connection.is_connected())
        self.assertIs(new_advertiser.connection, new_connection)
        self.assertIs(old_advertiser.connection, old_connection)
        self.assertIs(self.peripheral.DeviceConnection._connected[20], new_connection)
        self.assertEqual(
            len([call for call in self.ble.calls if call == ("gap_disconnect", (20,), {})]), 1
        )
        self.ble.fail_by_instance["gap_advertise_ext_stop"].pop(1)
        await asyncio.sleep(0.3)
        self.assertNotIn(1, self.peripheral._advertisers)
        self.assertTrue(new_connection.is_connected())
        self.assertIs(self.peripheral.DeviceConnection._connected[20], new_connection)
        await new_connection.disconnect()

    async def test_reused_handle_does_not_match_old_instance_connect_first(self):
        await self.assert_reused_handle_keeps_new_instance_connection(connect_first=True)

    async def test_reused_handle_does_not_match_old_instance_completion_first(self):
        await self.assert_reused_handle_keeps_new_instance_connection(connect_first=False)

    async def test_advertise_exit_failure_disconnects_unreturned_connection(self):
        task = asyncio.create_task(self.aioble.advertise(100000, extended=True))
        await asyncio.sleep(0)
        self.ble.fail["gap_advertise_ext_stop"] = OSError(12)
        self.ble.emit(1, (13, 0, b"abcdef"))
        self.ble.emit(42, (1, 0, 13))
        with self.assertRaises(OSError) as error:
            await task
        self.assertEqual(error.exception.args, (12,))
        await self.settle()
        self.assertIn(("gap_disconnect", (13,), {}), self.ble.calls)
        self.assertNotIn(13, self.peripheral.DeviceConnection._connected)
        self.assertEqual(self.peripheral._advertisers, {})

    async def test_unclaimed_disconnect_failure_is_retried(self):
        task = asyncio.create_task(self.aioble.advertise(100000, extended=True))
        await asyncio.sleep(0)
        self.ble.emit(1, (14, 0, b"abcdef"))
        self.ble.emit(42, (1, 0, 14))
        self.ble.fail["gap_disconnect"] = OSError(12)
        task.cancel()
        self.assertIsNone(await task)
        await self.settle()
        self.assertIn(14, self.peripheral.DeviceConnection._connected)
        await asyncio.sleep(0.3)
        self.assertNotIn(14, self.peripheral.DeviceConnection._connected)
        self.assertEqual(
            len([call for call in self.ble.calls if call == ("gap_disconnect", (14,), {})]), 2
        )

    async def test_peer_disconnect_before_completion_cleans_pending_connection(self):
        task = asyncio.create_task(self.aioble.advertise(100000, extended=True))
        await asyncio.sleep(0)
        self.ble.emit(1, (15, 0, b"abcdef"))
        self.ble.emit(2, (15, 0, b"abcdef"))
        await self.settle()
        self.assertNotIn(15, self.peripheral.DeviceConnection._connected)
        self.ble.emit(42, (1, 0, 15))
        with self.assertRaises(OSError):
            await task
        await self.settle()
        self.assertEqual(self.peripheral._advertisers, {})

    async def test_stop_clears_advertising_cleanup_before_restart(self):
        task = asyncio.create_task(self.aioble.advertise(100000, extended=True))
        await asyncio.sleep(0)
        self.ble.emit(42, (1, 0, 16))
        task.cancel()
        await task
        old_cleanup = self.peripheral._cleanup_task
        self.aioble.stop()
        await self.settle()
        self.assertTrue(old_cleanup.done())
        self.assertEqual(self.peripheral._advertisers, {})
        self.assertEqual(self.peripheral._pending_connections, {})
        self.assertEqual(self.peripheral._unclaimed_connections, {})
        new_task = asyncio.create_task(self.aioble.advertise(100000, extended=True))
        await asyncio.sleep(0)
        self.ble.emit(1, (17, 0, b"abcdef"))
        self.ble.emit(42, (1, 0, 17))
        connection = await new_task
        self.assertTrue(connection.is_connected())
        await connection.disconnect()

    async def test_advertise_timeout_between_completion_and_connect(self):
        task = asyncio.create_task(self.aioble.advertise(100000, extended=True, timeout_ms=10))
        await asyncio.sleep(0)
        self.ble.emit(42, (1, 0, 18))
        with self.assertRaises(asyncio.TimeoutError):
            await task
        self.assertIn(1, self.peripheral._advertisers)
        self.ble.emit(1, (18, 0, b"abcdef"))
        await self.settle()
        self.assertNotIn(18, self.peripheral.DeviceConnection._connected)
        self.assertEqual(self.peripheral._advertisers, {})

    async def test_failed_removal_retains_instance_and_retries(self):
        task = asyncio.create_task(self.aioble.advertise(100000, extended=True))
        await asyncio.sleep(0)
        self.ble.fail["gap_advertise_ext_stop"] = [OSError(12)] * 8
        self.ble.emit(1, (19, 0, b"abcdef"))
        self.ble.emit(42, (1, 0, 19))
        with self.assertRaises(OSError):
            await task
        # The worker's attempts also fail. Its next retry must own the
        # instance even after the unreturned connection has disconnected.
        await self.settle()
        self.assertIn(1, self.peripheral._advertisers)
        self.assertNotIn(19, self.peripheral.DeviceConnection._connected)
        with self.assertRaises(ValueError):
            await self.aioble.advertise(100000, extended=True)
        self.ble.fail.pop("gap_advertise_ext_stop", None)
        await asyncio.sleep(0.3)
        self.assertEqual(self.peripheral._advertisers, {})

    async def test_legacy_exit_failure_also_disconnects_unreturned_connection(self):
        task = asyncio.create_task(self.aioble.advertise(100000))
        await asyncio.sleep(0)
        self.ble.fail["gap_advertise"] = OSError(12)
        self.ble.emit(1, (22, 0, b"abcdef"))
        with self.assertRaises(OSError):
            await task
        await self.settle()
        self.assertNotIn(22, self.peripheral.DeviceConnection._connected)
        self.assertIn(("gap_disconnect", (22,), {}), self.ble.calls)
        self.assertEqual(self.peripheral._advertisers, {})

    async def test_stop_after_both_irqs_prevents_returning_closed_connection(self):
        task = asyncio.create_task(self.aioble.advertise(100000, extended=True))
        await asyncio.sleep(0)
        self.ble.emit(1, (23, 0, b"abcdef"))
        self.ble.emit(42, (1, 0, 23))
        self.aioble.stop()
        with self.assertRaises(OSError):
            await task
        await self.settle()
        self.assertNotIn(23, self.peripheral.DeviceConnection._connected)
        self.assertEqual(self.peripheral._unclaimed_connections, {})

    async def test_periodic_advertising_context_and_rollback(self):
        async with self.aioble.periodic_advertise(100000, b"data", sid=2):
            self.assertEqual(
                self.ble.calls[-1], ("gap_periodic_advertise", (100000, b"data"), {"instance": 1})
            )
            with self.assertRaises(ValueError):
                async with self.aioble.periodic_advertise(100000):
                    pass
        self.assertEqual(self.ble.calls[-1][0], "gap_advertise_ext_stop")
        self.ble.fail["gap_periodic_advertise"] = OSError(22)
        with self.assertRaises(OSError):
            async with self.aioble.periodic_advertise(100000):
                pass
        self.assertEqual(self.peripheral._advertisers, {})
        self.assertEqual(self.ble.calls[-1][0], "gap_advertise_ext_stop")

    async def test_invalid_extended_instance_leaves_no_cleanup_work(self):
        for instance, exception in (
            (0, ValueError),
            (-1, ValueError),
            (3, ValueError),
            (99, ValueError),
            (1.5, TypeError),
            ("1", TypeError),
            (None, TypeError),
            ([], TypeError),
        ):
            with self.subTest(instance=instance):
                with self.assertRaises(exception):
                    await self.aioble.advertise(100000, extended=True, instance=instance)
                await self.settle()
                self.assertEqual(self.ble.calls, [])
                self.assertEqual(self.peripheral._advertisers, {})
                self.assertIsNone(self.peripheral._cleanup_task)
        # Rejected input must not disable a subsequent legitimate cleanup.
        task = asyncio.create_task(self.aioble.advertise(100000, extended=True, instance=2))
        await asyncio.sleep(0)
        self.ble.emit(1, (50, 0, b"abcdef"))
        self.ble.emit(42, (2, 0, 50))
        task.cancel()
        self.assertIsNone(await task)
        await self.settle()
        self.assertIn(("gap_disconnect", (50,), {}), self.ble.calls)
        self.assertNotIn(50, self.peripheral.DeviceConnection._connected)
        self.assertEqual(self.peripheral._advertisers, {})

    async def test_invalid_periodic_instance_leaves_no_cleanup_work(self):
        for instance, exception in (
            (0, ValueError),
            (3, ValueError),
            (99, ValueError),
            (1.5, TypeError),
        ):
            with self.subTest(instance=instance):
                context = self.aioble.periodic_advertise(100000, instance=instance)
                with self.assertRaises(exception):
                    await context.__aenter__()
                self.assertIsNone(context._advertiser)
                await context.__aexit__(None, None, None)
                self.assertEqual(self.ble.calls, [])
                self.assertEqual(self.peripheral._advertisers, {})
                self.assertIsNone(self.peripheral._cleanup_task)
        async with self.aioble.periodic_advertise(100000, instance=2):
            self.assertEqual(self.ble.periodic_instances, {2})
        self.assertEqual(self.ble.extended_instances, set())
        self.assertEqual(self.ble.periodic_instances, set())

    async def test_cleanup_exception_can_recover_retained_instance(self):
        context = await self.aioble.periodic_advertise(100000).__aenter__()
        worker = self.peripheral._cleanup_task
        # The first command fails transiently; a later unexpected exception
        # must not leave a terminated worker registered as a running one.
        self.ble.fail["gap_advertise_ext_stop"] = [OSError(12), RuntimeError("worker failed")]
        with self.assertRaises(OSError):
            await context.__aexit__(None, None, None)
        with self.assertRaisesRegex(RuntimeError, "worker failed"):
            await worker
        self.assertIsNone(self.peripheral._cleanup_task)
        self.assertIn(1, self.peripheral._advertisers)
        async with self.aioble.periodic_advertise(100000, instance=2):
            await self.settle()
            self.assertNotIn(1, self.peripheral._advertisers)
            self.assertEqual(self.ble.periodic_instances, {2})
        self.assertEqual(self.peripheral._advertisers, {})

    async def test_old_cleanup_finally_cannot_clear_restarted_worker(self):
        async with self.aioble.periodic_advertise(100000):
            await self.settle()
        old_worker = self.peripheral._cleanup_task
        self.aioble.stop()
        # Recreate before cancellation has resumed the old worker's finally.
        async with self.aioble.periodic_advertise(100000):
            new_worker = self.peripheral._cleanup_task
            await self.settle()
            self.assertTrue(old_worker.done())
            self.assertIs(self.peripheral._cleanup_task, new_worker)
            self.assertFalse(new_worker.done())

    async def test_periodic_exit_failure_keeps_owner_and_retries(self):
        context = await self.aioble.periodic_advertise(100000).__aenter__()
        self.ble.fail["gap_advertise_ext_stop"] = OSError(12)
        with self.assertRaises(OSError) as error:
            await context.__aexit__(None, None, None)
        self.assertEqual(error.exception.args, (12,))
        self.assertIs(self.peripheral._advertisers[1], context._advertiser)
        self.assertEqual(self.ble.periodic_instances, {1})
        with self.assertRaises(ValueError):
            await self.aioble.periodic_advertise(100000).__aenter__()
        await self.settle()
        self.assertEqual(self.peripheral._advertisers, {})
        self.assertEqual(self.ble.extended_instances, set())
        self.assertEqual(self.ble.periodic_instances, set())

    async def test_periodic_failed_start_and_rollback_keeps_original_error(self):
        self.ble.fail["gap_periodic_advertise"] = OSError(22)
        self.ble.fail["gap_advertise_ext_stop"] = OSError(12)
        context = self.aioble.periodic_advertise(100000)
        with self.assertRaises(OSError) as error:
            await context.__aenter__()
        self.assertEqual(error.exception.args, (22,))
        self.assertIs(self.peripheral._advertisers[1], context._advertiser)
        self.assertEqual(self.ble.extended_instances, {1})
        await self.settle()
        self.assertEqual(self.peripheral._advertisers, {})
        self.assertEqual(self.ble.extended_instances, set())

    async def test_periodic_failed_extended_start_and_rollback_retries(self):
        self.ble.fail["gap_advertise_ext"] = OSError(22)
        self.ble.fail["gap_advertise_ext_stop"] = OSError(12)
        with self.assertRaises(OSError) as error:
            await self.aioble.periodic_advertise(100000).__aenter__()
        self.assertEqual(error.exception.args, (22,))
        self.assertIn(1, self.peripheral._advertisers)
        await self.settle()
        self.assertEqual(self.peripheral._advertisers, {})

    async def test_periodic_persistent_stop_failure_reserves_instance(self):
        context = await self.aioble.periodic_advertise(100000).__aenter__()
        self.ble.fail["gap_advertise_ext_stop"] = [OSError(12)] * 8
        with self.assertRaises(OSError):
            await context.__aexit__(None, None, None)
        await asyncio.sleep(0.3)
        self.assertIs(self.peripheral._advertisers[1], context._advertiser)
        self.assertEqual(self.ble.periodic_instances, {1})
        with self.assertRaises(ValueError):
            await self.aioble.advertise(100000, extended=True)
        self.ble.fail.pop("gap_advertise_ext_stop", None)
        await asyncio.sleep(0.3)
        self.assertEqual(self.peripheral._advertisers, {})
        self.assertEqual(self.ble.periodic_instances, set())

    async def test_periodic_exit_can_retry_before_background_cleanup(self):
        context = await self.aioble.periodic_advertise(100000).__aenter__()
        self.ble.fail["gap_advertise_ext_stop"] = OSError(12)
        with self.assertRaises(OSError):
            await context.__aexit__(None, None, None)
        await context.__aexit__(None, None, None)
        calls = len(self.ble.calls)
        await self.settle()
        self.assertEqual(len(self.ble.calls), calls)
        self.assertEqual(self.peripheral._advertisers, {})

    async def test_periodic_cleanup_stop_and_restart_preserves_new_owner(self):
        context = await self.aioble.periodic_advertise(100000).__aenter__()
        self.ble.fail["gap_advertise_ext_stop"] = OSError(12)
        with self.assertRaises(OSError):
            await context.__aexit__(None, None, None)
        self.aioble.stop()
        async with self.aioble.periodic_advertise(100000) as new_context:
            calls = len(self.ble.calls)
            await self.settle()
            await context.__aexit__(None, None, None)
            self.assertEqual(len(self.ble.calls), calls)
            self.assertIs(self.peripheral._advertisers[1], new_context._advertiser)
            self.assertEqual(self.ble.periodic_instances, {1})
        self.assertEqual(self.peripheral._advertisers, {})

    async def test_unentered_periodic_context_cannot_stop_another_owner(self):
        context = self.aioble.periodic_advertise(100000)
        await context.__aexit__(None, None, None)
        self.assertEqual(self.ble.calls, [])
        async with self.aioble.periodic_advertise(100000) as owner:
            calls = len(self.ble.calls)
            await context.__aexit__(None, None, None)
            self.assertEqual(len(self.ble.calls), calls)
            self.assertIs(self.peripheral._advertisers[1], owner._advertiser)

    async def create_sync(self, handle=30):
        task = asyncio.create_task(
            self.aioble.periodic_sync(self.aioble.Device(0, b"abcdef"), sid=2)
        )
        await asyncio.sleep(0)
        self.ble.emit(43, (0, handle, 2, 80, 2, 0, memoryview(b"abcdef")))
        return await task

    async def test_periodic_sync_reports_loss_and_context_exit(self):
        sync = await self.create_sync()
        self.assertTrue(sync.is_synced())
        self.ble.emit(44, (30, 1, -40, 3, memoryview(b"first")))
        data = bytearray(b"last")
        self.ble.emit(44, (30, 0, -41, 4, memoryview(data)))
        data[:] = b"xxxx"
        report = await sync.__anext__()
        self.assertEqual((report.data_status, report.adv_data), (0, b"firstlast"))
        self.ble.emit(44, (30, 2, -41, 4, b"bad"))
        self.assertIsNone((await sync.__anext__()).adv_data)
        self.ble.emit(45, (30, 8))
        with self.assertRaises(self.aioble.PeriodicSyncLostError) as error:
            await sync.__anext__()
        self.assertEqual(error.exception.reason, 8)
        sync = await self.create_sync(31)
        async with sync:
            pass
        self.assertFalse(sync.is_synced())
        self.assertEqual(self.periodic._syncs, {})

    async def test_periodic_sync_timeout_releases_pending_slot(self):
        with self.assertRaises(asyncio.TimeoutError):
            await self.aioble.periodic_sync(self.aioble.Device(0, b"abcdef"), sid=1, timeout_ms=1)
        for _ in range(4):
            await asyncio.sleep(0)
        self.assertIsNone(self.periodic._pending_sync)
        sync = await self.create_sync()
        await sync.close()

    async def test_cancelled_sync_that_completes_successfully_is_terminated(self):
        self.ble.complete_sync_on_cancel = False
        task = asyncio.create_task(
            self.aioble.periodic_sync(self.aioble.Device(0, b"abcdef"), sid=1)
        )
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        with self.assertRaises(ValueError):
            await self.aioble.periodic_sync(self.aioble.Device(0, b"abcdef"), sid=1)
        sync = self.periodic._pending_sync
        self.ble.fail["gap_periodic_sync_stop"] = OSError(12)
        self.ble.emit(43, (0, 40, 1, 80, 1, 0, b"abcdef"))
        self.ble.emit(44, (40, 0, -40, 0, b"orphan report"))
        await asyncio.sleep(0)
        self.assertIs(self.periodic._pending_sync, sync)
        self.assertIsNotNone(sync._cleanup_task)
        self.assertEqual(sync._queue, [])
        await asyncio.sleep(0.3)
        self.assertIsNone(self.periodic._pending_sync)
        self.assertEqual(self.periodic._syncs, {})
        self.assertIn(("gap_periodic_sync_stop", (40,), {}), self.ble.calls)
        self.assertEqual(
            len([call for call in self.ble.calls if call[0] == "gap_periodic_sync_stop"]), 2
        )

    async def test_stop_wakes_scanner_and_periodic_waiters(self):
        scanner = await self.aioble.scan(0, extended=True).__aenter__()
        sync = await self.create_sync()
        scan_waiter = asyncio.create_task(scanner.__anext__())
        report_waiter = asyncio.create_task(sync.__anext__())
        await asyncio.sleep(0)
        self.aioble.stop()
        with self.assertRaises(StopAsyncIteration):
            await scan_waiter
        with self.assertRaises(self.aioble.PeriodicSyncLostError):
            await report_waiter
        self.assertIsNone(self.central._active_scanner)
        self.assertEqual(self.periodic._syncs, {})

    async def test_periodic_sync_from_scan_result_and_failed_create(self):
        result = self.central.ScanResult(self.aioble.Device(0, b"abcdef"), sid=3)
        self.ble.fail["gap_periodic_sync"] = OSError(22)
        with self.assertRaises(OSError):
            await self.aioble.periodic_sync(result)
        self.assertIsNone(self.periodic._pending_sync)
        task = asyncio.create_task(self.aioble.periodic_sync(result))
        await asyncio.sleep(0)
        self.assertEqual(self.ble.calls[-1][2]["sid"], 3)
        self.ble.emit(43, (2, 0, 0, 0, 0, 0, b"\0" * 6))
        with self.assertRaises(OSError):
            await task
        self.assertIsNone(self.periodic._pending_sync)

    async def test_phy_timeout_keeps_update_reserved_until_irq(self):
        connection = await self.aioble.Device(0, b"abcdef").connect()
        original = self.ble.emit
        self.ble.emit = lambda event, data: None if event == 40 else original(event, data)
        with self.assertRaises(asyncio.TimeoutError):
            await connection.set_phy(3, 3, timeout_ms=1)
        with self.assertRaises(ValueError):
            await connection.set_phy(3, 3)
        original(40, (10, 0, 2, 2))
        self.ble.emit = original
        self.assertEqual(await connection.set_phy(3, 3), (2, 2))
        await connection.disconnect()


if __name__ == "__main__":
    asyncio.ThreadSafeFlag = ThreadSafeFlag
    asyncio.sleep_ms = sleep_ms
    asyncio.wait_for_ms = wait_for_ms
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    unittest.main()
