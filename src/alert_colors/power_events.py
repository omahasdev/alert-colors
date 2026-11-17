"""Windows power events: the display turns on, or the PC resumes from sleep.

Why: the ASUS lighting service takes the LEDs back when the display turns on.
The service gets WM_POWERBROADCAST (PBT_POWERSETTINGCHANGE), and soon after it
takes the Aura token and loads its own profile. The app gets no message about this, so it listens to the same
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
WM_QUERYENDSESSION = 0x0011
WM_ENDSESSION = 0x0016
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

    def __init__(self, on_wake: Callable[[], None], on_end_session: Callable[[], None] | None = None) -> None:
        self._on_wake = on_wake
        self._end_session_callback = on_end_session
        self._started = threading.Event()
        self.hwnd: int | None = None
        self._thread = threading.Thread(target=self._run, name="power-events", daemon=True)
        self._thread.start()
        if not self._started.wait(STARTUP_TIMEOUT_S):
            log.warning("power events: the window did not start within %ss", STARTUP_TIMEOUT_S)

    def stop(self) -> None:
        if self.hwnd is not None and self._thread.is_alive():
            import pywintypes
            import win32gui

            try:
                win32gui.PostMessage(self.hwnd, WM_CLOSE, 0, 0)
            except pywintypes.error as error:  # the window ended already
                log.debug("power events: the window is gone: %s", error)
            self._thread.join(STARTUP_TIMEOUT_S)

    def _on_power(self, hwnd: int, message: int, wparam: int, lparam: int) -> bool:
        try:
            if is_wake(wparam, lparam):
                self._on_wake()
        except Exception:  # noqa: BLE001  never let an error end the message loop
            log.exception("power events: the handler failed")
        return HANDLED

    def _on_end_session(self, hwnd: int, message: int, wparam: int, lparam: int) -> int:
        # WM_ENDSESSION with wparam TRUE: the session ends (logoff or shutdown). Windows ends
        # the process when this returns, so the handler stops the app and waits for the
        # LED release first (for a limited time).
        if wparam and self._end_session_callback is not None:
            try:
                self._end_session_callback()
            except Exception:  # noqa: BLE001  the session ends anyway
                log.exception("power events: the end-of-session handler failed")
        return 0

    def _run(self) -> None:
        hwnd = notification = None  # set in the order that the start makes them
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
            window_class.lpfnWndProc = {WM_POWERBROADCAST: self._on_power, WM_CLOSE: on_close,
                                        WM_QUERYENDSESSION: lambda *args: True,  # the session may end
                                        WM_ENDSESSION: self._on_end_session}
            atom = win32gui.RegisterClass(window_class)
            # A top-level window that is never shown: it gets the power broadcasts.
            hwnd = win32gui.CreateWindow(atom, WINDOW_CLASS, 0, 0, 0, 0, 0, 0, 0, window_class.hInstance, None)
            notification = _register_display_state(hwnd)
            only_from_this_level(hwnd)
        except Exception:  # noqa: BLE001  without this window the app still works
            log.exception("power events: cannot listen; the LEDs are taken again only by the interval")
            _close_window(hwnd, notification)
            self._started.set()
            return
        self.hwnd = hwnd  # only a window that listens: stop() closes it
        self._started.set()
        log.info("power events: listening for display on and resume")
        try:
            win32gui.PumpMessages()
        finally:
            if notification:
                _user32().UnregisterPowerSettingNotification(notification)


def _close_window(hwnd: int | None, notification: int | None) -> None:
    if notification:
        _user32().UnregisterPowerSettingNotification(notification)
    if hwnd:
        import win32gui

        try:
            win32gui.DestroyWindow(hwnd)
        except Exception:  # noqa: BLE001  the window ends with its thread anyway
            log.debug("power events: the window could not be closed", exc_info=True)


def _user32() -> Any:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.RegisterPowerSettingNotification.restype = wintypes.HANDLE
    user32.RegisterPowerSettingNotification.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
    user32.UnregisterPowerSettingNotification.argtypes = [wintypes.HANDLE]
    return user32


# Messages that a process with lower rights must not send to this window of the admin
# app: a forged WM_POWERBROADCAST carries an address that the handler reads, and a
# WM_CLOSE or WM_ENDSESSION would end the watch or the app. The real ones come from
# Windows, or from this process.
FILTERED_MESSAGES = (WM_POWERBROADCAST, WM_CLOSE, WM_QUERYENDSESSION, WM_ENDSESSION)
MSGFLT_DISALLOW = 2


def only_from_this_level(hwnd: int, user32: Any = None) -> None:
    user32 = user32 or ctypes.WinDLL("user32", use_last_error=True)
    user32.ChangeWindowMessageFilterEx.restype = wintypes.BOOL
    user32.ChangeWindowMessageFilterEx.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.DWORD, ctypes.c_void_p]
    for message in FILTERED_MESSAGES:
        if not user32.ChangeWindowMessageFilterEx(hwnd, message, MSGFLT_DISALLOW, None):
            raise ctypes.WinError(ctypes.get_last_error())


def _register_display_state(hwnd: int) -> int | None:
    guid = (ctypes.c_ubyte * 16).from_buffer_copy(GUID_CONSOLE_DISPLAY_STATE.bytes_le)
    handle = _user32().RegisterPowerSettingNotification(hwnd, ctypes.byref(guid), DEVICE_NOTIFY_WINDOW_HANDLE)
    if not handle:
        log.warning("power events: no display-state events (error %s)", ctypes.get_last_error())
    return handle


def watch(on_wake: Callable[[], None], on_end_session: Callable[[], None] | None = None) -> Callable[[], None]:
    """Start listening. Returns the function that stops it. Not Windows: nothing to do.
    `on_end_session` runs at logoff or shutdown, before Windows ends the process."""
    if sys.platform != "win32":
        return lambda: None
    return PowerWatcher(on_wake, on_end_session).stop
