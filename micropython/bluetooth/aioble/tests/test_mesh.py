"""Async Mesh adapter regression tests. No hardware/native firmware required."""
import asyncio
import importlib
import sys
import types
import unittest
from pathlib import Path

import test_ble5

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
asyncio.ThreadSafeFlag = test_ble5.ThreadSafeFlag
asyncio.wait_for_ms = test_ble5.wait_for_ms


class FakeMesh(types.ModuleType):
    def __init__(self):
        super().__init__("blemesh")
        self.state = 0
        self.handler = None
        self.queue = []
        self.dropped = 0
        self.calls = []
        self.fail_start = False
        self.fail_stop = False

    def irq(self, handler):
        self.handler = handler

    def start(self, uuid, **kwargs):
        self.calls.append(("start", uuid, kwargs))
        if self.fail_start:
            self.state = 4
            raise OSError(5)
        self.state = 2
        self.queue.clear()
        self.dropped = 0

    def stop(self):
        self.calls.append(("stop",))
        if self.fail_stop:
            raise OSError(5)
        self.state = 0
        if self.handler:
            self.handler(None)

    def status(self):
        return {"state": self.state, "dropped": self.dropped}

    def recv(self):
        return self.queue.pop(0) if self.queue else None

    def send(self, *args, **kwargs):
        self.calls.append(("send", args, kwargs))

    def emit(self, value):
        self.queue.append(value)
        if self.handler:
            self.handler(None)


class MeshTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        test_ble5.BLE5Tests.load_aioble(self)
        features = self.ble.ble5_features()
        features["advertising_instances"] = 4  # One of five belongs to Mesh.
        self.ble.ble5_features = lambda: features
        self.native = FakeMesh()
        sys.modules["blemesh"] = self.native
        self.mesh = importlib.import_module("aioble.mesh")
        self.node = self.mesh.Node(bytes(range(16)), company_id=0x1234)

    async def asyncTearDown(self):
        self.native.fail_stop = False
        self.node.stop()
        self.aioble.stop()
        await asyncio.sleep(0)

    async def test_optional_import(self):
        del sys.modules["blemesh"]
        test_ble5.BLE5Tests.load_aioble(self)
        self.assertTrue(callable(self.aioble.periodic_advertise))
        self.assertNotIn("aioble.mesh", sys.modules)

    async def test_same_ble_instance_and_irq(self):
        handler = self.ble.handler
        self.node.start()
        self.assertIs(self.ble.handler, handler)
        self.assertEqual(self.aioble.ble5_features()["advertising_instances"], 4)
        self.node.stop()
        self.assertTrue(self.ble.enabled)
        self.assertIs(self.ble.handler, handler)

    async def test_message_before_and_after_wait(self):
        self.node.start()
        self.native.emit((4, b"first"))
        self.assertEqual(await self.node.recv(), (4, b"first"))
        task = asyncio.create_task(self.node.recv())
        await asyncio.sleep(0)
        self.native.emit((4, b"second"))
        self.assertEqual(await task, (4, b"second"))

    async def test_mesh_keeps_new_public_instance_and_reserves_private_one(self):
        self.node.start()
        async with self.aioble.periodic_advertise(100000, instance=3):
            self.assertEqual(self.ble.periodic_instances, {3})
            with self.assertRaises(ValueError):
                async with self.aioble.periodic_advertise(100000, instance=4):
                    pass
            self.assertEqual(self.native.state, 2)
        self.assertEqual(self.ble.periodic_instances, set())

    async def test_timeout_then_receive(self):
        self.node.start()
        with self.assertRaises(asyncio.TimeoutError):
            await self.node.recv(timeout_ms=1)
        self.native.emit("after timeout")
        self.assertEqual(await self.node.recv(), "after timeout")

    async def test_cancel_does_not_stop_mesh(self):
        self.node.start()
        task = asyncio.create_task(self.node.recv())
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.native.state, 2)
        self.native.emit("after cancel")
        self.assertEqual(await self.node.recv(), "after cancel")

    async def test_stop_wakes_pending_receiver(self):
        self.node.start()
        task = asyncio.create_task(self.node.recv())
        await asyncio.sleep(0)
        self.node.stop()
        with self.assertRaises(self.mesh.MeshStoppedError):
            await task

    async def test_aioble_stop_wakes_pending_receiver(self):
        self.node.start()
        task = asyncio.create_task(self.node.recv())
        await asyncio.sleep(0)
        self.aioble.stop()
        with self.assertRaises(self.mesh.MeshStoppedError):
            await task

    async def test_immediate_restart_isolates_old_receiver(self):
        for timed in (False, True):
            with self.subTest(timed=timed):
                self.node.start()
                old = asyncio.create_task(self.node.recv(timeout_ms=1000 if timed else None))
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                self.node.stop()
                self.node.start()
                self.native.emit("new session")
                self.assertEqual(await self.node.recv(), "new session")
                with self.assertRaises(self.mesh.MeshStoppedError):
                    await old
                self.node.stop()

    async def test_aioble_shutdown_restart_isolates_old_receiver(self):
        self.node.start()
        old = asyncio.create_task(self.node.recv())
        await asyncio.sleep(0)
        self.aioble.stop()
        self.native.state = 0  # Native BLE shutdown also deinitialises Mesh.
        self.node.start()
        self.native.emit("new session")
        with self.assertRaises(self.mesh.MeshStoppedError):
            await old
        self.assertEqual(await self.node.recv(), "new session")

    async def test_old_receiver_cancel_does_not_release_new_receiver(self):
        self.node.start()
        old = asyncio.create_task(self.node.recv())
        await asyncio.sleep(0)
        self.node.stop()
        self.node.start()
        new = asyncio.create_task(self.node.recv())
        old.cancel()
        await asyncio.sleep(0)
        with self.assertRaises(asyncio.CancelledError):
            await old
        with self.assertRaises(ValueError):
            await self.node.recv()
        self.native.emit("new session")
        self.assertEqual(await new, "new session")

    async def test_overflow_is_observable(self):
        self.node.start()
        self.native.dropped = 1
        with self.assertRaises(self.mesh.MeshOverflowError):
            await self.node.recv()
        self.native.emit("remaining")
        self.assertEqual(await self.node.recv(), "remaining")

    async def test_single_reader(self):
        self.node.start()
        task = asyncio.create_task(self.node.recv())
        await asyncio.sleep(0)
        with self.assertRaises(ValueError):
            await self.node.recv()
        self.native.emit("first")
        self.assertEqual(await task, "first")

    async def test_foreign_native_node_is_not_stopped(self):
        self.native.state = 2
        with self.assertRaises(ValueError):
            self.node.start()
        self.assertEqual(self.native.calls, [])
        self.assertEqual(self.native.state, 2)

    async def test_failed_start_can_be_retried(self):
        self.native.fail_start = True
        with self.assertRaises(OSError):
            self.node.start()
        self.native.fail_start = False
        self.node.start()
        self.assertEqual(self.native.state, 2)

    async def test_failed_stop_preserves_ownership(self):
        self.node.start()
        self.native.fail_stop = True
        with self.assertRaises(OSError):
            self.node.stop()
        self.assertIs(self.mesh._node, self.node)
        self.assertIsNotNone(self.native.handler)

    async def test_context_cancel_closes_mesh_only(self):
        async def consume():
            async with self.node:
                await self.node.recv()
        task = asyncio.create_task(consume())
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.native.state, 0)
        self.assertTrue(self.ble.enabled)

    async def test_send_is_local_submission(self):
        self.node.start()
        self.assertIsNone(self.node.send(0xc001, b"data", net_idx=0, app_idx=1, opcode=1))
        self.assertEqual(self.native.calls[-1], (
            "send", (0xc001, b"data"), {"net_idx": 0, "app_idx": 1, "opcode": 1, "ttl": 7}))


if __name__ == "__main__":
    unittest.main()
