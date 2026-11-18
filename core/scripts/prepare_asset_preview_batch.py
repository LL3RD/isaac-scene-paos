#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict, deque
from pathlib import Path
from typing import Any


SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from isaac_scene.semantic_catalog import search  # noqa: E402


def balanced_select(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    by_source: dict[str, deque[dict[str, Any]]] = defaultdict(deque)
    for row in rows:
        by_source[str(row.get("source_library") or "unknown")].append(row)
    selected: list[dict[str, Any]] = []
    sources = sorted(by_source)
    while len(selected) < limit and sources:
        active = []
        for source in sources:
            if by_source[source]:
                selected.append(by_source[source].popleft())
                if len(selected) >= limit:
                    break
            if by_source[source]:
                active.append(source)
        sources = active
    return selected


def existing_paths(visual_index: Path | None) -> set[str]:
    if not visual_index or not visual_index.is_file():
        return set()
    connection = sqlite3.connect(f"file:{visual_index}?mode=ro", uri=True)
    paths = {str(row[0]) for row in connection.execute("SELECT source_path FROM visual_assets")}
    connection.close()
    return paths


def merge_unique(*groups: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for group in groups:
        for row in group:
            logical_id = str(row.get("logical_id") or "")
            if not logical_id or logical_id in seen:
                continue
            seen.add(logical_id)
            merged.append(row)
            if len(merged) >= limit:
                return merged
    return merged


def recall_class(
    catalog: Path,
    query: str,
    canonical_class: str,
    recall: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    query_rows = search(
        catalog,
        query,
        topk=recall,
        canonical_class=canonical_class,
        callable_only=True,
    )
    class_rows: list[dict[str, Any]] = []
    if len(query_rows) < recall:
        class_query = canonical_class.rsplit("/", 1)[-1].replace("_", " ")
        class_rows = search(
            catalog,
            class_query,
            topk=recall,
            canonical_class=canonical_class,
            callable_only=True,
        )
    return query_rows, class_rows


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a source-balanced semantic candidate list for preview rendering.")
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--query", required=True)
    parser.add_argument("--canonical-class", default=None)
    parser.add_argument(
        "--fallback-class",
        action="append",
        default=[],
        help=(
            "Explicit broader semantic class to search after the preferred class. "
            "Repeat for multiple fallbacks; visual QA must still promote any match."
        ),
    )
    parser.add_argument("--visual-index", type=Path, default=None)
    parser.add_argument("--recall", type=int, default=200)
    parser.add_argument("--limit", type=int, default=64)
    parser.add_argument("--include-source", action="append", default=[])
    parser.add_argument("--exclude-source", action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    requested_recall = max(1, min(args.recall, 200))
    if args.canonical_class:
        query_rows, class_rows = recall_class(
            args.catalog,
            args.query,
            args.canonical_class,
            requested_recall,
        )
    else:
        query_rows = search(
            args.catalog,
            args.query,
            topk=requested_recall,
            callable_only=True,
        )
        class_rows = []
    # A style-heavy phrase such as "tall indoor palm in black planter" can
    # legitimately have no lexical overlap with assets named only "plant".
    # Backfill from the canonical semantic leaf so visual ranking still sees
    # the full object class instead of a handful of path-token false positives.
    fallback_groups: list[list[dict[str, Any]]] = []
    fallback_counts: dict[str, dict[str, int]] = {}
    for fallback_class in dict.fromkeys(str(value) for value in args.fallback_class):
        fallback_query_rows, fallback_class_rows = recall_class(
            args.catalog,
            args.query,
            fallback_class,
            requested_recall,
        )
        fallback_groups.extend((fallback_query_rows, fallback_class_rows))
        fallback_counts[fallback_class] = {
            "query_count": len(fallback_query_rows),
            "class_backfill_count": len(fallback_class_rows),
        }
    rows = merge_unique(
        query_rows,
        class_rows,
        *fallback_groups,
        limit=requested_recall,
    )
    included_sources = {str(value) for value in args.include_source}
    excluded_sources = {str(value) for value in args.exclude_source}
    if included_sources:
        rows = [row for row in rows if str(row.get("source_library")) in included_sources]
    if excluded_sources:
        rows = [row for row in rows if str(row.get("source_library")) not in excluded_sources]
    seen = existing_paths(args.visual_index)
    candidates = [
        row for row in rows
        if row.get("path") and str(row["path"]) not in seen
    ]
    selected = balanced_select(candidates, max(1, args.limit))
    payload = {
        "schema_version": "isaac-asset-preview-batch/v1",
        "query": args.query,
        "canonical_class": args.canonical_class,
        "fallback_classes": list(dict.fromkeys(str(value) for value in args.fallback_class)),
        "fallback_recall": fallback_counts,
        "include_sources": sorted(included_sources),
        "exclude_sources": sorted(excluded_sources),
        "query_recall_count": len(query_rows),
        "class_backfill_count": len(class_rows),
        "semantic_recall_count": len(rows),
        "excluded_existing_preview_count": len(rows) - len(candidates),
        "selected_count": len(selected),
        "assets": [
            {
                "asset_id": row["logical_id"],
                "path": row["path"],
                "semantic_name": row["semantic_name"],
                "category": row["canonical_class"],
                "source_library": row["source_library"],
            }
            for row in selected
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "output": str(args.output),
        "semantic_recall_count": len(rows),
        "selected_count": len(selected),
        "sources": sorted({row["source_library"] for row in selected}),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
