"""The machine-only TLS trust, the proxy rule, and the preview limit."""
import ssl
import sys
import types
from datetime import UTC, datetime, timedelta

import pytest

from alert_colors import http
from alert_colors.daemon import Daemon, DaemonSettings
from alert_colors.policy import Policy
from alert_colors.tuning import PREVIEW_MAX_S, TuningError, parse

SERVER_AUTH, CODE_SIGNING = b"1.3.6.1.5.5.7.3.1", b"1.3.6.1.5.5.7.3.3"
NOW = datetime(2026, 1, 1, tzinfo=UTC)


class FakeError(Exception):
    def __init__(self, winerror):
        super().__init__(winerror)
        self.winerror = winerror


class Certificate:
    def __init__(self, der, usages, self_signed=False, properties=None):
        self.CertEncoded, self.usages, self.properties = der, usages, properties or {}
        self.Subject = b"subject-" + der
        self.Issuer = self.Subject if self_signed else b"issuer"

    def CertGetEnhancedKeyUsage(self, _flags):  # noqa: N802  win32crypt names
        if isinstance(self.usages, Exception):
            raise self.usages
        return self.usages


def fake_crypto(monkeypatch, stores):
    opened = []
    monkeypatch.setattr(http, "_certificate_property", lambda c, prop_id: c.properties.get(prop_id))

    def open_store(_provider, _encoding, _crypt, flags, name):
        opened.append((name, flags))
        return types.SimpleNamespace(CertEnumCertificatesInStore=lambda: stores.get(name, []))

    monkeypatch.setitem(sys.modules, "win32crypt", types.SimpleNamespace(CertOpenStore=open_store))
    monkeypatch.setitem(sys.modules, "pywintypes", types.SimpleNamespace(error=FakeError))
    return opened


def test_only_machine_stores_and_server_auth_certificates_are_used(monkeypatch):
    stores = {
        "Disallowed": [Certificate(b"G", (SERVER_AUTH,))],  # distrusted by an admin or Microsoft
        "ROOT": [Certificate(b"A", (SERVER_AUTH,)), Certificate(b"B", (CODE_SIGNING,)),
                 Certificate(b"G", (SERVER_AUTH,)),  # also still in ROOT: not trusted
                 Certificate(b"C", FakeError(http.CRYPT_E_NOT_FOUND))],  # no limit: all usages
        "AuthRoot": [Certificate(b"D", FakeError(-1))],  # another error: not used
        "CA": [Certificate(b"E", (SERVER_AUTH, CODE_SIGNING)),
               Certificate(b"H", (SERVER_AUTH,), properties={http.CERT_DISALLOWED_FILETIME_PROP_ID: bytes(8)}),
               Certificate(b"F", (SERVER_AUTH,), self_signed=True)],  # a root in the store of intermediates
    }
    opened = fake_crypto(monkeypatch, stores)
    assert http.machine_root_certificates() == [b"A", b"C", b"E"]  # F is not trusted
    assert [name for name, _flags in opened] == [http.DISALLOWED_STORE, *http.MACHINE_ROOT_STORES]
    machine_only = http.CERT_SYSTEM_STORE_LOCAL_MACHINE | http.CERT_STORE_READONLY_FLAG
    assert all(flags == machine_only for _name, flags in opened)  # never the stores of the user


def test_no_machine_root_is_an_error_not_the_user_stores(monkeypatch):
    if sys.platform != "win32":
        pytest.skip("the machine stores exist on Windows only")
    monkeypatch.setattr(http, "machine_root_certificates", lambda: [])
    with pytest.raises(ssl.SSLError, match="no root certificate"):
        http.tls_context()


def test_the_tls_context_allows_no_partial_chain():
    assert not http.tls_context().verify_flags & getattr(ssl, "VERIFY_X509_PARTIAL_CHAIN", 0)
    assert http.tls_context().verify_mode == ssl.CERT_REQUIRED


@pytest.mark.parametrize(("url", "policy", "direct"), [
    ("https://alerts.example/api", http.REMOTE, False),  # a proxy sees only TLS
    ("http://alerts.example/api", http.NetworkPolicy(allow_http=True), True),
    ("http://127.0.0.1:54235/razer/chromasdk", http.LOOPBACK, True),
    ("https://127.0.0.1:54236/local", http.LOOPBACK, True),  # loopback over https: never a proxy
])
def test_only_https_to_a_remote_server_may_use_a_proxy(url, policy, direct):
    assert (http._opener_for(url, policy) is http._DIRECT_OPENER) == direct


def test_a_proxy_of_the_user_reaches_only_the_normal_opener():
    """With HTTP_PROXY set (a user can set it without admin rights), the direct opener has no proxy."""
    import os
    import subprocess
    from pathlib import Path

    code = "\n".join([
        "import urllib.request",
        "from alert_colors import http",
        "def proxies(opener):",
        "    return [h.proxies for h in opener.handlers if isinstance(h, urllib.request.ProxyHandler) and h.proxies]",
        "print(len(proxies(http._OPENER)), len(proxies(http._DIRECT_OPENER)))",
    ])
    src = Path(__file__).resolve().parents[1] / "src"
    env = {**os.environ, "PYTHONPATH": str(src), "HTTP_PROXY": "http://127.0.0.1:9"}
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False, env=env)
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["1", "0"]


# ---- the preview of the tuning file ends ----------------------------------------------
BASE_SCENES = {"clear": {"effect": "solid", "color": "#00FF00"},
               "unknown": {"effect": "solid", "color": "#090909"}}


@pytest.mark.parametrize("until", [float("inf"), float("nan"), float("-inf")])
def test_a_preview_time_that_is_not_finite_is_refused(until):
    with pytest.raises(TuningError, match="finite"):
        parse({"preview": {"scene": "clear", "until": until}}, BASE_SCENES, set())


def test_a_preview_ends_after_the_limit_whatever_the_file_says():
    tuning = parse({"preview": {"scene": "clear", "until": NOW.timestamp() + 10 ** 9}}, BASE_SCENES, set())
    daemon = Daemon(source=types.SimpleNamespace(name="fake"), scenes={}, outputs=[],
                    policy=Policy.from_config([{"state": "unknown", "scene": "unknown"}, {"scene": "clear"}],
                                              {"clear", "unknown"}),
                    settings=DaemonSettings(poll_interval_s=1, stale_after_s=100, frame_interval_s=0.1),
                    wall_clock=lambda: NOW)
    daemon.apply_tuning(tuning)
    assert daemon._preview_until == NOW.timestamp() + PREVIEW_MAX_S


def test_every_https_request_uses_the_machine_context(monkeypatch):
    """Both openers use the machine context: the default context of Python
    also trusts the certificate stores of the user."""
    seen = []
    machine = object()
    monkeypatch.setattr(http._LazyTLSHandler, "_shared", machine)

    def do_open(self, _connection, _req, **kwargs):
        seen.append(kwargs.get("context"))
        raise OSError("stop here")

    monkeypatch.setattr(http._LazyTLSHandler, "do_open", do_open)
    for opener in (http._OPENER, http._DIRECT_OPENER):
        [handler] = [h for h in opener.handlers if isinstance(h, http._LazyTLSHandler)]
        with pytest.raises(OSError):
            handler.https_open(types.SimpleNamespace())
    assert seen == [machine, machine]


def test_one_bad_certificate_does_not_stop_https(monkeypatch, caplog):
    """A certificate that OpenSSL refuses is skipped, not fatal."""
    if sys.platform != "win32":
        pytest.skip("the machine stores exist on Windows only")
    good = http.machine_root_certificates()[0]
    monkeypatch.setattr(http, "machine_root_certificates", lambda: [good, b"not a certificate", good])
    import logging

    with caplog.at_level(logging.WARNING, logger="alert_colors.http"):
        context = http.tls_context()
    assert context.cert_store_stats()["x509"] == 1  # the good one, once (get_ca_certs lists only CA-marked)
    assert "1 machine certificate(s) not usable" in caplog.text
    assert not context.verify_flags & getattr(ssl, "VERIFY_X509_PARTIAL_CHAIN", 0)
    assert context.verify_flags & getattr(ssl, "VERIFY_X509_STRICT", 0)
    assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED


def test_a_tuning_file_that_grows_after_its_size_check_is_still_refused(monkeypatch):
    """The file is read with one open and a limited read, not after a separate size check."""
    import io

    from alert_colors import tuning

    monkeypatch.setattr(tuning, "_open_plain", lambda path: path.open("rb"))  # the fake file object

    class Growing:
        def stat(self):
            return types.SimpleNamespace(st_size=2)  # small at a size check

        def open(self, _mode):
            return io.BytesIO(b"[" + b" " * tuning.MAX_FILE_BYTES + b"]")  # large at the read

        def __str__(self):
            return "tuning.json"

    with pytest.raises(TuningError, match="larger than"):
        tuning.read_file(Growing())


def test_the_tuning_file_is_read_with_a_size_limit(monkeypatch):
    """The admin app never reads more of the user-writable file than the limit and one byte."""
    import io

    from alert_colors import tuning

    monkeypatch.setattr(tuning, "_open_plain", lambda path: path.open("rb"))  # the fake file object

    sizes = []

    class Recording(io.BytesIO):
        def read(self, size=-1):
            sizes.append(size)
            return super().read(size)

    class File:
        def open(self, _mode):
            return Recording(b"{}")

        def __str__(self):
            return "tuning.json"

    assert tuning.read_file(File()) == {}
    assert sizes == [tuning.MAX_FILE_BYTES + 1]


# ---- the distrust marks of Windows on a root --------------------------------------------------
def filetime(when):
    ticks = (when - http.FILETIME_EPOCH) // timedelta(microseconds=1) * http.FILETIME_TICKS_PER_MICROSECOND
    return ticks.to_bytes(http.FILETIME_BYTES, "little")


def usages(*oids_der):
    body = b"".join(bytes([http.DER_OID, len(oid)]) + oid for oid in oids_der)
    return bytes([http.DER_SEQUENCE, len(body)]) + body


TODAY = datetime(2026, 1, 1, tzinfo=UTC)
PAST, FUTURE = TODAY - timedelta(days=1), TODAY + timedelta(days=1)
OCSP_SIGNING_DER = bytes.fromhex("2b06010505070309")


@pytest.mark.parametrize(("properties", "distrusted"), [
    ({}, False),  # no mark
    ({104: filetime(PAST)}, True),  # a date that passed, for all usages
    ({104: filetime(FUTURE)}, False),  # Windows still trusts it today
    ({104: filetime(PAST), 122: usages(OCSP_SIGNING_DER)}, False),  # the date is for another usage
    ({104: filetime(PAST), 122: usages(OCSP_SIGNING_DER, http.SERVER_AUTH_OID_DER)}, True),
    ({122: usages(http.SERVER_AUTH_OID_DER)}, True),  # a usage list alone: not trusted for it
    ({122: usages(OCSP_SIGNING_DER)}, False),
    ({104: filetime(PAST), 122: bytes.fromhex("300506")}, True),  # a list that does not parse: all usages
    ({104: bytes(1)}, True),  # a date that does not parse
    ({104: filetime(PAST), 122: bytes.fromhex("3003020101")}, True),  # an item that is not an OID: all usages
    ({104: bytes.fromhex("ff" * 8)}, False),  # a date after year 9999: not yet, and no error
    ({126: filetime(PAST)}, True),  # "not before" a date that passed: new certificates are not trusted
    ({126: filetime(FUTURE)}, False),
    ({126: filetime(PAST), 127: usages(OCSP_SIGNING_DER)}, False),  # for another usage
    ({126: filetime(PAST), 127: usages(http.SERVER_AUTH_OID_DER)}, True),
    ({127: usages(http.SERVER_AUTH_OID_DER)}, False),  # a "not before" mark needs its date
])
def test_a_root_that_windows_distrusts_for_new_server_certificates_is_skipped(properties, distrusted):
    assert http.windows_distrusts(properties.get, TODAY) is distrusted


@pytest.mark.skipif(sys.platform != "win32", reason="the machine stores")
def test_the_real_roots_are_read_with_their_distrust_marks():
    """A read-only read of this PC's stores: the marks can be read, and roots stay."""
    assert http.machine_root_certificates()


# ---- the admin app reads only a plain tuning file -----------------------------------------
@pytest.mark.skipif(sys.platform != "win32", reason="Windows links")
def test_the_app_never_reads_the_target_of_a_link_at_the_tuning_file(tmp_path):
    from alert_colors import tuning

    target = tmp_path / "other.json"
    target.write_text('{"display": {"brightness": 0.5}}', encoding="utf-8")
    link = tmp_path / "tuning.json"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("this account cannot make symbolic links")
    with pytest.raises(OSError):
        tuning.read_file(link)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows hard links")
def test_the_app_never_reads_a_tuning_file_with_a_second_name(tmp_path):
    import os

    from alert_colors import tuning

    path = tmp_path / "tuning.json"
    path.write_text("{}", encoding="utf-8")
    os.link(path, tmp_path / "second-name.json")
    with pytest.raises(OSError):
        tuning.read_file(path)



@pytest.mark.skipif(sys.platform != "win32", reason="Windows links")
def test_a_tuning_file_that_is_a_link_gives_one_warning(tmp_path, caplog):
    import logging

    from alert_colors import tuning

    target = tmp_path / "other.json"
    target.write_text("{}", encoding="utf-8")
    link = tmp_path / "tuning.json"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("this account cannot make symbolic links")
    clock = {"t": 0.0}
    watcher = tuning.TuningWatcher(path=link, base_scenes={}, backend_types=set(), check_interval_s=1.0,
                                   monotonic=lambda: clock["t"])
    with caplog.at_level(logging.WARNING, logger=tuning.__name__):
        for t in range(3):
            clock["t"] = float(t)
            assert watcher.poll() is None
    warnings = [r for r in caplog.records if "not a plain file" in r.getMessage()]
    assert len(warnings) == 1


def test_a_tuning_path_that_cannot_be_checked_gives_one_warning_not_one_each_check(tmp_path, caplog):
    """Links that point at each other make stat() fail: the log gets one line, not a traceback each time."""
    import logging
    from pathlib import Path

    from alert_colors import tuning

    class Unresolvable(type(Path())):
        def stat(self, **kwargs):
            raise OSError(22, "The name of the file cannot be resolved by the system")

    clock = {"t": 0.0}
    watcher = tuning.TuningWatcher(path=Unresolvable(tmp_path / "tuning.json"), base_scenes={}, backend_types=set(),
                                   check_interval_s=1.0, monotonic=lambda: clock["t"])
    with caplog.at_level(logging.WARNING, logger=tuning.__name__):
        for t in range(3):
            clock["t"] = float(t)
            assert watcher.poll() is None
    assert len([r for r in caplog.records if "cannot be checked" in r.getMessage()]) == 1


def test_the_same_check_error_after_a_recovery_is_warned_again(tmp_path, caplog):
    import logging
    from pathlib import Path

    from alert_colors import tuning

    state = {"broken": True}

    class Flaky(type(Path())):
        def stat(self, **kwargs):
            if state["broken"]:
                raise OSError(22, "The name of the file cannot be resolved by the system")
            raise FileNotFoundError(2, "no file")

    clock = {"t": 0.0}
    watcher = tuning.TuningWatcher(path=Flaky(tmp_path / "tuning.json"), base_scenes={}, backend_types=set(),
                                   check_interval_s=1.0, monotonic=lambda: clock["t"])
    with caplog.at_level(logging.WARNING, logger=tuning.__name__):
        for t, broken in enumerate([True, False, True]):
            state["broken"], clock["t"] = broken, float(t)
            watcher.poll()
    assert len([r for r in caplog.records if "cannot be checked" in r.getMessage()]) == 2


# ---- the app with admin rights reads only a tuning folder of Administrators ----------------
@pytest.mark.skipif(sys.platform != "win32", reason="Windows owners")
@pytest.mark.parametrize(("owner", "readable"), [("S-1-5-32-544", True), ("S-1-5-18", True),
                                                 ("S-1-5-21-1-1-1-1002", False)])
def test_an_admin_app_reads_the_tuning_file_only_from_a_folder_of_administrators(monkeypatch, tmp_path,
                                                                                owner, readable):
    """A folder of another account (for example after a failed setup step) gives no look."""
    from alert_colors import tuning

    monkeypatch.setattr(tuning, "_is_admin", lambda: True)
    monkeypatch.setattr(tuning, "_owner_sid", lambda path: owner)
    path = tmp_path / "tuning.json"
    path.write_text("{}", encoding="utf-8")
    if readable:
        assert tuning.read_file(path) == {}
    else:
        with pytest.raises(tuning.NotPlainFileError, match="does not belong to Administrators"):
            tuning.read_file(path)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows links")
def test_an_admin_app_never_reads_a_tuning_folder_that_is_a_link(monkeypatch, tmp_path):
    import subprocess

    from alert_colors import tuning

    target = tmp_path / "elsewhere"
    target.mkdir()
    (target / "tuning.json").write_text("{}", encoding="utf-8")
    junction = tmp_path / "AlertColors"
    made = subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(target)], capture_output=True, text=True)
    assert made.returncode == 0, made.stdout + made.stderr
    monkeypatch.setattr(tuning, "_is_admin", lambda: True)
    monkeypatch.setattr(tuning, "_owner_sid", lambda path: "S-1-5-32-544")
    with pytest.raises(tuning.NotPlainFileError, match="is a link"):
        tuning.read_file(junction / "tuning.json")


def test_a_process_without_admin_rights_reads_a_tuning_folder_of_the_user(monkeypatch, tmp_path):
    """The configurator and a source run: the user can write that folder anyway."""
    from alert_colors import tuning

    monkeypatch.setattr(tuning, "_is_admin", lambda: False)
    monkeypatch.setattr(tuning, "_owner_sid", lambda path: pytest.fail("no owner check without admin rights"))
    path = tmp_path / "tuning.json"
    path.write_text("{}", encoding="utf-8")
    assert tuning.read_file(path) == {}


@pytest.mark.skipif(sys.platform != "win32", reason="Windows owners")
def test_the_owner_of_a_real_folder_is_read_not_its_group():
    """On NTFS (the system drive): a folder that this account makes belongs to this account."""
    import os
    import tempfile
    from pathlib import Path

    import win32api
    import win32con
    import win32security

    from alert_colors import tuning

    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    me = win32security.ConvertSidToStringSid(win32security.GetTokenInformation(token, win32security.TokenUser)[0])
    with tempfile.TemporaryDirectory(dir=os.environ["LOCALAPPDATA"]) as folder:
        assert tuning._owner_sid(Path(folder)) == me


@pytest.mark.skipif(sys.platform != "win32", reason="Windows files")
def test_a_missing_tuning_folder_is_not_read_now_not_a_link(monkeypatch, tmp_path):
    from alert_colors import tuning

    monkeypatch.setattr(tuning, "_is_admin", lambda: True)
    with pytest.raises(OSError) as raised:
        tuning._check_folder(tmp_path / "missing")
    assert not isinstance(raised.value, tuning.NotTrustedFolderError)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows owners")
def test_an_owner_that_cannot_be_read_is_an_os_error(monkeypatch, tmp_path):
    """The watcher then reads the file again at the next check, as for any other OSError."""
    import pywintypes
    import win32security

    from alert_colors import tuning

    def denied(path, info):
        raise pywintypes.error(5, "GetFileSecurity", "Access is denied.")

    monkeypatch.setattr(win32security, "GetFileSecurity", denied)
    with pytest.raises(OSError, match="cannot read the owner"):
        tuning._owner_sid(tmp_path)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows owners")
def test_a_tuning_folder_of_trusted_installer_is_read(monkeypatch, tmp_path):
    """Program Files and other machine folders belong to TrustedInstaller."""
    from alert_colors import tuning

    monkeypatch.setattr(tuning, "_is_admin", lambda: True)
    monkeypatch.setattr(tuning, "_owner_sid",
                        lambda path: "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464")
    path = tmp_path / "tuning.json"
    path.write_text("{}", encoding="utf-8")
    assert tuning.read_file(path) == {}


@pytest.mark.skipif(sys.platform != "win32", reason="Windows owners")
def test_a_folder_error_tells_what_to_do_and_not_to_save_again(monkeypatch, tmp_path, caplog):
    import logging

    from alert_colors import tuning

    monkeypatch.setattr(tuning, "_is_admin", lambda: True)
    monkeypatch.setattr(tuning, "_owner_sid", lambda path: "S-1-5-21-1-1-1-1002")
    path = tmp_path / "tuning.json"
    path.write_text("{}", encoding="utf-8")
    watcher = tuning.TuningWatcher(path=path, base_scenes={}, backend_types=set(), check_interval_s=1.0,
                                   monotonic=lambda: 0.0)
    with caplog.at_level(logging.WARNING, logger=tuning.__name__):
        assert watcher.poll() is None
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "make Administrators its owner" in text and "Save the tuning again" not in text


@pytest.mark.skipif(sys.platform != "win32", reason="Windows owners")
@pytest.mark.parametrize(("file_owner", "readable"), [("admins", True), ("me", True), ("other", False),
                                                     ("users", False)])
def test_an_admin_app_reads_only_a_tuning_file_of_administrators_or_of_its_own_account(
        monkeypatch, tmp_path, file_owner, readable):
    """Before the setup locked an old folder, another account could add a file to it."""
    from alert_colors import tuning

    path = tmp_path / "tuning.json"
    path.write_text("{}", encoding="utf-8")
    owners = {"admins": "S-1-5-32-544", "me": "S-1-5-21-1-1-1-1001", "other": "S-1-5-21-1-1-1-1002",
              "users": "S-1-5-32-545"}  # a group owner is not the account of the app
    monkeypatch.setattr(tuning, "_is_admin", lambda: True)
    monkeypatch.setattr(tuning, "_own_sid", lambda: owners["me"])
    monkeypatch.setattr(tuning, "_owner_sid",
                        lambda p: owners[file_owner] if p == path else "S-1-5-32-544")  # the folder: Administrators
    if readable:
        assert tuning.read_file(path) == {}
    else:
        with pytest.raises(tuning.NotTrustedFolderError, match="belongs to another account"):
            tuning.read_file(path)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows tokens")
def test_the_own_account_of_the_app_is_the_account_of_this_process():
    import os
    import tempfile
    from pathlib import Path

    from alert_colors import tuning

    with tempfile.TemporaryDirectory(dir=os.environ["LOCALAPPDATA"]) as folder:
        assert tuning._own_sid() == tuning._owner_sid(Path(folder))  # a new folder belongs to its maker


# ---- the rights of the tuning folder, its file and its parent (admin app) ------------------
@pytest.mark.skipif(sys.platform != "win32", reason="Windows rights")
@pytest.mark.parametrize(("grant_on", "right", "refused"), [
    (None, None, False),  # only this account, SYSTEM and Administrators: read
    ("folder", "(OI)(CI)M", True),  # inherited write for all, as on a new folder of another drive
    ("folder", "(WD,AD)", True),  # on the folder only: another account can add its own tuning file
    ("folder", "(CI)(IO)(M)", False),  # a rule for new subfolders only: not for this folder or its file
    ("folder", "(WDAC)", True),  # another account can change the rights
    ("folder", "(WO)", True),  # another account can take the folder
    ("parent", "(WDAC)", True),  # another account can give itself the right to rename
    ("folder", "own:(WD)", False),  # the account of the app may add files, as the setup gives it
    ("file", "own:M", False),  # and change the file: the configurator writes it
    ("folder", "own:(OI)(CI)M", True),  # but not delete or rename the folder: a process of the user could swap it
    ("folder", "(D)", True),  # another account can delete or rename the folder
    ("folder", "deny:(W)", False),  # a denial gives nobody a right
    ("file", "M", True),  # another account can change the content
    ("parent", "(DC)", True),  # another account can rename the folder through its parent
    ("parent", "(WD)", False),  # adding entries to the parent changes nothing here
])
def test_an_admin_app_refuses_a_tuning_folder_or_file_that_other_accounts_can_change(
        monkeypatch, grant_on, right, refused):
    """On NTFS (the system drive), with real access lists; the owners are faked as trusted."""
    import os
    import subprocess
    import tempfile
    from pathlib import Path

    from alert_colors import tuning

    with tempfile.TemporaryDirectory(dir=os.environ["LOCALAPPDATA"]) as parent:
        folder = Path(parent) / "AlertColors"
        folder.mkdir()
        path = folder / "tuning.json"
        path.write_text("{}", encoding="utf-8")
        targets = {"folder": folder, "file": path, "parent": Path(parent)}
        if grant_on:
            account = tuning._own_sid() if right.startswith("own:") else "S-1-1-0"
            action = "/deny" if right.startswith("deny:") else "/grant"
            rule = right.removeprefix("own:").removeprefix("deny:")
            subprocess.run(["icacls", str(targets[grant_on]), action, f"*{account}:{rule}"],
                           capture_output=True, check=True)
        monkeypatch.setattr(tuning, "_is_admin", lambda: True)
        monkeypatch.setattr(tuning, "_owner_sid", lambda p: "S-1-5-32-544")
        try:
            if refused:
                with pytest.raises(tuning.NotTrustedFolderError, match="other accounts can"):
                    tuning.read_file(path)
            else:
                assert tuning.read_file(path) == {}
        finally:
            if grant_on:
                subprocess.run(["icacls", str(targets[grant_on]), "/remove:g", "*S-1-1-0"], capture_output=True)
                subprocess.run(["icacls", str(targets[grant_on]), "/remove:d", "*S-1-1-0"], capture_output=True)
                subprocess.run(["icacls", str(targets[grant_on]), "/remove:g", f"*{tuning._own_sid()}"],
                               capture_output=True)



@pytest.mark.skipif(sys.platform != "win32", reason="Windows rights")
def test_no_access_list_and_unusual_rules_count_as_other_writers(monkeypatch, tmp_path):
    """No access list gives everybody every right; a rule that pywin32 cannot read could too."""
    import win32security

    from alert_colors import tuning

    class Security:
        def __init__(self, dacl):
            self.dacl = dacl

        def GetSecurityDescriptorDacl(self):  # noqa: N802
            return self.dacl

    class Unreadable:
        def GetAceCount(self):  # noqa: N802
            return 1

        def GetAce(self, index):  # noqa: N802
            raise NotImplementedError("a conditional rule")

    for dacl, text in ((None, "no access list"), (Unreadable(), "unusual access rule")):
        monkeypatch.setattr(win32security, "GetFileSecurity", lambda path, info, d=dacl: Security(d))
        others = tuning._other_writers(tmp_path, tuning.CHANGE)
        assert others and text in others[0]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows owners")
def test_a_tuning_folder_in_a_parent_of_another_account_is_refused(monkeypatch, tmp_path):
    """The owner of the parent can change its rights and rename the tuning folder."""
    from alert_colors import tuning

    folder = tmp_path / "AlertColors"
    folder.mkdir()
    (folder / "tuning.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(tuning, "_is_admin", lambda: True)
    monkeypatch.setattr(tuning, "_other_writers", lambda path, rights, own_rights=0: [])
    monkeypatch.setattr(tuning, "_own_sid", lambda: "S-1-5-21-1-1-1-1001")
    for parent_owner in ("S-1-5-21-1-1-1-1002", "S-1-5-21-1-1-1-1001"):  # another account, and the app's own
        monkeypatch.setattr(tuning, "_owner_sid", lambda p, o=parent_owner: o if p == tmp_path else "S-1-5-32-544")
        with pytest.raises(tuning.NotTrustedFolderError, match="belongs to another account"):
            tuning.read_file(folder / "tuning.json")


def test_a_preview_never_lasts_longer_than_two_minutes():
    """A preview must not hide a real alert for long: the limit itself is part of the design."""
    from alert_colors import tuning

    assert tuning.PREVIEW_MAX_S == 120.0


@pytest.mark.skipif(sys.platform != "win32", reason="the machine certificate stores")
def test_a_certificate_distrust_property_is_read_as_its_real_bytes():
    """machine_root_certificates relies on these bytes to drop a distrusted root; a read that
    returns nothing would silently keep every root."""
    import win32crypt

    from alert_colors import http

    store = win32crypt.CertOpenStore(http.CERT_STORE_PROV_SYSTEM, 0, None,
                                     http.CERT_SYSTEM_STORE_LOCAL_MACHINE | http.CERT_STORE_READONLY_FLAG, "ROOT")
    for certificate in store.CertEnumCertificatesInStore():
        if http.CERT_DISALLOWED_FILETIME_PROP_ID in certificate.CertEnumCertificateContextProperties():
            value = http._certificate_property(certificate, http.CERT_DISALLOWED_FILETIME_PROP_ID)
            assert value is not None and len(value) == http.FILETIME_BYTES
            return
    pytest.skip("no machine root carries a distrust-date mark on this PC")
