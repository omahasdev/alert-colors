"""Backend workers and the Windows event watchers, with fakes.

No test keeps a real window, a real NotifyAddrChange call or a real process
snapshot alive: the Windows parts are fake modules and fake DLLs.
"""
import ctypes
import logging
import sys
import threading
import types
import uuid
from dataclasses import dataclass, field

import pytest

from alert_colors import network_events, outputs, power_events, process_events
from alert_colors.domain import Color
from alert_colors.effects import Solid
from alert_colors.frame import Frame
from alert_colors.outputs import BackendWorker

RED, BLUE = Color(255, 0, 0), Color(0, 0, 255)
WAIT_S = 2.0
STEP_S = 0.01
SHORT_S = 0.05
LONG_RETRY_S = 60.0
FAKE_HWND = 0x1234
FAKE_NOTIFICATION = 0x5678


def frame_of(color):
    return Frame(Solid(color), 0.0)


def wait_for(predicate):
    pause = threading.Event()
    for _ in range(int(WAIT_S / STEP_S)):
        if predicate():
            return True
        pause.wait(STEP_S)
    return predicate()


# ---- BackendWorker ----------------------------------------------------------
@dataclass
class Backend:
    name: str = "fake"
    calls: list = field(default_factory=list)
    gate: threading.Event | None = None
    fail_show: bool = False
    fail_reclaim: bool = False
    fail_release: bool = False

    def show(self, frame):
        if self.gate is not None:
            self.gate.wait(WAIT_S)
        self.calls.append(("show", frame.color()))
        if self.fail_show:
            raise OSError("device gone")

    def reclaim(self):
        self.calls.append(("reclaim", None))
        if self.fail_reclaim:
            raise OSError("vendor service busy")

    def release(self):
        self.calls.append(("release", None))
        if self.fail_release:
            raise OSError("cannot release")


def worker_for(backend, retry_s=LONG_RETRY_S, close_timeout_s=WAIT_S, **kwargs):
    return BackendWorker(backend, retry_s=retry_s, close_timeout_s=close_timeout_s, **kwargs)


def test_live_tuning_changes_colors_and_none_restores_the_config_values():
    backend = Backend()
    worker = worker_for(backend, brightness=0.5)
    grey = Color(200, 200, 200)
    worker.set_tuning(1.0, (1.0, 0.0, 1.0))
    worker.show(frame_of(grey))
    assert wait_for(lambda: len(backend.calls) == 1)
    worker.set_tuning(None, None)
    worker.show(frame_of(grey))
    assert wait_for(lambda: len(backend.calls) == 2)
    worker.close()
    assert backend.calls[:2] == [("show", Color(200, 0, 200)), ("show", Color(100, 100, 100))]


def test_release_command_reaches_the_backend():
    backend = Backend()
    worker = worker_for(backend)
    worker.release()
    assert wait_for(lambda: backend.calls)
    worker.close()
    assert backend.calls[0] == ("release", None)


def test_commands_after_close_are_dropped():
    backend = Backend()
    worker = worker_for(backend)
    worker.close()
    worker.show(frame_of(RED))
    worker.reclaim()
    worker.release()
    assert backend.calls == [("release", None)]  # only the release of close()
    assert worker._pending is None and not worker._reclaim_requested


def test_close_warns_when_the_backend_hangs(caplog):
    gate = threading.Event()
    backend = Backend(gate=gate)
    worker = worker_for(backend, close_timeout_s=SHORT_S)
    worker.show(frame_of(RED))
    assert wait_for(lambda: worker._pending is None)  # the backend now blocks on the gate
    with caplog.at_level(logging.WARNING, logger=outputs.__name__):
        worker.close()
    assert f"backend fake did not stop within {SHORT_S}s" in caplog.text
    gate.set()
    worker._thread.join(WAIT_S)
    assert not worker._thread.is_alive()
    assert backend.calls == [("show", RED), ("release", None)]


def test_failed_reclaim_skips_the_frame_and_releases():
    gate = threading.Event()
    backend = Backend(gate=gate, fail_reclaim=True)
    worker = worker_for(backend)
    worker.show(frame_of(RED))
    assert wait_for(lambda: worker._pending is None)  # the worker is in show(RED)
    worker.reclaim()
    worker.show(frame_of(BLUE))
    gate.set()
    assert wait_for(lambda: ("release", None) in backend.calls)
    worker.close()
    assert backend.calls[:3] == [("show", RED), ("reclaim", None), ("release", None)]
    assert ("show", BLUE) not in backend.calls


def test_a_failed_release_after_an_error_is_logged(caplog):
    backend = Backend(fail_show=True, fail_release=True)
    worker = worker_for(backend)
    with caplog.at_level(logging.WARNING, logger=outputs.__name__):
        worker.show(frame_of(RED))
        assert wait_for(lambda: "release failed" in caplog.text)
        worker.close()
    assert "backend fake release failed: cannot release" in caplog.text
    assert not worker._thread.is_alive()


# ---- power events -----------------------------------------------------------
def fake_win32(monkeypatch, fail_register=False):
    """Fake win32gui and win32api. PumpMessages returns at once."""
    record = {"posted": [], "pumped": 0}
    gui = types.ModuleType("win32gui")
    gui.WNDCLASS = types.SimpleNamespace

    def register_class(window_class):
        if fail_register:
            raise OSError("RegisterClass failed")
        record["window_class"] = window_class
        return 1

    gui.RegisterClass = register_class
    gui.CreateWindow = lambda *args: FAKE_HWND
    gui.PumpMessages = lambda: record.__setitem__("pumped", record["pumped"] + 1)
    gui.PostMessage = lambda *args: record["posted"].append(args)
    api = types.ModuleType("win32api")
    api.GetModuleHandle = lambda name: 0
    monkeypatch.setitem(sys.modules, "win32gui", gui)
    monkeypatch.setitem(sys.modules, "win32api", api)
    record["filtered"] = []
    monkeypatch.setattr(power_events, "only_from_this_level", record["filtered"].append)
    return record


class FakeUser32:
    def __init__(self, handle):
        self.handle = handle
        self.unregistered = []
        self.RegisterPowerSettingNotification = lambda hwnd, guid, flags: self.handle
        self.UnregisterPowerSettingNotification = self.unregistered.append


def test_watcher_unregisters_the_display_notification_after_the_message_loop(monkeypatch, caplog):
    record = fake_win32(monkeypatch)
    user32 = FakeUser32(FAKE_NOTIFICATION)
    monkeypatch.setattr(power_events, "_user32", lambda: user32)
    with caplog.at_level(logging.INFO, logger=power_events.__name__):
        watcher = power_events.PowerWatcher(lambda: None)
        watcher._thread.join(WAIT_S)
    assert watcher.hwnd == FAKE_HWND and record["pumped"] == 1
    assert record["filtered"] == [FAKE_HWND]  # before the message loop
    assert user32.unregistered == [FAKE_NOTIFICATION]
    assert "power events: listening for display on and resume" in caplog.text
    handlers = record["window_class"].lpfnWndProc
    assert set(handlers) == {power_events.WM_POWERBROADCAST, power_events.WM_CLOSE,
                             power_events.WM_QUERYENDSESSION, power_events.WM_ENDSESSION}
    watcher.stop()
    assert record["posted"] == []  # the loop ended already: nothing to close


def test_no_display_notification_is_a_warning_and_nothing_to_unregister(monkeypatch, caplog):
    fake_win32(monkeypatch)
    user32 = FakeUser32(None)
    monkeypatch.setattr(power_events, "_user32", lambda: user32)
    with caplog.at_level(logging.WARNING, logger=power_events.__name__):
        watcher = power_events.PowerWatcher(lambda: None)
        watcher._thread.join(WAIT_S)
    assert "power events: no display-state events" in caplog.text
    assert user32.unregistered == []


def test_a_window_error_is_logged_and_stop_has_nothing_to_do(monkeypatch, caplog):
    record = fake_win32(monkeypatch, fail_register=True)
    with caplog.at_level(logging.ERROR, logger=power_events.__name__):
        watcher = power_events.PowerWatcher(lambda: None)
    watcher._thread.join(WAIT_S)
    assert "power events: cannot listen" in caplog.text
    assert watcher.hwnd is None
    watcher.stop()
    assert record["posted"] == []


def test_a_slow_window_start_is_a_warning(monkeypatch, caplog):
    release = threading.Event()
    monkeypatch.setattr(power_events, "STARTUP_TIMEOUT_S", SHORT_S)
    monkeypatch.setattr(power_events.PowerWatcher, "_run", lambda self: release.wait(WAIT_S))
    with caplog.at_level(logging.WARNING, logger=power_events.__name__):
        watcher = power_events.PowerWatcher(lambda: None)
    release.set()
    watcher._thread.join(WAIT_S)
    assert f"the window did not start within {SHORT_S}s" in caplog.text


def bare_power_watcher(on_wake):
    watcher = power_events.PowerWatcher.__new__(power_events.PowerWatcher)
    watcher._on_wake = on_wake
    return watcher


def test_an_other_power_event_does_not_wake():
    calls = []
    watcher = bare_power_watcher(lambda: calls.append(1))
    power_status_change = 0x000A
    assert watcher._on_power(FAKE_HWND, power_events.WM_POWERBROADCAST, power_status_change, 0) \
        is power_events.HANDLED
    assert calls == []


def test_a_failed_wake_handler_is_logged_and_the_message_is_handled(caplog):
    def broken():
        raise RuntimeError("daemon gone")

    watcher = bare_power_watcher(broken)
    with caplog.at_level(logging.ERROR, logger=power_events.__name__):
        result = watcher._on_power(FAKE_HWND, power_events.WM_POWERBROADCAST,
                                   power_events.PBT_APMRESUMEAUTOMATIC, 0)
    assert result is power_events.HANDLED
    assert "power events: the handler failed" in caplog.text


def test_power_watch_off_windows_does_nothing(monkeypatch):
    monkeypatch.setattr(power_events.sys, "platform", "linux")
    monkeypatch.setattr(power_events, "PowerWatcher", None)  # never created
    power_events.watch(lambda: None)()


def test_power_watch_on_windows_returns_the_stop_of_the_watcher(monkeypatch):
    stops = []

    class FakeWatcher:
        def __init__(self, on_wake, on_end_session=None):
            self.on_wake = on_wake

        def stop(self):
            stops.append(self.on_wake)

    def on_wake():
        return None

    monkeypatch.setattr(power_events.sys, "platform", "win32")
    monkeypatch.setattr(power_events, "PowerWatcher", FakeWatcher)
    power_events.watch(on_wake)()
    assert stops == [on_wake]


# ---- process events ---------------------------------------------------------
ARMOURY = "ArmouryCrate.exe"
NEW_PID = 200


def test_the_watcher_thread_reports_a_new_process_and_survives_a_failed_snapshot(caplog):
    started = threading.Event()
    calls = {"count": 0}

    def snapshot():
        calls["count"] += 1
        if calls["count"] == 2:
            raise OSError("snapshot failed")
        return [] if calls["count"] == 1 else [(NEW_PID, ARMOURY)]

    with caplog.at_level(logging.ERROR, logger=process_events.__name__):
        watcher = process_events.ProcessWatcher([ARMOURY], STEP_S, lambda name: started.set(),
                                                snapshot=snapshot)
        assert started.wait(WAIT_S)
        watcher.stop()
    assert not watcher._thread.is_alive()
    assert "process events: the check failed" in caplog.text


class FakeKernel32:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.closed = []
        self.CreateToolhelp32Snapshot = lambda flags, pid: self.snapshot
        self.Process32FirstW = lambda handle, entry: False
        self.CloseHandle = self.closed.append


@pytest.mark.skipif(sys.platform != "win32", reason="ctypes.WinError is Windows only")
@pytest.mark.parametrize("bad_handle", [None, process_events.INVALID_HANDLE_VALUE])
def test_a_failed_process_snapshot_raises_and_closes_nothing(monkeypatch, bad_handle):
    kernel32 = FakeKernel32(bad_handle)
    monkeypatch.setattr(process_events, "_kernel32", lambda: kernel32)
    with pytest.raises(OSError):
        process_events.list_processes()
    assert kernel32.closed == []


def test_an_empty_process_snapshot_is_closed(monkeypatch):
    kernel32 = FakeKernel32(FAKE_HWND)
    monkeypatch.setattr(process_events, "_kernel32", lambda: kernel32)
    assert process_events.list_processes() == []
    assert kernel32.closed == [FAKE_HWND]


def test_process_watch_on_windows_returns_the_stop_of_the_watcher(monkeypatch):
    stops = []

    class FakeWatcher:
        def __init__(self, names, poll_s, on_start):
            self.args = (list(names), poll_s)

        def stop(self):
            stops.append(self.args)

    monkeypatch.setattr(process_events.sys, "platform", "win32")
    monkeypatch.setattr(process_events, "ProcessWatcher", FakeWatcher)
    process_events.watch([ARMOURY], SHORT_S, lambda name: None)()
    assert stops == [([ARMOURY], SHORT_S)]


def test_process_watch_that_cannot_start_is_logged_and_returns_a_no_op(monkeypatch, caplog):
    def broken(names, poll_s, on_start):
        raise OSError("no snapshot")

    monkeypatch.setattr(process_events.sys, "platform", "win32")
    monkeypatch.setattr(process_events, "ProcessWatcher", broken)
    with caplog.at_level(logging.ERROR, logger=process_events.__name__):
        stop = process_events.watch([ARMOURY], SHORT_S, lambda name: None)
    stop()
    assert "process events: cannot watch" in caplog.text


# ---- network events ---------------------------------------------------------
def test_notify_addr_change_calls_the_api_with_null_arguments(monkeypatch):
    calls = []

    class FakeNotify:
        def __call__(self, handle, overlapped):
            calls.append((handle, overlapped))
            return network_events.NO_ERROR

    notify = FakeNotify()
    loaded = []

    def win_dll(name):
        loaded.append(name)
        return types.SimpleNamespace(NotifyAddrChange=notify)

    monkeypatch.setattr(network_events.ctypes, "WinDLL", win_dll)
    wait = network_events._notify_addr_change()
    assert loaded == ["iphlpapi"]
    assert notify.restype is ctypes.wintypes.DWORD and len(notify.argtypes) == 2
    assert wait() == network_events.NO_ERROR
    assert calls == [(None, None)]


def test_stop_in_the_handler_ends_the_network_thread():
    calls = []
    holder = {}
    gate = threading.Event()

    def wait_for_change():
        gate.wait(WAIT_S)  # the first change comes after `holder` has the stop function
        return network_events.NO_ERROR

    def on_change():
        calls.append(1)
        holder["stop"]()

    watcher = network_events.NetworkWatcher(on_change, wait_for_change)
    holder["stop"] = watcher.stop
    gate.set()
    watcher._thread.join(WAIT_S)
    assert not watcher._thread.is_alive()
    assert calls == [1]  # no wait and no call after the stop


def test_network_watch_off_windows_without_a_wait_does_nothing(monkeypatch):
    monkeypatch.setattr(network_events.sys, "platform", "linux")
    # pytest.fail raises a BaseException, which the except Exception of watch() does not hide.
    monkeypatch.setattr(network_events, "_notify_addr_change", lambda: pytest.fail("the Windows call was made"))
    network_events.watch(lambda: None)()


def test_a_failed_wait_ends_the_watcher_with_no_busy_loop():
    """A non-zero result of NotifyAddrChange ends the thread: it never calls the wait again."""
    calls = []

    def failing_wait():
        calls.append(1)
        return 1  # an error code

    watcher = network_events.NetworkWatcher(lambda: pytest.fail("no change happened"), failing_wait)
    watcher._thread.join(WAIT_S)
    assert not watcher._thread.is_alive()
    assert calls == [1]



# ---- the message filter of the power window ---------------------------------------------
def test_the_power_window_refuses_the_filtered_messages_from_processes_with_lower_rights():
    calls = []

    class Filter:
        def __init__(self):
            self.ChangeWindowMessageFilterEx = self

        def __call__(self, hwnd, message, action, extra):
            calls.append((hwnd, message, action))
            return 1

    power_events.only_from_this_level(FAKE_HWND, user32=Filter())
    assert calls == [(FAKE_HWND, message, power_events.MSGFLT_DISALLOW)
                     for message in (power_events.WM_POWERBROADCAST, power_events.WM_CLOSE,
                                     power_events.WM_QUERYENDSESSION, power_events.WM_ENDSESSION)]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows windows")
def test_the_message_filter_works_on_a_real_window_and_its_own_close_still_arrives():
    import win32api
    import win32gui

    closed = []
    window_class = win32gui.WNDCLASS()
    window_class.lpszClassName = f"AlertColorsFilterTest.{uuid.uuid4().hex}"
    window_class.hInstance = win32api.GetModuleHandle(None)
    window_class.lpfnWndProc = {power_events.WM_CLOSE: lambda *args: closed.append(1) or 0}
    atom = win32gui.RegisterClass(window_class)
    hwnd = win32gui.CreateWindow(atom, "test", 0, 0, 0, 0, 0, 0, 0, window_class.hInstance, None)
    try:
        power_events.only_from_this_level(hwnd)  # raises when Windows refuses the call
        win32gui.SendMessage(hwnd, power_events.WM_CLOSE, 0, 0)  # this process: not filtered
        assert closed == [1]
    finally:
        win32gui.DestroyWindow(hwnd)
        win32gui.UnregisterClass(atom, window_class.hInstance)



def test_stop_closes_a_window_whose_loop_still_runs(monkeypatch):
    record = fake_win32(monkeypatch)
    monkeypatch.setattr(power_events, "_user32", lambda: FakeUser32(FAKE_NOTIFICATION))
    closed = threading.Event()
    gui = sys.modules["win32gui"]
    gui.PumpMessages = lambda: closed.wait(WAIT_S)
    gui.PostMessage = lambda *args: record["posted"].append(args) or closed.set()
    watcher = power_events.PowerWatcher(lambda: None)
    watcher.stop()
    assert record["posted"] == [(FAKE_HWND, power_events.WM_CLOSE, 0, 0)]
    assert not watcher._thread.is_alive()


def test_a_failed_message_filter_leaves_no_window_to_close(monkeypatch, caplog):
    """The window ends with its thread: stop() must not post to a dead handle."""
    record = fake_win32(monkeypatch)
    user32 = FakeUser32(FAKE_NOTIFICATION)
    monkeypatch.setattr(power_events, "_user32", lambda: user32)
    destroyed = []
    sys.modules["win32gui"].DestroyWindow = destroyed.append

    def refuse(hwnd):
        raise OSError("ChangeWindowMessageFilterEx failed")

    monkeypatch.setattr(power_events, "only_from_this_level", refuse)
    with caplog.at_level(logging.ERROR, logger=power_events.__name__):
        watcher = power_events.PowerWatcher(lambda: None)
        watcher._thread.join(WAIT_S)
    assert watcher.hwnd is None and destroyed == [FAKE_HWND]
    assert user32.unregistered == [FAKE_NOTIFICATION]
    assert "cannot listen" in caplog.text
    watcher.stop()
    assert record["posted"] == []


def test_a_refused_message_filter_call_raises():
    class Filter:
        def __init__(self):
            self.ChangeWindowMessageFilterEx = lambda *args: 0

    with pytest.raises(OSError):
        power_events.only_from_this_level(FAKE_HWND, user32=Filter())



def test_the_end_of_the_session_runs_the_handler_before_windows_ends_the_process(monkeypatch):
    record = fake_win32(monkeypatch)
    monkeypatch.setattr(power_events, "_user32", lambda: FakeUser32(FAKE_NOTIFICATION))
    ended = []
    watcher = power_events.PowerWatcher(lambda: None, on_end_session=lambda: ended.append(1))
    watcher._thread.join(WAIT_S)
    handlers = record["window_class"].lpfnWndProc
    assert handlers[power_events.WM_QUERYENDSESSION](FAKE_HWND, power_events.WM_QUERYENDSESSION, 0, 0) is True
    handlers[power_events.WM_ENDSESSION](FAKE_HWND, power_events.WM_ENDSESSION, 0, 0)  # the session goes on
    assert ended == []
    handlers[power_events.WM_ENDSESSION](FAKE_HWND, power_events.WM_ENDSESSION, 1, 0)  # it ends
    assert ended == [1]
