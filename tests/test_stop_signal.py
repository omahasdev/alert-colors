"""The stop event: a normal process can watch an event that another owner restricts."""
import threading
import uuid

import pytest

win32event = pytest.importorskip("win32event")
import win32security  # noqa: E402

from alert_colors import stop_signal  # noqa: E402

WAIT_S = 5


def _restricted_event(name):
    """An event as the elevated daemon makes it: this user may only wait and signal."""
    import win32api
    import win32con

    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    sid = win32security.ConvertSidToStringSid(win32security.GetTokenInformation(token, win32security.TokenUser)[0])
    attributes = win32security.SECURITY_ATTRIBUTES()
    attributes.SECURITY_DESCRIPTOR = win32security.ConvertStringSecurityDescriptorToSecurityDescriptor(
        f"D:(A;;0x00100002;;;{sid})", win32security.SDDL_REVISION_1)
    return win32event.CreateEvent(attributes, True, False, name)


def test_watch_opens_an_event_that_refuses_full_access():
    name = f"Local\AlertColors.Test.{uuid.uuid4().hex}"
    owner = _restricted_event(name)  # noqa: F841  keep the event alive
    stop = threading.Event()
    handle = stop_signal.watch(name, stop)  # noqa: F841
    assert stop_signal.request(name)
    assert stop.wait(WAIT_S)
