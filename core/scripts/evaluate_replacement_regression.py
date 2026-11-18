#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


MANDATORY_GATES = (
    "geometry_pass",
    "facing_pass",
    "lighting_pass",
    "anchor_definition_pass",
    "anchor_real_asset_pass",
    "proxy_free_pass",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Decide whether a reference-guided asset replacement should be kept or rolled back."
    )
    parser.add_argument("--baseline-quality", type=Path, required=True)
    parser.add_argument("--challenger-quality", type=Path, required=True)
    parser.add_argument("--reference-regression-tolerance", type=float, default=0.005)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def unwrap(payload: dict[str, Any]) -> dict[str, Any]:
    result = payload.get("result")
    return result if isinstance(result, dict) else payload


def reference_score(payload: dict[str, Any]) -> float:
    body = unwrap(payload)
    reference = body.get("reference_match")
    if isinstance(reference, dict) and reference.get("score") is not None:
        return float(reference["score"])
    if body.get("score") is not None:
        return float(body["score"])
    raise ValueError("reference-match score not found")


def quality_gates(payload: dict[str, Any]) -> dict[str, bool]:
    body = unwrap(payload)
    gates = body.get("gates") or body.get("quality_gates") or {}
    return {name: bool(gates.get(name, False)) for name in MANDATORY_GATES}


def replacement_decision(
    baseline: dict[str, Any],
    challenger: dict[str, Any],
    *,
    tolerance: float = 0.005,
) -> dict[str, Any]:
    baseline_score = reference_score(baseline)
    challenger_score = reference_score(challenger)
    delta = challenger_score - baseline_score
    baseline_gates = quality_gates(baseline)
    challenger_gates = quality_gates(challenger)
    regressed_gates = [
        name
        for name in MANDATORY_GATES
        if baseline_gates[name] and not challenger_gates[name]
    ]
    reasons: list[str] = []
    if regressed_gates:
        reasons.append("mandatory_quality_gate_regressed")
    if delta < -abs(float(tolerance)):
        reasons.append("whole_scene_reference_score_regressed")
    decision = "rollback" if reasons else "keep"
    return {
        "schema_version": "isaac-asset-replacement-regression/v1",
        "decision": decision,
        "baseline_score": round(baseline_score, 6),
        "challenger_score": round(challenger_score, 6),
        "score_delta": round(delta, 6),
        "reference_regression_tolerance": abs(float(tolerance)),
        "baseline_gates": baseline_gates,
        "challenger_gates": challenger_gates,
        "regressed_gates": regressed_gates,
        "reasons": reasons,
        "reference_gate_resolved": bool(
            unwrap(challenger).get("gates", {}).get("reference_match_pass", False)
        ),
    }


def main() -> int:
    args = parse_args()
    baseline = json.loads(args.baseline_quality.read_text(encoding="utf-8"))
    challenger = json.loads(args.challenger_quality.read_text(encoding="utf-8"))
    result = replacement_decision(
        baseline,
        challenger,
        tolerance=args.reference_regression_tolerance,
    )
    text = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0 if result["decision"] == "keep" else 1


if __name__ == "__main__":
    raise SystemExit(main())
