from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


class AssetEntrypointMismatch(ValueError):
    """Raised when a raw asset layer bypasses a required alignment wrapper."""

    def __init__(self, report: dict[str, Any]):
        self.report = report
        super().__init__(str(report.get("summary") or "incompatible asset entrypoint"))


def _declared_up_axes(metadata_path: Path) -> list[str]:
    if not metadata_path.is_file():
        return []
    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return []
    raw = payload.get("upAxis")
    values = raw if isinstance(raw, list) else [raw]
    return [str(value).strip().upper().lstrip("+") for value in values if value]


def _alignment_wrapper_evidence(wrapper_path: Path, raw_path: Path) -> dict[str, Any]:
    if not wrapper_path.is_file():
        return {"exists": False, "references_selected_raw_layer": False, "authors_alignment_transform": False}
    try:
        text = wrapper_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {"exists": True, "references_selected_raw_layer": False, "authors_alignment_transform": False}
    raw_name = re.escape(raw_path.name)
    references_raw = bool(re.search(rf"@(?:\./)?{raw_name}@", text))
    orient_values = re.findall(
        r"(?:quat[fd]|quat)\s+xformOp:orient\s*=\s*\(([^)]*)\)",
        text,
    )
    authors_orient = False
    for value in orient_values:
        try:
            components = [float(part.strip()) for part in value.split(",")]
        except ValueError:
            # An authored, non-literal orientation still requires composition.
            authors_orient = True
            break
        if len(components) == 4 and any(
            abs(actual - expected) > 1e-6
            for actual, expected in zip(components, (1.0, 0.0, 0.0, 0.0))
        ):
            authors_orient = True
            break
    authors_rotate = bool(re.search(r"xformOp:rotate(?:X|Y|Z|XYZ)\s*=", text))
    authors_transform = bool(re.search(r"xformOp:transform\s*=", text))
    return {
        "exists": True,
        "references_selected_raw_layer": references_raw,
        "authors_alignment_transform": authors_orient or authors_rotate or authors_transform,
    }


def inspect_asset_entrypoint(asset_path: str | Path, *, target_up_axis: str = "Z") -> dict[str, Any]:
    """Detect a raw USD layer that bypasses a sibling alignment wrapper.

    GenieSim packages commonly keep source geometry in ``Aligned.usd`` while
    ``Aligned.usda`` supplies the semantic Y-up-to-Z-up transform. USD stage
    metadata on the raw layer can still say Z-up, so package metadata and the
    wrapper composition are the stronger entrypoint evidence.
    """
    path = Path(asset_path).expanduser().resolve()
    result: dict[str, Any] = {
        "schema_version": "isaac-asset-entrypoint-compatibility/v1",
        "asset_path": str(path),
        "target_up_axis": str(target_up_axis).upper().lstrip("+"),
        "status": "pass",
        "code": None,
        "summary": "selected asset entrypoint has no detected alignment-wrapper conflict",
        "recommended_path": None,
        "evidence": {},
    }
    if not path.is_file():
        result.update(status="fail", code="ASSET_NOT_FOUND", summary=f"asset not found: {path}")
        return result

    # A selected USDA layer is already the package-authored composition entrypoint.
    if path.suffix.lower() == ".usda":
        result["summary"] = "selected package composition layer"
        return result

    metadata_path = path.parent / "object_parameters.json"
    declared_up_axes = _declared_up_axes(metadata_path)
    wrapper_path = path.with_suffix(".usda")
    wrapper = _alignment_wrapper_evidence(wrapper_path, path)
    result["evidence"] = {
        "metadata_path": str(metadata_path) if metadata_path.is_file() else None,
        "declared_up_axes": declared_up_axes,
        "sibling_wrapper_path": str(wrapper_path),
        **wrapper,
    }
    target = result["target_up_axis"]
    non_target_up = bool(declared_up_axes) and target not in declared_up_axes
    requires_wrapper = (
        wrapper["exists"]
        and wrapper["references_selected_raw_layer"]
        and wrapper["authors_alignment_transform"]
        and non_target_up
    )
    if requires_wrapper:
        result.update(
            status="fail",
            code="RAW_ASSET_REQUIRES_ALIGNMENT_WRAPPER",
            summary=(
                f"{path.name} declares package up-axis {declared_up_axes} and is composed by "
                f"{wrapper_path.name} with an alignment transform; select the wrapper layer"
            ),
            recommended_path=str(wrapper_path),
        )
    return result


def require_compatible_asset_entrypoint(asset_path: str | Path) -> dict[str, Any]:
    report = inspect_asset_entrypoint(asset_path)
    if report["status"] == "fail":
        raise AssetEntrypointMismatch(report)
    return report
