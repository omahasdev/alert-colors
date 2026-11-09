"""Generic JSON-over-HTTP alert source. Every API detail comes from config.

    [source]
    type = "json_http"
    [source.vars]            values for ${name} placeholders (for example, region)
    [source.request]         url, method, headers, params, body, timeout_s,
                             allow_http, allow_private_network, max_response_bytes
                             (see http.py: https only, no redirects, no local
                             addresses, size limit, unless you allow it here)
    [source.fields]          JMESPath expressions on the JSON response:
        full        truthy -> the alert covers the watched place      (required)
        partial     truthy -> an alert somewhere near it              (optional)
        kinds       a string or a list of strings: the alert kinds    (optional)
        observed_at time stamp of the data, for the staleness check   (optional)
        changed_at  time stamp of the last change; a list gives max   (optional)
    [source.kinds]           default = kind when active and `kinds` is empty
                             unmapped = name for API values not in `map`
                                        (omit it to keep the API value)
                             map = { api_value = "kind" }
    [source.time]            format = "iso" | "epoch" | a strftime pattern
                             timezone = IANA zone for time stamps without one
    [source.lookups.<name>]  optional: fetched once at the first poll; the JSON of
                             `expression` becomes the variable ${<name>} for the
                             fields (put it in backticks: a JMESPath literal).
                             url, expression, and any key of [source.request].
    [source.change_marker]   optional cheap pre-check (fewer full requests):
                             url, field (JMESPath), max_age_s, and any key of
                             [source.request] to override (headers are shared).
                             Each poll fetches only the marker; the full request
                             runs when the marker changes or after max_age_s.

JMESPath reference: https://jmespath.org/specification.html
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import jmespath

from ..domain import AlertStatus, Coverage
from ..http import DEFAULT_MAX_RESPONSE_BYTES, NetworkPolicy, request_json
from ..template import expand
from .base import SOURCES

ISO = "iso"
EPOCH = "epoch"
ISO_UTC_SUFFIX = "Z"
# Forms that "iso" also accepts: meta.last_updated_at of alerts.in.ua is "2026/11/08 11:05:00 +0000".
ISO_LIKE_FORMATS = ("%Y/%m/%d %H:%M:%S %z",)


class FieldError(ValueError):
    """An expression gave no value. The daemon then shows the unknown scene."""


@dataclass
class KindMapping:
    default: str | None = None
    unmapped: str | None = None
    table: dict[str, str] = field(default_factory=dict)

    def apply(self, raw: Any, active: bool) -> frozenset[str]:
        values = raw if isinstance(raw, list) else ([] if raw is None else [raw])
        kinds = {self.table.get(str(v), self.unmapped or str(v)) for v in values if v is not None}
        if active and not kinds and self.default:
            kinds.add(self.default)
        return frozenset(kinds) if active else frozenset()


@dataclass
class TimeParser:
    format: str
    timezone: ZoneInfo

    def parse(self, raw: Any) -> datetime | None:
        if raw is None:
            return None
        if isinstance(raw, list):
            parsed = [t for t in (self.parse(item) for item in raw) if t is not None]
            return max(parsed) if parsed else None
        if self.format == EPOCH:
            return datetime.fromtimestamp(float(raw), UTC)
        text = str(raw)
        if self.format == ISO:
            value = _parse_iso(text)
        else:
            value = datetime.strptime(text, self.format)
        return value if value.tzinfo else value.replace(tzinfo=self.timezone)



def _parse_iso(text: str) -> datetime:
    try:
        return datetime.fromisoformat(text.replace(ISO_UTC_SUFFIX, "+00:00"))
    except ValueError:
        for form in ISO_LIKE_FORMATS:
            try:
                return datetime.strptime(text, form)
            except ValueError:
                continue
        raise


@dataclass
class ChangeMarker:
    request: dict[str, Any]
    expression: Any  # compiled JMESPath
    max_age_s: float


def _get(request: dict[str, Any], network: NetworkPolicy) -> Any:
    return request_json(
        request["url"],
        method=request.get("method", "GET"),
        headers=request.get("headers"),
        params=request.get("params"),
        body=request.get("body"),
        timeout_s=float(request["timeout_s"]),
        policy=network,
    )


@dataclass
class Lookup:
    request: dict[str, Any]
    expression: Any  # compiled JMESPath


@dataclass
class JsonHttpSource:
    request: dict[str, Any]
    raw_fields: dict[str, str]  # name -> JMESPath text, before ${...} expansion
    variables: dict[str, str]
    kinds: KindMapping
    times: TimeParser
    network: NetworkPolicy = NetworkPolicy()
    marker: ChangeMarker | None = None
    lookups: dict[str, Lookup] = field(default_factory=dict)
    name: str = "json_http"
    monotonic: Callable[[], float] = time.monotonic
    fields: dict[str, Any] = field(default_factory=dict, init=False)  # compiled
    _last_marker: Any = field(default=None, init=False)
    _last_status: AlertStatus | None = field(default=None, init=False)
    _last_full_at: float = field(default=0.0, init=False)

    def __post_init__(self) -> None:
        if not self.lookups:
            self._compile_fields()

    def _compile_fields(self) -> None:
        self.fields = {name: jmespath.compile(expand(expr, self.variables))
                       for name, expr in self.raw_fields.items()}

    def provide_lookup(self, name: str, value: Any) -> None:
        """Set a lookup value (the first fetch does this; tests call it directly)."""
        if value is None:
            raise FieldError(f"lookup {name!r} gave no value")
        self.variables[name] = json.dumps(value, ensure_ascii=False)
        if all(lookup_name in self.variables for lookup_name in self.lookups):
            self._compile_fields()

    def fetch_lookup_document(self, name: str) -> Any:
        """The full JSON of a lookup request, before its expression."""
        return _get(self.lookups[name].request, self.network)

    def _resolve_lookups(self) -> None:
        for name, lookup in self.lookups.items():
            if name not in self.variables:
                self.provide_lookup(name, lookup.expression.search(_get(lookup.request, self.network)))

    def fetch(self) -> AlertStatus:
        if not self.fields:
            self._resolve_lookups()
        if self.marker is None:
            return self.parse(_get(self.request, self.network))
        value = self.marker.expression.search(_get(self.marker.request, self.network))
        if value is None:
            raise FieldError(f"change marker ({self.marker.expression.expression}) gave no value")
        now = self.monotonic()
        fresh = now - self._last_full_at < self.marker.max_age_s
        if value == self._last_marker and self._last_status is not None and fresh:
            # Nothing changed since the full request: the old status is current.
            return replace(self._last_status, observed_at=None)
        status = self.parse(_get(self.request, self.network))
        self._last_marker, self._last_status, self._last_full_at = value, status, now
        return status

    def parse(self, payload: Any) -> AlertStatus:
        full = self._truthy("full", payload)
        partial = "partial" in self.fields and self._truthy("partial", payload)
        coverage = Coverage.FULL if full else Coverage.PARTIAL if partial else Coverage.NONE
        return AlertStatus(
            coverage=coverage,
            kinds=self.kinds.apply(self._value("kinds", payload), coverage is not Coverage.NONE),
            observed_at=self.times.parse(self._value("observed_at", payload)),
            changed_at=self.times.parse(self._value("changed_at", payload)),
            source=self.name,
        )

    def _value(self, name: str, payload: Any) -> Any:
        expression = self.fields.get(name)
        return expression.search(payload) if expression is not None else None

    def _truthy(self, name: str, payload: Any) -> bool:
        value = self._value(name, payload)
        if value is None:
            raise FieldError(f"field {name!r} ({self.fields[name].expression}) gave no value")
        return bool(value)


def create_json_http(options: dict[str, Any]) -> JsonHttpSource:
    variables = {key: str(value) for key, value in options.get("vars", {}).items()}
    request = expand(options["request"], variables)
    if "full" not in options["fields"]:
        raise ValueError("json_http: [source.fields] needs a `full` expression")
    lookups = {}
    for lookup_name, raw_lookup in options.get("lookups", {}).items():
        raw_lookup = expand(raw_lookup, variables)
        lookup_request = {**request, **{k: v for k, v in raw_lookup.items() if k != "expression"}}
        lookups[lookup_name] = Lookup(lookup_request, jmespath.compile(raw_lookup["expression"]))
    kinds = options.get("kinds", {})
    times = options.get("time", {})
    marker = None
    if "change_marker" in options:
        raw = expand(options["change_marker"], variables)
        marker_request = {**request, **{k: v for k, v in raw.items() if k not in ("field", "max_age_s")}}
        marker = ChangeMarker(marker_request, jmespath.compile(raw["field"]), float(raw["max_age_s"]))
    return JsonHttpSource(
        request=request,
        raw_fields=dict(options["fields"]),
        variables=variables,
        lookups=lookups,
        kinds=KindMapping(
            default=kinds.get("default"),
            unmapped=kinds.get("unmapped"),
            table={str(k): str(v) for k, v in kinds.get("map", {}).items()},
        ),
        times=TimeParser(format=times.get("format", ISO), timezone=ZoneInfo(times.get("timezone", "UTC"))),
        network=NetworkPolicy(
            allow_http=bool(request.get("allow_http", False)),
            allow_private_network=bool(request.get("allow_private_network", False)),
            max_response_bytes=int(request.get("max_response_bytes", DEFAULT_MAX_RESPONSE_BYTES)),
        ),
        marker=marker,
    )


SOURCES.register("json_http")(create_json_http)
