from __future__ import annotations

import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

from .scene_kits import context_for_brief, load_catalog as load_scene_kit_catalog


DEFAULT_LIBRARY = Path(__file__).resolve().parents[2] / "config" / "enrichment_profiles.json"
DENSITY_LEVELS = {"sparse", "normal", "rich"}
LAYER_ORDER = {
    "secondary_furniture": 10,
    "soft_furnishing": 20,
    "supported_prop": 30,
    "wall_dressing": 40,
    "decor": 50,
}
GENERATION_PHASES = (
    {
        "id": "anchors",
        "sources": ["prompt_required"],
        "objective": "establish architecture and the minimum functional anchor set",
    },
    {
        "id": "functional",
        "layer_ids": ["secondary_furniture", "soft_furnishing"],
        "objective": "complete declared functional groups, access and directional relations",
    },
    {
        "id": "detail",
        "layer_ids": ["supported_prop", "wall_dressing", "decor"],
        "objective": "add supported detail within surface and negative-space budgets",
    },
)
FORBIDDEN_PROMPT_KEYS = {
    "xform_matrix",
    "regression_alignment",
    "regression_instance_opinions",
    "source_world_matrix",
}

BINDING_STATUSES = {"bound", "partial", "gap"}


def load_library(path: str | Path | None = None) -> dict[str, Any]:
    source = Path(path) if path else DEFAULT_LIBRARY
    data = json.loads(source.read_text(encoding="utf-8"))
    if data.get("schema_version") != "isaac-scene-enrichment-profile-library/v1":
        raise ValueError(f"Unsupported enrichment profile library: {source}")
    if not isinstance(data.get("density_levels"), dict) or not isinstance(data.get("profiles"), dict):
        raise ValueError(f"Incomplete enrichment profile library: {source}")
    return data


def _normalized_tokens(value: Any) -> set[str]:
    raw = str(value or "").lower().strip()
    text = re.sub(r"[^a-z0-9]+", "_", raw).strip("_")
    tail = re.sub(r"[^a-z0-9]+", "_", re.split(r"[/\\]", raw)[-1]).strip("_")
    return {item for item in (text, tail) if item}


def _item_terms(item: dict[str, Any]) -> set[str]:
    terms: set[str] = set()
    for key in ("id", "role", "semantic_class", "semantic_name"):
        terms.update(_normalized_tokens(item.get(key)))
    return terms


def _normalized_semantic_class(value: Any) -> str:
    return str(value or "").strip().lower().rstrip("/")


def _class_matches(item_class: Any, request_class: Any) -> bool:
    item = _normalized_semantic_class(item_class)
    request = _normalized_semantic_class(request_class)
    if not item or not request:
        return False
    return item == request or item.startswith(request + "/") or request.startswith(item + "/")


def _support_terms_for_item(item: dict[str, Any], objects_by_id: dict[str, dict[str, Any]]) -> set[str]:
    terms: set[str] = set()
    explicit = str(item.get("support_target_role") or "").strip()
    if explicit:
        terms.update(_normalized_tokens(explicit))
    target = str(item.get("support_target") or "").strip()
    if not target or target in {"floor", "wall", "ceiling"}:
        terms.update(_normalized_tokens(target))
        return terms
    target_id = target.rsplit("/", 1)[-1]
    candidates = [target, target_id, re.sub(r"(?<!^)(?=[A-Z])", "_", target_id).lower()]
    for candidate in candidates:
        terms.update(_normalized_tokens(candidate))
        owner = objects_by_id.get(candidate)
        if owner:
            terms.update(_normalized_tokens(owner.get("role")))
            terms.update(_normalized_tokens(owner.get("id")))
    return terms


def _matches_request(
    item: dict[str, Any],
    request: dict[str, Any],
    *,
    objects_by_id: dict[str, dict[str, Any]],
) -> bool:
    terms = _item_terms(item)
    dedupe_terms = {
        token
        for value in request.get("dedupe_terms") or []
        for token in _normalized_tokens(value)
    }
    request_terms = set(_normalized_tokens(request.get("role")))
    semantic_match = bool(terms.intersection(dedupe_terms or request_terms))
    if not semantic_match:
        semantic_match = _class_matches(item.get("semantic_class"), request.get("canonical_class"))
    if not semantic_match:
        return False

    required_support = str(request.get("support_target_role") or "").strip()
    if not required_support:
        return True
    return bool(_support_terms_for_item(item, objects_by_id).intersection(_normalized_tokens(required_support)))


def _existing_count_for_request(brief: dict[str, Any], request: dict[str, Any]) -> tuple[int, list[str]]:
    objects = [item for item in brief.get("objects") or [] if isinstance(item, dict)]
    objects_by_id: dict[str, dict[str, Any]] = {}
    for item in objects:
        item_id = str(item.get("id") or "").strip()
        if not item_id:
            continue
        objects_by_id[item_id] = item
        objects_by_id[item_id.rsplit("/", 1)[-1]] = item
        prim = str(item.get("prim") or "").strip()
        if prim:
            objects_by_id[prim] = item
            objects_by_id[prim.rsplit("/", 1)[-1]] = item

    count = 0
    matched_ids: list[str] = []
    for item in objects:
        if not _matches_request(item, request, objects_by_id=objects_by_id):
            continue
        count += max(1, int(item.get("count") or 1))
        matched_ids.append(str(item.get("id") or item.get("role") or "unnamed"))
    return count, matched_ids


def _contains_forbidden_key(value: Any) -> list[str]:
    found: set[str] = set()

    def visit(row: Any) -> None:
        if isinstance(row, dict):
            for key, child in row.items():
                if key in FORBIDDEN_PROMPT_KEYS:
                    found.add(key)
                visit(child)
        elif isinstance(row, list):
            for child in row:
                visit(child)

    visit(value)
    return sorted(found)


def _count_for_density(row: dict[str, Any], density: str) -> int:
    counts = row.get("counts") if isinstance(row.get("counts"), dict) else {}
    return max(0, int(counts.get(density) or 0))


def _budget_for_density(row: dict[str, Any], field: str, density: str) -> int:
    values = row.get(field) if isinstance(row.get(field), dict) else {}
    return max(0, int(values.get(density) or 0))


def validate_plan(plan: dict[str, Any]) -> dict[str, Any]:
    errors: list[dict[str, str]] = []
    if plan.get("schema_version") != "isaac-scene-enrichment-plan/v1":
        errors.append({"code": "ENRICHMENT_PLAN_SCHEMA", "path": "/schema_version", "message": "Expected isaac-scene-enrichment-plan/v1."})
    if plan.get("density") not in DENSITY_LEVELS:
        errors.append({"code": "ENRICHMENT_PLAN_DENSITY", "path": "/density", "message": "Unknown density level."})
    request_ids: set[str] = set()
    for layer_index, layer in enumerate(plan.get("layers") or []):
        if not isinstance(layer, dict) or not str(layer.get("id") or "").strip():
            errors.append({"code": "ENRICHMENT_PLAN_LAYER", "path": f"/layers/{layer_index}", "message": "Layer id is required."})
            continue
        for request_index, request in enumerate(layer.get("requests") or []):
            path = f"/layers/{layer_index}/requests/{request_index}"
            request_id = str(request.get("id") or "") if isinstance(request, dict) else ""
            if not request_id:
                errors.append({"code": "ENRICHMENT_PLAN_REQUEST_ID", "path": path, "message": "Request id is required."})
                continue
            if request_id in request_ids:
                errors.append({"code": "ENRICHMENT_PLAN_REQUEST_DUPLICATE", "path": path, "message": f"Duplicate request id: {request_id}"})
            request_ids.add(request_id)
            if int(request.get("count") or 0) < 1:
                errors.append({"code": "ENRICHMENT_PLAN_REQUEST_COUNT", "path": f"{path}/count", "message": "Request count must be positive."})
            if request.get("binding_status") != "asset_search_required":
                errors.append({"code": "ENRICHMENT_PLAN_BINDING_STATUS", "path": f"{path}/binding_status", "message": "New enrichment requests must remain asset_search_required until the harness binds a verified asset."})
    forbidden = _contains_forbidden_key(plan)
    if forbidden:
        errors.append({"code": "ENRICHMENT_PLAN_PRIVILEGED_FIELD", "path": "/", "message": f"Privileged fields are forbidden: {forbidden}"})
    phases = plan.get("generation_phases")
    expected_phases = [row["id"] for row in GENERATION_PHASES]
    actual_phases = [str(row.get("id")) for row in phases or [] if isinstance(row, dict)]
    if actual_phases != expected_phases:
        errors.append({
            "code": "ENRICHMENT_PLAN_PHASES_INVALID",
            "path": "/generation_phases",
            "message": f"Expected ordered generation phases: {expected_phases}",
        })
    return {
        "schema_version": "isaac-scene-enrichment-plan-validation/v1",
        "status": "fail" if errors else "pass",
        "valid": not errors,
        "errors": errors,
        "metrics": {"request_count": len(request_ids), "layer_count": len(plan.get("layers") or [])},
    }


def plan_scene_enrichment(
    brief: dict[str, Any],
    *,
    density: str = "rich",
    library_path: str | Path | None = None,
    scene_kit_catalog_path: str | Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if density not in DENSITY_LEVELS:
        raise ValueError(f"Unknown enrichment density: {density}")
    if brief.get("schema_version") != "isaac-scene-brief/v1":
        raise ValueError("Expected isaac-scene-brief/v1 input")
    forbidden = _contains_forbidden_key(brief)
    if forbidden:
        raise ValueError(f"Prompt/image enrichment cannot consume privileged fields: {forbidden}")

    library = load_library(library_path)
    domain = str(brief.get("domain") or "generic_scene")
    room = brief.get("room") if isinstance(brief.get("room"), dict) else {}
    room_type = str(room.get("type") or "room")
    profile_id = f"{domain}/{room_type}"
    profile = library["profiles"].get(profile_id)
    warnings: list[dict[str, Any]] = []
    if not isinstance(profile, dict):
        profile = {}
        warnings.append({
            "code": "ENRICHMENT_PROFILE_NOT_FOUND",
            "summary": f"No enrichment profile for {profile_id}; emitted policy-only plan.",
            "retryable": True,
        })

    grouped: dict[str, list[dict[str, Any]]] = {}
    already_satisfied: list[dict[str, Any]] = []
    for raw in profile.get("suggestions") or []:
        if not isinstance(raw, dict):
            continue
        count = _count_for_density(raw, density)
        if count <= 0:
            continue
        existing_count, matched_ids = _existing_count_for_request(brief, raw)
        missing_count = max(0, count - existing_count)
        if missing_count == 0:
            already_satisfied.append({
                "id": str(raw.get("id")),
                "role": str(raw.get("role")),
                "desired_count": count,
                "existing_count": existing_count,
                "matched_existing_object_ids": matched_ids,
            })
            continue
        request = {
            key: deepcopy(value)
            for key, value in raw.items()
            if key not in {"counts", "dedupe_terms", "layer"}
        }
        request["count"] = missing_count
        request["desired_count"] = count
        request["existing_count"] = existing_count
        request["matched_existing_object_ids"] = matched_ids
        request["binding_status"] = "asset_search_required"
        request["selection_policy"] = {
            "candidate_count": [3, 5],
            "required_checks": ["identity", "asset_health", "units", "bbox", "materials", "orientation_if_directional"],
            "cross_category_fallback": "forbidden",
            "uncertain_identity": "asset_gap",
            "visual_confirmation": "required_for_non_exact_or_generic_match",
        }
        grouped.setdefault(str(raw.get("layer") or "decor"), []).append(request)

    layers = [
        {"id": layer_id, "order": LAYER_ORDER.get(layer_id, 100), "requests": requests}
        for layer_id, requests in grouped.items()
    ]
    layers.sort(key=lambda row: (row["order"], row["id"]))
    budgets = [
        {
            "support_role": str(row.get("support_role")),
            "minimum": _budget_for_density(row, "minimum", density),
            "maximum": _budget_for_density(row, "maximum", density),
        }
        for row in profile.get("surface_prop_budgets") or []
        if isinstance(row, dict)
    ]
    inputs = brief.get("inputs") if isinstance(brief.get("inputs"), dict) else {}
    preserved = [
        str(item.get("id"))
        for item in brief.get("objects") or []
        if isinstance(item, dict) and item.get("id")
    ]
    plan = {
        "schema_version": "isaac-scene-enrichment-plan/v1",
        "input_provenance": {
            "mode": str(inputs.get("mode") or "text"),
            "text": str(inputs.get("text") or ""),
            "reference_images": deepcopy(inputs.get("reference_images") or []),
            "pose_source": "none",
        },
        "domain": domain,
        "room_type": room_type,
        "density": density,
        "profile_id": profile_id,
        "recommended_shell_preset": profile.get("recommended_shell_preset"),
        "style_profile": deepcopy(profile.get("style_profile") or {}),
        "policies": deepcopy(library.get("defaults") or {}),
        "generation_phases": [
            {
                **deepcopy(row),
                "advance_requires_content_accept": True,
                "failure_action": "revise_current_phase_without_pose_delta",
            }
            for row in GENERATION_PHASES
        ],
        "density_control": {
            "objective": "functional_coverage_not_object_count",
            "space_budget_source": "prompt_then_domain_profile_then_agent_estimate",
            "overflow_action_order": ["remove_optional_detail", "reduce_repetition", "record_asset_or_space_gap"],
            "preserve": ["prompt_required_entities", "functional_relations", "declared_circulation"],
        },
        "preserve_required_object_ids": preserved,
        "zones": deepcopy(profile.get("zones") or []),
        "layout_rules": deepcopy(profile.get("layout_rules") or []),
        "functional_groups": deepcopy(profile.get("functional_groups") or []),
        "layers": layers,
        "surface_prop_budgets": budgets,
        "already_satisfied": already_satisfied,
        "validation_targets": deepcopy(profile.get("validation_targets") or []),
        "metrics": {
            "source_object_role_count": len(preserved),
            "planned_role_count": sum(len(layer["requests"]) for layer in layers),
            "planned_instance_count": sum(request["count"] for layer in layers for request in layer["requests"]),
            "already_satisfied_role_count": len(already_satisfied),
        },
    }
    if scene_kit_catalog_path:
        scene_kit_context = context_for_brief(load_scene_kit_catalog(scene_kit_catalog_path), brief)
        plan["scene_kit_context"] = scene_kit_context
        matched_slugs = [str(row.get("slug")) for row in scene_kit_context["matches"] if row.get("slug")]
        for layer in plan["layers"]:
            for request in layer["requests"]:
                candidates = []
                for match in scene_kit_context["matches"]:
                    for candidate in match.get("callable_asset_candidates") or []:
                        request_class = str(request.get("canonical_class") or "")
                        candidate_class = str(candidate.get("canonical_class") or "")
                        if request_class and (candidate_class == request_class or candidate_class.startswith(request_class + "/") or request_class.startswith(candidate_class + "/")):
                            candidates.append(deepcopy(candidate))
                request["selection_policy"]["preferred_scene_slugs"] = matched_slugs
                request["selection_policy"]["same_scene_asset_candidates"] = candidates[:8]
        plan["metrics"]["scene_kit_match_count"] = len(scene_kit_context["matches"])
        plan["metrics"]["same_scene_callable_candidate_count"] = sum(
            len(request["selection_policy"]["same_scene_asset_candidates"])
            for layer in plan["layers"] for request in layer["requests"]
        )
    validation = validate_plan(plan)
    observation = {
        "status": "fail" if not validation["valid"] else ("warn" if warnings else "pass"),
        "profile_id": profile_id,
        "density": density,
        "validation": validation,
        "warnings": warnings,
        "metrics": deepcopy(plan["metrics"]),
    }
    return plan, observation


def materialize_enrichment_bindings(
    brief: dict[str, Any],
    plan: dict[str, Any],
    bindings: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Apply pose-free, verified asset choices to a Scene Brief.

    The external harness chooses assets; this function only enforces the plan,
    preserves the prompt-required inventory, and writes support dependencies.
    It deliberately rejects transforms so the layout solver remains the pose
    authority.
    """
    if brief.get("schema_version") != "isaac-scene-brief/v1":
        raise ValueError("Expected isaac-scene-brief/v1 input")
    validation = validate_plan(plan)
    if not validation["valid"]:
        raise ValueError(f"Invalid enrichment plan: {validation['errors']}")
    if bindings.get("schema_version") != "isaac-scene-enrichment-bindings/v1":
        raise ValueError("Expected isaac-scene-enrichment-bindings/v1 input")
    forbidden = _contains_forbidden_key(bindings)
    if forbidden:
        raise ValueError(f"Enrichment bindings cannot contain poses: {forbidden}")
    if bindings.get("profile_id") != plan.get("profile_id"):
        raise ValueError("Binding profile_id does not match the enrichment plan")

    request_map = {
        str(request["id"]): request
        for layer in plan.get("layers") or []
        for request in layer.get("requests") or []
    }
    decisions = bindings.get("decisions") if isinstance(bindings.get("decisions"), list) else []
    decision_map: dict[str, dict[str, Any]] = {}
    for index, decision in enumerate(decisions):
        if not isinstance(decision, dict):
            raise ValueError(f"Binding decision {index} must be an object")
        request_id = str(decision.get("request_id") or "")
        if request_id not in request_map:
            raise ValueError(f"Unknown enrichment request_id: {request_id}")
        if request_id in decision_map:
            raise ValueError(f"Duplicate enrichment decision: {request_id}")
        if decision.get("status") not in BINDING_STATUSES:
            raise ValueError(f"Unknown binding status for {request_id}: {decision.get('status')}")
        decision_map[request_id] = decision

    missing_decisions = sorted(set(request_map).difference(decision_map))
    if missing_decisions:
        raise ValueError(f"Missing enrichment binding decisions: {missing_decisions}")

    output = deepcopy(brief)
    existing_ids = {
        str(item.get("id"))
        for item in output.get("objects") or []
        if isinstance(item, dict) and item.get("id")
    }
    preserved = set(plan.get("preserve_required_object_ids") or [])
    if not preserved.issubset(existing_ids):
        raise ValueError(f"Required input objects are missing: {sorted(preserved.difference(existing_ids))}")

    room = output.setdefault("room", {})
    shell_preset = bindings.get("selected_shell_preset") or plan.get("recommended_shell_preset")
    if shell_preset:
        room["shell_preset"] = str(shell_preset)

    existing_zones = {
        str(row.get("id"))
        for row in output.get("zones") or []
        if isinstance(row, dict) and row.get("id")
    }
    for zone in plan.get("zones") or []:
        if isinstance(zone, dict) and zone.get("id") and zone.get("bounds_norm") and str(zone["id"]) not in existing_zones:
            output.setdefault("zones", []).append(deepcopy(zone))
            existing_zones.add(str(zone["id"]))

    existing_rule_ids = {
        str(row.get("id"))
        for row in output.get("layout_rules") or []
        if isinstance(row, dict) and row.get("id")
    }
    for rule in plan.get("layout_rules") or []:
        if isinstance(rule, dict) and rule.get("id") and str(rule["id"]) not in existing_rule_ids:
            output.setdefault("layout_rules", []).append(deepcopy(rule))
            existing_rule_ids.add(str(rule["id"]))

    existing_group_ids = {
        str(row.get("id"))
        for row in output.get("functional_groups") or []
        if isinstance(row, dict) and row.get("id")
    }
    for group in plan.get("functional_groups") or []:
        if isinstance(group, dict) and group.get("id") and str(group["id"]) not in existing_group_ids:
            output.setdefault("functional_groups", []).append(deepcopy(group))
            existing_group_ids.add(str(group["id"]))

    added_ids: list[str] = []
    gap_rows: list[dict[str, Any]] = []
    partial_rows: list[dict[str, Any]] = []
    for request_id, request in request_map.items():
        decision = decision_map[request_id]
        instances = decision.get("instances") if isinstance(decision.get("instances"), list) else []
        if decision["status"] == "gap":
            gap_rows.append({
                "request_id": request_id,
                "code": str(decision.get("gap_code") or "ASSET_GAP"),
                "reason": str(decision.get("reason") or "No verified binding was supplied."),
            })
            if instances:
                raise ValueError(f"Gap decision cannot contain instances: {request_id}")
            continue
        planned_count = int(request.get("count") or 0)
        if not instances or len(instances) > planned_count:
            raise ValueError(f"Invalid bound instance count for {request_id}: {len(instances)} / {planned_count}")
        if decision["status"] == "bound" and len(instances) != planned_count:
            raise ValueError(f"Bound decision must satisfy the planned count for {request_id}")
        if decision["status"] == "partial":
            partial_rows.append({"request_id": request_id, "bound": len(instances), "planned": planned_count})

        semantic_terms: list[str] = []
        for instance in instances:
            if not isinstance(instance, dict):
                raise ValueError(f"Binding instance for {request_id} must be an object")
            object_id = str(instance.get("id") or "")
            if not object_id or object_id in existing_ids:
                raise ValueError(f"Invalid or duplicate enrichment object id: {object_id}")
            asset_binding = instance.get("asset_binding")
            if not isinstance(asset_binding, dict) or not asset_binding.get("path") or not asset_binding.get("logical_id"):
                raise ValueError(f"Verified asset_binding is required for {object_id}")
            size_m = instance.get("size_m")
            if not isinstance(size_m, list) or len(size_m) != 3 or any(float(value) <= 0 for value in size_m):
                raise ValueError(f"Positive measured size_m is required for {object_id}")
            support_target = instance.get("support_target") or request.get("support_target") or request.get("support_target_role")
            if request.get("mount_target") and not instance.get("mount_target"):
                raise ValueError(f"Wall-mounted enrichment requires mount metadata: {object_id}")
            semantic_name = str(instance.get("semantic_name") or request.get("query") or request.get("role"))
            row = {
                "id": object_id,
                "role": str(instance.get("role") or request.get("role")),
                "semantic_class": str(instance.get("semantic_class") or request.get("canonical_class")),
                "semantic_name": semantic_name,
                "criticality": str(instance.get("criticality") or request.get("criticality") or "detail"),
                "confidence": str(instance.get("confidence") or "high"),
                "evidence": str(instance.get("evidence") or f"verified asset binding for enrichment request {request_id}"),
                "size_m": [float(value) for value in size_m],
                "asset_binding": deepcopy(asset_binding),
            }
            if support_target:
                row["support_target"] = str(support_target)
            placement_policy = instance.get("placement_policy") or request.get("placement_policy")
            if placement_policy:
                row["placement_policy"] = str(placement_policy)
            if instance.get("mount_target"):
                row["mount_target"] = deepcopy(instance["mount_target"])
                row["mount_anchor"] = deepcopy(instance.get("mount_anchor"))
            if instance.get("facing_target"):
                row["facing_target"] = str(instance["facing_target"])
            output.setdefault("objects", []).append(row)
            existing_ids.add(object_id)
            added_ids.append(object_id)
            semantic_terms.append(semantic_name)

        zone = request.get("zone")
        if zone and semantic_terms and not request.get("support_target_role") and not request.get("mount_target"):
            output.setdefault("layout_rules", []).append({
                "id": f"enrichment_{request_id}",
                "match": {"semantic_terms": semantic_terms},
                "zone": str(zone),
                "distribution": str(request.get("distribution") or "grid"),
                "preferred_yaw_deg": float(request.get("preferred_yaw_deg") or 0.0),
            })

    output.setdefault("assumptions", []).append(
        "Scene enrichment was materialized from pose-free verified bindings; all poses remain solver-owned."
    )
    report = {
        "schema_version": "isaac-scene-enrichment-materialization/v1",
        "status": "warn" if gap_rows or partial_rows else "pass",
        "profile_id": plan.get("profile_id"),
        "selected_shell_preset": shell_preset,
        "preserved_required_object_ids": sorted(preserved),
        "added_object_ids": added_ids,
        "gaps": gap_rows,
        "partial_bindings": partial_rows,
        "metrics": {
            "preserved_required_count": len(preserved),
            "added_object_count": len(added_ids),
            "gap_count": len(gap_rows),
            "partial_count": len(partial_rows),
        },
    }
    return output, report


def merge_enrichment_inventory(
    base_inventory: dict[str, Any],
    enriched_inventory: dict[str, Any],
    materialization_report: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Keep verified baseline assets and append only declared enrichment IDs."""
    added_ids = {str(value) for value in materialization_report.get("added_object_ids") or []}
    if not added_ids:
        raise ValueError("materialization report contains no added_object_ids")
    base_entities = [deepcopy(row) for row in base_inventory.get("entities") or [] if isinstance(row, dict)]
    for row in base_entities:
        row.setdefault("generation_layer", "baseline_required")
    base_ids = {str(row.get("id")) for row in base_entities if row.get("id")}
    enriched_by_id = {
        str(row.get("id")): row
        for row in enriched_inventory.get("entities") or []
        if isinstance(row, dict) and row.get("id")
    }
    missing = sorted(added_ids.difference(enriched_by_id))
    collisions = sorted(added_ids.intersection(base_ids))
    if missing:
        raise ValueError(f"compiled enrichment entities are missing: {missing}")
    if collisions:
        raise ValueError(f"enrichment IDs collide with baseline inventory: {collisions}")

    output = deepcopy(base_inventory)
    added_entities = [deepcopy(enriched_by_id[value]) for value in sorted(added_ids)]
    for row in added_entities:
        row["generation_layer"] = "enrichment"
    output["entities"] = base_entities + added_entities
    base_relations = [deepcopy(row) for row in base_inventory.get("relations") or [] if isinstance(row, dict)]
    added_relations = [
        deepcopy(row)
        for row in enriched_inventory.get("relations") or []
        if isinstance(row, dict) and str(row.get("subject") or "") in added_ids
    ]
    output["relations"] = base_relations + added_relations
    output["brief_snapshot"] = deepcopy(enriched_inventory.get("brief_snapshot") or {})
    for key in ("camera", "lighting"):
        if enriched_inventory.get(key):
            output[key] = deepcopy(enriched_inventory[key])
    output["enrichment_provenance"] = {
        "schema_version": "isaac-inventory-enrichment-merge/v1",
        "baseline_entity_count": len(base_entities),
        "added_entity_ids": sorted(added_ids),
        "policy": "preserve_verified_baseline_append_declared_enrichment",
    }
    report = {
        "schema_version": "isaac-inventory-enrichment-merge-report/v1",
        "status": "pass",
        "baseline_entity_count": len(base_entities),
        "added_entity_count": len(added_ids),
        "output_entity_count": len(output["entities"]),
        "added_relation_count": len(added_relations),
        "added_entity_ids": sorted(added_ids),
    }
    return output, report
