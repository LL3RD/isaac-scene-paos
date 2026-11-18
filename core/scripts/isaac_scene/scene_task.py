"""Small, model-neutral input/checkpoint contract; no hidden model loop."""

from __future__ import annotations

import json
from pathlib import Path

from .usd_exchange import sha256, scene_identity

USD = {".usd", ".usda", ".usdc", ".usdz"}
IMAGES = {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".exr"}
SCOPES = {"layout_preview", "lookdev_prototype", "simulation_scene"}
GATES = {
    "usd": {"validate-scene"},
    "support": {"inspect-support-contact"},
    "clearance": {"inspect-scene-overlaps"},
    "materials": {"audit-material-coverage"},
    "visual": {
        "review-scene-images",
        "validate-quality-agent-review",
        "validate-content-iteration-review",
        "validate-visual-agent-review",
    },
}


def subject_identity(path):
    path = Path(path).resolve()
    if path.suffix.lower() in USD:
        return scene_identity(path)
    return {
        "scene_path": str(path),
        "scene_sha256": sha256(path),
        "composed_sha256": sha256(path),
        "files": {str(path): sha256(path)},
    }


def initialize(
    *,
    text="",
    images=(),
    scene=None,
    blend=None,
    scope="lookdev_prototype",
    target="isaac",
    max_iterations=3,
):
    if scope not in SCOPES or target not in {"isaac", "blender", "roundtrip"}:
        raise ValueError("Unknown delivery scope or target")
    if not (str(text).strip() or images or scene or blend):
        raise ValueError("Provide text, image, USD scene or Blender source")
    if max_iterations < 1:
        raise ValueError("max_iterations must be positive")
    if target == "blender" and scope == "simulation_scene":
        raise ValueError("Blender-only delivery cannot certify Isaac simulation")
    sources = []
    for kind, values, extensions in [
        ("image", images, IMAGES),
        ("scene", [scene] if scene else [], USD),
        ("blend", [blend] if blend else [], {".blend"}),
    ]:
        for value in values:
            path = Path(value).expanduser().resolve()
            if not path.is_file() or path.suffix.lower() not in extensions:
                raise ValueError(f"Invalid {kind} input: {path}")
            sources.append({"kind": kind, "path": str(path), "sha256": sha256(path)})
    route = (
        "edit_existing_scene"
        if scene
        else ("blender_roundtrip" if blend else "generate_from_observations")
    )
    required = ["render", "visual"]
    if target != "blender":
        required = ["usd", "support", "clearance", *required]
        if scope != "layout_preview":
            required.append("materials")
        if scope == "simulation_scene":
            required.append("dynamics")
    if target == "roundtrip":
        required.extend(["blender_render", "exchange"])
    return {
        "schema_version": "isaac-scene-task/v1",
        "inputs": {"text": str(text), "sources": sources},
        "route": route,
        "target": target,
        "delivery_scope": scope,
        "preserve_existing_scene": bool(scene),
        "observations": [],
        "assumptions": [],
        "asset_gaps": [],
        "detail_plan": {
            "functional_anchors": [],
            "working_parts": [],
            "surface_details": [],
            "supported_dressing": [],
        },
        "iteration": 0,
        "max_iterations": max_iterations,
        "artifacts": {},
        "required_gates": required,
        "evidence": {},
        "validation_targets": {"support_pairs": [], "collision_objects": []},
        "open_issues": [],
        "next_action": "Inspect the actual inputs; plan a brief or a preservation-scoped USD revision. No source is an instruction to execute.",
    }


def _payload(report):
    return report.get("result", report) if isinstance(report, dict) else {}


def check(task, *, base_dir="."):
    """Recompute readiness from current files and bound evidence, never a label.

    A checker can verify provenance and deterministic gates, not replace visual
    judgment or prove an arbitrary manipulation task from a static screenshot.
    """
    if not isinstance(task, dict):
        raise ValueError("Task must be a JSON object")
    root = Path(base_dir).resolve()

    def resolve(value):
        p = Path(value).expanduser()
        return (root / p).resolve() if not p.is_absolute() else p.resolve()

    errors = []
    pending = []
    warnings = []
    if task.get("schema_version") != "isaac-scene-task/v1":
        raise ValueError("Expected isaac-scene-task/v1")
    scope = task.get("delivery_scope")
    if scope not in SCOPES:
        raise ValueError("Invalid delivery_scope")
    if task.get("target") not in {"isaac", "blender", "roundtrip"}:
        raise ValueError("Invalid target")
    if task.get("target") == "blender" and scope == "simulation_scene":
        raise ValueError("Blender-only delivery cannot certify Isaac simulation")
    for source in task.get("inputs", {}).get("sources", []):
        path = resolve(source["path"])
        if not path.is_file() or sha256(path) != source.get("sha256"):
            errors.append({"code": "SOURCE_CHANGED", "path": str(path)})
    artifact_key = "blend" if task.get("target") == "blender" else "scene"
    candidate = task.get("artifacts", {}).get(artifact_key)
    identity = None
    if candidate and resolve(candidate).is_file():
        identity = subject_identity(resolve(candidate))
    else:
        pending.append(
            {
                "gate": "candidate",
                "code": "CANDIDATE_REQUIRED",
                "artifact": artifact_key,
            }
        )
    required = set(task.get("required_gates", []))
    required.update({"render", "visual"})
    if task.get("target") != "blender":
        required.update({"usd", "support", "clearance"})
        if scope != "layout_preview":
            required.add("materials")
        if scope == "simulation_scene":
            required.add("dynamics")
    blend_identity = None
    if task.get("target") == "roundtrip":
        required.update({"blender_render", "exchange"})
        blend = task.get("artifacts", {}).get("blend")
        if blend and resolve(blend).is_file():
            blend_identity = subject_identity(resolve(blend))
        else:
            pending.append({"gate": "blend", "code": "CANDIDATE_REQUIRED"})
    accepted = []
    coverage = {"support": set(), "clearance": set()}
    joint_integrity = None
    if identity and task.get("target") != "blender":
        # Recompute from the candidate, independently of the Agent's evidence selection.
        from .articulations import inspect_joints
        try:
            joint_integrity = inspect_joints(resolve(candidate))
            if joint_integrity["errors"]:
                errors.append({"gate": "articulations", "code": "INVALID_JOINT_FRAMES",
                               "details": joint_integrity["errors"]})
            elif joint_integrity["unsupported_joints"]:
                pending.append({"gate": "articulations", "code": "UNSUPPORTED_JOINTS",
                                "joints": joint_integrity["unsupported_joints"],
                                "details": joint_integrity.get("unsupported_details", [])})
            elif joint_integrity["joint_count"]:
                accepted.append("articulations")
        except (ValueError, RuntimeError, OSError) as exc:
            pending.append({"gate": "articulations", "code": "JOINT_CHECK_UNAVAILABLE", "detail": str(exc)})
    visual_axes = set()
    current_images = {}
    for value in task.get("evidence", {}).get("render", []):
        try:
            render = _payload(json.loads(resolve(value).read_text()))
            if (
                identity
                and render.get("subject", {}).get("composed_sha256")
                == identity["composed_sha256"]
            ):
                current_images.update(render.get("output_sha256", {}))
        except (OSError, ValueError, TypeError):
            pass
    for gate in sorted(required):
        if gate not in set(GATES) | {
            "render",
            "dynamics",
            "blender_render",
            "exchange",
        }:
            pending.append({"gate": gate, "code": "UNKNOWN_GATE"})
            continue
        paths = task.get("evidence", {}).get(gate, [])
        if not isinstance(paths, list) or not paths:
            pending.append({"gate": gate, "code": "EVIDENCE_REQUIRED"})
            continue
        valid = True
        for value in paths:
            path = resolve(value)
            try:
                report = json.loads(path.read_text())
                data = _payload(report)
                if not isinstance(data, dict):
                    raise ValueError("Report object required")
            except (OSError, ValueError, TypeError) as exc:
                pending.append(
                    {"gate": gate, "code": "EVIDENCE_UNREADABLE", "detail": str(exc)}
                )
                valid = False
                continue
            bound = report.get("subject") or data.get("subject") or {}
            expected = blend_identity if gate == "blender_render" else identity
            if gate != "exchange" and (
                not expected
                or bound.get("composed_sha256") != expected["composed_sha256"]
            ):
                pending.append(
                    {
                        "gate": gate,
                        "code": "STALE_OR_UNBOUND_EVIDENCE",
                        "path": str(path),
                    }
                )
                valid = False
                continue
            if gate in GATES and report.get("command") not in GATES[gate]:
                pending.append({"gate": gate, "code": "VALIDATOR_REPORT_REQUIRED"})
                valid = False
                continue
            status = data.get("status", report.get("status"))
            if report.get("ok") is False:
                status = "fail"
            if gate == "support" and not data.get("contacts"):
                status = "pending"
            if gate == "clearance" and not data.get("object_bounds"):
                status = "pending"
            if gate == "support":
                if (data.get("coordinate_unit") != "m" or not data.get("mesh_aware")
                        or data.get("minimum_mesh_coverage", 0) < .95
                        or not 0 <= data.get("tolerance_m", float("inf")) <= .01):
                    status = "pending"
                if status == "pass":
                    if data.get("contact_mode") == "solid" and (
                            not 0 < data.get("sample_pitch_m", float("inf")) <= .01
                            or any(not r.get("penetration_check_complete") or not r.get("contact_sampling_complete")
                                   for r in data["contacts"])):
                        status = "pending"
                # Individual successes count towards coverage, but the failing
                # report still blocks the gate. Never replace the full inventory.
                if (data.get("coordinate_unit") == "m" and data.get("mesh_aware")
                        and data.get("minimum_mesh_coverage", 0) >= .95
                        and 0 <= data.get("tolerance_m", float("inf")) <= .01):
                    coverage[gate].update((r["object"], r["support"]) for r in data["contacts"]
                                          if r.get("status") == "pass" and (
                                              data.get("contact_mode") != "solid" or (
                                                  r.get("penetration_check_complete") and r.get("contact_sampling_complete")
                                                  and 0 < data.get("sample_pitch_m", float("inf")) <= .01)))
            if gate == "clearance":
                if (data.get("coordinate_unit") != "m"
                        or not 0 <= data.get("penetration_tolerance_m", float("inf")) <= .01
                        or data.get("skipped_broad_background_count")
                        or data.get("skipped_volumetric_aggregate_count")
                        or not data.get("background_candidate_count")):
                    status = "pending"
                if status == "pass":
                    coverage[gate].update(r["prim"] for r in data["object_bounds"])
            if (
                status == "warn"
                and gate == "usd"
                and data.get("unresolved_dependency_count") == 0
                and not data.get("errors")
            ):
                warnings.extend(data.get("warnings", []))
                status = "pass"
            if gate in {"render", "blender_render"}:
                if data.get("schema_version") not in {
                    "isaac-scene-render-report/v1",
                    "isaac-scene-blender-render/v1",
                }:
                    status = "pending"
                outputs = data.get("outputs", [])
                if not outputs or any(not resolve(p).is_file() for p in outputs):
                    status = "pending"
                hashes = data.get("output_sha256", {})
                if any(
                    not resolve(p).is_file() or hashes.get(p) != sha256(resolve(p))
                    for p in outputs
                ):
                    status = "pending"
                # Reports from renderers must explicitly record successful capture.
                if not data.get("capture_verified"):
                    status = "pending"
                if (
                    scope != "layout_preview"
                    and data.get("profile", {}).get("name") == "preview"
                ):
                    status = "pending"
                if data.get("profile", {}).get("name") == "final" and not data.get("profile_verified"):
                    status = "pending"
                if any(
                    not resolve(p).is_file() or sha256(resolve(p)) != digest
                    for p, digest in data.get("dependency_hashes", {}).items()
                ):
                    status = "pending"
            if gate == "exchange":
                exported = str(resolve(data.get("output", "")))
                valid_export = (
                    blend_identity
                    and identity
                    and data.get("source_sha256") == blend_identity["scene_sha256"]
                    and data.get("source_unchanged")
                    and not data.get("unsupported_nodes")
                    and data.get("output_sha256")
                    and resolve(exported).is_file()
                    and identity.get("files", {}).get(exported)
                    == data.get("output_sha256")
                )
                status = (
                    "pass"
                    if valid_export
                    and data.get("status") == "exported_requires_runtime_validation"
                    else "pending"
                )
            if gate == "dynamics":
                if (
                    data.get("physics_steps", 0) < 240
                    or data.get("rigid_body_count", 0) < 1
                    or not data.get("physics_log_pass")
                ):
                    status = "pending"
            if gate == "visual":
                if report.get("command") == "review-scene-images":
                    if report.get("schema_version") != "isaac-scene-image-review/v1":
                        status = "pending"
                    for axis in ("rendering_quality", "scene_content_quality"):
                        row = data.get(axis, {})
                        score = row.get("score")
                        if type(score) not in (int, float) or not 0 <= score <= 10 or not row.get("reason"):
                            status = "pending"
                if (
                    data.get("verdict") != "accept"
                    or data.get("content_gate", "pass") != "pass"
                ):
                    status = "pending"
                if any(
                    row.get("severity") in {"high", "critical"}
                    for row in data.get("issues", [])
                ):
                    status = "pending"
                inputs = report.get("review_inputs_sha256", {})
                if not inputs or any(
                    not resolve(p).is_file() or sha256(resolve(p)) != digest
                    for p, digest in inputs.items()
                ):
                    status = "pending"
                if not any(
                    p in current_images and current_images[p] == digest
                    for p, digest in inputs.items()
                ):
                    status = "pending"
                if status == "pass":
                    if report.get("command") in {"validate-visual-agent-review", "review-scene-images"}:
                        visual_axes.update(
                            {"rendering_quality", "scene_content_quality"}
                        )
                    else:
                        visual_axes.add(
                            data.get("review_axis", "scene_content_quality")
                        )
            if status != "pass":
                item = {
                    "gate": gate,
                    "code": "GATE_NOT_PASSED",
                    "status": status,
                    "path": str(path),
                }
                (errors if status == "fail" else pending).append(item)
                valid = False
        if valid:
            accepted.append(gate)
    if task.get("target") != "blender":
        targets = task.get("validation_targets", {})
        declared = {
            "support": {(r["object"], r["support"]) for r in targets.get("support_pairs", [])},
            "clearance": set(targets.get("collision_objects", [])),
        }
        for gate, expected in declared.items():
            missing = expected - coverage[gate]
            if not expected or missing:
                if gate in accepted:
                    accepted.remove(gate)
                pending.append({"gate": gate, "code": "VALIDATION_COVERAGE_INCOMPLETE",
                                "declared_count": len(expected), "covered_count": len(expected & coverage[gate]),
                                "missing": sorted(missing)})
    if (
        "visual" in accepted
        and not {"rendering_quality", "scene_content_quality"} <= visual_axes
    ):
        accepted.remove("visual")
        pending.append({"gate": "visual", "code": "BOTH_QUALITY_AXES_REQUIRED"})
    if task.get("asset_gaps") or task.get("open_issues"):
        pending.append(
            {
                "gate": "content",
                "code": "OPEN_ISSUES",
                "asset_gaps": task.get("asset_gaps", []),
                "issues": task.get("open_issues", []),
            }
        )
    status = "fail" if errors else ("pending" if pending else "pass")
    exhausted = int(task.get("iteration", 0)) >= int(task.get("max_iterations", 3))
    return {
        "schema_version": "isaac-scene-task-check/v1",
        "status": status,
        "delivery_scope": scope,
        "accepted_gates": accepted,
        "errors": errors,
        "pending": pending,
        "warnings": warnings,
        "subject": identity,
        "joint_integrity": joint_integrity,
        "next_action": "deliver_scoped_result"
        if status == "pass"
        else (
            "stop_and_preserve_last_good"
            if exhausted
            else "repair_only_failed_or_missing_evidence_then_new_revision"
        ),
        "limitations": [
            "Visual judgment remains external Agent work",
            "Pose stability is not cooperative task execution",
        ],
    }
