"""The stop event: the error paths of stop_signal.py."""
import threading
import uuid

import pytest

win32event = pytest.importorskip("win32event")
import pywintypes  # noqa: E402
import win32con  # noqa: E402

from alert_colors import stop_signal  # noqa: E402

WAIT_S = 5
ERROR_FILE_NOT_FOUND = 2


def test_request_without_a_running_daemon_returns_false():
    assert stop_signal.request(f"Local\\AlertColors.Test.{uuid.uuid4().hex}") is False


def test_watch_raises_an_error_that_is_not_access_denied(monkeypatch):
    def fail(*args):
        raise pywintypes.error(ERROR_FILE_NOT_FOUND, "CreateEvent", "not found")

    monkeypatch.setattr(win32event, "CreateEvent", fail)
    with pytest.raises(pywintypes.error) as error:
        stop_signal.watch(f"Local\\AlertColors.Test.{uuid.uuid4().hex}", threading.Event())
    assert error.value.winerror == ERROR_FILE_NOT_FOUND


def test_watch_waits_again_when_the_wait_ends_without_the_signal(monkeypatch):
    """A wait result other than WAIT_OBJECT_0 does not stop the daemon: the thread waits again."""
    results = [win32con.WAIT_ABANDONED, win32con.WAIT_OBJECT_0]
    calls = []

    def fake_wait(handle, timeout):
        calls.append(timeout)
        return results.pop(0)

    monkeypatch.setattr(win32event, "WaitForSingleObject", fake_wait)
    stop = threading.Event()
    handle = stop_signal.watch(f"Local\\AlertColors.Test.{uuid.uuid4().hex}", stop)  # noqa: F841
    assert stop.wait(WAIT_S)
    assert len(calls) == 2


def test_a_stop_for_an_earlier_daemon_does_not_end_the_new_one():
    """Another process can still hold the event in its signaled state (for example the configurator)."""
    name = f"Local\\AlertColors.Test.{uuid.uuid4().hex}"
    held = win32event.CreateEvent(None, True, True, name)  # signaled, as after a stop
    try:
        stop = threading.Event()
        handle = stop_signal.watch(name, stop, reset=True)  # noqa: F841  keep alive
        assert not stop.wait(0.3)
        assert stop_signal.request(name)
        assert stop.wait(5)
        unreset = threading.Event()
        stop_signal.watch(name, unreset)  # the configurator: it does not reset
        assert unreset.wait(5)
    finally:
        held.Close()


def test_a_daemon_without_admin_rights_also_resets_a_stop_for_an_earlier_daemon():
    """The configurator made the event, and the account has only the rights to wait and to
    change its state (a standard-user install): the reset still works."""
    import win32api
    import win32security

    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    sid = win32security.ConvertSidToStringSid(win32security.GetTokenInformation(token, win32security.TokenUser)[0])
    attributes = win32security.SECURITY_ATTRIBUTES()
    attributes.SECURITY_DESCRIPTOR = win32security.ConvertStringSecurityDescriptorToSecurityDescriptor(
        f"D:(A;;0x00100002;;;{sid})", win32security.SDDL_REVISION_1)
    name = f"Local\\AlertColors.Test.{uuid.uuid4().hex}"
    held = win32event.CreateEvent(attributes, True, True, name)  # signaled, as after a stop
    try:
        stop = threading.Event()
        handle = stop_signal.watch(name, stop, reset=True)  # noqa: F841  keep alive
        assert not stop.wait(0.3)
        assert stop_signal.request(name)
        assert stop.wait(5)
    finally:
        held.Close()
