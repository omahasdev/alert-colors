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
from .http import RateLimitedError, no_network
from .outputs import Output
from .policy import Policy
from .scenes import Scene
from .tuning import PREVIEW_MAX_S, Tuning, TuningWatcher
from .sources import AlertSource

log = logging.getLogger(__name__)

BACKOFF_FACTOR = 2  # the wait doubles after each failed fetch in a row
# The doubling stops here (2**32 times the interval is far more than any backoff_max_s): after
# a long outage the count of failures cannot make the wait too large for a float.
MAX_BACKOFF_DOUBLINGS = 32
MIN_TUNING_WAIT_S = 0.01  # the tuning thread never loops faster, also with a frame interval of 0
OFFLINE_LOG_REPEAT_S = 300.0  # during a long "no network" failure, warn again this often


@dataclass(frozen=True)
class DaemonSettings:
    poll_interval_s: float
    stale_after_s: float
    frame_interval_s: float
    backoff_max_s: float = 0.0  # 0: no backoff, always wait poll_interval_s
    # This PC has no network: try again this soon, with no backoff (specs/001-offline-recovery).
    offline_retry_s: float = 0.0  # 0: off, the backoff applies to these errors too
    brightness: float = FULL_BRIGHTNESS  # global factor for every color
    # A vendor app can take the LEDs back without a message (see power_events.py).
    reclaim_interval_s: float = 0.0  # take them again this often; 0: never by time
    reclaim_after_wake_s: float = 0.0  # after a wake event, take them again for this long ...
    reclaim_step_s: float = 1.0  # ... this often


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
    _reclaim_until: float = field(default=0.0, init=False)  # monotonic; set by request_reclaim()
    _last_reclaim: float | None = field(default=None, init=False)
    _window_logged: bool = field(default=False, init=False)  # the first reclaim of a window is logged
    _scene: str | None = field(default=None, init=False)
    _scene_started: float = field(default=0.0, init=False)
    _released: bool = field(default=True, init=False)
    _base_brightness: float | None = field(default=None, init=False)
    _preview_scene: str | None = field(default=None, init=False)
    _preview_until: float = field(default=0.0, init=False)
    _preview_definition: Scene | None = field(default=None, init=False)
    _pending_tuning: Tuning | None = field(default=None, init=False)  # read by poll_tuning(), used by step()
    _tuning_checked: threading.Event = field(default_factory=threading.Event, init=False)  # first read done
    _wake: threading.Event = field(default_factory=threading.Event, init=False)  # fetch now
    _failing: bool = field(default=False, init=False)  # the last fetch failed
    _rate_limited: bool = field(default=False, init=False)  # ... with HTTP 429 or a retry hint
    _last_offline: bool = field(default=False, init=False)  # ... because this PC has no network
    _offline_since: float | None = field(default=None, init=False)  # monotonic
    _offline_logged: float = field(default=0.0, init=False)  # monotonic: the last offline warning
    _last_network_wake: float | None = field(default=None, init=False)  # monotonic

    # ---- data side -------------------------------------------------------
    def poll_once(self) -> float | None:
        """Fetch once. Returns None on success, else the server's retry hint (or 0)."""
        try:
            status = self.source.fetch()
        except Exception as error:  # noqa: BLE001  any source failure -> keep old sample
            self._failing = True
            self._rate_limited = isinstance(error, RateLimitedError) or bool(
                getattr(error, "retry_after_s", None)) or bool(getattr(error, "rate_limited", False))
            self._last_offline = self.settings.offline_retry_s > 0 and (
                no_network(error) or bool(getattr(error, "no_network", False)))
            if not self._last_offline:
                self._offline_since = None
                log.warning("source %s failed: %s", self.source.name, error)
            elif self._offline_since is None:  # one line for each outage: the log stays short
                self._offline_since = self._offline_logged = self.monotonic()
                log.warning("source %s failed: %s; no network: next try at most %ss later, until it works",
                            self.source.name, error, self.settings.offline_retry_s)
            elif self.monotonic() - self._offline_logged >= OFFLINE_LOG_REPEAT_S:
                # Also a wrong host name gives "no network": it must stay visible in the log.
                self._offline_logged = self.monotonic()
                log.warning("source %s still fails after %.0f s: %s", self.source.name,
                            self._offline_logged - self._offline_since, error)
            else:
                log.debug("source %s failed: %s", self.source.name, error)
            return float(getattr(error, "retry_after_s", None) or 0)
        now = self.monotonic()
        if self._offline_since is not None:
            log.info("source %s works again after %.0f s", self.source.name,
                     now - self._offline_since)
        self._failing, self._rate_limited, self._last_offline, self._offline_since = False, False, False, None
        with self._lock:
            self._sample = _Sample(status, now)
        return None

    def network_changed(self) -> None:
        """A network address of this PC changed: while the fetches fail, fetch now.

        Another thread calls it. It does nothing while the fetches succeed, or
        after HTTP 429 (the server gave the wait). It wakes the poller at most
        once each max(offline_retry_s, backoff_max_s): a flapping adapter never
        sends more requests to a slow server than the backoff. offline_retry_s
        0 turns it off.
        """
        if self.settings.offline_retry_s <= 0 or not self._failing or self._rate_limited:
            return
        now = self.monotonic()
        gap_s = max(self.settings.offline_retry_s, self.settings.backoff_max_s)
        if self._last_network_wake is not None and now - self._last_network_wake < gap_s:
            return
        self._last_network_wake = now
        retry_now = getattr(self.source, "retry_now", None)
        if retry_now is not None:
            retry_now()
        log.info("the network changed: fetching the source now")
        self._wake.set()

    def next_poll_delay(self, failures: int, retry_after_s: float) -> float:
        """Wait after a fetch: the poll interval, doubled for each failure in a row."""
        interval = self.settings.poll_interval_s
        if failures == 0 or self.settings.backoff_max_s <= interval:
            return max(interval, retry_after_s)
        backoff = min(interval * BACKOFF_FACTOR ** min(failures, MAX_BACKOFF_DOUBLINGS), self.settings.backoff_max_s)
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
        # A preview ends after PREVIEW_MAX_S at most, whatever "until" the file gives.
        latest = self.wall_clock().timestamp() + PREVIEW_MAX_S
        self._preview_scene, self._preview_until = tuning.preview_scene, min(tuning.preview_until, latest)
        self._preview_definition = tuning.preview_definition
        self._scene = None  # restart the effect with the new values

    def poll_tuning(self) -> None:
        """Read the tuning file when it changed. This is file I/O on a file that the user can
        write, and the user can hold such a read open, so it runs on its own thread
        (run()), never on the render loop: the LEDs and the stop never wait for it."""
        if self.tuning is not None and (tuning := self.tuning.poll()) is not None:
            with self._lock:
                self._pending_tuning = tuning

    def step(self) -> None:
        with self._lock:
            tuning, self._pending_tuning = self._pending_tuning, None
        if tuning is not None:
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
        self._reclaim_if_due(now)

    def request_reclaim(self, reason: str = "display on or resume") -> None:
        """A wake event or a vendor app start (any thread): take the LEDs again for a while.

        The ASUS service can take the LEDs back soon after the event, but apply its
        profile to the devices only some seconds later. So one reclaim is not enough:
        the daemon takes the LEDs again every reclaim_step_s for reclaim_after_wake_s.
        """
        with self._lock:
            self._reclaim_until = self.monotonic() + self.settings.reclaim_after_wake_s
            self._window_logged = False
        log.info("%s: taking the LEDs again for %ss", reason, self.settings.reclaim_after_wake_s)

    def _reclaim_if_due(self, now: float) -> None:
        if self._last_reclaim is None:
            self._last_reclaim = now  # control was just taken with the first frame
            return
        since = now - self._last_reclaim
        with self._lock:
            after_wake = now < self._reclaim_until
        interval = self.settings.reclaim_interval_s
        by_interval = bool(interval) and since >= interval
        if (after_wake and since >= self.settings.reclaim_step_s) or by_interval:
            self._last_reclaim = now
            if by_interval:
                log.info("taking the LEDs again (interval)")
            elif not self._window_logged:  # one line for each window: the log stays short
                log.info("taking the LEDs again (after an event, every %ss)", self.settings.reclaim_step_s)
                self._window_logged = True
            for output in self.outputs:
                output.reclaim()

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
        if self.tuning is not None:
            threading.Thread(target=self._tuning_loop, args=(stop,), name="tuning", daemon=True).start()
            # The first frame uses the tuning; a read that is held open
            # delays it by one frame interval at most.
            self._tuning_checked.wait(self.settings.frame_interval_s)
        log.info("started: source=%s outputs=%s", self.source.name,
                 ", ".join(o.name for o in self.outputs) or "none (poller only: scenes go to the log)")
        try:
            while not stop.is_set():
                self.step()
                stop.wait(self.settings.frame_interval_s)
        finally:
            stop.set()
            self._wake.set()  # the poller sees the stop at once
            for output in self.outputs:
                output.close()  # close() releases the LEDs first
            log.info("stopped")

    def _tuning_loop(self, stop: threading.Event) -> None:
        # The watcher itself waits check_interval_s between two reads of the file. This
        # loop only asks it once each frame interval, at the render-loop rate:
        # one time gate, and no busy loop for check_interval_s = 0.
        while not stop.is_set():
            try:
                self.poll_tuning()
            except Exception:  # noqa: BLE001  a bad read never ends the watch
                log.exception("tuning: the check failed")
            self._tuning_checked.set()
            stop.wait(max(self.settings.frame_interval_s, MIN_TUNING_WAIT_S))

    def _poll_loop(self, stop: threading.Event) -> None:
        failures = 0
        while not stop.is_set():
            retry_after_s = self.poll_once()
            if retry_after_s is None or self._rate_limited:
                # A network wake that came during this fetch: the data came, or the server
                # gave the wait. Either way it is not a reason to fetch again at once.
                self._wake.clear()
            if retry_after_s is None:
                failures = 0
            elif self._last_offline:
                pass  # no server got the request: no backoff
            else:
                failures += 1
            if self._wake.wait(self.poll_delay(failures, retry_after_s)):
                self._wake.clear()  # the backoff stays: a slow server is not asked more often

    def poll_delay(self, failures: int, retry_after_s: float | None) -> float:
        """The wait after a poll: next_poll_delay(). No network: offline_retry_s, but never
        more than the first backoff step, so the offline wait never exceeds the normal backoff."""
        if retry_after_s is not None and self._last_offline:
            return min(self.settings.offline_retry_s, self.next_poll_delay(failures + 1, retry_after_s))
        return self.next_poll_delay(failures, retry_after_s or 0.0)
