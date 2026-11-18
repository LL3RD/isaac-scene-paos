from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path
from statistics import median
from typing import Any, Iterable


Vec3 = tuple[float, float, float]
Bounds = tuple[Vec3, Vec3]


def _vec(values: Iterable[float]) -> Vec3:
    row = tuple(float(value) for value in values)
    if len(row) != 3:
        raise ValueError("Expected a three-dimensional vector")
    return row  # type: ignore[return-value]


def _sub(left: Vec3, right: Vec3) -> Vec3:
    return tuple(left[index] - right[index] for index in range(3))  # type: ignore[return-value]


def _dot(left: Vec3, right: Vec3) -> float:
    return sum(left[index] * right[index] for index in range(3))


def _cross(left: Vec3, right: Vec3) -> Vec3:
    return (
        left[1] * right[2] - left[2] * right[1],
        left[2] * right[0] - left[0] * right[2],
        left[0] * right[1] - left[1] * right[0],
    )


def _unit(value: Vec3) -> Vec3:
    length = math.sqrt(_dot(value, value))
    if length <= 1e-9:
        raise ValueError("Camera position and target must be different")
    return tuple(component / length for component in value)  # type: ignore[return-value]


def room_usable_bounds(spec: dict[str, Any]) -> Bounds:
    room = spec.get("room") or {}
    usable = room.get("usable_bounds_m") or {}
    if usable.get("min") is not None and usable.get("max") is not None:
        return _vec(usable["min"]), _vec(usable["max"])
    size = _vec(room.get("size_m") or [6.0, 5.0, 3.0])
    center = _vec(room.get("center_m") or [0.0, 0.0, 0.0])
    return (
        (center[0] - size[0] * 0.5, center[1] - size[1] * 0.5, center[2]),
        (center[0] + size[0] * 0.5, center[1] + size[1] * 0.5, center[2] + size[2]),
    )


def perimeter_camera_positions(
    bounds: Bounds,
    *,
    inset_m: float = 0.45,
    eye_heights_m: tuple[float, ...] = (1.55, 2.15),
) -> list[Vec3]:
    minimum, maximum = bounds
    inset = min(max(inset_m, 0.2), max(min(maximum[0] - minimum[0], maximum[1] - minimum[1]) * 0.2, 0.2))
    x0, x1 = minimum[0] + inset, maximum[0] - inset
    y0, y1 = minimum[1] + inset, maximum[1] - inset
    mx, my = (x0 + x1) * 0.5, (y0 + y1) * 0.5
    floor_z = minimum[2]
    ring = [
        (x0, y0), (x1, y0), (x1, y1), (x0, y1),
        (mx, y0), (x1, my), (mx, y1), (x0, my),
    ]
    return [
        (x, y, min(floor_z + height, maximum[2] - 0.25))
        for height in eye_heights_m
        for x, y in ring
    ]


def exterior_opening_camera_positions(
    bounds: Bounds,
    side_coverage: dict[str, float],
    *,
    opening_threshold: float = 0.35,
    eye_heights_m: tuple[float, ...] = (1.8, 2.6),
) -> list[Vec3]:
    minimum, maximum = bounds
    width, depth = maximum[0] - minimum[0], maximum[1] - minimum[1]
    offset = max(min(width, depth) * 0.18, 0.8)
    mx, my = (minimum[0] + maximum[0]) * 0.5, (minimum[1] + maximum[1]) * 0.5
    open_sides = {key for key, value in side_coverage.items() if float(value) < opening_threshold}
    xy: list[tuple[float, float]] = []
    if "x_min" in open_sides:
        xy.extend((minimum[0] - offset, y) for y in (my - depth * 0.22, my, my + depth * 0.22))
    if "x_max" in open_sides:
        xy.extend((maximum[0] + offset, y) for y in (my - depth * 0.22, my, my + depth * 0.22))
    if "y_min" in open_sides:
        xy.extend((x, minimum[1] - offset) for x in (mx - width * 0.22, mx, mx + width * 0.22))
    if "y_max" in open_sides:
        xy.extend((x, maximum[1] + offset) for x in (mx - width * 0.22, mx, mx + width * 0.22))
    for x_side, x in (("x_min", minimum[0] - offset), ("x_max", maximum[0] + offset)):
        for y_side, y in (("y_min", minimum[1] - offset), ("y_max", maximum[1] + offset)):
            if x_side in open_sides and y_side in open_sides:
                xy.append((x, y))
    unique_xy = list(dict.fromkeys((round(x, 6), round(y, 6)) for x, y in xy))
    return [
        (x, y, min(minimum[2] + height, maximum[2] - 0.25))
        for height in eye_heights_m
        for x, y in unique_xy
    ]


def _bounds_corners(bounds: Bounds) -> list[Vec3]:
    minimum, maximum = bounds
    return [
        (x, y, z)
        for x in (minimum[0], maximum[0])
        for y in (minimum[1], maximum[1])
        for z in (minimum[2], maximum[2])
    ]


def project_bounds(
    bounds: Bounds,
    eye: Vec3,
    target: Vec3,
    *,
    focal_length_mm: float = 24.0,
    horizontal_aperture_mm: float = 36.0,
    vertical_aperture_mm: float = 20.25,
) -> dict[str, Any]:
    forward = _unit(_sub(target, eye))
    right = _unit(_cross(forward, (0.0, 0.0, 1.0)))
    up = _unit(_cross(right, forward))
    tan_x = horizontal_aperture_mm / (2.0 * focal_length_mm)
    tan_y = vertical_aperture_mm / (2.0 * focal_length_mm)
    projected: list[tuple[float, float]] = []
    positive_depth_count = 0
    for corner in _bounds_corners(bounds):
        relative = _sub(corner, eye)
        depth = _dot(relative, forward)
        if depth <= 0.01:
            continue
        positive_depth_count += 1
        projected.append((_dot(relative, right) / (depth * tan_x), _dot(relative, up) / (depth * tan_y)))
    if not projected:
        return {"in_front": False, "frame_fraction": 0.0, "screen_area": 0.0, "center": None}
    raw = (
        min(value[0] for value in projected), min(value[1] for value in projected),
        max(value[0] for value in projected), max(value[1] for value in projected),
    )
    raw_area = max(raw[2] - raw[0], 1e-9) * max(raw[3] - raw[1], 1e-9)
    clipped = (max(raw[0], -1.0), max(raw[1], -1.0), min(raw[2], 1.0), min(raw[3], 1.0))
    clipped_area = max(clipped[2] - clipped[0], 0.0) * max(clipped[3] - clipped[1], 0.0)
    return {
        "in_front": positive_depth_count > 0,
        "positive_depth_corner_fraction": positive_depth_count / 8.0,
        "raw_bbox_ndc": list(raw),
        "clipped_bbox_ndc": list(clipped),
        "frame_fraction": min(clipped_area / raw_area, 1.0),
        "screen_area": min(clipped_area / 4.0, 1.0),
        "center": [(clipped[0] + clipped[2]) * 0.5, (clipped[1] + clipped[3]) * 0.5],
    }


def _segment_hits_bounds(eye: Vec3, target: Vec3, bounds: Bounds) -> bool:
    direction = _sub(target, eye)
    lower, upper = 0.0, 1.0
    for axis in range(3):
        if abs(direction[axis]) <= 1e-9:
            if eye[axis] < bounds[0][axis] or eye[axis] > bounds[1][axis]:
                return False
            continue
        first = (bounds[0][axis] - eye[axis]) / direction[axis]
        second = (bounds[1][axis] - eye[axis]) / direction[axis]
        near, far = min(first, second), max(first, second)
        lower, upper = max(lower, near), min(upper, far)
        if lower > upper:
            return False
    return 0.02 < lower < 0.94


def _inside_expanded(point: Vec3, bounds: Bounds, margin_m: float = 0.12) -> bool:
    return all(bounds[0][axis] - margin_m <= point[axis] <= bounds[1][axis] + margin_m for axis in range(3))


def _entity_weight(entity: dict[str, Any]) -> float:
    if entity.get("criticality") == "anchor":
        return 3.0
    if entity.get("generation_layer") == "baseline_required":
        return 2.0
    if entity.get("placement_policy") == "floor_underlay":
        return 0.25
    if entity.get("generation_layer") == "enrichment":
        return 0.75
    return 1.0


def _merged_interval_fraction(intervals: list[tuple[float, float]], minimum: float, maximum: float) -> float:
    if maximum <= minimum or not intervals:
        return 0.0
    clipped = sorted((max(minimum, left), min(maximum, right)) for left, right in intervals if right > minimum and left < maximum)
    if not clipped:
        return 0.0
    merged: list[list[float]] = []
    for left, right in clipped:
        if not merged or left > merged[-1][1]:
            merged.append([left, right])
        else:
            merged[-1][1] = max(merged[-1][1], right)
    return min(sum(right - left for left, right in merged) / (maximum - minimum), 1.0)


def shell_side_coverage(bounds: Bounds, structural_bounds: list[dict[str, Any]]) -> dict[str, float]:
    """Estimate which room sides provide a real visual backdrop.

    Prompt-only room shells intentionally leave one side open for an exterior overview camera.
    An interior camera looking back through that opening produces a large white void, so camera
    ranking must know which boundaries are actually authored.  This remains a broad-phase signal:
    final pixels and the visual agent are still authoritative.
    """
    minimum, maximum = bounds
    tolerance = max(min(maximum[0] - minimum[0], maximum[1] - minimum[1]) * 0.035, 0.12)
    intervals: dict[str, list[tuple[float, float]]] = {key: [] for key in ("x_min", "x_max", "y_min", "y_max")}
    tokens = ("wall", "window", "glazing", "glass", "door", "partition", "curtain")
    for row in structural_bounds:
        name = str(row.get("prim") or row.get("id") or "").lower()
        if not any(token in name for token in tokens):
            continue
        lower, upper = row["bounds"]
        if upper[2] - lower[2] < 1.2:
            continue
        extent_x, extent_y = upper[0] - lower[0], upper[1] - lower[1]
        x_plane = extent_x <= max(extent_y * 0.25, tolerance * 2.0)
        y_plane = extent_y <= max(extent_x * 0.25, tolerance * 2.0)
        if x_plane and (abs(lower[0] - minimum[0]) <= tolerance or abs(upper[0] - minimum[0]) <= tolerance):
            intervals["x_min"].append((lower[1], upper[1]))
        if x_plane and (abs(lower[0] - maximum[0]) <= tolerance or abs(upper[0] - maximum[0]) <= tolerance):
            intervals["x_max"].append((lower[1], upper[1]))
        if y_plane and (abs(lower[1] - minimum[1]) <= tolerance or abs(upper[1] - minimum[1]) <= tolerance):
            intervals["y_min"].append((lower[0], upper[0]))
        if y_plane and (abs(lower[1] - maximum[1]) <= tolerance or abs(upper[1] - maximum[1]) <= tolerance):
            intervals["y_max"].append((lower[0], upper[0]))
    return {
        "x_min": _merged_interval_fraction(intervals["x_min"], minimum[1], maximum[1]),
        "x_max": _merged_interval_fraction(intervals["x_max"], minimum[1], maximum[1]),
        "y_min": _merged_interval_fraction(intervals["y_min"], minimum[0], maximum[0]),
        "y_max": _merged_interval_fraction(intervals["y_max"], minimum[0], maximum[0]),
    }


def backdrop_score(eye: Vec3, target: Vec3, side_coverage: dict[str, float] | None) -> float:
    if not side_coverage:
        return 1.0
    direction = _sub(target, eye)
    x_weight, y_weight = abs(direction[0]), abs(direction[1])
    total = x_weight + y_weight
    if total <= 1e-9:
        return 0.0
    x_side = "x_max" if direction[0] >= 0.0 else "x_min"
    y_side = "y_max" if direction[1] >= 0.0 else "y_min"
    return (
        x_weight * float(side_coverage.get(x_side, 0.0))
        + y_weight * float(side_coverage.get(y_side, 0.0))
    ) / total


def score_camera_candidate(
    eye: Vec3,
    target: Vec3,
    entities: list[dict[str, Any]],
    *,
    blockers: list[dict[str, Any]] | None = None,
    focal_length_mm: float = 24.0,
    aspect_ratio: float = 16.0 / 9.0,
    horizontal_aperture_mm: float = 36.0,
    shell_side_coverage: dict[str, float] | None = None,
) -> dict[str, Any]:
    blockers = blockers or []
    if any(_inside_expanded(eye, row["bounds"]) for row in entities):
        return {
            "status": "reject",
            "reason": "camera_inside_object_clearance",
            "composition_score": 0.0,
            "anchor_visibility_score": 0.0,
            "visible_entity_count": 0,
            "entity_visibility": [],
        }
    evidence = []
    weighted_visibility = weighted_anchor_visibility = 0.0
    total_weight = total_anchor_weight = 0.0
    weighted_edge_safety = weighted_anchor_edge_safety = 0.0
    weighted_x = weighted_y = visible_weight = 0.0
    screen_area_sum = 0.0
    for entity in entities:
        weight = float(entity.get("weight") or _entity_weight(entity))
        projection = project_bounds(
            entity["bounds"],
            eye,
            target,
            focal_length_mm=focal_length_mm,
            horizontal_aperture_mm=horizontal_aperture_mm,
            vertical_aperture_mm=horizontal_aperture_mm / max(aspect_ratio, 0.01),
        )
        center = tuple((entity["bounds"][0][i] + entity["bounds"][1][i]) * 0.5 for i in range(3))
        occluders = []
        if projection["frame_fraction"] > 0.0:
            for blocker in blockers:
                if blocker.get("prim") == entity.get("prim"):
                    continue
                if _segment_hits_bounds(eye, center, blocker["bounds"]):
                    occluders.append(str(blocker.get("prim") or blocker.get("id") or "blocker"))
        occlusion_factor = 0.25 if occluders else 1.0
        visibility = float(projection["frame_fraction"]) * occlusion_factor
        weighted_visibility += weight * visibility
        total_weight += weight
        edge_safety = 1.0 if float(projection["frame_fraction"]) >= 0.985 else max(0.0, float(projection["frame_fraction"]))
        weighted_edge_safety += weight * edge_safety
        if weight >= 2.0:
            weighted_anchor_visibility += weight * visibility
            total_anchor_weight += weight
            weighted_anchor_edge_safety += weight * edge_safety
        if projection.get("center") and visibility > 0.0:
            weighted_x += weight * visibility * float(projection["center"][0])
            weighted_y += weight * visibility * float(projection["center"][1])
            visible_weight += weight * visibility
            screen_area_sum += weight * float(projection["screen_area"])
        evidence.append({
            "id": entity.get("id"),
            "prim": entity.get("prim"),
            "weight": weight,
            "visibility": visibility,
            "occluded": bool(occluders),
            "occluders": occluders[:4],
            **projection,
        })
    visibility_score = weighted_visibility / total_weight if total_weight else 0.0
    anchor_score = weighted_anchor_visibility / total_anchor_weight if total_anchor_weight else visibility_score
    edge_score = weighted_edge_safety / total_weight if total_weight else 0.0
    anchor_edge_score = weighted_anchor_edge_safety / total_anchor_weight if total_anchor_weight else edge_score
    if visible_weight:
        center = (weighted_x / visible_weight, weighted_y / visible_weight)
        center_score = max(0.0, 1.0 - math.hypot(center[0], center[1] + 0.08) / 1.25)
    else:
        center, center_score = (0.0, 0.0), 0.0
    normalized_area = screen_area_sum / max(total_weight, 1e-9)
    fill_score = max(0.0, 1.0 - abs(normalized_area - 0.10) / 0.10)
    dominant_screen_area = max((float(row["screen_area"]) for row in evidence if row["visibility"] > 0.0), default=1.0)
    dominance_score = max(0.0, 1.0 - max(dominant_screen_area - 0.12, 0.0) / 0.28)
    occluded_weight = sum(row["weight"] for row in evidence if row["occluded"])
    occlusion_score = 1.0 - occluded_weight / max(total_weight, 1e-9)
    shell_backdrop_score = backdrop_score(eye, target, shell_side_coverage)
    composition_score = (
        0.25 * visibility_score
        + 0.17 * anchor_score
        + 0.06 * edge_score
        + 0.05 * anchor_edge_score
        + 0.08 * center_score
        + 0.05 * fill_score
        + 0.12 * occlusion_score
        + 0.14 * dominance_score
        + 0.08 * shell_backdrop_score
    )
    return {
        "status": "pass",
        "composition_score": composition_score,
        "entity_visibility_score": visibility_score,
        "anchor_visibility_score": anchor_score,
        "edge_safety_score": edge_score,
        "anchor_edge_safety_score": anchor_edge_score,
        "center_score": center_score,
        "fill_score": fill_score,
        "dominance_score": dominance_score,
        "dominant_object_screen_area": dominant_screen_area,
        "occlusion_score": occlusion_score,
        "shell_backdrop_score": shell_backdrop_score,
        "weighted_screen_center_ndc": list(center),
        "weighted_screen_area": normalized_area,
        "visible_entity_count": sum(row["visibility"] >= 0.5 for row in evidence),
        "occluded_entity_count": sum(row["occluded"] for row in evidence),
        "entity_visibility": evidence,
    }


def _world_bounds(cache: Any, prim: Any) -> Bounds | None:
    bounds = cache.ComputeWorldBound(prim).ComputeAlignedRange()
    minimum = tuple(float(bounds.GetMin()[index]) for index in range(3))
    maximum = tuple(float(bounds.GetMax()[index]) for index in range(3))
    if not all(math.isfinite(value) for value in minimum + maximum):
        return None
    if max(maximum[index] - minimum[index] for index in range(3)) <= 0.005:
        return None
    return minimum, maximum  # type: ignore[return-value]


def _author_exposure(camera_prim: Any, exposure_ev: Any, Sdf: Any) -> None:
    if exposure_ev is None:
        return
    camera_prim.SetMetadata(
        "apiSchemas",
        Sdf.TokenListOp.Create(prependedItems=["OmniRtxCameraExposureAPI_1"]),
    )
    for attribute_name, value in {
        "exposure:fStop": 5.0,
        "exposure:iso": 100.0,
        "exposure:responsivity": 1.1026709,
        "exposure:time": 0.02 * math.pow(2.0, float(exposure_ev)),
    }.items():
        camera_prim.CreateAttribute(attribute_name, Sdf.ValueTypeNames.Float, custom=False).Set(value)


def _sha256_path(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def create_prompt_camera_candidates(
    spec_path: str | Path,
    scene_path: str | Path,
    output_scene: str | Path,
    manifest_path: str | Path,
    *,
    inset_m: float = 0.45,
    eye_heights_m: tuple[float, ...] = (1.55, 2.15),
    focal_lengths_mm: tuple[float, ...] = (20.0, 24.0, 28.0),
    look_at_z_offsets_m: tuple[float, ...] = (-0.2, 0.0, 0.2),
    look_at_lateral_offsets_m: tuple[float, ...] = (-0.4, 0.0, 0.4),
    max_candidates: int = 24,
) -> dict[str, Any]:
    from pxr import Gf, Sdf, Usd, UsdGeom  # type: ignore

    if not 4 <= max_candidates <= 48:
        raise ValueError("max_candidates must be between 4 and 48")
    if not focal_lengths_mm or any(not 12.0 <= float(value) <= 60.0 for value in focal_lengths_mm):
        raise ValueError("focal lengths must be between 12 and 60 mm")
    if not look_at_z_offsets_m or any(abs(float(value)) > 0.8 for value in look_at_z_offsets_m):
        raise ValueError("look-at Z offsets must be within +/-0.8 m")
    if not look_at_lateral_offsets_m or any(abs(float(value)) > 1.2 for value in look_at_lateral_offsets_m):
        raise ValueError("look-at lateral offsets must be within +/-1.2 m")
    spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
    input_path = Path(scene_path).expanduser()
    stage_in = Usd.Stage.Open(str(input_path))
    if not stage_in:
        raise ValueError(f"Unable to open scene: {input_path}")
    cache = UsdGeom.BBoxCache(Usd.TimeCode.Default(), [UsdGeom.Tokens.default_, UsdGeom.Tokens.render])
    entities = []
    for entity in spec.get("entities") or []:
        if not isinstance(entity, dict) or entity.get("asset_scope") in {"scene_root", "scene_layout_component", "external_feature"}:
            continue
        prim = stage_in.GetPrimAtPath(str(entity.get("prim") or entity.get("source_prim_path") or ""))
        bounds = _world_bounds(cache, prim) if prim else None
        if bounds:
            entities.append({"id": entity.get("id"), "prim": str(prim.GetPath()), "bounds": bounds, "weight": _entity_weight(entity)})
    if not entities:
        raise ValueError("No visible object bounds are available for camera planning")
    architectural = []
    structural = []
    for root_path in ("/World/Architecture", "/World/Colliders"):
        root_prim = stage_in.GetPrimAtPath(root_path)
        if not root_prim:
            continue
        for prim in Usd.PrimRange(root_prim):
            if prim == root_prim or not prim.IsA(UsdGeom.Boundable):
                continue
            prim_bounds = _world_bounds(cache, prim)
            if prim_bounds:
                row = {"prim": str(prim.GetPath()), "bounds": prim_bounds}
                structural.append(row)
                if root_path == "/World/Architecture":
                    architectural.append(row)
    blockers = [*entities, *architectural]
    room_bounds = room_usable_bounds(spec)
    centers = [tuple((row["bounds"][0][i] + row["bounds"][1][i]) * 0.5 for i in range(3)) for row in entities]
    target = (
        median(center[0] for center in centers),
        median(center[1] for center in centers),
        min(max(median(center[2] for center in centers), room_bounds[0][2] + 0.85), room_bounds[0][2] + 1.35),
    )
    anchor_centers = [center for center, row in zip(centers, entities) if float(row.get("weight") or 0.0) >= 2.0]
    anchor_target = (
        median(center[0] for center in anchor_centers) if anchor_centers else target[0],
        median(center[1] for center in anchor_centers) if anchor_centers else target[1],
        min(max(median(center[2] for center in anchor_centers), room_bounds[0][2] + 0.85), room_bounds[0][2] + 1.35)
        if anchor_centers else target[2],
    )
    target_hypotheses: list[tuple[str, Vec3]] = []
    for target_source, base in (("all_entities", target), ("anchors", anchor_target)):
        for offset in look_at_z_offsets_m:
            hypothesis = (base[0], base[1], min(max(base[2] + float(offset), room_bounds[0][2] + 0.65), room_bounds[1][2] - 0.45))
            if not any(math.dist(hypothesis, existing) < 0.04 for _, existing in target_hypotheses):
                target_hypotheses.append((target_source, hypothesis))
    camera_spec = spec.get("camera") or {}
    resolution = camera_spec.get("render_resolution") or [1280, 720]
    aspect_ratio = float(camera_spec.get("target_aspect_ratio") or (float(resolution[0]) / max(float(resolution[1]), 1.0)))
    hypotheses = [("interior", eye) for eye in perimeter_camera_positions(room_bounds, inset_m=inset_m, eye_heights_m=eye_heights_m)]
    baseline = spec.get("camera") or {}
    baseline_horizontal_aperture = float(baseline.get("horizontal_aperture") or 20.955)
    baseline_vertical_aperture = float(baseline.get("vertical_aperture") or (baseline_horizontal_aperture / aspect_ratio))
    baseline_prim = stage_in.GetPrimAtPath(str(baseline.get("prim") or ""))
    if baseline_prim and baseline_prim.IsA(UsdGeom.Camera):
        authored_aperture = UsdGeom.Camera(baseline_prim).GetHorizontalApertureAttr().Get()
        if authored_aperture is not None:
            baseline_horizontal_aperture = float(authored_aperture)
        authored_vertical_aperture = UsdGeom.Camera(baseline_prim).GetVerticalApertureAttr().Get()
        if authored_vertical_aperture is not None:
            baseline_vertical_aperture = float(authored_vertical_aperture)
    if baseline.get("translate") and baseline.get("look_at"):
        hypotheses.append(("baseline", _vec(baseline["translate"])))

    side_coverage = shell_side_coverage(room_bounds, structural)
    hypotheses.extend(("exterior_opening", eye) for eye in exterior_opening_camera_positions(room_bounds, side_coverage))
    scored = []
    for source, eye in hypotheses:
        if source == "baseline":
            variants = [("baseline", _vec(baseline["look_at"]), float(baseline.get("focal_length") or 24.0), baseline_horizontal_aperture, baseline_vertical_aperture, 0.0)]
        else:
            variants = []
            for target_source, base_target in target_hypotheses:
                horizontal = _unit((base_target[0] - eye[0], base_target[1] - eye[1], 0.0))
                right = (-horizontal[1], horizontal[0], 0.0)
                for lateral_offset in look_at_lateral_offsets_m:
                    candidate_target = (
                        base_target[0] + right[0] * float(lateral_offset),
                        base_target[1] + right[1] * float(lateral_offset),
                        base_target[2],
                    )
                    for candidate_focal in focal_lengths_mm:
                        variants.append((target_source, candidate_target, float(candidate_focal), 36.0, 36.0 / aspect_ratio, float(lateral_offset)))
        for target_source, candidate_target, candidate_focal, candidate_horizontal_aperture, candidate_vertical_aperture, lateral_offset in variants:
            result = score_camera_candidate(
                eye,
                candidate_target,
                entities,
                blockers=blockers,
                focal_length_mm=candidate_focal,
                aspect_ratio=aspect_ratio,
                horizontal_aperture_mm=candidate_horizontal_aperture,
                shell_side_coverage=side_coverage,
            )
            if result["status"] == "reject" and source != "baseline":
                continue
            scored.append({
                "source": source,
                "target_source": target_source,
                "position": eye,
                "target": candidate_target,
                "focal_length_mm": candidate_focal,
                "horizontal_aperture_mm": candidate_horizontal_aperture,
                "vertical_aperture_mm": candidate_vertical_aperture,
                "look_at_lateral_offset_m": lateral_offset,
                **result,
            })
    scored.sort(key=lambda row: (-float(row["composition_score"]), row["source"], row["position"]))
    baseline_rows = [row for row in scored if row["source"] == "baseline"]
    selected = []
    position_counts: dict[tuple[float, float, float], int] = {}
    for row in scored:
        if row["source"] == "baseline":
            continue
        key = tuple(round(float(value), 4) for value in row["position"])
        if position_counts.get(key, 0) >= 2:
            continue
        selected.append(row)
        position_counts[key] = position_counts.get(key, 0) + 1
        if len(selected) >= max_candidates - (1 if baseline_rows else 0):
            break
    if baseline_rows:
        selected.append(baseline_rows[0])
    selected.sort(key=lambda row: (-float(row["composition_score"]), row["source"], row["position"]))

    output = Path(output_scene)
    output.parent.mkdir(parents=True, exist_ok=True)
    stage = Usd.Stage.CreateNew(str(output))
    stage.GetRootLayer().subLayerPaths = [str(input_path.resolve())]
    root = UsdGeom.Xform.Define(stage, "/CameraPlanning")
    UsdGeom.Xform.Define(stage, "/CameraPlanning/Candidates")
    stage.SetDefaultPrim(root.GetPrim())
    candidates = []
    for index, row in enumerate(selected):
        candidate_id = "baseline" if row["source"] == "baseline" else "h_" + hashlib.sha256(json.dumps({
            "position": [round(float(value), 5) for value in row["position"]],
            "target": [round(float(value), 5) for value in row["target"]],
            "focal": round(float(row["focal_length_mm"]), 4),
            "aperture": round(float(row["horizontal_aperture_mm"]), 4),
        }, sort_keys=True).encode("utf-8")).hexdigest()[:10]
        camera_path = f"/CameraPlanning/Candidates/{candidate_id}"
        camera = UsdGeom.Camera.Define(stage, camera_path)
        camera.CreateFocalLengthAttr().Set(float(row["focal_length_mm"]))
        camera.CreateHorizontalApertureAttr().Set(float(row["horizontal_aperture_mm"]))
        camera.CreateVerticalApertureAttr().Set(float(row.get("vertical_aperture_mm") or (float(row["horizontal_aperture_mm"]) / aspect_ratio)))
        distance = math.sqrt(sum((row["position"][i] - row["target"][i]) ** 2 for i in range(3)))
        camera.CreateClippingRangeAttr().Set(Gf.Vec2f(max(distance * 1e-5, 0.01), max(distance * 100.0, 1000.0)))
        matrix = Gf.Matrix4d().SetLookAt(Gf.Vec3d(*row["position"]), Gf.Vec3d(*row["target"]), Gf.Vec3d(0, 0, 1)).GetInverse()
        UsdGeom.Xformable(camera.GetPrim()).MakeMatrixXform().Set(matrix)
        _author_exposure(camera.GetPrim(), camera_spec.get("exposure_ev"), Sdf)
        candidates.append({
            **row,
            "candidate_id": candidate_id,
            "camera_path": camera_path,
            "render_filename": f"{candidate_id}.png",
            "position": list(row["position"]),
            "target": list(row["target"]),
            "target_aspect_ratio": aspect_ratio,
        })
    stage.GetRootLayer().Save()
    manifest = {
        "schema_version": "isaac-scene-prompt-camera-candidates/v2",
        "status": "pass" if candidates else "fail",
        "spec_path": str(Path(spec_path).resolve()),
        "scene_path": str(input_path.resolve()),
        "candidate_scene_path": str(output.resolve()),
        "candidate_count": len(candidates),
        "input_fingerprints": {
            "spec_sha256": _sha256_path(spec_path),
            "scene_sha256": _sha256_path(input_path),
            "geometry_signature_sha256": hashlib.sha256(json.dumps([
                {"id": row.get("id"), "prim": row.get("prim"), "bounds": row.get("bounds")}
                for row in entities
            ], sort_keys=True).encode("utf-8")).hexdigest(),
        },
        "search_basis": {
            "room_usable_bounds_m": {"min": list(room_bounds[0]), "max": list(room_bounds[1])},
            "target": list(target),
            "entity_count": len(entities),
            "architectural_blocker_count": len(architectural),
            "inset_m": inset_m,
            "eye_heights_m": list(eye_heights_m),
            "focal_lengths_mm": [float(value) for value in focal_lengths_mm],
            "look_at_z_offsets_m": [float(value) for value in look_at_z_offsets_m],
            "look_at_lateral_offsets_m": [float(value) for value in look_at_lateral_offsets_m],
            "shell_side_coverage": side_coverage,
            "target_aspect_ratio": aspect_ratio,
            "geometry_frozen": True,
        },
        "candidates": candidates,
    }
    manifest_output = Path(manifest_path)
    manifest_output.parent.mkdir(parents=True, exist_ok=True)
    manifest_output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {**manifest, "manifest_path": str(manifest_output)}


def rank_prompt_camera_renders(
    manifest_path: str | Path,
    render_dir: str | Path,
    output_path: str | Path,
    *,
    evidence_dir: str | Path | None = None,
    visual_review_packet_path: str | Path | None = None,
    visual_agent_review_path: str | Path | None = None,
) -> dict[str, Any]:
    from isaac_scene.render_feedback import analyze_image
    from isaac_scene.visual_agent_feedback import validate_visual_agent_review

    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    root = Path(render_dir)
    evidence_root = Path(evidence_dir) if evidence_dir else None
    room_bounds = (manifest.get("search_basis") or {}).get("room_usable_bounds_m") or {}
    room_min = room_bounds.get("min") or [-1.0, -1.0, 0.0]
    room_max = room_bounds.get("max") or [1.0, 1.0, 2.0]
    room_center = [(float(room_min[index]) + float(room_max[index])) * 0.5 for index in range(2)]
    room_half = [max((float(room_max[index]) - float(room_min[index])) * 0.5, 1e-6) for index in range(2)]
    review_validation = None
    review_rows: dict[str, dict[str, Any]] = {}
    review_verdict = None
    preferred_candidate_id = None
    if visual_review_packet_path or visual_agent_review_path:
        if not visual_review_packet_path or not visual_agent_review_path:
            raise ValueError("Both visual review packet and visual agent review are required")
        review_validation = validate_visual_agent_review(visual_review_packet_path, visual_agent_review_path)
        if review_validation["status"] != "pass":
            messages = "; ".join(str(row.get("message")) for row in review_validation.get("errors") or [])
            raise ValueError(f"Visual agent review failed validation: {messages}")
        review_rows = {
            str(row.get("candidate_id")): row
            for row in review_validation.get("candidate_reviews") or []
            if isinstance(row, dict)
        }
        review_verdict = review_validation.get("verdict")
        preferred_candidate_id = review_validation.get("preferred_candidate_id")
    rows = []
    for candidate in manifest.get("candidates") or []:
        render_path = root / str(candidate.get("render_filename") or "")
        feedback = analyze_image(str(render_path), view_kind="perspective")
        pixel_visibility = None
        if evidence_root:
            evidence_path = evidence_root / render_path.stem / "visibility.json"
            if evidence_path.is_file():
                pixel_visibility = json.loads(evidence_path.read_text(encoding="utf-8"))
        metrics = feedback.get("metrics") or {}
        mean = float(metrics.get("mean_luminance") or 0.0)
        exposure_score = max(0.0, 1.0 - abs(mean - 0.32) / 0.32)
        render_status_score = {"pass": 1.0, "warn": 0.72, "fail": 0.15}.get(str(feedback.get("status")), 0.0)
        render_score = 0.70 * render_status_score + 0.30 * exposure_score
        evidence = candidate.get("entity_visibility") or []
        dominant_area = float(candidate.get("dominant_object_screen_area") or max(
            (float(row.get("screen_area") or 0.0) for row in evidence if float(row.get("visibility") or 0.0) > 0.0),
            default=1.0,
        ))
        dominance_score = float(candidate.get("dominance_score") or max(
            0.0,
            1.0 - max(dominant_area - 0.12, 0.0) / 0.28,
        ))
        naturalness_score = {"interior": 1.0, "exterior_opening": 0.78, "baseline": 0.35}.get(str(candidate.get("source")), 0.35)
        if candidate.get("source") == "interior":
            position = candidate.get("position") or room_center
            cornerness = min(
                min(abs(float(position[index]) - room_center[index]) / room_half[index], 1.0)
                for index in range(2)
            )
            view_angle_score = 0.25 + 0.75 * cornerness
        elif candidate.get("source") == "exterior_opening":
            view_angle_score = 0.80
        else:
            view_angle_score = 0.35
        shell_backdrop_score = float(candidate.get("shell_backdrop_score") if candidate.get("shell_backdrop_score") is not None else 1.0)
        composition_score = (
            0.26 * float(candidate.get("entity_visibility_score") or 0.0)
            + 0.17 * float(candidate.get("anchor_visibility_score") or 0.0)
            + 0.05 * float(candidate.get("anchor_edge_safety_score") or candidate.get("edge_safety_score") or 0.0)
            + 0.07 * float(candidate.get("center_score") or 0.0)
            + 0.05 * float(candidate.get("fill_score") or 0.0)
            + 0.11 * float(candidate.get("occlusion_score") or 0.0)
            + 0.13 * dominance_score
            + 0.05 * naturalness_score
            + 0.05 * view_angle_score
            + 0.06 * shell_backdrop_score
        )
        agent_row = review_rows.get(str(candidate.get("candidate_id")))
        agent_visual_score = float(agent_row.get("visual_score")) if agent_row else None
        pixel_utility_score = float(pixel_visibility.get("pixel_utility_score")) if pixel_visibility else None
        if pixel_utility_score is not None and agent_visual_score is not None:
            combined = 0.45 * composition_score + 0.12 * render_score + 0.28 * pixel_utility_score + 0.15 * agent_visual_score
        elif pixel_utility_score is not None:
            combined = 0.55 * composition_score + 0.15 * render_score + 0.30 * pixel_utility_score
        elif agent_visual_score is not None:
            combined = 0.70 * composition_score + 0.15 * render_score + 0.15 * agent_visual_score
        else:
            combined = 0.82 * composition_score + 0.18 * render_score
        rows.append({
            **candidate,
            "candidate_generation_score": candidate.get("composition_score"),
            "composition_score": composition_score,
            "dominance_score": dominance_score,
            "dominant_object_screen_area": dominant_area,
            "view_naturalness_score": naturalness_score,
            "view_angle_score": view_angle_score,
            "shell_backdrop_score": shell_backdrop_score,
            "visual_agent_review": agent_row,
            "visual_agent_score": agent_visual_score,
            "pixel_visibility": pixel_visibility,
            "pixel_utility_score": pixel_utility_score,
            "render_path": str(render_path),
            "render_feedback_status": feedback.get("status"),
            "render_feedback_metrics": metrics,
            "render_feedback_failures": feedback.get("failures") or [],
            "render_score": render_score,
            "combined_score": combined,
        })
    status_rank = {"pass": 0, "warn": 1, "fail": 2}
    rows.sort(key=lambda row: (
        status_rank.get(str(row["render_feedback_status"]), 3),
        -row["combined_score"],
        row["candidate_id"],
    ))
    # A preserved camera is only a comparison baseline while it is still
    # geometrically usable.  Enrichment can place a new cabinet or machine
    # around an old camera; keeping that rejected row as the safe fallback
    # would promote a view from inside the object.
    eligible_rows = [row for row in rows if row.get("status") != "reject"]
    deterministic_best = eligible_rows[0] if eligible_rows else None
    baseline_rows = [row for row in eligible_rows if row.get("source") == "baseline"]
    baseline = baseline_rows[0] if baseline_rows else None
    challenger = next((row for row in eligible_rows if row.get("source") != "baseline"), None)
    if preferred_candidate_id:
        preferred = next((row for row in eligible_rows if row.get("candidate_id") == preferred_candidate_id), None)
        if preferred and preferred.get("source") != "baseline":
            challenger = preferred
    baseline_anchor_visibility = float(baseline.get("anchor_visibility_score") or 0.0) if baseline else None
    challenger_anchor = float(challenger.get("anchor_visibility_score") or 0.0) if challenger else 0.0
    challenger_review = (challenger or {}).get("visual_agent_review") or {}
    agent_anchor_gate = bool(
        review_verdict == "accept"
        and float(challenger_review.get("recognizable_anchor_fraction") or 0.0) >= 0.80
        and float(challenger_review.get("prompt_coverage_score") or 0.0) >= 0.80
    )
    challenger_pixel = challenger.get("pixel_visibility") if challenger else None
    baseline_pixel = baseline.get("pixel_visibility") if baseline else None
    pixel_anchor_gate = bool(
        not challenger_pixel
        or float(challenger_pixel.get("recognizable_anchor_fraction") or 0.0) >= 0.65
    )
    pixel_regression_gate = bool(
        not challenger_pixel
        or not baseline_pixel
        or (
            float(challenger_pixel.get("recognizable_anchor_fraction") or 0.0)
            >= float(baseline_pixel.get("recognizable_anchor_fraction") or 0.0) - 0.10
            and int(challenger_pixel.get("anchor_edge_crop_count") or 0)
            <= int(baseline_pixel.get("anchor_edge_crop_count") or 0)
        )
    )
    # Segment/AABB visibility is deliberately conservative and can label a row of
    # visually distinct machines as mutually occluded. A validated pixel review
    # may satisfy the anchor gate, but only when it explicitly confirms both
    # recognizable anchors and prompt coverage; it never bypasses edge regression.
    anchor_gate = bool(challenger and (challenger_anchor >= 0.70 or agent_anchor_gate) and pixel_anchor_gate)
    backdrop_gate = bool(challenger and (not baseline or (
        float(challenger.get("shell_backdrop_score") or 0.0) >= float(baseline.get("shell_backdrop_score") or 0.0) - 0.08
        or (
            agent_anchor_gate
            and float(challenger.get("shell_backdrop_score") or 0.0) >= 0.80
        )
        or (
            float(challenger.get("occlusion_score") or 0.0) >= float(baseline.get("occlusion_score") or 0.0) + 0.25
            and float(challenger.get("shell_backdrop_score") or 0.0) >= 0.45
        )
    )))
    regression_gate = bool(challenger and (not baseline or (
        challenger_anchor >= float(baseline_anchor_visibility or 0.0) - 0.05
        and backdrop_gate
    )))
    score_gate = bool(challenger and (
        not baseline
        or float(challenger.get("combined_score") or 0.0) >= float(baseline.get("combined_score") or 0.0) + 0.01
        or (
            review_verdict == "accept"
            and challenger.get("visual_agent_score") is not None
            and baseline.get("visual_agent_score") is not None
            and float(challenger["visual_agent_score"]) >= float(baseline["visual_agent_score"]) + 0.05
        )
    ))
    challenger_accepted = bool(
        challenger
        and challenger.get("render_feedback_status") == "pass"
        and anchor_gate
        and regression_gate
        and pixel_regression_gate
        and score_gate
        and (review_validation is None or review_verdict == "accept")
    )
    best = challenger if challenger_accepted else baseline or deterministic_best
    accepted = bool(best and best.get("render_feedback_status") == "pass")
    decision = "accept_challenger" if challenger_accepted else "retain_baseline" if baseline else "accept_best_available"
    top_candidates = eligible_rows[:8]
    if baseline and all(row.get("candidate_id") != baseline.get("candidate_id") for row in top_candidates):
        top_candidates.append(baseline)
    result = {
        "schema_version": "isaac-scene-prompt-camera-ranking/v2",
        "status": "pass" if accepted else "warn" if best else "fail",
        "manifest_path": str(manifest_path),
        "render_dir": str(root),
        "evidence_dir": str(evidence_root) if evidence_root else None,
        "candidate_count": len(rows),
        "eligible_candidate_count": len(eligible_rows),
        "rejected_candidate_count": len(rows) - len(eligible_rows),
        "rendered_candidate_count": sum(Path(row["render_path"]).is_file() for row in rows),
        "best": best,
        "deterministic_best": deterministic_best,
        "challenger": challenger,
        "baseline": baseline,
        "top_candidates": top_candidates,
        "acceptance": {
            "accepted": accepted,
            "decision": decision,
            "challenger_accepted": challenger_accepted,
            "minimum_anchor_visibility_score": 0.70,
            "maximum_anchor_regression_from_baseline": 0.05,
            "maximum_backdrop_regression_from_baseline": 0.08,
            "minimum_score_improvement_over_baseline": 0.01,
            "baseline_anchor_visibility_score": baseline_anchor_visibility,
            "anchor_gate": anchor_gate,
            "agent_anchor_gate": agent_anchor_gate,
            "regression_gate": regression_gate,
            "backdrop_gate": backdrop_gate,
            "pixel_anchor_gate": pixel_anchor_gate,
            "pixel_regression_gate": pixel_regression_gate,
            "score_gate": score_gate,
            "requires_render_feedback_pass": True,
            "pixel_visibility_used": evidence_root is not None,
            "visual_agent_review_used": review_validation is not None,
            "visual_agent_verdict": review_verdict,
            "requires_independent_rerender": True,
            "geometry_frozen": True,
        },
        "limitations": [
            "Bounds projection and segment-AABB occlusion are deterministic broad-phase evidence, not pixel segmentation.",
            "The winner must be re-rendered alone before promotion because batch rendering can retain temporal state.",
        ],
    }
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {**result, "output": str(output)}


def confirm_prompt_camera_selection(
    manifest_path: str | Path,
    ranking_path: str | Path,
    baseline_render_path: str | Path,
    selected_render_path: str | Path,
    output_path: str | Path,
    *,
    baseline_visibility_path: str | Path | None = None,
    selected_visibility_path: str | Path | None = None,
) -> dict[str, Any]:
    from isaac_scene.render_feedback import analyze_image

    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    ranking = json.loads(Path(ranking_path).read_text(encoding="utf-8"))
    if manifest.get("schema_version") != "isaac-scene-prompt-camera-candidates/v2":
        raise ValueError("Fresh-session confirmation requires a v2 candidate manifest")
    if ranking.get("schema_version") != "isaac-scene-prompt-camera-ranking/v2":
        raise ValueError("Fresh-session confirmation requires a v2 ranking")
    selected = ranking.get("best") or {}
    baseline = ranking.get("baseline") or selected if selected.get("source") == "baseline" else ranking.get("baseline")
    selected_id = str(selected.get("candidate_id") or "")
    if not selected_id:
        raise ValueError("Ranking has no selected camera candidate")
    fingerprints = manifest.get("input_fingerprints") or {}
    current_fingerprints = {
        "spec_sha256": _sha256_path(manifest["spec_path"]),
        "scene_sha256": _sha256_path(manifest["scene_path"]),
    }
    fingerprint_match = all(current_fingerprints.get(key) == fingerprints.get(key) for key in current_fingerprints)
    baseline_feedback = analyze_image(str(baseline_render_path), view_kind="perspective")
    selected_feedback = analyze_image(str(selected_render_path), view_kind="perspective")
    baseline_visibility = json.loads(Path(baseline_visibility_path).read_text(encoding="utf-8")) if baseline_visibility_path else None
    selected_visibility = json.loads(Path(selected_visibility_path).read_text(encoding="utf-8")) if selected_visibility_path else None
    selected_is_baseline = selected.get("source") == "baseline"
    pixel_gate = bool(
        selected_is_baseline
        or not selected_visibility
        or float(selected_visibility.get("recognizable_anchor_fraction") or 0.0) >= 0.65
    )
    pixel_regression_gate = bool(
        selected_is_baseline
        or not selected_visibility
        or not baseline_visibility
        or (
            float(selected_visibility.get("recognizable_anchor_fraction") or 0.0)
            >= float(baseline_visibility.get("recognizable_anchor_fraction") or 0.0) - 0.10
            and int(selected_visibility.get("anchor_edge_crop_count") or 0)
            <= int(baseline_visibility.get("anchor_edge_crop_count") or 0)
        )
    )
    # A broken baseline is evidence for replacement, not a reason to preserve
    # it.  The challenger still has to pass fresh RGB, fingerprint, ranking and
    # pixel gates; a healthy baseline remains recorded for regression context.
    baseline_rgb_pass = baseline_feedback.get("status") == "pass"
    baseline_policy_gate = bool(selected_is_baseline or baseline_rgb_pass or selected_feedback.get("status") == "pass")
    promotion_allowed = bool(
        (ranking.get("acceptance") or {}).get("accepted")
        and fingerprint_match
        and baseline_policy_gate
        and selected_feedback.get("status") == "pass"
        and pixel_gate
        and pixel_regression_gate
    )
    decision = (
        "retain_baseline" if selected_is_baseline
        else "accept_challenger" if promotion_allowed
        else "rollback_to_baseline"
    )
    confirmed_id = selected_id if promotion_allowed else str((baseline or {}).get("candidate_id") or "baseline")
    result = {
        "schema_version": "isaac-scene-camera-selection-confirmation/v1",
        "status": "pass" if promotion_allowed else "warn",
        "decision": decision,
        "ranked_candidate_id": selected_id,
        "selected_candidate_id": confirmed_id,
        "promotion_allowed": promotion_allowed,
        "fresh_session_evidence": True,
        "input_fingerprints": fingerprints,
        "mandatory_gates": {
            "input_fingerprints_match": fingerprint_match,
            "baseline_rgb_feedback_pass": baseline_feedback.get("status") == "pass",
            "baseline_policy_gate": baseline_policy_gate,
            "selected_rgb_feedback_pass": selected_feedback.get("status") == "pass",
            "pixel_visibility_available": selected_visibility is not None,
            "pixel_anchor_gate": pixel_gate,
            "pixel_regression_gate": pixel_regression_gate,
        },
        "baseline_render_feedback": baseline_feedback,
        "selected_render_feedback": selected_feedback,
        "baseline_visibility": baseline_visibility,
        "selected_visibility": selected_visibility,
        "geometry_frozen": True,
    }
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {**result, "output": str(output)}


def promoted_camera_spec(
    spec: dict[str, Any],
    candidate: dict[str, Any],
    *,
    camera_prim: str = "/World/Cameras/AutoPerspective",
) -> dict[str, Any]:
    result = copy.deepcopy(spec)
    camera = copy.deepcopy(result.get("camera") or {})
    camera.update({
        "prim": camera_prim,
        "translate": [float(value) for value in candidate["position"]],
        "look_at": [float(value) for value in candidate["target"]],
        "focal_length": float(candidate["focal_length_mm"]),
        "horizontal_aperture": float(candidate.get("horizontal_aperture_mm") or 36.0),
        "confidence": "high",
        "evidence": "automatic indoor candidate ranking with independent render confirmation",
    })
    result["camera"] = camera
    result["camera_selection"] = {
        "schema_version": "isaac-scene-camera-selection/v1",
        "candidate_id": candidate.get("candidate_id"),
        "source": candidate.get("source"),
        "composition_score": candidate.get("composition_score"),
        "anchor_visibility_score": candidate.get("anchor_visibility_score"),
        "combined_score": candidate.get("combined_score"),
        "geometry_frozen": True,
    }
    return result


def promote_prompt_camera(
    spec_path: str | Path,
    scene_path: str | Path,
    manifest_path: str | Path,
    ranking_path: str | Path,
    output_spec: str | Path,
    output_scene: str | Path,
    *,
    candidate_id: str | None = None,
    confirmation_path: str | Path | None = None,
) -> dict[str, Any]:
    from pxr import Gf, Sdf, Usd, UsdGeom  # type: ignore

    spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    ranking = json.loads(Path(ranking_path).read_text(encoding="utf-8"))
    if ranking.get("schema_version") != "isaac-scene-prompt-camera-ranking/v2":
        raise ValueError("Camera promotion requires a v2 ranking decision")
    if not (ranking.get("acceptance") or {}).get("accepted"):
        raise ValueError("Camera promotion is not allowed by the ranking decision")
    if not confirmation_path:
        raise ValueError("Camera promotion requires fresh-session confirmation")
    confirmation = json.loads(Path(confirmation_path).read_text(encoding="utf-8"))
    if confirmation.get("schema_version") != "isaac-scene-camera-selection-confirmation/v1" or not confirmation.get("promotion_allowed"):
        raise ValueError("Camera promotion is not allowed by fresh-session confirmation")
    requested = candidate_id or confirmation.get("selected_candidate_id")
    if requested != confirmation.get("selected_candidate_id"):
        raise ValueError("Camera promotion cannot override the confirmed candidate")
    if requested != (ranking.get("best") or {}).get("candidate_id"):
        raise ValueError("Camera promotion cannot override the accepted candidate")
    candidates = {str(row.get("candidate_id")): row for row in manifest.get("candidates") or []}
    if requested not in candidates:
        raise ValueError(f"Camera candidate is unavailable: {requested}")
    candidate = {**candidates[requested], **(ranking.get("best") or {})} if requested == (ranking.get("best") or {}).get("candidate_id") else candidates[requested]
    camera_prim = "/World/Cameras/AutoPerspective"
    promoted = promoted_camera_spec(spec, candidate, camera_prim=camera_prim)
    spec_output = Path(output_spec)
    spec_output.parent.mkdir(parents=True, exist_ok=True)
    spec_output.write_text(json.dumps(promoted, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    input_scene = Path(scene_path).resolve()
    scene_output = Path(output_scene)
    scene_output.parent.mkdir(parents=True, exist_ok=True)
    stage = Usd.Stage.CreateNew(str(scene_output))
    stage.GetRootLayer().subLayerPaths = [str(input_scene)]
    world = UsdGeom.Xform.Define(stage, "/World")
    UsdGeom.Xform.Define(stage, "/World/Cameras")
    camera = UsdGeom.Camera.Define(stage, camera_prim)
    camera.CreateFocalLengthAttr().Set(float(candidate["focal_length_mm"]))
    horizontal_aperture = float(candidate.get("horizontal_aperture_mm") or 36.0)
    camera.CreateHorizontalApertureAttr().Set(horizontal_aperture)
    aspect_ratio = float(candidate.get("target_aspect_ratio") or (promoted.get("camera") or {}).get("target_aspect_ratio") or (16.0 / 9.0))
    camera.CreateVerticalApertureAttr().Set(float(candidate.get("vertical_aperture_mm") or (horizontal_aperture / max(aspect_ratio, 0.01))))
    eye, target = Gf.Vec3d(*candidate["position"]), Gf.Vec3d(*candidate["target"])
    distance = max(float((eye - target).GetLength()), 1.0)
    camera.CreateClippingRangeAttr().Set(Gf.Vec2f(max(distance * 1e-5, 0.01), max(distance * 100.0, 1000.0)))
    matrix = Gf.Matrix4d().SetLookAt(eye, target, Gf.Vec3d(0, 0, 1)).GetInverse()
    UsdGeom.Xformable(camera.GetPrim()).MakeMatrixXform().Set(matrix)
    exposure_ev = (promoted.get("camera") or {}).get("exposure_ev")
    _author_exposure(camera.GetPrim(), exposure_ev, Sdf)
    stage.SetDefaultPrim(world.GetPrim())
    stage.GetRootLayer().customLayerData = {
        "cameraPlanning": {
            "candidateId": str(requested),
            "inputScene": str(input_scene),
            "geometryFrozen": True,
        }
    }
    stage.GetRootLayer().Save()
    return {
        "schema_version": "isaac-scene-camera-promotion/v1",
        "status": "pass",
        "candidate_id": requested,
        "camera_path": camera_prim,
        "output_spec": str(spec_output),
        "output_scene": str(scene_output),
        "input_scene": str(input_scene),
        "geometry_frozen": True,
    }
