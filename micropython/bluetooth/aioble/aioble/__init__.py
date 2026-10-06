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
