"""Process hardening for the admin app. Call `apply()` before any other import of the app.

The installed app runs as admin, with the environment of the user, and a
process of the user without admin rights can change that environment. Thus:
- OpenSSL reads its config file and its certificate files from environment
  variables. A config file can load a module (a DLL), and a certificate file
  would let another server pass as the alert API. These variables are removed
  before `ssl` loads.
- Windows looks for a DLL that is loaded by name also in the folders of PATH,
  and the user can write many of them. The DLL search is limited to the app
  folder, the Windows system folder and the folders that the app adds.
- Windows parts (Winsock, among others) find the Windows folder through
  SystemRoot and windir. These variables are set again from Windows itself.

This module imports nothing that loads ssl or a third-party DLL.
"""

from __future__ import annotations

import os
import sys

# Environment variables that OpenSSL or the ssl module read.
UNSAFE_VARIABLES = ("OPENSSL_CONF", "OPENSSL_CONF_INCLUDE", "OPENSSL_MODULES", "OPENSSL_ENGINES",
                    "SSL_CERT_FILE", "SSL_CERT_DIR", "SSLKEYLOGFILE",
                    "PYTHONTZPATH")  # zoneinfo: time zone files (the bundled tzdata is used)
LOAD_LIBRARY_SEARCH_DEFAULT_DIRS = 0x00001000  # the app folder, System32, AddDllDirectory folders


def remove_unsafe_variables(environ: dict[str, str] | None = None) -> list[str]:
    """Remove UNSAFE_VARIABLES (any case). Returns the names that were set."""
    environ = os.environ if environ is None else environ
    removed = []
    for name in list(environ):
        if name.upper() in UNSAFE_VARIABLES:
            removed.append(name)
            del environ[name]
    return removed


# The variables that name the Windows folder: Windows parts read them.
WINDOWS_FOLDER_VARIABLES = ("SystemRoot", "windir")
WINDOWS_DRIVE_VARIABLE = "SystemDrive"


def windows_folder(kernel32: object | None = None) -> str:
    """The Windows folder from Windows itself, not from the environment."""
    import ctypes
    from ctypes import wintypes

    kernel32 = kernel32 or ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetSystemWindowsDirectoryW.restype = wintypes.UINT
    kernel32.GetSystemWindowsDirectoryW.argtypes = [wintypes.LPWSTR, wintypes.UINT]
    buffer = ctypes.create_unicode_buffer(wintypes.MAX_PATH)
    length = kernel32.GetSystemWindowsDirectoryW(buffer, len(buffer))
    if not 0 < length < len(buffer):
        raise OSError("GetSystemWindowsDirectoryW failed")
    return buffer.value


def restore_windows_variables(folder: str, environ: dict[str, str] | None = None) -> None:
    """Set SystemRoot, windir and SystemDrive from `folder` (any case of these names)."""
    environ = os.environ if environ is None else environ
    names = {name.upper() for name in (*WINDOWS_FOLDER_VARIABLES, WINDOWS_DRIVE_VARIABLE)}
    for name in [name for name in environ if name.upper() in names]:
        del environ[name]
    for name in WINDOWS_FOLDER_VARIABLES:
        environ[name] = folder
    environ[WINDOWS_DRIVE_VARIABLE] = os.path.splitdrive(folder)[0]


def restrict_dll_search(kernel32: object | None = None, folders: tuple[str, ...] = ()) -> None:
    """From now on, a DLL loaded by name comes only from the app folder, System32
    and `folders` (the PyInstaller folder of the app, when frozen)."""
    import ctypes
    from ctypes import wintypes

    kernel32 = kernel32 or ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.SetDefaultDllDirectories.restype = wintypes.BOOL
    kernel32.SetDefaultDllDirectories.argtypes = [wintypes.DWORD]
    kernel32.AddDllDirectory.restype = wintypes.LPVOID
    kernel32.AddDllDirectory.argtypes = [wintypes.LPCWSTR]
    if not kernel32.SetDefaultDllDirectories(LOAD_LIBRARY_SEARCH_DEFAULT_DIRS):
        raise OSError("SetDefaultDllDirectories failed")
    for folder in folders:
        if not kernel32.AddDllDirectory(folder):
            raise OSError(f"AddDllDirectory failed: {folder}")


# The modules that load OpenSSL: they read the OpenSSL variables when they load.
OPENSSL_MODULES = ("ssl", "_ssl", "_hashlib")


def apply() -> None:
    if any(name in sys.modules for name in OPENSSL_MODULES):
        raise RuntimeError("hardening.apply() must run before ssl loads")
    remove_unsafe_variables()
    if sys.platform == "win32":
        restore_windows_variables(windows_folder())
        bundle = getattr(sys, "_MEIPASS", None)  # PyInstaller: the folder of the bundled DLLs
        restrict_dll_search(folders=(bundle,) if bundle else ())


def main() -> int:
    """The `alert-colors` console script (pyproject.toml): hardening first, then the app."""
    apply()
    from .cli import main as cli_main

    return cli_main()
