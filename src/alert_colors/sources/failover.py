"""Failover: read a fallback source when the primary source has no data.

The primary source is `[source]`. `[failover]` lists fallback sources in order,
each with the keys of `[source]`. Without `[failover]`, the app reads only the
primary source, as before.

Rules:
- The primary source is always the first choice.
- A fallback is used only when the primary source had no success for `after_s`.
  An HTTP 429 or one timeout does not switch: the daemon keeps the last
  primary status. A switch on each 429 would change the scene often, because
  a fallback can have a different level (oblast) than the primary (community).
- After a failure that is not HTTP 429, a source is skipped for `retry_s`,
  doubled after each failure in a row, up to `retry_max_s`. Thus a primary
  source that times out does not delay each poll by its timeout.
- A fallback is fetched at most once each `poll_interval_s`. Between fetches,
  its last status is given again.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from ..domain import AlertStatus
from ..http import RateLimitedError
from .base import AlertSource

log = logging.getLogger(__name__)

RETRY_FACTOR = 2  # the skip time doubles after each failure in a row


class AllSourcesFailedError(OSError):
    """No source gave a status. `retry_after_s` is the primary source's hint."""

    def __init__(self, message: str, retry_after_s: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s


@dataclass
class Member:
    source: AlertSource
    poll_interval_s: float = 0.0  # fallbacks: fetch at most this often
    last_ok: float | None = None  # monotonic
    cached: AlertStatus | None = None
    failures: int = 0
    skip_until: float = 0.0

    @property
    def name(self) -> str:
        return self.source.name


@dataclass
class FailoverSource:
    primary: Member
    fallbacks: Sequence[Member]
    after_s: float
    retry_s: float
    retry_max_s: float
    monotonic: Callable[[], float] = time.monotonic
    name: str = field(default="", init=False)
    _started: float = field(default=0.0, init=False)
    _active: str = field(default="", init=False)

    def __post_init__(self) -> None:
        self.name = " > ".join(m.name for m in self.members)
        self._started = self.monotonic()
        self._active = self.primary.name

    @property
    def members(self) -> list[Member]:
        return [self.primary, *self.fallbacks]

    def fetch(self) -> AlertStatus:
        now = self.monotonic()
        errors: list[str] = []
        primary_error: Exception | None = None
        if now >= self.primary.skip_until:
            try:
                return self._success(self.primary, self.primary.source.fetch(), now)
            except Exception as error:  # noqa: BLE001  any failure: maybe use a fallback
                primary_error = error
                self._failure(self.primary, error, now)
                errors.append(f"{self.primary.name}: {error}")
        else:
            errors.append(f"{self.primary.name}: skipped after an error")
        since_ok = now - (self.primary.last_ok if self.primary.last_ok is not None else self._started)
        if since_ok < self.after_s:
            # Too early to switch: the daemon keeps the last status of the primary
            # and logs the error.
            if primary_error is not None:
                raise primary_error
            raise AllSourcesFailedError(errors[0])
        if primary_error is not None and not isinstance(primary_error, RateLimitedError):
            log.warning("source %s failed: %s (next try in %.0f s)", self.primary.name, primary_error,
                        self.primary.skip_until - now)
        for member in self.fallbacks:
            if member.cached is not None and member.last_ok is not None \
                    and now - member.last_ok < member.poll_interval_s:
                return self._use(member, member.cached, since_ok)
            if now < member.skip_until:
                errors.append(f"{member.name}: skipped after an error")
                continue
            try:
                return self._use(member, self._success(member, member.source.fetch(), now), since_ok)
            except Exception as error:  # noqa: BLE001  try the next fallback
                self._failure(member, error, now)
                errors.append(f"{member.name}: {error}")
        raise AllSourcesFailedError("; ".join(errors), getattr(primary_error, "retry_after_s", None))

    def _success(self, member: Member, status: AlertStatus, now: float) -> AlertStatus:
        member.last_ok, member.cached, member.failures, member.skip_until = now, status, 0, 0.0
        if member is self.primary and self._active != member.name:
            log.info("source %s works again: fallback %s is no longer used", member.name, self._active)
            self._active = member.name
        return status

    def _use(self, member: Member, status: AlertStatus, since_ok: float) -> AlertStatus:
        if self._active != member.name:
            log.info("source %s: no data for %.0f s: using fallback %s",
                     self.primary.name, since_ok, member.name)
            self._active = member.name
        return status

    def _failure(self, member: Member, error: Exception, now: float) -> None:
        if isinstance(error, RateLimitedError):
            member.skip_until = now + (error.retry_after_s or 0.0)  # the server works: no long skip
            return
        member.failures += 1
        skip = min(self.retry_s * RETRY_FACTOR ** (member.failures - 1), self.retry_max_s)
        member.skip_until = now + skip
        if member is not self.primary:  # fetch() logs the primary
            log.warning("source %s failed: %s (next try in %.0f s)", member.name, error, skip)
