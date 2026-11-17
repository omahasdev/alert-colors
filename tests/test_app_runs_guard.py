"""`devices` and `preview` do not run beside the running app (a second Aura SDK
client can hang the device scan of the app)."""
import threading
import uuid

import pytest

from alert_colors import cli, stop_signal
from alert_colors.stop_signal import mark_running as real_mark_running  # before the autouse fakes
from alert_colors.stop_signal import running as real_running


class FakeApp:
    def __init__(self):
        self.used = []

    def stop_event_name(self):
        return r"Local\AlertColors.Test"

    def backends(self):
        self.used.append("backends")
        return []

    def scenes(self):
        self.used.append("scenes")
        return {}


@pytest.mark.parametrize("handler", [cli._devices, cli._preview])
def test_the_command_refuses_while_the_app_runs(monkeypatch, capsys, handler):
    monkeypatch.setattr(stop_signal, "running", lambda name: True)
    app = FakeApp()
    assert handler(app, None) == cli.EXIT_APP_RUNS
    assert app.used == []  # no SDK was touched
    assert "the app runs" in capsys.readouterr().out


def test_devices_runs_when_no_app_runs(capsys):
    app = FakeApp()
    assert cli._devices(app, None) == 0
    assert app.used == ["backends"]


def test_running_sees_the_real_running_event_of_run():
    name = rf"Local\AlertColors.Test.{uuid.uuid4().hex}"
    assert not real_running(name)
    handle = real_mark_running(name)
    try:
        assert real_running(name)
    finally:
        handle.Close()
    assert not real_running(name)


def test_an_open_configurator_is_not_a_running_app():
    """`tune` watches the stop event too; only `run` makes the running event."""
    name = rf"Local\AlertColors.Test.{uuid.uuid4().hex}"
    stop = threading.Event()
    handle = stop_signal.watch(name, stop)
    try:
        assert not real_running(name)
    finally:
        stop_signal.request(name)
        handle.Close()


def test_an_event_of_another_account_counts_as_running(monkeypatch):
    import pywintypes
    import win32event

    def denied(*_args):
        raise pywintypes.error(stop_signal.ERROR_ACCESS_DENIED, "OpenEvent", "Access is denied.")

    monkeypatch.setattr(win32event, "OpenEvent", denied)
    assert real_running(r"Local\AlertColors.Other")
