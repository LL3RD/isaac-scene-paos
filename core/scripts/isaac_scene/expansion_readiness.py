"""Classify pose-free Synthesis scene kits for controlled prompt-only expansion."""

from __future__ import annotations

import json
import math
from collections import Counter
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "isaac-scene-synthesis28-expansion-readiness/v1"
ARCHITECTURE_PREFIX = "architecture/"
MULTI_ROOM_TOKENS = ("apartment", "two_bedroom")
GENERATION_ANCHOR_PREFIXES = (
    "electronics/computer",
    "furniture/bed",
    "furniture/seating/",
    "furniture/storage/",
    "furniture/surface/",
    "industrial/",
    "laboratory/",
    "logistics/vehicle",
    "medical/",
    "transport/",
)
DOMAIN_ALIASES = {
    "industrial": {"industrial_assembly"},
    "warehouse": {"warehouse_logistics"},
}


def _size(asset: dict[str, Any]) -> list[float]:
    values = ((asset.get("bbox_m") or {}).get("size") or [])
    try:
        size = [float(value) for value in values]
    except (TypeError, ValueError):
        return []
    return size if len(size) == 3 and all(math.isfinite(value) and value >= 0 for value in size) else []


def _is_architecture(asset: dict[str, Any]) -> bool:
    return str(asset.get("canonical_class") or "").startswith(ARCHITECTURE_PREFIX) or str(
        asset.get("asset_scope") or ""
    ) in {"environment", "architecture"}


def _is_anchor(asset: dict[str, Any]) -> bool:
    size = _size(asset)
    semantic_class = str(asset.get("canonical_class") or "")
    return bool(
        semantic_class.startswith(GENERATION_ANCHOR_PREFIXES)
        or (size and (max(size[:2]) >= 1.2 or size[2] >= 1.5))
    )


def _evidence_coverage(assets: list[dict[str, Any]], field: str, accepted: set[str]) -> float:
    if not assets:
        return 0.0
    return sum(str(asset.get(field) or "").lower() in accepted for asset in assets) / len(assets)


def _complexity(scene: dict[str, Any], non_architecture_instances: int) -> tuple[str, list[str], list[str]]:
    slug = str(scene.get("slug") or "")
    room_size = ((scene.get("metrics") or {}).get("room_bbox_m") or [])
    reasons: list[str] = []
    reference_scale_risks: list[str] = []
    if any(token in slug for token in MULTI_ROOM_TOKENS):
        reasons.append("multi_room_topology")
    if non_architecture_instances > 120:
        reasons.append("mass_repetition")
    try:
        if len(room_size) >= 2 and max(float(room_size[0]), float(room_size[1])) > 60.0:
            reference_scale_risks.append("large_source_extent")
    except (TypeError, ValueError):
        pass
    if reasons:
        return "scale_or_topology_blocked", reasons, reference_scale_risks
    if non_architecture_instances > 60:
        return "high", ["dense_scene"], reference_scale_risks
    if non_architecture_instances > 30:
        return "medium", ["moderate_scene_complexity"], reference_scale_risks
    return ("medium" if reference_scale_risks else "low"), [], reference_scale_risks


def _audit_scene(scene: dict[str, Any]) -> dict[str, Any]:
    assets = [asset for asset in scene.get("assets") or [] if isinstance(asset, dict) and not _is_architecture(asset)]
    callable_assets = [asset for asset in assets if asset.get("callable")]
    anchors = [asset for asset in assets if _is_anchor(asset)]
    callable_anchors = [asset for asset in anchors if asset.get("callable")]
    non_architecture_instances = sum(int(asset.get("instance_count") or 0) for asset in assets)
    callable_instances = sum(int(asset.get("instance_count") or 0) for asset in callable_assets)
    anchor_instances = sum(int(asset.get("instance_count") or 0) for asset in anchors)
    callable_anchor_instances = sum(int(asset.get("instance_count") or 0) for asset in callable_anchors)
    instance_coverage = callable_instances / non_architecture_instances if non_architecture_instances else 0.0
    unique_coverage = len(callable_assets) / len(assets) if assets else 0.0
    anchor_coverage = callable_anchor_instances / anchor_instances if anchor_instances else 0.0
    support_coverage = _evidence_coverage(callable_assets, "support_mode", {"floor", "surface", "mounted", "free"})
    orientation_coverage = _evidence_coverage(
        callable_assets,
        "front_axis_status",
        {"verified", "not_applicable", "not_directional", "approved"},
    )
    bbox_coverage = sum(bool(_size(asset)) for asset in callable_assets) / len(callable_assets) if callable_assets else 0.0
    complexity, complexity_reasons, reference_scale_risks = _complexity(scene, non_architecture_instances)

    blockers: list[str] = []
    compact_mass_repetition = bool(
        complexity == "scale_or_topology_blocked"
        and complexity_reasons == ["mass_repetition"]
        and anchor_coverage >= 0.20
        and support_coverage >= 0.70
        and bbox_coverage == 1.0
    )
    if compact_mass_repetition:
        readiness_class = "compact_canary"
        blockers.append("source_mass_repetition_requires_bounded_generation")
    elif complexity == "scale_or_topology_blocked":
        blockers.extend(complexity_reasons)
        readiness_class = "scale_or_topology_blocked"
    elif not callable_assets or not callable_anchors:
        blockers.append("no_callable_anchor_asset")
        readiness_class = "object_catalog_blocked"
    elif instance_coverage >= 0.55 and anchor_coverage >= 0.50 and support_coverage >= 0.80 and bbox_coverage == 1.0:
        readiness_class = "ready_canary"
    elif instance_coverage >= 0.12 and anchor_coverage >= 0.20 and support_coverage >= 0.70 and bbox_coverage == 1.0:
        readiness_class = "conditional_canary"
        blockers.append("requires_cross_library_fallbacks")
    else:
        readiness_class = "object_catalog_blocked"
        if instance_coverage < 0.12:
            blockers.append("callable_instance_coverage_below_0_12")
        if anchor_coverage < 0.20:
            blockers.append("callable_anchor_coverage_below_0_20")
        if support_coverage < 0.70:
            blockers.append("support_evidence_coverage_below_0_70")
        if bbox_coverage < 1.0:
            blockers.append("bbox_evidence_incomplete")

    score = (
        0.35 * instance_coverage
        + 0.30 * anchor_coverage
        + 0.15 * support_coverage
        + 0.10 * orientation_coverage
        + 0.10 * bbox_coverage
    )
    if complexity == "high":
        score -= 0.08
    elif complexity == "scale_or_topology_blocked":
        score -= 0.25
    return {
        "slug": scene.get("slug"),
        "title": scene.get("title"),
        "domain": scene.get("domain"),
        "readiness_class": readiness_class,
        "readiness_score": round(max(0.0, min(1.0, score)), 6),
        "complexity": complexity,
        "reference_scale_risks": reference_scale_risks,
        "counts": {
            "non_architecture_instances": non_architecture_instances,
            "callable_instances": callable_instances,
            "unique_assets": len(assets),
            "callable_unique_assets": len(callable_assets),
            "anchor_instances": anchor_instances,
            "callable_anchor_instances": callable_anchor_instances,
        },
        "coverage": {
            "callable_instance": round(instance_coverage, 6),
            "callable_unique": round(unique_coverage, 6),
            "callable_anchor": round(anchor_coverage, 6),
            "support_evidence": round(support_coverage, 6),
            "orientation_evidence": round(orientation_coverage, 6),
            "bbox_evidence": round(bbox_coverage, 6),
        },
        "callable_assets": [
            {
                "model_id": asset.get("model_id"),
                "semantic_name": asset.get("semantic_name"),
                "canonical_class": asset.get("canonical_class"),
                "instance_count": asset.get("instance_count"),
                "anchor": _is_anchor(asset),
            }
            for asset in callable_assets
        ],
        "blockers": blockers,
    }


def _preferred_domains(values: list[str] | None) -> list[set[str]]:
    groups: list[set[str]] = []
    for value in values or []:
        normalized = str(value).strip().lower()
        group = set(DOMAIN_ALIASES.get(normalized, {normalized}))
        if group and group not in groups:
            groups.append(group)
    return groups


def _select_canaries(
    rows: list[dict[str, Any]], excluded: set[str], maximum: int,
    preferred_domains: list[str] | None = None,
) -> list[dict[str, Any]]:
    eligible = [
        row for row in rows
        if row["slug"] not in excluded and row["readiness_class"] in {"ready_canary", "conditional_canary", "compact_canary"}
    ]
    preference_groups = _preferred_domains(preferred_domains)

    def preference_rank(row: dict[str, Any]) -> int:
        domain = str(row.get("domain") or "").lower()
        return next((index for index, group in enumerate(preference_groups) if domain in group), len(preference_groups))

    eligible.sort(key=lambda row: (
        preference_rank(row),
        {"ready_canary": 0, "conditional_canary": 1, "compact_canary": 2}.get(row["readiness_class"], 3),
        -row["readiness_score"], row["slug"],
    ))
    selected: list[dict[str, Any]] = []
    used_domains: set[str] = set()
    for row in eligible:
        if row["domain"] in used_domains:
            continue
        selected.append(row)
        used_domains.add(str(row["domain"]))
        if len(selected) >= maximum:
            break
    for row in eligible:
        if len(selected) >= maximum:
            break
        if row not in selected:
            selected.append(row)
    return [
        {
            "slug": row["slug"],
            "domain": row["domain"],
            "readiness_class": row["readiness_class"],
            "readiness_score": row["readiness_score"],
            "policy": (
                "bounded_compact_prompt_variant_without_source_repetition"
                if row["readiness_class"] == "compact_canary"
                else "pose_free_scene_kit_plus_verified_cross_library_fallbacks"
            ),
        }
        for row in selected
    ]


def audit_catalog(
    catalog_path: str | Path,
    output_path: str | Path,
    *,
    excluded_scenes: list[str] | None = None,
    maximum_canaries: int = 5,
    preferred_domains: list[str] | None = None,
) -> dict[str, Any]:
    catalog_file = Path(catalog_path).expanduser().resolve()
    payload = json.loads(catalog_file.read_text(encoding="utf-8"))
    if payload.get("schema_version") != "isaac-synthesis28-scene-kit-catalog/v1":
        raise ValueError("Unsupported Synthesis scene-kit catalog schema")
    rows = [_audit_scene(scene) for scene in payload.get("scenes") or [] if isinstance(scene, dict)]
    rows.sort(key=lambda row: str(row["slug"]))
    classes = Counter(row["readiness_class"] for row in rows)
    canaries = _select_canaries(
        rows, set(excluded_scenes or []), max(1, int(maximum_canaries)),
        preferred_domains=preferred_domains,
    )
    result = {
        "schema_version": SCHEMA_VERSION,
        "status": "pass",
        "scene_count": len(rows),
        "class_counts": dict(sorted(classes.items())),
        "full_28_expansion_ready": bool(rows and not any(row["readiness_class"].endswith("blocked") for row in rows)),
        "recommendation": "run_controlled_canaries" if canaries else "improve_object_catalog_before_expansion",
        "selected_canaries": canaries,
        "scenes": rows,
        "policy": {
            "pose_access": "forbidden",
            "source_camera_access": "forbidden",
            "source_scene_insertion": "forbidden",
            "selection": "Honor explicit domain priority, then prefer one unseen scene per domain; do not add per-scene solver constants.",
            "preferred_domains": list(preferred_domains or []),
            "interpretation": "This audit measures same-kit object readiness, not final visual realism.",
        },
    }
    output = Path(output_path).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    result["output"] = str(output.resolve())
    return result
