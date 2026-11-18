#!/usr/bin/env python3
"""Add a non-destructive PBR look-development layer to an industrial scene.

The output USD sublayers the input scene, rebinds only simple architectural
surfaces to verified Isaac warehouse materials, and optionally replaces flat
four-point lighting with a directional HDRI/key/fill setup. Referenced object
assets keep their authored materials.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from pxr import Gf, Sdf, Usd, UsdGeom, UsdLux, UsdShade


MATERIAL_FILES = {
    "floor": "opaque__concrete__floor_b_warehouse_a.usd",
    "wall": "opaque__concrete__painted_wall_warehouse_a.usd",
    "service_bay": "opaque__concrete__painted_gray_a_warehouse_a.usd",
    "metal": "opaque__metal__painted03_gray_c_warehouse_a.usd",
    "rack_blue": "opaque__metal__painted01_blue_a_warehouse_a.usd",
    "robot_paint": "opaque__metal__painted01_yellow_a_warehouse_a.usd",
    "worktable_metal": "opaque__metal__painted03_gray_b_warehouse_a.usd",
    "worktable_bolts": "opaque__metal__steel_b_warehouse_a.usd",
    "worktable_top": "opaque__wood__painted_b_warehouse_a.usd",
    "glass": "trans__glass__window_warehouse_a.usd",
    "safety": "opaque__vinyl__tape_yellow_a_warehouse_a.usd",
}

EXACT_BINDINGS = {
    "floor": (
        "/World/Colliders/Floor",
        "/World/Architecture/Appearance/ServiceFloor",
    ),
    "wall": (
        "/World/Colliders/BackWall",
        "/World/Colliders/LeftWall",
    ),
    "service_bay": (
        "/World/Architecture/Appearance/ServiceBayLeft",
        "/World/Architecture/Appearance/ServiceBayRight",
    ),
    "glass": (
        "/World/Architecture/BackHighGlass",
        "/World/Architecture/LeftHighGlass",
    ),
    "safety": (
        "/World/Architecture/Appearance/BackSafetyBand",
    ),
}


def _existing_paths(source: Usd.Stage, patterns: tuple[str, ...]) -> list[str]:
    paths: list[str] = []
    for prim in source.Traverse():
        path = str(prim.GetPath())
        if prim.IsA(UsdGeom.Gprim) and any(pattern in prim.GetName() for pattern in patterns):
            paths.append(path)
    return paths


def _reference_material(stage: Usd.Stage, name: str, asset_path: Path) -> UsdShade.Material:
    source = Usd.Stage.Open(str(asset_path))
    if not source or not source.GetDefaultPrim():
        raise RuntimeError(f"Material asset has no readable default prim: {asset_path}")
    target = UsdShade.Material.Define(stage, f"/World/Looks/Lookdev_{name}")
    target.GetPrim().GetReferences().AddReference(
        str(asset_path), source.GetDefaultPrim().GetPath()
    )
    return target


def _configure_projected_pbr(
    stage: Usd.Stage,
    material: UsdShade.Material,
    *,
    texture_scale: float,
    round_edges_radius: float = 0.0,
) -> None:
    """Enable meter-consistent projection for primitive shells without authored UVs."""
    shader_prim = stage.OverridePrim(material.GetPath().AppendChild("Shader"))
    shader = UsdShade.Shader(shader_prim)
    shader.CreateInput("project_uvw", Sdf.ValueTypeNames.Bool).Set(True)
    shader.CreateInput("world_or_object", Sdf.ValueTypeNames.Bool).Set(True)
    shader.CreateInput("uv_space_index", Sdf.ValueTypeNames.Int).Set(0)
    shader.CreateInput("texture_scale", Sdf.ValueTypeNames.Float2).Set(
        Gf.Vec2f(texture_scale, texture_scale)
    )
    shader.CreateInput("detail_texture_scale", Sdf.ValueTypeNames.Float2).Set(
        Gf.Vec2f(texture_scale * 2.0, texture_scale * 2.0)
    )
    if round_edges_radius > 0.0:
        shader.CreateInput("round_edges_radius", Sdf.ValueTypeNames.Float).Set(round_edges_radius)
        shader.CreateInput("round_edges_roundness", Sdf.ValueTypeNames.Float).Set(1.0)


def _bind(stage: Usd.Stage, prim_path: str, material: UsdShade.Material) -> None:
    prim = stage.OverridePrim(prim_path)
    UsdShade.MaterialBindingAPI.Apply(prim).Bind(
        material,
        bindingStrength=UsdShade.Tokens.strongerThanDescendants,
        materialPurpose=UsdShade.Tokens.allPurpose,
    )


def _robot_painted_shells(source: Usd.Stage) -> list[str]:
    """Find orange/yellow robot shell gprims without touching dark joints."""
    paths: list[str] = []
    for prim in source.Traverse():
        path = str(prim.GetPath())
        if not prim.IsA(UsdGeom.Gprim) or not path.startswith("/World/Objects/RobotArm/"):
            continue
        material, _ = UsdShade.MaterialBindingAPI(prim).ComputeBoundMaterial()
        if not material:
            continue
        shader = material.ComputeSurfaceSource()[0]
        if not shader:
            continue
        color = shader.GetInput("diffuseColor").Get()
        if color and float(color[0]) > 0.65 and float(color[1]) < 0.35 and float(color[2]) < 0.12:
            paths.append(path)
    return paths


def _rack_shells(source: Usd.Stage) -> dict[str, list[str]]:
    paths = {"rack_blue": [], "metal": []}
    for prim in source.Traverse():
        path = str(prim.GetPath())
        if not prim.IsA(UsdGeom.Gprim) or not path.startswith("/World/Objects/StorageRack/"):
            continue
        material, _ = UsdShade.MaterialBindingAPI(prim).ComputeBoundMaterial()
        material_name = material.GetPrim().GetName() if material else ""
        key = "rack_blue" if material_name == "opaque__metal__rack_painted" else "metal"
        paths[key].append(path)
    return paths


def _worktable_surfaces(source: Usd.Stage) -> dict[str, list[str]]:
    paths = {"worktable_metal": [], "worktable_bolts": [], "worktable_top": []}
    for prim in source.Traverse():
        path = str(prim.GetPath())
        if not prim.IsA(UsdGeom.Gprim) or not path.startswith("/World/Objects/AssemblyWorktable/"):
            continue
        name = prim.GetName().lower()
        if "tabletop" in name:
            paths["worktable_top"].append(path)
        elif "bolt" in name:
            paths["worktable_bolts"].append(path)
        else:
            paths["worktable_metal"].append(path)
    return paths


def _apply_lighting(stage: Usd.Stage, hdri_path: Path | None) -> dict[str, object]:
    dome_prim = stage.OverridePrim("/World/Lights/Dome")
    dome = UsdLux.DomeLight(dome_prim)
    if hdri_path:
        dome.CreateTextureFileAttr(Sdf.AssetPath(str(hdri_path)))
        dome.CreateTextureFormatAttr(UsdLux.Tokens.latlong)
    dome_intensity = 220.0 if hdri_path else 520.0
    dome.CreateIntensityAttr(dome_intensity)
    dome.CreateExposureAttr(0.0)

    settings = {
        "/World/Lights/WorkshopKeyLeft": (2800.0, 4300.0),
        "/World/Lights/WorkshopKeyRight": (900.0, 5000.0),
        "/World/Lights/WorkshopFrontFill": (500.0, 5200.0),
    }
    for path, (intensity, temperature) in settings.items():
        light_prim = stage.OverridePrim(path)
        light = UsdLux.RectLight(light_prim)
        light.CreateIntensityAttr(intensity)
        light.CreateEnableColorTemperatureAttr(True)
        light.CreateColorTemperatureAttr(temperature)
    return {
        "dome_texture": str(hdri_path) if hdri_path else None,
        "dome_intensity": dome_intensity,
        "rect_lights": {
            path: {"intensity": intensity, "color_temperature": temperature}
            for path, (intensity, temperature) in settings.items()
        },
    }


def apply_lookdev(
    source_path: Path,
    output_path: Path,
    material_root: Path,
    *,
    hdri_path: Path | None,
    directional_lighting: bool,
) -> dict[str, object]:
    source = Usd.Stage.Open(str(source_path))
    if not source:
        raise RuntimeError(f"Could not open source scene: {source_path}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    stage = Usd.Stage.CreateNew(str(output_path))
    stage.GetRootLayer().subLayerPaths = [str(source_path)]
    UsdGeom.SetStageUpAxis(stage, UsdGeom.GetStageUpAxis(source))
    UsdGeom.SetStageMetersPerUnit(stage, UsdGeom.GetStageMetersPerUnit(source))

    materials = {
        key: _reference_material(stage, key, material_root / filename)
        for key, filename in MATERIAL_FILES.items()
    }
    _configure_projected_pbr(stage, materials["floor"], texture_scale=0.55)
    _configure_projected_pbr(stage, materials["wall"], texture_scale=1.5, round_edges_radius=0.003)
    _configure_projected_pbr(stage, materials["service_bay"], texture_scale=1.1)
    _configure_projected_pbr(stage, materials["metal"], texture_scale=3.0, round_edges_radius=0.006)
    _configure_projected_pbr(stage, materials["rack_blue"], texture_scale=3.0, round_edges_radius=0.004)
    _configure_projected_pbr(stage, materials["robot_paint"], texture_scale=4.0, round_edges_radius=0.003)
    _configure_projected_pbr(stage, materials["worktable_metal"], texture_scale=3.0, round_edges_radius=0.003)
    _configure_projected_pbr(stage, materials["worktable_bolts"], texture_scale=4.0, round_edges_radius=0.001)
    _configure_projected_pbr(stage, materials["worktable_top"], texture_scale=1.6, round_edges_radius=0.004)
    _configure_projected_pbr(stage, materials["safety"], texture_scale=2.0)

    bindings: dict[str, list[str]] = {key: [] for key in materials}
    for material_key, paths in EXACT_BINDINGS.items():
        for path in paths:
            if source.GetPrimAtPath(path):
                _bind(stage, path, materials[material_key])
                bindings[material_key].append(path)

    metal_paths = _existing_paths(
        source,
        ("Mullion", "Transom", "HighBayColumn"),
    )
    for path in metal_paths:
        _bind(stage, path, materials["metal"])
    bindings["metal"].extend(metal_paths)

    robot_paint_paths = _robot_painted_shells(source)
    for path in robot_paint_paths:
        _bind(stage, path, materials["robot_paint"])
    bindings["robot_paint"].extend(robot_paint_paths)

    rack_paths = _rack_shells(source)
    for material_key, paths in rack_paths.items():
        for path in paths:
            _bind(stage, path, materials[material_key])
        bindings[material_key].extend(paths)

    worktable_paths = _worktable_surfaces(source)
    for material_key, paths in worktable_paths.items():
        for path in paths:
            _bind(stage, path, materials[material_key])
        bindings[material_key].extend(paths)

    lighting = _apply_lighting(stage, hdri_path) if directional_lighting or hdri_path else None
    stage.GetRootLayer().Save()
    return {
        "schema_version": "isaac-scene-industrial-lookdev/v1",
        "source_scene": str(source_path),
        "output_scene": str(output_path),
        "material_root": str(material_root),
        "materials": {key: str(material_root / filename) for key, filename in MATERIAL_FILES.items()},
        "bindings": bindings,
        "lighting": lighting,
        "preserved_object_materials": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scene", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--material-root", required=True, type=Path)
    parser.add_argument("--hdri", type=Path)
    parser.add_argument(
        "--directional-lighting",
        action="store_true",
        help="Apply an asymmetric key/fill setup even when no HDRI is supplied.",
    )
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()

    report = apply_lookdev(
        args.scene.resolve(),
        args.output.resolve(),
        args.material_root.resolve(),
        hdri_path=args.hdri.resolve() if args.hdri else None,
        directional_lighting=args.directional_lighting,
    )
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
