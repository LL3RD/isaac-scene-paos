from __future__ import annotations

from collections import defaultdict
import gzip
import hashlib
import json
import math
from pathlib import Path
import sqlite3
from typing import Any

from isaac_scene.support import point_in_triangle_xy


class HorizontalSurfaceIndex:
    """Small dependency-free XY/Z index for world-space horizontal triangles."""

    def __init__(self, surfaces: list[dict[str, Any]], *, cell_size_m: float, z_step_m: float) -> None:
        self.surfaces = surfaces
        self.cell_size_m = max(float(cell_size_m), 0.05)
        self.z_step_m = max(float(z_step_m), 0.001)
        self.bins: dict[tuple[int, int, int], list[int]] = defaultdict(list)
        for index, surface in enumerate(surfaces):
            bounds = surface["bounds_xy"]
            z_bin = self._z_bin(float(surface["z_m"]))
            for x_bin in range(self._xy_bin(bounds[0]), self._xy_bin(bounds[2]) + 1):
                for y_bin in range(self._xy_bin(bounds[1]), self._xy_bin(bounds[3]) + 1):
                    self.bins[(z_bin, x_bin, y_bin)].append(index)

    def _xy_bin(self, value: float) -> int:
        return math.floor(float(value) / self.cell_size_m)

    def _z_bin(self, value: float) -> int:
        return round(float(value) / self.z_step_m)

    def query(
        self,
        point_xy: tuple[float, float],
        bottom_z_m: float,
        *,
        vertical_tolerance_m: float,
        exclude_owner_instance_id: str,
    ) -> list[dict[str, Any]]:
        z_min = self._z_bin(bottom_z_m - vertical_tolerance_m) - 1
        z_max = self._z_bin(bottom_z_m + vertical_tolerance_m) + 1
        x_bin, y_bin = self._xy_bin(point_xy[0]), self._xy_bin(point_xy[1])
        indices: set[int] = set()
        for z_bin in range(z_min, z_max + 1):
            indices.update(self.bins.get((z_bin, x_bin, y_bin), []))
        matches = []
        for index in indices:
            surface = self.surfaces[index]
            if surface["owner_instance_id"] == exclude_owner_instance_id:
                continue
            gap = float(bottom_z_m) - float(surface["z_m"])
            if abs(gap) > vertical_tolerance_m:
                continue
            if point_in_triangle_xy(point_xy, tuple(tuple(row) for row in surface["triangle_xy"])):
                matches.append({**surface, "signed_gap_m": gap})
        return sorted(
            matches,
            key=lambda row: (
                abs(row["signed_gap_m"]),
                -float(row["z_m"]),
                str(row["support_prim"]),
            ),
        )


def _sample_points(points: set[tuple[float, float]], maximum: int = 96) -> list[list[float]]:
    rows = sorted(points)
    if len(rows) <= maximum:
        return [[x, y] for x, y in rows]
    step = len(rows) / maximum
    return [[*rows[min(int(index * step), len(rows) - 1)]] for index in range(maximum)]


def _within_z_interest(z_m: float, interest_z_m: list[float], tolerance_m: float) -> bool:
    return not interest_z_m or any(abs(z_m - value) <= tolerance_m for value in interest_z_m)


def _scene_fingerprint(
    scene_path: Path,
    instances: list[dict[str, Any]],
    parameters: dict[str, float],
) -> str:
    stat = scene_path.stat()
    has_mount_queries = any(row.get("mount_z_m") is not None for row in instances)
    instance_evidence = sorted(
        (
            str(row.get("instance_id")),
            str(row.get("source_prim_path")),
            row.get("mount_z_m"),
        ) if has_mount_queries else (
            str(row.get("instance_id")),
            str(row.get("source_prim_path")),
        )
        for row in instances
    )
    payload = {
        "scene": str(scene_path.resolve()),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        # Keep the original cache key for scenes without mount queries. Only
        # ceiling/fixed-mount candidates require wider Z interest bands.
        "instances": instance_evidence,
        "parameters": parameters,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def _cache_connection(path: str | Path) -> sqlite3.Connection:
    cache_path = Path(path)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(cache_path))
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS scene_surface_indices (
            scene_slug TEXT PRIMARY KEY,
            fingerprint TEXT NOT NULL,
            payload_gzip BLOB NOT NULL
        )
        """
    )
    return connection


def load_cached_surface_index(
    cache_path: str | Path,
    scene_slug: str,
    fingerprint: str,
) -> dict[str, Any] | None:
    connection = _cache_connection(cache_path)
    row = connection.execute(
        "SELECT payload_gzip FROM scene_surface_indices WHERE scene_slug=? AND fingerprint=?",
        (scene_slug, fingerprint),
    ).fetchone()
    connection.close()
    if row is None:
        return None
    return json.loads(gzip.decompress(row[0]).decode("utf-8"))


def save_cached_surface_index(
    cache_path: str | Path,
    scene_slug: str,
    fingerprint: str,
    payload: dict[str, Any],
) -> None:
    blob = gzip.compress(json.dumps(payload, separators=(",", ":")).encode("utf-8"), compresslevel=6)
    connection = _cache_connection(cache_path)
    with connection:
        connection.execute(
            """
            INSERT INTO scene_surface_indices(scene_slug, fingerprint, payload_gzip)
            VALUES(?,?,?)
            ON CONFLICT(scene_slug) DO UPDATE SET
              fingerprint=excluded.fingerprint,
              payload_gzip=excluded.payload_gzip
            """,
            (scene_slug, fingerprint, blob),
        )
    connection.close()


def build_scene_surface_index(
    scene_path: str | Path,
    scene_slug: str,
    instances: list[dict[str, Any]],
    *,
    cache_path: str | Path | None = None,
    planarity_tolerance_m: float = 0.006,
    contact_tolerance_m: float = 0.006,
    minimum_triangle_area_m2: float = 1e-6,
    cell_size_m: float = 0.5,
    z_step_m: float = 0.01,
    query_vertical_tolerance_m: float = 0.05,
) -> dict[str, Any]:
    """Traverse a USD stage once and index real horizontal mesh triangles by owner."""
    from pxr import Usd, UsdGeom  # type: ignore

    path = Path(scene_path)
    parameters = {
        "planarity_tolerance_m": float(planarity_tolerance_m),
        "contact_tolerance_m": float(contact_tolerance_m),
        "minimum_triangle_area_m2": float(minimum_triangle_area_m2),
        "cell_size_m": float(cell_size_m),
        "z_step_m": float(z_step_m),
    }
    fingerprint = _scene_fingerprint(path, instances, parameters)
    if cache_path:
        cached = load_cached_surface_index(cache_path, scene_slug, fingerprint)
        if cached is not None:
            z_filter = cached.get("surface_z_filter")
            if not isinstance(z_filter, dict) or float(z_filter.get("maximum_query_tolerance_m") or 0.0) >= query_vertical_tolerance_m:
                cached["cache_hit"] = True
                return cached

    stage = Usd.Stage.Open(str(path))
    if not stage:
        raise ValueError(f"Unable to open USD stage: {path}")
    instance_by_root = {
        str(row["source_prim_path"]): {
            "instance_id": str(row["instance_id"]),
            "model_id": row.get("model_id"),
            "semantic_name": row.get("semantic_name"),
            "asset_scope": row.get("asset_scope"),
            "source_prim_path": str(row["source_prim_path"]),
        }
        for row in instances
        if row.get("instance_id") and row.get("source_prim_path")
    }
    root = stage.GetPrimAtPath("/root")
    if root:
        for child in root.GetChildren():
            child_path = str(child.GetPath())
            instance_by_root.setdefault(child_path, {
                "instance_id": f"usd:{scene_slug}:{child_path}",
                "model_id": None,
                "semantic_name": child.GetName(),
                "asset_scope": "external_feature",
                "source_prim_path": child_path,
            })

    def owner_for(prim: Any) -> dict[str, Any] | None:
        cursor = prim
        while cursor and str(cursor.GetPath()) != "/":
            row = instance_by_root.get(str(cursor.GetPath()))
            if row:
                return row
            cursor = cursor.GetParent()
        return None

    xform_cache = UsdGeom.XformCache(Usd.TimeCode.Default())
    contacts: dict[str, dict[str, Any]] = {}
    surfaces: list[dict[str, Any]] = []
    interest_z_m = sorted({
        round(float(row[key]), 6)
        for row in instances
        for key in ("bottom_z_m", "mount_z_m")
        if row.get(key) is not None and math.isfinite(float(row[key]))
    })
    interest_tolerance_m = (
        float(query_vertical_tolerance_m) + float(contact_tolerance_m) + float(planarity_tolerance_m)
    )
    mesh_count = 0
    triangle_count = 0
    for prim in stage.Traverse():
        if not prim.IsA(UsdGeom.Mesh):
            continue
        owner = owner_for(prim)
        if owner is None:
            continue
        mesh_count += 1
        mesh = UsdGeom.Mesh(prim)
        authored_points = mesh.GetPointsAttr().Get() or []
        if not authored_points:
            continue
        matrix = xform_cache.GetLocalToWorldTransform(prim)
        points = [
            tuple(float(value) for value in matrix.TransformAffine(point))
            for point in authored_points
        ]
        owner_id = str(owner["instance_id"])
        state = contacts.setdefault(owner_id, {
            "bottom_z_m": math.inf,
            "points": set(),
            "geometry_source": "bottom_mesh_vertices",
        })
        for x, y, z in points:
            if z < state["bottom_z_m"] - contact_tolerance_m:
                state["bottom_z_m"] = z
                state["points"] = set()
            if z <= state["bottom_z_m"] + contact_tolerance_m:
                state["points"].add((round(x, 5), round(y, 5)))

        counts = list(mesh.GetFaceVertexCountsAttr().Get() or [])
        indices = list(mesh.GetFaceVertexIndicesAttr().Get() or [])
        offset = 0
        for count in counts:
            face = [int(value) for value in indices[offset:offset + int(count)]]
            offset += int(count)
            if len(face) < 3:
                continue
            for face_index in range(1, len(face) - 1):
                triangle = [points[face[0]], points[face[face_index]], points[face[face_index + 1]]]
                triangle_count += 1
                zs = [point[2] for point in triangle]
                if max(zs) - min(zs) > planarity_tolerance_m:
                    continue
                surface_z_m = sum(zs) / 3.0
                if not _within_z_interest(surface_z_m, interest_z_m, interest_tolerance_m):
                    continue
                triangle_xy = [[point[0], point[1]] for point in triangle]
                area = abs(
                    (triangle_xy[1][0] - triangle_xy[0][0]) * (triangle_xy[2][1] - triangle_xy[0][1])
                    - (triangle_xy[2][0] - triangle_xy[0][0]) * (triangle_xy[1][1] - triangle_xy[0][1])
                ) / 2.0
                if area < minimum_triangle_area_m2:
                    continue
                xs = [point[0] for point in triangle_xy]
                ys = [point[1] for point in triangle_xy]
                surfaces.append({
                    "z_m": surface_z_m,
                    "triangle_xy": triangle_xy,
                    "bounds_xy": [min(xs), min(ys), max(xs), max(ys)],
                    "area_m2": area,
                    "owner_instance_id": owner_id,
                    "owner_model_id": owner.get("model_id"),
                    "owner_semantic_name": owner.get("semantic_name"),
                    "owner_asset_scope": owner.get("asset_scope"),
                    "owner_prim": owner.get("source_prim_path"),
                    "support_prim": str(prim.GetPath()),
                })
    contact_payload = {
        owner_id: {
            "bottom_z_m": float(row["bottom_z_m"]),
            "points_xy": _sample_points(row["points"]),
            "geometry_source": row["geometry_source"],
        }
        for owner_id, row in contacts.items()
        if math.isfinite(float(row["bottom_z_m"]))
    }
    payload = {
        "schema_version": "isaac-horizontal-surface-index/v1",
        "scene_slug": scene_slug,
        "scene_path": str(path),
        "fingerprint": fingerprint,
        "parameters": parameters,
        "cache_hit": False,
        "mesh_count": mesh_count,
        "triangle_count": triangle_count,
        "horizontal_triangle_count": len(surfaces),
        "contact_instance_count": len(contact_payload),
        "surfaces": surfaces,
        "contacts": contact_payload,
        "surface_z_filter": {
            "method": "relation_bottom_z_bands",
            "interest_z_count": len(interest_z_m),
            "maximum_query_tolerance_m": float(query_vertical_tolerance_m),
            "build_padding_m": interest_tolerance_m,
        } if interest_z_m else None,
    }
    if cache_path:
        save_cached_surface_index(cache_path, scene_slug, fingerprint, payload)
    return payload


def refine_relations_with_surface_index(
    relations: list[dict[str, Any]],
    payload: dict[str, Any],
    *,
    vertical_tolerance_m: float = 0.05,
    minimum_mesh_coverage: float = 0.50,
    floor_plane_contact_tolerance_m: float = 0.015,
) -> dict[str, Any]:
    index = HorizontalSurfaceIndex(
        payload.get("surfaces") or [],
        cell_size_m=float((payload.get("parameters") or {}).get("cell_size_m") or 0.5),
        z_step_m=float((payload.get("parameters") or {}).get("z_step_m") or 0.01),
    )
    contacts = payload.get("contacts") or {}
    refined = []

    def promote_floor_plane(row: dict[str, Any]) -> bool:
        source_relation = str(row.get("aabb_relation") or row.get("relation") or "")
        gap = row.get("signed_gap_m")
        if source_relation != "supported_by_floor" or gap is None:
            return False
        if abs(float(gap)) > floor_plane_contact_tolerance_m:
            return False
        row.update({
            "relation": "grounded_on_floor_plane",
            "support_target": "floor",
            "confidence": "high",
            "regression_status": "pass",
            "mesh_regression": {
                "status": "pass",
                "method": "consensus_floor_plane",
                "floor_z_m": row.get("floor_z_m"),
                "signed_gap_m": float(gap),
                "contact_tolerance_m": floor_plane_contact_tolerance_m,
                "note": "No floor mesh owner was indexed; this is strict ground-plane evidence, not mesh evidence.",
            },
        })
        return True

    for source in relations:
        row = dict(source)
        instance_id = str(row.get("instance_id") or "")
        if row.get("relation") == "fixed_mount_candidate":
            point = row.get("mount_point_world_m") or []
            matches = index.query(
                (float(point[0]), float(point[1])),
                float(point[2]),
                vertical_tolerance_m=vertical_tolerance_m,
                exclude_owner_instance_id=instance_id,
            ) if len(point) >= 3 else []
            best = matches[0] if matches else None
            if best:
                row.update({
                    "relation": "fixed_to_ceiling_mesh",
                    "support_target": best["support_prim"],
                    "support_instance_id": best["owner_instance_id"],
                    "support_model_id": best.get("owner_model_id"),
                    "support_semantic_name": best.get("owner_semantic_name"),
                    "signed_gap_m": best["signed_gap_m"],
                    "confidence": "high" if abs(float(best["signed_gap_m"])) <= 0.01 else "medium",
                    "regression_status": "pass",
                    "mount_regression": {"status": "pass", "candidate_count": len(matches), **best},
                })
            else:
                row["mount_regression"] = {
                    "status": "pending",
                    "reason": "no_ceiling_mesh_at_mount_point",
                    "vertical_tolerance_m": vertical_tolerance_m,
                }
            refined.append(row)
            continue
        contact = contacts.get(instance_id)
        if not contact or not contact.get("points_xy"):
            if promote_floor_plane(row):
                refined.append(row)
                continue
            row["mesh_regression"] = {"status": "pending", "reason": "contact_geometry_missing"}
            if row.get("regression_status") == "pass":
                row["regression_status"] = "pending"
            refined.append(row)
            continue
        bottom_z = float(contact["bottom_z_m"])
        point_matches: list[dict[str, Any]] = []
        for point in contact["points_xy"]:
            matches = index.query(
                (float(point[0]), float(point[1])),
                bottom_z,
                vertical_tolerance_m=vertical_tolerance_m,
                exclude_owner_instance_id=instance_id,
            )
            nearest_by_owner: dict[str, dict[str, Any]] = {}
            for match in matches:
                nearest_by_owner.setdefault(str(match["owner_instance_id"]), match)
            point_matches.extend(nearest_by_owner.values())
        by_owner: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for match in point_matches:
            by_owner[str(match["owner_instance_id"])].append(match)
        candidates = []
        point_count = len(contact["points_xy"])
        for _owner_id, matches in by_owner.items():
            first = matches[0]
            mesh_prims = sorted({str(match["support_prim"]) for match in matches})
            candidates.append({
                "support_instance_id": first["owner_instance_id"],
                "support_model_id": first.get("owner_model_id"),
                "support_semantic_name": first.get("owner_semantic_name"),
                "support_asset_scope": first.get("owner_asset_scope"),
                "support_owner_prim": first.get("owner_prim"),
                "support_prim": mesh_prims[0] if len(mesh_prims) == 1 else first["owner_prim"],
                "support_mesh_prims": mesh_prims,
                "supported_contact_point_count": len(matches),
                "mesh_support_coverage": len(matches) / point_count,
                "signed_gap_m": sum(float(match["signed_gap_m"]) for match in matches) / len(matches),
            })
        candidates.sort(key=lambda item: (
            -item["mesh_support_coverage"],
            abs(item["signed_gap_m"]),
            1 if item.get("support_asset_scope") in {"scene_root", "external_feature"} else 0,
            item["support_prim"],
        ))
        best = candidates[0] if candidates else None
        if best and best["mesh_support_coverage"] >= minimum_mesh_coverage:
            text = " ".join(str(best.get(key) or "").lower() for key in ("support_semantic_name", "support_prim"))
            relation = "supported_by_floor_mesh" if any(token in text for token in ("floor", "ground")) else "supported_by_mesh_surface"
            row.update({
                "relation": relation,
                "support_target": best["support_prim"],
                "support_instance_id": best["support_instance_id"],
                "support_model_id": best["support_model_id"],
                "support_semantic_name": best["support_semantic_name"],
                "signed_gap_m": best["signed_gap_m"],
                "mesh_support_coverage": best["mesh_support_coverage"],
                "contact_point_count": point_count,
                "supported_contact_point_count": best["supported_contact_point_count"],
                "confidence": "high" if best["mesh_support_coverage"] >= 0.95 and abs(best["signed_gap_m"]) <= 0.01 else "medium",
                "regression_status": "pass",
                "mesh_regression": {"status": "pass", "candidate_count": len(candidates), **best},
            })
        else:
            if promote_floor_plane(row):
                refined.append(row)
                continue
            row["mesh_regression"] = {
                "status": "pending",
                "reason": "mesh_coverage_below_threshold" if best else "no_mesh_surface_candidate",
                "minimum_mesh_coverage": minimum_mesh_coverage,
                "best_candidate": best,
            }
            if row.get("regression_status") == "pass":
                row["aabb_relation"] = row.get("relation")
                row["relation"] = "aabb_only_pending"
                row["regression_status"] = "pending"
                row["confidence"] = "low"
        refined.append(row)
    return {
        "relations": refined,
        "surface_index": {
            key: payload.get(key)
            for key in (
                "cache_hit", "mesh_count", "triangle_count", "horizontal_triangle_count",
                "contact_instance_count", "fingerprint",
            )
        },
    }
