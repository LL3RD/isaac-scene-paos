from __future__ import annotations

import json
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "isaac-scene-validation-manifest/v1"
REPORT_FIELDS = (
    "validate_scene_reports",
    "overlap_reports",
    "support_reports",
    "facing_reports",
    "render_feedback_reports",
)


def _resolve_path(root: Path, value: Any, field: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty path string")
    path = Path(value).expanduser()
    return path if path.is_absolute() else (root / path).resolve()


def load_validation_manifest(path: str | Path) -> dict[str, Any]:
    """Load and resolve one strict validation manifest without reading its evidence."""
    manifest_path = Path(path).expanduser().resolve()
    value = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("validation manifest must be a JSON object")
    if value.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"unsupported validation manifest schema: {value.get('schema_version')!r}")
    quality_policy = value.get("quality_policy")
    if quality_policy not in {"layout_only", "realistic", "reference_match"}:
        raise ValueError(f"invalid quality_policy: {quality_policy!r}")
    root = manifest_path.parent
    result: dict[str, Any] = {
        "manifest": manifest_path,
        "quality_policy": quality_policy,
        "scene_spec": _resolve_path(root, value.get("scene_spec"), "scene_spec"),
        "compile_report": _resolve_path(root, value.get("compile_report"), "compile_report"),
    }
    for field in REPORT_FIELDS:
        rows = value.get(field)
        if not isinstance(rows, list) or not rows:
            raise ValueError(f"{field} must contain at least one evidence path")
        result[field] = [_resolve_path(root, row, field) for row in rows]
    output = value.get("output")
    result["output"] = _resolve_path(root, output, "output") if output else None
    return result


def flatten_evidence_paths(inputs: dict[str, Any]) -> list[Path]:
    paths: list[Path] = []
    for field in ("scene_spec", "compile_report", *REPORT_FIELDS):
        value = inputs.get(field)
        if isinstance(value, list):
            paths.extend(item for item in value if isinstance(item, Path))
        elif isinstance(value, Path):
            paths.append(value)
    return paths
