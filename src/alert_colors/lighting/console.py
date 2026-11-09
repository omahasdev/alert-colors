"""Debug backend: logs each color change. Used by `--dry-run`."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from ..frame import Frame
from .base import BACKENDS

log = logging.getLogger(__name__)


@dataclass
class ConsoleBackend:
    name: str = "console"
    _last: Any = None

    def show(self, frame: Frame) -> None:
        # A uniform frame logs its color; a spatial one logs its effect once, not each frame.
        key = frame.color() if frame.uniform else type(frame.effect).__name__
        if key != self._last:
            shown = key.to_hex() if frame.uniform else f"{key} (per-LED effect)"
            log.info("console LED -> %s", shown)
            self._last = key

    def reclaim(self) -> None:
        pass  # the log has no other owner

    def release(self) -> None:
        if self._last is not None:
            log.info("console LED released")
        self._last = None

    def describe(self) -> list[str]:
        return ["console (logs colors, no hardware)"]


@BACKENDS.register("console")
def _create(options: dict[str, Any]) -> ConsoleBackend:
    return ConsoleBackend()
