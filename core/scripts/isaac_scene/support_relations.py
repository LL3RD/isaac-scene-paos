from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sqlite3
from statistics import median
from typing import Any

from isaac_scene.surface_index import build_scene_surface_index, refine_relations_with_surface_index


CEILING_FIXTURE_TOKENS = (
    "ceiling", "chandelier", "hanging lamp", "overhead lamp", "pendant",
    "shadowless lamp", "surgical lamp",
)


def _is_ceiling_fixture_candidate(item: dict[str, Any], floor_z: float | None) -> bool:
    """Use semantics only to nominate a mount candidate; mesh contact is still required to pass."""
    bounds = item.get("bounds") or {}
    origin = item.get("origin_world_m")
    if floor_z is None or not origin or not bounds.get("max"):
        return False
    name = str(item.get("semantic_name") or "").lower()
    if not any(token in name for token in CEILING_FIXTURE_TOKENS):
        return False
    top_z = float(bounds["max"][2])
    return abs(float(origin[2]) - top_z) <= 0.03 and float(origin[2]) >= floor_z + 1.5


def _xy_overlap(child: dict[str, Any], support: dict[str, Any]) -> tuple[float, float]:
    child_min, child_max = child["bounds"]["min"], child["bounds"]["max"]
    support_min, support_max = support["bounds"]["min"], support["bounds"]["max"]
    width = max(0.0, min(child_max[0], support_max[0]) - max(child_min[0], support_min[0]))
    depth = max(0.0, min(child_max[1], support_max[1]) - max(child_min[1], support_min[1]))
    overlap = width * depth
    child_area = max((child_max[0] - child_min[0]) * (child_max[1] - child_min[1]), 1e-9)
    return overlap, overlap / child_area


def estimate_floor_z(instances: list[dict[str, Any]]) -> float | None:
    bottoms = sorted(
        float(item["bounds"]["min"][2])
        for item in instances
        if item.get("bounds")
        and math.isfinite(float(item["bounds"]["min"][2]))
    )
    if not bottoms:
        return None
    sample_count = max(1, math.ceil(len(bottoms) * 0.2))
    lower = bottoms[:sample_count]
    bins = Counter(round(value, 2) for value in lower)
    mode, count = bins.most_common(1)[0]
    cluster = [value for value in lower if abs(value - mode) <= 0.025]
    return float(median(cluster if len(cluster) >= max(2, count // 2) else lower))


def infer_scene_support_relations(
    instances: list[dict[str, Any]],
    *,
    vertical_tolerance_m: float = 0.05,
    floor_tolerance_m: float = 0.06,
    minimum_xy_coverage: float = 0.02,
) -> dict[str, Any]:
    """Assign every scene instance a geometric support state from world AABBs."""
    usable = [item for item in instances if item.get("bounds")]
    floor_z = estimate_floor_z(usable)
    relations: list[dict[str, Any]] = []
    for child in instances:
        base = {
            "instance_id": child.get("instance_id"),
            "model_id": child.get("model_id"),
            "semantic_name": child.get("semantic_name"),
            "asset_scope": child.get("asset_scope"),
            "source_prim_path": child.get("source_prim_path"),
            "origin_world_m": child.get("origin_world_m"),
        }
        if not child.get("bounds"):
            relations.append({
                **base,
                "relation": "unresolved",
                "confidence": "none",
                "regression_status": "fail",
                "reason": "geometry_missing",
            })
            continue
        child_bottom = float(child["bounds"]["min"][2])
        if floor_z is not None and abs(child_bottom - floor_z) <= floor_tolerance_m:
            gap = child_bottom - floor_z
            relations.append({
                **base,
                "relation": "supported_by_floor",
                "support_target": "floor_plane",
                "floor_z_m": floor_z,
                "signed_gap_m": gap,
                "confidence": "high" if abs(gap) <= 0.015 else "medium",
                "regression_status": "pass",
            })
            continue
        candidates: list[dict[str, Any]] = []
        for support in usable:
            if support.get("instance_id") == child.get("instance_id"):
                continue
            support_top = float(support["bounds"]["max"][2])
            gap = child_bottom - support_top
            if gap < -vertical_tolerance_m or gap > vertical_tolerance_m:
                continue
            overlap_area, coverage = _xy_overlap(child, support)
            if overlap_area <= 0.0 or coverage < minimum_xy_coverage:
                continue
            candidates.append({
                "instance_id": support.get("instance_id"),
                "model_id": support.get("model_id"),
                "semantic_name": support.get("semantic_name"),
                "source_prim_path": support.get("source_prim_path"),
                "signed_gap_m": gap,
                "xy_overlap_area_m2": overlap_area,
                "xy_coverage": coverage,
            })
        candidates.sort(key=lambda row: (abs(row["signed_gap_m"]), -row["xy_coverage"], str(row["instance_id"])))
        if not candidates:
            if _is_ceiling_fixture_candidate(child, floor_z):
                origin = [float(value) for value in child["origin_world_m"]]
                relations.append({
                    **base,
                    "relation": "fixed_mount_candidate",
                    "mount_anchor_prim": child.get("source_prim_path"),
                    "mount_point_world_m": origin,
                    "mount_z_m": origin[2],
                    "top_z_m": float(child["bounds"]["max"][2]),
                    "confidence": "medium",
                    "regression_status": "pending",
                    "reason": "ceiling_mesh_verification_required",
                })
                continue
            relations.append({
                **base,
                "relation": "unresolved",
                "confidence": "none",
                "regression_status": "pending",
                "reason": "no_aabb_support_candidate",
                "bottom_z_m": child_bottom,
            })
            continue
        best = candidates[0]
        ambiguous = [
            row for row in candidates[1:]
            if abs(abs(row["signed_gap_m"]) - abs(best["signed_gap_m"])) <= 0.005
            and abs(row["xy_coverage"] - best["xy_coverage"]) <= 0.10
        ]
        high = (
            abs(best["signed_gap_m"]) <= 0.01
            and best["xy_coverage"] >= 0.50
            and not ambiguous
        )
        relations.append({
            **base,
            "relation": "supported_by_instance",
            "support_target": best["source_prim_path"],
            "support_instance_id": best["instance_id"],
            "support_model_id": best["model_id"],
            "support_semantic_name": best["semantic_name"],
            "signed_gap_m": best["signed_gap_m"],
            "xy_overlap_area_m2": best["xy_overlap_area_m2"],
            "xy_coverage": best["xy_coverage"],
            "candidate_count": len(candidates),
            "ambiguous_candidate_instance_ids": [row["instance_id"] for row in ambiguous[:10]],
            "confidence": "high" if high else "medium" if not ambiguous else "low",
            "regression_status": "pass",
        })
    counts = Counter(row["relation"] for row in relations)
    confidence = Counter(row["confidence"] for row in relations)
    regression = Counter(row["regression_status"] for row in relations)
    return {
        "floor_z_m": floor_z,
        "instance_count": len(instances),
        "geometry_instance_count": len(usable),
        "relation_counts": dict(sorted(counts.items())),
        "confidence_counts": dict(sorted(confidence.items())),
        "regression_counts": dict(sorted(regression.items())),
        "relations": relations,
    }


def _load_instances(database_path: str | Path) -> dict[str, list[dict[str, Any]]]:
    connection = sqlite3.connect(str(database_path))
    connection.row_factory = sqlite3.Row
    rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in connection.execute(
        """
        SELECT instance_id, scene_slug, model_id, semantic_name, asset_scope, source_prim_path
        FROM instances
        ORDER BY scene_slug, source_prim_path
        """
    ):
        rows[str(row["scene_slug"])].append(dict(row))
    connection.close()
    return rows


def _scene_bounds(scene_path: Path, instances: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from pxr import Usd, UsdGeom  # type: ignore

    stage = Usd.Stage.Open(str(scene_path))
    if not stage:
        return [{**item, "bounds": None, "geometry_error": "stage_open_failed"} for item in instances]
    cache = UsdGeom.BBoxCache(
        Usd.TimeCode.Default(),
        [UsdGeom.Tokens.default_, UsdGeom.Tokens.render, UsdGeom.Tokens.proxy],
    )
    result = []
    for item in instances:
        row = dict(item)
        prim = stage.GetPrimAtPath(str(item["source_prim_path"]))
        if not prim:
            row.update(bounds=None, geometry_error="source_prim_missing")
            result.append(row)
            continue
        aligned = cache.ComputeWorldBound(prim).ComputeAlignedRange()
        minimum, maximum = aligned.GetMin(), aligned.GetMax()
        values = [float(minimum[index]) for index in range(3)] + [float(maximum[index]) for index in range(3)]
        if not all(math.isfinite(value) for value in values):
            row.update(bounds=None, geometry_error="non_finite_bounds")
        else:
            row["bounds"] = {"min": values[:3], "max": values[3:]}
            try:
                origin = UsdGeom.Xformable(prim).ComputeLocalToWorldTransform(
                    Usd.TimeCode.Default()
                ).ExtractTranslation()
                row["origin_world_m"] = [float(origin[index]) for index in range(3)]
            except Exception:
                row["origin_world_m"] = None
        result.append(row)
    return result


def extract_synthesis28_support_relations(
    scenes_root: str | Path,
    database_path: str | Path,
    output_path: str | Path,
    *,
    vertical_tolerance_m: float = 0.05,
    floor_tolerance_m: float = 0.06,
    minimum_xy_coverage: float = 0.02,
) -> dict[str, Any]:
    """Run deterministic AABB support extraction and regression over all catalog scenes."""
    root = Path(scenes_root)
    by_scene = _load_instances(database_path)
    scene_reports = []
    totals = Counter()
    confidence_totals = Counter()
    regression_totals = Counter()
    for scene_slug, instances in sorted(by_scene.items()):
        scene_path = root / scene_slug / "scene.usd"
        bounded = _scene_bounds(scene_path, instances) if scene_path.is_file() else [
            {**item, "bounds": None, "geometry_error": "scene_missing"} for item in instances
        ]
        report = infer_scene_support_relations(
            bounded,
            vertical_tolerance_m=vertical_tolerance_m,
            floor_tolerance_m=floor_tolerance_m,
            minimum_xy_coverage=minimum_xy_coverage,
        )
        totals.update(report["relation_counts"])
        confidence_totals.update(report["confidence_counts"])
        regression_totals.update(report["regression_counts"])
        scene_reports.append({
            "scene_slug": scene_slug,
            "scene_path": str(scene_path),
            "scene_exists": scene_path.is_file(),
            **report,
        })
    instance_count = sum(row["instance_count"] for row in scene_reports)
    pending = regression_totals.get("pending", 0) + regression_totals.get("fail", 0)
    result = {
        "schema_version": "synthesis28-support-relations/v1",
        "status": "pass" if pending == 0 else "warn",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "method": "world_aabb_support_candidate_regression",
        "semantic_truth": False,
        "parameters": {
            "vertical_tolerance_m": vertical_tolerance_m,
            "floor_tolerance_m": floor_tolerance_m,
            "minimum_xy_coverage": minimum_xy_coverage,
        },
        "scenes_root": str(root),
        "instances_database": str(database_path),
        "scene_count": len(scene_reports),
        "instance_count": instance_count,
        "relation_counts": dict(sorted(totals.items())),
        "confidence_counts": dict(sorted(confidence_totals.items())),
        "regression_counts": dict(sorted(regression_totals.items())),
        "scenes": scene_reports,
    }
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        key: result[key]
        for key in (
            "schema_version", "status", "scene_count", "instance_count",
            "relation_counts", "confidence_counts", "regression_counts",
        )
    } | {"output": str(output)}


def refine_synthesis28_mesh_support_relations(
    baseline_report_path: str | Path,
    output_path: str | Path,
    cache_path: str | Path,
    *,
    scene_slugs: list[str] | None = None,
    vertical_tolerance_m: float = 0.05,
    minimum_mesh_coverage: float = 0.50,
    planarity_tolerance_m: float = 0.006,
    contact_tolerance_m: float = 0.006,
    cell_size_m: float = 0.5,
    z_step_m: float = 0.01,
    floor_plane_contact_tolerance_m: float = 0.015,
) -> dict[str, Any]:
    """Refine the complete AABB baseline with exact world-space mesh surfaces."""
    baseline_path = Path(baseline_report_path)
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    requested = set(scene_slugs or [])
    scenes = []
    totals = Counter()
    confidence_totals = Counter()
    regression_totals = Counter()
    baseline_regression_totals = Counter()
    cache_hits = 0
    for source_scene in baseline.get("scenes") or []:
        scene_slug = str(source_scene["scene_slug"])
        if requested and scene_slug not in requested:
            continue
        relations = [row for row in source_scene.get("relations") or [] if isinstance(row, dict)]
        instances = [
            {
                key: row.get(key)
                for key in (
                    "instance_id", "model_id", "semantic_name", "asset_scope", "source_prim_path",
                    "bottom_z_m",
                    "mount_z_m",
                )
            }
            for row in relations
        ]
        payload = build_scene_surface_index(
            source_scene["scene_path"],
            scene_slug,
            instances,
            cache_path=cache_path,
            planarity_tolerance_m=planarity_tolerance_m,
            contact_tolerance_m=contact_tolerance_m,
            cell_size_m=cell_size_m,
            z_step_m=z_step_m,
            query_vertical_tolerance_m=vertical_tolerance_m,
        )
        refined = refine_relations_with_surface_index(
            relations,
            payload,
            vertical_tolerance_m=vertical_tolerance_m,
            minimum_mesh_coverage=minimum_mesh_coverage,
            floor_plane_contact_tolerance_m=floor_plane_contact_tolerance_m,
        )
        cache_hits += int(bool(payload.get("cache_hit")))
        relation_counts = Counter(row["relation"] for row in refined["relations"])
        confidence_counts = Counter(str(row.get("confidence") or "none") for row in refined["relations"])
        regression_counts = Counter(str(row.get("regression_status") or "pending") for row in refined["relations"])
        baseline_regression_counts = Counter(
            str(row.get("regression_status") or "pending") for row in relations
        )
        totals.update(relation_counts)
        confidence_totals.update(confidence_counts)
        regression_totals.update(regression_counts)
        baseline_regression_totals.update(baseline_regression_counts)
        scenes.append({
            "scene_slug": scene_slug,
            "scene_path": source_scene["scene_path"],
            "instance_count": len(relations),
            "relation_counts": dict(sorted(relation_counts.items())),
            "confidence_counts": dict(sorted(confidence_counts.items())),
            "regression_counts": dict(sorted(regression_counts.items())),
            "baseline_regression_counts": dict(sorted(baseline_regression_counts.items())),
            "regression_delta": {
                "pass": regression_counts.get("pass", 0) - baseline_regression_counts.get("pass", 0),
                "pending": regression_counts.get("pending", 0) - baseline_regression_counts.get("pending", 0),
            },
            "surface_index": refined["surface_index"],
            "relations": refined["relations"],
        })
    missing = sorted(requested - {row["scene_slug"] for row in scenes})
    pending = regression_totals.get("pending", 0) + regression_totals.get("fail", 0)
    result = {
        "schema_version": "synthesis28-mesh-support-relations/v1",
        "status": "fail" if missing else "pass" if pending == 0 else "warn",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "method": "world_horizontal_mesh_surface_index",
        "semantic_truth": False,
        "baseline_report": str(baseline_path),
        "surface_cache": str(cache_path),
        "parameters": {
            "vertical_tolerance_m": vertical_tolerance_m,
            "minimum_mesh_coverage": minimum_mesh_coverage,
            "planarity_tolerance_m": planarity_tolerance_m,
            "contact_tolerance_m": contact_tolerance_m,
            "cell_size_m": cell_size_m,
            "z_step_m": z_step_m,
            "floor_plane_contact_tolerance_m": floor_plane_contact_tolerance_m,
        },
        "requested_scenes": sorted(requested),
        "missing_scenes": missing,
        "scene_count": len(scenes),
        "instance_count": sum(row["instance_count"] for row in scenes),
        "cache_hit_scene_count": cache_hits,
        "relation_counts": dict(sorted(totals.items())),
        "confidence_counts": dict(sorted(confidence_totals.items())),
        "regression_counts": dict(sorted(regression_totals.items())),
        "baseline_regression_counts": dict(sorted(baseline_regression_totals.items())),
        "regression_delta": {
            "pass": regression_totals.get("pass", 0) - baseline_regression_totals.get("pass", 0),
            "pending": regression_totals.get("pending", 0) - baseline_regression_totals.get("pending", 0),
        },
        "scenes": scenes,
    }
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        key: result[key]
        for key in (
            "schema_version", "status", "scene_count", "instance_count", "cache_hit_scene_count",
            "relation_counts", "confidence_counts", "regression_counts", "missing_scenes",
            "baseline_regression_counts", "regression_delta",
        )
    } | {"output": str(output), "surface_cache": str(cache_path)}
