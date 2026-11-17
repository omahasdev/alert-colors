"""The command line (cli.py, __main__.py) and modules.py with fakes: no LEDs, no network, no registry."""
import argparse
import copy
import os
import runpy
import sys
import threading
import tomllib
import types
import uuid
from pathlib import Path

import pytest

from alert_colors import cli, modules, network_events, power_events, process_events, regions, stop_signal
from alert_colors.config import App
from alert_colors.domain import AlertStatus, Coverage
from alert_colors.http import RateLimitedError
from alert_colors.regions import Place

ROOT = Path(__file__).resolve().parents[1]
CONFIG_TEXT = (ROOT / "config.toml").read_text(encoding="utf-8")
RAW = tomllib.loads(CONFIG_TEXT)
STATUS = AlertStatus(coverage=Coverage.FULL, kinds=frozenset({"air_raid"}), source="fake")
DOWN = TimeoutError("timed out")
TEST_TOKEN_VARIABLE = "ALERTS_IN_UA_TOKEN"
RETRY_AFTER_S = 7.0
SHORT_S = 0.05
FRAME_S = 0.01
EXIT_HANDLER = 7  # any code: main must return the code of the handler


def real_app():
    return App(copy.deepcopy(RAW), ROOT)


class FakeSource:
    def __init__(self, name, results):
        self.name, self.results, self.calls = name, list(results), 0

    def fetch(self):
        self.calls += 1
        result = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(result, Exception):
            raise result
        return result


class SourcesApp:
    """The real App of config.toml, with fake sources for check and status."""

    def __init__(self, sources):
        self._app, self._sources = real_app(), sources

    def __getattr__(self, name):
        return getattr(self._app, name)

    def check_sources(self):
        return self._sources


# ---- main: parse and dispatch -------------------------------------------------
class LoggingApp:
    def __init__(self):
        self.to_file = None

    def configure_logging(self, to_file=True):
        self.to_file = to_file


@pytest.fixture
def loaded(monkeypatch):
    app = LoggingApp()
    paths = []
    monkeypatch.setattr(App, "load", classmethod(lambda cls, path: paths.append(path) or app))
    return app, paths


@pytest.mark.parametrize(("command", "to_file"), [("status", False), ("run", True), ("check", False)])
def test_main_dispatches_to_the_handler_and_logs_to_file_only_for_long_commands(loaded, monkeypatch, command,
                                                                                    to_file):
    app, paths = loaded
    calls = []
    monkeypatch.setattr(cli, f"_{command}", lambda a, args: calls.append((a, args.command)) or EXIT_HANDLER)
    assert cli.main(["--config", "x.toml", command]) == EXIT_HANDLER
    assert calls == [(app, command)]
    assert app.to_file is to_file
    assert paths == [Path("x.toml")]


def test_main_parses_the_options_of_preview(loaded, monkeypatch):
    seen = {}
    monkeypatch.setattr(cli, "_preview", lambda app, args: seen.update(vars(args)) or 0)
    assert cli.main(["preview", "alert", "--seconds", "1.5"]) == 0
    assert seen["scene"] == "alert" and seen["seconds"] == 1.5
    assert seen["config"] == cli.DEFAULT_CONFIG
    assert loaded[0].to_file is True


def test_main_without_a_command_is_a_usage_error(loaded):
    with pytest.raises(SystemExit) as exit_info:
        cli.main([])
    assert exit_info.value.code == 2


def test_preview_without_seconds_is_a_usage_error(loaded):
    with pytest.raises(SystemExit):
        cli.main(["preview", "alert"])


def test_module_entry_point_exits_with_the_code_of_main(monkeypatch):
    monkeypatch.setattr(cli, "main", lambda argv=None: EXIT_HANDLER)
    # ssl is loaded in this test process; test_hardening.py runs the real entry in a new process.
    monkeypatch.setattr("alert_colors.hardening.apply", lambda: None)
    monkeypatch.delitem(sys.modules, "alert_colors.__main__", raising=False)
    with pytest.raises(SystemExit) as exit_info:
        runpy.run_module("alert_colors.__main__")
    assert exit_info.value.code == EXIT_HANDLER


# ---- _stop_event --------------------------------------------------------------
@pytest.fixture
def signals(monkeypatch):
    """Record the signal handlers instead of installing them."""
    installed = {}
    monkeypatch.setattr(cli.signal, "signal", lambda number, handler: installed.__setitem__(number, handler))
    return installed


def fake_win32api(monkeypatch, set_handler):
    module = types.ModuleType("win32api")
    module.SetConsoleCtrlHandler = set_handler
    monkeypatch.setitem(sys.modules, "win32api", module)


def test_stop_event_is_set_by_sigint_and_sigterm(signals, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    stop = cli._stop_event()
    assert set(signals) == {cli.signal.SIGINT, cli.signal.SIGTERM}
    signals[cli.signal.SIGTERM](cli.signal.SIGTERM, None)
    assert stop.is_set()
    stop.clear()
    signals[cli.signal.SIGINT](cli.signal.SIGINT, None)
    assert stop.is_set()


def test_stop_event_is_set_by_a_console_close_on_windows(signals, monkeypatch):
    handlers = []
    monkeypatch.setattr(sys, "platform", "win32")
    fake_win32api(monkeypatch, lambda handler, add: handlers.append((handler, add)))
    stop = cli._stop_event()
    [(handler, add)] = handlers
    assert add is True
    assert handler(0) is True  # True: the event is handled
    assert stop.is_set()


def test_stop_event_works_without_a_console(signals, monkeypatch):
    """pythonw has no console: SetConsoleCtrlHandler fails, and the stop event still works."""
    def no_console(handler, add):
        raise OSError("no console")

    monkeypatch.setattr(sys, "platform", "win32")
    fake_win32api(monkeypatch, no_console)
    stop = cli._stop_event()
    assert not stop.is_set()
    signals[cli.signal.SIGINT](cli.signal.SIGINT, None)
    assert stop.is_set()


# ---- run ----------------------------------------------------------------------
class FakeDaemon:
    def __init__(self, offline_retry_s, error=None):
        self.settings = types.SimpleNamespace(offline_retry_s=offline_retry_s)
        self.reclaims, self.network_changes, self.ran_with, self.error = [], 0, None, error

    def request_reclaim(self, reason="display on or resume"):
        self.reclaims.append(reason)

    def network_changed(self):
        self.network_changes += 1

    def run(self, stop):
        self.ran_with = stop
        if self.error:
            raise self.error


class RunApp:
    def __init__(self, daemon):
        self._daemon, self.dry_runs = daemon, []

    def stop_event_name(self):
        return "Local\\AlertColors.Test"

    def daemon(self, dry_run=False):
        self.dry_runs.append(dry_run)
        return self._daemon

    def vendor_watch(self):
        return ["vendor.exe"], SHORT_S


@pytest.fixture
def watchers(monkeypatch):
    """Fake watchers: record what each one got, and the order of the stops."""
    record = {"stopped": [], "stop": threading.Event()}
    monkeypatch.setattr(cli, "_stop_event", lambda finished=None: record.__setitem__("finished", finished)
                        or record["stop"])

    def stop_watch(name, stop, reset=False):
        record["stop_signal"] = (name, stop)
        record["reset"] = reset
        return object()

    def power_watch(callback, on_end_session=None):
        record["power"] = callback
        record["end_session"] = on_end_session
        return lambda: record["stopped"].append("power")

    def process_watch(names, poll_s, callback):
        record["process"] = (names, poll_s, callback)
        return lambda: record["stopped"].append("process")

    def network_watch(callback):
        record["network"] = callback
        return lambda: record["stopped"].append("network")

    monkeypatch.setattr(stop_signal, "watch", stop_watch)
    monkeypatch.setattr(power_events, "watch", power_watch)
    monkeypatch.setattr(process_events, "watch", process_watch)
    monkeypatch.setattr(network_events, "watch", network_watch)
    return record


def test_run_connects_the_watchers_to_the_daemon_and_stops_them_in_reverse(watchers):
    daemon = FakeDaemon(offline_retry_s=1.0)
    app = RunApp(daemon)
    assert cli._run(app, argparse.Namespace(dry_run=True)) == 0
    assert app.dry_runs == [True]
    assert daemon.ran_with is watchers["stop"]
    assert watchers["stop_signal"] == (app.stop_event_name(), watchers["stop"])
    assert watchers["reset"] is True  # a stop for an earlier daemon does not end this one
    assert watchers["power"] == daemon.request_reclaim
    names, poll_s, on_start = watchers["process"]
    assert (names, poll_s) == (["vendor.exe"], SHORT_S)
    on_start("vendor.exe")
    assert daemon.reclaims == ["vendor.exe started"]
    watchers["network"]()
    assert daemon.network_changes == 1
    assert watchers["stopped"] == ["network", "process", "power"]


def test_run_does_not_watch_the_network_when_offline_retry_is_off(watchers):
    assert cli._run(RunApp(FakeDaemon(offline_retry_s=0)), argparse.Namespace(dry_run=False)) == 0
    assert "network" not in watchers
    assert watchers["stopped"] == ["process", "power"]


def test_run_stops_the_watchers_when_the_daemon_fails(watchers):
    with pytest.raises(RuntimeError):
        cli._run(RunApp(FakeDaemon(offline_retry_s=1.0, error=RuntimeError("x"))), argparse.Namespace(dry_run=False))
    assert watchers["stopped"] == ["network", "process", "power"]


# ---- stop ---------------------------------------------------------------------
@pytest.mark.parametrize(("listens", "code", "text"), [(True, 0, "stop requested"), (False, 1, "no running daemon")])
def test_stop_signals_the_daemon_by_its_event_name(monkeypatch, capsys, listens, code, text):
    names = []
    monkeypatch.setattr(stop_signal, "request", lambda name: names.append(name) or listens)
    app = real_app()
    assert cli._stop(app, None) == code
    assert names == [app.stop_event_name()]
    assert capsys.readouterr().out.strip() == text


# ---- _fetch_with_retry --------------------------------------------------------
@pytest.fixture
def sleeps(monkeypatch):
    waited = []
    monkeypatch.setattr(cli.time, "sleep", waited.append)
    return waited


def test_fetch_with_retry_waits_the_retry_after_of_the_server(sleeps, capsys):
    source = FakeSource("s", [RateLimitedError(RETRY_AFTER_S), STATUS])
    assert cli._fetch_with_retry(source) is STATUS
    assert sleeps == [RETRY_AFTER_S]
    assert f"try 2/{cli.CHECK_ATTEMPTS}" in capsys.readouterr().out


def test_fetch_with_retry_doubles_the_wait_without_retry_after(sleeps):
    source = FakeSource("s", [RateLimitedError(None), RateLimitedError(None), STATUS])
    assert cli._fetch_with_retry(source) is STATUS
    assert sleeps == [cli.CHECK_FIRST_WAIT_S, 2 * cli.CHECK_FIRST_WAIT_S]


def test_fetch_with_retry_gives_up_after_the_last_attempt(sleeps):
    source = FakeSource("s", [RateLimitedError(None)])
    with pytest.raises(RateLimitedError):
        cli._fetch_with_retry(source)
    assert source.calls == cli.CHECK_ATTEMPTS
    assert len(sleeps) == cli.CHECK_ATTEMPTS - 1


def test_fetch_with_retry_does_not_retry_other_errors(sleeps):
    source = FakeSource("s", [DOWN])
    with pytest.raises(TimeoutError):
        cli._fetch_with_retry(source)
    assert source.calls == 1 and sleeps == []


# ---- check --------------------------------------------------------------------
def test_check_reports_a_config_error(monkeypatch, capsys):
    app = real_app()
    monkeypatch.setattr(app, "scenes", lambda: (_ for _ in ()).throw(KeyError("scenes")))
    assert cli._check(app, None) == 1
    assert capsys.readouterr().out.startswith("CHECK FAILED: KeyError")


def test_check_fails_when_the_only_source_is_down(capsys):
    assert cli._check(SourcesApp([("primary", FakeSource("primary", [DOWN]), "")]), None) == 1
    assert "CHECK FAILED: TimeoutError" in capsys.readouterr().out


def test_check_skips_an_unused_source_and_passes_with_the_others(capsys):
    sources = [("token", None, "no token"), ("primary", FakeSource("primary", [STATUS]), "")]
    assert cli._check(SourcesApp(sources), None) == 0
    out = capsys.readouterr().out
    assert "source token: not used: no token" in out
    assert "scene:" in out and out.rstrip().endswith("CHECK OK")


def test_check_fails_when_no_source_is_usable(capsys):
    assert cli._check(SourcesApp([("token", None, "no token")]), None) == 1
    assert "no source gives a status" in capsys.readouterr().out


def test_check_reports_an_unreachable_source_and_passes_with_another(capsys):
    sources = [("primary", FakeSource("primary", [DOWN]), ""), ("fallback", FakeSource("fallback", [STATUS]), "")]
    assert cli._check(SourcesApp(sources), None) == 0
    assert "source primary: NOT REACHABLE: TimeoutError" in capsys.readouterr().out


# ---- tune ---------------------------------------------------------------------
@pytest.mark.parametrize(("no_browser", "open_browser"), [(True, False), (False, True)])
def test_tune_serves_the_configurator_with_the_stop_event(monkeypatch, no_browser, open_browser):
    from alert_colors import tuner

    served, watched = [], []

    class FakeTuner:
        def __init__(self, app):
            self.app = app

        def serve(self, open_browser=True, stop=None):
            served.append((self.app, open_browser, stop))

    monkeypatch.setattr(tuner, "Tuner", FakeTuner)
    monkeypatch.setattr(stop_signal, "watch", lambda name, stop: watched.append((name, stop)))
    app = real_app()
    assert cli._tune(app, argparse.Namespace(no_browser=no_browser)) == 0
    [(served_app, served_open, stop)] = served
    assert served_app is app and served_open is open_browser
    assert watched == [(app.stop_event_name(), stop)]


# ---- region -------------------------------------------------------------------
def place(number, name=None):
    name = name or f"Тестове село {number}"
    return Place(f"test-{number}", "test-district", "test-oblast", name, f"{name}, Тестовий район", "")


TEST_PLACE = Place("0", "0", "0", "Тестовий регіон", "Тестовий регіон", "Тестова область")


def region_app(monkeypatch, all_places, tmp_path):
    monkeypatch.setattr(regions, "fetch_tree", lambda source: {})
    monkeypatch.setattr(regions, "places", lambda tree: all_places)
    config = tmp_path / "config.toml"
    config.write_text(CONFIG_TEXT, encoding="utf-8")
    return App(tomllib.loads(CONFIG_TEXT), ROOT), config


def answers(monkeypatch, replies):
    prompts = []
    monkeypatch.setattr("builtins.input", lambda prompt="": prompts.append(prompt) or replies.pop(0))
    return prompts


def test_region_without_the_tree_changes_nothing(monkeypatch, capsys):
    def no_tree(source):
        raise regions.RegionError("the server gives no region tree")

    monkeypatch.setattr(regions, "fetch_tree", no_tree)
    assert cli._region(real_app(), argparse.Namespace(id=None)) == cli.EXIT_UNCHANGED
    assert "no region tree" in capsys.readouterr().out


def test_region_with_an_unknown_id_fails_and_keeps_the_config(monkeypatch, tmp_path, capsys):
    app, config = region_app(monkeypatch, [TEST_PLACE], tmp_path)
    assert cli._region(app, argparse.Namespace(id="no-such-id", config=config)) == 1
    assert "No place has the id no-such-id." in capsys.readouterr().out
    assert config.read_text(encoding="utf-8") == CONFIG_TEXT


def test_region_enter_keeps_the_watched_place(monkeypatch, tmp_path, capsys):
    app, config = region_app(monkeypatch, [TEST_PLACE], tmp_path)
    answers(monkeypatch, [""])
    app.raw["source"]["vars"]["oblast_name"] = TEST_PLACE.oblast_name  # the place is complete
    assert cli._region(app, argparse.Namespace(id=None, config=config)) == cli.EXIT_UNCHANGED
    out = capsys.readouterr().out
    assert f"Watched place now: {TEST_PLACE.path}" in out
    assert "did not change" in out
    assert config.read_text(encoding="utf-8") == CONFIG_TEXT


def test_region_enter_without_a_place_changes_nothing(monkeypatch, tmp_path, capsys):
    app, config = region_app(monkeypatch, [place(1)], tmp_path)  # region_id "0" is not in the list
    answers(monkeypatch, [""])
    assert cli._region(app, argparse.Namespace(id=None, config=config)) == cli.EXIT_UNCHANGED
    assert "Watched place now: not set" in capsys.readouterr().out


def test_region_writes_the_place_chosen_by_name_and_number(monkeypatch, tmp_path, capsys):
    app, config = region_app(monkeypatch, [place(1), TEST_PLACE], tmp_path)
    answers(monkeypatch, ["регіон", "1"])
    assert cli._region(app, argparse.Namespace(id=None, config=config)) == 0
    written = tomllib.loads(config.read_text(encoding="utf-8"))["source"]["vars"]
    assert written["region_id"] == "0" and written["oblast_name"] == TEST_PLACE.oblast_name
    assert "Saved in" in capsys.readouterr().out


def test_ask_place_searches_again_after_no_match(monkeypatch, capsys):
    prompts = answers(monkeypatch, ["zzz", "село 2", "1"])
    assert cli._ask_place([place(1), place(2)]) == place(2)
    assert "No match." in capsys.readouterr().out
    assert len(prompts) == 3


def test_ask_place_lists_at_most_the_limit_and_says_how_many_more(monkeypatch, capsys):
    extra = 3
    all_places = [place(n) for n in range(1, cli.MAX_LISTED_PLACES + extra + 1)]
    answers(monkeypatch, ["село", str(cli.MAX_LISTED_PLACES + 1), ""])
    assert cli._ask_place(all_places) is None
    out = capsys.readouterr().out
    listed = [line for line in out.splitlines() if line.strip()[:1].isdigit()]
    assert len(listed) == cli.MAX_LISTED_PLACES
    assert f"... {extra} more." in out


def test_ask_place_ignores_an_answer_that_is_not_a_listed_number(monkeypatch):
    answers(monkeypatch, ["село", "0", "село", "x", "село", "2"])
    assert cli._ask_place([place(1), place(2)]) == place(2)


# ---- token --------------------------------------------------------------------
def test_token_without_token_services_changes_nothing(monkeypatch, capsys):
    from alert_colors.sources import SOURCES

    monkeypatch.setattr(SOURCES, "names", lambda: ["json_http"])
    assert cli._token(real_app(), None) == cli.EXIT_UNCHANGED
    assert "no alert service with a token" in capsys.readouterr().out


def token_setup(monkeypatch, fetch):
    from alert_colors import user_env
    from alert_colors.sources import SOURCES
    from alert_colors.sources.preset import TOKENS

    services = [name for name in TOKENS if name in SOURCES.names()]
    number = next(n for n, name in enumerate(services, start=1) if TOKENS[name].variable == TEST_TOKEN_VARIABLE)
    saved = {}
    monkeypatch.setattr("builtins.input", lambda prompt="": str(number))
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "new-token")
    monkeypatch.setattr(user_env, "put", lambda name, value: saved.__setitem__(name, value))
    monkeypatch.setattr(cli, "_fetch_with_retry", fetch)
    return saved


def test_token_saves_a_token_that_cannot_be_tested_without_the_place(monkeypatch, capsys):
    from alert_colors.sources.preset import NotConfiguredError

    def not_configured(source):
        raise NotConfiguredError("no place")

    monkeypatch.delenv(TEST_TOKEN_VARIABLE, raising=False)
    saved = token_setup(monkeypatch, not_configured)
    assert cli._token(real_app(), None) == 0
    assert saved == {TEST_TOKEN_VARIABLE: "new-token"}
    assert "Not tested: no place" in capsys.readouterr().out
    assert TEST_TOKEN_VARIABLE not in os.environ


def test_token_test_uses_the_new_token_and_restores_the_old_environment(monkeypatch):
    monkeypatch.setenv(TEST_TOKEN_VARIABLE, "old-token")
    during = []
    saved = token_setup(monkeypatch, lambda source: during.append(os.environ[TEST_TOKEN_VARIABLE]) or STATUS)
    assert cli._token(real_app(), None) == 0
    assert during == ["new-token"]
    assert os.environ[TEST_TOKEN_VARIABLE] == "old-token"
    assert saved == {TEST_TOKEN_VARIABLE: "new-token"}


def test_restore_env_removes_a_variable_that_was_not_set(monkeypatch):
    name = f"ALERT_COLORS_TEST_{uuid.uuid4().hex}"
    monkeypatch.setenv(name, "temporary")
    cli._restore_env(name, None)
    assert name not in os.environ


# ---- status -------------------------------------------------------------------
def test_status_shows_each_source_of_a_chain(capsys):
    sources = [("token", None, "no token"), ("primary", FakeSource("primary", [DOWN]), ""),
               ("fallback", FakeSource("fallback", [STATUS]), "")]
    assert cli._status(SourcesApp(sources), None) == 0
    out = capsys.readouterr().out
    assert "source token: not used: no token" in out
    assert "source primary: FAILED: TimeoutError" in out
    assert f"source fallback: {STATUS}" in out
    assert "  scene: " in out


def test_status_fails_when_no_source_of_a_chain_works(capsys):
    sources = [("primary", FakeSource("primary", [DOWN]), ""), ("fallback", FakeSource("fallback", [DOWN]), "")]
    assert cli._status(SourcesApp(sources), None) == 1


# ---- devices ------------------------------------------------------------------
class FakeBackend:
    def __init__(self, name, lines=None, error=None):
        self.name, self.lines, self.error = name, lines, error

    def describe(self):
        if self.error:
            raise self.error
        return self.lines


def test_devices_lists_each_backend_and_reports_an_unavailable_one(monkeypatch, capsys):
    app = real_app()
    backends = [FakeBackend("broken", error=RuntimeError("no SDK")), FakeBackend("fake", lines=["led 1", "led 2"])]
    monkeypatch.setattr(app, "backends", lambda: backends)
    assert cli._devices(app, None) == 0
    assert capsys.readouterr().out.splitlines() == ["broken:", "    unavailable: no SDK",
                                                    "fake:", "    led 1", "    led 2"]


# ---- preview ------------------------------------------------------------------
class RecordingOutput:
    def __init__(self, error=None):
        self.frames, self.closed, self.error = [], 0, error

    def show(self, frame):
        if self.error:
            raise self.error
        self.frames.append(frame)

    def close(self):
        self.closed += 1


def preview_app(monkeypatch, outputs, release=False):
    app = real_app()
    scene = types.SimpleNamespace(release=release, effect=object())
    monkeypatch.setattr(app, "scenes", lambda: {"test": scene})
    monkeypatch.setattr(app, "outputs", lambda: outputs)
    monkeypatch.setattr(app, "settings", lambda: types.SimpleNamespace(frame_interval_s=FRAME_S))
    return app, scene


def test_preview_refuses_a_scene_that_only_releases(monkeypatch, capsys):
    outputs = [RecordingOutput()]
    app, _scene = preview_app(monkeypatch, outputs, release=True)
    assert cli._preview(app, argparse.Namespace(scene="test", seconds=SHORT_S)) == 1
    assert "only releases the LEDs" in capsys.readouterr().out
    assert outputs[0].closed == 0


def test_preview_shows_the_scene_for_the_given_time_then_closes(monkeypatch):
    stop = threading.Event()
    monkeypatch.setattr(cli, "_stop_event", lambda finished=None: stop)
    outputs = [RecordingOutput(), RecordingOutput()]
    app, scene = preview_app(monkeypatch, outputs)
    assert cli._preview(app, argparse.Namespace(scene="test", seconds=SHORT_S)) == 0
    for output in outputs:
        assert output.frames and output.closed == 1
        assert all(frame.effect is scene.effect for frame in output.frames)
        assert all(frame.elapsed_s < SHORT_S for frame in output.frames)


def test_preview_ends_at_once_when_stopped(monkeypatch):
    stop = threading.Event()
    stop.set()
    monkeypatch.setattr(cli, "_stop_event", lambda finished=None: stop)
    outputs = [RecordingOutput()]
    app, _scene = preview_app(monkeypatch, outputs)
    assert cli._preview(app, argparse.Namespace(scene="test", seconds=60)) == 0
    assert outputs[0].frames == [] and outputs[0].closed == 1


def test_preview_releases_the_leds_when_an_output_fails(monkeypatch):
    monkeypatch.setattr(cli, "_stop_event", lambda finished=None: threading.Event())
    outputs = [RecordingOutput(), RecordingOutput(error=RuntimeError("device lost"))]
    app, _scene = preview_app(monkeypatch, outputs)
    with pytest.raises(RuntimeError):
        cli._preview(app, argparse.Namespace(scene="test", seconds=SHORT_S))
    assert [output.closed for output in outputs] == [1, 1]


# ---- modules.load_optional ----------------------------------------------------
def test_load_optional_skips_a_module_that_is_not_in_the_build(tmp_path, monkeypatch):
    package = f"alert_colors_test_pkg_{uuid.uuid4().hex}"
    (tmp_path / package).mkdir()
    (tmp_path / package / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / package / "present.py").write_text("", encoding="utf-8")
    (tmp_path / package / "broken.py").write_text("import alert_colors_no_such_dependency\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    try:
        assert modules.load_optional(package, ["present", "absent"]) == ("present",)
        with pytest.raises(ModuleNotFoundError) as error:
            modules.load_optional(package, ["broken"])
        assert error.value.name == "alert_colors_no_such_dependency"
    finally:
        for name in [name for name in sys.modules if name.startswith(package)]:
            del sys.modules[name]


def test_fetch_with_retry_fails_at_once_when_the_server_asks_too_long_a_wait(sleeps):
    """The setup runs this check while the app is stopped: it must not wait for an hour."""
    source = FakeSource("s", [RateLimitedError(cli.CHECK_MAX_WAIT_S + 1), STATUS])
    with pytest.raises(RateLimitedError):
        cli._fetch_with_retry(source)
    assert sleeps == [] and source.calls == 1


def test_the_place_picker_stops_at_once_when_the_server_asks_too_long_a_wait():
    from alert_colors import regions

    class TreeSource:
        name, lookups, calls = "s", {regions.REGION_TREE_LOOKUP: None}, 0

        def fetch_lookup_document(self, name):
            self.calls += 1
            raise RateLimitedError(regions.MAX_RETRY_WAIT_S + 1)

    waited, source = [], TreeSource()
    with pytest.raises(RateLimitedError):
        regions.fetch_tree(source, sleep=waited.append)
    assert waited == [] and source.calls == 1


@pytest.mark.parametrize(("error", "text"), [
    (RateLimitedError(600), "try again in 600 s"),
    (OSError("no route to host"), "Cannot get the list of places"),
])
def test_region_without_an_answer_of_the_server_changes_nothing(monkeypatch, capsys, error, text):
    """One line for the setup window, not a traceback; the config stays."""
    def no_answer(source):
        raise error

    monkeypatch.setattr(regions, "fetch_tree", no_answer)
    assert cli._region(real_app(), argparse.Namespace(id=None)) == cli.EXIT_UNCHANGED
    out = capsys.readouterr().out
    assert text in out and "The place did not change" in out


@pytest.mark.parametrize("error", [ValueError("Expecting value"), KeyError("regionName"), TypeError("None"),
                                   IndexError("list index out of range")])
def test_region_with_an_answer_that_is_not_a_region_tree_changes_nothing(monkeypatch, capsys, error):
    def bad_answer(source):
        raise error

    monkeypatch.setattr(regions, "fetch_tree", bad_answer)
    assert cli._region(real_app(), argparse.Namespace(id=None)) == cli.EXIT_UNCHANGED
    assert "is not usable" in capsys.readouterr().out


def test_token_with_an_answer_of_another_shape_saves_the_token_without_a_test(monkeypatch, capsys):
    def other_shape(source):
        raise KeyError("alerts")

    monkeypatch.delenv(TEST_TOKEN_VARIABLE, raising=False)
    saved = token_setup(monkeypatch, other_shape)
    assert cli._token(real_app(), None) == 0
    assert saved == {TEST_TOKEN_VARIABLE: "new-token"}
    assert "the answer of the server is not usable" in capsys.readouterr().out



def test_the_end_of_the_session_stops_the_daemon_and_waits_for_the_release(watchers, monkeypatch):
    """At logoff or shutdown Windows ends the process when the handler returns."""
    cli._run(RunApp(FakeDaemon(offline_retry_s=1.0)), argparse.Namespace(dry_run=False))
    finished = watchers["finished"]
    assert finished.is_set()  # set after daemon.run() returned: the LEDs are released
    finished.clear()
    watchers["stop"].clear()
    waited = []
    monkeypatch.setattr(finished, "wait", lambda timeout: waited.append(timeout) or True)
    watchers["end_session"]()
    assert watchers["stop"].is_set() and waited == [cli.END_SESSION_WAIT_S]



def test_preview_tells_the_console_handler_when_the_leds_are_released(monkeypatch):
    """A console close waits for this before Windows ends the process."""
    stop = threading.Event()
    seen = {}

    def stop_event(finished=None):
        seen["finished"] = finished
        return stop

    monkeypatch.setattr(cli, "_stop_event", stop_event)

    class Output(RecordingOutput):
        def close(self):
            seen["set_before_close"] = seen["finished"].is_set()  # the handler must still wait here
            super().close()

    outputs = [Output()]
    app, _scene = preview_app(monkeypatch, outputs)
    assert cli._preview(app, argparse.Namespace(scene="test", seconds=SHORT_S)) == 0
    assert outputs[0].closed == 1 and seen["finished"].is_set() and seen["set_before_close"] is False


def test_the_built_daemon_aborts_requests_on_a_network_change():
    """config wires the daemon to http.abort_active_requests, so a network change can stop a
    hung fetch."""
    from alert_colors import http

    daemon = real_app().daemon(dry_run=True)
    assert daemon.abort_requests is http.abort_active_requests
    assert daemon.fast_fail is http.fast_fail
