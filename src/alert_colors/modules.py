"""Optional modules: import the ones that are in this build, skip the others.

A package (lighting, sources) lists its optional modules. The build profile
(build.toml) selects which of them go into the exe; PyInstaller leaves the
others out. At run time `load_optional` imports what exists, and each module
registers itself in its package registry.
"""

from __future__ import annotations

import importlib
from collections.abc import Iterable


def load_optional(package: str, names: Iterable[str]) -> tuple[str, ...]:
    """Import `package.<name>` for each name; return the names that exist."""
    loaded = []
    for name in names:
        qualified = f"{package}.{name}"
        try:
            importlib.import_module(qualified)
        except ModuleNotFoundError as error:
            if error.name != qualified:
                raise  # the module exists but one of its imports is missing: a real fault
            continue
        loaded.append(name)
    return tuple(loaded)
