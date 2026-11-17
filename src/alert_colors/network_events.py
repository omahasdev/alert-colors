"""Windows network changes: an IPv4 address of this PC is added or removed.

Why: after a network outage, the poller can wait up to its backoff before the
next fetch. When Wi-Fi or a cable connects again, the PC gets its address, and
this event lets the daemon fetch at once (see `Daemon.network_changed`).

`NotifyAddrChange(NULL, NULL)` of iphlpapi blocks until the next change. So the
watcher thread is a daemon thread that cannot be woken: `stop()` only stops the
calls of `on_change`, and the thread ends with the process. An outage behind
the router (the ISP is down) changes no address: the short retry of the
daemon (offline_retry_s) covers that case.
"""

from __future__ import annotations

import ctypes
import logging
import sys
import threading
from collections.abc import Callable
from ctypes import wintypes

log = logging.getLogger(__name__)

NO_ERROR = 0


def _notify_addr_change() -> Callable[[], int]:
    iphlpapi = ctypes.WinDLL("iphlpapi")
    notify = iphlpapi.NotifyAddrChange
    notify.restype = wintypes.DWORD
    notify.argtypes = [ctypes.POINTER(wintypes.HANDLE), ctypes.c_void_p]
    return lambda: notify(None, None)


class NetworkWatcher:
    """Calls `on_change()` on its own thread after each address change."""

    def __init__(self, on_change: Callable[[], None], wait_for_change: Callable[[], int]) -> None:
        self._on_change, self._wait_for_change = on_change, wait_for_change
        self._stopped = threading.Event()
        self._thread = threading.Thread(target=self._run, name="network-events", daemon=True)
        self._thread.start()
        log.info("network events: watching the address changes")

    def stop(self) -> None:
        self._stopped.set()

    def _run(self) -> None:
        while not self._stopped.is_set():
            result = self._wait_for_change()
            if self._stopped.is_set():
                return
            if result != NO_ERROR:
                log.warning("network events: NotifyAddrChange failed (%s); the short retry still works",
                            result)
                return  # no busy loop on a broken call
            try:
                self._on_change()
            except Exception:  # noqa: BLE001  one failed call never ends the watch
                log.exception("network events: the handler failed")


def watch(on_change: Callable[[], None],
          wait_for_change: Callable[[], int] | None = None) -> Callable[[], None]:
    """Start watching. Returns the function that stops it. Not Windows: nothing to do."""
    if wait_for_change is None and sys.platform != "win32":
        return lambda: None
    try:
        return NetworkWatcher(on_change, wait_for_change or _notify_addr_change()).stop
    except Exception:  # noqa: BLE001  without the watcher the app still works
        log.exception("network events: cannot watch; the short retry still works")
        return lambda: None
