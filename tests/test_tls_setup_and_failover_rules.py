"""TLS at import, the Python steps of the setup, the user log, the configurator log,
the TLS handler on each Python version, and two failover rules."""
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from alert_colors import config, hardening

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
SCRIPT = (ROOT / "scripts" / "manage.ps1").read_text(encoding="utf-8")


def run(code, env_extra=None):
    env = {**os.environ, "PYTHONPATH": str(SRC), **(env_extra or {})}
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False, env=env)


# ---- no TLS context is built at import ------------------------------------
def test_the_import_works_when_the_default_certificate_stores_fail():
    """A certificate in a store of the user that OpenSSL refuses must not stop the app."""
    code = chr(10).join([
        "import ssl",
        "def refuse(*_args, **_kwargs):",
        "    raise ssl.SSLError('a certificate that OpenSSL refuses')",
        "ssl.SSLContext.load_default_certs = refuse",
        "import alert_colors.http as http",
        "import alert_colors.cli",
        "print(http._LazyTLSHandler._shared is None)",
    ])
    result = run(code)
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["True"]


# ---- the setup steps, the unsafe variables and the user log path -----------
PYTHON_STEPS = ("Initialize-Venv", "Invoke-Build", "Test-Config", "Set-Token", "Select-Place")


@pytest.mark.parametrize("function", PYTHON_STEPS)
def test_each_python_step_refuses_admin_rights(function):
    start = SCRIPT.index(f"function {function}")
    body = SCRIPT[start:SCRIPT.index("\n}", start)]
    first_python = min(i for i in (body.find("$VenvPython"), body.find("Initialize-Venv", 20)) if i > 0)
    assert 0 < body.index("Assert-NoAdminPython") < first_python


def test_the_setup_renews_the_files_after_the_lock():
    body = SCRIPT[SCRIPT.index("function Initialize-TuningDir"):]
    body = body[:body.index("\n}")]
    assert body.index("Set-LockedRights $TuningDir") < body.index("Replace-Files $TuningDir") < \
        body.index('Grant-UserWrite $TuningDir $user')


# A fixed list: a test that takes the list from the code cannot see a removed name.
OPENSSL_AND_TZ_VARIABLES = ("OPENSSL_CONF", "OPENSSL_CONF_INCLUDE", "OPENSSL_MODULES", "OPENSSL_ENGINES",
                            "SSL_CERT_FILE", "SSL_CERT_DIR", "SSLKEYLOGFILE", "PYTHONTZPATH")


@pytest.mark.parametrize("name", OPENSSL_AND_TZ_VARIABLES)
def test_each_unsafe_variable_is_removed(name):
    environ = {name: "x", "PATH": "p"}
    hardening.remove_unsafe_variables(environ)
    assert environ == {"PATH": "p"}


def test_the_user_log_path_keeps_only_the_file_name(monkeypatch, tmp_path):
    import ctypes

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(ctypes.windll.shell32, "IsUserAnAdmin", lambda: 0)
    path = config.user_log_path(r"..\..\elsewhere\alert-colors.log")
    assert path == tmp_path / config.USER_LOG_FOLDER / "alert-colors.log"


# ---- the configurator reads the log that the app writes --------------------
def test_the_configurator_reads_the_newer_log(monkeypatch, tmp_path):
    from alert_colors import tuner

    install, user = tmp_path / "install.log", tmp_path / "user.log"
    install.write_text("x scene clear -> clear\n", encoding="utf-8")
    time.sleep(0.05)
    user.write_text("x scene unknown -> alert\n", encoding="utf-8")
    os.utime(install, (time.time() - 100, time.time() - 100))
    monkeypatch.setattr(tuner, "user_log_path", lambda _name: user)
    t = tuner.Tuner.__new__(tuner.Tuner)
    t.app = type("App", (), {"raw": {"logging": {"file": "x.log"}}, "log_path": lambda self, _n: install})()
    assert t._current_scene() == "alert"




# ---- the TLS handler on each Python, the UAC guard, and a log rotation -----------------
HANDLER_CHECK = chr(10).join([
    "import alert_colors.http as http",
    "machine = object()",
    "http._LazyTLSHandler._shared = machine",
    "seen = []",
    "def do_open(self, _connection, _req, **kwargs):",
    "    seen.append(kwargs.get('context'))",
    "    raise OSError('stop')",
    "http._LazyTLSHandler.do_open = do_open",
    "handler = http._LazyTLSHandler()",
    "try:",
    "    handler.https_open(object())",
    "except OSError:",
    "    pass",
    "print(seen == [machine])",
])


@pytest.mark.skipif(sys.platform != "win32", reason="the py launcher")
@pytest.mark.parametrize("version", ["3.11", "3.12", "3.13", "3.14"])
def test_the_tls_handler_works_on_each_supported_python(version):
    """On Python 3.11, https_open() also reads _check_hostname."""
    import shutil

    if shutil.which("py") is None:
        pytest.skip("no py launcher")
    probe = subprocess.run(["py", f"-{version}", "-c", "pass"], capture_output=True, check=False)
    if probe.returncode != 0:
        pytest.skip(f"Python {version} is not installed")
    result = subprocess.run(["py", f"-{version}", "-c", HANDLER_CHECK], capture_output=True, text=True,
                            check=False, env={**os.environ, "PYTHONPATH": str(SRC)})
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "True"


def test_the_elevated_part_stops_when_it_got_no_admin_rights():
    """With UAC turned off, RunAs gives no admin rights; the setup must not ask again."""
    block = SCRIPT[SCRIPT.index("if ($AdminActions -contains $Action) {"):]
    guard = block.index("if ($Elevated -and -not (Test-Admin)) {")
    admin_part = block.index("try { Invoke-AdminPart $Action } finally { Remove-AdminTemp }")
    assert guard < admin_part < block.index("-Verb RunAs")
    refused = block[guard:admin_part]
    assert "throw" in refused and "Start-ScheduledTask" in refused  # the app that the check stopped starts again


def test_the_configurator_survives_a_log_rotation(monkeypatch, tmp_path):
    """A log file that goes away between two checks fails no request."""
    from alert_colors import tuner

    class Rotating(type(tmp_path)):
        def exists(self):
            return True

        def stat(self, *args, **kwargs):
            raise FileNotFoundError(str(self))

    gone = Rotating(tmp_path / "gone.log")
    monkeypatch.setattr(tuner, "user_log_path", lambda _name: None)
    t = tuner.Tuner.__new__(tuner.Tuner)
    t.app = type("App", (), {"raw": {"logging": {"file": "x.log"}}, "log_path": lambda self, _n: gone})()
    assert t._current_scene() is None


# ---- two failover rules: an offline success and a waived retry -------
def offline_chain(primary_results, *fallback_results):
    from alert_colors.sources.failover import FailoverSource, Member

    clock = {"t": 0.0}

    class Scripted:
        def __init__(self, name, results):
            self.name, self.results = name, list(results)

        def fetch(self):
            result = self.results.pop(0) if len(self.results) > 1 else self.results[0]
            if isinstance(result, Exception):
                raise result
            return result

    members = [Member(Scripted("primary", primary_results))]
    members += [Member(Scripted(f"fallback{i}", r)) for i, r in enumerate(fallback_results)]
    source = FailoverSource(members[0], members[1:], after_s=30, retry_s=10, retry_max_s=120,
                            offline_retry_s=3, monotonic=lambda: clock["t"])
    return source, clock


def fetch_at(source, clock, t):
    clock["t"] = t
    return source.fetch()


NO_DNS = __import__("alert_colors.http", fromlist=["NameResolutionError"]).NameResolutionError("no DNS")
PRIMARY_OK = __import__("alert_colors.domain", fromlist=["AlertStatus"]).AlertStatus(
    coverage=__import__("alert_colors.domain", fromlist=["Coverage"]).Coverage.NONE, kinds=frozenset(),
    source="primary")
FALLBACK_OK = __import__("alert_colors.domain", fromlist=["AlertStatus"]).AlertStatus(
    coverage=__import__("alert_colors.domain", fromlist=["Coverage"]).Coverage.NONE, kinds=frozenset(),
    source="fallback")


def test_after_an_outage_a_primary_success_ends_the_offline_state():
    """A success of the primary ends the offline state: the next round does not give the turn to the
    fallback that has no network."""
    source, clock = offline_chain([NO_DNS, PRIMARY_OK], [NO_DNS])
    with pytest.raises(OSError):
        fetch_at(source, clock, 31)  # all sources: no network
    assert fetch_at(source, clock, 34) is PRIMARY_OK  # the network is back
    assert fetch_at(source, clock, 37) is PRIMARY_OK  # the primary again, not the fallback's turn


def test_in_an_offline_round_a_server_answer_of_a_fallback_lets_the_round_go_on():
    import urllib.error

    server_error = urllib.error.HTTPError("https://example.invalid", 500, "Error", None, None)
    source, clock = offline_chain([NO_DNS], [NO_DNS, server_error], [NO_DNS, FALLBACK_OK])
    for t in (31, 34):
        with pytest.raises(OSError):
            fetch_at(source, clock, t)  # 31: all sources; 34: the primary has its turn
    assert fetch_at(source, clock, 37) is FALLBACK_OK  # fallback0 answers 500: fallback1 is tried too


# ---- a server answer gives the sources skipped for the turn their try --------------------------
def test_a_server_answer_of_the_picked_fallback_lets_the_primary_try():
    import urllib.error

    server_error = urllib.error.HTTPError("https://example.invalid", 500, "Error", None, None)
    source, clock = offline_chain([NO_DNS, NO_DNS, PRIMARY_OK], [NO_DNS, server_error])
    for t in (31, 34):  # 31: all sources, no network; 34: the primary has its turn
        with pytest.raises(OSError):
            fetch_at(source, clock, t)
    # 37: the fallback has its turn and its server answers 500: the network works, and
    # the primary, skipped for that turn, is tried in the same round.
    assert fetch_at(source, clock, 37) is PRIMARY_OK


# ---- the tuning file: a failed read and a replace while the app reads -------------------------
def test_after_a_failed_read_the_next_check_reads_the_file_again(monkeypatch, tmp_path):
    from alert_colors import tuning

    path = tmp_path / "tuning.json"
    path.write_text('{"display": {"brightness": 0.5}}', encoding="utf-8")
    clock = {"t": 0.0}
    watcher = tuning.TuningWatcher(path=path, base_scenes={}, backend_types=set(), check_interval_s=1.0,
                                   monotonic=lambda: clock["t"])
    real_read = tuning.read_file
    failures = [PermissionError(13, "the configurator replaces the file")]

    def read_once_failing(p):
        if failures:
            raise failures.pop()
        return real_read(p)

    monkeypatch.setattr(tuning, "read_file", read_once_failing)
    assert watcher.poll() is None  # not read now
    clock["t"] = 1.0
    applied = watcher.poll()
    assert applied is not None and applied.brightness == 0.5  # read at the next check, unchanged file


def test_the_configurator_saves_also_while_the_app_reads(monkeypatch, tmp_path):
    from alert_colors import tuning

    real_replace = tuning.os.replace
    failures = [PermissionError(13, "in use"), PermissionError(13, "in use")]

    def replace_after_two_failures(source, target):
        if failures:
            raise failures.pop()
        real_replace(source, target)

    monkeypatch.setattr(tuning.os, "replace", replace_after_two_failures)
    monkeypatch.setattr(tuning, "REPLACE_WAIT_S", 0.0)
    tuning.write_file(tmp_path / "tuning.json", {"display": {"brightness": 0.5}})
    assert tuning.read_file(tmp_path / "tuning.json") == {"display": {"brightness": 0.5}}


def test_in_an_offline_round_a_waived_fallback_that_then_answers_is_used():
    """When a server answers in an offline round, the sources skipped for the turn (the waived
    ones) get their try; a waived fallback that then succeeds is used, not reported as failed."""
    import urllib.error

    server_error = urllib.error.HTTPError("https://example.invalid", 500, "Error", None, None)
    source, clock = offline_chain([NO_DNS], [FALLBACK_OK], [server_error])
    source._offline = True  # an earlier round found no network
    source.primary.last_try = 10.0
    source.fallbacks[0].last_try = 10.0
    source.fallbacks[1].last_try = 0.0  # least recently tried: the turn is fallback1's
    # fallback1 answers 500 (the network is back), so fallback0, waived for the turn, is tried.
    assert fetch_at(source, clock, 40) is FALLBACK_OK
