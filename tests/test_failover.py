"""Failover: a fallback source when the primary source has no data (sources/failover.py)."""
import copy
import tomllib
from pathlib import Path

import pytest

from alert_colors.config import App
from alert_colors.domain import AlertStatus, Coverage
from alert_colors.http import RateLimitedError
from alert_colors.sources.failover import AllSourcesFailedError, FailoverSource, Member
from alert_colors.sources.json_http import FieldError

ROOT = Path(__file__).resolve().parents[1]
RAW = tomllib.loads((ROOT / "config.toml").read_text(encoding="utf-8"))
AFTER_S, RETRY_S, RETRY_MAX_S, FALLBACK_POLL_S = 30.0, 10.0, 120.0, 10.0
PARTIAL = AlertStatus(coverage=Coverage.PARTIAL, kinds=frozenset({"air_raid"}), source="primary")
FULL = AlertStatus(coverage=Coverage.FULL, kinds=frozenset({"air_raid"}), source="fallback")
DOWN = TimeoutError("timed out")


class FakeSource:
    def __init__(self, name, results):
        self.name, self.results, self.calls = name, list(results), 0

    def fetch(self):
        self.calls += 1
        result = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(result, Exception):
            raise result
        return result


def make(primary_results, fallback_results):
    clock = {"t": 0.0}
    primary, fallback = FakeSource("primary", primary_results), FakeSource("fallback", fallback_results)
    source = FailoverSource(Member(primary), [Member(fallback, poll_interval_s=FALLBACK_POLL_S)],
                            after_s=AFTER_S, retry_s=RETRY_S, retry_max_s=RETRY_MAX_S,
                            monotonic=lambda: clock["t"])
    return source, primary, fallback, clock


def fetch_at(source, clock, t):
    clock["t"] = t
    return source.fetch()


def test_the_primary_is_used_while_it_works():
    source, primary, fallback, clock = make([PARTIAL], [FULL])
    assert fetch_at(source, clock, 0) is PARTIAL
    assert fetch_at(source, clock, 100) is PARTIAL
    assert fallback.calls == 0


def test_http_429_never_switches_to_the_fallback():
    """A 429 every few seconds is normal for the primary: no switch, no scene flicker."""
    limited = RateLimitedError(None)
    source, primary, fallback, clock = make([PARTIAL, limited, limited, limited, PARTIAL], [FULL])
    assert fetch_at(source, clock, 0) is PARTIAL
    for t in (5, 10, 20):
        with pytest.raises(RateLimitedError):
            fetch_at(source, clock, t)
    assert fetch_at(source, clock, 25) is PARTIAL
    assert fallback.calls == 0


def test_a_short_outage_does_not_switch():
    source, primary, fallback, clock = make([PARTIAL, DOWN, PARTIAL], [FULL])
    fetch_at(source, clock, 0)
    with pytest.raises(TimeoutError):
        fetch_at(source, clock, 1)
    with pytest.raises(AllSourcesFailedError):  # the primary is skipped for retry_s
        fetch_at(source, clock, 5)
    assert fetch_at(source, clock, 1 + RETRY_S) is PARTIAL
    assert fallback.calls == 0


def test_the_fallback_is_used_after_after_s_without_primary_data():
    source, primary, fallback, clock = make([PARTIAL, DOWN], [FULL])
    fetch_at(source, clock, 0)
    with pytest.raises(TimeoutError):
        fetch_at(source, clock, 1)
    assert fetch_at(source, clock, AFTER_S) is FULL


def test_the_fallback_is_used_at_start_when_the_primary_is_down():
    source, primary, fallback, clock = make([DOWN], [FULL])
    with pytest.raises(TimeoutError):
        fetch_at(source, clock, 0)
    assert fetch_at(source, clock, AFTER_S) is FULL


def test_the_fallback_is_fetched_once_each_poll_interval():
    source, primary, fallback, clock = make([DOWN], [FULL])
    with pytest.raises(TimeoutError):
        fetch_at(source, clock, 0)
    for t in range(int(AFTER_S), int(AFTER_S + FALLBACK_POLL_S)):
        assert fetch_at(source, clock, t) is FULL
    assert fallback.calls == 1
    fetch_at(source, clock, AFTER_S + FALLBACK_POLL_S)
    assert fallback.calls == 2


def test_the_primary_is_skipped_longer_after_each_failure():
    source, primary, fallback, clock = make([DOWN], [FULL])
    with pytest.raises(TimeoutError):
        fetch_at(source, clock, 0)  # failure 1: skipped for retry_s
    for t in range(int(AFTER_S), int(AFTER_S + 4 * RETRY_S)):
        fetch_at(source, clock, t)
    # tries at 0, 30 (skip 20), 50 (skip 40): three calls in 70 s, not 41
    assert primary.calls == 3


def test_the_primary_is_used_again_when_it_works():
    source, primary, fallback, clock = make([DOWN, DOWN, PARTIAL], [FULL])
    with pytest.raises(TimeoutError):
        fetch_at(source, clock, 0)
    assert fetch_at(source, clock, AFTER_S) is FULL  # primary failure 2: skip 2 * retry_s
    assert fetch_at(source, clock, AFTER_S + 2 * RETRY_S) is PARTIAL


def test_all_sources_down_raises():
    source, primary, fallback, clock = make([DOWN], [DOWN])
    with pytest.raises(TimeoutError):
        fetch_at(source, clock, 0)
    with pytest.raises(AllSourcesFailedError):
        fetch_at(source, clock, AFTER_S)


# ---- config -------------------------------------------------------------------
def with_failover(**failover):
    raw = copy.deepcopy(RAW)
    raw["failover"] = failover
    return App(raw, ROOT)


def test_an_old_config_without_failover_reads_only_the_primary():
    raw = copy.deepcopy(RAW)
    raw.pop("failover", None)
    app = App(raw, ROOT)
    assert app.fallback_entries() == []
    assert app.alert_source().name == app.source().name
    app.settings()


def test_a_fallback_gets_the_vars_of_the_primary():
    app = with_failover(sources=[{"type": "ubilling", "vars": {"region": "Київська область"}}])
    [entry] = app.fallback_entries()
    assert entry["vars"]["region"] == "Київська область"
    assert entry["vars"]["region_id"] == RAW["source"]["vars"]["region_id"]


def test_a_fallback_not_in_the_build_is_skipped():
    app = with_failover(sources=[{"type": "no_such_source"}])
    assert app.fallback_entries() == []


def test_after_s_must_be_less_than_stale_after_s():
    app = with_failover(after_s=RAW["source"]["stale_after_s"], sources=[{"type": "ubilling"}])
    with pytest.raises(ValueError, match="after_s"):
        app.settings()


def test_the_daemon_gets_the_failover_source():
    app = with_failover(sources=[{"type": "ubilling", "vars": {"region": "Київська область"}}])
    assert isinstance(app.daemon(dry_run=True).source, FailoverSource)


def test_a_wrong_ubilling_oblast_fails_and_never_clears():
    """A wrong oblast name must give an error (the unknown scene), never "no alert"."""
    app = with_failover(sources=[{"type": "ubilling", "vars": {"region": "No such oblast"}}])
    [fallback] = app.fallback_sources()
    payload = {"cachedat": "2026-01-01 00:00:00",
               "states": {"Київська область": {"alertnow": False, "changed": "2026-01-01 00:00:00"}}}
    with pytest.raises(FieldError):
        fallback.parse(payload)
