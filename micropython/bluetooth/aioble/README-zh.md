aioble
======

此库为 MicroPython 的
[bluetooth](https://docs.micropython.org/en/latest/library/bluetooth.html) API
提供面向对象、基于 asyncio 的封装。

**注意**：aioble 要求 MicroPython v1.17 或更高版本。

功能
----

广播者（advertiser）角色：
* 为常用字段生成广播和扫描响应载荷。
* 自动在广播和扫描响应之间拆分载荷。
* 开始广播（无限期或指定时长）。

外围设备角色：
* 等待中心设备连接。
* 等待 MTU 交换。

观察者（扫描器）角色：
* 扫描设备（被动 + 主动）。
* 合并同一设备的广播和扫描响应载荷。
* 解析广播载荷中的常用字段。

中心设备角色：
* 连接外围设备。
* 发起 MTU 交换。

GATT 客户端：
* 发现服务、特征和描述符（可选按 UUID）。
* 读取/写入/带响应写入特征和描述符。
* 订阅特征的通知和指示（通过 CCCD）。
* 等待通知和指示。

GATT 服务器：
* 注册服务、特征和描述符。
* 等待特征和描述符的写入。
* 拦截读取请求。
* 发送通知和指示（并等待响应）。

L2CAP：
* 接受和连接 L2CAP 面向连接的信道。
* 管理信道流量控制。

安全：
* 基于 JSON 的密钥/密文管理。
* 发起配对。
* 查询加密/认证状态。

所有远程操作（连接、断开连接、客户端读写、服务器指示、l2cap 接收/发送、配对）都可等待，并支持超时。

安装
----

你可以安装以下任意组合的软件包。
- `aioble-central` -- 中心设备（及观察者）角色功能，包括扫描和连接。
- `aioble-client` -- GATT 客户端，通常由中心设备使用，也可用于外围设备。
- `aioble-l2cap` -- L2CAP 面向连接信道支持。
- `aioble-peripheral` -- 外围设备（及广播者）角色功能，包括广播。
- `aioble-security` -- 配对和绑定支持。
- `aioble-server` -- GATT 服务器，通常由外围设备使用，也可用于中心设备。

或者，安装 `aioble` 软件包，它会安装全部组件。

用法
----

#### 被动扫描附近设备 5 秒：（观察者）

```py
async with aioble.scan(duration_ms=5000) as scanner:
    async for result in scanner:
        print(result, result.name(), result.rssi, result.services())
```

主动扫描（包含“扫描响应”数据）附近设备 5 秒，并使用最高占空比：（观察者）

```py
async with aioble.scan(duration_ms=5000, interval_us=30000, window_us=30000, active=True) as scanner:
    async for result in scanner:
        print(result, result.name(), result.rssi, result.services())
```

#### 连接外围设备：（中心设备）

```py
# 从扫描结果获取
device = result.device
# 或使用已知地址
device = aioble.Device(aioble.ADDR_PUBLIC, "aa:bb:cc:dd:ee:ff")

try:
    connection = await device.connect(timeout_ms=2000)
except asyncio.TimeoutError:
    print('Timeout')
```

#### 注册服务并等待连接：（外围设备、服务器）

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

#### 更新特征值：（服务器）

```py
# 写入本地值。
temp_char.write(b'data')
```

```py
# 写入本地值并通知/指示订阅者。
temp_char.write(b'data', send_update=True)
```

#### 发送通知：（服务器）

```py
# 使用当前值发送通知。
temp_char.notify(connection)
```

```py
# 使用自定义值发送通知。
temp_char.notify(connection, b'optional data')
```

#### 发送指示：（服务器）

```py
# 使用当前值发送指示。
await temp_char.indicate(connection, timeout_ms=2000)
```

```py
# 使用自定义值发送指示。
await temp_char.indicate(connection, b'optional data', timeout_ms=2000)
```

如果指示未被确认，将引发 `GattError`。

asyncio 超时或任务取消会结束等待，但不会取消已经提交给控制器的指示。
来自另一连接的完成 IRQ 会被忽略，包括超时或取消后的迟到确认；
它们不会结束当前连接的指示等待。
每次指示都会创建新的完成标志，因此在完成 IRQ 到达后取消等待者，也不会让后续指示在自身 IRQ 到达前完成。
同一连接的所有特征最多只能有一条底层指示等待确认；在完成 IRQ 到达前，
该连接再次调用 `indicate()` 会引发 `ValueError`，即使原等待者已经超时或被取消。
各连接的未完成请求独立记录，在最终确认、真实断开或 `aioble.stop()` 时释放，
避免把旧确认误认为新请求。每个特征仍只支持一个正在等待的 Python 调用。

#### 等待客户端写入：（服务器）

```py
# 普通特征，返回执行写入的连接。
connection = await char.written(timeout_ms=2000)
```

```py
# 启用捕获的特征，同时返回值。
char = Characteristic(..., capture=True)
connection, data = await char.written(timeout_ms=2000)
```

#### 查询特征值：（客户端）

```py
temp_service = await connection.service(_ENV_SENSE_UUID)
temp_char = await temp_service.characteristic(_ENV_SENSE_TEMP_UUID)

data = await temp_char.read(timeout_ms=1000)
```

#### 等待通知/指示：（客户端）

```py
# 通知
data = await temp_char.notified(timeout_ms=1000)
```

```py
# 指示
data = await temp_char.indicated(timeout_ms=1000)
```

#### 订阅特征：（客户端）

```py
# 订阅通知。
await temp_char.subscribe(notify=True)
while True:
    data = await temp_char.notified()
```

```py
# 订阅指示。
await temp_char.subscribe(indicate=True)
while True:
    data = await temp_char.indicated()
```

#### 打开 L2CAP 信道：（监听器）

```py
channel = await connection.l2cap_accept(_L2CAP_PSN, _L2CAP_MTU)
buf = bytearray(64)
n = channel.recvinto(buf)
channel.send(b'response')
```

#### 打开 L2CAP 信道：（发起者）

```py
channel = await connection.l2cap_connect(_L2CAP_PSN, _L2CAP_MTU)
channel.send(b'request')
buf = bytearray(64)
n = channel.recvinto(buf)
```


ESP32 BLE 5 扩展
----------------

此分支还支持本项目 ESP32 端口新增的 BLE 5 方法。
现有的 `scan()`、`advertise()` 和 `Device.connect()` 调用仍保留其旧版默认行为。
BLE 5 操作必须显式指定；缺少所需固件 API 时会引发 `NotImplementedError`。
在其他端口和较旧固件上导入 aioble 并使用其旧版函数仍然有效。

请从此检出版本安装适配后的 aioble 源码，包括 `periodic.py`。
`aioble-central` 清单包含周期同步；`aioble-peripheral` 清单包含周期广播。
安装未修改的上游 aioble 软件包不会提供这些扩展。
冻结清单可以在默认软件包库之前选择此检出版本：

```py
add_library("ble5-aioble", "$(MPY_DIR)/micropython-lib/micropython", prepend=True)
require("aioble")
```

ESP32 BLE 5 固件工作流启用底层 API；其默认开发板清单不会冻结 aioble。
必须单独安装适配后的库，或通过自定义冻结清单选择它。
请按该工作流使用 ESP-IDF v5.5.5；现有端口审计指出，v5.5 至 v5.5.3 存在 SDK 周期同步重试缺陷，
此 Python 适配器无法修复该缺陷。

#### 功能与 PHY

```py
print(aioble.ble5_features())

# 这些是掩码，与 phy() 和扫描结果报告的 PHY 值不同。
phys = aioble.PHY_1M_MASK | aioble.PHY_2M_MASK
aioble.set_default_phy(phys, phys)

connection = await device.connect(phys=aioble.PHY_CODED_MASK)
print(connection.phy())                # (tx_phy, rx_phy)
print(await connection.set_phy(phys, phys, timeout_ms=1000))
```

`Device.connect(..., phys=None)` 使用旧版连接 API。
提供 `phys` 会使用 `gap_connect_ext()`，并保留相同的连接/GATT 对象。

同一 `Device` 的首次 `connect()` 尚未结束时，再次调用会引发 `ValueError`；
成功 IRQ 已到达、但建立流程尚未返回时也一样。建立流程完成后，对仍然连接的设备
再次调用 `connect()` 会返回已有连接，不向控制器重复提交命令。

如果传出连接被取消或超时，aioble 会保留该请求，直到控制器报告取消/失败，或断开一个延迟成功建立的连接。
成功的 IRQ 会立即注册连接；连接建立和断开清理由不同的标志控制。
失败的取消和断开命令会在 asyncio 任务中每 250 毫秒重试一次。
当控制器发起/取消操作仍在等待，或同一设备的清理尚未完成时，新的发起操作会引发 `ValueError`。
旧发起操作完成后，其他设备即可连接。
`aioble.stop()` 会释放待处理请求的所有权并唤醒等待者。
重新使用的句柄会保留新连接的 IRQ 映射；清理操作会在删除映射或重试断开连接前检查对象所有权。

`is_connected()` 还会检查连接是否仍拥有其控制器句柄。
句柄属于替代连接后，对旧对象调用 `disconnect()` 或 `disconnected()` 只会等待其自身的清理任务，
不会断开替代连接。
PHY、MTU、GATT、配对和 L2CAP 操作会在发送命令前拒绝过期连接。
原有的 `ValueError` 检查保持不变；其他远程操作会引发 `DeviceDisconnectedError`
（信道数据操作使用 `L2CAPDisconnectedError`）。
通过缓存连接已丢失句柄的 `Device` 重新连接时，会创建新的连接对象。

`disconnected()` 和 `disconnect()` 使用独立的清理完成事件等待。
它们的超时或取消只结束当前调用者的等待；连接清理任务会继续运行，直到真实断开 IRQ 到达，
再清除句柄、映射、缓存连接和待处理操作的等待者。多个断开等待者可以独立取消。

`set_phy(tx_phys, rx_phys, *, coded=0, timeout_ms=1000)` 会等待 PHY 更新 IRQ 并返回协商后的 PHY 值。
`coded=0/1/2` 分别表示无偏好、S2 或 S8。
超时的更新会一直保留，直到其 IRQ 到达，因此延迟完成不会满足后续请求。
失败的 BLE 5 IRQ 操作会以原始 NimBLE 状态引发 `OSError(status)`；
方法立即失败时仍保留端口的 errno 映射。

#### 扩展扫描

```py
async with aioble.scan(
    5000, extended=True,
    phys=aioble.PHY_1M_MASK | aioble.PHY_CODED_MASK,
    interval_us=30000, window_us=30000, active=True,
) as scanner:
    async for result in scanner:
        print(result.name(), result.sid, result.primary_phy, result.secondary_phy)
```

`extended=True` 会选择扩展扫描 API。提供 `phys` 也会选择该 API；默认值为 1M 掩码。
扫描支持 1M 和 Coded，但不支持主广播信道上的 2M。
仍然只有一个扫描器。发起连接时，aioble 会像以前一样取消活动扫描器。

扩展的 `ScanResult` 除现有属性和字段解码器外，还包含 `properties`、`sid`、`primary_phy`、
`secondary_phy`、`periodic_interval`、`tx_power` 和 `data_status`。
`periodic_interval` 以 1.25ms 为单位；零表示没有周期广播。
结果按地址和 SID 分开。
为避免重复，会忽略与扩展报告同时发出的旧版报告。
旧版 PDU 的 SID 可以为 255，而 255 不是有效的周期同步 SID。

载荷片段按到达顺序组装，广播数据和扫描响应数据分别处理。
不完整的片段会被暂缓。截断或超大的片段链会产生 `data_status=2`，
受影响的载荷设为 `None`；它们绝不会作为完整广播数据解析。
组装后的载荷上限为 1650 字节。
地址和载荷的 memoryview 会在 IRQ 期间复制。
应及时消费排队的扫描结果和周期报告；如果应用跟不上，队列会增长。

#### 扩展广播

```py
connection = await aioble.advertise(
    100000, extended=True, instance=1, sid=2,
    secondary_phy=aioble.PHY_2M,
    name=b"extended-sensor", manufacturer=(0xabcd, b"x" * 80),
    timeout_ms=10000,
)
```

新增的关键字参数为 `extended=False`、`instance=1`、`scannable=False`、`primary_phy=1`、
`secondary_phy=1` 和 `sid=0`。
此处的 PHY 参数是值而不是掩码。
扩展实例必须大于零；实例零仍保留给旧版广播。
自动载荷生成使用固件的 `max_adv_data_len`，并将所有字段保存在一个载荷中
（可连接广播最多 251 字节）。
由于长度字段只有一个字节，每个 AD 字段的值数据最多为 254 字节。
可扫描的扩展广播会将生成的数据放入扫描响应；手动提供的数据也必须遵循相同规则。
扩展广播不能同时可连接和可扫描。
不可扫描的扩展广播不能包含扫描响应数据。

支持多个扩展实例，连接通过广播完成 IRQ 的实例和连接句柄进行路由。
在 aioble 中，旧版和扩展的可连接广播不能重叠，因为旧版连接 IRQ 没有实例标识符。
重新使用已占用的实例会引发 `ValueError`。
实例必须是
`1 <= instance < aioble.ble5_features()["advertising_instances"]` 范围内的整数；
无效类型会在预留实例前引发 `TypeError`，无效值会引发 `ValueError`。
超时和任务取消只会停止选定的实例；扩展实例也会在完成时移除。
与原 API 一样，`timeout_ms` 使用 asyncio 超时，取消时返回 `None`。

如果连接与取消或超时发生竞争，aioble 会继续负责该连接，直到两个连接 IRQ 完成关联，
并在 asyncio 任务中断开它。
关联未完成的实例仍会被预留，其控制器回调会一直保留到延迟 IRQ 到达。
之后会移除该实例。
只有在广播清理成功后才会返回连接。
停止或移除错误会传递给调用者，未返回的连接会在后台断开。
失败的移除或断开命令会使用 250 毫秒计时器和 IRQ 唤醒机制重试，直到成功或 `aioble.stop()` 关闭 BLE。

#### 周期广播

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
会拥有一个不可连接、不可扫描的扩展广播实例及其周期广播序列。
`adv_data` 是周期载荷；`discovery_data` 是用于发现该周期广播的扩展载荷。
退出上下文时会停止两者并移除实例。
周期间隔以微秒为单位，最小值为 7500us；端口会应用控制器的 1.25ms 单位。

周期广播使用相同的实例验证和后台清理机制。
如果停止或移除实例失败，错误会传递出去，并且实例在清理重试期间保持预留状态。
如果启动失败且回滚也失败，会传递原始启动错误，回滚则继续在后台进行。

#### 周期同步

在控制器建立同步期间保持扫描：

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
sync_timeout_ms=10000)` 接受带有显式 SID 的 `Device` 或扩展的 `ScanResult`，并返回 `PeriodicSync`。
`timeout_ms` 是本地 asyncio 截止时间；`sync_timeout_ms` 是控制器超时时间（100..163840ms）。
同一时间只能有一个同步创建操作处于等待状态；已建立的同步通过句柄路由。
由于使用显式地址，因此不受 SDK 广播者列表重试限制影响。

同步对象提供 `device`、`sid`、`periodic_interval`（1.25ms 单位）、`phy`、
`is_synced()` 和 `await close(timeout_ms=1000)`。
报告提供 `adv_data`、`data_status`、`rssi` 和 `tx_power`。
完整的片段链会被组装；截断/失败的报告的 `adv_data=None`，并保留其状态。
关闭操作会等待同步丢失事件。
当另一个创建操作处于等待状态时，不要关闭已建立的同步：NimBLE 会以忙错误拒绝终止操作。

如果创建操作超时或被取消，后台清理会取消待处理的控制器请求，等待其最终事件，
并终止因取消竞争而建立的任何同步。
在此之前，待处理槽位会保持预留。
`aioble.stop()` 会唤醒扫描器和周期等待者并清除其状态。
请从 asyncio 任务或主循环调用生命周期操作；ESP32 端口会在 NimBLE 主机 IRQ 中拒绝同步生命周期更改，并返回 `EBUSY`。

主机回归测试（不需要控制器或固件构建）：

```sh
python -B micropython-lib/micropython/bluetooth/aioble/tests/test_ble5.py
```

这些测试会模拟 BLE API 和 IRQ；真实的 ESP32/控制器互操作性仍需要硬件测试。


示例
----

请参阅 `examples` 目录中的示例应用。

* temp_sensor.py：温度传感器外围设备。
* temp_client.py：连接温度传感器。
* l2cap_file_server.py：简单的文件服务器外围设备。（WIP）
* l2cap_file_client.py：文件服务器的客户端。（WIP）

测试
----

`multitests` 目录提供了可通过 MicroPython 的 `run-multitests.py` 脚本运行的测试。
这些测试基于主仓库中现有的 `multi_bluetooth` 测试。
