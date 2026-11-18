from __future__ import annotations

from copy import deepcopy
from typing import Any


BACKGROUND_ROLES = {"background", "environment", "room_shell"}
ASSEMBLY_ROLES = {"assembly_set", "fabrica_recipe", "assembly_recipe"}


def normalize(spec: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Map the executable subset of Scene Spec v2 to the legacy composer fields.

    Relations and constraints remain in the sidecar for the external harness and
    future layout solvers.  This function only maps explicitly positioned
    entities; it does not pretend to solve spatial intent.
    """

    normalized = deepcopy(spec)
    entities = spec.get("entities") if isinstance(spec.get("entities"), list) else []
    relations = spec.get("relations") if isinstance(spec.get("relations"), list) else []
    if not entities:
        return normalized, {"schema": "legacy", "mapped_entities": [], "unresolved_relations": []}

    known_ids = {str(item.get("id")) for item in entities if isinstance(item, dict) and item.get("id")}
    mapped: list[dict[str, Any]] = []
    references = list(normalized.get("references") or [])

    for entity in entities:
        if not isinstance(entity, dict) or not entity.get("id") or not entity.get("role"):
            continue
        entity_id = str(entity["id"])
        role = str(entity["role"])
        executable = {
            key: deepcopy(entity[key])
            for key in (
                "id", "asset_id", "path", "query", "asset_query", "libraries",
                "types", "prim", "translate", "rotate", "scale", "unit_mode",
                "xform_matrix",
                "regression_instance_opinions",
                "allow_unit_mismatch", "source_meters_per_unit",
                "source_units_verified", "physics", "density", "front_axis",
                "facing_target", "orientation_confidence", "orientation_source",
            )
            if key in entity
        }
        executable.setdefault("id", entity_id)
        if role in BACKGROUND_ROLES and "background" not in normalized:
            executable.setdefault("prim", "/World/Background")
            normalized["background"] = executable
            mapped.append({"id": entity_id, "role": role, "target": "background"})
        elif role in ASSEMBLY_ROLES and "fabrica_recipe" not in normalized and entity.get("assembly"):
            recipe = {key: deepcopy(entity[key]) for key in ("assembly", "prim", "translate", "rotate", "scale", "unit_mode", "allow_unit_mismatch") if key in entity}
            normalized["fabrica_recipe"] = recipe
            mapped.append({"id": entity_id, "role": role, "target": "fabrica_recipe"})
        elif any(key in executable for key in ("asset_id", "path", "query", "asset_query")):
            references.append(executable)
            mapped.append({"id": entity_id, "role": role, "target": "references"})

    if references:
        normalized["references"] = references

    structural_targets = {
        "floor", "wall", "ceiling",
        "front", "back", "left", "right",
    }
    unresolved_relations: list[dict[str, Any]] = []
    for relation in relations:
        if not isinstance(relation, dict):
            continue
        subject = str(relation.get("subject") or "")
        target = str(relation.get("target") or "")
        reason = None
        target_is_known = (
            target in known_ids
            or target in structural_targets
            or target.startswith("/")
        )
        if subject not in known_ids or not target_is_known:
            reason = "unknown_entity"
        elif relation.get("type") and not relation.get("resolved_transform"):
            reason = "layout_solver_required"
        if reason:
            unresolved_relations.append({"relation": relation, "reason": reason})

    normalized.setdefault("schema_version", "isaac-scene-spec/v2")
    return normalized, {
        "schema": "isaac-scene-spec/v2",
        "mapped_entities": mapped,
        "unresolved_relations": unresolved_relations,
    }
