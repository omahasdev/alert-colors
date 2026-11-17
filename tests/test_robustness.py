"""Failover after an outage, log of long outages, region IDs, the COM code cache,
the Aura type library check, and the account and config of the elevated setup."""
import logging
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from alert_colors import regions
from alert_colors.daemon import OFFLINE_LOG_REPEAT_S, Daemon, DaemonSettings
from alert_colors.domain import AlertStatus, Coverage
from alert_colors.http import NameResolutionError
from alert_colors.policy import Policy
from alert_colors.sources.failover import FailoverSource, Member
from alert_colors.stop_signal import mark_running as REAL_MARK_RUNNING  # before the autouse fake

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
CONFIG_TEXT = (ROOT / "config.toml").read_text(encoding="utf-8")
AFTER_S, RETRY_S, RETRY_MAX_S, OFFLINE_S, FALLBACK_POLL_S = 30.0, 10.0, 120.0, 3.0, 10.0
PRIMARY_OK = AlertStatus(coverage=Coverage.NONE, kinds=frozenset(), source="primary")
FALLBACK_OK = AlertStatus(coverage=Coverage.NONE, kinds=frozenset(), source="fallback")
NO_DNS = NameResolutionError("cannot resolve example.invalid: [Errno 11001] getaddrinfo failed")
POLL_STEP_S = 3.0  # a daemon wait of 2 s plus a slow failed lookup
LONG_S = 300


class Scripted:
    """A source whose result depends on the time."""

    def __init__(self, name, clock, result_at):
        self.name, self.clock, self.result_at, self.calls = name, clock, result_at, 0

    def fetch(self):
        self.calls += 1
        result = self.result_at(self.clock["t"])
        if isinstance(result, Exception):
            raise result
        return result


# ---- a full outage must not stop the fallbacks later ----------------------
def test_after_a_full_outage_the_fallbacks_still_work_when_only_the_primary_fails():
    clock = {"t": 0.0}
    outage_end, primary_down_at = 60.0, 120.0

    def primary(t):
        return NO_DNS if t < outage_end or t >= primary_down_at else PRIMARY_OK

    def fallback(t):
        return NO_DNS if t < outage_end else FALLBACK_OK

    primary_source, fallback_source = Scripted("primary", clock, primary), Scripted("fallback", clock, fallback)
    source = FailoverSource(Member(primary_source), [Member(fallback_source, poll_interval_s=FALLBACK_POLL_S)],
                            after_s=AFTER_S, retry_s=RETRY_S, retry_max_s=RETRY_MAX_S,
                            offline_retry_s=OFFLINE_S, monotonic=lambda: clock["t"])
    results = []
    t = 0.0
    while t < primary_down_at + LONG_S:
        clock["t"] = t
        try:
            results.append((t, source.fetch()))
        except OSError:
            pass
        t += POLL_STEP_S
    later = [status for at, status in results if at >= primary_down_at + AFTER_S + RETRY_MAX_S]
    assert later, "no status at all after the primary failed alone"
    assert all(status is FALLBACK_OK for status in later)


# ---- a long "no network" failure stays visible in the log -----------------
class Failing:
    name = "fake"

    def fetch(self):
        raise NO_DNS


def test_a_long_no_network_failure_is_logged_again(caplog):
    clock = {"t": 0.0}
    daemon = Daemon(source=Failing(), scenes={}, outputs=[],
                    policy=Policy.from_config([{"state": "unknown", "scene": "unknown"}, {"scene": "clear"}],
                                              {"clear", "unknown"}),
                    settings=DaemonSettings(poll_interval_s=1, stale_after_s=180, frame_interval_s=0.1,
                                            backoff_max_s=60, offline_retry_s=OFFLINE_S),
                    monotonic=lambda: clock["t"])
    caplog.set_level(logging.WARNING, logger="alert_colors.daemon")
    t = 0.0
    while t <= 2 * OFFLINE_LOG_REPEAT_S:
        clock["t"] = t
        daemon.poll_once()
        t += OFFLINE_S
    warnings = [r.getMessage() for r in caplog.records]
    assert len(warnings) == 3  # the start, then one line each OFFLINE_LOG_REPEAT_S
    assert "still fails after" in warnings[1]


# ---- a quote in a place ID does not break the config on the next run -------
def test_a_second_region_run_after_a_quoted_id_keeps_one_key():
    """A quoted ID can come only from a config edited by hand (places() keeps digit IDs)."""
    place = regions.Place('a"b', 'a"b', 'a"b', "Тестовий регіон", "Тестовий регіон")
    once = regions.set_place(CONFIG_TEXT, place)
    twice = regions.set_place(once, place)
    assert twice == once
    assert tomllib.loads(twice)["source"]["vars"]["region_id"] == 'a"b'


def test_a_normal_second_region_run_changes_nothing():
    tree = {"states": [{"regionId": "0", "regionName": "Тестовий регіон", "regionChildIds": []}]}
    [place] = regions.places(tree)
    once = regions.set_place(CONFIG_TEXT, place)
    assert regions.set_place(once, place) == once


# ---- the COM code cache is never in a folder of the user -------------------
@pytest.mark.skipif(sys.platform != "win32", reason="win32com: Windows only")
def test_with_admin_rights_win32com_uses_the_chosen_folder(tmp_path):
    """The chosen folder (here a missing one, as after an update) becomes the cache of win32com.
    A folder that the process cannot write is covered by
    test_without_admin_rights_win32com_keeps_its_default_folder and the None case of
    test_the_com_cache_folder_for_each_kind_of_run."""
    folder = tmp_path / "missing" / "gen_py"
    code = chr(10).join([
        "import sys",
        "from alert_colors.lighting import aura_sdk",
        f"aura_sdk.com_cache_folder = lambda: {str(folder)!r}  # as with admin rights",
        "chosen = aura_sdk.admin_only_com_cache()",
        "import win32com, win32com.client",
        "print(win32com.__gen_path__ == chosen)",
    ])
    env = {**os.environ, "PYTHONPATH": str(SRC)}
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False, env=env)
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["True"]
    assert folder.is_dir()


@pytest.mark.skipif(sys.platform != "win32", reason="win32com: Windows only")
def test_without_admin_rights_win32com_keeps_its_default_folder():
    """A standard user must not need write rights in Program Files."""
    code = chr(10).join([
        "from alert_colors.lighting import aura_sdk",
        "aura_sdk.com_cache_folder = lambda: None  # as without admin rights",
        "print(aura_sdk.admin_only_com_cache())",
        "import win32com, win32com.client",
        "print('loaded')",
    ])
    env = {**os.environ, "PYTHONPATH": str(SRC)}
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False, env=env)
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["None", "loaded"]


def test_a_cache_that_loaded_before_is_refused(monkeypatch, tmp_path):
    from alert_colors.lighting import aura_sdk

    monkeypatch.setattr(aura_sdk, "_com_cache_chosen", False)  # a new process
    monkeypatch.setattr(aura_sdk, "com_cache_folder", lambda: str(tmp_path))
    monkeypatch.setitem(sys.modules, "win32com.client.gencache", object())
    with pytest.raises(RuntimeError, match="code cache"):
        aura_sdk.admin_only_com_cache()


def test_a_reconnect_after_the_cache_loaded_works(monkeypatch, tmp_path):
    """A show() error resets the SDK, and the next connect must work."""
    from alert_colors.lighting import aura_sdk

    monkeypatch.setattr(aura_sdk, "_com_cache_chosen", False)
    monkeypatch.setattr(aura_sdk, "_com_cache_folder", None)
    monkeypatch.setattr(aura_sdk, "com_cache_folder", lambda: str(tmp_path))
    monkeypatch.delitem(sys.modules, "win32com.client.gencache", raising=False)
    monkeypatch.delitem(sys.modules, "win32com.gen_py", raising=False)
    first = aura_sdk.admin_only_com_cache()
    monkeypatch.setitem(sys.modules, "win32com.client.gencache", object())  # win32com.client loaded it
    assert aura_sdk.admin_only_com_cache() == first == str(tmp_path)


def test_the_type_library_of_the_sdk_is_checked_too(monkeypatch):
    """The wrapper of win32com reads the type library to make its wrapper."""
    from alert_colors.lighting import aura_sdk

    clsid, typelib = "{00000000-0000-0000-0000-000000000001}", "{00000000-0000-0000-0000-000000000002}"

    class Registry:
        HKEY_LOCAL_MACHINE, HKEY_CURRENT_USER = "HKLM", "HKCU"
        machine = {rf"{aura_sdk.MACHINE_CLASSES}\{aura_sdk.PROG_ID}\CLSID": clsid,
                   rf"{aura_sdk.MACHINE_CLASSES}\CLSID\{clsid}\TypeLib": typelib}
        user = {rf"Software\Classes\TypeLib\{typelib}"}

        def QueryValue(self, root, path):  # noqa: N802
            if root == self.HKEY_LOCAL_MACHINE and path in self.machine:
                return self.machine[path]
            raise FileNotFoundError(path)

        def OpenKey(self, root, path):  # noqa: N802
            if root == self.HKEY_CURRENT_USER and path in self.user:
                return path
            raise FileNotFoundError(path)

        def CloseKey(self, _key):  # noqa: N802
            pass

    monkeypatch.setitem(sys.modules, "winreg", Registry())
    assert aura_sdk.per_user_overrides() == [rf"HKEY_CURRENT_USER\Software\Classes\TypeLib\{typelib}"]


# ---- a fallback gets its turn while only the primary fails ----
@pytest.mark.parametrize("poll_step_s", [1.0, 2.0, 3.0, 10.0])
def test_after_an_outage_a_fallback_gets_data_while_the_primary_host_still_fails(poll_step_s):
    clock = {"t": 0.0}
    network_back = 60.0

    def fallback(t):
        return NO_DNS if t < network_back else FALLBACK_OK

    primary_source = Scripted("primary", clock, lambda _t: NO_DNS)  # its DNS name stays broken
    fallback_source = Scripted("fallback", clock, fallback)
    source = FailoverSource(Member(primary_source), [Member(fallback_source, poll_interval_s=FALLBACK_POLL_S)],
                            after_s=AFTER_S, retry_s=RETRY_S, retry_max_s=RETRY_MAX_S,
                            offline_retry_s=OFFLINE_S, monotonic=lambda: clock["t"])
    first_data = None
    t = 0.0
    while t < network_back + LONG_S and first_data is None:
        clock["t"] = t
        try:
            if source.fetch() is FALLBACK_OK:
                first_data = t
        except OSError:
            pass
        t += poll_step_s
    assert first_data is not None, "the fallback never gave data"
    assert first_data - network_back <= 2 * max(poll_step_s, OFFLINE_S)


def test_a_network_change_ends_the_offline_state():
    clock = {"t": 0.0}
    source = FailoverSource(Member(Scripted("primary", clock, lambda _t: NO_DNS)),
                            [Member(Scripted("fallback", clock, lambda _t: NO_DNS))],
                            after_s=AFTER_S, retry_s=RETRY_S, retry_max_s=RETRY_MAX_S,
                            offline_retry_s=OFFLINE_S, monotonic=lambda: clock["t"])
    clock["t"] = AFTER_S + 1
    with pytest.raises(OSError):
        source.fetch()
    assert source._offline
    source.retry_now()
    clock["t"] += 1
    with pytest.raises(OSError):
        source.fetch()  # the change runs here; this round sees no network again
    assert all(m.last_try == clock["t"] for m in source.members)  # every source was tried


def test_the_backend_sets_the_cache_folder_before_win32com(monkeypatch):
    from alert_colors.lighting import aura_sdk

    order = []
    monkeypatch.setattr(aura_sdk, "per_user_overrides", lambda: [])
    monkeypatch.setattr(aura_sdk, "_init_mta", lambda: order.append("mta"))
    monkeypatch.setattr(aura_sdk, "admin_only_com_cache", lambda: order.append("cache"))
    monkeypatch.setitem(sys.modules, "win32com.client", None)  # the import after the cache fails
    with pytest.raises(ImportError):
        aura_sdk.AuraSdkBackend(include=["*"])._connect()
    assert order == ["mta", "cache"]




# ---- the tuning path, the running event, and region IDs --------------------------------
@pytest.mark.skipif(sys.platform != "win32", reason="Windows folders")
def test_a_faked_program_data_variable_does_not_move_the_tuning_file(tmp_path):
    code = ("from alert_colors.tuning import resolve_path" + chr(10) +
            "print(resolve_path(r'%ProgramData%" + chr(92) + "AlertColors" + chr(92) + "tuning.json'))" + chr(10))
    env = {**os.environ, "PYTHONPATH": str(SRC), "ProgramData": str(tmp_path)}
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False, env=env)
    assert result.returncode == 0, result.stderr
    assert str(tmp_path) not in result.stdout
    assert result.stdout.strip().lower().endswith(chr(92).join(["programdata", "alertcolors", "tuning.json"]))


def test_another_variable_in_the_tuning_path_still_expands(monkeypatch):
    from alert_colors import machine_paths

    monkeypatch.setenv("ALERT_COLORS_TEST_DIR", "D:/x")
    assert machine_paths.expand("%ALERT_COLORS_TEST_DIR%/t.json", strict=False, folder=str) == "D:/x/t.json"


def test_a_failed_running_event_never_stops_the_daemon(monkeypatch, caplog):
    import pywintypes
    import win32event

    from alert_colors import stop_signal

    def denied(*_args):
        raise pywintypes.error(stop_signal.ERROR_ACCESS_DENIED, "CreateEvent", "Access is denied.")

    monkeypatch.setattr(win32event, "CreateEvent", denied)
    with caplog.at_level(logging.WARNING, logger="alert_colors.stop_signal"):
        assert REAL_MARK_RUNNING(r"Local\AlertColors.Test") is None
    assert "cannot make the event" in caplog.text


# ---- the COM cache folder for each kind of run, and the account of the setup ------------
@pytest.mark.parametrize(("frozen", "admin", "in_program_files"),
                         [(True, True, False), (False, True, True), (False, False, False), (True, False, False)])
def test_the_com_cache_folder_for_each_kind_of_run(monkeypatch, frozen, admin, in_program_files):
    from alert_colors.lighting import aura_sdk

    program_files = "C:/Machine Program Files"
    monkeypatch.setattr(aura_sdk.machine_paths, "machine_folder", lambda _csidl: program_files)
    folder = aura_sdk.com_cache_folder(frozen=frozen, admin=admin)
    if not admin:
        assert folder is None  # the default of win32com
        return
    assert folder.startswith(program_files) == in_program_files
    if not in_program_files:
        assert os.path.dirname(folder) == os.path.dirname(os.path.abspath(sys.executable))


def test_the_elevated_setup_takes_the_account_from_install_user_only():
    """A different admin account can approve the UAC prompt: the task and the tuning
    folder are for the account that started setup.cmd."""
    script = (ROOT / "scripts" / "manage.ps1").read_text(encoding="utf-8")
    code = [line.strip() for line in script.splitlines() if not line.strip().startswith("#")]
    assert not [line for line in code if "$env:USERNAME" in line]  # the environment of the user can lie
    [target] = [line for line in code if line.startswith("$TargetUser =")]
    assert "if ($InstallUser) { $InstallUser }" in target
    assert "[Security.Principal.WindowsIdentity]::GetCurrent().Name" in target
    [passed] = [line for line in code if line.startswith("'-InstallUser',")]
    assert "$TargetUser" in passed and "'-InstallConfigSource'" in passed and "$SourceConfig" in passed
    assert script.count("$user = $TargetUser") == 2  # Initialize-TuningDir and Register-Task


def test_the_elevated_setup_uses_the_config_of_the_part_without_admin_rights():
    """The %APPDATA% of another admin account must not choose the config."""
    script = (ROOT / "scripts" / "manage.ps1").read_text(encoding="utf-8")
    assert "if ($InstallConfigSource) { $SourceConfig = $InstallConfigSource }" in script
    assert script.index("$SourceConfig = if (Test-Path $MachineConfig)") < \
        script.index("if ($InstallConfigSource) { $SourceConfig = $InstallConfigSource }")


def test_the_tuning_folder_is_locked_before_the_link_check_and_the_user_grant():
    """No write right for users while the folder is checked."""
    script = (ROOT / "scripts" / "manage.ps1").read_text(encoding="utf-8")
    body = script[script.index("function Initialize-TuningDir"):]
    body = body[:body.index("\n}")]
    lock, check, grant = (body.index("Set-LockedRights $TuningDir"),  # test_setup_rights.py runs it for real
                          body.rindex("Assert-NoLinks $TuningDir"),
                          body.index('Grant-UserWrite $TuningDir $user'))
    assert lock < check < grant
    assert "Grant-UserWrite" not in body[:grant]  # the user gets write rights only at the end
    assert "-ErrorAction Stop" in script[script.index("function Assert-NoLinks"):]


# ---- numbers that are not finite in effect options --------------------------------------------
@pytest.mark.parametrize("raw", [float("nan"), float("inf"), float("-inf")])
@pytest.mark.parametrize("kind", ["int", "float"])
def test_an_effect_option_that_is_not_a_finite_number_is_refused(raw, kind):
    """NaN passes every comparison with the limits: it must not reach the render."""
    from alert_colors import effects

    param = effects.Param("speed", getattr(effects, kind.upper()), 1, min=0, max=10)
    with pytest.raises(ValueError, match="not a finite number"):
        param.parse(raw)


def test_an_effect_option_given_as_the_text_nan_is_refused():
    from alert_colors import effects

    with pytest.raises(ValueError, match="not a finite number"):
        effects.Param("speed", effects.FLOAT, 1, min=0, max=10).parse("nan")


@pytest.mark.parametrize("kind", ["int", "float"])
def test_an_effect_option_that_is_a_huge_integer_is_refused(kind):
    from alert_colors import effects

    with pytest.raises(ValueError, match="too large"):
        effects.Param("speed", getattr(effects, kind.upper()), 1, min=0, max=10).parse(10 ** 400)
