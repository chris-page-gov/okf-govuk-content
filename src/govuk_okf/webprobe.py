"""Bounded, policy-aware public HTTP probes used by source preflight."""

from __future__ import annotations

import gzip
import hashlib
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import Message
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
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


def fetch_probe(probe: Probe, attempts: int = 3) -> dict[str, Any]:
    headers = {"User-Agent": USER_AGENT, "Accept": "*/*", "Accept-Encoding": "gzip"}
    if probe.partial:
        headers["Range"] = f"bytes=0-{probe.max_bytes - 1}"
    request = Request(probe.url, headers=headers)
    last_error = ""
    for attempt in range(1, attempts + 1):
        started = time.monotonic()
        try:
            with build_opener(HTTPRedirectHandler()).open(request, timeout=30) as response:
                raw = response.read(probe.max_bytes + 1)
                truncated = len(raw) > probe.max_bytes
                raw = raw[: probe.max_bytes]
                if response.headers.get("Content-Encoding", "").lower() == "gzip":
                    try:
                        raw = gzip.decompress(raw)
                    except (gzip.BadGzipFile, EOFError):
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
            if exc.code not in {408, 429, 500, 502, 503, 504}:
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
        except (URLError, TimeoutError, OSError) as exc:
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

