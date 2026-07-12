"""Bounded, policy-aware public HTTP probes used by source preflight."""

from __future__ import annotations

import gzip
import hashlib
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
from urllib.request import HTTPRedirectHandler, Request, build_opener

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


def _normalise_host(value: str) -> str:
    try:
        return value.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ValueError(f"probe hostname is invalid: {value}") from exc


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
    host = _normalise_host(parsed.hostname)
    approved = {_normalise_host(value) for value in allowed_hosts}
    if host not in approved:
        raise ValueError(f"probe host is not approved: {host}")
    try:
        answers = resolver(host, 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ValueError(f"probe host cannot be resolved: {host}") from exc
    addresses = {str(answer[4][0]).split("%", 1)[0] for answer in answers if answer and len(answer) > 4 and answer[4]}
    if not addresses:
        raise ValueError(f"probe host has no resolved address: {host}")
    for address in addresses:
        try:
            parsed_address = ipaddress.ip_address(address)
        except ValueError as exc:
            raise ValueError(f"probe host returned an invalid address: {address}") from exc
        if not parsed_address.is_global:
            raise ValueError(f"probe destination is not public: {address}")
    return host


class PolicyRedirectHandler(HTTPRedirectHandler):
    """Revalidate every redirect before urllib can perform the next request."""

    def __init__(self, *, allowed_hosts: tuple[str, ...], resolver: Any = socket.getaddrinfo, max_redirects: int = 5) -> None:
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


def fetch_probe(probe: Probe, attempts: int = 3) -> dict[str, Any]:
    parsed = urlsplit(probe.url)
    initial_host = _normalise_host(parsed.hostname or "")
    allowed_hosts = tuple(dict.fromkeys((*probe.allowed_hosts, initial_host)))
    headers = {"User-Agent": USER_AGENT, "Accept": "*/*", "Accept-Encoding": "gzip"}
    if probe.partial:
        headers["Range"] = f"bytes=0-{probe.max_bytes - 1}"
    request = Request(probe.url, headers=headers)
    last_error = ""
    for attempt in range(1, attempts + 1):
        started = time.monotonic()
        try:
            validate_public_https_url(probe.url, allowed_hosts=allowed_hosts)
            redirect_handler = PolicyRedirectHandler(allowed_hosts=allowed_hosts)
            with build_opener(redirect_handler).open(request, timeout=30) as response:
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
