#!/usr/bin/env python3
"""Fetch a bounded Search API page into the external extract cache."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from govuk_okf.acquisition import (  # noqa: E402
    SEARCH_FIELDS,
    SEARCH_PAGE_SIZE,
    SEARCH_URL,
    AcquisitionError,
    SnapshotBuilder,
    search_result_record,
)
from govuk_okf.search_extracts import SearchExtractError  # noqa: E402
from govuk_okf.storage import StoragePolicyError  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("label", help="local extract-cache snapshot label")
    parser.add_argument("--document-type", default="guide")
    parser.add_argument("--count", type=int, default=10)
    args = parser.parse_args()
    if args.count < 1 or args.count > SEARCH_PAGE_SIZE:
        parser.error(f"--count must be between 1 and {SEARCH_PAGE_SIZE}")
    builder = None
    try:
        builder = SnapshotBuilder(ROOT, args.label)
        params: list[tuple[str, object]] = [
            ("count", args.count),
            ("filter_content_store_document_type", args.document_type),
        ]
        params.extend(("fields", field) for field in SEARCH_FIELDS)
        url = SEARCH_URL + "?" + urlencode(params)
        payload, evidence = builder.cached_json(
            builder.cache / "extract-probe" / f"{args.document_type}.json.gz",
            url,
            builder.search_limiter,
        )
        rows = payload.get("results")
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise AcquisitionError("Search extract probe returned an invalid result list")
        if builder.extract_store is None:
            raise SearchExtractError("Search extract probe requires the external extract store")
        safe_rows = builder.extract_store.ingest_results(rows, evidence)
        public_records = [
            search_result_record(row, "search-extract-probe", evidence, index)
            for index, row in enumerate(safe_rows)
        ]
        result = {
            "schema": "govuk-okf-search-extract-probe.v1",
            "document_type": args.document_type,
            "returned": len(public_records),
            "public_record_bodies_retained": False,
            "records": [
                {
                    "content_id": record.get("content_id"),
                    "canonical_url": record.get("canonical_url"),
                    "title": record.get("title"),
                    "parts": len(record.get("parts") or []),
                }
                for record in public_records
            ],
            "extract_cache": builder.extract_store.summary(),
        }
    except (
        AcquisitionError,
        SearchExtractError,
        StoragePolicyError,
        OSError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        print(f"Search extract probe failed closed: {exc}", file=sys.stderr)
        return 1
    finally:
        if builder is not None:
            builder.close()
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
