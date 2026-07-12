#!/usr/bin/env python3
"""Build the non-compensatory, evidence-bound AF/HF aim scorecard.

The source decision table is human-maintained. Observations, hashes, statuses,
negative findings, Gate 11 and both output projections are deterministic.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator


DEFAULT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_RELATIVE = Path("governance/aim-assessment-source.json")
SCHEMA_RELATIVE = Path("governance/aim-assessment.schema.json")
REQUIREMENTS_RELATIVE = Path("governance/requirements.yaml")
TRACEABILITY_RELATIVE = Path("governance/traceability.json")
MANIFEST_RELATIVE = Path("release/manifest.yaml")
STATUS_RELATIVE = Path("release/status.json")
JSON_OUTPUT_RELATIVE = Path("release/aim-assessment.json")
MARKDOWN_OUTPUT_RELATIVE = Path("reports/aim-scorecard.md")
STATUSES = {"fulfilled", "partly_fulfilled", "not_fulfilled", "not_yet_testable"}
CONFIDENCE = {"low", "medium", "high"}
SHA256 = re.compile(r"^[0-9a-f]{64}$")


class AimAssessmentError(ValueError):
    """Raised for an unsafe or invalid assessment contract/evidence packet."""


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AimAssessmentError(f"missing {label}: {path}") from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AimAssessmentError(f"invalid {label}: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise AimAssessmentError(f"{label} must be a JSON object: {path}")
    return value


def resolve_relative(root: Path, value: object, label: str) -> tuple[Path, str]:
    if not isinstance(value, str) or not value.strip():
        raise AimAssessmentError(f"{label} must be a non-empty repository-relative path")
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise AimAssessmentError(f"unsafe {label}: {value}")
    root = root.resolve()
    path = (root / relative).resolve()
    if path != root and root not in path.parents:
        raise AimAssessmentError(f"{label} escapes the repository: {value}")
    return path, relative.as_posix()


def json_pointer(value: object, pointer: str) -> object:
    if pointer == "":
        return value
    if not pointer.startswith("/"):
        raise AimAssessmentError(f"invalid JSON Pointer: {pointer}")
    current = value
    for raw in pointer[1:].split("/"):
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict):
            if token not in current:
                raise KeyError(token)
            current = current[token]
        elif isinstance(current, list):
            if not token.isdigit() or int(token) >= len(current):
                raise KeyError(token)
            current = current[int(token)]
        else:
            raise KeyError(token)
    return current


def evaluate_check(root: Path, check_id: str, contract: dict[str, Any]) -> dict[str, Any]:
    path, relative = resolve_relative(root, contract.get("path"), f"{check_id}.path")
    operator = contract.get("operator")
    supported = {
        "path_exists",
        "json_equals",
        "json_in",
        "json_string_contains",
        "json_array_nonempty",
        "referenced_path_exists",
    }
    if operator not in supported:
        raise AimAssessmentError(f"unsupported evidence operator for {check_id}: {operator}")
    row: dict[str, Any] = {
        "check_id": check_id,
        "description": contract.get("description"),
        "path": relative,
        "sha256": sha256_file(path) if path.is_file() else None,
        "operator": operator,
        "matched": False,
    }
    if "pointer" in contract:
        row["pointer"] = contract["pointer"]
    if "expected" in contract:
        row["expected"] = contract["expected"]
    if operator == "path_exists":
        row["observed"] = path.is_file()
        row["matched"] = path.is_file()
        if not path.is_file():
            row["error"] = "missing evidence file"
        return row
    if not path.is_file():
        row["observed"] = None
        row["error"] = "missing evidence file"
        return row
    try:
        document = load_json(path, f"evidence {check_id}")
        observed = json_pointer(document, str(contract.get("pointer", "")))
    except (AimAssessmentError, KeyError) as exc:
        row["observed"] = None
        row["error"] = f"evidence locator failed: {exc}"
        return row
    row["observed"] = observed
    expected = contract.get("expected")
    if operator == "json_equals":
        row["matched"] = observed == expected and type(observed) is type(expected)
    elif operator == "json_in":
        if not isinstance(expected, list):
            raise AimAssessmentError(f"{check_id}.expected must be a list for json_in")
        row["matched"] = any(observed == item and type(observed) is type(item) for item in expected)
    elif operator == "json_string_contains":
        if not isinstance(observed, str) or not isinstance(expected, str):
            row["matched"] = False
        else:
            row["matched"] = expected in observed
    elif operator == "json_array_nonempty":
        row["matched"] = isinstance(observed, list) and bool(observed)
    elif operator == "referenced_path_exists":
        if not isinstance(observed, str) or not observed.strip():
            row["matched"] = False
        else:
            try:
                resolved, resolved_relative = resolve_relative(root, observed, f"{check_id} referenced path")
            except AimAssessmentError as exc:
                row["error"] = str(exc)
            else:
                row["resolved_path"] = resolved_relative
                row["resolved_sha256"] = sha256_file(resolved) if resolved.is_file() else None
                row["matched"] = resolved.is_file()
                if not resolved.is_file():
                    row["error"] = "referenced evidence file is missing"
    return row


def validate_source(
    source: dict[str, Any], requirements: dict[str, Any], traceability: dict[str, Any]
) -> None:
    if source.get("schema") != "afhf-govuk-okf-aim-assessment-source.v1":
        raise AimAssessmentError("aim assessment source schema is invalid")
    if set(source.get("status_vocabulary", [])) != STATUSES:
        raise AimAssessmentError("aim assessment source status vocabulary is incomplete")
    if set(source.get("confidence_vocabulary", [])) != CONFIDENCE:
        raise AimAssessmentError("aim assessment source confidence vocabulary is incomplete")
    checks = source.get("evidence_checks")
    aims = source.get("aims")
    if not isinstance(checks, dict) or not checks:
        raise AimAssessmentError("aim assessment source must define evidence checks")
    if not isinstance(aims, list) or len(aims) != 9:
        raise AimAssessmentError("aim assessment source must contain exactly nine original aims")
    requirement_ids = {row["id"] for row in requirements.get("requirements", [])}
    clause_ids = {row["clause_id"] for row in traceability.get("clauses", [])}
    aim_ids: set[str] = set()
    mapped_requirements: set[str] = set()
    mapped_clauses: set[str] = set()
    for aim in aims:
        aim_id = aim.get("aim_id")
        if not isinstance(aim_id, str) or not re.fullmatch(r"AIM-\d{3}", aim_id):
            raise AimAssessmentError(f"invalid aim identifier: {aim_id}")
        if aim_id in aim_ids:
            raise AimAssessmentError(f"duplicate aim identifier: {aim_id}")
        aim_ids.add(aim_id)
        aim_requirements = set(aim.get("requirement_ids", []))
        aim_clauses = set(aim.get("brief_clause_ids", []))
        unknown_requirements = aim_requirements - requirement_ids
        unknown_clauses = aim_clauses - clause_ids
        if unknown_requirements or unknown_clauses:
            raise AimAssessmentError(
                f"{aim_id} has unknown mappings: requirements={sorted(unknown_requirements)}, "
                f"clauses={sorted(unknown_clauses)}"
            )
        mapped_requirements.update(aim_requirements)
        mapped_clauses.update(aim_clauses)
        evidence_ids = aim.get("evidence_check_ids")
        if not isinstance(evidence_ids, list) or not evidence_ids:
            raise AimAssessmentError(f"{aim_id} must have evidence checks")
        referenced = set(evidence_ids)
        decision = aim.get("decision")
        if not isinstance(decision, dict):
            raise AimAssessmentError(f"{aim_id} decision is missing")
        for key in (
            "not_yet_testable_if_any",
            "not_fulfilled_if_any",
            "fulfilled_if_all",
            "partly_fulfilled_if_any",
        ):
            values = decision.get(key)
            if not isinstance(values, list):
                raise AimAssessmentError(f"{aim_id}.{key} must be a list")
            referenced.update(values)
        referenced.update(aim.get("negative_check_ids", []))
        referenced.update(row.get("unless") for row in aim.get("next_actions", []))
        referenced.update(row.get("unless") for row in aim.get("exceptions", []))
        unknown_checks = {item for item in referenced if item not in checks}
        if unknown_checks:
            raise AimAssessmentError(f"{aim_id} references unknown evidence checks: {sorted(unknown_checks)}")
        missing_observations = referenced - set(evidence_ids)
        if missing_observations:
            raise AimAssessmentError(
                f"{aim_id} decision checks must be included in evidence_check_ids: {sorted(missing_observations)}"
            )
        confidence = aim.get("confidence")
        if not isinstance(confidence, dict) or set(confidence) != STATUSES:
            raise AimAssessmentError(f"{aim_id} must define confidence for every status")
        if set(confidence.values()) - CONFIDENCE:
            raise AimAssessmentError(f"{aim_id} uses an invalid confidence level")
        if not isinstance(aim.get("standing_negative_finding"), str) or not aim["standing_negative_finding"].strip():
            raise AimAssessmentError(f"{aim_id} must retain a standing negative finding or limitation")
    if mapped_requirements != requirement_ids:
        raise AimAssessmentError(
            f"aim mappings do not cover every requirement: missing={sorted(requirement_ids - mapped_requirements)}"
        )
    if mapped_clauses != clause_ids:
        raise AimAssessmentError(
            f"aim mappings do not cover every controlling clause: missing={sorted(clause_ids - mapped_clauses)}"
        )
    gate_checks = source.get("gate_11_required_checks")
    if not isinstance(gate_checks, list) or not gate_checks:
        raise AimAssessmentError("Gate 11 must define required final-snapshot checks")
    unknown_gate_checks = set(gate_checks) - set(checks)
    if unknown_gate_checks:
        raise AimAssessmentError(f"Gate 11 references unknown checks: {sorted(unknown_gate_checks)}")


def choose_status(aim: dict[str, Any], matches: dict[str, bool]) -> str:
    decision = aim["decision"]
    if any(matches[item] for item in decision["not_yet_testable_if_any"]):
        return "not_yet_testable"
    if any(matches[item] for item in decision["not_fulfilled_if_any"]):
        return "not_fulfilled"
    fulfilled = decision["fulfilled_if_all"]
    if fulfilled and all(matches[item] for item in fulfilled):
        return "fulfilled"
    if any(matches[item] for item in decision["partly_fulfilled_if_any"]):
        return "partly_fulfilled"
    return "not_yet_testable"


def exception_id_exists(path: Path, exception_id: str) -> bool:
    if not path.is_file():
        return False
    text = path.read_text(encoding="utf-8")
    return re.search(rf"(?m)^\s*-\s+id:\s*{re.escape(exception_id)}\s*$", text) is not None


def build_assessment(root: Path) -> dict[str, Any]:
    root = root.resolve()
    source_path = root / SOURCE_RELATIVE
    requirements_path = root / REQUIREMENTS_RELATIVE
    traceability_path = root / TRACEABILITY_RELATIVE
    manifest_path = root / MANIFEST_RELATIVE
    status_path = root / STATUS_RELATIVE
    source = load_json(source_path, "aim assessment source")
    requirements = load_json(requirements_path, "requirements contract")
    traceability = load_json(traceability_path, "traceability contract")
    manifest = load_json(manifest_path, "release manifest")
    release_status = load_json(status_path, "release status")
    validate_source(source, requirements, traceability)

    check_rows = {
        check_id: evaluate_check(root, check_id, contract)
        for check_id, contract in sorted(source["evidence_checks"].items())
    }
    matches = {check_id: bool(row["matched"]) for check_id, row in check_rows.items()}
    aims: list[dict[str, Any]] = []
    for aim in source["aims"]:
        status = choose_status(aim, matches)
        evidence = [check_rows[check_id] for check_id in aim["evidence_check_ids"]]
        support = [row for row in evidence if row["matched"]]
        negative_findings: list[dict[str, Any]] = [
            {
                "finding_id": f"{aim['aim_id']}-NEG-BOUNDARY",
                "text": aim["standing_negative_finding"],
                "evidence_check_id": None,
            }
        ]
        explicit_failures = set(aim["decision"]["not_fulfilled_if_any"])
        for check_id in aim["negative_check_ids"]:
            row = check_rows[check_id]
            if not row["matched"]:
                negative_findings.append(
                    {
                        "finding_id": f"{aim['aim_id']}-NEG-{len(negative_findings):02d}",
                        "text": source["evidence_checks"][check_id]["negative_finding"],
                        "evidence_check_id": check_id,
                    }
                )
        for check_id in sorted(explicit_failures):
            if matches[check_id]:
                negative_findings.append(
                    {
                        "finding_id": f"{aim['aim_id']}-NEG-{len(negative_findings):02d}",
                        "text": source["evidence_checks"][check_id]["negative_finding"],
                        "evidence_check_id": check_id,
                    }
                )
        exceptions: list[dict[str, Any]] = []
        for exception in aim["exceptions"]:
            if matches[exception["unless"]]:
                continue
            exception_path, relative = resolve_relative(root, exception["path"], "exception path")
            if not exception_id_exists(exception_path, exception["id"]):
                raise AimAssessmentError(f"exception {exception['id']} is not present in {relative}")
            exceptions.append(
                {
                    "exception_id": exception["id"],
                    "path": relative,
                    "sha256": sha256_file(exception_path),
                }
            )
        next_actions = [
            row["action"] for row in aim["next_actions"] if not matches[row["unless"]]
        ]
        aims.append(
            {
                "aim_id": aim["aim_id"],
                "title": aim["title"],
                "status": status,
                "confidence": {
                    "level": aim["confidence"][status],
                    "basis": "Deterministic observations from the exact hashed evidence rows below; impact confidence remains bounded by the declared evidence tier.",
                },
                "brief_clause_ids": aim["brief_clause_ids"],
                "requirement_ids": aim["requirement_ids"],
                "research_question_ids": aim["research_question_ids"],
                "hypothesis_ids": aim["hypothesis_ids"],
                "interpretation": aim["interpretation"],
                "boundary": aim["boundary"],
                "strongest_supporting_evidence": [row["check_id"] for row in support[:5]],
                "evidence": evidence,
                "negative_findings": negative_findings,
                "exceptions": exceptions,
                "next_actions": next_actions,
            }
        )

    counts = Counter(row["status"] for row in aims)
    status_counts = {status: counts.get(status, 0) for status in sorted(STATUSES)}
    gate_rows = [check_rows[check_id] for check_id in source["gate_11_required_checks"]]
    unmet = [row["check_id"] for row in gate_rows if not row["matched"]]
    gate_passed = not unmet and len(aims) == 9 and all(
        row["negative_findings"] and row["confidence"]["level"] in CONFIDENCE for row in aims
    )
    snapshot = manifest.get("snapshot") if isinstance(manifest.get("snapshot"), dict) else {}
    if snapshot.get("kind") == "full_corpus" and snapshot.get("sampled") is False:
        assessment_tier = (
            "full_programme" if release_status.get("human_evaluation_status") == "completed" else "machine_release_candidate"
        )
    else:
        assessment_tier = "fixture_checkpoint"
    result = {
        "schema": "afhf-govuk-okf-aim-assessment.v1",
        "assessment_id": f"aim-assessment-{manifest.get('release_id', 'unknown')}",
        "assessment_date": source["assessment_date"],
        "assessment_tier": assessment_tier,
        "snapshot": {
            "release_id": manifest.get("release_id"),
            "kind": snapshot.get("kind"),
            "sampled": snapshot.get("sampled"),
        },
        "method": {
            "kind": "deterministic_non_compensatory_evidence_decision_table",
            "status_vocabulary": source["status_vocabulary"],
            "veto_rule": "A diagnostic or supporting artefact cannot compensate for a failed required final-snapshot check.",
            "human_rule": "Synthetic, automated, expert-only and agent evidence cannot satisfy the preferred-human-UI aim.",
            "unfavourable_results_permitted": True,
        },
        "inputs": {
            "assessment_source": {
                "path": SOURCE_RELATIVE.as_posix(),
                "sha256": sha256_file(source_path),
            },
            "requirements": {
                "path": REQUIREMENTS_RELATIVE.as_posix(),
                "sha256": sha256_file(requirements_path),
            },
            "traceability": {
                "path": TRACEABILITY_RELATIVE.as_posix(),
                "sha256": sha256_file(traceability_path),
            },
            "release_manifest": {
                "path": MANIFEST_RELATIVE.as_posix(),
                "sha256": sha256_file(manifest_path),
            },
            "release_status": {
                "path": STATUS_RELATIVE.as_posix(),
                "sha256": sha256_file(status_path),
            },
        },
        "coverage": {
            "aims": len(aims),
            "requirements": len({item for aim in aims for item in aim["requirement_ids"]}),
            "controlling_clauses": len({item for aim in aims for item in aim["brief_clause_ids"]}),
        },
        "counts": {
            "aims": len(aims),
            "by_status": status_counts,
        },
        "gate_11": {
            "passed": gate_passed,
            "status": "passed" if gate_passed else "pending",
            "required_check_ids": source["gate_11_required_checks"],
            "unmet_check_ids": unmet,
            "evidence": gate_rows,
            "qualification": (
                "Every original aim has an evidenced disposition against the closing full-snapshot machine evidence. Human-dependent aims may remain not_yet_testable."
                if gate_passed
                else "Gate 11 remains fail-closed because the assessment is not yet bound to every required closing full-snapshot evidence result."
            ),
        },
        "aims": aims,
    }
    return result


def escape_table(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def render_markdown(document: dict[str, Any]) -> str:
    snapshot = document["snapshot"]
    lines = [
        "# AF/HF GOV.UK OKF aim scorecard",
        "",
        f"- Assessment: `{document['assessment_id']}`",
        f"- Snapshot: `{snapshot['release_id']}` (`{snapshot['kind']}`, sampled: `{str(snapshot['sampled']).lower()}`)",
        f"- Assessment tier: `{document['assessment_tier']}`",
        f"Acceptance Gate 11: `{'passed' if document['gate_11']['passed'] else 'pending'}`",
        "",
        "This is a non-compensatory assessment. A fixture or design artefact can support partial fulfilment, but it cannot substitute for closing full-corpus, semantic, evaluation, citation, rights or reproduction evidence. An unfavourable result is valid. Human preference remains untestable without genuine participant evidence.",
        "",
        "## Summary",
        "",
        "| Aim | Status | Confidence | Strongest current evidence |",
        "|---|---|---|---|",
    ]
    for aim in document["aims"]:
        support = ", ".join(f"`{item}`" for item in aim["strongest_supporting_evidence"]) or "None"
        lines.append(
            f"| {escape_table(aim['aim_id'] + ' ' + aim['title'])} | `{aim['status']}` | "
            f"`{aim['confidence']['level']}` | {support} |"
        )
    lines.extend(
        [
            "",
            "## Gate 11",
            "",
            document["gate_11"]["qualification"],
            "",
        ]
    )
    if document["gate_11"]["unmet_check_ids"]:
        lines.append(
            "Unmet final-snapshot checks: "
            + ", ".join(f"`{item}`" for item in document["gate_11"]["unmet_check_ids"])
            + "."
        )
        lines.append("")
    for aim in document["aims"]:
        lines.extend(
            [
                f"## {aim['aim_id']} — {aim['title']}",
                "",
                f"Status: `{aim['status']}`. Confidence: `{aim['confidence']['level']}`.",
                "",
                aim["interpretation"],
                "",
                f"Boundary: {aim['boundary']}",
                "",
                "Evidence:",
                "",
            ]
        )
        for evidence in aim["evidence"]:
            observed = json.dumps(evidence.get("observed"), ensure_ascii=False, sort_keys=True)
            digest = evidence.get("resolved_sha256") or evidence.get("sha256") or "missing"
            locator = f"{evidence['path']}{evidence.get('pointer', '')}"
            result = "pass" if evidence["matched"] else "not met"
            lines.append(f"- `{evidence['check_id']}` — {result}; `{locator}`; SHA-256 `{digest}`; observed `{observed}`.")
        lines.extend(["", "Negative findings and limitations:", ""])
        for finding in aim["negative_findings"]:
            lines.append(f"- {finding['text']}")
        if aim["exceptions"]:
            lines.extend(["", "Applicable exceptions:", ""])
            for exception in aim["exceptions"]:
                lines.append(
                    f"- `{exception['exception_id']}` — `{exception['path']}`; SHA-256 `{exception['sha256']}`."
                )
        if aim["next_actions"]:
            lines.extend(["", "Next actions:", ""])
            for action in aim["next_actions"]:
                lines.append(f"- {action}")
        lines.append("")
    lines.extend(
        [
            "## Machine-readable evidence",
            "",
            "The canonical machine-readable projection is `release/aim-assessment.json`. Every evidence row records the repository-relative path, exact SHA-256, locator, observed value, expected value and deterministic match result. This Markdown file is generated from that same object.",
            "",
        ]
    )
    return "\n".join(lines)


def validate_output(root: Path, document: dict[str, Any]) -> None:
    schema = load_json(root / SCHEMA_RELATIVE, "aim assessment schema")
    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(document), key=lambda item: list(item.absolute_path))
    if errors:
        rendered = "; ".join(
            f"/{'/'.join(str(part) for part in error.absolute_path)}: {error.message}" for error in errors
        )
        raise AimAssessmentError(f"generated aim assessment does not match its schema: {rendered}")


def render(root: Path) -> dict[Path, str]:
    document = build_assessment(root)
    validate_output(root, document)
    return {
        root / JSON_OUTPUT_RELATIVE: canonical_json(document),
        root / MARKDOWN_OUTPUT_RELATIVE: render_markdown(document),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if checked-in scorecard projections differ")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    try:
        outputs = render(root)
        errors: list[str] = []
        for path, expected in outputs.items():
            if args.check:
                if not path.is_file() or path.read_text(encoding="utf-8") != expected:
                    errors.append(f"{path.relative_to(root)} is missing or stale")
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(expected, encoding="utf-8")
        if errors:
            raise AimAssessmentError("; ".join(errors))
        document = build_assessment(root)
    except (AimAssessmentError, OSError, TypeError, KeyError) as exc:
        print(f"aim assessment failed: {exc}", file=sys.stderr)
        return 1
    counts = document["counts"]["by_status"]
    action = "validated" if args.check else "wrote"
    print(
        f"{action} nine-aim scorecard: "
        + ", ".join(f"{status}={counts[status]}" for status in sorted(STATUSES))
        + f"; Gate 11={document['gate_11']['status']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
