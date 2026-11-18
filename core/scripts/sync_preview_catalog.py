#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from isaac_scene.preview_manifest import asset_is_complete  # noqa: E402
from isaac_scene.semantic_catalog import annotate, rebuild_fts  # noqa: E402


DEFAULT_VIEWS = ["neg_y", "pos_x", "pos_y", "neg_x"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Hydrate semantic preview/bbox metadata from strict preview manifests."
    )
    parser.add_argument("--preview-index", type=Path, action="append", required=True)
    parser.add_argument("--semantic-catalog", type=Path, required=True)
    parser.add_argument("--required-views", nargs="+", default=DEFAULT_VIEWS)
    parser.add_argument("--min-output-bytes", type=int, default=4000)
    parser.add_argument("--strict", action="store_true")
    return parser.parse_args()


def eligible_rows(
    manifest: dict,
    *,
    required_views: list[str],
    min_output_bytes: int,
) -> tuple[list[dict], list[dict]]:
    passed: list[dict] = []
    skipped: list[dict] = []
    for row in manifest.get("assets", []):
        if asset_is_complete(
            row,
            required_views=required_views,
            min_output_bytes=min_output_bytes,
        ) and row.get("bbox") and row.get("meters_per_unit") is not None:
            passed.append(row)
        else:
            skipped.append({
                "asset_id": row.get("asset_id"),
                "status": row.get("status"),
                "reason": "incomplete_views_or_measurement",
            })
    return passed, skipped


def main() -> int:
    args = parse_args()
    if not args.semantic_catalog.is_file():
        raise SystemExit(f"Semantic catalog not found: {args.semantic_catalog}")
    synced: list[dict] = []
    skipped: list[dict] = []
    errors: list[dict] = []
    for index_path in args.preview_index:
        manifest = json.loads(index_path.read_text(encoding="utf-8"))
        rows, rejected = eligible_rows(
            manifest,
            required_views=args.required_views,
            min_output_bytes=args.min_output_bytes,
        )
        skipped.extend({**item, "preview_index": str(index_path)} for item in rejected)
        for row in rows:
            try:
                result = annotate(
                    args.semantic_catalog,
                    source_path=str(row["path"]),
                    preview_index_path=str(index_path),
                    bbox=row["bbox"],
                    meters_per_unit=float(row["meters_per_unit"]),
                    metadata_patch={
                        "measurement_source": "four_view_preview",
                        "preview_qa": {
                            "status": "pass",
                            "required_views": args.required_views,
                            "min_output_bytes": args.min_output_bytes,
                        },
                    },
                    rebuild_search_index=False,
                )
                synced.append({
                    "asset_id": row.get("asset_id"),
                    "logical_id": result["logical_id"],
                    "preview_index": str(index_path),
                })
            except Exception as exc:
                errors.append({
                    "asset_id": row.get("asset_id"),
                    "path": row.get("path"),
                    "preview_index": str(index_path),
                    "error": f"{type(exc).__name__}: {exc}",
                })
    if synced:
        connection = sqlite3.connect(args.semantic_catalog)
        rebuild_fts(connection)
        connection.commit()
        connection.close()
    payload = {
        "schema_version": "isaac-preview-catalog-sync/v1",
        "semantic_catalog": str(args.semantic_catalog),
        "required_views": args.required_views,
        "synced_count": len(synced),
        "skipped_count": len(skipped),
        "error_count": len(errors),
        "synced": synced,
        "skipped": skipped,
        "errors": errors,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if args.strict and (skipped or errors):
        return 1
    return 0 if not errors else 2


if __name__ == "__main__":
    raise SystemExit(main())
