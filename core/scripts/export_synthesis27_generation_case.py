#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def export_case(
    contract: dict[str, Any],
    *,
    slug: str,
    mode: str,
    dataset_root: Path,
    output_root: Path,
) -> dict[str, Any]:
    matches = [row for row in contract.get("cases", []) if row.get("slug") == slug]
    if len(matches) != 1:
        raise ValueError(f"Expected exactly one case for slug={slug!r}, found {len(matches)}")
    source = matches[0]
    generation_input = dict(source["generation_inputs"][mode])
    if generation_input.get("reference_image"):
        generation_input["reference_image"] = str(
            dataset_root / str(generation_input["reference_image"])
        )
    return {
        "schema_version": "synthesis27-generation-case/v1",
        "case_id": source["id"],
        "slug": slug,
        "domain": source["domain"],
        "mode": mode,
        "generation_input": generation_input,
        "allowed_context": [
            "this file",
            "the referenced image",
            "isaac-scene-skill-lite",
            "callable semantic and visual asset catalogs",
        ],
        "forbidden_context": [
            "Synthesis regression oracle",
            "source scene_info.json or scene_data.json",
            "source object transforms",
            "source scene USD or source geometry",
        ],
        "required_outputs": {
            "scene_brief": str(output_root / "scene_brief.json"),
            "scene_brief_validation": str(output_root / "scene_brief_validation.json"),
            "generation_report": str(output_root / "generation_report.json"),
        },
        "acceptance": source["acceptance"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Export one leakage-safe Synthesis-27 generation input for an external agent."
    )
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--slug", required=True)
    parser.add_argument("--mode", choices=("text_only", "reference_guided"), required=True)
    parser.add_argument("--dataset-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.contract.is_file():
        parser.error(f"contract not found: {args.contract}")
    contract = json.loads(args.contract.read_text(encoding="utf-8"))
    try:
        case = export_case(
            contract,
            slug=args.slug,
            mode=args.mode,
            dataset_root=args.dataset_root,
            output_root=args.output_root,
        )
    except ValueError as exc:
        parser.error(str(exc))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(case, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "slug": case["slug"],
        "mode": case["mode"],
        "reference_image": case["generation_input"].get("reference_image"),
        "output": str(args.output),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
