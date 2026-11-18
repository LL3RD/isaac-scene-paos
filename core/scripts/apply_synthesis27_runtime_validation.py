#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any

from validate_synthesis27_packages import _ensure_columns


def apply_report(
    connection: sqlite3.Connection,
    report: dict[str, Any],
) -> dict[str, Any]:
    if report.get("schema_version") != "synthesis27-package-validation/v1":
        raise ValueError("unsupported runtime validation report schema")
    _ensure_columns(connection)
    applied: list[str] = []
    skipped: list[dict[str, str]] = []
    for result in report.get("results", []):
        if not isinstance(result, dict):
            continue
        model_id = str(result.get("model_id") or "")
        if not model_id:
            skipped.append({"model_id": "", "reason": "missing_model_id"})
            continue
        if result.get("structural_status") != "pass":
            skipped.append({"model_id": model_id, "reason": "validation_not_pass"})
            continue
        row = connection.execute(
            "SELECT source_path, availability FROM assets WHERE model_id=?",
            (model_id,),
        ).fetchone()
        if row is None:
            skipped.append({"model_id": model_id, "reason": "asset_not_found"})
            continue
        if str(row[1] or "") not in {"local_acquired", "local_structural_validated"}:
            skipped.append({"model_id": model_id, "reason": "asset_not_locally_acquired"})
            continue
        # Preserve the catalog's local package path. The report entrypoint may
        # point at a temporary compute-host validation mirror.
        connection.execute(
            """UPDATE assets
               SET availability='local_structural_validated',
                   meters_per_unit=?,
                   bbox_json=?,
                   up_axis=?,
                   joint_count=?,
                   articulation_root_count=?,
                   structural_validation_json=?,
                   qa_status='pending_preview_front_license_smoke',
                   callable=0
               WHERE model_id=?""",
            (
                result.get("meters_per_unit"),
                json.dumps(result.get("bbox") or {}, separators=(",", ":")),
                result.get("up_axis"),
                int(result.get("joint_count") or 0),
                int(result.get("articulation_root_count") or 0),
                json.dumps(result, ensure_ascii=False, separators=(",", ":")),
                model_id,
            ),
        )
        applied.append(model_id)
    connection.commit()
    return {
        "schema_version": "synthesis27-runtime-validation-import/v1",
        "applied_count": len(applied),
        "applied_model_ids": applied,
        "skipped_count": len(skipped),
        "skipped": skipped,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Import Isaac-runtime Synthesis package validation evidence into "
            "the local catalog without replacing local source paths."
        )
    )
    parser.add_argument("--library", type=Path, required=True)
    parser.add_argument("--report", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()
    if not args.library.is_file():
        parser.error(f"library not found: {args.library}")
    for report_path in args.report:
        if not report_path.is_file():
            parser.error(f"report not found: {report_path}")

    connection = sqlite3.connect(args.library)
    combined = {
        "schema_version": "synthesis27-runtime-validation-import-batch/v1",
        "reports": [],
        "applied_count": 0,
        "skipped_count": 0,
    }
    try:
        for report_path in args.report:
            report = json.loads(report_path.read_text(encoding="utf-8"))
            result = apply_report(connection, report)
            result["report_path"] = str(report_path)
            combined["reports"].append(result)
            combined["applied_count"] += result["applied_count"]
            combined["skipped_count"] += result["skipped_count"]
    finally:
        connection.close()

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(combined, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(combined, ensure_ascii=False, indent=2))
    return 1 if args.strict and combined["skipped_count"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
