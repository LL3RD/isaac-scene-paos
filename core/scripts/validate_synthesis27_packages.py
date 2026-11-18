#!/usr/bin/env python3
"""Validate acquired Synthesis-27 packages with USD and keep them non-callable.

Run this script with an Isaac Sim/pxr-capable Python. Structural validation is
only one promotion gate: previews, front-axis review, license review and an
Isaac insertion smoke test are still required before callable may become true.
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
from pathlib import Path
from typing import Any, Optional


USD_SUFFIXES = {".usd", ".usda", ".usdc", ".usdz"}


def select_entrypoint(package_dir: Path) -> Path:
    candidates = [
        path
        for path in package_dir.iterdir()
        if path.is_file() and path.suffix.lower() in USD_SUFFIXES
    ]
    if not candidates:
        candidates = [
            path
            for path in package_dir.rglob("*")
            if path.is_file() and path.suffix.lower() in USD_SUFFIXES
        ]
    if not candidates:
        raise RuntimeError(f"no USD entrypoint under {package_dir}")

    def rank(path: Path) -> tuple[int, int, int, str]:
        relative = path.relative_to(package_dir)
        top_level = int(len(relative.parts) != 1)
        model_named = int(not path.stem.lower().startswith("model_"))
        support_named = int(path.stem.lower() in {"material", "materials", "link_0"})
        return top_level, model_named, support_named, relative.as_posix()

    return min(candidates, key=rank)


def _ensure_columns(connection: sqlite3.Connection) -> None:
    existing = {row[1] for row in connection.execute("PRAGMA table_info(assets)")}
    columns = {
        "up_axis": "TEXT",
        "joint_count": "INTEGER",
        "articulation_root_count": "INTEGER",
        "structural_validation_json": "TEXT",
    }
    for name, declaration in columns.items():
        if name not in existing:
            connection.execute(f"ALTER TABLE assets ADD COLUMN {name} {declaration}")
    connection.commit()


def _resolve_entrypoint(
    *,
    model_id: str,
    source_path: Optional[str],
    packages_root: Optional[Path],
) -> Path:
    if packages_root is not None:
        return select_entrypoint(packages_root / model_id / "package")
    if not source_path:
        raise RuntimeError("asset has no source_path and --packages-root was not supplied")
    path = Path(source_path)
    return select_entrypoint(path) if path.is_dir() else path


def validate_stage(path: Path) -> dict[str, Any]:
    from pxr import Usd, UsdGeom, UsdPhysics, UsdUtils

    if not path.is_file():
        raise RuntimeError(f"entrypoint is missing: {path}")
    stage = Usd.Stage.Open(str(path))
    if stage is None:
        raise RuntimeError(f"USD stage could not be opened: {path}")
    default_prim = stage.GetDefaultPrim()
    if not default_prim or not default_prim.IsValid():
        raise RuntimeError("USD stage has no valid defaultPrim")

    meters_per_unit = float(UsdGeom.GetStageMetersPerUnit(stage))
    if not math.isfinite(meters_per_unit) or meters_per_unit <= 0.0:
        raise RuntimeError(f"invalid metersPerUnit: {meters_per_unit}")
    up_axis = str(UsdGeom.GetStageUpAxis(stage))
    if up_axis != "Z":
        raise RuntimeError(
            f"Synthesis top-level package must be Z-up; got {up_axis}. "
            "Do not apply an unverified Blender axis conversion."
        )

    purposes = [
        UsdGeom.Tokens.default_,
        UsdGeom.Tokens.render,
        UsdGeom.Tokens.proxy,
    ]
    cache = UsdGeom.BBoxCache(
        Usd.TimeCode.Default(),
        purposes,
        useExtentsHint=True,
    )
    aligned = cache.ComputeWorldBound(default_prim).ComputeAlignedRange()
    minimum = [float(value) for value in aligned.GetMin()]
    maximum = [float(value) for value in aligned.GetMax()]
    values = minimum + maximum
    if not all(math.isfinite(value) for value in values):
        raise RuntimeError(f"non-finite composed bounds: {values}")
    size = [maximum[index] - minimum[index] for index in range(3)]
    size_meters = [value * meters_per_unit for value in size]
    if max(size_meters) <= 1e-5:
        raise RuntimeError(f"degenerate composed bounds: {size_meters}")
    if max(size_meters) > 2000.0:
        raise RuntimeError(f"implausibly large composed bounds: {size_meters}")

    prims = list(stage.Traverse())
    joints = [str(prim.GetPath()) for prim in prims if prim.IsA(UsdPhysics.Joint)]
    articulation_roots = [
        str(prim.GetPath())
        for prim in prims
        if prim.HasAPI(UsdPhysics.ArticulationRootAPI)
    ]
    dependencies = UsdUtils.ComputeAllDependencies(str(path))
    unresolved = sorted(str(item) for item in dependencies[2])
    if unresolved:
        raise RuntimeError(f"unresolved USD dependencies: {unresolved}")

    return {
        "entrypoint": str(path),
        "default_prim": str(default_prim.GetPath()),
        "prim_count": len(prims),
        "meters_per_unit": meters_per_unit,
        "up_axis": up_axis,
        "bbox": {
            "min": minimum,
            "max": maximum,
            "size": size,
            "size_meters": size_meters,
        },
        "joint_count": len(joints),
        "joint_paths": joints,
        "articulation_root_count": len(articulation_roots),
        "articulation_roots": articulation_roots,
        "unresolved_dependencies": unresolved,
        "structural_status": "pass",
        "callable": False,
        "remaining_gates": [
            "four_view_preview",
            "front_axis_review",
            "license_review",
            "isaac_insertion_smoke",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Structurally validate acquired Synthesis-27 USD packages."
    )
    parser.add_argument("--library", type=Path, required=True)
    parser.add_argument("--packages-root", type=Path)
    parser.add_argument("--model-id", action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()

    connection = sqlite3.connect(args.library)
    connection.row_factory = sqlite3.Row
    _ensure_columns(connection)
    filters = ["availability IN ('local_acquired', 'local_structural_validated')"]
    parameters: list[Any] = []
    if args.model_id:
        filters.append("model_id IN (" + ",".join("?" for _ in args.model_id) + ")")
        parameters.extend(args.model_id)
    rows = list(connection.execute(
        "SELECT model_id, source_path FROM assets WHERE "
        + " AND ".join(filters)
        + " ORDER BY model_id",
        parameters,
    ))

    results = []
    failures = 0
    for row in rows:
        model_id = str(row["model_id"])
        try:
            entrypoint = _resolve_entrypoint(
                model_id=model_id,
                source_path=row["source_path"],
                packages_root=args.packages_root,
            )
            result = validate_stage(entrypoint)
            result["model_id"] = model_id
            connection.execute(
                """UPDATE assets
                   SET availability='local_structural_validated',
                       source_path=?,
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
                    str(entrypoint),
                    result["meters_per_unit"],
                    json.dumps(result["bbox"], separators=(",", ":")),
                    result["up_axis"],
                    result["joint_count"],
                    result["articulation_root_count"],
                    json.dumps(result, ensure_ascii=False, separators=(",", ":")),
                    model_id,
                ),
            )
        except Exception as exc:
            failures += 1
            result = {
                "model_id": model_id,
                "structural_status": "fail",
                "error": f"{type(exc).__name__}: {exc}",
                "callable": False,
            }
            connection.execute(
                """UPDATE assets
                   SET qa_status='structural_failed',
                       structural_validation_json=?,
                       callable=0
                   WHERE model_id=?""",
                (
                    json.dumps(result, ensure_ascii=False, separators=(",", ":")),
                    model_id,
                ),
            )
        connection.commit()
        results.append(result)
        print(
            f"SYNTHESIS_PACKAGE_VALIDATE {model_id} "
            f"{result['structural_status']}",
            flush=True,
        )
    connection.close()

    report = {
        "schema_version": "synthesis27-package-validation/v1",
        "selected_count": len(rows),
        "pass_count": len(rows) - failures,
        "failure_count": failures,
        "results": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "selected_count": report["selected_count"],
        "pass_count": report["pass_count"],
        "failure_count": report["failure_count"],
        "output": str(args.output),
    }, ensure_ascii=False, indent=2))
    return 2 if failures and args.strict else 0


if __name__ == "__main__":
    raise SystemExit(main())
