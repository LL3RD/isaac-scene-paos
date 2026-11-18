from __future__ import annotations

from typing import Any


def _weight(entity: dict[str, Any]) -> float:
    if entity.get("criticality") == "anchor":
        return 3.0
    if entity.get("generation_layer") == "baseline_required":
        return 2.0
    if entity.get("placement_policy") == "floor_underlay":
        return 0.25
    if entity.get("generation_layer") == "enrichment":
        return 0.75
    return 1.0


def summarize_instance_visibility(
    mask: Any,
    id_to_labels: dict[Any, Any],
    entities: list[dict[str, Any]],
    *,
    minimum_recognizable_pixels: int | None = None,
) -> dict[str, Any]:
    """Aggregate a raw Replicator instance mask into stable logical-entity evidence."""
    import numpy as np

    array = np.asarray(mask)
    if array.ndim != 2 or array.size == 0:
        raise ValueError("instance mask must be a non-empty HxW array")
    height, width = int(array.shape[0]), int(array.shape[1])
    total_pixels = int(array.size)
    threshold = int(minimum_recognizable_pixels or max(96, round(total_pixels * 0.0005)))
    labels = {int(key): value for key, value in (id_to_labels or {}).items()}
    path_to_ids: dict[str, list[int]] = {}
    special_ids: dict[str, list[int]] = {"BACKGROUND": [], "UNLABELLED": []}
    for raw_id, label in labels.items():
        if isinstance(label, dict):
            path = str(label.get("scene_instance") or label.get("class") or label)
        else:
            path = str(label)
        if path in special_ids:
            special_ids[path].append(raw_id)
        else:
            path_to_ids.setdefault(path, []).append(raw_id)

    rows = []
    weighted_recognizable = weighted_anchor_recognizable = 0.0
    total_weight = total_anchor_weight = 0.0
    entity_pixel_total = 0
    edge_anchor_count = 0
    for entity in entities:
        prim = str(entity.get("prim") or entity.get("source_prim_path") or "")
        logical_id = str(entity.get("id") or prim.rsplit("/", 1)[-1])
        matching_ids = list(path_to_ids.get(prim) or [])
        if not matching_ids:
            matching_ids = [raw_id for path, raw_ids in path_to_ids.items() if path == logical_id for raw_id in raw_ids]
        selected = np.isin(array, matching_ids) if matching_ids else np.zeros(array.shape, dtype=bool)
        ys, xs = np.nonzero(selected)
        visible_pixels = int(selected.sum())
        entity_pixel_total += visible_pixels
        frame_fraction = visible_pixels / total_pixels
        edges: list[str] = []
        bbox = centroid = None
        if visible_pixels:
            x0, x1, y0, y1 = int(xs.min()), int(xs.max()), int(ys.min()), int(ys.max())
            bbox = [x0, y0, x1, y1]
            if x0 == 0:
                edges.append("left")
            if x1 == width - 1:
                edges.append("right")
            if y0 == 0:
                edges.append("top")
            if y1 == height - 1:
                edges.append("bottom")
            centroid = [float(xs.mean() / max(width - 1, 1) * 2.0 - 1.0), float(ys.mean() / max(height - 1, 1) * 2.0 - 1.0)]
        recognizable = visible_pixels >= threshold
        weight = float(entity.get("weight") or _weight(entity))
        is_anchor = weight >= 2.0
        total_weight += weight
        weighted_recognizable += weight * float(recognizable)
        if is_anchor:
            total_anchor_weight += weight
            weighted_anchor_recognizable += weight * float(recognizable)
            if edges:
                edge_anchor_count += 1
        rows.append({
            "id": logical_id,
            "prim": prim,
            "weight": weight,
            "is_anchor": is_anchor,
            "mapping_status": "mapped_visible" if matching_ids else "not_visible_or_unmapped",
            "raw_instance_ids": matching_ids,
            "visible_pixels": visible_pixels,
            "frame_fraction": frame_fraction,
            "bbox_px": bbox,
            "centroid_ndc": centroid,
            "touches_frame_edges": edges,
            "recognizable": recognizable,
        })

    def special_fraction(name: str) -> float:
        raw_ids = special_ids.get(name) or []
        return float(np.isin(array, raw_ids).sum() / total_pixels) if raw_ids else 0.0

    dominant = max((row["frame_fraction"] for row in rows), default=0.0)
    entity_fraction = entity_pixel_total / total_pixels
    fill_score = max(0.0, 1.0 - abs(entity_fraction - 0.28) / 0.28)
    dominance_score = max(0.0, 1.0 - max(dominant - 0.18, 0.0) / 0.22)
    anchor_fraction = weighted_anchor_recognizable / total_anchor_weight if total_anchor_weight else 1.0
    entity_recognizable_fraction = weighted_recognizable / total_weight if total_weight else 1.0
    edge_score = 1.0 - edge_anchor_count / max(sum(row["is_anchor"] for row in rows), 1)
    pixel_utility = (
        0.40 * anchor_fraction
        + 0.20 * entity_recognizable_fraction
        + 0.15 * fill_score
        + 0.15 * dominance_score
        + 0.10 * edge_score
    )
    return {
        "schema_version": "isaac-scene-camera-pixel-visibility/v1",
        "status": "pass",
        "resolution": [width, height],
        "minimum_recognizable_pixels": threshold,
        "entity_count": len(rows),
        "recognizable_entity_count": sum(bool(row["recognizable"]) for row in rows),
        "recognizable_anchor_fraction": anchor_fraction,
        "recognizable_entity_fraction": entity_recognizable_fraction,
        "anchor_edge_crop_count": edge_anchor_count,
        "entity_pixel_fraction": entity_fraction,
        "background_pixel_fraction": special_fraction("BACKGROUND"),
        "unlabelled_pixel_fraction": special_fraction("UNLABELLED"),
        "dominant_entity_frame_fraction": dominant,
        "fill_score": fill_score,
        "dominance_score": dominance_score,
        "edge_score": edge_score,
        "pixel_utility_score": pixel_utility,
        "entities": rows,
    }

