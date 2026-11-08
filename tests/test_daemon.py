from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from alert_colors.daemon import Daemon, DaemonSettings
from alert_colors.domain import AlertStatus, Color, Coverage
from alert_colors.effects import Solid
from alert_colors.policy import Policy
from alert_colors.scenes import Scene

GREEN, GRAY, RED = Color(0, 255, 0), Color(9, 9, 9), Color(255, 0, 0)
STALE_S = 100
NOW = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)
FULL, HALF = 1.0, 0.5
RULES = [{"state": "unknown", "scene": "unknown"}, {"active": True, "scene": "alert"}, {"scene": "clear"}]


@dataclass
class FakeSource:
    status: AlertStatus | None = None
    error: Exception | None = None
    name: str = "fake"

    def fetch(self):
        if self.error:
            raise self.error
        return self.status


@dataclass
class FakeBackend:
    name: str = "fake"
    shown: list = field(default_factory=list)
    releases: int = 0
    fail: bool = False

    def show(self, frame):
        if self.fail:
            raise OSError("device gone")
        self.shown.append(frame.color())

    def release(self):
        self.releases += 1

    def close(self):
        self.release()

    def describe(self):
        return []


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def make(source, *backends, release_on_clear=False):
    clock = Clock()
    daemon = Daemon(
        source=source,
        policy=Policy.from_config(RULES, {"clear", "unknown", "alert"}),
        scenes={
            "clear": Scene("clear", release=True) if release_on_clear else Scene("clear", Solid(GREEN)),
            "unknown": Scene("unknown", Solid(GRAY)),
            "alert": Scene("alert", Solid(RED)),
        },
        outputs=list(backends),
        settings=DaemonSettings(
            poll_interval_s=1,
            stale_after_s=STALE_S,
            frame_interval_s=0.1,
            brightness=FULL,
        ),
        monotonic=clock,
        wall_clock=lambda: NOW,
    )
    return daemon, clock


def alert_status(active=True, observed=NOW):
    return AlertStatus(
        Coverage.FULL if active else Coverage.NONE,
        frozenset({"air_raid"}) if active else frozenset(),
        observed_at=observed,
    )


def test_no_data_shows_unknown():
    backend = FakeBackend()
    daemon, _ = make(FakeSource(), backend)
    daemon.step()
    assert backend.shown == [GRAY]


def test_failed_fetch_keeps_last_status_until_stale_then_unknown():
    source, backend = FakeSource(alert_status()), FakeBackend()
    daemon, clock = make(source, backend)
    daemon.poll_once()
    daemon.step()
    source.error = OSError("network down")
    clock.t = STALE_S / 2
    daemon.poll_once()
    daemon.step()
    clock.t = STALE_S + 1
    daemon.poll_once()
    daemon.step()
    assert backend.shown == [RED, RED, GRAY]


def test_old_provider_timestamp_is_unknown_not_clear():
    backend = FakeBackend()
    old = NOW - timedelta(seconds=STALE_S + 1)
    daemon, _ = make(FakeSource(alert_status(active=False, observed=old)), backend)
    daemon.poll_once()
    daemon.step()
    assert backend.shown == [GRAY]


def test_release_on_clear_releases_once():
    backend = FakeBackend()
    daemon, _ = make(FakeSource(alert_status(active=False)), backend, release_on_clear=True)
    daemon.step()  # no data yet: unknown is shown
    daemon.poll_once()
    daemon.step()
    daemon.step()
    assert backend.shown == [GRAY] and backend.releases == 1


def test_global_brightness_scales_colors():
    backend = FakeBackend()
    daemon, _ = make(FakeSource(alert_status()), backend)
    daemon.settings = DaemonSettings(poll_interval_s=1, stale_after_s=STALE_S, frame_interval_s=0.1,
                                     brightness=HALF)
    daemon.poll_once()
    daemon.step()
    assert backend.shown == [RED.scaled(HALF)]


def test_backoff_doubles_up_to_the_limit_and_honors_retry_after():
    daemon, _ = make(FakeSource())
    daemon.settings = DaemonSettings(poll_interval_s=1, stale_after_s=STALE_S, frame_interval_s=0.1,
                                     backoff_max_s=8)
    assert daemon.next_poll_delay(0, 0) == 1
    assert [daemon.next_poll_delay(n, 0) for n in (1, 2, 3, 4)] == [2, 4, 8, 8]
    assert daemon.next_poll_delay(1, 5) == 5     # Retry-After of the server
    assert daemon.next_poll_delay(1, 100) == 8   # never more than backoff_max_s


def test_rate_limit_error_gives_its_retry_hint():
    from alert_colors.http import RateLimitedError

    daemon, _ = make(FakeSource(error=RateLimitedError(7)))
    assert daemon.poll_once() == 7
