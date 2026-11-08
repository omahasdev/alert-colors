"""`${...}` placeholders in config strings.

- `${env:NAME}`  the environment variable NAME (for tokens and other secrets)
- `${name}`      a value from the `[source.vars]` table (for example, the region)

Placeholders work in any string of a source config: URL, headers, query
parameters and JMESPath expressions. A missing name is an error, so a typo
never becomes an empty string.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from typing import Any

PLACEHOLDER = re.compile(r"\$\{([^}]+)\}")
ENV_PREFIX = "env:"


def expand(value: Any, variables: Mapping[str, str]) -> Any:
    """Expand placeholders in a string, or in every string of a list or table."""
    if isinstance(value, str):
        return PLACEHOLDER.sub(lambda match: _lookup(match.group(1), variables), value)
    if isinstance(value, list):
        return [expand(item, variables) for item in value]
    if isinstance(value, dict):
        return {key: expand(item, variables) for key, item in value.items()}
    return value


def _lookup(name: str, variables: Mapping[str, str]) -> str:
    if name.startswith(ENV_PREFIX):
        env_name = name[len(ENV_PREFIX):]
        if env_name not in os.environ:
            raise KeyError(f"environment variable {env_name} is not set (used as ${{{name}}})")
        return os.environ[env_name]
    if name not in variables:
        known = ", ".join(sorted(variables)) or "none"
        raise KeyError(f"unknown placeholder ${{{name}}}; [source.vars] has: {known}")
    return str(variables[name])
