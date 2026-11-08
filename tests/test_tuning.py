import http.client
import json
import threading
import time
import tomllib
from pathlib import Path

import pytest

from alert_colors.config import App
from alert_colors.domain import Color
from alert_colors.tuning import TuningError, TuningWatcher, parse, write_file

ROOT = Path(__file__).resolve().parents[1]
RAW = tomllib.loads((ROOT / "config.toml").read_text(encoding="utf-8"))
BASE = RAW["scenes"]
BACKENDS = {"aura_sdk", "razer_chroma"}
GREEN_TUNED = {"effect": "solid", "color": "#00FF00", "brightness": 0.5}


# ---- the file format --------------------------------------------------------
def test_empty_tuning_gives_the_config_scenes():
    t = parse({}, BASE, BACKENDS)
    assert set(t.scenes) == set(BASE) and t.brightness is None and t.preview_scene is None


def test_a_tuned_scene_replaces_only_that_scene():
    t = parse({"scenes": {"clear": GREEN_TUNED}}, BASE, BACKENDS)
    assert t.scenes["clear"].effect.color == Color(0, 255, 0).scaled(0.5)
    assert type(t.scenes["alert"].effect).__name__ == "Pulse"


@pytest.mark.parametrize("document", [
    {"source": {"type": "json_http"}},                                  # never the source
    {"logging": {"file": "C:/Windows/x.log"}},                          # never a path
    {"rules": []},                                                      # never the rules
    {"scenes": {"new_scene": GREEN_TUNED}},                             # only existing scenes
    {"scenes": {"clear": {"effect": "solid", "colour": "#00FF00"}}},    # options from the schema only
    {"display": {"brightness": 2}},                                     # ranges
    {"lighting": {"other_backend": {"brightness": 0.5}}},
    {"lighting": {"aura_sdk": {"color_correction": [1, 1]}}},
    {"preview": {"scene": "missing", "until": 0}},
])
def test_everything_outside_the_allowed_content_is_refused(document):
    with pytest.raises(TuningError):
        parse(document, BASE, BACKENDS)


# ---- the watcher ------------------------------------------------------------
def test_watcher_reloads_on_change_and_keeps_last_valid(tmp_path):
    path = tmp_path / "tuning.json"
    clock = {"t": 0.0}
    watcher = TuningWatcher(path, BASE, BACKENDS, check_interval_s=1, monotonic=lambda: clock["t"])
    assert watcher.poll() is not None                       # no file: the config values
    write_file(path, {"display": {"brightness": 0.3}})
    clock["t"] = 0.5
    assert watcher.poll() is None                           # not yet time to check
    clock["t"] = 1.5
    assert watcher.poll().brightness == 0.3
    path.write_text('{"display": {"brightness": 9}}', encoding="utf-8")
    clock["t"] = 3.0
    assert watcher.poll() is None                           # invalid: ignored, the old one stays


# ---- the daemon applies it --------------------------------------------------
def test_daemon_applies_tuning_and_preview(monkeypatch, tmp_path):
    from datetime import UTC, datetime

    from tests_helpers import FakeOutput

    app = App(RAW, ROOT)
    output = FakeOutput("aura_sdk")
    daemon = app.daemon(dry_run=True)
    daemon.outputs = [output]
    daemon.tuning = None  # only the tuning of this test
    tuning = parse({"scenes": {"clear": GREEN_TUNED}, "lighting": {"aura_sdk": {"brightness": 0.4}},
                    "preview": {"scene": "alert", "until": time.time() + 60,
                                "definition": {"effect": "solid", "color": "#0000FF"}}}, BASE, {"aura_sdk"})
    daemon.apply_tuning(tuning)
    assert output.tuning == (0.4, None)
    daemon.wall_clock = lambda: datetime.now(UTC)
    daemon.step()
    assert output.frames[-1].color() == Color(0, 0, 255)     # the preview definition, not saved
    assert daemon.scenes["alert"].effect.__class__.__name__ == "Pulse"


# ---- the configurator server ------------------------------------------------
@pytest.fixture
def tuner(tmp_path):
    from alert_colors import tuner as tuner_module

    raw = dict(RAW)
    raw["tuning"] = {"file": str(tmp_path / "tuning.json"), "check_interval_s": 1}
    raw["logging"] = {"level": "INFO"}
    instance = tuner_module.Tuner(App(raw, ROOT))
    started = threading.Event()
    original = tuner_module.ThreadingHTTPServer

    class Server(original):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            instance.server = self
            started.set()

    tuner_module.ThreadingHTTPServer = Server
    thread = threading.Thread(target=instance.serve, kwargs={"open_browser": False}, daemon=True)
    thread.start()
    assert started.wait(5)
    tuner_module.ThreadingHTTPServer = original
    yield instance
    instance.server.shutdown()


def call(instance, method, path, body=None, token=True, host=None):
    port = instance.server.server_port
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {"Content-Type": "application/json", "Host": host or f"127.0.0.1:{port}"}
    if token:
        headers["X-Token"] = instance.token
    connection.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
    response = connection.getresponse()
    return response.status, response.read()


def test_tuner_needs_token_and_own_host(tuner):
    assert call(tuner, "GET", "/api/state", token=False)[0] == 403
    assert call(tuner, "GET", "/api/state", host="evil.example:80")[0] == 403
    status, body = call(tuner, "GET", "/api/state")
    assert status == 200 and "pulse" in {e["name"] for e in json.loads(body)["schema"]}


def test_tuner_apply_writes_only_differences_and_validates(tuner):
    state = json.loads(call(tuner, "GET", "/api/state")[1])
    scenes = state["scenes"]
    scenes["clear"] = GREEN_TUNED
    status, _ = call(tuner, "POST", "/api/apply", {"scenes": scenes, "brightness": 0.9, "lighting": {}})
    assert status == 200
    document = json.loads(tuner.path.read_text(encoding="utf-8"))
    assert list(document["scenes"]) == ["clear"] and document["display"]["brightness"] == 0.9
    scenes["clear"] = {"effect": "solid", "color": "not a color"}
    assert call(tuner, "POST", "/api/apply", {"scenes": scenes})[0] == 400


def test_tuner_preview_does_not_save_the_scene(tuner):
    status, _ = call(tuner, "POST", "/api/preview",
                     {"scene": "alert", "seconds": 5, "scene_definition": GREEN_TUNED})
    assert status == 200
    document = json.loads(tuner.path.read_text(encoding="utf-8"))
    assert "alert" not in document.get("scenes", {}) and document["preview"]["definition"] == GREEN_TUNED
