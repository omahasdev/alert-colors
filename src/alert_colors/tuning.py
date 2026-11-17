"""Live tuning: a small JSON file that changes the look while the app runs.

The installed app runs as admin and reads its config from Program Files, where
only administrators can write. The tuner (configure.cmd) runs with normal
rights, so it writes this separate file instead, and the app reloads it within
`check_interval_s`. No restart and no UAC are necessary.

The app reads this file with admin rights and your account can write it, so
its content is limited and checked strictly. Allowed:

    {
      "scenes":   {"<existing scene>": {"effect": "...", <effect options>}},
      "display":  {"brightness": 0..1},
      "lighting": {"<backend type>": {"brightness": 0..1, "color_correction": [r, g, b]}},
      "preview":  {"scene": "<existing scene>", "until": <unix time>,
                   "definition": {<optional scene definition, used only for the preview>}}
    }

A scene must already exist in config.toml (the rules name the scenes). The
file can never change the source, the URLs, the rules, the log path or any
other setting. On any error the app logs a warning and keeps the last valid
tuning, so the worst a bad file can do is wrong colors.
"""

from __future__ import annotations

import json
import math
import logging
import os
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import machine_paths
from .domain import FULL_BRIGHTNESS, NO_BRIGHTNESS
from .scenes import Scene, build_scenes

log = logging.getLogger(__name__)

TOP_KEYS = {"scenes", "display", "lighting", "preview"}
DISPLAY_KEYS = {"brightness"}
LIGHTING_KEYS = {"brightness", "color_correction"}
PREVIEW_KEYS = {"scene", "until", "definition"}
# The longest preview. The file is writable without admin rights: a preview must
# never hide a real alert for long. The configurator uses the same limit.
PREVIEW_MIN_S, PREVIEW_DEFAULT_S, PREVIEW_MAX_S = 1.0, 10.0, 120.0
# The configurator replaces the file while the app can read it: it tries again this often.
REPLACE_ATTEMPTS, REPLACE_WAIT_S = 20, 0.05
RGB_CHANNELS = 3
MAX_FILE_BYTES = 262_144  # a tuning file is small; refuse anything larger


_NEVER_CHECKED = object()  # differs from every file signature, also from None (no file)


class TuningError(ValueError):
    pass


@dataclass(frozen=True)
class LightingTuning:
    brightness: float | None = None
    correction: tuple[float, float, float] | None = None


@dataclass(frozen=True)
class Tuning:
    scenes: dict[str, Scene]  # the effective scenes: config scenes with the tuned ones replaced
    scene_options: dict[str, dict[str, Any]]  # the effective raw options, for the tuner UI
    brightness: float | None = None
    lighting: dict[str, LightingTuning] = field(default_factory=dict)
    preview_scene: str | None = None
    preview_until: float = 0.0
    preview_definition: Scene | None = None  # form values that are not applied yet


def _number(value: Any, where: str) -> float:
    """float(value), but a TuningError for null, a list or text: the watcher catches only that."""
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as error:  # OverflowError: an integer too large
        raise TuningError(f"{where}: {value!r} is not a number") from error
    if not math.isfinite(number):  # json.loads accepts Infinity and NaN
        raise TuningError(f"{where}: {value!r} is not a finite number")
    return number


def _factor(value: Any, where: str) -> float:
    number = _number(value, where)
    if not NO_BRIGHTNESS <= number <= FULL_BRIGHTNESS:
        raise TuningError(f"{where}: {number} is outside 0..1")
    return number


def _only(table: Any, allowed: set[str], where: str) -> Mapping[str, Any]:
    if not isinstance(table, dict):
        raise TuningError(f"{where}: must be an object")
    unknown = set(table) - allowed
    if unknown:
        raise TuningError(f"{where}: keys {sorted(unknown)} are not allowed; allowed: {sorted(allowed)}")
    return table


def parse(raw: Any, base_scenes: Mapping[str, dict[str, Any]], backend_types: set[str]) -> Tuning:
    """Validate a tuning document against the config. Raises TuningError."""
    raw = _only(raw, TOP_KEYS, "tuning")
    scenes_raw = _only(raw.get("scenes", {}), set(base_scenes), "scenes")
    options = {name: dict(base_scenes[name]) for name in base_scenes}
    for name, definition in scenes_raw.items():
        if not isinstance(definition, dict):
            raise TuningError(f"scenes.{name}: must be an object")
        options[name] = dict(definition)
    try:
        scenes = build_scenes(options)
    except (ValueError, KeyError, TypeError, OverflowError) as error:
        raise TuningError(f"scenes: {error}") from error

    display = _only(raw.get("display", {}), DISPLAY_KEYS, "display")
    brightness = _factor(display["brightness"], "display.brightness") if "brightness" in display else None

    lighting = {}
    for backend, table in _only(raw.get("lighting", {}), backend_types, "lighting").items():
        table = _only(table, LIGHTING_KEYS, f"lighting.{backend}")
        correction = table.get("color_correction")
        if correction is not None:
            if not isinstance(correction, list) or len(correction) != RGB_CHANNELS:
                raise TuningError(f"lighting.{backend}.color_correction: needs [r, g, b]")
            r, g, b = (_factor(c, f"lighting.{backend}.color_correction") for c in correction)
            correction = (r, g, b)
        backend_brightness = (_factor(table["brightness"], f"lighting.{backend}.brightness")
                              if "brightness" in table else None)
        lighting[backend] = LightingTuning(brightness=backend_brightness, correction=correction)

    preview = _only(raw.get("preview", {}), PREVIEW_KEYS, "preview")
    preview_scene = preview.get("scene")
    if preview_scene is not None and (not isinstance(preview_scene, str) or preview_scene not in scenes):
        raise TuningError(f"preview.scene: unknown scene {preview_scene!r}")
    preview_definition = None
    if "definition" in preview:
        if preview_scene is None or not isinstance(preview["definition"], dict):
            raise TuningError("preview.definition: needs preview.scene and an object")
        try:
            preview_definition = build_scenes({preview_scene: preview["definition"]})[preview_scene]
        except (ValueError, KeyError, TypeError, OverflowError) as error:
            raise TuningError(f"preview.definition: {error}") from error
    return Tuning(
        scenes=scenes,
        scene_options=options,
        brightness=brightness,
        lighting=lighting,
        preview_scene=preview_scene,
        preview_until=_number(preview.get("until", 0.0), "preview.until"),
        preview_definition=preview_definition,
    )


GENERIC_READ = 0x80000000
SHARE_ALL = 0x7  # read, write, delete: the read blocks no other open of the file
OPEN_EXISTING = 3
FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000  # the link itself, never its target
FILE_ATTRIBUTE_DIRECTORY, FILE_ATTRIBUTE_REPARSE_POINT = 0x10, 0x400
INVALID_HANDLE_VALUE = -1
INVALID_FILE_ATTRIBUTES = 0xFFFFFFFF
ONE_NAME = 1


class NotPlainFileError(PermissionError):
    """The tuning file is a link, a folder or a file with more names: the app never reads it."""


class NotTrustedFolderError(NotPlainFileError):
    """The app has admin rights, and the tuning folder is a link or not a folder of Administrators."""


# The owners of a tuning folder that an app with admin rights reads: the setup makes it so.
# With the checks of the rights below (no other account can change the folder, the file or
# the entries of the parent), no other account can rename or replace them during a read.
TRUSTED_FOLDER_OWNERS = (
    "S-1-5-32-544",  # Administrators
    "S-1-5-18",  # SYSTEM
    "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464",  # TrustedInstaller: Program Files and others
)


def _is_admin() -> bool:
    import ctypes

    return bool(ctypes.windll.shell32.IsUserAnAdmin())


def _owner_sid(path: Path) -> str:
    import pywintypes
    import win32security

    try:
        security = win32security.GetFileSecurity(str(path), win32security.OWNER_SECURITY_INFORMATION)
    except pywintypes.error as error:  # not an OSError: the watcher must see it as "not read now"
        raise OSError(f"cannot read the owner of {path}: {error.strerror}") from error
    return win32security.ConvertSidToStringSid(security.GetSecurityDescriptorOwner())


# Access rights that let an account change a tuning folder or file, or take it over.
FILE_WRITE_DATA, FILE_APPEND_DATA, FILE_DELETE_CHILD = 0x2, 0x4, 0x40  # on a folder: add a file, add a folder
DELETE, WRITE_DAC, WRITE_OWNER = 0x10000, 0x40000, 0x80000
GENERIC_ALL, GENERIC_WRITE = 0x10000000, 0x40000000
TAKE_OVER = WRITE_DAC | WRITE_OWNER | GENERIC_ALL
CHANGE = FILE_WRITE_DATA | FILE_APPEND_DATA | FILE_DELETE_CHILD | DELETE | GENERIC_WRITE | TAKE_OVER
RENAME_CHILD = FILE_DELETE_CHILD | TAKE_OVER  # on the parent: rename or delete the tuning folder
# Rules for "the owner of this object": the owner itself is checked against the trusted owners.
OWNER_STAND_INS = ("S-1-3-4", "S-1-3-0")  # OWNER RIGHTS, CREATOR OWNER
ACCESS_ALLOWED_ACE_TYPE, ACCESS_DENIED_ACE_TYPE = 0, 1
INHERIT_ONLY_ACE = 0x8  # a rule for new entries only, not for this object


def _other_writers(path: Path, rights: int, own_rights: int = 0) -> list[str]:
    """The accounts, other than the trusted owners, that `rights` let change `path`. The account of
    this process may have `own_rights` only: a process of the user without admin rights must not
    be able to rename or replace a folder of the read either."""
    import pywintypes
    import win32security

    try:
        security = win32security.GetFileSecurity(str(path), win32security.DACL_SECURITY_INFORMATION)
    except pywintypes.error as error:
        raise OSError(f"cannot read the rights of {path}: {error.strerror}") from error
    dacl = security.GetSecurityDescriptorDacl()
    if dacl is None:
        return ["Everyone (no access list)"]
    allowed = (*TRUSTED_FOLDER_OWNERS, *OWNER_STAND_INS)  # the owner is checked on its own
    own = _own_sid()
    others = []
    for index in range(dacl.GetAceCount()):
        try:
            (kind, flags), mask, sid = dacl.GetAce(index)
        except (NotImplementedError, ValueError, TypeError):
            # A conditional, object or other unusual rule: pywin32 cannot read it, so it
            # could give anybody any right.
            others.append(f"an unusual access rule ({index + 1})")
            continue
        if kind == ACCESS_DENIED_ACE_TYPE:
            continue  # a denial gives nobody a right
        if kind != ACCESS_ALLOWED_ACE_TYPE:
            others.append(f"an unusual access rule (type {kind})")
            continue
        if flags & INHERIT_ONLY_ACE or not mask & rights:
            continue
        name = win32security.ConvertSidToStringSid(sid)
        if name in allowed or (name == own and not mask & rights & ~own_rights):
            continue
        others.append(name)
    return others


def _check_folder(folder: Path) -> None:
    """With admin rights: a folder that another account made or links elsewhere (for example
    after a failed setup step) must not give the look of the app."""
    import ctypes
    from ctypes import wintypes

    if not _is_admin():
        return  # a process of the user reads what the user can write anyway
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetFileAttributesW.restype = wintypes.DWORD
    kernel32.GetFileAttributesW.argtypes = [wintypes.LPCWSTR]
    attributes = kernel32.GetFileAttributesW(str(folder))
    if attributes == INVALID_FILE_ATTRIBUTES:
        raise ctypes.WinError(ctypes.get_last_error())  # missing or not readable: not read now
    if attributes & FILE_ATTRIBUTE_REPARSE_POINT:
        raise NotTrustedFolderError(f"the tuning folder {folder} is a link: make it a real folder")
    if _owner_sid(folder) not in TRUSTED_FOLDER_OWNERS:
        raise NotTrustedFolderError(f"the tuning folder {folder} does not belong to Administrators: make "
                                    "Administrators its owner (setup.cmd update does this for the default folder)")
    # The owner alone is not enough: inherited rules can let every account write (for example
    # in a new folder on another drive), and the right to delete entries of the parent lets
    # an account rename the folder and put a link in its place.
    # The account of the app may only add files to the folder (the setup gives it no more): no
    # process of the user without admin rights can then rename the folder and put a link there.
    if others := _other_writers(folder, CHANGE, own_rights=FILE_WRITE_DATA):
        raise NotTrustedFolderError(f"other accounts can change the tuning folder {folder} ({', '.join(others)}): "
                                    "remove their write rights (setup.cmd update does this for the default folder)")
    # The owner of a folder can always change its rights: the parent must be trusted too.
    if _owner_sid(folder.parent) not in TRUSTED_FOLDER_OWNERS:
        raise NotTrustedFolderError(f"the folder {folder.parent} of the tuning folder belongs to another account: "
                                    "use a folder in ProgramData")
    if others := _other_writers(folder.parent, RENAME_CHILD):
        raise NotTrustedFolderError(f"other accounts can rename the tuning folder {folder} through its parent "
                                    f"({', '.join(others)}): use a folder in ProgramData")


def _own_sid() -> str:
    """The account of this process: the user that the task runs the app for."""
    import win32api
    import win32con
    import win32security

    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    return win32security.ConvertSidToStringSid(win32security.GetTokenInformation(token, win32security.TokenUser)[0])


def _check_file_owner(path: Path) -> None:
    """With admin rights: a file that another account put into the folder (before the setup
    locked it) must not give the look of the app either."""
    if not _is_admin():
        return
    if _owner_sid(path) not in (*TRUSTED_FOLDER_OWNERS, _own_sid()):
        raise NotTrustedFolderError(f"the tuning file {path} belongs to another account: delete it, or run "
                                    "setup.cmd update")
    # The file itself: the account of the app may change it (the configurator writes it). The open
    # refuses a link at its name and a second name, so a right on the file cannot redirect the read.
    if others := _other_writers(path, CHANGE, own_rights=CHANGE):
        raise NotTrustedFolderError(f"other accounts can change the tuning file {path} ({', '.join(others)}): "
                                    "run setup.cmd update, or remove their write rights")


def _open_plain(path: Path) -> Any:
    """The file for reading. On Windows only a plain file: not a link and with one name.
    The tuning folder is writable by the user, and the app reads it with admin rights:
    a link there must not let it read another file."""
    if sys.platform != "win32":
        return path.open("rb")
    _check_folder(path.parent)
    _check_file_owner(path)
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class Info(ctypes.Structure):  # BY_HANDLE_FILE_INFORMATION: 13 DWORDs
        _fields_ = [("attributes", wintypes.DWORD), ("times", wintypes.DWORD * 6), ("volume", wintypes.DWORD),
                    ("size_high", wintypes.DWORD), ("size_low", wintypes.DWORD), ("links", wintypes.DWORD),
                    ("index_high", wintypes.DWORD), ("index_low", wintypes.DWORD)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                     wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel32.GetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.POINTER(Info)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.CreateFileW(str(path), GENERIC_READ, SHARE_ALL, None, OPEN_EXISTING,
                                  FILE_FLAG_OPEN_REPARSE_POINT, None)
    if handle in (None, wintypes.HANDLE(INVALID_HANDLE_VALUE).value):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        info = Info()
        if not kernel32.GetFileInformationByHandle(handle, ctypes.byref(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        if info.attributes & (FILE_ATTRIBUTE_DIRECTORY | FILE_ATTRIBUTE_REPARSE_POINT) or info.links != ONE_NAME:
            raise NotPlainFileError(f"{path} is not a plain file (a link, a folder or a file with more names)")
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY)  # the descriptor owns the handle now
    except BaseException:
        kernel32.CloseHandle(handle)
        raise
    try:
        return os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)  # closes the handle too
        raise


def read_file(path: Path) -> Any:
    # One open and a limited read: a file that grows after a size check is still cut off.
    with _open_plain(path) as f:
        raw = f.read(MAX_FILE_BYTES + 1)
    if len(raw) > MAX_FILE_BYTES:
        raise TuningError(f"{path} is larger than {MAX_FILE_BYTES} bytes")
    try:
        return json.loads(raw.decode("utf-8"))
    except RecursionError as error:  # deep nesting: not a ValueError, the watcher would not catch it
        raise TuningError(f"{path} is nested too deeply") from error


def write_file(path: Path, document: dict[str, Any]) -> None:
    """Write atomically: the app never reads a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(document, indent=2, ensure_ascii=False), encoding="utf-8")
    for attempt in range(REPLACE_ATTEMPTS):
        try:
            os.replace(temporary, path)
            return
        except PermissionError:  # the app reads the file at this moment: try again soon
            if attempt == REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(REPLACE_WAIT_S)


def resolve_path(raw: str) -> Path:
    """The tuning path. The admin app reads it, so %ProgramData% and the other machine
    folders come from Windows, not from the environment of the user."""
    if sys.platform != "win32":
        return Path(os.path.expandvars(raw))
    return Path(machine_paths.expand(raw, strict=False))


@dataclass
class TuningWatcher:
    """Reloads the tuning file when it changes; keeps the last valid tuning."""

    path: Path
    base_scenes: Mapping[str, dict[str, Any]]
    backend_types: set[str]
    check_interval_s: float
    monotonic: Callable[[], float]
    _signature: object = field(default=_NEVER_CHECKED, init=False)
    _refused_signature: object = field(default=None, init=False)  # the version that was not plain
    _stat_error: str | None = field(default=None, init=False)  # the last error of the check, warned once
    _next_check: float = field(default=0.0, init=False)

    def poll(self) -> Tuning | None:
        """A new Tuning when the file changed (or was removed), else None."""
        now = self.monotonic()
        if now < self._next_check:
            return None
        self._next_check = now + self.check_interval_s
        try:
            stat = self.path.stat()
            signature = (stat.st_mtime, stat.st_size)
        except FileNotFoundError:
            signature = None
        except OSError as error:  # for example links that point at each other: not read now
            if str(error) != self._stat_error:
                self._stat_error = str(error)  # one warning for each error, not one at each check
                log.warning("tuning file %s cannot be checked: %s", self.path, error)
            return None
        self._stat_error = None
        if signature == self._signature:
            return None
        previous, self._signature = self._signature, signature
        try:
            raw = read_file(self.path) if signature is not None else {}
            tuning = parse(raw, self.base_scenes, self.backend_types)
        except OSError as error:
            # Not read (for example the configurator replaces it at this moment): the next
            # check reads it again.
            self._signature = previous
            if isinstance(error, NotPlainFileError) and signature != self._refused_signature:
                self._refused_signature = signature  # one warning for each version of the file
                hint = "" if isinstance(error, NotTrustedFolderError) else ". Save the tuning again in the configurator"
                log.warning("tuning file ignored: %s%s", error, hint)
            else:
                log.debug("tuning file %s not read now: %s", self.path, error)
            return None
        except ValueError as error:  # a bad file: TuningError and JSON errors are ValueErrors
            log.warning("tuning file %s ignored: %s", self.path, error)
            return None
        log.info("tuning applied from %s", self.path if signature is not None else "config (no tuning file)")
        return tuning
