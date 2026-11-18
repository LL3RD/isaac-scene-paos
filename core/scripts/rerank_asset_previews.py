#!/usr/bin/env python3
"""Rerank rendered USD candidate previews with CLIP text and reference-crop similarity."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--preview-index", type=Path, required=True)
    parser.add_argument("--query", required=True, help="Use a concise English visual description for CLIP.")
    parser.add_argument("--reference-crop", type=Path, default=None)
    parser.add_argument("--model", default="openai/clip-vit-base-patch32")
    parser.add_argument("--device", default="auto")
    parser.add_argument("--text-weight", type=float, default=0.55)
    parser.add_argument("--reference-weight", type=float, default=0.45)
    parser.add_argument("--topk", type=int, default=20)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def _resolve_preview(index_path: Path, value: str) -> Path:
    path = Path(value)
    if path.is_file():
        return path
    candidate = index_path.parent / path.name
    if candidate.is_file():
        return candidate
    parts = path.parts
    if len(parts) >= 2:
        candidate = index_path.parent / parts[-2] / parts[-1]
        if candidate.is_file():
            return candidate
    return path


def main() -> int:
    args = parse_args()
    import torch
    from PIL import Image
    from transformers import CLIPModel, CLIPProcessor

    index = json.loads(args.preview_index.read_text(encoding="utf-8"))
    assets = [row for row in index.get("assets", []) if row.get("status") == "pass" and row.get("previews")]
    if not assets:
        raise SystemExit("No successful previews in index")
    device = args.device
    if device == "auto":
        device = "cuda" if torch.cuda.is_available() else "cpu"
    model = CLIPModel.from_pretrained(args.model).to(device).eval()
    processor = CLIPProcessor.from_pretrained(args.model)

    image_rows: list[dict[str, Any]] = []
    images = []
    for asset in assets:
        for view, raw_path in asset["previews"].items():
            preview = _resolve_preview(args.preview_index, str(raw_path))
            if not preview.is_file():
                continue
            image_rows.append({"asset": asset, "view": view, "preview": str(preview)})
            images.append(Image.open(preview).convert("RGB"))
    if not images:
        raise SystemExit("Preview image files could not be resolved")

    with torch.inference_mode():
        image_inputs = processor(images=images, return_tensors="pt").to(device)
        image_features = model.get_image_features(**image_inputs)
        image_features = image_features / image_features.norm(dim=-1, keepdim=True)
        text_inputs = processor(text=[args.query], return_tensors="pt", padding=True).to(device)
        text_features = model.get_text_features(**text_inputs)
        text_features = text_features / text_features.norm(dim=-1, keepdim=True)
        text_scores = (image_features @ text_features.T).squeeze(-1)
        reference_scores = None
        if args.reference_crop:
            reference_image = Image.open(args.reference_crop).convert("RGB")
            reference_inputs = processor(images=[reference_image], return_tensors="pt").to(device)
            reference_features = model.get_image_features(**reference_inputs)
            reference_features = reference_features / reference_features.norm(dim=-1, keepdim=True)
            reference_scores = (image_features @ reference_features.T).squeeze(-1)

    by_asset: dict[str, dict[str, Any]] = {}
    for index_number, image_row in enumerate(image_rows):
        asset = image_row["asset"]
        asset_id = str(asset.get("asset_id") or asset.get("path"))
        text_score = float(text_scores[index_number].cpu())
        reference_score = float(reference_scores[index_number].cpu()) if reference_scores is not None else None
        combined = args.text_weight * text_score
        denominator = args.text_weight
        if reference_score is not None:
            combined += args.reference_weight * reference_score
            denominator += args.reference_weight
        combined /= max(denominator, 1e-8)
        view_row = {
            "view": image_row["view"],
            "preview": image_row["preview"],
            "text_similarity": round(text_score, 6),
            "reference_similarity": round(reference_score, 6) if reference_score is not None else None,
            "combined_score": round(combined, 6),
        }
        current = by_asset.get(asset_id)
        if current is None or combined > current["score"]:
            by_asset[asset_id] = {
                "asset_id": asset_id,
                "path": asset.get("path"),
                "semantic_name": asset.get("semantic_name"),
                "category": asset.get("category"),
                "score": round(combined, 6),
                "best_view": view_row,
            }

    ranking = sorted(by_asset.values(), key=lambda row: row["score"], reverse=True)[: args.topk]
    result = {
        "schema_version": "isaac-visual-asset-ranking/v1",
        "query": args.query,
        "reference_crop": str(args.reference_crop) if args.reference_crop else None,
        "model": args.model,
        "device": device,
        "weights": {
            "text": args.text_weight,
            "reference": args.reference_weight if args.reference_crop else 0.0,
        },
        "ranking": ranking,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"candidates": len(ranking), "top": ranking[:3], "output": str(args.output)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
