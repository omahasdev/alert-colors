"""The admin app ignores the OpenSSL variables of the user and limits its DLL search."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from alert_colors import hardening

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
# An OpenSSL config that a test can see: it allows only one cipher suite.
LIMITING_CONFIG = """openssl_conf = conf
[conf]
ssl_conf = ssl
[ssl]
system_default = sys
[sys]
CipherString = AES128-SHA
Ciphersuites = TLS_AES_128_GCM_SHA256
"""
COUNT_CIPHERS = "import ssl; print(len(ssl.create_default_context().get_ciphers()))"


def run(code, env_extra):
    env = {**os.environ, "PYTHONPATH": str(SRC), **env_extra}
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False, env=env)


def test_every_unsafe_variable_is_removed_in_any_case():
    environ = {"OPENSSL_CONF": "a", "ssl_cert_file": "b", "SSLKEYLOGFILE": "c", "PATH": "p", "HTTPS_PROXY": "x"}
    removed = hardening.remove_unsafe_variables(environ)
    assert sorted(removed) == ["OPENSSL_CONF", "SSLKEYLOGFILE", "ssl_cert_file"]
    assert environ == {"PATH": "p", "HTTPS_PROXY": "x"}  # a proxy sees only TLS: it stays usable


def test_a_user_openssl_config_has_no_effect_after_hardening(tmp_path):
    config = tmp_path / "openssl.cnf"
    config.write_text(LIMITING_CONFIG, encoding="utf-8")
    env = {"OPENSSL_CONF": str(config)}
    plain = run(COUNT_CIPHERS, {})
    hostile = run(COUNT_CIPHERS, env)
    hardened = run("from alert_colors import hardening; hardening.apply()\n" + COUNT_CIPHERS, env)
    for result in (plain, hostile, hardened):
        assert result.returncode == 0, result.stderr
    assert hostile.stdout != plain.stdout, "the test config must change something, else the test proves nothing"
    assert hardened.stdout == plain.stdout


@pytest.mark.parametrize("entry", ["packaging/entry.py", "src/alert_colors/__main__.py"])
def test_each_entry_hardens_first(entry):
    """A run test alone cannot see a missing apply() call,
    so the syntax tree is checked: hardening is the first thing that runs."""
    import ast

    tree = ast.parse((ROOT / entry).read_text(encoding="utf-8"))
    body = [node for node in tree.body
            if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant))  # the docstring
            and not (isinstance(node, ast.Import) and [a.name for a in node.names] == ["sys"])]
    first, second = body[0], body[1]
    assert isinstance(first, ast.ImportFrom) and first.module in ("alert_colors", None)
    assert [a.name for a in first.names] == ["hardening"]
    assert ast.unparse(second) == "hardening.apply()"


def test_the_module_entry_runs_with_a_hostile_openssl_config(tmp_path):
    config = tmp_path / "openssl.cnf"
    config.write_text(LIMITING_CONFIG, encoding="utf-8")
    result = subprocess.run([sys.executable, "-m", "alert_colors", "--help"], capture_output=True, text=True,
                            check=False, env={**os.environ, "PYTHONPATH": str(SRC), "OPENSSL_CONF": str(config)})
    assert result.returncode == 0, result.stderr
    assert "usage" in result.stdout


@pytest.mark.parametrize("module", ["ssl", "_ssl", "_hashlib"])
def test_apply_refuses_to_run_after_ssl(module):
    result = run(f"import {module}\nfrom alert_colors import hardening\nhardening.apply()", {})
    assert result.returncode != 0
    assert "before ssl loads" in result.stderr


@pytest.mark.skipif(sys.platform != "win32", reason="Windows DLL search")
def test_after_hardening_a_dll_in_a_path_folder_is_not_loaded(tmp_path):
    import shutil

    for name in ("alert_colors_probe_c.dll", "alert_colors_probe_d.dll"):
        shutil.copy(Path(os.environ["SystemRoot"]) / "System32" / "version.dll", tmp_path / name)
    code = "\n".join([
        "import ctypes",
        "from ctypes import wintypes",
        "k = ctypes.WinDLL('kernel32', use_last_error=True)",
        "k.LoadLibraryW.restype = wintypes.HMODULE",
        "k.LoadLibraryW.argtypes = [wintypes.LPCWSTR]",
        "print('before:', 'loaded' if k.LoadLibraryW('alert_colors_probe_c.dll') else 'refused')",
        "from alert_colors import hardening",
        "hardening.restrict_dll_search()",
        "print('after:', 'loaded' if k.LoadLibraryW('alert_colors_probe_d.dll') else 'refused')",
    ])
    result = run(code, {"PATH": f"{tmp_path};{os.environ['PATH']}"})
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["before:", "loaded", "after:", "refused"]


def test_the_windows_folder_variables_come_from_windows():
    from alert_colors import hardening

    environ = {"systemroot": r"C:\Users\x\fake", "WINDIR": r"C:\Users\x\fake", "SystemDrive": "X:", "PATH": "p"}
    hardening.restore_windows_variables(r"C:\Windows", environ)
    assert environ == {"SystemRoot": r"C:\Windows", "windir": r"C:\Windows", "SystemDrive": "C:", "PATH": "p"}


@pytest.mark.skipif(sys.platform != "win32", reason="the Windows folder")
def test_apply_sets_the_windows_folder_from_windows():
    fake = str(SRC)  # any folder that is not the Windows folder
    env = {name: value for name, value in os.environ.items() if name.upper() not in ("SYSTEMROOT", "WINDIR")}
    env.update({"PYTHONPATH": str(SRC), "SystemRoot": fake, "windir": fake})
    code = ("import os\nfrom alert_colors import hardening\nhardening.apply()\n"
            "import socket\nsocket.getaddrinfo('localhost', 80)\nprint(os.environ['SystemRoot'])")
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False, env=env)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == hardening.windows_folder()  # Winsock works with it
