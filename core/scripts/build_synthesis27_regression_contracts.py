#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable


FUNCTIONAL_PREFIXES = (
    "appliance/",
    "electronics/",
    "furniture/",
    "industrial/",
    "laboratory/equipment",
    "logistics/vehicle",
    "medical/",
    "retail/",
)
GENERATION_SCOPES = {"independent_object", "robot"}


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                value = json.loads(line)
                if isinstance(value, dict):
                    rows.append(value)
    return rows


def _snapshot_hash(paths: Iterable[Path]) -> str:
    digest = hashlib.sha256()
    for path in sorted(paths, key=lambda row: row.name):
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _domain(category: str, name: str) -> str:
    text = f"{category} {name}".lower()
    if "logistics" in text or "warehouse" in text:
        return "warehouse_logistics"
    if any(token in text for token in ("factory", "workshop", "assembly")):
        return "industrial_assembly"
    if "laboratory" in text:
        return "laboratory"
    if any(token in text for token in ("operating", "ward", "consulting")):
        return "medical_clinical"
    if "store" in text:
        return "retail_store"
    if "subway" in text:
        return "public_transit"
    if any(token in text for token in ("school", "kindergarten")):
        return "education_scene"
    if any(token in text for token in ("residence", "living", "bedroom", "kitchen", "dining")):
        return "home_scene"
    return "generic_scene"


def _is_anchor(asset: dict[str, Any]) -> bool:
    priority = str(asset.get("acquisition_priority") or "")
    canonical_class = str(asset.get("canonical_class") or "")
    if priority == "P0_anchor":
        return not canonical_class.startswith("architecture/")
    return priority == "P1_major" and canonical_class.startswith(FUNCTIONAL_PREFIXES)


def _simple_prompt(name: str, category: str, *, reference_guided: bool) -> str:
    base = (
        f"从空房间生成一个高真实度的{name}（{category}）Isaac Sim场景。"
        "只使用经过验证的真实USD资产，保证功能布局、通行空间、支撑关系、尺度和朝向合理。"
    )
    if reference_guided:
        return base + "匹配所给参考图的主要对象、空间构图、材质色调、相机视角和灯光氛围。"
    return base


def build_contract(
    *,
    catalog: dict[str, Any],
    assets: list[dict[str, Any]],
    scenes: list[dict[str, Any]],
    instances: list[dict[str, Any]],
    snapshot_hash: str,
) -> dict[str, Any]:
    asset_by_id = {str(row["model_id"]): row for row in assets}
    scene_by_slug = {str(row["scene_slug"]): row for row in scenes}
    instances_by_scene: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in instances:
        instances_by_scene[str(row["scene_slug"])].append(row)

    cases: list[dict[str, Any]] = []
    for catalog_row in catalog.get("scenes", []):
        slug = str(catalog_row["slug"])
        source_scene = scene_by_slug[slug]
        name = str(catalog_row.get("name") or source_scene.get("name") or slug)
        category = str(catalog_row.get("category") or source_scene.get("category") or "Unknown")
        scene_instances = instances_by_scene[slug]
        generated_instances = [
            row for row in scene_instances
            if str(row.get("asset_scope")) in GENERATION_SCOPES
        ]
        layout_instances = [
            row for row in scene_instances
            if str(row.get("asset_scope")) not in GENERATION_SCOPES
        ]
        model_counts = Counter(str(row["model_id"]) for row in generated_instances)
        scopes_by_model: dict[str, set[str]] = defaultdict(set)
        for row in generated_instances:
            scopes_by_model[str(row["model_id"])].add(str(row["asset_scope"]))

        exact_targets: list[dict[str, Any]] = []
        semantic_counts: Counter[str] = Counter()
        anchors: list[dict[str, Any]] = []
        excluded: list[dict[str, Any]] = []
        for model_id, count in sorted(model_counts.items()):
            asset = asset_by_id[model_id]
            canonical_class = str(asset["canonical_class"])
            semantic_counts[canonical_class] += count
            is_open_free = asset.get("is_open_free") is True
            row = {
                "model_id": model_id,
                "logical_id": str(asset["logical_id"]),
                "semantic_name": str(asset["semantic_name"]),
                "canonical_class": canonical_class,
                "subtype": str(asset["subtype"]),
                "count": count,
                "scopes": sorted(scopes_by_model[model_id]),
                "acquisition_priority": asset.get("acquisition_priority"),
                "is_open_free": is_open_free,
                "license_status": str(asset.get("license_status") or "unknown"),
                "exact_source_asset_allowed": is_open_free,
            }
            exact_targets.append(row)
            if not is_open_free:
                excluded.append({
                    "model_id": model_id,
                    "semantic_name": str(asset["semantic_name"]),
                    "count": count,
                    "required_action": "use a licensed, visually equivalent asset; do not download this source record",
                })
            if _is_anchor(asset):
                anchors.append({
                    **row,
                    "match_policy": "exact_source_asset" if is_open_free else "licensed_visual_equivalent",
                    "selection_reason": "acquisition_priority_and_functional_class",
                })

        if not anchors and exact_targets:
            fallback_candidates = [
                row for row in exact_targets
                if not str(row["canonical_class"]).startswith(
                    ("architecture/", "decor/", "food/", "small_prop")
                )
            ]
            for row in sorted(
                fallback_candidates or exact_targets,
                key=lambda item: (-int(item["count"]), str(item["canonical_class"]), str(item["model_id"])),
            )[:5]:
                anchors.append({
                    **row,
                    "match_policy": (
                        "exact_source_asset"
                        if row["exact_source_asset_allowed"]
                        else "licensed_visual_equivalent"
                    ),
                    "selection_reason": "scene_fallback_highest_instance_count",
                })

        allowed_count = sum(row["count"] for row in exact_targets if row["exact_source_asset_allowed"])
        case = {
            "id": str(catalog_row.get("id") or source_scene.get("scene_id")),
            "slug": slug,
            "name": name,
            "category": category,
            "domain": _domain(category, name),
            "generation_inputs": {
                "text_only": {
                    "text": _simple_prompt(name, category, reference_guided=False),
                },
                "reference_guided": {
                    "text": _simple_prompt(name, category, reference_guided=True),
                    "reference_image": f"{slug}/{catalog_row.get('reference_image', 'reference.jpg')}",
                },
            },
            "generator_visibility": {
                "allowed": ["selected generation_inputs mode", "skill domain pack", "callable asset catalogs"],
                "forbidden": [
                    "oracle",
                    "source scene_info/scene_data",
                    "source object poses",
                    "source USD or source scene geometry",
                ],
            },
            "expected_outputs": {
                "scene_spec": f"{slug}/candidate/scene_spec.json",
                "scene_usd": f"{slug}/candidate/scene.usda",
                "rendered_image": f"{slug}/candidate/render_perspective.png",
                "summary": f"{slug}/candidate/summary.json",
            },
            "acceptance": {
                "common": {
                    "unresolved_count_max": 0,
                    "visible_proxy_count_max": 0,
                    "geometry_validation_pass": True,
                    "overlap_validation_pass": True,
                    "support_validation_pass": True,
                    "facing_validation_pass": True,
                    "render_feedback_pass": True,
                    "anchor_real_asset_coverage_min": 1.0,
                },
                "text_only": {
                    "reference_match_is_diagnostic_only": True,
                    "semantic_anchor_coverage_min": 1.0,
                },
                "reference_guided": {
                    "reference_match_score_min": 0.72,
                    "semantic_anchor_coverage_min": 1.0,
                    "licensed_exact_anchor_coverage_min": 1.0,
                    "fixed_camera_ab_regression_required": True,
                },
            },
            "oracle": {
                "hidden_from_generator": True,
                "source_coordinate_warning": (
                    "Raw camera and object transforms are Synthesis-source metadata only; "
                    "their axes, units and Euler convention are not insertion transforms."
                ),
                "source_camera_raw": source_scene.get("camera"),
                "target_generated_instance_count": len(generated_instances),
                "licensed_exact_target_instance_count": allowed_count,
                "layout_instance_count": len(layout_instances),
                "semantic_instance_targets": [
                    {"canonical_class": key, "count": value}
                    for key, value in sorted(semantic_counts.items())
                ],
                "exact_asset_targets": exact_targets,
                "anchor_targets": anchors,
                "license_excluded_targets": excluded,
            },
            "baseline": {
                "rendered_image": f"{slug}/{catalog_row.get('rendered_image', 'agent_output/render_perspective.png')}",
                "scene_spec": f"{slug}/{catalog_row.get('scene_spec', 'agent_output/scene_spec.json')}",
                "summary": f"{slug}/{catalog_row.get('summary', 'agent_output/summary.json')}",
            },
        }
        cases.append(case)

    return {
        "schema_version": "synthesis27-regression-contract/v1",
        "name": "synthesis-27-empty-scene-generation",
        "source_snapshot_sha256": snapshot_hash,
        "source_policy": (
            "Generation receives only simple text or text plus reference image. "
            "Oracle metadata is evaluation-only and must never be passed to the generator."
        ),
        "case_count": len(cases),
        "cases": cases,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build leakage-resistant text/image regression contracts for Synthesis-27."
    )
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--library-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    paths = {
        "catalog": args.catalog,
        "assets": args.library_dir / "assets.json",
        "scenes": args.library_dir / "scenes.json",
        "instances": args.library_dir / "instances.jsonl",
    }
    for name, path in paths.items():
        if not path.is_file():
            parser.error(f"{name} not found: {path}")
    contract = build_contract(
        catalog=_read_json(paths["catalog"]),
        assets=_read_json(paths["assets"]),
        scenes=_read_json(paths["scenes"]),
        instances=_read_jsonl(paths["instances"]),
        snapshot_hash=_snapshot_hash(paths.values()),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(contract, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    summary = {
        "case_count": contract["case_count"],
        "target_instances": sum(
            row["oracle"]["target_generated_instance_count"] for row in contract["cases"]
        ),
        "anchor_targets": sum(len(row["oracle"]["anchor_targets"]) for row in contract["cases"]),
        "license_excluded_targets": sum(
            len(row["oracle"]["license_excluded_targets"]) for row in contract["cases"]
        ),
        "output": str(args.output),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
