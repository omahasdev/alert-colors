from __future__ import annotations

from typing import Protocol

from ..domain import AlertStatus
from ..registry import Registry


class AlertSource(Protocol):
    """Fetches the current alert status of one configured region.

    Raise an exception when the data cannot be fetched or parsed. The daemon
    turns failures and stale data into the "unknown" scene, never into "clear".
    """

    name: str

    def fetch(self) -> AlertStatus: ...


SOURCES: Registry[AlertSource] = Registry("alert source")
