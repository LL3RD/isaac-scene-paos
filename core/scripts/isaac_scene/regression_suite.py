"""Audit prompt-only scene suites before expanding to more scene types."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any


SCHEMA = "isaac-scene-prompt-regression-suite-audit/v1"
DEFAULT_THRESHOLDS = {
    "minimum_case_count": 5,
    "minimum_prompt_coverage": 0.90,
    "minimum_realism": 0.65,
    "minimum_composition": 0.78,
    "minimum_recognizable_anchor_fraction": 0.85,
    "maximum_asset_gap_count": 0,
}


def _read(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return payload


def _resolve(root: Path, value: Any) -> Path | None:
    if not value:
        return None
    path = Path(str(value)).expanduser()
    return path if path.is_absolute() else (root / path).resolve()


def _status(payload: dict[str, Any]) -> str:
    return str(payload.get("status") or (payload.get("result") or {}).get("status") or "unknown")


def _result(payload: dict[str, Any]) -> dict[str, Any]:
    value = payload.get("result")
    return value if isinstance(value, dict) else payload


def _report_pass(kind: str, payload: dict[str, Any]) -> bool:
    result = _result(payload)
    status = _status(payload)
    if kind == "usd_validation":
        return status in {"pass", "warn"} and not result.get("errors") and not result.get("unresolved_dependency_count")
    if kind == "placement":
        return status == "pass" and not result.get("errors") and int(result.get("quarantined_entity_count") or 0) == 0
    if kind == "camera_confirmation":
        return status == "pass" and bool(result.get("promotion_allowed", True)) and result.get("decision") != "rollback_to_baseline"
    return status == "pass" and not result.get("errors")


def _summary_pass(payload: dict[str, Any]) -> bool:
    status = _status(payload)
    placement = payload.get("placement") or {}
    camera = payload.get("camera") or {}
    return bool(
        status.startswith("pass")
        and int(placement.get("quarantined") or 0) == 0
        and int(placement.get("errors") or 0) == 0
        and payload.get("overlaps") == "pass"
        and camera.get("fresh_render_confirmation") == "pass"
    )


def _preferred_review(review: dict[str, Any]) -> dict[str, Any]:
    preferred = str(review.get("preferred_candidate_id") or "")
    rows = [row for row in review.get("candidate_reviews") or [] if isinstance(row, dict)]
    return next((row for row in rows if str(row.get("candidate_id")) == preferred), rows[0] if rows else {})


def _grade(score: float) -> str:
    for boundary, grade in ((0.80, "A"), (0.70, "B"), (0.60, "C"), (0.50, "D")):
        if score >= boundary:
            return grade
    return "E"


def audit_suite(manifest_path: str | Path, output_path: str | Path) -> dict[str, Any]:
    manifest_file = Path(manifest_path).expanduser().resolve()
    manifest = _read(manifest_file)
    if manifest.get("schema_version") != "isaac-scene-prompt-regression-suite/v1":
        raise ValueError("Unsupported prompt regression suite manifest schema")
    cases = manifest.get("cases") or []
    if not isinstance(cases, list) or not cases:
        raise ValueError("Suite manifest must contain at least one case")
    thresholds = {**DEFAULT_THRESHOLDS, **(manifest.get("thresholds") or {})}
    root = manifest_file.parent
    audited = []
    repair_queue = []
    seen_ids: set[str] = set()
    for case in cases:
        if not isinstance(case, dict) or not case.get("id"):
            raise ValueError("Every suite case requires an id")
        case_id = str(case["id"])
        if case_id in seen_ids:
            raise ValueError(f"Duplicate suite case id: {case_id}")
        seen_ids.add(case_id)
        spec_path = _resolve(root, case.get("spec"))
        render_path = _resolve(root, case.get("final_render"))
        review_path = _resolve(root, case.get("visual_review"))
        if not spec_path or not spec_path.is_file():
            raise ValueError(f"Missing scene spec for {case_id}: {spec_path}")
        if not render_path or not render_path.is_file():
            raise ValueError(f"Missing final render for {case_id}: {render_path}")
        if not review_path or not review_path.is_file():
            raise ValueError(f"Missing visual review for {case_id}: {review_path}")
        spec, review = _read(spec_path), _read(review_path)
        preferred = _preferred_review(review)
        reports = case.get("reports") or {}
        report_gates: dict[str, bool] = {}
        summary_path = _resolve(root, reports.get("summary")) if isinstance(reports, dict) else None
        if summary_path:
            if not summary_path.is_file():
                raise ValueError(f"Missing validation summary for {case_id}: {summary_path}")
            report_gates["summary"] = _summary_pass(_read(summary_path))
        else:
            for kind in ("placement", "usd_validation", "overlaps", "render_feedback", "camera_confirmation"):
                path = _resolve(root, reports.get(kind)) if isinstance(reports, dict) else None
                if not path or not path.is_file():
                    report_gates[kind] = False
                else:
                    report_gates[kind] = _report_pass(kind, _read(path))
        geometry_gate = bool(report_gates and all(report_gates.values()))
        prompt_score = float(preferred.get("prompt_coverage_score") or 0.0)
        realism_score = float(preferred.get("realism_score") or 0.0)
        composition_score = float(preferred.get("composition_score") or 0.0)
        anchor_score = float(preferred.get("recognizable_anchor_fraction") or 0.0)
        asset_gaps = [row for row in spec.get("asset_gaps") or [] if isinstance(row, dict)]
        entities = [row for row in spec.get("entities") or [] if isinstance(row, dict)]
        sources = Counter(str(row.get("source_library") or row.get("asset_source") or "procedural") for row in entities)
        maturity_score = (
            0.25 * float(geometry_gate)
            + 0.25 * prompt_score
            + 0.25 * realism_score
            + 0.15 * composition_score
            + 0.10 * anchor_score
        )
        blockers = []
        if not geometry_gate:
            blockers.append("geometry_or_evidence_gate_failed")
        for key, score, threshold in (
            ("prompt_coverage_below_threshold", prompt_score, thresholds["minimum_prompt_coverage"]),
            ("realism_below_threshold", realism_score, thresholds["minimum_realism"]),
            ("composition_below_threshold", composition_score, thresholds["minimum_composition"]),
            ("anchor_recognition_below_threshold", anchor_score, thresholds["minimum_recognizable_anchor_fraction"]),
        ):
            if score < float(threshold):
                blockers.append(key)
        if len(asset_gaps) > int(thresholds["maximum_asset_gap_count"]):
            blockers.append("asset_gap_count_above_threshold")
        audited.append({
            "id": case_id,
            "prompt": ((spec.get("brief_snapshot") or {}).get("inputs") or {}).get("text"),
            "entity_count": len(entities),
            "source_counts": dict(sorted(sources.items())),
            "asset_gap_count": len(asset_gaps),
            "report_gates": report_gates,
            "geometry_gate": geometry_gate,
            "scores": {
                "prompt_coverage": prompt_score,
                "realism": realism_score,
                "composition": composition_score,
                "recognizable_anchor_fraction": anchor_score,
                "maturity": round(maturity_score, 6),
            },
            "grade": _grade(maturity_score),
            "expansion_blockers": blockers,
            "visual_issues": review.get("issues") or [],
            "artifacts": {"spec": str(spec_path), "final_render": str(render_path), "visual_review": str(review_path)},
        })
        severity = "critical" if not geometry_gate else "high" if realism_score < float(thresholds["minimum_realism"]) else "medium"
        repair_queue.append({
            "case_id": case_id,
            "severity": severity,
            "priority_score": round((1.0 - realism_score) * 0.60 + (1.0 - prompt_score) * 0.20 + len(asset_gaps) * 0.10 + (0.10 if not geometry_gate else 0.0), 6),
            "primary_reason": "geometry_or_evidence_gate_failed" if not geometry_gate else "realism_below_threshold" if realism_score < float(thresholds["minimum_realism"]) else "remaining_visual_issues",
            "deferred_visual_issues": review.get("issues") or [],
        })
    repair_queue.sort(key=lambda row: (-float(row["priority_score"]), str(row["case_id"])))
    minimum_count_met = len(audited) >= int(thresholds["minimum_case_count"])
    expansion_ready = bool(minimum_count_met and all(not row["expansion_blockers"] for row in audited))
    result = {
        "schema_version": SCHEMA,
        "status": "pass",
        "suite_id": manifest.get("suite_id") or manifest_file.stem,
        "case_count": len(audited),
        "thresholds": thresholds,
        "minimum_case_count_met": minimum_count_met,
        "expansion_ready": expansion_ready,
        "recommendation": "expand_with_holdout_cases" if expansion_ready else "deepen_core_suite_before_expansion",
        "cases": audited,
        "repair_queue": repair_queue,
        "policy": {
            "core_suite": "Optimize the declared cases until every expansion gate passes.",
            "holdouts": "After readiness, add a small unseen canary set and do not tune per-scene constants on it.",
            "synthesis28": "Keep the 28 source-scene reconstruction benchmark separate from prompt-only development cases.",
        },
    }
    output = Path(output_path).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    result["output"] = str(output.resolve())
    return result
