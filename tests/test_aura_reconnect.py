"""The Aura backend connects again after any SDK error.

A LightingService restart (for example after resume) kills the SDK object. Each
later call on it fails, so the backend must drop it and make a new one.
"""
import sys
import types

import pytest

from alert_colors.domain import Color
from alert_colors.effects import Solid
from alert_colors.frame import Frame
from alert_colors.lighting import aura_sdk
from alert_colors.lighting.aura_sdk import AuraSdkBackend

RED = Color(255, 0, 0)
LIGHTS = 2


class Dead(Exception):
    """The RPC server is unavailable: the SDK object died."""


class Lights:
    def __init__(self, count):
        self.Count = count
        self.items = [types.SimpleNamespace(Color=None) for _ in range(count)]

    def Item(self, i):  # noqa: N802  COM names
        return self.items[i]


class Device:
    def __init__(self, sdk):
        self.sdk, self.Name, self.Lights = sdk, "Mainboard", Lights(LIGHTS)

    def Apply(self):  # noqa: N802
        self.sdk.check()


class Collection:
    def __init__(self, devices):
        self.devices, self.Count = devices, len(devices)

    def Item(self, i):  # noqa: N802
        return self.devices[i]


class Sdk:
    def __init__(self, world):
        self.world, self.alive = world, True

    def check(self):
        if not self.alive:
            raise Dead("RPC server unavailable")

    def Enumerate(self, _types):  # noqa: N802
        self.check()
        if self.world["fail_enumerate"]:
            self.world["fail_enumerate"] -= 1
            raise Dead("scan failed")
        return Collection([Device(self)])

    def SwitchMode(self):  # noqa: N802
        self.check()

    def ReleaseControl(self, _flags):  # noqa: N802
        self.check()


@pytest.fixture
def world(monkeypatch):
    state = {"sdks": [], "fail_enumerate": 0}

    def dispatch(_prog_id):
        sdk = Sdk(state)
        state["sdks"].append(sdk)
        return sdk

    client = types.ModuleType("win32com.client")
    client.Dispatch = dispatch
    package = types.ModuleType("win32com")
    package.client = client
    monkeypatch.setitem(sys.modules, "win32com", package)
    monkeypatch.setitem(sys.modules, "win32com.client", client)
    monkeypatch.setattr(aura_sdk, "_init_mta", lambda: None)
    monkeypatch.setattr(aura_sdk, "admin_only_com_cache", lambda: None)
    monkeypatch.setattr(aura_sdk, "per_user_overrides", lambda: [])
    monkeypatch.setattr(aura_sdk, "machine_instance", lambda prog_id: prog_id)
    # The fake objects have no COM type information: the type check is tested on its own.
    monkeypatch.setattr(aura_sdk, "machine_type_libraries", lambda prog_id: [])
    monkeypatch.setattr(aura_sdk.TypeCheck, "problem", lambda self, name, item: None)
    return state


def frame():
    return Frame(Solid(RED), 0.0)


def kill_service(world):
    for sdk in world["sdks"]:
        sdk.alive = False


def test_a_reclaim_error_makes_the_next_show_connect_again(world):
    backend = AuraSdkBackend(include=["*"])
    backend.show(frame())
    kill_service(world)
    with pytest.raises(Dead):
        backend.reclaim()
    backend.show(frame())  # a new SDK object
    assert len(world["sdks"]) == 2
    assert backend._devices[0].sdk is world["sdks"][1]  # no device of the dead object is kept


def test_a_switch_mode_error_makes_the_next_show_connect_again(world):
    backend = AuraSdkBackend(include=["*"])
    backend.show(frame())
    backend.release()
    kill_service(world)
    with pytest.raises(Dead):
        backend.show(frame())  # _acquire: SwitchMode on the dead object
    backend.show(frame())
    assert len(world["sdks"]) == 2


def test_a_failed_first_scan_connects_again(world):
    world["fail_enumerate"] = 1
    backend = AuraSdkBackend(include=["*"])
    with pytest.raises(Dead):
        backend.show(frame())
    backend.show(frame())
    assert len(world["sdks"]) == 2


def test_a_release_error_drops_the_dead_object(world):
    backend = AuraSdkBackend(include=["*"])
    backend.show(frame())
    kill_service(world)
    with pytest.raises(Dead):
        backend.release()
    assert backend._sdk is None and backend._devices == []


def test_a_describe_error_drops_the_dead_object(world):
    backend = AuraSdkBackend(include=["*"])
    backend.show(frame())
    kill_service(world)
    with pytest.raises(Dead):
        backend.describe()
    assert backend._sdk is None


def test_a_per_user_key_that_came_during_the_connect_drops_the_sdk(world, monkeypatch):
    answers = iter([[], [r"HKEY_CURRENT_USER\Software\Classes\TypeLib\{x}"]])
    monkeypatch.setattr(aura_sdk, "per_user_overrides", lambda: next(answers))
    backend = AuraSdkBackend(include=["*"])
    with pytest.raises(PermissionError, match="during the connect"):
        backend.show(frame())
    assert backend._sdk is None


@pytest.mark.skipif(sys.platform != "win32", reason="real COM")
def test_the_dll_search_limit_runs_one_time_for_each_folder():
    """A reconnect adds no DLL search folder: the limit runs one time for each folder."""
    import os
    import subprocess
    from pathlib import Path

    code = "\n".join([
        "from alert_colors.lighting import aura_sdk",
        "calls = []",
        "aura_sdk.restrict_dll_search = lambda kernel32, folder: calls.append(folder)",
        "for _ in range(3):",
        "    aura_sdk.machine_instance('Scripting.Dictionary')",
        "print(len(calls))",
    ])
    src = Path(__file__).resolve().parents[1] / "src"
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False,
                            env={**os.environ, "PYTHONPATH": str(src)})
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "1"


def test_type_information_that_is_not_the_one_of_hklm_drops_the_sdk_until_a_restart(world, monkeypatch):
    """The SDK is checked before its first member call, and the loaded SDK keeps the type
    information that it read: no new connect."""
    checked, scans = [], []
    monkeypatch.setattr(aura_sdk.TypeCheck, "problem",
                        lambda self, name, item: checked.append(name) or (f"{name} (x)" if name == "the SDK" else None))
    monkeypatch.setattr(Sdk, "Enumerate", lambda self, _types: scans.append(1) or Collection([Device(self)]))
    backend = AuraSdkBackend(include=["*"])
    with pytest.raises(PermissionError, match="not the one of HKLM"):
        backend.show(frame())
    assert backend._sdk is None
    assert checked == ["the SDK"] and scans == []  # no call went through the SDK
    with pytest.raises(PermissionError, match="restart the app"):
        backend.show(frame())
    assert checked == ["the SDK"]  # no new connect


def test_every_object_that_show_uses_is_checked_before_a_call_through_it(world, monkeypatch):
    checked = []
    monkeypatch.setattr(aura_sdk.TypeCheck, "problem", lambda self, name, item: checked.append(name) or None)
    backend = AuraSdkBackend(include=["*"])
    backend.show(frame())
    lights = [f"light {i} of Mainboard" for i in range(LIGHTS)]
    assert checked == ["the SDK", "the device list", "device 0",  # the first scan
                       "the device list", "device 0", "the lights of Mainboard", *lights]  # control taken


def test_a_light_with_foreign_type_information_never_gets_a_color(world, monkeypatch):
    refused = f"light {LIGHTS - 1} of Mainboard"
    monkeypatch.setattr(aura_sdk.TypeCheck, "problem",
                        lambda self, name, item: f"{name} (x)" if name == refused else None)
    devices = []
    real_enumerate = Sdk.Enumerate
    monkeypatch.setattr(Sdk, "Enumerate",
                        lambda self, types_: devices.append(real_enumerate(self, types_)) or devices[-1])
    backend = AuraSdkBackend(include=["*"])
    with pytest.raises(PermissionError, match=refused):
        backend.show(frame())
    colors = [light.Color for collection in devices for device in collection.devices for light in device.Lights.items]
    assert colors and all(color is None for color in colors)


def test_a_per_user_key_that_came_during_the_connect_drops_the_sdk_until_a_restart(world, monkeypatch):
    calls = iter([[], [r"HKEY_CURRENT_USER\Software\Classes\TypeLib\{x}"]])
    monkeypatch.setattr(aura_sdk, "per_user_overrides", lambda: next(calls, []))
    backend = AuraSdkBackend(include=["*"])
    with pytest.raises(PermissionError, match="came during the connect"):
        backend.show(frame())
    with pytest.raises(PermissionError, match="restart the app"):
        backend.show(frame())


@pytest.mark.skipif(sys.platform != "win32", reason="real COM")
def test_a_real_object_matches_the_type_library_of_its_hklm_registration():
    """The whole check on a harmless Windows class (never the Aura SDK beside the app):
    the genuine library passes, another library does not."""
    import win32com.client

    dictionary = win32com.client.Dispatch("Scripting.Dictionary")
    assert aura_sdk.foreign_types([("dict", dictionary)], aura_sdk.machine_type_libraries("Scripting.Dictionary")) == []
    other = aura_sdk.load_type_library(r"C:\Windows\System32\stdole2.tlb")
    assert aura_sdk.foreign_types([("dict", dictionary)], [other]) != []
    files = win32com.client.Dispatch("Scripting.FileSystemObject")
    objects = [("files", files), ("drives", files.Drives)]
    assert aura_sdk.foreign_types(objects, aura_sdk.machine_type_libraries("Scripting.FileSystemObject")) == []


class FakeFunction:
    def __init__(self, memid, offset):
        self.memid, self.funckind, self.invkind, self.callconv = memid, 1, 1, 4
        self.oVft, self.cParamsOpt, self.args, self.rettype, self.wFuncFlags = offset, 0, (), (19, 0, None), 0


class FakeAttr:
    def __init__(self, iid, typekind, flags, functions):
        self.iid, self.typekind, self.wTypeFlags = iid, typekind, flags
        self.cFuncs, self.cVars, self.cImplTypes, self.cbSizeVft = len(functions), 0, 0, 8 * len(functions)


class FakeType:
    """A dual interface: the dispatch view, and its vtable part through GetRefTypeOfImplType(-1)."""

    def __init__(self, typekind, functions, names, vtable_part=None):
        self.functions, self.names, self.vtable_part = functions, names, vtable_part
        flags = aura_sdk.TYPEFLAG_FDUAL if vtable_part is not None else 0
        self.attr = FakeAttr("{iid}", typekind, flags, functions)

    def GetTypeAttr(self):  # noqa: N802  pywin32 names
        return self.attr

    def GetFuncDesc(self, index):  # noqa: N802
        return self.functions[index]

    def GetNames(self, memid):  # noqa: N802
        return (self.names[memid],)

    def GetRefTypeOfImplType(self, index):  # noqa: N802
        return index

    def GetRefTypeInfo(self, ref):  # noqa: N802
        return self.vtable_part


TKIND_INTERFACE = 3


def dual(dispatch_step=8, vtable_offset=8, name="Count"):
    vtable_part = FakeType(TKIND_INTERFACE, [FakeFunction(1, vtable_offset)], {1: name})
    return FakeType(aura_sdk.TKIND_DISPATCH, [FakeFunction(1, dispatch_step)], {1: name}, vtable_part)


class FakeLibrary:
    def __init__(self, info):
        self.info = info

    def GetTypeInfoOfGuid(self, iid):  # noqa: N802
        return self.info


@pytest.mark.parametrize(("used", "same"), [
    (dual(), True),
    (dual(dispatch_step=4), True),  # the dispatch view counts in other steps: a caller uses the member ID
    (dual(vtable_offset=16), False),  # another slot of the vtable: ITypeInfo.Invoke would call it
    (dual(name="Other"), False),  # another name for the member ID
])
def test_a_type_is_the_machine_type_only_when_all_that_a_caller_uses_is_the_same(used, same):
    assert aura_sdk.same_as_machine_type(used, [FakeLibrary(dual())]) is same


class FakeRef:
    def __init__(self, name, iid):
        self.name, self.attr = name, FakeAttr(iid, TKIND_INTERFACE, 0, [])

    def GetTypeAttr(self):  # noqa: N802
        return self.attr

    def GetDocumentation(self, memid):  # noqa: N802
        return (self.name, None, 0, None)


class TypeWithResult(FakeType):
    """An interface whose one function returns a pointer to user-defined type 7."""

    def __init__(self, result_name, args=()):
        function = FakeFunction(1, 8)
        function.rettype, function.args = ((26, (29, 7)), 0, None), args
        super().__init__(TKIND_INTERFACE, [function], {1: "Item"})
        self.result = FakeRef(result_name, "{" + result_name + "}")

    def GetRefTypeInfo(self, ref):  # noqa: N802
        return self.result


def test_a_user_defined_type_is_compared_by_what_it_points_to():
    """The same handle number can point to another type in a forged library."""
    genuine = aura_sdk.describe_type(TypeWithResult("IAuraRgbLight"))
    assert aura_sdk.describe_type(TypeWithResult("IAuraRgbLight")) == genuine
    assert aura_sdk.describe_type(TypeWithResult("IOther")) != genuine


def test_other_arguments_make_another_type():
    genuine = aura_sdk.describe_type(TypeWithResult("IAuraRgbLight"))
    with_argument = aura_sdk.describe_type(TypeWithResult("IAuraRgbLight", args=((3, 1, None),)))
    assert with_argument != genuine


@pytest.mark.skipif(sys.platform != "win32", reason="real COM")
def test_a_type_that_passed_lets_only_objects_of_the_same_type_through():
    import win32com.client

    check = aura_sdk.TypeCheck(aura_sdk.machine_type_libraries("Scripting.Dictionary"))
    assert check.problem("dict", win32com.client.Dispatch("Scripting.Dictionary")) is None
    assert check.problem("second dict", win32com.client.Dispatch("Scripting.Dictionary")) is None  # kept
    # FileSystemObject is in the same library as Dictionary; WScript.Shell is in another one.
    assert check.problem("files", win32com.client.Dispatch("Scripting.FileSystemObject")) is None
    assert check.problem("shell", win32com.client.Dispatch("WScript.Shell")) is not None


def test_type_information_that_cannot_be_read_is_refused_not_retried(monkeypatch):
    """A type that does not resolve (a forged library) is refused like a different one."""
    import pythoncom

    def unreadable(info, libraries):
        raise pythoncom.com_error(-2147319765, "Element not found.", None, None)

    monkeypatch.setattr(aura_sdk, "same_as_machine_type", unreadable)

    class Item:
        class _oleobj_:  # noqa: N801  the pywin32 name
            @staticmethod
            def GetTypeInfo():  # noqa: N802
                return object()

    problem = aura_sdk.TypeCheck([]).problem("the SDK", Item())
    assert problem is not None and "cannot be read" in problem
