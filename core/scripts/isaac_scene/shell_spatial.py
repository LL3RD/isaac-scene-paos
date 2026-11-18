from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any


def _empty_bounds() -> list[float]:
    return [float("inf"), float("inf"), float("inf"), float("-inf"), float("-inf"), float("-inf")]


def _extend(bounds: list[float], point: Any) -> None:
    for axis in range(3):
        value = float(point[axis])
        bounds[axis] = min(bounds[axis], value)
        bounds[axis + 3] = max(bounds[axis + 3], value)


def _finish_bounds(bounds: list[float]) -> dict[str, list[float]] | None:
    if bounds[0] == float("inf"):
        return None
    minimum = bounds[:3]
    maximum = bounds[3:]
    return {
        "min_m": minimum,
        "max_m": maximum,
        "center_m": [(minimum[i] + maximum[i]) / 2.0 for i in range(3)],
        "size_m": [maximum[i] - minimum[i] for i in range(3)],
    }


def _subtree_bounds(prim: Any, xform_cache: Any, Usd: Any, UsdGeom: Any) -> tuple[dict[str, Any] | None, int, int]:
    bounds = _empty_bounds()
    mesh_count = 0
    point_count = 0
    for descendant in Usd.PrimRange(prim):
        if not descendant.IsA(UsdGeom.Mesh):
            continue
        points = UsdGeom.Mesh(descendant).GetPointsAttr().Get() or []
        if not points:
            continue
        mesh_count += 1
        point_count += len(points)
        matrix = xform_cache.GetLocalToWorldTransform(descendant)
        for point in points:
            _extend(bounds, matrix.TransformAffine(point))
    return _finish_bounds(bounds), mesh_count, point_count


def _component_rows(scope: Any, xform_cache: Any, Usd: Any, UsdGeom: Any) -> list[dict[str, Any]]:
    rows = []
    for child in scope.GetChildren():
        bounds, mesh_count, point_count = _subtree_bounds(child, xform_cache, Usd, UsdGeom)
        if bounds is None:
            continue
        rows.append({
            "prim": str(child.GetPath()),
            "type_name": child.GetTypeName(),
            **bounds,
            "mesh_count": mesh_count,
            "point_count": point_count,
        })
    return rows


def _xy_area(row: dict[str, Any]) -> float:
    size = row["size_m"]
    return max(0.0, float(size[0])) * max(0.0, float(size[1]))


def _xy_iou(left: dict[str, Any], right: dict[str, Any]) -> float:
    left_min, left_max = left["min_m"], left["max_m"]
    right_min, right_max = right["min_m"], right["max_m"]
    width = max(0.0, min(float(left_max[0]), float(right_max[0])) - max(float(left_min[0]), float(right_min[0])))
    depth = max(0.0, min(float(left_max[1]), float(right_max[1])) - max(float(left_min[1]), float(right_min[1])))
    intersection = width * depth
    union = _xy_area(left) + _xy_area(right) - intersection
    return intersection / union if union > 0.0 else 0.0


def _room_regions(
    rows: list[dict[str, Any]],
    *,
    duplicate_iou_threshold: float,
    clearance_m: float,
) -> list[dict[str, Any]]:
    """Collapse coincident material layers into rectangular layout candidates.

    These are deliberately called regions rather than semantic rooms. A floor
    mesh is strong geometric evidence for a usable XY frame, but it is not
    sufficient evidence for labels such as kitchen or bedroom.
    """
    groups: list[list[dict[str, Any]]] = []
    for row in sorted(rows, key=lambda item: (-_xy_area(item), str(item["prim"]))):
        match = next(
            (group for group in groups if _xy_iou(row, group[0]) >= duplicate_iou_threshold),
            None,
        )
        if match is None:
            groups.append([row])
        else:
            match.append(row)

    regions = []
    for index, group in enumerate(groups, 1):
        representative = group[0]
        minimum = [float(value) for value in representative["min_m"]]
        maximum = [float(value) for value in representative["max_m"]]
        clear_min = [minimum[0] + clearance_m, minimum[1] + clearance_m]
        clear_max = [maximum[0] - clearance_m, maximum[1] - clearance_m]
        clear_size = [max(0.0, clear_max[0] - clear_min[0]), max(0.0, clear_max[1] - clear_min[1])]
        regions.append({
            "id": f"region_{index:02d}",
            "semantic_label": None,
            "floor_z_m": float(maximum[2]),
            "bounds_xy_m": [minimum[0], minimum[1], maximum[0], maximum[1]],
            "center_xy_m": [(minimum[0] + maximum[0]) / 2.0, (minimum[1] + maximum[1]) / 2.0],
            "size_xy_m": [maximum[0] - minimum[0], maximum[1] - minimum[1]],
            "area_m2": _xy_area(representative),
            "clearance_m": clearance_m,
            "clear_bounds_xy_m": [clear_min[0], clear_min[1], clear_max[0], clear_max[1]],
            "clear_size_xy_m": clear_size,
            "floor_prims": sorted(str(row["prim"]) for row in group),
            "coincident_layer_count": len(group),
        })
    return regions


def inspect_shell_spatial(
    scene_path: str | Path,
    *,
    background_prim: str = "/World/Background",
    architecture_prim: str = "/World/Background/Meshes/Base",
    floor_scope: str = "/World/Background/Meshes/Base/ground",
    door_scope: str = "/World/Background/Meshes/BaseAnimation/door",
    window_scope: str = "/World/Background/Meshes/BaseAnimation/window",
    maximum_floor_thickness_m: float = 0.25,
    minimum_floor_span_m: float = 0.25,
    duplicate_iou_threshold: float = 0.98,
    layout_clearance_m: float = 0.15,
    include_components: bool = False,
) -> dict[str, Any]:
    """Inspect an asset-backed shell using evaluated world-space mesh points."""
    from pxr import Usd, UsdGeom  # type: ignore

    path = Path(scene_path)
    stage = Usd.Stage.Open(str(path))
    if not stage:
        raise ValueError(f"Unable to open USD stage: {path}")
    xform_cache = UsdGeom.XformCache(Usd.TimeCode.Default())

    def scope_rows(scope_path: str) -> tuple[Any, list[dict[str, Any]]]:
        prim = stage.GetPrimAtPath(scope_path)
        if not prim:
            return prim, []
        return prim, _component_rows(prim, xform_cache, Usd, UsdGeom)

    background = stage.GetPrimAtPath(background_prim)
    background_bounds, background_meshes, background_points = (
        _subtree_bounds(background, xform_cache, Usd, UsdGeom) if background else (None, 0, 0)
    )
    architecture = stage.GetPrimAtPath(architecture_prim)
    architecture_bounds, architecture_meshes, architecture_points = (
        _subtree_bounds(architecture, xform_cache, Usd, UsdGeom) if architecture else (None, 0, 0)
    )
    floor_prim, floor_components = scope_rows(floor_scope)
    door_prim, door_components = scope_rows(door_scope)
    window_prim, window_components = scope_rows(window_scope)
    floor_candidates = [
        row for row in floor_components
        if row["size_m"][2] <= maximum_floor_thickness_m
        and row["size_m"][0] >= minimum_floor_span_m
        and row["size_m"][1] >= minimum_floor_span_m
    ]
    levels = Counter(round(float(row["max_m"][2]), 2) for row in floor_candidates)
    selected_level = levels.most_common(1)[0][0] if levels else None
    selected = [row for row in floor_candidates if round(float(row["max_m"][2]), 2) == selected_level]
    regions = _room_regions(
        selected,
        duplicate_iou_threshold=duplicate_iou_threshold,
        clearance_m=layout_clearance_m,
    )

    def summary(prim: Any, rows: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "scope": str(prim.GetPath()) if prim else None,
            "scope_valid": bool(prim),
            "component_count": len(rows),
            "mesh_count": sum(int(row["mesh_count"]) for row in rows),
            "point_count": sum(int(row["point_count"]) for row in rows),
        }

    result = {
        "schema_version": "isaac-architectural-shell-spatial-audit/v2",
        "status": "pass" if architecture_bounds and regions else "pending",
        "scene_path": str(path),
        "stage_meters_per_unit": float(UsdGeom.GetStageMetersPerUnit(stage)),
        "background": {
            "prim": background_prim,
            "bounds": background_bounds,
            "mesh_count": background_meshes,
            "point_count": background_points,
        },
        "architecture": {
            "prim": architecture_prim,
            "bounds": architecture_bounds,
            "mesh_count": architecture_meshes,
            "point_count": architecture_points,
        },
        "floor": {
            **summary(floor_prim, floor_components),
            "candidate_count": len(floor_candidates),
            "level_counts": {str(key): value for key, value in sorted(levels.items())},
            "selected_floor_level_m": selected_level,
            "selected_component_count": len(selected),
            "selected_component_bounds": [
                {key: row[key] for key in ("prim", "min_m", "max_m", "center_m", "size_m")}
                for row in selected
            ],
            "duplicate_iou_threshold": duplicate_iou_threshold,
            "room_region_count": len(regions),
            "room_regions": regions,
        },
        "doors": summary(door_prim, door_components),
        "windows": summary(window_prim, window_components),
    }
    if include_components:
        result["floor"]["components"] = floor_components
        result["doors"]["components"] = door_components
        result["windows"]["components"] = window_components
    return result
