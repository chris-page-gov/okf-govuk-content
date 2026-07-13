from __future__ import annotations

import hashlib
import gzip
import io
import json
import re
import socket
import unittest
from email.message import Message
from pathlib import Path
from urllib.request import Request
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]

import sys

sys.path.insert(0, str(ROOT / "src"))

from govuk_okf.webprobe import (  # noqa: E402
    PinnedHTTPSConnection,
    PolicyHTTPSHandler,
    PolicyRedirectHandler,
    Probe,
    _bounded_gzip_decompress,
    fetch_probe,
    validate_public_https_url,
)


class SourcePreflightTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.document = json.loads((ROOT / "research" / "source-preflight.json").read_text(encoding="utf-8"))
        cls.plan_preflight = json.loads(
            (ROOT / "planning" / "PLAN_SOURCE_PREFLIGHT.json").read_text(encoding="utf-8")
        )

    def test_all_official_probes_pass_and_plan_urls_are_accounted(self) -> None:
        summary = self.document["summary"]
        self.assertEqual(32, summary["official_total"])
        self.assertEqual(0, summary["official_failed"])
        self.assertEqual(93, summary["plan_total"])
        self.assertEqual(93, summary["plan_ok"])
        self.assertEqual(0, summary["plan_failed"])

    def test_plan_hash_and_url_accounting_match_the_controlling_document(self) -> None:
        plan = (ROOT / "planning" / "AFHF_GOVUK_OKF_SOTA_RESEARCH_IMPLEMENTATION_PLAN.md").read_bytes()
        urls = sorted(set(re.findall(rb"\]\((https?://[^)]+)\)", plan)))
        expected_urls = [url.decode("utf-8") for url in urls]
        self.assertEqual(hashlib.sha256(plan).hexdigest(), self.plan_preflight["plan_sha256"])
        self.assertEqual(expected_urls, [source["url"] for source in self.plan_preflight["sources"]])
        self.assertEqual(expected_urls, [source["requested_url"] for source in self.document["plan_sources"]])

    def test_live_denominators_are_retained(self) -> None:
        by_id = {item["id"]: item for item in self.document["official_sources"]}
        self.assertEqual(715465, by_id["search-api-root-count"]["facts"]["reported_total"])
        self.assertEqual(35, by_id["sitemap-index"]["facts"]["declared_locations"])
        self.assertEqual(1256, by_id["organisations-api"]["facts"]["total"])
        self.assertEqual(20, by_id["content-api-root"]["facts"]["link_counts"]["level_one_taxons"])
        self.assertEqual(233, by_id["world-taxonomy-root"]["facts"]["link_counts"]["child_taxons"])
        self.assertEqual(16, by_id["mainstream-browse-root"]["facts"]["link_counts"]["top_level_browse_pages"])

    def test_every_probe_has_hash_and_timestamp(self) -> None:
        for item in [*self.document["official_sources"], *self.document["plan_sources"]]:
            self.assertEqual(64, len(item["sha256"]))
            self.assertIn("+00:00", item["retrieved_at"])

    def test_pirolli_replacement_preserves_transport_and_access_history(self) -> None:
        crossref = "https://api.crossref.org/works/10.1037%2F0033-295X.106.4.643"
        cmu = "https://act-r.psy.cmu.edu/wordpress/wp-content/uploads/2012/12/280uir-1999-05-pirolli.pdf"
        active_urls = {item["requested_url"] for item in self.document["plan_sources"]}
        self.assertIn(crossref, active_urls)
        self.assertNotIn(cmu, active_urls)

        history = self.document["plan_source_history"]
        self.assertEqual(93, history["preserved_original_result_count"])
        self.assertEqual(
            {"plan_total": 93, "plan_ok": 92, "plan_failed": 1},
            history["original_summary"],
        )
        failures = history["superseded_results"]
        self.assertEqual(1, len(failures))
        self.assertEqual(cmu, failures[0]["requested_url"])
        self.assertIn("DH_KEY_TOO_SMALL", failures[0]["error"])
        superseded_by_id = {item["id"]: item for item in failures}
        reconstructed = [
            superseded_by_id.get(item["id"], item) for item in self.document["plan_sources"]
        ]
        original_digest = hashlib.sha256(
            json.dumps(
                reconstructed,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        self.assertEqual(history["original_results_sha256"], original_digest)
        researchgate = [
            item for item in history["access_restrictions"] if "researchgate.net" in item["url"]
        ]
        self.assertEqual(1, len(researchgate))
        self.assertEqual("HTTP 403 Forbidden", researchgate[0]["publication_record_result"])
        self.assertEqual("HTTP 403 Forbidden", researchgate[0]["exact_author_pdf_result"])

    def test_probe_policy_rejects_non_https_private_and_unapproved_destinations(self) -> None:
        public_answer = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
        private_answer = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 443))]
        self.assertEqual(
            "example.test",
            validate_public_https_url(
                "https://example.test/path",
                allowed_hosts=("example.test",),
                resolver=lambda *_args, **_kwargs: public_answer,
            ),
        )
        with self.assertRaisesRegex(ValueError, "credential-free HTTPS"):
            validate_public_https_url(
                "http://example.test/path",
                allowed_hosts=("example.test",),
                resolver=lambda *_args, **_kwargs: public_answer,
            )
        with self.assertRaisesRegex(ValueError, "not public"):
            validate_public_https_url(
                "https://example.test/path",
                allowed_hosts=("example.test",),
                resolver=lambda *_args, **_kwargs: private_answer,
            )
        with self.assertRaisesRegex(ValueError, "not approved"):
            validate_public_https_url(
                "https://other.test/path",
                allowed_hosts=("example.test",),
                resolver=lambda *_args, **_kwargs: public_answer,
            )

    def test_probe_requires_an_explicit_host_policy(self) -> None:
        with self.assertRaisesRegex(ValueError, "approved host"):
            Probe("missing-policy", "https://example.test/", "test")

    def test_tls_connection_reuses_the_validated_public_dns_answer(self) -> None:
        public_answer = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
        private_answer = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 443))]
        events: dict[str, object] = {"resolver_calls": 0}

        def resolver(*_args: object, **_kwargs: object) -> list[tuple[object, ...]]:
            events["resolver_calls"] = int(events["resolver_calls"]) + 1
            return public_answer if events["resolver_calls"] == 1 else private_answer

        class FakeSocket:
            def settimeout(self, timeout: object) -> None:
                events["timeout"] = timeout

            def bind(self, address: object) -> None:
                events["bound"] = address

            def connect(self, address: object) -> None:
                events["connected"] = address

            def setsockopt(self, *_args: object) -> None:
                return None

            def close(self) -> None:
                events["closed"] = True

        class FakeContext:
            def wrap_socket(self, value: FakeSocket, *, server_hostname: str) -> FakeSocket:
                events["server_hostname"] = server_hostname
                return value

        connection = PinnedHTTPSConnection(
            "example.test",
            timeout=30,
            context=FakeContext(),
            allowed_hosts=("example.test",),
            resolver=resolver,
            socket_factory=lambda *_args: FakeSocket(),
        )
        connection.connect()
        self.assertEqual(events["resolver_calls"], 1)
        self.assertEqual(events["connected"], ("93.184.216.34", 443))
        self.assertEqual(events["server_hostname"], "example.test")

    def test_tls_connection_rejects_private_connection_time_dns(self) -> None:
        private_answer = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 443))]
        connection = PinnedHTTPSConnection(
            "example.test",
            timeout=30,
            allowed_hosts=("example.test",),
            resolver=lambda *_args, **_kwargs: private_answer,
        )
        with self.assertRaisesRegex(ValueError, "not public"):
            connection.connect()

    def test_fetch_probe_disables_proxies_and_reuses_policy_resolver(self) -> None:
        public_answer = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
        resolver = lambda *_args, **_kwargs: public_answer
        captured: dict[str, object] = {}

        class FakeResponse:
            status = 200
            headers = Message()

            def __enter__(self) -> "FakeResponse":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def read(self, _limit: int) -> bytes:
                return b"ok"

            def geturl(self) -> str:
                return "https://example.test/"

        class FakeOpener:
            def open(self, _request: Request, timeout: int) -> FakeResponse:
                captured["timeout"] = timeout
                return FakeResponse()

        def fake_build_opener(*handlers: object) -> FakeOpener:
            captured["handlers"] = handlers
            return FakeOpener()

        with patch("govuk_okf.webprobe.build_opener", side_effect=fake_build_opener):
            result = fetch_probe(
                Probe(
                    "policy-wiring",
                    "https://example.test/",
                    "test",
                    allowed_hosts=("example.test",),
                ),
                attempts=1,
                resolver=resolver,
            )
        self.assertTrue(result["ok"])
        handlers = captured["handlers"]
        proxy_handler = next(handler for handler in handlers if hasattr(handler, "proxies"))
        https_handler = next(handler for handler in handlers if isinstance(handler, PolicyHTTPSHandler))
        redirect_handler = next(handler for handler in handlers if isinstance(handler, PolicyRedirectHandler))
        self.assertEqual(proxy_handler.proxies, {})
        self.assertIs(https_handler.resolver, resolver)
        self.assertIs(redirect_handler.resolver, resolver)
        self.assertEqual(captured["timeout"], 30)

    def test_redirect_policy_revalidates_each_hop_before_request_creation(self) -> None:
        public_answer = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
        private_answer = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
        request = Request("https://example.test/start")
        headers = Message()
        handler = PolicyRedirectHandler(
            allowed_hosts=("example.test",),
            resolver=lambda *_args, **_kwargs: public_answer,
        )
        redirected = handler.redirect_request(request, None, 302, "Found", headers, "/next")
        self.assertEqual("https://example.test/next", redirected.full_url)
        blocked = PolicyRedirectHandler(
            allowed_hosts=("example.test",),
            resolver=lambda *_args, **_kwargs: private_answer,
        )
        with self.assertRaisesRegex(ValueError, "not public"):
            blocked.redirect_request(request, None, 302, "Found", headers, "https://example.test/internal")

    def test_gzip_probe_decompression_stops_at_decoded_byte_limit(self) -> None:
        compressed = io.BytesIO()
        with gzip.GzipFile(fileobj=compressed, mode="wb", mtime=0) as stream:
            stream.write(b"a" * 4096)
        decoded, truncated = _bounded_gzip_decompress(compressed.getvalue(), 128)
        self.assertEqual(128, len(decoded))
        self.assertTrue(truncated)


if __name__ == "__main__":
    unittest.main()
