"""Run each lighting backend on its own thread.

Why: vendor SDKs block. The Aura SDK needs ~30 s for its first device scan, and a
REST call can hang until its timeout. The render loop only posts the newest
color to a mailbox, so a slow or broken backend never delays the others.
A COM backend also gets a fresh thread, where it can choose its own apartment.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Protocol

from .domain import FULL_BRIGHTNESS
from .frame import Frame

NO_CORRECTION = (FULL_BRIGHTNESS, FULL_BRIGHTNESS, FULL_BRIGHTNESS)
from .lighting import LightingBackend

log = logging.getLogger(__name__)


class Output(Protocol):
    """What the daemon talks to. Calls return at once."""

    name: str

    def show(self, frame: Frame) -> None: ...

    def release(self) -> None: ...

    def reclaim(self) -> None: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class _Release:
    pass


@dataclass(frozen=True)
class _Reclaim:
    pass


_RELEASE = _Release()
_RECLAIM = _Reclaim()


class BackendWorker:
    """Output that forwards the newest command to a backend on a worker thread.

    Older commands are dropped: only the newest color matters for a display.
    After a backend error the worker releases the backend, waits `retry_s`,
    then continues with the newest command.
    """

    def __init__(
        self,
        backend: LightingBackend,
        retry_s: float,
        close_timeout_s: float,
        brightness: float = FULL_BRIGHTNESS,
        correction: tuple[float, float, float] = NO_CORRECTION,
    ) -> None:
        self.name = backend.name
        self._backend = backend
        self._retry_s = retry_s
        self._close_timeout_s = close_timeout_s
        self._brightness = self._base_brightness = brightness  # per-backend factor
        self._correction = self._base_correction = correction  # per-channel factors (r, g, b)
        self._cond = threading.Condition()
        self._pending: Frame | _Release | None = None
        # Not in the newest-wins mailbox: the next frame must not replace it.
        self._reclaim_requested = False
        self._closing = False
        self._thread = threading.Thread(target=self._loop, name=f"backend-{self.name}", daemon=True)
        self._thread.start()

    def set_tuning(self, brightness: float | None, correction: tuple[float, float, float] | None) -> None:
        """Live tuning; None restores the value from config.toml."""
        self._brightness = self._base_brightness if brightness is None else brightness
        self._correction = self._base_correction if correction is None else correction

    def show(self, frame: Frame) -> None:
        correction, brightness = self._correction, self._brightness
        self._post(frame.map(lambda c: c.corrected(correction).scaled(brightness)))

    def release(self) -> None:
        self._post(_RELEASE)

    def reclaim(self) -> None:
        with self._cond:
            if not self._closing:
                self._reclaim_requested = True
                self._cond.notify()

    def close(self) -> None:
        with self._cond:
            self._pending = _RELEASE
            self._closing = True
            self._cond.notify()
        self._thread.join(self._close_timeout_s)
        if self._thread.is_alive():
            log.warning("backend %s did not stop within %ss", self.name, self._close_timeout_s)

    def _post(self, command: Frame | _Release) -> None:
        with self._cond:
            if not self._closing:
                self._pending = command
                self._cond.notify()

    def _loop(self) -> None:
        while True:
            with self._cond:
                self._cond.wait_for(lambda: self._pending is not None or self._reclaim_requested
                                    or self._closing)
                command, self._pending = self._pending, None
                reclaim, self._reclaim_requested = self._reclaim_requested, False
                closing = self._closing
            ok = self._run(_RECLAIM) if reclaim and not closing else True
            if ok and command is not None:
                ok = self._run(command)
            if not ok:
                self._safe_release()
                with self._cond:  # back off; close() wakes us at once
                    self._cond.wait_for(lambda: self._closing, timeout=self._retry_s)
            if closing:
                return

    def _run(self, command: Frame | _Release | _Reclaim) -> bool:
        try:
            if isinstance(command, _Release):
                self._backend.release()
            elif isinstance(command, _Reclaim):
                self._backend.reclaim()
            else:
                self._backend.show(command)
            return True
        except Exception as error:  # noqa: BLE001  report and retry later
            log.error("backend %s failed, retry in %ss: %s", self.name, self._retry_s, error)
            return False

    def _safe_release(self) -> None:
        try:
            self._backend.release()
        except Exception as error:  # noqa: BLE001
            log.warning("backend %s release failed: %s", self.name, error)
