"""Full public closure: structured Content API plus rendered-link gap pass."""

from __future__ import annotations

import collections
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .acquisition import HostLimiter, request_observation, write_text_atomic
from .hydration import CorpusHydrator
from .rendered_gap import RobotsPolicy, parse_robots, rendered_observation
from .util import canonical_json_bytes, pretty_json

ROBOTS_URL = "https://www.gov.uk/robots.txt"
DEFAULT_RENDERED_SCAN_LIMIT = 75_000


class CompleteCorpusHydrator(CorpusHydrator):
    """Add a robots-aware transient HTML gap detector to hydration."""

    def __init__(
        self,
        *args: Any,
        max_queue_records: int = 1_500_000,
        max_rendered_requests: int = DEFAULT_RENDERED_SCAN_LIMIT,
        rendered_requests_per_second: float = 2.0,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        if max_queue_records < 1 or max_rendered_requests < 1:
            raise ValueError("queue and rendered-request ceilings must be positive")
        self.max_queue_records = max_queue_records
        self.max_rendered_requests = max_rendered_requests
        self.rendered_limiter = HostLimiter(
            rendered_requests_per_second,
            state_path=self.root / ".tmp" / "rate-limits" / "www.gov.uk.timestamp",
            budget_path=self.root / ".tmp" / "request-budget" / "official-sources.count",
            max_requests=1_000_000,
        )
        self.robots_policy: RobotsPolicy | None = None
        self.robots_request_attempts = 0
        self.rendered_selection: set[tuple[str, str]] = set()

    def _connect(self) -> sqlite3.Connection:
        connection = super()._connect()
        connection.execute("DROP TRIGGER IF EXISTS queue_volume_alarm")
        connection.execute(
            f"""
            CREATE TRIGGER queue_volume_alarm
            BEFORE INSERT ON queue
            WHEN (SELECT COUNT(*) FROM queue) >= {self.max_queue_records}
              AND NOT EXISTS (
                SELECT 1 FROM queue
                WHERE url = NEW.url AND locale = NEW.locale
              )
            BEGIN
              SELECT RAISE(ABORT, 'rendered-link closure exceeded the declared queue ceiling');
            END;
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS rendered_selection (
                url TEXT NOT NULL,
                locale TEXT NOT NULL,
                stratum TEXT NOT NULL,
                selection_sha256 TEXT NOT NULL,
                PRIMARY KEY (url, locale)
            )
            """
        )
        return connection

    @staticmethod
    def _rendered_eligible_sql() -> str:
        return "(url = 'https://www.gov.uk/' OR url LIKE 'https://www.gov.uk/%')"

    def prepare(self, connection: sqlite3.Connection) -> int:
        """Freeze a deterministic, stratified rendered-page gap sample."""

        source_count = super().prepare(connection)
        eligible = int(
            connection.execute(
                f"SELECT COUNT(*) FROM queue WHERE {self._rendered_eligible_sql()}"
            ).fetchone()[0]
        )
        target = min(eligible, self.max_rendered_requests)
        existing = int(connection.execute("SELECT COUNT(*) FROM rendered_selection").fetchone()[0])
        frozen_limit = connection.execute(
            "SELECT value FROM meta WHERE key='rendered_selection_limit'"
        ).fetchone()
        if existing:
            if frozen_limit is None or int(frozen_limit[0]) != self.max_rendered_requests or existing != target:
                raise ValueError("rendered gap selection contract changed after it was frozen; use a new snapshot label")
        else:
            completed = int(connection.execute("SELECT COUNT(*) FROM queue WHERE state='complete'").fetchone()[0])
            if completed:
                raise ValueError("rendered gap selection is missing after hydration began; use a new snapshot label")
            best_by_stratum: dict[str, tuple[str, str, str]] = {}
            cursor = connection.execute(
                f"SELECT url, locale, input_json FROM queue WHERE {self._rendered_eligible_sql()}"
            )
            for url, locale, input_json in cursor:
                record = json.loads(input_json)
                stratum = "\0".join(
                    (
                        str(record.get("document_type") or "unknown"),
                        str(record.get("schema_name") or "unknown"),
                        str(locale or "en"),
                    )
                )
                digest = hashlib.sha256(f"{url}\0{locale}".encode("utf-8")).hexdigest()
                previous = best_by_stratum.get(stratum)
                if previous is None or digest < previous[0]:
                    best_by_stratum[stratum] = (digest, str(url), str(locale))
            if len(best_by_stratum) > target:
                raise ValueError(
                    f"rendered scan limit {target} cannot cover all {len(best_by_stratum)} source strata"
                )
            connection.executemany(
                "INSERT INTO rendered_selection(url, locale, stratum, selection_sha256) VALUES (?, ?, ?, ?)",
                [
                    (url, locale, stratum, digest)
                    for stratum, (digest, url, locale) in sorted(best_by_stratum.items())
                ],
            )
            remaining = target - len(best_by_stratum)
            if remaining:
                connection.create_function(
                    "stable_sha256",
                    1,
                    lambda value: hashlib.sha256(str(value).encode("utf-8")).hexdigest(),
                    deterministic=True,
                )
                connection.execute(
                    f"""
                    INSERT INTO rendered_selection(url, locale, stratum, selection_sha256)
                    SELECT q.url, q.locale, 'deterministic-minhash', stable_sha256(q.url || char(0) || q.locale)
                    FROM queue AS q
                    WHERE {self._rendered_eligible_sql().replace('url', 'q.url')}
                      AND NOT EXISTS (
                        SELECT 1 FROM rendered_selection AS s
                        WHERE s.url=q.url AND s.locale=q.locale
                      )
                    ORDER BY stable_sha256(q.url || char(0) || q.locale), q.url, q.locale
                    LIMIT ?
                    """,
                    (remaining,),
                )
            selection_rows = connection.execute(
                "SELECT url, locale, stratum, selection_sha256 FROM rendered_selection ORDER BY url, locale"
            ).fetchall()
            if len(selection_rows) != target:
                raise ValueError(f"rendered gap selection did not close: {len(selection_rows)} != {target}")
            selection_digest = hashlib.sha256()
            for url, locale, stratum, digest in selection_rows:
                selection_digest.update(
                    canonical_json_bytes(
                        {"url": url, "locale": locale, "stratum": stratum, "selection_sha256": digest}
                    )
                )
            for key, value in (
                ("rendered_selection_limit", str(self.max_rendered_requests)),
                ("rendered_selection_population", str(eligible)),
                ("rendered_selection_records", str(target)),
                ("rendered_selection_strata", str(len(best_by_stratum))),
                ("rendered_selection_canonical_sha256", selection_digest.hexdigest()),
            ):
                connection.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value))
            connection.commit()
        self.rendered_selection = {
            (str(url), str(locale))
            for url, locale in connection.execute("SELECT url, locale FROM rendered_selection")
        }
        return source_count

    def _prepare_robots(self) -> RobotsPolicy:
        body, evidence = request_observation(ROBOTS_URL, limiter=self.rendered_limiter, max_bytes=1024 * 1024)
        if not evidence.get("ok") or evidence.get("partial"):
            raise ValueError("current robots.txt could not be verified; rendered-link acquisition is blocked")
        policy = parse_robots(body, evidence)
        self.robots_request_attempts = int(evidence.get("acquisition_attempt") or 1)
        connection = self._connect()
        try:
            previous = connection.execute(
                "SELECT value FROM meta WHERE key='rendered_robots_policy'"
            ).fetchone()
            completed = connection.execute("SELECT COUNT(*) FROM queue WHERE state='complete'").fetchone()[0]
            if previous and completed:
                previous_row = json.loads(previous[0])
                if previous_row.get("sha256") != policy.sha256:
                    raise ValueError("robots.txt changed after rendered acquisition began; use a new snapshot label")
            connection.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES ('rendered_robots_policy', ?)",
                (
                    json.dumps(
                        {
                            "url": policy.source_url,
                            "sha256": policy.sha256,
                            "retrieved_at": policy.retrieved_at,
                            "rules": len(policy.rules),
                            "request_attempts": self.robots_request_attempts,
                        },
                        sort_keys=True,
                    ),
                ),
            )
            connection.commit()
        finally:
            connection.close()
        return policy

    def _hydrate_one(self, input_json: str) -> tuple[dict[str, Any], str, list[dict[str, Any]]]:
        result, hydration_status, linked = super()._hydrate_one(input_json)
        original = json.loads(input_json)
        url = str(result.get("canonical_url") or "")
        if urlparse(url).netloc != "www.gov.uk" or result.get("entity_class") == "resource":
            result["rendered_gap_status"] = "not_applicable"
            return result, hydration_status, linked
        selection_key = (url, str(original.get("locale") or result.get("locale") or "en"))
        if selection_key not in self.rendered_selection:
            result["rendered_gap_status"] = "not_selected_by_bounded_detector"
            return result, hydration_status, linked
        if self.robots_policy is None:
            raise ValueError("robots policy was not prepared")
        if not self.robots_policy.allows(url):
            result["rendered_gap_status"] = "robots_blocked"
            return result, hydration_status, linked
        body, evidence = request_observation(url, limiter=self.rendered_limiter, max_bytes=8 * 1024 * 1024)
        content_type = str((evidence.get("headers") or {}).get("content-type") or "").casefold()
        if evidence.get("ok") and not evidence.get("partial") and (
            not content_type or "html" in content_type
        ):
            metadata, rendered_links = rendered_observation(result, body, evidence, self.robots_policy)
            metadata["request_attempts"] = int(evidence.get("acquisition_attempt") or 1)
            result["rendered_observation"] = metadata
            result["rendered_gap_status"] = "represented"
            linked.extend(rendered_links)
            return result, hydration_status, linked

        reason = (
            "rendered response exceeded the 8 MiB transient parser limit"
            if evidence.get("partial")
            else "rendered target is not HTML"
            if evidence.get("ok")
            else str(evidence.get("error") or f"HTTP {evidence.get('status') or 0}")
        )
        constraint_id = "constraint-" + hashlib.sha256(f"{url}\0rendered\0{reason}".encode()).hexdigest()[:24]
        result.setdefault("constraints", []).append(
            {
                "id": constraint_id,
                "class": "rendered_link_gap_detector",
                "reason": reason,
                "status": int(evidence.get("status") or 0),
                "evidence_url": evidence.get("requested_url") or url,
                "evidence_sha256": evidence.get("sha256"),
                "owner": "corpus-maintainer",
                "retry": "next closing acquisition",
            }
        )
        result["rendered_gap_status"] = "non_html" if evidence.get("ok") else "exception"
        result["rendered_observation"] = {
            "body_sha256": evidence.get("sha256"),
            "retrieved_at": evidence.get("retrieved_at"),
            "final_url": evidence.get("final_url"),
            "retained_body_bytes": 0,
            "request_attempts": int(evidence.get("acquisition_attempt") or 1),
        }
        return result, hydration_status, linked

    def run(self, *, request_limit: int | None = None) -> dict[str, Any]:
        # Do not spend even the robots request when the retained checkpoint is
        # already outside its signed storage authority.
        self._assert_retained_storage(phase="pre-robots hydration checkpoint")
        self.robots_policy = self._prepare_robots()
        result = super().run(request_limit=request_limit)
        result["rendered_gap_enabled"] = True
        result["robots_sha256"] = self.robots_policy.sha256
        result["queue_ceiling"] = self.max_queue_records
        result["rendered_scan_limit"] = self.max_rendered_requests
        result["rendered_scan_selected"] = len(self.rendered_selection)
        return result

    def export(self, enumeration_reconciliation: Path | None = None) -> dict[str, Any]:
        reconciliation = super().export(enumeration_reconciliation)
        connection = self._connect()
        status_counts: collections.Counter[str] = collections.Counter()
        discovered: collections.Counter[str] = collections.Counter()
        rendered_link_records = 0
        content_api_attempts = 0
        rendered_attempts = 0
        try:
            for (record_json,) in connection.execute("SELECT record_json FROM queue ORDER BY url, locale"):
                record = json.loads(record_json)
                status_counts[str(record.get("rendered_gap_status") or "missing")] += 1
                content_api_attempts += int(record.get("content_api_attempts") or 0)
                if "rendered-links" in record.get("source_memberships", []):
                    rendered_link_records += 1
                observation = record.get("rendered_observation")
                if isinstance(observation, dict):
                    if observation.get("retained_body_bytes") != 0:
                        raise ValueError("rendered gap detector retained body bytes")
                    rendered_attempts += int(observation.get("request_attempts") or 0)
                    for key, value in (observation.get("discovered") or {}).items():
                        if isinstance(value, int):
                            discovered[str(key)] += value
        finally:
            connection.close()
        if self.robots_policy is None:
            policy_connection = self._connect()
            try:
                policy_value = policy_connection.execute(
                    "SELECT value FROM meta WHERE key='rendered_robots_policy'"
                ).fetchone()
                if not policy_value:
                    raise ValueError("rendered robots policy evidence is missing")
                robots_row = json.loads(policy_value[0])
            finally:
                policy_connection.close()
        else:
            robots_row = {
                "url": self.robots_policy.source_url,
                "sha256": self.robots_policy.sha256,
                "retrieved_at": self.robots_policy.retrieved_at,
                "rules": len(self.robots_policy.rules),
                "request_attempts": self.robots_request_attempts or 1,
            }
        proof_connection = self._connect()
        try:
            selection_values = {
                str(key): str(value)
                for key, value in proof_connection.execute(
                    "SELECT key, value FROM meta WHERE key LIKE 'rendered_selection_%'"
                )
            }
            selected = int(selection_values.get("rendered_selection_records", "0"))
            population = int(selection_values.get("rendered_selection_population", "0"))
            completed_selection = int(
                proof_connection.execute(
                    "SELECT COUNT(*) FROM rendered_selection AS s JOIN queue AS q USING(url, locale) "
                    "WHERE q.state='complete'"
                ).fetchone()[0]
            )
        finally:
            proof_connection.close()
        if completed_selection != selected:
            raise ValueError(f"rendered gap selection is incomplete: {completed_selection} != {selected}")
        robots_attempts = int(robots_row.get("request_attempts") or 1)
        budget_path = self.root / ".tmp" / "request-budget" / "official-sources.count"
        cumulative_attempts = int(budget_path.read_text(encoding="utf-8").strip()) if budget_path.is_file() else 0
        proof = {
            "closed": True,
            "closure_scope": "deterministic bounded rendered-link gap detector",
            "queue_ceiling": self.max_queue_records,
            "eligible_population": population,
            "selected_records": selected,
            "unsampled_records": population - selected,
            "source_strata": int(selection_values.get("rendered_selection_strata", "0")),
            "selection_rule": "one min-hash record per document_type/schema_name/locale stratum, then global min-hash",
            "selection_canonical_sha256": selection_values.get("rendered_selection_canonical_sha256"),
            "selection_limit": self.max_rendered_requests,
            "robots": robots_row,
            "status_counts": dict(sorted(status_counts.items())),
            "rendered_link_records": rendered_link_records,
            "discovered_observations": dict(sorted(discovered.items())),
            "retained_body_bytes": 0,
            "request_accounting": {
                "content_api_attempts": content_api_attempts,
                "rendered_attempts": rendered_attempts,
                "robots_attempts": robots_attempts,
                "this_hydration_attempts": content_api_attempts + rendered_attempts + robots_attempts,
                "programme_cumulative_attempts": cumulative_attempts,
                "programme_ceiling": 1_000_000,
            },
        }
        reconciliation["rendered_gap_proof"] = proof
        source_counts = dict(reconciliation.get("source_counts") or {})
        source_counts["rendered_links"] = rendered_link_records
        reconciliation["source_counts"] = dict(sorted(source_counts.items()))
        target = self.reconciliation_root / f"{self.label}-hydrated.json"
        write_text_atomic(target, pretty_json(reconciliation))
        manifest_path = self.records_root / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["rendered_gap_proof"] = proof
        write_text_atomic(manifest_path, pretty_json(manifest))
        return reconciliation
