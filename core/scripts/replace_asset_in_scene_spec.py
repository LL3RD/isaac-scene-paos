#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any


AXIS_ANGLE_DEG = {
    "+X": 0.0,
    "+Y": 90.0,
    "-X": 180.0,
    "-Y": -90.0,
}


def normalize_yaw(value: float) -> float:
    result = (float(value) + 180.0) % 360.0 - 180.0
    return 180.0 if math.isclose(result, -180.0) else result


def replacement_yaw(old_yaw: float, old_front_axis: str, new_front_axis: str) -> float:
    desired_world_angle = float(old_yaw) + AXIS_ANGLE_DEG[old_front_axis]
    return normalize_yaw(desired_world_angle - AXIS_ANGLE_DEG[new_front_axis])


def main() -> int:
    parser = argparse.ArgumentParser(description="Replace one repeated asset while preserving its world-facing direction.")
    parser.add_argument("input_spec", type=Path)
    parser.add_argument("output_spec", type=Path)
    parser.add_argument("--match-path", required=True)
    parser.add_argument("--replacement-path", required=True)
    parser.add_argument("--front-axis", choices=sorted(AXIS_ANGLE_DEG), required=True)
    parser.add_argument("--bbox-min-z-m", type=float, default=None)
    parser.add_argument("--support-z", type=float, default=0.0)
    parser.add_argument("--scene-output", default=None)
    parser.add_argument("--visual-score", type=float, default=None)
    parser.add_argument("--selection-status", default="verified_fallback")
    args = parser.parse_args()

    spec = json.loads(args.input_spec.read_text(encoding="utf-8"))
    changed = []
    seen_objects: set[int] = set()
    for collection_name in ("entities", "references"):
        for item in spec.get(collection_name, []) or []:
            if not isinstance(item, dict) or id(item) in seen_objects:
                continue
            seen_objects.add(id(item))
            if str(item.get("path") or item.get("asset_path") or "") != args.match_path:
                continue
            old_front_axis = str(item.get("front_axis") or "")
            if old_front_axis not in AXIS_ANGLE_DEG:
                raise SystemExit(f"{item.get('id')}: valid existing front_axis is required")
            rotate = [float(value) for value in item.get("rotate", [0, 0, 0])]
            if len(rotate) != 3:
                raise SystemExit(f"{item.get('id')}: rotate must contain three values")
            rotate[2] = replacement_yaw(rotate[2], old_front_axis, args.front_axis)
            item["path"] = args.replacement_path
            item.pop("asset_path", None)
            item["rotate"] = rotate
            item["front_axis"] = args.front_axis
            item["orientation_confidence"] = "high"
            item["orientation_source"] = "four-view asset preview verified and persisted in semantic catalog"
            if args.bbox_min_z_m is not None:
                translate = [float(value) for value in item.get("translate", [0, 0, 0])]
                if len(translate) != 3:
                    raise SystemExit(f"{item.get('id')}: translate must contain three values")
                translate[2] = args.support_z - args.bbox_min_z_m
                item["translate"] = translate
            item["asset_selection"] = {
                "status": args.selection_status,
                "visual_score": args.visual_score,
                "source": "semantic_visual_rerank",
            }
            changed.append({
                "id": item.get("id"),
                "rotate": rotate,
                "translate": item.get("translate"),
            })
    if not changed:
        raise SystemExit(f"No scene entities matched path: {args.match_path}")
    if args.scene_output:
        spec["output_path"] = args.scene_output
    spec.setdefault("batch_metadata", {})["asset_replacement"] = {
        "match_path": args.match_path,
        "replacement_path": args.replacement_path,
        "front_axis": args.front_axis,
        "changed_count": len(changed),
        "selection_status": args.selection_status,
        "visual_score": args.visual_score,
    }
    args.output_spec.parent.mkdir(parents=True, exist_ok=True)
    args.output_spec.write_text(json.dumps(spec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "output": str(args.output_spec),
        "changed_count": len(changed),
        "changed": changed,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
