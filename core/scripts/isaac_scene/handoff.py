from __future__ import annotations

import hashlib
import json
import shutil
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .contracts import redact
from .output_policy import new_output

ARTIFACT_KEYS = ("scene_path", "scene_spec_path", "validation_path", "task_path")


def _copy_scene(source: Path, destination: Path) -> dict:
    """Rebase root-layer asset paths; keep composed geometry/layers untouched."""
    from pxr import Sdf, UsdUtils
    from .usd_exchange import scene_identity
    layer = Sdf.Layer.FindOrOpen(str(source.resolve()))
    if not layer:
        raise ValueError(f"Cannot open USD handoff source: {source}")
    if source.resolve() != destination.resolve():
        copy = Sdf.Layer.CreateAnonymous()
        copy.TransferContent(layer)
        def anchor(value):
            if not value or os.path.isabs(value) or "://" in value:
                return value
            # Anchor to the original authoring layer, including UDIM templates.
            return Sdf.ComputeAssetPathRelativeToLayer(layer, value)
        UsdUtils.ModifyAssetPaths(copy, anchor)
        if not copy.Export(str(destination)):
            raise RuntimeError(f"USD export failed: {destination}")
    identity = scene_identity(destination)
    return {"dependency_hashes": {key: value for key, value in identity["files"].items()
                                  if key != str(destination.resolve())},
            "unresolved_assets": identity["unresolved_assets"],
            "packaging": "thin; referenced layers/textures remain external, root paths rebased"}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def export_handoff(state: dict[str, Any], output_dir: str | Path, *, config: dict[str, Any], skill_root: str | Path) -> dict[str, Any]:
    output = new_output(output_dir, root=(config.get("runtime") or {}).get("output_root"))
    planned = [Path(str(state[key])) for key in ARTIFACT_KEYS if state.get(key)]
    names = [p.name for p in planned] + ["asset_lock.json", "handoff.json", "profile.private.json", "public-summary.json"]
    if len(names) != len(set(names)):
        raise ValueError("Handoff filenames collide; use distinct artifact names")
    for name in names:
        destination = output / name
        if destination.exists() and not any(p.resolve() == destination.resolve() for p in planned):
            raise ValueError(f"Handoff output already exists: {destination}; choose a new directory")
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    artifacts: list[dict[str, Any]] = []
    for key in ARTIFACT_KEYS:
        value = state.get(key)
        if not value:
            continue
        source = Path(str(value))
        if not source.is_file():
            artifacts.append({"role": key, "source": str(source), "status": "missing"})
            continue
        destination = output / source.name
        extra = {}
        if key == "scene_path" and source.suffix.lower() in {".usd", ".usda", ".usdc"}:
            extra = _copy_scene(source, destination)
        elif source.resolve() != destination.resolve():
            shutil.copy2(source, destination)
        artifacts.append({"role": key, "path": destination.name, "sha256": _sha256(destination), "size_bytes": destination.stat().st_size, **extra})

    now = datetime.now(timezone.utc).isoformat()
    handoff_id = state.get("handoff_id") or f"handoff_{hashlib.sha1((now + str(state.get('goal', ''))).encode()).hexdigest()[:12]}"
    selected_assets = list(state.get("selected_assets") or [])
    unit_audit = state.get("unit_audit") if isinstance(state.get("unit_audit"), dict) else {}
    asset_lock_path = output / "asset_lock.json"
    asset_lock_path.write_text(json.dumps({
        "schema_version": "isaac-scene-asset-lock/v1",
        "handoff_id": handoff_id,
        "assets": selected_assets,
        "unit_audit": unit_audit,
    }, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    artifacts.append({"role": "asset_lock", "path": asset_lock_path.name, "sha256": _sha256(asset_lock_path), "size_bytes": asset_lock_path.stat().st_size})
    task = {}
    if state.get("task_path") and Path(state["task_path"]).is_file():
        task = json.loads(Path(state["task_path"]).read_text())
    manifest = {
        "schema_version": "isaac-scene-handoff/v1",
        "audience": "internal_reproduction_only",
        "handoff_id": handoff_id,
        "created_at": now,
        "goal": str(state.get("goal") or ""),
        "domain": str(state.get("domain") or "generic_scene"),
        "phase": str(state.get("phase") or "planning"),
        "delivery_scope": state.get("delivery_scope") or task.get("delivery_scope", "unspecified"),
        "artifacts": artifacts,
        "selected_assets": selected_assets,
        "unit_audit": unit_audit,
        "completed_steps": list(state.get("completed_steps") or []),
        "open_issues": list(state.get("open_issues") or []),
        "next_recommended_actions": list(state.get("next_recommended_actions") or []),
        "environment_fingerprint": {
            "profile": config.get("_profile"),
            "config_path": config.get("_config_path"),
            "python": ((config.get("runtime") or {}).get("python") or {}).get("executable") if isinstance(config.get("runtime"), dict) else None,
            "skill_root": str(skill_root),
        },
    }
    manifest_path = output / "handoff.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    manifest_path.chmod(0o600)
    profile_path = output / "profile.private.json"
    profile_path.write_text(json.dumps(redact(config), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    profile_path.chmod(0o600)
    asset_lock_path.chmod(0o600)
    # Allowlist-only outward report: no free text, profile, source paths, IDs,
    # asset filenames or exception strings are copied from the internal state.
    public_path = output / "public-summary.json"
    public = {
        "schema_version": "isaac-scene-public-summary/v1",
        "packaging_status": "warn" if any(row.get("status") == "missing" for row in artifacts) else "pass",
        "scene_acceptance": "not_evaluated_by_export",
        "selected_asset_count": len(selected_assets),
        "open_issue_count": len(state.get("open_issues") or []),
        "artifacts": [{"role": row["role"], "available": row.get("status") != "missing",
                       **({"sha256": row["sha256"], "size_bytes": row["size_bytes"]} if "sha256" in row else {})}
                      for row in artifacts],
    }
    public_path.write_text(json.dumps(public, indent=2) + "\n", encoding="utf-8")
    return {"status": public["packaging_status"], "handoff_id": handoff_id,
            "manifest_path": str(manifest_path), "profile_snapshot": str(profile_path),
            "public_summary": str(public_path), "asset_lock": str(asset_lock_path), "artifacts": artifacts}


def verify_handoff(manifest_path: str | Path) -> dict[str, Any]:
    path = Path(manifest_path)
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"status": "fail", "errors": [{"code": "HANDOFF_UNREADABLE", "summary": f"{type(exc).__name__}: {exc}"}], "warnings": []}
    for field in ("schema_version", "handoff_id", "goal", "domain", "phase", "artifacts", "open_issues", "next_recommended_actions"):
        if field not in manifest:
            errors.append({"code": "HANDOFF_FIELD_MISSING", "summary": field})
    for artifact in manifest.get("artifacts", []) if isinstance(manifest.get("artifacts"), list) else []:
        if not isinstance(artifact, dict) or artifact.get("status") == "missing":
            warnings.append({"code": "HANDOFF_ARTIFACT_MISSING", "summary": str(artifact.get("source") if isinstance(artifact, dict) else artifact)})
            continue
        artifact_path = path.parent / str(artifact.get("path") or "")
        if not artifact_path.is_file():
            errors.append({"code": "HANDOFF_ARTIFACT_NOT_FOUND", "summary": str(artifact_path)})
        elif artifact.get("sha256") and _sha256(artifact_path) != artifact.get("sha256"):
            errors.append({"code": "HANDOFF_ARTIFACT_HASH_MISMATCH", "summary": str(artifact_path)})
        for dependency, digest in artifact.get("dependency_hashes", {}).items():
            target = Path(dependency)
            if not target.is_file() or _sha256(target) != digest:
                errors.append({"code": "HANDOFF_DEPENDENCY_CHANGED_OR_MISSING", "summary": dependency})
        if artifact.get("unresolved_assets"):
            warnings.append({"code": "HANDOFF_EXTERNAL_RESOLVER_REQUIRED", "summary": str(artifact["unresolved_assets"])})
    for asset in manifest.get("selected_assets", []) if isinstance(manifest.get("selected_assets"), list) else []:
        if not isinstance(asset, dict):
            continue
        resolved_path = asset.get("path")
        if resolved_path and not Path(str(resolved_path)).exists():
            warnings.append({
                "code": "ASSET_LOCK_REQUIRES_RELOCATION",
                "summary": str(asset.get("namespaced_id") or asset.get("asset_id") or resolved_path),
                "retryable": True,
                "details": {"source_path": str(resolved_path)},
            })
    status = "fail" if errors else ("warn" if warnings else "pass")
    return {"status": status, "handoff_id": manifest.get("handoff_id"), "errors": errors, "warnings": warnings, "manifest": manifest}
