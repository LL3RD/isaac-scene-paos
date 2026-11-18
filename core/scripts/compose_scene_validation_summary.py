#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from isaac_scene.quality import asset_quality_from_spec  # noqa: E402
from isaac_scene.validation import REPORT_FIELDS, flatten_evidence_paths, load_validation_manifest  # noqa: E402


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object: {path}")
    result = value.get("result")
    return result if isinstance(result, dict) else value


def _is_pass(report: dict[str, Any]) -> bool:
    return report.get("status") == "pass" and not report.get("errors")


def _is_scene_validation_pass(report: dict[str, Any]) -> bool:
    if _is_pass(report):
        return True
    warnings = report.get("warnings")
    warning_codes = {
        str(row.get("code") or "")
        for row in warnings
        if isinstance(row, dict)
    } if isinstance(warnings, list) else set()
    return (
        report.get("status") == "warn"
        and not report.get("errors")
        and int(report.get("unresolved_dependency_count", -1)) == 0
        and bool(warning_codes)
        and warning_codes <= {"RUNTIME_MDL_RESOLUTION_REQUIRED"}
    )


def _report_rows(
    single: dict[str, Any] | None,
    multiple: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    rows = list(multiple or [])
    if single is not None:
        rows.insert(0, single)
    return rows


def _all_reports_pass(reports: list[dict[str, Any]], predicate: Any = _is_pass) -> bool:
    return bool(reports) and all(predicate(report) for report in reports)


def compose_summary(
    *,
    scene_spec: dict[str, Any],
    compile_report: dict[str, Any],
    quality_policy: str,
    validate_report: dict[str, Any] | None = None,
    overlap_report: dict[str, Any] | None = None,
    support_report: dict[str, Any] | None = None,
    facing_report: dict[str, Any] | None = None,
    render_feedback_report: dict[str, Any] | None = None,
    validate_reports: list[dict[str, Any]] | None = None,
    overlap_reports: list[dict[str, Any]] | None = None,
    support_reports: list[dict[str, Any]] | None = None,
    facing_reports: list[dict[str, Any]] | None = None,
    render_feedback_reports: list[dict[str, Any]] | None = None,
    evidence_paths: dict[str, Any] | None = None,
) -> dict[str, Any]:
    unresolved_value = compile_report.get("unresolved_count")
    if unresolved_value is None and compile_report.get("status") in {"ok", "pass"}:
        unresolved_value = 0
    unresolved_count = int(unresolved_value if unresolved_value is not None else -1)
    asset_quality = asset_quality_from_spec(scene_spec)
    provenance_missing = 0
    entities = scene_spec.get("entities")
    if not isinstance(entities, list):
        entities = scene_spec.get("references", [])
    for entity in entities:
        if not isinstance(entity, dict):
            continue
        text = " ".join(
            str(entity.get(key) or "").lower()
            for key in ("role", "source", "asset_source")
        )
        if "shell" in text or "background" in text:
            continue
        asset = entity.get("asset") if isinstance(entity.get("asset"), dict) else {}
        logical_id = (
            entity.get("logical_id")
            or entity.get("asset_id")
            or entity.get("model_id")
            or asset.get("logical_id")
            or asset.get("model_id")
        )
        canonical_class = entity.get("canonical_class") or asset.get("canonical_class")
        if not logical_id or not canonical_class:
            provenance_missing += 1

    report_groups = {
        "validate_scene": _report_rows(validate_report, validate_reports),
        "overlap": _report_rows(overlap_report, overlap_reports),
        "support": _report_rows(support_report, support_reports),
        "facing": _report_rows(facing_report, facing_reports),
        "render_feedback": _report_rows(render_feedback_report, render_feedback_reports),
    }
    gates = {
        "unresolved_pass": unresolved_count == 0,
        "validation_pass": _all_reports_pass(report_groups["validate_scene"], _is_scene_validation_pass),
        "overlap_validation_pass": _all_reports_pass(report_groups["overlap"]),
        "support_validation_pass": _all_reports_pass(report_groups["support"]),
        "facing_pass": _all_reports_pass(report_groups["facing"]),
        "render_feedback_pass": _all_reports_pass(report_groups["render_feedback"]),
        "proxy_free_pass": asset_quality["proxy_count"] == 0,
        "anchor_real_asset_pass": (
            asset_quality["anchor_definition_complete"]
            and asset_quality["anchor_real_asset_coverage"] >= 1.0
        ),
        "asset_provenance_pass": provenance_missing == 0,
    }
    required = [
        "unresolved_pass",
        "validation_pass",
        "overlap_validation_pass",
        "support_validation_pass",
        "facing_pass",
        "render_feedback_pass",
    ]
    if quality_policy in {"realistic", "reference_match"}:
        required.extend(["proxy_free_pass", "anchor_real_asset_pass", "asset_provenance_pass"])
    failures = [
        {"code": key.upper(), "message": f"Required scene gate did not pass: {key}"}
        for key in required
        if not gates[key]
    ]
    return {
        "schema_version": "isaac-scene-validation-summary/v1",
        "status": "pass" if not failures else "fail",
        "quality_policy": quality_policy,
        "unresolved_count": unresolved_count,
        "validation_pass": gates["validation_pass"],
        "overlap_validation_pass": gates["overlap_validation_pass"],
        "support_validation_pass": gates["support_validation_pass"],
        "facing_pass": gates["facing_pass"],
        "render_feedback_pass": gates["render_feedback_pass"],
        "proxy_count": asset_quality["proxy_count"],
        "anchor_real_asset_coverage": asset_quality["anchor_real_asset_coverage"],
        "asset_provenance_missing_count": provenance_missing,
        "report_counts": {name: len(rows) for name, rows in report_groups.items()},
        "gates": gates,
        "failures": failures,
        "evidence": evidence_paths or {},
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compose one strict scene summary from independent validation reports."
    )
    parser.add_argument("--manifest", type=Path, default=None)
    parser.add_argument("--scene-spec", type=Path, default=None)
    parser.add_argument("--compile-report", type=Path, default=None)
    parser.add_argument("--validate-scene-report", type=Path, action="append", default=None)
    parser.add_argument("--overlap-report", type=Path, action="append", default=None)
    parser.add_argument("--support-report", type=Path, action="append", default=None)
    parser.add_argument("--facing-report", type=Path, action="append", default=None)
    parser.add_argument("--render-feedback-report", type=Path, action="append", default=None)
    parser.add_argument(
        "--quality-policy",
        choices=("layout_only", "realistic", "reference_match"),
        default=None,
    )
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()
    if args.manifest:
        try:
            inputs = load_validation_manifest(args.manifest)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            parser.error(str(exc))
        output = args.output or inputs.get("output")
        quality_policy = inputs["quality_policy"]
    else:
        raw = {
            "scene_spec": args.scene_spec,
            "compile_report": args.compile_report,
            "validate_scene_reports": args.validate_scene_report,
            "overlap_reports": args.overlap_report,
            "support_reports": args.support_report,
            "facing_reports": args.facing_report,
            "render_feedback_reports": args.render_feedback_report,
        }
        absent = [name for name, value in raw.items() if not value]
        if absent or not args.quality_policy:
            parser.error("missing required evidence arguments: " + ", ".join(absent + (["quality_policy"] if not args.quality_policy else [])))
        inputs = raw
        output = args.output
        quality_policy = args.quality_policy
    if not output:
        parser.error("--output or manifest.output is required")
    missing = [str(path) for path in flatten_evidence_paths(inputs) if not path.is_file()]
    if missing:
        parser.error("missing evidence: " + ", ".join(missing))
    summary = compose_summary(
        scene_spec=_read(inputs["scene_spec"]),
        compile_report=_read(inputs["compile_report"]),
        validate_reports=[_read(path) for path in inputs["validate_scene_reports"]],
        overlap_reports=[_read(path) for path in inputs["overlap_reports"]],
        support_reports=[_read(path) for path in inputs["support_reports"]],
        facing_reports=[_read(path) for path in inputs["facing_reports"]],
        render_feedback_reports=[_read(path) for path in inputs["render_feedback_reports"]],
        quality_policy=quality_policy,
        evidence_paths={
            key: [str(item) for item in value] if isinstance(value, list) else str(value)
            for key, value in inputs.items()
            if key not in {"quality_policy", "output"}
        },
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["status"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
