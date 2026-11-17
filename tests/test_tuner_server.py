"""The configurator: state, actions and the server, with every file in tmp_path."""

import copy
import http.client
import json
import threading
import tomllib
from pathlib import Path

import pytest

from alert_colors import tuner as tuner_module
from alert_colors.config import App
from alert_colors.tuning import write_file

ROOT = Path(__file__).resolve().parents[1]
RAW = tomllib.loads((ROOT / "config.toml").read_text(encoding="utf-8"))
LOG_NAME = "tuner-test.log"
TEST_IDLE_EXIT_S = 1.0  # the idle thread wakes each 0.1 s: it ends soon after a test
JOIN_TIMEOUT_S = 5.0
HTTP_TIMEOUT_S = 5.0
SERVER_POLL_S = 0.02  # how often serve_forever() sees a shutdown: short teardowns
DEEP_NESTING_DEPTH = 100_000  # below MAX_BODY_BYTES, deeper than the JSON parser can go
GREEN = {"effect": "solid", "color": "#00FF00", "brightness": 0.5}


def make_tuner(tmp_path, **changes):
    raw = copy.deepcopy(RAW)
    raw["tuning"] = {"file": str(tmp_path / "tuning.json"), "check_interval_s": 1}
    raw["logging"] = {"level": "INFO", "file": LOG_NAME}
    raw.update(changes)
    return tuner_module.Tuner(App(raw, tmp_path))


# ---- state and actions ------------------------------------------------------
def test_a_config_without_tuning_cannot_start_the_tuner(tmp_path):
    raw = copy.deepcopy(RAW)
    raw.pop("tuning", None)
    with pytest.raises(SystemExit, match="no \\[tuning\\]"):
        tuner_module.Tuner(App(raw, tmp_path))


def test_an_unreadable_tuning_file_shows_the_config_scenes(tmp_path):
    tuner = make_tuner(tmp_path)
    tuner.path.write_text("{not json", encoding="utf-8")
    assert tuner.state()["scenes"] == tuner.base_scenes


def test_an_invalid_tuning_file_shows_the_config_scenes(tmp_path):
    tuner = make_tuner(tmp_path)
    write_file(tuner.path, {"scenes": {"no_such_scene": GREEN}})
    assert tuner.state()["scenes"] == tuner.base_scenes


def test_a_disabled_lighting_entry_is_not_in_the_state(tmp_path):
    lighting = copy.deepcopy(RAW["lighting"])
    lighting[0]["enabled"] = False
    tuner = make_tuner(tmp_path, lighting=lighting)
    assert set(tuner.state()["lighting"]) == {entry["type"] for entry in lighting[1:]}


def test_the_current_scene_is_the_last_scene_line_of_the_log(tmp_path):
    tuner = make_tuner(tmp_path)
    assert tuner.state()["current_scene"] is None  # no log file yet
    log = tmp_path / LOG_NAME
    log.write_text("started\n", encoding="utf-8")
    assert tuner.state()["current_scene"] is None  # no scene line
    log.write_text("x scene None -> unknown (status: None)\nx scene unknown -> clear (status: ok)\n",
                   encoding="utf-8")
    assert tuner.state()["current_scene"] == "clear"


def test_preview_without_a_definition_and_with_a_clamped_time(tmp_path, monkeypatch):
    now = 1_000.0
    monkeypatch.setattr(tuner_module.time, "time", lambda: now)
    tuner = make_tuner(tmp_path)
    too_long = tuner_module.PREVIEW_MAX_S * 2
    result = tuner.preview({"scene": "alert", "seconds": too_long})
    document = json.loads(tuner.path.read_text(encoding="utf-8"))
    assert document["preview"] == {"scene": "alert", "until": now + tuner_module.PREVIEW_MAX_S}
    assert result["ok"] and f"{tuner_module.PREVIEW_MAX_S:.0f} s" in result["message"]
    tuner.preview({"scene": "alert", "seconds": 0})
    document = json.loads(tuner.path.read_text(encoding="utf-8"))
    assert document["preview"]["until"] == now + tuner_module.PREVIEW_MIN_S


def test_reset_removes_the_tuning_file_and_works_without_one(tmp_path):
    tuner = make_tuner(tmp_path)
    write_file(tuner.path, {"display": {"brightness": 0.5}})
    assert tuner.reset()["ok"] and not tuner.path.exists()
    assert tuner.reset()["ok"]


# ---- the server ---------------------------------------------------------------
class Served:
    def __init__(self, tuner, thread, stop, opened):
        self.tuner, self.thread, self.stop, self.opened = tuner, thread, stop, opened
        self.server = None

    @property
    def port(self):
        return self.server.server_port

    def request(self, method, path, body=b"", host=None, token=True, length=None):
        connection = http.client.HTTPConnection(tuner_module.HOST, self.port, timeout=HTTP_TIMEOUT_S)
        connection.putrequest(method, path, skip_host=True)
        connection.putheader("Host", host or f"{tuner_module.HOST}:{self.port}")
        if token:
            connection.putheader("X-Token", self.tuner.token)
        connection.putheader("Content-Length", str(len(body)) if length is None else length)
        connection.endheaders()
        if body:
            connection.send(body)
        response = connection.getresponse()
        data = response.read()
        connection.close()
        return response.status, data


@pytest.fixture
def served(tmp_path, monkeypatch):
    """A tuner server on a free port of 127.0.0.1; every thread of it ends in the teardown."""
    monkeypatch.setattr(tuner_module, "IDLE_EXIT_S", TEST_IDLE_EXIT_S)
    opened, browser_opened = [], threading.Event()

    def fake_open(url):
        opened.append(url)
        browser_opened.set()

    monkeypatch.setattr(tuner_module.webbrowser, "open", fake_open)
    tuner = make_tuner(tmp_path)
    started = threading.Event()
    box = {}

    class Server(tuner_module.ThreadingHTTPServer):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            box["server"] = self
            started.set()

        def serve_forever(self, poll_interval=SERVER_POLL_S):
            super().serve_forever(poll_interval)

    monkeypatch.setattr(tuner_module, "ThreadingHTTPServer", Server)
    before = set(threading.enumerate())
    stop = threading.Event()
    thread = threading.Thread(target=tuner.serve, kwargs={"open_browser": True, "stop": stop}, daemon=True)
    thread.start()
    assert started.wait(JOIN_TIMEOUT_S) and browser_opened.wait(JOIN_TIMEOUT_S)
    result = Served(tuner, thread, stop, opened)
    result.server = box["server"]
    yield result
    stop.set()
    thread.join(JOIN_TIMEOUT_S)
    tuner.last_request = float("-inf")  # the idle thread ends at its next check
    for other in set(threading.enumerate()) - before:
        other.join(JOIN_TIMEOUT_S)
        assert not other.is_alive()
    result.server.server_close()


def test_the_browser_opens_the_page_with_the_token(served):
    assert served.opened == [f"http://{tuner_module.HOST}:{served.port}/#{served.tuner.token}"]


def test_the_page_is_served_only_for_its_own_host(served):
    status, page = served.request("GET", "/?x=1", token=False)
    assert status == 200 and b"<html" in page.lower()
    assert served.request("GET", "/", host="evil.example", token=False)[0] == 403


def test_an_unknown_get_path_is_forbidden(served):
    assert served.request("GET", "/api/other")[0] == 403


def test_a_post_without_the_token_changes_nothing(served):
    write_file(served.tuner.path, {"display": {"brightness": 0.5}})
    assert served.request("POST", "/api/reset", token=False)[0] == 403
    assert served.tuner.path.exists()


def test_a_too_large_body_is_refused_before_it_is_read(served):
    status, body = served.request("POST", "/api/apply", length=str(tuner_module.MAX_BODY_BYTES + 1))
    assert status == 413 and json.loads(body)["ok"] is False


@pytest.mark.parametrize("length", ["abc", "-1"])
def test_a_bad_content_length_is_a_bad_request(served, length):
    assert served.request("POST", "/api/apply", length=length)[0] == 400


@pytest.mark.parametrize("body", [b"[]", b"{not json", b'{"scenes": []}', b"[" * DEEP_NESTING_DEPTH],
                         ids=["list", "not-json", "scenes-list", "deep-nesting"])
def test_a_malformed_body_is_a_bad_request_not_a_dropped_connection(served, body):
    status, answer = served.request("POST", "/api/apply", body=body)
    assert status == 400 and json.loads(answer)["ok"] is False
    assert not served.tuner.path.exists()


def test_reset_over_the_api_removes_the_file(served):
    write_file(served.tuner.path, {"display": {"brightness": 0.5}})
    status, body = served.request("POST", "/api/reset")
    assert status == 200 and json.loads(body)["ok"] and not served.tuner.path.exists()


def test_an_unknown_action_is_not_found(served):
    assert served.request("POST", "/api/other", body=b"{}")[0] == 404


def test_quit_closes_the_server(served):
    status, body = served.request("POST", "/api/quit")
    assert status == 200 and json.loads(body)["ok"]
    served.thread.join(JOIN_TIMEOUT_S)
    assert not served.thread.is_alive()


def test_the_stop_event_closes_the_server(served):
    served.stop.set()
    served.thread.join(JOIN_TIMEOUT_S)
    assert not served.thread.is_alive()


def test_the_server_exits_after_the_idle_time(tmp_path, monkeypatch):
    short_idle_s = 0.05
    monkeypatch.setattr(tuner_module, "IDLE_EXIT_S", short_idle_s)
    tuner = make_tuner(tmp_path)
    box = {}

    class Server(tuner_module.ThreadingHTTPServer):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            box["server"] = self

        def serve_forever(self, poll_interval=SERVER_POLL_S):
            super().serve_forever(poll_interval)

    monkeypatch.setattr(tuner_module, "ThreadingHTTPServer", Server)
    before = set(threading.enumerate())
    thread = threading.Thread(target=tuner.serve, kwargs={"open_browser": False}, daemon=True)
    thread.start()
    thread.join(JOIN_TIMEOUT_S)
    assert not thread.is_alive()
    for other in set(threading.enumerate()) - before:
        other.join(JOIN_TIMEOUT_S)
    box["server"].server_close()


@pytest.mark.parametrize("content", [
    "[1, 2]", '"text"', '{"display": 5, "lighting": [1]}', '{"lighting": {"aura_sdk": 3}}',
], ids=["list", "text", "wrong-sections", "wrong-backend-entry"])
def test_a_tuning_file_with_wrong_parts_still_gives_the_page_its_state(tmp_path, content):
    """The configurator is the tool that repairs the file: it must still load."""
    tuner = make_tuner(tmp_path)
    tuner.path.write_text(content, encoding="utf-8")
    state = tuner.state()
    assert state["scenes"] and "brightness" in state


def test_a_preview_time_too_large_for_a_number_is_a_bad_request(served):
    body = json.dumps({"scene": "clear", "seconds": 10 ** 400}).encode("utf-8")
    status, answer = served.request("POST", "/api/preview", body=body)
    assert status == 400 and json.loads(answer)["ok"] is False


def test_a_preview_replaces_a_tuning_file_that_is_not_an_object(tmp_path):
    tuner = make_tuner(tmp_path)
    tuner.path.write_text("[1, 2]", encoding="utf-8")
    assert tuner.preview({"scene": "clear", "seconds": 5})["ok"] is True
    assert isinstance(json.loads(tuner.path.read_text(encoding="utf-8")), dict)
