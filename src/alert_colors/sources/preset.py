"""Presets: named `json_http` sources with default values for one API.

The [source] table of config.toml is merged over the defaults, table by
table. A preset is thus only data, and the user can change any part of it:
the URL, a header, an expression, the kind map.
"""

from __future__ import annotations

import copy
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


def register_preset(name: str, defaults: dict[str, Any]) -> None:
    def factory(options: dict[str, Any]) -> AlertSource:
        source = create_json_http(deep_merge(defaults, options))
        source.name = name
        return source

    SOURCES.register(name)(factory)
