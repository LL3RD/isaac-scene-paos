from __future__ import annotations

import math
from collections import defaultdict
from typing import Any, Iterable


Triangle2D = tuple[
    tuple[float, float],
    tuple[float, float],
    tuple[float, float],
]
Point2D = tuple[float, float]


def select_support_surface_z(
    discovered_surfaces: list[dict[str, Any]],
    *,
    bbox_max_z: float,
    requested_z: float | None = None,
) -> tuple[float, str]:
    """Choose an explicit or real horizontal support level deterministically."""
    if requested_z is not None:
        return float(requested_z), "entity_constraint"
    if discovered_surfaces:
        return (
            max(float(row["surface_z_m"]) for row in discovered_surfaces),
            "discovered_highest_horizontal_surface",
        )
    return float(bbox_max_z), "support_world_bbox_max"


def point_in_triangle_xy(
    point: tuple[float, float],
    triangle: Triangle2D,
    *,
    epsilon: float = 1e-7,
) -> bool:
    """Return whether a 2D point is inside a triangle, including its boundary."""
    (px, py) = point
    (ax, ay), (bx, by), (cx, cy) = triangle
    v0x, v0y = cx - ax, cy - ay
    v1x, v1y = bx - ax, by - ay
    v2x, v2y = px - ax, py - ay
    denominator = v0x * v1y - v1x * v0y
    if abs(denominator) <= 1e-12:
        return False
    u = (v2x * v1y - v1x * v2y) / denominator
    v = (v0x * v2y - v2x * v0y) / denominator
    return u >= -epsilon and v >= -epsilon and u + v <= 1.0 + epsilon


def mesh_support_coverage_from_points(
    contact_points: list[tuple[float, float]],
    support_triangles: list[Triangle2D],
) -> dict[str, Any]:
    """Measure contact-point coverage against real support triangles in XY."""
    supported: list[tuple[float, float]] = []
    unsupported: list[tuple[float, float]] = []
    # Reuse the solver's exact broad phase for dense CAD shelves. Keep every
    # contact point and every triangle; do not trade accuracy for sampling.
    index = None
    if len(support_triangles) >= 64:
        vertices = [point for triangle in support_triangles for point in triangle]
        span = max(
            max(p[axis] for p in vertices) - min(p[axis] for p in vertices)
            for axis in (0, 1)
        )
        cell_size = max(span * 4.0 / math.sqrt(len(support_triangles)), 1e-4)
        index = _TriangleIndex(support_triangles, cell_size)
    for point in contact_points:
        contains = index.contains(point) if index else any(
            point_in_triangle_xy(point, triangle)
            for triangle in support_triangles
        )
        bucket = supported if contains else unsupported
        bucket.append(point)
    count = len(contact_points)
    return {
        "contact_point_count": count,
        "supported_contact_point_count": len(supported),
        "unsupported_contact_point_count": len(unsupported),
        "mesh_support_coverage": (len(supported) / count) if count else 0.0,
        "unsupported_contact_point_samples": [list(point) for point in unsupported[:20]],
    }


def world_mesh_points_and_triangles(
    root_prim: Any,
    Usd: Any,
    UsdGeom: Any,
    *,
    render_visible_only: bool = False,
    coordinate_scale: float = 1.0,
) -> list[tuple[list[tuple[float, float, float]], list[tuple[int, int, int]]]]:
    """Collect world-space mesh geometry below ``root_prim``.

    ``render_visible_only`` is intended for an object's physical footprint and
    bottom-height estimate. It excludes invisible collision/proxy meshes and
    non-render purposes. In particular, it prevents non-mesh boundables such
    as embedded lights from changing the apparent bottom of an asset.
    """
    xform_cache = UsdGeom.XformCache(Usd.TimeCode.Default())
    meshes: list[tuple[list[tuple[float, float, float]], list[tuple[int, int, int]]]] = []
    # Native robot/SimReady visuals often live behind instance proxies. Read
    # composed geometry without expanding instances or changing the source.
    for prim in Usd.PrimRange(root_prim, Usd.TraverseInstanceProxies()):
        if not (prim.IsA(UsdGeom.Mesh) or prim.IsA(UsdGeom.Cube)):
            continue
        if render_visible_only:
            imageable = UsdGeom.Imageable(prim)
            if imageable and imageable.ComputeVisibility() == UsdGeom.Tokens.invisible:
                continue
            purpose = imageable.ComputePurpose() if imageable else UsdGeom.Tokens.default_
            if purpose not in (UsdGeom.Tokens.default_, UsdGeom.Tokens.render):
                continue
        matrix = xform_cache.GetLocalToWorldTransform(prim)
        # World-space winding must include both USD orientation and mirrored
        # transforms, otherwise a bottom face can masquerade as an upper face.
        reverse_winding = (
            UsdGeom.Gprim(prim).GetOrientationAttr().Get() == UsdGeom.Tokens.leftHanded
        ) != (matrix.GetDeterminant() < 0)
        if prim.IsA(UsdGeom.Cube):
            # Analytic USD cubes are real render/collision geometry (e.g. a
            # procedural floor), not a bbox fallback for an arbitrary mesh.
            from pxr import Gf
            half = float(UsdGeom.Cube(prim).GetSizeAttr().Get()) * .5
            corners = [(-1,-1,-1),(1,-1,-1),(1,1,-1),(-1,1,-1),
                       (-1,-1,1),(1,-1,1),(1,1,1),(-1,1,1)]
            authored_points = [Gf.Vec3d(x*half, y*half, z*half) for x,y,z in corners]
            triangles = [(0,2,1),(0,3,2),(4,5,6),(4,6,7),
                         (0,1,5),(0,5,4),(1,2,6),(1,6,5),
                         (2,3,7),(2,7,6),(3,0,4),(3,4,7)]
            points = [tuple(float(v) * coordinate_scale for v in matrix.TransformAffine(p)) for p in authored_points]
            if reverse_winding:
                triangles = [tuple(reversed(face)) for face in triangles]
            meshes.append((points, triangles))
            continue
        mesh = UsdGeom.Mesh(prim)
        authored_points = mesh.GetPointsAttr().Get() or []
        points = [
            tuple(float(value) * coordinate_scale for value in matrix.TransformAffine(point))
            for point in authored_points
        ]
        counts = list(mesh.GetFaceVertexCountsAttr().Get() or [])
        indices = list(mesh.GetFaceVertexIndicesAttr().Get() or [])
        triangles: list[tuple[int, int, int]] = []
        offset = 0
        for count in counts:
            face = indices[offset:offset + int(count)]
            offset += int(count)
            if len(face) < 3:
                continue
            triangles.extend(
                (int(face[0]), int(face[index]), int(face[index + 1]))
                for index in range(1, len(face) - 1)
            )
        if points and triangles:
            if reverse_winding:
                triangles = [tuple(reversed(face)) for face in triangles]
            meshes.append((points, triangles))
    return meshes


def mesh_bounds_from_world_meshes(
    meshes: list[tuple[list[tuple[float, float, float]], list[tuple[int, int, int]]]],
) -> dict[str, list[float]] | None:
    """Return an AABB row from extracted mesh points, or ``None`` if empty."""
    points = [point for mesh_points, _triangles in meshes for point in mesh_points]
    if not points:
        return None
    minimum = [min(float(point[axis]) for point in points) for axis in range(3)]
    maximum = [max(float(point[axis]) for point in points) for axis in range(3)]
    return {
        "min": minimum,
        "max": maximum,
        "size": [maximum[axis] - minimum[axis] for axis in range(3)],
        "center": [(maximum[axis] + minimum[axis]) * 0.5 for axis in range(3)],
    }


def top_support_triangles(
    support_meshes: list[tuple[list[tuple[float, float, float]], list[tuple[int, int, int]]]],
    *,
    support_surface_z: float,
    tolerance: float,
) -> list[Triangle2D]:
    """Project top-facing support geometry into XY while retaining holes and cutouts."""
    triangles: list[Triangle2D] = []
    for points, indices_rows in support_meshes:
        for indices in indices_rows:
            vertices = [points[index] for index in indices]
            if _signed_area_xy(vertices) <= 1e-9:
                continue
            if (
                min(vertex[2] for vertex in vertices) < support_surface_z - tolerance
                or max(vertex[2] for vertex in vertices) > support_surface_z + tolerance
            ):
                continue
            triangles.append(tuple((vertex[0], vertex[1]) for vertex in vertices))
    return triangles


def _signed_area_xy(vertices) -> float:
    """Positive means an upward-facing world-space triangle."""
    a, b, c = vertices
    return ((b[0] - a[0]) * (c[1] - a[1])
            - (b[1] - a[1]) * (c[0] - a[0])) * .5


def compact_support_triangles(
    triangles: list[Triangle2D],
    *,
    maximum: int = 20_000,
) -> tuple[list[Triangle2D], dict[str, Any]]:
    """Deduplicate dense coplanar tessellation before spatial indexing.

    CAD-derived shelves can contain close to 100k tiny triangles. Centimetre
    quantization preserves their macroscopic boundary and holes while keeping
    the support-search index bounded. Ordinary meshes pass through unchanged.
    """
    source_count = len(triangles)
    if source_count <= int(maximum):
        return triangles, {
            "source_triangle_count": source_count,
            "triangle_count": source_count,
            "quantization_m": 0.0,
        }
    for digits in (3, 2, 1):
        rows: dict[tuple[tuple[float, float], ...], Triangle2D] = {}
        for triangle in triangles:
            quantized = tuple((round(x, digits), round(y, digits)) for x, y in triangle)
            key = tuple(sorted(quantized))
            area = abs(
                (quantized[1][0] - quantized[0][0]) * (quantized[2][1] - quantized[0][1])
                - (quantized[1][1] - quantized[0][1]) * (quantized[2][0] - quantized[0][0])
            ) * 0.5
            if area > 1e-9:
                rows.setdefault(key, quantized)
        if len(rows) <= int(maximum):
            compacted = list(rows.values())
            return compacted, {
                "source_triangle_count": source_count,
                "triangle_count": len(compacted),
                "quantization_m": 10.0 ** (-digits),
            }
    return triangles, {
        "source_triangle_count": source_count,
        "triangle_count": source_count,
        "quantization_m": 0.0,
    }


def discover_horizontal_support_surfaces(
    support_meshes: list[tuple[list[tuple[float, float, float]], list[tuple[int, int, int]]]],
    *,
    planarity_tolerance: float = 0.003,
    cluster_tolerance: float = 0.03,
    surface_tolerance: float = 0.006,
    minimum_area_m2: float = 0.01,
) -> list[dict[str, Any]]:
    """Find distinct real horizontal support levels in a compound mesh.

    Rack shelves are often authored as one mesh, so its world AABB exposes only
    the highest shelf. This helper clusters horizontal triangles by Z, retains
    the upper face in each thin cluster, and reports auditable per-level area and
    XY bounds without inventing proxy support geometry.
    """
    # Keep only compact aggregates per quantized height.  Production rack
    # meshes can contain hundreds of thousands of triangles; retaining a 2D
    # tuple for every triangle merely to discover shelf heights can exhaust a
    # worker before the actual placement search starts.
    buckets: dict[int, dict[str, float | int]] = {}
    quantization = max(float(planarity_tolerance), 0.001)
    for points, index_rows in support_meshes:
        for indices in index_rows:
            vertices = [points[index] for index in indices]
            zs = [float(vertex[2]) for vertex in vertices]
            if max(zs) - min(zs) > float(planarity_tolerance):
                continue
            triangle = tuple((float(vertex[0]), float(vertex[1])) for vertex in vertices)
            area = _signed_area_xy(vertices)
            if area <= 1e-9:
                continue
            z = sum(zs) / 3.0
            key = round(z / quantization)
            row = buckets.setdefault(key, {
                "z_weighted": 0.0, "area_m2": 0.0, "triangle_count": 0,
                "min_x": float("inf"), "min_y": float("inf"),
                "max_x": float("-inf"), "max_y": float("-inf"),
            })
            row["z_weighted"] = float(row["z_weighted"]) + z * area
            row["area_m2"] = float(row["area_m2"]) + area
            row["triangle_count"] = int(row["triangle_count"]) + 1
            row["min_x"] = min(float(row["min_x"]), *(point[0] for point in triangle))
            row["min_y"] = min(float(row["min_y"]), *(point[1] for point in triangle))
            row["max_x"] = max(float(row["max_x"]), *(point[0] for point in triangle))
            row["max_y"] = max(float(row["max_y"]), *(point[1] for point in triangle))
    if not buckets:
        return []
    candidates = []
    for row in buckets.values():
        area = float(row["area_m2"])
        # A narrow bevel must not define the top of a broad load surface.
        # Apply the existing area threshold to candidate planes as well as
        # the final level, rather than promoting tiny isolated crests.
        if area < float(minimum_area_m2):
            continue
        candidates.append({
            **row,
            "z": float(row["z_weighted"]) / area,
        })
    clusters: list[list[dict[str, float | int]]] = []
    for row in sorted(candidates, key=lambda item: float(item["z"])):
        # Bound the whole cluster span. Adjacent-height chaining can connect
        # bevels/braces from floor to rack top and swallow every real shelf.
        if not clusters or float(row["z"]) - float(clusters[-1][0]["z"]) > float(cluster_tolerance):
            clusters.append([row])
        else:
            clusters[-1].append(row)
    surfaces = []
    for cluster in clusters:
        surface_z = max(float(row["z"]) for row in cluster)
        top = [row for row in cluster if abs(float(row["z"]) - surface_z) <= float(surface_tolerance)]
        area = sum(float(row["area_m2"]) for row in top)
        if area < float(minimum_area_m2):
            continue
        surfaces.append({
            "surface_z_m": float(surface_z),
            "area_m2": float(area),
            "triangle_count": sum(int(row["triangle_count"]) for row in top),
            "bounds_xy": [
                min(float(row["min_x"]) for row in top),
                min(float(row["min_y"]) for row in top),
                max(float(row["max_x"]) for row in top),
                max(float(row["max_y"]) for row in top),
            ],
        })
    return surfaces


def _rotate(point: Point2D, center: Point2D, degrees: float) -> Point2D:
    angle = math.radians(degrees)
    cosine, sine = math.cos(angle), math.sin(angle)
    dx, dy = point[0] - center[0], point[1] - center[1]
    return (
        center[0] + cosine * dx - sine * dy,
        center[1] + sine * dx + cosine * dy,
    )


def _normalize_degrees(value: float) -> float:
    return (float(value) + 180.0) % 360.0 - 180.0


def _sample_points(points: Iterable[Point2D], maximum: int = 96) -> list[Point2D]:
    unique = sorted({(round(float(x), 6), round(float(y), 6)) for x, y in points})
    if len(unique) <= maximum:
        return unique
    extrema = {
        min(unique, key=lambda point: (point[0], point[1])),
        max(unique, key=lambda point: (point[0], point[1])),
        min(unique, key=lambda point: (point[1], point[0])),
        max(unique, key=lambda point: (point[1], point[0])),
    }
    remaining = [point for point in unique if point not in extrema]
    budget = max(0, maximum - len(extrema))
    if not budget:
        return sorted(extrema)[:maximum]
    step = len(remaining) / budget
    sampled = [remaining[min(int(index * step), len(remaining) - 1)] for index in range(budget)]
    return sorted(extrema | set(sampled))


class _TriangleIndex:
    """Small dependency-free spatial index for repeated point-in-mesh queries."""

    def __init__(self, triangles: list[Triangle2D], cell_size: float) -> None:
        self.triangles = triangles
        self.cell_size = max(float(cell_size), 1e-4)
        self.bins: dict[tuple[int, int], list[int]] = defaultdict(list)
        for index, triangle in enumerate(triangles):
            xs = [point[0] for point in triangle]
            ys = [point[1] for point in triangle]
            # Match point_in_triangle_xy's barycentric edge tolerance even
            # when an almost-boundary point falls in the adjacent grid cell.
            pad = max(max(xs)-min(xs), max(ys)-min(ys)) * 2e-7
            ix0, ix1 = self._cell(min(xs)-pad), self._cell(max(xs)+pad)
            iy0, iy1 = self._cell(min(ys)-pad), self._cell(max(ys)+pad)
            for ix in range(ix0, ix1 + 1):
                for iy in range(iy0, iy1 + 1):
                    self.bins[(ix, iy)].append(index)

    def _cell(self, value: float) -> int:
        return math.floor(value / self.cell_size)

    def contains(self, point: Point2D) -> bool:
        return any(
            point_in_triangle_xy(point, self.triangles[index])
            for index in self.bins.get((self._cell(point[0]), self._cell(point[1])), [])
        )


def _coverage(points: list[Point2D], index: _TriangleIndex) -> float:
    return sum(index.contains(point) for point in points) / len(points) if points else 0.0


def _expanded_points(points: list[Point2D], margin: float) -> list[Point2D]:
    if margin <= 0:
        return points
    offsets = (
        (-margin, -margin), (-margin, 0.0), (-margin, margin),
        (0.0, -margin), (0.0, 0.0), (0.0, margin),
        (margin, -margin), (margin, 0.0), (margin, margin),
    )
    return [(x + dx, y + dy) for x, y in points for dx, dy in offsets]


def _bounds(points: list[Point2D]) -> tuple[float, float, float, float]:
    return (
        min(point[0] for point in points),
        min(point[1] for point in points),
        max(point[0] for point in points),
        max(point[1] for point in points),
    )


def _overlaps_obstacle(
    bounds: tuple[float, float, float, float],
    obstacles: list[dict[str, Any]],
    clearance: float,
) -> bool:
    min_x, min_y, max_x, max_y = bounds
    for obstacle in obstacles:
        lower = obstacle.get("min") or []
        upper = obstacle.get("max") or []
        if len(lower) < 2 or len(upper) < 2:
            continue
        if (
            max_x > float(lower[0]) - clearance
            and min_x < float(upper[0]) + clearance
            and max_y > float(lower[1]) - clearance
            and min_y < float(upper[1]) + clearance
        ):
            return True
    return False


def _candidate_centers(
    triangles: list[Triangle2D],
    preferred: Point2D,
    *,
    grid_step: float,
    maximum: int,
) -> list[Point2D]:
    vertices = [point for triangle in triangles for point in triangle]
    min_x, min_y, max_x, max_y = _bounds(vertices)
    width, height = max_x - min_x, max_y - min_y
    requested_step = max(float(grid_step), 1e-3)
    area_step = math.sqrt(max(width * height, 1e-9) / max(maximum, 1))
    step = max(requested_step, area_step)
    values: set[Point2D] = {(round(preferred[0], 6), round(preferred[1], 6))}
    values.update(
        (
            round(sum(point[0] for point in triangle) / 3.0, 6),
            round(sum(point[1] for point in triangle) / 3.0, 6),
        )
        for triangle in triangles
    )
    x = min_x
    while x <= max_x + 1e-9:
        y = min_y
        while y <= max_y + 1e-9:
            values.add((round(x, 6), round(y, 6)))
            y += step
        x += step
    return sorted(
        values,
        key=lambda point: ((point[0] - preferred[0]) ** 2 + (point[1] - preferred[1]) ** 2, point),
    )[:maximum]


def solve_mesh_support_placement(
    *,
    object_contact_points: list[Point2D],
    object_xy_points: list[Point2D],
    support_triangles: list[Triangle2D],
    current_anchor_xy: Point2D,
    preferred_xy: Point2D | None = None,
    yaw_deltas_deg: list[float] | None = None,
    current_front_vector: Point2D | None = None,
    facing_target_xy: Point2D | None = None,
    facing_target_bounds: dict[str, Any] | None = None,
    facing_target_mode: str = "nearest",
    obstacle_bounds: list[dict[str, Any]] | None = None,
    minimum_coverage: float = 0.95,
    support_margin: float = 0.01,
    obstacle_clearance: float = 0.01,
    max_preferred_distance_m: float | None = None,
    grid_step: float = 0.05,
    max_candidates: int = 4096,
) -> dict[str, Any]:
    """Find a deterministic XY/yaw correction on a real, possibly concave support mesh."""
    if not object_contact_points:
        return {"status": "fail", "reason": "object_contact_geometry_missing"}
    if not support_triangles:
        return {"status": "fail", "reason": "support_top_geometry_missing"}
    if not 0.0 < minimum_coverage <= 1.0:
        raise ValueError("minimum_coverage must be in (0, 1]")
    if facing_target_mode not in {"nearest", "center"}:
        raise ValueError("facing_target_mode must be nearest or center")
    if max_preferred_distance_m is not None and float(max_preferred_distance_m) < 0.0:
        raise ValueError("max_preferred_distance_m must be non-negative")
    sampled_contact = _sample_points(object_contact_points)
    sampled_xy = _sample_points(object_xy_points or object_contact_points, maximum=192)
    footprint_center = (
        (min(point[0] for point in sampled_contact) + max(point[0] for point in sampled_contact)) / 2.0,
        (min(point[1] for point in sampled_contact) + max(point[1] for point in sampled_contact)) / 2.0,
    )
    preferred = preferred_xy or footprint_center
    candidate_centers = _candidate_centers(
        support_triangles,
        preferred,
        grid_step=grid_step,
        maximum=max_candidates,
    )
    support_index = _TriangleIndex(support_triangles, max(grid_step, support_margin * 2.0, 0.02))
    explicit_yaws = [float(value) for value in (yaw_deltas_deg or [0.0])]
    obstacles = obstacle_bounds or []
    rejections = {"mesh_coverage": 0, "support_margin": 0, "obstacle_overlap": 0}
    evaluated = 0
    best: tuple[tuple[float, float, float, float], dict[str, Any]] | None = None
    current_front_angle = None
    if current_front_vector and math.hypot(*current_front_vector) > 1e-9:
        current_front_angle = math.degrees(math.atan2(current_front_vector[1], current_front_vector[0]))

    for center in candidate_centers:
        if (
            max_preferred_distance_m is not None
            and math.hypot(center[0] - preferred[0], center[1] - preferred[1])
            > float(max_preferred_distance_m) + 1e-9
        ):
            continue
        if facing_target_xy is not None and current_front_angle is not None:
            direction = (facing_target_xy[0] - center[0], facing_target_xy[1] - center[1])
            if math.hypot(*direction) <= 1e-9:
                continue
            desired = math.degrees(math.atan2(direction[1], direction[0]))
            yaw_options = [_normalize_degrees(desired - current_front_angle)]
        else:
            yaw_options = explicit_yaws
        for yaw_delta in yaw_options:
            evaluated += 1
            if facing_target_xy is not None and current_front_angle is not None:
                for _iteration in range(4):
                    trial_contact = [_rotate(point, current_anchor_xy, yaw_delta) for point in sampled_contact]
                    trial_center = (
                        (min(point[0] for point in trial_contact) + max(point[0] for point in trial_contact)) / 2.0,
                        (min(point[1] for point in trial_contact) + max(point[1] for point in trial_contact)) / 2.0,
                    )
                    trial_delta = (center[0] - trial_center[0], center[1] - trial_center[1])
                    trial_xy = [_rotate(point, current_anchor_xy, yaw_delta) for point in sampled_xy]
                    trial_xy = [(x + trial_delta[0], y + trial_delta[1]) for x, y in trial_xy]
                    trial_bounds = _bounds(trial_xy)
                    subject_center = (
                        (trial_bounds[0] + trial_bounds[2]) / 2.0,
                        (trial_bounds[1] + trial_bounds[3]) / 2.0,
                    )
                    target_point = facing_target_xy
                    if facing_target_mode == "nearest" and facing_target_bounds:
                        lower = facing_target_bounds.get("min") or []
                        upper = facing_target_bounds.get("max") or []
                        if len(lower) >= 2 and len(upper) >= 2:
                            target_point = (
                                max(float(lower[0]), min(subject_center[0], float(upper[0]))),
                                max(float(lower[1]), min(subject_center[1], float(upper[1]))),
                            )
                            if math.hypot(target_point[0] - subject_center[0], target_point[1] - subject_center[1]) <= 1e-9:
                                target_point = facing_target_xy
                    direction = (
                        target_point[0] - subject_center[0],
                        target_point[1] - subject_center[1],
                    )
                    if math.hypot(*direction) <= 1e-9:
                        break
                    desired = math.degrees(math.atan2(direction[1], direction[0]))
                    updated_yaw = _normalize_degrees(desired - current_front_angle)
                    if abs(_normalize_degrees(updated_yaw - yaw_delta)) <= 1e-6:
                        yaw_delta = updated_yaw
                        break
                    yaw_delta = updated_yaw
            rotated_contact = [_rotate(point, current_anchor_xy, yaw_delta) for point in sampled_contact]
            rotated_center = (
                (min(point[0] for point in rotated_contact) + max(point[0] for point in rotated_contact)) / 2.0,
                (min(point[1] for point in rotated_contact) + max(point[1] for point in rotated_contact)) / 2.0,
            )
            delta = (center[0] - rotated_center[0], center[1] - rotated_center[1])
            transformed_contact = [(x + delta[0], y + delta[1]) for x, y in rotated_contact]
            coverage = _coverage(transformed_contact, support_index)
            if coverage + 1e-9 < minimum_coverage:
                rejections["mesh_coverage"] += 1
                continue
            margin_coverage = _coverage(_expanded_points(transformed_contact, support_margin), support_index)
            if margin_coverage + 1e-9 < minimum_coverage:
                rejections["support_margin"] += 1
                continue
            rotated_xy = [_rotate(point, current_anchor_xy, yaw_delta) for point in sampled_xy]
            transformed_xy = [(x + delta[0], y + delta[1]) for x, y in rotated_xy]
            candidate_bounds = _bounds(transformed_xy)
            if _overlaps_obstacle(candidate_bounds, obstacles, obstacle_clearance):
                rejections["obstacle_overlap"] += 1
                continue
            distance = math.hypot(center[0] - preferred[0], center[1] - preferred[1])
            # Coverage is a gate above; among valid candidates, semantic intent
            # (preferred position) must outrank tiny sampling differences.
            rank = (distance, -margin_coverage, abs(_normalize_degrees(yaw_delta)), center[0] + center[1] * 1e-6)
            row = {
                "status": "pass",
                "suggested_footprint_center_xy": [center[0], center[1]],
                "suggested_world_anchor_xy": [current_anchor_xy[0] + delta[0], current_anchor_xy[1] + delta[1]],
                "suggested_world_translation_delta_xy": [delta[0], delta[1]],
                "recommended_yaw_delta_deg": _normalize_degrees(yaw_delta),
                "mesh_support_coverage": coverage,
                "support_margin_coverage": margin_coverage,
                "candidate_object_bounds_xy": list(candidate_bounds),
            }
            if best is None or rank < best[0]:
                best = (rank, row)
    if best is None:
        return {
            "status": "fail",
            "reason": "no_valid_support_placement",
            "candidate_count": len(candidate_centers),
            "evaluated_pose_count": evaluated,
            "rejections": rejections,
        }
    result = best[1]
    result.update({
        "candidate_count": len(candidate_centers),
        "evaluated_pose_count": evaluated,
        "rejections": rejections,
        "minimum_mesh_coverage": float(minimum_coverage),
        "support_margin_m": float(support_margin),
        "obstacle_clearance_m": float(obstacle_clearance),
        "max_preferred_distance_m": (
            float(max_preferred_distance_m)
            if max_preferred_distance_m is not None else None
        ),
    })
    return result


def mesh_support_contact(
    object_prim: Any,
    support_prim: Any,
    *,
    object_bottom_z: float,
    support_surface_z: float,
    tolerance: float,
    minimum_mesh_coverage: float,
    Usd: Any,
    UsdGeom: Any,
    coordinate_scale: float = 1.0,
) -> dict[str, Any]:
    object_meshes = world_mesh_points_and_triangles(
        object_prim, Usd, UsdGeom, render_visible_only=True, coordinate_scale=coordinate_scale,
    )
    support_meshes = world_mesh_points_and_triangles(
        support_prim, Usd, UsdGeom, render_visible_only=True, coordinate_scale=coordinate_scale,
    )
    contact_points = {
        (round(point[0], 6), round(point[1], 6))
        for points, _triangles in object_meshes
        for point in points
        if point[2] <= object_bottom_z + tolerance
    }
    support_triangles = top_support_triangles(
        support_meshes,
        support_surface_z=support_surface_z,
        tolerance=tolerance,
    )
    coverage = mesh_support_coverage_from_points(sorted(contact_points), support_triangles)
    coverage.update({
        "mesh_aware": True,
        "support_top_triangle_count": len(support_triangles),
        "minimum_mesh_coverage": float(minimum_mesh_coverage),
    })
    if not contact_points:
        coverage["mesh_support_status"] = "object_contact_geometry_missing"
    elif not support_triangles:
        coverage["mesh_support_status"] = "support_top_geometry_missing"
    elif coverage["mesh_support_coverage"] < minimum_mesh_coverage:
        coverage["mesh_support_status"] = "support_mesh_miss"
    else:
        coverage["mesh_support_status"] = "pass"
    return coverage
