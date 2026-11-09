"""Failover: a fallback source when the primary source has no data (sources/failover.py)."""
import copy
import os
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
def with_failover(monkeypatch=None, **failover):
    raw = copy.deepcopy(RAW)
    raw["failover"] = failover
    return App(raw, ROOT)


def test_an_old_config_without_failover_reads_only_the_primary(monkeypatch):
    monkeypatch.setattr("alert_colors.user_env.get", lambda name: None)  # no token saved
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


def test_a_fallback_without_its_token_is_skipped_but_check_sees_it(monkeypatch):
    """A missing token of a fallback must not stop the app: the primary still works."""
    monkeypatch.delenv("ALERTS_IN_UA_TOKEN", raising=False)
    monkeypatch.setattr("alert_colors.user_env.get", lambda name: None)  # no token saved on this PC
    app = with_failover(sources=[{"type": "alerts_in_ua", "vars": {
        "city_uid": "1", "raion_uid": "2", "oblast_uid": "3"}}])
    assert app.alert_source().name == app.source().name  # the daemon runs on the primary
    with pytest.raises(KeyError, match="ALERTS_IN_UA_TOKEN"):
        app.fallback_sources()  # check uses this: it fails, so the install is stopped


# ---- check: the gate before an install ----------------------------------------
class CheckApp:
    """App for cli._check with fake sources."""

    def __init__(self, primary, fallbacks):
        self._app, self._primary, self._fallbacks = App(copy.deepcopy(RAW), ROOT), primary, fallbacks

    def __getattr__(self, name):
        return getattr(self._app, name)

    def check_sources(self):
        return [(s.name, s, "") for s in [self._primary, *self._fallbacks]]


def run_check(primary, fallbacks):
    from alert_colors import cli
    return cli._check(CheckApp(primary, fallbacks), None)


def http_error(code):
    import urllib.error
    return urllib.error.HTTPError("https://example.test", code, "refused", {}, None)


def test_check_passes_while_the_primary_server_is_down():
    assert run_check(FakeSource("primary", [DOWN]), [FakeSource("fallback", [FULL])]) == 0


def test_check_fails_when_no_source_works():
    assert run_check(FakeSource("primary", [DOWN]), [FakeSource("fallback", [DOWN])]) == 1


@pytest.mark.parametrize("code", [401, 403])
def test_check_fails_on_a_refused_token(code):
    assert run_check(FakeSource("primary", [PARTIAL]), [FakeSource("fallback", [http_error(code)])]) == 1


def test_check_fails_on_a_config_error_in_a_fallback():
    assert run_check(FakeSource("primary", [PARTIAL]), [FakeSource("fallback", [FieldError("x")])]) == 1


# ---- the chain: token sources first, then [source], then the fallbacks ---------
TOKEN_SOURCES = [{"type": "ukrainealarm", "poll_interval_s": 1}, {"type": "alerts_in_ua", "poll_interval_s": 10}]


def chain_app(monkeypatch, tokens):
    for name in ("UKRAINEALARM_API_KEY", "ALERTS_IN_UA_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("alert_colors.user_env.get", lambda name: tokens.get(name))
    raw = copy.deepcopy(RAW)
    raw["source"]["vars"]["oblast_name"] = "Київська область"
    raw["failover"] = {"token_sources": TOKEN_SOURCES, "sources": [{"type": "ubilling"}]}
    return App(raw, ROOT)


def chain_types(app):
    return [entry["type"] for entry, _poll in app.source_chain()]


def test_without_tokens_the_chain_starts_with_the_source(monkeypatch):
    app = chain_app(monkeypatch, {})
    assert chain_types(app) == ["siren_pp_ua", "ubilling"]
    assert [name for name, source, _why in app.check_sources() if source is None] == ["ukrainealarm",
                                                                                      "alerts_in_ua"]


def test_a_saved_token_puts_its_source_first(monkeypatch):
    app = chain_app(monkeypatch, {"ALERTS_IN_UA_TOKEN": "t"})
    assert chain_types(app) == ["alerts_in_ua", "siren_pp_ua", "ubilling"]
    source = app.alert_source()
    assert source.primary.poll_interval_s == 10  # the limit of that service also applies first
    assert source.fallbacks[0].poll_interval_s == 0  # [source] at each poll of the daemon


def test_the_order_of_token_sources_is_kept(monkeypatch):
    app = chain_app(monkeypatch, {"ALERTS_IN_UA_TOKEN": "t", "UKRAINEALARM_API_KEY": "k"})
    assert chain_types(app) == ["ukrainealarm", "alerts_in_ua", "siren_pp_ua", "ubilling"]


def test_a_token_source_that_is_the_source_is_not_doubled(monkeypatch):
    app = chain_app(monkeypatch, {"UKRAINEALARM_API_KEY": "k"})
    app.raw["source"]["type"] = "ukrainealarm"
    assert chain_types(app) == ["ukrainealarm", "ubilling"]


def test_a_fallback_without_its_place_is_not_used(monkeypatch):
    app = chain_app(monkeypatch, {})
    app.raw["source"]["vars"]["oblast_name"] = ""
    assert app.alert_source().name == "siren_pp_ua"
    assert ("ubilling", None) == app.check_sources()[-1][:2]


def test_a_refused_token_switches_at_once():
    source, primary, fallback, clock = make([http_error(401)], [FULL])
    assert fetch_at(source, clock, 0) is FULL  # no wait of after_s
    fetch_at(source, clock, RETRY_MAX_S - 1)
    assert primary.calls == 1  # skipped for retry_max_s
    fetch_at(source, clock, RETRY_MAX_S)
    assert primary.calls == 2


def test_the_first_source_is_fetched_once_each_poll_interval():
    clock = {"t": 0.0}
    primary = FakeSource("primary", [PARTIAL])
    source = FailoverSource(Member(primary, poll_interval_s=FALLBACK_POLL_S), [Member(FakeSource("f", [FULL]))],
                            after_s=AFTER_S, retry_s=RETRY_S, retry_max_s=RETRY_MAX_S,
                            monotonic=lambda: clock["t"])
    for t in range(int(FALLBACK_POLL_S)):
        assert fetch_at(source, clock, t) is PARTIAL
    assert primary.calls == 1


def test_an_old_config_uses_a_saved_token_first(monkeypatch):
    """No [failover] in the config: the default token sources still apply."""
    monkeypatch.setattr("alert_colors.user_env.get", lambda name: "k" if name == "UKRAINEALARM_API_KEY" else None)
    raw = copy.deepcopy(RAW)
    raw.pop("failover", None)
    app = App(raw, ROOT)
    assert chain_types(app) == ["ukrainealarm", "siren_pp_ua"]
    app.settings()


# ---- the token command ----------------------------------------------------------
def run_token(monkeypatch, answers, token, fetch_result):
    from alert_colors import cli, user_env

    saved = {}
    monkeypatch.setattr("builtins.input", lambda prompt="": answers.pop(0))
    monkeypatch.setattr("getpass.getpass", lambda prompt="": token)
    monkeypatch.setattr(user_env, "put", lambda name, value: saved.__setitem__(name, value))
    monkeypatch.setattr(user_env, "delete", lambda name: saved.__setitem__(name, None) or True)
    for name in ("UKRAINEALARM_API_KEY", "ALERTS_IN_UA_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(user_env, "get", lambda name: os.environ.get(name))  # no registry in tests
    monkeypatch.setattr(cli, "_fetch_with_retry", lambda source: fetch_result(source))
    return cli._token(App(copy.deepcopy(RAW), ROOT), None), saved


def raise_(error):
    raise error


def test_token_saves_a_working_token(monkeypatch):
    code, saved = run_token(monkeypatch, ["2"], "secret", lambda source: PARTIAL)
    assert code == 0 and saved == {"ALERTS_IN_UA_TOKEN": "secret"}


def test_token_does_not_save_a_refused_token(monkeypatch):
    code, saved = run_token(monkeypatch, ["1"], "bad", lambda source: raise_(http_error(401)))
    assert code == 1 and saved == {}


def test_token_saves_when_the_server_is_down(monkeypatch):
    code, saved = run_token(monkeypatch, ["1"], "k", lambda source: raise_(DOWN))
    assert code == 0 and saved == {"UKRAINEALARM_API_KEY": "k"}


def test_token_empty_input_removes_it(monkeypatch):
    code, saved = run_token(monkeypatch, ["1"], "", lambda source: PARTIAL)
    assert code == 0 and saved == {"UKRAINEALARM_API_KEY": None}


def test_token_enter_changes_nothing(monkeypatch):
    from alert_colors import cli

    code, saved = run_token(monkeypatch, [""], "x", lambda source: PARTIAL)
    assert code == cli.EXIT_UNCHANGED and saved == {}


def test_a_minimal_build_gets_no_warning_for_the_default_token_sources(monkeypatch, caplog):
    import logging

    from alert_colors.sources import SOURCES

    monkeypatch.setattr(SOURCES, "names", lambda: ["json_http", "siren_pp_ua"])
    raw = copy.deepcopy(RAW)
    raw.pop("failover", None)
    with caplog.at_level(logging.WARNING):
        assert App(raw, ROOT).token_entries() == ([], [])
    assert caplog.text == ""


def test_a_named_token_source_not_in_the_build_warns(monkeypatch, caplog):
    import logging

    from alert_colors.sources import SOURCES

    monkeypatch.setattr(SOURCES, "names", lambda: ["json_http", "siren_pp_ua"])
    app = with_failover(token_sources=[{"type": "ukrainealarm"}])
    with caplog.at_level(logging.WARNING):
        app.token_entries()
    assert "not in this build" in caplog.text


def test_status_of_an_old_config_has_the_old_output(monkeypatch, capsys):
    from alert_colors import cli

    monkeypatch.setattr("alert_colors.user_env.get", lambda name: None)
    raw = copy.deepcopy(RAW)
    raw.pop("failover", None)
    app = App(raw, ROOT)
    monkeypatch.setattr(App, "source", lambda self: FakeSource("primary", [PARTIAL]))
    assert cli._status(app, None) == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 2 and lines[1].startswith("scene:")
