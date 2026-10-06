aioble
======

This library provides an object-oriented, asyncio-based wrapper for MicroPython's
[bluetooth](https://docs.micropython.org/en/latest/library/bluetooth.html) API.

**Note**: aioble requires MicroPython v1.17 or higher.

Features
--------

Broadcaster (advertiser) role:
* Generate advertising and scan response payloads for common fields.
* Automatically split payload over advertising and scan response.
* Start advertising (indefinitely or for duration).

Peripheral role:
* Wait for connection from central.
* Wait for MTU exchange.

Observer (scanner) role:
* Scan for devices (passive + active).
* Combine advertising and scan response payloads for the same device.
* Parse common fields from advertising payloads.

Central role:
* Connect to peripheral.
* Initiate MTU exchange.

GATT Client:
* Discover services, characteristics, and descriptors (optionally by UUID).
* Read / write / write-with-response characters and descriptors.
* Subscribe to notifications and indications on characteristics (via the CCCD).
* Wait for notifications and indications.

GATT Server:
* Register services, characteristics, and descriptors.
* Wait for writes on characteristics and descriptors.
* Intercept read requests.
* Send notifications and indications (and wait on response).

L2CAP:
* Accept and connect L2CAP Connection-oriented-channels.
* Manage channel flow control.

Security:
* JSON-backed key/secret management.
* Initiate pairing.
* Query encryption/authentication state.

All remote operations (connect, disconnect, client read/write, server indicate, l2cap recv/send, pair) are awaitable and support timeouts.

Installation
------------

You can install any combination of the following packages.
- `aioble-central` -- Central (and Observer) role functionality including
  scanning and connecting.
- `aioble-client` -- GATT client, typically used by central role devices but
  can also be used on peripherals.
- `aioble-l2cap` -- L2CAP Connection-oriented-channels support.
- `aioble-peripheral` -- Peripheral (and Broadcaster) role functionality
  including advertising.
- `aioble-security` -- Pairing and bonding support.
- `aioble-server` -- GATT server, typically used by peripheral role devices
  but can also be used on centrals.

Alternatively, install the `aioble` package, which will install everything.

Usage
-----

#### Passive scan for nearby devices for 5 seconds: (Observer)

```py
async with aioble.scan(duration_ms=5000) as scanner:
    async for result in scanner:
        print(result, result.name(), result.rssi, result.services())
```

Active scan (includes "scan response" data) for nearby devices for 5 seconds
with the highest duty cycle: (Observer)

```py
async with aioble.scan(duration_ms=5000, interval_us=30000, window_us=30000, active=True) as scanner:
    async for result in scanner:
        print(result, result.name(), result.rssi, result.services())
```

#### Connect to a peripheral device: (Central)

```py
# Either from scan result
device = result.device
# Or with known address
device = aioble.Device(aioble.ADDR_PUBLIC, "aa:bb:cc:dd:ee:ff")

try:
    connection = await device.connect(timeout_ms=2000)
except asyncio.TimeoutError:
    print('Timeout')
```

#### Register services and wait for connection: (Peripheral, Server)

```py
_ENV_SENSE_UUID = bluetooth.UUID(0x181A)
_ENV_SENSE_TEMP_UUID = bluetooth.UUID(0x2A6E)
_GENERIC_THERMOMETER = const(768)

_ADV_INTERVAL_US = const(250000)

temp_service = aioble.Service(_ENV_SENSE_UUID)
temp_char = aioble.Characteristic(temp_service, _ENV_SENSE_TEMP_UUID, read=True, notify=True)

aioble.register_services(temp_service)

while True:
    connection = await aioble.advertise(
            _ADV_INTERVAL_US,
            name="temp-sense",
            services=[_ENV_SENSE_UUID],
            appearance=_GENERIC_THERMOMETER,
            manufacturer=(0xabcd, b"1234"),
        )
    print("Connection from", device)
```

#### Update characteristic value: (Server)

```py
# Write the local value.
temp_char.write(b'data')
```

```py
# Write the local value and notify/indicate subscribers.
temp_char.write(b'data', send_update=True)
```

#### Send notifications: (Server)

```py
# Notify with the current value.
temp_char.notify(connection)
```

```py
# Notify with a custom value.
temp_char.notify(connection, b'optional data')
```

#### Send indications: (Server)

```py
# Indicate with current value.
await temp_char.indicate(connection, timeout_ms=2000)
```

```py
# Indicate with custom value.
await temp_char.indicate(connection, b'optional data', timeout_ms=2000)
```

This will raise `GattError` if the indication is not acknowledged.

An asyncio timeout or task cancellation ends the wait but does not cancel an
indication already submitted to the controller. Completion IRQs from a different
connection are ignored, including late confirmations after timeout or cancellation;
they cannot complete another connection's indication wait.
Each indication also gets a fresh completion flag, so cancelling a waiter after
its completion IRQ cannot make a later indication complete before its own IRQ.
Only one controller indication may be pending per connection, across all
characteristics. Another `indicate()` on that connection raises `ValueError`
until its completion IRQ arrives, even if the original waiter timed out or was
cancelled. Pending requests are tracked independently for each connection and
released on completion, disconnection, or `aioble.stop()`. This prevents an old
completion from being mistaken for a new request. Each characteristic still
supports only one active Python waiter at a time.

#### Wait for a write from the client: (Server)

```py
# Normal characteristic, returns the connection that did the write.
connection = await char.written(timeout_ms=2000)
```

```py
# Characteristic with capture enabled, also returns the value.
char = Characteristic(..., capture=True)
connection, data = await char.written(timeout_ms=2000)
```

#### Query the value of a characteristic: (Client)

```py
temp_service = await connection.service(_ENV_SENSE_UUID)
temp_char = await temp_service.characteristic(_ENV_SENSE_TEMP_UUID)

data = await temp_char.read(timeout_ms=1000)
```

#### Wait for a notification/indication: (Client)

```py
# Notification
data = await temp_char.notified(timeout_ms=1000)
```

```py
# Indication
data = await temp_char.indicated(timeout_ms=1000)
```

#### Subscribe to a characteristic: (Client)

```py
# Subscribe for notification.
await temp_char.subscribe(notify=True)
while True:
    data = await temp_char.notified()
```

```py
# Subscribe for indication.
await temp_char.subscribe(indicate=True)
while True:
    data = await temp_char.indicated()
```

#### Open L2CAP channels: (Listener)

```py
channel = await connection.l2cap_accept(_L2CAP_PSN, _L2CAP_MTU)
buf = bytearray(64)
n = channel.recvinto(buf)
channel.send(b'response')
```

#### Open L2CAP channels: (Initiator)

```py
channel = await connection.l2cap_connect(_L2CAP_PSN, _L2CAP_MTU)
channel.send(b'request')
buf = bytearray(64)
n = channel.recvinto(buf)
```


ESP32 BLE 5 Extensions
---------------------

This fork also supports the BLE 5 methods added by this project's ESP32 port.
Existing `scan()`, `advertise()`, and `Device.connect()` calls retain their
legacy defaults. BLE 5 operations are explicit and raise `NotImplementedError`
when the required firmware API is absent. Importing aioble and using its legacy
functions still works on other ports and on older firmware.

Install the adapted aioble sources from this checkout, including `periodic.py`.
The `aioble-central` manifest includes periodic synchronisation; the
`aioble-peripheral` manifest includes periodic advertising. Installing an
unmodified upstream aioble package does not provide these extensions. A frozen
manifest can select this checkout before the default package libraries:

```py
add_library("ble5-aioble", "$(MPY_DIR)/micropython-lib/micropython", prepend=True)
require("aioble")
```

The ESP32 BLE 5 firmware workflow enables the low-level APIs; its default board
manifest does not freeze aioble. The adapted library must be installed separately
or selected by a custom frozen manifest. Use ESP-IDF v5.5.5 as in that workflow;
the existing port audit identifies an SDK periodic-sync retry defect in
v5.5 through v5.5.3 that this Python adapter cannot correct.

#### Capabilities and PHY

```py
print(aioble.ble5_features())

# These are masks, unlike the PHY values reported by phy() and scan results.
phys = aioble.PHY_1M_MASK | aioble.PHY_2M_MASK
aioble.set_default_phy(phys, phys)

connection = await device.connect(phys=aioble.PHY_CODED_MASK)
print(connection.phy())                # (tx_phy, rx_phy)
print(await connection.set_phy(phys, phys, timeout_ms=1000))
```

`Device.connect(..., phys=None)` uses the legacy connection API. Providing
`phys` selects `gap_connect_ext()` with the same connection/GATT objects.

A second `connect()` on the same `Device` raises `ValueError` until the first call
has finished, including while its connection IRQ has arrived but setup has not
returned. Once setup finishes, calling `connect()` on an active connection returns
the existing connection without sending another controller command.

If an outgoing connection is cancelled or times out, aioble retains the request
until the controller reports cancellation/failure or a late successful connection
has been disconnected. The successful IRQ registers the connection immediately;
connection setup and disconnect cleanup use separate flags. Failed cancellation
and disconnect commands retry every 250ms in an asyncio task. A new initiation
raises `ValueError` while controller initiation/cancellation is pending, or while
cleanup for the same device is unfinished. Other devices can connect once the
old initiation has completed. `aioble.stop()` releases pending request ownership
and wakes its waiters. Reused handles retain the new connection's IRQ mapping;
cleanup checks object ownership before deleting mappings or retrying disconnects.

`is_connected()` also checks that the connection still owns its controller handle.
Once a handle belongs to a replacement, calling `disconnect()` or `disconnected()`
on the old object only waits for its own cleanup task. It does not disconnect the
replacement. PHY, MTU, GATT, pairing and L2CAP operations reject stale connections
before sending commands. Existing `ValueError` checks remain; other remote
operations raise `DeviceDisconnectedError` (channel data operations use
`L2CAPDisconnectedError`). Reconnecting through a `Device` whose cached connection
has lost its handle creates a new connection object.

`disconnected()` and `disconnect()` wait on a separate cleanup-completion event.
Their timeout or cancellation only ends that caller's wait; the connection task
continues until the real disconnect IRQ arrives, then clears the handle, mapping,
cached connection, and pending operation waiters. Multiple disconnect waiters can
be cancelled independently.

`set_phy(tx_phys, rx_phys, *, coded=0, timeout_ms=1000)` waits for the PHY update
IRQ and returns the negotiated PHY values. `coded=0/1/2` selects no preference,
S2, or S8. A timed-out update remains reserved until its IRQ arrives, so a late
completion cannot satisfy a subsequent request. Failed BLE 5 IRQ operations
raise `OSError(status)` with the raw NimBLE status; immediate method failures
retain the port's errno mapping.

#### Extended scanning

```py
async with aioble.scan(
    5000, extended=True,
    phys=aioble.PHY_1M_MASK | aioble.PHY_CODED_MASK,
    interval_us=30000, window_us=30000, active=True,
) as scanner:
    async for result in scanner:
        print(result.name(), result.sid, result.primary_phy, result.secondary_phy)
```

`extended=True` selects the extended scan API. Providing `phys` also selects
it; the default is the 1M mask. Scanning supports 1M and Coded, not 2M on the
primary advertising channels. There is still only one scanner. Starting a
connection cancels aioble's active scanner as before.

An extended `ScanResult` includes `properties`, `sid`, `primary_phy`,
`secondary_phy`, `periodic_interval`, `tx_power`, and `data_status` alongside
the existing properties and field decoders. `periodic_interval` is in 1.25ms
units; zero means no periodic advertising. Results are separated by address
and SID. Legacy reports emitted alongside extended reports are ignored to
avoid duplicates. Legacy PDUs can have SID 255, which is not a valid periodic
sync SID.

Payload fragments are assembled in arrival order, separately for advertising
and scan response data. Incomplete fragments are withheld. Truncated or
oversized chains produce `data_status=2` with the affected payload set to
`None`; they are never parsed as complete advertising data. The assembled
payload limit is 1650 bytes. Address and payload memoryviews are copied during
the IRQ. Queued scan results and periodic reports should be consumed promptly;
their queues grow if the application cannot keep up.

#### Extended advertising

```py
connection = await aioble.advertise(
    100000, extended=True, instance=1, sid=2,
    secondary_phy=aioble.PHY_2M,
    name=b"extended-sensor", manufacturer=(0xabcd, b"x" * 80),
    timeout_ms=10000,
)
```

The added keyword parameters are `extended=False`, `instance=1`,
`scannable=False`, `primary_phy=1`, `secondary_phy=1`, and `sid=0`.
PHY parameters here are values, not masks. Extended instances must be greater
than zero; instance zero remains reserved for legacy advertising. Automatic
payload generation uses the firmware's `max_adv_data_len` and keeps all fields
in one payload (at most 251 bytes for connectable advertising). Each AD field
has at most 254 bytes of value data, due to its one-byte length. Scannable extended
advertising places generated data in the scan response; manually supplied data
must follow the same rule. Extended advertising cannot be both connectable and
scannable. Non-scannable extended advertising cannot have scan response data.

Multiple extended instances are supported, with connections routed by the
advertising-complete IRQ's instance and connection handle. Legacy and extended
connectable advertising cannot overlap in aioble because the legacy connection
IRQ has no instance identifier. Reusing an occupied instance raises `ValueError`.
The instance must be an integer in
`1 <= instance < aioble.ble5_features()["advertising_instances"]`; invalid types
raise `TypeError` and invalid values raise `ValueError` before reserving it.
Timeouts and task cancellation stop only the selected instance; extended
instances are also removed on completion. As with the original API,
`timeout_ms` uses an asyncio timeout, and cancellation returns `None`.

If a connection races with cancellation or timeout, aioble keeps responsibility
for it until the two connection IRQs have been associated and disconnects it in
an asyncio task. An instance with an unfinished association remains reserved;
its controller callback is retained until the late IRQ arrives. The instance is
removed afterwards. A connection is returned only after advertising cleanup
succeeds. Stop/remove errors propagate to the caller, and unreturned connections
are disconnected in the background. Failed removal or disconnect commands are
retried using a 250ms timer and IRQ wakeups until they succeed or `aioble.stop()`
closes BLE.

#### Periodic advertising

```py
async with aioble.periodic_advertise(
    100000, b"\x05\xff\xcd\xab\x01\x02",
    instance=1, sid=3, discovery_data=b"\x02\x01\x06",
    secondary_phy=aioble.PHY_2M,
):
    await asyncio.sleep_ms(10000)
```

`periodic_advertise(interval_us, adv_data=None, *, instance=1, sid=0,
discovery_data=b"", extended_interval_us=100000, primary_phy=1, secondary_phy=1)`
owns a nonconnectable, nonscannable extended advertising instance and its
periodic train. `adv_data` is the periodic payload; `discovery_data` is the
extended payload used to discover it. Exiting the context stops both and
removes the instance. Periodic intervals are in microseconds, with a 7500us
minimum; the port applies the controller's 1.25ms units.

Periodic advertising uses the same instance validation and background cleanup.
If stopping or removing an instance fails, the error propagates and the instance
stays reserved while cleanup retries. If starting fails and rollback also fails,
the original start error propagates and rollback continues in the background.

#### Periodic synchronisation

Keep scanning while the controller establishes the sync:

```py
async with aioble.scan(0, extended=True) as scanner:
    async for result in scanner:
        if result.periodic_interval and 0 <= result.sid <= 15:
            sync = await aioble.periodic_sync(result, timeout_ms=10000)
            break

async with sync:
    try:
        async for report in sync:
            if report.data_status == 0:
                print(report.adv_data, report.rssi, report.tx_power)
    except aioble.PeriodicSyncLostError as error:
        print("Sync lost", error.reason)
```

`periodic_sync(device_or_scan_result, *, sid=None, skip=0, timeout_ms=10000,
sync_timeout_ms=10000)` accepts a `Device` with an explicit SID or an extended
`ScanResult`, and returns a `PeriodicSync`. `timeout_ms` is the local asyncio
deadline; `sync_timeout_ms` is the controller timeout (100..163840ms).
Only one sync creation can be pending; established syncs are routed by handle.
Explicit addresses are used, so the SDK advertiser-list retry restriction
does not apply.

The sync exposes `device`, `sid`, `periodic_interval` (1.25ms units), `phy`,
`is_synced()`, and `await close(timeout_ms=1000)`. Reports expose `adv_data`,
`data_status`, `rssi`, and `tx_power`. Complete fragment chains are assembled;
truncated/failed reports have `adv_data=None` and retain their status.
Closing waits for the sync-lost event. Do not close an established sync while
another create is pending: NimBLE rejects termination with a busy error.

If creation times out or is cancelled, background cleanup cancels the pending
controller request, waits for its final event, and terminates any sync that
won the race with cancellation. Until then the pending slot stays reserved.
`aioble.stop()` wakes scanner and periodic waiters and clears their state.
Call lifecycle operations from an asyncio task or the main loop; the ESP32 port
rejects synchronous lifecycle changes in the NimBLE host IRQ with `EBUSY`.

Host regression tests (no controller or firmware build required):

```sh
python -B micropython-lib/micropython/bluetooth/aioble/tests/test_ble5.py
```

These tests simulate the BLE API and IRQs; real ESP32/controller interoperability
still requires hardware testing.


Examples
--------

See the `examples` directory for some example applications.

* temp_sensor.py: Temperature sensor peripheral.
* temp_client.py: Connects to the temp sensor.
* l2cap_file_server.py: Simple file server peripheral. (WIP)
* l2cap_file_client.py: Client for the file server. (WIP)

Tests
-----

The `multitests` directory provides tests that can be run with MicroPython's `run-multitests.py` script. These are based on the existing `multi_bluetooth` tests that are in the main repo.
