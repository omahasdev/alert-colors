"""Policy: AlertStatus -> scene name, by ordered rules. Pure logic, no I/O.

    [[rules]]                         # the first rule that matches wins
    state = "unknown"                 # "ok" (default) or "unknown"
    active = true                     # optional: an alert is active or not
    coverage = "partial"              # optional: "none" | "partial" | "full"
    kinds = ["artillery", "nuclear"]  # optional: any of these kinds is active
    scene = "unknown"                 # required: a name from [scenes]

The rule order is the priority. Safety: missing or stale data (state
"unknown") matches only rules with `state = "unknown"`, so it can never select
a "clear" scene. The config must have such a rule, and one "ok" rule with no
conditions, so that every status selects a scene.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import Any

from .domain import AlertStatus, Coverage

STATE_OK = "ok"
STATE_UNKNOWN = "unknown"
STATES = (STATE_OK, STATE_UNKNOWN)
RULE_KEYS = {"scene", "state", "active", "coverage", "kinds"}


@dataclass(frozen=True)
class Rule:
    scene: str
    state: str = STATE_OK
    active: bool | None = None
    coverage: Coverage | None = None
    kinds: frozenset[str] | None = None

    @classmethod
    def from_config(cls, raw: dict[str, Any]) -> Rule:
        unknown_keys = set(raw) - RULE_KEYS
        if unknown_keys:
            raise ValueError(f"rule {raw}: unknown keys {sorted(unknown_keys)}")
        state = raw.get("state", STATE_OK)
        if state not in STATES:
            raise ValueError(f"rule {raw}: state must be one of {STATES}")
        coverage = raw.get("coverage")
        kinds = raw.get("kinds")
        return cls(
            scene=raw["scene"],
            state=state,
            active=raw.get("active"),
            coverage=Coverage(coverage) if coverage else None,
            kinds=frozenset(kinds) if kinds is not None else None,
        )

    @property
    def is_catch_all(self) -> bool:
        return self.active is None and self.coverage is None and self.kinds is None

    def matches(self, status: AlertStatus | None) -> bool:
        if status is None:
            return self.state == STATE_UNKNOWN
        if self.state != STATE_OK:
            return False
        return (
            (self.active is None or status.active == self.active)
            and (self.coverage is None or status.coverage == self.coverage)
            and (self.kinds is None or bool(self.kinds & status.kinds))
        )


@dataclass(frozen=True)
class Policy:
    rules: Sequence[Rule]
    scene_names: Collection[str]

    def __post_init__(self) -> None:
        missing = sorted({rule.scene for rule in self.rules} - set(self.scene_names))
        if missing:
            raise ValueError(f"rules use undefined scenes: {', '.join(missing)}")
        if not any(rule.state == STATE_UNKNOWN for rule in self.rules):
            raise ValueError('rules need one rule with state = "unknown"')
        if not any(rule.state == STATE_OK and rule.is_catch_all for rule in self.rules):
            raise ValueError('rules need a last rule with no conditions, for example scene = "clear"')

    @classmethod
    def from_config(cls, rules: list[dict[str, Any]], scene_names: Collection[str]) -> Policy:
        return cls([Rule.from_config(rule) for rule in rules], scene_names)

    def select(self, status: AlertStatus | None) -> str:
        """`None` means no trustworthy data (fetch failed or data is stale)."""
        return next(rule.scene for rule in self.rules if rule.matches(status))
