#!/usr/bin/env python3
"""Apply per-asset Isaac insertion-smoke evidence to a Synthesis-27 library."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any


PASS = "pass"
FAIL = "fail"
PENDING = "pending"
VALID_CHECK_STATUSES = {PASS, FAIL, PENDING, "not_applicable"}
LICENSE_READY = {
    "approved_for_project_use",
    "license_verified",
    "synthesis_terms_verified",
}


def _ensure_columns(connection: sqlite3.Connection) -> None:
    existing = {row[1] for row in connection.execute("PRAGMA table_info(assets)")}
    columns = {
        "insertion_smoke_status": "TEXT NOT NULL DEFAULT 'pending'",
        "insertion_smoke_report_json": "TEXT",
    }
    for name, declaration in columns.items():
        if name not in existing:
            connection.execute(f"ALTER TABLE assets ADD COLUMN {name} {declaration}")
    connection.commit()


def _effective_status(row: dict[str, Any]) -> tuple[str, list[str]]:
    checks = row.get("checks")
    required = row.get("required_checks")
    if not isinstance(checks, dict):
        raise ValueError("checks must be an object")
    if not isinstance(required, list) or not required:
        raise ValueError("required_checks must be a non-empty list")
    missing: list[str] = []
    observed: list[str] = []
    for name in required:
        if not isinstance(name, str) or not name:
            raise ValueError("required_checks entries must be non-empty strings")
        value = str(checks.get(name) or PENDING)
        if value not in VALID_CHECK_STATUSES:
            raise ValueError(f"invalid check status for {name}: {value}")
        observed.append(value)
        if value == PENDING:
            missing.append(name)
    if FAIL in observed:
        return FAIL, missing
    if missing:
        return PENDING, missing
    return PASS, missing


def _qa_state(asset: sqlite3.Row, smoke_status: str) -> tuple[str, int]:
    structural = str(asset["availability"] or "") == "local_structural_validated"
    preview = str(asset["preview_status"] or "") == PASS
    front = str(asset["front_axis_status"] or "") in {"verified", "not_applicable"}
    license_ready = str(asset["license_status"] or "") in LICENSE_READY
    if structural and preview and front and smoke_status == PASS and license_ready:
        return "ready", 1
    pending = []
    if not structural:
        pending.append("structural")
    if not preview:
        pending.append("preview")
    if not front:
        pending.append("front_axis")
    if smoke_status != PASS:
        pending.append("insertion_smoke")
    if not license_ready:
        pending.append("license")
    return "pending_" + "_".join(pending), 0


def sync(
    library: Path,
    evidence_path: Path,
    *,
    strict: bool = False,
) -> dict[str, Any]:
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    rows = evidence.get("assets")
    if not isinstance(rows, list):
        raise ValueError("evidence must contain an assets list")
    connection = sqlite3.connect(library)
    connection.row_factory = sqlite3.Row
    _ensure_columns(connection)
    results = []
    failures = 0
    for raw in rows:
        if not isinstance(raw, dict):
            failures += 1
            results.append({"status": FAIL, "error": "asset_evidence_not_object"})
            continue
        model_id = str(raw.get("model_id") or "")
        asset = connection.execute(
            """SELECT model_id,availability,preview_status,front_axis_status,
                      license_status
               FROM assets WHERE model_id=?""",
            (model_id,),
        ).fetchone()
        if not model_id or asset is None:
            failures += 1
            results.append({
                "model_id": model_id,
                "status": FAIL,
                "error": "model_id_not_in_library",
            })
            continue
        try:
            smoke_status, missing = _effective_status(raw)
        except ValueError as exc:
            failures += 1
            results.append({
                "model_id": model_id,
                "status": FAIL,
                "error": str(exc),
            })
            continue
        report = {
            "schema_version": str(
                evidence.get("schema_version")
                or "synthesis27-insertion-smoke-evidence/v1"
            ),
            "scene_path": evidence.get("scene_path"),
            "render_path": evidence.get("render_path"),
            "render_feedback_path": evidence.get("render_feedback_path"),
            "asset": raw,
        }
        qa_status, callable_value = _qa_state(asset, smoke_status)
        connection.execute(
            """UPDATE assets
               SET insertion_smoke_status=?,
                   insertion_smoke_report_json=?,
                   qa_status=?,
                   callable=?
               WHERE model_id=?""",
            (
                smoke_status,
                json.dumps(report, ensure_ascii=False, separators=(",", ":")),
                qa_status,
                callable_value,
                model_id,
            ),
        )
        results.append({
            "model_id": model_id,
            "status": smoke_status,
            "missing_checks": missing,
            "qa_status": qa_status,
            "callable": bool(callable_value),
        })
    connection.commit()
    connection.close()
    report = {
        "schema_version": "synthesis27-insertion-smoke-sync/v1",
        "evidence_path": str(evidence_path),
        "result_count": len(results),
        "failure_count": failures,
        "results": results,
    }
    if strict and failures:
        report["strict_failure"] = True
    return report


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Sync per-asset Isaac insertion-smoke evidence into Synthesis-27."
    )
    parser.add_argument("--library", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()
    report = sync(args.library, args.evidence, strict=args.strict)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "result_count": report["result_count"],
        "failure_count": report["failure_count"],
        "output": str(args.output),
    }, ensure_ascii=False, indent=2))
    return 2 if args.strict and report["failure_count"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
