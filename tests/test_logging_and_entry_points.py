"""The 429 wait and network changes, the log of a standard user, and checks that the
entry points, the run command and the COM loader really do their part."""
import argparse
import copy
import logging.handlers
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import test_cli_commands as cli_tests

from alert_colors import cli, config, stop_signal
from alert_colors.config import App
from alert_colors.daemon import Daemon, DaemonSettings
from alert_colors.domain import AlertStatus, Coverage
from alert_colors.http import RateLimitedError
from alert_colors.policy import Policy
from alert_colors.sources.failover import FailoverSource, Member

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
RAW = tomllib.loads((ROOT / "config.toml").read_text(encoding="utf-8"))
OK = AlertStatus(coverage=Coverage.NONE, kinds=frozenset())


def run(code, env_extra=None):
    env = {**os.environ, "PYTHONPATH": str(SRC), **(env_extra or {})}
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False, env=env)


# ---- no wake by a network change while a source is in its 429 wait --------
class Scripted:
    def __init__(self, name, results):
        self.name, self.results = name, list(results)

    def fetch(self):
        result = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(result, Exception):
            raise result
        return result


def test_a_429_in_a_failover_round_blocks_the_network_wake():
    import urllib.error

    clock = {"t": 0.0}
    server_error = urllib.error.HTTPError("https://example.invalid", 500, "Error", None, None)
    source = FailoverSource(Member(Scripted("primary", [OK, RateLimitedError(None)])),
                            [Member(Scripted("fallback", [server_error]))],
                            after_s=30, retry_s=10, retry_max_s=120, offline_retry_s=3,
                            monotonic=lambda: clock["t"])
    daemon = Daemon(source=source, scenes={}, outputs=[],
                    policy=Policy.from_config([{"state": "unknown", "scene": "unknown"}, {"scene": "clear"}],
                                              {"clear", "unknown"}),
                    settings=DaemonSettings(poll_interval_s=1, stale_after_s=100, frame_interval_s=0.1,
                                            backoff_max_s=8, offline_retry_s=3),
                    monotonic=lambda: clock["t"])
    daemon.poll_once()
    clock["t"] = 31.0  # past after_s: the fallback is tried too
    daemon.poll_once()
    daemon.network_changed()
    assert not daemon._wake.is_set()


# ---- a standard user gets a log file in %LOCALAPPDATA% ----------------------
def test_without_admin_rights_the_log_goes_to_local_app_data(monkeypatch, tmp_path):
    import ctypes

    local = tmp_path / "local"
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setattr(ctypes.windll.shell32, "IsUserAnAdmin", lambda: 0)
    real = logging.handlers.RotatingFileHandler
    opened = []

    def handler(path, **kwargs):
        if str(path).startswith(str(local)):
            opened.append(Path(path))
            return real(path, **kwargs)
        raise PermissionError(13, "Access is denied")  # the folder in Program Files

    monkeypatch.setattr(logging.handlers, "RotatingFileHandler", handler)
    root = logging.getLogger()
    saved = root.handlers[:], root.level
    try:
        App(copy.deepcopy(RAW), tmp_path / "install").configure_logging()
        for h in root.handlers:
            h.close()
    finally:
        root.handlers, root.level = saved
    assert opened == [local / config.USER_LOG_FOLDER / RAW["logging"]["file"]]


def test_an_admin_process_never_logs_to_a_user_folder(monkeypatch, tmp_path):
    import ctypes

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(ctypes.windll.shell32, "IsUserAnAdmin", lambda: 1)
    assert config.user_log_path("alert-colors.log") is None


# ---- the DLL limit of apply(), the running event of run, the COM reference -----------
@pytest.mark.skipif(sys.platform != "win32", reason="Windows DLL search")
def test_apply_itself_limits_the_dll_search(tmp_path):
    import shutil

    for name in ("alert_colors_probe_e.dll", "alert_colors_probe_f.dll"):
        shutil.copy(Path(os.environ["SystemRoot"]) / "System32" / "version.dll", tmp_path / name)
    code = chr(10).join([
        "import ctypes",
        "from ctypes import wintypes",
        "k = ctypes.WinDLL('kernel32', use_last_error=True)",
        "k.LoadLibraryW.restype = wintypes.HMODULE",
        "k.LoadLibraryW.argtypes = [wintypes.LPCWSTR]",
        "print('before:', 'loaded' if k.LoadLibraryW('alert_colors_probe_e.dll') else 'refused')",
        "from alert_colors import hardening",
        "hardening.apply()",  # the entry points call this, not restrict_dll_search()
        "print('after:', 'loaded' if k.LoadLibraryW('alert_colors_probe_f.dll') else 'refused')",
    ])
    result = run(code, {"PATH": f"{tmp_path};{os.environ['PATH']}"})
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["before:", "loaded", "after:", "refused"]


def test_run_makes_the_running_event(monkeypatch, watchers):
    made = []
    monkeypatch.setattr(stop_signal, "mark_running", lambda name: made.append(name) or object())
    app = cli_tests.RunApp(cli_tests.FakeDaemon(offline_retry_s=1.0))
    assert cli._run(app, argparse.Namespace(dry_run=False)) == 0
    assert made == [app.stop_event_name()]


def test_a_dry_run_makes_no_running_event(monkeypatch, watchers):
    made = []
    monkeypatch.setattr(stop_signal, "mark_running", lambda name: made.append(name))
    assert cli._run(cli_tests.RunApp(cli_tests.FakeDaemon(offline_retry_s=1.0)), argparse.Namespace(dry_run=True)) == 0
    assert made == []  # a dry run uses no SDK: devices and preview may run beside it


@pytest.mark.skipif(sys.platform != "win32", reason="real COM")
def test_the_loader_leaves_one_reference_for_the_wrapper():
    code = chr(10).join([
        "import ctypes, pythoncom",
        "from alert_colors.lighting import aura_sdk",
        "seen = []",
        "real = pythoncom.ObjectFromAddress",
        "def capture(address, iid):",
        "    seen.append(address)",
        "    return real(address, iid)",
        "pythoncom.ObjectFromAddress = capture",
        "wrapper = aura_sdk.machine_instance('Scripting.Dictionary')",
        "vtable = ctypes.cast(ctypes.c_void_p(seen[0]), ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]",
        "addref = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtable[1])",
        "release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtable[2])",
        "print(addref(seen[0]) - 1)",  # the references before this AddRef
        "release(seen[0])",
    ])
    result = run(code)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "1"  # the wrapper only: CreateInstance's reference was given back


watchers = cli_tests.watchers  # the fixture of test_cli_commands.py
