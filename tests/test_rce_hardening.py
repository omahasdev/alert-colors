"""No code execution from outside data: the app runs with admin rights.

Data that a server or a normal user controls (API responses, the region list,
the token, the tuning file) must never run code or change settings that are
not its own. tests/test_security.py checks the network rules and the paths.
"""
import ast
import os
import sys
import socket
import tomllib
from pathlib import Path

import pytest

from alert_colors import http, regions

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "alert_colors"
CONFIG_TEXT = (ROOT / "config.toml").read_text(encoding="utf-8")

# Calls and modules that run code or commands. The app needs none of them.
FORBIDDEN_CALLS = {"eval", "exec", "compile", "__import__", "breakpoint"}
FORBIDDEN_MODULES = {"pickle", "marshal", "shelve", "subprocess", "pty", "code", "codeop", "runpy"}
FORBIDDEN_ATTRIBUTES = {("os", "system"), ("os", "popen"), ("os", "startfile"), ("os", "execv"),
                        ("os", "execve"), ("os", "spawnv"), ("os", "spawnve")}


def _sources():
    return sorted(SRC.rglob("*.py"))


@pytest.mark.parametrize("path", _sources(), ids=lambda p: str(p.relative_to(SRC)))
def test_the_app_has_no_code_execution_call(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in FORBIDDEN_CALLS:
            found.append(f"{node.func.id}() at line {node.lineno}")
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) \
                and (node.value.id, node.attr) in FORBIDDEN_ATTRIBUTES:
            found.append(f"{node.value.id}.{node.attr} at line {node.lineno}")
        if isinstance(node, ast.Import):
            found += [f"import {a.name} at line {node.lineno}" for a in node.names
                      if a.name.split(".")[0] in FORBIDDEN_MODULES]
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module \
                and node.module.split(".")[0] in FORBIDDEN_MODULES:
            found.append(f"from {node.module} import at line {node.lineno}")
    assert found == []


# ---- region names from a server never add lines to config.toml ------------------
HOSTILE_NAME = 'Місто"\n[logging]\nfile = "..\\\\evil.log"\n#\x7f'
FAKE_ID = "999999"  # not a real place
HOSTILE_ID = FAKE_ID + '"\nallow_private_network = true\nx = "'
HOSTILE_TREE = {"states": [{"regionId": FAKE_ID, "regionName": HOSTILE_NAME, "regionChildIds": []}]}


def test_a_region_id_that_is_not_digits_is_not_used():
    tree = {"states": [{"regionId": HOSTILE_ID, "regionName": "x", "regionChildIds": []},
                       {"regionId": "0", "regionName": "Тестовий регіон", "regionChildIds": []}]}
    assert [p.region_id for p in regions.places(tree)] == ["0"]


@pytest.mark.parametrize("region_id", [FAKE_ID, HOSTILE_ID])  # HOSTILE_ID: a config edited by hand
def test_a_hostile_region_name_changes_only_the_place_variables(region_id):
    [listed] = regions.places(HOSTILE_TREE)
    place = regions.Place(region_id, region_id, region_id, listed.name, listed.path, listed.oblast_name)
    text = regions.set_place(CONFIG_TEXT, place)
    new, old = tomllib.loads(text), tomllib.loads(CONFIG_TEXT)  # still valid TOML
    assert set(new["source"]["vars"]) == set(old["source"]["vars"])
    assert new["source"]["vars"]["region_id"] == place.region_id.replace("\n", " ")
    for table in set(old) | set(new):
        if table != "source":
            assert new.get(table) == old.get(table)
    assert {k: v for k, v in new["source"].items() if k != "vars"} == \
        {k: v for k, v in old["source"].items() if k != "vars"}


def test_a_normal_region_name_is_written_unchanged():
    tree = {"states": [{"regionId": "0", "regionName": "Тестовий регіон", "regionChildIds": []}]}
    [place] = regions.places(tree)
    text = regions.set_place(CONFIG_TEXT, place)
    assert 'region_id = "0"' in text
    assert "# Тестовий регіон" in text


# ---- a token with a line break never adds a request header -----------------------
def test_a_header_with_a_line_break_is_refused_before_any_connection(monkeypatch):
    # No DNS lookup and no address: the URL check passes, and the test blocks every connection.
    monkeypatch.setattr(http, "check_url", lambda *_args: None)

    def no_connection(*_args, **_kwargs):
        raise AssertionError("a connection was opened")

    monkeypatch.setattr(socket, "create_connection", no_connection)
    with pytest.raises(ValueError, match="header"):
        http.request_json("https://example.invalid/", headers={"X-Token": "t\r\nX-Evil: 1"}, timeout_s=1)


# ---- the Aura backend never loads a per-user COM class as admin --------------------
AURA_CLSID = "{00000000-0000-0000-0000-000000000001}"  # a made-up class id
HKLM, HKCU = "HKLM", "HKCU"


class FakeWinreg:
    HKEY_LOCAL_MACHINE, HKEY_CURRENT_USER = HKLM, HKCU

    def __init__(self, machine_values, user_keys):
        self.machine_values, self.user_keys = machine_values, set(user_keys)

    def QueryValue(self, root, path):  # noqa: N802  winreg names
        if root == HKLM and path in self.machine_values:
            return self.machine_values[path]
        raise FileNotFoundError(path)

    def OpenKey(self, root, path):  # noqa: N802
        if root == HKCU and path in self.user_keys:
            return path
        raise FileNotFoundError(path)

    def CloseKey(self, _key):  # noqa: N802
        pass


def fake_registry(monkeypatch, user_keys, installed=True):
    from alert_colors.lighting import aura_sdk

    machine = {rf"{aura_sdk.MACHINE_CLASSES}\{aura_sdk.PROG_ID}\CLSID": AURA_CLSID} if installed else {}
    monkeypatch.setitem(sys.modules, "winreg", FakeWinreg(machine, user_keys))
    return aura_sdk


def test_no_per_user_key_gives_no_override(monkeypatch):
    aura_sdk = fake_registry(monkeypatch, [])
    assert aura_sdk.per_user_overrides() == []


@pytest.mark.parametrize("key", ["aura.sdk.1", rf"CLSID\{AURA_CLSID}", rf"WOW6432Node\CLSID\{AURA_CLSID}"])
def test_a_per_user_prog_id_or_clsid_key_does_not_stop_aura(monkeypatch, key):
    """machine_instance() reads these from HKLM only: a user key changes nothing, so it
    must not let a process without admin rights turn the Aura LEDs off."""
    aura_sdk = fake_registry(monkeypatch, [rf"Software\Classes\{key}"])
    assert aura_sdk.per_user_overrides() == []


def test_without_the_sdk_nothing_is_checked(monkeypatch):
    aura_sdk = fake_registry(monkeypatch, [r"Software\Classes\aura.sdk.1"], installed=False)
    assert aura_sdk.per_user_overrides() == []


def test_the_backend_refuses_to_load_with_a_per_user_key(monkeypatch):
    from alert_colors.lighting import aura_sdk

    monkeypatch.setattr(aura_sdk, "per_user_overrides", lambda: [r"HKEY_CURRENT_USER\Software\Classes\x"])
    monkeypatch.setattr(aura_sdk, "_init_mta", lambda: pytest.fail("COM was started"))
    with pytest.raises(PermissionError, match="per-user COM key"):
        aura_sdk.AuraSdkBackend(include=["*"])._connect()


def test_the_dll_path_comes_from_hklm_only(monkeypatch, tmp_path):
    dll = tmp_path / "sdk.dll"
    dll.write_bytes(b"")
    aura_sdk = fake_registry(monkeypatch, [])
    sys.modules["winreg"].machine_values[rf"{aura_sdk.MACHINE_CLASSES}\CLSID\{AURA_CLSID}\InprocServer32"] = \
        f'"{dll}"'
    assert aura_sdk.machine_server() == (AURA_CLSID, str(dll))


@pytest.mark.parametrize("registered", ["sdk.dll", r"C:\no\such\folder\sdk.dll"])
def test_a_relative_or_missing_dll_path_is_refused(monkeypatch, registered):
    """A relative path would be searched for, also in folders that a user can write."""
    aura_sdk = fake_registry(monkeypatch, [])
    sys.modules["winreg"].machine_values[rf"{aura_sdk.MACHINE_CLASSES}\CLSID\{AURA_CLSID}\InprocServer32"] = \
        registered
    with pytest.raises(FileNotFoundError):
        aura_sdk.machine_server()


@pytest.mark.skipif(sys.platform != "win32", reason="real COM: Windows only")
def test_the_loader_makes_a_working_object_from_a_machine_dll():
    """Scripting.Dictionary (scrrun.dll) is on every Windows: no SDK and no device is necessary.
    Its own process: other tests put fake COM modules in sys.modules."""
    import subprocess

    code = ("import win32com.client\n"
            "from alert_colors.lighting import aura_sdk\n"
            "d = win32com.client.Dispatch(aura_sdk.machine_instance('Scripting.Dictionary'))\n"
            "d.Add('key', 1)\n"
            "print(d.Count)\n")
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False,
                            env={**os.environ, "PYTHONPATH": str(SRC.parent)})
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "1"


# ---- the registered DLL path never takes a folder from the environment --------------
def test_a_system_folder_variable_expands_from_windows_not_from_the_environment():
    from alert_colors.lighting import aura_sdk

    folders = {0x0026: r"C:\Machine Program Files"}
    path = aura_sdk.expand_machine_path(r'"%ProgramFiles%\Vendor\sdk.dll"', folders.__getitem__)
    assert path == r"C:\Machine Program Files\Vendor\sdk.dll"


@pytest.mark.parametrize("raw", [r"%TEMP%\sdk.dll", r"%USERPROFILE%\sdk.dll", r"%APPDATA%\x\sdk.dll"])
def test_any_other_variable_in_the_dll_path_is_refused(raw):
    from alert_colors.lighting import aura_sdk

    with pytest.raises(ValueError, match="not a system folder"):
        aura_sdk.expand_machine_path(raw, lambda _csidl: pytest.fail("no folder may be read"))


def run_python(code, env_extra=None):
    import subprocess

    env = {**os.environ, "PYTHONPATH": str(SRC.parent), **(env_extra or {})}
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False, env=env)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows folders")
def test_a_faked_program_files_variable_does_not_change_the_folder(tmp_path):
    result = run_python("from alert_colors.lighting import aura_sdk\n"
                        "print(aura_sdk.expand_machine_path('%ProgramFiles%'))\n",
                        {"ProgramFiles": str(tmp_path)})
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() != str(tmp_path)
    assert result.stdout.strip().lower().endswith("program files")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows DLL search")
def test_after_the_limit_a_dll_in_a_path_folder_is_not_loaded(tmp_path):
    """DLL planting: a DLL by name in a user-writable PATH folder must not load into the admin app."""
    import shutil

    first, second = "alert_colors_probe_a.dll", "alert_colors_probe_b.dll"
    for name in (first, second):
        shutil.copy(Path(os.environ["SystemRoot"]) / "System32" / "version.dll", tmp_path / name)
    # LoadLibraryW by name: the search order that a DLL such as the SDK uses (ctypes.WinDLL does not).
    code = (f'FIRST, SECOND = {first!r}, {second!r}' + chr(10) + chr(10).join([
        'import ctypes, os',
        'from ctypes import wintypes',
        'from alert_colors.lighting import aura_sdk',
        "k = ctypes.WinDLL('kernel32', use_last_error=True)",
        'k.LoadLibraryW.restype = wintypes.HMODULE',
        'k.LoadLibraryW.argtypes = [wintypes.LPCWSTR]',
        "print('before:', 'loaded' if k.LoadLibraryW(FIRST) else 'refused')",
        "aura_sdk.restrict_dll_search(k, os.environ['SystemRoot'])",
        "print('after:', 'loaded' if k.LoadLibraryW(SECOND) else 'refused')",
    ]))
    result = run_python(code, {"PATH": f"{tmp_path};{os.environ['PATH']}"})
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["before:", "loaded", "after:", "refused"]


# ---- a value from a server never changes a JMESPath expression -----------------------------
def test_a_crafted_name_cannot_leave_a_raw_string_of_a_preset():
    from alert_colors.sources import alerts_in_ua
    from alert_colors.template import expand_jmespath

    crafted = "X' || `true` || 'Y"  # a quote would end the raw string 'x' and add an expression
    with pytest.raises(ValueError, match="quote"):
        expand_jmespath(alerts_in_ua._IN_OBLAST, {"oblast_name": crafted, "oblast_uid": "0"})
    ordinary = expand_jmespath(alerts_in_ua._IN_OBLAST, {"oblast_name": "Тестова область", "oblast_uid": "0"})
    assert "'Тестова область'" in ordinary


def test_a_crafted_name_in_a_quoted_name_stays_one_key():
    import jmespath

    from alert_colors.template import expand_jmespath

    crafted = 'Z" || `true` || "Y'
    expression = jmespath.compile(expand_jmespath('states."${region}".alertnow', {"region": crafted}))
    assert expression.search({"states": {crafted: {"alertnow": False}}}) is False  # read as the key itself
    assert expression.search({"states": {"Z": {"alertnow": False}}}) is None  # never `true`


@pytest.mark.parametrize(("template", "value"), [
    ("[?contains(`${ids}`, regionId)]", '["1"]`] || `true'),  # a backtick ends the JSON literal
    ("[?id=='${id}']", "0\\"),  # a backslash would escape the closing quote
    ("alerts.${name}", "a || `true`"),  # outside a literal: an operator
])
def test_a_value_that_could_change_the_expression_is_refused(template, value):
    from alert_colors.template import expand_jmespath

    with pytest.raises(ValueError):
        expand_jmespath(template, {"ids": value, "id": value, "name": value})


def test_a_lookup_value_as_a_quoted_name_is_accepted():
    """A lookup value is JSON: a string arrives as "x", which JMESPath reads as a quoted name."""
    import json

    from alert_colors.template import expand_jmespath

    assert expand_jmespath("alerts.${first}", {"first": json.dumps('he"re')}) == 'alerts."he\\"re"'


def test_the_ubilling_preset_never_reads_clear_from_a_crafted_oblast_name():
    """End to end: the real preset, an active alert, and a crafted name that, if it could leave
    its quoted name, would make the expression `alertnow && false`: the "clear" state."""
    from alert_colors.domain import Coverage
    from alert_colors.sources import SOURCES

    crafted = 'X".alertnow && `false` && states."X'
    source = SOURCES.create("ubilling", {"vars": {"oblast_name": crafted}})
    payload = {"cachedat": "2026-01-01 00:00:00",
               "states": {"X": {"alertnow": True, "changed": "2026-01-01 00:00:00"}}}
    full = source.fields["full"].search(payload)  # the field that decides "alert" or "clear"
    assert full is not False  # with the crafted name as a real key: no value (None), never `false`
    try:
        status = source.parse(payload)
    except Exception:  # noqa: BLE001  the crafted key is not in the feed: no status, never "clear"
        return
    assert status.coverage is not Coverage.NONE


# ---- placeholders inside a JSON literal -------------------------------------------------------
def test_a_value_in_a_json_string_of_a_literal_stays_one_string():
    import jmespath

    from alert_colors.template import expand_jmespath

    crafted = '0", "5'  # would add the place 5 to the list if it could leave its JSON string
    expression = jmespath.compile(expand_jmespath('[?contains(`["${r}"]`, regionId)].a', {"r": crafted}))
    data = [{"regionId": "0", "a": 1}, {"regionId": "5", "a": 2}, {"regionId": crafted, "a": 3}]
    assert expression.search(data) == [3]  # only the item whose ID is the whole value


def test_a_value_in_a_json_object_of_a_literal_adds_no_key():
    import jmespath

    from alert_colors.template import expand_jmespath

    crafted = 'v", "k2": "w'
    literal = jmespath.compile(expand_jmespath('`{"k": "${r}"}`', {"r": crafted})).search({})
    assert literal == {"k": crafted}


@pytest.mark.parametrize(("template", "value"), [
    ('`["${r}"]`', "a`b"),  # a backtick ends the literal
    ('`[${r}]`', '"0"'),  # a part of a literal, not a whole JSON value
    ("`${r}`", "not json"),
])
def test_a_json_literal_refuses_what_could_change_it(template, value):
    from alert_colors.template import expand_jmespath

    with pytest.raises(ValueError):
        expand_jmespath(template, {"r": value})


def test_a_lookup_value_that_fills_a_json_literal_is_accepted():
    from alert_colors.template import expand_jmespath

    assert expand_jmespath("`${ids}`", {"ids": '["1", "2"]'}) == '`["1", "2"]`'
