from __future__ import annotations

import json
from pathlib import Path
from typing import Any


PROFILE_NAMES = ("preview", "review", "final")


def load_render_profiles(config_path: str | Path) -> dict[str, dict[str, Any]]:
    path = Path(config_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != "isaac-scene-render-profiles/v1":
        raise ValueError(f"Unsupported render-profile schema: {payload.get('schema_version')}")
    profiles = payload.get("profiles")
    if not isinstance(profiles, dict) or tuple(profiles) != PROFILE_NAMES:
        raise ValueError(f"Render profiles must be ordered exactly as {PROFILE_NAMES}")
    normalized: dict[str, dict[str, Any]] = {}
    for name in PROFILE_NAMES:
        row = profiles.get(name)
        if not isinstance(row, dict):
            raise ValueError(f"Missing render profile: {name}")
        renderer = row.get("renderer")
        if renderer not in {"RayTracedLighting", "PathTracing"}:
            raise ValueError(f"Unsupported renderer for {name}: {renderer}")
        for key in (
            "width", "height", "anti_aliasing", "settle_frames",
            "camera_settle_frames", "evidence_rt_subframes",
        ):
            if not isinstance(row.get(key), int) or int(row[key]) < 0:
                raise ValueError(f"{name}.{key} must be a non-negative integer")
        if name == "final" and not isinstance(row.get("path_tracing"), dict):
            raise ValueError("final.path_tracing is required")
        normalized[name] = dict(row)
    return normalized


def resolve_render_profile(
    config_path: str | Path,
    name: str,
    **overrides: Any,
) -> dict[str, Any]:
    profiles = load_render_profiles(config_path)
    if name not in profiles:
        raise ValueError(f"Unknown render profile: {name}")
    resolved = {"name": name, **profiles[name]}
    for key, value in overrides.items():
        if value is not None:
            resolved[key] = value
    return resolved


def path_tracing_settings(profile: dict[str, Any]) -> dict[str, Any]:
    if profile.get("renderer") != "PathTracing":
        return {}
    path = profile.get("path_tracing") or {}
    return {
        "/rtx/pathtracing/spp": int(path["spp"]),
        "/rtx/pathtracing/totalSpp": int(path["total_spp"]),
        "/rtx/pathtracing/maxBounces": int(path["max_bounces"]),
        "/rtx/pathtracing/maxSpecularAndTransmissionBounces": int(
            path["max_specular_and_transmission_bounces"]
        ),
        "/rtx/pathtracing/maxVolumeBounces": int(path["max_volume_bounces"]),
        "/rtx/pathtracing/optixDenoiser/enabled": bool(path["optix_denoiser"]),
    }
