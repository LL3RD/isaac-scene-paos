"""Geometry-frozen, domain-aware appearance passes for generated scenes."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Any


SCHEMA = "isaac-scene-appearance-pass/v1"

WAREHOUSE_CONCRETE_MATERIAL = {
    "library": "isaac_official",
    "relative_path": (
        "Isaac/Environments/Modular_Warehouse/Props/Source/materials/"
        "opaque__concrete__floor_b_warehouse_a.usd"
    ),
    "project_uvw": True,
    "world_space_projection": True,
    "texture_scale": [0.55, 0.55],
}
WORKSHOP_WALL_MATERIAL = {
    "library": "isaac_official",
    "relative_path": (
        "Isaac/Environments/Modular_Warehouse/Props/Source/materials/"
        "opaque__concrete__painted_wall_warehouse_a.usd"
    ),
    "project_uvw": True,
    "world_space_projection": True,
    "texture_scale": [1.5, 1.5],
    "round_edges_radius": 0.003,
}
TRANSIT_PAVER_MATERIAL = {
    "library": "isaac_official",
    "relative_path": (
        "Isaac/Environments/Outdoor/Rivermark/dsready_content/nv_core/"
        "materials/surroundings/opaque__concrete__pavers_02.usd"
    ),
}


PROFILES: dict[str, dict[str, Any]] = {
    "office": {
        "surfaces": {"floor": ([0.145, 0.14, 0.135], 0.9), "walls": ([0.69, 0.70, 0.70], 0.78), "ceiling": ([0.60, 0.61, 0.61], 0.74)},
        "overlays": [
            ("carpet_finish", "floor", [0.0, 0.0, 0.0005], [0.96, 0.95, 0.001], [0.12, 0.115, 0.11], 0.96),
            ("carpet_seam_a", "floor", [-0.25, 0.0, 0.0015], [0.006, 0.95, 0.001], [0.20, 0.19, 0.18], 0.88),
            ("carpet_seam_b", "floor", [0.0, 0.0, 0.0015], [0.006, 0.95, 0.001], [0.20, 0.19, 0.18], 0.88),
            ("carpet_seam_c", "floor", [0.25, 0.0, 0.0015], [0.006, 0.95, 0.001], [0.20, 0.19, 0.18], 0.88),
        ],
        "lighting": {"environment": (780.0, [0.86, 0.91, 1.0]), "fixtures": [
            ("office_key_left", [-0.24, 0.03, 0.94], [-0.12, 0.04, 0.18], [0.22, 0.42], 2600.0, [1.0, 0.93, 0.82]),
            ("office_key_right", [0.24, 0.03, 0.94], [0.12, 0.04, 0.18], [0.22, 0.42], 2600.0, [1.0, 0.93, 0.82]),
            ("office_front_fill", [0.0, -0.46, 0.62], [0.0, 0.04, 0.26], [0.34, 0.30], 1200.0, [0.84, 0.91, 1.0]),
        ]},
        "camera_exposure_ev": 0.35,
    },
    "living": {
        "surfaces": {"floor": ([0.33, 0.20, 0.105], 0.64), "walls": ([0.74, 0.69, 0.62], 0.8), "ceiling": ([0.82, 0.80, 0.75], 0.76)},
        "overlays": [
            ("floor_finish", "floor", [0.0, 0.0, 0.0005], [0.96, 0.95, 0.001], [0.31, 0.18, 0.09], 0.68),
            ("back_wall_lower_band", "back", [0.0, -0.002, 0.22], [0.96, 0.006, 0.10], [0.28, 0.16, 0.085], 0.62),
            ("left_wall_lower_band", "left", [0.002, 0.0, 0.22], [0.006, 0.96, 0.10], [0.28, 0.16, 0.085], 0.62),
            ("right_wall_lower_band", "right", [-0.002, 0.0, 0.22], [0.006, 0.96, 0.10], [0.28, 0.16, 0.085], 0.62),
        ],
    },
    "clinical": {
        "surfaces": {"floor": ([0.48, 0.56, 0.59], 0.56), "walls": ([0.80, 0.85, 0.86], 0.82), "ceiling": ([0.91, 0.93, 0.93], 0.84)},
        "overlays": [
            ("resilient_floor", "floor", [0.0, 0.0, 0.0005], [0.96, 0.95, 0.001], [0.42, 0.51, 0.55], 0.52),
            ("back_protection_rail", "back", [0.0, -0.003, 0.36], [0.94, 0.010, 0.045], [0.31, 0.48, 0.53], 0.45),
            ("left_protection_rail", "left", [0.003, 0.0, 0.36], [0.010, 0.92, 0.045], [0.31, 0.48, 0.53], 0.45),
            ("right_protection_rail", "right", [-0.003, 0.0, 0.36], [0.010, 0.92, 0.045], [0.31, 0.48, 0.53], 0.45),
        ],
        "lighting": {"environment": (680.0, [0.88, 0.94, 1.0]), "fixtures": [
            ("clinical_key", [-0.24, 0.04, 0.95], [-0.10, 0.02, 0.22], [0.27, 0.38], 2500.0, [0.88, 0.95, 1.0]),
            ("clinical_fill", [0.24, 0.04, 0.93], [0.12, 0.02, 0.22], [0.24, 0.34], 1450.0, [1.0, 0.91, 0.80]),
            ("clinical_front", [0.0, -0.46, 0.60], [0.0, 0.02, 0.24], [0.34, 0.28], 850.0, [0.90, 0.95, 1.0]),
        ]},
    },
    "warehouse": {
        "surfaces": {"floor": ([0.45, 0.47, 0.46], 0.42), "walls": ([0.70, 0.72, 0.71], 0.7), "ceiling": ([0.20, 0.20, 0.19], 0.7)},
        "overlays": [
            ("sealed_floor", "floor", [0.0, 0.0, 0.0005], [0.97, 0.96, 0.001], [0.39, 0.41, 0.40], 0.44),
            ("aisle_line_left", "floor", [-0.28, 0.0, 0.0015], [0.018, 0.90, 0.001], [0.92, 0.68, 0.08], 0.48),
            ("aisle_line_right", "floor", [0.28, 0.0, 0.0015], [0.018, 0.90, 0.001], [0.92, 0.68, 0.08], 0.48),
            ("packing_stop_line", "floor", [0.0, 0.27, 0.0015], [0.56, 0.018, 0.001], [0.92, 0.68, 0.08], 0.48),
        ],
    },
    "workshop": {
        "surfaces": {"floor": ([0.31, 0.32, 0.31], 0.58), "walls": ([0.64, 0.66, 0.65], 0.74), "ceiling": ([0.18, 0.18, 0.17], 0.72)},
        "surface_materials": {
            "floor": WAREHOUSE_CONCRETE_MATERIAL,
            "walls": WORKSHOP_WALL_MATERIAL,
        },
        "overlays": [
            ("service_floor", "floor", [0.0, 0.0, 0.0005], [0.97, 0.96, 0.001], [0.26, 0.27, 0.26], 0.60, WAREHOUSE_CONCRETE_MATERIAL),
            ("service_bay_left", "floor", [-0.24, 0.02, 0.0015], [0.022, 0.84, 0.001], [0.90, 0.65, 0.06], 0.52),
            ("service_bay_right", "floor", [0.24, 0.02, 0.0015], [0.022, 0.84, 0.001], [0.90, 0.65, 0.06], 0.52),
            ("back_safety_band", "back", [0.0, -0.003, 0.28], [0.94, 0.010, 0.055], [0.88, 0.62, 0.05], 0.50),
        ],
        "lighting": {"environment": (520.0, [0.86, 0.91, 1.0]), "fixtures": [
            ("workshop_key_left", [-0.25, 0.05, 0.94], [-0.14, 0.04, 0.16], [0.24, 0.44], 2800.0, [0.92, 0.96, 1.0]),
            ("workshop_key_right", [0.25, 0.05, 0.94], [0.14, 0.04, 0.16], [0.24, 0.44], 900.0, [0.92, 0.96, 1.0]),
            ("workshop_front_fill", [0.0, -0.46, 0.58], [0.0, 0.08, 0.24], [0.36, 0.30], 500.0, [1.0, 0.88, 0.70]),
        ]},
        "camera_exposure_ev": 0.10,
    },
    "transit": {
        "surfaces": {"floor": ([0.54, 0.56, 0.55], 0.34), "walls": ([0.70, 0.72, 0.71], 0.64), "ceiling": ([0.17, 0.18, 0.18], 0.70)},
        "overlays": [
            ("terrazzo_floor", "floor", [0.0, 0.0, 0.0005], [0.97, 0.96, 0.001], [0.48, 0.50, 0.49], 0.36, TRANSIT_PAVER_MATERIAL),
            ("tactile_gate_strip", "floor", [0.0, -0.08, 0.0015], [0.76, 0.045, 0.001], [0.88, 0.68, 0.08], 0.54),
            ("queue_guide_left", "floor", [-0.31, -0.20, 0.0015], [0.018, 0.44, 0.001], [0.10, 0.34, 0.62], 0.46),
            ("queue_guide_right", "floor", [0.31, -0.20, 0.0015], [0.018, 0.44, 0.001], [0.10, 0.34, 0.62], 0.46),
            ("wayfinding_band", "back", [0.0, -0.003, 0.64], [0.90, 0.012, 0.13], [0.06, 0.25, 0.50], 0.40),
        ],
        "lighting": {"environment": (1050.0, [0.88, 0.93, 1.0]), "fixtures": [
            ("concourse_key_left", [-0.23, 0.02, 0.93], [-0.10, 0.08, 0.16], [0.16, 0.72], 2700.0, [0.94, 0.97, 1.0]),
            ("concourse_key_right", [0.23, 0.02, 0.93], [0.10, 0.08, 0.16], [0.16, 0.72], 2700.0, [0.94, 0.97, 1.0]),
            ("concourse_front_fill", [0.0, -0.46, 0.56], [0.0, 0.08, 0.24], [0.38, 0.30], 1250.0, [1.0, 0.90, 0.76]),
        ]},
        "camera_exposure_ev": 0.20,
    },
    "laboratory": {
        "surfaces": {"floor": ([0.24, 0.30, 0.34], 0.5), "walls": ([0.72, 0.79, 0.80], 0.82), "ceiling": ([0.88, 0.91, 0.91], 0.82)},
        "overlays": [
            ("chemical_floor", "floor", [0.0, 0.0, 0.0005], [0.96, 0.95, 0.001], [0.20, 0.27, 0.31], 0.48),
            ("back_backsplash", "back", [0.0, -0.004, 0.34], [0.90, 0.012, 0.22], [0.18, 0.42, 0.45], 0.34),
            ("left_safety_band", "left", [0.004, 0.0, 0.42], [0.012, 0.88, 0.035], [0.88, 0.63, 0.10], 0.42),
            ("right_safety_band", "right", [-0.004, 0.0, 0.42], [0.012, 0.88, 0.035], [0.88, 0.63, 0.10], 0.42),
        ],
        "lighting": {"environment": (560.0, [0.84, 0.91, 1.0]), "fixtures": [
            ("lab_cool_key", [-0.25, 0.05, 0.95], [-0.12, 0.02, 0.20], [0.25, 0.38], 2800.0, [0.80, 0.91, 1.0]),
            ("lab_warm_fill", [0.25, 0.05, 0.92], [0.12, 0.02, 0.22], [0.22, 0.34], 1750.0, [1.0, 0.86, 0.70]),
            ("lab_front_fill", [0.0, -0.46, 0.58], [0.0, 0.02, 0.25], [0.32, 0.28], 950.0, [0.88, 0.94, 1.0]),
        ]},
    },
}


def _profile_id(spec: dict[str, Any]) -> str:
    text = f"{spec.get('domain', '')} {(spec.get('room') or {}).get('type', '')}".lower()
    if "office" in text or "meeting" in text:
        return "office"
    if "living" in text or "lounge" in text:
        return "living"
    if "clinic" in text or "medical" in text or "consult" in text or "ward" in text:
        return "clinical"
    if "warehouse" in text or "logistic" in text:
        return "warehouse"
    if "workshop" in text or "industrial_assembly" in text:
        return "workshop"
    if "transit" in text or "subway" in text or "metro" in text or "station" in text:
        return "transit"
    if "laborat" in text or " lab" in f" {text}":
        return "laboratory"
    raise ValueError(f"No appearance profile for domain/room: {text.strip()}")


def _digest(rows: list[dict[str, Any]], keys: tuple[str, ...]) -> str:
    payload = [{"id": row.get("id"), **{key: row.get(key) for key in keys}} for row in rows]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _point(room: dict[str, Any], values: list[float]) -> list[float]:
    size = [float(value) for value in room["size_m"]]
    center = [float(value) for value in (room.get("center_m") or [0, 0, 0])]
    return [center[index] + float(values[index]) * size[index] for index in range(3)]


def _overlay(room: dict[str, Any], row: tuple[Any, ...]) -> dict[str, Any]:
    if len(row) not in {6, 7}:
        raise ValueError("Appearance overlay rows require 6 fields plus an optional material asset")
    name, plane, translate_norm, size_norm, color, roughness = row[:6]
    material_asset = deepcopy(row[6]) if len(row) == 7 else None
    translate = _point(room, list(translate_norm))
    size = [float(size_norm[index]) * float(room["size_m"][index]) for index in range(3)]
    if plane == "back":
        translate[1] += float(room["size_m"][1]) * 0.5
    elif plane == "left":
        translate[0] -= float(room["size_m"][0]) * 0.5
    elif plane == "right":
        translate[0] += float(room["size_m"][0]) * 0.5
    result = {
        "id": name,
        "type": "box",
        "prim": f"/World/Architecture/Appearance/{''.join(part.title() for part in name.split('_'))}",
        "translate": translate,
        "size": size,
        "display_color": list(color),
        "material_roughness": float(roughness),
        "material_metallic": 0.0,
        "physics": "none",
        "semantic_role": "appearance_finish",
    }
    if material_asset:
        result["material_asset"] = material_asset
    return result


def _lighting(room: dict[str, Any], raw: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    intensity, color = raw["environment"]
    policy = {"mode": "geometry_frozen_appearance", "environment": {"enabled": True, "intensity": intensity, "color": color}, "fixtures": []}
    lights = [{"type": "dome", "prim": "/World/Lights/Dome", "intensity": intensity, "color": color}]
    for name, translate_norm, look_at_norm, dimensions, fixture_intensity, fixture_color in raw["fixtures"]:
        fixture = {
            "id": name, "type": "rect", "translate": _point(room, translate_norm), "look_at": _point(room, look_at_norm),
            "width": float(dimensions[0]) * float(room["size_m"][0]), "height": float(dimensions[1]) * float(room["size_m"][1]),
            "intensity": fixture_intensity, "color": fixture_color,
        }
        policy["fixtures"].append(deepcopy(fixture))
        lights.append({**fixture, "prim": f"/World/Lights/{''.join(part.title() for part in name.split('_'))}"})
    policy["evidence"] = "domain appearance profile; entity geometry frozen"
    return policy, lights


def apply_appearance(spec_path: str | Path, output_path: str | Path, *, profile: str = "auto") -> dict[str, Any]:
    source = json.loads(Path(spec_path).expanduser().read_text(encoding="utf-8"))
    result = deepcopy(source)
    profile_id = _profile_id(result) if profile == "auto" else profile
    if profile_id not in PROFILES:
        raise ValueError(f"Unknown appearance profile: {profile_id}")
    selected = PROFILES[profile_id]
    room = result.get("room") or {}
    if not isinstance(room.get("size_m"), list) or len(room["size_m"]) != 3:
        raise ValueError("Appearance pass requires room.size_m")
    entities = [row for row in result.get("entities") or [] if isinstance(row, dict)]
    shell = [row for row in result.get("support_colliders") or [] if isinstance(row, dict)]
    pose_before = _digest(entities, ("translate", "rotate", "scale", "matrix", "support_target"))
    shell_geometry_before = _digest(shell, ("translate", "rotate", "size", "scale"))
    surface_names = {"floor": "floor", "back_wall": "walls", "front_wall": "walls", "left_wall": "walls", "right_wall": "walls", "ceiling": "ceiling"}
    updated = []
    surface_material_ids = []
    for row in shell:
        surface = surface_names.get(str(row.get("id") or "").lower())
        if surface and surface in selected.get("surfaces", {}):
            color, roughness = selected["surfaces"][surface]
            row["display_color"] = list(color)
            row["material_roughness"] = float(roughness)
            row.setdefault("material_metallic", 0.0)
            material_asset = selected.get("surface_materials", {}).get(surface)
            if material_asset:
                row["material_asset"] = deepcopy(material_asset)
                surface_material_ids.append(str(row.get("id")))
            updated.append(str(row.get("id")))
    overlays = [_overlay(room, row) for row in selected.get("overlays", [])]
    result["appearance_overlays"] = overlays
    room["materials"] = {
        "profile": profile_id,
        "mode": "material_assets_plus_procedural_finish_layers",
        "surface_ids": updated,
        "overlay_ids": [row["id"] for row in overlays],
        "material_asset_ids": [row["id"] for row in overlays if row.get("material_asset")],
        "surface_material_asset_ids": surface_material_ids,
    }
    if selected.get("lighting"):
        result["lighting_policy"], result["lights"] = _lighting(room, selected["lighting"])
    if selected.get("camera_exposure_ev") is not None and isinstance(result.get("camera"), dict):
        result["camera"]["exposure_ev"] = float(selected["camera_exposure_ev"])
    result.setdefault("appearance_provenance", []).append({
        "schema_version": SCHEMA,
        "profile": profile_id,
        "pose_source": "none",
        "entity_transforms_frozen": True,
    })
    pose_after = _digest(entities, ("translate", "rotate", "scale", "matrix", "support_target"))
    shell_geometry_after = _digest(shell, ("translate", "rotate", "size", "scale"))
    if pose_before != pose_after or shell_geometry_before != shell_geometry_after:
        raise ValueError("Geometry-frozen appearance pass changed scene geometry")
    output = Path(output_path).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {
        "schema_version": SCHEMA,
        "status": "pass",
        "profile": profile_id,
        "output": str(output.resolve()),
        "entity_pose_digest": pose_after,
        "shell_geometry_digest": shell_geometry_after,
        "entity_transforms_frozen": True,
        "shell_geometry_frozen": True,
        "updated_shell_materials": updated,
        "appearance_overlay_count": len(overlays),
        "material_asset_count": sum(bool(row.get("material_asset")) for row in overlays),
        "surface_material_asset_count": len(surface_material_ids),
        "lighting_replaced": bool(selected.get("lighting")),
    }
