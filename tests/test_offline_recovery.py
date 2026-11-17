"""Fast recovery after a network outage (specs/001-offline-recovery)."""
import copy
import errno
import logging
import socket
import threading
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from alert_colors import http, network_events
from alert_colors.config import DEFAULT_OFFLINE_RETRY_S, App
from alert_colors.daemon import Daemon, DaemonSettings
from alert_colors.domain import AlertStatus, Coverage
from alert_colors.http import NameResolutionError, RateLimitedError, no_network, timed_out
from alert_colors.policy import Policy
from alert_colors.sources.failover import AllSourcesFailedError, FailoverSource, Member

ROOT = Path(__file__).resolve().parents[1]
RAW = tomllib.loads((ROOT / "config.toml").read_text(encoding="utf-8"))
AFTER_S, RETRY_S, RETRY_MAX_S, FALLBACK_POLL_S, OFFLINE_S = 30.0, 10.0, 120.0, 10.0, 3.0
NO_SKIP = 0.0  # a "no network" source in an offline round: the daemon's offline wait paces it
POLL_S, BACKOFF_MAX_S, STALE_S = 1.0, 8.0, 100.0
OFFLINE_WAIT_S = min(OFFLINE_S, 2 * POLL_S)  # never more than the first backoff step
RULES = [{"state": "unknown", "scene": "unknown"}, {"scene": "clear"}]
SCENE_NAMES = {"clear", "unknown"}
OK = AlertStatus(coverage=Coverage.NONE, kinds=frozenset(), source="primary")
NO_DNS = NameResolutionError("cannot resolve example.invalid: [Errno 11001] getaddrinfo failed")
SLOW = urllib.error.URLError(TimeoutError("timed out"))
REFUSED_TOKEN = urllib.error.HTTPError("https://example.invalid", 401, "Unauthorized", None, None)


# ---- http: the kind of an error ------------------------------------------------
@pytest.mark.parametrize("error", [
    NO_DNS,
    urllib.error.URLError(socket.gaierror(socket.EAI_NONAME, "getaddrinfo failed")),
    urllib.error.URLError(OSError(errno.ENETUNREACH, "network is unreachable")),
    OSError(errno.EHOSTUNREACH, "no route to host"),
    OSError(None, "network is down", None, errno.ENETDOWN),  # made from a winerror code
])
def test_errors_that_never_left_the_pc_are_no_network(error):
    assert no_network(error)
    assert not timed_out(error)


def test_a_wrapped_no_network_error_is_found_through_its_cause():
    try:
        try:
            raise NO_DNS
        except OSError as inner:
            raise ValueError("fetch failed") from inner
    except ValueError as outer:
        assert no_network(outer)


@pytest.mark.parametrize("error", [
    SLOW,
    TimeoutError("timed out"),
    ConnectionRefusedError(errno.ECONNREFUSED, "refused"),  # the server host answered
    urllib.error.HTTPError("https://example.invalid", 503, "Unavailable", None, None),
    RateLimitedError(5),
    REFUSED_TOKEN,
    ValueError("bad JSON"),
])
def test_server_errors_are_not_no_network(error):
    assert not no_network(error)


def test_timeouts_are_timed_out():
    assert timed_out(SLOW)
    assert timed_out(TimeoutError())
    assert not timed_out(ConnectionRefusedError())


def test_a_dns_failure_raises_name_resolution_error(monkeypatch):
    def fail(*_args, **_kwargs):
        raise socket.gaierror(socket.EAI_NONAME, "getaddrinfo failed")

    monkeypatch.setattr(socket, "getaddrinfo", fail)
    with pytest.raises(NameResolutionError, match="cannot resolve example.invalid"):
        http.check_url("https://example.invalid/", http.REMOTE)


def test_a_name_resolution_error_is_still_an_os_error():
    """Code that catches OSError also catches it."""
    assert issubclass(NameResolutionError, OSError)


# ---- failover -------------------------------------------------------------------
class Source:
    def __init__(self, name, results):
        self.name, self.results, self.calls = name, list(results), 0

    def fetch(self):
        self.calls += 1
        result = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(result, Exception):
            raise result
        return result


def chain(primary_results, *fallback_results, offline_retry_s=OFFLINE_S):
    clock = {"t": 0.0}
    primary = Source("primary", primary_results)
    fallbacks = [Source(f"fallback{i}", results) for i, results in enumerate(fallback_results)]
    source = FailoverSource(Member(primary), [Member(f, poll_interval_s=FALLBACK_POLL_S) for f in fallbacks],
                            after_s=AFTER_S, retry_s=RETRY_S, retry_max_s=RETRY_MAX_S,
                            offline_retry_s=offline_retry_s, monotonic=lambda: clock["t"])
    return source, clock


def at(source, clock, t):
    clock["t"] = t
    return source.fetch()


def skips(source, now):
    return [member.skip_until - now for member in source.members]


def test_the_outage_shape_gives_no_skip_for_no_network_and_keeps_the_timeout_skip():
    """A1: the primary and one fallback: no DNS; the other fallback: a timeout."""
    source, clock = chain([OK, NO_DNS], [SLOW], [NO_DNS])
    at(source, clock, 0)
    with pytest.raises(NameResolutionError):  # too early to switch: the primary only
        at(source, clock, 1)
    assert skips(source, 1)[0] == NO_SKIP
    with pytest.raises(AllSourcesFailedError) as raised:
        at(source, clock, AFTER_S + 1)
    assert raised.value.no_network
    t = AFTER_S + 1
    assert skips(source, t) == [NO_SKIP, RETRY_S, NO_SKIP]
    assert [m.failures for m in source.members] == [0, 1, 0]
    with pytest.raises(AllSourcesFailedError):
        at(source, clock, t + OFFLINE_S)
    assert [m.failures for m in source.members] == [0, 1, 0]  # no doubling of the "no network" sources


def test_the_data_comes_back_at_the_next_short_try():
    """A2"""
    source, clock = chain([OK, NO_DNS, NO_DNS, OK], [SLOW], [NO_DNS])
    at(source, clock, 0)
    for t in (1, 1 + OFFLINE_S):
        with pytest.raises(NameResolutionError):
            at(source, clock, t)
    assert at(source, clock, 1 + 2 * OFFLINE_S) is OK


def test_offline_retry_0_uses_the_normal_backoff_skips():
    """A4"""
    source, clock = chain([OK, NO_DNS], [NO_DNS], offline_retry_s=0)
    at(source, clock, 0)
    with pytest.raises(NameResolutionError):
        at(source, clock, 1)
    assert skips(source, 1)[0] == RETRY_S
    with pytest.raises(NameResolutionError):
        at(source, clock, 1 + RETRY_S)
    assert skips(source, 1 + RETRY_S)[0] == RETRY_S * 2
    with pytest.raises(AllSourcesFailedError) as raised:
        at(source, clock, AFTER_S + 1)
    assert not raised.value.no_network


def test_timeouts_only_keep_the_doubled_skip():
    """A3: a slow server is not asked more often."""
    source, clock = chain([OK, SLOW], [SLOW])
    at(source, clock, 0)
    with pytest.raises(urllib.error.URLError):
        at(source, clock, 1)
    assert skips(source, 1)[0] == RETRY_S
    with pytest.raises(AllSourcesFailedError) as raised:
        at(source, clock, AFTER_S + 1)
    assert not raised.value.no_network
    assert skips(source, AFTER_S + 1) == [RETRY_S * 2, RETRY_S]


def test_one_dead_host_while_a_fallback_works_switches_at_after_s_and_doubles_after():
    """A3: the switch time is the same as with offline_retry_s = 0."""
    def switch_time(offline_retry_s):
        source, clock = chain([OK, NO_DNS], [OK], offline_retry_s=offline_retry_s)
        at(source, clock, 0)
        for t in range(1, int(AFTER_S) * 2):
            try:
                at(source, clock, t)
                return t, source, clock
            except OSError:
                continue
        raise AssertionError("no switch")

    new_t, source, clock = switch_time(OFFLINE_S)
    old_t, _source, _clock = switch_time(0)
    assert new_t == old_t == AFTER_S
    primary = source.primary
    # In the switch round the fallback worked, so it was no offline round: the primary's
    # failure there counts, and the normal doubling goes on from the switch.
    assert primary.skip_until - new_t == RETRY_S
    t = primary.skip_until
    assert at(source, clock, t) is OK  # the fallback still works
    assert primary.skip_until - t == 2 * RETRY_S


def test_http_429_keeps_its_skip_and_is_no_offline_round():
    """A3"""
    source, clock = chain([OK, RateLimitedError(20)], [NO_DNS])
    at(source, clock, 0)
    with pytest.raises(RateLimitedError):
        at(source, clock, 1)
    assert skips(source, 1)[0] == 20
    source.retry_now()
    with pytest.raises(AllSourcesFailedError) as raised:
        at(source, clock, 2)
    assert source.primary.skip_until == 21  # a network change does not clear it
    assert not raised.value.no_network


def test_a_refused_token_keeps_its_skip_and_does_not_block_the_offline_round():
    source, clock = chain([REFUSED_TOKEN], [NO_DNS])
    with pytest.raises(AllSourcesFailedError) as raised:
        at(source, clock, 1)
    assert raised.value.no_network
    assert skips(source, 1) == [RETRY_MAX_S, NO_SKIP]
    source.retry_now()
    with pytest.raises(AllSourcesFailedError):
        at(source, clock, 2)
    assert source.primary.skip_until == 1 + RETRY_MAX_S


def test_retry_now_clears_the_skips_of_failed_sources_at_the_next_fetch():
    source, clock = chain([OK, SLOW, OK], [SLOW])
    at(source, clock, 0)
    with pytest.raises(urllib.error.URLError):
        at(source, clock, 1)
    source.retry_now()
    assert source.primary.skip_until == 1 + RETRY_S  # only the fetch thread changes it
    assert at(source, clock, 2) is OK


def test_one_log_line_for_each_source_and_outage(caplog):
    """The daemon logs the outage; failover logs each source once, not at each try."""
    source, clock = chain([OK, NO_DNS], [NO_DNS])
    at(source, clock, 0)
    caplog.set_level(logging.WARNING, logger="alert_colors.sources.failover")
    t = AFTER_S + 1
    for _ in range(5):
        with pytest.raises(AllSourcesFailedError):
            at(source, clock, t)
        t += OFFLINE_S
    lines = [r.getMessage() for r in caplog.records]
    assert sum("primary failed" in line for line in lines) == 1
    assert sum("fallback0 failed" in line for line in lines) == 1


# ---- daemon -------------------------------------------------------------------
class DaemonSource:
    name = "fake"

    def __init__(self, results):
        self.results, self.retries = list(results), 0

    def fetch(self):
        result = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(result, Exception):
            raise result
        return result

    def retry_now(self):
        self.retries += 1


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def daemon_with(results, offline_retry_s=OFFLINE_S):
    clock = Clock()
    daemon = Daemon(
        source=DaemonSource(results),
        policy=Policy.from_config([{"state": "unknown", "scene": "unknown"}, {"scene": "clear"}],
                                  {"clear", "unknown"}),
        scenes={},
        outputs=[],
        settings=DaemonSettings(poll_interval_s=POLL_S, stale_after_s=STALE_S, frame_interval_s=0.1,
                                backoff_max_s=BACKOFF_MAX_S, offline_retry_s=offline_retry_s),
        monotonic=clock,
    )
    return daemon, clock


class RecordingWake:
    """Replaces the wake event of the poller: records each wait, stops after `polls`."""

    def __init__(self, stop, polls, woken=()):
        self.stop, self.polls, self.woken, self.delays, self.cleared = stop, polls, set(woken), [], 0

    def wait(self, delay):
        self.delays.append(delay)
        if len(self.delays) >= self.polls:
            self.stop.set()
        return len(self.delays) in self.woken

    def clear(self):
        self.cleared += 1

    def set(self):
        pass


def poll_delays(results, polls, offline_retry_s=OFFLINE_S, woken=()):
    daemon, _clock = daemon_with(results, offline_retry_s)
    stop = threading.Event()
    daemon._wake = RecordingWake(stop, polls, woken)
    daemon._poll_loop(stop)
    return daemon._wake


def test_no_network_waits_offline_retry_s_and_the_backoff_does_not_grow():
    """A1 for the daemon: then a timeout starts the backoff from the start."""
    wake = poll_delays([NO_DNS, NO_DNS, NO_DNS, SLOW, SLOW, OK], polls=6)
    assert wake.delays == [OFFLINE_WAIT_S] * 3 + [2 * POLL_S, 4 * POLL_S, POLL_S]


def test_an_offline_round_of_failover_waits_offline_retry_s():
    wake = poll_delays([AllSourcesFailedError("x", no_network=True)], polls=2)
    assert wake.delays == [OFFLINE_WAIT_S, OFFLINE_WAIT_S]


def test_other_errors_keep_the_backoff():
    """A3"""
    wake = poll_delays([AllSourcesFailedError("x", no_network=False)], polls=4)
    assert wake.delays == [2 * POLL_S, 4 * POLL_S, BACKOFF_MAX_S, BACKOFF_MAX_S]
    assert poll_delays([RateLimitedError(5)], polls=2).delays == [5, 5]


def test_offline_retry_0_uses_the_normal_backoff_for_no_network():
    """A4"""
    wake = poll_delays([NO_DNS], polls=4, offline_retry_s=0)
    assert wake.delays == [2 * POLL_S, 4 * POLL_S, BACKOFF_MAX_S, BACKOFF_MAX_S]


def test_a_wake_does_not_reset_the_backoff():
    wake = poll_delays([SLOW], polls=3, woken={1})
    assert wake.cleared == 1
    assert wake.delays == [2 * POLL_S, 4 * POLL_S, BACKOFF_MAX_S]


def test_the_daemon_logs_the_start_and_the_end_of_an_outage(caplog):
    daemon, clock = daemon_with([NO_DNS, NO_DNS, NO_DNS, OK])
    caplog.set_level(logging.DEBUG, logger="alert_colors.daemon")
    for t in range(4):
        clock.t = t * OFFLINE_S
        daemon.poll_once()
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    infos = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert len(warnings) == 1 and "no network" in warnings[0].getMessage()
    assert any("works again after 9 s" in line for line in infos)


def test_stale_data_still_gives_unknown_during_an_outage():
    """A3"""
    daemon, clock = daemon_with([OK, NO_DNS])
    daemon.poll_once()
    clock.t = STALE_S - 1
    daemon.poll_once()
    assert daemon.current_status() is OK
    clock.t = STALE_S + 1
    daemon.poll_once()
    assert daemon.current_status() is None


# ---- timeline: daemon + failover, each fetch takes time --------------------------
SHIPPED_POLL_S, SHIPPED_BACKOFF_MAX_S, SHIPPED_STALE_S = 1.0, 60.0, 180.0
REQUEST_TIMEOUT_S = 10.0  # the time limit of one request of the presets
ONLINE_FETCH_S = 0.2


class TimedSource:
    """Fails until `back_at`, and each failed fetch takes `cost_s` of the clock."""

    def __init__(self, name, clock, back_at, error, cost_s):
        self.name, self.clock, self.back_at, self.error, self.cost_s = name, clock, back_at, error, cost_s

    def fetch(self):
        if self.clock.t < self.back_at:
            self.clock.t += self.cost_s
            raise self.error
        self.clock.t += ONLINE_FETCH_S
        return OK


class ClockWake:
    def __init__(self, clock, stop, daemon):
        self.clock, self.stop, self.daemon = clock, stop, daemon

    def wait(self, delay):
        self.clock.t += delay
        if self.daemon._sample is not None:
            self.stop.set()
        return False

    def clear(self):
        pass

    def set(self):
        pass


def recovery_s(back_at, offline_retry_s, lookup_s):
    """The time from "network back" to data, for the outage shape of A1."""
    clock = Clock()
    members = [Member(TimedSource("primary", clock, back_at, NO_DNS, lookup_s))] + [
        Member(TimedSource(name, clock, back_at, error, cost), poll_interval_s=FALLBACK_POLL_S)
        for name, error, cost in (("slow", SLOW, REQUEST_TIMEOUT_S), ("nodns", NO_DNS, lookup_s))]
    source = FailoverSource(members[0], members[1:], after_s=AFTER_S, retry_s=RETRY_S,
                            retry_max_s=RETRY_MAX_S, offline_retry_s=offline_retry_s, monotonic=clock)
    daemon = Daemon(
        source=source, policy=Policy.from_config(RULES, SCENE_NAMES), scenes={}, outputs=[],
        settings=DaemonSettings(poll_interval_s=SHIPPED_POLL_S, stale_after_s=SHIPPED_STALE_S,
                                frame_interval_s=0.1, backoff_max_s=SHIPPED_BACKOFF_MAX_S,
                                offline_retry_s=offline_retry_s),
        monotonic=clock)
    stop = threading.Event()
    daemon._wake = ClockWake(clock, stop, daemon)
    daemon._poll_loop(stop)
    return daemon._sample.fetched_at - back_at


BACK_TIMES = range(40, 400, 7)


@pytest.mark.parametrize("lookup_s", [0.05, REQUEST_TIMEOUT_S])  # a fast or a slow failed lookup
def test_the_data_comes_back_soon_after_the_network(lookup_s):
    """A2 with time: the limit is one request in progress plus offline_retry_s."""
    new = [recovery_s(t, OFFLINE_S, lookup_s) for t in BACK_TIMES]
    old = [recovery_s(t, 0, lookup_s) for t in BACK_TIMES]
    assert max(new) <= max(lookup_s, REQUEST_TIMEOUT_S) + OFFLINE_S + ONLINE_FETCH_S
    assert max(new) < max(old)
    assert sum(new) < sum(old)


def test_with_fast_lookups_the_data_comes_back_within_offline_retry_s():
    """No source in this outage times out: only the short retry is left."""
    def recovery(back_at):
        clock = Clock()
        members = [Member(TimedSource("primary", clock, back_at, NO_DNS, 0.05)),
                   Member(TimedSource("nodns", clock, back_at, NO_DNS, 0.05), poll_interval_s=FALLBACK_POLL_S)]
        source = FailoverSource(members[0], members[1:], after_s=AFTER_S, retry_s=RETRY_S,
                                retry_max_s=RETRY_MAX_S, offline_retry_s=OFFLINE_S, monotonic=clock)
        daemon = Daemon(
            source=source, policy=Policy.from_config(RULES, SCENE_NAMES), scenes={},
            outputs=[], monotonic=clock,
            settings=DaemonSettings(poll_interval_s=SHIPPED_POLL_S, stale_after_s=SHIPPED_STALE_S,
                                    frame_interval_s=0.1, backoff_max_s=SHIPPED_BACKOFF_MAX_S,
                                    offline_retry_s=OFFLINE_S))
        stop = threading.Event()
        daemon._wake = ClockWake(clock, stop, daemon)
        daemon._poll_loop(stop)
        return daemon._sample.fetched_at - back_at

    assert max(recovery(t) for t in BACK_TIMES) <= OFFLINE_S + ONLINE_FETCH_S


def test_the_offline_wait_is_never_longer_than_the_first_backoff_step():
    """A3: without backoff_max_s, the first wait after a failure is poll_interval_s."""
    daemon, _clock = daemon_with([NO_DNS])
    daemon.settings = DaemonSettings(poll_interval_s=POLL_S, stale_after_s=STALE_S, frame_interval_s=0.1,
                                     offline_retry_s=OFFLINE_S)  # backoff_max_s not set
    stop = threading.Event()
    daemon._wake = RecordingWake(stop, 3)
    daemon._poll_loop(stop)
    assert daemon._wake.delays == [POLL_S, POLL_S, POLL_S]


def test_while_offline_a_round_sends_one_request():
    source, clock = chain([OK, NO_DNS], [NO_DNS], [NO_DNS])
    at(source, clock, 0)
    with pytest.raises(AllSourcesFailedError):
        at(source, clock, AFTER_S + 1)  # the first offline round tries all
    calls = [m.source.calls for m in source.members]
    with pytest.raises(AllSourcesFailedError) as raised:
        at(source, clock, AFTER_S + 1 + OFFLINE_S)
    assert raised.value.no_network
    assert sum(m.source.calls for m in source.members) - sum(calls) == 1


def test_while_offline_a_server_error_lets_the_round_go_on():
    """The network works again when a server answers: the fallbacks are tried."""
    server_error = urllib.error.HTTPError("https://example.invalid", 500, "Error", None, None)
    source, clock = chain([OK, NO_DNS, server_error], [NO_DNS, OK])
    at(source, clock, 0)
    with pytest.raises(AllSourcesFailedError):
        at(source, clock, AFTER_S + 1)
    assert at(source, clock, AFTER_S + 1 + OFFLINE_S) is OK


# ---- daemon: network changes ---------------------------------------------------
def test_a_network_change_while_data_flows_does_nothing():
    """A5"""
    daemon, _clock = daemon_with([OK])
    daemon.poll_once()
    daemon.network_changed()
    assert not daemon._wake.is_set()
    assert daemon.source.retries == 0


def test_a_network_change_while_fetches_fail_fetches_now():
    daemon, _clock = daemon_with([SLOW])
    daemon.poll_once()
    daemon.network_changed()
    assert daemon._wake.is_set()
    assert daemon.source.retries == 1


def test_a_burst_of_network_changes_wakes_once_each_gap():
    """A5: the gap is max(offline_retry_s, backoff_max_s)."""
    daemon, clock = daemon_with([SLOW])
    daemon.poll_once()
    for t in (0, 1, 2, BACKOFF_MAX_S - 1):
        clock.t = t
        daemon.network_changed()
    assert daemon.source.retries == 1
    clock.t = BACKOFF_MAX_S
    daemon.network_changed()
    assert daemon.source.retries == 2


def test_a_network_change_after_http_429_does_nothing():
    daemon, _clock = daemon_with([RateLimitedError(30)])
    daemon.poll_once()
    daemon.network_changed()
    assert not daemon._wake.is_set()


def test_a_network_change_with_offline_retry_0_does_nothing():
    daemon, _clock = daemon_with([NO_DNS], offline_retry_s=0)
    daemon.poll_once()
    daemon.network_changed()
    assert not daemon._wake.is_set()


def test_a_source_without_retry_now_still_wakes():
    daemon, _clock = daemon_with([SLOW])
    daemon.source = type("Plain", (), {"name": "plain", "fetch": lambda self: (_ for _ in ()).throw(SLOW)})()
    daemon.poll_once()
    daemon.network_changed()
    assert daemon._wake.is_set()


# ---- network events ------------------------------------------------------------
def run_watcher(results, on_change):
    done = threading.Event()
    queue = list(results)

    def wait_for_change():
        if not queue:
            done.set()
            return 1  # an error code: the watcher ends
        return queue.pop(0)

    stop = network_events.watch(on_change, wait_for_change)
    assert done.wait(5)
    return stop


def test_each_address_change_calls_on_change():
    calls = []
    run_watcher([network_events.NO_ERROR, network_events.NO_ERROR], lambda: calls.append(1))
    assert len(calls) == 2


def test_a_failed_handler_does_not_end_the_watch():
    calls = []

    def handler():
        calls.append(1)
        raise RuntimeError("boom")

    run_watcher([network_events.NO_ERROR, network_events.NO_ERROR], handler)
    assert len(calls) == 2


def test_after_stop_no_call():
    calls, release = [], threading.Event()
    entered = threading.Event()

    def wait_for_change():
        entered.set()
        release.wait(5)
        return network_events.NO_ERROR

    stop = network_events.watch(lambda: calls.append(1), wait_for_change)
    assert entered.wait(5)
    stop()
    release.set()
    watcher_threads = [t for t in threading.enumerate() if t.name == "network-events"]
    for thread in watcher_threads:
        thread.join(5)
    assert calls == []


def test_a_watcher_that_cannot_start_returns_a_no_op(monkeypatch):
    monkeypatch.setattr(network_events.sys, "platform", "win32")

    def broken():
        raise OSError("no iphlpapi")

    monkeypatch.setattr(network_events, "_notify_addr_change", broken)
    network_events.watch(lambda: None)()  # no exception


# ---- config ---------------------------------------------------------------------
def app_with(**source_keys):
    raw = copy.deepcopy(RAW)
    raw["source"].pop("offline_retry_s", None)
    raw["source"].update(source_keys)
    raw["failover"] = {"sources": [{"type": "ubilling", "vars": {"oblast_name": "Тестова область"}}]}
    return App(raw, ROOT)


def test_an_old_config_without_the_key_gets_the_default():
    app = app_with()
    assert app.settings().offline_retry_s == DEFAULT_OFFLINE_RETRY_S
    source = app.alert_source()
    assert isinstance(source, FailoverSource)
    assert source.offline_retry_s == DEFAULT_OFFLINE_RETRY_S


@pytest.mark.parametrize(("value", "expected"), [(0, 0.0), (-5, 0.0), (7, 7.0)])
def test_the_key_reaches_the_daemon_and_failover(value, expected):
    app = app_with(offline_retry_s=value)
    assert app.settings().offline_retry_s == expected
    assert app.alert_source().offline_retry_s == expected


def test_the_shipped_config_has_the_key():
    assert RAW["source"]["offline_retry_s"] == DEFAULT_OFFLINE_RETRY_S


# ---- log volume and retry interval during an outage ----------------------------------
def test_a_long_outage_logs_each_source_once_also_a_source_that_times_out(caplog):
    """The A1 shape for 10 minutes: one warning for each source at the start, not one each try."""
    source, clock = chain([OK, NO_DNS], [SLOW], [NO_DNS])
    at(source, clock, 0)
    caplog.set_level(logging.WARNING, logger="alert_colors.sources.failover")
    t = AFTER_S + 1
    while t < AFTER_S + 600:
        with pytest.raises(OSError):
            at(source, clock, t)
        t += OFFLINE_WAIT_S
    lines = [r.getMessage() for r in caplog.records]
    for name in ("primary", "fallback0", "fallback1"):
        assert sum(line.startswith(f"source {name} failed") for line in lines) == 1, (name, lines)


@pytest.mark.parametrize("lookup_s", [0.05])  # a fast failed lookup: only the waits count
def test_in_the_first_after_s_the_data_comes_back_within_offline_retry_s(lookup_s):
    """A skip of its own (3 s) against a poll wait of 2 s would give a 4 s interval."""
    def recovery(back_at):
        clock = Clock()
        members = [Member(TimedSource("primary", clock, back_at, NO_DNS, lookup_s)),
                   Member(TimedSource("nodns", clock, back_at, NO_DNS, lookup_s), poll_interval_s=FALLBACK_POLL_S)]
        failover = FailoverSource(members[0], members[1:], after_s=AFTER_S, retry_s=RETRY_S,
                                  retry_max_s=RETRY_MAX_S, offline_retry_s=OFFLINE_S, monotonic=clock)
        daemon = Daemon(
            source=failover, policy=Policy.from_config(RULES, SCENE_NAMES), scenes={}, outputs=[], monotonic=clock,
            settings=DaemonSettings(poll_interval_s=SHIPPED_POLL_S, stale_after_s=SHIPPED_STALE_S,
                                    frame_interval_s=0.1, backoff_max_s=SHIPPED_BACKOFF_MAX_S,
                                    offline_retry_s=OFFLINE_S))
        stop = threading.Event()
        daemon._wake = ClockWake(clock, stop, daemon)
        daemon._poll_loop(stop)
        return daemon._sample.fetched_at - back_at

    worst = max(recovery(back_at / 10) for back_at in range(10, int(AFTER_S) * 10, 7))
    assert worst <= OFFLINE_S + ONLINE_FETCH_S


# ---- log of a dead host name while the network works -----------------------------------
def test_a_dead_primary_host_is_logged_again_while_a_fallback_works(caplog):
    """Only an offline round hides a repeated failure. Here the network works (the fallback
    answers), so a wrong host name of the primary must stay visible at each retry."""
    source, clock = chain([OK, NO_DNS], [OK])
    at(source, clock, 0)
    caplog.set_level(logging.WARNING, logger="alert_colors.sources.failover")
    hours_s = 2 * 3600
    t = 1.0
    while t < hours_s:
        try:
            at(source, clock, t)
        except OSError:
            pass  # the first after_s: too early to switch
        t += OFFLINE_WAIT_S
    primary_lines = [r for r in caplog.records if r.getMessage().startswith("source primary failed")]
    retries_after_the_switch = (hours_s - AFTER_S) / RETRY_MAX_S
    assert len(primary_lines) >= retries_after_the_switch - 2


# ---- a server answer means the network works -----------------------------------------
def test_a_server_error_of_a_fallback_makes_the_round_not_offline():
    """The primary host does not resolve, but a fallback server answers HTTP 500: the network
    works, so the normal backoff and the log of each failure apply (spec, rule 2 and A3)."""
    import urllib.error

    server_error = urllib.error.HTTPError("https://example.invalid", 500, "Error", None, None)
    source, clock = chain([OK, NO_DNS], [server_error])
    at(source, clock, 0)
    with pytest.raises(AllSourcesFailedError) as raised:
        at(source, clock, AFTER_S + 1)
    assert not raised.value.no_network
    assert not source._offline
    assert skips(source, AFTER_S + 1) == [RETRY_S, RETRY_S]  # the doubling starts: no short retry


# ---- the offline pick never chooses a source whose token was refused ---------------------
def test_while_offline_a_due_source_with_a_refused_token_is_not_fetched():
    source, clock = chain([REFUSED_TOKEN], [NO_DNS])
    with pytest.raises(AllSourcesFailedError) as raised:
        at(source, clock, 1)  # the token is refused, the fallback has no network
    assert raised.value.no_network
    primary_calls = source.primary.source.calls
    with pytest.raises(AllSourcesFailedError):
        at(source, clock, 1 + RETRY_MAX_S + 1)  # the skip of the refused token has ended
    assert source.primary.source.calls == primary_calls  # still offline: the fallback had the turn


# ---- the network watcher on other systems -----------------------------------------------
def test_on_another_system_the_network_watcher_starts_nothing(monkeypatch, caplog):
    from alert_colors import network_events

    started = []
    monkeypatch.setattr(network_events.sys, "platform", "linux")
    monkeypatch.setattr(network_events, "_notify_addr_change", lambda: started.append(1))
    caplog.set_level(logging.DEBUG, logger="alert_colors.network_events")
    stop = network_events.watch(lambda: None)
    stop()
    assert started == []
    assert caplog.records == []  # no traceback: nothing was tried


# ---- a wake during a fetch, and after a recovery ---------------------------------
@pytest.mark.parametrize(("answer", "cleared"), [(OK, 1), (RateLimitedError(30), 1), (NO_DNS, 0), (SLOW, 0)])
def test_a_wake_that_came_during_a_fetch_is_dropped_after_data_or_429(answer, cleared):
    """A5: after data or HTTP 429, the next request waits as usual. After a failure the
    wake stays: the network can be back."""
    assert poll_delays([answer], polls=1).cleared == cleared


def test_after_a_recovery_a_network_change_does_nothing():
    """A5 after an outage: the data flows again, so a change gives no extra request."""
    daemon, _clock = daemon_with([SLOW, OK])
    daemon.poll_once()
    daemon.poll_once()
    daemon.network_changed()
    assert not daemon._wake.is_set()


def test_the_tuning_thread_reports_its_first_read():
    """run() waits for this signal before the first frame."""
    daemon, _clock = daemon_with([OK])
    daemon.tuning = type("NoChange", (), {"poll": lambda self: None})()
    stop = threading.Event()
    thread = threading.Thread(target=daemon._tuning_loop, args=(stop,), daemon=True)
    thread.start()
    try:
        assert daemon._tuning_checked.wait(5)
    finally:
        stop.set()
        thread.join(timeout=5)


# ---- the wait of HTTP 429, and the name resolve under a proxy ---------------------
@pytest.mark.parametrize(("header", "wait"), [
    ("30", 30.0), ("inf", None), ("nan", None), ("-5", None), ("1e300", http.MAX_RETRY_AFTER_S),
    ("Wed, 21 Oct 2026 07:28:00 GMT", None),
])
def test_retry_after_is_a_finite_wait_with_a_limit(header, wait):
    error = urllib.error.HTTPError("https://example.invalid", 429, "Too Many", {"Retry-After": header}, None)
    assert http._retry_after_s(error) == wait


def test_with_a_proxy_the_proxy_resolves_the_host(monkeypatch):
    """A PC with no outside DNS of its own works through its proxy."""
    def no_dns(*_args, **_kwargs):
        raise socket.gaierror(11001, "no DNS on this PC")

    monkeypatch.setattr(socket, "getaddrinfo", no_dns)
    monkeypatch.setattr(http, "_proxies", lambda opener: {"https": "http://proxy.example:8080"})
    monkeypatch.setattr(urllib.request, "proxy_bypass", lambda host: False)
    http.check_url("https://alerts.example/api", http.REMOTE)  # no resolve on this PC
    with pytest.raises(NameResolutionError):
        http.check_url("http://alerts.example/api", http.NetworkPolicy(allow_http=True))  # direct
    monkeypatch.setattr(urllib.request, "proxy_bypass", lambda host: True)
    with pytest.raises(NameResolutionError):
        http.check_url("https://alerts.example/api", http.REMOTE)  # the host bypasses the proxy


def test_an_address_in_the_url_is_checked_also_with_a_proxy(monkeypatch):
    monkeypatch.setattr(http, "_proxies", lambda opener: {"https": "http://proxy.example:8080"})
    monkeypatch.setattr(urllib.request, "proxy_bypass", lambda host: False)
    with pytest.raises(http.UnsafeRequestError):
        http.check_url("https://192.168.1.1/api", http.REMOTE)


def test_the_proxy_bypass_sees_the_host_with_its_port(monkeypatch):
    """The same value as ProxyHandler: a bypass for host:port sends the request direct, so
    the address is checked on this PC."""
    seen = []

    def bypass(host):
        seen.append(host)
        return host == "alerts.example:8443"

    monkeypatch.setattr(http, "_proxies", lambda opener: {"https": "http://proxy.example:8080"})
    monkeypatch.setattr(urllib.request, "proxy_bypass", bypass)
    monkeypatch.setattr(http, "_resolve", lambda host, port: {http.ipaddress.ip_address("10.0.0.1")})
    with pytest.raises(http.UnsafeRequestError):
        http.check_url("https://alerts.example:8443/api", http.REMOTE)
    assert seen == ["alerts.example:8443"]


def test_a_loopback_backend_is_checked_on_this_pc_also_with_a_proxy(monkeypatch):
    """A loopback-only request never goes through the proxy, so its address check stays."""
    monkeypatch.setattr(http, "_proxies", lambda opener: {"https": "http://proxy.example:8080"})
    monkeypatch.setattr(urllib.request, "proxy_bypass", lambda host: False)
    monkeypatch.setattr(http, "_resolve", lambda host, port: {http.ipaddress.ip_address("10.0.0.1")})
    with pytest.raises(http.UnsafeRequestError, match="not a loopback address"):
        http.check_url("https://sdk.example/api", http.LOOPBACK)


# ---- plain http: the address that the connection reached ---------------------------------
@pytest.fixture
def local_json_server():
    import http.server

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


def test_a_plain_http_connection_that_reaches_a_local_address_is_refused(monkeypatch, local_json_server):
    """The check saw a public address, but the connect resolved the name again to this PC.
    The fake resolver gives check_url() a public address; "localhost" sends the connect
    to 127.0.0.1."""
    monkeypatch.setattr(http, "_resolve", lambda host, port: {http.ipaddress.ip_address("93.184.216.34")})
    with pytest.raises(http.UnsafeRequestError, match="local or private"):
        http.request_json(f"http://localhost:{local_json_server}/", timeout_s=5,
                          policy=http.NetworkPolicy(allow_http=True))


def test_a_loopback_backend_still_reaches_its_local_server(local_json_server):
    assert http.local_request_json(f"http://127.0.0.1:{local_json_server}/", timeout_s=5) == {"ok": True}


# ---- HTTP 429 of the primary while fallbacks exist ---------------------------------------
def test_a_long_429_wait_of_the_primary_still_lets_the_fallback_take_over_at_after_s():
    """The primary keeps its long wait; the daemon polls again when the fallback may take over."""
    fallback_ok = AlertStatus(coverage=Coverage.FULL, kinds=frozenset({"air_raid"}), source="fallback0")
    source, clock = chain([OK, RateLimitedError(3600)], [fallback_ok])
    at(source, clock, 0)
    with pytest.raises(RateLimitedError) as raised:
        at(source, clock, 5)
    assert raised.value.retry_after_s == AFTER_S - 5  # not 3600
    assert at(source, clock, AFTER_S) is fallback_ok  # the primary is still in its wait


def test_a_429_of_a_single_source_keeps_the_wait_of_the_server():
    """No fallback: the server's wait (capped by the HTTP layer) is the wait of the daemon."""
    source, clock = chain([OK, RateLimitedError(600)])
    at(source, clock, 0)
    with pytest.raises(RateLimitedError) as raised:
        at(source, clock, 5)
    assert raised.value.retry_after_s == 600


# ---- a very long outage -------------------------------------------------------------------
def test_the_backoff_stays_at_its_maximum_after_any_number_of_failures():
    """Thousands of failures in a row (days of outage) must not overflow the wait."""
    daemon, _clock = daemon_with([SLOW])
    assert daemon.next_poll_delay(5000, 0.0) == BACKOFF_MAX_S


def test_the_skip_of_a_source_stays_at_its_maximum_after_any_number_of_failures():
    source, clock = chain([SLOW], [OK])
    member = source.members[0]
    member.failures = 5000
    source._failure(member, SLOW, 0.0)
    assert member.skip_until <= RETRY_MAX_S


# ---- one request has a total time limit --------------------------------------------------
@pytest.fixture
def trickle_server():
    """Sends its answer one byte at a time: each read is fast, the whole answer is not."""
    import http.server
    import time

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = b'{"ok": true}' + b" " * 200
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            for byte in body:
                try:
                    self.wfile.write(bytes([byte]))
                    self.wfile.flush()
                except OSError:
                    return
                time.sleep(0.05)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


def test_a_server_that_sends_one_byte_at_a_time_cannot_hold_the_poller(trickle_server):
    import time

    timeout_s = 0.5  # each byte comes far sooner; the whole answer takes approximately 10 s
    started = time.monotonic()
    with pytest.raises(TimeoutError, match="no complete answer"):
        http.local_request_json(f"http://127.0.0.1:{trickle_server}/", timeout_s=timeout_s)
    assert time.monotonic() - started < timeout_s * http.REQUEST_DEADLINE_FACTOR + 2


def test_a_request_that_ends_in_time_is_not_affected(local_json_server):
    assert http.local_request_json(f"http://127.0.0.1:{local_json_server}/", timeout_s=5) == {"ok": True}
    assert http._request.sockets is None  # nothing left of the request


def drip_server(answer, tls_context=None):
    """A local server that sends `answer` one byte at a time; returns (port, close)."""
    import time

    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(5)
    done = threading.Event()

    def drip(conn):
        try:
            if tls_context is not None:
                conn = tls_context.wrap_socket(conn, server_side=True)
            conn.recv(4096)
            for byte in answer:
                if done.is_set():
                    return
                conn.send(bytes([byte]))
                time.sleep(0.05)
        except OSError:
            pass
        finally:
            conn.close()

    def serve():
        while not done.is_set():
            try:
                conn, _ = server.accept()
            except OSError:
                return
            threading.Thread(target=drip, args=(conn,), daemon=True).start()

    threading.Thread(target=serve, daemon=True).start()

    def close():
        done.set()
        server.close()

    return server.getsockname()[1], close


SLOW_BODY = b"HTTP/1.1 200 OK\r\nContent-Length: 400\r\n\r\n" + b" " * 400
TIMEOUT_S = 0.5  # each byte comes far sooner; the whole answer takes approximately 20 s


def ends_in_time(call):
    import time

    started = time.monotonic()
    with pytest.raises(TimeoutError, match="no complete answer"):
        call()
    assert time.monotonic() - started < TIMEOUT_S * http.REQUEST_DEADLINE_FACTOR + 2


def test_a_proxy_that_answers_the_tunnel_one_byte_at_a_time_cannot_hold_the_poller(monkeypatch):
    """The proxy of the user (HKCU) reads the CONNECT answer before any TLS: this phase has the limit too."""
    port, close = drip_server(b"HTTP/1.1 200 Connection established\r\n" + b"X-Slow: " + b"a" * 400 + b"\r\n\r\n")
    try:
        proxy = f"http://127.0.0.1:{port}"
        opener = urllib.request.build_opener(http._NoRedirect(), urllib.request.ProxyHandler({"https": proxy}),
                                             http._LazyTLSHandler())
        monkeypatch.setattr(http, "_OPENER", opener)
        monkeypatch.setattr(urllib.request, "proxy_bypass", lambda host: False)
        ends_in_time(lambda: http.request_json("https://alerts.example/api", timeout_s=TIMEOUT_S))
    finally:
        close()


def test_an_https_server_that_sends_one_byte_at_a_time_cannot_hold_the_poller(monkeypatch):
    import ssl
    import sys
    from pathlib import Path

    cert = Path(sys.base_prefix) / "Lib" / "test" / "certdata" / "keycert.pem"
    if not cert.exists():
        pytest.skip("no test certificate in this Python")
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(cert)
    monkeypatch.setattr(http._LazyTLSHandler, "_shared", ssl.create_default_context(cafile=str(cert)))
    port, close = drip_server(SLOW_BODY, server_context)
    try:
        ends_in_time(lambda: http.request_json(f"https://localhost:{port}/x", timeout_s=TIMEOUT_S,
                                               policy=http.LOOPBACK))
    finally:
        close()


def test_a_body_without_a_length_that_is_cut_at_the_limit_is_a_timeout_not_bad_json():
    """Without Content-Length the answer has no known end: when the limit cuts it, the request
    says so (a timeout), not a JSON error of the part that came."""
    port, close = drip_server(b"HTTP/1.0 200 OK\r\n\r\n" + b'{"ok": ' + b" " * 400 + b"true}")
    try:
        ends_in_time(lambda: http.local_request_json(f"http://127.0.0.1:{port}/", timeout_s=TIMEOUT_S))
    finally:
        close()


def test_a_socket_recorded_after_the_limit_is_shut_down_at_once():
    left, right = socket.socketpair()
    try:
        deadline = http._DeadlineSockets()
        deadline.shut_down()  # the limit passed during the connect
        http._request.sockets = deadline
        http._record_socket(left)
        try:
            right.sendall(b"data")
            received = left.recv(16)
        except OSError:  # Windows: the connection was aborted by the shut down
            received = b""
        assert received == b""  # shut down: no read of the answer starts
    finally:
        http._request.sockets = None
        left.close()
        right.close()


# ---- a fallback keeps its retry skip; a success clears a source's marks -------------------
def test_a_failed_fallback_is_not_fetched_again_before_its_skip_time():
    """A3: a fallback keeps its retry skip, as the primary does."""
    source, clock = chain([SLOW, SLOW, SLOW], [SLOW, OK], [OK])
    with pytest.raises(OSError):
        at(source, clock, 0)  # too early to reach the fallbacks
    assert at(source, clock, AFTER_S + 1) is OK  # fallback1 answers; fallback0 failed and is skipped
    tried = source.fallbacks[0].source.calls
    at(source, clock, AFTER_S + 2)  # within retry_s of fallback0's skip
    assert source.fallbacks[0].source.calls == tried  # not fetched again


def test_a_success_clears_the_refused_mark_and_kind_of_a_source():
    source, clock = chain([REFUSED_TOKEN, OK], [OK])
    at(source, clock, 0)  # the primary token is refused; the fallback answers
    assert source.primary.refused and source.primary.kind is not None
    assert at(source, clock, source.primary.skip_until) is OK  # the primary is retried and works
    assert not source.primary.refused and source.primary.kind is None


# ---- the lazy TLS context, and a non-deadline error reaching the caller ------------------
def test_the_https_handler_builds_the_tls_context_once_from_the_machine_stores(monkeypatch):
    """Without this, the connection would use Python's default context, which trusts the
    CurrentUser stores that a process without admin rights can add to."""
    monkeypatch.setattr(http._LazyTLSHandler, "_shared", None)
    sentinel = object()
    monkeypatch.setattr(http, "tls_context", lambda: sentinel)
    handler = http._LazyTLSHandler()
    seen = {}
    monkeypatch.setattr(handler, "do_open", lambda conn, req, context: seen.setdefault("context", context))
    handler.https_open("the-request")
    assert seen["context"] is sentinel and http._LazyTLSHandler._shared is sentinel


def test_a_connection_error_that_is_not_the_deadline_is_reraised_unchanged(monkeypatch):
    """The failover and the daemon judge the error by its type: a connection error that is not
    the request deadline must reach them as itself, not swallowed or turned into a TimeoutError."""
    refused = ConnectionRefusedError(errno.ECONNREFUSED, "refused")

    class Opener:
        def open(self, req, timeout):  # noqa: A003
            raise refused

    monkeypatch.setattr(http, "_opener_for", lambda url, policy: Opener())
    with pytest.raises(ConnectionRefusedError) as raised:
        http.local_request_json("http://127.0.0.1:9/", timeout_s=2)  # a literal IP: no DNS before the open
    assert raised.value is refused


def test_a_body_that_completes_just_as_the_deadline_fires_is_still_a_timeout(monkeypatch):
    """A slow answer can finish in the same instant the deadline passes: it must count as a
    timeout, not as good data, so the failover treats it the same as any other timeout."""
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, _size):
            return b'{"ok": true}'

    class Opener:
        def open(self, req, timeout):  # noqa: A003
            return Response()

    class FiredTimer:
        def __init__(self, delay, function):
            self.function, self.daemon = function, False

        def start(self):
            self.function()  # the deadline passes at once

        def cancel(self):
            pass

    monkeypatch.setattr(http, "_opener_for", lambda url, policy: Opener())
    monkeypatch.setattr(http.threading, "Timer", FiredTimer)
    with pytest.raises(TimeoutError, match="no complete answer"):
        http.local_request_json("http://127.0.0.1:9/", timeout_s=2)
