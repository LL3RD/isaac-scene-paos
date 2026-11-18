#!/usr/bin/env python3
"""Apply a compact, non-destructive PBR shell pass to generated scenes.

This pass targets only procedural room surfaces such as floors, walls, glass,
trim, and safety markings. Referenced furniture and equipment keep their
authored materials. The output sublayers the source scene, so it is reversible.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from pxr import Usd, UsdGeom, UsdLux

from apply_industrial_pbr_lookdev import (
    _bind,
    _configure_projected_pbr,
    _reference_material,
)


MATERIAL_FILES = {
    "concrete_floor": "opaque__concrete__floor_b_warehouse_a.usd",
    "wood_floor": "opaque__wood__painted_b_warehouse_a.usd",
    "wall": "opaque__concrete__painted_wall_warehouse_a.usd",
    "gray_wall": "opaque__concrete__painted_gray_a_warehouse_a.usd",
    "painted_metal": "opaque__metal__painted03_gray_c_warehouse_a.usd",
    "rack_blue": "opaque__metal__painted01_blue_a_warehouse_a.usd",
    "painted_wood": "opaque__wood__painted_b_warehouse_a.usd",
    "glass": "trans__glass__window_warehouse_a.usd",
    "safety": "opaque__vinyl__tape_yellow_a_warehouse_a.usd",
}

HOME_PROFILES = {"kitchen", "living"}


def _reference_material_prim(
    stage: Usd.Stage, name: str, source_path: Path, prim_path: str
):
    source = Usd.Stage.Open(str(source_path))
    if not source or not source.GetPrimAtPath(prim_path):
        raise RuntimeError(f"Material prim is missing: {source_path}:{prim_path}")
    from pxr import UsdShade

    target = UsdShade.Material.Define(stage, f"/World/Looks/Lookdev_{name}")
    target.GetPrim().GetReferences().AddReference(str(source_path), prim_path)
    return target


def _classify(path: str, profile: str) -> str | None:
    """Map a procedural shell gprim to a small material vocabulary."""
    name = path.rsplit("/", 1)[-1].lower()
    is_home = profile in HOME_PROFILES

    if "glass" in name:
        return "glass"
    if any(token in name for token in ("aisleline", "stopline", "safetyband")):
        return "safety"
    if any(token in name for token in ("floor", "sealedfloor", "floorfinish")):
        return "wood_floor" if profile == "living" else "concrete_floor"
    if "servicebay" in name:
        return "gray_wall"
    if any(token in name for token in ("mullion", "transom", "column")):
        return "painted_metal"
    if any(token in name for token in ("windowframe", "doorframe", "doorsideframe", "doorheader")):
        return "painted_wood" if is_home else "painted_metal"
    if any(token in name for token in ("baseboard", "windowsill", "windowwall")):
        return "painted_wood" if is_home else "painted_metal"
    if "doorleaf" in name or "doorpanel" in name:
        return "painted_wood" if is_home else "painted_metal"
    if any(token in name for token in ("wall", "lowerband", "ceiling", "cove")):
        return "wall"
    return None


def _refine_lighting(stage: Usd.Stage, profile: str) -> dict[str, dict[str, float]]:
    """Introduce color contrast while preserving authored light transforms."""
    if profile in HOME_PROFILES:
        settings = {
            "/World/Lights/Dome": (420.0, 5600.0),
            "/World/Lights/WindowDaylight": (2400.0, 5600.0),
            "/World/Lights/WarmPanelLeft": (3400.0, 3600.0),
            "/World/Lights/WarmPanelRight": (1800.0, 4000.0),
            "/World/Lights/OpenFrontFill": (700.0, 4800.0),
        }
    elif profile == "logistics_warehousing":
        settings = {
            "/World/Lights/Dome": (700.0, 5600.0),
            "/World/Lights/FactoryFill": (1500.0, 4400.0),
        }
    else:
        settings = {
            "/World/Lights/Dome": (520.0, 5600.0),
            "/World/Lights/WorkshopKeyLeft": (2800.0, 4300.0),
            "/World/Lights/WorkshopKeyRight": (900.0, 5000.0),
            "/World/Lights/WorkshopFrontFill": (500.0, 5200.0),
        }

    authored: dict[str, dict[str, float]] = {}
    for path, (intensity, temperature) in settings.items():
        prim = stage.GetPrimAtPath(path)
        if not prim:
            continue
        if prim.IsA(UsdLux.DomeLight):
            light = UsdLux.DomeLight(stage.OverridePrim(path))
        elif prim.IsA(UsdLux.RectLight):
            light = UsdLux.RectLight(stage.OverridePrim(path))
        else:
            continue
        light.CreateIntensityAttr(intensity)
        light.CreateEnableColorTemperatureAttr(True)
        light.CreateColorTemperatureAttr(temperature)
        authored[path] = {"intensity": intensity, "color_temperature": temperature}
    return authored


def apply_lookdev(
    source_path: Path,
    output_path: Path,
    material_root: Path,
    *,
    profile: str,
    refine_lighting: bool,
    interior_material_scene: Path | None,
) -> dict[str, object]:
    source = Usd.Stage.Open(str(source_path))
    if not source:
        raise RuntimeError(f"Could not open source scene: {source_path}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    stage = Usd.Stage.CreateNew(str(output_path))
    stage.GetRootLayer().subLayerPaths = [str(source_path)]
    UsdGeom.SetStageUpAxis(stage, UsdGeom.GetStageUpAxis(source))
    UsdGeom.SetStageMetersPerUnit(stage, UsdGeom.GetStageMetersPerUnit(source))

    use_interior = profile in HOME_PROFILES and interior_material_scene is not None
    materials = {
        key: _reference_material(stage, key, material_root / filename)
        for key, filename in MATERIAL_FILES.items()
        if not (use_interior and key in {"wood_floor", "painted_wood"})
    }
    if use_interior:
        materials["wood_floor"] = _reference_material_prim(
            stage, "wood_floor", interior_material_scene, "/Root/Looks/ParquetFloor"
        )
        materials["painted_wood"] = _reference_material_prim(
            stage, "painted_wood", interior_material_scene, "/Root/Looks/WoodWall"
        )
    projection = {
        "concrete_floor": (0.55, 0.0),
        "wood_floor": (1.25, 0.003),
        "wall": (1.5, 0.003),
        "gray_wall": (1.1, 0.003),
        "painted_metal": (3.0, 0.004),
        "rack_blue": (3.0, 0.004),
        "painted_wood": (1.6, 0.004),
        "safety": (2.0, 0.0),
    }
    for key, (scale, radius) in projection.items():
        _configure_projected_pbr(
            stage, materials[key], texture_scale=scale, round_edges_radius=radius
        )

    bindings: dict[str, list[str]] = {key: [] for key in materials}
    for prim in source.Traverse():
        path = str(prim.GetPath())
        if not prim.IsA(UsdGeom.Gprim):
            continue
        if not (path.startswith("/World/Architecture") or path.startswith("/World/Colliders")):
            continue
        key = _classify(path, profile)
        if not key:
            continue
        _bind(stage, path, materials[key])
        bindings[key].append(path)

    if profile == "logistics_warehousing":
        for prim in source.Traverse():
            path = str(prim.GetPath())
            if not prim.IsA(UsdGeom.Gprim) or not path.startswith("/World/Objects/RackBank"):
                continue
            key = "painted_metal" if "shelf" in prim.GetName().lower() else "rack_blue"
            _bind(stage, path, materials[key])
            bindings[key].append(path)

    lighting = _refine_lighting(stage, profile) if refine_lighting else {}
    stage.GetRootLayer().Save()
    return {
        "schema_version": "isaac-scene-pbr-shell-lookdev/v1",
        "source_scene": str(source_path),
        "output_scene": str(output_path),
        "profile": profile,
        "material_root": str(material_root),
        "interior_material_scene": (
            str(interior_material_scene) if interior_material_scene else None
        ),
        "bindings": bindings,
        "lighting": lighting,
        "preserved_referenced_object_materials": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scene", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--material-root", required=True, type=Path)
    parser.add_argument(
        "--interior-material-scene",
        type=Path,
        help="Optional USD containing /Root/Looks/ParquetFloor and WoodWall.",
    )
    parser.add_argument(
        "--profile",
        required=True,
        choices=("factory", "logistics_warehousing", "kitchen", "living"),
    )
    parser.add_argument("--refine-lighting", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()

    report = apply_lookdev(
        args.scene.resolve(),
        args.output.resolve(),
        args.material_root.resolve(),
        profile=args.profile,
        refine_lighting=args.refine_lighting,
        interior_material_scene=(
            args.interior_material_scene.resolve() if args.interior_material_scene else None
        ),
    )
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
