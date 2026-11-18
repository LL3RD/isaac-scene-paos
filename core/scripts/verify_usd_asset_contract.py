#!/usr/bin/env python3
"""Inspect a downloaded USD package before it is composed into an Isaac stage.

Run with Isaac Sim's Python, for example:

    /path/to/isaac/python scripts/verify_usd_asset_contract.py model.usd

This reports only facts authored by the composed USD stage.  It deliberately
does not infer the source application's scene coordinates or a semantic front
axis from a filename/bounding box.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any


def _vector(value: Any) -> list[float]:
    return [float(value[index]) for index in range(3)]


def _finite(values: list[float]) -> bool:
    return all(math.isfinite(value) for value in values)


def _root_ops(UsdGeom: Any, root: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for prim in root.GetChildren():
        ops = UsdGeom.Xformable(prim).GetOrderedXformOps()
        if not ops:
            continue
        rows.append({
            "prim": str(prim.GetPath()),
            "ops": [
                {
                    "name": str(op.GetOpName()),
                    "type": str(op.GetOpType()),
                    "value": str(op.Get()),
                }
                for op in ops
            ],
        })
    return rows


def inspect(path: Path) -> dict[str, Any]:
    from pxr import Usd, UsdGeom

    result: dict[str, Any] = {
        "schema_version": "usd-asset-contract-audit/v1",
        "asset_path": str(path),
        "exists": path.is_file(),
        "status": "fail",
        "coordinate_contract": {
            "source_scene_transform_verified": False,
            "front_axis_verified": False,
            "note": (
                "This audit validates the composed USD package only. It cannot prove how "
                "a source application encoded scene_info offset/rotation."
            ),
        },
        "errors": [],
        "warnings": [],
    }
    if not path.is_file():
        result["errors"].append({"code": "ASSET_NOT_FOUND", "summary": str(path)})
        return result
    stage = Usd.Stage.Open(str(path))
    if stage is None:
        result["errors"].append({"code": "USD_OPEN_FAILED", "summary": "Usd.Stage.Open returned None"})
        return result
    default_prim = stage.GetDefaultPrim()
    up_axis = str(UsdGeom.GetStageUpAxis(stage))
    meters_per_unit = float(UsdGeom.GetStageMetersPerUnit(stage))
    result.update({
        "default_prim": str(default_prim.GetPath()) if default_prim else None,
        "up_axis": up_axis,
        "up_axis_authored": bool(stage.HasAuthoredMetadata("upAxis")),
        "meters_per_unit": meters_per_unit,
        "meters_per_unit_authored": bool(stage.HasAuthoredMetadata("metersPerUnit")),
        "root_xform_ops": _root_ops(UsdGeom, default_prim) if default_prim else [],
        "used_layers": sorted(str(layer.identifier) for layer in stage.GetUsedLayers()),
    })
    if default_prim is None:
        result["errors"].append({"code": "DEFAULT_PRIM_MISSING", "summary": "Package has no default prim"})
        return result
    cache = UsdGeom.BBoxCache(
        Usd.TimeCode.Default(), [UsdGeom.Tokens.default_, UsdGeom.Tokens.render]
    )
    bounds = cache.ComputeWorldBound(default_prim).ComputeAlignedRange()
    minimum, maximum = _vector(bounds.GetMin()), _vector(bounds.GetMax())
    size = [maximum[index] - minimum[index] for index in range(3)]
    result["bbox"] = {"min": minimum, "max": maximum, "size": size}
    if not _finite([*minimum, *maximum]) or any(value <= 0 for value in size):
        result["errors"].append({"code": "BBOX_INVALID", "summary": "default-prim world bbox is non-finite or degenerate"})
        return result
    if not result["up_axis_authored"]:
        result["warnings"].append({"code": "UP_AXIS_UNAUTHORED", "summary": "Do not add an axis conversion based on the inherited default."})
    if not result["meters_per_unit_authored"]:
        result["warnings"].append({"code": "UNITS_UNAUTHORED", "summary": "Do not infer reference scale without bbox/domain evidence."})
    if up_axis != "Z":
        result["warnings"].append({"code": "NON_Z_UP_PACKAGE", "summary": "Use an explicit, recorded axis conversion only after visual validation."})
    if meters_per_unit <= 0 or not math.isfinite(meters_per_unit):
        result["errors"].append({"code": "UNITS_INVALID", "summary": "metersPerUnit is non-positive or non-finite"})
        return result
    result["status"] = "pass"
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit a composed USD package's units and up-axis without guessing source-scene transforms.")
    parser.add_argument("asset", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        payload = inspect(args.asset)
    except Exception as exc:  # Import/runtime failures still return structured diagnostics.
        payload = {
            "schema_version": "usd-asset-contract-audit/v1",
            "asset_path": str(args.asset),
            "status": "fail",
            "errors": [{"code": "AUDIT_EXCEPTION", "summary": f"{type(exc).__name__}: {exc}"}],
        }
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0 if payload.get("status") == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
