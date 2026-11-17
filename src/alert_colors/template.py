"""`${...}` placeholders in config strings.

- `${env:NAME}`  the environment variable NAME (for tokens and other secrets). On
                 Windows, also the user environment in the registry (see user_env.py)
- `${name}`      a value from the `[source.vars]` table (for example, the region)

Placeholders work in any string of a source config: URL, headers, query
parameters and JMESPath expressions. A missing name is an error, so a typo
never becomes an empty string.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Mapping
from typing import Any

from . import user_env

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


# JMESPath contexts of a placeholder: a raw string 'x', a quoted name "x", a JSON literal `x`.
_RAW_STRING, _QUOTED_NAME, _JSON_LITERAL = "'", '"', "`"
_PLAIN_TOKEN = re.compile(r"[A-Za-z0-9_]+")


def expand_jmespath(expression: str, variables: Mapping[str, str]) -> str:
    """Expand the placeholders of a JMESPath expression, escaped for their context.

    The values can come from a server (the region list, a lookup), and the result
    selects the alert status: a value must never change the expression itself.
    - In a quoted name "x": escaped as a JSON string, as JMESPath reads it.
    - In a raw string 'x': a value with a quote, a backslash (which escapes the
      closing quote) or a control character is refused.
    - In a JSON literal `x`: inside a JSON string there, escaped as a JSON string (a
      backtick is refused); elsewhere, letters, digits and "_", or a placeholder that
      is the whole literal with a value that is valid JSON (a lookup value) and has no
      backtick. Valid JSON cannot end in a backslash, so it cannot end the literal.
    - A placeholder right after a backslash in a literal is refused: the escape of the
      template would join the value.
    - Outside a literal: only letters, digits and "_", or exactly one JSON string (the
      form of a lookup value), which JMESPath reads as a quoted name.
    """
    out, i, context, in_json_string = [], 0, None, False
    while i < len(expression):
        if expression.startswith("${", i):
            end = expression.find("}", i)
            if end < 0:
                raise ValueError(f"unclosed placeholder in {expression!r}")
            value = _lookup(expression[i + 2:end], variables)
            whole_literal = (context == _JSON_LITERAL and expression[i - 1:i] == _JSON_LITERAL
                             and expression[end + 1:end + 2] == _JSON_LITERAL)
            out.append(_escape_for(value, context, in_json_string, whole_literal, expression))
            i = end + 1
            continue
        char = expression[i]
        if context is not None and char == "\\" and i + 1 < len(expression):
            if expression.startswith("${", i + 1):
                raise ValueError(f"a placeholder right after an escape in {expression!r}")
            out.append(expression[i:i + 2])  # an escape of the template itself
            i += 2
            continue
        if context is None and char in (_RAW_STRING, _QUOTED_NAME, _JSON_LITERAL):
            context, in_json_string = char, False
        elif char == context:
            context, in_json_string = None, False
        elif context == _JSON_LITERAL and char == '"':
            in_json_string = not in_json_string
        out.append(char)
        i += 1
    return "".join(out)


def _has_control(value: str) -> bool:
    return any(unicodedata.category(c).startswith("C") for c in value)


def _escape_for(value: str, context: str | None, in_json_string: bool, whole_literal: bool,
                expression: str) -> str:
    if context == _QUOTED_NAME:
        return json.dumps(value, ensure_ascii=False)[1:-1]
    if context == _RAW_STRING:
        if _RAW_STRING in value or "\\" in value or _has_control(value):
            raise ValueError(f"the value {value!r} cannot go into {expression!r}: it has a quote or an escape")
        return value
    if context == _JSON_LITERAL:
        if _JSON_LITERAL in value:
            raise ValueError(f"the value {value!r} cannot go into {expression!r}: it has a backtick")
        if in_json_string:
            return json.dumps(value, ensure_ascii=False)[1:-1]
        if _PLAIN_TOKEN.fullmatch(value):
            return value  # a number or a word: it cannot close a string or add structure
        if whole_literal and _is_json(value):
            return value  # a lookup value: one JSON document fills the literal
        raise ValueError(f"the value {value!r} cannot go into {expression!r}: only a whole JSON value fits there")
    if _PLAIN_TOKEN.fullmatch(value) or _is_json_string(value):
        return value  # a JSON string (a lookup value) is a quoted name: escaped already
    raise ValueError(f"the value {value!r} cannot go into {expression!r} outside a literal")


def _is_json(value: str) -> bool:
    try:
        json.loads(value)
    except ValueError:
        return False
    return True


def _is_json_string(value: str) -> bool:
    """True for exactly one JSON string, as json.dumps writes it (all its escapes are JSON)."""
    try:
        decoded = json.loads(value)
    except ValueError:
        return False
    return isinstance(decoded, str) and json.dumps(decoded, ensure_ascii=False) == value


def _lookup(name: str, variables: Mapping[str, str]) -> str:
    if name.startswith(ENV_PREFIX):
        env_name = name[len(ENV_PREFIX):]
        value = user_env.get(env_name)
        if value is None:
            raise KeyError(f"environment variable {env_name} is not set (used as ${{{name}}})")
        return value
    if name not in variables:
        known = ", ".join(sorted(variables)) or "none"
        raise KeyError(f"unknown placeholder ${{{name}}}; [source.vars] has: {known}")
    return str(variables[name])
