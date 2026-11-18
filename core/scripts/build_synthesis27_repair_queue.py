#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from isaac_scene.quality import classify_entity, is_anchor_entity  # noqa: E402
from isaac_scene.semantic_catalog import classify_semantics, normalize_semantic_name  # noqa: E402


NON_OBJECT_CLASSES = {
    "environment/scene",
    "environment/sample_scene",
    "architecture/room_shell",
    "architecture/surface/floor",
    "architecture/surface/wall",
    "architecture/surface/ceiling",
}


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else {}


def _tokens(value: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", normalize_semantic_name(value)))


def _similarity(first: str, second: str) -> float:
    a = _tokens(first)
    b = _tokens(second)
    return len(a & b) / len(a | b) if a and b else 0.0


def _entity_label(entity: dict[str, Any]) -> str:
    return " ".join(
        str(entity.get(key) or "")
        for key in ("semantic_name", "role", "category", "id")
    ).strip()


def _generated_gaps(spec: dict[str, Any]) -> list[dict[str, Any]]:
    entities = spec.get("entities")
    if not isinstance(entities, list):
        entities = spec.get("references", [])
    gaps: list[dict[str, Any]] = []
    for entity in entities:
        if not isinstance(entity, dict):
            continue
        asset_kind = classify_entity(entity)
        if asset_kind in {"retrieved", "shell"}:
            continue
        label = _entity_label(entity)
        canonical_class, subtype = classify_semantics(
            label,
            str(entity.get("category") or entity.get("semantic_class") or ""),
            "object",
        )
        gaps.append({
            "entity_id": str(entity.get("id") or entity.get("name") or ""),
            "role": str(entity.get("role") or ""),
            "semantic_name": str(entity.get("semantic_name") or ""),
            "canonical_class": canonical_class,
            "subtype": subtype,
            "asset_kind": asset_kind,
            "is_anchor": is_anchor_entity(entity),
            "criticality": str(entity.get("criticality") or ""),
            "size_m": entity.get("size") or entity.get("size_m") or entity.get("layout_hint", {}).get("size_m"),
            "visual": entity.get("visual", {}),
        })
    return gaps


def _declared_asset_gaps(spec: dict[str, Any]) -> list[dict[str, Any]]:
    gaps: list[dict[str, Any]] = []
    for index, row in enumerate(spec.get("asset_gaps", [])):
        if not isinstance(row, dict):
            continue
        canonical_class = str(row.get("canonical_class") or "").strip()
        if not canonical_class:
            continue
        gaps.append({
            "entity_id": str(row.get("entity_id") or f"declared_gap_{index + 1}"),
            "role": str(row.get("role") or "missing_reference_object"),
            "semantic_name": str(row.get("semantic_name") or canonical_class.rsplit("/", 1)[-1].replace("_", " ")),
            "canonical_class": canonical_class,
            "subtype": str(row.get("subtype") or canonical_class.rsplit("/", 1)[-1]),
            "asset_kind": "missing",
            "is_anchor": str(row.get("criticality") or "").lower() == "anchor",
            "criticality": str(row.get("criticality") or "major"),
            "size_m": row.get("size_m"),
            "visual": row.get("visual", {}),
            "reason": str(row.get("reason") or ""),
            "source": "scene_spec.asset_gaps",
        })
    return gaps


def _source_models(info: dict[str, Any]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for model in info.get("models", []):
        if not isinstance(model, dict):
            continue
        model_id = str(model.get("modelID") or model.get("vaultID") or "")
        name = str(model.get("name") or model_id)
        if not model_id:
            continue
        canonical_class, subtype = classify_semantics(name)
        if canonical_class in NON_OBJECT_CLASSES:
            continue
        row = grouped.setdefault(model_id, {
            "model_id": model_id,
            "names": Counter(),
            "canonical_class": canonical_class,
            "subtype": subtype,
            "server": model.get("server"),
            "instances": [],
        })
        row["names"][name] += 1
        row["instances"].append({
            "name": name,
            "offset": model.get("offset"),
            "rotation": model.get("rotation"),
            "scale": model.get("scale"),
        })
    rows: list[dict[str, Any]] = []
    for row in grouped.values():
        names: Counter[str] = row.pop("names")
        primary_name = names.most_common(1)[0][0]
        rows.append({
            **row,
            "semantic_name": normalize_semantic_name(primary_name),
            "aliases": sorted(names),
            "usage_count": len(row["instances"]),
            "callable": False,
            "availability": "synthesis_metadata_only",
        })
    return rows


def _match_source_candidates(
    gap: dict[str, Any],
    source_models: list[dict[str, Any]],
    acquisition_by_id: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    query = " ".join((gap["semantic_name"], gap["role"], gap["entity_id"]))
    ranked: list[tuple[float, dict[str, Any]]] = []
    for model in source_models:
        class_match = model["canonical_class"] == gap["canonical_class"]
        prefix_match = (
            model["canonical_class"].split("/", 1)[0]
            == gap["canonical_class"].split("/", 1)[0]
        )
        lexical = _similarity(query, " ".join([model["semantic_name"], *model["aliases"]]))
        score = (0.65 if class_match else (0.20 if prefix_match else 0.0)) + 0.35 * lexical
        if score <= 0.0:
            continue
        ranked.append((score, model))
    ranked.sort(key=lambda item: (-item[0], -item[1]["usage_count"], item[1]["model_id"]))
    candidates: list[dict[str, Any]] = []
    for score, model in ranked[:5]:
        acquisition = (acquisition_by_id or {}).get(model["model_id"], {})
        candidates.append({
            "model_id": model["model_id"],
            "semantic_name": model["semantic_name"],
            "canonical_class": model["canonical_class"],
            "usage_count": model["usage_count"],
            "server": model["server"],
            "callable": bool(model.get("callable", False)),
            "availability": str(model.get("availability") or "synthesis_metadata_only"),
            "match_score": round(score, 6),
            "remote_usd_available": bool(acquisition.get("remote_usd_available", False)),
            "acquisition_priority": acquisition.get("priority"),
        })
    return candidates


def _catalog_source_models(path: Path, scene_slug: str) -> list[dict[str, Any]]:
    uri = f"file:{path.resolve()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT model_id, semantic_name, aliases_json, canonical_class, subtype,
                   usage_count, callable, availability, source_path
              FROM objects
             WHERE scene_slugs_json LIKE ?
             ORDER BY callable DESC, usage_count DESC, model_id
            """,
            (f'%"{scene_slug}"%',),
        ).fetchall()
    finally:
        connection.close()
    models: list[dict[str, Any]] = []
    for row in rows:
        try:
            aliases = json.loads(str(row["aliases_json"] or "[]"))
        except json.JSONDecodeError:
            aliases = []
        models.append({
            "model_id": str(row["model_id"]),
            "semantic_name": str(row["semantic_name"]),
            "aliases": [str(value) for value in aliases],
            "canonical_class": str(row["canonical_class"]),
            "subtype": str(row["subtype"]),
            "usage_count": int(row["usage_count"] or 0),
            "server": row["source_path"],
            "callable": bool(row["callable"]),
            "availability": str(row["availability"] or "synthesis_metadata_only"),
        })
    return models


def _acquisition_index(plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    available = set()
    for collection in plan.get("scene_collections", {}).values():
        if not isinstance(collection, dict):
            continue
        for asset in collection.get("assets", []):
            if not isinstance(asset, dict):
                continue
            model_id = str(asset.get("model_id") or "")
            if not model_id:
                continue
            available.add(model_id)
    all_remote_available = (
        int(plan.get("asset_count", 0)) > 0
        and int(plan.get("availability_counts", {}).get("remote_usd_available", 0))
        == int(plan.get("asset_count", 0))
    )
    return {
        model_id: {
            "remote_usd_available": all_remote_available,
            "priority": next(
                (
                    asset.get("priority")
                    for collection in plan.get("scene_collections", {}).values()
                    if isinstance(collection, dict)
                    for asset in collection.get("assets", [])
                    if isinstance(asset, dict) and str(asset.get("model_id") or "") == model_id
                ),
                None,
            ),
        }
        for model_id in available
    }


def _markdown(report: dict[str, Any]) -> str:
    aggregate = report["aggregate"]
    lines = [
        "# Synthesis-27 repair queue",
        "",
        f"- Scenes: {aggregate['scene_count']}",
        f"- Generated asset gaps: {aggregate['gap_entity_count']}",
        f"- Anchor gaps: {aggregate['anchor_gap_count']}",
        f"- Synthesis metadata-only object models: {aggregate['source_model_count']}",
        "",
        "## Highest-priority scenes",
        "",
        "| Scene | Ref score | Proxy | Anchor real | Gaps | Priority |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for scene in report["scenes"]:
        lines.append(
            f"| {scene['slug']} | {scene['reference_match_score']:.3f} | "
            f"{scene['proxy_ratio']:.3f} | {scene['anchor_real_asset_coverage']:.3f} | "
            f"{len(scene['gaps'])} | {scene['repair_priority']:.3f} |"
        )
    lines.extend([
        "",
        "## Cross-scene semantic demand",
        "",
        "| Canonical class | Gap entities | Anchor gaps | Scenes | Exact Synthesis candidates |",
        "|---|---:|---:|---:|---:|",
    ])
    for row in report["semantic_demand"]:
        lines.append(
            f"| {row['canonical_class']} | {row['gap_entity_count']} | "
            f"{row['anchor_gap_count']} | {row['scene_count']} | "
            f"{len(row['synthesis_model_ids'])} |"
        )
    lines.extend([
        "",
        "Synthesis candidates are metadata-only until their individual packages are acquired, dependency-normalized, previewed, licensed and validated in Isaac Sim.",
        "",
    ])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a semantics-first repair and exact-asset acquisition queue for Synthesis-27.")
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--dataset-root", type=Path, required=True)
    parser.add_argument("--quality-report", type=Path, required=True)
    parser.add_argument("--acquisition-plan", type=Path)
    parser.add_argument("--object-catalog", type=Path)
    parser.add_argument("--slug", action="append", default=[])
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--output-markdown", type=Path)
    args = parser.parse_args()

    catalog = _read_json(args.catalog)
    quality_report = _read_json(args.quality_report)
    acquisition_by_id = _acquisition_index(
        _read_json(args.acquisition_plan) if args.acquisition_plan else {}
    )
    quality_rows = quality_report.get("scenes") or quality_report.get("cases") or []
    quality_by_slug = {
        str(row.get("slug")): row
        for row in quality_rows
        if isinstance(row, dict)
    }
    scenes: list[dict[str, Any]] = []
    demand: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "gap_entity_count": 0,
        "anchor_gap_count": 0,
        "scenes": set(),
        "entity_labels": set(),
        "synthesis_model_ids": set(),
    })
    source_model_ids: set[str] = set()
    selected_slugs = set(args.slug)
    for case in catalog.get("scenes", []):
        if not isinstance(case, dict):
            continue
        slug = str(case["slug"])
        if selected_slugs and slug not in selected_slugs:
            continue
        root = args.dataset_root / slug
        quality_row = quality_by_slug.get(slug, {})
        reported_spec = quality_row.get("paths", {}).get("scene_spec")
        spec_path = Path(reported_spec) if reported_spec else root / str(case.get("scene_spec", "agent_output/scene_spec.json"))
        spec = _read_json(spec_path)
        source_models = (
            _catalog_source_models(args.object_catalog, slug)
            if args.object_catalog
            else _source_models(_read_json(root / "scene_info.json"))
        )
        source_model_ids.update(model["model_id"] for model in source_models)
        gaps = _generated_gaps(spec) + _declared_asset_gaps(spec)
        for gap in gaps:
            gap["exact_synthesis_candidates"] = _match_source_candidates(
                gap,
                source_models,
                acquisition_by_id,
            )
            row = demand[gap["canonical_class"]]
            row["gap_entity_count"] += 1
            row["anchor_gap_count"] += int(gap["is_anchor"])
            row["scenes"].add(slug)
            row["entity_labels"].add(gap["semantic_name"] or gap["role"] or gap["entity_id"])
            row["synthesis_model_ids"].update(
                candidate["model_id"] for candidate in gap["exact_synthesis_candidates"]
            )
        quality = quality_row.get("quality", quality_row)
        asset = quality.get("asset_quality", {})
        image = quality.get("reference_match", {})
        score = float(image.get("score", 0.0))
        proxy_ratio = float(asset.get("proxy_ratio", 0.0))
        anchor_coverage = float(asset.get("anchor_real_asset_coverage", 0.0))
        priority = (
            4.0 * proxy_ratio
            + 3.0 * (1.0 - anchor_coverage)
            + 2.0 * max(0.0, 0.72 - score)
            + 0.05 * len(gaps)
        )
        scenes.append({
            "slug": slug,
            "category": case.get("category"),
            "reference_match_score": score,
            "proxy_ratio": proxy_ratio,
            "anchor_real_asset_coverage": anchor_coverage,
            "repair_priority": round(priority, 6),
            "gaps": gaps,
            "source_model_count": len(source_models),
        })
    scenes.sort(key=lambda row: (-row["repair_priority"], row["slug"]))
    semantic_demand = [
        {
            "canonical_class": canonical_class,
            "gap_entity_count": row["gap_entity_count"],
            "anchor_gap_count": row["anchor_gap_count"],
            "scene_count": len(row["scenes"]),
            "scenes": sorted(row["scenes"]),
            "entity_labels": sorted(row["entity_labels"]),
            "synthesis_model_ids": sorted(row["synthesis_model_ids"]),
        }
        for canonical_class, row in demand.items()
    ]
    semantic_demand.sort(key=lambda row: (-row["anchor_gap_count"], -row["gap_entity_count"], row["canonical_class"]))
    report = {
        "schema_version": "synthesis27-repair-queue/v1",
        "source_policy": catalog.get("source_policy"),
        "aggregate": {
            "scene_count": len(scenes),
            "gap_entity_count": sum(len(scene["gaps"]) for scene in scenes),
            "anchor_gap_count": sum(
                int(gap["is_anchor"])
                for scene in scenes
                for gap in scene["gaps"]
            ),
            "source_model_count": len(source_model_ids),
            "remote_usd_available_count": len(source_model_ids & set(acquisition_by_id)),
        },
        "scenes": scenes,
        "semantic_demand": semantic_demand,
    }
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.output_markdown:
        args.output_markdown.parent.mkdir(parents=True, exist_ok=True)
        args.output_markdown.write_text(_markdown(report), encoding="utf-8")
    print(json.dumps(report["aggregate"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
