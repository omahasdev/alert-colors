"""Lighting backends.

`console` is part of the core. The other backends are optional modules: a
build includes only the modules that `build.toml` lists under `lighting`
(see packaging/AlertColors.spec). A module that is not in the build is skipped.

To add a lighting system, write `lighting/<name>.py` that registers a factory
with `@BACKENDS.register("<name>")`, and add `<name>` to MODULE_NEEDS.
"""

from __future__ import annotations

from ..modules import load_optional
from . import console  # noqa: F401  (core: registration side effect)
from .base import BACKENDS, LightingBackend

# What each optional module needs from the install (read by the build manifest):
#   admin          the app must run with admin rights
#   armoury_crate  the exe must be in the Armoury Crate in-game priority list
MODULE_NEEDS = {
    "aura_sdk": {"admin": True, "armoury_crate": True},  # MB + ARGB headers need admin
    "razer_chroma": {"admin": False, "armoury_crate": False},
}
OPTIONAL_MODULES = tuple(MODULE_NEEDS)
AVAILABLE_MODULES = load_optional(__name__, OPTIONAL_MODULES)

__all__ = ["AVAILABLE_MODULES", "BACKENDS", "MODULE_NEEDS", "OPTIONAL_MODULES", "LightingBackend"]
