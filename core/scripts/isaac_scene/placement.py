from __future__ import annotations

from copy import deepcopy
import math
import re
from typing import Any

from isaac_scene.room_shells import resolve_declared_preset
from isaac_scene.scene_brief import _lighting_rig as build_lighting_rig
from isaac_scene.scene_brief import _room_shell as build_room_shell


FLOOR_TARGETS = {"floor", "ground", "room_floor", "/World/Floor", "/World/Room/Floor"}

PRIVILEGED_LAYOUT_FIELDS = {
    "xform_matrix",
    "source_world_matrix",
    "source_xform_ops",
    "wrapper_normalization_matrix",
    "regression_alignment",
    "regression_instance_opinions",
}

DEFAULT_ARCHITECTURAL_PATH_TOKENS = (
    "wall", "window", "glazing", "glass", "mullion", "transom", "blind",
    "door", "frame", "baseboard", "skirting", "partition", "ceiling", "roof",
    "floor", "column", "pillar", "beam", "stair", "railing",
    "light", "lamp", "cabinet",
)


def _safe_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]+", "_", value).strip("_") or "Asset"


def entity_prim(entity: dict[str, Any]) -> str:
    """Return the composer prim path for a Scene Spec entity."""
    if entity.get("prim"):
        return str(entity["prim"])
    return f"/World/Objects/{_safe_id(str(entity.get('id') or 'Asset'))}"


def architectural_obstacle_root_for_spec(spec: dict[str, Any]) -> str | None:
    """Return the composed architecture root for asset-backed or procedural rooms."""
    scene_root = next(
        (
            item for item in spec.get("entities") or []
            if isinstance(item, dict) and item.get("asset_scope") == "scene_root"
        ),
        None,
    )
    if scene_root is not None:
        return entity_prim(scene_root)
    room = spec.get("room") if isinstance(spec.get("room"), dict) else {}
    if room.get("size_m") or room.get("usable_bounds_m") or spec.get("support_colliders"):
        return "/World/Architecture"
    return None


def _support_collider_prim(item: dict[str, Any]) -> str:
    if item.get("prim"):
        return str(item["prim"])
    return f"/World/Colliders/{_safe_id(str(item.get('id') or 'SupportCollider'))}"


def _target_owner(
    target: str,
    entities: list[dict[str, Any]],
) -> str | None:
    for entity in entities:
        if target == str(entity.get("id") or ""):
            return str(entity["id"])
    if not target.startswith("/"):
        return None
    matches = [
        (len(entity_prim(entity)), str(entity["id"]))
        for entity in entities
        if target == entity_prim(entity) or target.startswith(entity_prim(entity) + "/")
    ]
    return max(matches)[1] if matches else None


def _bounds_size(entity: dict[str, Any]) -> list[float]:
    bounds = entity.get("asset_bounds_m") or (
        (entity.get("regression_alignment") or {}).get("wrapper_local_bounds_m")
    ) or {}
    size = bounds.get("size")
    if isinstance(size, list) and len(size) >= 3:
        return [abs(float(value)) for value in size[:3]]
    lower, upper = bounds.get("min"), bounds.get("max")
    if isinstance(lower, list) and isinstance(upper, list) and len(lower) >= 3 and len(upper) >= 3:
        return [abs(float(upper[index]) - float(lower[index])) for index in range(3)]
    return [0.0, 0.0, 0.0]


def _footprint_area(entity: dict[str, Any]) -> float:
    size = _bounds_size(entity)
    return size[0] * size[1]


def _clamp_footprint_center_to_room(
    entity: dict[str, Any],
    xy: list[float],
    room_bounds: dict[str, Any],
    room_margin_m: float,
    yaw_deg: float,
) -> list[float]:
    """Keep the rotated asset footprint, not merely its origin, in the room."""
    size = _bounds_size(entity)
    angle = math.radians(float(yaw_deg))
    half_extents = [
        0.5 * (abs(math.cos(angle)) * size[0] + abs(math.sin(angle)) * size[1]),
        0.5 * (abs(math.sin(angle)) * size[0] + abs(math.cos(angle)) * size[1]),
    ]
    result = []
    for axis in range(2):
        lower = float(room_bounds["min"][axis]) + room_margin_m + half_extents[axis]
        upper = float(room_bounds["max"][axis]) - room_margin_m - half_extents[axis]
        # Oversized assets cannot satisfy both limits; retain the room center so
        # the placement solver emits an honest geometry failure instead of an
        # arbitrary boundary-biased pose.
        result.append((lower + upper) * 0.5 if lower > upper else max(lower, min(float(xy[axis]), upper)))
    return result


def _placement_reference_local_xy(entity: dict[str, Any]) -> list[float]:
    """Return the measured local point used by ground-placement XY solving."""
    geometry = entity.get("asset_local_geometry") if isinstance(entity.get("asset_local_geometry"), dict) else {}
    value = geometry.get("bottom_contact_center_m") or entity.get("placement_reference_local_m")
    if isinstance(value, list) and len(value) >= 2:
        return [float(value[0]), float(value[1])]
    return [0.0, 0.0]


def _matrix_translation(matrix: Any) -> list[float] | None:
    if not isinstance(matrix, list) or len(matrix) != 4:
        return None
    try:
        return [float(value) for value in matrix[3][:3]]
    except (TypeError, ValueError, IndexError):
        return None


def _matrix_yaw_deg(matrix: Any) -> float:
    if not isinstance(matrix, list) or len(matrix) != 4:
        return 0.0
    try:
        return math.degrees(math.atan2(float(matrix[0][1]), float(matrix[0][0])))
    except (TypeError, ValueError, IndexError):
        return 0.0


def _local_bounds(entity: dict[str, Any]) -> dict[str, list[float]] | None:
    bounds = (entity.get("regression_alignment") or {}).get("wrapper_local_bounds_m") or {}
    lower, upper = bounds.get("min"), bounds.get("max")
    if not (isinstance(lower, list) and isinstance(upper, list) and len(lower) >= 3 and len(upper) >= 3):
        return None
    minimum = [float(value) for value in lower[:3]]
    maximum = [float(value) for value in upper[:3]]
    return {
        "min": minimum,
        "max": maximum,
        "size": [maximum[index] - minimum[index] for index in range(3)],
    }


def _world_bounds(entity: dict[str, Any]) -> dict[str, list[float]] | None:
    bounds = _local_bounds(entity)
    matrix = entity.get("xform_matrix")
    if not bounds or not isinstance(matrix, list) or len(matrix) != 4:
        return None
    points: list[list[float]] = []
    for x in (bounds["min"][0], bounds["max"][0]):
        for y in (bounds["min"][1], bounds["max"][1]):
            for z in (bounds["min"][2], bounds["max"][2]):
                source = [x, y, z, 1.0]
                points.append([
                    sum(source[row] * float(matrix[row][column]) for row in range(4))
                    for column in range(3)
                ])
    minimum = [min(point[index] for point in points) for index in range(3)]
    maximum = [max(point[index] for point in points) for index in range(3)]
    return {
        "min": minimum,
        "max": maximum,
        "size": [maximum[index] - minimum[index] for index in range(3)],
    }


def _zone_axis(
    value: float,
    source_minimum: float,
    source_maximum: float,
    target_minimum: float,
    target_maximum: float,
    count: int,
) -> tuple[int, float]:
    span = max(source_maximum - source_minimum, 1e-9)
    normalized = max(0.0, min(1.0 - 1e-12, (value - source_minimum) / span))
    index = min(count - 1, int(normalized * count))
    target_span = target_maximum - target_minimum
    center = target_minimum + target_span * (index + 0.5) / count
    return index, center


def _snap_yaw(value: float, step_deg: float) -> float:
    snapped = round(value / step_deg) * step_deg
    return ((snapped + 180.0) % 360.0) - 180.0


def _strip_privileged_entity(entity: dict[str, Any]) -> dict[str, Any]:
    return {
        key: deepcopy(value)
        for key, value in entity.items()
        if key not in PRIVILEGED_LAYOUT_FIELDS
    }


def _canonical_wrapper_target(target: Any, entities: list[dict[str, Any]]) -> str:
    """Map a source attachment namespace to the shared wrapper namespace."""
    value = str(target or "")
    owner_id = _target_owner(value, entities)
    if not owner_id:
        return value
    owner = next(item for item in entities if str(item.get("id") or "") == owner_id)
    attachment = str(
        ((owner.get("regression_instance_opinions") or {}).get("source_attachment_path")) or ""
    )
    if not attachment:
        return value
    geometry_root = entity_prim(owner) + str(owner.get("geometry_prim_suffix") or "")
    prefix = geometry_root + attachment
    if value == prefix:
        return geometry_root
    if value.startswith(prefix + "/"):
        return geometry_root + value[len(prefix):]
    return value


def derive_constraint_layout_spec(
    exact_spec: dict[str, Any],
    *,
    zone_columns: int = 5,
    zone_rows: int = 5,
    yaw_step_deg: float = 90.0,
    room_margin_m: float = 0.25,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Compile an exact regression fixture into a coarse, non-exact layout input.

    Exact matrices are used only to assign a quantized room zone and yaw bucket.
    The returned spec contains no authored pose matrices, source xform evidence, or
    instance opinions. Unsupported entities are retained in a pending inventory,
    not composed at an arbitrary origin.
    """
    if zone_columns < 2 or zone_rows < 2:
        raise ValueError("zone grid must be at least 2x2")
    if yaw_step_deg <= 0.0 or 360.0 % yaw_step_deg > 1e-9:
        raise ValueError("yaw_step_deg must divide 360")
    entities = [
        item for item in exact_spec.get("entities", [])
        if isinstance(item, dict) and item.get("id")
    ]
    room_entity = next(
        (item for item in entities if item.get("asset_scope") == "scene_root"),
        None,
    )
    if room_entity is None:
        raise ValueError("constraint layout requires one scene_root entity")
    source_room_bounds = _world_bounds(room_entity)
    target_room_bounds = _local_bounds(room_entity)
    if not source_room_bounds or not target_room_bounds:
        raise ValueError("scene_root wrapper bounds and exact matrix are required for coarse zone derivation")

    owner_ids = {
        str(item["id"]): item
        for item in entities
    }
    supported_owner_ids: set[str] = set()
    for item in entities:
        target = item.get("support_target") or item.get("mount_target")
        if target:
            owner = _target_owner(str(target), entities)
            if owner:
                supported_owner_ids.add(owner)

    active: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    zone_histogram: dict[str, int] = {}
    for source in entities:
        entity = _strip_privileged_entity(source)
        for target_key in ("support_target", "mount_target", "mount_anchor_prim", "facing_target"):
            if entity.get(target_key):
                entity[target_key] = _canonical_wrapper_target(entity[target_key], entities)
        entity["asset_bounds_m"] = _local_bounds(source) or {
            "min": [0.0, 0.0, 0.0], "max": [0.0, 0.0, 0.0], "size": [0.0, 0.0, 0.0],
        }
        entity.pop("support_plane_z_m", None)
        entity.pop("placement_solver", None)
        entity_id = str(entity["id"])
        if source is room_entity:
            entity["translate"] = [0.0, 0.0, 0.0]
            entity["rotate"] = [0.0, 0.0, 0.0]
            entity["layout_role"] = "architectural_shell"
            entity["layout_constraints"] = {"locked": True, "reason": "architectural_shell_origin"}
            active.append(entity)
            continue

        translation = _matrix_translation(source.get("xform_matrix"))
        target = entity.get("mount_target") or entity.get("support_target")
        target_owner_id = _target_owner(str(target or ""), entities)
        target_owner = owner_ids.get(str(target_owner_id or "")) or {}
        target_lower = str(target or "").lower()
        floor_like_target = (
            str(target) in FLOOR_TARGETS
            or "/floor" in target_lower
            or "floor_" in target_lower
            or "floor1" in target_lower
        )
        unsupported_architectural_mount = (
            bool(entity.get("support_target"))
            and target_owner.get("asset_scope") == "scene_root"
            and not floor_like_target
        )
        if translation is None or not target or unsupported_architectural_mount:
            pending.append({
                "id": entity_id,
                "semantic_name": entity.get("semantic_name"),
                "model_id": entity.get("model_id"),
                "logical_id": entity.get("logical_id"),
                "path": entity.get("path"),
                "prim": entity.get("prim"),
                "reason": (
                    "architectural_mount_relation_required" if unsupported_architectural_mount
                    else "missing_verified_support_or_mount_relation" if not target
                    else "exact_pose_unavailable_for_zone_derivation"
                ),
            })
            continue
        zone_x, preferred_x = _zone_axis(
            translation[0], source_room_bounds["min"][0], source_room_bounds["max"][0],
            target_room_bounds["min"][0], target_room_bounds["max"][0], zone_columns,
        )
        zone_y, preferred_y = _zone_axis(
            translation[1], source_room_bounds["min"][1], source_room_bounds["max"][1],
            target_room_bounds["min"][1], target_room_bounds["max"][1], zone_rows,
        )
        zone_id = f"z{zone_x}_{zone_y}"
        zone_histogram[zone_id] = zone_histogram.get(zone_id, 0) + 1
        snapped_yaw = _snap_yaw(_matrix_yaw_deg(source.get("xform_matrix")), yaw_step_deg)
        entity["translate"] = [preferred_x, preferred_y, 0.0]
        entity["rotate"] = [0.0, 0.0, snapped_yaw]
        if source.get("mount_target"):
            layout_role = "mounted_fixture"
            solver = "fixed_mount"
        elif entity_id in supported_owner_ids:
            layout_role = "support_furniture"
            solver = "mesh_support"
        elif floor_like_target:
            layout_role = "ground_object"
            solver = "mesh_support"
        else:
            layout_role = "supported_prop"
            solver = "mesh_support"
        entity["layout_role"] = layout_role
        entity["layout_constraints"] = {
            "solver": solver,
            "zone": {"id": zone_id, "column": zone_x, "row": zone_y, "grid": [zone_columns, zone_rows]},
            "preferred_xy": [preferred_x, preferred_y],
            "yaw_bucket_deg": snapped_yaw,
            "yaw_deltas_deg": [0.0, 90.0, -90.0, 180.0],
            "obstacle_clearance_m": 0.10 if layout_role in {"ground_object", "support_furniture"} else 0.01,
            "support_margin_m": 0.05 if layout_role in {"ground_object", "support_furniture"} else 0.01,
            **({"mount_surface": "ceiling"} if source.get("mount_target") else {}),
        }
        active.append(entity)

    active_ids = {str(item["id"]) for item in active}
    active_prims = [str(item.get("prim") or "") for item in active]
    relations = []
    for relation in exact_spec.get("relations", []):
        if not isinstance(relation, dict) or str(relation.get("subject") or "") not in active_ids:
            continue
        target = _canonical_wrapper_target(relation.get("target"), entities)
        if target != "floor" and target.startswith("/") and not any(
            target == prim or target.startswith(prim + "/") for prim in active_prims if prim
        ):
            continue
        row = {key: deepcopy(value) for key, value in relation.items() if key != "resolved_transform"}
        row["target"] = target
        if isinstance(row.get("evidence"), dict):
            row["evidence"] = {
                key: deepcopy(value)
                for key, value in row["evidence"].items()
                if key not in {"signed_gap_m", "mesh_support_coverage"}
            }
        relations.append(row)

    target_min, target_max = target_room_bounds["min"], target_room_bounds["max"]
    floor_size = [
        max(0.1, target_max[0] - target_min[0] - room_margin_m * 2.0),
        max(0.1, target_max[1] - target_min[1] - room_margin_m * 2.0),
        0.05,
    ]
    support_colliders = deepcopy(exact_spec.get("support_colliders") or [])
    if any(str(item.get("support_target")) in FLOOR_TARGETS for item in active):
        support_colliders = [
            item for item in support_colliders
            if str(item.get("id") or "").lower() not in {"floor", "ground", "room_floor"}
        ]
        support_colliders.append({
            "id": "room_floor",
            "type": "box",
            "prim": "/World/Colliders/ConstraintFloor",
            "translate": [
                (target_min[0] + target_max[0]) * 0.5,
                (target_min[1] + target_max[1]) * 0.5,
                target_min[2] - 0.025,
            ],
            "size": floor_size,
            "visible": False,
            "physics": "static",
        })

    spec = deepcopy(exact_spec)
    spec["entities"] = active
    spec["relations"] = relations
    stripped_scene_feature_count = len(spec.get("references") or [])
    spec["references"] = []
    spec["support_colliders"] = support_colliders
    spec["layout_pending_entities"] = pending
    spec.pop("reconstruction_provenance", None)
    spec.pop("reconstruction_report", None)
    room = deepcopy(spec.get("room") or {})
    room["quality_target"] = "constraint_layout_regression"
    room["usable_bounds_m"] = target_room_bounds
    spec["room"] = room
    spec["constraints"] = {
        "layout_mode": "constraint_only",
        "exact_pose_allowed": False,
        "instance_opinions_allowed": False,
        "zone_grid": [zone_columns, zone_rows],
        "yaw_step_deg": float(yaw_step_deg),
        "minimum_corridor_m": 0.7,
        "architectural_context_policy": "self_contained_scene_root_wrapper_only",
    }
    spec["validation_targets"] = [
        "assets_resolve", "support_contact", "no_overlap", "facing",
        "layout_coverage", "camera_composition", "render_feedback",
    ]
    spec["constraint_layout_provenance"] = {
        "schema_version": "isaac-constraint-layout-provenance/v1",
        "mode": "coarse_zone_without_exact_pose",
        "source_scene_slug": str((exact_spec.get("reconstruction_provenance") or {}).get("scene_slug") or exact_spec.get("domain") or ""),
        "zone_grid": [zone_columns, zone_rows],
        "yaw_step_deg": float(yaw_step_deg),
        "forbidden_fields": sorted(PRIVILEGED_LAYOUT_FIELDS),
        "note": "Source exact poses were used only to assign coarse zone/yaw buckets and are absent from this output.",
    }
    report = {
        "schema_version": "isaac-constraint-layout-compile-report/v1",
        "status": "pass" if active else "fail",
        "source_entity_count": len(entities),
        "active_entity_count": len(active),
        "pending_entity_count": len(pending),
        "relation_count": len(relations),
        "zone_histogram": dict(sorted(zone_histogram.items())),
        "room_bounds_m": target_room_bounds,
        "floor_collider_added": any(item.get("id") == "room_floor" for item in support_colliders),
        "stripped_source_scene_feature_count": stripped_scene_feature_count,
        "pending_entities": pending,
        "forbidden_fields_present": sorted(
            field for field in PRIVILEGED_LAYOUT_FIELDS
            if any(field in item for item in active)
        ),
    }
    if report["forbidden_fields_present"]:
        report["status"] = "fail"
    return spec, report


def _validated_semantic_zones(
    profile: dict[str, Any],
    brief: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]], str]:
    """Merge domain defaults with explicit Scene Brief layout guidance."""
    zones: dict[str, dict[str, Any]] = {}
    for row in [*(profile.get("default_zones") or []), *(brief.get("zones") or [])]:
        if not isinstance(row, dict) or not row.get("id"):
            continue
        bounds = row.get("bounds_norm")
        if not (
            isinstance(bounds, list) and len(bounds) == 4
            and all(isinstance(value, (int, float)) for value in bounds)
            and 0.0 <= float(bounds[0]) < float(bounds[2]) <= 1.0
            and 0.0 <= float(bounds[1]) < float(bounds[3]) <= 1.0
        ):
            raise ValueError(f"semantic zone {row.get('id')} requires bounds_norm=[xmin,ymin,xmax,ymax]")
        zones[str(row["id"])] = deepcopy(row)
    rules = brief.get("layout_rules") or profile.get("rules") or []
    if not isinstance(rules, list):
        raise ValueError("layout_rules must be a list")
    default_zone = str(brief.get("default_zone") or profile.get("default_zone") or "")
    if not zones:
        raise ValueError("semantic layout requires at least one functional zone")
    if not default_zone:
        default_zone = next(iter(zones))
    if default_zone not in zones:
        raise ValueError(f"unknown semantic default zone: {default_zone}")
    for rule in rules:
        if isinstance(rule, dict) and str(rule.get("zone") or default_zone) not in zones:
            raise ValueError(f"layout rule {rule.get('id')} references unknown zone {rule.get('zone')}")
        if isinstance(rule, dict) and rule.get("minimum_mesh_coverage") is not None:
            coverage = float(rule["minimum_mesh_coverage"])
            if not 0.0 < coverage <= 1.0:
                raise ValueError(
                    f"layout rule {rule.get('id')} minimum_mesh_coverage must be in (0, 1]"
                )
    ordered_rules = [deepcopy(row) for row in rules if isinstance(row, dict)]
    ordered_rules.sort(key=lambda row: -float(row.get("priority") or 0.0))
    return zones, ordered_rules, default_zone


def refresh_procedural_room_shell(
    inventory_spec: dict[str, Any],
    scene_brief: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Resize a declared procedural shell before semantic layout compilation.

    Stage templates may carry colliders expanded for a different room size.  The
    Scene Brief is authoritative for a new text/image generation request, so a
    declared object-free preset must be expanded again instead of reusing stale
    floor and wall meshes.
    """
    updated = deepcopy(inventory_spec)
    brief_room = deepcopy(scene_brief.get("room") or {})
    inventory_room = deepcopy(updated.get("room") or {})
    preset_id = str(brief_room.get("shell_preset") or inventory_room.get("shell_preset") or "")
    if not preset_id:
        return updated, {"status": "not_applicable", "reason": "no_procedural_shell_preset"}
    if not brief_room.get("size_m"):
        brief_room["size_m"] = deepcopy(inventory_room.get("size_m"))
    if not brief_room.get("size_m"):
        raise ValueError("procedural room-shell refresh requires room.size_m")
    brief_room.setdefault("type", inventory_room.get("type") or "room")
    brief_room.setdefault("center_m", inventory_room.get("center_m") or [0.0, 0.0, 0.0])
    brief_room["shell_preset"] = preset_id
    expanded = resolve_declared_preset({"room": brief_room})
    expanded_room = expanded["room"]
    new_shell = build_room_shell(expanded_room)
    shell_ids = {
        "floor", "back_wall", "front_wall", "left_wall", "right_wall", "ceiling",
    }
    retained = []
    for row in updated.get("support_colliders") or []:
        if not isinstance(row, dict):
            continue
        row_id = str(row.get("id") or "")
        prim = str(row.get("prim") or "")
        evidence = str(row.get("evidence") or "")
        if row_id in shell_ids or prim.startswith("/World/Architecture/") or evidence.startswith("room-shell preset "):
            continue
        retained.append(deepcopy(row))
    merged_room = deepcopy(inventory_room)
    merged_room.update(deepcopy(expanded_room))
    size = [float(value) for value in expanded_room["size_m"]]
    center = [float(value) for value in expanded_room.get("center_m") or [0.0, 0.0, 0.0]]
    merged_room["usable_bounds_m"] = {
        "min": [center[0] - size[0] / 2.0, center[1] - size[1] / 2.0, center[2]],
        "max": [center[0] + size[0] / 2.0, center[1] + size[1] / 2.0, center[2] + size[2]],
        "size": size,
    }
    updated["room"] = merged_room
    updated["support_colliders"] = new_shell + retained
    return updated, {
        "status": "pass",
        "preset": preset_id,
        "room_size_m": size,
        "shell_collider_count": len(new_shell),
        "retained_custom_collider_count": len(retained),
        "floor_size_m": next((row.get("size") for row in new_shell if row.get("id") == "floor"), None),
    }


def _validated_functional_groups(
    profile: dict[str, Any],
    brief: dict[str, Any],
) -> list[dict[str, Any]]:
    """Return validated anchor-relative functional-group templates."""
    groups = brief.get("functional_groups") or profile.get("functional_groups") or []
    if not isinstance(groups, list):
        raise ValueError("functional_groups must be a list")
    validated: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in groups:
        if not isinstance(row, dict) or not row.get("id"):
            raise ValueError("each functional group requires an id")
        group_id = str(row["id"])
        if group_id in seen:
            raise ValueError(f"duplicate functional group id: {group_id}")
        seen.add(group_id)
        if not isinstance(row.get("anchor_match"), dict):
            raise ValueError(f"functional group {group_id} requires anchor_match")
        if row.get("atomic") not in (None, True, False):
            raise ValueError(f"functional group {group_id} atomic must be boolean")
        members = row.get("members")
        if not isinstance(members, list) or not members:
            raise ValueError(f"functional group {group_id} requires members")
        for member in members:
            if not isinstance(member, dict) or not isinstance(member.get("match"), dict):
                raise ValueError(f"functional group {group_id} member requires match")
            per_anchor = int(member.get("per_anchor") or 1)
            offsets = member.get("offsets_m")
            if not (
                isinstance(offsets, list) and len(offsets) >= per_anchor
                and all(isinstance(offset, list) and len(offset) == 2 for offset in offsets)
            ):
                raise ValueError(
                    f"functional group {group_id} member requires at least per_anchor two-dimensional offsets_m"
                )
            yaw_offsets = member.get("yaw_offsets_deg") or [0.0]
            if not isinstance(yaw_offsets, list) or not yaw_offsets:
                raise ValueError(f"functional group {group_id} member requires yaw_offsets_deg")
            position_tolerance = member.get("position_tolerance_m")
            if position_tolerance is not None and float(position_tolerance) < 0.0:
                raise ValueError(f"functional group {group_id} position_tolerance_m must be non-negative")
            offset_reference = str(member.get("offset_reference") or "placement_reference")
            if offset_reference not in {"placement_reference", "asset_origin"}:
                raise ValueError(
                    f"functional group {group_id} offset_reference must be placement_reference or asset_origin"
                )
            connection = member.get("connection")
            if connection is not None:
                if not isinstance(connection, dict):
                    raise ValueError(f"functional group {group_id} connection must be an object")
                for key in ("anchor_port_local_m", "member_port_local_m"):
                    value = connection.get(key)
                    if not (
                        isinstance(value, list) and len(value) == 3
                        and all(isinstance(item, (int, float)) for item in value)
                    ):
                        raise ValueError(f"functional group {group_id} connection requires {key}=[x,y,z]")
                semantic_ports = [connection.get("anchor_port"), connection.get("member_port")]
                if connection.get("require_semantic_evidence"):
                    if not all(isinstance(port, dict) and port.get("evidence_status") == "verified" for port in semantic_ports):
                        raise ValueError(
                            f"functional group {group_id} connection requires two verified semantic ports"
                        )
                    if not all(str(port.get("evidence_source") or "").strip() for port in semantic_ports):
                        raise ValueError(
                            f"functional group {group_id} verified semantic ports require evidence_source"
                        )
                if connection.get("require_opposed_normals") and not all(
                    isinstance(port, dict) and port.get("normal_axis") in {"+X", "-X", "+Y", "-Y"}
                    for port in semantic_ports
                ):
                    raise ValueError(
                        f"functional group {group_id} opposed-normal validation requires two port normal axes"
                    )
            envelope = member.get("work_envelope")
            if envelope is not None:
                if not isinstance(envelope, dict) or float(envelope.get("radius_m") or 0.0) <= 0.0:
                    raise ValueError(f"functional group {group_id} work_envelope requires radius_m > 0")
        validated.append(deepcopy(row))
    return validated


def _semantic_rule_matches(
    entity: dict[str, Any],
    layout_role: str,
    rule: dict[str, Any],
) -> bool:
    match = rule.get("match") if isinstance(rule.get("match"), dict) else {}
    entity_ids = {str(value) for value in match.get("entity_ids") or []}
    if entity_ids and str(entity.get("id") or "") not in entity_ids:
        return False
    roles = {str(value) for value in match.get("layout_roles") or []}
    if roles and layout_role not in roles:
        return False
    text = " ".join(
        str(entity.get(key) or "").lower()
        for key in ("semantic_name", "semantic_class", "canonical_class", "role")
    )
    terms = [str(value).lower() for value in match.get("semantic_terms") or []]
    if terms and not any(term in text for term in terms):
        return False
    excluded = [str(value).lower() for value in match.get("exclude_terms") or []]
    return not any(term in text for term in excluded)


def _semantic_zone_slot(
    bounds_norm: list[float],
    index: int,
    count: int,
    distribution: str,
) -> tuple[float, float]:
    """Return one deterministic normalized slot inside a functional zone."""
    xmin, ymin, xmax, ymax = [float(value) for value in bounds_norm]
    count = max(1, count)
    if distribution == "center":
        if count == 1:
            return (xmin + xmax) * 0.5, (ymin + ymax) * 0.5
        inset_x = (xmax - xmin) * 0.28
        inset_y = (ymax - ymin) * 0.28
        xmin, xmax = xmin + inset_x, xmax - inset_x
        ymin, ymax = ymin + inset_y, ymax - inset_y
        distribution = "grid"
    if distribution == "perimeter":
        corners = [(xmin, ymin), (xmax, ymin), (xmax, ymax), (xmin, ymax)]
        if count <= 4:
            return corners[index % 4]
        t = (index % count) / count * 4.0
        side, offset = int(t) % 4, t % 1.0
        if side == 0:
            return xmin + (xmax - xmin) * offset, ymin
        if side == 1:
            return xmax, ymin + (ymax - ymin) * offset
        if side == 2:
            return xmax - (xmax - xmin) * offset, ymax
        return xmin, ymax - (ymax - ymin) * offset
    columns = max(1, math.ceil(math.sqrt(count)))
    rows = max(1, math.ceil(count / columns))
    column, row = index % columns, index // columns
    return (
        xmin + (xmax - xmin) * (column + 0.5) / columns,
        ymin + (ymax - ymin) * (row + 0.5) / rows,
    )


def _rotate_xy(offset: list[float], yaw_deg: float) -> list[float]:
    angle = math.radians(float(yaw_deg))
    cosine, sine = math.cos(angle), math.sin(angle)
    return [
        float(offset[0]) * cosine - float(offset[1]) * sine,
        float(offset[0]) * sine + float(offset[1]) * cosine,
    ]


def _axis_world_angle_deg(axis: str, yaw_deg: float) -> float | None:
    local_angle = {"+X": 0.0, "+Y": 90.0, "-X": 180.0, "-Y": -90.0}.get(str(axis).upper())
    return None if local_angle is None else float(yaw_deg) + local_angle


def _port_roles_compatible(anchor_role: str, member_role: str) -> bool:
    roles = {str(anchor_role), str(member_role)}
    if "bidirectional" in roles:
        return not ("operator" in roles)
    return roles == {"input", "output"} or roles in ({"service"}, {"operator"})


def _wrapped_yaw_delta(target_deg: float, current_deg: float) -> float:
    return (float(target_deg) - float(current_deg) + 180.0) % 360.0 - 180.0


def _wall_flush_pose(
    entity: dict[str, Any],
    rule: dict[str, Any],
    room_bounds: dict[str, Any],
    fallback_xy: list[float],
) -> tuple[list[float], float, str]:
    """Place directional storage flush to a wall with its front facing inward."""
    wall = str(rule.get("wall_affinity") or "nearest").lower()
    if wall == "nearest":
        distances = {
            "left": abs(float(fallback_xy[0]) - float(room_bounds["min"][0])),
            "right": abs(float(room_bounds["max"][0]) - float(fallback_xy[0])),
            "front": abs(float(fallback_xy[1]) - float(room_bounds["min"][1])),
            "back": abs(float(room_bounds["max"][1]) - float(fallback_xy[1])),
        }
        wall = min(distances, key=distances.get)
    if wall not in {"left", "right", "front", "back"}:
        raise ValueError(f"unsupported wall_affinity for wall_flush: {wall}")

    front_axis = str(entity.get("front_axis") or "").upper()
    local_heading = {"+X": 0.0, "+Y": 90.0, "-X": 180.0, "-Y": -90.0}.get(front_axis)
    if local_heading is None:
        raise ValueError(f"wall_flush requires a valid front_axis: {front_axis}")
    inward_heading = {"left": 0.0, "right": 180.0, "front": 90.0, "back": -90.0}[wall]
    yaw = _wrapped_yaw_delta(inward_heading, local_heading)

    size = (entity.get("asset_bounds_m") or {}).get("size") or (entity.get("layout_hint") or {}).get("size_m") or [0.0, 0.0]
    sx, sy = float(size[0]), float(size[1])
    angle = math.radians(yaw)
    half_x = 0.5 * (abs(math.cos(angle)) * sx + abs(math.sin(angle)) * sy)
    half_y = 0.5 * (abs(math.sin(angle)) * sx + abs(math.cos(angle)) * sy)
    clearance = max(0.0, float(rule.get("wall_clearance_m") or 0.05))
    xy = list(fallback_xy)
    if wall == "left":
        xy[0] = float(room_bounds["min"][0]) + half_x + clearance
    elif wall == "right":
        xy[0] = float(room_bounds["max"][0]) - half_x - clearance
    elif wall == "front":
        xy[1] = float(room_bounds["min"][1]) + half_y + clearance
    else:
        xy[1] = float(room_bounds["max"][1]) - half_y - clearance
    return xy, yaw, wall


def _apply_functional_groups(
    entities: list[dict[str, Any]],
    groups: list[dict[str, Any]],
    room_bounds: dict[str, Any],
    room_margin_m: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Bind matching members to anchors and author relative layout constraints."""
    candidates = [
        entity for entity in entities
        if entity.get("layout_role") not in {None, "architectural_shell", "mounted_fixture"}
    ]
    claimed: set[str] = set()
    records: list[dict[str, Any]] = []
    relations: list[dict[str, Any]] = []
    for group in groups:
        group_id = str(group["id"])
        anchors = sorted(
            (
                entity for entity in candidates
                if _semantic_rule_matches(entity, str(entity.get("layout_role") or ""), {"match": group["anchor_match"]})
            ),
            key=lambda entity: str(entity["id"]),
        )
        for member_rule_index, member_rule in enumerate(group["members"]):
            per_anchor = max(1, int(member_rule.get("per_anchor") or 1))
            members = sorted(
                (
                    entity for entity in candidates
                    if str(entity["id"]) not in claimed
                    and entity not in anchors
                    and _semantic_rule_matches(
                        entity,
                        str(entity.get("layout_role") or ""),
                        {"match": member_rule["match"]},
                    )
                ),
                key=lambda entity: str(entity["id"]),
            )
            assigned = 0
            orientation_pending = 0
            for anchor_index, anchor in enumerate(anchors):
                anchor_constraints = anchor.get("layout_constraints") or {}
                anchor_xy = list(anchor_constraints.get("preferred_xy") or (anchor.get("translate") or [0.0, 0.0])[:2])
                anchor_yaw = float((anchor.get("rotate") or [0.0, 0.0, 0.0])[2])
                for slot in range(per_anchor):
                    member_index = anchor_index * per_anchor + slot
                    if member_index >= len(members):
                        break
                    member = members[member_index]
                    semantic_offset_local = [float(value) for value in member_rule["offsets_m"][slot][:2]]
                    offset_reference = str(member_rule.get("offset_reference") or "placement_reference")
                    anchor_reference = _placement_reference_local_xy(anchor)
                    member_reference = _rotate_xy(
                        _placement_reference_local_xy(member),
                        float((member_rule.get("yaw_offsets_deg") or [0.0])[slot % len(member_rule.get("yaw_offsets_deg") or [0.0])]),
                    )
                    offset_local = list(semantic_offset_local)
                    if offset_reference == "asset_origin":
                        offset_local = [
                            semantic_offset_local[axis] + member_reference[axis] - anchor_reference[axis]
                            for axis in range(2)
                        ]
                    offset_world = _rotate_xy(offset_local, anchor_yaw)
                    yaw_offsets = member_rule.get("yaw_offsets_deg") or [0.0]
                    yaw_offset = float(yaw_offsets[slot % len(yaw_offsets)])
                    yaw = anchor_yaw + yaw_offset
                    preferred_xy = _clamp_footprint_center_to_room(
                        member,
                        [anchor_xy[axis] + offset_world[axis] for axis in range(2)],
                        room_bounds,
                        room_margin_m,
                        yaw,
                    )
                    constraints = member.setdefault("layout_constraints", {})
                    constraints["preferred_xy"] = preferred_xy
                    constraints["distribution"] = "functional_group"
                    constraints["yaw_bucket_deg"] = yaw
                    constraints["yaw_deltas_deg"] = deepcopy(member_rule.get("yaw_deltas_deg") or [0.0])
                    constraints["obstacle_clearance_m"] = float(
                        member_rule.get("obstacle_clearance_m")
                        or constraints.get("obstacle_clearance_m")
                        or 0.10
                    )
                    constraints["functional_group"] = {
                        "id": group_id,
                        "anchor_id": str(anchor["id"]),
                        "atomic": bool(group.get("atomic", False)),
                        "member_rule_index": member_rule_index,
                        "member_slot": slot,
                        "offset_local_m": offset_local,
                        "semantic_offset_local_m": semantic_offset_local,
                        "offset_reference": offset_reference,
                        "anchor_placement_reference_local_m": anchor_reference,
                        "member_placement_reference_local_m": member_reference,
                        "yaw_offset_deg": yaw_offset,
                        "rotate_with_anchor": True,
                        "facing_policy": str(member_rule.get("facing_policy") or "authored_yaw"),
                        "position_tolerance_m": float(member_rule.get("position_tolerance_m") or 0.45),
                    }
                    if isinstance(member_rule.get("connection"), dict):
                        constraints["functional_group"]["connection"] = deepcopy(member_rule["connection"])
                        anchor_port = member_rule["connection"]["anchor_port_local_m"]
                        member_port = member_rule["connection"]["member_port_local_m"]
                        rotated_member_port = _rotate_xy(member_port[:2], yaw_offset)
                        offset_local = [
                            float(anchor_port[0]) - rotated_member_port[0] - anchor_reference[0] + member_reference[0],
                            float(anchor_port[1]) - rotated_member_port[1] - anchor_reference[1] + member_reference[1],
                        ]
                        constraints["functional_group"]["offset_local_m"] = offset_local
                        offset_world = _rotate_xy(offset_local, anchor_yaw)
                        preferred_xy = [anchor_xy[axis] + offset_world[axis] for axis in range(2)]
                        constraints["preferred_xy"] = preferred_xy
                        member["translate"] = [preferred_xy[0], preferred_xy[1], 0.0]
                    if isinstance(member_rule.get("work_envelope"), dict):
                        constraints["functional_group"]["work_envelope"] = deepcopy(member_rule["work_envelope"])
                    member["translate"] = [preferred_xy[0], preferred_xy[1], 0.0]
                    member["rotate"] = [0.0, 0.0, yaw]
                    if str(member_rule.get("facing_policy") or "") == "toward_anchor":
                        constraints["functional_group"]["facing_target"] = str(anchor["id"])
                        if member.get("front_axis"):
                            member["facing_target"] = str(anchor["id"])
                            member["facing_target_mode"] = "nearest"
                            constraints["functional_group"]["orientation_status"] = "verified_front_axis"
                        else:
                            orientation_pending += 1
                            constraints["functional_group"]["orientation_status"] = "pending_front_axis"
                    claimed.add(str(member["id"]))
                    assigned += 1
                    relations.append({
                        "type": "functional_group_member",
                        "subject": str(member["id"]),
                        "target": str(anchor["id"]),
                        "group_id": group_id,
                        "member_slot": slot,
                    })
            required = len(anchors) * per_anchor
            records.append({
                "group_id": group_id,
                "member_rule_index": member_rule_index,
                "anchor_count": len(anchors),
                "required_member_count": required,
                "candidate_member_count": len(members),
                "assigned_member_count": assigned,
                "missing_member_count": max(0, required - assigned),
                "orientation_pending_count": orientation_pending,
                "status": "pass" if assigned == required else "incomplete",
            })
    _fit_functional_group_envelopes(candidates, room_bounds, room_margin_m)
    return records, relations


def _fit_functional_group_envelopes(
    entities: list[dict[str, Any]],
    room_bounds: dict[str, Any],
    room_margin_m: float,
) -> None:
    """Shift each anchor once so its complete rigid member envelope fits."""
    by_id = {str(entity["id"]): entity for entity in entities if entity.get("id")}
    members_by_anchor: dict[str, list[dict[str, Any]]] = {}
    for member in entities:
        group = ((member.get("layout_constraints") or {}).get("functional_group") or {})
        anchor_id = str(group.get("anchor_id") or "")
        if anchor_id in by_id:
            members_by_anchor.setdefault(anchor_id, []).append(member)
    for anchor_id, members in members_by_anchor.items():
        anchor = by_id[anchor_id]
        anchor_yaw = float((anchor.get("rotate") or [0.0, 0.0, 0.0])[2])
        anchor_xy = list(
            (anchor.get("layout_constraints") or {}).get("preferred_xy")
            or (anchor.get("translate") or [0.0, 0.0])[:2]
        )
        envelope: list[tuple[list[float], list[float]]] = []
        for entity, relative_xy, yaw in [
            (anchor, [0.0, 0.0], anchor_yaw),
            *[
                (
                    member,
                    _rotate_xy(
                        ((member.get("layout_constraints") or {}).get("functional_group") or {}).get("offset_local_m") or [0.0, 0.0],
                        anchor_yaw,
                    ),
                    anchor_yaw + float(
                        (((member.get("layout_constraints") or {}).get("functional_group") or {}).get("yaw_offset_deg") or 0.0)
                    ),
                )
                for member in members
            ],
        ]:
            size = _bounds_size(entity)
            angle = math.radians(yaw)
            half = [
                0.5 * (abs(math.cos(angle)) * size[0] + abs(math.sin(angle)) * size[1]),
                0.5 * (abs(math.sin(angle)) * size[0] + abs(math.cos(angle)) * size[1]),
            ]
            envelope.append((relative_xy, half))
        fitted = []
        for axis in range(2):
            relative_min = min(center[axis] - half[axis] for center, half in envelope)
            relative_max = max(center[axis] + half[axis] for center, half in envelope)
            lower = float(room_bounds["min"][axis]) + room_margin_m - relative_min
            upper = float(room_bounds["max"][axis]) - room_margin_m - relative_max
            fitted.append((lower + upper) * 0.5 if lower > upper else max(lower, min(float(anchor_xy[axis]), upper)))
        anchor["translate"] = [fitted[0], fitted[1], float((anchor.get("translate") or [0.0, 0.0, 0.0])[2])]
        (anchor.get("layout_constraints") or {})["preferred_xy"] = list(fitted)
        for member in members:
            group = ((member.get("layout_constraints") or {}).get("functional_group") or {})
            relative = _rotate_xy(group.get("offset_local_m") or [0.0, 0.0], anchor_yaw)
            preferred = [fitted[axis] + relative[axis] for axis in range(2)]
            member["translate"] = [preferred[0], preferred[1], float((member.get("translate") or [0.0, 0.0, 0.0])[2])]
            (member.get("layout_constraints") or {})["preferred_xy"] = preferred


def _assigned_functional_group_member_ids(
    assignments: list[dict[str, Any]],
    groups: list[dict[str, Any]],
) -> set[str]:
    """Predict the members whose absolute zone slots will be group-relative.

    Functional-group members receive their final preferred pose relative to an
    anchor in ``_apply_functional_groups``.  Counting those same members as
    independent zone occupants first can push the anchor away from the zone
    center, shifting the entire group or even placing its envelope outside the
    room.  This mirrors the deterministic member assignment order so only
    members that will actually be claimed stop consuming standalone slots.
    """
    candidates = [
        row["entity"]
        for row in assignments
        if row["layout_role"] not in {None, "architectural_shell", "mounted_fixture"}
    ]
    claimed: set[str] = set()
    for group in groups:
        anchors = sorted(
            (
                entity for entity in candidates
                if _semantic_rule_matches(
                    entity,
                    str(entity.get("layout_role") or ""),
                    {"match": group["anchor_match"]},
                )
            ),
            key=lambda entity: str(entity["id"]),
        )
        anchor_ids = {str(entity["id"]) for entity in anchors}
        for member_rule in group["members"]:
            per_anchor = max(1, int(member_rule.get("per_anchor") or 1))
            members = sorted(
                (
                    entity for entity in candidates
                    if str(entity["id"]) not in claimed
                    and str(entity["id"]) not in anchor_ids
                    and _semantic_rule_matches(
                        entity,
                        str(entity.get("layout_role") or ""),
                        {"match": member_rule["match"]},
                    )
                ),
                key=lambda entity: str(entity["id"]),
            )
            capacity = len(anchors) * per_anchor
            claimed.update(str(entity["id"]) for entity in members[:capacity])
    return claimed


def resolve_functional_group_preference(
    entity: dict[str, Any],
    entities_by_id: dict[str, dict[str, Any]],
    *,
    room_bounds: dict[str, Any] | None = None,
    room_margin_m: float = 0.25,
) -> dict[str, Any] | None:
    """Resolve a member target from the anchor's latest solved transform."""
    group = ((entity.get("layout_constraints") or {}).get("functional_group") or {})
    anchor = entities_by_id.get(str(group.get("anchor_id") or ""))
    offset = group.get("offset_local_m")
    if not anchor or not (isinstance(offset, list) and len(offset) >= 2):
        return None
    anchor_translate = anchor.get("translate") or [0.0, 0.0, 0.0]
    solved_anchor_reference = (anchor.get("placement_solver") or {}).get(
        "solved_footprint_center_xy"
    )
    if isinstance(solved_anchor_reference, list) and len(solved_anchor_reference) >= 2:
        anchor_xy = [float(solved_anchor_reference[0]), float(solved_anchor_reference[1])]
        anchor_xy_source = "placement_solver_visible_footprint_center"
    else:
        anchor_xy = [float(anchor_translate[0]), float(anchor_translate[1])]
        anchor_xy_source = "entity_translate_fallback"
    anchor_yaw = float((anchor.get("rotate") or [0.0, 0.0, 0.0])[2])
    rotated = _rotate_xy(offset, anchor_yaw) if group.get("rotate_with_anchor", True) else [float(offset[0]), float(offset[1])]
    desired_yaw = anchor_yaw + float(group.get("yaw_offset_deg") or 0.0)
    current_yaw = float((entity.get("rotate") or [0.0, 0.0, 0.0])[2])
    preferred_xy = [anchor_xy[0] + rotated[0], anchor_xy[1] + rotated[1]]
    if str(group.get("facing_policy") or "") == "toward_anchor":
        local_front_heading = {
            "+X": 0.0, "+Y": 90.0, "-X": 180.0, "-Y": -90.0,
        }.get(str(entity.get("front_axis") or "").upper())
        direction = [anchor_xy[0] - preferred_xy[0], anchor_xy[1] - preferred_xy[1]]
        if local_front_heading is not None and math.hypot(*direction) > 1e-9:
            world_heading = math.degrees(math.atan2(direction[1], direction[0]))
            desired_yaw = _wrapped_yaw_delta(world_heading, local_front_heading)
    if room_bounds and room_bounds.get("min") and room_bounds.get("max"):
        preferred_xy = _clamp_footprint_center_to_room(
            entity, preferred_xy, room_bounds, room_margin_m, desired_yaw,
        )
    return {
        "preferred_xy": preferred_xy,
        "anchor_xy_source": anchor_xy_source,
        "desired_yaw_deg": desired_yaw,
        "yaw_deltas_deg": [_wrapped_yaw_delta(desired_yaw, current_yaw)],
        "anchor_id": str(anchor["id"]),
        "position_tolerance_m": float(group.get("position_tolerance_m") or 0.45),
        "connection": deepcopy(group.get("connection")) if isinstance(group.get("connection"), dict) else None,
        "work_envelope": deepcopy(group.get("work_envelope")) if isinstance(group.get("work_envelope"), dict) else None,
        "atomic": bool(group.get("atomic", False)),
    }


def _world_group_point(entity: dict[str, Any], local_point: list[float]) -> list[float]:
    translate = [float(value) for value in (entity.get("translate") or [0.0, 0.0, 0.0])]
    yaw = float((entity.get("rotate") or [0.0, 0.0, 0.0])[2])
    rotated = _rotate_xy([float(local_point[0]), float(local_point[1])], yaw)
    return [translate[0] + rotated[0], translate[1] + rotated[1], translate[2] + float(local_point[2])]


def inspect_functional_group_layout(
    spec: dict[str, Any],
    *,
    position_tolerance_m: float = 0.45,
    yaw_tolerance_deg: float = 5.0,
) -> dict[str, Any]:
    """Check solved anchor-relative position/yaw without opening USD."""
    by_id = {
        str(entity["id"]): entity
        for entity in spec.get("entities") or []
        if isinstance(entity, dict) and entity.get("id")
    }
    rows: list[dict[str, Any]] = []
    for entity in by_id.values():
        group = ((entity.get("layout_constraints") or {}).get("functional_group") or {})
        if not group:
            continue
        preference = resolve_functional_group_preference(entity, by_id)
        actual = entity.get("translate") or [0.0, 0.0, 0.0]
        solved_footprint_center = (entity.get("placement_solver") or {}).get(
            "solved_footprint_center_xy"
        )
        if isinstance(solved_footprint_center, list) and len(solved_footprint_center) >= 2:
            actual_xy = [float(solved_footprint_center[0]), float(solved_footprint_center[1])]
            actual_xy_source = "placement_solver_visible_footprint_center"
        else:
            actual_xy = [float(actual[0]), float(actual[1])]
            actual_xy_source = "entity_translate_fallback"
        if not preference:
            rows.append({"entity_id": entity["id"], "status": "fail", "reason": "functional_group_anchor_missing"})
            continue
        position_error = math.hypot(
            actual_xy[0] - preference["preferred_xy"][0],
            actual_xy[1] - preference["preferred_xy"][1],
        )
        yaw_error = abs(_wrapped_yaw_delta(
            preference["desired_yaw_deg"],
            float((entity.get("rotate") or [0.0, 0.0, 0.0])[2]),
        ))
        position_pass = position_error <= float(position_tolerance_m)
        yaw_pass = yaw_error <= float(yaw_tolerance_deg)
        connection = group.get("connection") if isinstance(group.get("connection"), dict) else None
        connection_error = None
        connection_pass = True
        connection_position_pass = True
        semantic_port_status = "not_required"
        semantic_port_pass = True
        port_role_status = "not_checked"
        port_role_pass = True
        port_normal_error_deg = None
        port_normal_status = "not_checked"
        port_normal_pass = True
        if connection:
            anchor = by_id[preference["anchor_id"]]
            anchor_port = _world_group_point(anchor, connection["anchor_port_local_m"])
            member_port = _world_group_point(entity, connection["member_port_local_m"])
            connection_error = math.sqrt(sum((member_port[index] - anchor_port[index]) ** 2 for index in range(3)))
            connection_position_pass = connection_error <= float(connection.get("position_tolerance_m") or 0.08)
            anchor_semantic = connection.get("anchor_port") if isinstance(connection.get("anchor_port"), dict) else None
            member_semantic = connection.get("member_port") if isinstance(connection.get("member_port"), dict) else None
            require_semantic = bool(connection.get("require_semantic_evidence", False))
            if anchor_semantic or member_semantic or require_semantic:
                semantic_port_pass = bool(
                    anchor_semantic and member_semantic
                    and anchor_semantic.get("evidence_status") == "verified"
                    and member_semantic.get("evidence_status") == "verified"
                    and str(anchor_semantic.get("evidence_source") or "").strip()
                    and str(member_semantic.get("evidence_source") or "").strip()
                )
                semantic_port_status = "verified" if semantic_port_pass else "pending_or_missing"
                if not require_semantic:
                    semantic_port_pass = True
            if anchor_semantic and member_semantic:
                port_role_pass = _port_roles_compatible(
                    str(anchor_semantic.get("role") or ""),
                    str(member_semantic.get("role") or ""),
                )
                port_role_status = "pass" if port_role_pass else "fail"
            if connection.get("require_opposed_normals"):
                anchor_normal_angle = _axis_world_angle_deg(
                    str((anchor_semantic or {}).get("normal_axis") or ""),
                    float((anchor.get("rotate") or [0.0, 0.0, 0.0])[2]),
                )
                member_normal_angle = _axis_world_angle_deg(
                    str((member_semantic or {}).get("normal_axis") or ""),
                    float((entity.get("rotate") or [0.0, 0.0, 0.0])[2]),
                )
                if anchor_normal_angle is None or member_normal_angle is None:
                    port_normal_pass = False
                else:
                    opposed_error = abs(_wrapped_yaw_delta(anchor_normal_angle + 180.0, member_normal_angle))
                    port_normal_error_deg = opposed_error
                    port_normal_pass = opposed_error <= float(connection.get("normal_tolerance_deg") or 5.0)
                port_normal_status = "pass" if port_normal_pass else "fail"
            connection_pass = (
                connection_position_pass and semantic_port_pass and port_role_pass and port_normal_pass
            )
        envelope = group.get("work_envelope") if isinstance(group.get("work_envelope"), dict) else None
        work_distance = None
        work_pass = True
        if envelope:
            anchor = by_id[preference["anchor_id"]]
            target_local = envelope.get("target_offset_local_m") or [0.0, 0.0, 0.0]
            target_world = _world_group_point(anchor, target_local)
            actual_world = [float(value) for value in actual[:3]]
            work_distance = math.sqrt(sum((actual_world[index] - target_world[index]) ** 2 for index in range(3)))
            work_pass = work_distance <= float(envelope["radius_m"])
        rows.append({
            "entity_id": str(entity["id"]),
            "group_id": str(group.get("id") or ""),
            "anchor_id": preference["anchor_id"],
            "member_slot": group.get("member_slot"),
            "expected_xy": preference["preferred_xy"],
            "actual_xy": actual_xy,
            "actual_xy_source": actual_xy_source,
            "position_error_m": position_error,
            "position_status": "pass" if position_pass else "fail",
            "expected_yaw_deg": preference["desired_yaw_deg"],
            "actual_yaw_deg": float((entity.get("rotate") or [0.0, 0.0, 0.0])[2]),
            "yaw_error_deg": yaw_error,
            "yaw_status": "pass" if yaw_pass else "fail",
            "orientation_status": group.get("orientation_status") or "authored_yaw",
            "connection_error_m": connection_error,
            "connection_position_status": "pass" if connection_position_pass else "fail",
            "semantic_port_status": semantic_port_status,
            "port_role_status": port_role_status,
            "port_normal_error_deg": port_normal_error_deg,
            "port_normal_status": port_normal_status,
            "connection_status": "pass" if connection_pass else "fail",
            "work_envelope_distance_m": work_distance,
            "work_envelope_status": "pass" if work_pass else "fail",
            "atomic": bool(group.get("atomic", False)),
            "status": "pass" if position_pass and yaw_pass and connection_pass and work_pass else "fail",
        })
    failed = [row for row in rows if row["status"] != "pass"]
    return {
        "schema_version": "isaac-functional-group-inspection/v1",
        "status": "pass" if not failed else "fail",
        "member_count": len(rows),
        "passed_member_count": len(rows) - len(failed),
        "failed_member_count": len(failed),
        "orientation_pending_count": sum(row.get("orientation_status") == "pending_front_axis" for row in rows),
        "position_tolerance_m": float(position_tolerance_m),
        "yaw_tolerance_deg": float(yaw_tolerance_deg),
        "members": rows,
    }


def derive_semantic_layout_spec(
    inventory_spec: dict[str, Any],
    scene_brief: dict[str, Any],
    *,
    layout_profile: dict[str, Any] | None = None,
    room_margin_m: float = 0.25,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Compile a pose-free inventory into functional-zone placement constraints.

    This is the prompt/image generation path. It deliberately refuses exact-pose
    regression fields and discards any previous translate, rotate, coarse zone or
    placement solution before assigning deterministic slots from Scene Brief and
    domain semantics.
    """
    inventory_spec, shell_refresh = refresh_procedural_room_shell(inventory_spec, scene_brief)
    entities = [
        item for item in inventory_spec.get("entities", [])
        if isinstance(item, dict) and item.get("id")
    ]
    leaked = sorted(
        field for field in PRIVILEGED_LAYOUT_FIELDS
        if any(field in item for item in entities)
    )
    if leaked:
        raise ValueError(f"semantic layout input contains privileged regression fields: {', '.join(leaked)}")
    room_entity = next((item for item in entities if item.get("asset_scope") == "scene_root"), None)
    room_bounds = deepcopy((inventory_spec.get("room") or {}).get("usable_bounds_m") or {})
    if not room_bounds and room_entity:
        room_bounds = deepcopy(room_entity.get("asset_bounds_m") or {})
    lower, upper = room_bounds.get("min"), room_bounds.get("max")
    if not (
        isinstance(lower, list) and isinstance(upper, list)
        and len(lower) >= 3 and len(upper) >= 3
    ):
        size = (scene_brief.get("room") or {}).get("size_m")
        if not (isinstance(size, list) and len(size) >= 3):
            raise ValueError("semantic layout requires room usable bounds or Scene Brief room.size_m")
        lower = [-float(size[0]) * 0.5, -float(size[1]) * 0.5, 0.0]
        upper = [float(size[0]) * 0.5, float(size[1]) * 0.5, float(size[2])]
    room_bounds = {
        "min": [float(value) for value in lower[:3]],
        "max": [float(value) for value in upper[:3]],
    }
    room_bounds["size"] = [room_bounds["max"][i] - room_bounds["min"][i] for i in range(3)]
    zones, rules, default_zone = _validated_semantic_zones(layout_profile or {}, scene_brief)
    functional_groups = _validated_functional_groups(layout_profile or {}, scene_brief)

    owner_ids = {str(item["id"]): item for item in entities}
    supported_owner_ids: set[str] = set()
    for item in entities:
        owner = _target_owner(str(item.get("support_target") or item.get("mount_target") or ""), entities)
        if owner:
            supported_owner_ids.add(owner)

    prepared: list[dict[str, Any]] = []
    pending = [deepcopy(item) for item in inventory_spec.get("layout_pending_entities") or [] if isinstance(item, dict)]
    assignments: list[dict[str, Any]] = []
    for source in entities:
        entity = _strip_privileged_entity(source)
        # Every executable Scene Spec entity must carry the exact prim path
        # used by the composer.  make-scene can infer this path, but downstream
        # camera and visibility tools intentionally consume the spec rather
        # than guessing USD paths a second time.
        if not entity.get("prim"):
            entity["prim"] = "/World/Background" if source is room_entity else entity_prim(entity)
        entity.pop("layout_constraints", None)
        entity.pop("placement_solver", None)
        entity.pop("translate", None)
        entity.pop("rotate", None)
        entity.pop("support_plane_z_m", None)
        entity_id = str(entity["id"])
        if source is room_entity:
            entity["translate"] = [0.0, 0.0, 0.0]
            entity["rotate"] = [0.0, 0.0, 0.0]
            entity["layout_role"] = "architectural_shell"
            entity["layout_constraints"] = {"locked": True, "reason": "architectural_shell_origin"}
            prepared.append(entity)
            continue
        target = entity.get("mount_target") or entity.get("support_target")
        if not target:
            pending.append({
                "id": entity_id,
                "semantic_name": entity.get("semantic_name"),
                "model_id": entity.get("model_id"),
                "logical_id": entity.get("logical_id"),
                "path": entity.get("path"),
                "prim": entity.get("prim"),
                "reason": "missing_verified_support_or_mount_relation",
            })
            continue
        owner = _target_owner(str(target), entities)
        target_lower = str(target).lower()
        floor_like = (
            str(target) in FLOOR_TARGETS or "/floor" in target_lower
            or "floor_" in target_lower or "floor1" in target_lower
        )
        target_owner = owner_ids.get(str(owner or "")) or {}
        unsupported_architectural_mount = (
            bool(entity.get("support_target"))
            and target_owner.get("asset_scope") == "scene_root"
            and not floor_like
        )
        if unsupported_architectural_mount:
            pending.append({
                "id": entity_id,
                "semantic_name": entity.get("semantic_name"),
                "model_id": entity.get("model_id"),
                "logical_id": entity.get("logical_id"),
                "path": entity.get("path"),
                "prim": entity.get("prim"),
                "reason": "architectural_mount_relation_required",
            })
            continue
        if entity.get("mount_target"):
            layout_role, solver = "mounted_fixture", "fixed_mount"
        elif entity_id in supported_owner_ids:
            layout_role, solver = "support_furniture", "mesh_support"
        elif floor_like and str(entity.get("placement_policy") or "") == "floor_underlay":
            layout_role, solver = "floor_underlay", "mesh_support"
        elif floor_like:
            layout_role, solver = "ground_object", "mesh_support"
        elif owner:
            layout_role, solver = "supported_prop", "mesh_support"
        else:
            layout_role, solver = "ground_object", "mesh_support"
        entity["layout_role"] = layout_role
        rule = next((row for row in rules if _semantic_rule_matches(entity, layout_role, row)), {})
        zone_id = str(rule.get("zone") or default_zone)
        assignments.append({
            "entity": entity,
            "owner": owner,
            "layout_role": layout_role,
            "solver": solver,
            "rule": rule,
            "zone_id": zone_id,
            "distribution": str(rule.get("distribution") or zones[zone_id].get("distribution") or "grid"),
        })

    functional_member_ids = _assigned_functional_group_member_ids(assignments, functional_groups)
    group_counts: dict[tuple[str, str, str], int] = {}
    for row in assignments:
        if str(row["entity"]["id"]) in functional_member_ids:
            continue
        key = (row["zone_id"], row["distribution"], str(row["rule"].get("id") or "domain_default"))
        group_counts[key] = group_counts.get(key, 0) + 1
    group_indices: dict[tuple[str, str, str], int] = {}
    by_id: dict[str, dict[str, Any]] = {}
    zone_histogram: dict[str, int] = {}
    # Owners always precede their supported props in this pass.
    role_rank = {
        "floor_underlay": -1,
        "support_furniture": 0,
        "ground_object": 1,
        "mounted_fixture": 1,
        "supported_prop": 2,
    }
    assignments.sort(key=lambda row: (role_rank.get(row["layout_role"], 1), -_footprint_area(row["entity"]), str(row["entity"]["id"])))
    for row in assignments:
        entity, rule = row["entity"], row["rule"]
        owner_layout = by_id.get(str(row["owner"] or ""))
        if row["layout_role"] == "supported_prop" and owner_layout:
            zone_id = str(owner_layout["layout_constraints"]["zone"]["id"])
            preferred_xy = list(owner_layout["layout_constraints"]["preferred_xy"])
            distribution = "inherit_support"
            slot_index = 0
        else:
            zone_id, distribution = row["zone_id"], row["distribution"]
            key = (zone_id, distribution, str(rule.get("id") or "domain_default"))
            is_functional_member = str(entity["id"]) in functional_member_ids
            index = 0 if is_functional_member else group_indices.get(key, 0)
            slot_index = index
            if not is_functional_member:
                group_indices[key] = index + 1
            normalized_xy = _semantic_zone_slot(
                zones[zone_id]["bounds_norm"], index, group_counts.get(key, 1), distribution,
            )
            preferred_xy = [
                room_bounds["min"][axis] + room_bounds["size"][axis] * normalized_xy[axis]
                for axis in range(2)
            ]
            preferred_xy = [
                max(
                    room_bounds["min"][axis] + room_margin_m,
                    min(preferred_xy[axis], room_bounds["max"][axis] - room_margin_m),
                )
                for axis in range(2)
            ]
        orientation_priority = str(rule.get("orientation_priority") or "")
        wall_affinity = None
        suppressed_facing_target = None
        if orientation_priority == "wall_flush":
            preferred_xy, yaw, wall_affinity = _wall_flush_pose(entity, rule, room_bounds, preferred_xy)
            if entity.get("facing_target"):
                suppressed_facing_target = str(entity.pop("facing_target"))
        else:
            yaw_sequence = rule.get("yaw_sequence_deg") or []
            if isinstance(yaw_sequence, list) and yaw_sequence:
                yaw = float(yaw_sequence[slot_index % len(yaw_sequence)])
            else:
                yaw = float(rule.get("preferred_yaw_deg") or 0.0)
        if orientation_priority != "wall_flush":
            preferred_xy = _clamp_footprint_center_to_room(
                entity, preferred_xy, room_bounds, room_margin_m, yaw,
            )
        entity["translate"] = [preferred_xy[0], preferred_xy[1], 0.0]
        entity["rotate"] = [0.0, 0.0, yaw]
        entity["layout_constraints"] = {
            "solver": row["solver"],
            "zone": {"id": zone_id, "bounds_norm": deepcopy(zones[zone_id]["bounds_norm"]), "purpose": zones[zone_id].get("purpose")},
            "preferred_xy": preferred_xy,
            "distribution": distribution,
            "semantic_rule_id": rule.get("id") or "domain_default",
            "yaw_bucket_deg": yaw,
            "yaw_deltas_deg": deepcopy(rule.get("yaw_deltas_deg") or ([0.0] if orientation_priority == "wall_flush" else [0.0, 90.0, -90.0, 180.0])),
            "obstacle_clearance_m": float(rule.get("obstacle_clearance_m") or (0.0 if row["layout_role"] == "floor_underlay" else 0.10 if row["layout_role"] in {"ground_object", "support_furniture"} else 0.01)),
            "support_margin_m": float(rule.get("support_margin_m") or (0.05 if row["layout_role"] in {"ground_object", "support_furniture"} else 0.01)),
            **({
                "minimum_mesh_coverage": float(rule["minimum_mesh_coverage"]),
            } if rule.get("minimum_mesh_coverage") is not None else {}),
            **({"mount_surface": str(rule.get("mount_surface") or "ceiling")} if entity.get("mount_target") else {}),
            **({"orientation_priority": orientation_priority, "wall_affinity": wall_affinity, "wall_clearance_m": float(rule.get("wall_clearance_m") or 0.05)} if orientation_priority == "wall_flush" else {}),
            **({"suppressed_facing_target": suppressed_facing_target, "suppression_reason": "wall_flush_priority"} if suppressed_facing_target else {}),
        }
        prepared.append(entity)
        by_id[str(entity["id"])] = entity
        zone_histogram[zone_id] = zone_histogram.get(zone_id, 0) + 1

    functional_group_records, functional_group_relations = _apply_functional_groups(
        prepared,
        functional_groups,
        room_bounds,
        room_margin_m,
    )

    active_ids = {str(item["id"]) for item in prepared}
    relations = []
    for relation in inventory_spec.get("relations") or []:
        if not isinstance(relation, dict) or str(relation.get("subject") or "") not in active_ids:
            continue
        row = {key: deepcopy(value) for key, value in relation.items() if key != "resolved_transform"}
        relations.append(row)
    relations.extend(functional_group_relations)
    spec = deepcopy(inventory_spec)
    spec["entities"] = prepared
    spec["relations"] = relations
    spec["layout_pending_entities"] = pending
    spec.pop("constraint_layout_provenance", None)
    room = deepcopy(spec.get("room") or {})
    room["quality_target"] = "semantic_layout_generation"
    room["usable_bounds_m"] = room_bounds
    spec["room"] = room
    spec["constraints"] = {
        "layout_mode": "semantic_functional_zones",
        "exact_pose_allowed": False,
        "instance_opinions_allowed": False,
        "room_margin_m": float(room_margin_m),
        "minimum_corridor_m": float((scene_brief.get("circulation") or {}).get("minimum_path_width_m") or 0.7),
        "functional_zones": [deepcopy(zones[key]) for key in zones],
        "architectural_context_policy": "self_contained_scene_root_wrapper_only",
        "functional_groups": deepcopy(functional_groups),
    }
    # A pose-free inventory may intentionally contain asset identities only.
    # Materialize the current Scene Brief lighting here so make-scene never
    # receives a geometrically valid but completely unlit generated scene.
    # The current brief is authoritative over any stale inventory rig.
    lighting = scene_brief.get("lighting")
    lighting_room = deepcopy(room)
    lighting_room.setdefault(
        "size_m",
        deepcopy((scene_brief.get("room") or {}).get("size_m") or room_bounds["size"]),
    )
    if isinstance(lighting, dict):
        spec["lights"] = build_lighting_rig(scene_brief, lighting_room)
        spec["lighting_policy"] = deepcopy(lighting)
    elif not spec.get("lights"):
        spec["lights"] = build_lighting_rig(scene_brief, lighting_room)
        spec["lighting_policy"] = {"mode": "default"}
    spec["brief_snapshot"] = deepcopy(scene_brief)
    spec["semantic_layout_provenance"] = {
        "schema_version": "isaac-semantic-layout-provenance/v1",
        "mode": "scene_brief_functional_zones_without_pose",
        "domain": str(scene_brief.get("domain") or inventory_spec.get("domain") or ""),
        "input_mode": str((scene_brief.get("inputs") or {}).get("mode") or ""),
        "forbidden_fields": sorted(PRIVILEGED_LAYOUT_FIELDS),
        "ignored_prior_fields": ["translate", "rotate", "layout_constraints", "placement_solver"],
    }
    forbidden_present = sorted(
        field for field in PRIVILEGED_LAYOUT_FIELDS
        if any(field in item for item in prepared)
    )
    report = {
        "schema_version": "isaac-semantic-layout-compile-report/v1",
        "status": "fail" if forbidden_present or not prepared else "pass",
        "source_entity_count": len(entities),
        "active_entity_count": len(prepared),
        "pending_entity_count": len(pending),
        "relation_count": len(relations),
        "zone_histogram": dict(sorted(zone_histogram.items())),
        "room_bounds_m": room_bounds,
        "domain": spec["semantic_layout_provenance"]["domain"],
        "pose_source": "none",
        "forbidden_fields_present": forbidden_present,
        "pending_entities": pending,
        "functional_group_records": functional_group_records,
        "functional_group_member_count": sum(row["assigned_member_count"] for row in functional_group_records),
        "functional_group_incomplete_count": sum(row["status"] != "pass" for row in functional_group_records),
        "functional_group_orientation_pending_count": sum(row["orientation_pending_count"] for row in functional_group_records),
        "procedural_shell_refresh": shell_refresh,
    }
    return spec, report


def solve_fixed_mount_translation(
    *,
    anchor_world: list[float],
    target_bounds: dict[str, Any],
    preferred_xy: list[float] | None,
    mount_surface: str = "ceiling",
    margin_m: float = 0.02,
) -> dict[str, Any]:
    """Return a world translation that aligns an authored mount anchor to a surface."""
    lower, upper = target_bounds.get("min"), target_bounds.get("max")
    if not (isinstance(lower, list) and isinstance(upper, list) and len(lower) >= 3 and len(upper) >= 3):
        return {"status": "fail", "reason": "mount_target_bounds_missing"}
    if mount_surface != "ceiling":
        return {"status": "fail", "reason": "unsupported_mount_surface", "mount_surface": mount_surface}
    desired_xy = preferred_xy or [
        (float(lower[0]) + float(upper[0])) * 0.5,
        (float(lower[1]) + float(upper[1])) * 0.5,
    ]
    target = [
        max(float(lower[0]) + margin_m, min(float(desired_xy[0]), float(upper[0]) - margin_m)),
        max(float(lower[1]) + margin_m, min(float(desired_xy[1]), float(upper[1]) - margin_m)),
        float(lower[2]),
    ]
    delta = [target[index] - float(anchor_world[index]) for index in range(3)]
    return {
        "status": "pass",
        "mount_surface": mount_surface,
        "target_point_world": target,
        "suggested_world_translation_delta": delta,
        "recommended_yaw_delta_deg": 0.0,
        "mesh_support_coverage": None,
        "support_margin_coverage": None,
    }


def resolve_scene_target(spec: dict[str, Any], target: Any) -> dict[str, Any]:
    """Resolve an entity/collider id or absolute prim into a stable target record."""
    value = str(target or "").strip()
    entities = [
        item for item in spec.get("entities", [])
        if isinstance(item, dict) and item.get("id")
    ]
    colliders = [
        item for item in spec.get("support_colliders", [])
        if isinstance(item, dict) and item.get("id")
    ]
    if value in FLOOR_TARGETS or value.lower() in {item.lower() for item in FLOOR_TARGETS}:
        matching_floor = next(
            (
                item for item in colliders
                if str(item.get("id") or "").lower() in {"floor", "ground", "room_floor"}
            ),
            None,
        )
        return {
            "status": "resolved",
            "kind": "floor",
            "target": value,
            "prim": _support_collider_prim(matching_floor) if matching_floor else value,
            "owner_entity_id": None,
        }
    owner = _target_owner(value, entities)
    if owner:
        owner_entity = next(item for item in entities if str(item["id"]) == owner)
        return {
            "status": "resolved",
            "kind": "entity",
            "target": value,
            "prim": value if value.startswith("/") else entity_prim(owner_entity),
            "owner_entity_id": owner,
        }
    for collider in colliders:
        if value in {str(collider.get("id") or ""), _support_collider_prim(collider)}:
            return {
                "status": "resolved",
                "kind": "support_collider",
                "target": value,
                "prim": _support_collider_prim(collider),
                "owner_entity_id": None,
            }
    if value.startswith("/"):
        return {
            "status": "resolved",
            "kind": "external_prim",
            "target": value,
            "prim": value,
            "owner_entity_id": None,
        }
    return {
        "status": "unresolved",
        "kind": "unknown",
        "target": value,
        "prim": None,
        "owner_entity_id": None,
    }


def build_support_dependency_graph(spec: dict[str, Any]) -> dict[str, Any]:
    """Build a deterministic support graph and a safe placement order."""
    entities = [
        item for item in spec.get("entities", [])
        if isinstance(item, dict) and item.get("id")
    ]
    by_id = {str(item["id"]): item for item in entities}
    dependencies: dict[str, set[str]] = {entity_id: set() for entity_id in by_id}
    children: dict[str, set[str]] = {entity_id: set() for entity_id in by_id}
    target_records: dict[str, dict[str, Any]] = {}
    unresolved: list[dict[str, Any]] = []

    for entity_id, entity in by_id.items():
        target_kind = "fixed_mount" if entity.get("mount_target") not in (None, "") else "support"
        target = entity.get("mount_target") if target_kind == "fixed_mount" else entity.get("support_target")
        if target in (None, ""):
            continue
        record = resolve_scene_target(spec, target)
        record["relation_kind"] = target_kind
        target_records[entity_id] = record
        owner = record.get("owner_entity_id")
        if record["status"] != "resolved":
            unresolved.append({"entity_id": entity_id, "support_target": str(target)})
        elif owner and owner != entity_id:
            dependencies[entity_id].add(str(owner))
            children[str(owner)].add(entity_id)
        elif owner == entity_id:
            dependencies[entity_id].add(entity_id)

    for entity_id, entity in by_id.items():
        functional_group = ((entity.get("layout_constraints") or {}).get("functional_group") or {})
        anchor_id = str(functional_group.get("anchor_id") or "")
        if anchor_id and anchor_id in by_id and anchor_id != entity_id:
            dependencies[entity_id].add(anchor_id)
            children[anchor_id].add(entity_id)

    def tier(entity_id: str) -> tuple[int, str]:
        record = target_records.get(entity_id)
        declared_role = str(by_id[entity_id].get("layout_role") or "")
        functional_group = ((by_id[entity_id].get("layout_constraints") or {}).get("functional_group") or {})
        if functional_group.get("anchor_id"):
            return 0, "functional_group_member"
        if declared_role == "floor_underlay":
            return -1, "floor_underlay"
        if declared_role == "support_furniture":
            return 0, "support_furniture"
        if declared_role == "mounted_fixture":
            return (0 if children[entity_id] else 1), "mounted_fixture"
        if declared_role == "ground_object":
            return 1, "ground_object"
        if declared_role == "supported_prop":
            return 2, "supported_prop"
        if record and record.get("relation_kind") == "fixed_mount":
            return (0 if children[entity_id] else 1), "mounted_fixture"
        if record and record.get("owner_entity_id"):
            return 2, "supported_prop"
        if children[entity_id]:
            return 0, "support_furniture"
        return 1, "ground_object"

    def available_key(entity_id: str) -> tuple[int, int, int, int, float, str]:
        entity = by_id[entity_id]
        functional_group = (
            (entity.get("layout_constraints") or {}).get("functional_group") or {}
        )
        is_functional_member = bool(functional_group.get("anchor_id"))
        # Reserve anchor-relative seats/operators before decor and other peers can
        # occupy their functional envelope.  Within one group, preserve the
        # declared member-rule order (for example enclosure before console)
        # instead of letting footprint area silently reverse the topology.
        # Supported props remain dependency-led.
        group_priority = 0 if is_functional_member else 1
        group_member_priority = (
            int(functional_group.get("member_rule_index") or 0)
            if is_functional_member else 0
        )
        enrichment_priority = 1 if entity.get("generation_layer") == "enrichment" else 0
        return (
            tier(entity_id)[0], group_priority, group_member_priority,
            enrichment_priority, -_footprint_area(entity), entity_id,
        )

    indegree = {entity_id: len(rows) for entity_id, rows in dependencies.items()}
    available = sorted(
        available_key(entity_id)
        for entity_id, degree in indegree.items()
        if degree == 0
    )
    order: list[str] = []
    while available:
        (
            _rank, _group_priority, _group_member_priority,
            _enrichment_priority, _negative_area, entity_id,
        ) = available.pop(0)
        order.append(entity_id)
        for child in sorted(children[entity_id]):
            indegree[child] -= 1
            if indegree[child] == 0:
                available.append(available_key(child))
                available.sort()
    cycle_nodes = sorted(entity_id for entity_id, degree in indegree.items() if degree > 0)
    rows = [
        {
            "entity_id": entity_id,
            "prim": entity_prim(by_id[entity_id]),
            "tier": tier(entity_id)[1],
            "layout_role": str(by_id[entity_id].get("layout_role") or ""),
            "placement_policy": str(by_id[entity_id].get("placement_policy") or ""),
            "support": target_records.get(entity_id),
            "support_surface_z_m": (
                (by_id[entity_id].get("layout_constraints") or {}).get("support_surface_z_m")
            ),
            "depends_on": sorted(dependencies[entity_id]),
            "supports": sorted(children[entity_id]),
        }
        for entity_id in order + cycle_nodes
    ]
    return {
        "status": "fail" if cycle_nodes or unresolved else "pass",
        "order": order,
        "nodes": rows,
        "unresolved_targets": unresolved,
        "cycles": cycle_nodes,
    }


def apply_placement_solution(
    spec: dict[str, Any],
    entity_id: str,
    solution: dict[str, Any],
    *,
    support_prim: str,
    obstacle_prims: list[str],
    relation_kind: str = "surface_support",
) -> dict[str, Any]:
    """Return a new spec version with one solver result applied to an entity."""
    updated = deepcopy(spec)
    entity = next(
        (
            item for item in updated.get("entities", [])
            if isinstance(item, dict) and str(item.get("id") or "") == entity_id
        ),
        None,
    )
    if entity is None:
        raise KeyError(f"Unknown Scene Spec entity: {entity_id}")
    translation = [float(value) for value in (entity.get("translate") or [0.0, 0.0, 0.0])]
    rotation = [float(value) for value in (entity.get("rotate") or [0.0, 0.0, 0.0])]
    while len(translation) < 3:
        translation.append(0.0)
    while len(rotation) < 3:
        rotation.append(0.0)
    delta = [float(value) for value in solution["suggested_local_translation_delta"]]
    yaw_delta = float(solution.get("recommended_yaw_delta_deg") or 0.0)
    entity["translate"] = [translation[index] + delta[index] for index in range(3)]
    entity["rotate"] = [rotation[0], rotation[1], rotation[2] + yaw_delta]
    entity["placement_solver"] = {
        "schema_version": "isaac-scene-placement-solution/v1",
        "relation_kind": relation_kind,
        "support_prim": support_prim,
        "obstacle_prims": list(obstacle_prims),
        "translation_delta": delta,
        "yaw_delta_deg": yaw_delta,
        "solved_footprint_center_xy": (
            [float(value) for value in solution["suggested_footprint_center_xy"][:2]]
            if isinstance(solution.get("suggested_footprint_center_xy"), list)
            and len(solution["suggested_footprint_center_xy"]) >= 2
            else None
        ),
        "mesh_support_coverage": solution.get("mesh_support_coverage"),
        "support_margin_coverage": solution.get("support_margin_coverage"),
        "architectural_obstacle_root": solution.get("architectural_obstacle_root"),
        "architectural_obstacle_count": solution.get("architectural_obstacle_count", 0),
        "architectural_obstacle_prims": list(solution.get("architectural_obstacle_prims") or []),
    }
    for relation in updated.get("relations", []):
        if not isinstance(relation, dict) or str(relation.get("subject") or "") != entity_id:
            continue
        relation["resolved_transform"] = list(entity["translate"])
        relation["resolution_source"] = "solve_scene_placement_batch"
    return updated


def clear_pending_entity(spec: dict[str, Any], entity_id: str) -> dict[str, Any]:
    """Return a new spec without a successfully solved pending entry."""
    updated = deepcopy(spec)
    updated["layout_pending_entities"] = [
        pending
        for pending in updated.get("layout_pending_entities") or []
        if (
            str(pending.get("id") or "") if isinstance(pending, dict) else str(pending)
        ) != entity_id
    ]
    return updated


def automatic_obstacles(
    graph: dict[str, Any],
    entity_id: str,
    placed_entity_ids: list[str],
) -> list[str]:
    """Use earlier objects on the same exact support surface as obstacles."""
    nodes = {str(row["entity_id"]): row for row in graph.get("nodes", [])}
    current = nodes.get(entity_id) or {}
    current_prim = ((current.get("support") or {}).get("prim"))
    current_surface = current.get("support_surface_z_m")
    if not current_prim:
        return []
    return [
        str(nodes[placed]["prim"])
        for placed in placed_entity_ids
        if placed in nodes
        and ((nodes[placed].get("support") or {}).get("prim")) == current_prim
        and (
            (current_surface is None and nodes[placed].get("support_surface_z_m") is None)
            or (
                current_surface is not None
                and nodes[placed].get("support_surface_z_m") is not None
                and abs(float(nodes[placed]["support_surface_z_m"]) - float(current_surface)) <= 0.01
            )
        )
        and nodes[placed].get("layout_role") != "floor_underlay"
        and nodes[placed].get("placement_policy") != "non_blocking"
    ]


def select_architectural_obstacle_rows(
    rows: list[dict[str, Any]],
    *,
    object_z_min: float,
    object_z_max: float,
    support_prim_path: str,
    max_span_m: float = 10.0,
    penetration_tolerance: float = 0.005,
    support_layer_depth_m: float = 0.25,
    volumetric_aggregate_min_xy_m: float = 2.0,
    volumetric_aggregate_min_z_m: float = 1.0,
) -> dict[str, Any]:
    """Keep shell geometry that can physically intersect the placed object.

    The support surface only touches the object's bottom and must not become an
    obstacle.  Likewise, ceilings above a short object are irrelevant.  This
    small pure helper keeps USD traversal separate from deterministic filtering
    so the policy can be regression-tested without Isaac Sim.
    """
    selected: list[dict[str, Any]] = []
    skipped = {
        "support": 0,
        "degenerate": 0,
        "broad": 0,
        "support_contact_layer": 0,
        "volumetric_aggregate": 0,
        "outside_height_interval": 0,
    }
    support_prefix = support_prim_path.rstrip("/")
    for row in rows:
        prim_path = str(row.get("prim") or "")
        if prim_path == support_prefix or prim_path.startswith(support_prefix + "/"):
            skipped["support"] += 1
            continue
        lower = row.get("min") or []
        upper = row.get("max") or []
        size = row.get("size") or []
        if len(lower) < 3 or len(upper) < 3 or len(size) < 3 or any(float(value) <= 0.0 for value in size[:3]):
            skipped["degenerate"] += 1
            continue
        if max(float(value) for value in size[:3]) > float(max_span_m):
            skipped["broad"] += 1
            continue
        if (
            "floor" in prim_path.lower()
            and float(upper[2]) <= float(object_z_min) + float(support_layer_depth_m)
        ):
            skipped["support_contact_layer"] += 1
            continue
        if is_volumetric_aggregate_row(
            row,
            minimum_xy_span_m=volumetric_aggregate_min_xy_m,
            minimum_z_span_m=volumetric_aggregate_min_z_m,
        ):
            skipped["volumetric_aggregate"] += 1
            continue
        penetration = min(float(object_z_max), float(upper[2])) - max(float(object_z_min), float(lower[2]))
        if penetration <= float(penetration_tolerance):
            skipped["outside_height_interval"] += 1
            continue
        selected.append(row)
    return {
        "rows": selected,
        "candidate_count": len(rows),
        "selected_count": len(selected),
        "selected_prims": [str(row.get("prim") or "") for row in selected],
        "skipped": skipped,
        "object_z_interval": [float(object_z_min), float(object_z_max)],
    }


def is_architectural_prim_path(
    prim_path: str,
    path_tokens: list[str] | tuple[str, ...] | None = None,
) -> bool:
    """Return whether a scene-root descendant has architectural path semantics."""
    normalized = str(prim_path).lower()
    tokens = path_tokens or DEFAULT_ARCHITECTURAL_PATH_TOKENS
    return any(str(token).strip().lower() in normalized for token in tokens if str(token).strip())


def is_volumetric_aggregate_row(
    row: dict[str, Any],
    *,
    minimum_xy_span_m: float = 2.0,
    minimum_z_span_m: float = 1.0,
) -> bool:
    """Detect a multi-wall/perimeter AABB that is unsafe as a solid proxy."""
    size = row.get("size") or []
    return (
        len(size) >= 3
        and min(float(size[0]), float(size[1])) > float(minimum_xy_span_m)
        and float(size[2]) > float(minimum_z_span_m)
    )
