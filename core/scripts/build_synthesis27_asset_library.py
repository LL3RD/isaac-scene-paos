#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from isaac_scene.synthesis27_library import collect, write_library  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build a standalone, semantics-first asset library from the Synthesis-27 scenes."
    )
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--dataset-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--acquisition-plan", type=Path)
    parser.add_argument(
        "--semantic-overrides",
        type=Path,
        default=SKILL_ROOT / "config" / "synthesis27_semantic_overrides.json",
        help="Reviewed per-model Synthesis labels; unknown model IDs keep generic classification.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit 2 after writing the audit outputs when ingest errors are present.",
    )
    args = parser.parse_args()

    library = collect(
        args.manifest,
        args.dataset_root,
        acquisition_plan=args.acquisition_plan,
        semantic_overrides=args.semantic_overrides,
    )
    summary = write_library(args.output_dir, library)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 2 if args.strict and summary["error_count"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
