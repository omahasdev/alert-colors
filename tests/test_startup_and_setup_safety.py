"""Start of the app without a log file or machine roots, hardening of the console
script, and the checks of the tuning folder in the setup."""
import copy
import logging
import logging.handlers
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from alert_colors import hardening
from alert_colors.config import App

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
RAW = tomllib.loads((ROOT / "config.toml").read_text(encoding="utf-8"))
SCRIPT = (ROOT / "scripts" / "manage.ps1").read_text(encoding="utf-8")


def run(code, env_extra=None):
    env = {**os.environ, "PYTHONPATH": str(SRC), **(env_extra or {})}
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False, env=env)


# ---- a standard user cannot write the log file, and the app runs on -------
def test_a_log_file_that_cannot_open_does_not_stop_the_app(monkeypatch, tmp_path, capsys):
    def denied(*_args, **_kwargs):
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(logging.handlers, "RotatingFileHandler", denied)
    root = logging.getLogger()
    saved = root.handlers[:], root.level
    try:
        app = App(copy.deepcopy(RAW), tmp_path)
        app.configure_logging()  # no exception
        assert not any(type(h).__name__ == "RotatingFileHandler" for h in root.handlers)
    finally:
        root.handlers, root.level = saved
    assert "no log file" in capsys.readouterr().err  # basicConfig(force=True) replaced the handlers


# ---- the TLS context is made at the first https request, not at import ----
def test_no_machine_root_does_not_stop_the_import_of_the_app():
    code = "\n".join([
        "from alert_colors import hardening",
        "hardening.apply()",
        "import alert_colors.http as http",
        "print(http._LazyTLSHandler._shared is None)",  # nothing built at import
        "http.machine_root_certificates = lambda: b''",
        "import alert_colors.cli",  # every command still loads
        "print('cli loaded')",
    ])
    result = run(code)
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["True", "cli", "loaded"]


# ---- PYTHONTZPATH and the console script ------------------------------------
def test_a_time_zone_path_of_the_user_is_removed():
    environ = {"PYTHONTZPATH": "D:/fake-zones", "TZ": "keep"}
    assert hardening.remove_unsafe_variables(environ) == ["PYTHONTZPATH"]
    assert environ == {"TZ": "keep"}


def test_the_console_script_hardens_first():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["scripts"]["alert-colors"] == "alert_colors.hardening:main"
    result = run("import sys\nsys.argv = ['alert-colors', '--help']\n"
                 "from alert_colors import hardening\ntry:\n    hardening.main()\nexcept SystemExit as e:\n"
                 "    print('exit', e.code)\nimport os\nprint('OPENSSL_CONF' in os.environ)",
                 {"OPENSSL_CONF": "D:/missing.cnf"})
    assert result.returncode == 0, result.stderr
    assert result.stdout.split()[-3:] == ["exit", "0", "False"]


def ps_function(name):
    start = SCRIPT.index(f"function {name}(")
    return SCRIPT[start:SCRIPT.index("\n}", start) + 2]


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell")
@pytest.mark.parametrize(("make", "ok"), [("file", True), ("folder", False), ("hardlink", False)])
def test_the_tuning_folder_may_hold_files_only(tmp_path, make, ok):
    """The real Assert-NoLinks of manage.ps1: a subfolder stops the setup."""
    folder = tmp_path / "tuning"
    folder.mkdir()
    (folder / "tuning.json").write_text("{}", encoding="utf-8")
    if make == "folder":
        (folder / "sub").mkdir()
    if make == "hardlink":  # a second name of a file outside the folder
        outside = tmp_path / "outside.txt"
        outside.write_text("x", encoding="utf-8")
        os.link(outside, folder / "other.json")
    code = "\n".join([link_check_code(), f"Assert-NoLinks '{folder}'"])
    result = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", code],
                            capture_output=True, text=True, check=False)
    assert (result.returncode == 0) == ok, result.stderr
    if not ok:
        assert "contains folders or links" in result.stderr




# ---- owner of the tuning folder, and a fast stop of the poller -------------------------
def test_administrators_own_the_folder_before_the_lock():
    """The owner of a folder can always change its rights again, so a folder
    that a user made first must belong to Administrators before Set-LockedRights."""
    body = SCRIPT[SCRIPT.index("function Initialize-TuningDir"):]
    body = body[:body.index("\n}")]
    owner = body.index('/setowner "*$SidAdministrators"')
    assert owner < body.index("Set-LockedRights $TuningDir")
    assert "throw" in body[owner:body.index("Set-LockedRights $TuningDir")]  # a failed owner change stops it


def test_a_stop_ends_the_poller_at_once():
    """The run() method sets the wake event at its end, so a long poll wait does not keep the poller."""
    import threading
    import time

    from alert_colors.daemon import Daemon, DaemonSettings
    from alert_colors.domain import AlertStatus, Coverage
    from alert_colors.policy import Policy

    class Source:
        name = "fake"

        def fetch(self):
            return AlertStatus(coverage=Coverage.NONE, kinds=frozenset())

    long_poll_s, quick_s = 3600.0, 2.0
    daemon = Daemon(source=Source(), scenes={}, outputs=[],
                    policy=Policy.from_config([{"state": "unknown", "scene": "unknown"}, {"scene": "clear"}],
                                              {"clear", "unknown"}),
                    settings=DaemonSettings(poll_interval_s=long_poll_s, stale_after_s=100, frame_interval_s=0.01))
    daemon.step = lambda: None  # no scenes: only the threads are tested
    stop = threading.Event()
    runner = threading.Thread(target=daemon.run, args=(stop,))
    runner.start()
    time.sleep(0.2)  # the poller fetched once and now waits long_poll_s
    stop.set()
    runner.join(quick_s)
    pollers = [t for t in threading.enumerate() if t.name == "poller"]
    for poller in pollers:
        poller.join(quick_s)
    assert not runner.is_alive()
    assert not any(t.is_alive() for t in pollers)


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell")
def test_a_hard_link_that_is_held_open_is_refused_too(tmp_path):
    """LinkType is empty while another process holds the file open without
    sharing; the real Assert-NoLinks of manage.ps1 must refuse the link anyway."""
    import ctypes
    from ctypes import wintypes

    folder = tmp_path / "tuning"
    folder.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("x", encoding="utf-8")
    link = folder / "other.json"
    os.link(outside, link)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                     wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    generic_read, no_sharing, open_existing = 0x80000000, 0, 3
    handle = kernel32.CreateFileW(str(link), generic_read, no_sharing, None, open_existing, 0, None)
    assert handle not in (None, wintypes.HANDLE(-1).value)
    try:
        code = "\n".join([link_check_code(),
                          f"Assert-NoLinks '{folder}'"])
        result = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", code],
                                capture_output=True, text=True, check=False)
    finally:
        kernel32.CloseHandle(handle)
    assert result.returncode != 0
    assert "is a hard link (2 names)" in result.stderr  # the fsutil check, not another one


def deny_all_check(tmp_path, owner_stub):
    folder = tmp_path / "tuning"
    folder.mkdir()
    locked = folder / "tuning.json"
    locked.write_text("{}", encoding="utf-8")
    everyone = "*S-1-1-0"
    subprocess.run(["icacls", str(locked), "/deny", f"{everyone}:F"], capture_output=True, check=True)
    try:
        code = chr(10).join([link_check_code(), owner_stub, f"Assert-NoLinks '{folder}'"])
        return subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", code],
                              capture_output=True, text=True, check=False)
    finally:  # the owner can always change the rules: remove the deny, so that tmp_path can be deleted
        subprocess.run(["icacls", str(locked), "/remove:d", everyone], capture_output=True, check=False)


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell")
def test_a_file_of_the_user_that_cannot_be_checked_stops_the_setup(tmp_path):
    """A deny rule makes fsutil fail (exit 1, one error line). Only the exit
    code shows it, so the real Assert-NoLinks must stop on it."""
    result = deny_all_check(tmp_path, "function Get-OwnerSid([string]$Path) { Get-TargetSid }")
    assert result.returncode != 0
    assert "Cannot check" in result.stderr


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell")
def test_a_file_whose_owner_cannot_be_read_is_left_to_the_renew_step(tmp_path):
    """Its owner can refuse the admin every read: it counts as a file of another account, and
    Replace-Files deletes it after the lock."""
    result = deny_all_check(tmp_path, "function Get-OwnerSid([string]$Path) { $null }")
    assert result.returncode == 0, result.stderr


def test_a_blocked_tuning_read_stops_neither_the_frames_nor_the_app():
    """The user can hold a read of the tuning file open for any time. The render loop and the
    stop must not wait for it: the read runs on its own thread."""
    import threading
    import time

    from alert_colors.daemon import Daemon, DaemonSettings
    from alert_colors.domain import AlertStatus, Color, Coverage
    from alert_colors.effects import Solid
    from alert_colors.policy import Policy
    from alert_colors.scenes import Scene

    held = threading.Event()  # never set: the read waits for the whole test

    class BlockedWatcher:
        check_interval_s = 0.01

        def poll(self):
            held.wait()

    class Source:
        name = "fake"

        def fetch(self):
            return AlertStatus(coverage=Coverage.NONE, kinds=frozenset())

    frames = []

    class Output:
        name = "fake"

        def show(self, frame):
            frames.append(frame)

        def close(self):
            pass

        def reclaim(self):
            pass

    quick_s = 2.0
    black = Solid(Color(0, 0, 0))
    daemon = Daemon(source=Source(), scenes={name: Scene(name, black) for name in ("clear", "unknown")},
                    outputs=[Output()], tuning=BlockedWatcher(),
                    policy=Policy.from_config([{"state": "unknown", "scene": "unknown"}, {"scene": "clear"}],
                                              {"clear", "unknown"}),
                    settings=DaemonSettings(poll_interval_s=3600, stale_after_s=100, frame_interval_s=0.01))
    stop = threading.Event()
    runner = threading.Thread(target=daemon.run, args=(stop,))
    runner.start()
    time.sleep(0.3)
    frames_while_blocked = len(frames)
    stop.set()
    runner.join(quick_s)
    try:
        assert frames_while_blocked > 5  # the render loop goes on while the read is held
        assert not runner.is_alive()  # and the stop works
    finally:
        held.set()


def _tuning_daemon(watcher, frames, frame_interval_s):
    from alert_colors.daemon import Daemon, DaemonSettings
    from alert_colors.domain import AlertStatus, Color, Coverage
    from alert_colors.effects import Solid
    from alert_colors.policy import Policy
    from alert_colors.scenes import Scene

    class Source:
        name = "fake"

        def fetch(self):
            return AlertStatus(coverage=Coverage.NONE, kinds=frozenset())

    class Output:
        name = "fake"

        def show(self, frame):
            frames.append(frame.color())

        def close(self):
            pass

        def reclaim(self):
            pass

    blue = Solid(Color(0, 0, 255))
    return Daemon(source=Source(), scenes={name: Scene(name, blue) for name in ("clear", "unknown")},
                  outputs=[Output()], tuning=watcher,
                  policy=Policy.from_config([{"state": "unknown", "scene": "unknown"}, {"scene": "clear"}],
                                            {"clear", "unknown"}),
                  settings=DaemonSettings(poll_interval_s=3600, stale_after_s=100,
                                          frame_interval_s=frame_interval_s))


def _run_for(daemon, seconds):
    import threading
    import time

    stop = threading.Event()
    runner = threading.Thread(target=daemon.run, args=(stop,))
    runner.start()
    time.sleep(seconds)
    stop.set()
    runner.join(2.0)
    assert not runner.is_alive()


def test_the_first_frame_of_run_uses_the_tuning():
    import time

    from alert_colors.domain import Color
    from alert_colors.effects import Solid
    from alert_colors.scenes import Scene
    from alert_colors.tuning import Tuning

    red = {name: Scene(name, Solid(Color(255, 0, 0))) for name in ("clear", "unknown")}

    class Watcher:
        check_interval_s = 1.0

        def __init__(self):
            self.answers = [Tuning(scenes=red, scene_options={}, brightness=None, lighting={})]

        def poll(self):
            if self.answers:
                time.sleep(READ_S)  # a real file read takes time: the render loop must wait for it
                return self.answers.pop()
            return None

    frame_s = 0.05
    READ_S = frame_s / 2.5  # shorter than one frame interval, the longest wait for the first read
    frames = []
    _run_for(_tuning_daemon(Watcher(), frames, frame_s), 0.2)
    assert frames and frames[0] == Color(255, 0, 0)


@pytest.mark.parametrize("check_interval_s", [0.0, 10.0])
def test_the_tuning_thread_asks_once_each_frame_interval(check_interval_s):
    """No busy loop for check_interval_s = 0, and no second time gate for a long interval:
    the watcher alone waits check_interval_s between two reads of the file."""
    class Watcher:
        def __init__(self):
            self.check_interval_s, self.calls = check_interval_s, 0

        def poll(self):
            self.calls += 1

    frame_s, run_s = 0.05, 0.5
    watcher, frames = Watcher(), []
    _run_for(_tuning_daemon(watcher, frames, frame_s), run_s)
    asks = run_s / frame_s
    assert asks / 2 <= watcher.calls <= asks + 3


def test_the_tuning_thread_never_loops_without_a_wait():
    """With frame_interval_s = 0 (the config accepts it), the minimum wait keeps the tuning
    thread from a busy loop."""
    from alert_colors.daemon import MIN_TUNING_WAIT_S

    class Watcher:
        check_interval_s = 0.0

        def __init__(self):
            self.calls = 0

        def poll(self):
            self.calls += 1

    run_s = 0.3
    watcher, frames = Watcher(), []
    _run_for(_tuning_daemon(watcher, frames, 0.0), run_s)
    assert watcher.calls <= run_s / MIN_TUNING_WAIT_S + 5


def test_each_file_is_renewed_after_the_lock_and_the_link_check():
    """A file that another account made first is replaced by a new file of this admin process,
    after the lock (no new file can come) and after the second link check."""
    body = SCRIPT[SCRIPT.index("function Initialize-TuningDir"):]
    body = body[:body.index("\n}\n")]
    lock = body.index("Set-LockedRights $TuningDir")
    second_check = body.index("Assert-NoLinks $TuningDir", lock)
    renew = body.index("Replace-Files $TuningDir")
    grant = body.index('Grant-UserWrite $TuningDir $user')
    assert body.index("Assert-AdminOwner $TuningDir") < lock < second_check < renew < grant


def link_check_code():
    """Assert-NoLinks with what it needs: the SIDs, the owner helpers (this test account is the
    installing user) and the stop at the first error, as in the script."""
    import re

    def function(name):
        start = re.search(rf"^function {re.escape(name)}[ (]", SCRIPT, re.M).start()
        return SCRIPT[start:SCRIPT.index("\n}", start) + 2]

    sids = "\n".join(line for line in SCRIPT.splitlines() if line.startswith("$Sid") and " = '" in line)
    return "\n".join(["$ErrorActionPreference = 'Stop'", sids,
                      "$TargetUser = [Security.Principal.WindowsIdentity]::GetCurrent().Name",
                      function("Get-OwnerSid"), function("Get-TargetSid"),
                      function("Test-ReparsePoint"), function("Assert-NoLinks")])
