"""Vendor app starts: a new Armoury Crate or ASUS lighting process.

Why: a vendor app takes the LEDs back when it starts, and the app gets no
message about this. At logon, ArmouryCrate.exe can start more than 60 s
after the app, when the startup reclaim window has ended. It then loads its
own profile, and the LEDs show it until the next interval reclaim.

The watcher reads the process list (a Toolhelp snapshot: names only, no
access rights to the processes are necessary) every `poll_s`. A watched name
with a new process ID calls `on_start(name)`. The processes that run when the
watcher starts do not count: the app takes the LEDs at its own start.
"""

from __future__ import annotations

import ctypes
import logging
import sys
import threading
from collections.abc import Callable, Collection, Iterable
from ctypes import wintypes

log = logging.getLogger(__name__)

TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
STOP_TIMEOUT_S = 5.0


class _ProcessEntry(ctypes.Structure):
    """PROCESSENTRY32W."""

    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * wintypes.MAX_PATH),
    ]


def _kernel32():  # noqa: ANN202
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry)]
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    return kernel32


def list_processes() -> list[tuple[int, str]]:
    """(process ID, exe name) of every process."""
    kernel32 = _kernel32()
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot in (None, INVALID_HANDLE_VALUE):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        entry = _ProcessEntry()
        entry.dwSize = ctypes.sizeof(_ProcessEntry)
        result = []
        found = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while found:
            result.append((entry.th32ProcessID, entry.szExeFile))
            found = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
        return result
    finally:
        kernel32.CloseHandle(snapshot)


def watched(processes: Iterable[tuple[int, str]], names: Collection[str]) -> dict[int, str]:
    """The processes with a watched name (case-insensitive): {process ID: name}."""
    wanted = {name.lower() for name in names}
    return {pid: name for pid, name in processes if name.lower() in wanted}


def started(previous: dict[int, str], current: dict[int, str]) -> list[str]:
    """The names of the processes in `current` that are not in `previous`."""
    return [name for pid, name in current.items() if previous.get(pid) != name]


class ProcessWatcher:
    """Calls `on_start(name)` on its own thread when a watched process starts."""

    def __init__(self, names: Collection[str], poll_s: float, on_start: Callable[[str], None],
                 snapshot: Callable[[], list[tuple[int, str]]] = list_processes) -> None:
        self._names, self._poll_s, self._on_start, self._snapshot = names, poll_s, on_start, snapshot
        self._stop = threading.Event()
        self._known = watched(self._snapshot(), self._names)  # these already run: no event
        self._thread = threading.Thread(target=self._run, name="process-events", daemon=True)
        self._thread.start()
        log.info("process events: watching %s every %ss", ", ".join(self._names), self._poll_s)

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(STOP_TIMEOUT_S)

    def check(self) -> None:
        current = watched(self._snapshot(), self._names)
        for name in started(self._known, current):
            self._on_start(name)
        self._known = current

    def _run(self) -> None:
        while not self._stop.wait(self._poll_s):
            try:
                self.check()
            except Exception:  # noqa: BLE001  one failed snapshot never ends the watch
                log.exception("process events: the check failed")


def watch(names: Collection[str], poll_s: float, on_start: Callable[[str], None]) -> Callable[[], None]:
    """Start watching. Returns the function that stops it. No names, or not Windows: nothing to do."""
    if sys.platform != "win32" or not names or poll_s <= 0:
        return lambda: None
    try:
        return ProcessWatcher(names, poll_s, on_start).stop
    except Exception:  # noqa: BLE001  without the watcher the app still works
        log.exception("process events: cannot watch; the LEDs are taken again only by the interval")
        return lambda: None
