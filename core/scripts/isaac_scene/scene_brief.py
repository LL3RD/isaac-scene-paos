from __future__ import annotations

import json
import math
import re
from copy import deepcopy
from pathlib import Path
from typing import Any, Iterable

from isaac_scene.image_geometry import inspect_content_frame
from isaac_scene.room_shells import load_library as load_room_shell_library
from isaac_scene.room_shells import resolve_declared_preset


VALID_INPUT_MODES = {"text", "image", "hybrid"}
VALID_QUALITY_POLICIES = {"layout_only", "realistic", "reference_match"}
VALID_CONFIDENCE = {"high", "medium", "low"}
DIRECTIONAL_TOKENS = {
    "bed", "chair", "desk", "display", "machine", "monitor", "robot",
    "screen", "sofa", "table", "television", "vehicle", "workbench",
}
NON_SHELL_OBJECT_TOKENS = {
    "appliance", "bed", "cabinet", "chair", "computer", "desk", "display",
    "lamp", "machine", "monitor", "plant", "refrigerator", "robot", "screen",
    "sofa", "table", "television", "tv", "vehicle", "workbench",
}
FRONT_AXIS_ANGLE_DEG = {
    "+X": 0.0,
    "+Y": 90.0,
    "-X": 180.0,
    "-Y": -90.0,
}


def _issue(code: str, path: str, message: str) -> dict[str, str]:
    return {"code": code, "path": path, "message": message}


def _safe_id(value: str) -> str:
    parts = re.findall(r"[A-Za-z0-9]+", value)
    return "".join(part[:1].upper() + part[1:] for part in parts) or "Entity"


def _manual_review_override_valid(evidence: dict[str, Any]) -> bool:
    if evidence.get("selection_basis") != "independent_review_override":
        return True
    review = evidence.get("manual_review")
    if not isinstance(review, dict):
        return False
    reviewers = {
        str(value).strip()
        for value in review.get("reviewers") or []
        if str(value).strip()
    }
    comparison_previews = [
        str(value).strip()
        for value in review.get("comparison_preview_paths") or []
        if str(value).strip()
    ]
    features = [
        str(value).strip()
        for value in review.get("reference_features") or []
        if str(value).strip()
    ]
    rejected = review.get("rejected_candidates")
    automatic_failures = review.get("automatic_gate_failures")
    score = evidence.get("visual_score")
    pool_count = evidence.get("candidate_pool_count")
    return all((
        len(reviewers) >= 2,
        len(comparison_previews) >= 2,
        len(features) >= 2,
        isinstance(rejected, list) and len(rejected) >= 1,
        isinstance(automatic_failures, list) and len(automatic_failures) >= 1,
        review.get("identity_gate") == "pass",
        review.get("geometry_gate") == "pass",
        review.get("orientation_gate") == "pass",
        bool(str(review.get("selected_preview_path") or "").strip()),
        bool(str(review.get("rationale") or "").strip()),
        isinstance(score, (int, float)) and float(score) >= 0.24,
        isinstance(pool_count, int) and pool_count >= 2,
    ))


def validate(brief: dict[str, Any]) -> dict[str, Any]:
    errors: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    if brief.get("schema_version") != "isaac-scene-brief/v1":
        errors.append(_issue("BRIEF_SCHEMA_VERSION", "/schema_version", "Expected isaac-scene-brief/v1."))
    inputs = brief.get("inputs") if isinstance(brief.get("inputs"), dict) else {}
    mode = inputs.get("mode")
    if mode not in VALID_INPUT_MODES:
        errors.append(_issue("BRIEF_INPUT_MODE", "/inputs/mode", f"Expected one of {sorted(VALID_INPUT_MODES)}."))
    images = inputs.get("reference_images") if isinstance(inputs.get("reference_images"), list) else []
    if mode in {"image", "hybrid"} and not images:
        errors.append(_issue("BRIEF_REFERENCE_IMAGE_REQUIRED", "/inputs/reference_images", "Image input mode requires a reference image."))
    if mode in {"text", "hybrid"} and not str(inputs.get("text") or "").strip():
        errors.append(_issue("BRIEF_TEXT_REQUIRED", "/inputs/text", "Text or hybrid mode requires text."))
    quality_policy = brief.get("quality_policy")
    if quality_policy not in VALID_QUALITY_POLICIES:
        errors.append(_issue("BRIEF_QUALITY_POLICY", "/quality_policy", f"Expected one of {sorted(VALID_QUALITY_POLICIES)}."))

    room = brief.get("room") if isinstance(brief.get("room"), dict) else {}
    if not room.get("type"):
        errors.append(_issue("BRIEF_ROOM_TYPE", "/room/type", "Room type is required."))
    size = room.get("size_m")
    if not (isinstance(size, list) and len(size) == 3 and all(isinstance(v, (int, float)) and v > 0 for v in size)):
        errors.append(_issue("BRIEF_ROOM_SIZE", "/room/size_m", "Three positive room dimensions are required."))
    if mode in {"image", "hybrid"} and room.get("size_confidence") not in VALID_CONFIDENCE:
        warnings.append(_issue("BRIEF_ROOM_SIZE_CONFIDENCE", "/room/size_confidence", "Image-derived room size should carry confidence."))
    shell_preset = room.get("shell_preset")
    if shell_preset:
        try:
            presets = load_room_shell_library().get("presets") or {}
            if str(shell_preset) not in presets:
                errors.append(_issue(
                    "BRIEF_ROOM_SHELL_PRESET_UNKNOWN",
                    "/room/shell_preset",
                    f"Unknown room-shell preset: {shell_preset}",
                ))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            errors.append(_issue(
                "BRIEF_ROOM_SHELL_LIBRARY_INVALID",
                "/room/shell_preset",
                str(exc),
            ))
    architectural_elements = (
        room.get("architectural_elements")
        if isinstance(room.get("architectural_elements"), list)
        else []
    )
    if quality_policy in {"realistic", "reference_match"}:
        for index, element in enumerate(architectural_elements):
            if not isinstance(element, dict):
                continue
            evidence_text = re.sub(
                r"\s+",
                " ",
                re.sub(
                    r"[^a-z0-9]+",
                    " ",
                    " ".join(
                        str(element.get(key) or "")
                        for key in ("id", "semantic_role", "evidence")
                    ).lower(),
                ),
            ).strip()
            object_tokens = sorted(
                token
                for token in NON_SHELL_OBJECT_TOKENS
                if re.search(
                    rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])",
                    evidence_text,
                )
            )
            if object_tokens:
                errors.append(_issue(
                    "BRIEF_ARCHITECTURAL_OBJECT_PROXY",
                    f"/room/architectural_elements/{index}",
                    "Architectural primitives may only represent the room shell; "
                    f"object-like evidence {object_tokens} must be moved to objects "
                    "with a verified asset binding or reported as ASSET_GAP.",
                ))

    zone_ids: set[str] = set()
    for index, zone in enumerate(brief.get("zones") or []):
        path = f"/zones/{index}"
        if not isinstance(zone, dict) or not str(zone.get("id") or "").strip():
            errors.append(_issue("BRIEF_ZONE_ID", f"{path}/id", "Functional zone id is required."))
            continue
        zone_id = str(zone["id"])
        if zone_id in zone_ids:
            errors.append(_issue("BRIEF_ZONE_DUPLICATE", f"{path}/id", f"Duplicate functional zone: {zone_id}"))
        zone_ids.add(zone_id)
        bounds = zone.get("bounds_norm")
        if not (
            isinstance(bounds, list) and len(bounds) == 4
            and all(isinstance(value, (int, float)) for value in bounds)
            and 0 <= float(bounds[0]) < float(bounds[2]) <= 1
            and 0 <= float(bounds[1]) < float(bounds[3]) <= 1
        ):
            errors.append(_issue(
                "BRIEF_ZONE_BOUNDS",
                f"{path}/bounds_norm",
                "Expected normalized [xmin,ymin,xmax,ymax] with positive area.",
            ))
    for index, rule in enumerate(brief.get("layout_rules") or []):
        path = f"/layout_rules/{index}"
        if not isinstance(rule, dict) or not str(rule.get("id") or "").strip():
            errors.append(_issue("BRIEF_LAYOUT_RULE_ID", f"{path}/id", "Layout rule id is required."))
            continue
        if not str(rule.get("zone") or "").strip():
            errors.append(_issue("BRIEF_LAYOUT_RULE_ZONE", f"{path}/zone", "Layout rule zone is required."))
        elif zone_ids and str(rule["zone"]) not in zone_ids:
            errors.append(_issue("BRIEF_LAYOUT_RULE_ZONE_UNKNOWN", f"{path}/zone", f"Unknown brief zone: {rule['zone']}"))

    functional_group_ids: set[str] = set()
    for index, group in enumerate(brief.get("functional_groups") or []):
        path = f"/functional_groups/{index}"
        if not isinstance(group, dict) or not str(group.get("id") or "").strip():
            errors.append(_issue("BRIEF_FUNCTIONAL_GROUP_ID", f"{path}/id", "Functional group id is required."))
            continue
        group_id = str(group["id"])
        if group_id in functional_group_ids:
            errors.append(_issue("BRIEF_FUNCTIONAL_GROUP_DUPLICATE", f"{path}/id", f"Duplicate functional group: {group_id}"))
        functional_group_ids.add(group_id)
        if not isinstance(group.get("anchor_match"), dict):
            errors.append(_issue("BRIEF_FUNCTIONAL_GROUP_ANCHOR", f"{path}/anchor_match", "Functional group anchor_match is required."))
        if group.get("atomic") not in (None, True, False):
            errors.append(_issue("BRIEF_FUNCTIONAL_GROUP_ATOMIC", f"{path}/atomic", "atomic must be boolean."))
        members = group.get("members")
        if not isinstance(members, list) or not members:
            errors.append(_issue("BRIEF_FUNCTIONAL_GROUP_MEMBERS", f"{path}/members", "Functional group members are required."))
            continue
        for member_index, member in enumerate(members):
            member_path = f"{path}/members/{member_index}"
            if not isinstance(member, dict) or not isinstance(member.get("match"), dict):
                errors.append(_issue("BRIEF_FUNCTIONAL_GROUP_MATCH", f"{member_path}/match", "Member match is required."))
                continue
            per_anchor = member.get("per_anchor", 1)
            offsets = member.get("offsets_m")
            if not isinstance(per_anchor, int) or per_anchor < 1:
                errors.append(_issue("BRIEF_FUNCTIONAL_GROUP_COUNT", f"{member_path}/per_anchor", "per_anchor must be a positive integer."))
            elif not isinstance(offsets, list) or len(offsets) < per_anchor:
                errors.append(_issue("BRIEF_FUNCTIONAL_GROUP_OFFSETS", f"{member_path}/offsets_m", "Provide one two-dimensional offset per member slot."))
            if member.get("offset_reference") not in (None, "placement_reference", "asset_origin"):
                errors.append(_issue(
                    "BRIEF_FUNCTIONAL_GROUP_OFFSET_REFERENCE",
                    f"{member_path}/offset_reference",
                    "offset_reference must be placement_reference or asset_origin.",
                ))
            connection = member.get("connection")
            if connection is not None:
                if not isinstance(connection, dict):
                    errors.append(_issue("BRIEF_FUNCTIONAL_GROUP_CONNECTION", f"{member_path}/connection", "connection must be an object."))
                else:
                    for key in ("anchor_port_local_m", "member_port_local_m"):
                        value = connection.get(key)
                        if not (isinstance(value, list) and len(value) == 3 and all(isinstance(item, (int, float)) for item in value)):
                            errors.append(_issue("BRIEF_FUNCTIONAL_GROUP_CONNECTION_PORT", f"{member_path}/connection/{key}", "Expected [x,y,z] local port."))
                    semantic_ports: list[dict[str, Any]] = []
                    for key in ("anchor_port", "member_port"):
                        port = connection.get(key)
                        if port is None:
                            continue
                        if not isinstance(port, dict):
                            errors.append(_issue(
                                "BRIEF_FUNCTIONAL_GROUP_PORT_SEMANTICS",
                                f"{member_path}/connection/{key}",
                                "Semantic port evidence must be an object.",
                            ))
                            continue
                        semantic_ports.append(port)
                        if port.get("role") not in {"input", "output", "bidirectional", "service", "operator"}:
                            errors.append(_issue(
                                "BRIEF_FUNCTIONAL_GROUP_PORT_ROLE",
                                f"{member_path}/connection/{key}/role",
                                "Port role must be input, output, bidirectional, service, or operator.",
                            ))
                        if port.get("normal_axis") not in FRONT_AXIS_ANGLE_DEG:
                            errors.append(_issue(
                                "BRIEF_FUNCTIONAL_GROUP_PORT_NORMAL",
                                f"{member_path}/connection/{key}/normal_axis",
                                "Port normal_axis must be one of +X, -X, +Y, -Y.",
                            ))
                        if port.get("evidence_status") not in {"verified", "pending", "rejected"}:
                            errors.append(_issue(
                                "BRIEF_FUNCTIONAL_GROUP_PORT_EVIDENCE",
                                f"{member_path}/connection/{key}/evidence_status",
                                "Port evidence_status must be verified, pending, or rejected.",
                            ))
                    if connection.get("require_semantic_evidence"):
                        if len(semantic_ports) != 2 or any(port.get("evidence_status") != "verified" for port in semantic_ports):
                            errors.append(_issue(
                                "BRIEF_FUNCTIONAL_GROUP_PORT_EVIDENCE_REQUIRED",
                                f"{member_path}/connection",
                                "Both connection ports require verified semantic evidence.",
                            ))
                        elif any(not str(port.get("evidence_source") or "").strip() for port in semantic_ports):
                            errors.append(_issue(
                                "BRIEF_FUNCTIONAL_GROUP_PORT_EVIDENCE_SOURCE",
                                f"{member_path}/connection",
                                "Verified semantic ports require a non-empty evidence_source.",
                            ))
                    if connection.get("require_opposed_normals") and len(semantic_ports) != 2:
                        errors.append(_issue(
                            "BRIEF_FUNCTIONAL_GROUP_PORT_NORMALS_REQUIRED",
                            f"{member_path}/connection",
                            "Opposed-normal validation requires anchor_port and member_port semantics.",
                        ))
            envelope = member.get("work_envelope")
            if envelope is not None and (
                not isinstance(envelope, dict) or not isinstance(envelope.get("radius_m"), (int, float))
                or float(envelope["radius_m"]) <= 0.0
            ):
                errors.append(_issue("BRIEF_FUNCTIONAL_GROUP_WORK_ENVELOPE", f"{member_path}/work_envelope", "work_envelope.radius_m must be positive."))

    camera = brief.get("camera") if isinstance(brief.get("camera"), dict) else {}
    if mode in {"image", "hybrid"}:
        if not camera:
            errors.append(_issue("BRIEF_CAMERA_REQUIRED", "/camera", "Reference reconstruction requires a comparison camera hypothesis."))
        elif camera.get("confidence") not in VALID_CONFIDENCE:
            warnings.append(_issue("BRIEF_CAMERA_CONFIDENCE", "/camera/confidence", "Image-derived camera should carry confidence."))

    objects = brief.get("objects") if isinstance(brief.get("objects"), list) else []
    if not objects:
        warnings.append(_issue("BRIEF_OBJECTS_EMPTY", "/objects", "No objects have been observed or requested."))
    ids: set[str] = set()
    all_ids: set[str] = set()
    for item in objects:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        logical_id = str(item["id"])
        all_ids.add(logical_id)
        count = item.get("count", 1)
        if isinstance(count, int) and count > 1:
            all_ids.update(f"{logical_id}_{index:02d}" for index in range(1, count + 1))
    for index, item in enumerate(objects):
        path = f"/objects/{index}"
        if not isinstance(item, dict):
            errors.append(_issue("BRIEF_OBJECT_TYPE", path, "Object must be a JSON object."))
            continue
        for key in ("id", "role", "semantic_class"):
            if not item.get(key):
                errors.append(_issue("BRIEF_OBJECT_FIELD", f"{path}/{key}", f"{key} is required."))
        item_id = str(item.get("id") or "")
        if item_id in ids:
            errors.append(_issue("BRIEF_DUPLICATE_OBJECT_ID", f"{path}/id", f"Duplicate object id: {item_id}"))
        ids.add(item_id)
        if mode in {"image", "hybrid"} and item.get("confidence") not in VALID_CONFIDENCE:
            warnings.append(_issue("BRIEF_OBJECT_CONFIDENCE", f"{path}/confidence", "Image-derived object should carry confidence."))
        bbox = item.get("screen_bbox_norm")
        if bbox is not None and not (
            isinstance(bbox, list) and len(bbox) == 4
            and all(isinstance(v, (int, float)) and 0 <= v <= 1 for v in bbox)
        ):
            errors.append(_issue("BRIEF_SCREEN_BBOX", f"{path}/screen_bbox_norm", "Expected normalized [x,y,w,h]."))
        for relation_key in ("support_target", "facing_target"):
            target = item.get(relation_key)
            if target and str(target) not in all_ids and str(target) not in {"floor", "wall", "ceiling"}:
                warnings.append(_issue("BRIEF_RELATION_TARGET_UNKNOWN", f"{path}/{relation_key}", f"Unknown target: {target}"))
        if item.get("criticality") == "anchor" and not item.get("size_m"):
            warnings.append(_issue("BRIEF_ANCHOR_SIZE_UNKNOWN", f"{path}/size_m", "Anchor size is needed for stable layout."))
        if quality_policy == "reference_match" and item.get("criticality") == "anchor":
            binding = (
                item.get("asset_binding")
                if isinstance(item.get("asset_binding"), dict)
                else {}
            )
            if not binding.get("path") or not (
                binding.get("logical_id") or binding.get("model_id")
            ):
                errors.append(_issue(
                    "BRIEF_ANCHOR_PROVENANCE_REQUIRED",
                    f"{path}/asset_binding",
                    "Reference anchors require a resolved path and stable logical_id "
                    "or model_id; a path-only asset is not provenance evidence.",
                ))
            match_evidence = (
                binding.get("reference_match_evidence")
                if isinstance(binding.get("reference_match_evidence"), dict)
                else {}
            )
            if (
                match_evidence.get("status") != "pass"
                or not str(match_evidence.get("preview_index_path") or "").strip()
            ):
                errors.append(_issue(
                    "BRIEF_ANCHOR_VISUAL_GATE_REQUIRED",
                    f"{path}/asset_binding/reference_match_evidence",
                    "Reference anchors require status=pass and a persisted preview "
                    "index. A top candidate that failed score, margin, coverage, or "
                    "front-view inspection must remain ASSET_GAP.",
                ))
            elif not _manual_review_override_valid(match_evidence):
                errors.append(_issue(
                    "BRIEF_MANUAL_REVIEW_OVERRIDE_INVALID",
                    f"{path}/asset_binding/reference_match_evidence/manual_review",
                    "An independent-review override requires at least two distinct "
                    "reviewers, two comparison previews, two observed reference "
                    "features, rejected candidates, recorded automatic gate failures, "
                    "passing identity/geometry/orientation gates, a selected preview "
                    "and rationale, score>=0.24, and candidate_pool_count>=2.",
                ))
    status = "fail" if errors else ("warn" if warnings else "pass")
    return {
        "schema_version": "isaac-scene-brief-validation/v1",
        "status": status,
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "metrics": {
            "object_count": len(objects),
            "anchor_count": sum(1 for item in objects if isinstance(item, dict) and item.get("criticality") == "anchor"),
            "reference_image_count": len(images),
        },
    }


def initialize_brief(
    *,
    text: str | None,
    reference_images: Iterable[str | Path],
    domain: str,
    room_type: str,
    room_size_m: list[float],
    quality_policy: str,
) -> dict[str, Any]:
    images = []
    for index, value in enumerate(reference_images):
        path = Path(value)
        row: dict[str, Any] = {"path": str(path), "view_id": f"view_{index + 1:02d}"}
        try:
            frame = inspect_content_frame(path)
            row.update({
                key: frame[key]
                for key in (
                    "width_px", "height_px", "content_bbox_px", "content_bbox_norm",
                    "content_width_px", "content_height_px", "content_aspect_ratio",
                    "border_detected", "border_removed_fraction", "confidence",
                )
            })
        except Exception:
            pass
        images.append(row)
    mode = "hybrid" if text and images else ("image" if images else "text")
    first_frame = images[0] if images else {}
    content_aspect = float(first_frame.get("content_aspect_ratio") or (16.0 / 9.0))
    render_width = 1280
    render_height = max(1, round(render_width / content_aspect))
    return {
        "schema_version": "isaac-scene-brief/v1",
        "inputs": {
            "mode": mode,
            "text": text or "",
            "reference_images": images,
        },
        "domain": domain,
        "quality_policy": quality_policy,
        "room": {
            "type": room_type,
            "size_m": [float(value) for value in room_size_m],
            "size_confidence": "low" if images else "medium",
            "style_tags": [],
            "floor": {"material_tags": [], "color_rgb": [0.5, 0.5, 0.5]},
            "walls": {"material_tags": [], "color_rgb": [0.75, 0.75, 0.75]},
            "open_sides": ["front"],
            "openings": [],
        },
        "camera": {
            "projection": "perspective",
            "position_m": [0.0, -float(room_size_m[1]) * 0.75, float(room_size_m[2]) * 0.7],
            "look_at_m": [0.0, 0.0, float(room_size_m[2]) * 0.35],
            "up_axis": "+Z",
            "focal_length_mm": 28.0,
            "target_aspect_ratio": content_aspect,
            "render_resolution_px": [render_width, render_height],
            "confidence": "low",
            "evidence": "initial comparison-camera hypothesis; harness must refine from the reference image",
        } if images else {},
        "objects": [],
        "zones": [],
        "functional_groups": [],
        "circulation": {
            "minimum_path_width_m": 0.8,
            "keep_clear_zones": [],
        },
        "assumptions": ["Room dimensions and camera are initial hypotheses until verified."],
        "unresolved_questions": ["Identify visible anchor objects and refine camera/room evidence."],
    }


def _surface_color(room: dict[str, Any], name: str, fallback: list[float]) -> list[float]:
    surface = room.get(name) if isinstance(room.get(name), dict) else {}
    raw = surface.get("color_rgb")
    if isinstance(raw, list) and len(raw) == 3:
        return [float(value) for value in raw]
    return fallback


def _surface_material(room: dict[str, Any], name: str) -> dict[str, Any]:
    surface = room.get(name) if isinstance(room.get(name), dict) else {}
    mapping = {
        "roughness": "material_roughness",
        "metallic": "material_metallic",
        "opacity": "material_opacity",
        "emissive_color": "emissive_color",
    }
    return {
        target: deepcopy(surface[source])
        for source, target in mapping.items()
        if source in surface
    }


def _room_shell(room: dict[str, Any]) -> list[dict[str, Any]]:
    width, depth, height = [float(value) for value in room["size_m"]]
    center = room.get("center_m") if isinstance(room.get("center_m"), list) and len(room["center_m"]) == 3 else [0.0, 0.0, 0.0]
    center_x, center_y, floor_z = [float(value) for value in center]
    thickness = min(0.12, max(0.05, min(width, depth) * 0.015))
    open_sides = set(room.get("open_sides") or ["front"])
    floor_color = _surface_color(room, "floor", [0.5, 0.5, 0.5])
    wall_color = _surface_color(room, "walls", [0.75, 0.75, 0.75])
    rows = [{
        "id": "floor",
        "type": "box",
        "prim": "/World/Colliders/Floor",
        "translate": [center_x, center_y, floor_z - thickness / 2],
        "size": [width, depth, thickness],
        "display_color": floor_color,
        "physics": "static",
        **_surface_material(room, "floor"),
    }]
    definitions = {
        "back": ([center_x, center_y + depth / 2, floor_z + height / 2], [width, thickness, height]),
        "front": ([center_x, center_y - depth / 2, floor_z + height / 2], [width, thickness, height]),
        "left": ([center_x - width / 2, center_y, floor_z + height / 2], [thickness, depth, height]),
        "right": ([center_x + width / 2, center_y, floor_z + height / 2], [thickness, depth, height]),
    }
    for side, (translate, size) in definitions.items():
        if side in open_sides:
            continue
        rows.append({
            "id": f"{side}_wall",
            "type": "box",
            "prim": f"/World/Colliders/{_safe_id(side)}Wall",
            "translate": translate,
            "size": size,
            "display_color": wall_color,
            "physics": "static",
            **_surface_material(room, "walls"),
        })
    if isinstance(room.get("ceiling"), dict) and "ceiling" not in open_sides:
        rows.append({
            "id": "ceiling",
            "type": "box",
            "prim": "/World/Architecture/Ceiling",
            "translate": [center_x, center_y, floor_z + height + thickness / 2],
            "size": [width, depth, thickness],
            "display_color": _surface_color(room, "ceiling", [0.72, 0.72, 0.72]),
            "physics": "static",
            **_surface_material(room, "ceiling"),
        })
    for index, element in enumerate(room.get("architectural_elements") or []):
        if not isinstance(element, dict):
            continue
        row = deepcopy(element)
        row.setdefault("id", f"architectural_element_{index + 1:02d}")
        row.setdefault("type", "box")
        row.setdefault("prim", f"/World/Architecture/{_safe_id(str(row['id']))}")
        row.setdefault("physics", "static")
        rows.append(row)
    return rows


def _lighting_rig(brief: dict[str, Any], room: dict[str, Any]) -> list[dict[str, Any]]:
    lighting = brief.get("lighting") if isinstance(brief.get("lighting"), dict) else None
    if lighting is None:
        return [
            {
                "type": "dome",
                "prim": "/World/Lights/Dome",
                "intensity": 450.0,
                "color": [0.9, 0.94, 1.0],
            },
            {
                "type": "rect",
                "prim": "/World/Lights/Key",
                "translate": [0.0, 0.0, room["size_m"][2] * 0.85],
                "width": room["size_m"][0] * 0.6,
                "height": room["size_m"][1] * 0.5,
                "intensity": 1800.0,
                "color": [1.0, 0.9, 0.78],
            },
        ]

    lights: list[dict[str, Any]] = []
    environment = (
        lighting.get("environment")
        if isinstance(lighting.get("environment"), dict)
        else {"intensity": 450.0, "color": [0.9, 0.94, 1.0]}
    )
    if environment.get("enabled", True):
        dome = {
            "type": "dome",
            "prim": str(environment.get("prim") or "/World/Lights/Dome"),
            "intensity": float(environment.get("intensity") or 0.0),
            "color": deepcopy(environment.get("color") or [1.0, 1.0, 1.0]),
        }
        if environment.get("texture"):
            dome["texture"] = str(environment["texture"])
        lights.append(dome)

    allowed = {
        "type",
        "prim",
        "translate",
        "rotate",
        "look_at",
        "width",
        "height",
        "radius",
        "angle",
        "intensity",
        "color",
        "temperature",
        "exposure",
    }
    for index, fixture in enumerate(lighting.get("fixtures") or []):
        if not isinstance(fixture, dict):
            continue
        light = {
            key: deepcopy(value)
            for key, value in fixture.items()
            if key in allowed
        }
        light.setdefault("type", "rect")
        light.setdefault(
            "prim",
            f"/World/Lights/{_safe_id(str(fixture.get('id') or f'Fixture{index + 1}'))}",
        )
        light.setdefault("intensity", 1000.0)
        light.setdefault("color", [1.0, 1.0, 1.0])
        lights.append(light)
    return lights


def _is_directional(item: dict[str, Any]) -> bool:
    text = " ".join(str(item.get(key) or "").lower() for key in ("role", "semantic_class", "semantic_name"))
    return any(token in text for token in DIRECTIONAL_TOKENS)


def _has_orientation_evidence(binding: dict[str, Any]) -> bool:
    if binding.get("front_axis") in FRONT_AXIS_ANGLE_DEG:
        return True
    mode = str(binding.get("orientation_mode") or "unknown")
    if mode in {"radial", "symmetric"}:
        return True
    if mode in {"axial", "bidirectional"}:
        return binding.get("primary_axis") in {"X", "Y"}
    return False


def solve_facing_yaw(
    subject_translate: Iterable[float],
    target_translate: Iterable[float],
    front_axis: str,
) -> float | None:
    if front_axis not in FRONT_AXIS_ANGLE_DEG:
        return None
    subject = [float(value) for value in subject_translate]
    target = [float(value) for value in target_translate]
    if len(subject) != 3 or len(target) != 3:
        return None
    dx = target[0] - subject[0]
    dy = target[1] - subject[1]
    if math.hypot(dx, dy) <= 1e-6:
        return None
    desired_world_angle = math.degrees(math.atan2(dy, dx))
    yaw = desired_world_angle - FRONT_AXIS_ANGLE_DEG[front_axis]
    normalized = (yaw + 180.0) % 360.0 - 180.0
    return 180.0 if math.isclose(normalized, -180.0) else normalized


def nearest_target_footprint_point(
    subject_translate: Iterable[float],
    target_translate: Iterable[float],
    target_size_m: Iterable[float],
    target_rotate_deg: Iterable[float],
) -> list[float] | None:
    subject = [float(value) for value in subject_translate]
    target = [float(value) for value in target_translate]
    size = [float(value) for value in target_size_m]
    rotate = [float(value) for value in target_rotate_deg]
    if len(subject) != 3 or len(target) != 3 or len(size) != 3 or len(rotate) != 3:
        return None
    if size[0] <= 0 or size[1] <= 0:
        return None
    radians = math.radians(rotate[2])
    cosine = math.cos(radians)
    sine = math.sin(radians)
    dx = subject[0] - target[0]
    dy = subject[1] - target[1]
    local_x = cosine * dx + sine * dy
    local_y = -sine * dx + cosine * dy
    nearest_x = min(max(local_x, -size[0] / 2.0), size[0] / 2.0)
    nearest_y = min(max(local_y, -size[1] / 2.0), size[1] / 2.0)
    # An object inside the footprint has no meaningful closest boundary for a
    # facing relation; keep the established center-facing behavior.
    if (
        math.isclose(nearest_x, local_x, abs_tol=1e-6)
        and math.isclose(nearest_y, local_y, abs_tol=1e-6)
    ):
        return list(target)
    return [
        target[0] + cosine * nearest_x - sine * nearest_y,
        target[1] + sine * nearest_x + cosine * nearest_y,
        target[2],
    ]


def _automatic_facing_mode(target_entity: dict[str, Any]) -> str:
    semantic = " ".join(
        str(target_entity.get(key) or "").lower()
        for key in ("semantic_class", "semantic_name")
    )
    surface_tokens = ("table", "desk", "workbench", "counter", "surface")
    size = (target_entity.get("layout_hint") or {}).get("size_m")
    if any(token in semantic for token in surface_tokens) and size:
        return "nearest_target_footprint"
    return "target_center"


def _instances(item: dict[str, Any]) -> list[dict[str, Any]]:
    explicit = item.get("instances")
    if isinstance(explicit, list) and explicit:
        return [row if isinstance(row, dict) else {} for row in explicit]
    count = max(1, int(item.get("count") or 1))
    return [{"id": item["id"] if count == 1 else f"{item['id']}_{index + 1:02d}"} for index in range(count)]


def compile_to_scene_spec(brief: dict[str, Any], *, scene_output: str | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    brief = resolve_declared_preset(brief)
    report = validate(brief)
    if not report["valid"]:
        raise ValueError(json.dumps(report["errors"], ensure_ascii=False))
    room = brief["room"]
    entities: list[dict[str, Any]] = []
    relations: list[dict[str, Any]] = []
    unresolved: list[dict[str, str]] = []
    id_to_prim: dict[str, str] = {}
    object_items: list[tuple[dict[str, Any], dict[str, Any], str]] = []

    for item in brief.get("objects", []):
        binding = item.get("asset_binding") if isinstance(item.get("asset_binding"), dict) else {}
        for instance in _instances(item):
            instance_id = str(instance.get("id") or item["id"])
            prim = f"/World/Objects/{_safe_id(instance_id)}"
            id_to_prim[instance_id] = prim
            entity = {
                "id": instance_id,
                "role": item["role"],
                "semantic_class": item["semantic_class"],
                "canonical_class": (
                    binding.get("canonical_class") or item["semantic_class"]
                ),
                "semantic_name": item.get("semantic_name") or item["semantic_class"].rsplit("/", 1)[-1],
                "prim": prim,
                "criticality": item.get("criticality", "medium"),
                "visual": deepcopy(item.get("visual") or {}),
                "screen_bbox_norm": instance.get("screen_bbox_norm", item.get("screen_bbox_norm")),
                "layout_hint": {
                    key: deepcopy(item[key])
                    for key in ("size_m", "depth_order", "zone", "wall_affinity", "minimum_clearance_m")
                    if key in item
                },
            }
            if item.get("placement_policy"):
                entity["placement_policy"] = str(item["placement_policy"])
            size_m = item.get("size_m")
            if isinstance(size_m, list) and len(size_m) == 3:
                entity["asset_bounds_m"] = {"size": [float(value) for value in size_m]}
            if binding.get("logical_id"):
                entity["asset_id"] = binding["logical_id"]
                entity["logical_id"] = binding["logical_id"]
            if binding.get("model_id"):
                entity["model_id"] = binding["model_id"]
            if binding.get("path"):
                entity["path"] = binding["path"]
            for source_key, target_key in (
                ("asset_source", "asset_source"),
                ("license_status", "license_status"),
                ("license_evidence", "license_evidence"),
            ):
                if binding.get(source_key) is not None:
                    entity[target_key] = deepcopy(binding[source_key])
            if binding.get("query"):
                entity["query"] = binding["query"]
            elif not binding.get("logical_id") and not binding.get("path"):
                visual = item.get("visual") if isinstance(item.get("visual"), dict) else {}
                query_terms = [
                    item.get("semantic_name") or item["semantic_class"].replace("/", " "),
                    *visual.get("style_tags", []),
                    *visual.get("material_tags", []),
                    *visual.get("color_tags", []),
                    *visual.get("shape_tags", []),
                ]
                entity["query"] = " ".join(str(value) for value in query_terms if value)
            if not binding.get("logical_id") and not binding.get("path"):
                unresolved.append({"entity": instance_id, "reason": "asset_resolution_required"})
            for key in (
                "front_axis",
                "orientation_mode",
                "primary_axis",
                "operator_axis",
                "flow_axis",
                "orientation_confidence",
                "orientation_source",
                "scale",
                "unit_mode",
                "allow_unit_mismatch",
                "source_meters_per_unit",
                "source_units_verified",
            ):
                if key in binding:
                    entity[key] = deepcopy(binding[key])
            if instance.get("translate_m") is not None:
                entity["translate"] = deepcopy(instance["translate_m"])
            else:
                unresolved.append({"entity": instance_id, "reason": "layout_solver_required"})
            if instance.get("rotate_deg") is not None:
                entity["rotate"] = deepcopy(instance["rotate_deg"])
            if _is_directional(item) and not _has_orientation_evidence(binding):
                unresolved.append({"entity": instance_id, "reason": "orientation_verification_required"})
            entities.append(entity)
            object_items.append((item, entity, instance_id))

    entity_by_id = {str(entity["id"]): entity for _item, entity, _instance_id in object_items}
    first_entity_by_group: dict[str, dict[str, Any]] = {}
    entities_by_group: dict[str, list[dict[str, Any]]] = {}
    for item, entity, _instance_id in object_items:
        first_entity_by_group.setdefault(str(item["id"]), entity)
        entities_by_group.setdefault(str(item["id"]), []).append(entity)

    support_group_indices: dict[str, int] = {}

    for item, entity, instance_id in object_items:
        support = item.get("support_target")
        if support:
            support_id = str(support)
            support_prim = id_to_prim.get(support_id)
            if support_prim is None and support_id in entities_by_group:
                candidates = entities_by_group[support_id]
                index = support_group_indices.get(support_id, 0)
                support_prim = str(candidates[index % len(candidates)]["prim"])
                support_group_indices[support_id] = index + 1
            entity["support_target"] = support_prim or support_id
            relations.append({
                "type": "on",
                "subject": instance_id,
                "target": str(support),
                "resolved_transform": entity.get("translate"),
            })
        facing = item.get("facing_target")
        if facing:
            target_id = str(facing)
            target_entity = entity_by_id.get(target_id) or first_entity_by_group.get(target_id)
            solved_yaw = None
            facing_mode = str(item.get("facing_mode") or "auto")
            solved_target_point = None
            if target_entity is not None:
                if facing_mode == "auto":
                    facing_mode = _automatic_facing_mode(target_entity)
                solved_target_point = list(target_entity.get("translate") or [])
                if facing_mode == "nearest_target_footprint":
                    projected = nearest_target_footprint_point(
                        entity.get("translate") or [],
                        target_entity.get("translate") or [],
                        (target_entity.get("layout_hint") or {}).get("size_m") or [],
                        target_entity.get("rotate") or [0.0, 0.0, 0.0],
                    )
                    if projected is not None:
                        solved_target_point = projected
                solved_yaw = solve_facing_yaw(
                    entity.get("translate") or [],
                    solved_target_point,
                    str(entity.get("front_axis") or ""),
                )
            if solved_yaw is not None:
                requested_rotate = [
                    float(value)
                    for value in (entity.get("rotate") or [0.0, 0.0, 0.0])
                ]
                if len(requested_rotate) != 3:
                    requested_rotate = [0.0, 0.0, 0.0]
                solved_rotate = [requested_rotate[0], requested_rotate[1], solved_yaw]
                entity["rotate"] = solved_rotate
                entity["orientation_solver"] = {
                    "method": (
                        "front_axis_to_nearest_target_footprint"
                        if facing_mode == "nearest_target_footprint"
                        else "front_axis_to_facing_target"
                    ),
                    "front_axis": entity["front_axis"],
                    "target": target_id,
                    "facing_mode": facing_mode,
                    "solved_target_point": solved_target_point,
                    "requested_yaw_deg": requested_rotate[2],
                    "solved_yaw_deg": solved_yaw,
                }
            relations.append({
                "type": "faces",
                "subject": instance_id,
                "target": target_id,
                "max_angle_deg": 20.0,
                "resolved_transform": entity.get("rotate"),
            })
            if target_id in id_to_prim:
                entity["facing_target"] = id_to_prim[target_id]
        wall = item.get("wall_affinity")
        if wall and wall != "none":
            relations.append({
                "type": "against_wall",
                "subject": instance_id,
                "target": str(wall),
                "minimum_clearance_m": float(item.get("minimum_clearance_m") or 0.05),
                "resolved_transform": entity.get("translate"),
            })

    camera = brief.get("camera") if isinstance(brief.get("camera"), dict) else {}
    scene_spec: dict[str, Any] = {
        "schema_version": "isaac-scene-spec/v2",
        "domain": brief["domain"],
        "stage": {"up_axis": "Z", "meters_per_unit": 1.0, "unit_policy": "normalize_references"},
        "room": {
            "type": room["type"],
            "size_m": deepcopy(room["size_m"]),
            "center_m": deepcopy(room.get("center_m") or [0.0, 0.0, 0.0]),
            "style_tags": deepcopy(room.get("style_tags") or []),
            "quality_target": brief["quality_policy"],
            "shell_preset": room.get("shell_preset"),
            "shell_preset_resolved": deepcopy(room.get("shell_preset_resolved")),
        },
        "entities": entities,
        "relations": relations,
        "constraints": {
            "room_bounds_m": deepcopy(room["size_m"]),
            "minimum_path_width_m": float((brief.get("circulation") or {}).get("minimum_path_width_m") or 0.8),
            "zones": deepcopy(brief.get("zones") or []),
            "unresolved": unresolved,
            "input_provenance": deepcopy(brief["inputs"]),
        },
        "validation_targets": [
            "assets_resolve", "plausible_bounds", "no_severe_overlap",
            "support_contact", "directional_facing<=20deg", "render_feedback",
        ],
        "render_feedback_policy": {
            "views": ["perspective", "top_down"],
            "max_iterations": 2,
            "preserve_geometry": True,
        },
        "support_colliders": _room_shell(room),
        "lights": _lighting_rig(brief, room),
        "lighting_policy": deepcopy(brief.get("lighting") or {"mode": "default"}),
        "brief_snapshot": deepcopy(brief),
    }
    if scene_output:
        scene_spec["output_path"] = scene_output
    if camera:
        scene_spec["camera"] = {
            "prim": "/World/Cameras/Perspective",
            "translate": deepcopy(camera.get("position_m") or [0.0, -5.0, 2.5]),
            "look_at": deepcopy(camera.get("look_at_m") or [0.0, 0.0, 1.0]),
            "focal_length": float(camera.get("focal_length_mm") or 28.0),
            "target_aspect_ratio": float(camera.get("target_aspect_ratio") or (16.0 / 9.0)),
            "render_resolution": deepcopy(camera.get("render_resolution_px") or [1280, 720]),
            "clipping_range": [0.01, 1000.0],
            "confidence": camera.get("confidence"),
            "evidence": camera.get("evidence"),
        }
        if camera.get("exposure_ev") is not None:
            scene_spec["camera"]["exposure_ev"] = float(camera["exposure_ev"])
    observation = {
        "schema_version": "isaac-scene-brief-compile/v1",
        "status": "warn" if unresolved else "pass",
        "entity_count": len(entities),
        "relation_count": len(relations),
        "unresolved_count": len(unresolved),
        "unresolved": unresolved,
        "brief_validation": report,
    }
    return scene_spec, observation
