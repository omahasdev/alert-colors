"""ASUS Aura SDK backend (COM object `aura.sdk.1`, AuraSdk_x64.dll).

Measured facts:
- The SDK enumerates nothing in a single-threaded COM apartment. It needs MTA.
- Without admin rights it sees only some devices (for example, a graphics
  card). The motherboard and the ARGB headers need an elevated process.
- `SwitchMode()` takes the LEDs from Armoury Crate. `ReleaseControl(0)` gives
  them back.
- The ASUS service takes the LEDs back each time the display turns on, and the
  SDK of this process gets no error: its Apply() calls only stop showing. So
  `reclaim()` calls SwitchMode() again (see power_events.py).
- The first `Enumerate()` in a process takes ~30 s (elevated). Later calls,
  and SwitchMode/ReleaseControl, take ~0 s. So the SDK object and the device
  list live as long as the process, and release only gives up control.
"""

from __future__ import annotations

import fnmatch
import logging
import sys
from dataclasses import dataclass, field
from typing import Any

from ..frame import Frame
from .base import BACKENDS

log = logging.getLogger(__name__)

PROG_ID = "aura.sdk.1"
ALL_DEVICE_TYPES = 0
RELEASE_FLAGS = 0
COINIT_MULTITHREADED = 0
RPC_E_CHANGED_MODE = -2147417850


def _init_mta() -> Any:
    if "pythoncom" not in sys.modules:
        sys.coinit_flags = COINIT_MULTITHREADED  # read by pythoncom on first import
    import pythoncom

    try:
        pythoncom.CoInitializeEx(pythoncom.COINIT_MULTITHREADED)
    except pythoncom.com_error as error:
        if error.hresult == RPC_E_CHANGED_MODE:
            raise RuntimeError(
                "Aura SDK needs a multithreaded COM apartment, but this thread is "
                "already single-threaded. Create the Aura backend before other COM use."
            ) from error
        # S_FALSE / already initialized as MTA: fine
    return pythoncom


@dataclass
class AuraSdkBackend:
    include: list[str]
    # LEDs that are really connected, by device name. An ARGB header reports
    # its maximum (120); a per-LED effect then spans this many LEDs.
    led_counts: dict[str, int] = field(default_factory=dict)
    name: str = "aura_sdk"
    _sdk: Any = field(default=None, repr=False)
    _devices: list[Any] = field(default_factory=list, repr=False)
    _controlling: bool = False
    _last: dict[str, tuple[int, ...]] = field(default_factory=dict, repr=False)

    def _connect(self) -> Any:
        if self._sdk is None:
            _init_mta()
            import win32com.client

            log.info("Aura SDK: scanning devices (the first scan takes ~30 s)")
            self._sdk = win32com.client.Dispatch(PROG_ID)
            self._enumerate()
        return self._sdk

    def _acquire(self) -> None:
        if self._controlling:
            return
        sdk = self._connect()
        sdk.SwitchMode()
        self._devices = [d for d in self._enumerate() if self._wanted(d.Name)]
        self._controlling = True
        self._last = {}
        log.info("Aura control taken: %s", ", ".join(d.Name for d in self._devices) or "no devices")

    def _enumerate(self) -> list[Any]:
        collection = self._sdk.Enumerate(ALL_DEVICE_TYPES)
        return [collection.Item(i) for i in range(collection.Count)]

    def _wanted(self, device_name: str) -> bool:
        return any(fnmatch.fnmatchcase(device_name, pattern) for pattern in self.include)

    def _packed(self, frame: Frame, name: str, lights: int) -> tuple[int, ...]:
        if frame.uniform:
            return (frame.color().to_bgr_int(),) * lights
        count = min(self.led_counts.get(name, lights), lights)
        colors = [c.to_bgr_int() for c in frame.colors(count)]
        return tuple(colors + [colors[-1]] * (lights - count))  # LEDs past `count` copy the last one

    def show(self, frame: Frame) -> None:
        self._acquire()
        try:
            for device in self._devices:
                lights = device.Lights
                packed = self._packed(frame, device.Name, lights.Count)
                if packed == self._last.get(device.Name):
                    continue  # unchanged: no COM calls
                for i, value in enumerate(packed):
                    lights.Item(i).Color = value
                device.Apply()
                self._last[device.Name] = packed
        except Exception:
            self._sdk, self._controlling = None, False  # reconnect on the next show
            raise

    def reclaim(self) -> None:
        if not self._controlling:
            return  # the next show() takes control anyway
        self._sdk.SwitchMode()
        self._last = {}  # the service applied its own profile: send every color again
        log.debug("Aura control taken again")

    def release(self) -> None:
        if not self._controlling:
            return
        self._controlling = False
        self._last = {}
        self._sdk.ReleaseControl(RELEASE_FLAGS)
        log.info("Aura control released to Armoury Crate")

    def describe(self) -> list[str]:
        collection = self._connect().Enumerate(ALL_DEVICE_TYPES)
        lines = []
        for i in range(collection.Count):
            device = collection.Item(i)
            mark = "x" if self._wanted(device.Name) else " "
            lines.append(
                f"[{mark}] {device.Name}  type=0x{device.Type:08X}  lights={device.Lights.Count}"
            )
        return lines


@BACKENDS.register("aura_sdk")
def _create(options: dict[str, Any]) -> AuraSdkBackend:
    return AuraSdkBackend(
        include=list(options["include"]),
        led_counts={str(k): int(v) for k, v in options.get("led_counts", {}).items()},
    )
