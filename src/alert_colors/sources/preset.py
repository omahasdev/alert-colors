"""Presets: named `json_http` sources with default values for one API.

The [source] table of config.toml is merged over the defaults, table by
table. A preset is thus only data, and the user can change any part of it:
the URL, a header, an expression, the kind map.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .base import SOURCES, AlertSource
from .json_http import create_json_http


def deep_merge(defaults: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(defaults)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


class NotConfiguredError(ValueError):
    """The place has no value for a variable that the preset needs (for example,
    no oblast name). A fallback source with this error is skipped."""


@dataclass(frozen=True)
class Token:
    """The token of a preset: the environment variable, and where to request it."""

    variable: str
    request_url: str


TOKENS: dict[str, Token] = {}  # preset name -> its token


def register_preset(name: str, defaults: dict[str, Any], aliases: Mapping[str, str] | None = None,
                    token: Token | None = None) -> None:
    """`aliases` = {preset variable: shared variable}: a missing preset variable takes
    the value of the shared variable of [source.vars], which `setup.cmd region`
    writes. Thus the place is chosen one time for all sources. A variable that
    the config gives itself always wins (older configs keep working)."""
    def factory(options: dict[str, Any]) -> AlertSource:
        options = dict(options)
        variables = dict(options.get("vars", {}))
        for target, shared in (aliases or {}).items():
            if not variables.get(target) and variables.get(shared):
                variables[target] = variables[shared]
            if not variables.get(target):
                raise NotConfiguredError(f"{name}: [source.vars] has no {target!r} or {shared!r}; "
                                         "choose the place with setup.cmd region")
        options["vars"] = variables
        source = create_json_http(deep_merge(defaults, options))
        source.name = name
        return source

    SOURCES.register(name)(factory)
    if token is not None:
        TOKENS[name] = token
