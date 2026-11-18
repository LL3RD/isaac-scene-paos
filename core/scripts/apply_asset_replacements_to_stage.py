#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply verified Scene Spec asset replacements while preserving the rest of a USD stage.")
    parser.add_argument("--source-scene", type=Path, required=True)
    parser.add_argument("--scene-spec", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    from pxr import Gf, Sdf, Usd, UsdGeom

    source = Usd.Stage.Open(str(args.source_scene))
    if source is None:
        raise SystemExit(f"Failed to open source scene: {args.source_scene}")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if not source.GetRootLayer().Export(str(args.output)):
        raise SystemExit(f"Failed to export source scene: {args.output}")
    stage = Usd.Stage.Open(str(args.output))
    spec = json.loads(args.scene_spec.read_text(encoding="utf-8"))
    changed = []

    def set_transform(prim: Usd.Prim, item: dict) -> None:
        xform = UsdGeom.Xformable(prim)
        ops = {op.GetOpType(): op for op in xform.GetOrderedXformOps()}
        values = (
            (UsdGeom.XformOp.TypeTranslate, item.get("translate"), Gf.Vec3d),
            (UsdGeom.XformOp.TypeRotateXYZ, item.get("rotate"), Gf.Vec3d),
            (UsdGeom.XformOp.TypeScale, item.get("scale"), Gf.Vec3f),
        )
        for op_type, raw, vector_type in values:
            if raw is None:
                continue
            op = ops.get(op_type)
            if op is None:
                op = (
                    xform.AddTranslateOp()
                    if op_type == UsdGeom.XformOp.TypeTranslate
                    else xform.AddRotateXYZOp()
                    if op_type == UsdGeom.XformOp.TypeRotateXYZ
                    else xform.AddScaleOp()
                )
            op.Set(vector_type(*[float(value) for value in raw]))

    for item in spec.get("entities", []) or []:
        if not isinstance(item, dict) or not item.get("asset_selection"):
            continue
        prim_path = str(item.get("prim") or "")
        prim = stage.GetPrimAtPath(prim_path)
        if not prim or not prim.IsValid():
            raise SystemExit(f"Replacement prim not found: {prim_path}")
        replacement_path = str(item.get("path") or "")
        if not replacement_path:
            raise SystemExit(f"Replacement path missing for: {prim_path}")
        prim.GetReferences().ClearReferences()
        prim.GetReferences().AddReference(replacement_path)
        set_transform(prim, item)
        prim.CreateAttribute("scene:frontAxis", Sdf.ValueTypeNames.String, custom=True).Set(str(item["front_axis"]))
        prim.CreateAttribute("scene:orientationConfidence", Sdf.ValueTypeNames.String, custom=True).Set(
            str(item.get("orientation_confidence") or "high")
        )
        prim.CreateAttribute("scene:orientationSource", Sdf.ValueTypeNames.String, custom=True).Set(
            str(item.get("orientation_source") or "verified asset preview")
        )
        if item.get("facing_target"):
            prim.CreateAttribute("scene:facingTarget", Sdf.ValueTypeNames.String, custom=True).Set(str(item["facing_target"]))
        changed.append({"prim": prim_path, "path": replacement_path})
    if not changed:
        raise SystemExit("No asset_selection replacements found in scene spec")
    stage.GetRootLayer().Save()
    print(json.dumps({
        "source_scene": str(args.source_scene),
        "output": str(args.output),
        "changed_count": len(changed),
        "changed": changed,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
