#!/usr/bin/env python3
"""Upgrade flat neutral equipment coatings without repainting whole assets.

The pass is deliberately conservative. It reads a solved Scene Spec, visits
only selected equipment classes, and rebinds untextured neutral white/gray
UsdPreviewSurface gprims to projected warehouse PBR coatings. Textured,
colored, emissive, transparent, black screen-like, and non-equipment surfaces
are preserved. The output sublayers the input scene and is reversible.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from pxr import Gf, Sdf, Usd, UsdGeom, UsdShade

from apply_industrial_pbr_lookdev import (
    _bind,
    _configure_projected_pbr,
    _reference_material,
)


MATERIAL_FILES = {
    "painted_white": "opaque__metal__painted02_gray_b_warehouse_a.usd",
    "painted_light_gray": "opaque__metal__painted02_gray_b_warehouse_a.usd",
    "painted_mid_gray": "opaque__metal__painted02_gray_a_warehouse_a.usd",
}

MATERIAL_TINTS = {
    "painted_white": (0.56, 0.57, 0.58),
    "painted_light_gray": (0.37, 0.39, 0.41),
    "painted_mid_gray": (0.21, 0.23, 0.25),
}

DEFAULT_CLASS_PREFIXES = (
    "industrial/",
    "furniture/surface/workbench",
    "furniture/storage/workstation",
)


def _has_authored_texture(material: UsdShade.Material) -> bool:
    for prim in Usd.PrimRange(material.GetPrim()):
        if not prim.IsA(UsdShade.Shader):
            continue
        for shader_input in UsdShade.Shader(prim).GetInputs():
            if shader_input.GetTypeName() != Sdf.ValueTypeNames.Asset:
                continue
            value = shader_input.Get()
            if value and str(value):
                return True
    return False


def _classify_neutral_material(material: UsdShade.Material) -> tuple[str | None, str]:
    if not material or _has_authored_texture(material):
        return None, "authored_texture"
    shader_prim = material.ComputeSurfaceSource()[0]
    if not shader_prim:
        return None, "no_surface_shader"
    shader = UsdShade.Shader(shader_prim)
    if shader.GetIdAttr().Get() != "UsdPreviewSurface":
        return None, "non_preview_surface"
    # Constant fallback values do not describe a connected shader network.
    # Repainting them would silently discard procedural/texture-driven inputs.
    if any(shader_input.HasConnectedSource() for shader_input in shader.GetInputs()):
        return None, "connected_surface_inputs"
    metallic_input = shader.GetInput("metallic")
    metallic = metallic_input.Get() if metallic_input else None
    if metallic is not None and float(metallic) > 0.05:
        return None, "authored_metallic_surface"
    workflow = shader.GetInput("useSpecularWorkflow")
    if workflow and workflow.Get():
        return None, "specular_workflow"
    opacity_input = shader.GetInput("opacity")
    opacity = opacity_input.Get() if opacity_input else None
    if opacity is not None and float(opacity) < 0.98:
        return None, "transparent"
    emissive_input = shader.GetInput("emissiveColor")
    emissive = emissive_input.Get() if emissive_input else None
    if emissive and max(float(value) for value in emissive) > 0.02:
        return None, "emissive"
    color_input = shader.GetInput("diffuseColor")
    color = color_input.Get() if color_input else None
    if not color:
        return None, "missing_color"
    channels = [float(value) for value in color]
    if max(channels) - min(channels) > 0.10:
        return None, "colored_identity"
    luminance = sum(channels) / 3.0
    if luminance < 0.18:
        return None, "dark_screen_or_rubber"
    if luminance >= 0.82:
        return "painted_white", "neutral_white"
    if luminance >= 0.48:
        return "painted_light_gray", "neutral_light_gray"
    return "painted_mid_gray", "neutral_mid_gray"


def _source_roughness(material: UsdShade.Material) -> float:
    shader = material.ComputeSurfaceSource()[0]
    value = shader.GetInput("roughness").Get() if shader else None
    # Retain material intent; quantize only for a bounded reusable palette.
    return round(min(1.0, max(0.0, float(value if value is not None else 0.5))), 2)


def apply_object_lookdev(
    scene_path: Path,
    spec_path: Path,
    output_path: Path,
    material_root: Path,
    *,
    class_prefixes: tuple[str, ...] = DEFAULT_CLASS_PREFIXES,
) -> dict[str, object]:
    scene_path, spec_path, output_path = (p.expanduser().resolve() for p in (scene_path, spec_path, output_path))
    material_root = material_root.expanduser().resolve()
    if output_path in {scene_path, spec_path}:
        raise ValueError("Lookdev output must not overwrite its source scene or spec")
    if output_path.exists():
        raise FileExistsError(f"Choose a new lookdev version: {output_path}")
    source = Usd.Stage.Open(str(scene_path))
    if not source:
        raise RuntimeError(f"Could not open scene: {scene_path}")
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    selected = {
        str(entity.get("prim")): {
            "id": str(entity.get("id") or ""),
            "canonical_class": str(entity.get("canonical_class") or ""),
        }
        for entity in spec.get("entities") or spec.get("references") or []
        if isinstance(entity, dict)
        and str(entity.get("prim") or "")
        and any(
            str(entity.get("canonical_class") or "").startswith(prefix)
            for prefix in class_prefixes
        )
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    stage = Usd.Stage.CreateNew(str(output_path))
    stage.GetRootLayer().subLayerPaths = [str(scene_path)]
    UsdGeom.SetStageUpAxis(stage, UsdGeom.GetStageUpAxis(source))
    UsdGeom.SetStageMetersPerUnit(stage, UsdGeom.GetStageMetersPerUnit(source))
    materials: dict[str, UsdShade.Material] = {}
    bindings: dict[str, list[str]] = {}
    surface_parameters: dict[str, dict[str, object]] = {}
    preserved: dict[str, int] = {}
    for root_path in selected:
        root = source.GetPrimAtPath(root_path)
        if not root:
            preserved["missing_object_root"] = preserved.get("missing_object_root", 0) + 1
            continue
        for prim in Usd.PrimRange(root):
            if not prim.IsA(UsdGeom.Gprim):
                continue
            imageable = UsdGeom.Imageable(prim)
            if imageable.ComputeVisibility() == UsdGeom.Tokens.invisible or imageable.ComputePurpose() in {"guide", "proxy"}:
                preserved["non_render_surface"] = preserved.get("non_render_surface", 0) + 1
                continue
            # A stronger parent binding would erase face-level material identity.
            if UsdShade.MaterialBindingAPI(prim).GetMaterialBindSubsets():
                preserved["face_material_subsets"] = preserved.get("face_material_subsets", 0) + 1
                continue
            material, _ = UsdShade.MaterialBindingAPI(prim).ComputeBoundMaterial()
            key, reason = _classify_neutral_material(material)
            if not key:
                preserved[reason] = preserved.get(reason, 0) + 1
                continue
            roughness = _source_roughness(material)
            variant = f"{key}_r{round(roughness * 100):03d}"
            if variant not in materials:
                replacement = _reference_material(stage, variant, material_root / MATERIAL_FILES[key])
                _configure_projected_pbr(stage, replacement, texture_scale=7.0, round_edges_radius=0.002)
                shader = UsdShade.Shader(stage.OverridePrim(replacement.GetPath().AppendChild("Shader")))
                shader.CreateInput("diffuse_tint", Sdf.ValueTypeNames.Color3f).Set(Gf.Vec3f(*MATERIAL_TINTS[key]))
                shader.CreateInput("detail_bump_factor", Sdf.ValueTypeNames.Float).Set(0.025)
                shader.CreateInput("bump_factor", Sdf.ValueTypeNames.Float).Set(0.04)
                shader.CreateInput("reflection_roughness_constant", Sdf.ValueTypeNames.Float).Set(roughness)
                materials[variant] = replacement
                bindings[variant] = []
                surface_parameters[variant] = {"surface_family": key, "source_roughness": roughness}
            path = str(prim.GetPath())
            _bind(stage, path, materials[variant])
            bindings[variant].append(path)

    stage.GetRootLayer().Save()
    return {
        "schema_version": "isaac-scene-object-surface-lookdev/v1",
        "source_scene": str(scene_path),
        "source_spec": str(spec_path),
        "output_scene": str(output_path),
        "material_root": str(material_root),
        "class_prefixes": list(class_prefixes),
        "selected_objects": selected,
        "bindings": bindings,
        "surface_parameters": surface_parameters,
        "source_sha256": hashlib.sha256(scene_path.read_bytes()).hexdigest(),
        "binding_count": sum(len(paths) for paths in bindings.values()),
        "preserved_surface_counts": preserved,
        "geometry_frozen": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scene", required=True, type=Path)
    parser.add_argument("--spec", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--material-root", required=True, type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--class-prefix", action="append", default=[])
    args = parser.parse_args()
    result = apply_object_lookdev(
        args.scene,
        args.spec,
        args.output,
        args.material_root,
        class_prefixes=tuple(args.class_prefix) or DEFAULT_CLASS_PREFIXES,
    )
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
