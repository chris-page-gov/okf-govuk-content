"""OKF core 0.3 frontmatter helpers for the active GOV.UK projection.

The v0.2 helpers remain available for frozen compatibility fixtures. Core 0.3
permits ancillary Markdown without a non-empty ``type``; those files are not
concepts and are reported separately by the independent source checker.
"""

from __future__ import annotations

import re
from pathlib import Path

from .okf_v02 import (
    FrontmatterError,
    GENERATOR_ACTOR,
    RESERVED_FILENAMES,
    concept_metadata,
    parse_frontmatter,
    render_frontmatter,
    validate_concept_metadata,
)

OKF_VERSION = "0.3"


def validate_okf_v03_bundle(bundle: Path) -> list[str]:
    errors: list[str] = []
    for path in sorted(bundle.rglob("*.md")):
        relative = path.relative_to(bundle).as_posix()
        try:
            metadata, body = parse_frontmatter(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, FrontmatterError) as exc:
            errors.append(f"{relative}: cannot parse Markdown/frontmatter: {exc}")
            continue
        if path.name in RESERVED_FILENAMES:
            if path.name == "index.md" and path.parent == bundle:
                if metadata != {"okf_version": OKF_VERSION}:
                    errors.append(f"{relative}: root index must declare only okf_version {OKF_VERSION}")
            elif metadata is not None:
                errors.append(f"{relative}: reserved nested file must not contain frontmatter")
            if path.name == "log.md":
                for heading in re.findall(r"^##\s+(.+?)\s*$", body, flags=re.MULTILINE):
                    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", heading):
                        errors.append(f"{relative}: log date heading is invalid: {heading}")
            continue
        if metadata is None or not isinstance(metadata.get("type"), str) or not metadata["type"].strip():
            # Ancillary Markdown is allowed and is not an OKF core concept.
            continue
        errors.extend(validate_concept_metadata(metadata, label=relative))
    return errors
