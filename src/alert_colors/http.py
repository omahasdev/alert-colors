"""JSON-over-HTTP client with the limits of a process that runs as admin.

The app runs elevated, and the response comes from the network. So:
- Only https (http only with `allow_http`): no file://, ftp:// or data: URLs.
- No redirects: a hacked or hostile server cannot send the request elsewhere.
- Hosts that resolve to loopback, private, link-local or reserved addresses
  are refused, unless `allow_private_network`. This stops requests to local
  services (for example, the Razer SDK on localhost) through a changed config.
  Local backends use `local_request_json`, which allows only loopback.
- The response body has a size limit, and the TLS certificate is always checked.
"""

from __future__ import annotations

import ipaddress
import json
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

USER_AGENT = "alert-colors/0.2"
HTTPS = "https"
HTTP = "http"
DEFAULT_MAX_RESPONSE_BYTES = 1_048_576
DEFAULT_PORTS = {HTTPS: 443, HTTP: 80}


class UnsafeRequestError(ValueError):
    """The request breaks a network rule (scheme, address, redirect, size)."""


HTTP_TOO_MANY_REQUESTS = 429


TOKEN_REFUSED = (401, 403)  # HTTP Unauthorized, Forbidden: the token is wrong or blocked


def token_refused(error: BaseException) -> bool:
    """True when the server refused the token: a config error, not a network error."""
    return getattr(error, "code", None) in TOKEN_REFUSED


class RateLimitedError(OSError):
    """HTTP 429. `retry_after_s` is the server's Retry-After, when it gives seconds."""

    def __init__(self, retry_after_s: float | None) -> None:
        super().__init__(f"HTTP 429 Too Many Requests (Retry-After: {retry_after_s})")
        self.retry_after_s = retry_after_s


def _retry_after_s(error: urllib.error.HTTPError) -> float | None:
    value = error.headers.get("Retry-After") if error.headers else None
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None  # an HTTP date: use the normal backoff


@dataclass(frozen=True)
class NetworkPolicy:
    allow_http: bool = False
    allow_private_network: bool = False
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES
    loopback_only: bool = False  # for local SDK servers


REMOTE = NetworkPolicy()
LOOPBACK = NetworkPolicy(allow_http=True, allow_private_network=True, loopback_only=True)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, D102
        raise UnsafeRequestError(f"redirect refused: HTTP {code} to {newurl}")


_OPENER = urllib.request.build_opener(
    _NoRedirect(), urllib.request.HTTPSHandler(context=ssl.create_default_context())
)


def check_url(url: str, policy: NetworkPolicy) -> None:
    parts = urllib.parse.urlsplit(url)
    allowed = {HTTPS, HTTP} if policy.allow_http else {HTTPS}
    if parts.scheme not in allowed:
        raise UnsafeRequestError(f"scheme {parts.scheme!r} refused; allowed: {sorted(allowed)}")
    if not parts.hostname:
        raise UnsafeRequestError(f"URL has no host: {url}")
    if parts.username or parts.password:
        raise UnsafeRequestError("credentials in the URL are refused; use a header")
    port = parts.port or DEFAULT_PORTS[parts.scheme]
    for address in _resolve(parts.hostname, port):
        _check_address(address, policy)


def _resolve(host: str, port: int) -> set[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as error:
        raise OSError(f"cannot resolve {host}: {error}") from error
    return {ipaddress.ip_address(info[4][0].split("%")[0]) for info in infos}


def _check_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address, policy: NetworkPolicy) -> None:
    if policy.loopback_only:
        if not address.is_loopback:
            raise UnsafeRequestError(f"{address} is not a loopback address")
        return
    local = (address.is_loopback or address.is_private or address.is_link_local
             or address.is_reserved or address.is_multicast or address.is_unspecified)
    if local and not policy.allow_private_network:
        raise UnsafeRequestError(f"{address} is a local or private address; set allow_private_network")


def request_json(
    url: str,
    *,
    method: str = "GET",
    body: Any = None,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    timeout_s: float,
    policy: NetworkPolicy = REMOTE,
) -> Any:
    if params:
        separator = "&" if urllib.parse.urlsplit(url).query else "?"
        url = url + separator + urllib.parse.urlencode(params, doseq=True)
    check_url(url, policy)
    data = None if body is None else json.dumps(body).encode("utf-8")
    all_headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if data is not None:
        all_headers["Content-Type"] = "application/json"
    all_headers.update(headers or {})
    req = urllib.request.Request(url, data=data, method=method, headers=all_headers)
    try:
        with _OPENER.open(req, timeout=timeout_s) as response:
            raw = response.read(policy.max_response_bytes + 1)
    except urllib.error.HTTPError as error:
        if error.code == HTTP_TOO_MANY_REQUESTS:
            raise RateLimitedError(_retry_after_s(error)) from error
        raise
    if len(raw) > policy.max_response_bytes:
        raise UnsafeRequestError(f"response is larger than {policy.max_response_bytes} bytes")
    return json.loads(raw) if raw else None


def local_request_json(url: str, **kwargs: Any) -> Any:
    """For SDK servers on this PC: loopback addresses only."""
    return request_json(url, policy=LOOPBACK, **kwargs)
