"""A vendor app start takes the LEDs again (see process_events.py)."""
import logging
import sys
import tomllib
from pathlib import Path

import pytest

from alert_colors import process_events
from alert_colors.config import App
from alert_colors.process_events import ProcessWatcher, started, watched

ROOT = Path(__file__).resolve().parents[1]
RAW = tomllib.loads((ROOT / "config.toml").read_text(encoding="utf-8"))
ARMOURY = "ArmouryCrate.exe"
NAMES = [ARMOURY, "LightingService.exe"]
POLL_S = 3600.0  # the thread never checks by itself: the tests call check()
OLD_PID, NEW_PID, OTHER_PID = 100, 200, 300


def test_watched_ignores_case_and_other_names():
    processes = [(OLD_PID, "armourycrate.EXE"), (OTHER_PID, "notepad.exe")]
    assert watched(processes, NAMES) == {OLD_PID: "armourycrate.EXE"}


def test_started_gives_only_new_process_ids():
    previous = {OLD_PID: ARMOURY}
    assert started(previous, {OLD_PID: ARMOURY}) == []
    assert started(previous, {OLD_PID: ARMOURY, NEW_PID: ARMOURY}) == [ARMOURY]
    assert started(previous, {}) == []  # an exit is not a start


def make_watcher(snapshots):
    calls = []
    state = {"list": snapshots[0]}
    watcher = ProcessWatcher(NAMES, POLL_S, calls.append, snapshot=lambda: state["list"])
    return watcher, state, calls


def test_a_process_that_runs_at_the_start_is_no_event():
    watcher, state, calls = make_watcher([[(OLD_PID, ARMOURY)]])
    watcher.check()
    watcher.stop()
    assert calls == []


def test_a_new_process_is_one_event():
    watcher, state, calls = make_watcher([[]])
    state["list"] = [(NEW_PID, ARMOURY), (OTHER_PID, "notepad.exe")]
    watcher.check()
    watcher.check()  # the same process again: no second event
    watcher.stop()
    assert calls == [ARMOURY]


def test_a_restart_with_a_new_pid_is_an_event_again():
    watcher, state, calls = make_watcher([[(OLD_PID, ARMOURY)]])
    state["list"] = []
    watcher.check()
    state["list"] = [(NEW_PID, ARMOURY)]
    watcher.check()
    watcher.stop()
    assert calls == [ARMOURY]


def test_no_names_means_no_watcher():
    stop = process_events.watch([], POLL_S, lambda name: None)
    stop()  # nothing to stop, no error


def test_the_shipped_config_watches_armoury_crate():
    names, poll_s = App(RAW, ROOT).vendor_watch()
    assert ARMOURY in names
    assert poll_s > 0


@pytest.mark.skipif(sys.platform != "win32", reason="Toolhelp is Windows only")
def test_the_process_list_has_this_process():
    import os

    assert os.getpid() in dict(process_events.list_processes())


def test_a_vendor_start_logs_the_reason(caplog):
    daemon = App(RAW, ROOT).daemon(dry_run=True)
    with caplog.at_level(logging.INFO, logger="alert_colors.daemon"):
        daemon.request_reclaim(f"{ARMOURY} started")
    assert f"{ARMOURY} started: taking the LEDs again" in caplog.text
