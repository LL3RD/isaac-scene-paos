from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from typing import Any


DEFAULT_LIBRARY = Path(__file__).resolve().parents[2] / "config" / "architectural_shells.json"
OCCUPANCY_RANK = {
    "architecture_only": 0,
    "empty": 0,
    "sparse": 1,
    "unknown": 2,
    "furnished": 3,
}


def load_library(path: str | Path | None = None) -> dict[str, Any]:
    source = Path(path) if path else DEFAULT_LIBRARY
    data = json.loads(source.read_text(encoding="utf-8"))
    if data.get("schema_version") != "isaac-architectural-shell-library/v1":
        raise ValueError(f"Unsupported architectural-shell library: {source}")
    if not isinstance(data.get("shells"), dict):
        raise ValueError(f"Architectural-shell library has no shells: {source}")
    return data


def list_shells(path: str | Path | None = None) -> list[dict[str, Any]]:
    library = load_library(path)
    return [
        {
            "id": shell_id,
            "kind": row.get("kind"),
            "room_types": deepcopy(row.get("room_types") or []),
            "features": deepcopy(row.get("features") or []),
            "occupancy": row.get("occupancy"),
            "asset_backed": str(row.get("kind") or "").startswith("asset_backed"),
        }
        for shell_id, row in sorted(library["shells"].items())
    ]


def select_shell(
    room_type: str,
    *,
    required_features: list[str] | None = None,
    max_occupancy: str = "sparse",
    library_path: str | Path | None = None,
) -> dict[str, Any]:
    library = load_library(library_path)
    maximum = OCCUPANCY_RANK.get(max_occupancy)
    if maximum is None:
        raise ValueError(f"Unknown max occupancy: {max_occupancy}")
    required = {str(value).lower() for value in (required_features or [])}
    accepted: list[tuple[tuple[int, int, str], str, dict[str, Any]]] = []
    rejected: list[dict[str, Any]] = []
    for shell_id, source in sorted(library["shells"].items()):
        row = deepcopy(source)
        features = {str(value).lower() for value in row.get("features") or []}
        reasons = []
        if room_type not in (row.get("room_types") or []):
            reasons.append("room_type_mismatch")
        if not required <= features:
            reasons.append("missing_features:" + ",".join(sorted(required - features)))
        occupancy = str(row.get("occupancy") or "unknown")
        if OCCUPANCY_RANK.get(occupancy, OCCUPANCY_RANK["unknown"]) > maximum:
            reasons.append("occupancy_too_high")
        if reasons:
            rejected.append({"id": shell_id, "reasons": reasons})
            continue
        kind = str(row.get("kind") or "")
        rank = (
            0 if kind == "asset_backed_filtered" else 1 if kind == "asset_backed" else 2,
            OCCUPANCY_RANK.get(occupancy, 2),
            shell_id,
        )
        accepted.append((rank, shell_id, row))
    if not accepted:
        return {
            "status": "pending",
            "room_type": room_type,
            "required_features": sorted(required),
            "max_occupancy": max_occupancy,
            "selected": None,
            "rejected": rejected,
        }
    _rank, shell_id, row = sorted(accepted)[0]
    return {
        "status": "pass",
        "room_type": room_type,
        "required_features": sorted(required),
        "max_occupancy": max_occupancy,
        "selected": {"id": shell_id, **row},
        "rejected": rejected,
    }


def _library_root(config: dict[str, Any], name: str) -> Path:
    for row in config.get("libraries", []) or []:
        if isinstance(row, dict) and str(row.get("name") or "") == name:
            return Path(str(row["root"])).expanduser()
    raise ValueError(f"Configured asset library not found: {name}")


def apply_shell(
    spec: dict[str, Any],
    shell_id: str,
    config: dict[str, Any],
    *,
    replace_background: bool = False,
    replace_procedural_shell: bool = False,
    preserve_existing_layout: bool = False,
    library_path: str | Path | None = None,
) -> dict[str, Any]:
    """Apply an asset-backed catalog shell to a new Scene Spec copy."""
    library = load_library(library_path)
    source = library["shells"].get(shell_id)
    if not isinstance(source, dict):
        raise ValueError(f"Unknown architectural shell: {shell_id}")
    if not str(source.get("kind") or "").startswith("asset_backed"):
        raise ValueError(f"{shell_id} is procedural; use apply-room-shell-preset")
    result = deepcopy(spec)
    entities = [item for item in result.get("entities", []) if isinstance(item, dict)]
    existing = [item for item in entities if item.get("role") in {"background", "environment", "room_shell"}]
    if existing and not replace_background:
        raise ValueError("Scene Spec already has a background entity; pass replace_background explicitly")
    if replace_background:
        entities = [item for item in entities if item not in existing]
    asset_path = _library_root(config, str(source["library"])) / str(source["relative_path"])
    shell_entity = {
        "id": shell_id,
        "role": "room_shell",
        "path": str(asset_path),
        "prim": "/World/Background",
        "unit_mode": str(source.get("unit_mode") or "auto"),
        "physics": "none",
    }
    if source.get("scale") is not None:
        shell_entity["scale"] = deepcopy(source["scale"])
    if source.get("allow_unit_mismatch") is not None:
        shell_entity["allow_unit_mismatch"] = bool(source["allow_unit_mismatch"])
    entities.insert(0, shell_entity)
    invalidated_ids: list[str] = []
    if not preserve_existing_layout:
        pose_fields = {
            "translate", "rotate", "matrix", "xform_matrix", "placement_solver",
        }
        for entity in entities[1:]:
            if not any(field in entity for field in pose_fields):
                continue
            entity_id = str(entity.get("id") or entity.get("prim") or "")
            if not entity_id:
                continue
            invalidated_ids.append(entity_id)
            for field in pose_fields:
                entity.pop(field, None)
            constraints = deepcopy(entity.get("layout_constraints") or {})
            constraints["requires_relayout_after_shell_change"] = True
            constraints["architectural_shell_id"] = shell_id
            entity["layout_constraints"] = constraints
    result["entities"] = entities
    if invalidated_ids:
        invalidated = set(invalidated_ids)
        for relation in result.get("relations", []) or []:
            if not isinstance(relation, dict) or str(relation.get("subject") or "") not in invalidated:
                continue
            relation.pop("resolved_transform", None)
            relation.pop("resolution_source", None)
        pending = [
            deepcopy(row)
            for row in result.get("layout_pending_entities", []) or []
            if isinstance(row, dict) and str(row.get("id") or "") not in invalidated
        ]
        by_id = {str(item.get("id") or item.get("prim") or ""): item for item in entities[1:]}
        pending.extend({
            "id": entity_id,
            "prim": by_id[entity_id].get("prim"),
            "support_target": by_id[entity_id].get("support_target"),
            "mount_target": by_id[entity_id].get("mount_target"),
            "reason": "architectural_shell_changed_requires_relayout",
            "architectural_shell_id": shell_id,
        } for entity_id in invalidated_ids)
        result["layout_pending_entities"] = pending
    overrides = [item for item in result.get("prim_overrides", []) if isinstance(item, dict)]
    by_prim = {str(item.get("prim")): deepcopy(item) for item in overrides if item.get("prim")}
    by_prim.update({
        str(item["prim"]): deepcopy(item)
        for item in source.get("prim_overrides", [])
        if isinstance(item, dict) and item.get("prim")
    })
    result["prim_overrides"] = list(by_prim.values())
    room = result.get("room") if isinstance(result.get("room"), dict) else {}
    room["architectural_shell"] = {
        "id": shell_id,
        "kind": source.get("kind"),
        "features": deepcopy(source.get("features") or []),
        "occupancy": source.get("occupancy"),
        "scale": deepcopy(source.get("scale")),
        "unit_mode": source.get("unit_mode"),
        "evidence": deepcopy(source.get("evidence") or {}),
        "preserved_existing_layout": bool(preserve_existing_layout),
        "invalidated_layout_entity_ids": invalidated_ids,
        "layout_order_contract": "architectural_shell_then_support_graph_then_object_placement",
    }
    result["room"] = room
    if replace_procedural_shell:
        result["support_colliders"] = []
        result["lights"] = []
        requested_lighting = deepcopy(result.get("lighting_policy") or {})
        result["lighting_policy"] = {
            "mode": "asset_authored",
            "source": shell_id,
            "requested_policy": requested_lighting,
        }
    return result


def resolve_for_compiled_spec(
    spec: dict[str, Any],
    brief: dict[str, Any],
    config: dict[str, Any],
    *,
    shell: str = "auto",
    required_features: list[str] | None = None,
    max_occupancy: str = "sparse",
    keep_procedural_shell: bool = False,
    library_path: str | Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Resolve and apply an architectural shell after Scene Brief compilation.

    ``auto`` is conservative: it is active only for realistic/reference-match
    briefs and falls back to the compiled procedural room if no verified,
    asset-backed shell satisfies the requested room type and features.
    """
    policy = str(brief.get("quality_policy") or "")
    room = brief.get("room") if isinstance(brief.get("room"), dict) else {}
    room_type = str(room.get("type") or "room")
    requested = room.get("architectural_shell_requirements")
    requested = requested if isinstance(requested, dict) else {}
    features = required_features
    if features is None:
        explicit = requested.get("features")
        features = list(explicit) if isinstance(explicit, list) else ["doors", "windows", "lights"]

    if shell == "none" or (shell == "auto" and policy not in {"realistic", "reference_match"}):
        return deepcopy(spec), {
            "status": "skipped",
            "mode": shell,
            "reason": "disabled" if shell == "none" else "quality_policy_layout_only",
            "selected": None,
        }

    if shell == "auto":
        selection = select_shell(
            room_type,
            required_features=features,
            max_occupancy=max_occupancy,
            library_path=library_path,
        )
        selected = selection.get("selected")
        if selection["status"] != "pass" or not isinstance(selected, dict):
            return deepcopy(spec), {"mode": "auto", **selection}
        if not str(selected.get("kind") or "").startswith("asset_backed"):
            return deepcopy(spec), {
                "mode": "auto",
                **selection,
                "status": "pending",
                "reason": "selected_shell_is_not_asset_backed",
            }
        shell_id = str(selected["id"])
    else:
        shell_id = shell
        selection = {
            "status": "pass",
            "room_type": room_type,
            "required_features": sorted({str(value).lower() for value in features}),
            "max_occupancy": max_occupancy,
            "selected": {"id": shell_id},
            "rejected": [],
        }

    result = apply_shell(
        spec,
        shell_id,
        config,
        replace_background=True,
        replace_procedural_shell=not keep_procedural_shell,
        library_path=library_path,
    )
    return result, {
        "mode": shell,
        **selection,
        "status": "pass",
        "applied": True,
        "procedural_shell_replaced": not keep_procedural_shell,
    }
