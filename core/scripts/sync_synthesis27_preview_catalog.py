#!/usr/bin/env python3
"""Apply preview and reviewed front-axis evidence to the Synthesis-27 library."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any


VALID_FRONT_AXES = {"+X", "-X", "+Y", "-Y"}
VALID_FRONT_STATUSES = {"verified", "not_applicable", "pending"}
HORIZONTAL_VIEWS = {"neg_y", "pos_x", "pos_y", "neg_x"}


def effective_preview_status(
    row: dict[str, Any],
    *,
    canonical_class: str,
    subtype: str,
    min_output_bytes: int = 4000,
) -> str:
    if str(row.get("status") or "") != "pass":
        return "fail"
    if canonical_class == "architecture/surface/ceiling" or subtype == "ceiling":
        required = {"pos_z", "neg_z"}
    elif canonical_class == "architecture/surface/floor" or subtype == "floor":
        required = {"pos_z"}
    else:
        required = HORIZONTAL_VIEWS
    previews = row.get("previews")
    if not isinstance(previews, dict) or not required.issubset(previews):
        return "fail"
    for view in required:
        path = Path(str(previews[view]))
        if not path.is_file() or path.stat().st_size < min_output_bytes:
            return "fail"
    return "pass"


def _ensure_columns(connection: sqlite3.Connection) -> None:
    existing = {row[1] for row in connection.execute("PRAGMA table_info(assets)")}
    columns = {
        "preview_status": "TEXT NOT NULL DEFAULT 'pending'",
        "front_axis_status": "TEXT NOT NULL DEFAULT 'pending'",
    }
    for name, declaration in columns.items():
        if name not in existing:
            connection.execute(f"ALTER TABLE assets ADD COLUMN {name} {declaration}")
    connection.commit()


def _read_preview_rows(paths: list[Path]) -> dict[str, tuple[Path, dict[str, Any]]]:
    rows: dict[str, tuple[Path, dict[str, Any]]] = {}
    for path in paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        for row in payload.get("assets", []):
            asset_id = str(row.get("asset_id") or "")
            if asset_id.startswith("synthesis27:"):
                rows[asset_id.split(":", 1)[1]] = (path, row)
    return rows


def _read_front_review(path: Path | None) -> dict[str, dict[str, str]]:
    if path is None:
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("assets", payload)
    if not isinstance(rows, dict):
        raise ValueError("front review must be an object or contain an assets object")
    result: dict[str, dict[str, str]] = {}
    for model_id, row in rows.items():
        if not isinstance(row, dict):
            raise ValueError(f"front review for {model_id} must be an object")
        status = str(row.get("status") or "pending")
        axis = str(row.get("front_axis") or "")
        if status not in VALID_FRONT_STATUSES:
            raise ValueError(f"invalid front review status for {model_id}: {status}")
        if status == "verified" and axis not in VALID_FRONT_AXES:
            raise ValueError(f"verified front axis for {model_id} must be one of {sorted(VALID_FRONT_AXES)}")
        if status != "verified" and axis:
            raise ValueError(f"{model_id}: only verified reviews may define front_axis")
        result[str(model_id)] = {"status": status, "front_axis": axis}
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Sync Synthesis-27 preview manifests and reviewed front-axis evidence."
    )
    parser.add_argument("--library", type=Path, required=True)
    parser.add_argument("--preview-index", type=Path, action="append", required=True)
    parser.add_argument("--front-review", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()

    preview_rows = _read_preview_rows(args.preview_index)
    front_reviews = _read_front_review(args.front_review)
    connection = sqlite3.connect(args.library)
    connection.row_factory = sqlite3.Row
    _ensure_columns(connection)
    known = {
        str(row[0]) for row in connection.execute("SELECT model_id FROM assets")
    }
    results = []
    failures = 0
    for model_id in sorted(set(preview_rows) | set(front_reviews)):
        if model_id not in known:
            failures += 1
            results.append({
                "model_id": model_id,
                "status": "fail",
                "error": "model_id_not_in_library",
            })
            continue
        preview_path, preview = preview_rows.get(model_id, (None, {}))
        preview_status = str(preview.get("status") or "pending")
        if preview_status not in {"pass", "fail", "pending", "running"}:
            preview_status = "fail"
        front = front_reviews.get(model_id)
        current = connection.execute(
            """SELECT front_axis, front_axis_status, availability,
                      canonical_class, subtype
               FROM assets WHERE model_id=?""",
            (model_id,),
        ).fetchone()
        if preview_path is not None:
            preview_status = effective_preview_status(
                preview,
                canonical_class=str(current["canonical_class"]),
                subtype=str(current["subtype"]),
            )
        front_axis = current["front_axis"]
        front_status = str(current["front_axis_status"] or "pending")
        if front is not None:
            front_status = front["status"]
            front_axis = front["front_axis"] or None

        bbox = preview.get("bbox")
        meters_per_unit = preview.get("meters_per_unit")
        if preview_path is not None:
            connection.execute(
                """UPDATE assets
                   SET preview_index_path=?,
                       preview_status=?,
                       bbox_json=COALESCE(?, bbox_json),
                       meters_per_unit=COALESCE(?, meters_per_unit)
                   WHERE model_id=?""",
                (
                    str(preview_path),
                    preview_status,
                    json.dumps(bbox, separators=(",", ":")) if bbox else None,
                    float(meters_per_unit) if meters_per_unit is not None else None,
                    model_id,
                ),
            )
        connection.execute(
            """UPDATE assets
               SET front_axis=?, front_axis_status=?
               WHERE model_id=?""",
            (front_axis, front_status, model_id),
        )
        structural_ready = str(current["availability"]) == "local_structural_validated"
        preview_ready = preview_status == "pass" or (
            preview_path is None
            and connection.execute(
                "SELECT preview_status FROM assets WHERE model_id=?",
                (model_id,),
            ).fetchone()[0] == "pass"
        )
        front_ready = front_status in {"verified", "not_applicable"}
        if structural_ready and preview_ready and front_ready:
            qa_status = "pending_license_and_insertion_smoke"
        elif structural_ready and preview_ready:
            qa_status = "pending_front_axis_license_and_insertion_smoke"
        else:
            qa_status = "pending_preview_front_license_smoke"
        connection.execute(
            "UPDATE assets SET qa_status=?, callable=0 WHERE model_id=?",
            (qa_status, model_id),
        )
        connection.commit()
        results.append({
            "model_id": model_id,
            "status": "pass",
            "preview_status": preview_status,
            "front_axis_status": front_status,
            "front_axis": front_axis,
            "qa_status": qa_status,
            "callable": False,
        })
    connection.close()
    report = {
        "schema_version": "synthesis27-preview-sync/v1",
        "result_count": len(results),
        "failure_count": failures,
        "results": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "result_count": len(results),
        "failure_count": failures,
        "output": str(args.output),
    }, ensure_ascii=False, indent=2))
    return 2 if failures and args.strict else 0


if __name__ == "__main__":
    raise SystemExit(main())
