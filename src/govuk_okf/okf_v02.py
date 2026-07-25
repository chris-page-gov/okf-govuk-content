"""Deterministic OKF v0.2 Markdown rendering and conformance checks.

The GOV.UK publication uses JSON-valued YAML scalars in frontmatter.  JSON is
valid YAML, keeps generated values unambiguous, and lets the release validator
parse the exact emitted subset without adding an unpinned YAML implementation
to the reproducible build.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

OKF_VERSION = "0.2"
GENERATOR_ACTOR = "govuk-okf/0.1.0"
RESERVED_FILENAMES = {"index.md", "log.md"}
LIFECYCLE_STATUSES = {"draft", "stable", "deprecated"}
ACTOR_PATTERN = re.compile(r"^(?:human:[^\s]+|process:[^\s]+|[^/\s]+/[^/\s]+)$")


class FrontmatterError(ValueError):
    """Raised when generated OKF frontmatter cannot be parsed safely."""


def _json_value(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def render_frontmatter(metadata: Mapping[str, object]) -> str:
    """Render top-level JSON-valued YAML frontmatter deterministically."""

    lines = ["---"]
    for key, value in metadata.items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", key):
            raise FrontmatterError(f"unsafe frontmatter key: {key!r}")
        lines.append(f"{key}: {_json_value(value)}")
    lines.extend(("---", ""))
    return "\n".join(lines)


def parse_frontmatter(text: str) -> tuple[dict[str, Any] | None, str]:
    """Parse the deterministic frontmatter subset emitted by this project."""

    if not text.startswith("---\n"):
        return None, text
    end = text.find("\n---\n", 4)
    if end < 0:
        raise FrontmatterError("frontmatter has no closing delimiter")
    metadata: dict[str, Any] = {}
    for number, raw_line in enumerate(text[4:end].splitlines(), start=2):
        if not raw_line or raw_line.startswith((" ", "\t")) or ":" not in raw_line:
            raise FrontmatterError(
                f"frontmatter line {number} is outside the deterministic YAML subset"
            )
        key, encoded = raw_line.split(":", 1)
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", key):
            raise FrontmatterError(f"frontmatter line {number} has an invalid key")
        if key in metadata:
            raise FrontmatterError(f"frontmatter line {number} repeats {key!r}")
        try:
            metadata[key] = json.loads(encoded.strip())
        except json.JSONDecodeError as exc:
            raise FrontmatterError(
                f"frontmatter line {number} is not a JSON-valued YAML scalar"
            ) from exc
    return metadata, text[end + 5 :]


def _is_datetime(value: object) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def _is_date(value: object) -> bool:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _valid_actor(value: object) -> bool:
    return isinstance(value, str) and bool(ACTOR_PATTERN.fullmatch(value))


def source_last_modified(value: object) -> str | None:
    """Return the date part of an observed source modification datetime."""

    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.date().isoformat()


def concept_metadata(
    *,
    concept_type: str,
    title: str,
    description: str,
    resource: str,
    tags: Iterable[object],
    generated_at: str,
    snapshot_id: str,
    route: str,
    evidence_url: str = "",
    source_modified_at: object = None,
    extension: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Build honest v0.2 metadata for one deterministic projection concept."""

    metadata: dict[str, object] = {
        "type": concept_type,
        "title": title,
    }
    if description:
        metadata["description"] = description.replace("\r", " ").replace("\n", " ").strip()
    if resource:
        metadata["resource"] = resource
    clean_tags = sorted({str(tag) for tag in tags if str(tag).strip()})
    if clean_tags:
        metadata["tags"] = clean_tags
    metadata["generated"] = {"at": generated_at, "by": GENERATOR_ACTOR}
    sources: list[dict[str, object]] = []
    if evidence_url:
        source: dict[str, object] = {
            "id": "govuk-source",
            "resource": evidence_url,
            "title": "Official GOV.UK public metadata observation",
        }
        last_modified = source_last_modified(source_modified_at)
        if last_modified:
            source["last_modified"] = last_modified
        sources.append(source)
    if sources:
        metadata["sources"] = sources
    # These projections have not received a v0.2 verification event.  Draft is
    # an honest lifecycle signal; absence of ``verified`` keeps trust unverified.
    metadata["status"] = "draft"
    metadata["govuk"] = {
        "route": route,
        "snapshot": snapshot_id,
        **dict(extension or {}),
    }
    return metadata


def _verification_events(value: object) -> Sequence[object] | None:
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return value
    return None


def validate_concept_metadata(metadata: Mapping[str, object], *, label: str) -> list[str]:
    errors: list[str] = []
    concept_type = metadata.get("type")
    if not isinstance(concept_type, str) or not concept_type.strip():
        errors.append(f"{label}: type must be a non-empty string")
    generated = metadata.get("generated")
    if generated is not None:
        if not isinstance(generated, dict):
            errors.append(f"{label}: generated must be a mapping")
        else:
            if not _valid_actor(generated.get("by")):
                errors.append(f"{label}: generated.by does not follow the actor convention")
            if "at" in generated and not _is_datetime(generated.get("at")):
                errors.append(f"{label}: generated.at is not an ISO 8601 datetime")
    verified = metadata.get("verified")
    if verified is not None:
        events = _verification_events(verified)
        if events is None:
            errors.append(f"{label}: verified must be a mapping or list")
        else:
            for index, event in enumerate(events):
                if not isinstance(event, dict):
                    errors.append(f"{label}: verified[{index}] must be a mapping")
                    continue
                if not _valid_actor(event.get("by")):
                    errors.append(
                        f"{label}: verified[{index}].by does not follow the actor convention"
                    )
                if not _is_datetime(event.get("at")):
                    errors.append(f"{label}: verified[{index}].at is not an ISO 8601 datetime")
    status = metadata.get("status")
    if status is not None and status not in LIFECYCLE_STATUSES:
        errors.append(f"{label}: status must be draft, stable, or deprecated")
    stale_after = metadata.get("stale_after")
    if stale_after is not None and not _is_date(stale_after):
        errors.append(f"{label}: stale_after is not an ISO 8601 date")
    sources = metadata.get("sources")
    if sources is not None:
        if not isinstance(sources, list):
            errors.append(f"{label}: sources must be a list")
        else:
            for index, source in enumerate(sources):
                if not isinstance(source, dict):
                    errors.append(f"{label}: sources[{index}] must be a mapping")
                    continue
                resource = source.get("resource")
                if not isinstance(resource, str) or not resource.strip():
                    errors.append(f"{label}: sources[{index}].resource is required")
                if "usage_count" in source and (
                    not isinstance(source["usage_count"], int)
                    or isinstance(source["usage_count"], bool)
                    or source["usage_count"] < 0
                ):
                    errors.append(f"{label}: sources[{index}].usage_count is invalid")
                if "last_modified" in source and not _is_date(source["last_modified"]):
                    errors.append(f"{label}: sources[{index}].last_modified is invalid")
    if concept_type == "Attested Computation":
        if not isinstance(metadata.get("runtime"), str) or not metadata["runtime"]:
            errors.append(f"{label}: Attested Computation requires runtime")
    return errors


def validate_okf_v02_bundle(bundle: Path) -> list[str]:
    """Validate the normative Markdown tree while tolerating all extensions."""

    errors: list[str] = []
    for path in sorted(bundle.rglob("*.md")):
        relative = path.relative_to(bundle).as_posix()
        try:
            text = path.read_text(encoding="utf-8")
            metadata, body = parse_frontmatter(text)
        except (OSError, UnicodeDecodeError, FrontmatterError) as exc:
            errors.append(f"{relative}: cannot parse Markdown/frontmatter: {exc}")
            continue
        if path.name in RESERVED_FILENAMES:
            if path.name == "index.md" and path.parent == bundle:
                if metadata != {"okf_version": OKF_VERSION}:
                    errors.append(
                        f"{relative}: root index frontmatter must contain only okf_version {OKF_VERSION}"
                    )
            elif metadata is not None:
                errors.append(f"{relative}: reserved nested file must not contain frontmatter")
            if path.name == "log.md":
                for heading in re.findall(r"^##\s+(.+?)\s*$", body, flags=re.MULTILINE):
                    if not _is_date(heading):
                        errors.append(f"{relative}: log date heading is invalid: {heading}")
            continue
        if metadata is None:
            errors.append(f"{relative}: concept is missing frontmatter")
            continue
        errors.extend(validate_concept_metadata(metadata, label=relative))
    return errors
