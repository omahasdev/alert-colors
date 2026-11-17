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

import datetime
import errno
import functools
import hashlib
import http.client
import ipaddress
import logging
import json
import math
import socket
import ssl
import threading
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

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


class NameResolutionError(OSError):
    """DNS gave no address for the host. Often, this PC has no network."""


class NetworkChangedError(OSError):
    """A request was stopped so the caller retries at once after the network changed."""


# The network of this PC is down or has no route: the request did not leave the PC.
_NO_ROUTE_ERRNOS = frozenset({errno.ENETDOWN, errno.ENETUNREACH, errno.EHOSTUNREACH})


def _chain(error: BaseException | None) -> list[BaseException]:
    """The error, the reason of a URLError, and the causes, without loops."""
    found: list[BaseException] = []
    while isinstance(error, BaseException) and all(error is not seen for seen in found):
        found.append(error)
        reason = getattr(error, "reason", None) if isinstance(error, urllib.error.URLError) else None
        error = reason if isinstance(reason, BaseException) else error.__cause__
    return found


def no_network(error: BaseException) -> bool:
    """True when the request never left this PC: the host name did not resolve,
    or the network is down or unreachable. No server got the request, so a quick
    retry puts no load on a server. A timeout or a refused connection is not in
    this group: a server can be slow or down."""
    for item in _chain(error):
        if isinstance(item, (NameResolutionError, socket.gaierror)):
            return True
        if isinstance(item, OSError) and not isinstance(item, urllib.error.HTTPError) and (
                item.errno in _NO_ROUTE_ERRNOS or getattr(item, "winerror", None) in _NO_ROUTE_ERRNOS):
            return True
    return False


def timed_out(error: BaseException) -> bool:
    """True when the request got no answer in its time limit."""
    return any(isinstance(item, TimeoutError) for item in _chain(error))


def _retry_after_s(error: urllib.error.HTTPError) -> float | None:
    value = error.headers.get("Retry-After") if error.headers else None
    try:
        seconds = float(value) if value is not None else None
    except ValueError:
        return None  # an HTTP date: use the normal backoff
    if seconds is None or not math.isfinite(seconds) or seconds < 0:
        return None  # not a usable wait: use the normal backoff
    return min(seconds, MAX_RETRY_AFTER_S)  # a server never stops the polling for long


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


SERVER_AUTH_OID = "1.3.6.1.5.5.7.3.1"
# ROOT, AuthRoot: the roots. CA: intermediates, for a server that does not send its own.
# Everything in cadata is trusted, so a self-signed certificate of CA (which Windows
# does not treat as a root) is not used.
MACHINE_ROOT_STORES = ("ROOT", "AuthRoot", "CA")
INTERMEDIATE_STORE = "CA"
DISALLOWED_STORE = "Disallowed"  # certificates that an admin or Microsoft distrusts
CERT_STORE_PROV_SYSTEM = 10
CERT_SYSTEM_STORE_LOCAL_MACHINE = 0x00020000
CERT_STORE_READONLY_FLAG = 0x00008000
CERT_FIND_PROP_ONLY_ENHKEY_USAGE_FLAG = 0x4
CRYPT_E_NOT_FOUND = -2146885628  # the certificate limits no usage
# Distrust marks that Windows Update puts on a root, each with the usages that it applies
# to (all usages when the usage mark is not set):
# - "disallowed": from this date on, the root itself is not trusted;
# - "not before": certificates that the CA issued after this date are not trusted.
# OpenSSL cannot check the issue date against the root, so both mean: after the date,
# the root is not used. A certificate from before a "not before" date then fails, also
# though Windows still accepts it.
CERT_DISALLOWED_FILETIME_PROP_ID = 104
CERT_DISALLOWED_ENHKEY_USAGE_PROP_ID = 122
CERT_NOT_BEFORE_FILETIME_PROP_ID = 126
CERT_NOT_BEFORE_ENHKEY_USAGE_PROP_ID = 127
FILETIME_EPOCH = datetime.datetime(1601, 1, 1, tzinfo=datetime.timezone.utc)
FILETIME_TICKS_PER_MICROSECOND = 10
FILETIME_BYTES = 8
SERVER_AUTH_OID_DER = bytes.fromhex("2b06010505070301")  # the content of the SERVER_AUTH_OID object
DER_SEQUENCE, DER_OID, DER_LONG_LENGTH = 0x30, 0x06, 0x80


def _der_items(data: bytes) -> list[tuple[int, bytes]]:
    """The (tag, content) items of DER data. ValueError for data that is not DER."""
    items, at = [], 0
    while at < len(data):
        if at + 2 > len(data):
            raise ValueError("cut DER item")
        tag, length = data[at], data[at + 1]
        at += 2
        if length & DER_LONG_LENGTH:
            count = length & ~DER_LONG_LENGTH
            if not 0 < count <= 4 or at + count > len(data):
                raise ValueError("bad DER length")
            length, at = int.from_bytes(data[at:at + count], "big"), at + count
        if at + length > len(data):
            raise ValueError("cut DER item")
        items.append((tag, data[at:at + length]))
        at += length
    return items


def _limits_server_auth(encoded_usages: bytes | None) -> bool:
    """True when a distrust mark applies to server authentication. Data that does not
    parse counts as a distrust of all usages."""
    if encoded_usages is None:
        return True  # no usage list: the mark applies to all usages
    try:
        (tag, content), = _der_items(encoded_usages)
        if tag != DER_SEQUENCE:
            raise ValueError("not a sequence")
        oids = _der_items(content)
    except ValueError:
        return True
    return any(tag != DER_OID or value == SERVER_AUTH_OID_DER for tag, value in oids)


def _certificate_property(certificate: object, prop_id: int) -> bytes | None:
    """A property of a certificate in a Windows store (pywin32 cannot read these two)."""
    import ctypes
    from ctypes import wintypes

    get = ctypes.WinDLL("crypt32", use_last_error=True).CertGetCertificateContextProperty
    get.restype = wintypes.BOOL
    get.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)]
    size = wintypes.DWORD(0)
    if not get(certificate.HANDLE, prop_id, None, ctypes.byref(size)):
        return None  # the property is not set
    buffer = ctypes.create_string_buffer(size.value)
    if not get(certificate.HANDLE, prop_id, buffer, ctypes.byref(size)):
        return None
    return buffer.raw[:size.value]


def windows_distrusts(read_property: Any, now: datetime.datetime) -> bool:
    """True when Windows distrusts new server certificates of this root by now.

    `read_property(prop_id)` gives the property bytes or None. A date in the future
    keeps the root: Windows still trusts it today."""
    return (_mark_applies(read_property, CERT_DISALLOWED_FILETIME_PROP_ID, CERT_DISALLOWED_ENHKEY_USAGE_PROP_ID,
                          now, usages_alone_distrust=True)
            or _mark_applies(read_property, CERT_NOT_BEFORE_FILETIME_PROP_ID, CERT_NOT_BEFORE_ENHKEY_USAGE_PROP_ID,
                             now, usages_alone_distrust=False))


def _mark_applies(read_property: Any, date_id: int, usages_id: int, now: datetime.datetime,
                  usages_alone_distrust: bool) -> bool:
    usages = read_property(usages_id)
    if not _limits_server_auth(usages):
        return False
    filetime = read_property(date_id)
    if filetime is None:
        # "disallowed" usages without a date: not trusted for them at all. A "not before"
        # mark needs its date.
        return usages_alone_distrust and usages is not None
    if len(filetime) < FILETIME_BYTES:
        return True
    ticks = int.from_bytes(filetime[:FILETIME_BYTES], "little")
    try:
        since = FILETIME_EPOCH + datetime.timedelta(microseconds=ticks // FILETIME_TICKS_PER_MICROSECOND)
    except OverflowError:  # after year 9999: not yet
        return False
    return since <= now


def machine_root_certificates() -> list[bytes]:
    """The DER root certificates of this PC (LocalMachine stores) for server authentication.

    The default context of Python also trusts the CurrentUser stores, and a process
    of the user can add a certificate there without admin rights. The app runs as
    admin, so a server certificate must chain to a root that an admin installed.
    """
    import pywintypes
    import win32crypt

    def machine_store(name: str) -> object:
        return win32crypt.CertOpenStore(CERT_STORE_PROV_SYSTEM, 0, None,
                                        CERT_SYSTEM_STORE_LOCAL_MACHINE | CERT_STORE_READONLY_FLAG, name)

    distrusted = {hashlib.sha1(c.CertEncoded).digest()  # noqa: S324  the Windows thumbprint is SHA-1
                  for c in machine_store(DISALLOWED_STORE).CertEnumCertificatesInStore()}
    roots = []
    now = datetime.datetime.now(datetime.timezone.utc)
    for name in MACHINE_ROOT_STORES:
        store = machine_store(name)
        for certificate in store.CertEnumCertificatesInStore():
            if hashlib.sha1(certificate.CertEncoded).digest() in distrusted:  # noqa: S324
                continue
            if name == INTERMEDIATE_STORE and certificate.Subject == certificate.Issuer:
                continue  # self-signed: a root in a store of intermediates
            if windows_distrusts(lambda prop_id, c=certificate: _certificate_property(c, prop_id), now):
                continue
            try:
                usages = {oid.decode("ascii") if isinstance(oid, bytes) else oid  # win32crypt gives bytes
                          for oid in certificate.CertGetEnhancedKeyUsage(CERT_FIND_PROP_ONLY_ENHKEY_USAGE_FLAG)}
            except pywintypes.error as error:
                if error.winerror != CRYPT_E_NOT_FOUND:
                    continue
                usages = None  # no limit
            if usages is None or SERVER_AUTH_OID in usages:
                roots.append(certificate.CertEncoded)
    return roots


def tls_context() -> ssl.SSLContext:
    """Server certificates must chain to a machine root; no partial chain."""
    if sys.platform != "win32":
        context = ssl.create_default_context()
    else:
        # As create_default_context(), but without its fallback to the default stores
        # (they include the stores of the user). Each certificate loads on its own: one
        # that OpenSSL refuses must not stop every https fetch.
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.verify_mode, context.check_hostname = ssl.CERT_REQUIRED, True
        context.verify_flags |= getattr(ssl, "VERIFY_X509_STRICT", 0)
        loaded, skipped = 0, 0
        for der in machine_root_certificates():
            try:
                context.load_verify_locations(cadata=der)
                loaded += 1
            except ssl.SSLError:
                skipped += 1
        if skipped:
            log.warning("TLS: %d machine certificate(s) not usable by OpenSSL, skipped", skipped)
        if not loaded:
            raise ssl.SSLError("no root certificate in the machine stores of this PC")
    context.verify_flags &= ~getattr(ssl, "VERIFY_X509_PARTIAL_CHAIN", 0)
    return context


class _LazyTLSHandler(urllib.request.HTTPSHandler):
    """HTTPS with tls_context(), made at the first https request, not at import:
    an error there (for example no machine root) then stops only the https
    fetches, with a log line, and never `stop`, `tune` or the other commands."""

    _shared: ssl.SSLContext | None = None

    def __init__(self) -> None:
        # Not HTTPSHandler.__init__(): with no context, it builds Python's default
        # context at once, and that reads the certificate stores of the user.
        urllib.request.AbstractHTTPHandler.__init__(self, http.client.HTTPSConnection.debuglevel)
        self._context = None
        self._check_hostname = None  # read by https_open() of Python 3.11

    def https_open(self, req):  # noqa: ANN001, ANN201
        if _LazyTLSHandler._shared is None:
            _LazyTLSHandler._shared = tls_context()
        self._context = _LazyTLSHandler._shared
        return self.do_open(_DeadlineHTTPSConnection, req, context=self._context)


# Retry-After of HTTP 429: a longer wait comes to this many seconds.
MAX_RETRY_AFTER_S = 3600.0
_OPENER = urllib.request.build_opener(_NoRedirect(), _LazyTLSHandler())
# No proxy: the user can set one without admin rights. Over https it sees only the
# encrypted connection; over http and to loopback it could change the answer.
# The time limit of a whole request, in steps of its socket timeout: timeout_s holds for
# each socket operation, so a server that sends one byte at a time could hold the poller
# for days. When the limit passes, the sockets of the request are shut down.
REQUEST_DEADLINE_FACTOR = 3
_request = threading.local()  # the sockets of the request that runs on this thread
_fast_fail = threading.local()  # a shorter cap on the connect while this PC has no network


def fast_fail(seconds: float | None) -> None:
    """Cap the connect of each request on this thread to `seconds` (None: no cap). The daemon sets
    it while this PC has no network, so a host it cannot reach fails within the retry gap. A host
    that answers keeps the full read time, so a slow-but-working link is not cut short."""
    _fast_fail.seconds = seconds


class _DeadlineSockets:
    """Records the sockets of one request; shut_down() ends a read that is still blocked.

    `expired`: the request deadline passed. `aborted`: a network change asked for an
    at-once retry, so a fetch that hangs now does not wait for its deadline.
    """

    def __init__(self, read_timeout: float | None = None) -> None:
        self.sockets: list[socket.socket] = []
        self.expired = False
        self.aborted = False
        self.read_timeout = read_timeout  # the full read time, restored after connect when the cap is on

    def shut_down(self, aborted: bool = False) -> None:
        if aborted:
            self.aborted = True
        else:
            self.expired = True
        for sock in list(self.sockets):
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass  # closed already


# The in-flight requests, so another thread (the network watcher) can stop a hung fetch.
_active_lock = threading.Lock()
_active_requests: set[_DeadlineSockets] = set()


def abort_active_requests() -> int:
    """Stop every in-flight request so a fetch that blocks now returns at once. Called from
    another thread after the network changes, so the poller retries without the full deadline."""
    with _active_lock:
        active = list(_active_requests)
    for deadline in active:
        deadline.shut_down(aborted=True)
    return len(active)


def _record_socket(sock: socket.socket) -> socket.socket:
    current = getattr(_request, "sockets", None)
    if current is not None:
        current.sockets.append(sock)
        if current.expired:  # the connect used the whole time: no read starts
            current.shut_down()
        elif current.aborted:  # a network change asked to retry now
            current.shut_down(aborted=True)
        elif current.read_timeout is not None:  # connected: reads get the full time, not the connect cap
            sock.settimeout(current.read_timeout)
    return sock


def _record_new_sockets(connection: http.client.HTTPConnection) -> None:
    """Record each socket when the connection makes it: before a proxy tunnel or a TLS
    handshake, which read from the network too."""
    create = connection._create_connection
    connection._create_connection = lambda *args, **kwargs: _record_socket(create(*args, **kwargs))


class _DeadlineHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        _record_new_sockets(self)

    def connect(self) -> None:
        super().connect()
        _record_socket(self.sock)  # the TLS socket: the plain one is detached in the wrap


class _CheckedHTTPConnection(http.client.HTTPConnection):
    """Plain http: the connect resolves the name again, and a DNS answer can differ from
    the one of check_url(). So the address that the socket reached is checked too."""

    def __init__(self, *args: Any, policy: NetworkPolicy, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._policy = policy
        _record_new_sockets(self)

    def connect(self) -> None:
        super().connect()
        peer = self.sock.getpeername()[0]
        try:
            _check_address(ipaddress.ip_address(peer.split("%")[0]), self._policy)
        except UnsafeRequestError:
            self.close()
            raise


class _CheckedHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):  # noqa: ANN001, ANN201
        policy = getattr(req, "network_policy", REMOTE)
        return self.do_open(functools.partial(_CheckedHTTPConnection, policy=policy), req)


_DIRECT_OPENER = urllib.request.build_opener(_NoRedirect(), urllib.request.ProxyHandler({}), _CheckedHTTPHandler(),
                                             _LazyTLSHandler())


def _proxies(opener: urllib.request.OpenerDirector) -> dict[str, str]:
    for handler in opener.handlers:
        if isinstance(handler, urllib.request.ProxyHandler):
            return dict(handler.proxies)
    return {}


def _proxy_carries(url: str, policy: NetworkPolicy) -> bool:
    """True when the proxy, not this PC, resolves the host: an https request to a remote
    server with a proxy set and no bypass for the host."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != HTTPS or _opener_for(url, policy) is not _OPENER or HTTPS not in _proxies(_OPENER):
        return False
    # The same value that ProxyHandler gives to proxy_bypass(): the host with its port.
    return not urllib.request.proxy_bypass(parts.netloc.rpartition("@")[2])


def _opener_for(url: str, policy: NetworkPolicy) -> urllib.request.OpenerDirector:
    plain = urllib.parse.urlsplit(url).scheme == HTTP
    return _DIRECT_OPENER if plain or policy.loopback_only else _OPENER


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
    try:
        literal = ipaddress.ip_address(parts.hostname.split("%")[0])
    except ValueError:
        literal = None
    if literal is not None:
        _check_address(literal, policy)
        return
    if _proxy_carries(url, policy):
        # The proxy resolves the name, so a PC with no outside DNS still works; the TLS
        # check of the host name still holds.
        return
    for address in _resolve(parts.hostname, port):
        _check_address(address, policy)


def _resolve(host: str, port: int) -> set[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as error:
        raise NameResolutionError(f"cannot resolve {host}: {error}") from error
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
    req.network_policy = policy  # for the address check of a plain http connection
    cap = getattr(_fast_fail, "seconds", None)
    # While offline, cap the connect so a host this PC cannot reach fails fast; a host that answers
    # keeps the full read time, so a slow-but-working link is not cut short and a failover does not stall.
    connect_timeout = timeout_s if not cap else min(timeout_s, cap)
    total_deadline = timeout_s * REQUEST_DEADLINE_FACTOR
    deadline = _DeadlineSockets(read_timeout=timeout_s if cap else None)
    timer = threading.Timer(total_deadline, deadline.shut_down)
    timer.daemon = True
    _request.sockets = deadline
    with _active_lock:
        _active_requests.add(deadline)
    timer.start()
    try:
        with _opener_for(url, policy).open(req, timeout=connect_timeout) as response:
            raw = response.read(policy.max_response_bytes + 1)
    except urllib.error.HTTPError as error:
        if error.code == HTTP_TOO_MANY_REQUESTS:
            raise RateLimitedError(_retry_after_s(error)) from error
        raise
    except (OSError, http.client.HTTPException) as error:
        if deadline.aborted:
            raise NetworkChangedError("the request was stopped to retry after the network changed") from error
        if deadline.expired:
            raise TimeoutError(f"no complete answer within {total_deadline:.0f} s") from error
        raise
    finally:
        timer.cancel()
        with _active_lock:
            _active_requests.discard(deadline)
        _request.sockets = None
    if deadline.aborted:  # a late abort after a clean EOF: the body may be truncated, so retry
        raise NetworkChangedError("the request was stopped to retry after the network changed")
    if deadline.expired:
        raise TimeoutError(f"no complete answer within {total_deadline:.0f} s")
    if len(raw) > policy.max_response_bytes:
        raise UnsafeRequestError(f"response is larger than {policy.max_response_bytes} bytes")
    return json.loads(raw) if raw else None


def local_request_json(url: str, **kwargs: Any) -> Any:
    """For SDK servers on this PC: loopback addresses only."""
    return request_json(url, policy=LOOPBACK, **kwargs)
