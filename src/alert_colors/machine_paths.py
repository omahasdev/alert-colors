"""Machine folders (%ProgramFiles%, %ProgramData%, %SystemRoot%) from Windows.

The installed app runs as admin, with the environment of the user, and the user
can change that environment. So a path that the admin process trusts must take
these folders from Windows (SHGetFolderPathW), not from os.environ.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable

# %NAME% (lowercase) -> CSIDL
MACHINE_FOLDERS = {
    "systemroot": 0x0024, "windir": 0x0024,  # CSIDL_WINDOWS
    "programfiles": 0x0026, "programw6432": 0x0026,  # CSIDL_PROGRAM_FILES
    "programfiles(x86)": 0x002A,  # CSIDL_PROGRAM_FILESX86
    "commonprogramfiles": 0x002B, "commonprogramw6432": 0x002B,  # CSIDL_PROGRAM_FILES_COMMON
    "commonprogramfiles(x86)": 0x002C,  # CSIDL_PROGRAM_FILES_COMMONX86
    "programdata": 0x0023, "allusersprofile": 0x0023,  # CSIDL_COMMON_APPDATA
}
PLACEHOLDER = re.compile(r"%([^%]+)%")


def machine_folder(csidl: int) -> str:
    import ctypes
    from ctypes import wintypes

    buffer = ctypes.create_unicode_buffer(wintypes.MAX_PATH)
    result = ctypes.windll.shell32.SHGetFolderPathW(None, csidl, None, 0, buffer)
    if result != 0:
        raise OSError(f"SHGetFolderPathW({csidl:#x}) failed: {result:#x}")
    return buffer.value


def expand(raw: str, strict: bool, folder: Callable[[int], str] = machine_folder) -> str:
    """Expand the machine folders from Windows. Any other %NAME%: an error if
    `strict`, else the value of the environment (os.path.expandvars)."""
    def replace(match: re.Match[str]) -> str:
        csidl = MACHINE_FOLDERS.get(match.group(1).lower())
        if csidl is not None:
            return folder(csidl)
        if strict:
            raise ValueError(f"%{match.group(1)}% is not a machine folder: {raw!r}")
        return os.path.expandvars(match.group(0))

    return PLACEHOLDER.sub(replace, raw)
