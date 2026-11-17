"""Graceful stop for a running daemon through a named Windows event.

Why: a killed process cannot release the LEDs. A kill (for example by
`Stop-ScheduledTask`) can leave the ASUS lighting service in SDK control, and
Armoury Crate then loses its devices until the service restarts. `stop` sets
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


def watch(name: str, stop: threading.Event, reset: bool = False) -> Any:
    """Create the named event and set `stop` when someone signals it.

    `reset` (the daemon): the event is a manual-reset one, and a stop for an earlier
    daemon can still be set while another process holds the event. It is reset before
    the wait starts, so that only a stop for this daemon ends it.

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
        if reset:
            win32event.ResetEvent(handle)
    except pywintypes.error as error:
        if error.winerror != ERROR_ACCESS_DENIED:
            raise
        handle = None
        if reset:
            # The event of another daemon run (for example a standard-user install, where
            # the configurator made it): the right to change its state is enough to reset it.
            try:
                handle = win32event.OpenEvent(SYNCHRONIZE | EVENT_MODIFY_STATE, False, name)
                win32event.ResetEvent(handle)
            except pywintypes.error as open_error:
                if open_error.winerror != ERROR_ACCESS_DENIED:
                    raise
                handle = None
        if handle is None:
            handle = win32event.OpenEvent(SYNCHRONIZE, False, name)

    def wait() -> None:
        while not stop.is_set():
            if win32event.WaitForSingleObject(handle, win32event.INFINITE) == win32con.WAIT_OBJECT_0:
                log.info("stop requested through event %s", name)
                stop.set()

    threading.Thread(target=wait, name="stop-signal", daemon=True).start()
    return handle


ERROR_ACCESS_DENIED = 5  # the event exists, but for another account: a daemon runs
RUNNING_SUFFIX = ".Running"  # only `run` makes this event; `tune` also watches the stop event


def mark_running(name: str) -> Any:
    """Make the event that says "the daemon runs". Keep the handle while it runs.

    It only lets `devices` and `preview` see the app, so an error never stops
    the daemon: then it returns None and logs a warning.
    """
    import pywintypes
    import win32event

    try:
        return win32event.CreateEvent(None, True, False, name + RUNNING_SUFFIX)
    except pywintypes.error as error:
        log.warning("cannot make the event %s%s (%s): devices and preview cannot see this app",
                    name, RUNNING_SUFFIX, error.strerror)
        return None


def running(name: str) -> bool:
    """True while a daemon runs (`mark_running` of stop event `name`)."""
    import pywintypes
    import win32event

    try:
        handle = win32event.OpenEvent(SYNCHRONIZE, False, name + RUNNING_SUFFIX)
    except pywintypes.error as error:
        return error.winerror == ERROR_ACCESS_DENIED
    handle.Close()
    return True


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
