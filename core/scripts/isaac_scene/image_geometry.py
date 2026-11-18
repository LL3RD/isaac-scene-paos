from __future__ import annotations

from pathlib import Path
from typing import Any


def inspect_content_frame(
    image_path: str | Path,
    *,
    color_tolerance: float = 0.06,
    minimum_active_fraction: float = 0.03,
) -> dict[str, Any]:
    """Detect a uniform letterbox/pillarbox around the actual image content."""
    import numpy as np
    from PIL import Image

    path = Path(image_path)
    with Image.open(path) as image:
        rgb = np.asarray(image.convert("RGB"), dtype=np.float32) / 255.0
    height, width, _ = rgb.shape
    row_mean = rgb.mean(axis=1)
    row_spread = rgb.std(axis=(1, 2))
    row_neutral = (row_mean.max(axis=1) - row_mean.min(axis=1)) <= 0.05
    row_extreme = (row_mean.mean(axis=1) >= 0.72) | (row_mean.mean(axis=1) <= 0.08)
    uniform_horizontal_band = (row_spread <= 0.025) & row_neutral & row_extreme

    def consecutive_from_start(mask: Any) -> int:
        count = 0
        for value in mask:
            if not bool(value):
                break
            count += 1
        return count

    top_band = consecutive_from_start(uniform_horizontal_band)
    bottom_band = consecutive_from_start(uniform_horizontal_band[::-1])
    minimum_y_band = max(2, round(height * 0.02))
    horizontal_letterbox = (
        top_band >= minimum_y_band
        and bottom_band >= minimum_y_band
        and top_band + bottom_band < height * 0.7
    )

    column_mean = rgb.mean(axis=0)
    column_spread = rgb.std(axis=(0, 2))
    column_neutral = (column_mean.max(axis=1) - column_mean.min(axis=1)) <= 0.05
    column_extreme = (column_mean.mean(axis=1) >= 0.72) | (column_mean.mean(axis=1) <= 0.08)
    uniform_vertical_band = (column_spread <= 0.025) & column_neutral & column_extreme
    left_band = consecutive_from_start(uniform_vertical_band)
    right_band = consecutive_from_start(uniform_vertical_band[::-1])
    minimum_x_band = max(2, round(width * 0.02))
    vertical_letterbox = (
        left_band >= minimum_x_band
        and right_band >= minimum_x_band
        and left_band + right_band < width * 0.7
    )

    edge_samples = np.concatenate(
        [rgb[0], rgb[-1], rgb[:, 0], rgb[:, -1]],
        axis=0,
    )
    border_color = np.median(edge_samples, axis=0)
    edge_spread = float(np.median(np.max(np.abs(edge_samples - border_color), axis=1)))
    difference = np.max(np.abs(rgb - border_color), axis=2)
    active = difference > float(color_tolerance)
    active_rows = np.flatnonzero(active.mean(axis=1) >= float(minimum_active_fraction))
    active_columns = np.flatnonzero(active.mean(axis=0) >= float(minimum_active_fraction))
    if active_rows.size == 0 or active_columns.size == 0:
        left, top, right, bottom = 0, 0, width, height
    else:
        left = int(active_columns[0])
        right = int(active_columns[-1]) + 1
        top = int(active_rows[0])
        bottom = int(active_rows[-1]) + 1
    if horizontal_letterbox:
        top, bottom = top_band, height - bottom_band
        left, right = 0, width
    if vertical_letterbox:
        left, right = left_band, width - right_band
        if not horizontal_letterbox:
            top, bottom = 0, height
    content_width = max(1, right - left)
    content_height = max(1, bottom - top)
    removed_fraction = 1.0 - (content_width * content_height) / float(width * height)
    spans_one_full_axis = content_width >= width * 0.98 or content_height >= height * 0.98
    border_neutral = float(border_color.max() - border_color.min()) <= 0.05
    border_extreme = float(border_color.mean()) >= 0.72 or float(border_color.mean()) <= 0.08
    cropped = (
        horizontal_letterbox
        or vertical_letterbox
        or (
            removed_fraction >= 0.025
            and edge_spread <= 0.08
            and spans_one_full_axis
            and border_neutral
            and border_extreme
        )
    )
    if not cropped:
        left, top, right, bottom = 0, 0, width, height
        content_width, content_height = width, height
        removed_fraction = 0.0
    return {
        "path": str(path),
        "width_px": width,
        "height_px": height,
        "content_bbox_px": [left, top, right, bottom],
        "content_bbox_norm": [
            left / width,
            top / height,
            right / width,
            bottom / height,
        ],
        "content_width_px": content_width,
        "content_height_px": content_height,
        "content_aspect_ratio": content_width / float(content_height),
        "border_detected": cropped,
        "border_removed_fraction": round(removed_fraction, 6),
        "border_color_rgb": [round(float(value), 6) for value in border_color],
        "confidence": "high" if cropped and edge_spread <= 0.025 else ("medium" if cropped else "low"),
    }
