"""Take the LEDs again after a vendor app took them (display on, resume, interval)."""
import ctypes
import sys
import threading
import time
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest

from alert_colors import power_events
from alert_colors.config import App
from alert_colors.domain import Color
from alert_colors.effects import Solid
from alert_colors.frame import Frame
from alert_colors.lighting.aura_sdk import AuraSdkBackend
from alert_colors.outputs import BackendWorker
from tests_helpers import FakeOutput

ROOT = Path(__file__).resolve().parents[1]
RAW = tomllib.loads((ROOT / "config.toml").read_text(encoding="utf-8"))
WAIT_S = 2.0
RETRY_S = 60.0
CLOSE_S = 5.0
INTERVAL_S, AFTER_WAKE_S, STEP_S = 300.0, 60.0, 5.0
RED = Color(255, 0, 0)
SETTLE_S = 0.05  # lets the worker enter the first show
POLL_S = 0.01


# ---- the daemon schedule ----------------------------------------------------
def make_daemon():
    clock = {"t": 0.0}
    daemon = App(RAW, ROOT).daemon(dry_run=True)
    daemon.outputs = [FakeOutput("aura_sdk")]
    daemon.tuning = None
    daemon.settings = replace(daemon.settings, reclaim_interval_s=INTERVAL_S,
                              reclaim_after_wake_s=AFTER_WAKE_S, reclaim_step_s=STEP_S)
    daemon.monotonic = lambda: clock["t"]
    return daemon, clock


def reclaims_at(daemon, clock, times):
    for t in times:
        clock["t"] = t
        daemon.step()
    return daemon.outputs[0].reclaims


def test_no_reclaim_without_a_reason():
    daemon, clock = make_daemon()
    assert reclaims_at(daemon, clock, [0, 1, 100, INTERVAL_S - 1]) == 0


def test_the_interval_reclaims_at_any_time():
    daemon, clock = make_daemon()
    assert reclaims_at(daemon, clock, [0, INTERVAL_S, INTERVAL_S + 1]) == 1


def test_a_wake_event_reclaims_each_step_for_the_window():
    daemon, clock = make_daemon()
    reclaims_at(daemon, clock, [0, 10])
    daemon.request_reclaim()  # at t = 10: the window lasts until 70
    steps = [10 + STEP_S * i for i in range(1, 20)]  # 15 ... 105
    in_window = sum(1 for t in steps if t < 10 + AFTER_WAKE_S)
    assert reclaims_at(daemon, clock, steps) == in_window


def test_the_shipped_config_has_the_keys():
    settings = App(RAW, ROOT).settings()
    assert settings.reclaim_after_wake_s > settings.reclaim_step_s > 0
    assert settings.reclaim_interval_s > settings.reclaim_after_wake_s


# ---- the worker: a reclaim is never replaced by a newer frame -----------------
@dataclass
class Recorder:
    name: str = "rec"
    calls: list = field(default_factory=list)
    done: threading.Event = field(default_factory=threading.Event)

    def show(self, frame):
        self.calls.append("show")
        self.done.set()

    def reclaim(self):
        self.calls.append("reclaim")

    def release(self):
        self.calls.append("release")


def test_the_worker_runs_the_reclaim_before_the_newest_frame():
    backend = Recorder()
    gate = threading.Event()
    original_show = backend.show
    backend.show = lambda frame: (gate.wait(WAIT_S), original_show(frame))
    worker = BackendWorker(backend, retry_s=RETRY_S, close_timeout_s=CLOSE_S)
    worker.show(Frame(Solid(RED), 0.0))  # the worker waits in this show
    time.sleep(SETTLE_S)
    worker.reclaim()
    worker.show(Frame(Solid(RED), 0.0))  # replaces nothing but the pending frame
    gate.set()
    deadline = time.monotonic() + WAIT_S
    while backend.calls.count("show") < 2 and time.monotonic() < deadline:
        time.sleep(POLL_S)
    worker.close()
    assert backend.calls[:3] == ["show", "reclaim", "show"]


# ---- the Aura backend -------------------------------------------------------
class FakeLight:
    Color = 0


class FakeLights:
    def __init__(self, count):
        self.items = [FakeLight() for _ in range(count)]
        self.Count = count

    def Item(self, i):  # noqa: N802  COM name
        return self.items[i]


class FakeDevice:
    def __init__(self, name, count):
        self.Name, self.Lights, self.applies = name, FakeLights(count), 0

    def Apply(self):  # noqa: N802
        self.applies += 1


class FakeSdk:
    switches = 0

    def SwitchMode(self):  # noqa: N802
        self.switches += 1


def test_aura_reclaim_switches_mode_and_sends_unchanged_colors_again():
    device = FakeDevice("Mainboard", 4)
    sdk = FakeSdk()
    backend = AuraSdkBackend(include=["*"], _sdk=sdk, _devices=[device], _controlling=True)
    frame = Frame(Solid(RED), 0.0)
    backend.show(frame)
    backend.show(frame)
    assert device.applies == 1  # unchanged colors: no second apply
    backend.reclaim()
    backend.show(frame)
    assert (sdk.switches, device.applies) == (1, 2)


def test_aura_reclaim_without_control_does_nothing():
    sdk = FakeSdk()
    AuraSdkBackend(include=["*"], _sdk=sdk).reclaim()
    assert sdk.switches == 0


# ---- the Windows events -----------------------------------------------------
def display_setting(guid, state):
    setting = power_events._PowerSetting()
    ctypes.memmove(setting.power_setting, guid.bytes_le, len(guid.bytes_le))
    setting.data_length, setting.data = ctypes.sizeof(ctypes.c_uint32), state
    return setting


@pytest.mark.parametrize(("event", "state", "expected"), [
    (power_events.PBT_POWERSETTINGCHANGE, power_events.DISPLAY_ON, True),
    (power_events.PBT_POWERSETTINGCHANGE, 0, False),  # display off
    (power_events.PBT_POWERSETTINGCHANGE, 2, False),  # dimmed
    (power_events.PBT_APMRESUMEAUTOMATIC, None, True),
    (power_events.PBT_APMRESUMESUSPEND, None, True),
    (0x000A, None, False),  # PBT_POWERSTATUSCHANGE: on battery or AC
])
def test_is_wake(event, state, expected):
    setting = display_setting(power_events.GUID_CONSOLE_DISPLAY_STATE, state) if state is not None else None
    address = ctypes.addressof(setting) if setting is not None else 0
    assert power_events.is_wake(event, address) is expected


def test_an_other_power_setting_is_not_a_wake():
    import uuid

    setting = display_setting(uuid.uuid4(), power_events.DISPLAY_ON)
    assert not power_events.is_wake(power_events.PBT_POWERSETTINGCHANGE, ctypes.addressof(setting))


@pytest.mark.skipif(sys.platform != "win32", reason="Windows messages")
def test_the_window_gets_a_resume_broadcast():
    import win32gui

    woke = threading.Event()
    watcher = power_events.PowerWatcher(woke.set)
    assert watcher.hwnd
    woke.clear()  # Windows sends the current display state at once
    win32gui.SendMessage(watcher.hwnd, power_events.WM_POWERBROADCAST, power_events.PBT_APMRESUMEAUTOMATIC, 0)
    assert woke.wait(WAIT_S)
    watcher.stop()
