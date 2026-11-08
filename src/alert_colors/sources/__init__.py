"""Alert sources.

`json_http` is part of the core: it reads any JSON API from config. The named
APIs are optional modules: presets of `json_http` with the right request,
JMESPath fields and kind map. A build includes only the presets that
`build.toml` lists under `sources`. In config.toml, `type = "<preset>"` takes
its defaults, and any key in [source] overrides them.

To add an API, write `sources/<name>.py` that calls `register_preset("<name>",
DEFAULTS)` (see sources/preset.py), and add `<name>` to OPTIONAL_MODULES. For an
API that is not JSON (XML, text, a WebSocket), register a new source class
with `@SOURCES.register("<name>")`.
"""

from __future__ import annotations

from ..modules import load_optional
from . import json_http  # noqa: F401  (core: registration side effect)
from .base import SOURCES, AlertSource

OPTIONAL_MODULES = ("siren_pp_ua", "ukrainealarm", "ubilling", "alerts_in_ua", "alerts_com_ua")
AVAILABLE_MODULES = load_optional(__name__, OPTIONAL_MODULES)

__all__ = ["AVAILABLE_MODULES", "OPTIONAL_MODULES", "SOURCES", "AlertSource"]
