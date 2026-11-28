"""Generate a source-checked visual walkthrough for the bounded demonstrator."""

from __future__ import annotations

import hashlib
import html
import json
from pathlib import Path
from typing import Any, Sequence

from .util import pretty_json

ROOT = Path(__file__).resolve().parents[2]
CASE_PATH = ROOT / "questions" / "new-child-metadata-walkthrough.json"
READER_BASE = "https://chris-page-gov.github.io/okf-explorer/"
DESCRIPTOR_URL = "https://chris-page-gov.github.io/okf-govuk-content/okf-explorer.json"


class WalkthroughError(ValueError):
    """Raised when source fields no longer support the authored case."""


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def route_path(route: str) -> str:
    return "concepts/" + route + ".md"


def _wrap_svg_text(value: str, width: int) -> list[str]:
    lines: list[str] = []
    current = ""
    for word in value.split():
        candidate = f"{current} {word}" if current else word
        if current and len(candidate) > width:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines or [""]


def _link(title: str, route: str) -> str:
    return f'<a href="../{html.escape(route_path(route), quote=True)}">{html.escape(title)}</a>'


def _svg(title: str, description: str, cards: Sequence[tuple[str, str, str]], mobile: bool) -> str:
    if mobile:
        width, height = 420, 960
        points = [(22, 24 + index * 158, 376, 126) for index in range(len(cards))]
        edge = ('<path d="M210 120 H8 V202 H22 M210 120 H412 V360 H398 '
                'M210 120 H8 V518 H22 M210 120 H412 V676 H398" class="edge" />')
    else:
        width, height = 1200, 720
        points = [(44, 160, 330, 120), (510, 38, 640, 82), (510, 146, 640, 82),
                  (510, 254, 640, 82), (510, 362, 640, 82), (44, 492, 330, 104)]
        edge = ('<path d="M374 220 C430 220 440 79 510 79 M374 220 C440 220 440 187 510 187 '
                'M374 220 H510 V295 M374 220 C440 220 440 403 510 403" class="edge" />')
        edge += '<path d="M210 280 V492" class="negative-edge" />'
    boxes = []
    for index, ((heading, detail, role), (x, y, w, h)) in enumerate(zip(cards, points)):
        style = "negative" if role == "negative" else "primary" if index == 0 else "linked"
        if mobile:
            heading_lines = _wrap_svg_text(heading, 36)
            detail_lines = _wrap_svg_text(detail, 42)
            heading_text = "".join(
                f'<tspan x="{x + 16}" y="{y + 28 + line * 18}">{html.escape(text)}</tspan>'
                for line, text in enumerate(heading_lines)
            )
            detail_start = y + 28 + len(heading_lines) * 18 + 10
            detail_text = "".join(
                f'<tspan x="{x + 16}" y="{detail_start + line * 16}">{html.escape(text)}</tspan>'
                for line, text in enumerate(detail_lines)
            )
            boxes.append(
                f'<g class="{style}"><rect x="{x}" y="{y}" width="{w}" height="{h}" rx="12" />'
                f'<text class="heading">{heading_text}</text><text class="detail">{detail_text}</text></g>'
            )
            continue
        boxes.append(
            f'<g class="{style}"><rect x="{x}" y="{y}" width="{w}" height="{h}" rx="12" />'
            f'<text x="{x + 16}" y="{y + 29}" class="heading">{html.escape(heading)}</text>'
            f'<text x="{x + 16}" y="{y + 56}" class="detail">{html.escape(detail)}</text></g>'
        )
    if mobile:
        diagram = "".join(boxes)
    else:
        # The source concept occupies the left lane; related and publisher rows
        # occupy the right lane. Negative control is a separate lower lane.
        boxes = []
        for index, ((heading, detail, role), (x, y, w, h)) in enumerate(zip(cards, points)):
            style = "negative" if role == "negative" else "primary" if index == 0 else "linked"
            boxes.append(
                f'<g class="{style}"><rect x="{x}" y="{y}" width="{w}" height="{h}" rx="12" />'
                f'<text x="{x + 16}" y="{y + 29}" class="heading">{html.escape(heading)}</text>'
                f'<text x="{x + 16}" y="{y + 56}" class="detail">{html.escape(detail)}</text></g>'
            )
        diagram = edge + "".join(boxes)
    svg_class = ' class="mobile"' if mobile else ""
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" role="img"{svg_class} '
        f'aria-labelledby="title desc"><title id="title">{html.escape(title)}</title>'
        f'<desc id="desc">{html.escape(description)}</desc><style>'
        'text{font-family:system-ui,sans-serif;fill:#142b3b}.heading{font-size:18px;font-weight:700}'
        '.mobile .heading{font-size:16px}.mobile .detail{font-size:13px}.detail{font-size:14px}.edge{fill:none;stroke:#42677b;stroke-width:3}.negative-edge{fill:none;stroke:#956000;stroke-width:2;stroke-dasharray:7 5}'
        'rect{stroke-width:2}.primary rect{fill:#e4f1fa;stroke:#155d86}.linked rect{fill:#edf4ed;stroke:#356b3d}'
        '.negative rect{fill:#fff2dc;stroke:#956000;stroke-dasharray:7 4}</style>'
        f'<rect width="{width}" height="{height}" fill="#fff" />{diagram}</svg>'
    )


def build_walkthrough(
    output: Path,
    datasets: Sequence[dict[str, Any]],
    publishers: Sequence[dict[str, Any]],
    relationships: Sequence[dict[str, Any]],
    snapshot_id: str,
) -> dict[str, dict[str, str]]:
    case_bytes = CASE_PATH.read_bytes()
    case = json.loads(case_bytes)
    if not isinstance(case, dict) or case.get("schema") != "govuk-okf-source-question.v1":
        raise WalkthroughError("walkthrough case is not the reviewed JSON object")
    dataset_by_route = {str(row["open"]): row for row in datasets}
    publisher_by_route = {str(row["open"]): row for row in publishers}
    primary_route = str(case["primary_route"])
    negative_route = str(case["negative_control_route"])
    primary = dataset_by_route.get(primary_route)
    negative = dataset_by_route.get(negative_route)
    if primary is None or negative is None:
        raise WalkthroughError("walkthrough records are absent from the declared source snapshot")
    if not set(case["primary_memberships"]).issubset(primary.get("source_memberships", [])):
        raise WalkthroughError("primary source memberships differ from the authored walkthrough case")
    if not set(case["negative_control_memberships"]).issubset(negative.get("source_memberships", [])):
        raise WalkthroughError("negative-control memberships differ from the authored walkthrough case")
    if case["primary_memberships"][1] not in primary["source_memberships"]:
        raise WalkthroughError("primary record no longer has the expected browse membership")
    if "mainstream-browse:childcare-parenting/pregnancy-birth" in negative["source_memberships"]:
        raise WalkthroughError("negative control unexpectedly matches pregnancy and birth")
    source_edges = [edge for edge in relationships if edge.get("source") == primary_route]
    expected = {
        ("related to", case["related_route"]),
        ("published by", case["publisher_route"]),
        *(("associated with organisation", route) for route in case["associated_organisation_routes"]),
    }
    observed = {(str(edge.get("kind")), str(edge.get("target"))) for edge in source_edges}
    if not expected.issubset(observed):
        raise WalkthroughError("required source relationships are absent from the published graph")
    related = dataset_by_route[str(case["related_route"])]
    publisher = publisher_by_route[str(case["publisher_route"])]
    organisations = [publisher_by_route[route] for route in case["associated_organisation_routes"]]
    primary_path = output / route_path(primary_route)
    negative_path = output / route_path(negative_route)
    related_path = output / route_path(str(case["related_route"]))
    publisher_path = output / route_path(str(case["publisher_route"]))
    organisation_paths = [output / route_path(route) for route in case["associated_organisation_routes"]]
    if not all(path.is_file() for path in [primary_path, negative_path, related_path, publisher_path, *organisation_paths]):
        raise WalkthroughError("walkthrough source Markdown path is missing")
    primary_hash, negative_hash = sha256(primary_path), sha256(negative_path)
    relation_facts = [
        {
            "assertion_id": edge["assertion_id"],
            "kind": edge["kind"],
            "target": edge["target"],
            "evidence_sha256": edge.get("evidence_sha256"),
            "evidence_url": edge.get("evidence_url"),
            "evidence_locator": edge.get("evidence_locator"),
        }
        for edge in source_edges
        if (edge.get("kind"), edge.get("target")) in expected
    ]
    target_base = (
        READER_BASE
        + "?bundle="
        + __import__("urllib.parse", fromlist=["quote"]).quote(DESCRIPTOR_URL, safe="")
        + "&view=reader"
    )
    reader_link = (
        target_base
        + "&q="
        + __import__("urllib.parse", fromlist=["quote"]).quote(str(primary["title"]), safe="")
        + "#"
        + primary_route
    )
    related_link = (
        target_base
        + "&q="
        + __import__("urllib.parse", fromlist=["quote"]).quote(str(related["title"]), safe="")
        + "#"
        + str(case["related_route"])
    )
    membership_titles = [
        "Financial help for children",
        "Pregnancy and birth",
    ]
    org_names = [str(row.get("title") or row.get("name")) for row in organisations]
    explanation = (
        f"The snapshot lists “{primary['title']}” under {membership_titles[1]} and "
        f"{membership_titles[0]}. It links to “{related['title']}”, shows “{publisher['title']}” "
        f"as publisher, and associates {', '.join('“' + name + '”' for name in org_names)}. "
        "These are frozen metadata observations. They do not establish a family's current eligibility, "
        "payment amount, current GOV.UK wording or present organisational responsibility."
    )
    limits = case["limits"]
    evidence = {
        "schema": "govuk-okf-walkthrough-evidence.v1",
        "case_id": case["id"],
        "snapshot": snapshot_id,
        "question": case["question"],
        "answer_scope": "frozen metadata discovery only; not current entitlement or advice",
        "case_sha256": hashlib.sha256(case_bytes).hexdigest(),
        "reader_link": reader_link,
        "records": {
            "primary": {"route": primary_route, "title": primary["title"], "path": route_path(primary_route), "sha256": primary_hash, "evidence_sha256": primary.get("evidence_sha256"), "source_memberships": primary["source_memberships"], "modified_at": primary.get("public_updated_at"), "retrieved_at": primary.get("retrieved_at")},
            "negative_control": {"route": negative_route, "title": negative["title"], "path": route_path(negative_route), "sha256": negative_hash, "source_memberships": negative["source_memberships"]},
            "related": {"route": case["related_route"], "title": related["title"], "path": route_path(str(case["related_route"])), "sha256": sha256(related_path)},
            "publisher": {"route": case["publisher_route"], "title": publisher["title"], "path": route_path(str(case["publisher_route"])), "sha256": sha256(publisher_path)},
            "associated_organisations": [{"route": route, "title": row["title"], "path": route_path(route), "sha256": sha256(path)} for route, row, path in zip(case["associated_organisation_routes"], organisations, organisation_paths)],
        },
        "assertions": relation_facts,
        "limitations": limits,
    }
    cards = [
        (str(primary["title"]), "both: pregnancy and birth + financial help", "primary"),
        (str(related["title"]), "source assertion: related to", "linked"),
        (str(publisher["title"]), "source assertion: published by", "linked"),
        (org_names[0], "source assertion: associated with organisation", "linked"),
        (org_names[1], "source assertion: associated with organisation", "linked"),
        (str(negative["title"]), "childcare + financial help; not pregnancy and birth", "negative"),
    ]
    diagram_title = "Frozen GOV.UK metadata relationship walkthrough"
    description = "The parental leave record, its source-linked related page and publisher/organisations, with a childcare-only negative control."
    directory = output / "walkthroughs"
    directory.mkdir(parents=True, exist_ok=True)
    desktop = directory / "new-child-metadata-desktop.svg"
    mobile = directory / "new-child-metadata-mobile.svg"
    desktop.write_text(_svg(diagram_title, description, cards, False), encoding="utf-8")
    mobile.write_text(_svg(diagram_title, description, cards, True), encoding="utf-8")
    evidence["diagrams"] = {
        "desktop": {"path": desktop.relative_to(output).as_posix(), "sha256": sha256(desktop)},
        "mobile": {"path": mobile.relative_to(output).as_posix(), "sha256": sha256(mobile)},
    }
    evidence_path = directory / "new-child-metadata-evidence.json"
    evidence_path.write_text(pretty_json(evidence), encoding="utf-8")
    journey_path = directory / "new-child-metadata-explorer-journeys.json"
    journey_manifest = {
        "schema": "okf-explorer-journeys.v1",
        "bundle_descriptor": "okf-explorer.json",
        "expected_identity": {
            "schema": "okf-explorer-large-corpus.v1",
            "id": DESCRIPTOR_URL,
            "version": "0.1.0",
            "snapshot": snapshot_id,
        },
        "journeys": [
            {
                "id": "new-child-related-edge",
                "actions": [
                    {"type": "goto", "hash": "#" + primary_route, "params": {"view": "graph"}},
                    {"type": "wait_for", "selector": f'svg.graph .graph-node[data-route="{case["related_route"]}"]'},
                    {"type": "click", "selector": f'svg.graph .graph-node[data-route="{case["related_route"]}"]'},
                    {
                        "type": "wait_for",
                        "selector": f'aside[aria-label="Record details"] h2:has-text("{case["related_route"].split("/", 1)[1]}")',
                    },
                ],
                "assertions": [
                    {
                        "type": "text",
                        "selector": 'aside[aria-label="Record details"]',
                        "includes": str(case["related_route"].split("/", 1)[1]),
                    },
                    {"type": "url_hash", "equals": "#" + str(case["related_route"])},
                    {"type": "requested", "includes": "/okf-explorer.json"},
                    {"type": "console_clean"},
                ],
            },
            {
                "id": "new-child-source-record-search",
                "actions": [
                    {"type": "goto", "hash": "#" + primary_route, "params": {"view": "reader"}},
                    {"type": "fill", "selector": ".search-input", "value": str(primary["title"])},
                    {
                        "type": "wait_for",
                        "selector": f'.result-open:has(h2:text-is("{primary["title"]}"))',
                    },
                    {"type": "click", "selector": f'.result-open:has(h2:text-is("{primary["title"]}"))'},
                    {
                        "type": "wait_for",
                        "selector": f'aside[aria-label="Record details"] h2:has-text("{primary["title"]}")',
                    },
                ],
                "assertions": [
                    {
                        "type": "text",
                        "selector": 'aside[aria-label="Record details"]',
                        "includes": str(primary["title"]),
                    },
                    {
                        "type": "text",
                        "selector": 'aside[aria-label="Record details"]',
                        "includes": str(publisher["title"]),
                    },
                    {"type": "url_hash", "equals": "#" + primary_route},
                    {"type": "console_clean"},
                ],
            },
        ],
    }
    journey_path.write_text(pretty_json(journey_manifest), encoding="utf-8")
    html_path = directory / "new-child-metadata.html"
    html_text = f'''<!doctype html>
<html lang="en-GB"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="icon" href="data:,">
<title>{html.escape(case['title'])}</title><style>
body{{font:1rem/1.55 system-ui,sans-serif;max-width:70rem;margin:auto;padding:1rem;color:#142b3b}}
main{{display:grid;grid-template-columns:minmax(0,1fr);gap:1rem;overflow-wrap:anywhere}}.answer,.control,.limits{{padding:1rem;border-radius:.6rem}}.answer{{background:#e4f1fa}}.control{{background:#fff2dc}}
img{{max-width:100%;height:auto}}.mobile{{display:none}}.sources li{{margin:.4rem 0}}a{{color:#075b89}}
@media(max-width:40rem){{body{{padding:.75rem}}.desktop{{display:none}}.mobile{{display:block}}}}
</style></head><body><main><header><p>Frozen metadata · {html.escape(snapshot_id)}</p><h1>{html.escape(case['title'])}</h1></header>
<section><h2>Question</h2><p>{html.escape(case['question'])}</p></section>
<section class="answer"><h2>Answer from this snapshot</h2><p>{html.escape(explanation)}</p>
<p>Primary source: {_link(str(primary['title']), primary_route)} · SHA-256 <code>{primary_hash}</code></p>
<p><a href="{html.escape(reader_link, quote=True)}" rel="external">Open this record in OKF Explorer</a> · <a href="{html.escape(related_link, quote=True)}" rel="external">Open the related record</a></p></section>
<figure class="desktop"><img src="new-child-metadata-desktop.svg" alt="{html.escape(description, quote=True)}"><figcaption>Desktop relationship diagram; each edge is listed in the evidence JSON.</figcaption></figure>
<figure class="mobile"><img src="new-child-metadata-mobile.svg" alt="{html.escape(description, quote=True)}"><figcaption>Mobile relationship diagram.</figcaption></figure>
<section class="control"><h2>Negative control</h2><p>{_link(str(negative['title']), negative_route)} appears in childcare and financial-help metadata, but not in the pregnancy-and-birth membership used in the question. It must not be returned as the matching answer. Source Markdown SHA-256 <code>{negative_hash}</code>.</p></section>
<section><h2>Source evidence</h2><ul class="sources"><li>{_link(str(primary['title']), primary_route)} · modified {html.escape(str(primary.get('public_updated_at') or 'not recorded'))} · retrieved {html.escape(str(primary.get('retrieved_at') or 'not recorded'))}</li>
<li>Related: {_link(str(related['title']), str(case['related_route']))}</li><li>Publisher: {_link(str(publisher['title']), str(case['publisher_route']))}</li>
{''.join('<li>Associated organisation: ' + _link(str(row['title']), str(route)) + '</li>' for row, route in zip(organisations, case['associated_organisation_routes']))}</ul>
<p><a href="new-child-metadata-evidence.json">Machine-readable evidence and source digests</a></p></section>
<section class="limits"><h2>Limits</h2><ul>{''.join('<li>' + html.escape(str(limit)) + '</li>' for limit in limits)}</ul></section></main></body></html>'''
    html_path.write_text(html_text, encoding="utf-8")
    return {
        "walkthrough": {"path": html_path.relative_to(output).as_posix(), "sha256": sha256(html_path)},
        "walkthrough_desktop": {"path": desktop.relative_to(output).as_posix(), "sha256": sha256(desktop)},
        "walkthrough_mobile": {"path": mobile.relative_to(output).as_posix(), "sha256": sha256(mobile)},
        "walkthrough_evidence": {"path": evidence_path.relative_to(output).as_posix(), "sha256": sha256(evidence_path)},
        "walkthrough_journeys": {"path": journey_path.relative_to(output).as_posix(), "sha256": sha256(journey_path)},
    }
