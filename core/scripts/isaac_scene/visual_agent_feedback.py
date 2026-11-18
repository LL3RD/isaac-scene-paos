from __future__ import annotations

import json
from pathlib import Path
from typing import Any


ISSUE_CATEGORIES = {
    "camera", "framing", "occlusion", "lighting", "material",
    "asset_richness", "semantic_identity", "architecture",
}
SEVERITIES = {"low", "medium", "high", "critical"}
VERDICTS = {"accept", "revise", "reject"}
QUALITY_REVIEW_AXES = {
    "rendering_quality": (
        "lighting_and_exposure",
        "noise_and_aliasing",
        "global_illumination_and_shadows",
        "reflections_and_transmission",
        "material_readability",
        "overall",
    ),
    "scene_content_quality": (
        "prompt_coverage",
        "asset_identity_and_quality",
        "asset_richness",
        "spatial_plausibility",
        "functional_coherence",
        "architecture_and_set_dressing",
        "overall",
    ),
}
QUALITY_CAUSES = {"rendering", "scene_content", "geometry_uncertain"}


def create_visual_review_packet(
    spec_path: str | Path,
    manifest_path: str | Path,
    ranking_path: str | Path,
    render_dir: str | Path,
    output_path: str | Path,
    *,
    scene_brief_path: str | Path | None = None,
) -> dict[str, Any]:
    spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    ranking = json.loads(Path(ranking_path).read_text(encoding="utf-8"))
    brief = json.loads(Path(scene_brief_path).read_text(encoding="utf-8")) if scene_brief_path else None
    root = Path(render_dir).resolve()
    ranked = {str(row.get("candidate_id")): row for row in ranking.get("top_candidates") or []}
    candidates = []
    for candidate in manifest.get("candidates") or []:
        candidate_id = str(candidate.get("candidate_id") or "")
        if not candidate_id or (ranked and candidate_id not in ranked):
            continue
        row = ranked.get(candidate_id) or candidate
        candidates.append({
            "candidate_id": candidate_id,
            "source": candidate.get("source"),
            "render_path": str(root / str(candidate.get("render_filename") or "")),
            "position": candidate.get("position"),
            "target": candidate.get("target"),
            "focal_length_mm": candidate.get("focal_length_mm"),
            "anchor_visibility_score": row.get("anchor_visibility_score"),
            "entity_visibility_score": row.get("entity_visibility_score"),
            "weighted_screen_area": row.get("weighted_screen_area"),
            "dominant_object_screen_area": row.get("dominant_object_screen_area"),
            "shell_backdrop_score": row.get("shell_backdrop_score"),
            "render_feedback_status": row.get("render_feedback_status"),
            "deterministic_score": row.get("combined_score"),
        })
    packet = {
        "schema_version": "isaac-scene-visual-review-packet/v1",
        "scene_id": spec.get("scene_id") or spec.get("id") or Path(spec_path).parent.name,
        "input": (brief or {}).get("input") or spec.get("input") or {},
        "required_entities": [
            {
                "id": row.get("id"),
                "canonical_class": row.get("canonical_class"),
                "criticality": row.get("criticality"),
                "generation_layer": row.get("generation_layer"),
            }
            for row in spec.get("entities") or []
            if isinstance(row, dict) and row.get("asset_scope") not in {"scene_root", "scene_layout_component", "external_feature"}
        ],
        "candidates": candidates,
        "review_contract": {
            "geometry_frozen": True,
            "allowed_issue_categories": sorted(ISSUE_CATEGORIES),
            "required_candidate_fields": [
                "candidate_id", "visual_score", "prompt_coverage_score",
                "composition_score", "realism_score", "recognizable_anchor_fraction",
            ],
            "score_range": [0.0, 1.0],
            "forbidden": [
                "direct object transforms", "asset deletion", "support bypass",
                "collision bypass", "unvalidated scale change",
            ],
            "instruction": (
                "Compare pixels against the input, score recognizable prompt coverage and visual quality, "
                "then choose one rendered candidate. Diagnose geometry-changing needs as deferred issues only."
            ),
        },
    }
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(packet, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {**packet, "output": str(output)}


def validate_visual_agent_review(
    packet_path: str | Path,
    review_path: str | Path,
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    packet = json.loads(Path(packet_path).read_text(encoding="utf-8"))
    review = json.loads(Path(review_path).read_text(encoding="utf-8"))
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    candidate_ids = {str(row.get("candidate_id")) for row in packet.get("candidates") or []}
    if review.get("schema_version") != "isaac-scene-visual-agent-review/v1":
        errors.append({"code": "VISUAL_REVIEW_SCHEMA_INVALID", "message": "Unexpected visual review schema_version."})
    verdict = str(review.get("verdict") or "")
    if verdict not in VERDICTS:
        errors.append({"code": "VISUAL_REVIEW_VERDICT_INVALID", "message": f"Unsupported verdict: {verdict}"})
    preferred = str(review.get("preferred_candidate_id") or "")
    if preferred not in candidate_ids:
        errors.append({"code": "VISUAL_REVIEW_CANDIDATE_UNKNOWN", "message": f"Unknown preferred candidate: {preferred}"})
    rows = review.get("candidate_reviews") or []
    reviewed_ids: set[str] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            errors.append({"code": "VISUAL_REVIEW_ROW_INVALID", "message": f"candidate_reviews[{index}] must be an object."})
            continue
        candidate_id = str(row.get("candidate_id") or "")
        if candidate_id not in candidate_ids:
            errors.append({"code": "VISUAL_REVIEW_CANDIDATE_UNKNOWN", "message": f"Unknown reviewed candidate: {candidate_id}"})
        if candidate_id in reviewed_ids:
            errors.append({"code": "VISUAL_REVIEW_DUPLICATE_CANDIDATE", "message": f"Duplicate candidate review: {candidate_id}"})
        reviewed_ids.add(candidate_id)
        for key in ("visual_score", "prompt_coverage_score", "composition_score", "realism_score", "recognizable_anchor_fraction"):
            value = row.get(key)
            if not isinstance(value, (int, float)) or not 0.0 <= float(value) <= 1.0:
                errors.append({"code": "VISUAL_REVIEW_SCORE_INVALID", "message": f"{candidate_id}.{key} must be in [0, 1]."})
    if preferred and preferred not in reviewed_ids:
        errors.append({"code": "VISUAL_REVIEW_PREFERRED_NOT_SCORED", "message": "The preferred candidate must have a candidate review."})
    if len(reviewed_ids) < min(2, len(candidate_ids)):
        warnings.append({"code": "VISUAL_REVIEW_COMPARISON_THIN", "message": "Fewer than two candidates were compared."})
    for index, issue in enumerate(review.get("issues") or []):
        if not isinstance(issue, dict):
            errors.append({"code": "VISUAL_REVIEW_ISSUE_INVALID", "message": f"issues[{index}] must be an object."})
            continue
        if issue.get("category") not in ISSUE_CATEGORIES:
            errors.append({"code": "VISUAL_REVIEW_CATEGORY_INVALID", "message": f"Unsupported issue category: {issue.get('category')}"})
        if issue.get("severity") not in SEVERITIES:
            errors.append({"code": "VISUAL_REVIEW_SEVERITY_INVALID", "message": f"Unsupported issue severity: {issue.get('severity')}"})
        patch = issue.get("suggested_constrained_patch")
        if patch is not None and not isinstance(patch, str):
            errors.append({"code": "VISUAL_REVIEW_PATCH_INVALID", "message": "suggested_constrained_patch must be natural language, not executable data."})
    normalized = {
        "schema_version": "isaac-scene-visual-agent-review-validation/v1",
        "status": "pass" if not errors else "fail",
        "packet_path": str(Path(packet_path).resolve()),
        "review_path": str(Path(review_path).resolve()),
        "verdict": verdict,
        "preferred_candidate_id": preferred or None,
        "candidate_reviews": rows,
        "issues": review.get("issues") or [],
        "geometry_frozen": True,
        "errors": errors,
        "warnings": warnings,
    }
    if output_path:
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(normalized, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        normalized["output"] = str(output)
    return normalized


def create_quality_review_packet(
    comparison_manifest_path: str | Path,
    axis: str,
    output_path: str | Path,
) -> dict[str, Any]:
    if axis not in QUALITY_REVIEW_AXES:
        raise ValueError(f"Unsupported quality-review axis: {axis}")
    manifest_path = Path(comparison_manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != "isaac-scene-render-comparison/v1":
        raise ValueError("Expected isaac-scene-render-comparison/v1")
    candidates = manifest.get("candidates") or []
    if len(candidates) < 2:
        raise ValueError("A quality comparison requires at least two renders")
    scene_hashes = {str(row.get("scene_sha256") or "") for row in candidates}
    cameras = {str(row.get("camera_path") or "") for row in candidates}
    if "" in scene_hashes or len(scene_hashes) != 1:
        raise ValueError("All comparison renders must share one non-empty scene_sha256")
    if "" in cameras or len(cameras) != 1:
        raise ValueError("All comparison renders must share one non-empty camera_path")
    packet = {
        "schema_version": "isaac-scene-quality-review-packet/v1",
        "review_axis": axis,
        "scene_id": manifest.get("scene_id"),
        "prompt": manifest.get("prompt"),
        "scene_path": manifest.get("scene_path"),
        "scene_sha256": next(iter(scene_hashes)),
        "fixed_camera_path": next(iter(cameras)),
        "candidates": [
            {
                "candidate_id": row.get("candidate_id"),
                "render_path": row.get("render_path"),
                "render_profile": row.get("render_profile"),
                "renderer": row.get("renderer"),
                "elapsed_seconds": row.get("elapsed_seconds"),
            }
            for row in candidates
        ],
        "review_contract": {
            "required_scores": list(QUALITY_REVIEW_AXES[axis]),
            "score_range": [0.0, 1.0],
            "allowed_primary_causes": sorted(QUALITY_CAUSES),
            "instruction": (
                "Judge only rendering fidelity, convergence, lighting, reflections, and material readability; "
                "do not lower rendering scores merely because the scene lacks assets."
                if axis == "rendering_quality"
                else
                "Judge prompt coverage, asset quality/richness, spatial and functional plausibility, and architecture; "
                "do not raise content scores merely because Path Tracing makes the same content look polished."
            ),
            "geometry_frozen": True,
            "camera_frozen": True,
        },
    }
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(packet, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {**packet, "output": str(output)}


def validate_quality_review(
    packet_path: str | Path,
    review_path: str | Path,
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    packet = json.loads(Path(packet_path).read_text(encoding="utf-8"))
    review = json.loads(Path(review_path).read_text(encoding="utf-8"))
    errors: list[dict[str, Any]] = []
    axis = str(packet.get("review_axis") or "")
    if axis not in QUALITY_REVIEW_AXES:
        errors.append({"code": "QUALITY_REVIEW_AXIS_INVALID", "message": f"Unsupported packet axis: {axis}"})
    if review.get("schema_version") != "isaac-scene-quality-agent-review/v1":
        errors.append({"code": "QUALITY_REVIEW_SCHEMA_INVALID", "message": "Unexpected quality review schema_version."})
    if review.get("review_axis") != axis:
        errors.append({"code": "QUALITY_REVIEW_AXIS_MISMATCH", "message": "Review axis does not match the packet."})
    if str(review.get("verdict") or "") not in VERDICTS:
        errors.append({"code": "QUALITY_REVIEW_VERDICT_INVALID", "message": "Unsupported verdict."})
    candidate_ids = {str(row.get("candidate_id") or "") for row in packet.get("candidates") or []}
    reviewed_ids: set[str] = set()
    required_scores = QUALITY_REVIEW_AXES.get(axis, ())
    for index, row in enumerate(review.get("candidate_reviews") or []):
        if not isinstance(row, dict):
            errors.append({"code": "QUALITY_REVIEW_ROW_INVALID", "message": f"candidate_reviews[{index}] must be an object."})
            continue
        candidate_id = str(row.get("candidate_id") or "")
        if candidate_id not in candidate_ids:
            errors.append({"code": "QUALITY_REVIEW_CANDIDATE_UNKNOWN", "message": f"Unknown candidate: {candidate_id}"})
        if candidate_id in reviewed_ids:
            errors.append({"code": "QUALITY_REVIEW_CANDIDATE_DUPLICATE", "message": f"Duplicate candidate: {candidate_id}"})
        reviewed_ids.add(candidate_id)
        scores = row.get("scores")
        if not isinstance(scores, dict):
            errors.append({"code": "QUALITY_REVIEW_SCORES_INVALID", "message": f"{candidate_id}.scores must be an object."})
            continue
        for key in required_scores:
            value = scores.get(key)
            if not isinstance(value, (int, float)) or not 0.0 <= float(value) <= 1.0:
                errors.append({"code": "QUALITY_REVIEW_SCORE_INVALID", "message": f"{candidate_id}.scores.{key} must be in [0, 1]."})
    if reviewed_ids != candidate_ids:
        errors.append({"code": "QUALITY_REVIEW_COMPARISON_INCOMPLETE", "message": "Every candidate must be reviewed exactly once."})
    for index, issue_row in enumerate(review.get("issues") or []):
        if not isinstance(issue_row, dict):
            errors.append({"code": "QUALITY_REVIEW_ISSUE_INVALID", "message": f"issues[{index}] must be an object."})
            continue
        if issue_row.get("primary_cause") not in QUALITY_CAUSES:
            errors.append({"code": "QUALITY_REVIEW_CAUSE_INVALID", "message": f"Unsupported primary cause: {issue_row.get('primary_cause')}"})
        suggestion = issue_row.get("suggested_constrained_patch")
        if suggestion is not None and not isinstance(suggestion, str):
            errors.append({"code": "QUALITY_REVIEW_PATCH_INVALID", "message": "Suggested patches must be natural language."})
    normalized = {
        "schema_version": "isaac-scene-quality-agent-review-validation/v1",
        "status": "pass" if not errors else "fail",
        "review_axis": axis,
        "packet_path": str(Path(packet_path).resolve()),
        "review_path": str(Path(review_path).resolve()),
        "verdict": review.get("verdict"),
        "candidate_reviews": review.get("candidate_reviews") or [],
        "issues": review.get("issues") or [],
        "errors": errors,
        "geometry_frozen": True,
        "camera_frozen": True,
    }
    if output_path:
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(normalized, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        normalized["output"] = str(output)
    return normalized
