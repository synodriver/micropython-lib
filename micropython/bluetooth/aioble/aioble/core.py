# MicroPython aioble module
# MIT license; Copyright (c) 2021 Jim Mussared

import bluetooth


log_level = 1


def log_error(*args):
    if log_level > 0:
        print("[aioble] E:", *args)


def log_warn(*args):
    if log_level > 1:
        print("[aioble] W:", *args)


def log_info(*args):
    if log_level > 2:
        print("[aioble] I:", *args)


class GattError(Exception):
    def __init__(self, status):
        self._status = status


def ensure_active():
    if not ble.active():
        try:
            from .security import load_secrets

            load_secrets()
        except:
            pass
        ble.active(True)


def config(*args, **kwargs):
    ensure_active()
    return ble.config(*args, **kwargs)


def _ble5_method(name):
    method = getattr(ble, name, None)
    if method is None:
        raise NotImplementedError("BLE 5 API unavailable: " + name)
    ensure_active()
    return method


def ble5_features():
    method = getattr(ble, "ble5_features", None)
    if method:
        ensure_active()
        return method()
    return {
        "phys": 1,
        "extended_advertising": False,
        "periodic_advertising": False,
        "tx_power": False,
        "advertising_instances": 1,
        "max_adv_data_len": 31,
    }


def set_default_phy(tx_phys, rx_phys):
    _ble5_method("gap_set_phy")(None, tx_phys, rx_phys)


def set_tx_power(power_type, handle, power_level):
    return _ble5_method("gap_set_tx_power")(power_type, handle, power_level)


# Because different functionality is enabled by which files are available the
# different modules can register their IRQ handlers and shutdown handlers
# dynamically.
_irq_handlers = []
_shutdown_handlers = []


def register_irq_handler(irq, shutdown):
    if irq:
        _irq_handlers.append(irq)
    if shutdown:
        _shutdown_handlers.append(shutdown)


def stop():
    ble.active(False)
    for handler in _shutdown_handlers:
        handler()


# Dispatch IRQs to the registered sub-modules.
def ble_irq(event, data):
    log_info(event, data)

    for handler in _irq_handlers:
        result = handler(event, data)
        if result is not None:
            return result


# TODO: Allow this to be injected.
ble = bluetooth.BLE()
ble.irq(ble_irq)
