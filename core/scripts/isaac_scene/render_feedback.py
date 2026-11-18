from __future__ import annotations

import math
from pathlib import Path
from typing import Any


VIEW_THRESHOLDS: dict[str, dict[str, float]] = {
    "perspective": {
        "min_mean_luminance": 0.20,
        "target_mean_luminance": 0.32,
        "max_mean_luminance": 0.75,
        "max_shadow_fraction": 0.35,
        "min_subject_mean_luminance": 0.16,
        "max_subject_shadow_fraction": 0.40,
        "max_highlight_clip_fraction": 0.03,
        "min_contrast_span": 0.30,
    },
    "top_down": {
        "min_mean_luminance": 0.15,
        "target_mean_luminance": 0.24,
        "max_mean_luminance": 0.78,
        "max_shadow_fraction": 0.25,
        "max_highlight_clip_fraction": 0.05,
        "min_contrast_span": 0.24,
    },
    "asset_preview": {
        "min_mean_luminance": 0.18,
        "target_mean_luminance": 0.30,
        "max_mean_luminance": 0.78,
        "max_shadow_fraction": 0.30,
        "max_highlight_clip_fraction": 0.04,
        "min_contrast_span": 0.25,
    },
}


def assess_metrics(
    metrics: dict[str, Any],
    *,
    view_kind: str = "perspective",
    threshold_overrides: dict[str, float] | None = None,
) -> dict[str, Any]:
    if view_kind not in VIEW_THRESHOLDS:
        raise ValueError(f"view_kind must be one of {sorted(VIEW_THRESHOLDS)}")
    thresholds = {**VIEW_THRESHOLDS[view_kind], **(threshold_overrides or {})}
    mean = float(metrics["mean_luminance"])
    median = float(metrics["median_luminance"])
    p05 = float(metrics["p05_luminance"])
    p95 = float(metrics["p95_luminance"])
    shadow_fraction = float(metrics["shadow_fraction"])
    highlight_fraction = float(metrics["highlight_clip_fraction"])
    contrast_span = p95 - p05
    channel_means = [float(value) for value in metrics.get("channel_means", [mean, mean, mean])]
    channel_spread = max(channel_means) - min(channel_means)
    subject_mean = metrics.get("subject_region_mean_luminance")
    subject_shadow_fraction = metrics.get("subject_region_shadow_fraction")

    failures: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    if mean < 0.01 or p95 < 0.02:
        failures.append({
            "code": "BLACK_OR_EMPTY_RENDER",
            "summary": "The render is nearly black or contains no useful visible range.",
        })
    else:
        if mean < thresholds["min_mean_luminance"] or shadow_fraction > thresholds["max_shadow_fraction"]:
            failures.append({
                "code": "UNDEREXPOSED_RENDER",
                "summary": "The view is too dark for reliable scene review.",
                "details": {
                    "mean_luminance": mean,
                    "shadow_fraction": shadow_fraction,
                },
            })
        if (
            view_kind == "perspective"
            and subject_mean is not None
            and subject_shadow_fraction is not None
            and (
                float(subject_mean) < thresholds["min_subject_mean_luminance"]
                or float(subject_shadow_fraction) > thresholds["max_subject_shadow_fraction"]
            )
        ):
            failures.append({
                "code": "SUBJECT_REGION_UNDEREXPOSED",
                "summary": "The lower subject region is too dark even though the global image average may pass.",
                "details": {
                    "subject_region_mean_luminance": float(subject_mean),
                    "subject_region_shadow_fraction": float(subject_shadow_fraction),
                },
            })
        if mean > thresholds["max_mean_luminance"]:
            failures.append({
                "code": "OVEREXPOSED_RENDER",
                "summary": "The view is globally overexposed.",
                "details": {"mean_luminance": mean},
            })
        if highlight_fraction > thresholds["max_highlight_clip_fraction"]:
            failures.append({
                "code": "HIGHLIGHT_CLIPPING",
                "summary": "Too much of the image is clipped near white.",
                "details": {"highlight_clip_fraction": highlight_fraction},
            })
    if contrast_span < thresholds["min_contrast_span"]:
        warnings.append({
            "code": "LOW_CONTRAST_RENDER",
            "summary": "The visible tonal range is too narrow.",
            "details": {"p05_to_p95_span": contrast_span},
        })
    if channel_spread > 0.15:
        warnings.append({
            "code": "STRONG_COLOR_CAST",
            "summary": "Average RGB channels indicate a strong global color cast.",
            "details": {"channel_means": channel_means, "channel_spread": channel_spread},
        })

    target_mean = thresholds["target_mean_luminance"]
    exposure_delta = max(-2.0, min(2.0, math.log2(target_mean / max(mean, 0.01))))
    recommended_adjustments: dict[str, Any] = {
        "exposure_delta_ev": round(exposure_delta, 2),
        "preserve_geometry": True,
        "max_feedback_iterations": 2,
    }
    if any(item["code"] in {
        "UNDEREXPOSED_RENDER",
        "SUBJECT_REGION_UNDEREXPOSED",
        "BLACK_OR_EMPTY_RENDER",
    } for item in failures):
        recommended_adjustments.update({
            "ambient_intensity_multiplier": round(max(1.25, min(2.5, 2.0 ** (max(exposure_delta, 0.0) * 0.55))), 2),
            "add_or_strengthen_fill_light": True,
            "note": "Raise ambient/fill before increasing only the key light; keep the key/fill ratio readable.",
        })
    if any(item["code"] == "SUBJECT_REGION_UNDEREXPOSED" for item in failures):
        recommended_adjustments["subject_fill_required"] = True
    elif any(item["code"] in {"OVEREXPOSED_RENDER", "HIGHLIGHT_CLIPPING"} for item in failures):
        recommended_adjustments.update({
            "key_intensity_multiplier": round(max(0.4, min(0.9, 2.0 ** min(exposure_delta, 0.0))), 2),
            "reduce_exposure_or_key": True,
        })
    if contrast_span < thresholds["min_contrast_span"]:
        recommended_adjustments["increase_key_fill_separation"] = True

    status = "fail" if failures else ("warn" if warnings else "pass")
    return {
        "status": status,
        "view_kind": view_kind,
        "metrics": {
            **metrics,
            "contrast_span_p05_p95": contrast_span,
            "channel_spread": channel_spread,
        },
        "thresholds": thresholds,
        "failures": failures,
        "warnings": warnings,
        "recommended_adjustments": recommended_adjustments,
    }


def analyze_image(
    image_path: str,
    *,
    view_kind: str = "perspective",
    threshold_overrides: dict[str, float] | None = None,
) -> dict[str, Any]:
    path = Path(image_path)
    if not path.is_file():
        return {
            "status": "fail",
            "image_path": str(path),
            "failures": [{"code": "RENDER_NOT_FOUND", "summary": "Render image does not exist."}],
            "warnings": [],
        }
    try:
        import numpy as np
        from PIL import Image
    except Exception as exc:  # pragma: no cover - runtime dependency check
        return {
            "status": "fail",
            "image_path": str(path),
            "failures": [{
                "code": "IMAGE_ANALYSIS_DEPENDENCY_MISSING",
                "summary": f"{type(exc).__name__}: {exc}",
            }],
            "warnings": [],
        }
    pixels = np.asarray(Image.open(path).convert("RGB"), dtype=np.float32) / 255.0
    luminance = (
        0.2126 * pixels[..., 0]
        + 0.7152 * pixels[..., 1]
        + 0.0722 * pixels[..., 2]
    )
    height, width = luminance.shape
    if view_kind == "perspective":
        # Indoor anchors and circulation usually occupy the lower half. Keep
        # this separate from the full-image mean so a bright wall or ceiling
        # cannot hide unreadable furniture and robots.
        subject_region = luminance[height // 2 :, :]
    elif view_kind == "asset_preview":
        subject_region = luminance[
            int(height * 0.1) : max(int(height * 0.9), 1),
            int(width * 0.1) : max(int(width * 0.9), 1),
        ]
    else:
        subject_region = luminance
    metrics = {
        "width": int(pixels.shape[1]),
        "height": int(pixels.shape[0]),
        "mean_luminance": float(luminance.mean()),
        "median_luminance": float(np.median(luminance)),
        "p05_luminance": float(np.percentile(luminance, 5)),
        "p95_luminance": float(np.percentile(luminance, 95)),
        "shadow_fraction": float((luminance < 0.08).mean()),
        "highlight_clip_fraction": float((luminance > 0.98).mean()),
        "channel_means": [float(value) for value in pixels.mean(axis=(0, 1))],
        "subject_region": (
            "lower_half"
            if view_kind == "perspective"
            else "center_80pct"
            if view_kind == "asset_preview"
            else "full_frame"
        ),
        "subject_region_mean_luminance": float(subject_region.mean()),
        "subject_region_shadow_fraction": float((subject_region < 0.08).mean()),
    }
    result = assess_metrics(
        metrics,
        view_kind=view_kind,
        threshold_overrides=threshold_overrides,
    )
    result["image_path"] = str(path)
    return result
