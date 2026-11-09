"""Windows power events: the display turns on, or the PC resumes from sleep.

Why: the ASUS lighting service takes the LEDs back when the display turns on.
Measured on 2026-11-09: at 09:34:13.8 the service got WM_POWERBROADCAST
(PBT_POWERSETTINGCHANGE), and at 09:34:14.0 it took the Aura token and loaded
its own profile. The app gets no message about this, so it listens to the same
events and takes the LEDs again (Daemon.request_reclaim).

A hidden top-level window gets the events. A message-only window would miss
the broadcast PBT_APMRESUME* messages. RegisterPowerSettingNotification adds
the display state (GUID_CONSOLE_DISPLAY_STATE): Windows sends it at once with
the current state, then on each change.
"""

from __future__ import annotations

import ctypes
import logging
import sys
import threading
import uuid
from collections.abc import Callable
from ctypes import wintypes
from typing import Any

log = logging.getLogger(__name__)

WM_POWERBROADCAST = 0x0218
WM_CLOSE = 0x0010
PBT_APMRESUMESUSPEND = 0x0007
PBT_APMRESUMEAUTOMATIC = 0x0012
PBT_POWERSETTINGCHANGE = 0x8013
RESUME_EVENTS = (PBT_APMRESUMESUSPEND, PBT_APMRESUMEAUTOMATIC)
DEVICE_NOTIFY_WINDOW_HANDLE = 0
GUID_CONSOLE_DISPLAY_STATE = uuid.UUID("6fe69556-704a-47a0-8f24-c28d936fda47")
DISPLAY_ON = 1  # the other states: 0 = off, 2 = dimmed
HANDLED = True  # the return value for WM_POWERBROADCAST
STARTUP_TIMEOUT_S = 5.0
WINDOW_CLASS = "AlertColors.PowerEvents"


class _PowerSetting(ctypes.Structure):
    """POWERBROADCAST_SETTING with a DWORD of data (the display state has 4 bytes)."""

    _fields_ = [("power_setting", ctypes.c_ubyte * 16), ("data_length", wintypes.DWORD),
                ("data", wintypes.DWORD)]


def is_wake(event: int, setting_address: int) -> bool:
    """True when the display turned on, or the PC resumed.

    `setting_address` is the lParam of PBT_POWERSETTINGCHANGE: the address of
    a POWERBROADCAST_SETTING. Other power settings and other states are False.
    """
    if event in RESUME_EVENTS:
        return True
    if event != PBT_POWERSETTINGCHANGE or not setting_address:
        return False
    setting = _PowerSetting.from_address(setting_address)
    if bytes(setting.power_setting) != GUID_CONSOLE_DISPLAY_STATE.bytes_le:
        return False
    return setting.data == DISPLAY_ON


class PowerWatcher:
    """Calls `on_wake()` on its own thread when the display turns on or the PC resumes."""

    def __init__(self, on_wake: Callable[[], None]) -> None:
        self._on_wake = on_wake
        self._started = threading.Event()
        self.hwnd: int | None = None
        self._thread = threading.Thread(target=self._run, name="power-events", daemon=True)
        self._thread.start()
        if not self._started.wait(STARTUP_TIMEOUT_S):
            log.warning("power events: the window did not start within %ss", STARTUP_TIMEOUT_S)

    def stop(self) -> None:
        if self.hwnd is not None:
            import win32gui

            win32gui.PostMessage(self.hwnd, WM_CLOSE, 0, 0)
            self._thread.join(STARTUP_TIMEOUT_S)

    def _on_power(self, hwnd: int, message: int, wparam: int, lparam: int) -> bool:
        try:
            if is_wake(wparam, lparam):
                self._on_wake()
        except Exception:  # noqa: BLE001  never let an error end the message loop
            log.exception("power events: the handler failed")
        return HANDLED

    def _run(self) -> None:
        try:
            import win32api
            import win32gui

            def on_close(hwnd: int, message: int, wparam: int, lparam: int) -> int:
                win32gui.DestroyWindow(hwnd)
                win32gui.PostQuitMessage(0)
                return 0

            window_class = win32gui.WNDCLASS()
            window_class.lpszClassName = f"{WINDOW_CLASS}.{uuid.uuid4().hex}"  # unique for each watcher
            window_class.hInstance = win32api.GetModuleHandle(None)
            window_class.lpfnWndProc = {WM_POWERBROADCAST: self._on_power, WM_CLOSE: on_close}
            atom = win32gui.RegisterClass(window_class)
            # A top-level window that is never shown: it gets the power broadcasts.
            self.hwnd = win32gui.CreateWindow(atom, WINDOW_CLASS, 0, 0, 0, 0, 0, 0, 0,
                                              window_class.hInstance, None)
            notification = _register_display_state(self.hwnd)
        except Exception:  # noqa: BLE001  without this window the app still works
            log.exception("power events: cannot listen; the LEDs are taken again only by the interval")
            self._started.set()
            return
        self._started.set()
        log.info("power events: listening for display on and resume")
        try:
            win32gui.PumpMessages()
        finally:
            if notification:
                _user32().UnregisterPowerSettingNotification(notification)


def _user32() -> Any:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.RegisterPowerSettingNotification.restype = wintypes.HANDLE
    user32.RegisterPowerSettingNotification.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
    user32.UnregisterPowerSettingNotification.argtypes = [wintypes.HANDLE]
    return user32


def _register_display_state(hwnd: int) -> int | None:
    guid = (ctypes.c_ubyte * 16).from_buffer_copy(GUID_CONSOLE_DISPLAY_STATE.bytes_le)
    handle = _user32().RegisterPowerSettingNotification(hwnd, ctypes.byref(guid), DEVICE_NOTIFY_WINDOW_HANDLE)
    if not handle:
        log.warning("power events: no display-state events (error %s)", ctypes.get_last_error())
    return handle


def watch(on_wake: Callable[[], None]) -> Callable[[], None]:
    """Start listening. Returns the function that stops it. Not Windows: nothing to do."""
    if sys.platform != "win32":
        return lambda: None
    return PowerWatcher(on_wake).stop
