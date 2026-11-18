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

from isaac_scene.quality import benchmark_reuse_metrics, evaluate_scene  # noqa: E402


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return value


def _catalog_cases(catalog: dict[str, Any], dataset_root: Path) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for row in catalog.get("scenes", []):
        if not isinstance(row, dict):
            continue
        slug = str(row["slug"])
        scene_root = dataset_root / slug
        cases.append({
            **row,
            "reference_image": scene_root / str(row.get("reference_image", "reference.jpg")),
            "rendered_image": scene_root / str(row.get("rendered_image", "agent_output/render_perspective.png")),
            "scene_spec": scene_root / str(row.get("scene_spec", "agent_output/scene_spec.json")),
            "summary": scene_root / str(row.get("summary", "agent_output/summary.json")),
        })
    return cases


def _external_manifest_cases(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for row in manifest.get("scenes", []):
        if not isinstance(row, dict):
            continue
        output = Path(str(row["output_dir"]))
        cases.append({
            **row,
            "reference_image": Path(str(row["reference_image"])),
            "rendered_image": output / "render_perspective.png",
            "scene_spec": output / "scene_spec.json",
            "summary": output / "summary.json",
        })
    return cases


def _markdown(report: dict[str, Any]) -> str:
    aggregate = report["aggregate"]
    lines = [
        "# Scene benchmark report",
        "",
        f"- Cases: {aggregate['case_count']}",
        f"- Quality pass / warn / fail: {aggregate['status_counts'].get('pass', 0)} / "
        f"{aggregate['status_counts'].get('warn', 0)} / {aggregate['status_counts'].get('fail', 0)}",
        f"- Mean reference-match score: {aggregate['mean_reference_match_score']:.3f}",
        f"- Mean proxy ratio: {aggregate['mean_proxy_ratio']:.3f}",
        f"- Mean anchor real-asset coverage: {aggregate['mean_anchor_real_asset_coverage']:.3f}",
        f"- Cross-scene asset reuse ratio: {report['asset_reuse']['cross_scene_reuse_ratio']:.3f}",
        "",
        "## Failure distribution",
        "",
    ]
    for code, count in aggregate.get("failure_counts", {}).items():
        lines.append(f"- `{code}`: {count}")
    lines.extend([
        "",
        "| Scene | Status | Ref match | Proxy | Anchor real | Asset diversity |",
        "|---|---:|---:|---:|---:|---:|",
    ])
    for row in report["scenes"]:
        quality = row["quality"]
        asset = quality["asset_quality"]
        lines.append(
            f"| {row['slug']} | {quality['status']} | {quality['reference_match'].get('score', 0.0):.3f} "
            f"| {asset['proxy_ratio']:.3f} | {asset['anchor_real_asset_coverage']:.3f} "
            f"| {asset['asset_diversity']:.3f} |"
        )
    lines.extend(["", "This report separates structural validity from reference similarity; a USD can be structurally valid and still fail the visual gate.", ""])
    return "\n".join(lines)


def _remediation(quality: dict[str, Any]) -> list[str]:
    asset = quality["asset_quality"]
    image = quality["reference_match"]
    actions: list[str] = []
    if not asset.get("anchor_definition_complete", True):
        actions.append("declare anchor criticality/roles before evaluating real-asset coverage")
    if asset.get("proxy_count", 0):
        actions.append(f"replace {asset['proxy_count']} visible proxy entities with verified assets")
    if asset.get("anchor_real_asset_coverage", 1.0) < 1.0:
        actions.append("replace authored/proxy anchors with verified real assets")
    if asset.get("retrieved_count", 0) > 1 and asset.get("asset_diversity", 1.0) < 0.5:
        actions.append("broaden semantic/visual retrieval to reduce repeated-asset collapse")
    if image.get("score", 0.0) < 0.72:
        actions.append("repair fixed-camera composition, anchor silhouette, materials and palette")
    return actions


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate a scene-generation benchmark without importing source USD assets.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--catalog", type=Path, help="Portable benchmark catalog with relative paths.")
    source.add_argument("--manifest", type=Path, help="Existing generation manifest with absolute paths.")
    parser.add_argument("--dataset-root", type=Path, default=None, help="Required with --catalog unless SYNTHESIS27_ROOT is set.")
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--output-markdown", type=Path, default=None)
    args = parser.parse_args()

    if args.catalog:
        import os

        root = args.dataset_root or (Path(os.environ["SYNTHESIS27_ROOT"]) if os.environ.get("SYNTHESIS27_ROOT") else None)
        if root is None:
            parser.error("--dataset-root or SYNTHESIS27_ROOT is required with --catalog")
        cases = _catalog_cases(_read_json(args.catalog), root)
    else:
        cases = _external_manifest_cases(_read_json(args.manifest))

    scene_rows: list[dict[str, Any]] = []
    for case in cases:
        quality = evaluate_scene(
            reference_image=case["reference_image"],
            rendered_image=case["rendered_image"],
            scene_spec=case["scene_spec"],
            summary=case["summary"],
        )
        scene_rows.append({
            "id": case.get("id"),
            "slug": case.get("slug"),
            "name": case.get("name"),
            "category": case.get("category"),
            "quality": quality,
            "remediation": _remediation(quality),
        })

    statuses = Counter(row["quality"]["status"] for row in scene_rows)
    failure_counts = Counter(
        failure["code"]
        for row in scene_rows
        for failure in row["quality"].get("failures", [])
    )
    warning_counts = Counter(
        warning["code"]
        for row in scene_rows
        for warning in row["quality"].get("warnings", [])
    )
    count = len(scene_rows)
    aggregate = {
        "case_count": count,
        "status_counts": dict(statuses),
        "mean_reference_match_score": sum(row["quality"]["reference_match"].get("score", 0.0) for row in scene_rows) / count if count else 0.0,
        "mean_proxy_ratio": sum(row["quality"]["asset_quality"]["proxy_ratio"] for row in scene_rows) / count if count else 0.0,
        "mean_anchor_real_asset_coverage": sum(row["quality"]["asset_quality"]["anchor_real_asset_coverage"] for row in scene_rows) / count if count else 0.0,
        "failure_counts": dict(failure_counts.most_common()),
        "warning_counts": dict(warning_counts.most_common()),
    }
    report = {
        "schema_version": "isaac-scene-benchmark/v1",
        "aggregate": aggregate,
        "asset_reuse": benchmark_reuse_metrics(row["quality"] for row in scene_rows),
        "scenes": scene_rows,
    }
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.output_markdown:
        args.output_markdown.parent.mkdir(parents=True, exist_ok=True)
        args.output_markdown.write_text(_markdown(report), encoding="utf-8")
    print(json.dumps(aggregate, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
