#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from isaac_scene.visual_catalog import search_embeddings  # noqa: E402


FRONT_AXIS_TO_VIEW = {
    "+X": "pos_x",
    "-X": "neg_x",
    "+Y": "pos_y",
    "-Y": "neg_y",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Search persistent USD preview embeddings.")
    parser.add_argument("--visual-index", type=Path, required=True)
    parser.add_argument("--semantic-catalog", type=Path, default=None)
    parser.add_argument("--query", required=True)
    parser.add_argument("--reference-crop", type=Path, default=None)
    parser.add_argument("--canonical-class", default=None)
    parser.add_argument(
        "--fallback-class",
        action="append",
        default=[],
        help="Explicit additional semantic class to include in one visual comparison pool.",
    )
    parser.add_argument("--min-size-m", nargs=3, type=float, default=None)
    parser.add_argument("--max-size-m", nargs=3, type=float, default=None)
    parser.add_argument("--target-size-m", nargs=3, type=float, default=None)
    parser.add_argument("--size-weight", type=float, default=0.20)
    parser.add_argument("--include-unavailable", action="store_true")
    parser.add_argument("--model", default="openai/clip-vit-base-patch32")
    parser.add_argument("--device", default="auto")
    parser.add_argument("--text-weight", type=float, default=0.55)
    parser.add_argument("--reference-weight", type=float, default=0.45)
    parser.add_argument("--topk", type=int, default=20)
    return parser.parse_args()


def bbox_size_meters(
    bbox_json: str | None,
    meters_per_unit: float | None,
) -> list[float] | None:
    if not bbox_json:
        return None
    bbox = json.loads(bbox_json)
    # Agent annotations commonly persist an already-normalized
    # [x, y, z] size in meters. Preview manifests instead persist a richer
    # object with size/size_meters. Visual retrieval must accept both catalog
    # representations rather than crashing after a valid annotation.
    if isinstance(bbox, list) and len(bbox) == 3:
        return [float(value) for value in bbox]
    if not isinstance(bbox, dict):
        return None
    size_meters = bbox.get("size_meters")
    if isinstance(size_meters, list) and len(size_meters) == 3:
        return [float(value) for value in size_meters]
    size = bbox.get("size")
    if isinstance(size, list) and len(size) == 3 and meters_per_unit is not None:
        return [float(value) * float(meters_per_unit) for value in size]
    return None


def dimension_similarity(
    actual_size_m: list[float] | None,
    target_size_m: list[float] | None,
) -> float | None:
    if actual_size_m is None or target_size_m is None:
        return None
    actual = [*sorted(float(value) for value in actual_size_m[:2]), float(actual_size_m[2])]
    target = [*sorted(float(value) for value in target_size_m[:2]), float(target_size_m[2])]
    if any(value <= 0 for value in [*actual, *target]):
        return None
    mean_log_error = sum(abs(math.log(a / b)) for a, b in zip(actual, target)) / 3.0
    return math.exp(-mean_log_error)


def semantic_candidate_paths(
    args: argparse.Namespace,
    *,
    apply_size_filters: bool = True,
) -> set[str] | None:
    if not args.semantic_catalog:
        return None
    connection = sqlite3.connect(f"file:{args.semantic_catalog}?mode=ro", uri=True)
    filters = ["source_path IS NOT NULL"]
    params: list[object] = []
    if not args.include_unavailable:
        filters.append("callable=1")
    classes = list(dict.fromkeys([
        *([str(args.canonical_class)] if args.canonical_class else []),
        *(str(value) for value in getattr(args, "fallback_class", []) or []),
    ]))
    if classes:
        filters.append(
            "(" + " OR ".join("canonical_class LIKE ?" for _value in classes) + ")"
        )
        params.extend(value.rstrip("/") + "%" for value in classes)
    paths: set[str] = set()
    for row in connection.execute(
        f"""SELECT source_path, bbox_json, meters_per_unit
            FROM semantic_assets WHERE {' AND '.join(filters)}""",
        params,
    ):
        size = bbox_size_meters(row[1], row[2])
        if apply_size_filters and args.min_size_m is not None:
            if size is None or any(value < bound for value, bound in zip(size, args.min_size_m)):
                continue
        if apply_size_filters and args.max_size_m is not None:
            if size is None or any(value > bound for value, bound in zip(size, args.max_size_m)):
                continue
        paths.add(str(row[0]))
    connection.close()
    return paths


def semantic_metadata_by_path(args: argparse.Namespace) -> dict[str, dict[str, object]]:
    if not args.semantic_catalog:
        return {}
    connection = sqlite3.connect(f"file:{args.semantic_catalog}?mode=ro", uri=True)
    filters = ["source_path IS NOT NULL"]
    params: list[object] = []
    if not args.include_unavailable:
        filters.append("callable=1")
    classes = list(dict.fromkeys([
        *([str(args.canonical_class)] if args.canonical_class else []),
        *(str(value) for value in getattr(args, "fallback_class", []) or []),
    ]))
    if classes:
        filters.append(
            "(" + " OR ".join("canonical_class LIKE ?" for _value in classes) + ")"
        )
        params.extend(value.rstrip("/") + "%" for value in classes)
    result: dict[str, dict[str, object]] = {}
    for path, canonical_class, subtype, semantic_name, front_axis, metadata_json in connection.execute(
            f"""SELECT source_path, canonical_class, subtype, semantic_name,
                       front_axis, metadata_json
                FROM semantic_assets WHERE {' AND '.join(filters)}""",
            params,
        ):
        metadata = json.loads(metadata_json or "{}")
        metadata.update({
            "canonical_class": canonical_class,
            "subtype": subtype,
            "semantic_name": semantic_name,
            "front_axis": front_axis,
        })
        result[str(path)] = metadata
    connection.close()
    return result


def preview_candidate_count(
    visual_index: str | Path,
    *,
    model_id: str,
    candidate_paths: set[str] | None,
) -> int:
    connection = sqlite3.connect(f"file:{Path(visual_index)}?mode=ro", uri=True)
    rows = connection.execute(
        """SELECT DISTINCT a.source_path
           FROM visual_views v JOIN visual_assets a ON a.asset_id=v.asset_id
           WHERE v.model_id=?""",
        (model_id,),
    )
    count = sum(
        1
        for (source_path,) in rows
        if candidate_paths is None or str(source_path) in candidate_paths
    )
    connection.close()
    return count


def main() -> int:
    args = parse_args()
    import torch
    from PIL import Image
    from transformers import CLIPModel, CLIPProcessor

    if not args.visual_index.is_file():
        raise SystemExit(f"Visual index not found: {args.visual_index}")
    device = args.device
    if device == "auto":
        device = "cuda" if torch.cuda.is_available() else "cpu"
    model = CLIPModel.from_pretrained(args.model).to(device).eval()
    processor = CLIPProcessor.from_pretrained(args.model)
    embeddings: dict[str, list[float]] = {}
    with torch.inference_mode():
        text_inputs = processor(text=[args.query], return_tensors="pt", padding=True).to(device)
        text = model.get_text_features(**text_inputs)
        text = text / text.norm(dim=-1, keepdim=True)
        embeddings["text"] = text[0].detach().cpu().tolist()
        if args.reference_crop:
            reference = Image.open(args.reference_crop).convert("RGB")
            image_inputs = processor(images=[reference], return_tensors="pt").to(device)
            image = model.get_image_features(**image_inputs)
            image = image / image.norm(dim=-1, keepdim=True)
            embeddings["reference"] = image[0].detach().cpu().tolist()
    weights = {
        "text": args.text_weight,
        "reference": args.reference_weight if args.reference_crop else 0.0,
    }
    filtered_paths = semantic_candidate_paths(args)
    class_paths = semantic_candidate_paths(args, apply_size_filters=False)
    metadata_by_path = semantic_metadata_by_path(args)
    preferred_view_by_path = {
        path: FRONT_AXIS_TO_VIEW[str(metadata["front_axis"])]
        for path, metadata in metadata_by_path.items()
        if metadata.get("front_axis") in FRONT_AXIS_TO_VIEW
    }
    all_ranking = search_embeddings(
        args.visual_index,
        model_id=args.model,
        query_embeddings=embeddings,
        weights=weights,
        topk=200,
        candidate_paths=filtered_paths,
        preferred_view_by_path=preferred_view_by_path,
    )
    for row in all_ranking:
        semantic_metadata = metadata_by_path.get(str(row["path"]), {})
        row["equivalence_group"] = semantic_metadata.get("equivalence_group")
        if semantic_metadata:
            # The semantic catalog is authoritative. The visual table may have
            # been built before a QA-driven reclassification.
            row["category"] = semantic_metadata.get("canonical_class")
            row["subtype"] = semantic_metadata.get("subtype")
            row["semantic_name"] = semantic_metadata.get("semantic_name")
            row["front_axis"] = semantic_metadata.get("front_axis")
    size_weight = min(max(float(args.size_weight), 0.0), 1.0) if args.target_size_m else 0.0
    if args.target_size_m:
        for row in all_ranking:
            bbox = row.get("bbox") or {}
            size = bbox.get("size_meters")
            similarity = dimension_similarity(size, args.target_size_m)
            visual_score = float(row["score"])
            row["components"]["visual_combined"] = round(visual_score, 6)
            row["components"]["size"] = round(float(similarity), 6) if similarity is not None else 0.0
            row["score"] = round(
                (1.0 - size_weight) * visual_score + size_weight * float(similarity or 0.0),
                6,
            )
        all_ranking.sort(key=lambda item: (-float(item["score"]), str(item["asset_id"])))
    ranking = all_ranking[: max(1, min(args.topk, 200))]
    print(json.dumps({
        "schema_version": "isaac-visual-asset-ranking/v2",
        "query": args.query,
        "reference_crop": str(args.reference_crop) if args.reference_crop else None,
        "canonical_class": args.canonical_class,
        "fallback_classes": list(dict.fromkeys(str(value) for value in args.fallback_class)),
        "size_filters_m": {
            "min": args.min_size_m,
            "max": args.max_size_m,
            "target": args.target_size_m,
            "weight": size_weight,
        },
        "model": args.model,
        "device": device,
        "weights": weights,
        "candidate_count": len(all_ranking),
        "candidate_pool_count": preview_candidate_count(
            args.visual_index,
            model_id=args.model,
            candidate_paths=class_paths,
        ),
        "candidate_inventory_count": len(class_paths) if class_paths is not None else None,
        "verified_front_restricted_count": len(preferred_view_by_path),
        "ranking": ranking,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
