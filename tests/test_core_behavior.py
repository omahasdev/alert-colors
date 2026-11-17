"""Core modules: config, tuning, template, http, json_http, daemon, effects and others.

No test makes a network request, starts a real backend or writes outside tmp_path.
"""

import copy
import email.message
import logging
import sys
import threading
import tomllib
import urllib.error
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from alert_colors import http
from alert_colors.config import App, _correction
from alert_colors.daemon import Daemon, DaemonSettings
from alert_colors.domain import AlertStatus, Color, Coverage
from alert_colors.effects import COLORS, EFFECTS, Blink, Param, Pulse, Spatial, Uniform
from alert_colors.outputs import NO_CORRECTION
from alert_colors.policy import Policy, Rule
from alert_colors.regions import RegionError, places
from alert_colors.registry import Registry
from alert_colors.scenes import Scene
from alert_colors.sources import json_http
from alert_colors.sources.json_http import FieldError, TimeParser, create_json_http
from alert_colors.template import expand
from alert_colors.tuning import MAX_FILE_BYTES, Tuning, TuningError, TuningWatcher, parse, read_file, write_file

ROOT = Path(__file__).resolve().parents[1]
RAW = tomllib.loads((ROOT / "config.toml").read_text(encoding="utf-8"))
BASE = RAW["scenes"]
BACKEND_TYPES = {"aura_sdk", "razer_chroma"}
URL = "https://alerts.example/api"
FAKE_DNS_ANSWER = "127.0.0.1"  # no real address; the fixture turns off the private-address rule
TIMEOUT_S = 1.0
JOIN_TIMEOUT_S = 5.0
GREEN, RED = Color(0, 255, 0), Color(255, 0, 0)
DEEP_NESTING = "[" * 100_000  # below MAX_FILE_BYTES, deeper than the JSON parser can go
FULL, HALF, LOW = 1.0, 0.5, 0.25


def make_app(tmp_path, **changes):
    raw = copy.deepcopy(RAW)
    raw["tuning"] = {"file": str(tmp_path / "tuning.json"), "check_interval_s": 1}
    raw.update(changes)
    return App(raw, tmp_path)


# ---- config.py ----------------------------------------------------------------
def test_a_disabled_fallback_source_is_not_used(tmp_path):
    app = make_app(tmp_path, failover={"sources": [{"type": "ubilling", "enabled": False}]})
    assert app.fallback_entries() == []


def test_a_disabled_lighting_entry_is_skipped(tmp_path):
    app = make_app(tmp_path, lighting=[{"type": "console", "enabled": False}, {"type": "console", "name": "on"}])
    assert app.lighting_entries() == [{"type": "console", "name": "on"}]


def test_no_tuning_section_gives_no_tuning_watcher(tmp_path):
    app = make_app(tmp_path)
    del app.raw["tuning"]
    assert app.tuning_path() is None and app.tuning_watcher([]) is None


def test_the_stop_event_name_comes_from_the_service_section(tmp_path):
    app = make_app(tmp_path, service={"stop_event": "Local\\AlertColorsTestStop"})
    assert app.stop_event_name() == "Local\\AlertColorsTestStop"


def test_a_log_file_inside_the_config_folder_is_allowed(tmp_path):
    assert make_app(tmp_path).log_path("logs/app.log") == (tmp_path / "logs" / "app.log").resolve()


@pytest.fixture
def root_logger():
    """configure_logging() replaces the root handlers: give them back after the test."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield root
    for handler in root.handlers:
        if handler not in handlers:
            handler.close()
    root.handlers[:] = handlers
    root.setLevel(level)


def test_configure_logging_writes_the_log_file_in_the_config_folder(tmp_path, root_logger):
    app = make_app(tmp_path, logging={"level": "WARNING", "file": "test.log", "max_bytes": 1000, "backups": 1})
    app.configure_logging()
    assert root_logger.level == logging.WARNING
    files = [h for h in root_logger.handlers if isinstance(h, logging.handlers.RotatingFileHandler)]
    assert [Path(h.baseFilename) for h in files] == [(tmp_path / "test.log").resolve()]
    assert any(type(h) is logging.StreamHandler for h in root_logger.handlers)
    logging.getLogger("alert_colors.test").warning("a test line")
    files[0].flush()
    assert "a test line" in (tmp_path / "test.log").read_text(encoding="utf-8")


def test_configure_logging_without_a_file_logs_only_to_the_console(tmp_path, root_logger):
    app = make_app(tmp_path, logging={"level": "INFO", "file": "test.log", "max_bytes": 1000, "backups": 1})
    app.configure_logging(to_file=False)
    assert not any(isinstance(h, logging.FileHandler) for h in root_logger.handlers)
    assert not (tmp_path / "test.log").exists()


def test_configure_logging_under_pythonw_has_no_console_handler(tmp_path, root_logger, monkeypatch):
    monkeypatch.setattr(sys, "stderr", None)  # pythonw.exe has no console
    app = make_app(tmp_path, logging={"level": "INFO", "file": "test.log", "max_bytes": 1000, "backups": 1})
    app.configure_logging()
    assert [type(h) for h in root_logger.handlers] == [logging.handlers.RotatingFileHandler]


def test_color_correction_of_a_lighting_entry():
    assert _correction({"type": "console"}) == NO_CORRECTION
    assert _correction({"type": "console", "color_correction": [1, HALF, LOW]}) == (FULL, HALF, LOW)
    with pytest.raises(ValueError, match="3 factors"):
        _correction({"type": "console", "color_correction": [1, 1]})


# ---- tuning.py ----------------------------------------------------------------
@pytest.mark.parametrize("document, message", [
    ([], "tuning: must be an object"),
    ({"scenes": {"clear": "red"}}, "scenes.clear: must be an object"),
    ({"preview": {"definition": {"effect": "solid"}}}, "needs preview.scene"),
    ({"preview": {"scene": "alert", "definition": {"effect": "no_such_effect"}}}, "preview.definition"),
])
def test_malformed_tuning_parts_are_refused(document, message):
    with pytest.raises(TuningError, match=message):
        parse(document, BASE, BACKEND_TYPES)


def test_a_valid_color_correction_is_applied():
    tuning = parse({"lighting": {"aura_sdk": {"color_correction": [1, HALF, 0]}}}, BASE, BACKEND_TYPES)
    assert tuning.lighting["aura_sdk"].correction == (FULL, HALF, 0.0)
    assert tuning.lighting["aura_sdk"].brightness is None


def test_display_and_backend_brightness_stay_separate():
    tuning = parse({"display": {"brightness": HALF}, "lighting": {"aura_sdk": {"brightness": LOW}}},
                   BASE, BACKEND_TYPES)
    assert tuning.brightness == HALF and tuning.lighting["aura_sdk"].brightness == LOW


@pytest.mark.parametrize("document", [
    {"display": {"brightness": None}},
    {"display": {"brightness": [1]}},
    {"lighting": {"aura_sdk": {"brightness": None}}},
    {"lighting": {"aura_sdk": {"color_correction": [1, None, 1]}}},
    {"preview": {"scene": "alert", "until": None}},
    {"preview": {"scene": "alert", "until": "soon"}},
    {"preview": {"scene": []}},
])
def test_a_wrong_value_type_is_a_tuning_error(document):
    with pytest.raises(TuningError):
        parse(document, BASE, BACKEND_TYPES)


@pytest.mark.parametrize("text", ['{"display": {"brightness": null}}', '{"preview": {"scene": ["alert"]}}',
                                  DEEP_NESTING], ids=["null-number", "list-scene", "deep-nesting"])
def test_the_watcher_keeps_the_last_tuning_for_a_file_with_wrong_types(tmp_path, text):
    path = tmp_path / "tuning.json"
    clock = {"t": 0.0}
    watcher = TuningWatcher(path, BASE, BACKEND_TYPES, check_interval_s=1, monotonic=lambda: clock["t"])
    write_file(path, {"display": {"brightness": HALF}})
    assert watcher.poll().brightness == HALF
    path.write_text(text, encoding="utf-8")
    clock["t"] = 2.0
    assert watcher.poll() is None  # a warning, never an error that stops the daemon


def test_the_watcher_ignores_an_unchanged_file(tmp_path):
    path = tmp_path / "tuning.json"
    clock = {"t": 0.0}
    watcher = TuningWatcher(path, BASE, BACKEND_TYPES, check_interval_s=1, monotonic=lambda: clock["t"])
    write_file(path, {"display": {"brightness": HALF}})
    assert watcher.poll() is not None
    clock["t"] = 2.0
    assert watcher.poll() is None


def test_a_too_large_tuning_file_is_not_read(tmp_path):
    path = tmp_path / "tuning.json"
    path.write_bytes(b" " * (MAX_FILE_BYTES + 1))
    with pytest.raises(TuningError, match="larger"):
        read_file(path)


# ---- template.py and registry.py ---------------------------------------------
def test_expand_works_in_lists_and_keeps_other_values():
    assert expand(["${region}", 7, {"id": "${region}"}], {"region": "0"}) == ["0", 7, {"id": "0"}]


def test_an_unknown_placeholder_names_the_known_variables():
    with pytest.raises(KeyError, match="has: alpha, region"):
        expand("${missing}", {"region": "0", "alpha": "a"})
    with pytest.raises(KeyError, match="has: none"):
        expand("${missing}", {})


def test_a_name_cannot_register_two_times():
    registry = Registry("thing")
    registry.register("one")(lambda options: options)
    with pytest.raises(ValueError, match="already registered"):
        registry.register("one")(lambda options: options)


# ---- http.py ------------------------------------------------------------------
class FakeResponse:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size):
        return self.body[:size]


@pytest.fixture
def network(monkeypatch):
    """A fake DNS and a fake opener: the requests are recorded, never sent."""
    sent = []
    answer = {"body": b'{"ok": true}', "error": None}

    def fake_open(req, timeout):
        sent.append(req)
        if answer["error"] is not None:
            raise answer["error"]
        return FakeResponse(answer["body"])

    monkeypatch.setattr(http.socket, "getaddrinfo",
                        lambda host, port, type=0: [(None, None, None, "", (FAKE_DNS_ANSWER, port))])
    monkeypatch.setattr(http, "_check_address", lambda address, policy: None)  # see test_security.py
    monkeypatch.setattr(http._OPENER, "open", fake_open)
    return sent, answer


def test_params_are_added_to_the_url(network):
    sent, _answer = network
    http.request_json(URL, params={"a": 1, "b": ["x", "y"]}, timeout_s=TIMEOUT_S)
    http.request_json(URL + "?k=v", params={"a": 1}, timeout_s=TIMEOUT_S)
    assert [req.full_url for req in sent] == [URL + "?a=1&b=x&b=y", URL + "?k=v&a=1"]


def test_a_json_body_is_sent_with_its_content_type(network):
    sent, _answer = network
    http.request_json(URL, method="POST", body={"x": 1}, headers={"X-Test": "1"}, timeout_s=TIMEOUT_S)
    (req,) = sent
    assert req.data == b'{"x": 1}' and req.get_method() == "POST"
    assert req.get_header("Content-type") == "application/json" and req.get_header("X-test") == "1"
    assert req.get_header("User-agent") == http.USER_AGENT


def test_an_empty_response_gives_none(network):
    _sent, answer = network
    answer["body"] = b""
    assert http.request_json(URL, timeout_s=TIMEOUT_S) is None


def http_error(code, retry_after=None, with_headers=True):
    headers = email.message.Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return urllib.error.HTTPError(URL, code, "error", headers if with_headers else None, None)


@pytest.mark.parametrize("error, retry_after_s", [
    (http_error(http.HTTP_TOO_MANY_REQUESTS, "7"), 7.0),
    (http_error(http.HTTP_TOO_MANY_REQUESTS, "Wed, 21 Oct 2026 07:28:00 GMT"), None),  # a date: normal backoff
    (http_error(http.HTTP_TOO_MANY_REQUESTS), None),
    (http_error(http.HTTP_TOO_MANY_REQUESTS, with_headers=False), None),
])
def test_http_429_becomes_a_rate_limit_with_the_retry_hint(network, error, retry_after_s):
    _sent, answer = network
    answer["error"] = error
    with pytest.raises(http.RateLimitedError) as raised:
        http.request_json(URL, timeout_s=TIMEOUT_S)
    assert raised.value.retry_after_s == retry_after_s


def test_other_http_errors_pass_unchanged(network):
    _sent, answer = network
    server_error = 500
    answer["error"] = http_error(server_error)
    with pytest.raises(urllib.error.HTTPError) as raised:
        http.request_json(URL, timeout_s=TIMEOUT_S)
    assert raised.value.code == server_error and not isinstance(raised.value, http.RateLimitedError)


def test_a_url_without_a_host_is_refused():
    with pytest.raises(http.UnsafeRequestError, match="no host"):
        http.check_url("https:///path", http.REMOTE)


def test_local_requests_use_the_loopback_policy(monkeypatch):
    seen = []
    monkeypatch.setattr(http, "check_url", lambda url, policy: seen.append(policy))
    monkeypatch.setattr(http._OPENER, "open", lambda req, timeout: pytest.fail("loopback went to the proxy opener"))
    monkeypatch.setattr(http._DIRECT_OPENER, "open", lambda req, timeout: FakeResponse(b"[1]"))
    assert http.local_request_json("http://127.0.0.1:1/x", timeout_s=TIMEOUT_S) == [1]
    assert seen == [http.LOOPBACK]


# ---- sources/json_http.py -----------------------------------------------------
def test_get_passes_every_request_option(monkeypatch):
    calls = []
    monkeypatch.setattr(json_http, "request_json", lambda url, **kwargs: calls.append((url, kwargs)) or {})
    policy = http.NetworkPolicy(allow_http=True)
    json_http._get({"url": URL, "method": "POST", "headers": {"A": "1"}, "params": {"p": 1},
                    "body": {"b": 2}, "timeout_s": "3"}, policy)
    assert calls == [(URL, {"method": "POST", "headers": {"A": "1"}, "params": {"p": 1}, "body": {"b": 2},
                            "timeout_s": 3.0, "policy": policy})]


def test_epoch_times_are_utc():
    assert TimeParser("epoch", ZoneInfo("UTC")).parse(0) == datetime(1970, 1, 1, tzinfo=UTC)


def test_a_time_in_no_known_form_is_an_error():
    with pytest.raises(ValueError):
        TimeParser("iso", ZoneInfo("UTC")).parse("yesterday")


def test_json_http_needs_a_full_expression():
    with pytest.raises(ValueError, match="needs a `full`"):
        create_json_http({"request": {"url": URL, "timeout_s": 1}, "fields": {"partial": "p"}})


LOOKUP_OPTIONS = {
    "request": {"url": URL, "timeout_s": 1},
    "fields": {"full": "alerts.${first}", "partial": "alerts.${second}"},
    "lookups": {
        "first": {"url": URL + "/first", "expression": "name"},
        "second": {"url": URL + "/second", "expression": "name"},
    },
}
DOCUMENTS = {
    URL + "/first": {"name": "here"},
    URL + "/second": {"name": "near"},
    URL: {"alerts": {"here": False, "near": True}},
}


def test_lookups_are_fetched_once_then_the_fields_use_them(monkeypatch):
    fetched = []

    def fake_get(request, network):
        fetched.append(request["url"])
        return DOCUMENTS[request["url"]]

    monkeypatch.setattr(json_http, "_get", fake_get)
    source = create_json_http(copy.deepcopy(LOOKUP_OPTIONS))
    assert source.fields == {}
    source.provide_lookup("first", "here")
    assert source.fields == {}  # "second" is still missing
    assert source.fetch().coverage is Coverage.PARTIAL
    assert fetched == [URL + "/second", URL]  # "first" was given, not fetched
    assert source.fetch().coverage is Coverage.PARTIAL
    assert fetched == [URL + "/second", URL, URL]
    assert source.fetch_lookup_document("first") == DOCUMENTS[URL + "/first"]


def test_a_lookup_without_a_value_is_a_field_error(monkeypatch):
    monkeypatch.setattr(json_http, "_get", lambda request, network: {})
    source = create_json_http(copy.deepcopy(LOOKUP_OPTIONS))
    with pytest.raises(FieldError, match="lookup 'first'"):
        source.fetch()


def test_a_change_marker_without_a_value_is_a_field_error(monkeypatch):
    monkeypatch.setattr(json_http, "_get", lambda request, network: {"other": 1})
    source = create_json_http({"request": {"url": URL, "timeout_s": 1}, "fields": {"full": "alert"},
                               "change_marker": {"url": URL + "/marker", "field": "index", "max_age_s": 60}})
    with pytest.raises(FieldError, match="change marker"):
        source.fetch()


# ---- daemon.py ----------------------------------------------------------------
class SimpleSource:
    name = "simple"

    def __init__(self):
        self.fetches = 0

    def fetch(self):
        self.fetches += 1
        return AlertStatus(Coverage.NONE)


class PlainOutput:
    """An output without set_tuning() and with a close() counter."""

    def __init__(self, name="plain", stop_after=None, stop=None):
        self.name, self.frames, self.closes = name, [], 0
        self.stop_after, self.stop = stop_after, stop
        self.ready = lambda: True

    def show(self, frame):
        self.frames.append(frame.color())
        if self.stop_after is not None and len(self.frames) >= self.stop_after and self.ready():
            self.stop.set()

    def release(self):
        pass

    def reclaim(self):
        pass

    def close(self):
        self.closes += 1


def make_daemon(*outputs, brightness=FULL, frame_interval_s=0.1):
    return Daemon(
        source=SimpleSource(),
        policy=Policy.from_config([{"state": "unknown", "scene": "clear"}, {"scene": "clear"}], {"clear"}),
        scenes={"clear": Scene("clear", EFFECTS.create("solid", {"color": "#00FF00"}))},
        outputs=list(outputs),
        settings=DaemonSettings(poll_interval_s=1, stale_after_s=100, frame_interval_s=frame_interval_s,
                                brightness=brightness),
    )


def red_tuning(brightness):
    return Tuning(scenes={"clear": Scene("clear", EFFECTS.create("solid", {"color": "#FF0000"}))},
                  scene_options={}, brightness=brightness)


def test_tuning_brightness_returns_to_the_config_value_when_removed():
    output = PlainOutput()  # no set_tuning(): the tuning skips it
    daemon = make_daemon(output, brightness=HALF)
    daemon.apply_tuning(red_tuning(LOW))
    assert daemon.settings.brightness == LOW
    daemon.apply_tuning(red_tuning(None))
    assert daemon.settings.brightness == HALF


def test_step_applies_a_new_tuning_from_the_watcher():
    class OneTuning:
        check_interval_s = 1.0

        def __init__(self):
            self.tunings = [red_tuning(FULL)]

        def poll(self):
            return self.tunings.pop() if self.tunings else None

    output = PlainOutput()
    daemon = make_daemon(output)
    daemon.tuning = OneTuning()
    daemon.poll_tuning()  # the tuning thread of run() does this
    daemon.step()
    daemon.step()
    assert output.frames == [RED, RED]


def test_run_shows_frames_then_closes_the_outputs_and_ends_the_poller():
    short_frame_s = 0.01
    frames_to_show = 3
    stop = threading.Event()
    output = PlainOutput(stop_after=frames_to_show, stop=stop)
    daemon = make_daemon(output, frame_interval_s=short_frame_s)
    output.ready = lambda: daemon.source.fetches >= 1  # stop only after the poller fetched
    runner = threading.Thread(target=daemon.run, args=(stop,))
    runner.start()
    runner.join(JOIN_TIMEOUT_S)
    assert not runner.is_alive()
    for thread in threading.enumerate():
        if thread.name == "poller":
            thread.join(JOIN_TIMEOUT_S)
            assert not thread.is_alive()
    assert len(output.frames) >= frames_to_show and output.closes == 1
    assert daemon.source.fetches >= 1


# ---- domain, policy, regions ----------------------------------------------------
def test_a_color_channel_out_of_range_is_refused():
    with pytest.raises(ValueError, match="out of range"):
        Color(256, 0, 0)


def test_a_color_gives_its_hex_text():
    assert Color(1, 2, 255).to_hex() == "#0102FF"


def test_a_rule_with_an_unknown_state_is_refused():
    with pytest.raises(ValueError, match="state must be one of"):
        Rule.from_config({"scene": "clear", "state": "maybe"})


def test_an_empty_region_tree_is_an_error():
    with pytest.raises(RegionError, match="empty"):
        places({"states": []})


# ---- effects.py -----------------------------------------------------------------
def test_a_color_list_shorter_than_its_minimum_is_refused():
    with pytest.raises(ValueError, match="at least 2"):
        Param("colors", COLORS, [], min=2).parse(["#FF0000"])
    with pytest.raises(ValueError, match="at least 1"):
        Param("colors", COLORS, []).parse([])


def test_a_value_outside_the_choices_is_refused():
    with pytest.raises(ValueError, match="is not one of"):
        EFFECTS.create("comet", {"direction": "sideways"})


def test_a_color_cycle_of_one_color_shows_that_color():
    cycle = EFFECTS.create("color_cycle", {"colors": ["#00FF00"], "brightness": HALF})
    assert cycle.color_at(0) == cycle.color_at(1.5) == GREEN.scaled(HALF)


def test_the_base_classes_need_their_color_method():
    with pytest.raises(NotImplementedError):
        Uniform().color_at(0)
    with pytest.raises(NotImplementedError):
        Spatial().colors(0, 1)


def test_a_spatial_effect_on_one_led_shows_the_first_position():
    rainbow = EFFECTS.create("rainbow", {"moving": False})
    assert rainbow.color_at(0) == rainbow.colors(0, 1)[0] == Color(0, 255, 255)  # the hue at the middle


@pytest.mark.parametrize("build", [
    lambda: Pulse(RED, 0, 0.0),
    lambda: Blink(RED, 0, HALF),
])
def test_a_zero_period_is_refused(build):
    with pytest.raises(ValueError, match="period_s > 0"):
        build()
