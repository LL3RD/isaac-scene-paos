from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any


CONTENT_SCORE_FIELDS = (
    "prompt_coverage",
    "asset_identity",
    "spatial_plausibility",
    "functional_coherence",
    "controlled_density",
    "overall",
)
CONTENT_THRESHOLDS = {
    "prompt_coverage": 0.80,
    "asset_identity": 0.85,
    "spatial_plausibility": 0.78,
    "functional_coherence": 0.78,
    "controlled_density": 0.72,
    "overall": 0.78,
}
CONTENT_ISSUE_CATEGORIES = {
    "missing_required_role",
    "wrong_asset_identity",
    "support_or_contact",
    "overlap_or_clearance",
    "orientation",
    "functional_relation",
    "circulation",
    "density",
    "composition",
    "architecture",
    "asset_quality",
}
CONTENT_REPAIR_ACTIONS = {
    "requery_asset",
    "add_required_role",
    "remove_optional",
    "reduce_density",
    "rebuild_functional_group",
    "repair_declared_relation",
    "revise_architecture",
    "preserve_and_recompose",
}
SEVERITIES = {"low", "medium", "high", "critical"}
VERDICTS = {"accept", "revise", "reject"}
PHASES = ("anchors", "functional", "detail")
GENERIC_INVARIANTS = (
    "verified_asset_identity",
    "physical_support_and_contact",
    "declared_orientation_connection_and_containment_relations",
    "declared_circulation_and_access_clearance",
    "content_review_acceptance_before_final_render",
)
FORBIDDEN_EXECUTION_KEYS = {
    "translate",
    "rotate",
    "scale",
    "position",
    "quaternion",
    "xform_matrix",
    "matrix",
    "source_world_matrix",
    "regression_alignment",
    "regression_instance_opinions",
}


def _read_json(path: str | Path) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return value


def _write_json(path: str | Path, value: dict[str, Any]) -> str:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(output)


def _forbidden_keys(value: Any) -> list[str]:
    found: set[str] = set()

    def visit(row: Any) -> None:
        if isinstance(row, dict):
            for key, child in row.items():
                if str(key) in FORBIDDEN_EXECUTION_KEYS:
                    found.add(str(key))
                visit(child)
        elif isinstance(row, list):
            for child in row:
                visit(child)

    visit(value)
    return sorted(found)


def _entity_summary(entity: dict[str, Any]) -> dict[str, Any]:
    binding = entity.get("asset_binding") if isinstance(entity.get("asset_binding"), dict) else {}
    return {
        "id": entity.get("id"),
        "role": entity.get("role"),
        "canonical_class": entity.get("canonical_class") or entity.get("semantic_class"),
        "criticality": entity.get("criticality"),
        "generation_layer": entity.get("generation_layer"),
        "support_target": entity.get("support_target"),
        "facing_target": entity.get("facing_target"),
        "logical_id": entity.get("logical_id") or binding.get("logical_id"),
        "asset_source": entity.get("asset_source") or binding.get("asset_source"),
    }


def _relation_summary(relation: dict[str, Any]) -> dict[str, Any]:
    return {
        key: deepcopy(relation.get(key))
        for key in ("id", "type", "subject", "target", "required", "tolerance_m")
        if relation.get(key) is not None
    }


def _render_rows(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    raw_rows = manifest.get("renders")
    if not isinstance(raw_rows, list):
        raw_rows = manifest.get("candidates")
    if not isinstance(raw_rows, list):
        raw_rows = manifest.get("jobs")
    if not isinstance(raw_rows, list):
        raise ValueError("Render manifest must contain renders, candidates, or jobs")
    rows: list[dict[str, Any]] = []
    used: set[str] = set()
    for index, raw in enumerate(raw_rows):
        if not isinstance(raw, dict):
            continue
        view_id = str(raw.get("view_id") or raw.get("candidate_id") or raw.get("id") or f"view_{index:02d}")
        render_path = raw.get("render_path") or raw.get("output_path") or raw.get("path")
        if not render_path:
            continue
        if view_id in used:
            raise ValueError(f"Duplicate render view_id: {view_id}")
        used.add(view_id)
        rows.append({
            "view_id": view_id,
            "render_path": str(render_path),
            "camera_path": raw.get("camera_path") or raw.get("camera"),
            "view_kind": raw.get("view_kind") or raw.get("source") or "perspective",
        })
    if not rows:
        raise ValueError("Render manifest does not contain any reviewable render path")
    return rows


def create_content_iteration_packet(
    brief_path: str | Path,
    spec_path: str | Path,
    render_manifest_path: str | Path,
    output_path: str | Path,
    *,
    iteration: int = 0,
    phase: str = "anchors",
    max_iterations: int = 6,
    previous_validation_path: str | Path | None = None,
) -> dict[str, Any]:
    if iteration < 0:
        raise ValueError("iteration must be non-negative")
    if phase not in PHASES:
        raise ValueError(f"Unsupported content phase: {phase}")
    if max_iterations < 1 or max_iterations > 12:
        raise ValueError("max_iterations must be in [1, 12]")
    brief = _read_json(brief_path)
    spec = _read_json(spec_path)
    manifest = _read_json(render_manifest_path)
    renders = _render_rows(manifest)
    if brief.get("schema_version") != "isaac-scene-brief/v1":
        raise ValueError("Expected isaac-scene-brief/v1")
    if spec.get("schema_version") != "isaac-scene-spec/v2":
        raise ValueError("Expected isaac-scene-spec/v2")
    entities = [
        _entity_summary(row)
        for row in spec.get("entities") or []
        if isinstance(row, dict)
        and row.get("asset_scope") not in {"scene_root", "scene_layout_component", "external_feature"}
    ]
    relations = [
        _relation_summary(row)
        for row in spec.get("relations") or []
        if isinstance(row, dict)
    ]
    previous = None
    if previous_validation_path:
        previous_data = _read_json(previous_validation_path)
        previous = {
            "validation_path": str(Path(previous_validation_path).resolve()),
            "verdict": previous_data.get("verdict"),
            "scores": deepcopy(previous_data.get("scores") or {}),
            "content_gate": previous_data.get("content_gate"),
            "issue_count": len(previous_data.get("issues") or []),
        }
    packet = {
        "schema_version": "isaac-scene-content-iteration-packet/v1",
        "scene_id": spec.get("scene_id") or spec.get("id") or Path(spec_path).parent.name,
        "iteration": iteration,
        "phase": phase,
        "max_iterations": max_iterations,
        "prompt_input": deepcopy(brief.get("inputs") or brief.get("input") or {}),
        "domain": brief.get("domain") or spec.get("domain"),
        "room_type": (brief.get("room") or {}).get("type") if isinstance(brief.get("room"), dict) else None,
        "required_entities": entities,
        "declared_relations": relations,
        "functional_groups": deepcopy(brief.get("functional_groups") or spec.get("functional_groups") or []),
        "declared_zones": deepcopy(brief.get("zones") or spec.get("zones") or []),
        "renders": renders,
        "previous_iteration": previous,
        "review_contract": {
            "review_axis": "scene_content_quality",
            "required_scores": list(CONTENT_SCORE_FIELDS),
            "accept_thresholds": deepcopy(CONTENT_THRESHOLDS),
            "score_range": [0.0, 1.0],
            "allowed_issue_categories": sorted(CONTENT_ISSUE_CATEGORIES),
            "allowed_repair_actions": sorted(CONTENT_REPAIR_ACTIONS),
            "generic_hard_invariants": list(GENERIC_INVARIANTS),
            "domain_rule_policy": (
                "Judge prompt-declared and selected-domain relations. Do not invent scene-name-specific rules."
            ),
            "density_policy": (
                "Judge functional completeness, readable grouping, access and negative space; object count alone is not richness."
            ),
            "identity_policy": (
                "A visually wrong category cannot be accepted as a substitute; request asset re-query or record an asset gap."
            ),
            "forbidden": [
                "object transforms or executable patches",
                "renderer or lighting criticism",
                "rewarding object count without functional coherence",
                "inventing undeclared domain-specific dimensions",
            ],
            "instruction": (
                "Review every supplied view, separate observable evidence from diagnosis, and emit semantic repair intents only."
            ),
        },
    }
    packet["output"] = _write_json(output_path, packet)
    return packet


def validate_content_iteration_review(
    packet_path: str | Path,
    review_path: str | Path,
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    packet = _read_json(packet_path)
    review = _read_json(review_path)
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    if packet.get("schema_version") != "isaac-scene-content-iteration-packet/v1":
        errors.append({"code": "CONTENT_PACKET_SCHEMA_INVALID", "message": "Unexpected content packet schema."})
    if review.get("schema_version") != "isaac-scene-content-agent-review/v1":
        errors.append({"code": "CONTENT_REVIEW_SCHEMA_INVALID", "message": "Unexpected content review schema."})
    if review.get("scene_id") != packet.get("scene_id"):
        errors.append({"code": "CONTENT_REVIEW_SCENE_MISMATCH", "message": "Review scene_id does not match the packet."})
    if review.get("iteration") != packet.get("iteration"):
        errors.append({"code": "CONTENT_REVIEW_ITERATION_MISMATCH", "message": "Review iteration does not match the packet."})
    if review.get("phase") != packet.get("phase"):
        errors.append({"code": "CONTENT_REVIEW_PHASE_MISMATCH", "message": "Review phase does not match the packet."})
    verdict = str(review.get("verdict") or "")
    if verdict not in VERDICTS:
        errors.append({"code": "CONTENT_REVIEW_VERDICT_INVALID", "message": f"Unsupported verdict: {verdict}"})
    forbidden = _forbidden_keys(review)
    if forbidden:
        errors.append({
            "code": "CONTENT_REVIEW_EXECUTABLE_PATCH_FORBIDDEN",
            "message": f"Review contains solver-owned or privileged fields: {forbidden}",
        })
    scores = review.get("scores")
    if not isinstance(scores, dict):
        errors.append({"code": "CONTENT_REVIEW_SCORES_INVALID", "message": "scores must be an object."})
        scores = {}
    for key in CONTENT_SCORE_FIELDS:
        value = scores.get(key)
        if not isinstance(value, (int, float)) or not 0.0 <= float(value) <= 1.0:
            errors.append({"code": "CONTENT_REVIEW_SCORE_INVALID", "message": f"scores.{key} must be in [0, 1]."})
    view_ids = {str(row.get("view_id")) for row in packet.get("renders") or []}
    entity_ids = {str(row.get("id")) for row in packet.get("required_entities") or [] if row.get("id")}
    issue_ids: set[str] = set()
    normalized_issues: list[dict[str, Any]] = []
    for index, issue in enumerate(review.get("issues") or []):
        if not isinstance(issue, dict):
            errors.append({"code": "CONTENT_REVIEW_ISSUE_INVALID", "message": f"issues[{index}] must be an object."})
            continue
        issue_id = str(issue.get("id") or "")
        if not issue_id or issue_id in issue_ids:
            errors.append({"code": "CONTENT_REVIEW_ISSUE_ID_INVALID", "message": f"issues[{index}] needs a unique id."})
        issue_ids.add(issue_id)
        if issue.get("category") not in CONTENT_ISSUE_CATEGORIES:
            errors.append({"code": "CONTENT_REVIEW_CATEGORY_INVALID", "message": f"Unsupported category: {issue.get('category')}"})
        if issue.get("severity") not in SEVERITIES:
            errors.append({"code": "CONTENT_REVIEW_SEVERITY_INVALID", "message": f"Unsupported severity: {issue.get('severity')}"})
        evidence_views = [str(value) for value in issue.get("evidence_view_ids") or []]
        unknown_views = sorted(set(evidence_views).difference(view_ids))
        if unknown_views:
            errors.append({"code": "CONTENT_REVIEW_EVIDENCE_UNKNOWN", "message": f"Unknown evidence views: {unknown_views}"})
        affected = [str(value) for value in issue.get("affected_entity_ids") or []]
        unknown_entities = sorted(set(affected).difference(entity_ids))
        if unknown_entities:
            warnings.append({"code": "CONTENT_REVIEW_ENTITY_UNKNOWN", "message": f"Issue references non-existing or missing-role IDs: {unknown_entities}"})
        action = issue.get("repair_action")
        if not isinstance(action, dict):
            errors.append({"code": "CONTENT_REVIEW_REPAIR_INVALID", "message": f"issues[{index}].repair_action must be an object."})
            action = {}
        if action.get("type") not in CONTENT_REPAIR_ACTIONS:
            errors.append({"code": "CONTENT_REVIEW_REPAIR_TYPE_INVALID", "message": f"Unsupported repair action: {action.get('type')}"})
        if not isinstance(action.get("instruction"), str) or not action.get("instruction", "").strip():
            errors.append({"code": "CONTENT_REVIEW_REPAIR_INSTRUCTION_REQUIRED", "message": f"issues[{index}] needs a natural-language repair instruction."})
        normalized_issues.append(deepcopy(issue))
    if verdict in {"revise", "reject"} and not normalized_issues:
        errors.append({"code": "CONTENT_REVIEW_REPAIR_MISSING", "message": "revise/reject requires at least one repair issue."})
    thresholds = packet.get("review_contract", {}).get("accept_thresholds") or CONTENT_THRESHOLDS
    threshold_failures = [
        {"score": key, "actual": scores.get(key), "required": float(thresholds.get(key, 0.0))}
        for key in CONTENT_SCORE_FIELDS
        if isinstance(scores.get(key), (int, float)) and float(scores[key]) < float(thresholds.get(key, 0.0))
    ]
    blocking_issues = [row.get("id") for row in normalized_issues if row.get("severity") in {"high", "critical"}]
    if verdict == "accept" and (threshold_failures or blocking_issues):
        errors.append({
            "code": "CONTENT_REVIEW_ACCEPTANCE_UNSUPPORTED",
            "message": "accept requires all thresholds and no high/critical content issue.",
        })
    valid = not errors
    content_gate = "pass" if valid and verdict == "accept" else "pending"
    result = {
        "schema_version": "isaac-scene-content-agent-review-validation/v1",
        "status": "pass" if valid else "fail",
        "content_gate": content_gate,
        "retry_required": valid and content_gate != "pass",
        "scene_id": packet.get("scene_id"),
        "iteration": packet.get("iteration"),
        "phase": packet.get("phase"),
        "max_iterations": packet.get("max_iterations"),
        "verdict": verdict,
        "scores": deepcopy(scores),
        "threshold_failures": threshold_failures,
        "issues": normalized_issues,
        "errors": errors,
        "warnings": warnings,
        "packet_path": str(Path(packet_path).resolve()),
        "review_path": str(Path(review_path).resolve()),
    }
    if output_path:
        result["output"] = _write_json(output_path, result)
    return result


def create_scene_revision_request(
    packet_path: str | Path,
    validation_path: str | Path,
    output_path: str | Path,
) -> dict[str, Any]:
    packet = _read_json(packet_path)
    validation = _read_json(validation_path)
    if packet.get("schema_version") != "isaac-scene-content-iteration-packet/v1":
        raise ValueError("Expected isaac-scene-content-iteration-packet/v1")
    if validation.get("schema_version") != "isaac-scene-content-agent-review-validation/v1":
        raise ValueError("Expected isaac-scene-content-agent-review-validation/v1")
    if validation.get("status") != "pass":
        raise ValueError("A structurally invalid Agent review cannot drive scene revision")
    if validation.get("scene_id") != packet.get("scene_id") or validation.get("iteration") != packet.get("iteration"):
        raise ValueError("Review validation does not belong to the content packet")
    iteration = int(packet.get("iteration") or 0)
    max_iterations = int(packet.get("max_iterations") or 6)
    phase = str(packet.get("phase") or "anchors")
    next_phase = PHASES[min(PHASES.index(phase) + 1, len(PHASES) - 1)]
    gate_passed = validation.get("content_gate") == "pass"
    if gate_passed and phase == PHASES[-1]:
        action = "freeze_content"
        status = "complete"
        target_phase = phase
        final_ready = True
    elif gate_passed:
        action = "advance_phase"
        status = "ready"
        target_phase = next_phase
        final_ready = False
    elif iteration + 1 >= max_iterations:
        action = "stop_for_human_review"
        status = "exhausted"
        target_phase = phase
        final_ready = False
    else:
        action = "regenerate_complete_spec"
        status = "ready"
        target_phase = phase
        final_ready = False
    issues = deepcopy(validation.get("issues") or [])
    request = {
        "schema_version": "isaac-scene-revision-request/v1",
        "status": status,
        "action": action,
        "scene_id": packet.get("scene_id"),
        "source_iteration": iteration,
        "target_iteration": iteration + (0 if action == "freeze_content" else 1),
        "source_phase": phase,
        "target_phase": target_phase,
        "final_render_allowed": final_ready,
        "repair_intents": [
            {
                "issue_id": row.get("id"),
                "category": row.get("category"),
                "severity": row.get("severity"),
                "affected_entity_ids": deepcopy(row.get("affected_entity_ids") or []),
                "evidence_view_ids": deepcopy(row.get("evidence_view_ids") or []),
                "diagnosis": row.get("diagnosis"),
                "repair_action": deepcopy(row.get("repair_action") or {}),
            }
            for row in issues
        ],
        "regeneration_contract": {
            "output_mode": "complete_scene_brief_and_complete_scene_spec_not_delta",
            "pose_authority": "deterministic_layout_solver",
            "preserve_prompt_required_entities": True,
            "preserve_unaffected_verified_asset_bindings": True,
            "requery_only_when_identity_or_asset_quality_is_affected": True,
            "rebuild_affected_dependency_closure": True,
            "forbidden_fields_in_agent_output": sorted(FORBIDDEN_EXECUTION_KEYS),
            "missing_asset_policy": "record_asset_gap_instead_of_cross_category_substitution",
            "density_policy": "remove_optional_detail_before_required_or_functional_entities",
        },
        "next_required_checks": [
            "compile_complete_spec",
            "solve_affected_dependency_closure",
            "geometry_and_support_validation",
            "multi_view_preview_render",
            "new_independent_content_agent_review",
        ] if action in {"regenerate_complete_spec", "advance_phase"} else [],
    }
    request["output"] = _write_json(output_path, request)
    return request
