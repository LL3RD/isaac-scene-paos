from __future__ import annotations

import json
import math
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from isaac_scene.image_geometry import inspect_content_frame


PROXY_MARKERS = ("proxy", "primitive", "fallback", "placeholder")
SHELL_MARKERS = ("shell", "background")
ANCHOR_TOKENS = {
    "appliance",
    "bed",
    "cabinet",
    "chair",
    "conveyor",
    "counter",
    "crane",
    "desk",
    "equipment",
    "fixture",
    "gondola",
    "hospital",
    "island",
    "machine",
    "operating",
    "platform screen door",
    "process vessel",
    "rack",
    "refrigerator",
    "robot",
    "shelf",
    "sink",
    "sofa",
    "stove",
    "table",
    "tank",
    "vehicle",
    "workbench",
    "workstation",
}


def _read_json(path: str | Path | None) -> dict[str, Any]:
    if not path:
        return {}
    candidate = Path(path)
    if not candidate.is_file():
        return {}
    value = json.loads(candidate.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else {}


def _entity_text(entity: dict[str, Any]) -> str:
    return " ".join(
        str(entity.get(key, ""))
        for key in ("id", "name", "role", "category", "semantic_name", "source", "path", "asset_path", "usd_path")
    ).lower()


def classify_entity(entity: dict[str, Any]) -> str:
    text = _entity_text(entity)
    if any(marker in text for marker in PROXY_MARKERS) or entity.get("proxy_for") or entity.get("proxy_for_batch"):
        return "proxy"
    path = entity.get("path") or entity.get("asset_path") or entity.get("usd_path")
    if path:
        return "retrieved"
    if any(marker in text for marker in SHELL_MARKERS):
        return "shell"
    return "authored"


def is_anchor_entity(entity: dict[str, Any]) -> bool:
    if str(entity.get("criticality", "")).lower() == "anchor":
        return True
    text = _entity_text(entity)
    return any(token in text for token in ANCHOR_TOKENS)


def asset_quality_from_spec(spec: dict[str, Any]) -> dict[str, Any]:
    entities = spec.get("entities")
    if not isinstance(entities, list):
        entities = spec.get("references", [])
    rows = [row for row in entities if isinstance(row, dict)]
    classes = Counter(classify_entity(row) for row in rows)
    retrieved_paths = [
        str(row.get("path") or row.get("asset_path") or row.get("usd_path"))
        for row in rows
        if classify_entity(row) == "retrieved"
    ]
    anchor_rows = [row for row in rows if is_anchor_entity(row)]
    anchor_real = [row for row in anchor_rows if classify_entity(row) == "retrieved"]
    non_shell_rows = [row for row in rows if classify_entity(row) != "shell"]
    total = len(rows)
    retrieved = classes["retrieved"]
    proxy = classes["proxy"]
    unique_assets = len(set(retrieved_paths))
    return {
        "entity_count": total,
        "retrieved_count": retrieved,
        "proxy_count": proxy,
        "authored_count": classes["authored"],
        "shell_count": classes["shell"],
        "non_shell_entity_count": len(non_shell_rows),
        "retrieved_coverage": retrieved / total if total else 0.0,
        "proxy_ratio": proxy / total if total else 0.0,
        "unique_asset_count": unique_assets,
        "asset_diversity": unique_assets / retrieved if retrieved else 0.0,
        "anchor_count": len(anchor_rows),
        "anchor_definition_complete": not non_shell_rows or bool(anchor_rows),
        "anchor_real_asset_count": len(anchor_real),
        "anchor_real_asset_coverage": len(anchor_real) / len(anchor_rows) if anchor_rows else 1.0,
        "retrieved_paths": retrieved_paths,
    }


def _load_rgb(
    path: str | Path,
    size: tuple[int, int] = (128, 128),
    content_bbox_px: list[int] | None = None,
) -> Any:
    import numpy as np
    from PIL import Image, ImageOps

    image = Image.open(path).convert("RGB")
    if content_bbox_px is not None:
        image = image.crop(tuple(int(value) for value in content_bbox_px))
    resampling = getattr(Image, "Resampling", Image)
    image = ImageOps.fit(image, size, method=resampling.LANCZOS)
    return np.asarray(image, dtype=np.float32) / 255.0


def _cosine_similarity(first: Any, second: Any) -> float:
    import numpy as np

    a = np.asarray(first, dtype=np.float32).reshape(-1)
    b = np.asarray(second, dtype=np.float32).reshape(-1)
    denominator = float(np.linalg.norm(a) * np.linalg.norm(b))
    if denominator <= 1e-12:
        return 1.0 if float(np.linalg.norm(a - b)) <= 1e-12 else 0.0
    return max(0.0, min(1.0, float(np.dot(a, b)) / denominator))


def _histogram_similarity(first: Any, second: Any, bins: int = 32) -> float:
    import numpy as np

    scores: list[float] = []
    for channel in range(3):
        hist_a, _ = np.histogram(first[..., channel], bins=bins, range=(0.0, 1.0), density=False)
        hist_b, _ = np.histogram(second[..., channel], bins=bins, range=(0.0, 1.0), density=False)
        hist_a = hist_a.astype(np.float64) / max(float(hist_a.sum()), 1.0)
        hist_b = hist_b.astype(np.float64) / max(float(hist_b.sum()), 1.0)
        scores.append(float(np.minimum(hist_a, hist_b).sum()))
    return sum(scores) / len(scores)


def _gradient_map(rgb: Any) -> Any:
    import numpy as np

    gray = 0.2126 * rgb[..., 0] + 0.7152 * rgb[..., 1] + 0.0722 * rgb[..., 2]
    dx = np.diff(gray, axis=1, append=gray[:, -1:])
    dy = np.diff(gray, axis=0, append=gray[-1:, :])
    gradient = np.sqrt(dx * dx + dy * dy)
    norm = float(np.linalg.norm(gradient))
    return gradient / norm if norm > 1e-12 else gradient


def _block_features(rgb: Any, blocks: int = 8) -> Any:
    import numpy as np

    height, width, _ = rgb.shape
    crop = rgb[: height - height % blocks, : width - width % blocks]
    feature = crop.reshape(blocks, crop.shape[0] // blocks, blocks, crop.shape[1] // blocks, 3)
    feature = feature.mean(axis=(1, 3))
    mean = feature.mean(axis=(0, 1), keepdims=True)
    std = feature.std(axis=(0, 1), keepdims=True)
    return (feature - mean) / np.maximum(std, 0.08)


def compare_images(reference_image: str | Path, rendered_image: str | Path) -> dict[str, Any]:
    reference_path = Path(reference_image)
    rendered_path = Path(rendered_image)
    missing = [str(path) for path in (reference_path, rendered_path) if not path.is_file()]
    if missing:
        return {
            "status": "fail",
            "score": 0.0,
            "missing": missing,
            "failures": [{"code": "REFERENCE_MATCH_INPUT_MISSING", "paths": missing}],
        }
    try:
        import numpy as np

        reference_frame = inspect_content_frame(reference_path)
        rendered_frame = inspect_content_frame(rendered_path)
        reference = _load_rgb(reference_path, content_bbox_px=reference_frame["content_bbox_px"])
        rendered = _load_rgb(rendered_path, content_bbox_px=rendered_frame["content_bbox_px"])
    except Exception as exc:
        return {
            "status": "fail",
            "score": 0.0,
            "failures": [{"code": "REFERENCE_MATCH_DEPENDENCY_OR_IMAGE_ERROR", "message": f"{type(exc).__name__}: {exc}"}],
        }

    color = _histogram_similarity(reference, rendered)
    structure = _cosine_similarity(_gradient_map(reference), _gradient_map(rendered))
    composition = _cosine_similarity(_block_features(reference), _block_features(rendered))
    ref_luma = float((0.2126 * reference[..., 0] + 0.7152 * reference[..., 1] + 0.0722 * reference[..., 2]).mean())
    out_luma = float((0.2126 * rendered[..., 0] + 0.7152 * rendered[..., 1] + 0.0722 * rendered[..., 2]).mean())
    luminance = max(0.0, 1.0 - abs(ref_luma - out_luma))
    reference_aspect = float(reference_frame["content_aspect_ratio"])
    rendered_aspect = float(rendered_frame["content_aspect_ratio"])
    aspect = min(reference_aspect, rendered_aspect) / max(reference_aspect, rendered_aspect)
    base_score = 0.30 * color + 0.30 * structure + 0.30 * composition + 0.10 * luminance
    score = base_score * (0.85 + 0.15 * aspect)
    status = "pass" if score >= 0.72 else ("warn" if score >= 0.50 else "fail")
    return {
        "status": status,
        "score": round(float(score), 6),
        "metrics": {
            "color_histogram_similarity": round(float(color), 6),
            "edge_structure_similarity": round(float(structure), 6),
            "spatial_composition_similarity": round(float(composition), 6),
            "luminance_similarity": round(float(luminance), 6),
            "reference_mean_luminance": round(ref_luma, 6),
            "rendered_mean_luminance": round(out_luma, 6),
            "content_aspect_ratio_similarity": round(float(aspect), 6),
        },
        "reference_frame": reference_frame,
        "rendered_frame": rendered_frame,
        "thresholds": {"pass": 0.72, "warn": 0.50},
        "limitations": [
            "This deterministic score measures palette, edges, coarse composition and luminance.",
            "Uniform letterbox/pillarbox is removed before comparison and content aspect ratio is scored separately.",
            "Object identity and semantic style require the optional visual-embedding/VLM evaluation stage.",
        ],
    }


def evaluate_scene(
    *,
    reference_image: str | Path,
    rendered_image: str | Path,
    scene_spec: str | Path | None = None,
    summary: str | Path | None = None,
) -> dict[str, Any]:
    asset = asset_quality_from_spec(_read_json(scene_spec))
    image = compare_images(reference_image, rendered_image)
    summary_data = _read_json(summary)
    geometry_pass = bool(summary_data.get("validation_pass", False))
    facing_pass = bool(summary_data.get("facing_pass", False))
    lighting_pass = bool(summary_data.get("render_feedback_pass", summary_data.get("pass", False)))

    failures: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    if not asset["anchor_definition_complete"]:
        failures.append({
            "code": "ANCHOR_DEFINITION_MISSING",
            "message": "Reference-match scenes with visible non-shell entities must declare or expose at least one anchor.",
        })
    if asset["anchor_real_asset_coverage"] < 1.0:
        failures.append({
            "code": "ANCHOR_PROXY_OR_AUTHORED_FALLBACK",
            "value": asset["anchor_real_asset_coverage"],
            "message": "Visible anchor objects must use verified real assets in reference-match mode.",
        })
    if asset["proxy_ratio"] > 0.0:
        failures.append({
            "code": "VISIBLE_PROXY_ASSET_PRESENT",
            "value": asset["proxy_ratio"],
            "message": "Reference-match outputs cannot pass while visible object proxies remain.",
        })
    if asset["retrieved_count"] > 1 and asset["asset_diversity"] < 0.5:
        warnings.append({"code": "LOW_ASSET_DIVERSITY", "value": asset["asset_diversity"]})
    if not geometry_pass:
        failures.append({"code": "GEOMETRY_VALIDATION_NOT_PASS"})
    if not facing_pass:
        failures.append({"code": "FACING_VALIDATION_NOT_PASS"})
    if not lighting_pass:
        failures.append({"code": "LIGHTING_VALIDATION_NOT_PASS"})
    if image.get("status") == "fail":
        image_failures = image.get("failures") or []
        infrastructure_codes = {
            "REFERENCE_MATCH_INPUT_MISSING",
            "REFERENCE_MATCH_DEPENDENCY_OR_IMAGE_ERROR",
        }
        propagated = [
            failure for failure in image_failures
            if failure.get("code") in infrastructure_codes
        ]
        if propagated:
            failures.extend(propagated)
        else:
            failures.append({"code": "REFERENCE_MATCH_LOW", "value": image.get("score", 0.0)})
    elif image.get("status") == "warn":
        warnings.append({"code": "REFERENCE_MATCH_NEEDS_IMPROVEMENT", "value": image.get("score", 0.0)})

    status = "fail" if failures else ("warn" if warnings else "pass")
    return {
        "schema_version": "isaac-scene-quality/v1",
        "status": status,
        "gates": {
            "geometry_pass": geometry_pass,
            "facing_pass": facing_pass,
            "lighting_pass": lighting_pass,
            "anchor_definition_pass": asset["anchor_definition_complete"],
            "anchor_real_asset_pass": (
                asset["anchor_definition_complete"]
                and asset["anchor_real_asset_coverage"] >= 1.0
            ),
            "proxy_free_pass": asset["proxy_ratio"] == 0.0,
            "reference_match_pass": image.get("status") == "pass",
        },
        "asset_quality": asset,
        "reference_match": image,
        "failures": failures,
        "warnings": warnings,
    }


def benchmark_reuse_metrics(scene_results: Iterable[dict[str, Any]]) -> dict[str, Any]:
    paths: list[str] = []
    for result in scene_results:
        paths.extend(result.get("asset_quality", {}).get("retrieved_paths", []))
    counts = Counter(paths)
    repeated_instances = sum(count - 1 for count in counts.values() if count > 1)
    return {
        "retrieved_instances": len(paths),
        "unique_retrieved_assets": len(counts),
        "cross_scene_repeated_instances": repeated_instances,
        "cross_scene_reuse_ratio": repeated_instances / len(paths) if paths else 0.0,
        "most_reused_assets": [
            {"path": path, "count": count}
            for path, count in counts.most_common(10)
            if count > 1
        ],
    }
