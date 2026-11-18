#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any


SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from isaac_scene.quality import evaluate_scene  # noqa: E402


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else {}


def _entities(spec: dict[str, Any]) -> list[dict[str, Any]]:
    rows = spec.get("entities")
    if not isinstance(rows, list):
        rows = spec.get("references", [])
    return [row for row in rows if isinstance(row, dict)]


def _entity_model_id(entity: dict[str, Any], known_model_ids: set[str]) -> str | None:
    asset = entity.get("asset") if isinstance(entity.get("asset"), dict) else {}
    values = [
        entity.get("model_id"),
        entity.get("logical_id"),
        entity.get("asset_id"),
        entity.get("asset_logical_id"),
        asset.get("model_id"),
        asset.get("logical_id"),
    ]
    for value in values:
        text = str(value or "")
        if text.startswith("synthesis27:"):
            text = text.split(":", 1)[1]
        if text in known_model_ids:
            return text
    path_text = " ".join(
        str(entity.get(key) or "") for key in ("path", "asset_path", "usd_path")
    )
    matches = [model_id for model_id in known_model_ids if model_id in path_text]
    return matches[0] if len(matches) == 1 else None


def _entity_class(entity: dict[str, Any]) -> str | None:
    asset = entity.get("asset") if isinstance(entity.get("asset"), dict) else {}
    value = entity.get("canonical_class") or asset.get("canonical_class")
    text = str(value or "").strip()
    return text or None


def contract_coverage(case: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    oracle = case["oracle"]
    exact_targets = oracle["exact_asset_targets"]
    known_model_ids = {str(row["model_id"]) for row in exact_targets}
    model_counts: Counter[str] = Counter()
    class_counts: Counter[str] = Counter()
    missing_provenance = 0
    for entity in _entities(spec):
        model_id = _entity_model_id(entity, known_model_ids)
        canonical_class = _entity_class(entity)
        if model_id:
            model_counts[model_id] += 1
        if canonical_class:
            class_counts[canonical_class] += 1
        if not model_id and not canonical_class:
            missing_provenance += 1

    anchor_targets = oracle["anchor_targets"]
    exact_required = [
        row for row in anchor_targets if row["match_policy"] == "exact_source_asset"
    ]
    exact_denominator = sum(int(row["count"]) for row in exact_required)
    exact_matched = sum(
        min(int(row["count"]), model_counts[str(row["model_id"])])
        for row in exact_required
    )

    semantic_required: Counter[str] = Counter()
    for row in anchor_targets:
        semantic_required[str(row["canonical_class"])] += int(row["count"])
    semantic_denominator = sum(semantic_required.values())
    semantic_matched = sum(
        min(count, class_counts[canonical_class])
        for canonical_class, count in semantic_required.items()
    )
    return {
        "entity_count": len(_entities(spec)),
        "entity_missing_model_and_class_provenance_count": missing_provenance,
        "licensed_exact_anchor_target_count": exact_denominator,
        "licensed_exact_anchor_matched_count": exact_matched,
        "licensed_exact_anchor_coverage": (
            exact_matched / exact_denominator if exact_denominator else 1.0
        ),
        "semantic_anchor_target_count": semantic_denominator,
        "semantic_anchor_matched_count": semantic_matched,
        "semantic_anchor_coverage": (
            semantic_matched / semantic_denominator if semantic_denominator else 1.0
        ),
        "matched_model_counts": dict(sorted(model_counts.items())),
        "matched_semantic_class_counts": dict(sorted(class_counts.items())),
    }


def _artifact_paths(
    case: dict[str, Any],
    *,
    dataset_root: Path,
    artifact_set: str,
) -> dict[str, Path]:
    source = case["baseline"] if artifact_set == "baseline" else case["expected_outputs"]
    return {
        "reference_image": dataset_root
        / str(case["generation_inputs"]["reference_guided"]["reference_image"]),
        "rendered_image": dataset_root / str(source["rendered_image"]),
        "scene_spec": dataset_root / str(source["scene_spec"]),
        "summary": dataset_root / str(source["summary"]),
    }


def evaluate_case(
    case: dict[str, Any],
    *,
    dataset_root: Path,
    artifact_set: str,
    mode: str,
) -> dict[str, Any]:
    paths = _artifact_paths(case, dataset_root=dataset_root, artifact_set=artifact_set)
    spec = _read_json(paths["scene_spec"])
    summary = _read_json(paths["summary"])
    quality = evaluate_scene(
        reference_image=paths["reference_image"],
        rendered_image=paths["rendered_image"],
        scene_spec=paths["scene_spec"],
        summary=paths["summary"],
    )
    coverage = contract_coverage(case, spec)
    declared_asset_gaps = [
        row for row in spec.get("asset_gaps", []) if isinstance(row, dict)
    ]
    common = case["acceptance"]["common"]
    mode_acceptance = case["acceptance"][mode]
    gates = {
        "unresolved_pass": int(summary.get("unresolved_count", -1)) <= int(
            common["unresolved_count_max"]
        ) and "unresolved_count" in summary,
        "proxy_free_pass": quality["gates"]["proxy_free_pass"],
        "geometry_pass": quality["gates"]["geometry_pass"],
        "overlap_pass": summary.get("overlap_validation_pass") is True,
        "support_pass": summary.get("support_validation_pass") is True,
        "facing_pass": quality["gates"]["facing_pass"],
        "render_feedback_pass": quality["gates"]["lighting_pass"],
        "anchor_real_asset_pass": quality["gates"]["anchor_real_asset_pass"],
        "semantic_anchor_coverage_pass": coverage["semantic_anchor_coverage"]
        >= float(mode_acceptance["semantic_anchor_coverage_min"]),
    }
    if mode == "reference_guided":
        gates["licensed_exact_anchor_coverage_pass"] = (
            coverage["licensed_exact_anchor_coverage"]
            >= float(mode_acceptance["licensed_exact_anchor_coverage_min"])
        )
        gates["reference_match_pass"] = (
            quality["reference_match"].get("score", 0.0)
            >= float(mode_acceptance["reference_match_score_min"])
        )
    metrics = quality.get("reference_match", {}).get("metrics", {})
    layout_gate_names = (
        "unresolved_pass",
        "geometry_pass",
        "overlap_pass",
        "support_pass",
        "facing_pass",
    )
    layout_score = sum(bool(gates.get(name)) for name in layout_gate_names) / len(layout_gate_names)
    exact_score = coverage["licensed_exact_anchor_coverage"]
    asset_score = sum((
        float(quality["asset_quality"]["anchor_real_asset_coverage"]),
        float(gates["proxy_free_pass"]),
        float(coverage["semantic_anchor_coverage"]),
        float(exact_score),
        float(not declared_asset_gaps),
    )) / 5.0
    layer_scores = {
        "layout": round(layout_score, 6),
        "asset": round(asset_score, 6),
        "material": round(float(metrics.get("color_histogram_similarity", 0.0)), 6),
        "camera": round(sum((
            float(metrics.get("spatial_composition_similarity", 0.0)),
            float(metrics.get("content_aspect_ratio_similarity", 0.0)),
        )) / 2.0, 6),
        "visual": round(float(quality["reference_match"].get("score", 0.0)), 6),
    }
    missing_artifacts = [
        str(path) for path in paths.values() if not path.is_file()
    ]
    status = "pass" if not missing_artifacts and all(gates.values()) else "fail"
    return {
        "slug": case["slug"],
        "domain": case["domain"],
        "mode": mode,
        "artifact_set": artifact_set,
        "status": status,
        "paths": {key: str(value) for key, value in paths.items()},
        "missing_artifacts": missing_artifacts,
        "gates": gates,
        "coverage": coverage,
        "declared_asset_gaps": declared_asset_gaps,
        "layer_scores": layer_scores,
        "quality": quality,
    }


def _markdown(report: dict[str, Any]) -> str:
    aggregate = report["aggregate"]
    lines = [
        "# Synthesis-27 regression report",
        "",
        f"- Artifact set: {report['artifact_set']}",
        f"- Mode: {report['mode']}",
        f"- Cases: {aggregate['case_count']}",
        f"- Pass / fail: {aggregate['pass_count']} / {aggregate['fail_count']}",
        f"- Mean reference score: {aggregate['mean_reference_match_score']:.3f}",
        f"- Mean semantic anchor coverage: {aggregate['mean_semantic_anchor_coverage']:.3f}",
        f"- Mean licensed exact-anchor coverage: {aggregate['mean_licensed_exact_anchor_coverage']:.3f}",
        f"- Declared asset gaps: {aggregate['declared_asset_gap_count']}",
        "",
        "| Scene | Status | Layout | Asset | Material | Camera | Visual | Gaps |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in report["cases"]:
        lines.append(
            f"| {row['slug']} | {row['status']} "
            f"| {row['layer_scores']['layout']:.3f} "
            f"| {row['layer_scores']['asset']:.3f} "
            f"| {row['layer_scores']['material']:.3f} "
            f"| {row['layer_scores']['camera']:.3f} "
            f"| {row['layer_scores']['visual']:.3f} "
            f"| {len(row['declared_asset_gaps'])} |"
        )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate Synthesis-27 outputs against leakage-resistant scene contracts."
    )
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--dataset-root", type=Path, required=True)
    parser.add_argument("--artifact-set", choices=("baseline", "candidate"), default="candidate")
    parser.add_argument("--mode", choices=("text_only", "reference_guided"), default="reference_guided")
    parser.add_argument(
        "--slug",
        action="append",
        default=[],
        help="Evaluate only this scene slug; repeat for a representative subset.",
    )
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--output-markdown", type=Path)
    args = parser.parse_args()
    contract = _read_json(args.contract)
    selected_slugs = set(args.slug)
    selected_cases = [
        case for case in contract.get("cases", [])
        if not selected_slugs or str(case.get("slug")) in selected_slugs
    ]
    missing_slugs = selected_slugs - {str(case.get("slug")) for case in selected_cases}
    if missing_slugs:
        parser.error("unknown slug(s): " + ", ".join(sorted(missing_slugs)))
    rows = [
        evaluate_case(
            case,
            dataset_root=args.dataset_root,
            artifact_set=args.artifact_set,
            mode=args.mode,
        )
        for case in selected_cases
    ]
    count = len(rows)
    aggregate = {
        "case_count": count,
        "pass_count": sum(row["status"] == "pass" for row in rows),
        "fail_count": sum(row["status"] == "fail" for row in rows),
        "mean_reference_match_score": (
            sum(row["quality"]["reference_match"].get("score", 0.0) for row in rows) / count
            if count else 0.0
        ),
        "mean_semantic_anchor_coverage": (
            sum(row["coverage"]["semantic_anchor_coverage"] for row in rows) / count
            if count else 0.0
        ),
        "mean_licensed_exact_anchor_coverage": (
            sum(row["coverage"]["licensed_exact_anchor_coverage"] for row in rows) / count
            if count else 0.0
        ),
        "declared_asset_gap_count": sum(len(row["declared_asset_gaps"]) for row in rows),
        "mean_layer_scores": {
            layer: (
                sum(row["layer_scores"][layer] for row in rows) / count if count else 0.0
            )
            for layer in ("layout", "asset", "material", "camera", "visual")
        },
    }
    report = {
        "schema_version": "synthesis27-regression-report/v1",
        "source_contract_sha256": contract.get("source_snapshot_sha256"),
        "artifact_set": args.artifact_set,
        "mode": args.mode,
        "aggregate": aggregate,
        "cases": rows,
    }
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    if args.output_markdown:
        args.output_markdown.parent.mkdir(parents=True, exist_ok=True)
        args.output_markdown.write_text(_markdown(report), encoding="utf-8")
    print(json.dumps(aggregate, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
