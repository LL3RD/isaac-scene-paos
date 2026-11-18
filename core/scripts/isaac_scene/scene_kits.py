from __future__ import annotations

import json
import math
import re
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "isaac-synthesis28-scene-kit-catalog/v1"
FORBIDDEN_POSE_FIELDS = {
    "xform_json",
    "metadata_pose_json",
    "xform_matrix",
    "source_world_matrix",
    "regression_alignment",
    "regression_instance_opinions",
}

SCENE_FAMILIES: list[tuple[str, tuple[str, ...], tuple[str, ...]]] = [
    ("laboratory", ("laboratory", "lab"), ("laboratory", "lab", "实验室", "实验台", "实验")),
    ("medical_clinical", ("consulting_room", "operating_room", "ward"), ("clinic", "clinical", "hospital", "consulting", "operating room", "ward", "诊室", "门诊", "医院", "病房", "手术室")),
    ("warehouse_logistics", ("logistics_warehousing",), ("warehouse", "logistics", "packing", "storage", "仓库", "物流", "打包", "货架")),
    ("industrial_assembly", ("factory", "workshop", "assembly", "car_factory"), ("factory", "industrial", "assembly", "workshop", "工厂", "工业", "车间", "装配")),
    ("public_transit", ("subway_station",), ("subway", "station", "metro", "地铁", "车站")),
    ("retail_store", ("store",), ("store", "retail", "shop", "商店", "零售")),
    ("education_scene", ("kindergarten", "children_room"), ("kindergarten", "children", "classroom", "幼儿园", "儿童", "教室")),
    ("home_scene", ("living_room", "dining_room", "kitchen", "bedroom", "parlor"), ("living room", "dining", "kitchen", "bedroom", "home", "客厅", "餐厅", "厨房", "卧室", "住宅")),
    ("generic_scene", ("office", "meeting_room", "demo"), ("office", "meeting", "workplace", "办公室", "会议室", "办公")),
]


def _tokens(value: Any) -> set[str]:
    text = str(value or "").lower().replace("_", " ")
    latin = set(re.findall(r"[a-z0-9]+", text))
    chinese = {
        phrase
        for _domain, _slugs, aliases in SCENE_FAMILIES
        for phrase in aliases
        if re.search(r"[\u4e00-\u9fff]", phrase) and phrase in text
    }
    return latin | chinese


def _family_for_slug(slug: str) -> tuple[str, list[str]]:
    normalized = slug.lower()
    for domain, slug_terms, aliases in SCENE_FAMILIES:
        if any(term in normalized for term in slug_terms):
            return domain, sorted(set(aliases) | {slug.replace("_", " ")})
    return "generic_scene", [slug.replace("_", " ")]


def _size_tier(scope: str, bbox_json: str | None) -> str:
    if scope not in {"independent_object", "robot"}:
        return "architecture"
    try:
        size = [float(value) for value in json.loads(bbox_json or "{}").get("size", [])]
    except (TypeError, ValueError, json.JSONDecodeError):
        return "unknown"
    if len(size) != 3 or not all(math.isfinite(value) and value >= 0 for value in size):
        return "unknown"
    horizontal = max(size[:2])
    if horizontal >= 1.2 or size[2] >= 1.5:
        return "anchor"
    if horizontal >= 0.28 or size[2] >= 0.35:
        return "functional"
    return "detail"


def _load_environment_metrics(path: str | Path | None) -> dict[str, dict[str, Any]]:
    if not path:
        return {}
    source = Path(path)
    if not source.is_file():
        return {}
    payload = json.loads(source.read_text(encoding="utf-8"))
    return {
        str(row.get("slug")): {
            "prim_count": int(row.get("prim_count") or 0),
            "mesh_count": int(row.get("mesh_count") or 0),
            "used_layer_count": int((row.get("isaac_validation_report") or {}).get("used_layer_count") or 0),
            "room_bbox_m": ((row.get("isaac_validation_report") or {}).get("bbox") or {}).get("size"),
        }
        for row in payload.get("assets") or []
        if isinstance(row, dict) and row.get("slug")
    }


def build_catalog(
    object_catalog: str | Path,
    *,
    environment_catalog: str | Path | None = None,
) -> dict[str, Any]:
    """Distill pose-free composition signatures from the Synthesis object catalog."""
    connection = sqlite3.connect(str(object_catalog))
    connection.row_factory = sqlite3.Row
    metrics = _load_environment_metrics(environment_catalog)
    try:
        rows = connection.execute(
            """SELECT i.scene_slug, i.model_id, i.semantic_name, i.asset_scope,
                      o.logical_id, o.canonical_class, o.subtype, o.callable,
                      o.qa_status, o.wrapper_path, o.bbox_m_json,
                      o.front_axis, o.front_axis_status, o.review_report_json,
                      o.license_status, o.allowed_use_scope, o.attribution_text
               FROM instances i JOIN objects o ON o.model_id = i.model_id
               ORDER BY i.scene_slug, i.model_id"""
        ).fetchall()
    finally:
        connection.close()

    grouped: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        grouped[str(row["scene_slug"])].append(row)

    scenes: list[dict[str, Any]] = []
    for slug in sorted(grouped):
        scene_rows = grouped[slug]
        domain, aliases = _family_for_slug(slug)
        per_model: dict[str, dict[str, Any]] = {}
        class_counts: Counter[str] = Counter()
        semantic_counts: Counter[str] = Counter()
        tier_counts: Counter[str] = Counter()
        for row in scene_rows:
            model_id = str(row["model_id"])
            canonical_class = str(row["canonical_class"] or "object/other")
            semantic_name = str(row["semantic_name"] or "object")
            scope = str(row["asset_scope"] or "unclassified")
            class_counts[canonical_class] += 1
            semantic_counts[semantic_name] += 1
            tier_counts[_size_tier(scope, row["bbox_m_json"])] += 1
            try:
                review = json.loads(row["review_report_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                review = {}
            item = per_model.setdefault(model_id, {
                "model_id": model_id,
                "logical_id": str(row["logical_id"] or f"synthesis28_objects:{model_id}"),
                "semantic_name": semantic_name,
                "canonical_class": canonical_class,
                "subtype": str(row["subtype"] or ""),
                "asset_scope": scope,
                "instance_count": 0,
                "callable": bool(row["callable"]),
                "qa_status": str(row["qa_status"] or ""),
                "wrapper_path": str(row["wrapper_path"] or ""),
                "bbox_m": json.loads(row["bbox_m_json"] or "null"),
                "front_axis": row["front_axis"],
                "front_axis_status": str(row["front_axis_status"] or ""),
                "orientation_mode": review.get("orientation_mode"),
                "support_mode": review.get("support_mode"),
                "mount_anchor_prim": review.get("mount_anchor_prim"),
                "license_status": str(row["license_status"] or ""),
                "allowed_use_scope": row["allowed_use_scope"],
                "attribution_text": row["attribution_text"],
            })
            item["instance_count"] += 1

        assets = sorted(
            per_model.values(),
            key=lambda item: (
                item["asset_scope"] not in {"independent_object", "robot"},
                not item["callable"],
                -item["instance_count"],
                item["semantic_name"],
                item["model_id"],
            ),
        )
        scenes.append({
            "id": f"synthesis28:{slug}",
            "slug": slug,
            "title": slug.replace("_", " ").title(),
            "domain": domain,
            "room_types": sorted({slug, slug.rstrip("0123456789"), *[term for term in slug.split("_") if len(term) > 2]}),
            "aliases": aliases,
            "source_policy": {
                "pose_access": "forbidden",
                "complete_scene_insertion": "forbidden",
                "allowed_evidence": ["semantic_composition", "asset_family", "density", "environment_complexity"],
            },
            "metrics": {
                "instance_count": len(scene_rows),
                "unique_model_count": len(per_model),
                "callable_unique_model_count": sum(item["callable"] for item in assets),
                "callable_instance_count": sum(item["instance_count"] for item in assets if item["callable"]),
                "density_tiers": dict(sorted(tier_counts.items())),
                **metrics.get(slug, {}),
            },
            "semantic_composition": [
                {"canonical_class": key, "instance_count": count}
                for key, count in class_counts.most_common()
            ],
            "semantic_names": [
                {"semantic_name": key, "instance_count": count}
                for key, count in semantic_counts.most_common()
            ],
            "assets": assets,
        })

    payload = {
        "schema_version": SCHEMA_VERSION,
        "source": "synthesis28_object_catalog_pose_free_distillation",
        "scene_count": len(scenes),
        "scenes": scenes,
    }
    forbidden = sorted(key for key in FORBIDDEN_POSE_FIELDS if key in json.dumps(payload, sort_keys=True))
    if forbidden:
        raise ValueError(f"scene-kit catalog leaked pose fields: {forbidden}")
    return payload


def load_catalog(path: str | Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"Unsupported scene-kit catalog: {path}")
    return payload


def search_catalog(
    catalog: dict[str, Any],
    query: str,
    *,
    domain: str | None = None,
    room_type: str | None = None,
    topk: int = 3,
) -> list[dict[str, Any]]:
    query_tokens = _tokens(" ".join(filter(None, [query, room_type or ""])))
    ranked: list[tuple[float, str, dict[str, Any]]] = []
    for scene in catalog.get("scenes") or []:
        if not isinstance(scene, dict):
            continue
        if domain and scene.get("domain") != domain:
            continue
        scene_tokens = _tokens(" ".join([
            str(scene.get("slug") or ""),
            str(scene.get("title") or ""),
            " ".join(scene.get("aliases") or []),
            " ".join(row.get("semantic_name", "") for row in scene.get("semantic_names") or []),
        ]))
        overlap = sorted(query_tokens & scene_tokens)
        score = 1.5 * len(overlap)
        if domain and scene.get("domain") == domain:
            score += 8.0
        if room_type and room_type in (scene.get("room_types") or []):
            score += 10.0
        elif room_type and _tokens(room_type) & scene_tokens:
            score += 4.0
        score += min(float((scene.get("metrics") or {}).get("callable_unique_model_count") or 0), 10.0) * 0.05
        if score <= 0:
            continue
        callable_assets = [
            item for item in scene.get("assets") or []
            if item.get("callable") and item.get("asset_scope") in {"independent_object", "robot"}
        ]
        ranked.append((score, str(scene.get("slug") or ""), {
            "scene_id": scene.get("id"),
            "slug": scene.get("slug"),
            "domain": scene.get("domain"),
            "score": round(score, 4),
            "matched_terms": overlap,
            "metrics": scene.get("metrics") or {},
            "semantic_composition": (scene.get("semantic_composition") or [])[:12],
            "callable_asset_candidates": callable_assets[:20],
            "source_policy": scene.get("source_policy") or {},
        }))
    ranked.sort(key=lambda row: (-row[0], row[1]))
    return [row[2] for row in ranked[:max(1, int(topk))]]


def context_for_brief(catalog: dict[str, Any], brief: dict[str, Any], *, topk: int = 3) -> dict[str, Any]:
    inputs = brief.get("inputs") if isinstance(brief.get("inputs"), dict) else {}
    room = brief.get("room") if isinstance(brief.get("room"), dict) else {}
    matches = search_catalog(
        catalog,
        str(inputs.get("text") or ""),
        domain=str(brief.get("domain") or "") or None,
        room_type=str(room.get("type") or "") or None,
        topk=topk,
    )
    return {
        "schema_version": "isaac-scene-kit-retrieval/v1",
        "source": "synthesis28_pose_free_scene_signatures",
        "pose_access": "forbidden",
        "matches": matches,
    }
