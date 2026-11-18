#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from isaac_scene.visual_catalog import initialize, upsert_asset, upsert_view  # noqa: E402
from isaac_scene.preview_manifest import asset_is_complete  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Persist CLIP embeddings for rendered USD asset previews.")
    parser.add_argument("--preview-index", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", default="openai/clip-vit-base-patch32")
    parser.add_argument("--device", default="auto")
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument(
        "--required-views",
        nargs="+",
        default=["neg_y", "pos_x", "pos_y", "neg_x"],
        choices=["neg_y", "pos_x", "pos_y", "neg_x"],
    )
    parser.add_argument("--min-output-bytes", type=int, default=4000)
    return parser.parse_args()


def resolve_preview(index_path: Path, value: str) -> Path:
    path = Path(value)
    if path.is_file():
        return path
    for candidate in (
        index_path.parent / path.name,
        index_path.parent / Path(*path.parts[-2:]) if len(path.parts) >= 2 else path,
    ):
        if candidate.is_file():
            return candidate
    return path


def main() -> int:
    args = parse_args()
    import sqlite3
    import torch
    from PIL import Image
    from transformers import CLIPModel, CLIPProcessor

    device = args.device
    if device == "auto":
        device = "cuda" if torch.cuda.is_available() else "cpu"
    model = CLIPModel.from_pretrained(args.model).to(device).eval()
    processor = CLIPProcessor.from_pretrained(args.model)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(args.output)
    initialize(connection)
    image_rows: list[dict[str, Any]] = []
    asset_count = 0
    skipped_qa_assets = 0
    for index_path in args.preview_index:
        manifest = json.loads(index_path.read_text(encoding="utf-8"))
        for asset in manifest.get("assets", []):
            if asset.get("status") != "pass" or not asset.get("path"):
                continue
            resolved_previews = {
                view_name: str(resolve_preview(index_path, str(asset.get("previews", {}).get(view_name, ""))))
                for view_name in args.required_views
            }
            resolved_asset = {**asset, "previews": resolved_previews}
            if not asset_is_complete(
                resolved_asset,
                required_views=args.required_views,
                min_output_bytes=args.min_output_bytes,
            ):
                skipped_qa_assets += 1
                continue
            try:
                for preview_path in resolved_previews.values():
                    with Image.open(preview_path) as image:
                        image.verify()
            except Exception:
                skipped_qa_assets += 1
                continue
            asset_id = str(asset.get("asset_id") or asset["path"])
            upsert_asset(
                connection,
                asset_id=asset_id,
                source_path=str(asset["path"]),
                semantic_name=asset.get("semantic_name"),
                category=asset.get("category"),
                preview_index_path=str(index_path),
                bbox=asset.get("bbox"),
                metadata={"views": list(args.required_views), "preview_qa": "pass"},
            )
            asset_count += 1
            for view_name in args.required_views:
                image_rows.append({
                    "asset_id": asset_id,
                    "view_name": str(view_name),
                    "preview": Path(resolved_previews[view_name]),
                })
    connection.commit()
    if not image_rows:
        connection.close()
        raise SystemExit("No preview images could be resolved")

    for start in range(0, len(image_rows), args.batch_size):
        batch = image_rows[start:start + args.batch_size]
        images = [Image.open(row["preview"]).convert("RGB") for row in batch]
        with torch.inference_mode():
            inputs = processor(images=images, return_tensors="pt").to(device)
            embeddings = model.get_image_features(**inputs)
            embeddings = embeddings / embeddings.norm(dim=-1, keepdim=True)
        for row, embedding in zip(batch, embeddings.detach().cpu().tolist()):
            upsert_view(
                connection,
                asset_id=row["asset_id"],
                view_name=row["view_name"],
                preview_path=str(row["preview"]),
                model_id=args.model,
                embedding=embedding,
            )
        connection.commit()
        print(f"VISUAL_INDEX_BATCH {min(start + len(batch), len(image_rows))}/{len(image_rows)}", flush=True)
    integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    indexed_assets = connection.execute("SELECT COUNT(*) FROM visual_assets").fetchone()[0]
    indexed_views = connection.execute("SELECT COUNT(*) FROM visual_views WHERE model_id=?", (args.model,)).fetchone()[0]
    connection.close()
    print(json.dumps({
        "schema_version": "isaac-asset-visual-index-build/v1",
        "output": str(args.output),
        "model": args.model,
        "device": device,
        "input_assets": asset_count,
        "skipped_qa_assets": skipped_qa_assets,
        "required_views": args.required_views,
        "indexed_assets": indexed_assets,
        "indexed_views": indexed_views,
        "integrity": integrity,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
