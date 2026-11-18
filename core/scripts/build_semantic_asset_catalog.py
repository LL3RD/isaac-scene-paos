#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from isaac_scene.semantic_catalog import (  # noqa: E402
    initialize,
    official_index_rows,
    pcnl_catalog_rows,
    rebuild_fts,
    synthesis_rows,
    taxonomy,
    upsert,
)


def load_existing_overrides(path: Path) -> dict[str, dict]:
    if not path.is_file():
        return {}
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    tables = {
        row[0]
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    overrides: dict[str, dict] = {}
    if "semantic_overrides" in tables:
        for row in connection.execute("SELECT logical_id, patch_json FROM semantic_overrides"):
            overrides[str(row["logical_id"])] = json.loads(row["patch_json"])
    if "semantic_assets" in tables:
        enriched = connection.execute(
            """SELECT logical_id, preview_index_path, front_axis, meters_per_unit,
                      bbox_json, style_tags_json, material_tags_json, color_tags_json,
                      metadata_json, search_text
               FROM semantic_assets
               WHERE preview_index_path IS NOT NULL
                  OR front_axis IS NOT NULL
                  OR meters_per_unit IS NOT NULL
                  OR bbox_json IS NOT NULL
                  OR style_tags_json <> '[]'
                  OR material_tags_json <> '[]'
                  OR color_tags_json <> '[]'"""
        )
        for row in enriched:
            patch = overrides.setdefault(str(row["logical_id"]), {})
            for key in (
                "preview_index_path", "front_axis", "meters_per_unit", "bbox_json",
                "style_tags_json", "material_tags_json", "color_tags_json",
                "metadata_json", "search_text",
            ):
                patch.setdefault(key, row[key])
    connection.close()
    return overrides


def main() -> int:
    parser = argparse.ArgumentParser(description="Build one logical semantic catalog while retaining source provenance.")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--pcnl-catalog", type=Path)
    parser.add_argument("--official-index", type=Path)
    parser.add_argument("--synthesis-manifest", type=Path)
    parser.add_argument("--synthesis-root", type=Path)
    parser.add_argument(
        "--taxonomy-output",
        type=Path,
        help="Optional semantic-first JSON view; defaults beside the SQLite catalog.",
    )
    args = parser.parse_args()

    if args.synthesis_manifest and not args.synthesis_root:
        parser.error("--synthesis-root is required with --synthesis-manifest")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    existing_overrides = load_existing_overrides(args.output)
    working_output = Path(str(args.output) + ".building")
    for stale in (
        working_output,
        Path(str(working_output) + "-wal"),
        Path(str(working_output) + "-shm"),
    ):
        if stale.exists():
            stale.unlink()
    connection = sqlite3.connect(working_output)
    initialize(connection)
    for logical_id, patch in existing_overrides.items():
        connection.execute(
            """INSERT INTO semantic_overrides(logical_id, patch_json, updated_at)
               VALUES(?, ?, ?)""",
            (
                logical_id,
                json.dumps(patch, ensure_ascii=False, sort_keys=True),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
    counts: Counter[str] = Counter()
    inputs = []
    if args.pcnl_catalog:
        inputs.append(("pcnl_catalog", pcnl_catalog_rows(args.pcnl_catalog)))
    if args.official_index:
        inputs.append(("official_index", official_index_rows(args.official_index)))
    if args.synthesis_manifest:
        inputs.append(("synthesis27", synthesis_rows(args.synthesis_manifest, args.synthesis_root)))
    for input_name, rows in inputs:
        for row in rows:
            try:
                upsert(connection, row)
                counts[input_name] += 1
            except sqlite3.IntegrityError as exc:
                if "source_path" not in str(exc):
                    raise
                counts[f"{input_name}_duplicate_path"] += 1
            if sum(counts.values()) % 5000 == 0:
                connection.commit()
    rebuild_fts(connection)
    summary_rows = connection.execute(
        """SELECT canonical_class, COUNT(*) AS total, SUM(callable) AS callable
           FROM semantic_assets GROUP BY canonical_class ORDER BY total DESC"""
    ).fetchall()
    total = connection.execute("SELECT COUNT(*) FROM semantic_assets").fetchone()[0]
    connection.commit()
    connection.close()
    working_output.replace(args.output)
    taxonomy_output = args.taxonomy_output or args.output.with_suffix(".taxonomy.json")
    taxonomy_output.parent.mkdir(parents=True, exist_ok=True)
    taxonomy_payload = taxonomy(args.output, limit=1000)
    taxonomy_payload["schema_version"] = "isaac-semantic-asset-taxonomy/v1"
    taxonomy_payload["generated_at"] = datetime.now(timezone.utc).isoformat()
    taxonomy_working = Path(str(taxonomy_output) + ".building")
    taxonomy_working.write_text(
        json.dumps(taxonomy_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    taxonomy_working.replace(taxonomy_output)
    summary = {
        "schema_version": "isaac-semantic-asset-catalog-build/v2",
        "output": str(args.output),
        "taxonomy_output": str(taxonomy_output),
        "total_assets": total,
        "input_counts": dict(counts),
        "classes": [
            {"canonical_class": row[0], "total": row[1], "callable": row[2]}
            for row in summary_rows
        ],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
