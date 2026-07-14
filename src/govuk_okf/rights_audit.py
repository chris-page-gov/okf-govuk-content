"""Bounded, snapshot-bound rights and privacy audit for release data.

The audit is deliberately metadata-led.  It does not decide that an item is
legally reusable; it proves the mechanically testable boundary (no retained
page/attachment bodies or credential material), identifies conservative
item-review triggers, and binds any review dispositions to one release
snapshot.
"""

from __future__ import annotations

import codecs
import collections
import gzip
import hashlib
import json
import re
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator
from urllib.parse import urlparse

from .util import canonical_json_bytes, pretty_json, reference_path


class RightsAuditError(RuntimeError):
    """Raised when the audit cannot establish a bounded, trustworthy result."""


@dataclass(frozen=True)
class AuditLimits:
    """Hard resource ceilings used by the streaming scanner."""

    max_files: int = 100_000
    max_records: int = 2_000_000
    max_manifest_bytes: int = 16 * 1024 * 1024
    max_compressed_bytes_per_file: int = 50 * 1024 * 1024
    max_uncompressed_bytes_per_file: int = 64 * 1024 * 1024
    max_record_bytes: int = 16 * 1024 * 1024
    max_nodes_per_record: int = 250_000
    max_depth: int = 64
    max_string_chars: int = 1_048_576
    example_limit: int = 5


TRIGGERS = (
    "third_party_credit_or_rights",
    "personal_data_indicator",
    "logo_crest_royal_arms_or_insignia",
    "patent_trademark_or_design_right",
    "resource_attachment_or_image",
    "non_govuk_boundary",
    "explicit_licence_notice",
    "identity_document",
)
TRIGGER_BITS = {name: 1 << index for index, name in enumerate(TRIGGERS)}

FORBIDDEN_BODY_FIELDS = {
    "body",
    "body_html",
    "body_markdown",
    "body_text",
    "page_body",
    "page_html",
    "page_markdown",
    "page_text",
    "raw_body",
    "raw_content",
    "rendered_body",
    "rendered_html",
    "source_body",
    "source_html",
    "attachment_body",
    "attachment_bytes",
    "file_bytes",
    "image_bytes",
    "media_bytes",
    "binary",
    "blob",
    "base64",
    "payload",
    "document_content",
}

CREDENTIAL_FIELDS = {
    "password",
    "passwd",
    "secret",
    "client_secret",
    "api_key",
    "apikey",
    "access_token",
    "refresh_token",
    "bearer_token",
    "authorization",
    "proxy_authorization",
    "cookie",
    "set_cookie",
    "private_key",
    "ssh_key",
    "credential",
    "credentials",
}

THIRD_PARTY_FIELDS = {
    "third_party",
    "third_party_rights",
    "third_party_copyright",
    "credit",
    "credits",
    "attribution",
    "copyright",
    "copyright_holder",
    "rights_holder",
    "licensor",
    "licence_holder",
    "license_holder",
    "source_credit",
}

PERSONAL_DATA_FIELDS = {
    "personal_data",
    "personal_information",
    "email",
    "email_address",
    "telephone",
    "telephone_number",
    "phone",
    "phone_number",
    "contact",
    "contact_details",
    "named_contact",
    "date_of_birth",
    "dob",
    "national_insurance_number",
    "passport_number",
    "home_address",
}

LOGO_FIELDS = {
    "logo",
    "logos",
    "crest",
    "crests",
    "royal_arms",
    "insignia",
    "emblem",
    "branding_image",
}

PROTECTED_RIGHT_FIELDS = {
    "patent",
    "patents",
    "trademark",
    "trademarks",
    "trade_mark",
    "trade_marks",
    "design_right",
    "design_rights",
}

LICENCE_FIELDS = {
    "licence",
    "license",
    "licence_notice",
    "license_notice",
    "licensing",
    "rights_status",
    "usage_terms",
    "reuse_terms",
}

IDENTITY_DOCUMENT_FIELDS = {
    "identity_document",
    "identity_documents",
    "passport",
    "driving_licence",
    "driving_license",
    "biometric_residence_permit",
}

RESOURCE_FIELDS = {
    "attachment",
    "attachments",
    "image",
    "images",
    "document_attachment",
    "machine_representation",
    "download",
    "downloads",
}

TARGET_URL_FIELDS = {"@id", "url", "canonical_url", "web_url", "href", "link"}
ADMITTED_CONTENT_HOSTS = {
    "www.gov.uk",
    "assets.publishing.service.gov.uk",
    "content-api.publishing.service.gov.uk",
}

_SECRET_PATTERNS = (
    ("private_key_material", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("bearer_credential", re.compile(r"(?i)\bauthorization\s*:\s*bearer\s+[A-Za-z0-9._~+/=-]{12,}")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("data_uri_payload", re.compile(r"(?i)^data:[^;,]{1,100};base64,")),
    ("long_base64_payload", re.compile(r"^[A-Za-z0-9+/]{512,}={0,2}$")),
)

_OGL_PATTERN = re.compile(r"(?i)(open government licence|nationalarchives\.gov\.uk/doc/open-government-licence)")

POLICY_SOURCE_IDS = {"robots", "reuse", "terms", "ogl-v3", "ogl-exceptions"}
VALID_REVIEW_DISPOSITIONS = {
    "metadata_only_safe",
    "ogl_confirmed",
    "exceptioned",
    "excluded_from_publication",
}

RIGHTS_AUDIT_SCHEMA = "afhf-govuk-okf-rights-privacy-audit.v1"
RIGHTS_AUDIT_INPUT_SCHEMA = "afhf-govuk-okf-rights-audit-inputs.v1"
COMPARATOR_RIGHTS_SCHEMA = "govuk-chat-comparator-rights-disposition.v1"
DEFAULT_COMPARATOR_EVIDENCE_PATHS = (
    Path("evaluation/govuk-chat/new-parent-multi-service.json"),
    Path("evaluation/govuk-chat/official-published-example.json"),
)
WALKTHROUGH_REVIEW_TRIGGER = (
    "review_required_before_retaining_or_republishing_any_chat_answer_or_source_asset"
)
PUBLISHED_OBSERVATION_REVIEW_TRIGGER = (
    "item_level_review_required_before_expanding_the_excerpt_or_copying_the_image"
)
PUBLISHED_ANSWER_RIGHTS_STATUS = (
    "not_independently_verified_for_republication_beyond_this_bounded_evidence_use"
)
PUBLISHED_SOURCE_CARD_RIGHTS_STATUS = "linked_GOV.UK_items_may_contain_item_level_exceptions"


def _normalise_key(value: object) -> str:
    text = re.sub(r"[^a-z0-9]+", "_", str(value).casefold()).strip("_")
    return text


def _sha256_file(path: Path, ceiling: int | None = None) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            total += len(chunk)
            if ceiling is not None and total > ceiling:
                raise RightsAuditError(f"{path}: compressed/file size exceeds {ceiling} bytes")
            digest.update(chunk)
    return digest.hexdigest(), total


def _contract_path(root: Path, path: Path, label: str) -> str:
    """Return one repository-relative regular-file path for an audit contract."""

    resolved_root = root.resolve()
    try:
        relative = path.relative_to(resolved_root) if path.is_absolute() else path
    except ValueError as exc:
        raise RightsAuditError(f"{label} escapes the repository: {path}") from exc
    if not relative.parts or ".." in relative.parts:
        raise RightsAuditError(f"{label} escapes the repository: {path}")
    cursor = resolved_root
    for part in relative.parts:
        cursor /= part
        if cursor.is_symlink():
            raise RightsAuditError(f"{label} cannot traverse a symbolic link: {path}")
    resolved = cursor.resolve()
    if resolved_root not in resolved.parents:
        raise RightsAuditError(f"{label} escapes the repository: {path}")
    if not resolved.is_file():
        raise RightsAuditError(f"{label} is not a regular file: {path}")
    return relative.as_posix()


def _bound_file(root: Path, path: Path, label: str, ceiling: int) -> dict[str, Any]:
    relative = _contract_path(root, path, label)
    digest, size = _sha256_file(path.resolve(), ceiling)
    return {"path": relative, "sha256": digest, "bytes": size}


def _release_reproduction(release: dict[str, Any]) -> dict[str, Any] | None:
    for key in ("promotion", "promotion_contract"):
        transition = release.get(key)
        reproduction = transition.get("reproduction") if isinstance(transition, dict) else None
        if isinstance(reproduction, dict):
            return reproduction
    return None


def _safe_path(root: Path, relative: object, label: str) -> Path:
    if not isinstance(relative, str) or not relative:
        raise RightsAuditError(f"{label}: path must be a non-empty string")
    part = Path(relative)
    root = root.resolve()
    if part.is_absolute() or ".." in part.parts:
        raise RightsAuditError(f"{label}: unsafe path")
    cursor = root
    for component in part.parts:
        cursor /= component
        if cursor.is_symlink():
            raise RightsAuditError(f"{label}: path cannot traverse a symbolic link: {relative}")
    target = cursor.resolve()
    if target == root or root not in target.parents:
        raise RightsAuditError(f"{label}: unsafe path")
    if not target.is_file():
        raise RightsAuditError(f"{label}: file does not exist: {relative}")
    return target


def _load_bound_path(
    root: Path,
    binding: object,
    label: str,
    ceiling: int,
    *,
    allow_missing: bool,
) -> tuple[Path | None, list[str]]:
    errors: list[str] = []
    if (
        not isinstance(binding, dict)
        or not isinstance(binding.get("path"), str)
        or not isinstance(binding.get("bytes"), int)
        or isinstance(binding.get("bytes"), bool)
        or binding["bytes"] < 0
        or not isinstance(binding.get("sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", binding["sha256"]) is None
    ):
        return None, [f"{label} binding is invalid"]
    try:
        path = _safe_path(root, binding["path"], label)
    except RightsAuditError as exc:
        # Frozen full-corpus acquisition inputs can be intentionally external
        # after clean-room promotion, but their path/hash binding is mandatory.
        if allow_missing and "file does not exist" in str(exc):
            return None, []
        return None, [str(exc)]
    try:
        digest, size = _sha256_file(path, ceiling)
    except RightsAuditError as exc:
        return None, [str(exc)]
    if digest != binding["sha256"] or size != binding["bytes"]:
        errors.append(f"{label} content differs from its audit input binding")
    return path, errors


def audit_from_input_contract(
    root: Path,
    contract: dict[str, Any],
    *,
    release_manifest_path: Path | None = None,
    limits: AuditLimits = AuditLimits(),
) -> dict[str, Any]:
    """Re-run an audit from its exact, hash-bound immutable input contract."""

    root = root.resolve()
    if contract.get("schema") != RIGHTS_AUDIT_INPUT_SCHEMA:
        raise RightsAuditError("rights audit input contract schema is invalid")
    publication, publication_errors = _load_bound_path(
        root,
        contract.get("publication_manifest"),
        "rights audit publication manifest",
        limits.max_manifest_bytes,
        allow_missing=False,
    )
    corpus_bindings = contract.get("corpus_manifests")
    if not isinstance(corpus_bindings, list):
        raise RightsAuditError("rights audit input contract corpus manifests are invalid")
    corpus_paths: list[Path] = []
    errors = list(publication_errors)
    for ordinal, binding in enumerate(corpus_bindings):
        path, binding_errors = _load_bound_path(
            root,
            binding,
            f"rights audit corpus manifest {ordinal}",
            limits.max_manifest_bytes,
            allow_missing=False,
        )
        errors.extend(binding_errors)
        if path is not None:
            corpus_paths.append(path)
    review_binding = contract.get("review_ledger")
    review_path: Path | None = None
    if review_binding is not None:
        review_path, review_errors = _load_bound_path(
            root,
            review_binding,
            "rights audit review ledger",
            limits.max_manifest_bytes,
            allow_missing=False,
        )
        errors.extend(review_errors)
    comparator_bindings = contract.get("comparator_evidence", [])
    if not isinstance(comparator_bindings, list):
        raise RightsAuditError("rights audit input contract comparator evidence is invalid")
    comparator_paths: list[Path] = []
    for ordinal, binding in enumerate(comparator_bindings):
        path, binding_errors = _load_bound_path(
            root,
            binding,
            f"rights audit comparator evidence {ordinal}",
            limits.max_manifest_bytes,
            allow_missing=False,
        )
        errors.extend(binding_errors)
        if path is not None:
            comparator_paths.append(path)
    generated_at = contract.get("generated_at")
    if not isinstance(generated_at, str) or not generated_at.strip():
        errors.append("rights audit input contract generated_at is invalid")
    if (
        errors
        or publication is None
        or len(corpus_paths) != len(corpus_bindings)
        or len(comparator_paths) != len(comparator_bindings)
    ):
        raise RightsAuditError("; ".join(errors or ["rights audit inputs are incomplete"]))
    return audit_release(
        root,
        release_manifest_path=release_manifest_path,
        publication_manifest_path=publication,
        corpus_manifest_paths=corpus_paths,
        review_ledger_path=review_path,
        comparator_evidence_paths=comparator_paths,
        generated_at=generated_at,
        auto_review_ledger=False,
        limits=limits,
    )


def audit_contract_has_missing_corpus_inputs(
    root: Path,
    contract: dict[str, Any],
    *,
    limits: AuditLimits = AuditLimits(),
) -> bool:
    """Return whether a valid contract has intentionally absent corpus inputs."""

    root = root.resolve()
    if contract.get("schema") != RIGHTS_AUDIT_INPUT_SCHEMA:
        raise RightsAuditError("rights audit input contract schema is invalid")
    corpus_bindings = contract.get("corpus_manifests")
    if not isinstance(corpus_bindings, list):
        raise RightsAuditError("rights audit input contract corpus manifests are invalid")
    missing = False
    errors: list[str] = []
    for ordinal, binding in enumerate(corpus_bindings):
        path, binding_errors = _load_bound_path(
            root,
            binding,
            f"rights audit corpus manifest {ordinal}",
            limits.max_manifest_bytes,
            allow_missing=True,
        )
        errors.extend(binding_errors)
        missing = missing or path is None
    if errors:
        raise RightsAuditError("; ".join(errors))
    return missing


def _comparator_rights_evidence(
    root: Path,
    paths: Iterable[Path],
    limits: AuditLimits,
) -> tuple[dict[str, Any], list[str]]:
    """Validate and bind the small GOV.UK Chat comparator rights contract."""

    errors: list[str] = []
    files: list[dict[str, Any]] = []
    dispositions: list[dict[str, Any]] = []
    for supplied in paths:
        path = supplied if supplied.is_absolute() else root / supplied
        try:
            binding = _bound_file(root, path, "comparator rights evidence", limits.max_manifest_bytes)
            document = _load_json(path.resolve(), limits.max_manifest_bytes)
        except RightsAuditError as exc:
            errors.append(str(exc))
            continue
        files.append(binding)
        relative = binding["path"]
        if not isinstance(document, dict):
            errors.append(f"{relative}: comparator evidence must be an object")
            continue
        rights = document.get("rights_and_reuse")
        valid_common = (
            isinstance(rights, dict)
            and rights.get("schema") == COMPARATOR_RIGHTS_SCHEMA
            and rights.get("not_a_legal_conclusion") is True
        )
        if not valid_common:
            errors.append(f"{relative}: comparator rights disposition is incomplete")
            continue
        if document.get("schema") == "govuk-chat-comparison-walkthrough.v1":
            valid = (
                valid_common
                and rights.get("fair_use_or_fair_dealing_trigger") == WALKTHROUGH_REVIEW_TRIGGER
                and rights.get("disposition") == "links_and_minimal_source_metadata_only"
                and rights.get("published_material_retained") is False
                and rights.get("official_context_rights_status")
                == "not_independently_verified_for_each_linked_item"
            )
            disposition = "link_metadata_only_pending_item_review"
            binary_bytes_published = False
        elif document.get("schema") == "govuk-chat-published-observation.v1":
            capture = document.get("capture")
            answer_document = document.get("answer")
            image = rights.get("image")
            answer = rights.get("answer")
            cards = rights.get("source_cards")
            excerpt = (
                answer_document.get("short_verbatim_excerpt")
                if isinstance(answer_document, dict)
                else None
            )
            valid = (
                valid_common
                and rights.get("fair_use_or_fair_dealing_trigger")
                == PUBLISHED_OBSERVATION_REVIEW_TRIGGER
                and isinstance(capture, dict)
                and capture.get("asset_retained") is False
                and isinstance(capture.get("asset_sha256"), str)
                and re.fullmatch(r"[0-9a-f]{64}", capture["asset_sha256"]) is not None
                and isinstance(image, dict)
                and image.get("bytes_retained") is False
                and image.get("bytes_published") is False
                and image.get("disposition") == "source_url_and_sha256_only"
                and image.get("rights_status") == "not_independently_verified"
                and isinstance(answer, dict)
                and answer.get("disposition")
                == "short_attributed_excerpt_and_structured_paraphrase_only"
                and answer.get("rights_status") == PUBLISHED_ANSWER_RIGHTS_STATUS
                and isinstance(cards, dict)
                and cards.get("destination_content_copied") is False
                and cards.get("disposition") == "ordered_title_and_url_metadata_only"
                and cards.get("rights_status") == PUBLISHED_SOURCE_CARD_RIGHTS_STATUS
                and isinstance(excerpt, str)
                and 0 < len(excerpt.split()) <= 25
            )
            disposition = "bounded_excerpt_link_and_digest_pending_item_review"
            binary_bytes_published = False
        else:
            valid = False
            disposition = "unsupported"
            binary_bytes_published = True
        if not valid:
            errors.append(f"{relative}: comparator rights controls do not match the declared schema")
            continue
        dispositions.append(
            {
                "path": relative,
                "disposition": disposition,
                "rights_verified": False,
                "item_level_review_triggered": True,
                "binary_bytes_published": binary_bytes_published,
            }
        )
    return (
        {
            "schema": "afhf-govuk-okf-comparator-rights-evidence.v1",
            "controls_passed": not errors,
            "files": files,
            "dispositions": dispositions,
            "limitations": [
                "These controls prove bounded retention and an explicit review trigger; they do not determine that OGL, fair dealing or another permission applies.",
                "The official screenshot bytes are not retained or published by the repository.",
            ],
        },
        errors,
    )


def _load_json(path: Path, ceiling: int) -> Any:
    digest, size = _sha256_file(path, ceiling)
    del digest
    if size > ceiling:
        raise RightsAuditError(f"{path}: JSON document exceeds {ceiling} bytes")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RightsAuditError(f"{path}: invalid UTF-8 JSON: {exc}") from exc


def _decoded_chunks(path: Path, ceiling: int) -> Iterator[str]:
    opener = gzip.open if path.suffix == ".gz" else open
    decoder = codecs.getincrementaldecoder("utf-8")()
    total = 0
    try:
        with opener(path, "rb") as stream:
            while chunk := stream.read(1024 * 1024):
                total += len(chunk)
                if total > ceiling:
                    raise RightsAuditError(f"{path}: uncompressed size exceeds {ceiling} bytes")
                text = decoder.decode(chunk)
                if text:
                    yield text
            tail = decoder.decode(b"", final=True)
            if tail:
                yield tail
    except (OSError, UnicodeDecodeError) as exc:
        raise RightsAuditError(f"{path}: cannot decode bounded JSON stream: {exc}") from exc


def _iter_json_records(path: Path, limits: AuditLimits) -> Iterator[dict[str, Any]]:
    """Yield a top-level JSON array incrementally, or a bounded JSON object/graph."""

    chunks = iter(_decoded_chunks(path, limits.max_uncompressed_bytes_per_file))
    buffer = ""
    exhausted = False

    def fill() -> bool:
        nonlocal buffer, exhausted
        if exhausted:
            return False
        try:
            buffer += next(chunks)
            return True
        except StopIteration:
            exhausted = True
            return False

    while not buffer.strip() and fill():
        pass
    stripped = buffer.lstrip()
    if not stripped:
        raise RightsAuditError(f"{path}: empty JSON stream")
    if not stripped.startswith("["):
        while fill():
            pass
        try:
            document = json.loads(buffer)
        except json.JSONDecodeError as exc:
            raise RightsAuditError(f"{path}: invalid JSON: {exc}") from exc
        if isinstance(document, dict) and isinstance(document.get("@graph"), list):
            for item in document["@graph"]:
                if not isinstance(item, dict):
                    raise RightsAuditError(f"{path}: @graph entry is not an object")
                yield item
            return
        if not isinstance(document, dict):
            raise RightsAuditError(f"{path}: expected a JSON array, object, or JSON-LD @graph")
        yield document
        return

    # Keep only the array text from its opening bracket.
    buffer = stripped[1:]
    decoder = json.JSONDecoder()
    expect_value = True
    while True:
        buffer = buffer.lstrip()
        while not buffer and fill():
            buffer = buffer.lstrip()
        if not buffer:
            raise RightsAuditError(f"{path}: unterminated JSON array")
        if buffer[0] == "]":
            if not expect_value:
                raise RightsAuditError(f"{path}: malformed array terminator")
            if buffer[1:].strip():
                raise RightsAuditError(f"{path}: trailing data after JSON array")
            return
        while True:
            try:
                value, end = decoder.raw_decode(buffer)
                break
            except json.JSONDecodeError as exc:
                if len(buffer) > limits.max_record_bytes:
                    raise RightsAuditError(f"{path}: record exceeds {limits.max_record_bytes} bytes") from exc
                if not fill():
                    raise RightsAuditError(f"{path}: invalid/incomplete JSON array record: {exc}") from exc
        if len(canonical_json_bytes(value)) > limits.max_record_bytes:
            raise RightsAuditError(f"{path}: record exceeds {limits.max_record_bytes} bytes")
        if not isinstance(value, dict):
            raise RightsAuditError(f"{path}: top-level array entry is not an object")
        yield value
        buffer = buffer[end:].lstrip()
        while not buffer and fill():
            buffer = buffer.lstrip()
        if not buffer or buffer[0] not in ",]":
            raise RightsAuditError(f"{path}: missing array separator")
        if buffer[0] == "]":
            if buffer[1:].strip():
                while fill():
                    pass
                if buffer[1:].strip():
                    raise RightsAuditError(f"{path}: trailing data after JSON array")
            return
        buffer = buffer[1:]
        expect_value = True


def _iter_jsonl_gzip(path: Path, limits: AuditLimits) -> Iterator[dict[str, Any]]:
    total = 0
    number = 0
    try:
        with gzip.open(path, "rb") as stream:
            while True:
                line = stream.readline(limits.max_record_bytes + 1)
                if not line:
                    break
                number += 1
                total += len(line)
                if total > limits.max_uncompressed_bytes_per_file:
                    raise RightsAuditError(
                        f"{path}: uncompressed size exceeds {limits.max_uncompressed_bytes_per_file} bytes"
                    )
                if len(line) > limits.max_record_bytes:
                    raise RightsAuditError(f"{path}:{number}: record exceeds {limits.max_record_bytes} bytes")
                if not line.strip():
                    continue
                value = json.loads(line.decode("utf-8"))
                if not isinstance(value, dict):
                    raise RightsAuditError(f"{path}:{number}: record is not an object")
                yield value
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RightsAuditError(f"{path}:{number}: invalid bounded JSONL gzip stream: {exc}") from exc


def _record_fingerprint(record: dict[str, Any]) -> str:
    identity = None
    for key in ("canonical_url", "url", "@id", "candidate_key", "content_id", "id", "name"):
        value = record.get(key)
        if isinstance(value, (str, int)) and str(value):
            identity = f"{key}\0{value}"
            break
    if identity is None:
        identity = f"record\0{hashlib.sha256(canonical_json_bytes(record)).hexdigest()}"
    return "sha256:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _meaningful(value: Any) -> bool:
    return value not in (None, "", [], {})


def _trigger_mask(record: dict[str, Any], source_kind: str, limits: AuditLimits) -> int:
    mask = 0
    if source_kind == "resources" or str(record.get("record_type", "")).casefold().endswith("attachment"):
        mask |= TRIGGER_BITS["resource_attachment_or_image"]
    if str(record.get("entity_class", "")).casefold() == "external_boundary" or str(
        record.get("document_type", "")
    ).casefold() in {"external_content", "external_boundary"}:
        mask |= TRIGGER_BITS["non_govuk_boundary"]

    stack: list[tuple[Any, int]] = [(record, 0)]
    nodes = 0
    while stack:
        value, depth = stack.pop()
        nodes += 1
        if nodes > limits.max_nodes_per_record:
            raise RightsAuditError(f"record {_record_fingerprint(record)}: node ceiling exceeded")
        if depth > limits.max_depth:
            raise RightsAuditError(f"record {_record_fingerprint(record)}: nesting ceiling exceeded")
        if isinstance(value, dict):
            for raw_key, child in value.items():
                key = _normalise_key(raw_key)
                if _meaningful(child):
                    if key in THIRD_PARTY_FIELDS:
                        mask |= TRIGGER_BITS["third_party_credit_or_rights"]
                    if key in PERSONAL_DATA_FIELDS:
                        mask |= TRIGGER_BITS["personal_data_indicator"]
                    if key in LOGO_FIELDS:
                        mask |= TRIGGER_BITS["logo_crest_royal_arms_or_insignia"]
                    if key in PROTECTED_RIGHT_FIELDS:
                        mask |= TRIGGER_BITS["patent_trademark_or_design_right"]
                    if key in IDENTITY_DOCUMENT_FIELDS:
                        mask |= TRIGGER_BITS["identity_document"]
                    if key in RESOURCE_FIELDS:
                        mask |= TRIGGER_BITS["resource_attachment_or_image"]
                    if key in LICENCE_FIELDS:
                        if not (isinstance(child, str) and _OGL_PATTERN.search(child)):
                            mask |= TRIGGER_BITS["explicit_licence_notice"]
                    if key == "content_type" and isinstance(child, str) and child.casefold().startswith("image/"):
                        mask |= TRIGGER_BITS["resource_attachment_or_image"]
                    if (
                        key in TARGET_URL_FIELDS
                        and isinstance(child, str)
                        and child.startswith(("http://", "https://"))
                    ):
                        host = (urlparse(child).hostname or "").casefold()
                        if host not in ADMITTED_CONTENT_HOSTS:
                            mask |= TRIGGER_BITS["non_govuk_boundary"]
                stack.append((child, depth + 1))
        elif isinstance(value, list):
            stack.extend((child, depth + 1) for child in value)
    return mask


def _scan_record_safety(
    record: dict[str, Any],
    source_path: str,
    limits: AuditLimits,
    finding_counts: collections.Counter[str],
    finding_examples: dict[str, list[dict[str, str]]],
) -> None:
    fingerprint = _record_fingerprint(record)
    stack: list[tuple[Any, int]] = [(record, 0)]
    nodes = 0
    while stack:
        value, depth = stack.pop()
        nodes += 1
        if nodes > limits.max_nodes_per_record:
            raise RightsAuditError(f"{source_path}: {fingerprint}: node ceiling exceeded")
        if depth > limits.max_depth:
            raise RightsAuditError(f"{source_path}: {fingerprint}: nesting ceiling exceeded")
        if isinstance(value, dict):
            for raw_key, child in value.items():
                key = _normalise_key(raw_key)
                kinds: list[str] = []
                if key in FORBIDDEN_BODY_FIELDS:
                    kinds.append("prohibited_body_field")
                if key in CREDENTIAL_FIELDS:
                    kinds.append("credential_field")
                for kind in kinds:
                    finding_counts[kind] += 1
                    examples = finding_examples.setdefault(kind, [])
                    if len(examples) < limits.example_limit:
                        examples.append(
                            {
                                "record_fingerprint": fingerprint,
                                "source": source_path,
                                "field": key,
                                "value_retained": "false",
                            }
                        )
                stack.append((child, depth + 1))
        elif isinstance(value, list):
            stack.extend((child, depth + 1) for child in value)
        elif isinstance(value, str):
            if len(value) > limits.max_string_chars:
                finding_counts["oversized_string"] += 1
                examples = finding_examples.setdefault("oversized_string", [])
                if len(examples) < limits.example_limit:
                    examples.append(
                        {
                            "record_fingerprint": fingerprint,
                            "source": source_path,
                            "field": "redacted",
                            "value_retained": "false",
                        }
                    )
                continue
            if value.lstrip().casefold().startswith(("<!doctype html", "<html")):
                finding_counts["complete_html_document"] += 1
                examples = finding_examples.setdefault("complete_html_document", [])
                if len(examples) < limits.example_limit:
                    examples.append(
                        {
                            "record_fingerprint": fingerprint,
                            "source": source_path,
                            "field": "redacted",
                            "value_retained": "false",
                        }
                    )
            for kind, pattern in _SECRET_PATTERNS:
                if pattern.search(value):
                    finding_counts[kind] += 1
                    examples = finding_examples.setdefault(kind, [])
                    if len(examples) < limits.example_limit:
                        examples.append(
                            {
                                "record_fingerprint": fingerprint,
                                "source": source_path,
                                "field": "redacted",
                                "value_retained": "false",
                            }
                        )


def _mask_names(mask: int) -> list[str]:
    return [name for name in TRIGGERS if mask & TRIGGER_BITS[name]]


def _insert_item(connection: sqlite3.Connection, fingerprint: str, source_kind: str, mask: int) -> None:
    connection.execute(
        "INSERT INTO items(fingerprint, source_kinds, trigger_mask, observations) VALUES (?, ?, ?, 1) "
        "ON CONFLICT(fingerprint) DO UPDATE SET "
        "source_kinds = CASE "
        "WHEN instr(',' || items.source_kinds || ',', ',' || excluded.source_kinds || ',') > 0 "
        "THEN items.source_kinds ELSE items.source_kinds || ',' || excluded.source_kinds END, "
        "trigger_mask = items.trigger_mask | excluded.trigger_mask, observations = items.observations + 1",
        (fingerprint, source_kind, mask),
    )


def _nested_shard_rows(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        if isinstance(value.get("path"), str) and isinstance(value.get("sha256"), str):
            yield value
        for child in value.values():
            yield from _nested_shard_rows(child)
    elif isinstance(value, list):
        for child in value:
            yield from _nested_shard_rows(child)


def _publication_assets(
    publication_manifest_path: Path,
    bundle_root: Path,
    snapshot: str,
    limits: AuditLimits,
) -> tuple[dict[Path, dict[str, Any]], dict[Path, str], list[str]]:
    manifest = _load_json(publication_manifest_path, limits.max_manifest_bytes)
    if not isinstance(manifest, dict):
        raise RightsAuditError("publication manifest must be an object")
    errors: list[str] = []
    if manifest.get("snapshot") != snapshot:
        errors.append("publication manifest snapshot differs from release snapshot")

    assets: dict[Path, dict[str, Any]] = {}
    primary: dict[Path, str] = {}
    main_shards = manifest.get("shards")
    if not isinstance(main_shards, dict):
        errors.append("publication manifest has no primary shard object")
        main_shards = {}
    for kind, rows in main_shards.items():
        if not isinstance(rows, list):
            errors.append(f"publication primary shard group {kind} is not an array")
            continue
        for row in rows:
            if not isinstance(row, dict):
                errors.append(f"publication primary shard row {kind} is not an object")
                continue
            try:
                path = _safe_path(bundle_root, row.get("path"), f"publication shard {kind}")
            except RightsAuditError as exc:
                errors.append(str(exc))
                continue
            assets[path] = row
            primary[path] = str(kind)

    index_queue: list[Path] = []
    indexes = manifest.get("indexes")
    if isinstance(indexes, dict):
        for name, relative in indexes.items():
            try:
                index_queue.append(_safe_path(bundle_root, relative, f"publication index {name}"))
            except RightsAuditError as exc:
                errors.append(str(exc))
    seen_indexes: set[Path] = set()
    while index_queue:
        index_path = index_queue.pop()
        if index_path in seen_indexes:
            continue
        seen_indexes.add(index_path)
        try:
            index_document = _load_json(index_path, limits.max_manifest_bytes)
        except RightsAuditError as exc:
            errors.append(str(exc))
            continue
        # Index documents are themselves scanned, even when they do not point to shards.
        assets.setdefault(index_path, {})
        if isinstance(index_document, dict):
            index_snapshot = index_document.get("snapshot")
            if index_snapshot is not None and index_snapshot != snapshot:
                errors.append(f"{index_path.relative_to(bundle_root)} snapshot differs from release snapshot")
            metadata_path = index_document.get("shard_metadata")
            if isinstance(metadata_path, str):
                try:
                    index_queue.append(_safe_path(bundle_root, metadata_path, "search shard metadata"))
                except RightsAuditError as exc:
                    errors.append(str(exc))
            for row in _nested_shard_rows(index_document):
                try:
                    path = _safe_path(bundle_root, row.get("path"), "publication indexed shard")
                except RightsAuditError as exc:
                    errors.append(str(exc))
                    continue
                existing = assets.get(path)
                if existing and existing.get("sha256") not in (None, row.get("sha256")):
                    errors.append(f"conflicting integrity metadata for {path.relative_to(bundle_root)}")
                assets[path] = row
        else:
            errors.append(f"publication index {index_path.relative_to(bundle_root)} is not an object")

    if len(assets) > limits.max_files:
        raise RightsAuditError(f"publication asset count exceeds {limits.max_files}")
    return assets, primary, errors


def _validate_asset(path: Path, metadata: dict[str, Any], snapshot: str, limits: AuditLimits) -> tuple[str, int]:
    digest, size = _sha256_file(path, limits.max_compressed_bytes_per_file)
    expected_hash = metadata.get("sha256")
    if expected_hash is not None and digest != expected_hash:
        raise RightsAuditError(f"{path}: SHA-256 differs from publication manifest")
    expected_size = metadata.get("compressed_bytes")
    if expected_size is not None and size != expected_size:
        raise RightsAuditError(f"{path}: compressed byte count differs from publication manifest")
    expected_snapshot = metadata.get("snapshot")
    if expected_snapshot is not None and expected_snapshot != snapshot:
        raise RightsAuditError(f"{path}: shard snapshot differs from release snapshot")
    return digest, size


def _corpus_record_manifests(
    root: Path,
    corpus_manifest_paths: Iterable[Path],
    snapshot: str,
    limits: AuditLimits,
) -> tuple[list[Path], list[dict[str, Any]], list[str]]:
    record_manifests: list[Path] = []
    documents: list[dict[str, Any]] = []
    errors: list[str] = []
    for path in corpus_manifest_paths:
        resolved = path if path.is_absolute() else root / path
        resolved = resolved.resolve()
        if root.resolve() not in resolved.parents or not resolved.is_file():
            errors.append("corpus manifest path is unsafe or missing")
            continue
        try:
            document = _load_json(resolved, limits.max_manifest_bytes)
        except RightsAuditError as exc:
            errors.append(str(exc))
            continue
        if not isinstance(document, dict):
            errors.append(f"{resolved.relative_to(root)} is not an object")
            continue
        documents.append(document)
        document_snapshot = document.get("snapshot")
        if document_snapshot is not None and document_snapshot != snapshot:
            errors.append(f"{resolved.relative_to(root)} snapshot differs from release snapshot")
        if document.get("metadata_only") is False or document.get("complete_page_bodies_retained") is True:
            errors.append(f"{resolved.relative_to(root)} contradicts the metadata-only release boundary")
        if document.get("schema") == "govuk-okf-jsonl-shards.v1":
            record_manifests.append(resolved)
            continue
        candidates: list[Any] = [
            document.get("source_record_manifest"),
            document.get("hydrated_records_path"),
            document.get("inventory_path"),
        ]
        reconciliation = document.get("reconciliation")
        if isinstance(reconciliation, dict):
            candidates.extend(
                [
                    reconciliation.get("hydrated_records_path"),
                    reconciliation.get("inventory_path"),
                ]
            )
        elif isinstance(reconciliation, str):
            try:
                reconciliation_path = _safe_path(root, reconciliation, "corpus reconciliation")
                reconciliation_document = _load_json(reconciliation_path, limits.max_manifest_bytes)
                if isinstance(reconciliation_document, dict):
                    if reconciliation_document.get("snapshot") != snapshot:
                        errors.append("corpus reconciliation snapshot differs from release snapshot")
                    candidates.extend(
                        [
                            reconciliation_document.get("hydrated_records_path"),
                            reconciliation_document.get("inventory_path"),
                        ]
                    )
            except RightsAuditError as exc:
                errors.append(str(exc))
        for candidate in candidates:
            if not isinstance(candidate, str):
                continue
            try:
                record_manifests.append(_safe_path(root, candidate, "corpus source-record manifest"))
            except RightsAuditError as exc:
                errors.append(str(exc))
        if not any(isinstance(candidate, str) for candidate in candidates):
            errors.append(f"{resolved.relative_to(root)} does not identify a source-record manifest")
    unique = sorted(set(record_manifests))
    if len(unique) > limits.max_files:
        raise RightsAuditError("corpus record-manifest count exceeds file ceiling")
    return unique, documents, errors


def _policy_evidence(root: Path, limits: AuditLimits) -> tuple[dict[str, Any], list[str]]:
    preflight_path = root / "research" / "source-preflight.json"
    constraints_path = root / "research" / "source-constraints.json"
    errors: list[str] = []
    preflight = _load_json(preflight_path, limits.max_manifest_bytes)
    constraints = _load_json(constraints_path, limits.max_manifest_bytes)
    if not isinstance(preflight, dict) or not isinstance(constraints, dict):
        raise RightsAuditError("policy evidence documents must be objects")
    official = preflight.get("official_sources")
    by_id = (
        {
            row.get("id"): row
            for row in official
            if isinstance(row, dict) and isinstance(row.get("id"), str)
        }
        if isinstance(official, list)
        else {}
    )
    for source_id in sorted(POLICY_SOURCE_IDS):
        row = by_id.get(source_id)
        if not row or row.get("ok") is not True or row.get("status") != 200:
            errors.append(f"policy source {source_id} did not pass the frozen official-source preflight")
    constraint_rows = constraints.get("constraints")
    rights_constraint = (
        next(
            (
                row
                for row in constraint_rows
                if isinstance(row, dict) and row.get("id") == "SRC-CONSTRAINT-006"
            ),
            None,
        )
        if isinstance(constraint_rows, list)
        else None
    )
    if not rights_constraint or rights_constraint.get("class") != "item_specific_rights":
        errors.append("SRC-CONSTRAINT-006 item-specific rights constraint is missing")
    policy_permits_metadata_links = bool(
        rights_constraint
        and "metadata-and-link default" in str(rights_constraint.get("disposition", "")).casefold()
    )
    return (
        {
            "source_preflight": {
                "path": "research/source-preflight.json",
                "sha256": _sha256_file(preflight_path)[0],
                "completed_at": preflight.get("completed_at"),
                "required_policy_sources": sorted(POLICY_SOURCE_IDS),
            },
            "source_constraints": {
                "path": "research/source-constraints.json",
                "sha256": _sha256_file(constraints_path)[0],
                "rights_constraint_id": "SRC-CONSTRAINT-006",
            },
            "metadata_and_link_default_permits_unresolved_triggers": policy_permits_metadata_links,
            "checks_passed": not errors,
        },
        errors,
    )


def _load_reviews(
    connection: sqlite3.Connection,
    path: Path | None,
    root: Path,
    snapshot: str,
    limits: AuditLimits,
) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    if path is None or not path.is_file():
        return {"provided": False, "review_count": 0, "sha256": None}, errors
    document = _load_json(path, limits.max_manifest_bytes)
    if not isinstance(document, dict) or document.get("schema") != "afhf-govuk-okf-rights-review-ledger.v1":
        return {"provided": True, "review_count": 0, "sha256": _sha256_file(path)[0]}, [
            "rights review ledger schema is invalid"
        ]
    if document.get("snapshot") != snapshot:
        errors.append("rights review ledger snapshot differs from release snapshot")
    rows = document.get("reviews")
    if not isinstance(rows, list):
        errors.append("rights review ledger reviews must be an array")
        rows = []
    for ordinal, row in enumerate(rows):
        if not isinstance(row, dict):
            errors.append(f"rights review {ordinal} is not an object")
            continue
        fingerprint = row.get("record_fingerprint")
        trigger_ids = row.get("trigger_ids")
        disposition = row.get("disposition")
        evidence_ids = row.get("evidence_ids")
        valid = (
            isinstance(fingerprint, str)
            and re.fullmatch(r"sha256:[0-9a-f]{64}", fingerprint) is not None
            and isinstance(trigger_ids, list)
            and trigger_ids
            and all(name in TRIGGER_BITS for name in trigger_ids)
            and disposition in VALID_REVIEW_DISPOSITIONS
            and isinstance(row.get("reviewed_by"), str)
            and bool(row.get("reviewed_by"))
            and isinstance(row.get("reviewed_at"), str)
            and bool(row.get("reviewed_at"))
            and isinstance(evidence_ids, list)
            and bool(evidence_ids)
        )
        if not valid:
            errors.append(f"rights review {ordinal} is incomplete or invalid")
            continue
        mask = 0
        for name in trigger_ids:
            mask |= TRIGGER_BITS[name]
        connection.execute(
            "INSERT INTO reviews(fingerprint, trigger_mask, disposition) VALUES (?, ?, ?) "
            "ON CONFLICT(fingerprint) DO UPDATE SET trigger_mask = reviews.trigger_mask | excluded.trigger_mask, "
            "disposition = excluded.disposition",
            (fingerprint, mask, disposition),
        )
    connection.commit()
    return {
        "provided": True,
        "path": path.relative_to(root).as_posix(),
        "review_count": len(rows),
        "sha256": _sha256_file(path)[0],
    }, errors


def audit_release(
    root: Path,
    *,
    release_manifest_path: Path | None = None,
    publication_manifest_path: Path | None = None,
    corpus_manifest_paths: Iterable[Path] = (),
    review_ledger_path: Path | None = None,
    generated_at: str | None = None,
    review_packet_path: Path | None = None,
    comparator_evidence_paths: Iterable[Path] | None = None,
    auto_review_ledger: bool = True,
    limits: AuditLimits = AuditLimits(),
) -> dict[str, Any]:
    """Audit one release snapshot and return deterministic machine evidence."""

    root = root.resolve()
    release_path = (release_manifest_path or root / "release" / "manifest.yaml").resolve()
    release = _load_json(release_path, limits.max_manifest_bytes)
    if not isinstance(release, dict):
        raise RightsAuditError("release manifest must be an object")
    snapshot_document = release.get("snapshot")
    if not isinstance(snapshot_document, dict) or not isinstance(snapshot_document.get("id"), str):
        raise RightsAuditError("release manifest has no snapshot identity")
    snapshot = snapshot_document["id"]
    snapshot_kind = snapshot_document.get("kind")
    sampled = snapshot_document.get("sampled")
    artifacts = release.get("artifacts")
    if not isinstance(artifacts, dict):
        raise RightsAuditError("release manifest artifacts must be an object")
    bundle_relative = artifacts.get("bundle")
    if not isinstance(bundle_relative, str):
        raise RightsAuditError("release manifest does not identify the bundle")
    bundle_root = (root / bundle_relative).resolve()
    if root not in bundle_root.parents or not bundle_root.is_dir():
        raise RightsAuditError("release bundle path is unsafe or missing")

    if publication_manifest_path is None:
        descriptor = _safe_path(root, artifacts.get("descriptor"), "release descriptor")
        descriptor_document = _load_json(descriptor, limits.max_manifest_bytes)
        data_manifest_reference = (
            descriptor_document.get("entrypoints", {}).get("data_manifest")
            if isinstance(descriptor_document, dict)
            else None
        )
        data_manifest = reference_path(data_manifest_reference)
        publication_path = _safe_path(descriptor.parent, data_manifest, "descriptor data manifest")
    else:
        publication_path = publication_manifest_path.resolve()

    errors: list[str] = []
    finding_counts: collections.Counter[str] = collections.Counter()
    finding_examples: dict[str, list[dict[str, str]]] = {}
    assets, primary_assets, publication_errors = _publication_assets(
        publication_path, bundle_root, snapshot, limits
    )
    errors.extend(publication_errors)
    policy_evidence, policy_errors = _policy_evidence(root, limits)
    errors.extend(policy_errors)
    if comparator_evidence_paths is None:
        resolved_comparator_paths = [root / path for path in DEFAULT_COMPARATOR_EVIDENCE_PATHS]
    else:
        resolved_comparator_paths = [
            path if path.is_absolute() else root / path for path in comparator_evidence_paths
        ]
    expected_comparator_paths = {
        (root / path).resolve() for path in DEFAULT_COMPARATOR_EVIDENCE_PATHS
    }
    if len(resolved_comparator_paths) != len(DEFAULT_COMPARATOR_EVIDENCE_PATHS) or {
        path.resolve() for path in resolved_comparator_paths
    } != expected_comparator_paths:
        errors.append("rights audit must bind both repository GOV.UK Chat comparator documents")
    comparator_evidence, comparator_errors = _comparator_rights_evidence(
        root, resolved_comparator_paths, limits
    )
    errors.extend(comparator_errors)
    corpus_paths = [path if path.is_absolute() else root / path for path in corpus_manifest_paths]
    corpus_input_bindings: list[dict[str, Any]] = []
    for ordinal, path in enumerate(corpus_paths):
        try:
            corpus_input_bindings.append(
                _bound_file(
                    root,
                    path,
                    f"corpus manifest {ordinal}",
                    limits.max_manifest_bytes,
                )
            )
        except RightsAuditError as exc:
            errors.append(str(exc))
    record_manifests, corpus_documents, corpus_errors = _corpus_record_manifests(
        root, corpus_paths, snapshot, limits
    )
    errors.extend(corpus_errors)
    resolved_review_path = review_ledger_path
    if resolved_review_path is None and auto_review_ledger:
        candidate = root / "governance" / "rights-review-ledger.json"
        resolved_review_path = candidate if candidate.is_file() else None
    elif resolved_review_path is not None and not resolved_review_path.is_absolute():
        resolved_review_path = root / resolved_review_path
    review_input_binding: dict[str, Any] | None = None
    if resolved_review_path is not None:
        try:
            review_input_binding = _bound_file(
                root,
                resolved_review_path,
                "rights review ledger",
                limits.max_manifest_bytes,
            )
        except RightsAuditError as exc:
            errors.append(str(exc))

    files_scanned = 0
    compressed_bytes_scanned = 0
    uncompressed_records = 0
    records_by_source: collections.Counter[str] = collections.Counter()
    asset_hash = hashlib.sha256()
    corpus_hash = hashlib.sha256()

    with tempfile.TemporaryDirectory(prefix="govuk-okf-rights-") as directory:
        connection = sqlite3.connect(Path(directory) / "audit.sqlite3")
        try:
            connection.executescript(
                "PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF; PRAGMA temp_store=FILE;"
                "CREATE TABLE items(fingerprint TEXT PRIMARY KEY, source_kinds TEXT NOT NULL, "
                "trigger_mask INTEGER NOT NULL, observations INTEGER NOT NULL);"
                "CREATE TABLE reviews(fingerprint TEXT PRIMARY KEY, trigger_mask INTEGER NOT NULL, "
                "disposition TEXT NOT NULL);"
            )
            publication_document = _load_json(publication_path, limits.max_manifest_bytes)
            manifest_documents: list[tuple[str, dict[str, Any]]] = [
                (release_path.relative_to(root).as_posix(), release),
            ]
            if isinstance(publication_document, dict):
                manifest_documents.append(
                    (publication_path.relative_to(root).as_posix(), publication_document)
                )
            manifest_documents.extend(
                (f"corpus-manifest-{ordinal}", document)
                for ordinal, document in enumerate(corpus_documents)
                if isinstance(document, dict)
            )
            for source_path, document in manifest_documents:
                _scan_record_safety(document, source_path, limits, finding_counts, finding_examples)
            for path in sorted(assets):
                metadata = assets[path]
                try:
                    digest, size = _validate_asset(path, metadata, snapshot, limits)
                    records = 0
                    relative = path.relative_to(bundle_root).as_posix()
                    source_kind = primary_assets.get(path)
                    for record in _iter_json_records(path, limits):
                        records += 1
                        uncompressed_records += 1
                        if uncompressed_records > limits.max_records:
                            raise RightsAuditError(f"record count exceeds {limits.max_records}")
                        _scan_record_safety(record, relative, limits, finding_counts, finding_examples)
                        if source_kind:
                            _insert_item(
                                connection,
                                _record_fingerprint(record),
                                source_kind,
                                _trigger_mask(record, source_kind, limits),
                            )
                            records_by_source[f"publication:{source_kind}"] += 1
                    expected_uncompressed = metadata.get("uncompressed_bytes")
                    if expected_uncompressed is not None:
                        opener = gzip.open if path.suffix == ".gz" else open
                        total_uncompressed = 0
                        with opener(path, "rb") as stream:
                            while chunk := stream.read(1024 * 1024):
                                total_uncompressed += len(chunk)
                                if total_uncompressed > limits.max_uncompressed_bytes_per_file:
                                    raise RightsAuditError(f"{relative}: uncompressed byte ceiling exceeded")
                        if total_uncompressed != expected_uncompressed:
                            raise RightsAuditError(f"{relative}: uncompressed byte count differs from manifest")
                    files_scanned += 1
                    compressed_bytes_scanned += size
                    asset_hash.update(f"{relative}\0{digest}\0{records}\n".encode("utf-8"))
                except RightsAuditError as exc:
                    errors.append(str(exc))

            for manifest_path in record_manifests:
                try:
                    manifest = _load_json(manifest_path, limits.max_manifest_bytes)
                    if not isinstance(manifest, dict) or manifest.get("schema") != "govuk-okf-jsonl-shards.v1":
                        raise RightsAuditError(f"{manifest_path}: unsupported corpus shard manifest")
                    _scan_record_safety(
                        manifest,
                        manifest_path.relative_to(root).as_posix(),
                        limits,
                        finding_counts,
                        finding_examples,
                    )
                    rows = manifest.get("shards")
                    if not isinstance(rows, list):
                        raise RightsAuditError(f"{manifest_path}: corpus shards must be an array")
                    aggregate = hashlib.sha256()
                    total_records = 0
                    for row in rows:
                        if not isinstance(row, dict):
                            raise RightsAuditError(f"{manifest_path}: corpus shard row is not an object")
                        shard = _safe_path(manifest_path.parent, row.get("path"), "corpus source-record shard")
                        digest, size = _sha256_file(shard, limits.max_compressed_bytes_per_file)
                        if digest != row.get("file_sha256") or size != row.get("bytes"):
                            raise RightsAuditError(f"{shard}: corpus shard file integrity failed")
                        shard_digest = hashlib.sha256()
                        shard_records = 0
                        relative = shard.relative_to(root).as_posix()
                        for record in _iter_jsonl_gzip(shard, limits):
                            encoded = canonical_json_bytes(record)
                            aggregate.update(encoded)
                            shard_digest.update(encoded)
                            shard_records += 1
                            total_records += 1
                            uncompressed_records += 1
                            if uncompressed_records > limits.max_records:
                                raise RightsAuditError(f"record count exceeds {limits.max_records}")
                            _scan_record_safety(record, relative, limits, finding_counts, finding_examples)
                            _insert_item(
                                connection,
                                _record_fingerprint(record),
                                "corpus",
                                _trigger_mask(record, "corpus", limits),
                            )
                            records_by_source["corpus"] += 1
                        if shard_records != row.get("records") or shard_digest.hexdigest() != row.get(
                            "canonical_sha256"
                        ):
                            raise RightsAuditError(f"{shard}: corpus shard canonical integrity failed")
                        files_scanned += 1
                        compressed_bytes_scanned += size
                        corpus_hash.update(f"{relative}\0{digest}\0{shard_records}\n".encode("utf-8"))
                    if total_records != manifest.get("records") or aggregate.hexdigest() != manifest.get(
                        "canonical_sha256"
                    ):
                        raise RightsAuditError(f"{manifest_path}: corpus manifest canonical integrity failed")
                except RightsAuditError as exc:
                    errors.append(str(exc))

            connection.commit()
            review_evidence, review_errors = _load_reviews(
                connection, resolved_review_path, root, snapshot, limits
            )
            errors.extend(review_errors)

            classification_count = int(connection.execute("SELECT COUNT(*) FROM items").fetchone()[0])
            review_required = int(
                connection.execute("SELECT COUNT(*) FROM items WHERE trigger_mask <> 0").fetchone()[0]
            )
            ogl_default = classification_count - review_required
            reviewed = int(
                connection.execute(
                    "SELECT COUNT(*) FROM items i JOIN reviews r ON r.fingerprint=i.fingerprint "
                    "WHERE i.trigger_mask <> 0 AND (r.trigger_mask & i.trigger_mask)=i.trigger_mask"
                ).fetchone()[0]
            )
            unresolved = review_required - reviewed
            trigger_rows: dict[str, dict[str, Any]] = {}
            trigger_digest = hashlib.sha256()
            for fingerprint, mask in connection.execute(
                "SELECT fingerprint, trigger_mask FROM items WHERE trigger_mask <> 0 ORDER BY fingerprint"
            ):
                names = _mask_names(int(mask))
                trigger_digest.update(f"{fingerprint}\0{','.join(names)}\n".encode("utf-8"))
            for name in TRIGGERS:
                bit = TRIGGER_BITS[name]
                count = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM items WHERE (trigger_mask & ?) <> 0", (bit,)
                    ).fetchone()[0]
                )
                examples = [
                    row[0]
                    for row in connection.execute(
                        "SELECT fingerprint FROM items WHERE (trigger_mask & ?) <> 0 ORDER BY fingerprint LIMIT ?",
                        (bit, limits.example_limit),
                    )
                ]
                per_trigger_digest = hashlib.sha256()
                for (fingerprint,) in connection.execute(
                    "SELECT fingerprint FROM items WHERE (trigger_mask & ?) <> 0 ORDER BY fingerprint", (bit,)
                ):
                    per_trigger_digest.update(f"{fingerprint}\n".encode("utf-8"))
                trigger_rows[name] = {
                    "items": count,
                    "item_set_sha256": per_trigger_digest.hexdigest(),
                    "example_record_fingerprints": examples,
                    "examples_contain_source_values": False,
                }

            packet_evidence: dict[str, Any] | None = None
            if review_packet_path is not None:
                review_packet_path.parent.mkdir(parents=True, exist_ok=True)
                packet_hash = hashlib.sha256()
                packet_count = 0
                with review_packet_path.open("wb") as stream:
                    for fingerprint, source_kinds, mask in connection.execute(
                        "SELECT fingerprint, source_kinds, trigger_mask FROM items "
                        "WHERE trigger_mask <> 0 ORDER BY fingerprint"
                    ):
                        row = {
                            "record_fingerprint": fingerprint,
                            "source_kinds": sorted(source_kinds.split(",")),
                            "trigger_ids": _mask_names(int(mask)),
                            "source_values_retained": False,
                        }
                        encoded = canonical_json_bytes(row) + b"\n"
                        stream.write(encoded)
                        packet_hash.update(encoded)
                        packet_count += 1
                packet_evidence = {
                    "path": review_packet_path.as_posix(),
                    "records": packet_count,
                    "sha256": packet_hash.hexdigest(),
                    "contains_source_values": False,
                }

            violation_count = sum(finding_counts.values())
            full_snapshot = snapshot_kind == "full_corpus" and sampled is False
            corpus_bound = bool(corpus_paths) and bool(record_manifests) and not corpus_errors
            controls_passed = (
                not errors
                and violation_count == 0
                and policy_evidence["checks_passed"]
                and comparator_evidence["controls_passed"]
            )
            unresolved_permitted = bool(
                policy_evidence["metadata_and_link_default_permits_unresolved_triggers"]
            )
            release_blocking_unresolved = 0 if unresolved_permitted else unresolved
            audit_passed = (
                controls_passed
                and full_snapshot
                and corpus_bound
                and release_blocking_unresolved == 0
            )
            status = "passed" if audit_passed else "checkpoint"
            publication_generated_at = publication_document.get("generated_at")
            effective_generated_at = generated_at or publication_generated_at
            reproduction = _release_reproduction(release)
            frozen_source_binding = (
                reproduction.get("source_binding") if isinstance(reproduction, dict) else None
            )
            if full_snapshot and not isinstance(frozen_source_binding, dict):
                errors.append("full-corpus rights audit lacks the frozen-source reproduction binding")
                controls_passed = False
                audit_passed = False
                status = "checkpoint"
            result = {
                "schema": RIGHTS_AUDIT_SCHEMA,
                "snapshot": snapshot,
                "snapshot_kind": snapshot_kind,
                "sampled": sampled,
                "generated_at": effective_generated_at,
                "status": status,
                "rights_privacy_audit_passed": audit_passed,
                "release_eligible": audit_passed,
                "mechanical_controls_passed": controls_passed,
                "policy": {
                    "default_classification": "ogl_v3_except_where_otherwise_stated",
                    "classification_caveat": (
                        "ogl_default_candidate means no machine trigger was observed; it is not a legal "
                        "determination that OGL applies to the item."
                    ),
                    "metadata_only": True,
                    "complete_page_or_attachment_bodies_permitted": False,
                    "credential_fields_permitted": False,
                    "fair_dealing_or_item_review_required_for_triggers": True,
                },
                "snapshot_binding": {
                    "release_manifest": {
                        "path": release_path.relative_to(root).as_posix(),
                        "sha256": _sha256_file(release_path)[0],
                    },
                    "publication_manifest": {
                        "path": publication_path.relative_to(root).as_posix(),
                        "sha256": _sha256_file(publication_path)[0],
                    },
                    "publication_asset_set_sha256": asset_hash.hexdigest(),
                    "corpus_manifest_count": len(corpus_paths),
                    "resolved_corpus_record_manifest_count": len(record_manifests),
                    "corpus_asset_set_sha256": corpus_hash.hexdigest(),
                    "frozen_source": frozen_source_binding,
                    "full_unsampled_snapshot": full_snapshot,
                    "corpus_snapshot_bound": corpus_bound,
                },
                "audit_input_contract": {
                    "schema": RIGHTS_AUDIT_INPUT_SCHEMA,
                    "generated_at": effective_generated_at,
                    "publication_manifest": _bound_file(
                        root,
                        publication_path,
                        "publication manifest",
                        limits.max_manifest_bytes,
                    ),
                    "corpus_manifests": corpus_input_bindings,
                    "review_ledger": review_input_binding,
                    "comparator_evidence": comparator_evidence["files"],
                },
                "scan": {
                    "mode": "bounded_streaming_disk_backed",
                    "limits": {
                        "max_files": limits.max_files,
                        "max_records": limits.max_records,
                        "max_manifest_bytes": limits.max_manifest_bytes,
                        "max_compressed_bytes_per_file": limits.max_compressed_bytes_per_file,
                        "max_uncompressed_bytes_per_file": limits.max_uncompressed_bytes_per_file,
                        "max_record_bytes": limits.max_record_bytes,
                        "max_nodes_per_record": limits.max_nodes_per_record,
                        "max_depth": limits.max_depth,
                        "max_string_chars": limits.max_string_chars,
                    },
                    "publication_assets_scanned": len(assets),
                    "files_scanned": files_scanned,
                    "compressed_bytes_scanned": compressed_bytes_scanned,
                    "record_objects_scanned": uncompressed_records,
                    "classification_items": classification_count,
                    "record_observations_by_source": dict(sorted(records_by_source.items())),
                },
                "retention_and_secret_findings": {
                    "passed": violation_count == 0,
                    "finding_count": violation_count,
                    "counts": dict(sorted(finding_counts.items())),
                    "examples": {name: values for name, values in sorted(finding_examples.items())},
                    "source_values_retained_in_report": False,
                },
                "classification": {
                    "ogl_default_candidate_items": ogl_default,
                    "item_review_triggered_items": review_required,
                    "trigger_manifest_sha256": trigger_digest.hexdigest(),
                    "triggers": trigger_rows,
                },
                "review": {
                    **review_evidence,
                    "resolved_triggered_items": reviewed,
                    "unresolved_triggered_items": unresolved,
                    "policy_controlled_unresolved_triggered_items": unresolved if unresolved_permitted else 0,
                    "release_blocking_unresolved_triggered_items": release_blocking_unresolved,
                    "unresolved_triggers_are_release_blocking": release_blocking_unresolved != 0,
                    "all_triggered_items_resolved": unresolved == 0,
                    "review_packet": packet_evidence,
                },
                "policy_evidence": policy_evidence,
                "comparator_evidence": comparator_evidence,
                "corpus_manifest_declarations": {
                    "count": len(corpus_documents),
                    "metadata_only_false": sum(row.get("metadata_only") is False for row in corpus_documents),
                    "complete_page_bodies_retained_true": sum(
                        row.get("complete_page_bodies_retained") is True for row in corpus_documents
                    ),
                },
                "errors": sorted(set(errors)),
                "remaining_item_review_work": (
                    [] if unresolved == 0 else ["review or exception every snapshot-bound item trigger"]
                ),
                "remaining_release_blockers": [
                    item
                    for condition, item in (
                        (not full_snapshot, "rerun against the final unsampled full-corpus T1 snapshot"),
                        (not corpus_bound, "supply the final T1 hydration/source-record manifest"),
                        (violation_count != 0, "remove every prohibited body/credential finding and rerun"),
                        (
                            not policy_evidence["checks_passed"],
                            "refresh and resolve robots/reuse/OGL policy evidence",
                        ),
                        (
                            release_blocking_unresolved != 0,
                            "resolve item triggers not covered by the metadata-and-link publication policy",
                        ),
                        (bool(errors), "resolve all manifest, integrity and snapshot-binding errors"),
                    )
                    if condition
                ],
            }
            return result
        finally:
            connection.close()


def validate_audit_evidence(
    root: Path,
    evidence: dict[str, Any],
    *,
    require_release: bool = False,
    allow_missing_corpus_inputs: bool = False,
    limits: AuditLimits = AuditLimits(),
) -> list[str]:
    """Validate current control bindings without rescanning archived corpus bytes."""

    root = root.resolve()
    errors: list[str] = []
    if evidence.get("schema") != RIGHTS_AUDIT_SCHEMA:
        return ["rights/privacy evidence schema is invalid"]
    contract = evidence.get("audit_input_contract")
    binding = evidence.get("snapshot_binding")
    if not isinstance(contract, dict) or contract.get("schema") != RIGHTS_AUDIT_INPUT_SCHEMA:
        return ["rights/privacy evidence lacks its audit input contract"]
    if not isinstance(binding, dict):
        return ["rights/privacy evidence lacks snapshot bindings"]
    try:
        release_path = _safe_path(root, "release/manifest.yaml", "release manifest")
        release = _load_json(release_path, limits.max_manifest_bytes)
    except RightsAuditError as exc:
        return [str(exc)]
    if not isinstance(release, dict):
        return ["release manifest must be an object"]
    release_binding = binding.get("release_manifest")
    expected_release = _bound_file(
        root, release_path, "release manifest", limits.max_manifest_bytes
    )
    if not isinstance(release_binding, dict) or {
        "path": release_binding.get("path"),
        "sha256": release_binding.get("sha256"),
    } != {
        "path": expected_release["path"],
        "sha256": expected_release["sha256"],
    }:
        errors.append("rights/privacy evidence release-manifest binding is stale")
    publication_path, publication_errors = _load_bound_path(
        root,
        contract.get("publication_manifest"),
        "rights audit publication manifest",
        limits.max_manifest_bytes,
        allow_missing=False,
    )
    errors.extend(publication_errors)
    publication_binding = binding.get("publication_manifest")
    contract_publication = contract.get("publication_manifest")
    if not isinstance(publication_binding, dict) or not isinstance(contract_publication, dict) or {
        "path": publication_binding.get("path"),
        "sha256": publication_binding.get("sha256"),
    } != {
        "path": contract_publication.get("path"),
        "sha256": contract_publication.get("sha256"),
    }:
        errors.append("rights/privacy publication-manifest bindings disagree")
    if publication_path is None:
        errors.append("rights audit publication manifest is unavailable")
    corpus_bindings = contract.get("corpus_manifests")
    if not isinstance(corpus_bindings, list):
        errors.append("rights audit corpus-manifest contract is invalid")
        corpus_bindings = []
    for ordinal, corpus_binding in enumerate(corpus_bindings):
        _, corpus_errors = _load_bound_path(
            root,
            corpus_binding,
            f"rights audit corpus manifest {ordinal}",
            limits.max_manifest_bytes,
            allow_missing=allow_missing_corpus_inputs,
        )
        errors.extend(corpus_errors)
    review_binding = contract.get("review_ledger")
    if review_binding is not None:
        _, review_errors = _load_bound_path(
            root,
            review_binding,
            "rights audit review ledger",
            limits.max_manifest_bytes,
            allow_missing=False,
        )
        errors.extend(review_errors)
        review = evidence.get("review")
        if not isinstance(review, dict) or {
            "path": review.get("path"),
            "sha256": review.get("sha256"),
        } != {
            "path": review_binding.get("path"),
            "sha256": review_binding.get("sha256"),
        }:
            errors.append("rights/privacy review-ledger bindings disagree")
    comparator_bindings = contract.get("comparator_evidence", [])
    if not isinstance(comparator_bindings, list):
        errors.append("rights audit comparator-evidence contract is invalid")
        comparator_bindings = []
    comparator_paths: list[Path] = []
    for ordinal, comparator_binding in enumerate(comparator_bindings):
        path, comparator_errors = _load_bound_path(
            root,
            comparator_binding,
            f"rights audit comparator evidence {ordinal}",
            limits.max_manifest_bytes,
            allow_missing=False,
        )
        errors.extend(comparator_errors)
        if path is not None:
            comparator_paths.append(path)
    expected_comparator_paths = {
        (root / path).resolve() for path in DEFAULT_COMPARATOR_EVIDENCE_PATHS
    }
    if len(comparator_bindings) != len(DEFAULT_COMPARATOR_EVIDENCE_PATHS) or {
        path.resolve() for path in comparator_paths
    } != expected_comparator_paths:
        errors.append("rights audit must bind both repository GOV.UK Chat comparator documents")
    recomputed_comparator, comparator_errors = _comparator_rights_evidence(
        root, comparator_paths, limits
    )
    errors.extend(comparator_errors)
    if evidence.get("comparator_evidence") != recomputed_comparator:
        errors.append("rights/privacy comparator-evidence binding or disposition is stale")
    if contract.get("generated_at") != evidence.get("generated_at"):
        errors.append("rights/privacy generated-at differs from its audit input contract")
    snapshot = release.get("snapshot")
    release_snapshot = snapshot.get("id") if isinstance(snapshot, dict) else None
    if evidence.get("snapshot") != release_snapshot:
        errors.append("rights/privacy evidence snapshot differs from release manifest")
    reproduction = _release_reproduction(release)
    expected_source = (
        reproduction.get("source_binding") if isinstance(reproduction, dict) else None
    )
    if binding.get("frozen_source") != expected_source:
        errors.append("rights/privacy evidence differs from the frozen-source binding")
    if require_release:
        if evidence.get("rights_privacy_audit_passed") is not True:
            errors.append("rights/privacy release audit did not pass")
        if evidence.get("mechanical_controls_passed") is not True:
            errors.append("rights/privacy mechanical controls did not pass")
        if evidence.get("snapshot_kind") != "full_corpus" or evidence.get("sampled") is not False:
            errors.append("rights/privacy evidence is not for an unsampled full corpus")
        if binding.get("full_unsampled_snapshot") is not True:
            errors.append("rights/privacy full-snapshot binding is false")
        if binding.get("corpus_snapshot_bound") is not True:
            errors.append("rights/privacy corpus binding is false")
        if not corpus_bindings or binding.get("corpus_manifest_count") != len(corpus_bindings):
            errors.append("rights/privacy corpus-manifest bindings are incomplete")
    return errors


def rebind_audit_release(
    root: Path,
    evidence: dict[str, Any],
    *,
    allow_missing_corpus_inputs: bool = False,
    limits: AuditLimits = AuditLimits(),
) -> dict[str, Any]:
    """Bind completed audit findings to a release transition without rescanning them.

    This path is only for a candidate/final manifest transition where the
    publication, review ledger and every still-present corpus manifest retain
    their immutable audit-input hashes.  Missing corpus manifests are accepted
    only when the caller explicitly identifies them as archived external
    inputs.  Present-but-changed inputs always fail closed.
    """

    root = root.resolve()
    try:
        document = json.loads(json.dumps(evidence))
    except (TypeError, ValueError) as exc:
        raise RightsAuditError(f"rights/privacy evidence is not JSON serializable: {exc}") from exc
    if not isinstance(document, dict) or document.get("schema") != RIGHTS_AUDIT_SCHEMA:
        raise RightsAuditError("rights/privacy evidence schema is invalid")
    binding = document.get("snapshot_binding")
    prior_release = binding.get("release_manifest") if isinstance(binding, dict) else None
    if (
        not isinstance(binding, dict)
        or not isinstance(prior_release, dict)
        or prior_release.get("path") != "release/manifest.yaml"
        or not isinstance(prior_release.get("sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", prior_release["sha256"]) is None
    ):
        raise RightsAuditError("rights/privacy evidence has no valid prior release binding")
    contract = document.get("audit_input_contract")
    if not isinstance(contract, dict) or contract.get("schema") != RIGHTS_AUDIT_INPUT_SCHEMA:
        raise RightsAuditError("rights/privacy evidence lacks its audit input contract")

    corpus_bindings = contract.get("corpus_manifests")
    if not isinstance(corpus_bindings, list):
        raise RightsAuditError("rights audit corpus-manifest contract is invalid")
    archived_inputs = False
    input_errors: list[str] = []
    for ordinal, corpus_binding in enumerate(corpus_bindings):
        path, errors = _load_bound_path(
            root,
            corpus_binding,
            f"rights audit corpus manifest {ordinal}",
            limits.max_manifest_bytes,
            allow_missing=allow_missing_corpus_inputs,
        )
        input_errors.extend(errors)
        archived_inputs = archived_inputs or path is None
    if input_errors:
        raise RightsAuditError("; ".join(input_errors))
    if archived_inputs and not allow_missing_corpus_inputs:
        raise RightsAuditError("rights audit corpus inputs are unavailable")
    comparator_bindings = contract.get("comparator_evidence", [])
    if not isinstance(comparator_bindings, list):
        raise RightsAuditError("rights audit comparator-evidence contract is invalid")
    for ordinal, comparator_binding in enumerate(comparator_bindings):
        _, errors = _load_bound_path(
            root,
            comparator_binding,
            f"rights audit comparator evidence {ordinal}",
            limits.max_manifest_bytes,
            allow_missing=False,
        )
        input_errors.extend(errors)
    if input_errors:
        raise RightsAuditError("; ".join(input_errors))

    release_path = _safe_path(root, "release/manifest.yaml", "release manifest")
    release = _load_json(release_path, limits.max_manifest_bytes)
    if not isinstance(release, dict):
        raise RightsAuditError("release manifest must be an object")
    release_snapshot = release.get("snapshot")
    if (
        not isinstance(release_snapshot, dict)
        or release_snapshot.get("id") != document.get("snapshot")
    ):
        raise RightsAuditError("release transition changed the audited snapshot")
    binding["release_manifest"] = _bound_file(
        root, release_path, "release manifest", limits.max_manifest_bytes
    )
    reproduction = _release_reproduction(release)
    binding["frozen_source"] = (
        reproduction.get("source_binding") if isinstance(reproduction, dict) else None
    )
    document["release_binding_refresh"] = {
        "mode": "static_archived_input_validation" if archived_inputs else "exact_input_validation",
        "prior_release_manifest": prior_release,
        "release_manifest": binding["release_manifest"],
    }
    errors = validate_audit_evidence(
        root,
        document,
        require_release=True,
        allow_missing_corpus_inputs=allow_missing_corpus_inputs,
        limits=limits,
    )
    if errors:
        raise RightsAuditError("rights/privacy release rebinding failed: " + "; ".join(errors))
    return document


def write_audit(path: Path, result: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(pretty_json(result), encoding="utf-8")
