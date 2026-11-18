from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from isaac_scene.camera_planning import project_bounds


TEXTURE_SUFFIXES = {".bmp", ".exr", ".hdr", ".jpeg", ".jpg", ".png", ".tga", ".tif", ".tiff"}
COLOR_TOKENS = ("albedo", "base_color", "basecolor", "diffuse", "color", "opacity")
MICRO_TOKENS = ("normal", "bump", "rough", "orm", "rma", "mra", "gloss")
TRANSMISSION_TOKENS = ("glass", "opacity", "transmission", "thin_walled", "ior")


def classify_material_signals(signals: dict[str, Any]) -> dict[str, Any]:
    """Classify whether a bound material can create non-uniform physical response.

    This intentionally distinguishes a texture file being declared from the
    geometry having usable UVs or an enabled procedural projection.
    """
    bound = bool(signals.get("bound"))
    texture_assets = [str(value) for value in signals.get("texture_assets") or []]
    mapped = bool(signals.get("has_st") or signals.get("project_uvw"))
    color_texture = bool(signals.get("color_texture"))
    micro_texture = bool(signals.get("micro_texture"))
    specialized = bool(signals.get("specialized_pbr"))
    pbr_controls = bool(signals.get("pbr_controls"))

    if not bound:
        tier, score = "unbound", 0.0
    elif texture_assets and not mapped:
        tier, score = "texture_declared_unmapped", 0.12
    elif texture_assets and micro_texture:
        tier, score = "textured_microdetail", 1.0
    elif texture_assets and color_texture:
        tier, score = "textured_color_only", 0.72
    elif texture_assets:
        tier, score = "textured_other", 0.62
    elif specialized:
        tier, score = "specialized_pbr", 0.76
    elif pbr_controls:
        tier, score = "constant_pbr", 0.36
    else:
        tier, score = "constant_basic", 0.18

    return {
        "tier": tier,
        "quality_score": score,
        "texture_declared": bool(texture_assets),
        "texture_mapping_effective": bool(texture_assets and mapped),
        "micro_surface_effective": bool(texture_assets and mapped and micro_texture),
    }


def summarize_material_rows(
    rows: Iterable[dict[str, Any]],
    *,
    minimum_score: float = 0.65,
    minimum_effective_texture_fraction: float = 0.55,
    maximum_unmapped_texture_fraction: float = 0.05,
    maximum_unbound_fraction: float = 0.02,
    max_gaps: int = 30,
) -> dict[str, Any]:
    rows = list(rows)
    total_weight = sum(max(float(row.get("weight") or 0.0), 0.0) for row in rows)
    if total_weight <= 1e-12:
        total_weight = float(len(rows)) or 1.0
        normalized_rows = [{**row, "weight": 1.0} for row in rows]
    else:
        normalized_rows = rows

    tiers: Counter[str] = Counter()
    weighted_score = 0.0
    effective_texture_weight = 0.0
    micro_surface_weight = 0.0
    unmapped_weight = 0.0
    unbound_weight = 0.0
    bound_weight = 0.0
    for row in normalized_rows:
        weight = max(float(row.get("weight") or 0.0), 0.0)
        tier = str(row.get("tier") or "unbound")
        tiers[tier] += weight
        weighted_score += weight * float(row.get("quality_score") or 0.0)
        bound_weight += weight * float(tier != "unbound")
        effective_texture_weight += weight * float(bool(row.get("texture_mapping_effective")))
        micro_surface_weight += weight * float(bool(row.get("micro_surface_effective")))
        unmapped_weight += weight * float(tier == "texture_declared_unmapped")
        unbound_weight += weight * float(tier == "unbound")

    fractions = {
        "material_bound": bound_weight / total_weight,
        "effective_texture": effective_texture_weight / total_weight,
        "micro_surface": micro_surface_weight / total_weight,
        "unmapped_texture": unmapped_weight / total_weight,
        "unbound": unbound_weight / total_weight,
    }
    score = weighted_score / total_weight
    failures = []
    if score < minimum_score:
        failures.append("weighted_quality_score_below_minimum")
    if fractions["effective_texture"] < minimum_effective_texture_fraction:
        failures.append("effective_texture_coverage_below_minimum")
    if fractions["unmapped_texture"] > maximum_unmapped_texture_fraction:
        failures.append("unmapped_texture_coverage_above_maximum")
    if fractions["unbound"] > maximum_unbound_fraction:
        failures.append("unbound_coverage_above_maximum")

    def gap_priority(row: dict[str, Any]) -> float:
        return max(float(row.get("weight") or 0.0), 0.0) * (1.0 - float(row.get("quality_score") or 0.0))

    gaps = []
    for row in sorted(normalized_rows, key=gap_priority, reverse=True):
        tier = str(row.get("tier") or "unbound")
        if float(row.get("quality_score") or 0.0) >= 0.72:
            continue
        recommendation = {
            "unbound": "bind a physically plausible surface-class PBR material",
            "texture_declared_unmapped": "author primvars:st or enable verified world/object-space projection",
            "constant_pbr": "replace repeated constants with surface-class albedo/roughness/normal or ORM detail",
            "constant_basic": "replace display-color/basic shading with a physically controlled material",
            "textured_other": "verify the texture role and add roughness/normal response where appropriate",
        }.get(tier, "inspect the material network and rendered response")
        gaps.append({
            "prim_path": row.get("prim_path"),
            "material_path": row.get("material_path"),
            "tier": tier,
            "role": row.get("role"),
            "weight_fraction": float(row.get("weight") or 0.0) / total_weight,
            "priority": gap_priority(row) / total_weight,
            "recommendation": recommendation,
        })
        if len(gaps) >= max_gaps:
            break

    return {
        "status": "pass" if not failures else "fail",
        "weighted_quality_score": score,
        "coverage_fractions": fractions,
        "tier_weight_fractions": {key: value / total_weight for key, value in sorted(tiers.items())},
        "thresholds": {
            "minimum_score": minimum_score,
            "minimum_effective_texture_fraction": minimum_effective_texture_fraction,
            "maximum_unmapped_texture_fraction": maximum_unmapped_texture_fraction,
            "maximum_unbound_fraction": maximum_unbound_fraction,
        },
        "failures": failures,
        "priority_gaps": gaps,
    }


def _asset_path(value: Any) -> str | None:
    path = str(getattr(value, "resolvedPath", "") or getattr(value, "path", "") or (value if isinstance(value, str) else ""))
    if path and Path(path.split("?", 1)[0]).suffix.lower() in TEXTURE_SUFFIXES:
        return path
    return None


def _material_signals(material: Any, prim: Any, Usd: Any, UsdGeom: Any, UsdShade: Any) -> dict[str, Any]:
    if not material:
        return {"bound": False}
    texture_assets: list[str] = []
    texture_inputs: list[tuple[str, str]] = []
    input_names: set[str] = set()
    shader_ids: set[str] = set()
    project_uvw = False
    source_assets: list[str] = []
    for child in Usd.PrimRange(material.GetPrim()):
        if not child.IsA(UsdShade.Shader):
            continue
        shader = UsdShade.Shader(child)
        shader_ids.add(str(shader.GetIdAttr().Get() or "").lower())
        for context in ("mdl", "", "universal"):
            try:
                source = shader.GetSourceAsset(context)
            except Exception:
                source = None
            if source:
                source_assets.append(str(source.path or "").lower())
        for shader_input in shader.GetInputs():
            name = str(shader_input.GetBaseName()).lower()
            input_names.add(name)
            value = shader_input.Get()
            asset = _asset_path(value)
            if asset:
                texture_assets.append(asset)
                texture_inputs.append((name, asset.lower()))
            if name == "project_uvw" and bool(value):
                project_uvw = True

    primvars = UsdGeom.PrimvarsAPI(prim)
    try:
        has_st = bool(primvars.FindPrimvarWithInheritance("st"))
    except Exception:
        has_st = False
    if not has_st:
        has_st = any(
            str(item.GetPrimvarName()).lower() == "uv"
            or str(item.GetPrimvarName()).lower().startswith("st")
            for item in primvars.GetPrimvars()
        )
    color_texture = any(any(token in name for token in COLOR_TOKENS) for name, _path in texture_inputs)
    micro_texture = any(
        any(token in name or token in path for token in MICRO_TOKENS)
        for name, path in texture_inputs
    )
    specialized_pbr = any(
        any(token in name for token in TRANSMISSION_TOKENS) for name in input_names
    ) or any("glass" in value for value in source_assets)
    pbr_controls = any(
        token in name
        for name in input_names
        for token in ("rough", "metal", "specular", "ior", "clearcoat", "diffuse_color", "base_color")
    ) or any(value for value in shader_ids if "previewsurface" in value)
    return {
        "bound": True,
        "texture_assets": sorted(set(texture_assets)),
        "input_names": sorted(input_names),
        "has_st": has_st,
        "project_uvw": project_uvw,
        "color_texture": color_texture,
        "micro_texture": micro_texture,
        "specialized_pbr": specialized_pbr,
        "pbr_controls": pbr_controls,
    }


def _world_bounds(prim: Any, bbox_cache: Any) -> tuple[tuple[float, float, float], tuple[float, float, float]] | None:
    try:
        world_range = bbox_cache.ComputeWorldBound(prim).ComputeAlignedRange()
        minimum, maximum = world_range.GetMin(), world_range.GetMax()
        values = tuple(float(minimum[index]) for index in range(3)), tuple(float(maximum[index]) for index in range(3))
    except Exception:
        return None
    if any(abs(value) > 1e8 for point in values for value in point):
        return None
    return values


def _bbox_surface_area(bounds: tuple[tuple[float, float, float], tuple[float, float, float]]) -> float:
    size = [max(bounds[1][index] - bounds[0][index], 0.0) for index in range(3)]
    return max(2.0 * (size[0] * size[1] + size[0] * size[2] + size[1] * size[2]), 1e-8)


def _camera_projection(stage: Any, camera_path: str, UsdGeom: Any, Gf: Any) -> dict[str, Any]:
    prim = stage.GetPrimAtPath(camera_path)
    if not prim or not prim.IsA(UsdGeom.Camera):
        raise ValueError(f"Camera not found: {camera_path}")
    camera = UsdGeom.Camera(prim)
    matrix = UsdGeom.Xformable(prim).ComputeLocalToWorldTransform(0.0)
    eye_vec = matrix.ExtractTranslation()
    forward_vec = matrix.TransformDir(Gf.Vec3d(0.0, 0.0, -1.0)).GetNormalized()
    eye = tuple(float(eye_vec[index]) for index in range(3))
    target = tuple(float(eye_vec[index] + forward_vec[index]) for index in range(3))
    return {
        "eye": eye,
        "target": target,
        "focal_length_mm": float(camera.GetFocalLengthAttr().Get() or 24.0),
        "horizontal_aperture_mm": float(camera.GetHorizontalApertureAttr().Get() or 36.0),
        "vertical_aperture_mm": float(camera.GetVerticalApertureAttr().Get() or 20.25),
    }


def audit_material_coverage(
    scene_path: str | Path,
    *,
    root_path: str = "/World",
    camera_path: str | None = None,
    minimum_score: float = 0.65,
    minimum_effective_texture_fraction: float = 0.55,
    maximum_unmapped_texture_fraction: float = 0.05,
    maximum_unbound_fraction: float = 0.02,
    max_gaps: int = 30,
) -> dict[str, Any]:
    """Audit renderable USD geometry using camera or surface-area proxy weights."""
    from pxr import Gf, Usd, UsdGeom, UsdLux, UsdShade  # type: ignore

    stage = Usd.Stage.Open(str(scene_path))
    if not stage:
        raise ValueError(f"Unable to open USD stage: {scene_path}")
    root = stage.GetPrimAtPath(root_path)
    if not root:
        raise ValueError(f"Root prim not found: {root_path}")
    bbox_cache = UsdGeom.BBoxCache(0.0, [UsdGeom.Tokens.default_, UsdGeom.Tokens.render])
    camera = _camera_projection(stage, camera_path, UsdGeom, Gf) if camera_path else None
    rows: list[dict[str, Any]] = []
    for prim in Usd.PrimRange(root):
        if not prim.IsA(UsdGeom.Boundable):
            continue
        if prim.HasAPI(UsdLux.LightAPI) or prim.GetTypeName().endswith("Light"):
            continue
        imageable = UsdGeom.Imageable(prim)
        if imageable.ComputeVisibility() == UsdGeom.Tokens.invisible:
            continue
        if str(imageable.GetPurposeAttr().Get() or "default") in {"guide", "proxy"}:
            continue
        bounds = _world_bounds(prim, bbox_cache)
        if not bounds:
            continue
        if camera:
            projection = project_bounds(bounds, **camera)
            weight = float(projection.get("screen_area") or 0.0)
            if weight <= 1e-8:
                continue
        else:
            projection = None
            weight = _bbox_surface_area(bounds)

        path = str(prim.GetPath())
        role = "shell" if any(token in path.lower() for token in ("/architecture/", "/colliders/", "floor", "wall", "ceiling")) else "object"
        subset_materials = []
        for child in prim.GetChildren():
            if child.GetTypeName() != "GeomSubset":
                continue
            material, _relationship = UsdShade.MaterialBindingAPI(child).ComputeBoundMaterial()
            if material:
                subset_materials.append((child, material))
        material_rows = subset_materials
        if not material_rows:
            material, _relationship = UsdShade.MaterialBindingAPI(prim).ComputeBoundMaterial()
            material_rows = [(prim, material)]
        row_weight = weight / max(len(material_rows), 1)
        for binding_prim, material in material_rows:
            signals = _material_signals(material, prim, Usd, UsdGeom, UsdShade)
            classified = classify_material_signals(signals)
            rows.append({
                "prim_path": str(binding_prim.GetPath()) if binding_prim != prim else path,
                "geometry_prim_path": path,
                "prim_type": prim.GetTypeName(),
                "material_path": str(material.GetPath()) if material else None,
                "role": role,
                "weight": row_weight,
                "bounds_m": {"min": list(bounds[0]), "max": list(bounds[1])},
                "screen_area_proxy": projection.get("screen_area") if projection else None,
                **signals,
                **classified,
            })

    summary = summarize_material_rows(
        rows,
        minimum_score=minimum_score,
        minimum_effective_texture_fraction=minimum_effective_texture_fraction,
        maximum_unmapped_texture_fraction=maximum_unmapped_texture_fraction,
        maximum_unbound_fraction=maximum_unbound_fraction,
        max_gaps=max_gaps,
    )
    return {
        "schema_version": "isaac-scene-material-coverage/v1",
        "scene_path": str(scene_path),
        "root_path": root_path,
        "camera_path": camera_path,
        "weight_mode": "camera_projected_bbox_proxy" if camera else "world_bbox_surface_area_proxy",
        "geometry_count": len(rows),
        **summary,
    }
