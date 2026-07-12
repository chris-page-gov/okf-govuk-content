"""Deterministic, disk-backed evaluation of the GOV.UK OKF discovery layer.

The harness deliberately makes no network or model calls.  It imports the
published bundle into bounded SQLite/FTS indexes, runs one matched query
contract for the proposal, baselines and ablations, and keeps enough state to
resume an interrupted run.  Release mode fails closed unless the independently
verified v2 matrix contains all 28,800 questions for the matching snapshot.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import math
import os
import platform
import re
import shutil
import sqlite3
import statistics
import subprocess
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import NormalDist
from typing import Any, Iterable, Iterator, Sequence

from .util import yaml_load_subset


HARNESS_VERSION = "govuk-okf-deterministic-evaluation-v1"
TRACE_SCHEMA = "govuk-okf-agent-evaluation-trace.v1"
RELEASE_QUESTION_COUNT = 28_800
RELEASE_PERSONA_COUNT = 48
RELEASE_STORY_COUNT = 288
MAX_COMPRESSED_SHARD_BYTES = 50 * 1024 * 1024
MAX_UNCOMPRESSED_SHARD_BYTES = 64 * 1024 * 1024
MAX_JSON_VALUE_BYTES = 16 * 1024 * 1024
DEFAULT_TRACE_SHARD_RECORDS = 5_000
MAX_TRACE_SHARD_RECORDS = 10_000
MAX_TRACE_UNCOMPRESSED_BYTES = 32 * 1024 * 1024
RESULT_LIMIT = 10


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def pretty_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_text(value: str) -> str:
    return sha256_bytes(value.encode("utf-8"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def atomic_json(path: Path, value: object) -> None:
    atomic_text(path, pretty_json(value))


def load_json(path: Path, *, max_bytes: int = 64 * 1024 * 1024) -> dict[str, Any]:
    if path.stat().st_size > max_bytes:
        raise ValueError(f"JSON document exceeds {max_bytes} bytes: {path}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return value


def safe_relative(root: Path, relative_text: str) -> Path:
    relative = Path(relative_text)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"unsafe relative path: {relative_text}")
    candidate = (root / relative).resolve()
    if not candidate.is_relative_to(root.resolve()):
        raise ValueError(f"path escapes root: {relative_text}")
    return candidate


def checked_record(record: dict[str, Any]) -> bool:
    material = dict(record)
    checksum = str(material.pop("checksum", ""))
    return bool(checksum) and checksum == sha256_text(canonical_json(material))


def iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    with path.open("rb") as stream:
        for line_number, line in enumerate(stream, start=1):
            if len(line) > MAX_JSON_VALUE_BYTES:
                raise ValueError(f"JSONL record exceeds limit: {path}:{line_number}")
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"JSONL record is not an object: {path}:{line_number}")
            yield value


def iter_json_array(path: Path) -> Iterator[dict[str, Any]]:
    """Stream a bounded JSON array from a plain or gzip file.

    Publication shards are arrays rather than JSONL.  A small incremental
    decoder avoids list-loading a 10,000-record shard and enforces both the
    compressed and decompressed limits used by the publication contract.
    """

    if path.suffix == ".gz":
        if path.stat().st_size > MAX_COMPRESSED_SHARD_BYTES:
            raise ValueError(f"compressed shard exceeds limit: {path}")
        binary: Any = gzip.open(path, "rb")
    else:
        binary = path.open("rb")
    decoder = json.JSONDecoder()
    utf8 = io.TextIOWrapper(binary, encoding="utf-8", errors="strict", newline="")
    buffer = ""
    decompressed = 0
    started = False
    first = True
    eof = False

    def read_more() -> bool:
        nonlocal buffer, decompressed, eof
        chunk = utf8.read(64 * 1024)
        if not chunk:
            eof = True
            return False
        decompressed += len(chunk.encode("utf-8"))
        if decompressed > MAX_UNCOMPRESSED_SHARD_BYTES:
            raise ValueError(f"decompressed shard exceeds limit: {path}")
        buffer += chunk
        return True

    try:
        while True:
            while not buffer.strip() and not eof:
                read_more()
            buffer = buffer.lstrip()
            if not started:
                if not buffer and eof:
                    raise ValueError(f"empty JSON shard: {path}")
                if not buffer:
                    continue
                if buffer[0] != "[":
                    raise ValueError(f"JSON shard is not an array: {path}")
                buffer = buffer[1:]
                started = True
            while not buffer.strip() and not eof:
                read_more()
            buffer = buffer.lstrip()
            if buffer.startswith("]"):
                trailing = buffer[1:]
                buffer = ""
                while not eof:
                    read_more()
                    trailing += buffer
                    buffer = ""
                if trailing.strip():
                    raise ValueError(f"trailing data after JSON array: {path}")
                return
            if not first:
                while not buffer and not eof:
                    read_more()
                buffer = buffer.lstrip()
                if not buffer.startswith(","):
                    raise ValueError(f"missing comma in JSON shard: {path}")
                buffer = buffer[1:].lstrip()
            while True:
                try:
                    value, end = decoder.raw_decode(buffer)
                    break
                except json.JSONDecodeError as exc:
                    if eof:
                        raise ValueError(f"invalid JSON shard {path}: {exc}") from exc
                    if len(buffer.encode("utf-8")) > MAX_JSON_VALUE_BYTES:
                        raise ValueError(f"JSON value exceeds limit: {path}")
                    read_more()
            if not isinstance(value, dict):
                raise ValueError(f"publication shard value is not an object: {path}")
            yield value
            buffer = buffer[end:]
            first = False
    finally:
        utf8.close()


def _normalise_space(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _flatten_text(value: object, *, limit: int = 16_384) -> str:
    values: list[str] = []

    def visit(item: object) -> None:
        if sum(len(part) for part in values) >= limit:
            return
        if isinstance(item, dict):
            for key in sorted(item):
                visit(item[key])
        elif isinstance(item, list):
            for nested in item:
                visit(nested)
        elif item is not None:
            values.append(_normalise_space(item))

    visit(value)
    return " ".join(part for part in values if part)[:limit]


def normalise_predicate(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").casefold()).strip("_")


def quoted_phrases(value: str) -> list[str]:
    matches = re.findall(r"[\u201c\"]([^\u201d\"]+)[\u201d\"]", value)
    return [_normalise_space(item).casefold() for item in matches if _normalise_space(item)]


STOPWORDS = {
    "about",
    "after",
    "again",
    "against",
    "also",
    "and",
    "answer",
    "are",
    "authoritative",
    "before",
    "being",
    "canonical",
    "could",
    "does",
    "every",
    "find",
    "from",
    "give",
    "gov",
    "govuk",
    "have",
    "identify",
    "into",
    "item",
    "need",
    "official",
    "only",
    "record",
    "result",
    "should",
    "source",
    "that",
    "the",
    "their",
    "then",
    "this",
    "through",
    "uk",
    "version",
    "what",
    "when",
    "where",
    "which",
    "with",
    "would",
}


def lexical_tokens(value: str, *, limit: int = 32) -> list[str]:
    result: list[str] = []
    for token in re.findall(r"[^\W_]+", value.casefold(), flags=re.UNICODE):
        if len(token) < 2 or token in STOPWORDS or token.isdigit() or token in result:
            continue
        result.append(token)
        if len(result) >= limit:
            break
    return result


def deterministic_abstention(wording: str) -> bool:
    text = wording.casefold()
    guarantee = "guarantee" in text or "guaranteed" in text
    missing_facts = "no case facts" in text or "without the facts" in text or "personal outcome" in text
    unsafe = "not enough to answer" in text and "safely" in text
    return (guarantee and missing_facts) or unsafe


@dataclass(frozen=True)
class SystemSpec:
    system_id: str
    role: str
    retrieval: str
    graph_enabled: bool
    trust_signals: bool
    inferred_enabled: bool
    answerability_classifier: bool
    citation_mode: str
    single_source: bool = False
    serialization: str | None = None


SYSTEMS: tuple[SystemSpec, ...] = (
    SystemSpec(
        "proposal-okf-graph",
        "proposal",
        "typed_fts",
        True,
        True,
        True,
        True,
        "full",
    ),
    SystemSpec(
        "baseline-exact-known-item",
        "baseline",
        "exact",
        False,
        False,
        False,
        False,
        "canonical",
    ),
    SystemSpec(
        "baseline-flat-metadata-fts",
        "baseline",
        "flat_fts",
        False,
        False,
        False,
        False,
        "canonical",
    ),
    SystemSpec(
        "baseline-typed-metadata-fts",
        "baseline",
        "native_fts",
        False,
        True,
        False,
        False,
        "full",
    ),
    SystemSpec(
        "ablation-no-graph",
        "ablation",
        "typed_fts",
        False,
        True,
        True,
        True,
        "full",
    ),
    SystemSpec(
        "ablation-no-provenance-lifecycle",
        "ablation",
        "typed_fts",
        True,
        False,
        True,
        True,
        "canonical",
    ),
    SystemSpec(
        "ablation-single-source-inventory",
        "ablation",
        "typed_fts",
        True,
        True,
        True,
        True,
        "full",
        single_source=True,
    ),
    SystemSpec(
        "ablation-source-native-only",
        "ablation",
        "native_fts",
        True,
        True,
        False,
        True,
        "full",
    ),
    SystemSpec(
        "control-expanded-jsonld",
        "serialization_control",
        "typed_fts",
        True,
        True,
        True,
        True,
        "full",
        serialization="JSON-LD",
    ),
    SystemSpec(
        "control-expanded-yamlld",
        "serialization_control",
        "typed_fts",
        True,
        True,
        True,
        True,
        "full",
        serialization="YAML-LD",
    ),
)


SYSTEM_BY_ID = {item.system_id: item for item in SYSTEMS}
PROPOSAL_ID = "proposal-okf-graph"


@dataclass
class InputContract:
    mode: str
    question_manifest: dict[str, Any]
    question_contract: dict[str, Any]
    verification_report: dict[str, Any] | None
    bundle_manifest: dict[str, Any]
    bundle_descriptor: dict[str, Any]
    question_manifest_sha256: str
    bundle_manifest_sha256: str
    snapshot_id: str
    expected_questions: int
    release_question_contract_passed: bool
    git_sha: str | None
    git_dirty: bool | None
    python_version: str
    sqlite_version: str


def runtime_provenance() -> dict[str, Any]:
    repository = Path(__file__).resolve().parents[2]
    git_sha: str | None = None
    git_dirty: bool | None = None
    try:
        revision = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        git_sha = revision.stdout.strip() or None
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        git_dirty = bool(status.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return {
        "git_sha": git_sha,
        "git_dirty": git_dirty,
        "python_version": platform.python_version(),
        "sqlite_version": sqlite3.sqlite_version,
    }


def verify_question_inputs(root: Path, mode: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None]:
    manifest_path = root / "manifest.json"
    contract_path = root / "contract.json"
    manifest = load_json(manifest_path)
    contract = load_json(contract_path)
    material = ""
    seen_manifest_paths: set[str] = set()
    for entry in manifest.get("files", []):
        if not isinstance(entry, dict):
            raise ValueError("question manifest file entry is not an object")
        relative = str(entry.get("path") or "")
        if not relative or relative in seen_manifest_paths:
            raise ValueError(f"question manifest path is missing or duplicated: {relative!r}")
        seen_manifest_paths.add(relative)
        path = safe_relative(root, relative)
        if not path.is_file():
            raise ValueError(f"question manifest file is missing: {relative}")
        digest = sha256_file(path)
        if digest != entry.get("sha256") or path.stat().st_size != entry.get("bytes"):
            raise ValueError(f"question manifest integrity failed: {relative}")
        material += f"{relative}\0{digest}\n"
    if sha256_text(material) != manifest.get("root_sha256"):
        raise ValueError("question manifest root hash is invalid")

    report_path = root / "verification-report.json"
    report: dict[str, Any] | None = load_json(report_path) if report_path.is_file() else None
    if report is not None:
        report_material = dict(report)
        report_digest = str(report_material.pop("report_sha256", ""))
        if report_digest != sha256_text(pretty_json(report_material)):
            raise ValueError("question verification report checksum is invalid")
        ledger = report.get("verification_ledger") or {}
        ledger_path = root / "verification-ledger.jsonl"
        if not ledger_path.is_file() or sha256_file(ledger_path) != ledger.get("sha256"):
            raise ValueError("question verification ledger integrity failed")

    if mode == "release":
        counts = manifest.get("counts") or {}
        failures: list[str] = []
        if counts.get("questions") != RELEASE_QUESTION_COUNT:
            failures.append(f"questions={counts.get('questions')} expected={RELEASE_QUESTION_COUNT}")
        if counts.get("primary_personas") != RELEASE_PERSONA_COUNT:
            failures.append(f"personas={counts.get('primary_personas')} expected={RELEASE_PERSONA_COUNT}")
        if counts.get("stories") != RELEASE_STORY_COUNT:
            failures.append(f"stories={counts.get('stories')} expected={RELEASE_STORY_COUNT}")
        if manifest.get("artifact_tier") != "release_candidate" or not manifest.get("publication_ready_candidate"):
            failures.append("question matrix is not a release candidate")
        if contract.get("artifact_tier") != "release_candidate" or not contract.get("publication_ready_candidate"):
            failures.append("question contract is not publication-ready")
        if report is None or not report.get("question_contract_passed"):
            failures.append("independent question verification did not pass the release contract")
        else:
            if report.get("counts", {}).get("questions") != RELEASE_QUESTION_COUNT:
                failures.append("independent verifier question count is not 28,800")
            if report.get("manifest_root_sha256") != manifest.get("root_sha256"):
                failures.append("independent verifier is bound to a different question manifest")
            ledger_summary = report.get("verification_ledger") or {}
            if (
                ledger_summary.get("count") != RELEASE_QUESTION_COUNT
                or ledger_summary.get("verified") != RELEASE_QUESTION_COUNT
                or ledger_summary.get("failed") != 0
            ):
                failures.append("independent verification ledger is not a complete all-pass release ledger")
            ledger_count = 0
            ledger_failures = 0
            for ledger_row in iter_jsonl(root / "verification-ledger.jsonl"):
                ledger_count += 1
                ledger_failures += ledger_row.get("gold_verification_status") != "verified"
            if ledger_count != RELEASE_QUESTION_COUNT or ledger_failures:
                failures.append("independent verification ledger records are incomplete or failed")
        if any(marker in str(manifest.get("snapshot_id", "")).casefold() for marker in ("fixture", "sample", "pending", "capacity")):
            failures.append("question snapshot is not release-eligible")
        if failures:
            raise ValueError("release question gate failed: " + "; ".join(failures))
    return manifest, contract, report


def verify_bundle_inputs(root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    descriptor = load_json(root / "okf-explorer.json")
    manifest = load_json(root / "data" / "manifest.json")
    if descriptor.get("entrypoints", {}).get("data_manifest") != "data/manifest.json":
        raise ValueError("bundle descriptor does not point to the canonical data manifest")
    if descriptor.get("counts") != manifest.get("counts"):
        raise ValueError("bundle descriptor and data manifest counts differ")
    jsonld_path = root / "okf-bundle.jsonld"
    yamlld_path = root / "okf-bundle.yamlld"
    if jsonld_path.is_file() != yamlld_path.is_file():
        raise ValueError("bundle must provide JSON-LD and YAML-LD together")
    if jsonld_path.is_file():
        jsonld = json.loads(jsonld_path.read_text(encoding="utf-8"))
        yamlld = yaml_load_subset(yamlld_path.read_text(encoding="utf-8"))
        if yamlld != jsonld:
            raise ValueError("bundle JSON-LD and YAML-LD control descriptors are not equivalent")
    checksums_path = root / "checksums.json"
    if checksums_path.is_file():
        checksum_document = load_json(checksums_path)
        entries = {str(item["path"]): item for item in checksum_document.get("files", []) if isinstance(item, dict) and item.get("path")}
        used = {"okf-explorer.json", "data/manifest.json", "okf-bundle.jsonld", "okf-bundle.yamlld"}
        for family in ("datasets", "relationships"):
            used.update(str(item) for item in manifest.get("chunks", {}).get(family, []))
        for relative in sorted(used):
            path = safe_relative(root, relative)
            entry = entries.get(relative)
            if entry is None or not path.is_file():
                raise ValueError(f"bundle checksum entry is missing: {relative}")
            if sha256_file(path) != entry.get("sha256") or path.stat().st_size != entry.get("bytes"):
                raise ValueError(f"bundle checksum failed: {relative}")
    return descriptor, manifest


def validate_input_contract(questions: Path, bundle: Path, mode: str) -> InputContract:
    question_manifest, question_contract, report = verify_question_inputs(questions, mode)
    descriptor, bundle_manifest = verify_bundle_inputs(bundle)
    question_snapshot = str(question_manifest.get("snapshot_id") or question_contract.get("snapshot", {}).get("snapshot_id") or "")
    bundle_snapshot = str(bundle_manifest.get("snapshot") or "")
    if not question_snapshot or question_snapshot != bundle_snapshot:
        raise ValueError(f"question/bundle snapshot mismatch: {question_snapshot!r} != {bundle_snapshot!r}")
    runtime = runtime_provenance()
    if mode == "release" and (not runtime["git_sha"] or runtime["git_dirty"] is not False):
        raise ValueError("release evaluation must run from a clean, identified Git commit")
    return InputContract(
        mode=mode,
        question_manifest=question_manifest,
        question_contract=question_contract,
        verification_report=report,
        bundle_manifest=bundle_manifest,
        bundle_descriptor=descriptor,
        question_manifest_sha256=sha256_file(questions / "manifest.json"),
        bundle_manifest_sha256=sha256_file(bundle / "data" / "manifest.json"),
        snapshot_id=bundle_snapshot,
        expected_questions=int(question_manifest.get("counts", {}).get("questions", 0)),
        release_question_contract_passed=bool(report and report.get("question_contract_passed")),
        **runtime,
    )


RECORD_SELECT = (
    "r.rid,r.open,r.content_id,r.url,r.title,r.description,r.publisher,r.document_type,"
    "r.schema_name,r.language,r.jurisdiction,r.lifecycle,r.evidence_url,r.evidence_sha256,"
    "r.confidence,r.record_sha256,r.record_shard"
)


class BundleIndex:
    def __init__(self, bundle: Path, database: Path, contract: InputContract) -> None:
        self.bundle = bundle.resolve()
        self.database = database
        self.contract = contract
        database.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(database)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=NORMAL")
        self.connection.execute("PRAGMA temp_store=FILE")
        self.connection.execute("PRAGMA cache_size=-32768")
        self.primary_source = ""
        try:
            if not self._reusable():
                self._build()
            self.primary_source = self._meta("primary_source") or ""
        except Exception:
            self.connection.close()
            raise

    def close(self) -> None:
        self.connection.close()

    def _meta(self, key: str) -> str | None:
        try:
            row = self.connection.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        except sqlite3.OperationalError:
            return None
        return str(row[0]) if row else None

    def _reusable(self) -> bool:
        return (
            self._meta("complete") == "true"
            and self._meta("bundle_manifest_sha256") == self.contract.bundle_manifest_sha256
            and self._meta("snapshot_id") == self.contract.snapshot_id
        )

    def _build(self) -> None:
        self.connection.executescript(
            """
            DROP TABLE IF EXISTS metadata;
            DROP TABLE IF EXISTS records;
            DROP TABLE IF EXISTS aliases;
            DROP TABLE IF EXISTS record_sources;
            DROP TABLE IF EXISTS edges;
            DROP TABLE IF EXISTS fts_flat;
            DROP TABLE IF EXISTS fts_native;
            DROP TABLE IF EXISTS fts_typed;
            CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE records (
              rid INTEGER PRIMARY KEY,
              open TEXT NOT NULL UNIQUE,
              content_id TEXT,
              url TEXT NOT NULL,
              title TEXT NOT NULL,
              title_key TEXT NOT NULL,
              description TEXT NOT NULL,
              publisher TEXT NOT NULL,
              document_type TEXT NOT NULL,
              schema_name TEXT NOT NULL,
              language TEXT NOT NULL,
              jurisdiction TEXT NOT NULL,
              lifecycle TEXT NOT NULL,
              evidence_url TEXT NOT NULL,
              evidence_sha256 TEXT NOT NULL,
              confidence TEXT NOT NULL,
              record_sha256 TEXT NOT NULL,
              tags TEXT NOT NULL,
              inferred_text TEXT NOT NULL,
              record_shard TEXT NOT NULL
            );
            CREATE TABLE aliases (alias TEXT PRIMARY KEY, rid INTEGER NOT NULL REFERENCES records(rid));
            CREATE TABLE record_sources (
              rid INTEGER NOT NULL REFERENCES records(rid),
              source TEXT NOT NULL,
              PRIMARY KEY (rid, source)
            );
            CREATE INDEX record_sources_source ON record_sources(source, rid);
            CREATE TABLE edges (
              source TEXT NOT NULL,
              predicate TEXT NOT NULL,
              target TEXT NOT NULL,
              source_native_predicate TEXT NOT NULL,
              assertion_status TEXT NOT NULL,
              evidence_url TEXT NOT NULL,
              evidence_sha256 TEXT NOT NULL,
              edge_shard TEXT NOT NULL,
              synthetic INTEGER NOT NULL DEFAULT 0,
              PRIMARY KEY (source, predicate, target, source_native_predicate)
            );
            CREATE INDEX edges_source ON edges(source);
            CREATE INDEX edges_target ON edges(target);
            CREATE VIRTUAL TABLE fts_flat USING fts5(title, description, url, tokenize='unicode61 remove_diacritics 2');
            CREATE VIRTUAL TABLE fts_native USING fts5(
              title, description, publisher, document_type, schema_name, tags, url,
              tokenize='unicode61 remove_diacritics 2'
            );
            CREATE VIRTUAL TABLE fts_typed USING fts5(
              title, description, publisher, document_type, schema_name, tags, url, inferred_text,
              tokenize='unicode61 remove_diacritics 2'
            );
            """
        )
        rid = 0
        source_counts: Counter[str] = Counter()
        record_count = 0
        for relative in self.contract.bundle_manifest.get("chunks", {}).get("datasets", []):
            path = safe_relative(self.bundle, str(relative))
            for row in iter_json_array(path):
                rid += 1
                record_count += 1
                route = _normalise_space(row.get("open"))
                if not route:
                    raise ValueError(f"bundle record has no runtime route in {relative}")
                content_id = _normalise_space(row.get("canonical_content_id") or row.get("content_id")) or None
                url = _normalise_space(row.get("url") or row.get("@id"))
                title = _normalise_space(row.get("title"))
                if not url or not title:
                    raise ValueError(f"bundle record lacks URL/title: {route}")
                description = _normalise_space(row.get("description") or row.get("notes"))
                publisher = _normalise_space(row.get("publisher_title") or row.get("publisher"))
                document_type = _normalise_space(row.get("document_type") or row.get("record_type"))
                schema_name = _normalise_space(row.get("schema_name"))
                language = _normalise_space(row.get("language") or "en")
                jurisdiction = _flatten_text(row.get("jurisdiction"))
                lifecycle = _normalise_space(row.get("lifecycle") or row.get("status"))
                evidence_url = _normalise_space(row.get("evidence_url"))
                evidence_sha256 = _normalise_space(row.get("evidence_sha256"))
                confidence = _normalise_space(row.get("confidence"))
                record_sha256 = sha256_text(canonical_json(row))
                tags = _flatten_text(row.get("tags"))
                inferred_text = _flatten_text(
                    {
                        "inferred_labels": row.get("inferred_labels"),
                        "inferred_keywords": row.get("inferred_keywords"),
                        "enrichment": row.get("enrichment"),
                    }
                )
                values = (
                    rid,
                    route,
                    content_id,
                    url,
                    title,
                    title.casefold(),
                    description,
                    publisher,
                    document_type,
                    schema_name,
                    language,
                    jurisdiction,
                    lifecycle,
                    evidence_url,
                    evidence_sha256,
                    confidence,
                    record_sha256,
                    tags,
                    inferred_text,
                    str(relative),
                )
                self.connection.execute(
                    "INSERT INTO records VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    values,
                )
                self.connection.execute("INSERT INTO fts_flat(rowid,title,description,url) VALUES (?,?,?,?)", (rid, title, description, url))
                self.connection.execute(
                    "INSERT INTO fts_native(rowid,title,description,publisher,document_type,schema_name,tags,url) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (rid, title, description, publisher, document_type, schema_name, tags, url),
                )
                self.connection.execute(
                    "INSERT INTO fts_typed(rowid,title,description,publisher,document_type,schema_name,tags,url,inferred_text) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (rid, title, description, publisher, document_type, schema_name, tags, url, inferred_text),
                )
                aliases = {route, url, url.rstrip("/"), str(row.get("@id") or "")}
                if content_id:
                    aliases.update({content_id, f"content:{content_id}:{language}"})
                for alias in sorted(item for item in aliases if item):
                    self.connection.execute("INSERT OR IGNORE INTO aliases(alias,rid) VALUES (?,?)", (alias, rid))
                memberships = row.get("source_memberships") or []
                if not isinstance(memberships, list):
                    memberships = []
                for source in sorted({_normalise_space(item) for item in memberships if _normalise_space(item)}):
                    self.connection.execute("INSERT OR IGNORE INTO record_sources(rid,source) VALUES (?,?)", (rid, source))
                    source_counts[source] += 1
                for predicate, target in (
                    ("has_content_type", f"content-type:{document_type}"),
                    ("uses_schema_family", f"schema-family:{schema_name}"),
                ):
                    if target.rsplit(":", 1)[-1]:
                        self.connection.execute(
                            "INSERT OR IGNORE INTO edges VALUES (?,?,?,?,?,?,?,?,1)",
                            (route, predicate, target, predicate, "source-declared", evidence_url, evidence_sha256, str(relative)),
                        )
        expected_records = int(self.contract.bundle_manifest.get("counts", {}).get("datasets", -1))
        if record_count != expected_records:
            raise ValueError(f"bundle record count mismatch: {record_count} != {expected_records}")

        edge_count = 0
        for relative in self.contract.bundle_manifest.get("chunks", {}).get("relationships", []):
            path = safe_relative(self.bundle, str(relative))
            for row in iter_json_array(path):
                source = _normalise_space(row.get("source"))
                target = _normalise_space(row.get("target"))
                predicate = normalise_predicate(row.get("kind") or row.get("source_native_predicate"))
                native = normalise_predicate(row.get("source_native_predicate") or row.get("kind"))
                if not source or not target or not predicate:
                    raise ValueError(f"invalid relationship in {relative}")
                self.connection.execute(
                    "INSERT OR IGNORE INTO edges VALUES (?,?,?,?,?,?,?,?,0)",
                    (
                        source,
                        predicate,
                        target,
                        native,
                        _normalise_space(row.get("assertion_status")),
                        _normalise_space(row.get("evidence_url")),
                        _normalise_space(row.get("evidence_sha256")),
                        str(relative),
                    ),
                )
                edge_count += 1
        expected_edges = int(self.contract.bundle_manifest.get("counts", {}).get("relationships", -1))
        if edge_count != expected_edges:
            raise ValueError(f"bundle relationship count mismatch: {edge_count} != {expected_edges}")
        primary_source = ""
        if source_counts:
            primary_source = sorted(source_counts.items(), key=lambda item: (-item[1], item[0]))[0][0]
        metadata = {
            "complete": "true",
            "bundle_manifest_sha256": self.contract.bundle_manifest_sha256,
            "snapshot_id": self.contract.snapshot_id,
            "records": str(record_count),
            "relationships": str(edge_count),
            "primary_source": primary_source,
        }
        self.connection.executemany("INSERT INTO metadata(key,value) VALUES (?,?)", sorted(metadata.items()))
        self.connection.execute("INSERT INTO fts_flat(fts_flat) VALUES ('optimize')")
        self.connection.execute("INSERT INTO fts_native(fts_native) VALUES ('optimize')")
        self.connection.execute("INSERT INTO fts_typed(fts_typed) VALUES ('optimize')")
        self.connection.commit()

    def resolve_alias(self, alias: str) -> str | None:
        candidates = [alias, alias.rstrip("/")]
        for value in candidates:
            row = self.connection.execute(
                "SELECT r.open FROM aliases a JOIN records r ON r.rid=a.rid WHERE a.alias=?",
                (value,),
            ).fetchone()
            if row:
                return str(row[0])
        return None

    def resolve_target(self, target: dict[str, Any]) -> str | None:
        for value in (
            target.get("identity"),
            target.get("content_id"),
            target.get("url"),
        ):
            if value and (resolved := self.resolve_alias(str(value))):
                return resolved
        return None

    def _row(self, row: sqlite3.Row) -> dict[str, Any]:
        return {key: row[key] for key in row.keys()}

    def _exact(self, wording: str, spec: SystemSpec, limit: int) -> list[dict[str, Any]]:
        phrases = quoted_phrases(wording)
        urls = [item.rstrip(".,;:!?)]}\u201d\"") for item in re.findall(r"https://www\.gov\.uk/[^\s<]+", wording)]
        routes = sorted({route for url in urls if (route := self.resolve_alias(url))})
        if not phrases and not routes:
            return []
        conditions: list[str] = []
        params: list[Any] = []
        if phrases:
            conditions.append(f"r.title_key IN ({','.join('?' for _ in phrases)})")
            params.extend(phrases)
        if routes:
            conditions.append(f"r.open IN ({','.join('?' for _ in routes)})")
            params.extend(routes)
        source_join = " JOIN record_sources rs ON rs.rid=r.rid " if spec.single_source else ""
        source_filter = " AND rs.source=?" if spec.single_source else ""
        if spec.single_source:
            params.append(self.primary_source)
        params.append(limit)
        rows = self.connection.execute(
            f"SELECT {RECORD_SELECT} FROM records r {source_join} WHERE ({' OR '.join(conditions)}){source_filter} "
            "ORDER BY r.title_key,r.open LIMIT ?",
            params,
        )
        return [self._row(row) for row in rows]

    def _fts(self, wording: str, spec: SystemSpec, limit: int) -> list[dict[str, Any]]:
        table = {"flat_fts": "fts_flat", "native_fts": "fts_native", "typed_fts": "fts_typed"}[spec.retrieval]
        tokens = lexical_tokens(wording)
        if not tokens:
            tokens = [phrase for phrase in quoted_phrases(wording) if phrase][:1]
        if not tokens:
            return []
        query = " OR ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens)
        source_join = " JOIN record_sources rs ON rs.rid=r.rid " if spec.single_source else ""
        source_filter = " AND rs.source=?" if spec.single_source else ""
        params: list[Any] = [query]
        if spec.single_source:
            params.append(self.primary_source)
        params.append(limit)
        rows = self.connection.execute(
            f"SELECT {RECORD_SELECT},bm25({table}) AS fts_score FROM {table} f "
            f"JOIN records r ON r.rid=f.rowid {source_join} WHERE {table} MATCH ?{source_filter} "
            "ORDER BY fts_score,r.open LIMIT ?",
            params,
        )
        return [self._row(row) for row in rows]

    def _edges(self, routes: Sequence[str], *, include_inferred: bool, synthetic_only: bool = False) -> list[dict[str, Any]]:
        if not routes:
            return []
        placeholders = ",".join("?" for _ in routes)
        conditions = [f"(source IN ({placeholders}) OR target IN ({placeholders}))"]
        params: list[Any] = list(routes) + list(routes)
        if not include_inferred:
            conditions.append("assertion_status NOT LIKE '%inferred%'")
        if synthetic_only:
            conditions.append("synthetic=1")
        cursor = self.connection.execute(
            "SELECT source,predicate,target,source_native_predicate,assertion_status,evidence_url,evidence_sha256,edge_shard,synthetic "
            f"FROM edges WHERE {' AND '.join(conditions)} ORDER BY source,predicate,target LIMIT 1000",
            params,
        )
        return [self._row(row) for row in cursor]

    def _records_for_routes(self, routes: Iterable[str]) -> dict[str, dict[str, Any]]:
        route_list = sorted(set(routes))
        if not route_list:
            return {}
        placeholders = ",".join("?" for _ in route_list)
        cursor = self.connection.execute(
            f"SELECT {RECORD_SELECT} FROM records r WHERE r.open IN ({placeholders})",
            route_list,
        )
        return {str(row["open"]): self._row(row) for row in cursor}

    def search(self, spec: SystemSpec, wording: str) -> dict[str, Any]:
        start = time.perf_counter_ns()
        bytes_read = len(wording.encode("utf-8"))
        query_steps = 1
        tool_calls = 0
        shards: set[str] = set()
        if spec.answerability_classifier and deterministic_abstention(wording):
            elapsed = time.perf_counter_ns() - start
            result = {
                "abstained": True,
                "abstention_reason": "unsupported_guarantee_or_missing_case_facts",
                "ranked_results": [],
                "predicted_relationships": [],
                "citation": None,
            }
            return {
                "result": result,
                "result_sha256": sha256_text(canonical_json(result)),
                "efficiency": {
                    "latency_ns": elapsed,
                    "tool_calls": tool_calls,
                    "query_steps": query_steps,
                    "bytes_read": bytes_read,
                    "shards_read": 0,
                    "model_calls": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cost_gbp": 0.0,
                    "network_requests": 0,
                },
            }

        candidates = self._exact(wording, spec, 50) if spec.retrieval == "exact" else self._fts(wording, spec, 50)
        tool_calls += 1
        phrases = quoted_phrases(wording)
        scored: dict[str, tuple[float, dict[str, Any]]] = {}
        for rank, row in enumerate(candidates, start=1):
            score = 10_000.0 - rank * 100.0
            if row["title"].casefold() in phrases:
                score += 10_000.0
            if spec.trust_signals:
                score += 5.0 if row.get("evidence_sha256") else 0.0
                score += 2.0 if row.get("lifecycle") else 0.0
            scored[str(row["open"])] = (score, row)
            shards.add(str(row["record_shard"]))
            bytes_read += len(canonical_json(row).encode("utf-8"))

        graph_edges: list[dict[str, Any]] = []
        if spec.graph_enabled and candidates:
            seed_routes = [str(item["open"]) for item in candidates[:5]]
            graph_edges = self._edges(seed_routes, include_inferred=spec.inferred_enabled)
            tool_calls += 1
            query_steps += 1
            bytes_read += sum(len(canonical_json(edge).encode("utf-8")) for edge in graph_edges)
            shards.update(str(edge["edge_shard"]) for edge in graph_edges)
            neighbour_routes = {
                endpoint
                for edge in graph_edges
                for endpoint in (str(edge["source"]), str(edge["target"]))
                if endpoint not in scored
            }
            neighbours = self._records_for_routes(neighbour_routes)
            floor = min((item[0] for item in scored.values()), default=0.0) - 500.0
            for route, row in neighbours.items():
                scored[route] = (floor, row)
                shards.add(str(row["record_shard"]))
                bytes_read += len(canonical_json(row).encode("utf-8"))

        ranked = sorted(scored.values(), key=lambda item: (-item[0], str(item[1]["open"])))[:RESULT_LIMIT]
        ranked_results = [
            {
                "rank": index,
                "route": str(row["open"]),
                "content_id": row.get("content_id"),
                "url": str(row["url"]),
                "title": str(row["title"]),
                "score": round(score, 6),
            }
            for index, (score, row) in enumerate(ranked, start=1)
        ]
        top = ranked[0][1] if ranked else None
        predicted_edges: list[dict[str, Any]] = []
        if top is not None:
            if spec.graph_enabled:
                predicted_edges = [
                    edge for edge in graph_edges if str(top["open"]) in {str(edge["source"]), str(edge["target"])}
                ]
            elif spec.retrieval in {"native_fts", "typed_fts"}:
                predicted_edges = self._edges([str(top["open"])], include_inferred=False, synthetic_only=True)
                if predicted_edges:
                    tool_calls += 1
                    query_steps += 1
                    bytes_read += sum(len(canonical_json(edge).encode("utf-8")) for edge in predicted_edges)
                    shards.update(str(edge["edge_shard"]) for edge in predicted_edges)
        predicted_relationships = sorted(
            {
                normalise_predicate(value)
                for edge in predicted_edges
                for value in (edge.get("predicate"), edge.get("source_native_predicate"))
                if value
            }
        )
        citation: dict[str, Any] | None = None
        if top is not None and spec.citation_mode != "none":
            citation = {
                "canonical_url": str(top["url"]),
                "content_id": top.get("content_id"),
                "snapshot_id": self.contract.snapshot_id,
            }
            if spec.citation_mode == "full":
                citation.update(
                    {
                        "source_evidence_url": str(top.get("evidence_url") or ""),
                        "source_evidence_sha256": str(top.get("evidence_sha256") or ""),
                        "record_sha256": str(top.get("record_sha256") or ""),
                        "snapshot_manifest_sha256": self.contract.bundle_manifest_sha256,
                        "confidence": str(top.get("confidence") or ""),
                        "lifecycle": str(top.get("lifecycle") or ""),
                    }
                )
        result = {
            "abstained": False,
            "abstention_reason": None,
            "ranked_results": ranked_results,
            "predicted_relationships": predicted_relationships,
            "citation": citation,
        }
        elapsed = time.perf_counter_ns() - start
        return {
            "result": result,
            "result_sha256": sha256_text(canonical_json(result)),
            "efficiency": {
                "latency_ns": elapsed,
                "tool_calls": tool_calls,
                "query_steps": query_steps,
                "bytes_read": bytes_read,
                "shards_read": len(shards),
                "model_calls": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cost_gbp": 0.0,
                "network_requests": 0,
            },
        }


def _dcg(relevances: Sequence[int]) -> float:
    return sum(((2**grade) - 1) / math.log2(index + 2) for index, grade in enumerate(relevances))


def grade_result(question: dict[str, Any], search: dict[str, Any], index: BundleIndex) -> tuple[dict[str, float | None], list[str], dict[str, Any]]:
    result = search["result"]
    expected_unanswerable = bool(question.get("expected_unanswerable"))
    gold = question.get("gold") or {}
    primary_targets = [item for item in gold.get("primary_targets") or [] if isinstance(item, dict)]
    primary_routes = {route for target in primary_targets if (route := index.resolve_target(target))}
    graded_routes: dict[str, int] = {route: 3 for route in primary_routes}
    for path in gold.get("expected_paths") or []:
        for node in path.get("nodes") or []:
            if isinstance(node, dict) and (route := index.resolve_target(node)):
                graded_routes[route] = max(graded_routes.get(route, 0), 1)
    ranked_routes = [str(item["route"]) for item in result.get("ranked_results") or []]
    answerable = not expected_unanswerable
    retrieved_primary = [route for route in ranked_routes[:RESULT_LIMIT] if route in primary_routes]
    recall10 = (len(set(retrieved_primary)) / len(primary_routes)) if answerable and primary_routes else (0.0 if answerable else None)
    reciprocal_rank = next((1.0 / rank for rank, route in enumerate(ranked_routes[:RESULT_LIMIT], start=1) if route in primary_routes), 0.0)
    mrr10 = reciprocal_rank if answerable else None
    observed_relevance = [graded_routes.get(route, 0) for route in ranked_routes[:RESULT_LIMIT]]
    ideal_relevance = sorted(graded_routes.values(), reverse=True)[:RESULT_LIMIT]
    ideal_dcg = _dcg(ideal_relevance)
    ndcg10 = (_dcg(observed_relevance) / ideal_dcg if ideal_dcg else 0.0) if answerable else None
    top_route = ranked_routes[0] if ranked_routes else None
    top_content_id = (result.get("ranked_results") or [{}])[0].get("content_id") if ranked_routes else None
    exact_content_id = (
        1.0 if top_content_id and top_content_id in set(gold.get("content_ids") or []) else 0.0
    ) if answerable else None

    expected_predicates = {
        normalise_predicate(value)
        for value in question.get("target_relationships") or []
        if normalise_predicate(value)
    }
    predicted_predicates = {
        normalise_predicate(value)
        for value in result.get("predicted_relationships") or []
        if normalise_predicate(value)
    }
    if expected_predicates and answerable:
        intersection = expected_predicates & predicted_predicates
        relationship_precision = len(intersection) / len(predicted_predicates) if predicted_predicates else 0.0
        relationship_recall = len(intersection) / len(expected_predicates)
        relationship_f1 = (
            2 * relationship_precision * relationship_recall / (relationship_precision + relationship_recall)
            if relationship_precision + relationship_recall
            else 0.0
        )
    else:
        relationship_precision = relationship_recall = relationship_f1 = None

    citation = result.get("citation")
    citation_correctness: float | None
    provenance_completeness: float | None
    provenance_evidence_match: float | None
    if answerable:
        citation_matches = False
        evidence_matches = False
        if isinstance(citation, dict):
            for target in primary_targets:
                id_matches = not target.get("content_id") or citation.get("content_id") == target.get("content_id")
                url_matches = not target.get("url") or str(citation.get("canonical_url", "")).rstrip("/") == str(target.get("url", "")).rstrip("/")
                citation_matches = citation_matches or (id_matches and url_matches and top_route in primary_routes)
                evidence_matches = evidence_matches or (
                    bool(target.get("source_evidence_sha256"))
                    and citation.get("source_evidence_sha256") == target.get("source_evidence_sha256")
                    and citation.get("source_evidence_url") == target.get("source_evidence_url")
                )
            required = [
                "canonical_url",
                "snapshot_id",
                "snapshot_manifest_sha256",
                "record_sha256",
                "source_evidence_url",
                "source_evidence_sha256",
            ]
            if any(target.get("content_id") for target in primary_targets):
                required.append("content_id")
            provenance_completeness = sum(bool(citation.get(field)) for field in required) / len(required)
        else:
            provenance_completeness = 0.0
        citation_correctness = float(citation_matches)
        provenance_evidence_match = float(evidence_matches)
    else:
        citation_correctness = provenance_completeness = provenance_evidence_match = None

    abstained = bool(result.get("abstained"))
    answerability_accuracy = float(abstained == expected_unanswerable)
    if expected_unanswerable:
        end_task_success = float(abstained)
    else:
        end_task_success = float(not abstained and bool(retrieved_primary) and citation_correctness == 1.0)
    failures: list[str] = []
    if answerable and not retrieved_primary:
        failures.append("target_not_in_top_10")
    if answerable and top_route not in primary_routes:
        failures.append("wrong_top_result")
    if answerable and abstained:
        failures.append("inappropriate_abstention")
    if expected_unanswerable and not abstained:
        failures.append("missed_required_abstention")
    if relationship_recall is not None and relationship_recall < 1.0:
        failures.append("relationship_recall_shortfall")
    if citation_correctness == 0.0:
        failures.append("citation_incorrect_or_missing")
    if provenance_completeness is not None and provenance_completeness < 1.0:
        failures.append("provenance_incomplete")
    if provenance_evidence_match == 0.0:
        failures.append("provenance_evidence_mismatch")
    metrics: dict[str, float | None] = {
        "recall_at_10": recall10,
        "mrr_at_10": mrr10,
        "ndcg_at_10": ndcg10,
        "exact_content_id_match": exact_content_id,
        "relationship_precision": relationship_precision,
        "relationship_recall": relationship_recall,
        "relationship_f1": relationship_f1,
        "citation_correctness": citation_correctness,
        "citation_precision": citation_correctness,
        "citation_recall": citation_correctness,
        "provenance_completeness": provenance_completeness,
        "provenance_evidence_match": provenance_evidence_match,
        "answerability_accuracy": answerability_accuracy,
        "end_task_success": end_task_success,
    }
    gold_summary = {
        "expected_unanswerable": expected_unanswerable,
        "primary_routes": sorted(primary_routes),
        "graded_routes": dict(sorted(graded_routes.items())),
        "expected_relationships": sorted(expected_predicates),
    }
    return metrics, failures, gold_summary


METRIC_COLUMNS = (
    "recall_at_10",
    "mrr_at_10",
    "ndcg_at_10",
    "exact_content_id_match",
    "relationship_precision",
    "relationship_recall",
    "relationship_f1",
    "citation_correctness",
    "citation_precision",
    "citation_recall",
    "provenance_completeness",
    "provenance_evidence_match",
    "answerability_accuracy",
    "end_task_success",
)


EFFICIENCY_COLUMNS = (
    "latency_ns",
    "tool_calls",
    "query_steps",
    "bytes_read",
    "shards_read",
    "model_calls",
    "input_tokens",
    "output_tokens",
    "network_requests",
    "cost_gbp",
)


class OutcomeStore:
    def __init__(self, path: Path, run_key: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=NORMAL")
        self.connection.execute("PRAGMA temp_store=FILE")
        self.connection.execute("PRAGMA cache_size=-32768")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS questions (
              question_id TEXT PRIMARY KEY,
              checksum TEXT NOT NULL,
              story_id TEXT NOT NULL,
              split_group TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS outcomes (
              question_id TEXT NOT NULL,
              system_id TEXT NOT NULL,
              persona_id TEXT NOT NULL,
              story_id TEXT NOT NULL,
              story_role TEXT NOT NULL,
              operation TEXT NOT NULL,
              challenge TEXT NOT NULL,
              risk TEXT NOT NULL,
              difficulty TEXT NOT NULL,
              locale TEXT NOT NULL,
              jurisdiction TEXT NOT NULL,
              split TEXT NOT NULL,
              split_group TEXT NOT NULL,
              discovery_stage TEXT NOT NULL,
              expected_answerable INTEGER NOT NULL,
              abstained INTEGER NOT NULL,
              result_sha256 TEXT NOT NULL,
              recall_at_10 REAL,
              mrr_at_10 REAL,
              ndcg_at_10 REAL,
              exact_content_id_match REAL,
              relationship_precision REAL,
              relationship_recall REAL,
              relationship_f1 REAL,
              citation_correctness REAL,
              citation_precision REAL,
              citation_recall REAL,
              provenance_completeness REAL,
              provenance_evidence_match REAL,
              answerability_accuracy REAL NOT NULL,
              end_task_success REAL NOT NULL,
              latency_ns INTEGER NOT NULL,
              tool_calls INTEGER NOT NULL,
              query_steps INTEGER NOT NULL,
              bytes_read INTEGER NOT NULL,
              shards_read INTEGER NOT NULL,
              model_calls INTEGER NOT NULL,
              input_tokens INTEGER NOT NULL,
              output_tokens INTEGER NOT NULL,
              network_requests INTEGER NOT NULL,
              cost_gbp REAL NOT NULL,
              failure_codes TEXT NOT NULL,
              trace_json TEXT NOT NULL,
              PRIMARY KEY (question_id, system_id)
            );
            CREATE INDEX IF NOT EXISTS outcomes_system ON outcomes(system_id, question_id);
            CREATE INDEX IF NOT EXISTS outcomes_pair ON outcomes(system_id, split_group, question_id);
            """
        )
        existing = self.connection.execute("SELECT value FROM metadata WHERE key='run_key'").fetchone()
        if existing and existing[0] != run_key:
            raise ValueError("resume state belongs to different inputs or system contract")
        self.connection.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES ('run_key',?)", (run_key,))
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def completed_systems(self, question_id: str) -> set[str]:
        return {str(row[0]) for row in self.connection.execute("SELECT system_id FROM outcomes WHERE question_id=?", (question_id,))}

    def register_question(self, question: dict[str, Any]) -> None:
        row = self.connection.execute("SELECT checksum FROM questions WHERE question_id=?", (question["question_id"],)).fetchone()
        if row and row[0] != question.get("checksum"):
            raise ValueError(f"question changed during resume: {question['question_id']}")
        self.connection.execute(
            "INSERT OR IGNORE INTO questions VALUES (?,?,?,?)",
            (question["question_id"], question["checksum"], question["story_id"], question["split_group"]),
        )

    def add(self, trace: dict[str, Any]) -> None:
        metrics = trace["metrics"]
        efficiency = trace["efficiency"]
        question = trace["question"]
        dimensions = trace["dimensions"]
        values: list[Any] = [
            question["question_id"],
            trace["system"]["system_id"],
            dimensions["persona_id"],
            dimensions["story_id"],
            dimensions["story_role"],
            dimensions["operation"],
            dimensions["challenge"],
            dimensions["risk"],
            dimensions["difficulty"],
            dimensions["locale"],
            dimensions["jurisdiction"],
            dimensions["split"],
            dimensions["split_group"],
            dimensions["discovery_stage"],
            int(not trace["gold"]["expected_unanswerable"]),
            int(trace["output"]["abstained"]),
            trace["output_sha256"],
        ]
        values.extend(metrics[column] for column in METRIC_COLUMNS)
        values.extend(
            [
                efficiency["latency_ns"],
                efficiency["tool_calls"],
                efficiency["query_steps"],
                efficiency["bytes_read"],
                efficiency["shards_read"],
                efficiency["model_calls"],
                efficiency["input_tokens"],
                efficiency["output_tokens"],
                efficiency["network_requests"],
                efficiency["cost_gbp"],
                canonical_json(trace["failure_codes"]),
                canonical_json(trace),
            ]
        )
        placeholders = ",".join("?" for _ in values)
        self.connection.execute(f"INSERT OR REPLACE INTO outcomes VALUES ({placeholders})", values)

    def commit(self) -> None:
        self.connection.commit()


def iter_questions(root: Path) -> Iterator[dict[str, Any]]:
    seen: set[str] = set()
    for path in sorted((root / "bindings").glob("*.jsonl")):
        for question in iter_jsonl(path):
            question_id = str(question.get("question_id") or "")
            if not question_id or question_id in seen:
                raise ValueError(f"missing or duplicate question identifier: {question_id!r}")
            if not checked_record(question):
                raise ValueError(f"question checksum failed: {question_id}")
            seen.add(question_id)
            yield question


def make_trace(
    *,
    run_id: str,
    system: SystemSpec,
    question: dict[str, Any],
    search: dict[str, Any],
    metrics: dict[str, float | None],
    failures: list[str],
    gold: dict[str, Any],
    contract: InputContract,
) -> dict[str, Any]:
    persona_ids = question.get("persona_ids") or ["unknown"]
    jurisdiction = question.get("jurisdiction") or []
    if not isinstance(jurisdiction, list):
        jurisdiction = [jurisdiction]
    return {
        "schema": TRACE_SCHEMA,
        "harness_version": HARNESS_VERSION,
        "run_id": run_id,
        "system": asdict(system),
        "input_contract": {
            "snapshot_id": contract.snapshot_id,
            "question_manifest_sha256": contract.question_manifest_sha256,
            "bundle_manifest_sha256": contract.bundle_manifest_sha256,
            "matched_conditions": True,
            "git_sha": contract.git_sha,
            "git_dirty": contract.git_dirty,
            "python_version": contract.python_version,
            "sqlite_version": contract.sqlite_version,
        },
        "question": {
            "question_id": question["question_id"],
            "checksum": question["checksum"],
            "wording": question["wording"],
        },
        "dimensions": {
            "persona_id": str(persona_ids[0]),
            "story_id": str(question.get("story_id") or ""),
            "story_role": str(question.get("story_role") or "unknown"),
            "operation": str(question.get("operation") or "unknown"),
            "challenge": str(question.get("challenge") or "unknown"),
            "risk": str(question.get("risk") or "unknown"),
            "difficulty": str(question.get("difficulty") or "unknown"),
            "locale": str(question.get("locale") or "unknown"),
            "jurisdiction": "|".join(sorted(str(item) for item in jurisdiction)),
            "split": str(question.get("split") or "unknown"),
            "split_group": str(question.get("split_group") or question.get("story_id") or "unknown"),
            "discovery_stage": str(question.get("discovery_stage") or "unknown"),
        },
        "gold": gold,
        "output": search["result"],
        "output_sha256": search["result_sha256"],
        "metrics": metrics,
        "efficiency": search["efficiency"],
        "failure_codes": sorted(set(failures)),
        "usage": {
            "model_calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cost_gbp": 0.0,
            "network_requests": 0,
        },
    }


def _round(value: float | None) -> float | None:
    return round(float(value), 9) if value is not None else None


def percentile(connection: sqlite3.Connection, system_id: str, column: str, fraction: float) -> float | None:
    if column not in EFFICIENCY_COLUMNS:
        raise ValueError(f"unsupported percentile column: {column}")
    count = int(connection.execute(f"SELECT COUNT({column}) FROM outcomes WHERE system_id=?", (system_id,)).fetchone()[0])
    if not count:
        return None
    offset = max(0, min(count - 1, math.ceil(fraction * count) - 1))
    row = connection.execute(
        f"SELECT {column} FROM outcomes WHERE system_id=? ORDER BY {column} LIMIT 1 OFFSET ?",
        (system_id, offset),
    ).fetchone()
    return float(row[0]) if row else None


def aggregate_metrics(connection: sqlite3.Connection) -> dict[str, Any]:
    systems: dict[str, Any] = {}
    select_averages = ",".join(f"AVG({column})" for column in METRIC_COLUMNS + EFFICIENCY_COLUMNS)
    for system_id in sorted(SYSTEM_BY_ID):
        row = connection.execute(
            f"SELECT COUNT(*),SUM(expected_answerable),SUM(1-expected_answerable),SUM(abstained),{select_averages} "
            "FROM outcomes WHERE system_id=?",
            (system_id,),
        ).fetchone()
        averages = {
            column: _round(row[4 + index])
            for index, column in enumerate(METRIC_COLUMNS + EFFICIENCY_COLUMNS)
        }
        confusion = connection.execute(
            "SELECT "
            "SUM(CASE WHEN expected_answerable=0 AND abstained=1 THEN 1 ELSE 0 END),"
            "SUM(CASE WHEN expected_answerable=0 AND abstained=0 THEN 1 ELSE 0 END),"
            "SUM(CASE WHEN expected_answerable=1 AND abstained=1 THEN 1 ELSE 0 END),"
            "SUM(CASE WHEN expected_answerable=1 AND abstained=0 THEN 1 ELSE 0 END) "
            "FROM outcomes WHERE system_id=?",
            (system_id,),
        ).fetchone()
        tp, fn, fp, tn = (int(value or 0) for value in confusion)
        averages.update(
            {
                "abstention_precision": _round(tp / (tp + fp)) if tp + fp else None,
                "abstention_recall": _round(tp / (tp + fn)) if tp + fn else None,
                "latency_ms_mean": _round((averages["latency_ns"] or 0.0) / 1_000_000),
                "latency_ms_p50": _round((percentile(connection, system_id, "latency_ns", 0.50) or 0.0) / 1_000_000),
                "latency_ms_p95": _round((percentile(connection, system_id, "latency_ns", 0.95) or 0.0) / 1_000_000),
                "latency_ms_p99": _round((percentile(connection, system_id, "latency_ns", 0.99) or 0.0) / 1_000_000),
            }
        )
        averages.pop("latency_ns", None)
        systems[system_id] = {
            "role": SYSTEM_BY_ID[system_id].role,
            "questions": int(row[0]),
            "answerable_questions": int(row[1] or 0),
            "deliberately_unanswerable_questions": int(row[2] or 0),
            "abstained": int(row[3] or 0),
            "abstention_confusion": {"true_positive": tp, "false_negative": fn, "false_positive": fp, "true_negative": tn},
            "metrics": averages,
        }
    return {
        "schema_version": 1,
        "harness_version": HARNESS_VERSION,
        "metric_definitions": {
            "retrieval": ["Recall@10", "MRR@10", "nDCG@10", "exact content ID at rank 1"],
            "relationships": ["predicate precision", "predicate recall", "predicate F1"],
            "trust": ["citation correctness", "provenance completeness", "source-evidence match"],
            "answerability": ["classification accuracy", "abstention precision", "abstention recall"],
            "efficiency": [
                "latency",
                "tool calls",
                "query steps",
                "bytes read",
                "shards read",
                "model calls",
                "input/output tokens",
                "network requests",
                "cost",
            ],
        },
        "systems": systems,
    }


PAIR_METRICS = (
    "recall_at_10",
    "mrr_at_10",
    "ndcg_at_10",
    "relationship_f1",
    "citation_correctness",
    "provenance_completeness",
    "answerability_accuracy",
    "end_task_success",
    "latency_ns",
    "tool_calls",
    "query_steps",
    "bytes_read",
    "shards_read",
    "model_calls",
    "input_tokens",
    "output_tokens",
    "network_requests",
    "cost_gbp",
)


LOWER_IS_BETTER = {
    "latency_ns",
    "tool_calls",
    "query_steps",
    "bytes_read",
    "shards_read",
    "model_calls",
    "input_tokens",
    "output_tokens",
    "network_requests",
    "cost_gbp",
}


def paired_comparisons(connection: sqlite3.Connection) -> dict[str, Any]:
    comparators = [system_id for system_id in sorted(SYSTEM_BY_ID) if system_id != PROPOSAL_ID]
    family_size = len(comparators) * len(PAIR_METRICS)
    z95 = NormalDist().inv_cdf(0.975)
    z_family = NormalDist().inv_cdf(1 - 0.05 / (2 * family_size))
    results: list[dict[str, Any]] = []
    for comparator in comparators:
        for metric in PAIR_METRICS:
            direction = -1.0 if metric in LOWER_IS_BETTER else 1.0
            rows = connection.execute(
                f"SELECT p.split_group,AVG((p.{metric}-c.{metric})*?) AS difference,COUNT(*) "
                "FROM outcomes p JOIN outcomes c ON c.question_id=p.question_id "
                f"WHERE p.system_id=? AND c.system_id=? AND p.{metric} IS NOT NULL AND c.{metric} IS NOT NULL "
                "GROUP BY p.split_group ORDER BY p.split_group",
                (direction, PROPOSAL_ID, comparator),
            ).fetchall()
            cluster_values = [float(row[1]) for row in rows]
            paired_questions = sum(int(row[2]) for row in rows)
            if not cluster_values:
                mean = standard_error = lower = upper = adjusted_lower = adjusted_upper = None
                wins = ties = losses = 0
            else:
                mean = statistics.fmean(cluster_values)
                standard_error = (
                    statistics.stdev(cluster_values) / math.sqrt(len(cluster_values)) if len(cluster_values) > 1 else 0.0
                )
                lower = mean - z95 * standard_error
                upper = mean + z95 * standard_error
                adjusted_lower = mean - z_family * standard_error
                adjusted_upper = mean + z_family * standard_error
                wins = sum(value > 1e-15 for value in cluster_values)
                ties = sum(abs(value) <= 1e-15 for value in cluster_values)
                losses = len(cluster_values) - wins - ties
            if adjusted_lower is not None and adjusted_lower > 0:
                favours = "proposal"
            elif adjusted_upper is not None and adjusted_upper < 0:
                favours = "comparator"
            else:
                favours = "inconclusive"
            results.append(
                {
                    "proposal": PROPOSAL_ID,
                    "comparator": comparator,
                    "metric": metric,
                    "difference_definition": (
                        "comparator minus proposal (positive favours proposal)"
                        if metric in LOWER_IS_BETTER
                        else "proposal minus comparator (positive favours proposal)"
                    ),
                    "paired_questions": paired_questions,
                    "independent_clusters": len(cluster_values),
                    "mean_difference": _round(mean),
                    "standard_error": _round(standard_error),
                    "ci_95": [_round(lower), _round(upper)],
                    "familywise_ci_95": [_round(adjusted_lower), _round(adjusted_upper)],
                    "cluster_wins_ties_losses": {"wins": wins, "ties": ties, "losses": losses},
                    "familywise_disposition": favours,
                }
            )
    return {
        "schema_version": 1,
        "method": {
            "estimator": "paired mean difference over split-group cluster means",
            "confidence_interval": "normal interval over independent cluster means",
            "alpha": 0.05,
            "multiplicity": "Bonferroni simultaneous intervals",
            "family_size": family_size,
            "cluster_rationale": "One story anchor generates 100 correlated matrix cells; split-group clustering prevents treating them as independent.",
        },
        "comparisons": results,
    }


SLICE_DIMENSIONS = (
    "persona_id",
    "story_role",
    "operation",
    "challenge",
    "risk",
    "difficulty",
    "locale",
    "jurisdiction",
    "split",
    "discovery_stage",
    "expected_answerable",
)


def slice_analysis(connection: sqlite3.Connection) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for dimension in SLICE_DIMENSIONS:
        cursor = connection.execute(
            f"SELECT system_id,{dimension},COUNT(*),AVG(recall_at_10),AVG(mrr_at_10),AVG(ndcg_at_10),"
            "AVG(relationship_f1),AVG(citation_correctness),AVG(provenance_completeness),"
            "AVG(answerability_accuracy),AVG(end_task_success),AVG(latency_ns),AVG(bytes_read),AVG(shards_read) "
            f"FROM outcomes GROUP BY system_id,{dimension} ORDER BY system_id,{dimension}"
        )
        for row in cursor:
            rows.append(
                {
                    "dimension": dimension,
                    "value": str(row[1]),
                    "system_id": str(row[0]),
                    "questions": int(row[2]),
                    "metrics": {
                        "recall_at_10": _round(row[3]),
                        "mrr_at_10": _round(row[4]),
                        "ndcg_at_10": _round(row[5]),
                        "relationship_f1": _round(row[6]),
                        "citation_correctness": _round(row[7]),
                        "provenance_completeness": _round(row[8]),
                        "answerability_accuracy": _round(row[9]),
                        "end_task_success": _round(row[10]),
                        "latency_ms_mean": _round(float(row[11] or 0) / 1_000_000),
                        "bytes_read_mean": _round(row[12]),
                        "shards_read_mean": _round(row[13]),
                    },
                }
            )
    return {"schema_version": 1, "dimensions": list(SLICE_DIMENSIONS), "slices": rows}


def failure_analysis(connection: sqlite3.Connection) -> dict[str, Any]:
    counts: Counter[tuple[str, str]] = Counter()
    operation_counts: Counter[tuple[str, str, str]] = Counter()
    samples: defaultdict[tuple[str, str], list[str]] = defaultdict(list)
    for system_id, question_id, operation, encoded in connection.execute(
        "SELECT system_id,question_id,operation,failure_codes FROM outcomes ORDER BY system_id,question_id"
    ):
        for code in json.loads(encoded):
            key = (str(system_id), str(code))
            counts[key] += 1
            operation_counts[(str(system_id), str(operation), str(code))] += 1
            if len(samples[key]) < 20:
                samples[key].append(str(question_id))
    failures = [
        {
            "system_id": system_id,
            "failure_code": code,
            "count": count,
            "sample_question_ids": samples[(system_id, code)],
        }
        for (system_id, code), count in sorted(counts.items(), key=lambda item: (item[0][0], -item[1], item[0][1]))
    ]
    by_operation = [
        {"system_id": system_id, "operation": operation, "failure_code": code, "count": count}
        for (system_id, operation, code), count in sorted(operation_counts.items())
    ]
    return {"schema_version": 1, "failures": failures, "by_operation": by_operation}


class TraceShardWriter:
    def __init__(self, root: Path, shard_records: int) -> None:
        if not 1 <= shard_records <= MAX_TRACE_SHARD_RECORDS:
            raise ValueError(f"trace shard record limit must be 1..{MAX_TRACE_SHARD_RECORDS}")
        self.root = root
        self.shard_records = shard_records
        self.entries: list[dict[str, Any]] = []
        self.buffer: list[bytes] = []
        self.buffer_bytes = 0
        self.ordinal = 0
        self.total = 0

    def add(self, trace_json: str) -> None:
        encoded = trace_json.encode("utf-8") + b"\n"
        if len(encoded) > MAX_JSON_VALUE_BYTES:
            raise ValueError("evaluation trace exceeds the maximum single-record size")
        if self.buffer and self.buffer_bytes + len(encoded) > MAX_TRACE_UNCOMPRESSED_BYTES:
            self.flush()
        self.buffer.append(encoded)
        self.buffer_bytes += len(encoded)
        if len(self.buffer) >= self.shard_records:
            self.flush()

    def flush(self) -> None:
        if not self.buffer:
            return
        canonical_payload = b"".join(self.buffer)
        canonical_sha = sha256_bytes(canonical_payload)
        filename = f"part-{self.ordinal:05d}-{canonical_sha[:16]}.jsonl.gz"
        path = self.root / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as raw:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0, compresslevel=9) as compressed:
                compressed.write(canonical_payload)
        entry = {
            "path": f"traces/{filename}",
            "records": len(self.buffer),
            "bytes": path.stat().st_size,
            "file_sha256": sha256_file(path),
            "canonical_sha256": canonical_sha,
            "first_key": json.loads(self.buffer[0])["question"]["question_id"],
            "last_key": json.loads(self.buffer[-1])["question"]["question_id"],
        }
        self.entries.append(entry)
        self.total += len(self.buffer)
        self.ordinal += 1
        self.buffer = []
        self.buffer_bytes = 0

    def finish(self) -> dict[str, Any]:
        self.flush()
        aggregate = sha256_text("".join(f"{entry['path']}\0{entry['canonical_sha256']}\n" for entry in self.entries))
        return {
            "schema": "govuk-okf-agent-evaluation-traces.v1",
            "compression": "gzip",
            "canonical_encoding": "UTF-8 RFC 8259 JSON Lines with sorted keys",
            "records": self.total,
            "max_records_per_shard": self.shard_records,
            "max_uncompressed_bytes_per_shard": MAX_TRACE_UNCOMPRESSED_BYTES,
            "shards": self.entries,
            "root_sha256": aggregate,
        }


def materialise_traces(connection: sqlite3.Connection, output: Path, shard_records: int) -> dict[str, Any]:
    trace_root = output / "traces"
    if trace_root.exists():
        shutil.rmtree(trace_root)
    writer = TraceShardWriter(trace_root, shard_records)
    for (encoded,) in connection.execute("SELECT trace_json FROM outcomes ORDER BY system_id,question_id"):
        writer.add(str(encoded))
    manifest = writer.finish()
    atomic_json(output / "trace-manifest.json", manifest)
    return manifest


def serialization_invariance(connection: sqlite3.Connection) -> dict[str, Any]:
    row = connection.execute(
        "SELECT COUNT(*) FROM outcomes j JOIN outcomes y ON y.question_id=j.question_id "
        "WHERE j.system_id='control-expanded-jsonld' AND y.system_id='control-expanded-yamlld' "
        "AND j.result_sha256 != y.result_sha256"
    ).fetchone()
    compared = connection.execute(
        "SELECT COUNT(*) FROM outcomes j JOIN outcomes y ON y.question_id=j.question_id "
        "WHERE j.system_id='control-expanded-jsonld' AND y.system_id='control-expanded-yamlld'"
    ).fetchone()
    mismatches = int(row[0])
    total = int(compared[0])
    return {
        "compared_questions": total,
        "result_mismatches": mismatches,
        "passed": total > 0 and mismatches == 0,
        "interpretation": "Equivalent expanded JSON-LD and YAML-LD controls must not produce a retrieval-quality difference.",
    }


def write_report(path: Path, *, run_id: str, mode: str, metrics: dict[str, Any], status: dict[str, Any]) -> None:
    lines = [
        "# Deterministic agent evaluation",
        "",
        f"Run: `{run_id}`  ",
        f"Mode: `{mode}`  ",
        f"Snapshot: `{status['snapshot_id']}`  ",
        f"Questions: {status['questions']} across {status['systems']} matched systems.",
        "",
        "All retrieval, grading, confidence intervals and trace generation are deterministic local code. "
        "The measured latency is observational; no network, model, token or paid API call is made.",
        "",
        "## Overall results",
        "",
        "| System | Recall@10 | MRR@10 | nDCG@10 | Relationship F1 | Citation | Provenance | Abstention accuracy | p95 ms | Cost GBP |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for system_id, item in metrics["systems"].items():
        values = item["metrics"]
        lines.append(
            "| " + " | ".join(
                [
                    system_id,
                    str(values.get("recall_at_10")),
                    str(values.get("mrr_at_10")),
                    str(values.get("ndcg_at_10")),
                    str(values.get("relationship_f1")),
                    str(values.get("citation_correctness")),
                    str(values.get("provenance_completeness")),
                    str(values.get("answerability_accuracy")),
                    str(values.get("latency_ms_p95")),
                    str(values.get("cost_gbp")),
                ]
            ) + " |"
        )
    lines.extend(
        [
            "",
            "## Claim boundary",
            "",
            f"- Agent/machine status: `{status['agent_evaluation_status']}`.",
            f"- Human evaluation status: `{status['human_evaluation_status']}`.",
            f"- Human UI of choice status: `{status['human_ui_of_choice_status']}`.",
            "- These results measure metadata discovery, relationship exposure, citation/provenance and abstention. "
            "They do not claim that metadata alone answers substantive body-content questions.",
            "- No human preference, usability or accessibility conclusion is inferred from deterministic agent runs.",
            "",
            "See `metrics.json`, `paired-comparisons.json`, `slices.json`, `failure-analysis.json` and the "
            "content-addressed raw trace shards for the complete evidence.",
        ]
    )
    atomic_text(path, "\n".join(lines) + "\n")


def output_manifest(output: Path, metadata: dict[str, Any]) -> dict[str, Any]:
    for finder_artifact in output.rglob(".DS_Store"):
        finder_artifact.unlink(missing_ok=True)
    files: list[dict[str, Any]] = []
    for path in sorted(
        item
        for item in output.rglob("*")
        if item.is_file() and ".work" not in item.parts and item.name != ".DS_Store"
    ):
        relative = path.relative_to(output).as_posix()
        if relative in {"manifest.json", "checksums.txt"}:
            continue
        files.append({"path": relative, "bytes": path.stat().st_size, "sha256": sha256_file(path)})
    root = sha256_text("".join(f"{item['path']}\0{item['sha256']}\n" for item in files))
    manifest = {"schema_version": 1, "harness_version": HARNESS_VERSION, "files": files, "root_sha256": root, **metadata}
    atomic_json(output / "manifest.json", manifest)
    checksum_paths = sorted(
        item
        for item in output.rglob("*")
        if item.is_file() and ".work" not in item.parts and item.name not in {"checksums.txt", ".DS_Store"}
    )
    atomic_text(
        output / "checksums.txt",
        "".join(f"{sha256_file(path)}  {path.relative_to(output).as_posix()}\n" for path in checksum_paths),
    )
    return manifest


def run_evaluation(
    *,
    questions: Path,
    bundle: Path,
    output: Path,
    run_id: str,
    mode: str,
    question_limit: int | None = None,
    trace_shard_records: int = DEFAULT_TRACE_SHARD_RECORDS,
    resume: bool = False,
) -> dict[str, Any]:
    questions = questions.resolve()
    bundle = bundle.resolve()
    output = output.resolve()
    if mode not in {"fixture", "release"}:
        raise ValueError("mode must be fixture or release")
    if not _normalise_space(run_id) or len(run_id) > 128:
        raise ValueError("run_id must contain 1..128 non-whitespace characters")
    if mode == "release" and question_limit is not None:
        raise ValueError("question limits are forbidden in release mode")
    if (
        output in {questions, bundle}
        or output.is_relative_to(questions)
        or output.is_relative_to(bundle)
        or questions.is_relative_to(output)
        or bundle.is_relative_to(output)
    ):
        raise ValueError("evaluation output must be a dedicated path disjoint from both input trees")
    if (output / "manifest.json").is_file():
        raise ValueError(f"completed evaluation runs are immutable; choose a new output path: {output}")
    if output.exists() and not resume:
        raise ValueError(f"output already exists; use a new immutable run path or --resume: {output}")
    contract = validate_input_contract(questions, bundle, mode)
    output.mkdir(parents=True, exist_ok=True)
    work = output / ".work"
    work.mkdir(parents=True, exist_ok=True)
    system_contract_sha = sha256_text(canonical_json([asdict(item) for item in SYSTEMS]))
    run_key = sha256_text(
        canonical_json(
            {
                "run_id": run_id,
                "harness_version": HARNESS_VERSION,
                "mode": mode,
                "question_manifest_sha256": contract.question_manifest_sha256,
                "bundle_manifest_sha256": contract.bundle_manifest_sha256,
                "system_contract_sha256": system_contract_sha,
                "question_limit": question_limit,
            }
        )
    )
    index = BundleIndex(bundle, work / "index.sqlite", contract)
    try:
        outcomes = OutcomeStore(work / "outcomes.sqlite", run_key)
    except Exception:
        index.close()
        raise
    started_at = time.time()
    question_count = 0
    processed = 0
    connections_closed = False
    try:
        for question in iter_questions(questions):
            if question_limit is not None and question_count >= question_limit:
                break
            question_count += 1
            outcomes.register_question(question)
            completed = outcomes.completed_systems(str(question["question_id"]))
            for system in SYSTEMS:
                if system.system_id in completed:
                    continue
                search = index.search(system, str(question["wording"]))
                metrics, failures, gold = grade_result(question, search, index)
                trace = make_trace(
                    run_id=run_id,
                    system=system,
                    question=question,
                    search=search,
                    metrics=metrics,
                    failures=failures,
                    gold=gold,
                    contract=contract,
                )
                outcomes.add(trace)
                processed += 1
            if question_count % 100 == 0:
                outcomes.commit()
                atomic_json(
                    work / "checkpoint.json",
                    {
                        "schema_version": 1,
                        "run_key": run_key,
                        "questions_seen": question_count,
                        "outcomes": outcomes.connection.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0],
                        "status": "running",
                    },
                )
        outcomes.commit()
        if question_count == 0:
            raise ValueError("question matrix contains no binding records")
        if question_limit is None and question_count != contract.expected_questions:
            raise ValueError(f"question binding count differs from manifest: {question_count} != {contract.expected_questions}")
        if mode == "release" and question_count != RELEASE_QUESTION_COUNT:
            raise ValueError(f"release run must contain {RELEASE_QUESTION_COUNT} questions")
        expected_outcomes = question_count * len(SYSTEMS)
        actual_outcomes = int(outcomes.connection.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0])
        if actual_outcomes != expected_outcomes:
            raise ValueError(f"matched-system outcome matrix is incomplete: {actual_outcomes} != {expected_outcomes}")
        system_counts = {
            str(row[0]): int(row[1])
            for row in outcomes.connection.execute("SELECT system_id,COUNT(*) FROM outcomes GROUP BY system_id")
        }
        if set(system_counts) != set(SYSTEM_BY_ID) or any(count != question_count for count in system_counts.values()):
            raise ValueError("not every matched system ran against every question")
        usage_totals = outcomes.connection.execute(
            "SELECT COALESCE(SUM(model_calls),0),COALESCE(SUM(input_tokens),0),"
            "COALESCE(SUM(output_tokens),0),COALESCE(SUM(network_requests),0),COALESCE(SUM(cost_gbp),0) FROM outcomes"
        ).fetchone()
        if any(float(value) != 0.0 for value in usage_totals):
            raise ValueError("deterministic evaluation unexpectedly recorded model, token, network or paid usage")
        invariance = serialization_invariance(outcomes.connection)
        if mode == "release" and not invariance["passed"]:
            raise ValueError("JSON-LD/YAML-LD serialization invariance control failed")

        metrics = aggregate_metrics(outcomes.connection)
        paired = paired_comparisons(outcomes.connection)
        slices = slice_analysis(outcomes.connection)
        failures = failure_analysis(outcomes.connection)
        traces = materialise_traces(outcomes.connection, output, trace_shard_records)
        machine_complete = mode == "release" and question_count == RELEASE_QUESTION_COUNT and invariance["passed"]
        status = {
            "schema_version": 1,
            "run_id": run_id,
            "mode": mode,
            "snapshot_id": contract.snapshot_id,
            "questions": question_count,
            "systems": len(SYSTEMS),
            "outcomes": actual_outcomes,
            "all_questions_all_systems_complete": True,
            "release_question_contract_passed": contract.release_question_contract_passed,
            "serialization_invariance": invariance,
            "model_usage": {"model_calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_gbp": 0.0},
            "network_requests": 0,
            "agent_evaluation_status": "completed" if machine_complete else "fixture_completed",
            "human_evaluation_status": "not_authorised",
            "human_ui_of_choice_status": "not_yet_testable",
            "machine_evaluation_complete": machine_complete,
            "full_evaluation_complete": False,
            "programme_complete": False,
            "release_eligible": machine_complete,
            "claim_boundary": (
                "Machine results cover metadata discovery, retrieval ranking, typed relationships, citation/provenance and abstention. "
                "No human preference or body-content answering claim is made."
            ),
        }
        usage = {
            "schema_version": 1,
            "harness_version": HARNESS_VERSION,
            "execution": "deterministic local Python and SQLite/FTS5",
            "runtime": {
                "git_sha": contract.git_sha,
                "git_dirty": contract.git_dirty,
                "python_version": contract.python_version,
                "sqlite_version": contract.sqlite_version,
            },
            "model_usage": status["model_usage"],
            "source_access": {
                "mode": "frozen local bundle and independently verified question assets",
                "network_requests": 0,
                "restrictions": [
                    "No GOV.UK page body is fetched or retained.",
                    "No external search, authenticated source, model provider or paid API is contacted.",
                ],
            },
            "licensing_and_fair_use_triggers": [
                "Evaluation traces retain public metadata identifiers, titles, URLs and short evidence fields only.",
                "Attachment and page bodies are not copied into traces.",
            ],
            "fallbacks_used": [
                "SQLite FTS5 supplies the reproducible lexical baseline; unavailable dense, live Search API, GOV.UK Chat and internal GovGraph systems remain non-run comparators.",
                "Normal paired cluster intervals are used without a third-party statistics dependency.",
            ],
            "wall_seconds": round(time.time() - started_at, 6),
            "new_outcomes_this_invocation": processed,
        }
        atomic_json(output / "metrics.json", metrics)
        atomic_json(output / "paired-comparisons.json", paired)
        atomic_json(output / "slices.json", slices)
        atomic_json(output / "failure-analysis.json", failures)
        atomic_json(output / "status.json", status)
        atomic_json(output / "usage.json", usage)
        write_report(output / "report.md", run_id=run_id, mode=mode, metrics=metrics, status=status)
        manifest = output_manifest(
            output,
            {
                "run_id": run_id,
                "mode": mode,
                "snapshot_id": contract.snapshot_id,
                "questions": question_count,
                "systems": len(SYSTEMS),
                "outcomes": actual_outcomes,
                "trace_records": traces["records"],
                "question_manifest_sha256": contract.question_manifest_sha256,
                "bundle_manifest_sha256": contract.bundle_manifest_sha256,
                "system_contract_sha256": system_contract_sha,
                "git_sha": contract.git_sha,
                "git_dirty": contract.git_dirty,
                "python_version": contract.python_version,
                "sqlite_version": contract.sqlite_version,
                "release_eligible": machine_complete,
            },
        )
        outcomes.close()
        index.close()
        connections_closed = True
        shutil.rmtree(work)
        return {"manifest": manifest, "status": status, "metrics": metrics}
    except Exception:
        if not connections_closed:
            outcomes.commit()
            outcomes.close()
            index.close()
        raise
