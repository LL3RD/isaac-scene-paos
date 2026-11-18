from __future__ import annotations

import math
from typing import Iterable


FRONT_AXES_2D = {
    "+X": (1.0, 0.0),
    "-X": (-1.0, 0.0),
    "+Y": (0.0, 1.0),
    "-Y": (0.0, -1.0),
}


def normalize_front_axis(value: str) -> str:
    axis = str(value).strip().upper()
    if axis not in FRONT_AXES_2D:
        raise ValueError(f"front_axis must be one of {sorted(FRONT_AXES_2D)}: {value!r}")
    return axis


def required_yaw_deg(
    subject_xy: Iterable[float],
    target_xy: Iterable[float],
    front_axis: str,
) -> float:
    """Return Z yaw that makes a local front axis point at a target in XY."""
    sx, sy = [float(value) for value in subject_xy]
    tx, ty = [float(value) for value in target_xy]
    dx, dy = tx - sx, ty - sy
    if math.hypot(dx, dy) <= 1e-9:
        raise ValueError("subject and target cannot share the same XY position")
    fx, fy = FRONT_AXES_2D[normalize_front_axis(front_axis)]
    yaw = math.degrees(math.atan2(dy, dx) - math.atan2(fy, fx))
    return ((yaw + 180.0) % 360.0) - 180.0


def facing_error_deg(front_xy: Iterable[float], target_delta_xy: Iterable[float]) -> float:
    """Return the unsigned heading error between a world front and target vector."""
    fx, fy = [float(value) for value in front_xy]
    dx, dy = [float(value) for value in target_delta_xy]
    front_length = math.hypot(fx, fy)
    target_length = math.hypot(dx, dy)
    if front_length <= 1e-9 or target_length <= 1e-9:
        raise ValueError("front and target vectors must be non-zero in XY")
    dot = (fx * dx + fy * dy) / (front_length * target_length)
    return math.degrees(math.acos(max(-1.0, min(1.0, dot))))
