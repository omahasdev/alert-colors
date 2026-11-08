"""The loop that connects a source, the policy and the lighting backends.

Two threads:
- The poller thread fetches the source. A slow network never freezes effects.
- The caller's thread renders frames and posts colors to the outputs.
  Each output runs its backend on its own thread (see outputs.py).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

from .domain import FULL_BRIGHTNESS, AlertStatus
from .frame import Frame
from .outputs import Output
from .policy import Policy
from .scenes import Scene
from .tuning import Tuning, TuningWatcher
from .sources import AlertSource

log = logging.getLogger(__name__)

BACKOFF_FACTOR = 2  # the wait doubles after each failed fetch in a row


@dataclass(frozen=True)
class DaemonSettings:
    poll_interval_s: float
    stale_after_s: float
    frame_interval_s: float
    backoff_max_s: float = 0.0  # 0: no backoff, always wait poll_interval_s
    brightness: float = FULL_BRIGHTNESS  # global factor for every color


@dataclass
class _Sample:
    status: AlertStatus
    fetched_at: float  # monotonic


@dataclass
class Daemon:
    source: AlertSource
    policy: Policy
    scenes: dict[str, Scene]
    outputs: Sequence[Output]
    settings: DaemonSettings
    tuning: TuningWatcher | None = None
    monotonic: Callable[[], float] = time.monotonic
    wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    _sample: _Sample | None = field(default=None, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)
    _scene: str | None = field(default=None, init=False)
    _scene_started: float = field(default=0.0, init=False)
    _released: bool = field(default=True, init=False)
    _base_brightness: float | None = field(default=None, init=False)
    _preview_scene: str | None = field(default=None, init=False)
    _preview_until: float = field(default=0.0, init=False)
    _preview_definition: Scene | None = field(default=None, init=False)

    # ---- data side -------------------------------------------------------
    def poll_once(self) -> float | None:
        """Fetch once. Returns None on success, else the server's retry hint (or 0)."""
        try:
            status = self.source.fetch()
        except Exception as error:  # noqa: BLE001  any source failure -> keep old sample
            log.warning("source %s failed: %s", self.source.name, error)
            return float(getattr(error, "retry_after_s", None) or 0)
        with self._lock:
            self._sample = _Sample(status, self.monotonic())
        return None

    def next_poll_delay(self, failures: int, retry_after_s: float) -> float:
        """Wait after a fetch: the poll interval, doubled for each failure in a row."""
        interval = self.settings.poll_interval_s
        if failures == 0 or self.settings.backoff_max_s <= interval:
            return max(interval, retry_after_s)
        backoff = min(interval * BACKOFF_FACTOR ** failures, self.settings.backoff_max_s)
        return max(backoff, min(retry_after_s, self.settings.backoff_max_s))

    def current_status(self) -> AlertStatus | None:
        """The latest status, or None when it is missing or too old to trust."""
        with self._lock:
            sample = self._sample
        if sample is None:
            return None
        if self.monotonic() - sample.fetched_at > self.settings.stale_after_s:
            return None
        observed = sample.status.observed_at
        if observed and (self.wall_clock() - observed).total_seconds() > self.settings.stale_after_s:
            return None
        return sample.status

    # ---- render side -----------------------------------------------------
    def apply_tuning(self, tuning: Tuning) -> None:
        if self._base_brightness is None:
            self._base_brightness = self.settings.brightness
        self.scenes = tuning.scenes
        brightness = self._base_brightness if tuning.brightness is None else tuning.brightness
        self.settings = replace(self.settings, brightness=brightness)
        for output in self.outputs:
            if hasattr(output, "set_tuning"):
                lighting = tuning.lighting.get(output.name)
                output.set_tuning(lighting.brightness if lighting else None,
                                  lighting.correction if lighting else None)
        self._preview_scene, self._preview_until = tuning.preview_scene, tuning.preview_until
        self._preview_definition = tuning.preview_definition
        self._scene = None  # restart the effect with the new values

    def step(self) -> None:
        if self.tuning is not None and (tuning := self.tuning.poll()) is not None:
            self.apply_tuning(tuning)
        status = self.current_status()
        scene = self.policy.select(status)
        previewing = bool(self._preview_scene) and self.wall_clock().timestamp() < self._preview_until
        if previewing:
            scene = self._preview_scene  # the tuner shows a scene for a short time
        now = self.monotonic()
        if scene != self._scene:
            log.info("scene %s -> %s (status: %s)", self._scene, scene, status)
            self._scene, self._scene_started = scene, now

        definition = self.scenes[scene]
        if previewing and self._preview_definition is not None:
            definition = self._preview_definition
        if definition.release:
            self.release_all()
            return
        brightness = self.settings.brightness
        frame = Frame(definition.effect, now - self._scene_started).map(lambda c: c.scaled(brightness))
        self._released = False
        for output in self.outputs:
            output.show(frame)

    def release_all(self) -> None:
        if self._released:
            return
        for output in self.outputs:
            output.release()
        self._released = True

    # ---- lifecycle -------------------------------------------------------
    def run(self, stop: threading.Event) -> None:
        poller = threading.Thread(target=self._poll_loop, args=(stop,), name="poller", daemon=True)
        poller.start()
        log.info("started: source=%s outputs=%s", self.source.name,
                 ", ".join(o.name for o in self.outputs) or "none (poller only: scenes go to the log)")
        try:
            while not stop.is_set():
                self.step()
                stop.wait(self.settings.frame_interval_s)
        finally:
            stop.set()
            for output in self.outputs:
                output.close()  # close() releases the LEDs first
            log.info("stopped")

    def _poll_loop(self, stop: threading.Event) -> None:
        failures = 0
        while not stop.is_set():
            retry_after_s = self.poll_once()
            failures = 0 if retry_after_s is None else failures + 1
            stop.wait(self.next_poll_delay(failures, retry_after_s or 0.0))
