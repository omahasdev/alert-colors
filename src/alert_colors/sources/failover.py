"""Failover: read a fallback source when the primary source has no data.

The primary source is `[source]`. `[failover]` lists fallback sources in order,
each with the keys of `[source]`. Without `[failover]`, the app reads only the
primary source.

Rules:
- The primary source is always the first choice.
- A fallback is used only when the primary source had no success for `after_s`.
  An HTTP 429 or one timeout does not switch: the daemon keeps the last
  primary status. A switch on each 429 would change the scene often, because
  a fallback can have a different level (oblast) than the primary (community).
- After a failure that is not HTTP 429, a source is skipped for `retry_s`,
  doubled after each failure in a row, up to `retry_max_s`. Thus a primary
  source that times out does not delay each poll by its timeout.
- A source is fetched at most once each `poll_interval_s` of its entry (also
  the first source: a token service can allow few requests). Between fetches,
  its last status is given again.
- A refused token (HTTP 401 or 403) is not a short outage: that source is
  skipped for `retry_max_s`, and the next source is used at once.
- No network (specs/001-offline-recovery): when no source gave data, at least
  one failed because the request never left this PC (see `http.no_network`),
  and each other one failed so or timed out, the round is offline. Then a
  source with a "no network" error is not skipped, and its failure count does
  not grow: the offline wait of the daemon (at most `offline_retry_s`) paces
  its tries. A source that timed out keeps the doubled skip: a server can be
  slow. In an offline round, a repeated "no network" or timeout of a source is
  not logged again: the daemon logs the outage. While the last round was offline, a round sends
  one request only: a failed lookup can take 10 s, and a full round would
  delay the fetch after the network is back. `offline_retry_s = 0` turns
  this off.
- `retry_now()` (a network change) clears the skips of the failed sources at
  the next fetch. HTTP 429 and refused-token skips stay.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from ..domain import AlertStatus
from ..http import RateLimitedError, no_network, timed_out, token_refused
from .base import AlertSource

log = logging.getLogger(__name__)

RETRY_FACTOR = 2  # the skip time doubles after each failure in a row
MAX_RETRY_DOUBLINGS = 32  # far more than retry_max_s needs: no float overflow after a long outage

# The kind of the last failure of a member.
NO_NETWORK = "no network"  # the request never left this PC
TIMEOUT = "timeout"
OTHER = "other"
RATE_LIMITED = "rate limited"  # HTTP 429: the skip of the server stays
REFUSED = "refused"  # HTTP 401 or 403: the long skip stays
OFFLINE_KINDS = (NO_NETWORK, TIMEOUT)  # failures that agree with "this PC has no network"
CLEARED_BY_NETWORK_CHANGE = (NO_NETWORK, TIMEOUT, OTHER)
NEVER = float("-inf")  # last_try of a source that was not fetched yet


class AllSourcesFailedError(OSError):
    """No source gave a status. `retry_after_s` is the primary source's hint.
    `no_network`: the round was offline (see the module rules).
    `rate_limited`: a source of the round is in its HTTP 429 wait."""

    def __init__(self, message: str, retry_after_s: float | None = None, no_network: bool = False,
                 rate_limited: bool = False) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s
        self.no_network = no_network
        self.rate_limited = rate_limited


@dataclass
class Member:
    source: AlertSource
    poll_interval_s: float = 0.0  # fetch at most this often; 0: at each poll of the daemon
    last_ok: float | None = None  # monotonic
    cached: AlertStatus | None = None
    failures: int = 0
    skip_until: float = 0.0
    refused: bool = False  # the server refused the token
    kind: str | None = None  # the kind of the last failure; None after a success
    last_try: float = NEVER  # monotonic: the last fetch of this source

    def fresh(self, now: float) -> AlertStatus | None:
        """The last status, while it is younger than poll_interval_s."""
        if self.cached is not None and self.last_ok is not None and now - self.last_ok < self.poll_interval_s:
            return self.cached
        return None

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
    offline_retry_s: float = 0.0  # 0: no short skip when this PC has no network
    monotonic: Callable[[], float] = time.monotonic
    name: str = field(default="", init=False)
    _started: float = field(default=0.0, init=False)
    _active: str = field(default="", init=False)
    _retry_requested: bool = field(default=False, init=False)
    _offline: bool = field(default=False, init=False)  # the last round was offline

    def __post_init__(self) -> None:
        self.name = " > ".join(m.name for m in self.members)
        self._started = self.monotonic()
        self._active = self.primary.name

    @property
    def members(self) -> list[Member]:
        return [self.primary, *self.fallbacks]

    def retry_now(self) -> None:
        """The network changed: at the next fetch, try the failed sources again.
        Another thread can call it: the fetch thread does the change."""
        self._retry_requested = True

    def fetch(self) -> AlertStatus:
        now = self.monotonic()
        if self._retry_requested:
            self._retry_requested = False
            self._clear_skips()
        errors: list[str] = []
        failed: list[tuple[Member, Exception, str | None]] = []  # (member, error, kind before)
        primary_error: Exception | None = None
        if (fresh := self.primary.fresh(now)) is not None:
            return fresh
        # Still offline: one request for each round, to the due source that was
        # tried least recently. A failed lookup can take 10 s, so a full round
        # would delay the fetch after the network is back, and the turn of the
        # sources makes sure that a fallback is tried when only the primary fails.
        pick = self._offline_pick(now) if self._offline else None
        waived: list[Member] = []  # skipped for the turn of another source in this round
        if now >= self.primary.skip_until and pick in (None, self.primary):
            self.primary.last_try = now
            try:
                return self._success(self.primary, self.primary.source.fetch(), now)
            except Exception as error:  # noqa: BLE001  any failure: maybe use a fallback
                primary_error = error
                failed.append((self.primary, error, self._failure(self.primary, error, now)))
                errors.append(f"{self.primary.name}: {error}")
                if self.primary.kind not in OFFLINE_KINDS:
                    pick = None  # a server answered: the network works, so the round goes on
        elif now >= self.primary.skip_until:
            waived.append(self.primary)
            errors.append(f"{self.primary.name}: not tried: no network, another source has its turn")
        else:
            errors.append(f"{self.primary.name}: skipped after an error")
        since_ok = now - (self.primary.last_ok if self.primary.last_ok is not None else self._started)
        if since_ok < self.after_s and not self.primary.refused:
            # Too early to switch: the daemon keeps the last status of the primary
            # and logs the error.
            offline = self._end_round(failed, [self.primary], now, log_primary=False)
            if primary_error is not None:
                if isinstance(primary_error, RateLimitedError) and self.fallbacks and primary_error.retry_after_s:
                    # The primary keeps its wait (its skip). The daemon polls again when the
                    # fallbacks may take over, not after the whole wait of the primary.
                    wait_s = min(primary_error.retry_after_s, self.after_s - since_ok)
                    raise RateLimitedError(wait_s) from primary_error
                raise primary_error
            raise AllSourcesFailedError(errors[0], no_network=offline)
        for member in self.fallbacks:
            if (fresh := member.fresh(now)) is not None:
                self._end_round(failed, None, now)
                return self._use(member, fresh, since_ok)
            if now < member.skip_until:
                errors.append(f"{member.name}: skipped after an error")
                continue
            if pick is not None and member is not pick:
                waived.append(member)
                errors.append(f"{member.name}: not tried: no network, another source has its turn")
                continue
            member.last_try = now
            try:
                status = self._success(member, member.source.fetch(), now)
            except Exception as error:  # noqa: BLE001  try the next fallback
                failed.append((member, error, self._failure(member, error, now)))
                errors.append(f"{member.name}: {error}")
                if member.kind not in OFFLINE_KINDS:
                    pick = None  # a server answered: the network works, so the round goes on
                continue
            self._end_round(failed, None, now)
            return self._use(member, status, since_ok)
        if pick is None:
            # A server answered in this round, so the network works: the sources that were
            # skipped for the turn get their try now, the primary first.
            for member in waived:
                member.last_try = now
                try:
                    status = self._success(member, member.source.fetch(), now)
                except Exception as error:  # noqa: BLE001  try the next one
                    if member is self.primary:
                        primary_error = error
                    failed.append((member, error, self._failure(member, error, now)))
                    errors.append(f"{member.name}: {error}")
                    continue
                if member is self.primary:
                    self._end_round(failed, None, now)
                    return status
                self._end_round(failed, None, now)
                return self._use(member, status, since_ok)
        offline = self._end_round(failed, self.members, now)
        raise AllSourcesFailedError("; ".join(errors), getattr(primary_error, "retry_after_s", None),
                                    no_network=offline,
                                    rate_limited=any(m.kind == RATE_LIMITED for m in self.members))

    def _offline_pick(self, now: float) -> Member | None:
        """The due source that was fetched least recently (the primary first on a tie)."""
        due = [m for m in self.members if now >= m.skip_until and not m.refused]
        return min(due, key=lambda m: m.last_try) if due else None

    def _is_offline(self, considered: Sequence[Member]) -> bool:
        """No data from `considered`, and their failures agree with "no network"."""
        counted = [m for m in considered if not m.refused]
        return (self.offline_retry_s > 0 and bool(counted)
                and all(m.kind in OFFLINE_KINDS for m in counted)
                and any(m.kind == NO_NETWORK for m in counted))

    def _end_round(self, failed: list[tuple[Member, Exception, str | None]],
                   considered: Sequence[Member] | None, now: float, log_primary: bool = True) -> bool:
        """Apply the offline rule, then log the failures. Returns True for an offline round.
        `considered`: the members without data in this round; None when a member gave data."""
        offline = considered is not None and self._is_offline(considered)
        if considered is None or len(considered) == len(self.members):
            # Only a round of all sources says "offline". A round of the primary
            # alone must not stop the fallbacks when only its host fails.
            self._offline = offline
        if offline:
            attempted = [member for member, _error, _kind in failed]
            for member in considered:
                if member.kind != NO_NETWORK:
                    continue
                if any(member is other for other in attempted):
                    member.failures -= 1  # no doubling: no server got the request
                # No skip: the daemon waits at most offline_retry_s between polls, and a
                # skip of its own would make the tries slower than that wait.
                member.skip_until = min(member.skip_until, now)
        for member, error, kind_before in failed:
            if member.kind in (RATE_LIMITED, REFUSED):
                continue  # _failure() logs a refused token; a 429 is normal
            if offline and member.kind == kind_before and member.kind in OFFLINE_KINDS:
                # The same outage: the daemon logs its start, each 5 min, and its end. Out of
                # an offline round (for example only the primary host is dead and a fallback
                # works), each failure is logged.
                continue
            if member is self.primary and not log_primary:
                continue  # the daemon logs this error of the primary
            log.warning("source %s failed: %s (next try in %.0f s)", member.name, error,
                        member.skip_until - now)
        return offline

    def _clear_skips(self) -> None:
        self._offline = False  # the network changed: the next round tries every source
        for member in self.members:
            if member.kind in CLEARED_BY_NETWORK_CHANGE:
                member.skip_until = 0.0  # the failure count stays: a slow server is not asked more often

    def _success(self, member: Member, status: AlertStatus, now: float) -> AlertStatus:
        member.last_ok, member.cached, member.failures, member.skip_until = now, status, 0, 0.0
        member.refused, member.kind = False, None
        self._offline = False  # a source answered: the network works
        if member is self.primary and self._active != member.name:
            log.info("source %s works again: fallback %s is no longer used", member.name, self._active)
            self._active = member.name
        return status

    def _use(self, member: Member, status: AlertStatus, since_ok: float) -> AlertStatus:
        if self._active != member.name:
            reason = "its token was refused" if self.primary.refused else f"no data for {since_ok:.0f} s"
            log.info("source %s: %s: using fallback %s", self.primary.name, reason, member.name)
            self._active = member.name
        return status

    def _failure(self, member: Member, error: Exception, now: float) -> str | None:
        """Record a failure and set its skip. Returns the kind of the last failure before it."""
        kind_before = member.kind
        if isinstance(error, RateLimitedError):
            member.kind = RATE_LIMITED
            member.skip_until = now + (error.retry_after_s or 0.0)  # the server works: no long skip
            return kind_before
        if token_refused(error):
            if not member.refused:
                log.error("source %s: the server refused the token (%s); next try in %.0f s. "
                          "Save the token again with setup.cmd token", member.name, error, self.retry_max_s)
            member.kind = REFUSED
            member.refused, member.skip_until = True, now + self.retry_max_s
            return kind_before
        member.kind = NO_NETWORK if no_network(error) else TIMEOUT if timed_out(error) else OTHER
        member.failures += 1
        skip = min(self.retry_s * RETRY_FACTOR ** min(member.failures - 1, MAX_RETRY_DOUBLINGS), self.retry_max_s)
        member.skip_until = now + skip
        return kind_before
