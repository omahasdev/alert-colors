"""Lighting backends with fake vendor SDKs: requests, colors, release and logs.

No test talks to the real Aura COM object, the real Chroma REST server or LEDs.
"""
import importlib.abc
import importlib.util
import logging
import sys
import types

import pytest

from alert_colors.domain import Color
from alert_colors.effects import Gradient, Solid
from alert_colors.frame import Frame
from alert_colors.lighting import BACKENDS, aura_sdk, console, razer_chroma
from alert_colors.lighting.aura_sdk import AuraSdkBackend
from alert_colors.lighting.console import ConsoleBackend
from alert_colors.lighting.razer_chroma import RazerChromaBackend

RED, BLUE = Color(255, 0, 0), Color(0, 0, 255)
BASE_URL = "http://localhost:54235/razer/chromasdk"
SESSION_URI = "http://localhost:54236/chromasdk"
SDK_VERSION = "3.0.0"
INIT_DELAY_S = 1.5
TIMEOUT_S = 2.0
NEVER_S = 3600.0  # a heartbeat interval that never ends in a test
AT_ONCE_S = 0.0  # a heartbeat interval that ends at once
TEST_LIGHTS = 4


def solid(color):
    return Frame(Solid(color), 0.0)


def gradient():
    return Frame(Gradient((RED, BLUE), period_s=1.0, repeats=1.0, direction=1, brightness=1.0,
                          moving=False), 0.0)


# ---- Razer Chroma -----------------------------------------------------------
class FakeChroma:
    """Records each REST call; answers like the Chroma SDK server."""

    def __init__(self, fail_delete=False):
        self.calls = []
        self.fail_delete = fail_delete

    def __call__(self, url, method="GET", body=None, timeout_s=None):
        self.calls.append((method, url, body, timeout_s))
        if method == "POST":
            return {"uri": SESSION_URI}
        if method == "DELETE" and self.fail_delete:
            raise OSError("Chroma server gone")
        if method == "GET":
            return {"version": SDK_VERSION}
        return {"result": 0}

    def methods(self):
        return [(method, url) for method, url, _, _ in self.calls]


@pytest.fixture
def chroma(monkeypatch):
    server = FakeChroma()
    sleeps = []
    monkeypatch.setattr(razer_chroma, "local_request_json", server)
    monkeypatch.setattr(razer_chroma.time, "sleep", sleeps.append)
    server.sleeps = sleeps
    return server


def razer(devices=("mouse",), heartbeat_s=NEVER_S):
    return RazerChromaBackend(url=BASE_URL, devices=list(devices), app_title="Alert Colors",
                              heartbeat_interval_s=heartbeat_s, init_delay_s=INIT_DELAY_S,
                              timeout_s=TIMEOUT_S)


def static(color):
    return {"effect": razer_chroma.STATIC_EFFECT, "param": {"color": color.to_bgr_int()}}


def test_razer_first_show_opens_a_session_waits_then_sends_static_colors(chroma, caplog):
    backend = razer(devices=("mouse", "headset"))
    with caplog.at_level(logging.INFO, logger=razer_chroma.__name__):
        backend.show(solid(RED))
    method, url, app, timeout = chroma.calls[0]
    assert (method, url, timeout) == ("POST", BASE_URL, TIMEOUT_S)
    assert app["title"] == "Alert Colors" and app["device_supported"] == ["mouse", "headset"]
    assert chroma.sleeps == [INIT_DELAY_S]
    assert chroma.calls[1:] == [("PUT", f"{SESSION_URI}/mouse", static(RED), TIMEOUT_S),
                                ("PUT", f"{SESSION_URI}/headset", static(RED), TIMEOUT_S)]
    assert f"Razer Chroma session opened: {SESSION_URI}" in caplog.text


def test_razer_unchanged_color_sends_nothing_before_the_heartbeat(chroma):
    backend = razer(heartbeat_s=NEVER_S)
    backend.show(solid(RED))
    backend.show(solid(RED))
    assert chroma.methods().count(("POST", BASE_URL)) == 1  # one session for all frames
    assert len(chroma.calls) == 2


def test_razer_unchanged_color_sends_a_heartbeat_after_the_interval(chroma):
    backend = razer(heartbeat_s=AT_ONCE_S)
    backend.show(solid(RED))
    backend.show(solid(RED))
    assert chroma.methods()[-1] == ("PUT", f"{SESSION_URI}/heartbeat")


def test_razer_new_color_is_sent_and_no_heartbeat_is_added(chroma):
    backend = razer(heartbeat_s=AT_ONCE_S)
    backend.show(solid(RED))
    backend.show(solid(BLUE))
    assert chroma.calls[-1][2] == static(BLUE)
    assert ("PUT", f"{SESSION_URI}/heartbeat") not in chroma.methods()


def test_razer_spatial_frame_is_a_grid_on_the_keyboard_and_one_color_elsewhere(chroma):
    backend = razer(devices=(razer_chroma.KEYBOARD, "mouse"))
    frame = gradient()
    backend.show(frame)
    keyboard, mouse = chroma.calls[1][2], chroma.calls[2][2]
    row = [c.to_bgr_int() for c in frame.colors(razer_chroma.KEYBOARD_COLUMNS)]
    assert keyboard == {"effect": razer_chroma.CUSTOM_EFFECT, "param": [row] * razer_chroma.KEYBOARD_ROWS}
    assert mouse == static(frame.color())


def test_razer_uniform_frame_is_static_on_the_keyboard_too(chroma):
    razer(devices=(razer_chroma.KEYBOARD,)).show(solid(BLUE))
    assert chroma.calls[-1][2] == static(BLUE)


def test_razer_reclaim_sends_the_same_effect_again(chroma):
    backend = razer()
    backend.show(solid(RED))
    backend.reclaim()
    backend.show(solid(RED))
    assert chroma.methods().count(("PUT", f"{SESSION_URI}/mouse")) == 2


def test_razer_release_deletes_the_session_and_the_next_show_opens_a_new_one(chroma, caplog):
    backend = razer()
    backend.show(solid(RED))
    with caplog.at_level(logging.INFO, logger=razer_chroma.__name__):
        backend.release()
    assert chroma.calls[-1] == ("DELETE", SESSION_URI, None, TIMEOUT_S)
    assert "Razer Chroma session closed" in caplog.text
    backend.show(solid(RED))
    assert chroma.methods()[-2:] == [("POST", BASE_URL), ("PUT", f"{SESSION_URI}/mouse")]


def test_razer_release_without_a_session_sends_nothing(chroma):
    razer().release()
    assert chroma.calls == []


def test_razer_release_forgets_the_session_when_delete_fails(chroma):
    backend = razer()
    backend.show(solid(RED))
    chroma.fail_delete = True
    with pytest.raises(OSError):
        backend.release()
    backend.release()  # the session is gone: no second DELETE
    assert chroma.methods().count(("DELETE", SESSION_URI)) == 1


def test_razer_describe_gives_the_sdk_version_and_the_devices(chroma):
    lines = razer(devices=("mouse", "keyboard")).describe()
    assert lines == [f"Chroma SDK {SDK_VERSION} at {BASE_URL}", "mouse", "keyboard"]
    assert chroma.calls == [("GET", BASE_URL, None, TIMEOUT_S)]


def test_razer_factory_reads_the_options():
    backend = BACKENDS.create("razer_chroma", {
        "url": BASE_URL, "devices": ("mouse",), "app_title": "T",
        "heartbeat_interval_s": "1", "init_delay_s": 2, "timeout_s": "3"})
    assert isinstance(backend, RazerChromaBackend)
    assert (backend.devices, backend.heartbeat_interval_s, backend.init_delay_s, backend.timeout_s) \
        == (["mouse"], 1.0, 2.0, 3.0)


# ---- ASUS Aura SDK ----------------------------------------------------------
class FakeLight:
    Color = None


class FakeLights:
    def __init__(self, count):
        self.items = [FakeLight() for _ in range(count)]
        self.Count = count

    def Item(self, i):  # noqa: N802  COM name
        return self.items[i]

    def colors(self):
        return [light.Color for light in self.items]


class FakeDevice:
    def __init__(self, name, count, device_type=0x00010000, fail_apply=False):
        self.Name, self.Type, self.Lights = name, device_type, FakeLights(count)
        self.applies = 0
        self.fail_apply = fail_apply

    def Apply(self):  # noqa: N802
        if self.fail_apply:
            raise OSError("COM call failed")
        self.applies += 1


class FakeCollection:
    def __init__(self, devices):
        self.devices = devices
        self.Count = len(devices)

    def Item(self, i):  # noqa: N802
        return self.devices[i]


class FakeAura:
    """The COM object tree of `aura.sdk.1`."""

    def __init__(self, devices):
        self.devices = devices
        self.calls = []

    def Enumerate(self, device_type):  # noqa: N802
        self.calls.append(("Enumerate", device_type))
        return FakeCollection(self.devices)

    def SwitchMode(self):  # noqa: N802
        self.calls.append(("SwitchMode",))

    def ReleaseControl(self, flags):  # noqa: N802
        self.calls.append(("ReleaseControl", flags))


@pytest.fixture
def fake_com(monkeypatch):
    """A fake `win32com.client.Dispatch`; MTA setup is a recorder."""
    state = {"dispatched": [], "sdks": [], "devices": [], "mta": 0}

    def dispatch(prog_id):
        state["dispatched"].append(prog_id)
        sdk = FakeAura(state["devices"])
        state["sdks"].append(sdk)
        return sdk

    client = types.ModuleType("win32com.client")
    client.Dispatch = dispatch
    package = types.ModuleType("win32com")
    package.client = client
    monkeypatch.setitem(sys.modules, "win32com", package)
    monkeypatch.setitem(sys.modules, "win32com.client", client)
    monkeypatch.setattr(aura_sdk, "_init_mta", lambda: state.__setitem__("mta", state["mta"] + 1))
    monkeypatch.setattr(aura_sdk, "per_user_overrides", lambda: [])
    monkeypatch.setattr(aura_sdk, "machine_instance", lambda prog_id: prog_id)  # Dispatch gets the ProgID
    # The fake objects have no COM type information: the type check is tested on its own.
    monkeypatch.setattr(aura_sdk, "machine_type_libraries", lambda prog_id: [])
    monkeypatch.setattr(aura_sdk.TypeCheck, "problem", lambda self, name, item: None)
    return state


def test_aura_first_show_scans_takes_control_and_colors_wanted_devices(fake_com, caplog):
    board, card = FakeDevice("Mainboard", 3), FakeDevice("GPU", 2)
    fake_com["devices"] = [board, card]
    backend = AuraSdkBackend(include=["Main*"])
    with caplog.at_level(logging.INFO, logger=aura_sdk.__name__):
        backend.show(solid(RED))
    assert fake_com["dispatched"] == [aura_sdk.PROG_ID] and fake_com["mta"] == 1
    assert ("SwitchMode",) in fake_com["sdks"][0].calls
    assert board.Lights.colors() == [RED.to_bgr_int()] * 3 and board.applies == 1
    assert card.Lights.colors() == [None, None] and card.applies == 0
    assert "Aura control taken: Mainboard" in caplog.text


def test_aura_unchanged_frame_makes_no_com_calls(fake_com):
    board = FakeDevice("Mainboard", 2)
    fake_com["devices"] = [board]
    backend = AuraSdkBackend(include=["*"])
    backend.show(solid(RED))
    backend.show(solid(RED))
    assert board.applies == 1
    backend.show(solid(BLUE))
    assert board.applies == 2 and board.Lights.colors() == [BLUE.to_bgr_int()] * 2


def test_aura_spatial_frame_spans_the_real_leds_and_the_rest_copy_the_last(fake_com):
    strip = FakeDevice("Strip", 5)
    fake_com["devices"] = [strip]
    frame = gradient()
    AuraSdkBackend(include=["*"], led_counts={"Strip": 3}).show(frame)
    spread = [c.to_bgr_int() for c in frame.colors(3)]
    assert strip.Lights.colors() == spread + [spread[-1]] * 2


def test_aura_device_without_leds_does_not_break_a_spatial_frame(fake_com):
    empty, strip = FakeDevice("Empty", 0), FakeDevice("Strip", 2)
    fake_com["devices"] = [empty, strip]
    frame = gradient()
    AuraSdkBackend(include=["*"]).show(frame)
    assert strip.Lights.colors() == [c.to_bgr_int() for c in frame.colors(2)]


def test_aura_led_count_of_zero_leaves_the_leds_unset(fake_com):
    strip = FakeDevice("Strip", 2)
    fake_com["devices"] = [strip]
    AuraSdkBackend(include=["*"], led_counts={"Strip": 0}).show(gradient())
    assert strip.Lights.colors() == [None, None] and strip.applies == 1


def test_aura_no_wanted_device_logs_no_devices(fake_com, caplog):
    fake_com["devices"] = [FakeDevice("GPU", 1)]
    with caplog.at_level(logging.INFO, logger=aura_sdk.__name__):
        AuraSdkBackend(include=["Main*"]).show(solid(RED))
    assert "Aura control taken: no devices" in caplog.text


def test_aura_com_error_raises_and_the_next_show_connects_again(fake_com):
    board = FakeDevice("Mainboard", 1, fail_apply=True)
    fake_com["devices"] = [board]
    backend = AuraSdkBackend(include=["*"])
    with pytest.raises(OSError):
        backend.show(solid(RED))
    board.fail_apply = False
    backend.show(solid(RED))
    assert fake_com["dispatched"] == [aura_sdk.PROG_ID] * 2
    assert ("SwitchMode",) in fake_com["sdks"][1].calls and board.applies == 1


def test_aura_release_gives_control_back_once_and_show_takes_it_again(fake_com, caplog):
    board = FakeDevice("Mainboard", 1)
    fake_com["devices"] = [board]
    backend = AuraSdkBackend(include=["*"])
    backend.show(solid(RED))
    with caplog.at_level(logging.INFO, logger=aura_sdk.__name__):
        backend.release()
        backend.release()
    sdk = fake_com["sdks"][0]
    assert sdk.calls.count(("ReleaseControl", aura_sdk.RELEASE_FLAGS)) == 1
    assert caplog.text.count("Aura control released to Armoury Crate") == 1
    backend.show(solid(RED))  # the same color is sent again after the release
    assert sdk.calls.count(("SwitchMode",)) == 2 and board.applies == 2
    assert fake_com["dispatched"] == [aura_sdk.PROG_ID]  # the SDK object lives on


def test_aura_release_without_control_makes_no_com_call(fake_com):
    AuraSdkBackend(include=["*"]).release()
    assert fake_com["dispatched"] == []


def test_aura_describe_marks_the_wanted_devices(fake_com):
    fake_com["devices"] = [FakeDevice("Mainboard", 3, device_type=0x00010000),
                           FakeDevice("GPU", 2, device_type=0x00020000)]
    lines = AuraSdkBackend(include=["Main*"]).describe()
    assert lines == ["[x] Mainboard  type=0x00010000  lights=3",
                     "[ ] GPU  type=0x00020000  lights=2"]


def test_aura_factory_reads_include_and_led_counts():
    backend = BACKENDS.create("aura_sdk", {"include": ("A*",), "led_counts": {"Strip": "30"}})
    assert isinstance(backend, AuraSdkBackend)
    assert (backend.include, backend.led_counts) == (["A*"], {"Strip": 30})
    assert BACKENDS.create("aura_sdk", {"include": []}).led_counts == {}


# ---- the COM apartment ------------------------------------------------------
class FakeComError(Exception):
    def __init__(self, hresult):
        super().__init__(hresult)
        self.hresult = hresult


def fake_pythoncom(error=None):
    module = types.ModuleType("pythoncom")
    module.COINIT_MULTITHREADED = aura_sdk.COINIT_MULTITHREADED
    module.com_error = FakeComError
    module.calls = []

    def co_initialize_ex(flags):
        module.calls.append(flags)
        if error is not None:
            raise error

    module.CoInitializeEx = co_initialize_ex
    return module


class _FakeFinder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    """Gives the fake module for one name; records the flags at import time."""

    def __init__(self, name, module):
        self.name, self.module = name, module
        self.flags_at_import = None

    def find_spec(self, fullname, path=None, target=None):
        if fullname != self.name:
            return None
        return importlib.util.spec_from_loader(fullname, self)

    def create_module(self, spec):
        self.flags_at_import = getattr(sys, "coinit_flags", None)
        return self.module

    def exec_module(self, module):
        pass


def test_mta_flag_is_set_before_the_first_pythoncom_import(monkeypatch):
    module = fake_pythoncom()
    finder = _FakeFinder("pythoncom", module)
    monkeypatch.delitem(sys.modules, "pythoncom", raising=False)
    monkeypatch.setattr(sys, "coinit_flags", None, raising=False)
    monkeypatch.setattr(sys, "meta_path", [finder, *sys.meta_path])
    assert aura_sdk._init_mta() is module
    assert finder.flags_at_import == aura_sdk.COINIT_MULTITHREADED
    assert module.calls == [aura_sdk.COINIT_MULTITHREADED]


def test_mta_already_initialized_is_fine(monkeypatch):
    s_false = 1
    module = fake_pythoncom(FakeComError(s_false))
    monkeypatch.setitem(sys.modules, "pythoncom", module)
    assert aura_sdk._init_mta() is module


def test_single_threaded_apartment_is_a_clear_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "pythoncom",
                        fake_pythoncom(FakeComError(aura_sdk.RPC_E_CHANGED_MODE)))
    with pytest.raises(RuntimeError, match="multithreaded COM apartment"):
        aura_sdk._init_mta()


# ---- console ----------------------------------------------------------------
def test_console_logs_each_color_change_once(caplog):
    backend = ConsoleBackend()
    with caplog.at_level(logging.INFO, logger=console.__name__):
        backend.show(solid(RED))
        backend.show(solid(RED))
        backend.show(solid(BLUE))
    assert caplog.messages == [f"console LED -> {RED.to_hex()}", f"console LED -> {BLUE.to_hex()}"]


def test_console_logs_a_spatial_effect_once_by_its_name(caplog):
    backend = ConsoleBackend()
    with caplog.at_level(logging.INFO, logger=console.__name__):
        backend.show(gradient())
        backend.show(Frame(gradient().effect, 1.0))
    assert caplog.messages == ["console LED -> Gradient (per-LED effect)"]


def test_console_release_logs_only_after_a_show_and_then_logs_the_color_again(caplog):
    backend = ConsoleBackend()
    with caplog.at_level(logging.INFO, logger=console.__name__):
        backend.release()  # nothing shown: no line
        backend.show(solid(RED))
        backend.reclaim()
        backend.release()
        backend.show(solid(RED))
    assert caplog.messages == [f"console LED -> {RED.to_hex()}", "console LED released",
                               f"console LED -> {RED.to_hex()}"]


def test_console_describe_and_factory():
    backend = BACKENDS.create("console", {})
    assert isinstance(backend, ConsoleBackend)
    assert backend.describe() == ["console (logs colors, no hardware)"]
