"""Graceful stop for a running daemon through a named Windows event.

Why: a killed process cannot release the LEDs. On 2026-11-08 a kill by
`Stop-ScheduledTask` left the ASUS lighting service in SDK control, and
Armoury Crate lost its devices until the service was restarted. `stop` sets
the event, the daemon releases every backend, then exits.

The daemon runs elevated, and `stop` can run without admin rights. So the
event gets an explicit DACL: SYSTEM and Administrators have full access, and
only the user who runs the daemon can signal it. Other users of the PC cannot.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

log = logging.getLogger(__name__)

# SYSTEM and Administrators: full access. The daemon's user: SYNCHRONIZE | EVENT_MODIFY_STATE.
EVENT_SDDL_TEMPLATE = "D:(A;;GA;;;SY)(A;;GA;;;BA)(A;;0x00100002;;;{user_sid})"
EVENT_MODIFY_STATE = 0x0002
SYNCHRONIZE = 0x00100000
ERROR_ACCESS_DENIED = 5
MANUAL_RESET = True
INITIALLY_SET = False


def watch(name: str, stop: threading.Event) -> Any:
    """Create the named event and set `stop` when someone signals it.

    Returns the event handle. Keep it alive as long as the daemon runs.
    If an elevated daemon already owns the event, a normal process (the
    configurator) cannot get full access. It then opens the event to wait only.
    """
    import pywintypes
    import win32api
    import win32con
    import win32event
    import win32security

    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    user_sid = win32security.ConvertSidToStringSid(
        win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    )
    attributes = win32security.SECURITY_ATTRIBUTES()
    attributes.SECURITY_DESCRIPTOR = (
        win32security.ConvertStringSecurityDescriptorToSecurityDescriptor(
            EVENT_SDDL_TEMPLATE.format(user_sid=user_sid), win32security.SDDL_REVISION_1
        )
    )
    try:
        handle = win32event.CreateEvent(attributes, MANUAL_RESET, INITIALLY_SET, name)
    except pywintypes.error as error:
        if error.winerror != ERROR_ACCESS_DENIED:
            raise
        handle = win32event.OpenEvent(SYNCHRONIZE, False, name)

    def wait() -> None:
        while not stop.is_set():
            if win32event.WaitForSingleObject(handle, win32event.INFINITE) == win32con.WAIT_OBJECT_0:
                log.info("stop requested through event %s", name)
                stop.set()

    threading.Thread(target=wait, name="stop-signal", daemon=True).start()
    return handle


def request(name: str) -> bool:
    """Signal a running daemon. Returns False when no daemon listens."""
    import pywintypes
    import win32event

    try:
        handle = win32event.OpenEvent(EVENT_MODIFY_STATE, False, name)
    except pywintypes.error:
        return False
    win32event.SetEvent(handle)
    return True
