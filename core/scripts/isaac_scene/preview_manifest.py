"""Durable helpers for resumable four-view asset preview runs."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = "isaac-asset-preview-index/v1"


def load_or_create(
    index_path: str | Path,
    *,
    resolution: int,
    views: Iterable[str],
    resume: bool,
) -> dict[str, Any]:
    path = Path(index_path)
    expected_views = list(views)
    if resume and path.is_file():
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if manifest.get("schema_version") != SCHEMA_VERSION:
            raise ValueError(f"Unsupported preview manifest schema: {manifest.get('schema_version')}")
        if int(manifest.get("resolution", 0)) != int(resolution):
            raise ValueError("Cannot resume with a different preview resolution")
        if list(manifest.get("views", [])) != expected_views:
            raise ValueError("Cannot resume with a different preview view set or order")
        if not isinstance(manifest.get("assets"), list):
            raise ValueError("Preview manifest assets must be a list")
        return manifest
    return {
        "schema_version": SCHEMA_VERSION,
        "created_at_unix": time.time(),
        "resolution": int(resolution),
        "views": expected_views,
        "assets": [],
    }


def asset_is_complete(
    row: dict[str, Any] | None,
    *,
    required_views: Iterable[str],
    min_output_bytes: int,
) -> bool:
    if not row or row.get("status") != "pass":
        return False
    previews = row.get("previews")
    if not isinstance(previews, dict):
        return False
    for view in required_views:
        preview = previews.get(view)
        if not preview:
            return False
        path = Path(str(preview))
        if not path.is_file() or path.stat().st_size < min_output_bytes:
            return False
    return True


def upsert_asset(manifest: dict[str, Any], row: dict[str, Any]) -> None:
    asset_id = str(row["asset_id"])
    assets = manifest.setdefault("assets", [])
    for index, existing in enumerate(assets):
        if str(existing.get("asset_id")) == asset_id:
            assets[index] = row
            break
    else:
        assets.append(row)
    manifest["updated_at_unix"] = time.time()


def write_atomic(index_path: str | Path, manifest: dict[str, Any]) -> None:
    path = Path(index_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
