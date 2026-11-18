"""Pose-free realism gap audit against matched Synthesis scene kits."""

from __future__ import annotations

import json
import math
from collections import Counter
from pathlib import Path
from typing import Any


SCHEMA = "isaac-scene-synthesis-gap-audit/v1"
DEFAULT_MATCH_COUNT = 2


def _read(path: str | Path) -> dict[str, Any]:
    payload = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return payload


def _class_prefix(value: Any) -> str:
    parts = [part for part in str(value or "object/other").split("/") if part]
    if not parts:
        return "object/other"
    # The generated catalog historically used ``laboratory/glassware`` for
    # beakers and reagent bottles, while the Synthesis library calls the same
    # semantic family ``laboratory/labware``.  Normalize the vocabulary before
    # measuring coverage so the audit reports a real inventory gap rather than
    # a taxonomy mismatch.
    if parts[:2] == ["laboratory", "glassware"]:
        return "laboratory/labware"
    if tuple(parts[:2]) in {
        ("office", "notebook"),
        ("office", "document"),
        ("household", "book"),
    }:
        return "small_prop"
    if parts[0] in {"medical", "laboratory", "industrial", "logistics", "warehouse", "electronics"}:
        return "/".join(parts[:2]) if len(parts) > 1 else parts[0]
    return "/".join(parts[:2]) if len(parts) > 1 else parts[0]


def _layer_for_class(canonical_class: Any) -> str:
    value = str(canonical_class or "object/other")
    if value.startswith("architecture/"):
        return "architecture"
    if value.startswith(("decor/", "household/", "office/", "small_prop", "food/", "kitchenware/")):
        return "detail"
    if value.startswith(("furniture/", "medical/", "laboratory/", "industrial/", "logistics/", "warehouse/", "robot/", "transport/")):
        return "anchor"
    return "functional"


def _normalize_count(count: int, room_area_m2: float, *, cap: int = 80) -> float:
    # Saturation prevents a warehouse with 1,194 cartons from becoming a literal target.
    density = max(0.0, float(count)) / max(8.0, float(room_area_m2))
    return math.log1p(min(int(count), cap)) * (0.65 + 0.35 * min(1.0, density * 8.0))


def _scene_kit_rows(catalog: dict[str, Any]) -> list[dict[str, Any]]:
    rows = catalog.get("scenes") or catalog.get("scene_kits") or catalog.get("kits") or []
    return [row for row in rows if isinstance(row, dict)]


def _tokens(*values: Any) -> set[str]:
    out: set[str] = set()
    for value in values:
        text = str(value or "").lower().replace("_", " ")
        out.update(part for part in text.split() if part)
    return out


def _normalized_label(value: Any) -> str:
    return " ".join(str(value or "").lower().replace("_", " ").split())


def _match_score(spec: dict[str, Any], kit: dict[str, Any]) -> float:
    room = spec.get("room") or {}
    domain = str(spec.get("domain") or "")
    room_label = _normalized_label(room.get("type"))
    primary_labels = {
        _normalized_label(kit.get("slug")),
        _normalized_label(kit.get("title")),
        *(_normalized_label(value) for value in kit.get("room_types") or []),
    }
    primary_tokens = _tokens(kit.get("slug"), kit.get("title"), *(kit.get("room_types") or []))
    room_tokens = _tokens(room.get("type"))
    score = 4.0 if domain and domain == str(kit.get("domain") or "") else 0.0
    if room_label and room_label in primary_labels:
        score += 12.0
    elif room_tokens and room_tokens <= primary_tokens:
        score += 8.0
    elif room_tokens & primary_tokens:
        score += 5.0 * len(room_tokens & primary_tokens) / len(room_tokens)
    # Aliases help recall, but never outrank a primary room-name match.
    alias_tokens = _tokens(*(kit.get("aliases") or []))
    score += 1.5 * len(room_tokens & alias_tokens) / max(1, len(room_tokens))
    return score


def _composition(rows: list[dict[str, Any]], *, count_key: str = "instance_count") -> tuple[Counter[str], Counter[str]]:
    classes: Counter[str] = Counter()
    layers: Counter[str] = Counter()
    for row in rows:
        cls = str(row.get("canonical_class") or "object/other")
        count = int(row.get(count_key) or 1)
        classes[_class_prefix(cls)] += count
        layers[_layer_for_class(cls)] += count
    return classes, layers


def audit_scene(
    spec_path: str | Path,
    catalog_path: str | Path,
    output_path: str | Path,
    *,
    match_count: int = DEFAULT_MATCH_COUNT,
) -> dict[str, Any]:
    spec = _read(spec_path)
    catalog = _read(catalog_path)
    room = spec.get("room") or {}
    size = room.get("usable_bounds_m", {}).get("size") or room.get("size_m") or [6.0, 5.0, 3.0]
    room_area = max(1.0, float(size[0]) * float(size[1]))
    entities = [row for row in spec.get("entities") or [] if isinstance(row, dict) and row.get("asset_scope") not in {"scene_root", "scene_layout_component", "external_feature"}]
    generated_classes, generated_layers = _composition(entities, count_key="__one__")
    # The generated shell lives under `room`, not in the independently retrieved entity list.
    generated_classes["architecture/room_shell"] += 1
    generated_layers["architecture"] += 1

    ranked = sorted(
        ((_match_score(spec, kit), kit) for kit in _scene_kit_rows(catalog)),
        key=lambda pair: (-pair[0], str(pair[1].get("slug") or "")),
    )
    matched = [kit for score, kit in ranked if score > 0][:max(1, int(match_count))]
    if not matched:
        raise ValueError("No Synthesis scene kit matches the scene domain/room type")

    reference_classes: Counter[str] = Counter()
    reference_layers: Counter[str] = Counter()
    reference_counts = []
    complexity = []
    for kit in matched:
        classes, layers = _composition(kit.get("semantic_composition") or [])
        reference_classes.update(classes)
        reference_layers.update(layers)
        metrics = kit.get("metrics") or {}
        reference_counts.append(int(metrics.get("instance_count") or 0))
        complexity.append({
            "slug": kit.get("slug"),
            "prim_count": int(metrics.get("prim_count") or 0),
            "mesh_count": int(metrics.get("mesh_count") or 0),
            "used_layer_count": int(metrics.get("used_layer_count") or 0),
        })
    divisor = float(len(matched))
    reference_classes = Counter({key: value / divisor for key, value in reference_classes.items()})
    reference_layers = Counter({key: value / divisor for key, value in reference_layers.items()})

    class_gaps = []
    for cls in sorted(set(reference_classes) | set(generated_classes)):
        ref = float(reference_classes.get(cls, 0.0))
        have = float(generated_classes.get(cls, 0.0))
        if ref <= 0.0:
            continue
        ratio = min(1.0, _normalize_count(int(have), room_area) / max(1e-6, _normalize_count(int(round(ref)), room_area)))
        class_gaps.append({
            "canonical_class_prefix": cls,
            "generated_count": int(have),
            "synthesis_reference_mean_count": round(ref, 3),
            "saturation_coverage": round(ratio, 4),
            "missing_priority": round((1.0 - ratio) * math.log1p(ref), 4),
        })
    class_gaps.sort(key=lambda row: (-row["missing_priority"], row["canonical_class_prefix"]))

    layer_coverage = {}
    for layer in ("anchor", "functional", "detail", "architecture"):
        ref = float(reference_layers.get(layer, 0.0))
        have = float(generated_layers.get(layer, 0.0))
        layer_coverage[layer] = {
            "generated_count": int(have),
            "synthesis_reference_mean_count": round(ref, 3),
            "coverage": round(min(1.0, _normalize_count(int(have), room_area) / max(1e-6, _normalize_count(int(round(ref)), room_area))), 4) if ref else 1.0,
        }

    lights = spec.get("lights") or []
    shell_rows = [
        row for row in spec.get("support_colliders") or []
        if isinstance(row, dict)
        and str(row.get("id") or "").lower() in {"floor", "back_wall", "front_wall", "left_wall", "right_wall", "ceiling"}
    ]
    authored_shell_materials = sum(
        1 for row in shell_rows
        if row.get("display_color") is not None
        and any(row.get(key) is not None for key in ("material_roughness", "material_metallic", "material_opacity"))
    )
    overlay_rows = [row for row in spec.get("appearance_overlays") or [] if isinstance(row, dict)]
    textured_shell_materials = sum(
        1 for row in [*shell_rows, *overlay_rows]
        if any(row.get(key) for key in ("material_path", "material_asset", "texture", "texture_path"))
    )
    overlay_count = len(overlay_rows)
    appearance_signals = {
        "explicit_lighting_policy": bool(spec.get("lighting_policy")),
        "light_count": len(lights),
        "camera_exposure_authored": (spec.get("camera") or {}).get("exposure_ev") is not None,
        "room_material_layers_authored": bool(room.get("materials") or spec.get("materials") or authored_shell_materials >= 2),
        "authored_shell_material_count": authored_shell_materials,
        "texture_backed_shell_material_count": textured_shell_materials,
        "appearance_overlay_count": overlay_count,
        "source_diversity": len({str(row.get("source_library") or row.get("asset_source") or "procedural") for row in entities}),
    }
    appearance_score = (
        0.30 * float(appearance_signals["explicit_lighting_policy"])
        + 0.20 * float(appearance_signals["light_count"] >= 3)
        + 0.15 * float(appearance_signals["camera_exposure_authored"])
        + 0.15 * float(appearance_signals["room_material_layers_authored"])
        + 0.05 * float(overlay_count >= 4)
        + 0.05 * float(textured_shell_materials > 0)
        + 0.10 * min(1.0, appearance_signals["source_diversity"] / 3.0)
    )
    instance_target = sum(reference_counts) / divisor
    density_coverage = min(1.0, _normalize_count(len(entities), room_area) / max(1e-6, _normalize_count(int(round(instance_target)), room_area)))
    semantic_coverage = sum(row["saturation_coverage"] for row in class_gaps) / max(1, len(class_gaps))
    hierarchy_coverage = sum(float(row["coverage"]) for row in layer_coverage.values()) / 4.0
    overall = 0.30 * density_coverage + 0.30 * semantic_coverage + 0.25 * hierarchy_coverage + 0.15 * appearance_score

    repairs = []
    repeated_inventory_gap = any(
        row["synthesis_reference_mean_count"] >= 12.0
        and row["saturation_coverage"] < 0.75
        for row in class_gaps
    )
    if layer_coverage["detail"]["coverage"] < 0.75 or repeated_inventory_gap:
        repairs.append({
            "priority": "high",
            "kind": "detail_density",
            "action": (
                "Add support-aware repeated micro-props or inventory in bounded clusters; "
                "represent extreme repetition with batches and never chase raw warehouse counts."
            ),
        })
    if layer_coverage["anchor"]["coverage"] < 0.75:
        repairs.append({"priority": "high", "kind": "functional_anchor", "action": "Add or replace one coherent medium/large functional anchor before adding more tabletop clutter."})
    if not appearance_signals["room_material_layers_authored"]:
        repairs.append({"priority": "high", "kind": "material_layering", "action": "Author room-level finish layers and verify them with a geometry-frozen fixed-camera render."})
    elif textured_shell_materials == 0:
        repairs.append({"priority": "high", "kind": "material_fidelity", "action": "Add bounded texture-backed or procedural finish variation while freezing every entity transform."})
    if not appearance_signals["explicit_lighting_policy"] or appearance_signals["light_count"] < 3:
        repairs.append({"priority": "high", "kind": "lighting", "action": "Add a domain-coherent environment/key/fill policy before changing camera exposure."})
    for row in class_gaps[:4]:
        if row["saturation_coverage"] < 0.7:
            repairs.append({"priority": "medium", "kind": "semantic_class_gap", "canonical_class_prefix": row["canonical_class_prefix"], "action": "Retrieve same-kit candidates first, then fall back to the unified semantic catalog."})

    result = {
        "schema_version": SCHEMA,
        "status": "pass",
        "pose_source": "none",
        "source_policy": {
            "allowed": ["semantic_composition", "density", "asset_family", "environment_complexity"],
            "forbidden": ["source transforms", "source matrices", "source camera", "complete scene insertion"],
        },
        "scene": {
            "id": spec.get("scene_id") or spec.get("id") or Path(spec_path).parent.name,
            "domain": spec.get("domain"),
            "room_type": room.get("type"),
            "room_area_m2": round(room_area, 3),
            "entity_count": len(entities),
        },
        "matched_synthesis_scene_kits": [
            {"slug": kit.get("slug"), "domain": kit.get("domain"), "instance_count": (kit.get("metrics") or {}).get("instance_count"), "unique_model_count": (kit.get("metrics") or {}).get("unique_model_count")}
            for kit in matched
        ],
        "scores": {
            "density_coverage": round(density_coverage, 4),
            "semantic_coverage": round(semantic_coverage, 4),
            "hierarchy_coverage": round(hierarchy_coverage, 4),
            "appearance_readiness": round(appearance_score, 4),
            "overall_gap_coverage": round(overall, 4),
        },
        "layer_coverage": layer_coverage,
        "class_gaps": class_gaps,
        "appearance_signals": appearance_signals,
        "reference_environment_complexity": complexity,
        "repair_plan": repairs,
    }
    output = Path(output_path).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    result["output"] = str(output.resolve())
    return result
