# MicroPython aioble module
# MIT license; Copyright (c) 2021 Jim Mussared

from micropython import const

from .device import Device, DeviceDisconnectedError
from .core import (
    log_info,
    log_warn,
    log_error,
    GattError,
    config,
    stop,
    ble5_features,
    set_default_phy,
    set_tx_power,
)

try:
    from .peripheral import advertise, periodic_advertise
except:
    log_info("Peripheral support disabled")

try:
    from .central import scan
except:
    log_info("Central support disabled")

try:
    from .periodic import periodic_sync, PeriodicSyncLostError
except:
    log_info("Periodic sync support disabled")

try:
    from .server import (
        Service,
        Characteristic,
        BufferedCharacteristic,
        Descriptor,
        register_services,
    )
except:
    log_info("GATT server support disabled")


ADDR_PUBLIC = const(0)
ADDR_RANDOM = const(1)

PHY_1M = const(1)
PHY_2M = const(2)
PHY_CODED = const(3)
PHY_1M_MASK = const(1)
PHY_2M_MASK = const(2)
PHY_CODED_MASK = const(4)

# ESP-IDF enhanced power types and power-level indices, not dBm values.
TX_POWER_TYPE_DEFAULT = const(0)
TX_POWER_TYPE_ADV = const(1)
TX_POWER_TYPE_SCAN = const(2)
TX_POWER_TYPE_INIT = const(3)
TX_POWER_TYPE_CONN = const(4)
TX_POWER_N24 = const(0)
TX_POWER_N21 = const(1)
TX_POWER_N18 = const(2)
TX_POWER_N15 = const(3)
TX_POWER_N12 = const(4)
TX_POWER_N9 = const(5)
TX_POWER_N6 = const(6)
TX_POWER_N3 = const(7)
TX_POWER_N0 = const(8)
TX_POWER_P3 = const(9)
TX_POWER_P6 = const(10)
TX_POWER_P9 = const(11)
TX_POWER_P12 = const(12)
TX_POWER_P15 = const(13)
TX_POWER_P18 = const(14)
TX_POWER_P20 = const(15)
