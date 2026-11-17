"""ASUS Aura SDK backend (COM object `aura.sdk.1`, AuraSdk_x64.dll).

How the SDK behaves:
- The SDK enumerates nothing in a single-threaded COM apartment. It needs MTA.
- Without admin rights it sees only some devices (for example, a graphics
  card). The motherboard and the ARGB headers need an elevated process.
- `SwitchMode()` takes the LEDs from Armoury Crate. `ReleaseControl(0)` gives
  them back.
- The ASUS service takes the LEDs back each time the display turns on, and the
  SDK of this process gets no error: its Apply() calls only stop showing. So
  `reclaim()` calls SwitchMode() again (see power_events.py).
- The first `Enumerate()` in a process (elevated) is slow: it scans all
  devices. Later calls, and SwitchMode/ReleaseControl, are fast. So the SDK object and the device
  list live as long as the process, and release only gives up control.
"""

from __future__ import annotations

import fnmatch
import struct
import logging
import os
import sys
import uuid
from dataclasses import dataclass, field
from typing import Any

from .. import machine_paths
from ..frame import Frame
from .base import BACKENDS

log = logging.getLogger(__name__)

PROG_ID = "aura.sdk.1"
BITS_PER_BYTE, POINTER_BITS_64 = 8, 64
MACHINE_CLASSES = r"SOFTWARE\Classes"  # under HKEY_LOCAL_MACHINE: only admins can write
USER_CLASSES = r"Software\Classes"  # under HKEY_CURRENT_USER: any process of the user can write
ALL_DEVICE_TYPES = 0
RELEASE_FLAGS = 0
COINIT_MULTITHREADED = 0
RPC_E_CHANGED_MODE = -2147417850


def _init_mta() -> Any:
    if "pythoncom" not in sys.modules:
        sys.coinit_flags = COINIT_MULTITHREADED  # read by pythoncom on first import
    import pythoncom

    try:
        pythoncom.CoInitializeEx(pythoncom.COINIT_MULTITHREADED)
    except pythoncom.com_error as error:
        if error.hresult == RPC_E_CHANGED_MODE:
            raise RuntimeError(
                "Aura SDK needs a multithreaded COM apartment, but this thread is "
                "already single-threaded. Create the Aura backend before other COM use."
            ) from error
        # S_FALSE / already initialized as MTA: fine
    return pythoncom


def per_user_overrides(prog_id: str = PROG_ID) -> list[str]:
    """The per-user (HKCU) keys that could change what this admin process uses for the class.

    HKCU is writable without admin rights. machine_instance() reads the class ID
    and the DLL from HKLM only, so per-user ProgID and CLSID keys change nothing.
    But win32com reads the type library of the SDK to make its wrapper, and that
    lookup uses the merged registry view. The SDK installer registers the type
    library in HKLM only.
    """
    import winreg

    keys = []
    try:
        clsid = winreg.QueryValue(winreg.HKEY_LOCAL_MACHINE, rf"{MACHINE_CLASSES}\{prog_id}\CLSID")
        typelib = winreg.QueryValue(winreg.HKEY_LOCAL_MACHINE, rf"{MACHINE_CLASSES}\CLSID\{clsid}\TypeLib")
        keys.append(rf"TypeLib\{typelib}")
    except OSError:
        pass  # the SDK is not installed (Dispatch() fails with its own error), or it has no type library
    found = []
    for key in keys:
        try:
            winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CURRENT_USER, rf"{USER_CLASSES}\{key}"))
        except OSError:
            continue
        found.append(rf"HKEY_CURRENT_USER\{USER_CLASSES}\{key}")
    return found


IID_ICLASSFACTORY = "{00000001-0000-0000-C000-000000000046}"
IID_IDISPATCH = "{00020400-0000-0000-C000-000000000046}"
VTABLE_RELEASE, VTABLE_CREATE_INSTANCE = 2, 3  # IUnknown::Release, IClassFactory::CreateInstance
# DLL search without PATH and the current folder: a user without admin rights can
# write many PATH folders, and the SDK loads some DLLs by name.
LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR = 0x00000100  # the folder of the DLL that is loaded
LOAD_LIBRARY_SEARCH_DEFAULT_DIRS = 0x00001000  # the app folder, System32, AddDllDirectory folders
def expand_machine_path(raw: str, folder: Any = machine_paths.machine_folder) -> str:
    """A registered DLL path: only machine folders (from Windows); any other %NAME% is refused."""
    try:
        return machine_paths.expand(raw, strict=True, folder=folder).strip('"')
    except ValueError as error:
        raise ValueError(f"Aura SDK path: {error} (not a system folder)") from error


def machine_server(prog_id: str = PROG_ID) -> tuple[str, str]:
    """(class ID, DLL path) of `prog_id`, read from HKLM only (only admins can write it)."""
    import winreg

    clsid = winreg.QueryValue(winreg.HKEY_LOCAL_MACHINE, rf"{MACHINE_CLASSES}\{prog_id}\CLSID")
    raw = winreg.QueryValue(winreg.HKEY_LOCAL_MACHINE, rf"{MACHINE_CLASSES}\CLSID\{clsid}\InprocServer32")
    path = expand_machine_path(raw)
    if not os.path.isabs(path) or not os.path.isfile(path):
        raise FileNotFoundError(f"Aura SDK DLL of {prog_id} not found: {raw!r}")
    return clsid, path


def restrict_dll_search(kernel32: Any, sdk_folder: str) -> None:
    """From now on, this process loads DLLs by name only from the app folder,
    System32 and `sdk_folder`. Python and pywin32 are loaded already."""
    from ctypes import wintypes

    kernel32.SetDefaultDllDirectories.restype = wintypes.BOOL
    kernel32.SetDefaultDllDirectories.argtypes = [wintypes.DWORD]
    kernel32.AddDllDirectory.restype = wintypes.LPVOID
    kernel32.AddDllDirectory.argtypes = [wintypes.LPCWSTR]
    if not kernel32.SetDefaultDllDirectories(LOAD_LIBRARY_SEARCH_DEFAULT_DIRS):
        raise OSError("SetDefaultDllDirectories failed")
    if not kernel32.AddDllDirectory(sdk_folder):
        raise OSError(f"AddDllDirectory failed: {sdk_folder}")


COM_CACHE_FOLDER = "gen_py"
_restricted_for: set[str] = set()  # the SDK folders added to the DLL search of this process
SOURCE_RUN_CACHE = r"AlertColors\gen_py-source-run"  # under Program Files: a source run as admin
CSIDL_PROGRAM_FILES = 0x0026
_com_cache_folder: str | None = None  # set one time for each process
_com_cache_chosen = False


def com_cache_folder(frozen: bool | None = None, admin: bool | None = None) -> str | None:
    """The folder for the win32com code cache, or None for the default of win32com.

    - With admin rights, the installed exe: next to the exe, in Program Files.
    - With admin rights, a run from source: the folder next to the Python exe can be
      writable for the user (a venv), so a folder in Program Files, from Windows.
    - Without admin rights: None. The default (TEMP of the user) gives such a
      process no rights that it does not have, and it can always write there.
    """
    import ctypes

    frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    admin = bool(ctypes.windll.shell32.IsUserAnAdmin()) if admin is None else admin
    if not admin:
        return None
    if not frozen:
        return os.path.join(machine_paths.machine_folder(CSIDL_PROGRAM_FILES), SOURCE_RUN_CACHE)
    return os.path.join(os.path.dirname(os.path.abspath(sys.executable)), COM_CACHE_FOLDER)


def admin_only_com_cache() -> str | None:
    """Choose the code cache folder of win32com (gen_py), before win32com loads.

    Its default folder is in the TEMP folder of the user, where a process without
    admin rights can write, and win32com loads code from that cache. See
    `com_cache_folder` for the folder.
    """
    import types

    global _com_cache_folder, _com_cache_chosen
    if _com_cache_chosen:
        return _com_cache_folder  # a reconnect: the folder is set already
    folder = com_cache_folder()
    if folder is None:  # no admin rights: the default of win32com
        _com_cache_chosen = True
        return None
    if "win32com.client.gencache" in sys.modules:
        raise RuntimeError("win32com loaded its code cache before the Aura backend could choose its folder")
    # win32com makes the folder itself (an update removes app/gen_py).
    _com_cache_folder, _com_cache_chosen = folder, True
    module = sys.modules.get("win32com.gen_py") or types.ModuleType("win32com.gen_py")
    module.__path__ = [folder]
    sys.modules["win32com.gen_py"] = module
    if (win32com := sys.modules.get("win32com")) is not None:
        win32com.__gen_path__ = folder  # imported before: its cache folder is set already
    return folder


def _guid(text: str) -> Any:
    import ctypes

    return (ctypes.c_byte * len(uuid.UUID(text).bytes_le)).from_buffer_copy(uuid.UUID(text).bytes_le)


def machine_instance(prog_id: str = PROG_ID) -> Any:
    """The COM object of `prog_id`, made by the DLL of its HKLM registration.

    CoCreateInstance would look the class up in the merged registry view, where
    a per-user (HKCU) key can come first. This loads the HKLM DLL path, then
    asks that DLL for the object (DllGetClassObject, CreateInstance): no
    registry key of the user can choose the code that runs as admin.
    """
    import ctypes
    from ctypes import wintypes

    import pythoncom

    clsid, path = machine_server(prog_id)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LoadLibraryExW.restype = wintypes.HMODULE
    kernel32.LoadLibraryExW.argtypes = [wintypes.LPCWSTR, wintypes.HANDLE, wintypes.DWORD]
    if (folder := os.path.dirname(path)) not in _restricted_for:  # one time for each folder
        restrict_dll_search(kernel32, folder)
        _restricted_for.add(folder)
    module = kernel32.LoadLibraryExW(path, None, LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR | LOAD_LIBRARY_SEARCH_DEFAULT_DIRS)
    if not module:
        raise ctypes.WinError(ctypes.get_last_error())
    get_class_object = ctypes.WinDLL(path, handle=module).DllGetClassObject  # never unloaded
    get_class_object.restype = ctypes.HRESULT  # a failed HRESULT raises OSError
    get_class_object.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    clsid_buffer, factory_iid, dispatch_iid = _guid(clsid), _guid(IID_ICLASSFACTORY), _guid(IID_IDISPATCH)
    factory = ctypes.c_void_p()
    get_class_object(ctypes.byref(clsid_buffer), ctypes.byref(factory_iid), ctypes.byref(factory))
    vtable = ctypes.cast(factory, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    create_instance = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                                         ctypes.POINTER(ctypes.c_void_p))(vtable[VTABLE_CREATE_INSTANCE])
    release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtable[VTABLE_RELEASE])
    instance = ctypes.c_void_p()
    try:
        create_instance(factory, None, ctypes.byref(dispatch_iid), ctypes.byref(instance))
    finally:
        release(factory)
    # Give back the reference of CreateInstance: then the object ends with its
    # Python wrapper, and a reconnect never keeps an old SDK object alive.
    return _wrap(instance, pythoncom.IID_IDispatch)


def _wrap(pointer: Any, iid: Any) -> Any:
    """A pywin32 object for a raw COM pointer. The wrapper adds its own reference, and
    the reference of the pointer is given back."""
    import ctypes

    import pythoncom

    wrapped = pythoncom.ObjectFromAddress(pointer.value, iid)
    vtable = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtable[VTABLE_RELEASE])(pointer)
    return wrapped


REGKIND_NONE = 2  # LoadTypeLibEx: load only, never register
TYPELIB_PLATFORM = "win64" if struct.calcsize("P") * BITS_PER_BYTE == POINTER_BITS_64 else "win32"
TYPEFLAG_FDUAL = 0x40
TKIND_DISPATCH = 4
DUAL_VTABLE_INTERFACE = -1  # GetRefTypeOfImplType(-1): the vtable part of a dual interface


def load_type_library(path: str) -> Any:
    """The type library in `path`, loaded without registration."""
    import ctypes

    import pythoncom

    load = ctypes.WinDLL("oleaut32").LoadTypeLibEx
    load.restype = ctypes.HRESULT  # a failed HRESULT raises OSError
    load.argtypes = [ctypes.c_wchar_p, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)]
    library = ctypes.c_void_p()
    load(path, REGKIND_NONE, ctypes.byref(library))
    return _wrap(library, pythoncom.IID_ITypeLib)


def machine_type_libraries(prog_id: str = PROG_ID) -> list[Any]:
    """Each type library that HKLM registers for the class of `prog_id`, loaded from
    its HKLM path: the type information that the SDK is meant to use."""
    import winreg

    clsid = winreg.QueryValue(winreg.HKEY_LOCAL_MACHINE, rf"{MACHINE_CLASSES}\{prog_id}\CLSID")
    libid = winreg.QueryValue(winreg.HKEY_LOCAL_MACHINE, rf"{MACHINE_CLASSES}\CLSID\{clsid}\TypeLib")
    root = rf"{MACHINE_CLASSES}\TypeLib\{libid}"
    libraries = []
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, root) as versions:
        for version in _subkeys(versions):
            with winreg.OpenKey(versions, version) as locales:
                for locale in _subkeys(locales):
                    try:
                        raw = winreg.QueryValue(locales, rf"{locale}\{TYPELIB_PLATFORM}")
                    except OSError:
                        continue  # not a locale key (FLAGS, HELPDIR), or no library for this platform
                    libraries.append(load_type_library(expand_machine_path(raw)))
    return libraries


def _subkeys(key: Any) -> list[str]:
    import winreg

    names, index = [], 0
    while True:
        try:
            names.append(winreg.EnumKey(key, index))
        except OSError:
            return names
        index += 1


VT_USERDEFINED = 29
TKIND_ALIAS = 6
MEMBERID_NIL = -1  # GetDocumentation(-1): the type itself
MAX_ALIAS_DEPTH = 8  # an alias of an alias: far more than any real library


def _type_key(info: Any, typedesc: Any, depth: int = 0) -> Any:
    """A type description in which a user-defined type is what it points to (its IID,
    kind and name, and the target of an alias), not a handle number of its library."""
    if isinstance(typedesc, tuple) and len(typedesc) == 2 and isinstance(typedesc[0], int):
        kind, inner = typedesc
        if kind != VT_USERDEFINED:
            return kind, _type_key(info, inner, depth)
        ref = info.GetRefTypeInfo(inner)
        attr = ref.GetTypeAttr()
        key = ("type", str(attr.iid), attr.typekind, ref.GetDocumentation(MEMBERID_NIL)[0])
        if attr.typekind == TKIND_ALIAS:
            if depth >= MAX_ALIAS_DEPTH:
                return key + ("too deep",)
            key += (_type_key(ref, attr.tdescAlias, depth + 1),)
        return key
    return typedesc


def _element_key(info: Any, element: Any) -> Any:
    """An argument, a return value or a variable: (type, flags, default)."""
    if isinstance(element, tuple) and element:
        return (_type_key(info, element[0]),) + tuple(repr(part) for part in element[1:])
    return repr(element)


def describe_type(info: Any) -> tuple:
    """What a caller depends on in a type: its functions (IDs, vtable offsets, kinds,
    arguments, names), its variables and the interfaces that it builds on."""
    attr = info.GetTypeAttr()
    functions = []
    for index in range(attr.cFuncs):
        f = info.GetFuncDesc(index)
        # A caller through IDispatch uses the member IDs, not the vtable offsets, and the
        # dispatch view of an object can count its offsets in other steps than the file.
        # The vtable part of a dual interface (below) keeps them.
        slot = None if attr.typekind == TKIND_DISPATCH else f.oVft
        functions.append((f.memid, f.funckind, f.invkind, f.callconv, slot, f.cParamsOpt,
                          tuple(_element_key(info, a) for a in f.args), _element_key(info, f.rettype),
                          f.wFuncFlags, tuple(info.GetNames(f.memid))))
    variables = []
    for index in range(attr.cVars):
        v = info.GetVarDesc(index)
        variables.append((v.memid, v.varkind, _element_key(info, v.elemdescVar), v.wVarFlags,
                          tuple(info.GetNames(v.memid))))
    bases = tuple(str(info.GetRefTypeInfo(info.GetRefTypeOfImplType(index)).GetTypeAttr().iid)
                  for index in range(attr.cImplTypes))
    described = (str(attr.iid), attr.typekind, attr.wTypeFlags, tuple(functions), tuple(variables), bases)
    if attr.typekind == TKIND_DISPATCH and attr.wTypeFlags & TYPEFLAG_FDUAL:
        # The vtable part of a dual interface (the same IID): what ITypeInfo.Invoke calls.
        vtable_part = info.GetRefTypeInfo(info.GetRefTypeOfImplType(DUAL_VTABLE_INTERFACE))
        described += (describe_type(vtable_part),)
    return described


def same_as_machine_type(info: Any, libraries: list[Any]) -> bool:
    """True when `info` equals the type of the same IID in one of the HKLM libraries."""
    import pythoncom

    used = describe_type(info)
    iid = info.GetTypeAttr().iid
    for library in libraries:
        try:
            genuine = library.GetTypeInfoOfGuid(iid)
        except pythoncom.com_error:
            continue
        if describe_type(genuine) == used:
            return True
    return False


class TypeCheck:
    """Checks the type information of an SDK object before the first call through it.

    GetTypeInfo() calls no member, and it makes the SDK load the type information of
    that interface (it does so once, at the first use). A type information object that
    passed once is kept: an object that shares it needs no new compare."""

    def __init__(self, libraries: list[Any]) -> None:
        self.libraries = libraries
        self.passed: list[Any] = []  # PyITypeInfo: compared by COM identity, not hashable

    def problem(self, name: str, item: Any) -> str | None:
        import pythoncom

        try:
            info = item._oleobj_.GetTypeInfo()
        except pythoncom.com_error as error:
            return f"{name} (no type information: {error})"
        if any(info == known for known in self.passed):
            return None
        try:
            same = same_as_machine_type(info, self.libraries)
        except (pythoncom.com_error, RecursionError) as error:  # a type that cannot be read: not the genuine one
            return f"{name} (its type information cannot be read: {error})"
        if not same:
            return f"{name} ({info.GetTypeAttr().iid})"
        self.passed.append(info)
        return None


def foreign_types(objects: list[Any], libraries: list[Any]) -> list[str]:
    """The objects whose type information is not the one of the HKLM libraries."""
    check = TypeCheck(libraries)
    return [problem for name, item in objects if (problem := check.problem(name, item)) is not None]


@dataclass
class AuraSdkBackend:
    include: list[str]
    # LEDs that are really connected, by device name. An ARGB header reports
    # its maximum (120); a per-LED effect then spans this many LEDs.
    led_counts: dict[str, int] = field(default_factory=dict)
    name: str = "aura_sdk"
    _sdk: Any = field(default=None, repr=False)
    _devices: list[Any] = field(default_factory=list, repr=False)
    _controlling: bool = False
    _last: dict[str, tuple[int, ...]] = field(default_factory=dict, repr=False)
    # Set when the SDK used type information that is not the one of HKLM: the loaded SDK
    # keeps it, so no new connect until the app restarts.
    _refused: str | None = field(default=None, repr=False)
    _types: Any = field(default=None, repr=False)  # TypeCheck, made at the first connect

    def _connect(self) -> Any:
        if self._refused is not None:
            raise PermissionError(self._refused)
        if self._sdk is None:
            self._connect_checked()
        return self._sdk

    def _connect_checked(self) -> None:
        if overrides := per_user_overrides():
            raise PermissionError(
                f"Aura SDK not loaded: a per-user COM key can make this admin process load "
                f"another DLL: {', '.join(overrides)}. Remove the key if you did not add it")
        _init_mta()
        admin_only_com_cache()
        import win32com.client

        log.info("Aura SDK: scanning devices (the first scan is slow)")
        try:
            if self._types is None:
                self._types = TypeCheck(machine_type_libraries(PROG_ID))
            self._sdk = self._checked("the SDK", win32com.client.Dispatch(machine_instance(PROG_ID)))
            self._enumerate()  # the first scan, and a check of each device before its use
        except Exception:
            self._forget()
            raise
        if overrides := per_user_overrides():
            self._refuse(f"a per-user COM key came during the connect: {', '.join(overrides)}")

    def _refuse(self, what: str) -> None:
        # The SDK reads its type library through the merged registry view, where a key of
        # the user can come first, also for a moment only, and it keeps what it read.
        self._forget()
        self._refused = (f"Aura SDK dropped: {what}. Remove the key if you did not add it, then restart "
                         "the app (setup.cmd restart)")
        raise PermissionError(self._refused)

    def _checked(self, name: str, item: Any) -> Any:
        """`item`, after its type information passed: only then a member call goes through it."""
        if (problem := self._types.problem(name, item)) is not None:
            self._refuse(f"type information that is not the one of HKLM: {problem}")
        return item

    def _forget(self) -> None:
        """After an SDK error: drop the SDK object and its devices, so that the next
        show() connects again (a LightingService restart kills the old object)."""
        self._sdk, self._devices, self._controlling, self._last = None, [], False, {}

    def _acquire(self) -> None:
        if self._controlling:
            return
        sdk = self._connect()
        try:
            sdk.SwitchMode()
            self._devices = [d for d in self._enumerate() if self._wanted(d.Name)]
            for device in self._devices:  # show() calls through these: check them all now
                lights = self._checked(f"the lights of {device.Name}", device.Lights)
                for index in range(lights.Count):
                    self._checked(f"light {index} of {device.Name}", lights.Item(index))
        except Exception:
            self._forget()
            raise
        self._controlling = True
        self._last = {}
        log.info("Aura control taken: %s", ", ".join(d.Name for d in self._devices) or "no devices")

    def _enumerate(self) -> list[Any]:
        collection = self._checked("the device list", self._sdk.Enumerate(ALL_DEVICE_TYPES))
        return [self._checked(f"device {i}", collection.Item(i)) for i in range(collection.Count)]

    def _wanted(self, device_name: str) -> bool:
        return any(fnmatch.fnmatchcase(device_name, pattern) for pattern in self.include)

    def _packed(self, frame: Frame, name: str, lights: int) -> tuple[int, ...]:
        if frame.uniform:
            return (frame.color().to_bgr_int(),) * lights
        count = min(self.led_counts.get(name, lights), lights)
        colors = [c.to_bgr_int() for c in frame.colors(count)]
        # LEDs past `count` copy the last one. No LEDs (count 0): nothing to copy.
        return tuple(colors + colors[-1:] * (lights - count))

    def show(self, frame: Frame) -> None:
        self._acquire()
        try:
            for device in self._devices:
                lights = device.Lights
                packed = self._packed(frame, device.Name, lights.Count)
                if packed == self._last.get(device.Name):
                    continue  # unchanged: no COM calls
                for i, value in enumerate(packed):
                    lights.Item(i).Color = value
                device.Apply()
                self._last[device.Name] = packed
        except Exception:
            self._forget()  # reconnect on the next show
            raise

    def reclaim(self) -> None:
        if not self._controlling:
            return  # the next show() takes control anyway
        try:
            self._sdk.SwitchMode()
        except Exception:
            self._forget()  # the next show() connects again
            raise
        self._last = {}  # the service applied its own profile: send every color again
        log.debug("Aura control taken again")

    def release(self) -> None:
        if not self._controlling:
            return
        self._controlling = False
        self._last = {}
        try:
            self._sdk.ReleaseControl(RELEASE_FLAGS)
        except Exception:
            self._forget()
            raise
        log.info("Aura control released to Armoury Crate")

    def describe(self) -> list[str]:
        try:
            self._connect()
            lines = []
            for device in self._enumerate():
                mark = "x" if self._wanted(device.Name) else " "
                lights = self._checked(f"the lights of {device.Name}", device.Lights)
                lines.append(f"[{mark}] {device.Name}  type=0x{device.Type:08X}  lights={lights.Count}")
        except Exception:
            self._forget()
            raise
        return lines


@BACKENDS.register("aura_sdk")
def _create(options: dict[str, Any]) -> AuraSdkBackend:
    return AuraSdkBackend(
        include=list(options["include"]),
        led_counts={str(k): int(v) for k, v in options.get("led_counts", {}).items()},
    )
