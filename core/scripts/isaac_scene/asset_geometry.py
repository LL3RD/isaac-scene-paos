from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Iterable


def summarize_local_geometry_points(
    points: Iterable[Iterable[float]],
    *,
    meters_per_unit: float = 1.0,
) -> dict[str, Any]:
    """Summarize render-visible points in asset-root local meter space.

    The result deliberately separates measured geometry from semantic claims.
    Face records are candidate attachment surfaces, not proof that a hole,
    connector, conveyor inlet, or robot task frame exists there.
    """
    scale = float(meters_per_unit)
    rows = [
        [float(value) * scale for value in list(point)[:3]]
        for point in points
    ]
    rows = [point for point in rows if len(point) == 3 and all(math.isfinite(value) for value in point)]
    if not rows:
        raise ValueError("render-visible geometry contains no finite points")

    minimum = [min(point[axis] for point in rows) for axis in range(3)]
    maximum = [max(point[axis] for point in rows) for axis in range(3)]
    size = [maximum[axis] - minimum[axis] for axis in range(3)]
    center = [(minimum[axis] + maximum[axis]) * 0.5 for axis in range(3)]
    bottom_tolerance = max(0.005, size[2] * 0.01)
    bottom = [point for point in rows if point[2] <= minimum[2] + bottom_tolerance]
    bottom_center = [
        (min(point[axis] for point in bottom) + max(point[axis] for point in bottom)) * 0.5
        for axis in range(2)
    ] + [minimum[2]]

    faces = {
        "x_min": {
            "axis": "X", "side": "min", "plane_m": minimum[0],
            "span_m": {"y": [minimum[1], maximum[1]], "z": [minimum[2], maximum[2]]},
            "center_m": [minimum[0], center[1], center[2]],
        },
        "x_max": {
            "axis": "X", "side": "max", "plane_m": maximum[0],
            "span_m": {"y": [minimum[1], maximum[1]], "z": [minimum[2], maximum[2]]},
            "center_m": [maximum[0], center[1], center[2]],
        },
        "y_min": {
            "axis": "Y", "side": "min", "plane_m": minimum[1],
            "span_m": {"x": [minimum[0], maximum[0]], "z": [minimum[2], maximum[2]]},
            "center_m": [center[0], minimum[1], center[2]],
        },
        "y_max": {
            "axis": "Y", "side": "max", "plane_m": maximum[1],
            "span_m": {"x": [minimum[0], maximum[0]], "z": [minimum[2], maximum[2]]},
            "center_m": [center[0], maximum[1], center[2]],
        },
    }
    return {
        "point_count": len(rows),
        "visible_bounds_m": {
            "min": minimum,
            "max": maximum,
            "size": size,
            "center": center,
        },
        "pivot_to_visible_center_m": center,
        "bottom_contact_center_m": bottom_center,
        "bottom_contact_point_count": len(bottom),
        "maximum_horizontal_radius_from_pivot_m": max(
            math.hypot(point[0], point[1]) for point in rows
        ),
        "candidate_attachment_faces": faces,
        "evidence_policy": {
            "geometry_status": "measured_render_visible_mesh",
            "semantic_status": "candidate_only_requires_agent_or_domain_verification",
        },
    }


def inspect_asset_local_geometry(
    asset_path: str,
    *,
    root_prim_path: str | None = None,
) -> dict[str, Any]:
    """Inspect visible mesh geometry relative to an asset root's local pivot."""
    from pxr import Usd, UsdGeom

    from isaac_scene.asset_entrypoint import inspect_asset_entrypoint
    from isaac_scene.support import world_mesh_points_and_triangles

    path = Path(asset_path).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(str(path))
    stage = Usd.Stage.Open(str(path))
    if stage is None:
        raise ValueError(f"could not open USD stage: {path}")
    root = stage.GetPrimAtPath(root_prim_path) if root_prim_path else stage.GetDefaultPrim()
    if not root or not root.IsValid():
        root = stage.GetPseudoRoot()
    meshes = world_mesh_points_and_triangles(
        root,
        Usd,
        UsdGeom,
        render_visible_only=True,
    )
    world_points = [point for mesh_points, _triangles in meshes for point in mesh_points]
    if not world_points:
        raise ValueError(f"no render-visible mesh points below {root.GetPath()}")

    root_to_world = UsdGeom.XformCache(Usd.TimeCode.Default()).GetLocalToWorldTransform(root)
    world_to_root = root_to_world.GetInverse()
    local_points = [world_to_root.Transform(point) for point in world_points]
    meters_per_unit = float(UsdGeom.GetStageMetersPerUnit(stage))
    summary = summarize_local_geometry_points(local_points, meters_per_unit=meters_per_unit)
    entrypoint = inspect_asset_entrypoint(path)
    return {
        "schema_version": "isaac-asset-local-geometry/v1",
        "status": "pass" if entrypoint["status"] == "pass" else "fail",
        "asset_path": str(path),
        "root_prim": str(root.GetPath()),
        "stage_meters_per_unit": meters_per_unit,
        "mesh_count": len(meshes),
        "entrypoint_compatibility": entrypoint,
        **summary,
    }
