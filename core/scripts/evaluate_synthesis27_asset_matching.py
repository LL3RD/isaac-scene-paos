#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


def _normalize(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.lower().replace("_", " ")))


def _tokens(value: str) -> set[str]:
    return set(_normalize(value).split())


def _lexical_score(query: str, candidate: str) -> float:
    first = _tokens(query)
    second = _tokens(candidate)
    if not first or not second:
        return 0.0
    return len(first & second) / len(first | second)


def _candidate_coverage(preview_count: int, inventory_count: int) -> bool:
    if preview_count >= 20:
        return True
    return inventory_count == preview_count and preview_count >= 10


def _read_assets(path: Path) -> list[dict[str, Any]]:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    rows = [dict(row) for row in connection.execute(
        """SELECT model_id, logical_id, semantic_name, canonical_class, subtype,
                  business_type, asset_scopes_json, usage_count, scene_count,
                  scene_slugs_json, callable, availability, acquisition_priority
           FROM assets
           ORDER BY model_id"""
    )]
    connection.close()
    return rows


def _read_candidates(path: Path) -> list[dict[str, Any]]:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    rows = [dict(row) for row in connection.execute(
        """SELECT logical_id, semantic_name, canonical_class, subtype,
                  source_library, source_path, callable, availability,
                  preview_index_path, bbox_json, front_axis, meters_per_unit
           FROM semantic_assets
           WHERE callable=1 AND source_library!='synthesis27'
           ORDER BY logical_id"""
    )]
    connection.close()
    return rows


def _compact_candidate(row: dict[str, Any], query: str) -> dict[str, Any]:
    return {
        "logical_id": row["logical_id"],
        "semantic_name": row["semantic_name"],
        "canonical_class": row["canonical_class"],
        "subtype": row["subtype"],
        "source_library": row["source_library"],
        "source_path": row["source_path"],
        "preview_ready": bool(row["preview_index_path"] and row["bbox_json"]),
        "front_axis_ready": bool(row["front_axis"]),
        "lexical_score": round(_lexical_score(query, row["semantic_name"]), 6),
    }


def evaluate(
    synthesis_library: Path,
    semantic_catalog: Path,
) -> dict[str, Any]:
    source_assets = _read_assets(synthesis_library)
    candidates = _read_candidates(semantic_catalog)
    by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_class: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for candidate in candidates:
        by_name[_normalize(candidate["semantic_name"])].append(candidate)
        by_class[candidate["canonical_class"]].append(candidate)

    asset_results = []
    status_counts: Counter[str] = Counter()
    status_usage: Counter[str] = Counter()
    class_summary: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "asset_count": 0,
        "usage_count": 0,
        "status_counts": Counter(),
    })
    for asset in source_assets:
        name = _normalize(asset["semantic_name"])
        exact = by_name.get(name, [])
        class_pool = by_class.get(asset["canonical_class"], [])
        preview_pool = [
            row for row in class_pool
            if row["preview_index_path"] and row["bbox_json"]
        ]
        exact_preview = [
            row for row in exact
            if row["preview_index_path"] and row["bbox_json"]
        ]
        coverage_ready = _candidate_coverage(len(preview_pool), len(class_pool))
        if asset["callable"]:
            status = "source_exact_callable"
        elif exact_preview:
            status = "exact_name_previewed"
        elif exact:
            status = "exact_name_semantic_only"
        elif coverage_ready:
            status = "class_visual_pool_ready"
        elif preview_pool:
            status = "class_visual_pool_partial"
        elif class_pool:
            status = "class_semantic_only"
        else:
            status = "asset_gap"
        ranked = sorted(
            class_pool,
            key=lambda row: (
                -int(bool(row["preview_index_path"] and row["bbox_json"])),
                -_lexical_score(asset["semantic_name"], row["semantic_name"]),
                -int(bool(row["front_axis"])),
                row["logical_id"],
            ),
        )
        result = {
            "model_id": asset["model_id"],
            "logical_id": asset["logical_id"],
            "semantic_name": asset["semantic_name"],
            "canonical_class": asset["canonical_class"],
            "subtype": asset["subtype"],
            "business_type": asset["business_type"],
            "asset_scopes": json.loads(asset["asset_scopes_json"]),
            "usage_count": asset["usage_count"],
            "scene_count": asset["scene_count"],
            "scene_slugs": json.loads(asset["scene_slugs_json"]),
            "acquisition_priority": asset["acquisition_priority"],
            "source_exact_callable": bool(asset["callable"]),
            "substitution_status": status,
            "exact_name_candidate_count": len(exact),
            "exact_name_previewed_count": len(exact_preview),
            "class_candidate_count": len(class_pool),
            "class_previewed_count": len(preview_pool),
            "class_visual_coverage_ready": coverage_ready,
            "top_exact_name_candidates": [
                _compact_candidate(row, asset["semantic_name"])
                for row in sorted(
                    exact,
                    key=lambda row: (
                        -int(bool(row["preview_index_path"] and row["bbox_json"])),
                        -int(bool(row["front_axis"])),
                        row["logical_id"],
                    ),
                )[:5]
            ],
            "top_class_candidates": [
                _compact_candidate(row, asset["semantic_name"])
                for row in ranked[:5]
            ],
        }
        asset_results.append(result)
        status_counts[status] += 1
        status_usage[status] += int(asset["usage_count"])
        class_row = class_summary[asset["canonical_class"]]
        class_row["asset_count"] += 1
        class_row["usage_count"] += int(asset["usage_count"])
        class_row["status_counts"][status] += 1

    asset_count = len(asset_results)
    usage_total = sum(int(row["usage_count"]) for row in asset_results)
    summary = {
        "source_asset_count": asset_count,
        "source_instance_usage_count": usage_total,
        "source_exact_callable_count": sum(row["source_exact_callable"] for row in asset_results),
        "source_exact_callable_coverage": (
            sum(row["source_exact_callable"] for row in asset_results) / asset_count
            if asset_count else 0.0
        ),
        "exact_name_callable_count": sum(
            row["exact_name_candidate_count"] > 0 for row in asset_results
        ),
        "exact_name_callable_coverage": (
            sum(row["exact_name_candidate_count"] > 0 for row in asset_results) / asset_count
            if asset_count else 0.0
        ),
        "exact_name_previewed_count": sum(
            row["exact_name_previewed_count"] > 0 for row in asset_results
        ),
        "exact_name_previewed_coverage": (
            sum(row["exact_name_previewed_count"] > 0 for row in asset_results) / asset_count
            if asset_count else 0.0
        ),
        "class_callable_count": sum(
            row["class_candidate_count"] > 0 for row in asset_results
        ),
        "class_callable_coverage": (
            sum(row["class_candidate_count"] > 0 for row in asset_results) / asset_count
            if asset_count else 0.0
        ),
        "class_visual_pool_ready_count": sum(
            row["class_visual_coverage_ready"] for row in asset_results
        ),
        "class_visual_pool_ready_coverage": (
            sum(row["class_visual_coverage_ready"] for row in asset_results) / asset_count
            if asset_count else 0.0
        ),
        "status_counts": dict(sorted(status_counts.items())),
        "status_usage_counts": dict(sorted(status_usage.items())),
        "status_usage_coverage": {
            key: round(value / usage_total, 6) if usage_total else 0.0
            for key, value in sorted(status_usage.items())
        },
    }
    class_rows = []
    for canonical_class, value in class_summary.items():
        class_rows.append({
            "canonical_class": canonical_class,
            "asset_count": value["asset_count"],
            "usage_count": value["usage_count"],
            "status_counts": dict(sorted(value["status_counts"].items())),
        })
    class_rows.sort(key=lambda row: (-row["usage_count"], row["canonical_class"]))
    return {
        "schema_version": "synthesis27-matching-evaluation/v1",
        "summary": summary,
        "classes": class_rows,
        "assets": asset_results,
    }


def _markdown(report: dict[str, Any]) -> str:
    summary = report["summary"]
    lines = [
        "# Synthesis-27 asset matching evaluation",
        "",
        "This report separates exact source availability from external semantic substitutions.",
        "A semantic candidate is not proof of visual identity.",
        "",
        f"- Synthesis stable assets: {summary['source_asset_count']}",
        f"- Exact Synthesis assets locally callable: {summary['source_exact_callable_count']} "
        f"({summary['source_exact_callable_coverage']:.1%})",
        f"- External callable assets with the same normalized name: {summary['exact_name_callable_count']} "
        f"({summary['exact_name_callable_coverage']:.1%})",
        f"- Same-name candidates already previewed: {summary['exact_name_previewed_count']} "
        f"({summary['exact_name_previewed_coverage']:.1%})",
        f"- Assets with at least one same-class callable candidate: {summary['class_callable_count']} "
        f"({summary['class_callable_coverage']:.1%})",
        f"- Assets whose same-class visual pool meets coverage gate: "
        f"{summary['class_visual_pool_ready_count']} "
        f"({summary['class_visual_pool_ready_coverage']:.1%})",
        "",
        "## Matching status",
        "",
        "| Status | Stable assets | Referenced instances | Instance coverage |",
        "|---|---:|---:|---:|",
    ]
    for status, count in summary["status_counts"].items():
        usage = summary["status_usage_counts"].get(status, 0)
        coverage = summary["status_usage_coverage"].get(status, 0.0)
        lines.append(f"| {status} | {count} | {usage} | {coverage:.1%} |")
    lines.extend([
        "",
        "## Highest-usage semantic classes",
        "",
        "| Canonical class | Stable assets | Referenced instances | Statuses |",
        "|---|---:|---:|---|",
    ])
    for row in report["classes"][:30]:
        statuses = ", ".join(f"{key}:{value}" for key, value in row["status_counts"].items())
        lines.append(
            f"| {row['canonical_class']} | {row['asset_count']} | "
            f"{row['usage_count']} | {statuses} |"
        )
    lines.extend([
        "",
        "Use same-name and same-class candidates only as a shortlist. A candidate becomes an accepted replacement only after reference-crop visual reranking, bbox/units filtering, front-axis validation and a fixed-camera A/B render.",
        "",
    ])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate how well the callable unified catalog can substitute Synthesis-27 objects."
    )
    parser.add_argument("--synthesis-library", type=Path, required=True)
    parser.add_argument("--semantic-catalog", type=Path, required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--output-markdown", type=Path)
    args = parser.parse_args()

    report = evaluate(args.synthesis_library, args.semantic_catalog)
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    if args.output_markdown:
        args.output_markdown.parent.mkdir(parents=True, exist_ok=True)
        args.output_markdown.write_text(_markdown(report), encoding="utf-8")
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
