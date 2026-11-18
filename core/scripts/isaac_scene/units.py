from __future__ import annotations

import math
from typing import Any


VALID_UNIT_MODES = {"auto", "multiply", "preserve"}


class UnitScaleMismatch(ValueError):
    pass


def select_source_unit_contract(
    *,
    layer_meters_per_unit: float | None,
    layer_units_authored: bool,
    declared_meters_per_unit: float | None = None,
    declared_units_verified: bool = False,
) -> dict[str, Any]:
    """Choose the unit evidence used to normalize a referenced asset.

    Some asset entry layers do not author ``metersPerUnit`` even though a
    deterministic asset audit has established the units of their referenced
    geometry.  Trust that declaration only when the spec explicitly records it
    as verified; otherwise preserve the existing fail-safe behaviour.
    """

    if layer_units_authored and layer_meters_per_unit is not None:
        value = float(layer_meters_per_unit)
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"layer metersPerUnit must be positive: {value!r}")
        return {
            "meters_per_unit": value,
            "units_verified": True,
            "source": "authored_layer_metadata",
        }
    if declared_units_verified and declared_meters_per_unit is not None:
        value = float(declared_meters_per_unit)
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"verified declared metersPerUnit must be positive: {value!r}")
        return {
            "meters_per_unit": value,
            "units_verified": True,
            "source": "verified_asset_audit",
        }
    return {
        "meters_per_unit": layer_meters_per_unit,
        "units_verified": bool(layer_units_authored),
        "source": "unverified_or_unavailable",
    }


def _scale3(value: Any) -> list[float] | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return [float(value)] * 3
    if isinstance(value, (list, tuple)) and len(value) == 3:
        return [float(part) for part in value]
    raise ValueError(f"scale must be a number or three values: {value!r}")


def resolve_reference_scale(
    *,
    source_meters_per_unit: float | None,
    target_meters_per_unit: float,
    authored_source_units: bool,
    explicit_scale: Any = None,
    unit_mode: str = "auto",
    allow_unit_mismatch: bool = False,
    mismatch_ratio: float = 32.0,
) -> dict[str, Any]:
    """Return the total reference scale and a serializable unit audit.

    USD references do not convert layer units.  In auto mode an authored source
    metersPerUnit therefore becomes a scale of source/target when the scene item
    has no explicit scale.  Explicit scales are treated as total scales so old
    specs remain reproducible; extreme conflicts fail unless explicitly waived.
    """

    target = float(target_meters_per_unit)
    if not math.isfinite(target) or target <= 0:
        raise ValueError(f"target metersPerUnit must be positive: {target!r}")
    mode = str(unit_mode or "auto").lower()
    if mode not in VALID_UNIT_MODES:
        raise ValueError(f"unit_mode must be one of {sorted(VALID_UNIT_MODES)}: {mode!r}")
    supplied = _scale3(explicit_scale)
    source = float(source_meters_per_unit) if source_meters_per_unit is not None else None
    if source is not None and (not math.isfinite(source) or source <= 0):
        raise ValueError(f"source metersPerUnit must be positive: {source!r}")

    audit: dict[str, Any] = {
        "source_meters_per_unit": source,
        "source_units_authored": bool(authored_source_units),
        "target_meters_per_unit": target,
        "unit_mode": mode,
        "explicit_scale": supplied,
        "allow_unit_mismatch": bool(allow_unit_mismatch),
        "warnings": [],
    }
    if not authored_source_units or source is None:
        effective = supplied or [1.0, 1.0, 1.0]
        audit.update({
            "expected_unit_scale": None,
            "effective_scale": effective,
            "resolution": "preserved_unknown_source_units",
        })
        audit["warnings"].append({
            "code": "SOURCE_UNITS_UNAUTHORED",
            "summary": "Source layer does not author metersPerUnit; reference scale was not inferred.",
        })
        return audit

    expected = source / target
    audit["expected_unit_scale"] = expected
    if mode == "preserve":
        effective = supplied or [1.0, 1.0, 1.0]
        audit.update({"effective_scale": effective, "resolution": "preserved_by_policy"})
        if not allow_unit_mismatch and not math.isclose(expected, 1.0, rel_tol=1e-9, abs_tol=1e-12):
            audit["warnings"].append({
                "code": "UNIT_NORMALIZATION_BYPASSED",
                "summary": f"Source requires unit scale {expected:g}, but unit_mode=preserve was requested.",
            })
        return audit

    if mode == "multiply":
        artistic = supplied or [1.0, 1.0, 1.0]
        effective = [expected * value for value in artistic]
        audit.update({"effective_scale": effective, "resolution": "unit_scale_multiplied"})
        return audit

    if supplied is None:
        effective = [expected, expected, expected]
        audit.update({"effective_scale": effective, "resolution": "auto_normalized"})
        return audit

    ratios = [abs(value) / expected for value in supplied if value != 0]
    extreme = not ratios or min(ratios) < 1.0 / mismatch_ratio or max(ratios) > mismatch_ratio
    if extreme and not allow_unit_mismatch:
        raise UnitScaleMismatch(
            f"explicit scale {supplied} conflicts with source/target unit scale {expected:g}; "
            "use unit_mode='multiply' for artistic scaling or allow_unit_mismatch=true to waive"
        )
    audit.update({"effective_scale": supplied, "resolution": "explicit_total_scale"})
    if extreme:
        audit["warnings"].append({
            "code": "EXPLICIT_UNIT_MISMATCH_WAIVED",
            "summary": f"Explicit scale differs materially from expected unit scale {expected:g}.",
        })
    return audit
