"""Tokens in the environment of the Windows user account (HKCU\\Environment).

Why: a token must not be in a config file. `setx` puts it into the user
environment, but a process gets the new value only after the next logon. The
app thus also reads the registry when its own environment does not have the
name. A token that the token tool saves works at the next app start, with no
logon.

The app runs as admin, but for the same user, so HKCU is the key of that user.
"""

from __future__ import annotations

import os
import sys

ENVIRONMENT_KEY = "Environment"
HWND_BROADCAST = 0xFFFF
WM_SETTINGCHANGE = 0x001A
SMTO_ABORTIFHUNG = 0x0002
BROADCAST_TIMEOUT_MS = 1000


def get(name: str) -> str | None:
    """The value of an environment variable, or of the user environment in the registry."""
    if name in os.environ:
        return os.environ[name]
    if sys.platform != "win32":
        return None
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, ENVIRONMENT_KEY) as key:
            value, _kind = winreg.QueryValueEx(key, name)
    except OSError:
        return None
    return str(value) if value else None


def put(name: str, value: str) -> None:
    """Save `value` in the user environment (the same as `setx`), and in this process."""
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, ENVIRONMENT_KEY, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
    os.environ[name] = value
    _announce()


def delete(name: str) -> bool:
    """Remove the name from the user environment. False: it was not there."""
    import winreg

    os.environ.pop(name, None)
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, ENVIRONMENT_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, name)
    except FileNotFoundError:
        return False
    _announce()
    return True


def _announce() -> None:
    """Tell Explorer that the environment changed: new windows get the value."""
    import ctypes

    result = ctypes.c_ulong()
    ctypes.windll.user32.SendMessageTimeoutW(HWND_BROADCAST, WM_SETTINGCHANGE, 0, ENVIRONMENT_KEY,
                                             SMTO_ABORTIFHUNG, BROADCAST_TIMEOUT_MS, ctypes.byref(result))
