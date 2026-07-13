"""Bounded, policy-aware public HTTP probes used by source preflight."""

from __future__ import annotations

import gzip
import hashlib
import http.client
import io
import ipaddress
import socket
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import Message
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import (
    HTTPRedirectHandler,
    HTTPSHandler,
    ProxyHandler,
    Request,
    build_opener,
)

USER_AGENT = "govuk-okf/0.1 (+https://github.com/chris-page-gov/okf-govuk-content)"
MAX_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class Probe:
    id: str
    url: str
    family: str
    partial: bool = False
    max_bytes: int = MAX_BYTES
    allowed_hosts: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.allowed_hosts:
            raise ValueError("probe requires at least one approved host")
        if self.max_bytes < 1:
            raise ValueError("probe byte limit must be positive")


def _normalise_host(value: str) -> str:
    try:
        return value.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ValueError(f"probe hostname is invalid: {value}") from exc


def _approved_address_infos(
    host: str,
    port: int,
    *,
    allowed_hosts: tuple[str, ...],
    resolver: Any,
) -> tuple[str, tuple[tuple[Any, ...], ...]]:
    """Resolve one approved host and retain only its complete public answer."""

    host = _normalise_host(host)
    approved = {_normalise_host(value) for value in allowed_hosts}
    if host not in approved:
        raise ValueError(f"probe host is not approved: {host}")
    try:
        answers = resolver(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ValueError(f"probe host cannot be resolved: {host}") from exc
    if not answers:
        raise ValueError(f"probe host has no resolved address: {host}")
    validated: list[tuple[Any, ...]] = []
    seen: set[tuple[Any, ...]] = set()
    for answer in answers:
        if not answer or len(answer) < 5 or not answer[4]:
            raise ValueError(f"probe host returned an invalid address record: {host}")
        family, socktype, protocol, canonical_name, socket_address = answer
        address = str(socket_address[0]).split("%", 1)[0]
        try:
            parsed_address = ipaddress.ip_address(address)
        except ValueError as exc:
            raise ValueError(f"probe host returned an invalid address: {address}") from exc
        if not parsed_address.is_global:
            raise ValueError(f"probe destination is not public: {address}")
        key = (family, socktype, protocol, socket_address)
        if key not in seen:
            validated.append((family, socktype, protocol, canonical_name, socket_address))
            seen.add(key)
    return host, tuple(validated)


def validate_public_https_url(
    url: str,
    *,
    allowed_hosts: tuple[str, ...],
    resolver: Any = socket.getaddrinfo,
) -> str:
    """Require one approved HTTPS origin whose complete DNS answer is public."""

    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("probe URL must be credential-free HTTPS")
    try:
        port = parsed.port or 443
    except ValueError as exc:
        raise ValueError("probe URL has an invalid port") from exc
    if port != 443:
        raise ValueError("probe URL must use HTTPS port 443")
    host, _ = _approved_address_infos(
        parsed.hostname,
        port,
        allowed_hosts=allowed_hosts,
        resolver=resolver,
    )
    return host


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Connect TLS to the exact public DNS answer that passed policy checks."""

    def __init__(
        self,
        host: str,
        *,
        allowed_hosts: tuple[str, ...],
        resolver: Any = socket.getaddrinfo,
        socket_factory: Any = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(host, **kwargs)
        self.allowed_hosts = allowed_hosts
        self.resolver = resolver
        self.socket_factory = socket_factory or socket.socket

    def connect(self) -> None:
        if self._tunnel_host:
            raise ValueError("probe connections do not support HTTP proxies")
        host, answers = _approved_address_infos(
            self.host,
            self.port,
            allowed_hosts=self.allowed_hosts,
            resolver=self.resolver,
        )
        last_error: OSError | None = None
        for family, socktype, protocol, _canonical_name, socket_address in answers:
            raw_socket = self.socket_factory(family, socktype, protocol)
            try:
                raw_socket.settimeout(self.timeout)
                if self.source_address:
                    raw_socket.bind(self.source_address)
                raw_socket.connect(socket_address)
                try:
                    raw_socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                except OSError:
                    pass
                self.sock = self._context.wrap_socket(raw_socket, server_hostname=host)
                return
            except OSError as exc:
                last_error = exc
                raw_socket.close()
        if last_error is not None:
            raise last_error
        raise OSError(f"probe host has no usable public address: {host}")


class PolicyHTTPSHandler(HTTPSHandler):
    """urllib HTTPS handler whose connection reuses the approved DNS answer."""

    def __init__(
        self,
        *,
        allowed_hosts: tuple[str, ...],
        resolver: Any = socket.getaddrinfo,
    ) -> None:
        super().__init__()
        self.allowed_hosts = allowed_hosts
        self.resolver = resolver

    def https_open(self, request: Request) -> Any:
        def connection(host: str, **kwargs: Any) -> PinnedHTTPSConnection:
            return PinnedHTTPSConnection(
                host,
                allowed_hosts=self.allowed_hosts,
                resolver=self.resolver,
                **kwargs,
            )

        return self.do_open(connection, request, context=self._context)


class PolicyRedirectHandler(HTTPRedirectHandler):
    """Revalidate every redirect before urllib can perform the next request."""

    def __init__(
        self,
        *,
        allowed_hosts: tuple[str, ...],
        resolver: Any = socket.getaddrinfo,
        max_redirects: int = 5,
    ) -> None:
        super().__init__()
        self.allowed_hosts = allowed_hosts
        self.resolver = resolver
        self.max_redirects = max_redirects
        self.redirects = 0

    def redirect_request(
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Message,
        newurl: str,
    ) -> Request | None:
        if self.redirects >= self.max_redirects:
            raise HTTPError(req.full_url, code, "probe redirect limit exceeded", headers, fp)
        target = urljoin(req.full_url, newurl)
        validate_public_https_url(target, allowed_hosts=self.allowed_hosts, resolver=self.resolver)
        self.redirects += 1
        return super().redirect_request(req, fp, code, msg, headers, target)


def _headers(message: Message) -> dict[str, str]:
    allowed = {
        "content-type",
        "content-length",
        "content-encoding",
        "cache-control",
        "etag",
        "last-modified",
        "retry-after",
        "x-ratelimit-limit",
        "x-ratelimit-remaining",
        "ratelimit-limit",
        "ratelimit-remaining",
    }
    return {key.lower(): value for key, value in message.items() if key.lower() in allowed}


def _bounded_gzip_decompress(raw: bytes, limit: int) -> tuple[bytes, bool]:
    """Decompress no more than limit bytes from an untrusted gzip response."""
    with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
        value = stream.read(limit + 1)
    return value[:limit], len(value) > limit


def fetch_probe(
    probe: Probe,
    attempts: int = 3,
    *,
    resolver: Any = socket.getaddrinfo,
) -> dict[str, Any]:
    allowed_hosts = tuple(dict.fromkeys(probe.allowed_hosts))
    headers = {"User-Agent": USER_AGENT, "Accept": "*/*", "Accept-Encoding": "gzip"}
    if probe.partial:
        headers["Range"] = f"bytes=0-{probe.max_bytes - 1}"
    request = Request(probe.url, headers=headers)
    last_error = ""
    for attempt in range(1, attempts + 1):
        started = time.monotonic()
        try:
            validate_public_https_url(
                probe.url,
                allowed_hosts=allowed_hosts,
                resolver=resolver,
            )
            redirect_handler = PolicyRedirectHandler(
                allowed_hosts=allowed_hosts,
                resolver=resolver,
            )
            https_handler = PolicyHTTPSHandler(
                allowed_hosts=allowed_hosts,
                resolver=resolver,
            )
            opener = build_opener(ProxyHandler({}), https_handler, redirect_handler)
            with opener.open(request, timeout=30) as response:
                raw = response.read(probe.max_bytes + 1)
                truncated = len(raw) > probe.max_bytes
                raw = raw[: probe.max_bytes]
                if response.headers.get("Content-Encoding", "").lower() == "gzip":
                    try:
                        raw, decompressed_truncated = _bounded_gzip_decompress(raw, probe.max_bytes)
                        truncated = truncated or decompressed_truncated
                    except (gzip.BadGzipFile, EOFError, OSError):
                        pass
                return {
                    "id": probe.id,
                    "family": probe.family,
                    "requested_url": probe.url,
                    "final_url": response.geturl(),
                    "status": response.status,
                    "ok": 200 <= response.status < 400,
                    "retrieved_at": datetime.now(timezone.utc).isoformat(),
                    "elapsed_ms": round((time.monotonic() - started) * 1000),
                    "headers": _headers(response.headers),
                    "bytes_retained": len(raw),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "partial": probe.partial or truncated or response.status == 206,
                    "body": raw,
                    "attempts": attempt,
                }
        except HTTPError as exc:
            last_error = f"HTTP {exc.code}"
            if exc.code not in {408, 429, 500, 502, 503, 504} or attempt == attempts:
                return {
                    "id": probe.id,
                    "family": probe.family,
                    "requested_url": probe.url,
                    "final_url": exc.geturl(),
                    "status": exc.code,
                    "ok": False,
                    "retrieved_at": datetime.now(timezone.utc).isoformat(),
                    "elapsed_ms": round((time.monotonic() - started) * 1000),
                    "headers": _headers(exc.headers),
                    "bytes_retained": 0,
                    "sha256": hashlib.sha256(b"").hexdigest(),
                    "partial": probe.partial,
                    "error": last_error,
                    "attempts": attempt,
                    "body": b"",
                }
        except (URLError, TimeoutError, OSError, ValueError) as exc:
            last_error = str(exc)
        if attempt < attempts:
            time.sleep(min(8.0, 0.5 * (2 ** (attempt - 1))))
    return {
        "id": probe.id,
        "family": probe.family,
        "requested_url": probe.url,
        "final_url": probe.url,
        "status": 0,
        "ok": False,
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "elapsed_ms": 0,
        "headers": {},
        "bytes_retained": 0,
        "sha256": hashlib.sha256(b"").hexdigest(),
        "partial": probe.partial,
        "error": last_error or "unknown error",
        "attempts": attempts,
        "body": b"",
    }


def public_result(result: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in result.items() if key != "body"}
