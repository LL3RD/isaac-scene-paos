#!/usr/bin/env python3
"""Lightweight Isaac Sim scene skill tools.

This file intentionally contains deterministic helpers only. A higher-level
agent should read the user's prompt, decide what scene to create, then call
these commands for asset lookup, Fabrica recipe lookup, USD composition,
validation, and versioned scene export.
"""

from __future__ import annotations

import argparse
import hashlib
import heapq
import json
import math
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from isaac_scene.contracts import envelope, issue
from isaac_scene.output_policy import check_cli_outputs, check_scene_outputs, new_output
from isaac_scene.camera_planning import (
    confirm_prompt_camera_selection,
    create_prompt_camera_candidates,
    promote_prompt_camera,
    rank_prompt_camera_renders,
)
from isaac_scene.visual_agent_feedback import (
    create_quality_review_packet,
    create_visual_review_packet,
    validate_quality_review,
    validate_visual_agent_review,
)
from isaac_scene.content_iteration import (
    create_content_iteration_packet,
    create_scene_revision_request,
    validate_content_iteration_review,
)
from isaac_scene.architectural_shells import apply_shell as apply_architectural_shell
from isaac_scene.architectural_shells import list_shells as list_architectural_shells
from isaac_scene.architectural_shells import resolve_for_compiled_spec as resolve_architectural_shell
from isaac_scene.architectural_shells import select_shell as select_architectural_shell
from isaac_scene.appearance import apply_appearance as apply_scene_appearance
from isaac_scene.asset_entrypoint import AssetEntrypointMismatch, require_compatible_asset_entrypoint
from isaac_scene.asset_geometry import inspect_asset_local_geometry
from isaac_scene.doctor import inspect as doctor_inspect
from isaac_scene.domains import discover_domains, expand_query, route_domain, tokenize as domain_tokenize
from isaac_scene.enrichment import merge_enrichment_inventory, materialize_enrichment_bindings, plan_scene_enrichment
from isaac_scene.scene_kits import build_catalog as build_scene_kit_catalog
from isaac_scene.scene_kits import load_catalog as load_scene_kit_catalog
from isaac_scene.scene_kits import search_catalog as search_scene_kit_catalog
from isaac_scene.handoff import export_handoff, verify_handoff
from isaac_scene.scene_task import initialize as initialize_scene_task, check as check_scene_task, subject_identity
from isaac_scene.usd_exchange import create_revision as create_usd_revision, prepare_assets as prepare_usd_assets
from isaac_scene.material_quality import audit_material_coverage
from isaac_scene.articulations import inspect_joints, pose_joints, initialize_articulation
from isaac_scene.orientation import FRONT_AXES_2D, facing_error_deg, normalize_front_axis
from isaac_scene.placement import (
    DEFAULT_ARCHITECTURAL_PATH_TOKENS,
    apply_placement_solution,
    architectural_obstacle_root_for_spec,
    automatic_obstacles,
    build_support_dependency_graph,
    clear_pending_entity,
    derive_constraint_layout_spec,
    derive_semantic_layout_spec,
    entity_prim,
    inspect_functional_group_layout,
    is_architectural_prim_path,
    is_volumetric_aggregate_row,
    resolve_functional_group_preference,
    resolve_scene_target,
    select_architectural_obstacle_rows,
    solve_fixed_mount_translation,
)
from isaac_scene.quality import compare_images, evaluate_scene
from isaac_scene.regression_suite import audit_suite as audit_prompt_regression_suite
from isaac_scene.expansion_readiness import audit_catalog as audit_synthesis28_expansion_readiness
from isaac_scene.synthesis_gap import audit_scene as audit_synthesis_gap
from isaac_scene.render_feedback import analyze_image
from isaac_scene.room_shells import apply_preset as apply_room_shell_preset
from isaac_scene.room_shells import list_presets as list_room_shell_presets
from isaac_scene.scene_brief import _lighting_rig as build_lighting_rig
from isaac_scene.scene_brief import _room_shell as build_room_shell
from isaac_scene.scene_brief import compile_to_scene_spec
from isaac_scene.scene_brief import initialize_brief
from isaac_scene.scene_brief import validate as validate_scene_brief
from isaac_scene.scene_spec import normalize as normalize_scene_spec
from isaac_scene.semantic_catalog import annotate as annotate_semantic_catalog
from isaac_scene.semantic_catalog import infer_retrieval_class
from isaac_scene.semantic_catalog import search as search_semantic_catalog
from isaac_scene.semantic_catalog import set_asset_state as set_semantic_asset_state
from isaac_scene.semantic_catalog import taxonomy as semantic_taxonomy
from isaac_scene.shell_spatial import inspect_shell_spatial
from isaac_scene.support import mesh_support_contact as _mesh_support_contact
from isaac_scene.support import mesh_support_coverage_from_points as _mesh_support_coverage_from_points
from isaac_scene.support import mesh_bounds_from_world_meshes as _mesh_bounds_from_world_meshes
from isaac_scene.support import solve_mesh_support_placement as _solve_mesh_support_placement
from isaac_scene.support import top_support_triangles as _top_support_triangles
from isaac_scene.support import discover_horizontal_support_surfaces as _discover_horizontal_support_surfaces
from isaac_scene.support import select_support_surface_z as _select_support_surface_z
from isaac_scene.support import world_mesh_points_and_triangles as _world_mesh_points_and_triangles
from isaac_scene.support_relations import (
    extract_synthesis28_support_relations,
    refine_synthesis28_mesh_support_relations,
)
from isaac_scene.synthesis_reconstruction import (
    create_reference_camera_candidates,
    create_reconstruction_render_pair,
    evaluate_reconstruction_materials,
    evaluate_reconstruction_scene,
    export_reconstruction_specs,
    rank_reference_camera_renders,
)
from isaac_scene.units import UnitScaleMismatch, resolve_reference_scale, select_source_unit_contract
from isaac_scene.validation import flatten_evidence_paths, load_validation_manifest
from isaac_scene.visual_selection import candidate_coverage
from compose_scene_validation_summary import compose_summary


SKILL_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = SKILL_ROOT / "config" / "config.json"
DEFAULTS_CONFIG = SKILL_ROOT / "config" / "defaults.json"
PROFILE_ROOT = SKILL_ROOT / "config" / "profiles"
USD_SUFFIXES = {".usd", ".usda", ".usdc"}
TEXTURE_SUFFIXES = {".png", ".jpg", ".jpeg", ".tga", ".exr", ".hdr", ".bmp", ".tif", ".tiff"}
_UNIT_METADATA_CACHE: dict[str, dict[str, Any]] = {}
_ISAAC_RUNTIME_MDL_PATHS = {"gltf/pbr.mdl"}


def _is_runtime_mdl_module(path: str) -> bool:
    normalized = path.replace("\\", "/").lower()
    return normalized.endswith(".mdl") and (
        "/" not in normalized or normalized in _ISAAC_RUNTIME_MDL_PATHS
    )

HOME_BACKGROUND_CATALOG: list[dict[str, Any]] = [
    {
        "id": "geniesim_home_1",
        "room_types": ["home", "living_room", "bedroom"],
        "relative_path": "background/home/home_1/background.usda",
        "quality_hint": "complete_geniesim_background",
        "notes": "Generic home background; inspect before adding duplicate large furniture.",
    },
    {
        "id": "geniesim_home_2",
        "room_types": ["home", "living_room", "bedroom"],
        "relative_path": "background/home/home_2/background.usda",
        "quality_hint": "complete_geniesim_background",
        "notes": "Generic home background; useful when home_1 composition is not suitable.",
    },
    {
        "id": "geniesim_home_3",
        "room_types": ["home", "living_room", "bedroom", "study"],
        "relative_path": "background/home/home_3/background.usda",
        "quality_hint": "complete_geniesim_background_with_common_refs",
        "notes": "Preferred first candidate for realistic home/living-room tests.",
    },
    {
        "id": "geniesim_room_1",
        "room_types": ["room", "empty_room", "home"],
        "relative_path": "background/room/room_1/background.usda",
        "quality_hint": "real_room_shell",
        "notes": "Use for from-empty-room generation before primitive shell.",
    },
    {
        "id": "geniesim_kitchen_1",
        "room_types": ["kitchen", "dining_kitchen"],
        "relative_path": "background/kitchen/kitchen_1/background.usda",
        "quality_hint": "complete_geniesim_kitchen",
        "notes": "Preferred candidate for kitchen scenes.",
    },
    {
        "id": "geniesim_common_kitchen",
        "room_types": ["kitchen"],
        "relative_path": "background/common/kitchen/kitchen.usd",
        "quality_hint": "common_kitchen_background",
        "notes": "Common kitchen asset; can be used when kitchen_1 is unsuitable.",
    },
    {
        "id": "geniesim_dining_room_01",
        "room_types": ["dining_room", "dining"],
        "relative_path": "background/dining_room/dining_room_01/background.usda",
        "quality_hint": "complete_geniesim_dining_room",
        "notes": "Preferred candidate for dining-room or dining area scenes.",
    },
    {
        "id": "geniesim_study_1",
        "room_types": ["study", "home_office", "office"],
        "relative_path": "background/study_room/study_1/background.usda",
        "quality_hint": "complete_geniesim_study",
        "notes": "Preferred candidate for study/home-office scenes.",
    },
    {
        "id": "geniesim_study_2",
        "room_types": ["study", "home_office", "office"],
        "relative_path": "background/study_room/study_2/background.usda",
        "quality_hint": "complete_geniesim_study",
        "notes": "Alternative study/home-office background.",
    },
    {
        "id": "geniesim_study_3",
        "room_types": ["study", "home_office", "office"],
        "relative_path": "background/study_room/study_3/background.usda",
        "quality_hint": "complete_geniesim_study",
        "notes": "Alternative study/home-office background.",
    },
    {
        "id": "geniesim_study_4",
        "room_types": ["study", "home_office", "office"],
        "relative_path": "background/study_room/study_4/background.usda",
        "quality_hint": "complete_geniesim_study",
        "notes": "Alternative study/home-office background.",
    },
    {
        "id": "geniesim_office_00",
        "room_types": ["office", "home_office", "study"],
        "relative_path": "background/office/office_00/background.usda",
        "quality_hint": "complete_geniesim_office",
        "notes": "Office scene; use for work room or office variants.",
    },
    {
        "id": "geniesim_common_office",
        "room_types": ["office", "home_office", "study"],
        "relative_path": "background/common/office/Aligned.usd",
        "quality_hint": "common_office_background",
        "notes": "Common office asset; inspect visual quality before use.",
    },
]


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _profile_config_path(profile: str) -> Path:
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", profile):
        raise ValueError("Profile must be a simple name; use --config for an external file")
    descriptor_path = PROFILE_ROOT / f"{profile}.json"
    descriptor = _read_json(descriptor_path)
    configured = descriptor.get("config") if descriptor else None
    if configured:
        candidate = Path(str(configured))
        return candidate if candidate.is_absolute() else (descriptor_path.parent / candidate).resolve()
    legacy = SKILL_ROOT / "config" / f"config.{profile}.json"
    if not legacy.is_file():
        raise ValueError("Unknown deployment profile; configure --config explicitly")
    return legacy


def _expand_config(value: Any) -> Any:
    """Expand explicit ${VAR} placeholders; missing deployment values fail closed."""
    if isinstance(value, dict):
        return {key: _expand_config(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_expand_config(item) for item in value]
    if isinstance(value, str):
        def replace(match):
            name = match.group(1)
            if not os.environ.get(name):
                raise ValueError(f"Deployment environment variable is unset: {name}")
            return os.environ[name]
        return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", replace, value)
    return value


def load_config(path: str | os.PathLike | None = None, *, profile: str | None = None) -> dict[str, Any]:
    defaults = _read_json(DEFAULTS_CONFIG)
    config_env = str(defaults.get("config_env") or "ISAAC_SCENE_SKILL_CONFIG")
    profile_env = str(defaults.get("profile_env") or "ISAAC_SCENE_PROFILE")
    explicit = path or os.environ.get(config_env)
    selected_profile = profile or os.environ.get(profile_env) or defaults.get("default_profile")
    config_path = Path(explicit).expanduser().resolve() if explicit else (_profile_config_path(str(selected_profile)) if selected_profile else DEFAULT_CONFIG)
    config = _read_json(config_path)
    if config.get("config") and not config.get("runtime"):
        target = Path(str(config["config"]))
        target = target if target.is_absolute() else (config_path.parent / target).resolve()
        alias_profile = config.get("profile")
        config_path = target
        config = _read_json(config_path)
        if alias_profile and not profile:
            selected_profile = str(alias_profile)
    if not config:
        raise ValueError("Deployment config not found or invalid")
    config = _expand_config(config)
    config["_config_path"] = str(config_path)
    config["_skill_root"] = str(SKILL_ROOT)
    config["_profile"] = str(selected_profile) if selected_profile else None
    return config


def _tokenize(text: str) -> list[str]:
    return domain_tokenize(text)


def _library_specs(config: dict[str, Any]) -> list[dict[str, Any]]:
    specs = []
    for raw in config.get("libraries", []):
        if not isinstance(raw, dict) or not raw.get("name") or not raw.get("root"):
            continue
        item = dict(raw)
        item["root"] = str(Path(os.path.expandvars(os.path.expanduser(str(item["root"]))))
                           .resolve())
        item["exists"] = Path(item["root"]).exists()
        specs.append(item)
    return specs


def _library_root(config: dict[str, Any], name: str) -> Path | None:
    for spec in _library_specs(config):
        if spec.get("name") == name and spec.get("exists"):
            return Path(str(spec["root"]))
    return None


def _state_root(config: dict[str, Any]) -> Path:
    runtime = config.get("runtime", {}) if isinstance(config.get("runtime"), dict) else {}
    root = runtime.get("state_root") or (Path.cwd() / "runs" / ".cache")
    candidate = Path(str(root)).expanduser()
    probe = candidate
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    if probe.exists() and os.access(probe, os.W_OK):
        return candidate
    fallback = Path(os.environ.get("ISAAC_SCENE_LOCAL_STATE_ROOT", Path.home() / ".cache" / "isaac-scene-skill-lite"))
    return fallback.expanduser()


def _cache_path(config: dict[str, Any], libraries: list[str] | None = None) -> Path:
    def fingerprint(lib: dict[str, Any]) -> dict[str, Any]:
        source = Path(str(lib.get("index_path") or lib.get("root") or ""))
        try:
            stat = source.stat()
            source_state = {"exists": True, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
        except OSError:
            source_state = {"exists": False}
        marker_value = lib.get("readiness_marker")
        marker = Path(str(marker_value)) if marker_value else None
        marker_state: dict[str, Any] | None = None
        if marker:
            try:
                stat = marker.stat()
                marker_state = {"exists": True, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
            except OSError:
                marker_state = {"exists": False}
        return {"source": source_state, "readiness_marker": marker_state}

    sig = {
        "version": 5,
        "libraries": [
            {
                "name": lib.get("name"),
                "scanner": lib.get("scanner"),
                "root": lib.get("root"),
                "include_roots": lib.get("include_roots"),
                "exclude_roots": lib.get("exclude_roots"),
                "max_depth": lib.get("max_depth"),
                "max_records": lib.get("max_records"),
                "index_path": lib.get("index_path"),
                "assets": lib.get("assets") if lib.get("scanner") == "curated" else None,
                "fingerprint": fingerprint(lib),
            }
            for lib in _library_specs(config)
            if libraries is None or lib.get("name") in libraries
        ],
    }
    key = hashlib.sha1(json.dumps(sig, sort_keys=True).encode("utf-8")).hexdigest()[:12]
    return _state_root(config) / "asset_index" / f"index_{key}.json"


def _search_policy(config: dict[str, Any]) -> dict[str, Any]:
    policy = config.get("search_policy")
    return policy if isinstance(policy, dict) else {}


def _configured_library_names(config: dict[str, Any]) -> list[str]:
    return [str(lib["name"]) for lib in _library_specs(config)]


def _slow_libraries(config: dict[str, Any]) -> list[str]:
    policy = _search_policy(config)
    raw = policy.get("slow_libraries")
    if isinstance(raw, list):
        return [str(item) for item in raw]
    return []


def _default_search_libraries(config: dict[str, Any]) -> list[str]:
    policy = _search_policy(config)
    raw = policy.get("default_libraries")
    configured_names = _configured_library_names(config)
    configured = set(configured_names)
    if isinstance(raw, list) and raw:
        return [str(item) for item in raw if str(item) in configured]
    slow = set(_slow_libraries(config))
    return [name for name in configured_names if name not in slow]


def _effective_search_libraries(
    config: dict[str, Any],
    libraries: list[str] | None = None,
) -> list[str] | None:
    if libraries is not None:
        return libraries
    return _default_search_libraries(config)


def _make_record(
    *,
    library: str,
    asset_id: str,
    asset_type: str,
    path: Path,
    root: Path,
    semantic_name: str | None = None,
    category: str = "",
    tags: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    try:
        relative_path = str(path.relative_to(root))
    except ValueError:
        relative_path = str(path)
    tag_set = set(tags or [])
    tag_set.update(_tokenize(asset_id))
    tag_set.update(_tokenize(asset_type))
    tag_set.update(_tokenize(category))
    tag_set.update(_tokenize(semantic_name or ""))
    tag_set.update(_tokenize(relative_path))
    record = {
        "library": library,
        "namespaced_id": f"{library}:{asset_id}",
        "asset_id": asset_id,
        "asset_type": asset_type,
        "path": str(path),
        "payload_path": str(path),
        "relative_path": relative_path,
        "semantic_name": semantic_name or asset_id,
        "category": category,
        "tags": sorted(tag_set),
        "metadata": metadata or {},
    }
    record.update(extra)
    return record


def _infer_usd_type(path: Path, root: Path) -> str:
    try:
        tokens = set(_tokenize(str(path.relative_to(root))))
    except ValueError:
        tokens = set(_tokenize(str(path)))
    name = path.name.lower()
    if "environment" in tokens or "environments" in tokens:
        return "environment"
    if "robot" in tokens or "robots" in tokens:
        return "robot"
    if "sensor" in tokens or "sensors" in tokens or "camera" in tokens:
        return "sensor"
    if "material" in tokens or "materials" in tokens:
        return "material"
    if "prop" in tokens or "props" in tokens:
        return "object"
    if name.startswith("background") or name.startswith("scene"):
        return "background"
    return "asset"


def _norm_rel_parts(value: str | os.PathLike) -> tuple[str, ...]:
    text = str(value).strip().strip("/")
    if not text:
        return ()
    return tuple(part.lower() for part in Path(text).parts if part not in {"", "."})


def _has_prefix(parts: tuple[str, ...], prefix: tuple[str, ...]) -> bool:
    return bool(prefix) and len(parts) >= len(prefix) and parts[:len(prefix)] == prefix


def _iter_usd_files(
    scan_root: Path,
    *,
    library_root: Path | None = None,
    exclude_roots: list[str] | None = None,
    max_depth: int | None = None,
    max_records: int | None = None,
) -> list[Path]:
    skip_dirs = {
        ".git",
        "__pycache__",
        ".thumbs",
        "textures",
        "texture",
        "materials",
        "material",
        "mdl",
        "thumbnails",
        "preview",
    }
    out: list[Path] = []
    library_root = library_root or scan_root
    exclude_parts = [_norm_rel_parts(item) for item in (exclude_roots or [])]

    for current, dirs, files in os.walk(scan_root):
        current_path = Path(current)
        try:
            rel_to_library = tuple(part.lower() for part in current_path.relative_to(library_root).parts)
        except ValueError:
            rel_to_library = ()
        try:
            rel_to_scan = current_path.relative_to(scan_root)
            depth = 0 if str(rel_to_scan) == "." else len(rel_to_scan.parts)
        except ValueError:
            depth = 0

        kept_dirs: list[str] = []
        for dirname in dirs:
            dirname_l = dirname.lower()
            child_rel = rel_to_library + (dirname_l,)
            if dirname_l in skip_dirs:
                continue
            if any(_has_prefix(child_rel, prefix) for prefix in exclude_parts):
                continue
            if max_depth is not None and depth + 1 > max_depth:
                continue
            kept_dirs.append(dirname)
        dirs[:] = kept_dirs

        for filename in files:
            if Path(filename).suffix.lower() not in USD_SUFFIXES:
                continue
            path = current_path / filename
            try:
                file_rel = tuple(part.lower() for part in path.relative_to(library_root).parts)
            except ValueError:
                file_rel = ()
            if any(_has_prefix(file_rel, prefix) for prefix in exclude_parts):
                continue
            out.append(path)
            if max_records is not None and len(out) >= max_records:
                return out
    return out


def _scan_usd_tree(spec: dict[str, Any]) -> list[dict[str, Any]]:
    root = Path(spec["root"])
    records = []
    if not root.exists():
        return records
    include_roots = spec.get("include_roots") or []
    scan_roots = [root / rel for rel in include_roots] if include_roots else [root]
    exclude_roots = [str(item) for item in (spec.get("exclude_roots") or [])]
    max_depth = spec.get("max_depth")
    max_records = spec.get("max_records")
    for scan_root in scan_roots:
        if not scan_root.exists():
            continue
        for path in _iter_usd_files(
            scan_root,
            library_root=root,
            exclude_roots=exclude_roots,
            max_depth=int(max_depth) if max_depth is not None else None,
            max_records=int(max_records) if max_records is not None else None,
        ):
            try:
                rel = path.relative_to(root)
            except ValueError:
                rel = path.relative_to(scan_root)
            asset_id = str(rel.with_suffix("")).replace("/", "_")
            records.append(_make_record(
                library=spec["name"],
                asset_id=asset_id,
                asset_type=_infer_usd_type(path, root),
                path=path,
                root=root,
                semantic_name=" ".join(_tokenize(path.stem if path.stem not in {"scene", "background"} else path.parent.name)),
                category="/".join(rel.parts[:-1]),
                tags=[spec["name"]],
            ))
    return records


def _home_catalog_by_relative_path(relative_path: str) -> dict[str, Any] | None:
    normalized = relative_path.replace("\\", "/")
    for item in HOME_BACKGROUND_CATALOG:
        if normalized == str(item["relative_path"]).replace("\\", "/"):
            return item
    return None


def _scan_geniesim(spec: dict[str, Any]) -> list[dict[str, Any]]:
    root = Path(spec["root"])
    records = []
    if not root.exists():
        return records
    bg_root = root / "background"
    if bg_root.exists():
        for path in sorted(set(
            list(bg_root.rglob("background.usd"))
            + list(bg_root.rglob("background.usda"))
            + list(bg_root.rglob("scene.usd"))
            + list(bg_root.rglob("scene.usda"))
        )):
            rel = path.relative_to(root)
            home_catalog = _home_catalog_by_relative_path(str(rel))
            home_tags: list[str] = []
            home_metadata: dict[str, Any] = {}
            semantic_name = " ".join(rel.parts[:-1]).replace("_", " ")
            if home_catalog:
                home_tags.extend(str(v) for v in home_catalog.get("room_types", []) or [])
                home_tags.append(str(home_catalog.get("quality_hint", "")))
                semantic_name = f"{home_catalog['id']} {' '.join(home_tags)} home indoor background"
                home_metadata = {
                    "home_background_catalog_id": home_catalog["id"],
                    "home_room_types": home_catalog.get("room_types", []),
                    "home_quality_hint": home_catalog.get("quality_hint"),
                    "home_notes": home_catalog.get("notes"),
                }
            records.append(_make_record(
                library=spec["name"],
                asset_id=f"background_{str(rel.with_suffix('')).replace('/', '_')}",
                asset_type="background",
                path=path,
                root=root,
                semantic_name=semantic_name,
                category="/".join(rel.parts[:-1]),
                tags=["background", "environment", "home_scene", *home_tags],
                metadata=home_metadata,
            ))
    obj_root = root / "objects"
    if obj_root.exists():
        by_parent: dict[Path, Path] = {}
        for path in obj_root.rglob("Aligned.usda"):
            by_parent[path.parent] = path
        for path in obj_root.rglob("Aligned.usd"):
            by_parent[path.parent] = path
        for path in sorted(by_parent.values()):
            rel = path.relative_to(root)
            asset_id = path.parent.name
            records.append(_make_record(
                library=spec["name"],
                asset_id=asset_id,
                asset_type="object",
                path=path,
                root=root,
                semantic_name=asset_id.replace("_", " "),
                category="/".join(rel.parts[:-2]),
                tags=["object", "aligned"],
            ))
    robot_root = root / "robot"
    if robot_root.exists():
        for robot_dir in sorted(path for path in robot_root.iterdir() if path.is_dir()):
            candidates = [robot_dir / "robot.usd", robot_dir / "robot.usda"]
            path = next((item for item in candidates if item.is_file()), None)
            if path is None:
                continue
            robot_name = robot_dir.name
            normalized_name = re.sub(r"[^a-z0-9]+", "_", robot_name.lower()).strip("_")
            semantic = robot_name.replace("_", " ")
            tags = ["robot", "geniesim", normalized_name]
            metadata: dict[str, Any] = {"source_library": "geniesim", "entrypoint": path.name}
            if normalized_name == "g2_omnipicker":
                semantic = "GenieSim G2 omnipicker wheeled humanoid mobile dual arm robot"
                tags.extend(["wheeled_humanoid", "mobile_humanoid", "mobile_dual_arm", "transport_robot", "轮式人形机器人"])
                metadata["preferred_for"] = ["wheeled humanoid", "mobile dual-arm robot", "轮式人形机器人"]
            elif normalized_name == "g1_omnipicker":
                semantic = "GenieSim G1 omnipicker dual arm humanoid robot"
                tags.extend(["humanoid", "dual_arm", "manipulation_robot"])
            records.append(_make_record(
                library=spec["name"],
                asset_id=f"geniesim_{normalized_name}",
                asset_type="robot",
                path=path,
                root=root,
                semantic_name=semantic,
                category=f"robot/{robot_name}",
                tags=tags,
                metadata=metadata,
            ))
    return records


def _scan_curated(spec: dict[str, Any]) -> list[dict[str, Any]]:
    root = Path(spec["root"])
    records = []
    if not root.exists():
        return records
    for raw in spec.get("assets", []) or []:
        if not isinstance(raw, dict) or not raw.get("asset_id") or not raw.get("path"):
            continue
        path = Path(str(raw["path"]))
        if not path.is_absolute():
            path = root / path
        if not path.exists():
            continue
        records.append(_make_record(
            library=spec["name"],
            asset_id=str(raw["asset_id"]),
            asset_type=str(raw.get("asset_type") or _infer_usd_type(path, root)),
            path=path,
            root=root,
            semantic_name=str(raw.get("semantic_name") or raw["asset_id"]),
            category=str(raw.get("category") or "curated"),
            tags=[str(tag) for tag in raw.get("tags", []) or []],
            metadata=raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {},
        ))
    return records


def _bundle_path(root: Path, package: dict[str, Any]) -> Path:
    value = package.get("path") or package.get("package") or ""
    path = Path(str(value))
    if path.is_absolute() and path.exists():
        return path
    package_name = str(package.get("package") or path.name)
    rebased = root / package_name
    if rebased.exists():
        return rebased
    return path if path.is_absolute() else root / path


def _path_under(root: Path, value: Any, fallback: str | None = None) -> Path:
    raw = value if value not in (None, "", [], {}) else fallback
    path = Path(str(raw or ""))
    if not path.is_absolute():
        return root / path
    if path.exists():
        return path
    if root.name in path.parts:
        index = path.parts.index(root.name)
        rebased = root.joinpath(*path.parts[index + 1 :])
        if rebased.exists():
            return rebased
    fallback_path = root / path.name
    return fallback_path if fallback_path.exists() else path


def _scan_fabrica_task_bundles(spec: dict[str, Any]) -> list[dict[str, Any]]:
    root = Path(spec["root"])
    if not root.exists():
        return []
    index = _read_json(root / "ASSET_INDEX.json")
    packages = index.get("packages") if isinstance(index.get("packages"), list) else None
    if not packages:
        packages = [
            {"package": p.name, "path": str(p)}
            for p in root.iterdir()
            if p.is_dir() and (p / "PACKAGE_MANIFEST.json").exists()
        ]

    records: list[dict[str, Any]] = []
    for package in packages:
        if not isinstance(package, dict):
            continue
        package_root = _bundle_path(root, package)
        manifest = _read_json(package_root / "PACKAGE_MANIFEST.json")
        scene_spec = _read_json(package_root / "scene" / "scene_spec.json")
        if not manifest and not scene_spec:
            continue
        assembly = str(manifest.get("assembly") or package.get("assembly") or package_root.name)
        package_name = str(manifest.get("package_name") or package.get("package") or package_root.name)
        entrypoints = manifest.get("entrypoints") if isinstance(manifest.get("entrypoints"), dict) else {}
        scene_usda = _path_under(package_root, entrypoints.get("scene_usda") or package.get("scene_usda"), "scene/scene.usda")
        scene_spec_path = _path_under(package_root, entrypoints.get("scene_spec_json") or package.get("scene_spec_json"), "scene/scene_spec.json")
        pickup_json = _path_under(package_root, entrypoints.get("pickup_json") or package.get("pickup_json"), f"assets/fabrica_official_fixture/{assembly}/pickup.json")
        plan_info_pkl = _path_under(package_root, entrypoints.get("plan_info_pkl") or package.get("plan_info_pkl"), "metadata/plan_info.pkl")
        physics = scene_spec.get("physics_alignment") if isinstance(scene_spec.get("physics_alignment"), dict) else {}
        collision = physics.get("collision") if isinstance(physics.get("collision"), dict) else {}
        sdf_margin = package.get("sdf_margin_m", collision.get("sdf_margin_m", 0.001))
        sdf_resolution = package.get("sdf_resolution", collision.get("sdf_resolution", 512))
        common_tags = [
            "fabrica",
            "official",
            "task_bundle",
            "assembly_recipe",
            "sdf",
            "sdf001",
            "fixture",
            "pickup_fixture",
            "optical_board",
            assembly,
            package_name,
        ]

        recipe_meta = {
            "package_root": str(package_root),
            "package_name": package_name,
            "scene_usda": str(scene_usda),
            "scene_spec_json": str(scene_spec_path),
            "pickup_json": str(pickup_json),
            "plan_info_pkl": str(plan_info_pkl),
            "part_count": manifest.get("part_count") or package.get("part_count"),
            "plan_steps": manifest.get("plan_steps") or package.get("plan_steps"),
            "validation_status": manifest.get("validation_status") or package.get("validation_status"),
            "collision_approximation": "sdf",
            "sdf_resolution": sdf_resolution,
            "sdf_margin_m": sdf_margin,
            "part_density_kg_per_m3": physics.get("part_density_kg_per_m3"),
            "part_friction": physics.get("part_friction"),
        }

        records.append(_make_record(
            library=spec["name"],
            asset_id=f"fabrica_{assembly}_official_set_sdf001",
            asset_type="assembly_set",
            path=scene_usda,
            root=root,
            semantic_name=f"Official Fabrica {assembly} assembly set with fixture, board, pickup poses, and final poses",
            category="fabrica/task_bundle",
            tags=[*common_tags, "assembly_set"],
            metadata=recipe_meta,
            assembly_name=assembly,
            package_root=str(package_root),
            part_count=recipe_meta["part_count"],
            scene_usda=str(scene_usda),
            scene_spec_json=str(scene_spec_path),
            pickup_json=str(pickup_json),
            plan_info_pkl=str(plan_info_pkl),
        ))
        records.append(_make_record(
            library=spec["name"],
            asset_id=f"fabrica_{assembly}_official_workcell_recipe",
            asset_type="workcell_recipe",
            path=scene_usda,
            root=root,
            semantic_name=f"Official Fabrica {assembly} workcell recipe scene",
            category="fabrica/task_bundle",
            tags=[*common_tags, "recipe", "workcell", "scene"],
            metadata=recipe_meta,
            assembly_name=assembly,
            package_root=str(package_root),
            scene_usda=str(scene_usda),
            scene_spec_json=str(scene_spec_path),
        ))

        assets = scene_spec.get("assets") if isinstance(scene_spec.get("assets"), dict) else {}
        for key, asset_type, label in (
            ("optical_board", "locating_board", "optical locating board"),
            ("fixture", "pickup_fixture", "pickup fixture"),
        ):
            raw = assets.get(key) if isinstance(assets.get(key), dict) else {}
            usd_path = _path_under(package_root, raw.get("usd_path"))
            if usd_path.exists():
                records.append(_make_record(
                    library=spec["name"],
                    asset_id=f"fabrica_{assembly}_{asset_type}",
                    asset_type=asset_type,
                    path=usd_path,
                    root=root,
                    semantic_name=f"Fabrica {assembly} {label}",
                    category=f"fabrica/{assembly}",
                    tags=[*common_tags, asset_type, key],
                    metadata={**recipe_meta, **raw},
                    assembly_name=assembly,
                    package_root=str(package_root),
                ))

        scene = scene_spec.get("scene") if isinstance(scene_spec.get("scene"), dict) else {}
        parts = scene.get("parts") if isinstance(scene.get("parts"), dict) else {}
        for part_id, part in sorted(parts.items(), key=lambda item: str(item[0])):
            if not isinstance(part, dict):
                continue
            usd_path = _path_under(package_root, part.get("relative_usd_path") or part.get("usd_path"))
            if not usd_path.exists():
                continue
            records.append(_make_record(
                library=spec["name"],
                asset_id=f"fabrica_{assembly}_{part_id}",
                asset_type="assembly_part",
                path=usd_path,
                root=root,
                semantic_name=f"Fabrica {assembly} part {part_id}",
                category=f"fabrica/{assembly}",
                tags=[*common_tags, "part", "dynamic"],
                metadata={**recipe_meta, **part},
                assembly_name=assembly,
                package_root=str(package_root),
                part_index=int(part_id) if str(part_id).isdigit() else None,
            ))
    return records


def scan_library(spec: dict[str, Any]) -> list[dict[str, Any]]:
    scanner = spec.get("scanner", "usd_tree")
    if scanner == "sqlite_asset_index":
        return []
    if scanner == "unique_asset_index":
        return _scan_usd_tree(spec)
    if scanner == "curated":
        return _scan_curated(spec)
    if scanner == "geniesim":
        return _scan_geniesim(spec)
    if scanner == "fabrica_task_bundle":
        return _scan_fabrica_task_bundles(spec)
    return _scan_usd_tree(spec)


def _sqlite_index_path(spec: dict[str, Any]) -> Path:
    return Path(str(spec.get("index_path") or spec.get("root") or ""))


def _sqlite_record_count(spec: dict[str, Any]) -> int | None:
    path = _sqlite_index_path(spec)
    if not path.is_file():
        return None
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        count = int(connection.execute("SELECT COUNT(*) FROM assets WHERE callable=1").fetchone()[0])
        connection.close()
        return count
    except (sqlite3.Error, OSError, TypeError, ValueError):
        return None


def _unique_index_record_count(spec: dict[str, Any]) -> int | None:
    path = _sqlite_index_path(spec)
    if not path.is_file():
        return None
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        has_entries = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='asset_entries'"
        ).fetchone()
        if has_entries:
            count = int(connection.execute(
                "SELECT COUNT(*) FROM asset_entries WHERE library=? AND callable=1",
                (str(spec.get("name")),),
            ).fetchone()[0])
        else:
            count = int(connection.execute("SELECT COUNT(*) FROM canonical_assets WHERE extension IN ('.usd','.usda','.usdc')").fetchone()[0])
        connection.close()
        return count
    except (sqlite3.Error, OSError, TypeError, ValueError):
        return None


def _search_sqlite_asset_index(
    spec: dict[str, Any], query: str, *, types: list[str] | None = None, topk: int = 10
) -> list[dict[str, Any]]:
    path = _sqlite_index_path(spec)
    if not path.is_file():
        return []
    tokens = [token for token in _tokenize(query) if re.fullmatch(r"[a-z0-9]+", token)]
    if not tokens:
        return []
    fts_query = " OR ".join(f'"{token}"*' for token in dict.fromkeys(tokens))
    filters = ["assets_fts MATCH ?", "a.callable=1"]
    params: list[Any] = [fts_query]
    kind_map = {
        "background": ["scene", "environment", "layout", "articulated_layout"],
        "environment": ["scene", "environment", "layout", "articulated_layout"],
        "object": ["object", "articulated_object", "industrial_prop", "usd_asset"],
        "robot": ["robot", "articulated_object", "industrial_prop"],
    }
    if types:
        kinds = sorted({kind for asset_type in types for kind in kind_map.get(asset_type, [asset_type])})
        placeholders = ",".join("?" for _ in kinds)
        filters.append(f"a.kind IN ({placeholders})")
        params.extend(kinds)
    params.append(max(10, min(topk * 8, 200)))
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            f"""
            SELECT a.asset_id, a.label, a.library, a.kind, a.category, a.readiness,
                   a.risk, a.path, a.relative_path, a.metadata_path, a.interactive_metadata_path
            FROM assets_fts
            JOIN assets a ON a.rowid=assets_fts.rowid
            WHERE {' AND '.join(filters)}
            ORDER BY bm25(assets_fts), a.entry_rank DESC, a.asset_id
            LIMIT ?
            """,
            params,
        ).fetchall()
        connection.close()
    except (sqlite3.Error, OSError):
        return []
    records: list[dict[str, Any]] = []
    for row in rows:
        asset_id = str(row["asset_id"])
        records.append({
            "library": str(row["library"] or spec.get("name")),
            "namespaced_id": asset_id,
            "asset_id": asset_id.split(":", 1)[-1],
            "asset_type": str(row["kind"]),
            "path": str(row["path"]),
            "payload_path": str(row["path"]),
            "relative_path": str(row["relative_path"] or ""),
            "semantic_name": str(row["label"] or Path(str(row["path"])).stem),
            "category": str(row["category"] or ""),
            "tags": sorted(set([str(row["kind"]), str(row["library"]), *_tokenize(str(row["category"] or ""))])),
            "metadata": {
                "metadata_path": row["metadata_path"],
                "interactive_metadata_path": row["interactive_metadata_path"],
                "catalog": str(path),
            },
            "readiness": str(row["readiness"] or "unknown"),
            "risk": str(row["risk"] or ""),
        })
    return records


def _search_unique_asset_index(
    spec: dict[str, Any],
    query: str,
    *,
    types: list[str] | None = None,
    topk: int = 10,
) -> list[dict[str, Any]]:
    path = _sqlite_index_path(spec)
    if not path.is_file():
        return []
    tokens = [token for token in _tokenize(query) if re.fullmatch(r"[a-z0-9]+", token)]
    if not tokens:
        return []
    fts_query = " OR ".join(f'"{token}"*' for token in dict.fromkeys(tokens))
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        has_entries = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='asset_entries'"
        ).fetchone()
        if has_entries:
            filters = ["asset_entry_search MATCH ?", "ae.library=?", "ae.callable=1"]
            params: list[Any] = [fts_query, str(spec.get("name"))]
            if types:
                placeholders = ",".join("?" for _ in types)
                filters.append(f"ae.asset_type IN ({placeholders})")
                params.extend(str(item) for item in types)
            params.append(max(10, min(topk * 8, 200)))
            rows = connection.execute(
                f"""
                SELECT ae.id, ae.asset_id, ae.name, ae.semantic_name,
                       ae.asset_type, ae.category, ae.rel_path, ae.extension,
                       ae.source, ae.library, ae.path, ae.entry_rank,
                       ae.support_reason, ae.is_canonical, ca.size, ca.sha256,
                       ca.location_count
                FROM asset_entry_search
                JOIN asset_entries ae ON ae.id=asset_entry_search.rowid
                LEFT JOIN canonical_assets ca ON ca.id=ae.canonical_asset_id
                WHERE {' AND '.join(filters)}
                ORDER BY bm25(asset_entry_search), ae.entry_rank DESC,
                         ae.is_canonical DESC, ae.rel_path
                LIMIT ?
                """,
                params,
            ).fetchall()
        else:
            rows = connection.execute(
                """
                SELECT ca.id, NULL AS asset_id, ca.name, ca.name AS semantic_name,
                       NULL AS asset_type, CAST(path AS TEXT) AS category,
                       ca.rel_path, ca.extension, ca.source,
                       NULL AS library, ca.path, 0 AS entry_rank,
                       NULL AS support_reason, 1 AS is_canonical,
                       ca.size, ca.sha256, ca.location_count
                FROM asset_search
                JOIN canonical_assets ca ON ca.id=asset_search.rowid
                WHERE asset_search MATCH ? AND ca.extension IN ('.usd','.usda','.usdc')
                ORDER BY bm25(asset_search), ca.location_count DESC, ca.rel_path
                LIMIT ?
                """,
                (fts_query, max(10, min(topk * 8, 200))),
            ).fetchall()
        connection.close()
    except (sqlite3.Error, OSError):
        return []
    root = Path(str(spec.get("root") or ""))
    records: list[dict[str, Any]] = []
    for row in rows:
        asset_path = Path(str(row["path"]))
        asset_type = str(row["asset_type"] or _infer_usd_type(asset_path, root))
        asset_id = str(row["asset_id"] or f"official_{row['id']}_{re.sub(r'[^a-zA-Z0-9]+', '_', str(row['name'])).strip('_')}")
        records.append(_make_record(
            library=str(spec.get("name")),
            asset_id=asset_id,
            asset_type=asset_type,
            path=asset_path,
            root=root,
            semantic_name=str(row["semantic_name"] or row["name"]),
            category=str(row["category"] or Path(str(row["rel_path"])).parent),
            tags=["isaac", "official", str(row["source"]), asset_type],
            metadata={
                "catalog": str(path),
                "sha256": row["sha256"],
                "size_bytes": row["size"],
                "location_count": row["location_count"],
                "canonical_source": row["source"],
                "catalog_schema": 2 if has_entries else 1,
                "entry_rank": row["entry_rank"],
                "support_reason": row["support_reason"],
                "is_canonical": bool(row["is_canonical"]),
            },
        ))
    return records


def asset_inventory(config: dict[str, Any], *, library: str | None = None) -> dict[str, Any]:
    spec = next((item for item in _library_specs(config) if item.get("scanner") == "unique_asset_index"), None)
    if not spec:
        return {"status": "pending", "reason": "unique_asset_index_not_configured", "libraries": []}
    path = _sqlite_index_path(spec)
    if not path.is_file():
        return {"status": "pending", "reason": "index_not_available", "index_path": str(path), "libraries": []}
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        has_entries = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='asset_entries'").fetchone()
        if not has_entries:
            connection.close()
            return {"status": "pending", "reason": "index_schema_v2_required", "index_path": str(path), "libraries": []}
        filters = ["1=1"]
        params: list[Any] = []
        if library:
            filters.append("library=?")
            params.append(library)
        rows = connection.execute(
            f"""SELECT library, asset_type, COUNT(*) AS usd_entries,
                       SUM(callable) AS callable_entries,
                       SUM(CASE WHEN callable=0 THEN 1 ELSE 0 END) AS support_entries
                FROM asset_entries WHERE {' AND '.join(filters)}
                GROUP BY library, asset_type ORDER BY library, asset_type""",
            params,
        ).fetchall()
        meta = {}
        for key, value in connection.execute("SELECT key, value FROM meta"):
            try:
                meta[str(key)] = json.loads(value)
            except (TypeError, json.JSONDecodeError):
                meta[str(key)] = value
        connection.close()
    except (sqlite3.Error, OSError) as exc:
        return {"status": "fail", "reason": "index_read_failed", "index_path": str(path), "error": str(exc), "libraries": []}
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        item = grouped.setdefault(str(row["library"]), {"library": str(row["library"]), "usd_entries": 0, "callable_entries": 0, "support_entries": 0, "types": []})
        type_row = {"asset_type": str(row["asset_type"]), "usd_entries": int(row["usd_entries"] or 0), "callable_entries": int(row["callable_entries"] or 0), "support_entries": int(row["support_entries"] or 0)}
        item["types"].append(type_row)
        item["usd_entries"] += type_row["usd_entries"]
        item["callable_entries"] += type_row["callable_entries"]
        item["support_entries"] += type_row["support_entries"]
    libraries = list(grouped.values())
    return {"status": "pass", "index_path": str(path), "schema_version": meta.get("schema_version"), "created_at": meta.get("created_at"), "library_count": len(libraries), "usd_entries": sum(item["usd_entries"] for item in libraries), "callable_entries": sum(item["callable_entries"] for item in libraries), "support_entries": sum(item["support_entries"] for item in libraries), "libraries": libraries}


def build_asset_index(
    config: dict[str, Any],
    *,
    libraries: list[str] | None = None,
    force_refresh: bool = False,
) -> list[dict[str, Any]]:
    requested = set(libraries or [])
    selected = [
        lib for lib in _library_specs(config)
        if not requested or lib.get("name") in requested
    ]
    cache = _cache_path(config, libraries)
    if cache.exists() and not force_refresh:
        cached = _read_json(cache)
        records = cached.get("records")
        if isinstance(records, list):
            return records

    records: list[dict[str, Any]] = []
    for spec in selected:
        records.extend(scan_library(spec))
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(_json({"created_at": time.time(), "records": records}), encoding="utf-8")
    return records


def list_libraries(
    config: dict[str, Any],
    *,
    counts: bool = False,
    include_slow_counts: bool = False,
) -> list[dict[str, Any]]:
    rows = []
    slow = set(_slow_libraries(config))
    for spec in _library_specs(config):
        display_root = spec.get("index_path") or spec.get("root")
        row = {
            "name": spec.get("name"),
            "scanner": spec.get("scanner"),
            "root": display_root,
            "exists": spec.get("exists"),
            "description": spec.get("description", ""),
        }
        if counts:
            if spec.get("name") in slow and not include_slow_counts:
                row["record_count"] = None
                row["count_note"] = "skipped slow library; pass --include-slow-counts to count it"
            else:
                if spec.get("scanner") == "sqlite_asset_index":
                    row["record_count"] = _sqlite_record_count(spec)
                elif spec.get("scanner") == "unique_asset_index":
                    row["record_count"] = _unique_index_record_count(spec)
                else:
                    row["record_count"] = len(build_asset_index(config, libraries=[str(spec["name"])]))
        rows.append(row)
    return rows


def _haystack(record: dict[str, Any]) -> str:
    parts = [
        record.get("namespaced_id", ""),
        record.get("asset_id", ""),
        record.get("asset_type", ""),
        record.get("semantic_name", ""),
        record.get("category", ""),
        record.get("relative_path", ""),
        " ".join(str(tag) for tag in record.get("tags", [])),
        str(record.get("assembly_name", "")),
    ]
    return " ".join(parts).lower()


def _score(record: dict[str, Any], query: str) -> float:
    query_l = query.lower().strip()
    if not query_l:
        return 1.0
    hay = _haystack(record)
    asset_id = str(record.get("asset_id", "")).lower()
    namespaced_id = str(record.get("namespaced_id", "")).lower()
    semantic_name = str(record.get("semantic_name", "")).lower()
    assembly = str(record.get("assembly_name", "")).lower()
    if query_l in {asset_id, namespaced_id, assembly}:
        return 1.0
    if query_l in asset_id or query_l in namespaced_id:
        return 0.9
    if query_l in semantic_name:
        return 0.85
    if query_l in hay:
        return 0.65
    q_tokens = set(_tokenize(query_l))
    h_tokens = set(_tokenize(hay))
    if not q_tokens:
        return 0.0
    overlap = q_tokens & h_tokens
    if not overlap:
        return 0.0
    score = 0.2 + 0.1 * len(overlap)
    asset_tokens = set(_tokenize(asset_id))
    semantic_tokens = set(_tokenize(str(record.get("semantic_name", ""))))
    category_tokens = set(_tokenize(str(record.get("category", ""))))
    if overlap & asset_tokens:
        score += 0.2
    if overlap & semantic_tokens:
        score += 0.1
    if overlap & category_tokens:
        score += 0.05
    if q_tokens <= h_tokens:
        score += 0.25
    if record.get("asset_type") in {"assembly_set", "workcell_recipe"} and assembly in q_tokens:
        score += 0.2
    return min(score, 0.95)


def search_assets(
    config: dict[str, Any],
    query: str,
    *,
    libraries: list[str] | None = None,
    types: list[str] | None = None,
    topk: int = 10,
    force_refresh: bool = False,
    allow_slow: bool = False,
    exhaustive: bool = False,
) -> list[dict[str, Any]]:
    query_info = expand_query(SKILL_ROOT, query)
    effective_query = str(query_info["expanded"])
    preferred_terms = {
        token
        for values in query_info["matched_synonyms"].values()
        for value in values[:1]
        for token in _tokenize(str(value))
        if re.fullmatch(r"[a-z0-9]+", token)
    }
    effective_libraries = _effective_search_libraries(config, libraries)
    records: list[dict[str, Any]] = []
    ranked: list[dict[str, Any]] = []

    specs_by_name = {str(spec["name"]): spec for spec in _library_specs(config)}

    def records_for(library: str) -> list[dict[str, Any]]:
        spec = specs_by_name.get(library)
        if spec and spec.get("scanner") == "sqlite_asset_index":
            return _search_sqlite_asset_index(spec, effective_query, types=types, topk=topk)
        if spec and spec.get("scanner") == "unique_asset_index":
            return _search_unique_asset_index(spec, effective_query, types=types, topk=topk)
        return build_asset_index(config, libraries=[library], force_refresh=force_refresh)

    staged = _search_policy(config).get("staged_default_search", True) is not False
    if libraries is None and not exhaustive and staged and isinstance(effective_libraries, list):
        for library in effective_libraries:
            records.extend(records_for(library))
            ranked = _rank_records(records, effective_query, original_query=query, preferred_terms=preferred_terms, types=types, topk=topk)
            if _should_stop_default_search(config, ranked, query=effective_query, topk=topk):
                break
    else:
        for library in effective_libraries or []:
            records.extend(records_for(library))
        ranked = _rank_records(records, effective_query, original_query=query, preferred_terms=preferred_terms, types=types, topk=topk)

    if libraries is None and allow_slow and len(ranked) < topk:
        slow_libraries = _slow_libraries(config)
        if slow_libraries:
            slow_records = [record for library in slow_libraries for record in records_for(library)]
            by_key: dict[str, dict[str, Any]] = {}
            for record in [*records, *slow_records]:
                key = str(record.get("namespaced_id") or record.get("path") or id(record))
                by_key[key] = record
            ranked = _rank_records(list(by_key.values()), effective_query, original_query=query, preferred_terms=preferred_terms, types=types, topk=topk)
    for record in ranked:
        retrieval = record.setdefault("retrieval", {})
        if isinstance(retrieval, dict):
            retrieval.update({
                "query": query,
                "expanded_query": effective_query,
                "matched_synonyms": query_info["matched_synonyms"],
            })
    return ranked


def _should_stop_default_search(
    config: dict[str, Any],
    ranked: list[dict[str, Any]],
    *,
    query: str,
    topk: int,
) -> bool:
    if not ranked:
        return False
    policy = _search_policy(config)
    if policy.get("staged_default_search", True) is False:
        return False

    stop_after = int(policy.get("default_stop_after_results", 5))
    if len(ranked) >= min(topk, stop_after) and float(ranked[0].get("_retrieval_score", 0.0)) >= 0.75:
        return True

    best = ranked[0]
    best_score = _score(best, query)
    curated_threshold = float(policy.get("curated_high_confidence_score", 0.8))
    if best.get("library") == "curated_common" and best_score >= curated_threshold:
        return True

    fabrica_threshold = float(policy.get("fabrica_recipe_confidence_score", 0.6))
    if best.get("library") == "fabrica_task_bundles" and best.get("asset_type") in {"assembly_set", "workcell_recipe"}:
        return best_score >= fabrica_threshold
    return False


def _rank_records(
    records: list[dict[str, Any]],
    query: str,
    *,
    original_query: str | None = None,
    preferred_terms: set[str] | None = None,
    types: list[str] | None = None,
    topk: int = 10,
) -> list[dict[str, Any]]:
    if types:
        wanted = set(types)
        records = [r for r in records if r.get("asset_type") in wanted]
    generic_terms = {
        "robot", "arm", "manipulator", "scene", "environment", "background",
        "asset", "object", "assembly", "workcell", "table", "room", "indoor",
    }
    required_terms = {
        token for token in _tokenize(original_query or "")
        if re.fullmatch(r"[a-z0-9]+", token) and len(token) >= 3 and token not in generic_terms
    }
    scored = []
    for record in records:
        score = _score(record, query)
        hay_tokens = set(_tokenize(_haystack(record)))
        if required_terms:
            missing = required_terms - hay_tokens
            if missing:
                score *= 0.35
        if preferred_terms:
            coverage = len(preferred_terms & hay_tokens) / len(preferred_terms)
            score *= 0.65 + 0.35 * coverage
        if record.get("library") == "curated_common":
            score += 0.3
        elif record.get("library") == "fabrica_task_bundles" and record.get("asset_type") in {"assembly_set", "workcell_recipe"}:
            score += 0.1
        score = min(score, 0.999)
        scored.append((score, record))
    scored = [item for item in scored if item[0] > 0]
    scored.sort(key=lambda item: (item[0], item[1].get("asset_type") in {"assembly_set", "workcell_recipe"}), reverse=True)
    ranked: list[dict[str, Any]] = []
    for score, record in scored[:topk]:
        row = dict(record)
        row["_retrieval_score"] = round(float(score), 6)
        ranked.append(row)
    return ranked


def get_asset(config: dict[str, Any], value: str, *, libraries: list[str] | None = None) -> dict[str, Any] | None:
    needle = value.lower()
    effective_libraries = _effective_search_libraries(config, libraries)
    for record in build_asset_index(config, libraries=effective_libraries):
        candidates = {
            str(record.get("asset_id", "")).lower(),
            str(record.get("namespaced_id", "")).lower(),
            str(record.get("assembly_name", "")).lower(),
            str(record.get("path", "")).lower(),
        }
        if needle in candidates:
            return record
    hits = search_assets(config, value, libraries=libraries, topk=1)
    return hits[0] if hits else None


def get_fabrica_recipe(config: dict[str, Any], assembly: str) -> dict[str, Any] | None:
    records = build_asset_index(config, libraries=["fabrica_task_bundles"])
    needle = assembly.lower()
    for record in records:
        if record.get("asset_type") == "assembly_set" and str(record.get("assembly_name", "")).lower() == needle:
            return record
    hits = search_assets(
        config,
        assembly,
        libraries=["fabrica_task_bundles"],
        types=["assembly_set"],
        topk=1,
    )
    return hits[0] if hits else None


def list_home_backgrounds(
    config: dict[str, Any],
    *,
    room_type: str | None = None,
    with_health: bool = False,
) -> list[dict[str, Any]]:
    """Return curated home/indoor backgrounds resolved against the GenieSim root."""
    geniesim_root = _library_root(config, "geniesim")
    room_key = room_type.lower().replace("-", "_").replace(" ", "_") if room_type else None
    rows: list[dict[str, Any]] = []
    for item in HOME_BACKGROUND_CATALOG:
        room_types = [str(v) for v in item.get("room_types", [])]
        if room_key and room_key not in {v.lower() for v in room_types}:
            continue
        path = (geniesim_root / str(item["relative_path"])) if geniesim_root else Path(str(item["relative_path"]))
        row = {
            **item,
            "path": str(path),
            "exists": path.exists(),
            "library": "geniesim",
        }
        if with_health and path.exists():
            row["health"] = inspect_asset_health(str(path))
        rows.append(row)
    return rows


def _looks_like_external_reference(value: str) -> bool:
    lowered = value.lower()
    return (
        lowered.startswith(("http://", "https://", "omniverse://", "anon:", "asset:"))
        or "${" in value
        or "<" in value
    )


def _resolve_authored_asset_path(base_dir: Path, value: str) -> Path | None:
    value = value.strip()
    if not value or _looks_like_external_reference(value):
        return None
    path = Path(value)
    if path.is_absolute():
        return path
    return (base_dir / path).resolve()


def inspect_asset_health(asset_path: str) -> dict[str, Any]:
    """Fast text-level USD reference and texture health check.

    This intentionally does not require pxr so agents can run it before Isaac Sim
    Python is available. For binary/crate USD files it can only report that text
    inspection was limited.
    """
    path = Path(asset_path)
    result: dict[str, Any] = {
        "asset_path": str(path),
        "exists": path.exists(),
        "status": "fail",
        "checked_as_text": False,
        "authored_reference_count": 0,
        "texture_reference_count": 0,
        "missing_references": [],
        "missing_textures": [],
        "external_or_unchecked_references": [],
    }
    if not path.exists():
        result["errors"] = ["asset file does not exist"]
        return result

    try:
        raw = path.read_bytes()
    except OSError as exc:
        result["errors"] = [f"read failed: {exc}"]
        return result

    sample = raw[:2_000_000]
    checked_as_text = path.suffix.lower() == ".usda" or b"\0" not in sample[:4096]
    result["checked_as_text"] = checked_as_text
    if not checked_as_text:
        result["status"] = "warn"
        result["warnings"] = ["binary/crate USD; run Isaac Sim validation for full dependency checks"]
        return result

    text = sample.decode("utf-8", errors="ignore")
    authored = []
    skip_delete_block = False
    for line in text.splitlines():
        stripped = line.strip()
        if skip_delete_block:
            if "]" in stripped:
                skip_delete_block = False
            continue
        if "delete references" in stripped:
            if "[" in stripped and "]" not in stripped:
                skip_delete_block = True
            continue
        for match in re.finditer(r"@([^@\n\r]+)@", line):
            value = match.group(1).strip()
            if value:
                authored.append(value)
        # Some USD assets author texture paths as plain strings rather than @asset@ references.
        authored.extend(
            m.group(1).strip()
            for m in re.finditer(
                r'"([^"\n\r]+\.(?:png|jpg|jpeg|tga|exr|hdr|bmp|tif|tiff))"',
                line,
                flags=re.IGNORECASE,
            )
        )

    missing_references: list[str] = []
    missing_textures: list[str] = []
    external_or_unchecked: list[str] = []
    seen: set[str] = set()
    for value in authored:
        if value in seen:
            continue
        seen.add(value)
        suffix = Path(value.split("?")[0]).suffix.lower()
        resolved = _resolve_authored_asset_path(path.parent, value)
        if resolved is None:
            external_or_unchecked.append(value)
            continue
        if not resolved.exists():
            if suffix in TEXTURE_SUFFIXES:
                missing_textures.append(value)
            else:
                missing_references.append(value)

    texture_count = sum(1 for value in seen if Path(value.split("?")[0]).suffix.lower() in TEXTURE_SUFFIXES)
    result.update({
        "authored_reference_count": len(seen),
        "texture_reference_count": texture_count,
        "missing_references": missing_references,
        "missing_textures": missing_textures,
        "external_or_unchecked_references": external_or_unchecked,
    })
    if missing_references or missing_textures:
        result["status"] = "warn"
    else:
        result["status"] = "pass"
    return result


def _require_pxr() -> Any:
    try:
        from pxr import Gf, Sdf, Usd, UsdGeom, UsdLux, UsdPhysics  # type: ignore
    except Exception as exc:  # pragma: no cover - depends on Isaac Sim Python.
        raise SystemExit(
            "pxr modules are required. Run this command with Isaac Sim python.sh."
        ) from exc
    return Gf, Sdf, Usd, UsdGeom, UsdLux, UsdPhysics


def _ensure_parent_prims(stage: Any, prim_path: str) -> None:
    parts = [part for part in prim_path.strip("/").split("/")[:-1] if part]
    current = ""
    for part in parts:
        current += "/" + part
        if not stage.GetPrimAtPath(current):
            stage.DefinePrim(current, "Xform")


def _vec3_for_xform_op(Gf: Any, UsdGeom: Any, op: Any, values: Any) -> Any:
    vals = [float(v) for v in values]
    if op.GetPrecision() == UsdGeom.XformOp.PrecisionFloat:
        return Gf.Vec3f(*vals)
    return Gf.Vec3d(*vals)


def _existing_xform_op(UsdGeom: Any, xform: Any, op_type: Any, attr_name: str) -> Any | None:
    for op in xform.GetOrderedXformOps():
        if not op.IsInverseOp() and op.GetOpType() == op_type:
            return op
    attr = xform.GetPrim().GetAttribute(attr_name)
    if attr:
        return UsdGeom.XformOp(attr)
    return None


def _set_transform(prim: Any, translate: Any = None, rotate: Any = None, scale: Any = None) -> None:
    Gf, _Sdf, _Usd, UsdGeom, _UsdLux, _UsdPhysics = _require_pxr()
    xform = UsdGeom.Xformable(prim)
    ordered_ops = []
    if translate is not None:
        op = _existing_xform_op(UsdGeom, xform, UsdGeom.XformOp.TypeTranslate, "xformOp:translate")
        if op is None:
            op = xform.AddTranslateOp(precision=UsdGeom.XformOp.PrecisionDouble)
        op.Set(_vec3_for_xform_op(Gf, UsdGeom, op, translate))
        ordered_ops.append(op)
    if rotate is not None:
        op = _existing_xform_op(UsdGeom, xform, UsdGeom.XformOp.TypeRotateXYZ, "xformOp:rotateXYZ")
        if op is None:
            op = xform.AddRotateXYZOp(precision=UsdGeom.XformOp.PrecisionDouble)
        op.Set(_vec3_for_xform_op(Gf, UsdGeom, op, rotate))
        ordered_ops.append(op)
    if scale is not None:
        op = _existing_xform_op(UsdGeom, xform, UsdGeom.XformOp.TypeScale, "xformOp:scale")
        if op is None:
            op = xform.AddScaleOp(precision=UsdGeom.XformOp.PrecisionDouble)
        op.Set(_vec3_for_xform_op(Gf, UsdGeom, op, scale))
        ordered_ops.append(op)
    xform.SetXformOpOrder(ordered_ops)


def _look_at_up_axis(eye_values: Any, target_values: Any) -> tuple[float, float, float]:
    eye_components = [float(value) for value in eye_values]
    target_components = [float(value) for value in target_values]
    delta = [
        target_components[index] - eye_components[index]
        for index in range(3)
    ]
    length = math.sqrt(sum(value * value for value in delta))
    if length <= 1e-9:
        raise ValueError("look_at target must differ from translate")
    direction = [value / length for value in delta]
    # Gf.Matrix4d.SetLookAt becomes singular when the view direction is
    # parallel to the up vector. Ceiling lights commonly point straight down,
    # so use Y-up for near-vertical rays and Z-up otherwise.
    return (
        (0.0, 1.0, 0.0)
        if abs(direction[2]) > 0.999
        else (0.0, 0.0, 1.0)
    )


def _look_at_matrix(Gf: Any, eye_values: Any, target_values: Any) -> Any:
    eye_components = [float(value) for value in eye_values]
    target_components = [float(value) for value in target_values]
    up = Gf.Vec3d(*_look_at_up_axis(eye_components, target_components))
    eye = Gf.Vec3d(*eye_components)
    target = Gf.Vec3d(*target_components)
    return Gf.Matrix4d().SetLookAt(eye, target, up).GetInverse()


def _source_unit_metadata(asset_path: str) -> dict[str, Any]:
    cached = _UNIT_METADATA_CACHE.get(asset_path)
    if cached is not None:
        return dict(cached)
    _Gf, _Sdf, Usd, UsdGeom, _UsdLux, _UsdPhysics = _require_pxr()
    try:
        source_stage = Usd.Stage.Open(str(asset_path))
    except Exception as exc:
        result = {"opened": False, "authored": False, "meters_per_unit": None, "error": f"{type(exc).__name__}: {exc}"}
    else:
        if source_stage is None:
            result = {"opened": False, "authored": False, "meters_per_unit": None, "error": "Usd.Stage.Open returned None"}
        else:
            result = {
                "opened": True,
                "authored": bool(source_stage.HasAuthoredMetadata("metersPerUnit")),
                "meters_per_unit": float(UsdGeom.GetStageMetersPerUnit(source_stage)),
                "error": None,
            }
    _UNIT_METADATA_CACHE[asset_path] = dict(result)
    return result


def inspect_asset_units(asset_path: str, *, target_meters_per_unit: float = 1.0) -> dict[str, Any]:
    Gf, _Sdf, Usd, UsdGeom, _UsdLux, _UsdPhysics = _require_pxr()
    path = Path(asset_path)
    target = float(target_meters_per_unit)
    result: dict[str, Any] = {
        "asset_path": str(path),
        "exists": path.is_file(),
        "status": "fail",
        "target_meters_per_unit": target,
        "warnings": [],
        "errors": [],
    }
    if target <= 0:
        result["errors"].append({"code": "TARGET_UNITS_INVALID", "summary": "target metersPerUnit must be positive"})
        return result
    if not path.is_file():
        result["errors"].append({"code": "ASSET_NOT_FOUND", "summary": str(path)})
        return result
    metadata = _source_unit_metadata(str(path))
    if not metadata.get("opened"):
        result["errors"].append({"code": "USD_OPEN_FAILED", "summary": str(metadata.get("error"))})
        return result
    stage = Usd.Stage.Open(str(path))
    source = float(metadata["meters_per_unit"])
    authored = bool(metadata["authored"])
    result.update({
        "source_meters_per_unit": source,
        "source_units_authored": authored,
        "recommended_reference_scale": source / target if authored else None,
        "default_prim": str(stage.GetDefaultPrim().GetPath()) if stage.GetDefaultPrim() else None,
    })
    root = stage.GetDefaultPrim()
    if root:
        cache = UsdGeom.BBoxCache(Usd.TimeCode.Default(), [UsdGeom.Tokens.default_, UsdGeom.Tokens.render])
        box = cache.ComputeWorldBound(root).ComputeAlignedRange()
        minimum = box.GetMin()
        maximum = box.GetMax()
        authored_size = [float(maximum[index] - minimum[index]) for index in range(3)]
        result["bbox_size_authored_units"] = authored_size
        result["bbox_size_meters"] = [value * source for value in authored_size]
    if authored:
        result["status"] = "pass"
    else:
        result["status"] = "warn"
        result["warnings"].append({
            "code": "SOURCE_UNITS_UNAUTHORED",
            "summary": "metersPerUnit is not authored; do not infer a conversion without bbox/domain evidence.",
        })
    return result


def _add_reference(
    stage: Any,
    prim_path: str,
    asset_path: str,
    item: dict[str, Any],
    *,
    target_meters_per_unit: float,
    normalize_reference_units: bool,
) -> tuple[Any, dict[str, Any]]:
    Gf, Sdf, _Usd, UsdGeom, _UsdLux, _UsdPhysics = _require_pxr()
    entrypoint_compatibility = require_compatible_asset_entrypoint(asset_path)
    metadata = _source_unit_metadata(asset_path)
    unit_mode = str(item.get("unit_mode") or ("auto" if normalize_reference_units else "preserve"))
    unit_contract = select_source_unit_contract(
        layer_meters_per_unit=metadata.get("meters_per_unit"),
        layer_units_authored=bool(metadata.get("authored")),
        declared_meters_per_unit=item.get("source_meters_per_unit"),
        declared_units_verified=bool(item.get("source_units_verified", False)),
    )
    audit = resolve_reference_scale(
        source_meters_per_unit=unit_contract["meters_per_unit"],
        target_meters_per_unit=target_meters_per_unit,
        authored_source_units=bool(unit_contract["units_verified"]),
        explicit_scale=item.get("scale"),
        unit_mode=unit_mode,
        allow_unit_mismatch=bool(item.get("allow_unit_mismatch", False)),
    )
    audit.update({
        "prim": prim_path,
        "path": str(asset_path),
        "source_opened": bool(metadata.get("opened")),
        "source_unit_evidence": unit_contract["source"],
        "entrypoint_compatibility": entrypoint_compatibility,
    })
    if metadata.get("error"):
        audit["source_open_error"] = metadata["error"]
    _ensure_parent_prims(stage, prim_path)
    prim = stage.DefinePrim(prim_path, "Xform")
    reference_prim_path = item.get("reference_prim_path")
    if reference_prim_path:
        prim.GetReferences().AddReference(str(asset_path), Sdf.Path(str(reference_prim_path)))
        audit["reference_prim_path"] = str(reference_prim_path)
    else:
        prim.GetReferences().AddReference(str(asset_path))
    matrix_rows = item.get("xform_matrix")
    if matrix_rows is not None:
        if not (
            isinstance(matrix_rows, list) and len(matrix_rows) == 4
            and all(isinstance(row, list) and len(row) == 4 for row in matrix_rows)
        ):
            raise SystemExit(f"xform_matrix must be a numeric 4x4 row-major matrix: {prim_path}")
        effective_scale = [float(value) for value in audit["effective_scale"]]
        if any(abs(value - 1.0) > 1e-9 for value in effective_scale):
            raise SystemExit(
                f"xform_matrix requires an effective reference scale of [1,1,1]: {prim_path}"
            )
        flat = [float(value) for row in matrix_rows for value in row]
        UsdGeom.Xformable(prim).AddTransformOp(
            precision=UsdGeom.XformOp.PrecisionDouble
        ).Set(Gf.Matrix4d(*flat))
    else:
        _set_transform(
            prim,
            item.get("translate", [0, 0, 0]),
            item.get("rotate", [0, 0, 0]),
            audit["effective_scale"],
        )
    return prim, audit


def _author_orientation_metadata(prim: Any, item: dict[str, Any], Sdf: Any) -> dict[str, Any] | None:
    """Persist the asset-local front contract used by post-compose validation."""
    raw_axis = item.get("front_axis")
    if raw_axis is None:
        return None
    try:
        front_axis = normalize_front_axis(str(raw_axis))
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    facing = item.get("facing") if isinstance(item.get("facing"), dict) else {}
    target = item.get("facing_target") or facing.get("target")
    confidence = item.get("orientation_confidence") or facing.get("confidence")
    source = item.get("orientation_source") or facing.get("source")
    prim.CreateAttribute("scene:frontAxis", Sdf.ValueTypeNames.String, custom=True).Set(front_axis)
    if target:
        prim.CreateAttribute("scene:facingTarget", Sdf.ValueTypeNames.String, custom=True).Set(str(target))
    if confidence:
        prim.CreateAttribute("scene:orientationConfidence", Sdf.ValueTypeNames.String, custom=True).Set(str(confidence))
    if source:
        prim.CreateAttribute("scene:orientationSource", Sdf.ValueTypeNames.String, custom=True).Set(str(source))
    return {
        "front_axis": front_axis,
        "facing_target": str(target) if target else None,
        "orientation_confidence": str(confidence) if confidence else None,
        "orientation_source": str(source) if source else None,
    }


def _material_asset_path(config: dict[str, Any], descriptor: Any) -> str | None:
    if not isinstance(descriptor, dict):
        return None
    if descriptor.get("path"):
        path = Path(str(descriptor["path"])).expanduser()
    else:
        library = str(descriptor.get("library") or "")
        relative = str(descriptor.get("relative_path") or "")
        root = _library_root(config, library) if library else None
        path = (root / relative) if root and relative else Path()
    if not path.is_file():
        raise SystemExit(f"Material asset not found: {descriptor}")
    return str(path.resolve())


def _bind_material_asset(
    stage: Any,
    prim: Any,
    material_path: str,
    descriptor: dict[str, Any] | None = None,
) -> str:
    """Reference an external UsdShade material and bind it to a primitive."""
    Gf, Sdf, _Usd, UsdGeom, _UsdLux, _UsdPhysics = _require_pxr()
    from pxr import UsdShade

    source_stage = _Usd.Stage.Open(material_path)
    source_prim = source_stage.GetDefaultPrim() if source_stage else None
    if not source_prim or not source_prim.IsA(UsdShade.Material):
        raise SystemExit(f"Material asset default prim is not UsdShade.Material: {material_path}")
    safe_id = re.sub(r"[^A-Za-z0-9_]+", "_", str(prim.GetPath())).strip("_")
    target_path = f"/World/Looks/{safe_id}_AssetMaterial"
    _ensure_parent_prims(stage, target_path)
    target = stage.DefinePrim(target_path, "Material")
    target.GetReferences().AddReference(material_path, Sdf.Path(str(source_prim.GetPath())))
    UsdShade.MaterialBindingAPI.Apply(prim).Bind(UsdShade.Material(target))

    options = descriptor if isinstance(descriptor, dict) else {}
    has_st = any(
        primvar.GetPrimvarName() == "st"
        for primvar in UsdGeom.PrimvarsAPI(prim).GetPrimvars()
    )
    project_uvw = bool(options.get("project_uvw", prim.IsA(UsdGeom.Cube) and not has_st))
    if project_uvw:
        for shader_prim in _Usd.PrimRange(target):
            if not shader_prim.IsA(UsdShade.Shader):
                continue
            shader = UsdShade.Shader(shader_prim)
            source_asset = shader.GetSourceAsset("mdl")
            if not source_asset or "OmniPBR.mdl" not in source_asset.path:
                continue
            shader.CreateInput("project_uvw", Sdf.ValueTypeNames.Bool).Set(True)
            shader.CreateInput("world_or_object", Sdf.ValueTypeNames.Bool).Set(
                bool(options.get("world_space_projection", True))
            )
            texture_scale = options.get("texture_scale")
            if isinstance(texture_scale, (int, float)):
                texture_scale = [float(texture_scale), float(texture_scale)]
            if isinstance(texture_scale, list) and len(texture_scale) == 2:
                shader.CreateInput("texture_scale", Sdf.ValueTypeNames.Float2).Set(
                    Gf.Vec2f(float(texture_scale[0]), float(texture_scale[1]))
                )
            round_edges = options.get("round_edges_radius")
            if isinstance(round_edges, (int, float)) and float(round_edges) > 0:
                shader.CreateInput("round_edges_radius", Sdf.ValueTypeNames.Float).Set(float(round_edges))
                shader.CreateInput("round_edges_roundness", Sdf.ValueTypeNames.Float).Set(1.0)
    return target_path


def _add_box_primitive(config: dict[str, Any], stage: Any, prim_path: str, item: dict[str, Any]) -> Any:
    """Add a simple box primitive for deterministic support/collision geometry."""
    Gf, _Sdf, _Usd, UsdGeom, _UsdLux, UsdPhysics = _require_pxr()
    _ensure_parent_prims(stage, prim_path)
    cube = UsdGeom.Cube.Define(stage, prim_path)
    cube.CreateSizeAttr().Set(1.0)
    prim = cube.GetPrim()
    size = item.get("size") or item.get("scale") or [1, 1, 0.05]
    _set_transform(
        prim,
        item.get("translate", [0, 0, 0]),
        item.get("rotate", [0, 0, 0]),
        size,
    )
    if item.get("display_color"):
        cube.CreateDisplayColorAttr().Set([Gf.Vec3f(*[float(v) for v in item["display_color"]])])
    if item.get("display_opacity") is not None:
        cube.CreateDisplayOpacityAttr().Set([float(item["display_opacity"])])
    material_keys = {
        "material_roughness", "material_metallic", "material_opacity",
        "emissive_color",
    }
    material_asset = _material_asset_path(config, item.get("material_asset"))
    if material_asset:
        _bind_material_asset(stage, prim, material_asset, item.get("material_asset"))
    elif any(key in item for key in material_keys):
        from pxr import UsdShade

        safe_material_id = re.sub(r"[^A-Za-z0-9_]+", "_", prim_path).strip("_")
        material_path = f"/World/Looks/{safe_material_id}_Material"
        shader_path = f"{material_path}/PreviewSurface"
        _ensure_parent_prims(stage, material_path)
        material = UsdShade.Material.Define(stage, material_path)
        shader = UsdShade.Shader.Define(stage, shader_path)
        shader.CreateIdAttr("UsdPreviewSurface")
        diffuse = item.get("display_color") or [0.5, 0.5, 0.5]
        shader.CreateInput("diffuseColor", _Sdf.ValueTypeNames.Color3f).Set(
            Gf.Vec3f(*[float(value) for value in diffuse])
        )
        if item.get("material_roughness") is not None:
            shader.CreateInput("roughness", _Sdf.ValueTypeNames.Float).Set(float(item["material_roughness"]))
        if item.get("material_metallic") is not None:
            shader.CreateInput("metallic", _Sdf.ValueTypeNames.Float).Set(float(item["material_metallic"]))
        if item.get("material_opacity") is not None:
            shader.CreateInput("opacity", _Sdf.ValueTypeNames.Float).Set(float(item["material_opacity"]))
        if item.get("emissive_color") is not None:
            shader.CreateInput("emissiveColor", _Sdf.ValueTypeNames.Color3f).Set(
                Gf.Vec3f(*[float(value) for value in item["emissive_color"]])
            )
        material.CreateSurfaceOutput().ConnectToSource(shader.ConnectableAPI(), "surface")
        UsdShade.MaterialBindingAPI.Apply(prim).Bind(material)
    if item.get("visible") is False:
        UsdGeom.Imageable(prim).MakeInvisible()

    physics_mode = str(item.get("physics", "static")).lower()
    if physics_mode not in {"", "none", "false", "null"}:
        UsdPhysics.CollisionAPI.Apply(prim)
        if physics_mode == "dynamic":
            UsdPhysics.RigidBodyAPI.Apply(prim)
            mass_api = UsdPhysics.MassAPI.Apply(prim)
            if item.get("density") is not None:
                mass_api.CreateDensityAttr().Set(float(item["density"]))
    return prim


def _apply_prim_overrides(stage: Any, overrides: list[Any] | None) -> list[dict[str, Any]]:
    """Apply deterministic edits to existing prims composed from references."""
    _Gf, _Sdf, _Usd, UsdGeom, _UsdLux, _UsdPhysics = _require_pxr()
    applied: list[dict[str, Any]] = []
    for item in overrides or []:
        if not isinstance(item, dict) or not item.get("prim"):
            continue
        prim_path = str(item["prim"])
        prim = stage.GetPrimAtPath(prim_path)
        if not prim:
            prim = stage.OverridePrim(prim_path)
        if item.get("active") is not None:
            prim.SetActive(bool(item["active"]))
        if item.get("visible") is not None and prim:
            imageable = UsdGeom.Imageable(prim)
            if item.get("visible"):
                imageable.MakeVisible()
            else:
                imageable.MakeInvisible()
        if prim and prim.IsActive() and any(key in item for key in ("translate", "rotate", "scale")):
            _set_transform(prim, item.get("translate"), item.get("rotate"), item.get("scale"))
        applied.append({
            "kind": "prim_override",
            "prim": prim_path,
            "active": item.get("active"),
            "visible": item.get("visible"),
        })
    return applied


def _apply_regression_visual_opinions(
    stage: Any,
    destination_root: str,
    descriptor: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Replay source-scene visual opinions below an object-library reference.

    Synthesis scenes sometimes author per-instance child transforms, visibility,
    activation, shader inputs, primvars or material bindings.  Referencing only
    the normalized object wrapper loses those stronger scene-layer opinions.
    This internal-regression-only adapter copies the typed Sdf property specs
    into the reconstructed layer without re-referencing the complete scene.
    Root transforms are deliberately excluded because ``xform_matrix`` owns the
    reconstructed object pose.
    """
    if not isinstance(descriptor, dict):
        return None
    if descriptor.get("policy") != "visual_instance_overrides_v1":
        raise SystemExit("Unsupported regression instance-opinion policy")
    source_path = str(descriptor.get("source_layer") or "")
    source_root_path = str(descriptor.get("source_prim_path") or "")
    source_attachment = str(descriptor.get("source_attachment_path") or "")
    suffix = str(descriptor.get("target_prim_suffix") or "")
    if not source_path or not source_root_path:
        raise SystemExit("Regression instance opinions require source_layer and source_prim_path")

    _Gf, Sdf, _Usd, _UsdGeom, _UsdLux, _UsdPhysics = _require_pxr()
    destination_base = Sdf.Path(destination_root + suffix)
    source_layer = Sdf.Layer.FindOrOpen(source_path)
    if not source_layer:
        raise SystemExit(f"Unable to open regression opinion source layer: {source_path}")
    source_root = source_layer.GetPrimAtPath(source_root_path)
    if not source_root:
        return {
            "policy": "visual_instance_overrides_v1",
            "status": "no_local_opinions",
            "source_layer": source_path,
            "source_prim_path": source_root_path,
            "source_attachment_path": source_attachment,
            "target_prim": str(destination_base),
            "opinion_prim_count": 0,
            "copied_property_count": 0,
            "deactivated_prim_count": 0,
        }
    target_layer = stage.GetRootLayer()
    copied_properties = 0
    deactivated_prims = 0
    opinion_prims = 0

    def selected_property(name: str, *, is_root: bool) -> bool:
        if name.startswith("xformOp"):
            return not is_root
        return (
            name in {"visibility", "purpose", "doubleSided"}
            or name.startswith("inputs:")
            or name.startswith("primvars:")
            or name == "material:binding"
            or name.startswith("material:binding:")
        )

    def remap_path(path: Any) -> Any:
        rendered = str(path)
        if rendered == source_root_path:
            return destination_base
        if rendered.startswith(source_root_path + "/"):
            return Sdf.Path(str(destination_base) + rendered[len(source_root_path):])
        return path

    def visit(source_spec: Any) -> None:
        nonlocal copied_properties, deactivated_prims, opinion_prims
        rendered_source = str(source_spec.path)
        relative = rendered_source[len(source_root_path):]
        if source_attachment:
            if not relative:
                for child in source_spec.nameChildren:
                    visit(child)
                return
            if relative != source_attachment and not relative.startswith(source_attachment + "/"):
                return
            mapped_relative = relative[len(source_attachment):]
        else:
            mapped_relative = relative
        is_root = not mapped_relative
        target_prim_path = Sdf.Path(str(destination_base) + mapped_relative)
        chosen = [
            prop for prop in source_spec.properties
            if selected_property(str(prop.name), is_root=is_root)
        ]
        needs_prim = bool(chosen) or (not is_root and source_spec.active is False)
        target_spec = Sdf.CreatePrimInLayer(target_layer, target_prim_path) if needs_prim else None
        if target_spec:
            target_spec.specifier = Sdf.SpecifierOver
        if needs_prim:
            opinion_prims += 1
        for prop in chosen:
            target_property_path = target_prim_path.AppendProperty(str(prop.name))
            if not Sdf.CopySpec(source_layer, prop.path, target_layer, target_property_path):
                raise SystemExit(f"Unable to copy regression visual opinion: {prop.path}")
            copied = target_layer.GetPropertyAtPath(target_property_path)
            if copied and hasattr(copied, "targetPathList"):
                targets = list(copied.targetPathList.GetAddedOrExplicitItems())
                copied.targetPathList.ClearEdits()
                copied.targetPathList.explicitItems = [remap_path(path) for path in targets]
            if copied and hasattr(copied, "connectionPathList"):
                connections = list(copied.connectionPathList.GetAddedOrExplicitItems())
                copied.connectionPathList.ClearEdits()
                copied.connectionPathList.explicitItems = [remap_path(path) for path in connections]
            copied_properties += 1
        # Recurse before deactivating a parent so target descendants remain
        # authorable even when the source instance disables an entire branch.
        for child in source_spec.nameChildren:
            visit(child)
        if target_spec and not is_root and source_spec.active is False:
            target_spec.active = False
            deactivated_prims += 1

    visit(source_root)
    return {
        "policy": "visual_instance_overrides_v1",
        "status": "applied",
        "source_layer": source_path,
        "source_prim_path": source_root_path,
        "source_attachment_path": source_attachment,
        "target_prim": str(destination_base),
        "opinion_prim_count": opinion_prims,
        "copied_property_count": copied_properties,
        "deactivated_prim_count": deactivated_prims,
    }


def _ensure_physics_scene(stage: Any, gravity: bool = True) -> None:
    _Gf, _Sdf, _Usd, _UsdGeom, _UsdLux, UsdPhysics = _require_pxr()
    scene = UsdPhysics.Scene.Define(stage, "/World/PhysicsScene")
    if gravity:
        scene.CreateGravityDirectionAttr().Set((0.0, 0.0, -1.0))
        scene.CreateGravityMagnitudeAttr().Set(9.81)


def _apply_physics(stage: Any, prim_path: str, mode: str, density: float | None = None) -> dict[str, Any]:
    _Gf, Sdf, Usd, UsdGeom, _UsdLux, UsdPhysics = _require_pxr()
    prim = stage.GetPrimAtPath(prim_path)
    if not prim or mode in {"", "none", "false", "null"}:
        return {"prim": prim_path, "mode": "none", "mesh_colliders": 0}

    descendants = list(Usd.PrimRange(prim))
    existing_collision_count = sum(
        1
        for child in descendants
        if child.HasAPI(UsdPhysics.CollisionAPI)
        or (
            child.GetAttribute("physics:collisionEnabled")
            and child.GetAttribute("physics:collisionEnabled").Get() is not False
        )
    )
    mesh_count = 0
    disabled_rigid_bodies = 0
    disabled_joints = 0
    anchored_articulation_bodies: list[str] = []
    independently_solved_joints: list[str] = []
    held_articulation_joints: list[str] = []
    convex_dynamic_colliders = 0
    for child in descendants:
        # Preserve authored SimReady collision whenever it exists.  Turning
        # every render mesh in an articulated fixture into a triangle-mesh
        # collider is both expensive and frequently invalid (non-manifold or
        # unsuitable for SDF).  Only synthesize a mesh fallback when the whole
        # referenced asset has no collision signal at all.
        if child.IsA(UsdGeom.Mesh) and (mode == "dynamic" or existing_collision_count == 0):
            UsdPhysics.CollisionAPI.Apply(child)
            mesh_count += 1

        # A referenced asset may already contain dynamic bodies and joints.
        # `physics: static` means the composed scene owns a fixed fixture, not
        # merely "add collision and preserve inherited dynamics".  Authoring
        # the disabling opinions at the scene layer keeps the source asset
        # untouched while preventing gravity or initial penetration impulses
        # from moving furniture and architectural fixtures.
        if mode == "static":
            rigid_attr = child.GetAttribute("physics:rigidBodyEnabled")
            if child.HasAPI(UsdPhysics.RigidBodyAPI) or rigid_attr:
                if not rigid_attr:
                    rigid_attr = child.CreateAttribute(
                        "physics:rigidBodyEnabled", Sdf.ValueTypeNames.Bool
                    )
                rigid_attr.Set(False)
                disabled_rigid_bodies += 1
            if "Joint" in (child.GetTypeName() or ""):
                joint_attr = child.GetAttribute("physics:jointEnabled")
                if not joint_attr:
                    joint_attr = child.CreateAttribute(
                        "physics:jointEnabled", Sdf.ValueTypeNames.Bool
                    )
                joint_attr.Set(False)
                # UsdPhysics has no uniformly honored enabled switch across
                # all joint implementations.  An inactive scene-layer
                # override is deterministic, leaves the source untouched and
                # prevents PhysX from parsing stale/disjoint fixed joints.
                child.SetActive(False)
                disabled_joints += 1

        if mode == "articulated_fixed_base" and child.GetTypeName() == "PhysicsFixedJoint":
            joint = UsdPhysics.Joint(child)
            body1_targets = list(joint.GetBody1Rel().GetTargets())
            if not body1_targets:
                continue
            body_prim = stage.GetPrimAtPath(body1_targets[0])
            if not body_prim:
                continue
            body_api = UsdPhysics.RigidBodyAPI.Apply(body_prim)
            body_api.CreateRigidBodyEnabledAttr().Set(True)
            body_api.CreateKinematicEnabledAttr().Set(True)
            child.SetActive(False)
            anchored_articulation_bodies.append(str(body_prim.GetPath()))

        if mode == "articulated_fixed_base" and child.GetTypeName() in {
            "PhysicsRevoluteJoint",
            "PhysicsPrismaticJoint",
        }:
            # PhysX does not permit a reduced-coordinate articulation to
            # contain kinematic links.  A fixture such as a cabinet still
            # needs a fixed/kinematic corpus, so solve its door and drawer
            # joints independently instead of silently dropping them.
            joint = UsdPhysics.Joint(child)
            joint.CreateExcludeFromArticulationAttr().Set(True)
            independently_solved_joints.append(str(child.GetPath()))

            # Keep doors and drawers at their authored closed pose during
            # scene startup.  These are finite force drives: the movable link
            # remains a dynamic rigid body and can still be pushed or pulled.
            if child.GetTypeName() == "PhysicsRevoluteJoint":
                drive = UsdPhysics.DriveAPI.Apply(child, "angular")
                drive.CreateStiffnessAttr().Set(25.0)
                drive.CreateDampingAttr().Set(5.0)
                drive.CreateMaxForceAttr().Set(80.0)
            else:
                drive = UsdPhysics.DriveAPI.Apply(child, "linear")
                drive.CreateStiffnessAttr().Set(800.0)
                drive.CreateDampingAttr().Set(80.0)
                drive.CreateMaxForceAttr().Set(300.0)
            drive.CreateTypeAttr().Set("force")
            drive.CreateTargetPositionAttr().Set(0.0)
            drive.CreateTargetVelocityAttr().Set(0.0)
            held_articulation_joints.append(str(child.GetPath()))

    if mode == "articulated_fixed_base":
        # Triangle meshes are invalid collision shapes on dynamic rigid
        # bodies.  Preserve the source collision selection, but override its
        # approximation to a supported convex hull on movable links only.
        for child in descendants:
            if not child.IsA(UsdGeom.Mesh) or not child.HasAPI(UsdPhysics.CollisionAPI):
                continue
            ancestor = child.GetParent()
            dynamic_ancestor = None
            while ancestor and ancestor.GetPath() != prim.GetParent().GetPath():
                rigid_attr = ancestor.GetAttribute("physics:rigidBodyEnabled")
                if ancestor.HasAPI(UsdPhysics.RigidBodyAPI) or rigid_attr:
                    if not rigid_attr or rigid_attr.Get() is not False:
                        dynamic_ancestor = ancestor
                    break
                ancestor = ancestor.GetParent()
            if not dynamic_ancestor:
                continue
            kinematic_attr = dynamic_ancestor.GetAttribute("physics:kinematicEnabled")
            if kinematic_attr and kinematic_attr.Get() is True:
                continue
            UsdPhysics.MeshCollisionAPI.Apply(child).CreateApproximationAttr().Set("convexHull")
            convex_dynamic_colliders += 1

    if mode == "dynamic":
        UsdPhysics.RigidBodyAPI.Apply(prim)
        mass_api = UsdPhysics.MassAPI.Apply(prim)
        if density is not None:
            mass_api.CreateDensityAttr().Set(float(density))
    return {
        "prim": prim_path,
        "mode": mode,
        "mesh_colliders": mesh_count,
        "preserved_inherited_colliders": existing_collision_count,
        "disabled_inherited_rigid_bodies": disabled_rigid_bodies,
        "disabled_inherited_joints": disabled_joints,
        "anchored_articulation_bodies": anchored_articulation_bodies,
        "independently_solved_joints": independently_solved_joints,
        "held_articulation_joints": held_articulation_joints,
        "convex_dynamic_colliders": convex_dynamic_colliders,
    }


def _resolve_asset_path(config: dict[str, Any], item: dict[str, Any]) -> str:
    if item.get("path"):
        return str(item["path"])
    if item.get("asset_id"):
        record = get_asset(config, str(item["asset_id"]), libraries=item.get("libraries"))
        if record:
            return str(record["path"])
    query = item.get("query") or item.get("asset_query")
    if query:
        hits = search_assets(
            config,
            str(query),
            libraries=item.get("libraries"),
            types=item.get("types") or item.get("asset_types"),
            topk=1,
        )
        if hits:
            return str(hits[0]["path"])
    raise SystemExit(f"Cannot resolve asset path for item: {item}")


def make_scene(config: dict[str, Any], spec: dict[str, Any], *, output: str | None = None, _owned_outputs: set | None = None) -> dict[str, Any]:
    Gf, Sdf, _Usd, UsdGeom, UsdLux, _UsdPhysics = _require_pxr()
    from pxr import Usd  # type: ignore

    spec, spec_observation = normalize_scene_spec(spec)

    runtime = config.get("runtime", {}) if isinstance(config.get("runtime"), dict) else {}
    output_path = check_scene_outputs(output or spec.get("output_path") or
        str(Path(runtime.get("output_root") or Path.cwd() / "runs") / "scene.usda"), config, _owned_outputs)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    stage_spec = spec.get("stage") if isinstance(spec.get("stage"), dict) else {}
    target_meters_per_unit = float(stage_spec.get("meters_per_unit", 1.0))
    unit_policy = str(stage_spec.get("unit_policy", "normalize_references"))
    if unit_policy not in {"normalize_references", "preserve_references"}:
        raise SystemExit(f"Unsupported stage.unit_policy: {unit_policy}")
    normalize_reference_units = unit_policy == "normalize_references"

    stage = Usd.Stage.CreateNew(str(output_path))
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    UsdGeom.SetStageMetersPerUnit(stage, target_meters_per_unit)
    world = stage.DefinePrim("/World", "Xform")
    stage.SetDefaultPrim(world)

    physics_spec = spec.get("physics") if isinstance(spec.get("physics"), dict) else {}
    if physics_spec.get("gravity", True):
        _ensure_physics_scene(stage, gravity=True)

    created: list[dict[str, Any]] = []
    unit_audit: list[dict[str, Any]] = []
    background = spec.get("background") if isinstance(spec.get("background"), dict) else None
    if background:
        bg_path = _resolve_asset_path(config, background)
        bg_prim = str(background.get("prim") or "/World/Background")
        _bg, audit = _add_reference(
            stage,
            bg_prim,
            bg_path,
            background,
            target_meters_per_unit=target_meters_per_unit,
            normalize_reference_units=normalize_reference_units,
        )
        unit_audit.append(audit)
        created.append({"kind": "background", "prim": bg_prim, "path": bg_path})

    created.extend(_apply_prim_overrides(stage, spec.get("prim_overrides")))

    recipe_spec = spec.get("fabrica_recipe") if isinstance(spec.get("fabrica_recipe"), dict) else None
    if recipe_spec:
        recipe = get_fabrica_recipe(config, str(recipe_spec.get("assembly", "car")))
        if not recipe:
            raise SystemExit(f"Fabrica recipe not found: {recipe_spec.get('assembly')}")
        prim_path = str(recipe_spec.get("prim") or f"/World/Fabrica/{recipe['assembly_name']}")
        scene_usda = str(recipe.get("scene_usda") or recipe.get("path"))
        _recipe_prim, audit = _add_reference(
            stage,
            prim_path,
            scene_usda,
            recipe_spec,
            target_meters_per_unit=target_meters_per_unit,
            normalize_reference_units=normalize_reference_units,
        )
        unit_audit.append(audit)
        created.append({
            "kind": "fabrica_recipe",
            "assembly": recipe.get("assembly_name"),
            "prim": prim_path,
            "path": scene_usda,
            "package_root": recipe.get("package_root"),
        })

    physics_results = []
    for item in spec.get("support_colliders", []) or []:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("id") or "SupportCollider")
        safe_id = re.sub(r"[^A-Za-z0-9_]+", "_", item_id).strip("_") or "SupportCollider"
        prim_path = str(item.get("prim") or f"/World/Colliders/{safe_id}")
        collider_type = str(item.get("type", "box")).lower()
        box_semantic_types = {"box", "glass_panel", "frame", "beam"}
        if collider_type not in box_semantic_types:
            raise SystemExit(f"Unsupported support collider type: {collider_type}")
        _add_box_primitive(config, stage, prim_path, item)
        physics_results.append({
            "prim": prim_path,
            "mode": str(item.get("physics", "static")).lower(),
            "collider_type": collider_type,
            "geometry_type": "box",
            "size": item.get("size") or item.get("scale") or [1, 1, 0.05],
        })
        created.append({"kind": "support_collider", "id": item_id, "prim": prim_path, "type": collider_type})

    for item in spec.get("appearance_overlays", []) or []:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("id") or "AppearanceOverlay")
        safe_id = re.sub(r"[^A-Za-z0-9_]+", "_", item_id).strip("_") or "AppearanceOverlay"
        prim_path = str(item.get("prim") or f"/World/Architecture/Appearance/{safe_id}")
        overlay_type = str(item.get("type", "box")).lower()
        if overlay_type not in {"box", "frame", "beam"}:
            raise SystemExit(f"Unsupported appearance overlay type: {overlay_type}")
        authored = dict(item)
        authored.setdefault("physics", "none")
        _add_box_primitive(config, stage, prim_path, authored)
        created.append({
            "kind": "appearance_overlay", "id": item_id, "prim": prim_path, "type": overlay_type,
            "material_asset": authored.get("material_asset"),
        })

    for item in spec.get("references", []) or []:
        if not isinstance(item, dict):
            continue
        asset_path = _resolve_asset_path(config, item)
        item_id = str(item.get("id") or Path(asset_path).stem)
        safe_id = re.sub(r"[^A-Za-z0-9_]+", "_", item_id).strip("_") or "Asset"
        prim_path = str(item.get("prim") or f"/World/Objects/{safe_id}")
        _ref_prim, audit = _add_reference(
            stage,
            prim_path,
            asset_path,
            item,
            target_meters_per_unit=target_meters_per_unit,
            normalize_reference_units=normalize_reference_units,
        )
        opinion_audit = _apply_regression_visual_opinions(
            stage,
            prim_path,
            item.get("regression_instance_opinions"),
        )
        orientation = _author_orientation_metadata(_ref_prim, item, Sdf)
        unit_audit.append(audit)
        physics_mode = str(item.get("physics", "none")).lower()
        if physics_mode != "none":
            physics_results.append(_apply_physics(stage, prim_path, physics_mode, item.get("density")))
        created.append({
            "kind": "reference",
            "id": item_id,
            "prim": prim_path,
            "path": asset_path,
            **({"regression_instance_opinions": opinion_audit} if opinion_audit else {}),
            **({"orientation": orientation} if orientation else {}),
        })

    for light in spec.get("lights", []) or []:
        if not isinstance(light, dict):
            continue
        light_type = str(light.get("type", "distant")).lower()
        prim_path = str(light.get("prim") or f"/World/Lights/{light_type.title()}Light")
        _ensure_parent_prims(stage, prim_path)
        if light_type == "dome":
            light_obj = UsdLux.DomeLight.Define(stage, prim_path)
            if light.get("texture"):
                light_obj.CreateTextureFileAttr().Set(Sdf.AssetPath(str(light["texture"])))
            light_prim = light_obj.GetPrim()
        elif light_type == "rect":
            light_obj = UsdLux.RectLight.Define(stage, prim_path)
            if light.get("width") is not None:
                light_obj.CreateWidthAttr().Set(float(light["width"]))
            if light.get("height") is not None:
                light_obj.CreateHeightAttr().Set(float(light["height"]))
            light_prim = light_obj.GetPrim()
        elif light_type == "disk":
            light_obj = UsdLux.DiskLight.Define(stage, prim_path)
            if light.get("radius") is not None:
                light_obj.CreateRadiusAttr().Set(float(light["radius"]))
            light_prim = light_obj.GetPrim()
        elif light_type == "sphere":
            light_obj = UsdLux.SphereLight.Define(stage, prim_path)
            if light.get("radius") is not None:
                light_obj.CreateRadiusAttr().Set(float(light["radius"]))
            light_prim = light_obj.GetPrim()
        else:
            light_obj = UsdLux.DistantLight.Define(stage, prim_path)
            if light.get("angle") is not None:
                light_obj.CreateAngleAttr().Set(float(light["angle"]))
            light_prim = light_obj.GetPrim()
        light_api = UsdLux.LightAPI(light_prim)
        light_api.CreateIntensityAttr().Set(float(light.get("intensity", 3000)))
        if light.get("color"):
            light_api.CreateColorAttr().Set(Gf.Vec3f(*[float(v) for v in light["color"]]))
        if light.get("temperature") is not None:
            light_api.CreateEnableColorTemperatureAttr().Set(True)
            light_api.CreateColorTemperatureAttr().Set(float(light["temperature"]))
        if light.get("exposure") is not None:
            light_api.CreateExposureAttr().Set(float(light["exposure"]))
        if light.get("look_at") is not None:
            matrix = _look_at_matrix(
                Gf,
                light.get("translate", [0, 0, 0]),
                light["look_at"],
            )
            UsdGeom.Xformable(light_prim).AddTransformOp().Set(matrix)
        else:
            _set_transform(
                light_prim,
                light.get("translate"),
                light.get("rotate"),
                light.get("scale"),
            )
        created.append({
            "kind": "light",
            "prim": prim_path,
            "type": light_type,
            **({"texture": str(light["texture"])} if light.get("texture") else {}),
            **({"temperature": float(light["temperature"])} if light.get("temperature") is not None else {}),
            **({"exposure": float(light["exposure"])} if light.get("exposure") is not None else {}),
            **({"look_at": [float(value) for value in light["look_at"]]} if light.get("look_at") is not None else {}),
        })

    camera = spec.get("camera") if isinstance(spec.get("camera"), dict) else None
    if camera:
        prim_path = str(camera.get("prim") or "/World/Camera")
        _ensure_parent_prims(stage, prim_path)
        cam = UsdGeom.Camera.Define(stage, prim_path)
        # Scene Brief compilation emits translate/focal_length, while older
        # hand-authored and imported specs commonly use position/focal_length_mm.
        # Accept both spellings and never silently fall back to the default
        # camera when a valid legacy value is present.
        camera_position = camera.get("translate")
        if camera_position is None:
            camera_position = camera.get("position", [4, -5, 3])
        focal_length = camera.get("focal_length")
        if focal_length is None:
            focal_length = camera.get("focal_length_mm", 28)
        cam.CreateFocalLengthAttr().Set(float(focal_length))
        cam.CreateClippingRangeAttr().Set(Gf.Vec2f(*camera.get("clipping_range", [0.01, 1000.0])))
        if camera.get("exposure_ev") is not None:
            camera_prim = cam.GetPrim()
            # make-scene intentionally runs in a lightweight pxr-only process where
            # RTX schemas may not be registered. Author the equivalent USD list-op
            # directly; Kit resolves the schema when it opens the composed stage.
            camera_prim.SetMetadata(
                "apiSchemas",
                Sdf.TokenListOp.Create(
                    prependedItems=["OmniRtxCameraExposureAPI_1"],
                ),
            )
            exposure_ev = float(camera["exposure_ev"])
            # Isaac Sim 6.0.1 exposes the legacy RTX physical-exposure
            # attributes but not OpenUSD 25.05's standalone `exposure`
            # compensation attribute. Preserve Kit's neutral camera values and
            # encode EV compensation in exposure time; shutter open/close remain
            # independent, so this does not alter motion-blur duration.
            exposure_attributes = {
                "exposure:fStop": 5.0,
                "exposure:iso": 100.0,
                "exposure:responsivity": 1.1026709,
                "exposure:time": 0.02 * math.pow(2.0, exposure_ev),
            }
            for attribute_name, value in exposure_attributes.items():
                camera_prim.CreateAttribute(
                    attribute_name,
                    Sdf.ValueTypeNames.Float,
                    custom=False,
                ).Set(value)
        if camera.get("target_aspect_ratio") is not None:
            aspect = max(float(camera["target_aspect_ratio"]), 0.01)
            horizontal_aperture = float(camera.get("horizontal_aperture", 20.955))
            cam.CreateHorizontalApertureAttr().Set(horizontal_aperture)
            cam.CreateVerticalApertureAttr().Set(horizontal_aperture / aspect)
        if camera.get("look_at") is not None:
            matrix = _look_at_matrix(
                Gf,
                camera_position,
                camera["look_at"],
            )
            UsdGeom.Xformable(cam.GetPrim()).AddTransformOp().Set(matrix)
        else:
            _set_transform(cam.GetPrim(), camera_position, camera.get("rotate", [60, 0, 40]), camera.get("scale"))
        created.append({
            "kind": "camera",
            "prim": prim_path,
            "exposure_ev": camera.get("exposure_ev"),
        })

    stage.GetRootLayer().Save()
    sidecar = output_path.with_suffix(".scene_spec.json")
    sidecar_payload = json.loads(json.dumps(spec))
    sidecar_payload["composition_audit"] = {
        "unit_policy": unit_policy,
        "target_meters_per_unit": target_meters_per_unit,
        "references": unit_audit,
    }
    sidecar.write_text(_json(sidecar_payload), encoding="utf-8")
    if _owned_outputs is not None:
        _owned_outputs.update((output_path, sidecar))
    return {
        "status": "ok",
        "scene_path": str(output_path),
        "sidecar_spec": str(sidecar),
        "created": created,
        "physics": physics_results,
        "unit_audit": {
            "unit_policy": unit_policy,
            "target_meters_per_unit": target_meters_per_unit,
            "references": unit_audit,
        },
        "scene_spec": spec_observation,
    }


def validate_scene(scene_path: str) -> dict[str, Any]:
    _Gf, _Sdf, Usd, _UsdGeom, _UsdLux, _UsdPhysics = _require_pxr()
    from pxr import UsdUtils  # type: ignore

    path = Path(scene_path)
    result = {
        "scene_path": str(path),
        "exists": path.exists(),
        "status": "fail",
        "errors": [],
        "warnings": [],
        "prim_count": 0,
        "reference_count": 0,
        "dependency_layer_count": 0,
        "dependency_asset_count": 0,
        "unresolved_dependency_count": 0,
        "unresolved_dependencies": [],
        "runtime_mdl_modules": [],
    }
    if not path.exists():
        result["errors"].append(issue("SCENE_NOT_FOUND", "scene file does not exist", retryable=False, details={"path": str(path)}))
        return result
    stage = Usd.Stage.Open(str(path))
    if not stage:
        result["errors"].append(issue("USD_STAGE_OPEN_FAILED", "Usd.Stage.Open failed", retryable=True, details={"path": str(path)}))
        return result
    for prim in stage.Traverse():
        result["prim_count"] += 1
        refs = prim.GetMetadata("references")
        if refs:
            result["reference_count"] += 1
    roots = list(stage.GetPseudoRoot().GetChildren())
    result["root_prims"] = [str(prim.GetPath()) for prim in roots]
    result["default_prim"] = str(stage.GetDefaultPrim().GetPath()) if stage.GetDefaultPrim() else None
    if not roots or not result["prim_count"]:
        result["errors"].append(issue("EMPTY_SCENE", "No defined active scene prims", retryable=True))
    elif not stage.GetDefaultPrim():
        result["warnings"].append(issue("DEFAULT_PRIM_UNAUTHORED", "Use explicit observed root paths; do not assume /World", retryable=True))
    try:
        layers, assets, unresolved = UsdUtils.ComputeAllDependencies(str(path))
        unresolved_text = sorted({str(getattr(item, "identifier", item)) for item in unresolved})
        runtime_modules = [item for item in unresolved_text if _is_runtime_mdl_module(item)]
        missing_files = [item for item in unresolved_text if item not in runtime_modules]
        result["dependency_layer_count"] = len(layers)
        result["dependency_asset_count"] = len(assets)
        result["unresolved_dependency_count"] = len(missing_files)
        result["unresolved_dependencies"] = missing_files[:200]
        result["runtime_mdl_modules"] = runtime_modules
        if missing_files:
            result["errors"].append(issue(
                "ASSET_DEPENDENCY_MISSING",
                f"{len(missing_files)} unresolved file dependencies",
                retryable=True,
                details={"sample": missing_files[:20], "truncated": len(missing_files) > 20},
            ))
        if runtime_modules:
            result["warnings"].append(issue(
                "RUNTIME_MDL_RESOLUTION_REQUIRED",
                f"{len(runtime_modules)} MDL modules rely on the Isaac runtime search path",
                retryable=True,
                details={"modules": runtime_modules},
            ))
    except Exception as exc:
        result["errors"].append(issue("DEPENDENCY_AUDIT_FAILED", f"{type(exc).__name__}: {exc}", retryable=True))
    result["status"] = "fail" if result["errors"] else ("warn" if result["warnings"] else "pass")
    return result


def _prim_has_authored_reference(prim: Any) -> bool:
    refs = prim.GetMetadata("references")
    return bool(refs)


def _vec3_to_list(value: Any) -> list[float]:
    return [float(value[0]), float(value[1]), float(value[2])]


def _aabb_intersection(
    left: dict[str, Any],
    right: dict[str, Any],
    *,
    tolerance: float = 0.0,
) -> dict[str, Any] | None:
    """Return the strict 3D overlap between two bbox rows.

    Contact within ``tolerance`` is not penetration. Keeping this helper free of
    USD types makes the broad-phase policy easy to regression-test without pxr.
    """
    overlap_min = [max(float(left["min"][axis]), float(right["min"][axis])) for axis in range(3)]
    overlap_max = [min(float(left["max"][axis]), float(right["max"][axis])) for axis in range(3)]
    overlap_size = [overlap_max[axis] - overlap_min[axis] for axis in range(3)]
    if any(size <= tolerance for size in overlap_size):
        return None
    return {
        "min": overlap_min,
        "max": overlap_max,
        "size": overlap_size,
        "volume": overlap_size[0] * overlap_size[1] * overlap_size[2],
    }


def _support_contact_from_rows(
    object_row: dict[str, Any],
    support_row: dict[str, Any],
    *,
    tolerance: float = 0.005,
    minimum_xy_overlap: float = 0.001,
    support_surface_z: float | None = None,
) -> dict[str, Any]:
    overlap_xy = [
        min(float(object_row["max"][axis]), float(support_row["max"][axis]))
        - max(float(object_row["min"][axis]), float(support_row["min"][axis]))
        for axis in range(2)
    ]
    support_surface_z = float(support_row["max"][2]) if support_surface_z is None else float(support_surface_z)
    object_bottom_z = float(object_row["min"][2])
    gap = object_bottom_z - support_surface_z
    if any(size < minimum_xy_overlap for size in overlap_xy):
        status = "support_xy_miss"
    elif gap > tolerance:
        status = "floating_above_support"
    elif gap < -tolerance:
        status = "penetrating_support"
    else:
        status = "pass"
    return {
        "status": status,
        "object": object_row["prim"],
        "support": support_row["prim"],
        "object_bottom_z": object_bottom_z,
        "support_surface_z": support_surface_z,
        "signed_gap_m": gap,
        "xy_overlap_m": overlap_xy,
    }


def inspect_bounds(
    scene_path: str,
    *,
    prim_paths: list[str] | None = None,
    root_path: str = "/World/Objects",
    floor_z: float = 0.0,
    tolerance: float = 0.005,
) -> dict[str, Any]:
    """Return world-space bounding boxes for placed reference prims.

    This is intentionally generic. A higher-level agent can use the min/max
    values to align any asset's bottom to the intended support surface instead
    of assuming the USD pivot is at the contact point.
    """
    _Gf, _Sdf, Usd, UsdGeom, _UsdLux, _UsdPhysics = _require_pxr()
    path = Path(scene_path)
    result: dict[str, Any] = {
        "scene_path": str(path),
        "exists": path.exists(),
        "root_path": root_path,
        "floor_z": float(floor_z),
        "tolerance": float(tolerance),
        "bounds": [],
        "warnings": [],
    }
    if not path.exists():
        result["warnings"].append({"kind": "missing_scene", "path": str(path)})
        return result
    stage = Usd.Stage.Open(str(path))
    if not stage:
        result["warnings"].append({"kind": "stage_open_failed", "path": str(path)})
        return result

    if prim_paths:
        candidates = [stage.GetPrimAtPath(item) for item in prim_paths]
        candidates = [prim for prim in candidates if prim]
    else:
        root = stage.GetPrimAtPath(root_path)
        candidates = []
        if root:
            selected_paths: list[str] = []
            for prim in Usd.PrimRange(root):
                if prim == root:
                    continue
                if _prim_has_authored_reference(prim):
                    path_text = str(prim.GetPath())
                    if any(path_text.startswith(parent + "/") for parent in selected_paths):
                        continue
                    candidates.append(prim)
                    selected_paths.append(path_text)

    result.update(coordinate_unit="stage_unit", meters_per_unit=float(UsdGeom.GetStageMetersPerUnit(stage)))
    cache = UsdGeom.BBoxCache(
        Usd.TimeCode.Default(),
        [UsdGeom.Tokens.default_, UsdGeom.Tokens.render, UsdGeom.Tokens.proxy],
    )
    for prim in candidates:
        box = cache.ComputeWorldBound(prim).ComputeAlignedBox()
        mn = box.GetMin()
        mx = box.GetMax()
        center = box.GetMidpoint()
        size = mx - mn
        row = {
            "prim": str(prim.GetPath()),
            "type_name": prim.GetTypeName(),
            "min": _vec3_to_list(mn),
            "max": _vec3_to_list(mx),
            "center": _vec3_to_list(center),
            "size": _vec3_to_list(size),
        }
        if float(mn[2]) < floor_z - tolerance:
            dz = floor_z - float(mn[2])
            warning = {
                "kind": "below_floor",
                "prim": row["prim"],
                "min_z": float(mn[2]),
                "floor_z": float(floor_z),
                "suggested_delta_z": dz,
            }
            row["warning"] = warning
            result["warnings"].append(warning)
        result["bounds"].append(row)
    return result


def inspect_scene_overlaps(
    scene_path: str,
    *,
    object_prim_paths: list[str] | None = None,
    objects_root_path: str = "/World/Objects",
    background_prim_path: str = "/World/Background",
    penetration_tolerance: float = 0.005,
    max_background_span_m: float = 10.0,
    volumetric_aggregate_min_xy_m: float = 2.0,
    volumetric_aggregate_min_z_m: float = 1.0,
    ignore_background_prim_paths: list[str] | None = None,
    max_reported_overlaps: int = 100,
) -> dict[str, Any]:
    """Broad-phase check for placed objects intersecting background geometry.

    The check intentionally visits leaf-like Boundable prims instead of using the
    background root bbox. Very broad shell/floor prims are skipped because their
    AABBs are poor occupancy proxies; callers can lower ``max_background_span_m``
    or inspect reported/skipped candidates when a background needs tighter rules.
    """
    _Gf, _Sdf, Usd, UsdGeom, _UsdLux, _UsdPhysics = _require_pxr()
    path = Path(scene_path)
    result: dict[str, Any] = {
        "scene_path": str(path),
        "exists": path.exists(),
        "status": "pass",
        "objects_root": objects_root_path,
        "background_prim": background_prim_path,
        "penetration_tolerance_m": float(penetration_tolerance),
        "max_background_span_m": float(max_background_span_m),
        "object_bounds": [],
        "background_candidate_count": 0,
        "skipped_broad_background_count": 0,
        "skipped_volumetric_aggregate_count": 0,
        "overlap_count": 0,
        "overlaps": [],
        "warnings": [],
        "errors": [],
    }
    if not path.exists():
        result["status"] = "fail"
        result["errors"].append({"kind": "missing_scene", "path": str(path)})
        return result
    stage = Usd.Stage.Open(str(path))
    if not stage:
        result["status"] = "fail"
        result["errors"].append({"kind": "stage_open_failed", "path": str(path)})
        return result

    cache = UsdGeom.BBoxCache(
        Usd.TimeCode.Default(),
        [UsdGeom.Tokens.default_, UsdGeom.Tokens.render, UsdGeom.Tokens.proxy],
    )
    if object_prim_paths:
        object_prims = [stage.GetPrimAtPath(item) for item in object_prim_paths]
        missing_objects = [item for item, prim in zip(object_prim_paths, object_prims) if not prim]
        object_prims = [prim for prim in object_prims if prim]
        for item in missing_objects:
            result["errors"].append({"kind": "missing_object_prim", "prim": item})
    else:
        objects_root = stage.GetPrimAtPath(objects_root_path)
        object_prims = []
        selected_paths: list[str] = []
        if objects_root:
            for prim in Usd.PrimRange(objects_root):
                if prim == objects_root or not _prim_has_authored_reference(prim):
                    continue
                prim_path = str(prim.GetPath())
                if any(prim_path.startswith(parent + "/") for parent in selected_paths):
                    continue
                object_prims.append(prim)
                selected_paths.append(prim_path)
        else:
            result["errors"].append({"kind": "missing_objects_root", "prim": objects_root_path})

    background = stage.GetPrimAtPath(background_prim_path)
    if not background:
        result["errors"].append({"kind": "missing_background_prim", "prim": background_prim_path})
    if result["errors"]:
        result["status"] = "fail"
        return result

    mpu = float(UsdGeom.GetStageMetersPerUnit(stage))
    result.update(coordinate_unit="m", meters_per_unit=mpu)
    if (not math.isfinite(mpu) or mpu <= 0 or not math.isfinite(penetration_tolerance)
            or penetration_tolerance < 0 or max_reported_overlaps < 1):
        result.update(status="fail", errors=[{"kind": "invalid_units_or_tolerance"}])
        return result
    object_rows = [_bbox_row_from_prim(cache, prim, coordinate_scale=mpu) for prim in object_prims]
    result["object_bounds"] = object_rows
    ignored = list(ignore_background_prim_paths or [])
    background_rows: list[dict[str, Any]] = []
    for prim in Usd.PrimRange(background, Usd.TraverseInstanceProxies()):
        if not prim.IsA(UsdGeom.Boundable):
            continue
        prim_path = str(prim.GetPath())
        if any(prim_path == item or prim_path.startswith(item + "/") for item in ignored):
            continue
        imageable = UsdGeom.Imageable(prim)
        if imageable and imageable.ComputeVisibility() == UsdGeom.Tokens.invisible:
            continue
        if imageable and imageable.ComputePurpose() not in (UsdGeom.Tokens.default_, UsdGeom.Tokens.render):
            continue
        row = _bbox_row_from_prim(cache, prim, coordinate_scale=mpu)
        if any(float(size) <= 0.0 for size in row["size"]):
            continue
        if max(float(size) for size in row["size"]) > max_background_span_m:
            result["skipped_broad_background_count"] += 1
            continue
        if is_volumetric_aggregate_row(
            row,
            minimum_xy_span_m=volumetric_aggregate_min_xy_m,
            minimum_z_span_m=volumetric_aggregate_min_z_m,
        ):
            result["skipped_volumetric_aggregate_count"] += 1
            continue
        background_rows.append(row)
    result["background_candidate_count"] = len(background_rows)

    total = 0
    largest_overlaps: list[tuple[float, int, dict[str, Any]]] = []
    for object_row in object_rows:
        for background_row in background_rows:
            intersection = _aabb_intersection(
                object_row,
                background_row,
                tolerance=penetration_tolerance,
            )
            if not intersection:
                continue
            total += 1
            item = {
                "object": object_row["prim"],
                "background_prim": background_row["prim"],
                "overlap": intersection,
            }
            ranked = (float(intersection["volume"]), total, item)
            if len(largest_overlaps) < max_reported_overlaps:
                heapq.heappush(largest_overlaps, ranked)
            elif max_reported_overlaps and ranked[0] > largest_overlaps[0][0]:
                heapq.heapreplace(largest_overlaps, ranked)
    reported = [item for _volume, _ordinal, item in sorted(largest_overlaps, reverse=True)]
    result["overlap_count"] = total
    result["overlaps"] = reported
    if total:
        result["status"] = "fail"
        result["errors"].append({
            "kind": "object_background_overlap",
            "count": total,
            "reported": len(reported),
            "truncated": total > len(reported),
            "suggestion": "Move the workcell to a background-clear region, then rerun this check; do not fix lateral occupancy by changing only Z.",
        })
    elif (result["skipped_broad_background_count"] or result["skipped_volumetric_aggregate_count"]
          or not object_rows or not background_rows):
        result["warnings"].append({
            "kind": "broad_background_prims_skipped",
            "count": result["skipped_broad_background_count"],
            "aggregate_count": result["skipped_volumetric_aggregate_count"],
            "object_count": len(object_rows),
            "background_count": len(background_rows),
            "max_background_span_m": float(max_background_span_m),
            "note": "Large shells and floors are poor AABB occupancy proxies; verify designated support separately.",
        })
        result["status"] = "warn"
    return result


def inspect_support_contacts(
    scene_path: str,
    *,
    prim_pairs: list[tuple[str, str]],
    tolerance: float = 0.005,
    minimum_xy_overlap: float = 0.001,
    mesh_aware: bool = False,
    minimum_mesh_coverage: float = 0.95,
) -> dict[str, Any]:
    """Validate explicit object/support pairs using bounds and optional mesh contact."""
    _Gf, _Sdf, Usd, UsdGeom, _UsdLux, _UsdPhysics = _require_pxr()
    path = Path(scene_path)
    result: dict[str, Any] = {
        "scene_path": str(path),
        "exists": path.exists(),
        "status": "pass",
        "tolerance_m": float(tolerance),
        "minimum_xy_overlap_m": float(minimum_xy_overlap),
        "mesh_aware": bool(mesh_aware),
        "minimum_mesh_coverage": float(minimum_mesh_coverage),
        "contacts": [],
        "warnings": [],
        "errors": [],
    }
    if not path.exists():
        result["status"] = "fail"
        result["errors"].append({"kind": "missing_scene", "path": str(path)})
        return result
    stage = Usd.Stage.Open(str(path))
    if not stage:
        result["status"] = "fail"
        result["errors"].append({"kind": "stage_open_failed", "path": str(path)})
        return result
    mpu = float(UsdGeom.GetStageMetersPerUnit(stage))
    result.update(coordinate_unit="m", meters_per_unit=mpu)
    if (not math.isfinite(mpu) or mpu <= 0 or not math.isfinite(tolerance)
            or tolerance < 0 or not math.isfinite(minimum_xy_overlap) or minimum_xy_overlap < 0
            or not 0 <= minimum_mesh_coverage <= 1):
        result.update(status="fail", errors=[{"kind": "invalid_units_or_tolerance"}])
        return result
    if UsdGeom.GetStageUpAxis(stage) != UsdGeom.Tokens.z:
        result.update(status="fail", errors=[{"kind": "support_requires_z_up"}])
        return result
    cache = UsdGeom.BBoxCache(
        Usd.TimeCode.Default(),
        [UsdGeom.Tokens.default_, UsdGeom.Tokens.render, UsdGeom.Tokens.proxy],
    )
    for object_path, support_path in prim_pairs:
        object_prim = stage.GetPrimAtPath(object_path)
        support_prim = stage.GetPrimAtPath(support_path)
        if not object_prim or not support_prim:
            result["errors"].append({
                "kind": "missing_contact_prim",
                "object": object_path,
                "support": support_path,
                "missing": [
                    item for item, prim in ((object_path, object_prim), (support_path, support_prim)) if not prim
                ],
            })
            continue
        composed_object_row = _bbox_row_from_prim(cache, object_prim, coordinate_scale=mpu)
        visible_object_meshes = _world_mesh_points_and_triangles(
            object_prim, Usd, UsdGeom, render_visible_only=True, coordinate_scale=mpu,
        )
        visible_object_bounds = _mesh_bounds_from_world_meshes(visible_object_meshes)
        if visible_object_bounds:
            object_row = {
                "prim": object_path,
                "type_name": object_prim.GetTypeName(),
                **visible_object_bounds,
            }
            object_bounds_source = "render_visible_mesh_points"
        else:
            object_row = composed_object_row
            object_bounds_source = "composed_bbox_fallback"
            result["warnings"].append({
                "kind": "object_visible_mesh_unavailable",
                "object": object_path,
                "message": "Support contact fell back to the composed object bbox.",
            })
        support_row = _bbox_row_from_prim(cache, support_prim, coordinate_scale=mpu)
        selected_surface_z = None
        discovered_surfaces = []
        if mesh_aware:
            support_meshes = _world_mesh_points_and_triangles(
                support_prim, Usd, UsdGeom, render_visible_only=True, coordinate_scale=mpu,
            )
            discovered_surfaces = _discover_horizontal_support_surfaces(support_meshes)
            object_bottom_z = float(object_row["min"][2])
            matching = [
                row for row in discovered_surfaces
                if abs(float(row["surface_z_m"]) - object_bottom_z) <= tolerance
            ]
            if matching:
                selected_surface_z = min(
                    matching,
                    key=lambda row: abs(float(row["surface_z_m"]) - object_bottom_z),
                )["surface_z_m"]
        contact = _support_contact_from_rows(
            object_row,
            support_row,
            tolerance=tolerance,
            minimum_xy_overlap=minimum_xy_overlap,
            support_surface_z=selected_surface_z,
        )
        contact["object_bounds_source"] = object_bounds_source
        contact["diagnostic_scope"] = "top_surface_relation_not_solid_intersection"
        if contact["status"] == "penetrating_support":
            contact["next_check"] = "For a pocket/fixture, rerun inspect-support-contact --contact-mode solid; below the rim alone is not solid penetration."
        contact["object_visible_bounds"] = object_row
        contact["object_composed_bounds"] = composed_object_row
        if mesh_aware and contact["status"] == "pass":
            mesh_contact = _mesh_support_contact(
                object_prim,
                support_prim,
                object_bottom_z=float(contact["object_bottom_z"]),
                support_surface_z=float(contact["support_surface_z"]),
                tolerance=tolerance,
                minimum_mesh_coverage=minimum_mesh_coverage,
                Usd=Usd,
                UsdGeom=UsdGeom,
                coordinate_scale=mpu,
            )
            contact.update(mesh_contact)
            contact["discovered_support_surface_count"] = len(discovered_surfaces)
            contact["selected_compound_support_surface_z_m"] = selected_surface_z
            if mesh_contact["mesh_support_status"] != "pass":
                contact["status"] = str(mesh_contact["mesh_support_status"])
        result["contacts"].append(contact)
        if contact["status"] != "pass":
            result["errors"].append({"kind": contact["status"], **contact})
    if result["errors"]:
        result["status"] = "fail"
    elif result["warnings"] or not prim_pairs:
        result["status"] = "warn"
        if not prim_pairs:
            result["warnings"].append({"kind": "missing_contact_pairs"})
    return result


def solve_support_placement(
    scene_path: str,
    *,
    object_prim_path: str,
    support_prim_path: str,
    obstacle_prim_paths: list[str] | None = None,
    architectural_obstacle_root_path: str | None = None,
    architectural_path_tokens: list[str] | None = None,
    group_member_prim_paths: list[str] | None = None,
    facing_target_prim_path: str | None = None,
    front_axis: str | None = None,
    facing_target_mode: str = "nearest",
    preferred_xy: list[float] | None = None,
    yaw_deltas_deg: list[float] | None = None,
    tolerance: float = 0.005,
    minimum_mesh_coverage: float = 0.95,
    support_margin: float = 0.01,
    obstacle_clearance: float = 0.01,
    architectural_max_span_m: float = 10.0,
    clearance_z: float = 0.0,
    grid_step: float = 0.05,
    max_candidates: int = 4096,
    support_surface_z_m: float | None = None,
    max_preferred_distance_m: float | None = None,
) -> dict[str, Any]:
    """Suggest a non-mutating transform correction on a real support mesh."""
    Gf, _Sdf, Usd, UsdGeom, _UsdLux, _UsdPhysics = _require_pxr()
    path = Path(scene_path)
    result: dict[str, Any] = {
        "scene_path": str(path),
        "object": object_prim_path,
        "support": support_prim_path,
        "status": "pass",
        "mutated_scene": False,
        "warnings": [],
        "errors": [],
    }
    if not path.is_file():
        result.update(status="fail", errors=[{"kind": "missing_scene", "path": str(path)}])
        return result
    stage = Usd.Stage.Open(str(path))
    if not stage:
        result.update(status="fail", errors=[{"kind": "stage_open_failed", "path": str(path)}])
        return result
    if not math.isclose(float(UsdGeom.GetStageMetersPerUnit(stage)), 1.0):
        result.update(status="fail", errors=[{"kind": "placement_requires_meter_stage",
            "suggestion": "Normalize into a new metre stage before solving; do not interpret centimetres as metres."}])
        return result
    object_prim = stage.GetPrimAtPath(object_prim_path)
    support_prim = stage.GetPrimAtPath(support_prim_path)
    missing = [
        prim_path for prim_path, prim in (
            (object_prim_path, object_prim), (support_prim_path, support_prim)
        ) if not prim
    ]
    if missing:
        result.update(status="fail", errors=[{"kind": "missing_placement_prim", "missing": missing}])
        return result
    cache = UsdGeom.BBoxCache(
        Usd.TimeCode.Default(),
        [UsdGeom.Tokens.default_, UsdGeom.Tokens.render, UsdGeom.Tokens.proxy],
    )
    composed_object_bounds = _bbox_row_from_prim(cache, object_prim)
    support_bounds = _bbox_row_from_prim(cache, support_prim)
    object_meshes = _world_mesh_points_and_triangles(
        object_prim, Usd, UsdGeom, render_visible_only=True,
    )
    visible_object_bounds = _mesh_bounds_from_world_meshes(object_meshes)
    object_bounds = visible_object_bounds or composed_object_bounds
    object_bounds_source = (
        "render_visible_mesh_points" if visible_object_bounds else "composed_bbox_fallback"
    )
    object_bottom_z = float(object_bounds["min"][2])
    support_meshes = _world_mesh_points_and_triangles(support_prim, Usd, UsdGeom)
    discovered_surfaces = _discover_horizontal_support_surfaces(support_meshes)
    support_surface_z, support_surface_source = _select_support_surface_z(
        discovered_surfaces,
        bbox_max_z=float(support_bounds["max"][2]),
        requested_z=support_surface_z_m,
    )
    if support_surface_z_m is not None and not any(
        abs(float(row["surface_z_m"]) - support_surface_z) <= max(tolerance, 0.01)
        for row in discovered_surfaces
    ):
        result.update(status="fail", errors=[{
            "kind": "requested_support_surface_missing",
            "requested_support_surface_z_m": support_surface_z,
            "available_support_surface_z_m": [round(float(row["surface_z_m"]), 6) for row in discovered_surfaces],
        }])
        return result
    contact_points = sorted({
        (round(point[0], 6), round(point[1], 6))
        for points, _triangles in object_meshes
        for point in points
        if point[2] <= object_bottom_z + tolerance
    })
    object_xy_points = [
        (float(point[0]), float(point[1]))
        for points, _triangles in object_meshes
        for point in points
    ]
    # Use the same render-visible geometry for bottom height, footprint, and
    # collision envelope. A composed prim bound can include RectLights, hidden
    # collision proxies, guides, and other helpers that are not the object body.
    object_bbox_xy = [
        (float(object_bounds["min"][0]), float(object_bounds["min"][1])),
        (float(object_bounds["min"][0]), float(object_bounds["max"][1])),
        (float(object_bounds["max"][0]), float(object_bounds["min"][1])),
        (float(object_bounds["max"][0]), float(object_bounds["max"][1])),
    ]
    object_xy_points.extend(object_bbox_xy)
    group_members_used: list[str] = []
    group_xy_points: list[tuple[float, float]] = []
    for member_path in group_member_prim_paths or []:
        member_prim = stage.GetPrimAtPath(member_path)
        if not member_prim:
            result["warnings"].append({"kind": "missing_group_member_prim", "prim": member_path})
            continue
        composed_member_bounds = _bbox_row_from_prim(cache, member_prim)
        member_meshes = _world_mesh_points_and_triangles(
            member_prim, Usd, UsdGeom, render_visible_only=True,
        )
        member_bounds = _mesh_bounds_from_world_meshes(member_meshes) or composed_member_bounds
        member_bottom_z = float(member_bounds["min"][2])
        member_contact = [
            (round(point[0], 6), round(point[1], 6))
            for points, _triangles in member_meshes
            for point in points
            if point[2] <= member_bottom_z + tolerance
        ]
        member_xy = [
            (float(point[0]), float(point[1]))
            for points, _triangles in member_meshes
            for point in points
        ]
        if not member_contact:
            min_x, min_y = float(member_bounds["min"][0]), float(member_bounds["min"][1])
            max_x, max_y = float(member_bounds["max"][0]), float(member_bounds["max"][1])
            member_contact = [(min_x, min_y), (min_x, max_y), (max_x, min_y), (max_x, max_y)]
        group_xy_points.extend(member_xy or member_contact)
        object_xy_points.extend(member_xy or member_contact)
        group_members_used.append(member_path)
    if group_xy_points:
        envelope_points = [*object_xy_points]
        min_x = min(point[0] for point in envelope_points)
        min_y = min(point[1] for point in envelope_points)
        max_x = max(point[0] for point in envelope_points)
        max_y = max(point[1] for point in envelope_points)
        contact_points.extend([
            (min_x, min_y), (min_x, max_y), (max_x, min_y), (max_x, max_y),
            ((min_x + max_x) * 0.5, (min_y + max_y) * 0.5),
        ])
    support_triangles = _top_support_triangles(
        support_meshes,
        support_surface_z=support_surface_z,
        tolerance=tolerance,
    )
    # Rounded CAD triangle compaction can close small shelf perforations and
    # make placement disagree with the exact post-composition contact audit.
    # The spatial index handles dense raw triangles; preserve the real holes.
    support_triangle_compaction = {
        "source_triangle_count": len(support_triangles),
        "triangle_count": len(support_triangles),
        "quantization_m": 0.0,
        "policy": "exact_geometry_no_hole_filling",
    }
    if not contact_points:
        min_x, min_y = float(object_bounds["min"][0]), float(object_bounds["min"][1])
        max_x, max_y = float(object_bounds["max"][0]), float(object_bounds["max"][1])
        contact_points = [
            (min_x, min_y), (min_x, max_y), (max_x, min_y), (max_x, max_y),
            ((min_x + max_x) / 2.0, (min_y + max_y) / 2.0),
        ]
        result["warnings"].append({
            "kind": "object_contact_mesh_unavailable",
            "message": "Using the object world bbox footprint because no bottom mesh vertices were available.",
        })
    if not object_xy_points:
        object_xy_points = list(contact_points)
    if not support_triangles:
        min_x, min_y = float(support_bounds["min"][0]), float(support_bounds["min"][1])
        max_x, max_y = float(support_bounds["max"][0]), float(support_bounds["max"][1])
        support_triangles = [
            ((min_x, min_y), (max_x, min_y), (max_x, max_y)),
            ((min_x, min_y), (max_x, max_y), (min_x, max_y)),
        ]
        result["warnings"].append({
            "kind": "simple_support_aabb_fallback",
            "message": "Using the top of a simple support bbox; keep mesh-aware validation mandatory for concave or perforated supports.",
        })
    xform_cache = UsdGeom.XformCache(Usd.TimeCode.Default())
    object_world = xform_cache.GetLocalToWorldTransform(object_prim)
    anchor = object_world.ExtractTranslation()
    current_anchor_xy = (float(anchor[0]), float(anchor[1]))
    target_xy = None
    target_bounds = None
    current_front = None
    if facing_target_prim_path:
        target_prim = stage.GetPrimAtPath(facing_target_prim_path)
        if not target_prim:
            result.update(status="fail", errors=[{
                "kind": "missing_facing_target_prim", "prim": facing_target_prim_path,
            }])
            return result
        if not front_axis:
            result.update(status="fail", errors=[{
                "kind": "front_axis_required", "message": "--front-axis is required with --facing-target",
            }])
            return result
        axis = normalize_front_axis(front_axis)
        local_front = FRONT_AXES_2D[axis]
        world_front = object_world.TransformDir(Gf.Vec3d(local_front[0], local_front[1], 0.0))
        current_front = (float(world_front[0]), float(world_front[1]))
        target_bounds = _bbox_row_from_prim(cache, target_prim)
        target_center = target_bounds["center"]
        target_xy = (float(target_center[0]), float(target_center[1]))
    obstacle_rows: list[dict[str, Any]] = []
    for obstacle_path in obstacle_prim_paths or []:
        obstacle_prim = stage.GetPrimAtPath(obstacle_path)
        if not obstacle_prim:
            result["warnings"].append({"kind": "missing_obstacle_prim", "prim": obstacle_path})
            continue
        obstacle_rows.append(_bbox_row_from_prim(cache, obstacle_prim))
    architectural_selection = {
        "rows": [], "candidate_count": 0, "selected_count": 0,
        "selected_prims": [], "skipped": {},
    }
    if architectural_obstacle_root_path:
        root_prim = stage.GetPrimAtPath(architectural_obstacle_root_path)
        if not root_prim:
            result.update(status="fail", errors=[{
                "kind": "missing_architectural_obstacle_root",
                "prim": architectural_obstacle_root_path,
            }])
            return result
        architectural_rows: list[dict[str, Any]] = []
        active_architectural_tokens = list(architectural_path_tokens or DEFAULT_ARCHITECTURAL_PATH_TOKENS)
        nonarchitectural_boundable_count = 0
        for prim in Usd.PrimRange(root_prim):
            if prim == root_prim or not prim.IsA(UsdGeom.Boundable):
                continue
            imageable = UsdGeom.Imageable(prim)
            if imageable and imageable.ComputeVisibility() == UsdGeom.Tokens.invisible:
                continue
            if not is_architectural_prim_path(str(prim.GetPath()), active_architectural_tokens):
                nonarchitectural_boundable_count += 1
                continue
            architectural_rows.append(_bbox_row_from_prim(cache, prim))
        placed_object_z_min = support_surface_z + float(clearance_z)
        placed_object_z_max = placed_object_z_min + float(object_bounds["size"][2])
        architectural_selection = select_architectural_obstacle_rows(
            architectural_rows,
            object_z_min=placed_object_z_min,
            object_z_max=placed_object_z_max,
            support_prim_path=support_prim_path,
            max_span_m=architectural_max_span_m,
            penetration_tolerance=tolerance,
        )
        architectural_selection["path_tokens"] = active_architectural_tokens
        architectural_selection["nonarchitectural_boundable_count"] = nonarchitectural_boundable_count
        obstacle_rows.extend(architectural_selection["rows"])
    solution = _solve_mesh_support_placement(
        object_contact_points=contact_points,
        object_xy_points=object_xy_points,
        support_triangles=support_triangles,
        current_anchor_xy=current_anchor_xy,
        preferred_xy=tuple(preferred_xy) if preferred_xy else None,
        yaw_deltas_deg=yaw_deltas_deg,
        current_front_vector=current_front,
        facing_target_xy=target_xy,
        facing_target_bounds=target_bounds,
        facing_target_mode=facing_target_mode,
        obstacle_bounds=obstacle_rows,
        minimum_coverage=minimum_mesh_coverage,
        support_margin=support_margin,
        obstacle_clearance=obstacle_clearance,
        max_preferred_distance_m=max_preferred_distance_m,
        grid_step=grid_step,
        max_candidates=max_candidates,
    )
    solution["object_contact_point_count"] = len(contact_points)
    solution["architectural_obstacle_root"] = architectural_obstacle_root_path
    solution["architectural_obstacle_candidate_count"] = architectural_selection["candidate_count"]
    solution["architectural_obstacle_count"] = architectural_selection["selected_count"]
    solution["architectural_obstacle_prims"] = architectural_selection["selected_prims"]
    solution["architectural_obstacle_skipped"] = architectural_selection["skipped"]
    solution["architectural_path_tokens"] = architectural_selection.get("path_tokens") or []
    solution["nonarchitectural_boundable_count"] = architectural_selection.get("nonarchitectural_boundable_count", 0)
    solution["group_envelope_member_prims"] = group_members_used
    solution["support_top_triangle_count"] = len(support_triangles)
    solution["support_triangle_compaction"] = support_triangle_compaction
    solution["object_bottom_z"] = object_bottom_z
    solution["object_bounds_source"] = object_bounds_source
    solution["object_visible_bounds"] = object_bounds
    solution["object_composed_bounds"] = composed_object_bounds
    solution["support_surface_z"] = support_surface_z
    solution["support_surface_source"] = support_surface_source
    solution["discovered_support_surfaces"] = [
        {key: value for key, value in row.items() if key != "triangles"}
        for row in discovered_surfaces
    ]
    if solution.get("status") != "pass":
        result["status"] = "fail"
        result["errors"].append({"kind": str(solution.get("reason") or "placement_failed"), **solution})
        result["solution"] = solution
        return result
    world_delta_xy = solution["suggested_world_translation_delta_xy"]
    world_delta = Gf.Vec3d(
        float(world_delta_xy[0]),
        float(world_delta_xy[1]),
        support_surface_z - object_bottom_z + float(clearance_z),
    )
    parent_prim = object_prim.GetParent()
    parent_world = xform_cache.GetLocalToWorldTransform(parent_prim) if parent_prim else Gf.Matrix4d(1.0)
    local_delta = parent_world.GetInverse().TransformDir(world_delta)
    solution["suggested_world_translation_delta"] = [float(value) for value in world_delta]
    solution["suggested_local_translation_delta"] = [float(value) for value in local_delta]
    solution["clearance_z_m"] = float(clearance_z)
    solution["facing_target"] = facing_target_prim_path
    solution["front_axis"] = normalize_front_axis(front_axis) if front_axis else None
    solution["facing_target_mode"] = facing_target_mode if facing_target_prim_path else None
    result["solution"] = solution
    return result


def solve_fixed_mount_placement(
    scene_path: str,
    *,
    object_prim_path: str,
    mount_target_prim_path: str,
    mount_anchor_prim_path: str,
    preferred_xy: list[float] | None = None,
    mount_surface: str = "ceiling",
    margin_m: float = 0.02,
) -> dict[str, Any]:
    """Align an explicit object mount anchor to a ceiling target."""
    Gf, _Sdf, Usd, UsdGeom, _UsdLux, _UsdPhysics = _require_pxr()
    path = Path(scene_path)
    result: dict[str, Any] = {
        "scene_path": str(path),
        "object": object_prim_path,
        "mount_target": mount_target_prim_path,
        "mount_anchor": mount_anchor_prim_path,
        "status": "pass",
        "mutated_scene": False,
        "warnings": [],
        "errors": [],
    }
    if not path.is_file():
        result.update(status="fail", errors=[{"kind": "missing_scene", "path": str(path)}])
        return result
    stage = Usd.Stage.Open(str(path))
    if not stage:
        result.update(status="fail", errors=[{"kind": "stage_open_failed", "path": str(path)}])
        return result
    if not math.isclose(float(UsdGeom.GetStageMetersPerUnit(stage)), 1.0):
        result.update(status="fail", errors=[{"kind": "placement_requires_meter_stage"}])
        return result
    object_prim = stage.GetPrimAtPath(object_prim_path)
    target_prim = stage.GetPrimAtPath(mount_target_prim_path)
    anchor_prim = stage.GetPrimAtPath(mount_anchor_prim_path)
    missing = [
        prim_path for prim_path, prim in (
            (object_prim_path, object_prim),
            (mount_target_prim_path, target_prim),
            (mount_anchor_prim_path, anchor_prim),
        ) if not prim
    ]
    if missing:
        result.update(status="fail", errors=[{"kind": "missing_mount_prim", "missing": missing}])
        return result
    cache = UsdGeom.BBoxCache(
        Usd.TimeCode.Default(),
        [UsdGeom.Tokens.default_, UsdGeom.Tokens.render, UsdGeom.Tokens.proxy],
    )
    xform_cache = UsdGeom.XformCache(Usd.TimeCode.Default())
    anchor_world_matrix = xform_cache.GetLocalToWorldTransform(anchor_prim)
    anchor_world = anchor_world_matrix.ExtractTranslation()
    solution = solve_fixed_mount_translation(
        anchor_world=[float(value) for value in anchor_world],
        target_bounds=_bbox_row_from_prim(cache, target_prim),
        preferred_xy=preferred_xy,
        mount_surface=mount_surface,
        margin_m=margin_m,
    )
    if solution.get("status") != "pass":
        result["status"] = "fail"
        result["errors"].append({"kind": str(solution.get("reason") or "mount_placement_failed"), **solution})
        result["solution"] = solution
        return result
    world_delta = Gf.Vec3d(*[float(value) for value in solution["suggested_world_translation_delta"]])
    parent_prim = object_prim.GetParent()
    parent_world = xform_cache.GetLocalToWorldTransform(parent_prim) if parent_prim else Gf.Matrix4d(1.0)
    local_delta = parent_world.GetInverse().TransformDir(world_delta)
    solution["suggested_local_translation_delta"] = [float(value) for value in local_delta]
    solution["mount_target_bounds"] = _bbox_row_from_prim(cache, target_prim)
    result["solution"] = solution
    return result


def solve_scene_placement_batch(
    config: dict[str, Any],
    spec: dict[str, Any],
    *,
    source_spec_path: str,
    scene_path: str,
    output_spec_path: str,
    output_scene_path: str,
    entity_ids: list[str] | None = None,
    strict: bool = False,
    tolerance: float = 0.005,
    minimum_mesh_coverage: float = 0.95,
    support_margin: float = 0.01,
    obstacle_clearance: float = 0.01,
    use_architectural_obstacles: bool = True,
    architectural_path_tokens: list[str] | None = None,
    architectural_max_span_m: float = 10.0,
    clearance_z: float = 0.0,
    grid_step: float = 0.05,
    max_candidates: int = 4096,
    quarantine_failed: bool = True,
) -> dict[str, Any]:
    """Solve a Scene Spec support graph into a complete, new spec version."""
    source_scene = Path(scene_path).resolve()
    output_scene = Path(output_scene_path).resolve()
    source_spec = Path(source_spec_path).resolve()
    output_spec = Path(output_spec_path).resolve()
    new_output(output_spec, root=(config.get("runtime") or {}).get("output_root"))
    check_scene_outputs(output_scene, config)
    owned_outputs: set[Path] = set()
    if source_scene == output_scene:
        raise ValueError("output_scene_path must differ from the source scene")
    if source_spec == output_spec:
        raise ValueError("output_spec_path must differ from the source spec")
    graph = build_support_dependency_graph(spec)
    if graph["status"] != "pass":
        return {
            "status": "fail",
            "source_spec": str(source_spec),
            "source_scene": str(source_scene),
            "output_spec": str(output_spec),
            "output_scene": str(output_scene),
            "graph": graph,
            "placements": [],
            "errors": [{"kind": "invalid_support_dependency_graph"}],
            "warnings": [],
        }

    entities = {
        str(item["id"]): item
        for item in spec.get("entities", [])
        if isinstance(item, dict) and item.get("id")
    }
    architectural_obstacle_root = (
        architectural_obstacle_root_for_spec(spec)
        if use_architectural_obstacles
        else None
    )
    requested = set(entity_ids or [])
    missing_ids = sorted(requested - set(entities))
    if missing_ids:
        return {
            "status": "fail",
            "source_spec": str(source_spec),
            "source_scene": str(source_scene),
            "output_spec": str(output_spec),
            "output_scene": str(output_scene),
            "graph": graph,
            "placements": [],
            "errors": [{"kind": "unknown_entity_ids", "entity_ids": missing_ids}],
            "warnings": [],
        }
    selected = {
        entity_id for entity_id, item in entities.items()
        if (item.get("support_target") not in (None, "") or item.get("mount_target") not in (None, ""))
        and (not requested or entity_id in requested)
    }
    frozen = [
        entity_id for entity_id, item in entities.items()
        if (item.get("support_target") not in (None, "") or item.get("mount_target") not in (None, ""))
        and entity_id not in selected
    ]
    working_spec = json.loads(json.dumps(spec))
    working_scene = source_scene
    placed = list(frozen)
    placements: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    quarantined: list[dict[str, Any]] = []
    successful = 0

    def quarantine_entity(entity_id: str, failure: dict[str, Any]) -> None:
        nonlocal working_spec, working_scene
        source_entity = entities[entity_id]
        working_spec["entities"] = [
            item for item in working_spec.get("entities", [])
            if not isinstance(item, dict) or str(item.get("id") or "") != entity_id
        ]
        working_spec["relations"] = [
            row for row in working_spec.get("relations", [])
            if not isinstance(row, dict) or str(row.get("subject") or "") != entity_id
        ]
        pending = {
            "id": entity_id,
            "semantic_name": source_entity.get("semantic_name"),
            "model_id": source_entity.get("model_id"),
            "logical_id": source_entity.get("logical_id"),
            "path": source_entity.get("path"),
            "prim": source_entity.get("prim"),
            "reason": "automatic_placement_failed",
            "failure": failure,
        }
        working_spec.setdefault("layout_pending_entities", []).append(pending)
        quarantined.append(pending)
        make_scene(config, working_spec, output=str(output_scene), _owned_outputs=owned_outputs)
        working_scene = output_scene

    nodes = {str(row["entity_id"]): row for row in graph["nodes"]}
    for entity_id in graph["order"]:
        if entity_id not in selected:
            continue
        entity = entities[entity_id]
        working_entities = {
            str(item["id"]): item
            for item in working_spec.get("entities", [])
            if isinstance(item, dict) and item.get("id")
        }
        working_entity = working_entities.get(entity_id, entity)
        support = (nodes[entity_id].get("support") or {})
        support_prim = str(support.get("prim") or "")
        obstacles = automatic_obstacles(graph, entity_id, placed)
        layout_constraints = working_entity.get("layout_constraints") or {}
        preferred_xy = layout_constraints.get("preferred_xy")
        yaw_deltas_deg = layout_constraints.get("yaw_deltas_deg")
        group_preference = resolve_functional_group_preference(
            working_entity,
            working_entities,
            room_bounds=(working_spec.get("room") or {}).get("usable_bounds_m"),
            room_margin_m=float((working_spec.get("constraints") or {}).get("room_margin_m") or 0.25),
        )
        if group_preference:
            preferred_xy = group_preference["preferred_xy"]
            yaw_deltas_deg = group_preference["yaw_deltas_deg"]
            if group_preference.get("connection"):
                # A declared port pair is allowed to make boundary contact with
                # its anchor. Other already placed peers remain obstacles, and
                # the post-solve port/overlap gates still reject penetration.
                anchor = working_entities.get(str(group_preference.get("anchor_id") or ""))
                if anchor:
                    anchor_prim = entity_prim(anchor)
                    obstacles = [prim for prim in obstacles if prim != anchor_prim]
        max_preferred_distance_m = (
            float(group_preference["position_tolerance_m"])
            if group_preference and group_preference.get("atomic") else None
        )
        relation_kind = str(support.get("relation_kind") or "support")
        group_member_prims: list[str] = []
        for candidate_id, candidate in working_entities.items():
            # A pose-free member selected in this batch still exists at its
            # temporary composition origin.  Including that unsolved prim in
            # the anchor envelope can span half the room and falsely reject a
            # valid functional group. Frozen/pre-positioned members remain
            # eligible evidence for an anchor envelope.
            if candidate_id in selected and candidate_id not in placed:
                continue
            group = ((candidate.get("layout_constraints") or {}).get("functional_group") or {})
            if str(group.get("anchor_id") or "") != entity_id:
                continue
            candidate_support = resolve_scene_target(working_spec, candidate.get("support_target"))
            if candidate_support.get("status") == "resolved" and str(candidate_support.get("prim") or "") == support_prim:
                group_member_prims.append(entity_prim(candidate))
        facing_target = working_entity.get("facing_target")
        facing_record = resolve_scene_target(working_spec, facing_target) if facing_target else None
        facing_prim = str(facing_record.get("prim")) if facing_record and facing_record.get("status") == "resolved" else None
        if facing_target and not facing_prim:
            row = {
                "entity_id": entity_id,
                "status": "fail",
                "kind": "unresolved_facing_target",
                "facing_target": str(facing_target),
            }
            placements.append(row)
            errors.append(row)
            if quarantine_failed:
                quarantine_entity(entity_id, row)
            if strict:
                break
            continue
        if relation_kind == "fixed_mount":
            mount_anchor = str(working_entity.get("mount_anchor_prim") or "")
            if not mount_anchor:
                result = {
                    "status": "fail", "warnings": [],
                    "errors": [{"kind": "mount_anchor_required", "entity_id": entity_id}],
                }
            else:
                result = solve_fixed_mount_placement(
                    str(working_scene),
                    object_prim_path=entity_prim(working_entity),
                    mount_target_prim_path=support_prim,
                    mount_anchor_prim_path=mount_anchor,
                    preferred_xy=preferred_xy,
                    mount_surface=str(layout_constraints.get("mount_surface") or "ceiling"),
                    margin_m=float(layout_constraints.get("support_margin_m") or support_margin),
                )
        else:
            entity_minimum_mesh_coverage = float(
                layout_constraints.get("minimum_mesh_coverage")
                if layout_constraints.get("minimum_mesh_coverage") is not None
                else minimum_mesh_coverage
            )
            result = solve_support_placement(
                str(working_scene),
                object_prim_path=entity_prim(working_entity),
                support_prim_path=support_prim,
                obstacle_prim_paths=obstacles,
                architectural_obstacle_root_path=architectural_obstacle_root,
                architectural_path_tokens=architectural_path_tokens,
                group_member_prim_paths=group_member_prims,
                facing_target_prim_path=facing_prim,
                front_axis=str(working_entity.get("front_axis")) if working_entity.get("front_axis") else None,
                facing_target_mode=str(working_entity.get("facing_target_mode") or "nearest"),
                preferred_xy=preferred_xy,
                yaw_deltas_deg=yaw_deltas_deg,
                tolerance=tolerance,
                minimum_mesh_coverage=entity_minimum_mesh_coverage,
                support_margin=float(layout_constraints.get("support_margin_m") or support_margin),
                obstacle_clearance=float(layout_constraints.get("obstacle_clearance_m") or obstacle_clearance),
                architectural_max_span_m=architectural_max_span_m,
                clearance_z=clearance_z,
                grid_step=grid_step,
                max_candidates=max_candidates,
                support_surface_z_m=(
                    float(layout_constraints["support_surface_z_m"])
                    if layout_constraints.get("support_surface_z_m") is not None
                    else None
                ),
                max_preferred_distance_m=max_preferred_distance_m,
            )
        row = {
            "entity_id": entity_id,
            "object_prim": entity_prim(working_entity),
            "support_prim": support_prim,
            "relation_kind": relation_kind,
            "automatic_obstacles": obstacles,
            "architectural_obstacle_root": architectural_obstacle_root,
            "architectural_obstacle_count": ((result.get("solution") or {}).get("architectural_obstacle_count")),
            "architectural_obstacle_prims": ((result.get("solution") or {}).get("architectural_obstacle_prims") or []),
            "preferred_xy": preferred_xy,
            "preferred_xy_source": "functional_group_runtime_anchor" if group_preference else "layout_constraints",
            "functional_group_anchor_id": group_preference.get("anchor_id") if group_preference else None,
            "functional_group_atomic": group_preference.get("atomic") if group_preference else None,
            "functional_group_position_tolerance_m": max_preferred_distance_m,
            "group_envelope_member_prims": group_member_prims,
            "facing_target_prim": facing_prim,
            "status": result.get("status"),
            "warnings": result.get("warnings") or [],
            "solution": result.get("solution"),
        }
        placements.append(row)
        warnings.extend(
            {"entity_id": entity_id, **warning}
            for warning in (result.get("warnings") or [])
        )
        if result.get("status") != "pass":
            failure = {
                "kind": "placement_failed",
                "entity_id": entity_id,
                "details": result.get("errors") or [],
            }
            errors.append(failure)
            if quarantine_failed:
                quarantine_entity(entity_id, failure)
            if strict:
                break
            continue
        working_spec = apply_placement_solution(
            working_spec,
            entity_id,
            result["solution"],
            support_prim=support_prim,
            obstacle_prims=obstacles,
            relation_kind=relation_kind,
        )
        working_spec = clear_pending_entity(working_spec, entity_id)
        make_scene(config, working_spec, output=str(output_scene), _owned_outputs=owned_outputs)
        working_scene = output_scene
        placed.append(entity_id)
        successful += 1

    status = "fail" if errors else "pass"
    plan_record = {
        "schema_version": "isaac-scene-placement-plan/v1",
        "status": status,
        "source_spec": str(source_spec),
        "source_scene": str(source_scene),
        "graph_order": graph["order"],
        "selected_entities": [item for item in graph["order"] if item in selected],
        "frozen_entities": frozen,
        "successful_placement_count": successful,
        "quarantined_entity_count": len(quarantined),
        "architectural_obstacle_root": architectural_obstacle_root,
        "quarantined_entities": quarantined,
        "placements": placements,
    }
    working_spec["placement_plan"] = plan_record
    output_spec.parent.mkdir(parents=True, exist_ok=True)
    if successful == 0 or not output_scene.is_file():
        make_scene(config, working_spec, output=str(output_scene), _owned_outputs=owned_outputs)
    else:
        # Refresh the scene from the final transforms and quarantine decisions.
        make_scene(config, working_spec, output=str(output_scene), _owned_outputs=owned_outputs)
    # make_scene writes an executable normalized sidecar at this same path.
    # The batch contract promises a new canonical v2 input spec, so restore the
    # relation-rich source form after composition instead of returning mapped
    # references that would duplicate entities on the next edit/solve cycle.
    output_spec.write_text(_json(working_spec), encoding="utf-8")
    return {
        "status": status,
        "source_spec": str(source_spec),
        "source_scene": str(source_scene),
        "output_spec": str(output_spec),
        "output_scene": str(output_scene),
        "graph": graph,
        "placements": placements,
        "successful_placement_count": successful,
        "quarantined_entity_count": len(quarantined),
        "quarantined_entities": quarantined,
        "warnings": warnings,
        "errors": errors,
    }


def inspect_facing(
    scene_path: str,
    *,
    prim_pairs: list[tuple[str, str]],
    front_axis_overrides: dict[str, str] | None = None,
    max_angle_deg: float = 20.0,
    target_mode: str = "nearest",
) -> dict[str, Any]:
    """Validate directional assets against explicit target prims in world XY."""
    Gf, _Sdf, Usd, UsdGeom, _UsdLux, _UsdPhysics = _require_pxr()
    path = Path(scene_path)
    result: dict[str, Any] = {
        "scene_path": str(path),
        "exists": path.exists(),
        "status": "pass",
        "max_angle_deg": float(max_angle_deg),
        "target_mode": target_mode,
        "pairs": [],
        "warnings": [],
        "errors": [],
    }
    if not path.exists():
        result["status"] = "fail"
        result["errors"].append({"kind": "missing_scene", "path": str(path)})
        return result
    if max_angle_deg <= 0 or max_angle_deg >= 180:
        result["status"] = "fail"
        result["errors"].append({"kind": "invalid_max_angle_deg", "value": max_angle_deg})
        return result
    if target_mode not in {"nearest", "center"}:
        result["status"] = "fail"
        result["errors"].append({"kind": "invalid_target_mode", "value": target_mode})
        return result
    stage = Usd.Stage.Open(str(path))
    if not stage:
        result["status"] = "fail"
        result["errors"].append({"kind": "stage_open_failed", "path": str(path)})
        return result
    cache = UsdGeom.BBoxCache(
        Usd.TimeCode.Default(),
        [UsdGeom.Tokens.default_, UsdGeom.Tokens.render, UsdGeom.Tokens.proxy],
    )
    xform_cache = UsdGeom.XformCache(Usd.TimeCode.Default())
    overrides = front_axis_overrides or {}
    for subject_path, target_path in prim_pairs:
        subject = stage.GetPrimAtPath(subject_path)
        target = stage.GetPrimAtPath(target_path)
        if not subject or not target:
            result["errors"].append({
                "kind": "missing_facing_prim",
                "subject": subject_path,
                "target": target_path,
                "missing": [
                    item for item, prim in ((subject_path, subject), (target_path, target)) if not prim
                ],
            })
            continue
        axis_value = overrides.get(subject_path)
        if axis_value is None:
            attr = subject.GetAttribute("scene:frontAxis")
            axis_value = attr.Get() if attr and attr.HasAuthoredValueOpinion() else None
        if axis_value is None:
            result["errors"].append({
                "kind": "front_axis_missing",
                "subject": subject_path,
                "suggestion": "Inspect the source asset, then author front_axis as +X, -X, +Y, or -Y.",
            })
            continue
        try:
            front_axis = normalize_front_axis(str(axis_value))
        except ValueError:
            result["errors"].append({
                "kind": "front_axis_invalid",
                "subject": subject_path,
                "value": axis_value,
                "allowed": sorted(FRONT_AXES_2D),
            })
            continue
        local_x, local_y = FRONT_AXES_2D[front_axis]
        local_front = Gf.Vec3d(local_x, local_y, 0.0)
        world_front = xform_cache.GetLocalToWorldTransform(subject).TransformDir(local_front)
        subject_center = _bbox_row_from_prim(cache, subject)["center"]
        target_row = _bbox_row_from_prim(cache, target)
        target_center = target_row["center"]
        if target_mode == "nearest":
            target_point = [
                max(float(target_row["min"][axis]), min(float(subject_center[axis]), float(target_row["max"][axis])))
                for axis in range(2)
            ]
            if all(abs(target_point[axis] - float(subject_center[axis])) <= 1e-9 for axis in range(2)):
                target_point = [float(target_center[0]), float(target_center[1])]
        else:
            target_point = [float(target_center[0]), float(target_center[1])]
        delta = [
            float(target_point[0] - subject_center[0]),
            float(target_point[1] - subject_center[1]),
        ]
        try:
            angle = facing_error_deg([world_front[0], world_front[1]], delta)
        except ValueError as exc:
            result["errors"].append({
                "kind": "facing_vector_degenerate",
                "subject": subject_path,
                "target": target_path,
                "summary": str(exc),
            })
            continue
        row = {
            "status": "pass" if angle <= max_angle_deg else "fail",
            "subject": subject_path,
            "target": target_path,
            "front_axis_local": front_axis,
            "front_world_xy": [float(world_front[0]), float(world_front[1])],
            "target_point_xy": target_point,
            "target_delta_xy": delta,
            "angle_error_deg": float(angle),
        }
        result["pairs"].append(row)
        if row["status"] == "fail":
            result["errors"].append({
                "kind": "facing_angle_exceeded",
                **row,
                "suggestion": "Correct the asset front_axis contract or rotate the reference around Z, then rerun.",
            })
    if result["errors"]:
        result["status"] = "fail"
    elif not prim_pairs:
        result["status"] = "warn"
        result["warnings"].append({"kind": "missing_facing_pairs"})
    return result


def _bbox_row_from_prim(cache: Any, prim: Any, *, coordinate_scale: float = 1.0) -> dict[str, Any]:
    box = cache.ComputeWorldBound(prim).ComputeAlignedBox()
    mn = box.GetMin() * coordinate_scale
    mx = box.GetMax() * coordinate_scale
    center = box.GetMidpoint() * coordinate_scale
    size = mx - mn
    return {
        "prim": str(prim.GetPath()),
        "type_name": prim.GetTypeName(),
        "min": _vec3_to_list(mn),
        "max": _vec3_to_list(mx),
        "center": _vec3_to_list(center),
        "size": _vec3_to_list(size),
    }


def _union_bbox_rows(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not rows:
        return None
    mn = [min(row["min"][axis] for row in rows) for axis in range(3)]
    mx = [max(row["max"][axis] for row in rows) for axis in range(3)]
    center = [(mn[axis] + mx[axis]) / 2.0 for axis in range(3)]
    size = [mx[axis] - mn[axis] for axis in range(3)]
    return {"min": mn, "max": mx, "center": center, "size": size}


def _infer_layout_bounds_prim(stage: Any, cache: Any, background_prim: Any) -> tuple[Any, dict[str, Any], list[dict[str, Any]]]:
    """Pick the most likely room shell child for layout checks.

    Complete GenieSim backgrounds often include a room shell plus loose background
    furniture, curtains, doors, and lights. The visual union bbox is too large for
    placement because props can extend past walls. For layout, prefer the largest
    room-like child under the background root when available.
    """
    background_row = _bbox_row_from_prim(cache, background_prim)
    bg_area = max(background_row["size"][0] * background_row["size"][1], 1e-9)
    candidates: list[dict[str, Any]] = []
    container_names = {"background", "objects", "object", "light", "lights", "colliders", "cameras"}
    stack = [(child, 1) for child in background_prim.GetChildren()]
    while stack:
        prim, path_depth = stack.pop(0)
        if path_depth < 3:
            stack.extend((child, path_depth + 1) for child in prim.GetChildren())
        row = _bbox_row_from_prim(cache, prim)
        size = row["size"]
        area = max(float(size[0]) * float(size[1]), 0.0)
        if area <= 1e-8 or area > bg_area * 4.0 or max(abs(float(v)) for v in [*row["min"], *row["max"], *size]) > 1e6:
            continue
        name = prim.GetName().lower()
        roomish_name = any(token in name for token in (
            "home", "room", "kitchen", "office", "study", "dining", "house", "apartment", "shell",
        ))
        is_container = prim.GetTypeName() == "Scope" or name in container_names
        candidates.append({
            "prim": str(prim.GetPath()),
            "name": prim.GetName(),
            "area_xy": area,
            "area_ratio": area / bg_area,
            "roomish_name": roomish_name,
            "is_container": is_container,
            "depth": path_depth,
            "bounds": row,
        })

    roomish = sorted(
        [item for item in candidates if item["roomish_name"] and item["area_ratio"] >= 0.20],
        key=lambda item: (item["area_ratio"], item["area_xy"]),
        reverse=True,
    )
    if roomish:
        best = roomish[0]
        prim = stage.GetPrimAtPath(best["prim"])
        if prim:
            return prim, best["bounds"], candidates

    ranked = sorted(
        [
            item for item in candidates
            if not item["is_container"] and 0.05 <= item["area_ratio"] < 0.98
        ],
        key=lambda item: (item["area_ratio"], item["area_xy"]),
        reverse=True,
    )
    if ranked:
        best = ranked[0]
        if best["area_ratio"] >= 0.60:
            prim = stage.GetPrimAtPath(best["prim"])
            if prim:
                return prim, best["bounds"], candidates
    return background_prim, background_row, candidates


def _collect_background_prop_rows(cache: Any, background_prim: Any, layout_prim_path: str) -> list[dict[str, Any]]:
    """Collect referenced background props outside the inferred room shell prim."""
    rows: list[dict[str, Any]] = []
    selected_paths: list[str] = []
    skip_names = {"light", "lights", "looks", "physicsmaterial", "material", "materials", "colliders", "cameras"}
    for prim in background_prim.GetChildren():
        stack = [prim]
        while stack:
            current = stack.pop(0)
            path_text = str(current.GetPath())
            name = current.GetName().lower()
            if path_text == layout_prim_path or path_text.startswith(layout_prim_path + "/"):
                continue
            if name in skip_names or any(part.lower() in skip_names for part in path_text.split("/")):
                continue
            if _prim_has_authored_reference(current):
                if any(path_text.startswith(parent + "/") for parent in selected_paths):
                    continue
                row = _bbox_row_from_prim(cache, current)
                size = row["size"]
                area = max(float(size[0]) * float(size[1]), 0.0)
                if area <= 1e-8 or max(abs(float(v)) for v in [*row["min"], *row["max"], *size]) > 1e6:
                    continue
                rows.append(row)
                selected_paths.append(path_text)
                continue
            stack.extend(current.GetChildren())
    return rows


def _outside_layout_candidates(
    rows: list[dict[str, Any]],
    layout_bounds: dict[str, Any],
    *,
    min_boundary_margin_m: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    edge_candidates: list[dict[str, Any]] = []
    outside_candidates: list[dict[str, Any]] = []
    for row in rows:
        margins = {
            "left_x": float(row["min"][0] - layout_bounds["min"][0]),
            "right_x": float(layout_bounds["max"][0] - row["max"][0]),
            "front_y": float(row["min"][1] - layout_bounds["min"][1]),
            "back_y": float(layout_bounds["max"][1] - row["max"][1]),
        }
        min_margin = min(margins.values())
        if min_margin < min_boundary_margin_m:
            edge_candidates.append({
                "prim": row["prim"],
                "min_margin_m": float(min_margin),
                "margins_xy": margins,
                "suggestion": "Move inward, hide/delete this background prop, or choose a cleaner background asset.",
            })
        if min_margin < -0.01:
            outside_candidates.append({
                "prim": row["prim"],
                "min_margin_m": float(min_margin),
                "margins_xy": margins,
            })
    edge_candidates.sort(key=lambda item: item["min_margin_m"])
    outside_candidates.sort(key=lambda item: item["min_margin_m"])
    return edge_candidates, outside_candidates


def inspect_layout_coverage(
    scene_path: str,
    *,
    background_prim_path: str = "/World/Background",
    objects_root_path: str = "/World/Objects",
    layout_prim_path: str | None = None,
    min_coverage_xy: float = 0.55,
    max_coverage_xy: float = 0.90,
    min_boundary_margin_m: float = 0.35,
) -> dict[str, Any]:
    """Check whether placed objects use a reasonable portion of the room bbox."""
    _Gf, _Sdf, Usd, UsdGeom, _UsdLux, _UsdPhysics = _require_pxr()
    path = Path(scene_path)
    result: dict[str, Any] = {
        "scene_path": str(path),
        "exists": path.exists(),
        "background_prim": background_prim_path,
        "objects_root": objects_root_path,
        "layout_prim": layout_prim_path,
        "min_coverage_xy": float(min_coverage_xy),
        "max_coverage_xy": float(max_coverage_xy),
        "min_boundary_margin_m": float(min_boundary_margin_m),
        "warnings": [],
    }
    if not path.exists():
        result["warnings"].append({"kind": "missing_scene", "path": str(path)})
        return result
    stage = Usd.Stage.Open(str(path))
    if not stage:
        result["warnings"].append({"kind": "stage_open_failed", "path": str(path)})
        return result

    cache = UsdGeom.BBoxCache(
        Usd.TimeCode.Default(),
        [UsdGeom.Tokens.default_, UsdGeom.Tokens.render, UsdGeom.Tokens.proxy],
    )
    bg = stage.GetPrimAtPath(background_prim_path)
    if not bg:
        result["warnings"].append({"kind": "missing_background_prim", "prim": background_prim_path})
        return result
    visual_background = _bbox_row_from_prim(cache, bg)
    result["visual_background_bounds"] = visual_background
    layout_candidates: list[dict[str, Any]] = []
    if layout_prim_path:
        layout_prim = stage.GetPrimAtPath(layout_prim_path)
        if not layout_prim:
            result["warnings"].append({"kind": "missing_layout_prim", "prim": layout_prim_path})
            return result
        background = _bbox_row_from_prim(cache, layout_prim)
    else:
        layout_prim, background, layout_candidates = _infer_layout_bounds_prim(stage, cache, bg)
        result["layout_prim"] = str(layout_prim.GetPath())
    result["background_bounds"] = background
    result["layout_bounds"] = background
    result["layout_bounds_source"] = "explicit" if layout_prim_path else "auto_inferred"
    result["layout_candidate_summary"] = sorted(
        [
            {
                "prim": item["prim"],
                "area_ratio": float(item["area_ratio"]),
                "roomish_name": bool(item["roomish_name"]),
            }
            for item in layout_candidates
        ],
        key=lambda item: item["area_ratio"],
        reverse=True,
    )[:10]

    objects_root = stage.GetPrimAtPath(objects_root_path)
    object_rows: list[dict[str, Any]] = []
    if objects_root:
        selected_paths: list[str] = []
        for prim in Usd.PrimRange(objects_root):
            if prim == objects_root:
                continue
            if _prim_has_authored_reference(prim):
                path_text = str(prim.GetPath())
                if any(path_text.startswith(parent + "/") for parent in selected_paths):
                    continue
                object_rows.append(_bbox_row_from_prim(cache, prim))
                selected_paths.append(path_text)
    result["object_count"] = len(object_rows)
    result["object_bounds"] = object_rows
    objects_union = _union_bbox_rows(object_rows)
    result["objects_union_bounds"] = objects_union
    if not objects_union:
        result["warnings"].append({"kind": "missing_objects", "root": objects_root_path})
        return result

    bg_size = background["size"]
    obj_size = objects_union["size"]
    coverage_x = obj_size[0] / bg_size[0] if bg_size[0] else 0.0
    coverage_y = obj_size[1] / bg_size[1] if bg_size[1] else 0.0
    result["coverage_xy"] = {
        "x": float(coverage_x),
        "y": float(coverage_y),
        "min": float(min(coverage_x, coverage_y)),
    }
    result["margins_xy"] = {
        "left_x": float(objects_union["min"][0] - background["min"][0]),
        "right_x": float(background["max"][0] - objects_union["max"][0]),
        "front_y": float(objects_union["min"][1] - background["min"][1]),
        "back_y": float(background["max"][1] - objects_union["max"][1]),
    }
    result["usable_bounds_xy"] = {
        "min": [
            float(background["min"][0] + min_boundary_margin_m),
            float(background["min"][1] + min_boundary_margin_m),
        ],
        "max": [
            float(background["max"][0] - min_boundary_margin_m),
            float(background["max"][1] - min_boundary_margin_m),
        ],
        "note": "Use this as a conservative interior layout box for floor furniture; do not treat the full visual background bbox as usable space.",
    }

    edge_candidates, outside_candidates = _outside_layout_candidates(
        object_rows,
        background,
        min_boundary_margin_m=min_boundary_margin_m,
    )
    result["edge_object_candidates"] = edge_candidates[:20]
    result["outside_background_candidates"] = outside_candidates
    background_prop_rows = _collect_background_prop_rows(cache, bg, str(result["layout_prim"]))
    bg_prop_edge, bg_prop_outside = _outside_layout_candidates(
        background_prop_rows,
        background,
        min_boundary_margin_m=min_boundary_margin_m,
    )
    result["background_prop_count"] = len(background_prop_rows)
    result["background_prop_bounds"] = background_prop_rows
    result["edge_background_prop_candidates"] = bg_prop_edge[:20]
    result["outside_layout_background_props"] = bg_prop_outside

    if max(bg_size[0], bg_size[1]) >= 6.0 and min(coverage_x, coverage_y) < min_coverage_xy:
        result["warnings"].append({
            "kind": "underused_large_room",
            "coverage_x": float(coverage_x),
            "coverage_y": float(coverage_y),
            "min_required": float(min_coverage_xy),
            "suggestion": "Spread anchor furniture across room zones using background bbox; do not keep all anchors in the central template area.",
        })
    tight_margins = {
        key: value
        for key, value in result["margins_xy"].items()
        if value < min_boundary_margin_m
    }
    if tight_margins:
        result["warnings"].append({
            "kind": "objects_too_close_to_room_boundary",
            "margins_xy": result["margins_xy"],
            "min_required_margin_m": float(min_boundary_margin_m),
            "tight_margins": tight_margins,
            "suggestion": "Shrink the usable layout box by the wall clearance and keep floor furniture bboxes inside it; background visual bbox includes wall/decor thickness and is not fully walkable interior.",
        })
    if max(bg_size[0], bg_size[1]) >= 6.0 and max(coverage_x, coverage_y) > max_coverage_xy:
        result["warnings"].append({
            "kind": "overfilled_room_bounds",
            "coverage_x": float(coverage_x),
            "coverage_y": float(coverage_y),
            "max_recommended": float(max_coverage_xy),
            "suggestion": "A full room should use multiple zones but leave wall clearance and circulation space; reduce edge-hugging objects or move them inward.",
        })
    if outside_candidates:
        result["warnings"].append({
            "kind": "objects_outside_background_bbox",
            "count": len(outside_candidates),
            "candidates": outside_candidates[:10],
            "suggestion": "These bboxes extend outside the background bbox. Move them inward using bbox-aware placement.",
        })
    if bg_prop_outside:
        result["warnings"].append({
            "kind": "background_props_outside_layout_bounds",
            "count": len(bg_prop_outside),
            "candidates": bg_prop_outside[:10],
            "suggestion": "The background asset contains props outside the inferred room shell. Use prim_overrides to hide/delete them or select a cleaner background.",
        })
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Lightweight Isaac scene skill tools")
    parser.add_argument("--config", default=None, help="Path to config JSON")
    parser.add_argument("--profile", default=None, help="Named deployment profile (optional)")
    parser.add_argument("--envelope", action="store_true", help="Wrap output in the stable harness result contract")
    parser.add_argument("--report-output", default=None, help="Also write the exact JSON response to this artifact path")
    parser.add_argument("--evidence-scene", default=None, help="Bind this check/review to the unchanged composed USD or .blend; required for task readiness")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("capabilities")
    sub.add_parser("list-domains")
    sub.add_parser("list-room-shell-presets")
    sub.add_parser("list-architectural-shells")

    p = sub.add_parser("inspect-architectural-shell-spatial")
    p.add_argument("scene_path")
    p.add_argument("--background-prim", default="/World/Background")
    p.add_argument("--floor-scope", default="/World/Background/Meshes/Base/ground")
    p.add_argument("--door-scope", default="/World/Background/Meshes/BaseAnimation/door")
    p.add_argument("--window-scope", default="/World/Background/Meshes/BaseAnimation/window")
    p.add_argument("--maximum-floor-thickness-m", type=float, default=0.25)
    p.add_argument("--minimum-floor-span-m", type=float, default=0.25)
    p.add_argument("--include-components", action="store_true")

    p = sub.add_parser("select-architectural-shell")
    p.add_argument("room_type")
    p.add_argument("--required-feature", dest="required_features", action="append", default=[])
    p.add_argument(
        "--max-occupancy",
        choices=["architecture_only", "empty", "sparse", "unknown", "furnished"],
        default="sparse",
    )

    p = sub.add_parser("apply-architectural-shell")
    p.add_argument("spec_json")
    p.add_argument("--shell", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--replace-background", action="store_true")
    p.add_argument("--replace-procedural-shell", action="store_true")
    p.add_argument(
        "--preserve-existing-layout",
        action="store_true",
        help="Diagnostic A/B only: retain old poses even though the shell changed.",
    )

    p = sub.add_parser("route-domain")
    p.add_argument("prompt")

    p = sub.add_parser("init-scene-task")
    p.add_argument("--text", default="")
    p.add_argument("--image", action="append", default=[])
    p.add_argument("--scene", default=None)
    p.add_argument("--blend", default=None)
    p.add_argument("--scope", choices=["layout_preview", "lookdev_prototype", "simulation_scene"], default="lookdev_prototype")
    p.add_argument("--target", choices=["isaac", "blender", "roundtrip"], default="isaac")
    p.add_argument("--max-iterations", type=int, default=3)
    p.add_argument("--output", required=True)

    p = sub.add_parser("check-scene-task")
    p.add_argument("task_json")

    p = sub.add_parser("create-usd-revision")
    p.add_argument("source")
    p.add_argument("--output", required=True)
    p.add_argument("--deactivate", action="append", default=[])

    p = sub.add_parser("prepare-usd-assets")
    p.add_argument("source")
    p.add_argument("--output", required=True)
    p.add_argument("--prim", action="append", required=True)

    p = sub.add_parser("init-scene-brief")
    p.add_argument("--text", default=None)
    p.add_argument("--reference-image", action="append", default=[])
    p.add_argument("--domain", default=None)
    p.add_argument("--room-type", required=True)
    p.add_argument("--room-size-m", nargs=3, type=float, required=True)
    p.add_argument("--quality-policy", choices=["layout_only", "realistic", "reference_match"], default="realistic")
    p.add_argument("--room-shell-preset", default=None)
    p.add_argument("--output", required=True)

    p = sub.add_parser("plan-scene-enrichment")
    p.add_argument("brief_json")
    p.add_argument("--density", choices=["sparse", "normal", "rich"], default="rich")
    p.add_argument("--profiles", default=None, help="Optional enrichment profile library JSON")
    p.add_argument("--scene-kit-catalog", default=None, help="Optional pose-free Synthesis 28 scene-kit catalog")
    p.add_argument("--output", required=True)

    p = sub.add_parser("build-synthesis28-scene-kits")
    p.add_argument("--object-catalog", required=True)
    p.add_argument("--environment-catalog", default=None)
    p.add_argument("--output", required=True)

    p = sub.add_parser("search-synthesis28-scene-kits")
    p.add_argument("query")
    p.add_argument("--catalog", required=True)
    p.add_argument("--domain", default=None)
    p.add_argument("--room-type", default=None)
    p.add_argument("--topk", type=int, default=3)

    p = sub.add_parser("materialize-scene-enrichment")
    p.add_argument("brief_json")
    p.add_argument("plan_json")
    p.add_argument("bindings_json")
    p.add_argument("--output", required=True)
    p.add_argument("--report", required=True)

    p = sub.add_parser("merge-enrichment-inventory")
    p.add_argument("base_inventory_json")
    p.add_argument("enriched_inventory_json")
    p.add_argument("materialization_report_json")
    p.add_argument("--output", required=True)
    p.add_argument("--report", required=True)

    p = sub.add_parser("apply-room-shell-preset")
    p.add_argument("input_json")
    p.add_argument("--preset", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--input-kind", choices=["auto", "scene-brief", "scene-spec"], default="auto")
    p.add_argument("--room-size-m", nargs=3, type=float, default=None)
    p.add_argument("--room-center-m", nargs=3, type=float, default=[0.0, 0.0, 0.0])
    p.add_argument("--preserve-collider-id", action="append", default=[])
    p.add_argument("--replace-lighting", action="store_true")

    p = sub.add_parser("validate-scene-brief")
    p.add_argument("brief_json")

    p = sub.add_parser("compile-scene-brief")
    p.add_argument("brief_json")
    p.add_argument("--output", required=True)
    p.add_argument("--scene-output", default=None)
    p.add_argument(
        "--architectural-shell",
        default="auto",
        help="auto, none, or an explicit verified asset-backed shell id (default: auto)",
    )
    p.add_argument("--required-shell-feature", dest="required_shell_features", action="append", default=None)
    p.add_argument(
        "--max-shell-occupancy",
        choices=["architecture_only", "empty", "sparse", "unknown", "furnished"],
        default="sparse",
    )
    p.add_argument("--keep-procedural-shell", action="store_true")

    sub.add_parser("doctor")

    p = sub.add_parser("list-libraries")
    p.add_argument("--counts", action="store_true")
    p.add_argument("--include-slow-counts", action="store_true")

    p = sub.add_parser("asset-inventory")
    p.add_argument("--library", default=None)

    p = sub.add_parser("search-assets")
    p.add_argument("query")
    p.add_argument("--libraries", nargs="*", default=None)
    p.add_argument("--types", nargs="*", default=None)
    p.add_argument("--topk", type=int, default=10)
    p.add_argument("--force-refresh", action="store_true")
    p.add_argument("--allow-slow", action="store_true")
    p.add_argument("--exhaustive", action="store_true")

    p = sub.add_parser("search-semantic-assets")
    p.add_argument("query")
    p.add_argument("--catalog", default=None)
    p.add_argument("--canonical-class", default=None)
    p.add_argument("--subtype", default=None)
    p.add_argument("--style", nargs="*", default=[])
    p.add_argument("--material", nargs="*", default=[])
    p.add_argument("--color", nargs="*", default=[])
    p.add_argument("--min-size-m", nargs=3, type=float, default=None)
    p.add_argument("--max-size-m", nargs=3, type=float, default=None)
    p.add_argument("--topk", type=int, default=20)
    p.add_argument("--include-unavailable", action="store_true")
    p.add_argument("--broad", action="store_true", help="Disable automatic semantic-class inference.")

    p = sub.add_parser("list-semantic-classes")
    p.add_argument("--catalog", default=None)
    p.add_argument("--prefix", default=None)
    p.add_argument("--callable-only", action="store_true")
    p.add_argument("--limit", type=int, default=200)

    p = sub.add_parser("search-visual-assets")
    p.add_argument("query")
    p.add_argument("--reference-crop", default=None)
    p.add_argument("--canonical-class", default=None)
    p.add_argument("--fallback-class", action="append", default=[])
    p.add_argument("--min-size-m", nargs=3, type=float, default=None)
    p.add_argument("--max-size-m", nargs=3, type=float, default=None)
    p.add_argument("--target-size-m", nargs=3, type=float, default=None)
    p.add_argument("--size-weight", type=float, default=0.20)
    p.add_argument("--topk", type=int, default=20)
    p.add_argument("--device", default="auto")
    p.add_argument("--include-unavailable", action="store_true")

    p = sub.add_parser("annotate-semantic-asset")
    selector = p.add_mutually_exclusive_group(required=True)
    selector.add_argument("--logical-id", default=None)
    selector.add_argument("--path", dest="source_path", default=None)
    p.add_argument("--catalog", default=None)
    p.add_argument("--canonical-class", default=None)
    p.add_argument("--subtype", default=None)
    p.add_argument("--semantic-name", default=None)
    p.add_argument("--preview-index", default=None)
    p.add_argument("--front-axis", choices=["+X", "-X", "+Y", "-Y"], default=None)
    p.add_argument("--meters-per-unit", type=float, default=None)
    p.add_argument("--bbox-json", default=None)
    p.add_argument("--style", nargs="*", default=[])
    p.add_argument("--material", nargs="*", default=[])
    p.add_argument("--color", nargs="*", default=[])
    p.add_argument(
        "--orientation-mode",
        choices=["front_facing", "axial", "bidirectional", "radial", "symmetric", "scene_root", "unknown"],
        default=None,
    )
    p.add_argument("--primary-axis", choices=["X", "Y"], default=None)
    p.add_argument("--operator-axis", choices=["+X", "-X", "+Y", "-Y"], default=None)
    p.add_argument("--flow-axis", choices=["+X", "-X", "+Y", "-Y", "X", "Y", "unknown"], default=None)
    p.add_argument("--equivalence-group", default=None)

    p = sub.add_parser("set-semantic-asset-state")
    selector = p.add_mutually_exclusive_group(required=True)
    selector.add_argument("--logical-id", default=None)
    selector.add_argument("--path", dest="source_path", default=None)
    p.add_argument("--catalog", default=None)
    p.add_argument("--state", choices=["quarantine", "ready"], required=True)
    p.add_argument("--reason", required=True)
    p.add_argument("--evidence", default=None)

    p = sub.add_parser("get-fabrica-recipe")
    p.add_argument("assembly")

    p = sub.add_parser("list-home-backgrounds")
    p.add_argument("--room-type", default=None)
    p.add_argument("--with-health", action="store_true")

    p = sub.add_parser("inspect-asset-health")
    p.add_argument("asset_path")

    p = sub.add_parser("inspect-asset-local-geometry")
    p.add_argument("asset_path")
    p.add_argument("--root-prim", default=None)

    p = sub.add_parser("inspect-asset-units")
    p.add_argument("asset_path")
    p.add_argument("--target-meters-per-unit", type=float, default=1.0)

    p = sub.add_parser("make-scene")
    p.add_argument("spec_json")
    p.add_argument("--output", default=None)

    p = sub.add_parser("validate-scene")
    p.add_argument("scene_path")

    p = sub.add_parser("inspect-articulation-joints", help="Static joint anchor/axis/limit check; not IK or dynamics")
    p.add_argument("scene_path")
    p.add_argument("--root", action="append", default=None)
    p.add_argument("--tolerance-m", type=float, default=0.002)

    p = sub.add_parser("pose-articulation", help="Pose an authored joint tree into a new USD; degrees/metres, not IK")
    p.add_argument("scene_path")
    p.add_argument("--root", required=True)
    p.add_argument("--positions", required=True, help="JSON map: absolute joint path -> degrees (revolute) or metres (prismatic)")
    p.add_argument("--output", required=True)

    p = sub.add_parser("initialize-articulation", help="Plan authored joint initialization; optional output writes a NEW static pose, not a simulation")
    p.add_argument("scene_path")
    p.add_argument("--root", required=True)
    p.add_argument("--from", dest="position_source", choices=["drive-targets", "authored-state"], required=True)
    p.add_argument("--output", help="Omit to inspect the complete proposed joint positions without writing a scene")

    p = sub.add_parser("inspect-bounds")
    p.add_argument("scene_path")
    p.add_argument("--prim", dest="prim_paths", action="append", default=None)
    p.add_argument("--root", default="/World/Objects")
    p.add_argument("--floor-z", type=float, default=0.0)
    p.add_argument("--tolerance", type=float, default=0.005)

    p = sub.add_parser("inspect-scene-overlaps")
    p.add_argument("scene_path")
    p.add_argument("--object", dest="object_prim_paths", action="append", default=None)
    p.add_argument("--objects-root", default="/World/Objects")
    p.add_argument("--background-prim", default="/World/Background")
    p.add_argument("--penetration-tolerance", type=float, default=0.005)
    p.add_argument("--max-background-span-m", type=float, default=10.0)
    p.add_argument("--volumetric-aggregate-min-xy-m", type=float, default=2.0)
    p.add_argument("--volumetric-aggregate-min-z-m", type=float, default=1.0)
    p.add_argument("--ignore-background-prim", dest="ignore_background_prim_paths", action="append", default=None)
    p.add_argument("--max-reported-overlaps", type=int, default=100)

    p = sub.add_parser("inspect-support-contact")
    p.add_argument("scene_path")
    p.add_argument("--pair", dest="prim_pairs", action="append", required=True, help="OBJECT_PRIM=SUPPORT_PRIM")
    p.add_argument("--tolerance", type=float, default=0.005)
    p.add_argument("--minimum-xy-overlap", type=float, default=0.001)
    p.add_argument(
        "--mesh-aware",
        action="store_true",
        help="Require bottom contact points to land on real top-facing support mesh triangles, preserving holes and concave edges.",
    )
    p.add_argument("--minimum-mesh-coverage", type=float, default=0.95)

    p.add_argument("--contact-mode", choices=["surface", "solid"], default="surface",
                   help="surface: legacy planar support; solid: full volume intersection plus pocket/contact probes")
    p.add_argument("--sample-pitch-m", type=float, default=.01, help="Solid-contact probe spacing; not collision tolerance")
    p.add_argument("--max-contact-samples", type=int, default=20000)

    p = sub.add_parser("solve-support-placement")
    p.add_argument("scene_path")
    p.add_argument("--object", dest="object_prim_path", required=True)
    p.add_argument("--support", dest="support_prim_path", required=True)
    p.add_argument("--obstacle", dest="obstacle_prim_paths", action="append", default=[])
    p.add_argument("--architectural-obstacle-root", default=None)
    p.add_argument("--architectural-path-token", dest="architectural_path_tokens", action="append", default=None)
    p.add_argument("--architectural-max-span-m", type=float, default=10.0)
    p.add_argument("--group-member", dest="group_member_prim_paths", action="append", default=[])
    p.add_argument("--facing-target", dest="facing_target_prim_path", default=None)
    p.add_argument("--front-axis", choices=sorted(FRONT_AXES_2D), default=None)
    p.add_argument("--target-mode", choices=["nearest", "center"], default="nearest")
    p.add_argument("--preferred-xy", nargs=2, type=float, default=None)
    p.add_argument("--yaw-delta-deg", dest="yaw_deltas_deg", action="append", type=float, default=None)
    p.add_argument("--tolerance", type=float, default=0.005)
    p.add_argument("--minimum-mesh-coverage", type=float, default=0.95)
    p.add_argument("--support-margin", type=float, default=0.01)
    p.add_argument("--obstacle-clearance", type=float, default=0.01)
    p.add_argument("--clearance-z", type=float, default=0.0)
    p.add_argument("--grid-step", type=float, default=0.05)
    p.add_argument("--max-candidates", type=int, default=4096)
    p.add_argument("--support-surface-z-m", type=float, default=None)

    p = sub.add_parser("derive-constraint-layout-spec")
    p.add_argument("exact_spec_json")
    p.add_argument("--output-spec", required=True)
    p.add_argument("--report", required=True)
    p.add_argument("--zone-columns", type=int, default=5)
    p.add_argument("--zone-rows", type=int, default=5)
    p.add_argument("--yaw-step-deg", type=float, default=90.0)
    p.add_argument("--room-margin-m", type=float, default=0.25)

    p = sub.add_parser("derive-semantic-layout-spec")
    p.add_argument("inventory_spec_json", help="Pose-free asset inventory and verified support graph")
    p.add_argument("scene_brief_json", help="Validated Scene Brief with text/image provenance and optional zone overrides")
    p.add_argument("--output-spec", required=True)
    p.add_argument("--report", required=True)
    p.add_argument("--room-margin-m", type=float, default=0.25)

    p = sub.add_parser("solve-scene-placement-batch")
    p.add_argument("spec_json")
    p.add_argument("scene_path")
    p.add_argument("--output-spec", required=True)
    p.add_argument("--output-scene", required=True)
    p.add_argument("--entity-id", dest="entity_ids", action="append", default=[])
    p.add_argument("--strict", action="store_true")
    p.add_argument("--tolerance", type=float, default=0.005)
    p.add_argument("--minimum-mesh-coverage", type=float, default=0.95)
    p.add_argument("--support-margin", type=float, default=0.01)
    p.add_argument("--obstacle-clearance", type=float, default=0.01)
    p.add_argument("--no-architectural-obstacles", action="store_true")
    p.add_argument("--architectural-path-token", dest="architectural_path_tokens", action="append", default=None)
    p.add_argument("--architectural-max-span-m", type=float, default=10.0)
    p.add_argument("--clearance-z", type=float, default=0.0)
    p.add_argument("--grid-step", type=float, default=0.05)
    p.add_argument("--max-candidates", type=int, default=4096)
    p.add_argument("--keep-failed-entities", action="store_true")

    p = sub.add_parser("extract-synthesis-support-relations")
    p.add_argument("--scenes-root", required=True)
    p.add_argument("--instances-db", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--vertical-tolerance", type=float, default=0.05)
    p.add_argument("--floor-tolerance", type=float, default=0.06)
    p.add_argument("--minimum-xy-coverage", type=float, default=0.02)

    p = sub.add_parser("refine-synthesis-support-relations")
    p.add_argument("baseline_report")
    p.add_argument("--output", required=True)
    p.add_argument("--surface-cache", required=True)
    p.add_argument("--scene", dest="scene_slugs", action="append", default=[])
    p.add_argument("--vertical-tolerance", type=float, default=0.05)
    p.add_argument("--minimum-mesh-coverage", type=float, default=0.50)
    p.add_argument("--planarity-tolerance", type=float, default=0.006)
    p.add_argument("--contact-tolerance", type=float, default=0.006)
    p.add_argument("--cell-size", type=float, default=0.5)
    p.add_argument("--z-step", type=float, default=0.01)
    p.add_argument("--floor-plane-contact-tolerance", type=float, default=0.015)

    p = sub.add_parser("evaluate-synthesis28-reconstruction")
    p.add_argument("spec_json")
    p.add_argument("scene_path")
    p.add_argument("--matrix-tolerance", type=float, default=1e-8)
    p.add_argument("--bounds-tolerance-m", type=float, default=1e-4)
    p.add_argument("--max-failures", type=int, default=100)

    p = sub.add_parser("evaluate-synthesis28-material-parity")
    p.add_argument("spec_json")
    p.add_argument("scene_path")
    p.add_argument("--max-failures", type=int, default=100)

    p = sub.add_parser("create-synthesis28-render-pair")
    p.add_argument("spec_json")
    p.add_argument("scene_path")
    p.add_argument("--output-dir", required=True)

    p = sub.add_parser("create-synthesis28-camera-candidates")
    p.add_argument("spec_json")
    p.add_argument("scene_path")
    p.add_argument("--output-scene", required=True)
    p.add_argument("--manifest", required=True)
    p.add_argument("--azimuth-count", type=int, default=8)
    p.add_argument("--azimuth-center-deg", type=float, default=None)
    p.add_argument("--azimuth-span-deg", type=float, default=360.0)
    p.add_argument("--distance-scale", type=float, action="append", default=[])
    p.add_argument("--eye-height-offset-m", type=float, action="append", default=[])

    p = sub.add_parser("rank-synthesis28-camera-renders")
    p.add_argument("manifest")
    p.add_argument("reference_image")
    p.add_argument("--render-dir", required=True)
    p.add_argument("--output", required=True)

    p = sub.add_parser("create-prompt-camera-candidates")
    p.add_argument("spec_json")
    p.add_argument("scene_path")
    p.add_argument("--output-scene", required=True)
    p.add_argument("--manifest", required=True)
    p.add_argument("--inset-m", type=float, default=0.45)
    p.add_argument("--eye-height-m", type=float, action="append", default=[])
    p.add_argument("--focal-length-mm", type=float, action="append", default=[])
    p.add_argument("--look-at-z-offset-m", type=float, action="append", default=[])
    p.add_argument("--look-at-lateral-offset-m", type=float, action="append", default=[])
    p.add_argument("--max-candidates", type=int, default=24)

    p = sub.add_parser("rank-prompt-camera-renders")
    p.add_argument("manifest")
    p.add_argument("--render-dir", required=True)
    p.add_argument("--evidence-dir")
    p.add_argument("--visual-review-packet")
    p.add_argument("--visual-agent-review")
    p.add_argument("--output", required=True)

    p = sub.add_parser("audit-prompt-regression-suite")
    p.add_argument("manifest")
    p.add_argument("--output", required=True)

    p = sub.add_parser("audit-synthesis28-expansion-readiness")
    p.add_argument("scene_kit_catalog")
    p.add_argument("--output", required=True)
    p.add_argument("--exclude-scene", dest="excluded_scenes", action="append", default=[])
    p.add_argument("--prefer-domain", dest="preferred_domains", action="append", default=[])
    p.add_argument("--max-canaries", type=int, default=5)

    p = sub.add_parser("audit-synthesis-gap")
    p.add_argument("spec_json")
    p.add_argument("--scene-kit-catalog", required=True)
    p.add_argument("--match-count", type=int, default=2)
    p.add_argument("--output", required=True)

    p = sub.add_parser("apply-scene-appearance")
    p.add_argument("spec_json")
    p.add_argument("--profile", default="auto", choices=["auto", "office", "living", "clinical", "warehouse", "workshop", "transit", "laboratory"])
    p.add_argument("--output", required=True)

    p = sub.add_parser("audit-material-coverage")
    p.add_argument("scene_path")
    p.add_argument("--root", default="/World")
    p.add_argument("--camera", default=None)
    p.add_argument("--minimum-score", type=float, default=0.65)
    p.add_argument("--minimum-effective-texture-fraction", type=float, default=0.55)
    p.add_argument("--maximum-unmapped-texture-fraction", type=float, default=0.05)
    p.add_argument("--maximum-unbound-fraction", type=float, default=0.02)
    p.add_argument("--max-gaps", type=int, default=30)
    p.add_argument("--output", required=True)

    p = sub.add_parser("create-visual-review-packet")
    p.add_argument("spec_json")
    p.add_argument("manifest")
    p.add_argument("ranking")
    p.add_argument("--render-dir", required=True)
    p.add_argument("--scene-brief")
    p.add_argument("--output", required=True)

    p = sub.add_parser("validate-visual-agent-review")
    p.add_argument("packet")
    p.add_argument("review")
    p.add_argument("--output", required=True)

    p = sub.add_parser("create-quality-review-packet")
    p.add_argument("comparison_manifest")
    p.add_argument("--axis", required=True, choices=["rendering_quality", "scene_content_quality"])
    p.add_argument("--output", required=True)

    p = sub.add_parser("validate-quality-agent-review")
    p.add_argument("packet")
    p.add_argument("review")
    p.add_argument("--output", required=True)

    p = sub.add_parser("create-content-iteration-packet")
    p.add_argument("brief_json")
    p.add_argument("spec_json")
    p.add_argument("render_manifest")
    p.add_argument("--iteration", type=int, default=0)
    p.add_argument("--phase", choices=["anchors", "functional", "detail"], default="anchors")
    p.add_argument("--max-iterations", type=int, default=6)
    p.add_argument("--previous-validation")
    p.add_argument("--output", required=True)

    p = sub.add_parser("validate-content-iteration-review")
    p.add_argument("packet")
    p.add_argument("review")
    p.add_argument("--output", required=True)

    p = sub.add_parser("create-scene-revision-request")
    p.add_argument("packet")
    p.add_argument("validation")
    p.add_argument("--output", required=True)

    p = sub.add_parser("promote-prompt-camera")
    p.add_argument("spec_json")
    p.add_argument("scene_path")
    p.add_argument("manifest")
    p.add_argument("ranking")
    p.add_argument("--output-spec", required=True)
    p.add_argument("--output-scene", required=True)
    p.add_argument("--candidate-id")
    p.add_argument("--confirmation", required=True)

    p = sub.add_parser("confirm-prompt-camera-selection")
    p.add_argument("manifest")
    p.add_argument("ranking")
    p.add_argument("--baseline-render", required=True)
    p.add_argument("--selected-render", required=True)
    p.add_argument("--baseline-visibility")
    p.add_argument("--selected-visibility")
    p.add_argument("--output", required=True)

    p = sub.add_parser("export-synthesis28-reconstruction-specs")
    p.add_argument("--catalog", required=True, help="Synthesis object-library SQLite")
    p.add_argument("--support-relations", required=True, help="Stage 11 mesh support report")
    p.add_argument("--output-dir", required=True)
    p.add_argument("--scene", dest="scene_slugs", action="append", default=[])

    p = sub.add_parser("inspect-facing")
    p.add_argument("scene_path")
    p.add_argument("--pair", dest="prim_pairs", action="append", required=True, help="SUBJECT_PRIM=TARGET_PRIM")
    p.add_argument("--front-axis", dest="front_axes", action="append", default=None, help="SUBJECT_PRIM=+X|-X|+Y|-Y")
    p.add_argument("--max-angle-deg", type=float, default=20.0)
    p.add_argument("--target-mode", choices=["nearest", "center"], default="nearest")

    p = sub.add_parser("inspect-functional-groups")
    p.add_argument("spec_json")
    p.add_argument("--position-tolerance-m", type=float, default=0.45)
    p.add_argument("--yaw-tolerance-deg", type=float, default=5.0)

    p = sub.add_parser("inspect-layout-coverage")
    p.add_argument("scene_path")
    p.add_argument("--background-prim", default="/World/Background")
    p.add_argument("--objects-root", default="/World/Objects")
    p.add_argument("--layout-prim", default=None)
    p.add_argument("--min-coverage-xy", type=float, default=0.55)
    p.add_argument("--max-coverage-xy", type=float, default=0.90)
    p.add_argument("--min-boundary-margin-m", type=float, default=0.35)

    p = sub.add_parser("inspect-render-feedback")
    p.add_argument("image_path")
    p.add_argument("--view-kind", choices=["perspective", "top_down", "asset_preview"], default="perspective")
    p.add_argument("--min-mean-luminance", type=float, default=None)
    p.add_argument("--max-shadow-fraction", type=float, default=None)
    p.add_argument("--min-subject-mean-luminance", type=float, default=None)
    p.add_argument("--max-subject-shadow-fraction", type=float, default=None)
    p.add_argument("--max-highlight-clip-fraction", type=float, default=None)

    p = sub.add_parser("inspect-reference-match")
    p.add_argument("reference_image")
    p.add_argument("rendered_image")

    p = sub.add_parser("evaluate-scene-quality")
    p.add_argument("reference_image")
    p.add_argument("rendered_image")
    p.add_argument("--scene-spec", default=None)
    p.add_argument("--summary", default=None)

    p = sub.add_parser("compose-scene-summary")
    p.add_argument("--manifest", default=None)
    p.add_argument("--scene-spec", default=None)
    p.add_argument("--compile-report", default=None)
    p.add_argument("--validate-scene-report", action="append", default=None)
    p.add_argument("--overlap-report", action="append", default=None)
    p.add_argument("--support-report", action="append", default=None)
    p.add_argument("--facing-report", action="append", default=None)
    p.add_argument("--render-feedback-report", action="append", default=None)
    p.add_argument(
        "--quality-policy",
        choices=["layout_only", "realistic", "reference_match"],
        default=None,
    )
    p.add_argument("--output", default=None)

    p = sub.add_parser("export-handoff")
    p.add_argument("state_json")
    p.add_argument("output_dir")

    p = sub.add_parser("verify-handoff")
    p.add_argument("manifest_path")

    args = parser.parse_args(argv)
    try:
        config = load_config(args.config, profile=args.profile)
        check_cli_outputs(args, config)
    except (ValueError, OSError) as exc:
        payload = envelope(args.command, {}, ok=False, status="fail",
                           errors=[issue("CONFIG_OR_OUTPUT_REJECTED", str(exc))])
        print(_json(payload))
        return 2
    evidence_before = None
    evidence_error = None
    if args.evidence_scene:
        try:
            if args.report_output and Path(args.report_output).exists():
                args.report_output = None
                raise ValueError("Use a new evidence report path; preserve existing inputs and reports")
            allowed_evidence_commands = {"validate-scene", "inspect-support-contact", "inspect-scene-overlaps", "audit-material-coverage", "inspect-articulation-joints",
                "validate-quality-agent-review", "validate-content-iteration-review", "validate-visual-agent-review"}
            if args.command not in allowed_evidence_commands:
                raise ValueError("--evidence-scene is only for scene/task gate validators")
            inspected_scene = getattr(args, "scene_path", None)
            if inspected_scene and Path(inspected_scene).resolve() != Path(args.evidence_scene).resolve():
                raise ValueError("Evidence subject must be the scene actually being inspected")
            evidence_before = subject_identity(args.evidence_scene)
        except (ValueError, OSError, ImportError, RuntimeError) as exc:
            evidence_error = str(exc)

    def emit(result: Any, *, exit_code: int = 0, status: str | None = None, warnings: list[dict[str, Any]] | None = None, errors: list[dict[str, Any]] | None = None, artifacts: list[dict[str, Any]] | None = None, next_actions: list[str] | None = None) -> int:
        if args.envelope:
            payload = envelope(
                args.command,
                result,
                ok=exit_code == 0,
                status=status,
                warnings=warnings,
                errors=errors,
                artifacts=artifacts,
                next_actions=next_actions,
            )
        else:
            payload = result
        if evidence_before is not None:
            try:
                evidence_after = subject_identity(args.evidence_scene)
            except (ValueError, OSError, ImportError, RuntimeError):
                evidence_after = None
            payload["subject"] = evidence_before
            if evidence_after != evidence_before:
                payload["status"] = "fail"
                payload["ok"] = False
                payload.setdefault("errors", []).append(issue("EVIDENCE_SCENE_CHANGED", "Candidate dependencies changed during validation", retryable=True))
                exit_code = 2
            if args.command in {"validate-quality-agent-review", "validate-content-iteration-review", "validate-visual-agent-review"}:
                from isaac_scene.usd_exchange import sha256
                evidence_files = {}
                for field in ("packet_path", "review_path"):
                    path = Path(str(result.get(field, "")))
                    if path.is_file():
                        evidence_files[str(path.resolve())] = sha256(path)
                        if field == "packet_path":
                            packet = _read_json(path)
                            for row in (packet.get("renders", []) + packet.get("candidates", [])):
                                image_path = Path(str(row.get("render_path", "")))
                                if image_path.is_file():
                                    evidence_files[str(image_path.resolve())] = sha256(image_path)
                payload["review_inputs_sha256"] = evidence_files
        rendered = _json(payload)
        if args.report_output:
            report_output = Path(args.report_output)
            report_output.parent.mkdir(parents=True, exist_ok=True)
            report_output.write_text(rendered + "\n", encoding="utf-8")
        print(rendered)
        return exit_code

    if evidence_error is not None:
        return emit({"status": "fail"}, exit_code=2, errors=[issue("EVIDENCE_SUBJECT_UNAVAILABLE", evidence_error, retryable=True)])
    if args.command in {"init-scene-task", "check-scene-task", "create-usd-revision", "prepare-usd-assets"}:
        try:
            if args.command == "init-scene-task":
                output = Path(args.output)
                if output.exists():
                    raise ValueError("Task output exists; preserve checkpoints and choose a new path")
                result = initialize_scene_task(text=args.text, images=args.image, scene=args.scene, blend=args.blend,
                    scope=args.scope, target=args.target, max_iterations=args.max_iterations)
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(_json(result) + "\n")
                return emit({"status": "pending", "task_path": str(output.resolve()), "task": result}, status="pending")
            if args.command == "check-scene-task":
                task_path = Path(args.task_json).resolve()
                result = check_scene_task(_read_json(task_path), base_dir=task_path.parent)
            elif args.command == "create-usd-revision":
                result = create_usd_revision(args.source, args.output, args.deactivate)
            else:
                result = prepare_usd_assets(args.source, args.output, args.prim)
            return emit(result, exit_code=2 if result.get("status") == "fail" else 0, status=result.get("status"))
        except (ValueError, OSError, ImportError, RuntimeError) as exc:
            return emit({"status": "fail"}, exit_code=2, status="fail",
                errors=[issue("SCENE_TASK_INPUT_OR_RUNTIME", str(exc), retryable=True)])

    if args.command == "capabilities":
        return emit(_read_json(SKILL_ROOT / "config" / "capabilities.json"))
    if args.command == "list-domains":
        return emit(discover_domains(SKILL_ROOT))
    if args.command == "list-room-shell-presets":
        return emit({
            "schema_version": "isaac-room-shell-preset-list/v1",
            "presets": list_room_shell_presets(),
        })
    if args.command == "list-architectural-shells":
        return emit({
            "schema_version": "isaac-architectural-shell-library/v1",
            "shells": list_architectural_shells(),
        })
    if args.command == "inspect-architectural-shell-spatial":
        try:
            result = inspect_shell_spatial(
                args.scene_path,
                background_prim=args.background_prim,
                floor_scope=args.floor_scope,
                door_scope=args.door_scope,
                window_scope=args.window_scope,
                maximum_floor_thickness_m=args.maximum_floor_thickness_m,
                minimum_floor_span_m=args.minimum_floor_span_m,
                include_components=args.include_components,
            )
        except ValueError as exc:
            return emit(
                {"status": "fail", "reason": str(exc)},
                exit_code=2,
                status="fail",
                errors=[issue("ARCHITECTURAL_SHELL_SPATIAL_AUDIT_FAILED", str(exc), retryable=True)],
            )
        return emit(
            result,
            status=str(result["status"]),
            warnings=[issue(
                "ARCHITECTURAL_SHELL_SPATIAL_PROFILE_PENDING",
                "No verified floor components were found for this shell.",
                retryable=True,
            )] if result["status"] == "pending" else [],
        )
    if args.command == "select-architectural-shell":
        result = select_architectural_shell(
            args.room_type,
            required_features=args.required_features,
            max_occupancy=args.max_occupancy,
        )
        return emit(
            result,
            status=str(result["status"]),
            warnings=[issue(
                "ARCHITECTURAL_SHELL_NOT_FOUND",
                "No verified architectural shell satisfies all requested features and occupancy constraints.",
                retryable=True,
                details={"rejected": result.get("rejected")},
            )] if result["status"] == "pending" else [],
        )
    if args.command == "apply-architectural-shell":
        spec = _read_json(Path(args.spec_json))
        if not spec:
            raise SystemExit(f"Invalid spec JSON: {args.spec_json}")
        try:
            result = apply_architectural_shell(
                spec,
                args.shell,
                config,
                replace_background=args.replace_background,
                replace_procedural_shell=args.replace_procedural_shell,
                preserve_existing_layout=args.preserve_existing_layout,
            )
        except ValueError as exc:
            return emit(
                {"status": "fail", "reason": str(exc)},
                exit_code=2,
                status="fail",
                errors=[issue("ARCHITECTURAL_SHELL_APPLY_FAILED", str(exc), retryable=True)],
            )
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(_json(result), encoding="utf-8")
        return emit(
            {"status": "pass", "output_spec": str(output), "architectural_shell": result["room"]["architectural_shell"]},
            status="pass",
            artifacts=[{"role": "scene_spec", "path": str(output)}],
            next_actions=[
                "run solve-scene-placement-batch for every invalidated entity and its support/functional-group closure",
                "then validate support coverage, localized shell clearance, lighting, and camera",
            ],
        )
    if args.command == "route-domain":
        return emit(route_domain(SKILL_ROOT, args.prompt))
    if args.command == "init-scene-brief":
        if not args.text and not args.reference_image:
            return emit(
                {"status": "fail"},
                exit_code=2,
                status="fail",
                errors=[issue("SCENE_BRIEF_INPUT_REQUIRED", "Provide --text or --reference-image.", retryable=True)],
            )
        domain = args.domain
        if not domain:
            route_prompt = args.text or args.room_type
            routed = route_domain(SKILL_ROOT, route_prompt)
            domain = str(routed.get("domain") or routed.get("selected_domain") or "generic_scene")
        brief = initialize_brief(
            text=args.text,
            reference_images=args.reference_image,
            domain=domain,
            room_type=args.room_type,
            room_size_m=args.room_size_m,
            quality_policy=args.quality_policy,
        )
        if args.room_shell_preset:
            brief["room"]["shell_preset"] = args.room_shell_preset
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(_json(brief) + "\n", encoding="utf-8")
        validation = validate_scene_brief(brief)
        return emit(
            {"brief": brief, "validation": validation},
            status=str(validation["status"]),
            artifacts=[{"kind": "scene_brief", "path": str(output)}],
            warnings=[
                issue("SCENE_BRIEF_REQUIRES_HARNESS_OBSERVATION", "Inspect the input and populate objects, evidence and camera before compiling.", retryable=True)
            ],
            next_actions=["have the harness populate observed/requested objects and refine low-confidence room/camera fields"],
        )
    if args.command == "plan-scene-enrichment":
        brief_path = Path(args.brief_json)
        if not brief_path.is_file():
            return emit(
                {"status": "fail", "path": str(brief_path)},
                exit_code=2,
                status="fail",
                errors=[issue("SCENE_BRIEF_NOT_FOUND", str(brief_path), retryable=True)],
            )
        try:
            plan, observation = plan_scene_enrichment(
                _read_json(brief_path),
                density=args.density,
                library_path=args.profiles,
                scene_kit_catalog_path=args.scene_kit_catalog or (config.get("semantic_catalog") or {}).get("scene_kit_catalog"),
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return emit(
                {"status": "fail", "path": str(brief_path), "reason": str(exc)},
                exit_code=2,
                status="fail",
                errors=[issue("ENRICHMENT_PLAN_FAILED", str(exc), retryable=True)],
            )
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(_json(plan) + "\n", encoding="utf-8")
        validation = observation["validation"]
        return emit(
            {**observation, "output": str(output), "plan": plan},
            exit_code=0 if validation.get("valid") else 2,
            status=str(observation["status"]),
            warnings=observation.get("warnings") or [],
            errors=[
                issue(
                    row.get("code", "ENRICHMENT_PLAN_INVALID"),
                    row.get("message", "Enrichment plan validation failed."),
                    retryable=True,
                    details={"path": row.get("path")},
                )
                for row in validation.get("errors") or []
            ],
            artifacts=[{"kind": "scene_enrichment_plan", "path": str(output)}],
            next_actions=[
                "let the external harness bind 3-5 verified asset candidates per active request",
                "compile a complete new Scene Brief/Spec version; do not accumulate coordinate deltas",
            ],
        )
    if args.command == "build-synthesis28-scene-kits":
        try:
            payload = build_scene_kit_catalog(
                args.object_catalog,
                environment_catalog=args.environment_catalog,
            )
        except (OSError, ValueError, sqlite3.Error, json.JSONDecodeError) as exc:
            return emit(
                {"status": "fail", "reason": str(exc)}, exit_code=2, status="fail",
                errors=[issue("SYNTHESIS28_SCENE_KIT_BUILD_FAILED", str(exc), retryable=True)],
            )
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(_json(payload) + "\n", encoding="utf-8")
        return emit(
            {"status": "pass", "scene_count": payload["scene_count"], "output": str(output)},
            status="pass", artifacts=[{"kind": "synthesis28_scene_kit_catalog", "path": str(output)}],
        )
    if args.command == "search-synthesis28-scene-kits":
        try:
            matches = search_scene_kit_catalog(
                load_scene_kit_catalog(args.catalog), args.query,
                domain=args.domain, room_type=args.room_type, topk=args.topk,
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return emit(
                {"status": "fail", "reason": str(exc)}, exit_code=2, status="fail",
                errors=[issue("SYNTHESIS28_SCENE_KIT_SEARCH_FAILED", str(exc), retryable=True)],
            )
        return emit({"status": "pass", "matches": matches}, status="pass")
    if args.command == "materialize-scene-enrichment":
        input_paths = [Path(args.brief_json), Path(args.plan_json), Path(args.bindings_json)]
        missing = [str(path) for path in input_paths if not path.is_file()]
        if missing:
            return emit(
                {"status": "fail", "missing": missing},
                exit_code=2,
                status="fail",
                errors=[issue("ENRICHMENT_INPUT_NOT_FOUND", ", ".join(missing), retryable=True)],
            )
        try:
            enriched, report = materialize_enrichment_bindings(*(_read_json(path) for path in input_paths))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return emit(
                {"status": "fail", "reason": str(exc)},
                exit_code=2,
                status="fail",
                errors=[issue("ENRICHMENT_MATERIALIZATION_FAILED", str(exc), retryable=True)],
            )
        output = Path(args.output)
        report_path = Path(args.report)
        output.parent.mkdir(parents=True, exist_ok=True)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(_json(enriched) + "\n", encoding="utf-8")
        report_path.write_text(_json(report) + "\n", encoding="utf-8")
        return emit(
            {**report, "output": str(output), "report": str(report_path)},
            status=str(report["status"]),
            warnings=[
                issue(row["code"], row["reason"], retryable=True, details={"request_id": row["request_id"]})
                for row in report.get("gaps") or []
            ],
            artifacts=[
                {"kind": "scene_brief", "path": str(output)},
                {"kind": "scene_enrichment_materialization_report", "path": str(report_path)},
            ],
            next_actions=[
                "apply the selected room-shell preset and compile one new pose-free Scene Spec",
                "derive and solve the support graph; do not hand-author coordinate deltas",
            ],
        )
    if args.command == "merge-enrichment-inventory":
        input_paths = [Path(args.base_inventory_json), Path(args.enriched_inventory_json), Path(args.materialization_report_json)]
        missing = [str(path) for path in input_paths if not path.is_file()]
        if missing:
            return emit(
                {"status": "fail", "missing": missing}, exit_code=2, status="fail",
                errors=[issue("ENRICHMENT_INVENTORY_INPUT_NOT_FOUND", ", ".join(missing), retryable=True)],
            )
        try:
            merged, report = merge_enrichment_inventory(*(_read_json(path) for path in input_paths))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return emit(
                {"status": "fail", "reason": str(exc)}, exit_code=2, status="fail",
                errors=[issue("ENRICHMENT_INVENTORY_MERGE_FAILED", str(exc), retryable=True)],
            )
        output, report_path = Path(args.output), Path(args.report)
        output.parent.mkdir(parents=True, exist_ok=True)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(_json(merged) + "\n", encoding="utf-8")
        report_path.write_text(_json(report) + "\n", encoding="utf-8")
        return emit(
            {**report, "output": str(output), "report": str(report_path)},
            status="pass",
            artifacts=[
                {"kind": "scene_spec", "path": str(output)},
                {"kind": "scene_enrichment_inventory_merge_report", "path": str(report_path)},
            ],
            next_actions=["derive a fresh semantic layout and solve the complete support graph"],
        )
    if args.command == "apply-room-shell-preset":
        input_path = Path(args.input_json)
        if not input_path.is_file():
            return emit(
                {"status": "fail", "path": str(input_path)},
                exit_code=2,
                status="fail",
                errors=[issue("ROOM_SHELL_INPUT_NOT_FOUND", str(input_path), retryable=True)],
            )
        source = _read_json(input_path)
        kind = args.input_kind
        if kind == "auto":
            kind = "scene-brief" if source.get("schema_version") == "isaac-scene-brief/v1" else "scene-spec"
        try:
            if kind == "scene-brief":
                source.setdefault("room", {})["shell_preset"] = args.preset
                if args.replace_lighting:
                    source["room"].setdefault("shell_preset_options", {})["replace_lighting"] = True
                result = apply_room_shell_preset(source, args.preset)
                validation = validate_scene_brief(result)
            else:
                room_size = args.room_size_m or ((source.get("room") or {}).get("size_m"))
                if not room_size:
                    raise ValueError("Scene Spec input requires --room-size-m when room.size_m is absent")
                shell_brief = {
                    "room": {
                        "type": str((source.get("room") or {}).get("type") or source.get("domain") or "room"),
                        "size_m": [float(value) for value in room_size],
                        "center_m": [float(value) for value in args.room_center_m],
                    }
                }
                expanded = apply_room_shell_preset(shell_brief, args.preset)
                preserve = set(args.preserve_collider_id)
                retained = [
                    row for row in source.get("support_colliders") or []
                    if str(row.get("id")) in preserve
                ]
                result = dict(source)
                result["support_colliders"] = build_room_shell(expanded["room"]) + retained
                result["room"] = {
                    "type": expanded["room"]["type"],
                    "size_m": expanded["room"]["size_m"],
                    "center_m": expanded["room"]["center_m"],
                    "shell_preset": args.preset,
                    "shell_preset_resolved": expanded["room"]["shell_preset_resolved"],
                }
                if args.replace_lighting:
                    result["lights"] = build_lighting_rig(expanded, expanded["room"])
                    result["lighting_policy"] = expanded.get("lighting") or {"mode": "default"}
                validation = {"status": "pass", "valid": True, "errors": [], "warnings": []}
        except (KeyError, OSError, ValueError, json.JSONDecodeError) as exc:
            return emit(
                {"status": "fail", "path": str(input_path)},
                exit_code=2,
                status="fail",
                errors=[issue("ROOM_SHELL_PRESET_APPLY_FAILED", str(exc), retryable=True)],
            )
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(_json(result) + "\n", encoding="utf-8")
        return emit(
            {"preset": args.preset, "input_kind": kind, "output": str(output), "validation": validation},
            exit_code=0 if validation.get("valid", True) else 2,
            status=str(validation.get("status") or "pass"),
            artifacts=[{"kind": kind, "path": str(output)}],
            warnings=validation.get("warnings") or [],
            errors=validation.get("errors") or [],
        )
    if args.command == "validate-scene-brief":
        brief_path = Path(args.brief_json)
        if not brief_path.is_file():
            return emit(
                {"status": "fail", "path": str(brief_path)},
                exit_code=2,
                status="fail",
                errors=[issue("SCENE_BRIEF_NOT_FOUND", str(brief_path), retryable=True)],
            )
        result = validate_scene_brief(_read_json(brief_path))
        return emit(
            result,
            exit_code=0 if result["valid"] else 2,
            status=str(result["status"]),
            warnings=result["warnings"],
            errors=result["errors"],
        )
    if args.command == "compile-scene-brief":
        brief_path = Path(args.brief_json)
        if not brief_path.is_file():
            return emit(
                {"status": "fail", "path": str(brief_path)},
                exit_code=2,
                status="fail",
                errors=[issue("SCENE_BRIEF_NOT_FOUND", str(brief_path), retryable=True)],
            )
        brief = _read_json(brief_path)
        try:
            scene_spec, observation = compile_to_scene_spec(
                brief,
                scene_output=args.scene_output,
            )
            scene_spec, shell_observation = resolve_architectural_shell(
                scene_spec,
                brief,
                config,
                shell=args.architectural_shell,
                required_features=args.required_shell_features,
                max_occupancy=args.max_shell_occupancy,
                keep_procedural_shell=args.keep_procedural_shell,
            )
            observation["object_entity_count"] = observation["entity_count"]
            observation["entity_count"] = len(scene_spec.get("entities") or [])
            observation["architectural_shell"] = shell_observation
            if shell_observation["status"] == "pending":
                observation["status"] = "warn"
        except ValueError as exc:
            return emit(
                {"status": "fail", "path": str(brief_path)},
                exit_code=2,
                status="fail",
                errors=[issue("SCENE_BRIEF_INVALID", str(exc), retryable=True)],
            )
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(_json(scene_spec) + "\n", encoding="utf-8")
        warnings = []
        next_actions = []
        if observation["unresolved_count"]:
            warnings.append(issue(
                "SCENE_BRIEF_COMPILE_UNRESOLVED",
                f"{observation['unresolved_count']} asset/layout/orientation items remain unresolved.",
                retryable=True,
            ))
            next_actions.append("resolve semantic assets, bbox-grounded transforms and verified front axes before make-scene")
        shell_observation = observation.get("architectural_shell") or {}
        if shell_observation.get("status") == "pending":
            warnings.append(issue(
                "ARCHITECTURAL_SHELL_NOT_FOUND",
                "No verified asset-backed shell satisfies the room type and requested features; the procedural room remains active.",
                retryable=True,
                details={"rejected": shell_observation.get("rejected")},
            ))
            next_actions.append("audit or register a sparse asset-backed shell with the required doors, windows, and lights")
        return emit(
            {"scene_spec": scene_spec, "observation": observation},
            status=str(observation["status"]),
            warnings=warnings,
            artifacts=[{"kind": "scene_spec", "path": str(output)}],
            next_actions=next_actions,
        )
    if args.command == "doctor":
        result = doctor_inspect(SKILL_ROOT, config)
        code = 2 if result.get("status") == "fail" else 0
        pending = [row.get("name") for row in result.get("checks", []) if row.get("status") == "pending"]
        next_actions = ["wait for official asset READY and POSTPROCESS_READY markers, then rerun doctor"] if "official_assets" in pending else []
        return emit(result, exit_code=code, status=str(result.get("status")), next_actions=next_actions)
    if args.command == "list-libraries":
        return emit(list_libraries(config, counts=args.counts, include_slow_counts=args.include_slow_counts))
    if args.command == "asset-inventory":
        result = asset_inventory(config, library=args.library)
        code = 2 if result.get("status") == "fail" else 0
        warnings = []
        next_actions = []
        if result.get("status") == "pending":
            warnings.append(issue("ASSET_INDEX_PENDING", str(result.get("reason")), retryable=True))
            next_actions.append("rebuild the asset index with schema v2, then retry")
        return emit(result, exit_code=code, status=str(result.get("status")), warnings=warnings, next_actions=next_actions)
    elif args.command == "search-assets":
        hits = search_assets(
            config,
            args.query,
            libraries=args.libraries,
            types=args.types,
            topk=args.topk,
            force_refresh=args.force_refresh,
            allow_slow=args.allow_slow,
            exhaustive=args.exhaustive,
        )
        if not args.envelope:
            return emit(hits)
        top_score = float(hits[0].get("_retrieval_score", 0.0)) if hits else 0.0
        confidence = "high" if top_score >= 0.75 else ("medium" if top_score >= 0.55 else ("low" if hits else "none"))
        warnings = []
        next_actions = []
        if not hits:
            warnings.append(issue("ASSET_NOT_FOUND", f"No asset matched: {args.query}", retryable=True))
            next_actions.append("broaden the query or enable a pending asset library")
        elif confidence == "low":
            warnings.append(issue("ASSET_MATCH_LOW_CONFIDENCE", f"Top score {top_score:.3f} is below the selection threshold", retryable=True))
            next_actions.append("do not auto-select; refine the query or wait for the intended library")
        return emit(
            {"query": args.query, "confidence": confidence, "top_score": top_score, "hits": hits},
            status="warn" if warnings else "pass",
            warnings=warnings,
            next_actions=next_actions,
        )
    elif args.command == "list-semantic-classes":
        configured = config.get("semantic_catalog", {}) if isinstance(config.get("semantic_catalog"), dict) else {}
        catalog_path = args.catalog or configured.get("path")
        if not catalog_path:
            return emit(
                {"status": "pending", "reason": "semantic_catalog_not_configured", "classes": []},
                status="pending",
                warnings=[issue("SEMANTIC_CATALOG_PENDING", "No semantic catalog path is configured.", retryable=True)],
                next_actions=["build the semantic asset catalog and configure semantic_catalog.path"],
            )
        result = semantic_taxonomy(
            catalog_path,
            prefix=args.prefix,
            callable_only=args.callable_only,
            limit=args.limit,
            read_only_adapters=configured.get("read_only_adapters", []),
        )
        status = "pass" if result.get("classes") else "warn"
        warnings = [] if result.get("classes") else [
            issue("SEMANTIC_CLASS_NOT_FOUND", f"No semantic classes matched prefix: {args.prefix or '*'}", retryable=True)
        ]
        return emit(
            result,
            status=status,
            warnings=warnings,
            next_actions=[] if result.get("classes") else ["broaden or remove --prefix"],
        )
    elif args.command == "search-semantic-assets":
        configured = config.get("semantic_catalog", {}) if isinstance(config.get("semantic_catalog"), dict) else {}
        catalog_path = args.catalog or configured.get("path")
        if not catalog_path:
            return emit(
                {"status": "pending", "reason": "semantic_catalog_not_configured", "hits": []},
                status="pending",
                warnings=[issue("SEMANTIC_CATALOG_PENDING", "No semantic catalog path is configured.", retryable=True)],
                next_actions=["build the semantic asset catalog and configure semantic_catalog.path"],
            )
        query_info = expand_query(SKILL_ROOT, args.query)
        # For Chinese/domain synonyms, use the first (most canonical) alias
        # from each matched concept as the lexical intent. The full expansion
        # remains visible for audit, but generic backup aliases must not make
        # an absent "popcorn machine" look like a refrigerator match merely
        # because both are kitchen appliances.
        primary_aliases = [
            str(values[0])
            for values in query_info["matched_synonyms"].values()
            if values
        ]
        original_ascii = " ".join(re.findall(r"[a-z0-9]+", args.query.lower()))
        retrieval_query = " ".join(
            part for part in (original_ascii, *primary_aliases) if part
        ) or str(query_info["expanded"])
        inferred_class = (
            None if args.broad or args.canonical_class
            else infer_retrieval_class(retrieval_query)
        )
        hits = search_semantic_catalog(
            catalog_path,
            retrieval_query,
            topk=args.topk,
            canonical_class=args.canonical_class,
            subtype=args.subtype,
            style_tags=args.style,
            material_tags=args.material,
            color_tags=args.color,
            min_size_m=args.min_size_m,
            max_size_m=args.max_size_m,
            callable_only=not args.include_unavailable,
            infer_class=not args.broad,
            read_only_adapters=configured.get("read_only_adapters", []),
        )
        unavailable = [row for row in hits if not row.get("callable")]
        warnings = []
        next_actions = []
        status = "pass"
        if not hits:
            status = "warn"
            warnings.append(issue("SEMANTIC_ASSET_NOT_FOUND", f"No semantic asset matched: {args.query}", retryable=True))
            next_actions.append("broaden the semantic class or build previews/metadata for additional source assets")
        elif unavailable:
            status = "warn"
            warnings.append(issue(
                "SEMANTIC_ASSET_METADATA_ONLY",
                f"{len(unavailable)} matches are metadata-only and cannot be inserted until downloaded and normalized.",
                retryable=True,
            ))
            next_actions.append("verify license, acquire the selected asset package, normalize USD dependencies, then mark it callable")
        return emit(
            {
                "query": args.query,
                "expanded_query": query_info["expanded"],
                "retrieval_query": retrieval_query,
                "canonical_class": args.canonical_class or inferred_class,
                "class_source": (
                    "explicit" if args.canonical_class
                    else ("inferred" if inferred_class else "unconstrained")
                ),
                "filters": {
                    "subtype": args.subtype,
                    "style": args.style,
                    "material": args.material,
                    "color": args.color,
                    "min_size_m": args.min_size_m,
                    "max_size_m": args.max_size_m,
                },
                "catalog_path": str(catalog_path),
                "hits": hits,
            },
            status=status,
            warnings=warnings,
            next_actions=next_actions,
        )
    elif args.command == "search-visual-assets":
        visual = config.get("visual_search", {}) if isinstance(config.get("visual_search"), dict) else {}
        semantic = config.get("semantic_catalog", {}) if isinstance(config.get("semantic_catalog"), dict) else {}
        python_path = visual.get("python")
        visual_index = visual.get("index_path")
        if not python_path or not visual_index:
            return emit(
                {"status": "pending", "reason": "visual_search_not_configured", "ranking": []},
                status="pending",
                warnings=[issue("VISUAL_SEARCH_PENDING", "Visual search runtime or index is not configured.", retryable=True)],
                next_actions=["build the preview embedding index and configure visual_search"],
            )
        command = [
            str(python_path),
            str(SKILL_ROOT / "scripts" / "search_asset_visual_index.py"),
            "--visual-index", str(visual_index),
            "--query", args.query,
            "--device", args.device,
            "--topk", str(args.topk),
        ]
        catalog_path = semantic.get("path")
        if catalog_path:
            command.extend(["--semantic-catalog", str(catalog_path)])
        if args.reference_crop:
            command.extend(["--reference-crop", args.reference_crop])
        if args.canonical_class:
            command.extend(["--canonical-class", args.canonical_class])
        for fallback_class in args.fallback_class:
            command.extend(["--fallback-class", fallback_class])
        if args.min_size_m:
            command.extend(["--min-size-m", *(str(value) for value in args.min_size_m)])
        if args.max_size_m:
            command.extend(["--max-size-m", *(str(value) for value in args.max_size_m)])
        if args.target_size_m:
            command.extend(["--target-size-m", *(str(value) for value in args.target_size_m)])
            command.extend(["--size-weight", str(args.size_weight)])
        if args.include_unavailable:
            command.append("--include-unavailable")
        environment = os.environ.copy()
        environment.update({
            str(key): str(value)
            for key, value in (visual.get("env") or {}).items()
        })
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                env=environment,
                timeout=float(visual.get("timeout_seconds", 300)),
            )
        except subprocess.TimeoutExpired as exc:
            return emit(
                {"status": "fail", "timeout_seconds": exc.timeout},
                exit_code=2,
                status="fail",
                errors=[issue("VISUAL_SEARCH_TIMEOUT", f"Visual search exceeded {exc.timeout}s.", retryable=True)],
                next_actions=["retry on CUDA or reduce the preview candidate class"],
            )
        if completed.returncode != 0:
            return emit(
                {
                    "status": "fail",
                    "returncode": completed.returncode,
                    "stderr": completed.stderr[-4000:],
                },
                exit_code=2,
                status="fail",
                errors=[issue("VISUAL_SEARCH_FAILED", completed.stderr[-1000:] or "visual search failed", retryable=True)],
            )
        result = json.loads(completed.stdout.strip().splitlines()[-1])
        ranking = result.get("ranking", [])
        minimum_score = float(visual.get("minimum_score", 0.24))
        minimum_margin = float(visual.get("minimum_top_margin", 0.015))
        minimum_candidates = int(visual.get("minimum_candidate_count", 20))
        minimum_exhausted_candidates = int(visual.get("minimum_exhausted_candidate_count", 10))
        warnings = []
        next_actions = []
        if not ranking:
            warnings.append(issue("VISUAL_ASSET_NOT_FOUND", "No previewed asset passed the semantic filters.", retryable=True))
            next_actions.append("render previews for more semantic candidates")
        else:
            top_score = float(ranking[0].get("score", 0.0))
            margin = top_score - float(ranking[1].get("score", 0.0)) if len(ranking) > 1 else 0.0
            candidate_count = int(result.get("candidate_count", len(ranking)))
            candidate_pool_count = int(result.get("candidate_pool_count", candidate_count))
            candidate_inventory_count = result.get("candidate_inventory_count")
            coverage = candidate_coverage(
                preview_count=candidate_pool_count,
                inventory_count=candidate_inventory_count,
                minimum_candidates=minimum_candidates,
                minimum_exhausted_candidates=minimum_exhausted_candidates,
            )
            top_group = ranking[0].get("equivalence_group")
            runner_up_group = ranking[1].get("equivalence_group") if len(ranking) > 1 else None
            equivalent_top_tie = bool(top_group and top_group == runner_up_group)
            result["selection_gate"] = {
                "pass": (
                    top_score >= minimum_score
                    and (margin >= minimum_margin or equivalent_top_tie)
                    and coverage["pass"]
                ),
                "top_score": top_score,
                "top_margin": round(margin, 6),
                "candidate_count": candidate_count,
                "candidate_pool_count": candidate_pool_count,
                "candidate_inventory_count": coverage["candidate_inventory_count"],
                "candidate_inventory_exhausted": coverage["inventory_exhausted"],
                "candidate_coverage_pass": coverage["pass"],
                "equivalent_top_tie": equivalent_top_tie,
                "equivalence_group": top_group if equivalent_top_tie else None,
                "thresholds": {
                    "minimum_score": minimum_score,
                    "minimum_top_margin": minimum_margin,
                    "minimum_candidate_count": minimum_candidates,
                    "minimum_exhausted_candidate_count": minimum_exhausted_candidates,
                },
            }
            if top_score < minimum_score:
                warnings.append(issue(
                    "VISUAL_ASSET_LOW_CONFIDENCE",
                    f"Top visual score {top_score:.3f} is below {minimum_score}.",
                    retryable=True,
                ))
            if margin < minimum_margin and not equivalent_top_tie:
                warnings.append(issue(
                    "VISUAL_ASSET_AMBIGUOUS",
                    f"Top-two margin {margin:.3f} is below {minimum_margin}.",
                    retryable=True,
                ))
            if not coverage["pass"]:
                warnings.append(issue(
                    "VISUAL_CANDIDATE_POOL_TOO_SMALL",
                    (
                        f"Only {candidate_pool_count} previewed class candidates are available; "
                        f"require {minimum_candidates}, or the entire callable inventory with at "
                        f"least {minimum_exhausted_candidates} candidates."
                    ),
                    retryable=True,
                ))
            if warnings:
                next_actions.append("do not auto-select; render a larger, source-diverse candidate pool or acquire a closer asset")
        return emit(
            result,
            status="warn" if warnings else "pass",
            warnings=warnings,
            next_actions=next_actions,
        )
    elif args.command == "annotate-semantic-asset":
        configured = config.get("semantic_catalog", {}) if isinstance(config.get("semantic_catalog"), dict) else {}
        catalog_path = args.catalog or configured.get("path")
        if not catalog_path:
            return emit(
                {"status": "pending", "reason": "semantic_catalog_not_configured"},
                status="pending",
                warnings=[issue("SEMANTIC_CATALOG_PENDING", "No semantic catalog path is configured.", retryable=True)],
            )
        bbox = json.loads(args.bbox_json) if args.bbox_json else None
        orientation_metadata = {
            key: value
            for key, value in {
                "orientation_mode": args.orientation_mode,
                "primary_axis": args.primary_axis,
                "operator_axis": args.operator_axis,
                "flow_axis": args.flow_axis,
                "equivalence_group": args.equivalence_group,
            }.items()
            if value is not None
        }
        try:
            result = annotate_semantic_catalog(
                catalog_path,
                logical_id=args.logical_id,
                source_path=args.source_path,
                canonical_class=args.canonical_class,
                subtype=args.subtype,
                semantic_name=args.semantic_name,
                preview_index_path=args.preview_index,
                front_axis=args.front_axis,
                meters_per_unit=args.meters_per_unit,
                bbox=bbox,
                style_tags=args.style,
                material_tags=args.material,
                color_tags=args.color,
                metadata_patch={
                    "annotation_source": "agent_verified",
                    **orientation_metadata,
                },
            )
        except (FileNotFoundError, KeyError, ValueError) as exc:
            return emit(
                {"status": "fail", "selector": args.logical_id or args.source_path},
                exit_code=2,
                status="fail",
                errors=[issue("SEMANTIC_ASSET_ANNOTATION_FAILED", str(exc), retryable=True)],
            )
        return emit(result, status="pass")
    elif args.command == "set-semantic-asset-state":
        configured = config.get("semantic_catalog", {}) if isinstance(config.get("semantic_catalog"), dict) else {}
        catalog_path = args.catalog or configured.get("path")
        if not catalog_path:
            return emit(
                {"status": "pending", "reason": "semantic_catalog_not_configured"},
                status="pending",
                warnings=[issue("SEMANTIC_CATALOG_PENDING", "No semantic catalog path is configured.", retryable=True)],
            )
        try:
            result = set_semantic_asset_state(
                catalog_path,
                state=args.state,
                reason=args.reason,
                logical_id=args.logical_id,
                source_path=args.source_path,
                evidence_path=args.evidence,
            )
        except (FileNotFoundError, KeyError, ValueError) as exc:
            return emit(
                {"status": "fail", "selector": args.logical_id or args.source_path},
                exit_code=2,
                status="fail",
                errors=[issue("SEMANTIC_ASSET_STATE_UPDATE_FAILED", str(exc), retryable=True)],
            )
        return emit(
            result,
            status="warn" if args.state == "quarantine" else "pass",
            warnings=[
                issue(
                    "SEMANTIC_ASSET_QUARANTINED",
                    f"{result['logical_id']} is excluded from automatic retrieval: {args.reason}",
                    retryable=True,
                )
            ] if args.state == "quarantine" else [],
            next_actions=[
                "repair or replace the source package, rerender previews, then restore only after validation passes"
            ] if args.state == "quarantine" else [],
        )
    elif args.command == "get-fabrica-recipe":
        record = get_fabrica_recipe(config, args.assembly)
        return emit(
            record or {"status": "not_found", "assembly": args.assembly},
            exit_code=0 if record else 2,
            errors=None if record else [issue("FABRICA_RECIPE_NOT_FOUND", f"No recipe found for {args.assembly}", retryable=True)],
        )
    elif args.command == "list-home-backgrounds":
        return emit(list_home_backgrounds(config, room_type=args.room_type, with_health=args.with_health))
    elif args.command == "inspect-asset-health":
        result = inspect_asset_health(args.asset_path)
        code = 0 if result.get("status") in {"pass", "warn"} else 2
        return emit(result, exit_code=code, status=str(result.get("status")))
    elif args.command == "inspect-asset-local-geometry":
        try:
            result = inspect_asset_local_geometry(
                args.asset_path,
                root_prim_path=args.root_prim,
            )
        except (FileNotFoundError, ValueError, RuntimeError) as exc:
            return emit(
                {"status": "fail", "asset_path": args.asset_path},
                exit_code=2,
                status="fail",
                errors=[issue("ASSET_LOCAL_GEOMETRY_FAILED", str(exc), retryable=True)],
            )
        entrypoint = result.get("entrypoint_compatibility") or {}
        if result.get("status") == "fail":
            return emit(
                result,
                exit_code=2,
                status="fail",
                errors=[issue(
                    str(entrypoint.get("code") or "ASSET_ENTRYPOINT_INCOMPATIBLE"),
                    str(entrypoint.get("summary") or "asset entrypoint is incompatible"),
                    retryable=True,
                    details=entrypoint,
                )],
                next_actions=[f"use the package alignment wrapper: {entrypoint.get('recommended_path')}"] if entrypoint.get("recommended_path") else [],
            )
        return emit(result, status="pass")
    elif args.command == "inspect-asset-units":
        result = inspect_asset_units(args.asset_path, target_meters_per_unit=args.target_meters_per_unit)
        code = 0 if result.get("status") in {"pass", "warn"} else 2
        return emit(
            result,
            exit_code=code,
            status=str(result.get("status")),
            warnings=result.get("warnings"),
            errors=result.get("errors"),
        )
    elif args.command == "make-scene":
        spec = _read_json(Path(args.spec_json))
        if not spec:
            raise SystemExit(f"Invalid spec JSON: {args.spec_json}")
        try:
            result = make_scene(config, spec, output=args.output)
        except AssetEntrypointMismatch as exc:
            report = exc.report
            return emit(
                {"status": "fail", "asset_entrypoint": report},
                exit_code=2,
                status="fail",
                errors=[issue(
                    str(report.get("code") or "ASSET_ENTRYPOINT_INCOMPATIBLE"),
                    str(report.get("summary") or exc),
                    retryable=True,
                    details=report,
                )],
                next_actions=[f"replace the raw layer with {report.get('recommended_path')}"] if report.get("recommended_path") else [],
            )
        except UnitScaleMismatch as exc:
            return emit(
                {"status": "fail", "reason": str(exc)},
                exit_code=2,
                status="fail",
                errors=[issue("UNIT_SCALE_MISMATCH", str(exc), retryable=True)],
                next_actions=["inspect the asset with inspect-asset-units, then remove scale, use unit_mode=multiply, or explicitly waive the mismatch"],
            )
        except (ValueError, OSError) as exc:
            return emit({"status": "fail"}, exit_code=2, status="fail",
                        errors=[issue("SCENE_OUTPUT_OR_INPUT_REJECTED", str(exc), retryable=True)],
                        next_actions=["preserve existing artifacts and choose a valid new revision path"])
        unresolved_relations = ((result.get("scene_spec") or {}).get("unresolved_relations") or []) if isinstance(result.get("scene_spec"), dict) else []
        warnings = []
        next_actions = []
        if unresolved_relations:
            warnings.append(issue(
                "LAYOUT_RELATIONS_UNRESOLVED",
                f"{len(unresolved_relations)} semantic relations still require harness planning or a layout solver",
                retryable=True,
                details={"relations": unresolved_relations},
            ))
            next_actions.append("resolve relation transforms in the external harness before final validation")
        unit_references = ((result.get("unit_audit") or {}).get("references") or []) if isinstance(result.get("unit_audit"), dict) else []
        unit_warnings = [warning for row in unit_references if isinstance(row, dict) for warning in row.get("warnings", []) if isinstance(warning, dict)]
        for warning in unit_warnings:
            warnings.append(issue(
                str(warning.get("code") or "UNIT_AUDIT_WARNING"),
                str(warning.get("summary") or "Reference unit audit warning"),
                retryable=True,
            ))
        if unit_warnings:
            next_actions.append("inspect source metersPerUnit and bbox before accepting references with unknown or waived unit conversion")
        return emit(
            result,
            status="warn" if warnings else "pass",
            warnings=warnings,
            next_actions=next_actions,
            artifacts=[{"role": "scene", "path": result.get("scene_path")}, {"role": "scene_spec", "path": result.get("sidecar_spec")}],
        )
    elif args.command in {"inspect-articulation-joints", "pose-articulation", "initialize-articulation"}:
        try:
            if args.command == "inspect-articulation-joints":
                result = inspect_joints(args.scene_path, roots=args.root, tolerance_m=args.tolerance_m)
            elif args.command == "initialize-articulation":
                result = initialize_articulation(args.scene_path, root=args.root,
                    position_source=args.position_source, output=args.output)
            else:
                result = pose_joints(args.scene_path, args.output, root=args.root, positions=_read_json(Path(args.positions)))
        except (ValueError, RuntimeError, OSError) as exc:
            return emit({"status": "fail"}, status="fail", exit_code=2,
                        errors=[issue("ARTICULATION_CONTRACT_ERROR", str(exc), retryable=True)])
        return emit(result, status=result["status"], exit_code=2 if result["status"] == "fail" else 0,
                    errors=result.get("errors", []))
    elif args.command == "validate-scene":
        result = validate_scene(args.scene_path)
        code = 0 if result.get("status") in {"pass", "warn"} else 2
        return emit(result, exit_code=code, status=str(result.get("status")), warnings=result.get("warnings"), errors=result.get("errors"))
    elif args.command == "inspect-bounds":
        return emit(inspect_bounds(
            args.scene_path,
            prim_paths=args.prim_paths,
            root_path=args.root,
            floor_z=args.floor_z,
            tolerance=args.tolerance,
        ))
    elif args.command == "inspect-scene-overlaps":
        result = inspect_scene_overlaps(
            args.scene_path,
            object_prim_paths=args.object_prim_paths,
            objects_root_path=args.objects_root,
            background_prim_path=args.background_prim,
            penetration_tolerance=args.penetration_tolerance,
            max_background_span_m=args.max_background_span_m,
            volumetric_aggregate_min_xy_m=args.volumetric_aggregate_min_xy_m,
            volumetric_aggregate_min_z_m=args.volumetric_aggregate_min_z_m,
            ignore_background_prim_paths=args.ignore_background_prim_paths,
            max_reported_overlaps=args.max_reported_overlaps,
        )
        errors = [
            issue(
                str(row.get("kind") or "SCENE_SPATIAL_VALIDATION_FAILED").upper(),
                str(row.get("suggestion") or row.get("kind") or "Scene spatial validation failed"),
                retryable=True,
                details=row,
            )
            for row in result.get("errors", [])
        ]
        warnings = [
            issue(
                str(row.get("kind") or "SCENE_SPATIAL_VALIDATION_WARNING").upper(),
                str(row.get("note") or row.get("kind") or "Scene spatial validation warning"),
                retryable=True,
                details=row,
            )
            for row in result.get("warnings", [])
        ]
        code = 2 if result.get("status") == "fail" else 0
        return emit(
            result,
            exit_code=code,
            status=str(result.get("status")),
            warnings=warnings,
            errors=errors,
            next_actions=["move the workcell to a background-clear region and rerun overlap and support checks"] if errors else [],
        )
    elif args.command == "inspect-support-contact":
        parsed_pairs: list[tuple[str, str]] = []
        invalid_pairs: list[str] = []
        for item in args.prim_pairs:
            if "=" not in item:
                invalid_pairs.append(item)
                continue
            object_path, support_path = item.split("=", 1)
            if not object_path.startswith("/") or not support_path.startswith("/"):
                invalid_pairs.append(item)
                continue
            parsed_pairs.append((object_path, support_path))
        if invalid_pairs:
            return emit(
                {"status": "fail", "invalid_pairs": invalid_pairs},
                exit_code=2,
                status="fail",
                errors=[issue(
                    "INVALID_SUPPORT_PAIR",
                    "Support pairs must use OBJECT_PRIM=SUPPORT_PRIM with absolute prim paths",
                    retryable=True,
                    details={"invalid_pairs": invalid_pairs},
                )],
            )
        if args.contact_mode == "solid":
            from isaac_scene.contacts import inspect_solid_contacts
            result = inspect_solid_contacts(args.scene_path, parsed_pairs, tolerance_m=args.tolerance,
                minimum_coverage=args.minimum_mesh_coverage, pitch_m=args.sample_pitch_m,
                max_samples=args.max_contact_samples)
        else:
            result = inspect_support_contacts(
                args.scene_path, prim_pairs=parsed_pairs, tolerance=args.tolerance,
                minimum_xy_overlap=args.minimum_xy_overlap, mesh_aware=args.mesh_aware,
                minimum_mesh_coverage=args.minimum_mesh_coverage)
        errors = [
            issue(
                str(row.get("kind") or "SUPPORT_CONTACT_FAILED").upper(),
                str(row.get("kind") or "Support contact validation failed"),
                retryable=True,
                details=row,
            )
            for row in result.get("errors", [])
        ]
        warnings = [
            issue(
                str(row.get("kind") or "SUPPORT_CONTACT_WARNING").upper(),
                str(row.get("kind") or "Support contact validation warning"),
                retryable=True,
                details=row,
            )
            for row in result.get("warnings", [])
        ]
        code = 2 if result.get("status") == "fail" else 0
        return emit(
            result,
            exit_code=code,
            status=str(result.get("status")),
            warnings=warnings,
            errors=errors,
            next_actions=["adjust the object transform or its designated support, then rerun the contact check"] if errors else [],
        )
    elif args.command == "derive-constraint-layout-spec":
        exact_spec = _read_json(Path(args.exact_spec_json))
        if not exact_spec:
            raise SystemExit(f"Invalid spec JSON: {args.exact_spec_json}")
        try:
            derived_spec, result = derive_constraint_layout_spec(
                exact_spec,
                zone_columns=args.zone_columns,
                zone_rows=args.zone_rows,
                yaw_step_deg=args.yaw_step_deg,
                room_margin_m=args.room_margin_m,
            )
        except (ValueError, TypeError) as exc:
            return emit(
                {"status": "fail", "reason": str(exc)},
                exit_code=2,
                status="fail",
                errors=[issue("CONSTRAINT_LAYOUT_DERIVATION_FAILED", str(exc), retryable=True)],
            )
        output_spec = Path(args.output_spec).resolve()
        report_path = Path(args.report).resolve()
        output_spec.parent.mkdir(parents=True, exist_ok=True)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        output_spec.write_text(_json(derived_spec), encoding="utf-8")
        report_path.write_text(_json(result), encoding="utf-8")
        return emit(
            {**result, "output_spec": str(output_spec), "report": str(report_path)},
            exit_code=2 if result.get("status") == "fail" else 0,
            status=str(result.get("status")),
            errors=[
                issue(
                    "CONSTRAINT_LAYOUT_PRIVILEGED_FIELD_LEAK",
                    "The derived spec still contains exact-pose regression fields.",
                    retryable=False,
                    details={"fields": result.get("forbidden_fields_present")},
                )
            ] if result.get("forbidden_fields_present") else [],
            warnings=[
                issue(
                    "CONSTRAINT_LAYOUT_ENTITY_PENDING",
                    f"{result.get('pending_entity_count')} entities lack verified support or mount relations.",
                    retryable=True,
                    details={"pending_entity_count": result.get("pending_entity_count")},
                )
            ] if result.get("pending_entity_count") else [],
            artifacts=[
                {"role": "constraint_layout_spec", "path": str(output_spec)},
                {"role": "constraint_layout_compile_report", "path": str(report_path)},
            ],
            next_actions=["compose the constraint spec, then run solve-scene-placement-batch"],
        )
    elif args.command == "derive-semantic-layout-spec":
        inventory_spec = _read_json(Path(args.inventory_spec_json))
        brief = _read_json(Path(args.scene_brief_json))
        if not inventory_spec:
            raise SystemExit(f"Invalid inventory spec JSON: {args.inventory_spec_json}")
        if not brief:
            raise SystemExit(f"Invalid Scene Brief JSON: {args.scene_brief_json}")
        validation = validate_scene_brief(brief)
        if not validation.get("valid"):
            return emit(
                {"status": "fail", "brief_validation": validation},
                exit_code=2,
                status="fail",
                errors=[issue("SEMANTIC_LAYOUT_BRIEF_INVALID", "Scene Brief validation failed", retryable=True, details=validation)],
            )
        domain_name = str(brief.get("domain") or "generic_scene")
        domain = next(
            (row for row in discover_domains(SKILL_ROOT) if str(row.get("name")) == domain_name),
            None,
        )
        if domain is None:
            return emit(
                {"status": "fail", "domain": domain_name},
                exit_code=2,
                status="fail",
                errors=[issue("SEMANTIC_LAYOUT_DOMAIN_UNKNOWN", f"Unknown domain pack: {domain_name}", retryable=True)],
            )
        try:
            derived_spec, result = derive_semantic_layout_spec(
                inventory_spec,
                brief,
                layout_profile=domain.get("layout_profile") or {},
                room_margin_m=args.room_margin_m,
            )
        except (ValueError, TypeError) as exc:
            return emit(
                {"status": "fail", "reason": str(exc)},
                exit_code=2,
                status="fail",
                errors=[issue("SEMANTIC_LAYOUT_DERIVATION_FAILED", str(exc), retryable=True)],
            )
        output_spec = Path(args.output_spec).resolve()
        report_path = Path(args.report).resolve()
        output_spec.parent.mkdir(parents=True, exist_ok=True)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        output_spec.write_text(_json(derived_spec), encoding="utf-8")
        report_path.write_text(_json(result), encoding="utf-8")
        return emit(
            {**result, "output_spec": str(output_spec), "report": str(report_path)},
            exit_code=2 if result.get("status") == "fail" else 0,
            status=str(result.get("status")),
            errors=[
                issue(
                    "SEMANTIC_LAYOUT_PRIVILEGED_FIELD_LEAK",
                    "The semantic layout output contains exact-pose regression fields.",
                    retryable=False,
                    details={"fields": result.get("forbidden_fields_present")},
                )
            ] if result.get("forbidden_fields_present") else [],
            warnings=[
                issue(
                    "SEMANTIC_LAYOUT_ENTITY_PENDING",
                    f"{result.get('pending_entity_count')} entities remain outside the active support/mount graph.",
                    retryable=True,
                    details={"pending_entity_count": result.get("pending_entity_count")},
                )
            ] if result.get("pending_entity_count") else [],
            artifacts=[
                {"role": "semantic_layout_spec", "path": str(output_spec)},
                {"role": "semantic_layout_compile_report", "path": str(report_path)},
            ],
            next_actions=["compose the semantic layout spec, then run solve-scene-placement-batch"],
        )
    elif args.command == "solve-support-placement":
        result = solve_support_placement(
            args.scene_path,
            object_prim_path=args.object_prim_path,
            support_prim_path=args.support_prim_path,
            obstacle_prim_paths=args.obstacle_prim_paths,
            architectural_obstacle_root_path=args.architectural_obstacle_root,
            architectural_path_tokens=args.architectural_path_tokens,
            group_member_prim_paths=args.group_member_prim_paths,
            facing_target_prim_path=args.facing_target_prim_path,
            front_axis=args.front_axis,
            facing_target_mode=args.target_mode,
            preferred_xy=args.preferred_xy,
            yaw_deltas_deg=args.yaw_deltas_deg,
            tolerance=args.tolerance,
            minimum_mesh_coverage=args.minimum_mesh_coverage,
            support_margin=args.support_margin,
            obstacle_clearance=args.obstacle_clearance,
            architectural_max_span_m=args.architectural_max_span_m,
            clearance_z=args.clearance_z,
            grid_step=args.grid_step,
            max_candidates=args.max_candidates,
            support_surface_z_m=args.support_surface_z_m,
        )
        errors = [
            issue(
                str(row.get("kind") or "SUPPORT_PLACEMENT_FAILED").upper(),
                str(row.get("message") or row.get("kind") or "No valid support placement was found"),
                retryable=True,
                details=row,
            )
            for row in result.get("errors", [])
        ]
        warnings = [
            issue(
                str(row.get("kind") or "SUPPORT_PLACEMENT_WARNING").upper(),
                str(row.get("message") or row.get("kind") or "Support placement warning"),
                retryable=True,
                details=row,
            )
            for row in result.get("warnings", [])
        ]
        code = 2 if result.get("status") == "fail" else 0
        return emit(
            result,
            exit_code=code,
            status=str(result.get("status")),
            warnings=warnings,
            errors=errors,
            next_actions=[
                "apply suggested_local_translation_delta and recommended_yaw_delta_deg to the Scene Spec, regenerate, then rerun support and facing checks"
            ] if not errors else [
                "relax only documented margins, remove false obstacle prims, or choose another support asset; do not bypass the mesh gate"
            ],
        )
    elif args.command == "solve-scene-placement-batch":
        spec = _read_json(Path(args.spec_json))
        if not spec:
            raise SystemExit(f"Invalid spec JSON: {args.spec_json}")
        try:
            result = solve_scene_placement_batch(
                config,
                spec,
                source_spec_path=args.spec_json,
                scene_path=args.scene_path,
                output_spec_path=args.output_spec,
                output_scene_path=args.output_scene,
                entity_ids=args.entity_ids,
                strict=args.strict,
                tolerance=args.tolerance,
                minimum_mesh_coverage=args.minimum_mesh_coverage,
                support_margin=args.support_margin,
                obstacle_clearance=args.obstacle_clearance,
                use_architectural_obstacles=not args.no_architectural_obstacles,
                architectural_path_tokens=args.architectural_path_tokens,
                architectural_max_span_m=args.architectural_max_span_m,
                clearance_z=args.clearance_z,
                grid_step=args.grid_step,
                max_candidates=args.max_candidates,
                quarantine_failed=not args.keep_failed_entities,
            )
        except (UnitScaleMismatch, ValueError) as exc:
            return emit(
                {"status": "fail", "reason": str(exc)},
                exit_code=2,
                status="fail",
                errors=[issue("BATCH_PLACEMENT_FAILED", str(exc), retryable=True)],
            )
        return emit(
            result,
            exit_code=2 if result.get("status") == "fail" else 0,
            status=str(result.get("status")),
            warnings=result.get("warnings"),
            errors=[
                issue(
                    str(row.get("kind") or "BATCH_PLACEMENT_FAILED").upper(),
                    f"Batch placement failed for {row.get('entity_id') or 'the support graph'}",
                    retryable=True,
                    details=row,
                )
                for row in result.get("errors", [])
            ],
            artifacts=[
                {"role": "scene_spec", "path": result.get("output_spec")},
                {"role": "scene", "path": result.get("output_scene")},
            ],
            next_actions=[
                "run mesh-aware support, overlap, and facing validation on the generated scene"
            ] if result.get("status") == "pass" else [
                "inspect the failed graph node or placement and correct its target contract"
            ],
        )
    elif args.command == "extract-synthesis-support-relations":
        result = extract_synthesis28_support_relations(
            args.scenes_root,
            args.instances_db,
            args.output,
            vertical_tolerance_m=args.vertical_tolerance,
            floor_tolerance_m=args.floor_tolerance,
            minimum_xy_coverage=args.minimum_xy_coverage,
        )
        return emit(
            result,
            status=str(result.get("status")),
            warnings=[issue(
                "SUPPORT_RELATIONS_REQUIRE_REVIEW",
                "Some instances have no unambiguous AABB support candidate; these remain explicit pending records.",
                retryable=True,
                details={"regression_counts": result.get("regression_counts")},
            )] if result.get("status") == "warn" else [],
            artifacts=[{"role": "support_relations", "path": result.get("output")}],
            next_actions=[
                "review pending and low-confidence relations, then promote verified support targets into regression Scene Specs"
            ],
        )
    elif args.command == "refine-synthesis-support-relations":
        result = refine_synthesis28_mesh_support_relations(
            args.baseline_report,
            args.output,
            args.surface_cache,
            scene_slugs=args.scene_slugs,
            vertical_tolerance_m=args.vertical_tolerance,
            minimum_mesh_coverage=args.minimum_mesh_coverage,
            planarity_tolerance_m=args.planarity_tolerance,
            contact_tolerance_m=args.contact_tolerance,
            cell_size_m=args.cell_size,
            z_step_m=args.z_step,
            floor_plane_contact_tolerance_m=args.floor_plane_contact_tolerance,
        )
        return emit(
            result,
            exit_code=2 if result.get("status") == "fail" else 0,
            status=str(result.get("status")),
            warnings=[issue(
                "MESH_SUPPORT_RELATIONS_REQUIRE_REVIEW",
                "Mesh-unverified wall, ceiling, contained, or missing-contact cases remain pending.",
                retryable=True,
                details={"regression_counts": result.get("regression_counts")},
            )] if result.get("status") == "warn" else [],
            errors=[issue(
                "MESH_SUPPORT_SCENE_MISSING",
                "One or more requested baseline scenes were not found.",
                retryable=True,
                details={"missing_scenes": result.get("missing_scenes")},
            )] if result.get("status") == "fail" else [],
            artifacts=[
                {"role": "mesh_support_relations", "path": result.get("output")},
                {"role": "surface_cache", "path": result.get("surface_cache")},
            ],
            next_actions=["review remaining pending mount/containment cases and promote only exact mesh targets"],
        )
    elif args.command == "export-synthesis28-reconstruction-specs":
        try:
            result = export_reconstruction_specs(
                args.catalog,
                args.support_relations,
                args.output_dir,
                scene_slugs=args.scene_slugs or None,
            )
        except (OSError, ValueError, json.JSONDecodeError, sqlite3.Error) as exc:
            return emit(
                {"status": "fail", "reason": str(exc)},
                exit_code=2,
                status="fail",
                errors=[issue("SYNTHESIS28_RECONSTRUCTION_EXPORT_FAILED", str(exc), retryable=True)],
            )
        return emit(
            result,
            status=str(result.get("status")),
            warnings=[issue(
                "SYNTHESIS28_RECONSTRUCTION_FIXTURES_ONLY",
                "Non-callable assets remain marked regression_only and are not promoted into general retrieval.",
                retryable=False,
                details={"missing_entity_count": result.get("missing_entity_count")},
            )],
            artifacts=[
                {"role": "reconstruction_index", "path": str(Path(result["output_dir"]) / "index.json")},
            ],
            next_actions=[
                "compose all 28 specs and compare object count, bounds, support contacts, materials, cameras, and fixed-view renders"
            ],
        )
    elif args.command == "evaluate-synthesis28-reconstruction":
        try:
            result = evaluate_reconstruction_scene(
                args.spec_json,
                args.scene_path,
                matrix_tolerance=args.matrix_tolerance,
                bounds_tolerance_m=args.bounds_tolerance_m,
                max_failures=args.max_failures,
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return emit(
                {"status": "fail", "reason": str(exc)},
                exit_code=2,
                status="fail",
                errors=[issue("SYNTHESIS28_RECONSTRUCTION_EVALUATION_FAILED", str(exc), retryable=True)],
            )
        return emit(
            result,
            exit_code=2 if result.get("status") == "fail" else 0,
            status=str(result.get("status")),
            errors=[issue(
                "SYNTHESIS28_RECONSTRUCTION_PARITY_FAILED",
                "One or more reconstructed instance transforms or world bounds differ from the source scene.",
                retryable=True,
                details={"failure_count": result.get("failure_count")},
            )] if result.get("status") == "fail" else [],
            artifacts=[{"role": "reconstructed_scene", "path": result.get("reconstructed_scene_path")}],
            next_actions=[
                "run material, camera, and fixed-view image parity after geometry parity passes"
            ] if result.get("status") == "pass" else [
                "inspect the first matrix or bounds mismatch before expanding the batch"
            ],
        )
    elif args.command == "evaluate-synthesis28-material-parity":
        try:
            result = evaluate_reconstruction_materials(
                args.spec_json,
                args.scene_path,
                max_failures=args.max_failures,
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return emit(
                {"status": "fail", "reason": str(exc)},
                exit_code=2,
                status="fail",
                errors=[issue("SYNTHESIS28_MATERIAL_PARITY_EVALUATION_FAILED", str(exc), retryable=True)],
            )
        return emit(
            result,
            exit_code=2 if result.get("status") == "fail" else 0,
            status=str(result.get("status")),
            errors=[issue(
                "SYNTHESIS28_MATERIAL_PARITY_FAILED",
                "One or more reconstructed objects differ in composed material bindings, shader inputs, or texture assets.",
                retryable=True,
                details={"failure_count": result.get("failure_count")},
            )] if result.get("status") == "fail" else [],
            artifacts=[{"role": "reconstructed_scene", "path": result.get("reconstructed_scene_path")}],
            next_actions=[
                "extract a fixed comparison camera and render source/reconstruction pairs"
            ] if result.get("status") == "pass" else [
                "inspect the first material signature mismatch; preserve the source binding or keep it pending"
            ],
        )
    elif args.command == "create-synthesis28-render-pair":
        try:
            result = create_reconstruction_render_pair(
                args.spec_json,
                args.scene_path,
                args.output_dir,
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return emit(
                {"status": "fail", "reason": str(exc)},
                exit_code=2,
                status="fail",
                errors=[issue("SYNTHESIS28_RENDER_PAIR_FAILED", str(exc), retryable=True)],
            )
        return emit(
            result,
            status="pass",
            artifacts=[
                {"role": "source_comparison_scene", "path": result.get("source_comparison_scene")},
                {"role": "reconstruction_comparison_scene", "path": result.get("reconstruction_comparison_scene")},
            ],
            next_actions=[
                "render both comparison layers at the reported camera_path, then compare pixels and render feedback"
            ],
        )
    elif args.command == "create-synthesis28-camera-candidates":
        try:
            result = create_reference_camera_candidates(
                args.spec_json,
                args.scene_path,
                args.output_scene,
                args.manifest,
                azimuth_count=args.azimuth_count,
                azimuth_center_deg=args.azimuth_center_deg,
                azimuth_span_deg=args.azimuth_span_deg,
                distance_scales=tuple(args.distance_scale) if args.distance_scale else (0.78, 1.08),
                eye_height_offsets_m=tuple(args.eye_height_offset_m) if args.eye_height_offset_m else (1.50, 2.10),
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return emit(
                {"status": "fail", "reason": str(exc)},
                exit_code=2,
                status="fail",
                errors=[issue("SYNTHESIS28_CAMERA_CANDIDATE_FAILED", str(exc), retryable=True)],
            )
        return emit(
            result,
            status="pass",
            artifacts=[
                {"role": "camera_candidate_scene", "path": result.get("candidate_scene_path")},
                {"role": "camera_candidate_manifest", "path": result.get("manifest_path")},
            ],
            next_actions=["render the manifest in one Isaac session, then rank candidates against the reference"],
        )
    elif args.command == "rank-synthesis28-camera-renders":
        try:
            result = rank_reference_camera_renders(
                args.manifest,
                args.reference_image,
                args.render_dir,
                args.output,
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return emit(
                {"status": "fail", "reason": str(exc)},
                exit_code=2,
                status="fail",
                errors=[issue("SYNTHESIS28_CAMERA_RANKING_FAILED", str(exc), retryable=True)],
            )
        return emit(
            result,
            status=str(result["status"]),
            warnings=[issue(
                "REFERENCE_CAMERA_SCORE_BELOW_TARGET",
                "The best candidate is usable for iteration but remains below the reference-match target.",
                retryable=True,
                details={"best_score": (result.get("best") or {}).get("reference_score")},
            )] if result["status"] == "warn" else [],
            artifacts=[{"role": "camera_ranking", "path": result.get("output")}],
            next_actions=["review the best candidate for occlusion before promoting it to the scene spec"],
        )
    elif args.command == "audit-prompt-regression-suite":
        try:
            result = audit_prompt_regression_suite(args.manifest, args.output)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return emit(
                {"status": "fail", "reason": str(exc)},
                exit_code=2,
                status="fail",
                errors=[issue("PROMPT_REGRESSION_SUITE_AUDIT_FAILED", str(exc), retryable=True)],
            )
        return emit(
            result,
            status="pass",
            warnings=[issue(
                "PROMPT_SUITE_NOT_READY_FOR_EXPANSION",
                "The core prompt-only suite has unresolved expansion blockers.",
                retryable=True,
                details={"repair_queue": result.get("repair_queue")},
            )] if not result.get("expansion_ready") else [],
            artifacts=[{"role": "prompt_regression_suite_audit", "path": result.get("output")}],
            next_actions=[
                "repair the core suite in the reported priority order"
                if not result.get("expansion_ready")
                else "add a small unseen holdout canary set without per-scene tuning"
            ],
        )
    elif args.command == "audit-synthesis28-expansion-readiness":
        try:
            result = audit_synthesis28_expansion_readiness(
                args.scene_kit_catalog,
                args.output,
                excluded_scenes=args.excluded_scenes,
                maximum_canaries=args.max_canaries,
                preferred_domains=args.preferred_domains,
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return emit(
                {"status": "fail", "reason": str(exc)},
                exit_code=2,
                status="fail",
                errors=[issue("SYNTHESIS28_EXPANSION_AUDIT_FAILED", str(exc), retryable=True)],
            )
        return emit(
            result,
            status="pass",
            warnings=[issue(
                "SYNTHESIS28_FULL_EXPANSION_NOT_READY",
                "Use the selected canary scenes; the full 28-scene set still contains catalog or topology blockers.",
                retryable=True,
                details={"class_counts": result.get("class_counts")},
            )] if not result.get("full_28_expansion_ready") else [],
            artifacts=[{"role": "synthesis28_expansion_readiness", "path": result.get("output")}],
            next_actions=[
                "generate and validate the selected pose-free canary scenes"
                if result.get("selected_canaries")
                else "improve callable anchor coverage in the Synthesis object catalog"
            ],
        )
    elif args.command == "audit-synthesis-gap":
        try:
            result = audit_synthesis_gap(
                args.spec_json,
                args.scene_kit_catalog,
                args.output,
                match_count=args.match_count,
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return emit(
                {"status": "fail", "reason": str(exc)},
                exit_code=2,
                status="fail",
                errors=[issue("SYNTHESIS_GAP_AUDIT_FAILED", str(exc), retryable=True)],
            )
        return emit(
            result,
            status="pass",
            artifacts=[{"role": "synthesis_gap_audit", "path": result.get("output")}],
            next_actions=["apply the pose-free repair plan without copying Synthesis source poses"],
        )
    elif args.command == "apply-scene-appearance":
        try:
            result = apply_scene_appearance(args.spec_json, args.output, profile=args.profile)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return emit(
                {"status": "fail", "reason": str(exc)},
                exit_code=2,
                status="fail",
                errors=[issue("SCENE_APPEARANCE_PASS_FAILED", str(exc), retryable=True)],
            )
        return emit(
            result,
            status="pass",
            artifacts=[{"role": "appearance_scene_spec", "path": result.get("output")}],
            next_actions=["recompose and render from the fixed comparison camera"],
        )
    elif args.command == "audit-material-coverage":
        try:
            result = audit_material_coverage(
                args.scene_path,
                root_path=args.root,
                camera_path=args.camera,
                minimum_score=args.minimum_score,
                minimum_effective_texture_fraction=args.minimum_effective_texture_fraction,
                maximum_unmapped_texture_fraction=args.maximum_unmapped_texture_fraction,
                maximum_unbound_fraction=args.maximum_unbound_fraction,
                max_gaps=args.max_gaps,
            )
            output_path = Path(args.output)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        except (OSError, ValueError) as exc:
            return emit(
                {"status": "fail", "reason": str(exc)},
                exit_code=2,
                status="fail",
                errors=[issue("MATERIAL_COVERAGE_AUDIT_FAILED", str(exc), retryable=True)],
            )
        failed = result.get("status") == "fail"
        return emit(
            result,
            exit_code=2 if failed else 0,
            status=str(result.get("status")),
            errors=[issue(
                "MATERIAL_COVERAGE_BELOW_THRESHOLD",
                "Visible surface material coverage is below the requested quality thresholds.",
                retryable=True,
                details={
                    "weighted_quality_score": result.get("weighted_quality_score"),
                    "failures": result.get("failures"),
                },
            )] if failed else [],
            artifacts=[{"role": "material_coverage_audit", "path": str(Path(args.output).resolve())}],
            next_actions=[
                "repair the highest-priority unmapped, unbound, or constant-material surfaces and rerender the fixed camera"
            ] if failed else [
                "freeze material bindings and continue to the fixed-camera review/final render profiles"
            ],
        )
    elif args.command == "create-prompt-camera-candidates":
        try:
            result = create_prompt_camera_candidates(
                args.spec_json,
                args.scene_path,
                args.output_scene,
                args.manifest,
                inset_m=args.inset_m,
                eye_heights_m=tuple(args.eye_height_m) if args.eye_height_m else (1.55, 2.15),
                focal_lengths_mm=tuple(args.focal_length_mm) if args.focal_length_mm else (20.0, 24.0, 28.0),
                look_at_z_offsets_m=tuple(args.look_at_z_offset_m) if args.look_at_z_offset_m else (-0.2, 0.0, 0.2),
                look_at_lateral_offsets_m=tuple(args.look_at_lateral_offset_m) if args.look_at_lateral_offset_m else (-0.4, 0.0, 0.4),
                max_candidates=args.max_candidates,
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return emit(
                {"status": "fail", "reason": str(exc)},
                exit_code=2,
                status="fail",
                errors=[issue("PROMPT_CAMERA_CANDIDATE_FAILED", str(exc), retryable=True)],
            )
        return emit(
            result,
            status=str(result["status"]),
            artifacts=[
                {"role": "prompt_camera_candidate_scene", "path": result.get("candidate_scene_path")},
                {"role": "prompt_camera_candidate_manifest", "path": result.get("manifest_path")},
            ],
            next_actions=["render the candidate manifest, then run rank-prompt-camera-renders"],
        )
    elif args.command == "rank-prompt-camera-renders":
        try:
            result = rank_prompt_camera_renders(
                args.manifest,
                args.render_dir,
                args.output,
                evidence_dir=args.evidence_dir,
                visual_review_packet_path=args.visual_review_packet,
                visual_agent_review_path=args.visual_agent_review,
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return emit(
                {"status": "fail", "reason": str(exc)},
                exit_code=2,
                status="fail",
                errors=[issue("PROMPT_CAMERA_RANKING_FAILED", str(exc), retryable=True)],
            )
        return emit(
            result,
            status=str(result["status"]),
            warnings=[issue(
                "PROMPT_CAMERA_CONFIRMATION_REQUIRED",
                "The selected batch candidate must be rendered alone before promotion.",
                retryable=True,
                details={"candidate_id": (result.get("best") or {}).get("candidate_id")},
            )] if result.get("best") else [],
            artifacts=[{"role": "prompt_camera_ranking", "path": result.get("output")}],
            next_actions=["promote the best candidate and render it alone in a fresh Isaac session"],
        )
    elif args.command == "create-visual-review-packet":
        try:
            result = create_visual_review_packet(
                args.spec_json,
                args.manifest,
                args.ranking,
                args.render_dir,
                args.output,
                scene_brief_path=args.scene_brief,
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return emit(
                {"status": "fail", "reason": str(exc)},
                exit_code=2,
                status="fail",
                errors=[issue("VISUAL_REVIEW_PACKET_FAILED", str(exc), retryable=True)],
            )
        return emit(
            result,
            status="pass",
            artifacts=[{"role": "visual_review_packet", "path": result.get("output")}],
            next_actions=["ask an external visual agent to compare the listed renders and return the review contract"],
        )
    elif args.command == "validate-visual-agent-review":
        try:
            result = validate_visual_agent_review(args.packet, args.review, args.output)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return emit(
                {"status": "fail", "reason": str(exc)},
                exit_code=2,
                status="fail",
                errors=[issue("VISUAL_AGENT_REVIEW_FAILED", str(exc), retryable=True)],
            )
        return emit(
            result,
            exit_code=2 if result["status"] == "fail" else 0,
            status=str(result["status"]),
            errors=[issue(row["code"], row["message"], retryable=True) for row in result.get("errors") or []],
            warnings=[issue(row["code"], row["message"], retryable=True) for row in result.get("warnings") or []],
            artifacts=[{"role": "visual_agent_review_validation", "path": result.get("output")}],
            next_actions=["rerun rank-prompt-camera-renders with the validated review"],
        )
    elif args.command == "create-quality-review-packet":
        try:
            result = create_quality_review_packet(
                args.comparison_manifest,
                args.axis,
                args.output,
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return emit(
                {"status": "fail", "reason": str(exc)},
                exit_code=2,
                status="fail",
                errors=[issue("QUALITY_REVIEW_PACKET_FAILED", str(exc), retryable=True)],
            )
        return emit(
            result,
            status="pass",
            artifacts=[{"role": f"{args.axis}_review_packet", "path": result.get("output")}],
            next_actions=[f"ask an independent visual Agent to complete only the {args.axis} contract"],
        )
    elif args.command == "validate-quality-agent-review":
        try:
            result = validate_quality_review(args.packet, args.review, args.output)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return emit(
                {"status": "fail", "reason": str(exc)},
                exit_code=2,
                status="fail",
                errors=[issue("QUALITY_AGENT_REVIEW_FAILED", str(exc), retryable=True)],
            )
        return emit(
            result,
            exit_code=2 if result["status"] == "fail" else 0,
            status=str(result["status"]),
            errors=[issue(row["code"], row["message"], retryable=True) for row in result.get("errors") or []],
            artifacts=[{"role": "quality_agent_review_validation", "path": result.get("output")}],
        )
    elif args.command == "create-content-iteration-packet":
        try:
            result = create_content_iteration_packet(
                args.brief_json,
                args.spec_json,
                args.render_manifest,
                args.output,
                iteration=args.iteration,
                phase=args.phase,
                max_iterations=args.max_iterations,
                previous_validation_path=args.previous_validation,
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return emit(
                {"status": "fail", "reason": str(exc)},
                exit_code=2,
                status="fail",
                errors=[issue("CONTENT_ITERATION_PACKET_FAILED", str(exc), retryable=True)],
            )
        return emit(
            result,
            status="pass",
            artifacts=[{"role": "content_iteration_packet", "path": result.get("output")}],
            next_actions=["ask an independent content Agent to review all listed views using the packet contract"],
        )
    elif args.command == "validate-content-iteration-review":
        try:
            result = validate_content_iteration_review(args.packet, args.review, args.output)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return emit(
                {"status": "fail", "reason": str(exc)},
                exit_code=2,
                status="fail",
                errors=[issue("CONTENT_ITERATION_REVIEW_FAILED", str(exc), retryable=True)],
            )
        structural_failure = result.get("status") == "fail"
        gate_pending = not structural_failure and result.get("content_gate") != "pass"
        return emit(
            result,
            exit_code=2 if structural_failure else 0,
            status="fail" if structural_failure else ("pending" if gate_pending else "pass"),
            errors=[issue(row["code"], row["message"], retryable=True) for row in result.get("errors") or []],
            warnings=[issue(row["code"], row["message"], retryable=True) for row in result.get("warnings") or []],
            artifacts=[{"role": "content_iteration_review_validation", "path": result.get("output")}],
            next_actions=["create a scene revision request and regenerate the complete spec"] if gate_pending else ["create a scene revision request to advance or freeze the accepted phase"],
        )
    elif args.command == "create-scene-revision-request":
        try:
            result = create_scene_revision_request(args.packet, args.validation, args.output)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return emit(
                {"status": "fail", "reason": str(exc)},
                exit_code=2,
                status="fail",
                errors=[issue("SCENE_REVISION_REQUEST_FAILED", str(exc), retryable=True)],
            )
        action = str(result.get("action") or "")
        envelope_status = "pass" if action == "freeze_content" else "pending"
        if action == "regenerate_complete_spec":
            next_actions = ["give the revision request to the planning Agent, emit a complete pose-free brief/spec, solve, render multiple views, and review again"]
        elif action == "advance_phase":
            next_actions = ["materialize only the next enrichment phase, solve its dependency closure, render multiple views, and review again"]
        elif action == "stop_for_human_review":
            next_actions = ["preserve the last good iteration and request human direction; do not continue adding assets"]
        else:
            next_actions = ["freeze content and proceed to camera, material, and final-render quality gates"]
        return emit(
            result,
            status=envelope_status,
            warnings=[issue(
                "CONTENT_ITERATION_BUDGET_EXHAUSTED",
                "The content iteration budget is exhausted; preserve the last good scene.",
                retryable=False,
            )] if action == "stop_for_human_review" else [],
            artifacts=[{"role": "scene_revision_request", "path": result.get("output")}],
            next_actions=next_actions,
        )
    elif args.command == "promote-prompt-camera":
        try:
            result = promote_prompt_camera(
                args.spec_json,
                args.scene_path,
                args.manifest,
                args.ranking,
                args.output_spec,
                args.output_scene,
                candidate_id=args.candidate_id,
                confirmation_path=args.confirmation,
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return emit(
                {"status": "fail", "reason": str(exc)},
                exit_code=2,
                status="fail",
                errors=[issue("PROMPT_CAMERA_PROMOTION_FAILED", str(exc), retryable=True)],
            )
        return emit(
            result,
            status="pass",
            artifacts=[
                {"role": "camera_promoted_spec", "path": result.get("output_spec")},
                {"role": "camera_promoted_scene", "path": result.get("output_scene")},
            ],
            next_actions=["render the promoted camera alone and run inspect-render-feedback"],
        )
    elif args.command == "confirm-prompt-camera-selection":
        try:
            result = confirm_prompt_camera_selection(
                args.manifest,
                args.ranking,
                args.baseline_render,
                args.selected_render,
                args.output,
                baseline_visibility_path=args.baseline_visibility,
                selected_visibility_path=args.selected_visibility,
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return emit(
                {"status": "fail", "reason": str(exc)},
                exit_code=2,
                status="fail",
                errors=[issue("PROMPT_CAMERA_CONFIRMATION_FAILED", str(exc), retryable=True)],
            )
        return emit(
            result,
            status=str(result["status"]),
            warnings=[issue(
                "PROMPT_CAMERA_ROLLED_BACK",
                "The fresh-session challenger failed a mandatory gate; retain the baseline camera.",
                retryable=True,
            )] if result["decision"] == "rollback_to_baseline" else [],
            artifacts=[{"role": "prompt_camera_confirmation", "path": result.get("output")}],
            next_actions=["promote only the confirmed candidate" if result["promotion_allowed"] else "retain the original baseline camera"],
        )
    elif args.command == "inspect-functional-groups":
        spec = _read_json(Path(args.spec_json))
        if not spec:
            raise SystemExit(f"Invalid scene spec JSON: {args.spec_json}")
        result = inspect_functional_group_layout(
            spec,
            position_tolerance_m=args.position_tolerance_m,
            yaw_tolerance_deg=args.yaw_tolerance_deg,
        )
        return emit(
            result,
            exit_code=2 if result.get("status") == "fail" else 0,
            status=str(result.get("status")),
            errors=[issue(
                "FUNCTIONAL_GROUP_LAYOUT_FAILED",
                f"{result.get('failed_member_count')} functional-group members violate relative layout tolerances.",
                retryable=True,
                details={"failed_member_count": result.get("failed_member_count")},
            )] if result.get("failed_member_count") else [],
            warnings=[issue(
                "FUNCTIONAL_GROUP_FRONT_AXIS_PENDING",
                f"{result.get('orientation_pending_count')} members use authored yaw because front-axis metadata is pending.",
                retryable=True,
                details={"orientation_pending_count": result.get("orientation_pending_count")},
            )] if result.get("orientation_pending_count") else [],
        )
    elif args.command == "inspect-facing":
        parsed_pairs: list[tuple[str, str]] = []
        front_axes: dict[str, str] = {}
        invalid_items: list[str] = []
        for item in args.prim_pairs:
            if "=" not in item:
                invalid_items.append(item)
                continue
            subject_path, target_path = item.split("=", 1)
            if not subject_path.startswith("/") or not target_path.startswith("/"):
                invalid_items.append(item)
                continue
            parsed_pairs.append((subject_path, target_path))
        for item in args.front_axes or []:
            if "=" not in item:
                invalid_items.append(item)
                continue
            subject_path, front_axis = item.split("=", 1)
            try:
                front_axes[subject_path] = normalize_front_axis(front_axis)
            except ValueError:
                invalid_items.append(item)
        if invalid_items:
            return emit(
                {"status": "fail", "invalid_items": invalid_items},
                exit_code=2,
                status="fail",
                errors=[issue(
                    "INVALID_FACING_ARGUMENT",
                    "Use SUBJECT_PRIM=TARGET_PRIM and SUBJECT_PRIM=+X|-X|+Y|-Y",
                    retryable=True,
                    details={"invalid_items": invalid_items},
                )],
            )
        result = inspect_facing(
            args.scene_path,
            prim_pairs=parsed_pairs,
            front_axis_overrides=front_axes,
            max_angle_deg=args.max_angle_deg,
            target_mode=args.target_mode,
        )
        errors = [
            issue(
                str(row.get("kind") or "FACING_VALIDATION_FAILED").upper(),
                str(row.get("suggestion") or row.get("kind") or "Facing validation failed"),
                retryable=True,
                details=row,
            )
            for row in result.get("errors", [])
        ]
        code = 2 if result.get("status") == "fail" else 0
        return emit(
            result,
            exit_code=code,
            status=str(result.get("status")),
            warnings=result.get("warnings"),
            errors=errors,
            next_actions=["inspect the source asset front, fix front_axis or Z rotation, and rerun facing validation"] if errors else [],
        )
    elif args.command == "inspect-layout-coverage":
        return emit(inspect_layout_coverage(
            args.scene_path,
            background_prim_path=args.background_prim,
            objects_root_path=args.objects_root,
            layout_prim_path=args.layout_prim,
            min_coverage_xy=args.min_coverage_xy,
            max_coverage_xy=args.max_coverage_xy,
            min_boundary_margin_m=args.min_boundary_margin_m,
        ))
    elif args.command == "inspect-render-feedback":
        overrides = {
            key: value
            for key, value in {
                "min_mean_luminance": args.min_mean_luminance,
                "max_shadow_fraction": args.max_shadow_fraction,
                "min_subject_mean_luminance": args.min_subject_mean_luminance,
                "max_subject_shadow_fraction": args.max_subject_shadow_fraction,
                "max_highlight_clip_fraction": args.max_highlight_clip_fraction,
            }.items()
            if value is not None
        }
        result = analyze_image(
            args.image_path,
            view_kind=args.view_kind,
            threshold_overrides=overrides,
        )
        errors = [
            issue(
                str(row.get("code") or "RENDER_FEEDBACK_FAILED"),
                str(row.get("summary") or "Render feedback failed"),
                retryable=True,
                details=row.get("details"),
            )
            for row in result.get("failures", [])
        ]
        warnings = [
            issue(
                str(row.get("code") or "RENDER_FEEDBACK_WARNING"),
                str(row.get("summary") or "Render feedback warning"),
                retryable=True,
                details=row.get("details"),
            )
            for row in result.get("warnings", [])
        ]
        code = 2 if result.get("status") == "fail" else 0
        return emit(
            result,
            exit_code=code,
            status=str(result.get("status")),
            warnings=warnings,
            errors=errors,
            next_actions=[
                "apply only the recommended lighting/exposure changes, rerender the same camera, and recheck; keep geometry fixed"
            ] if errors or warnings else [],
        )
    elif args.command == "inspect-reference-match":
        result = compare_images(args.reference_image, args.rendered_image)
        failures = [
            issue(
                str(row.get("code") or "REFERENCE_MATCH_FAILED"),
                str(row.get("message") or "Rendered view does not match the reference closely enough."),
                retryable=True,
                details=row,
            )
            for row in result.get("failures", [])
        ]
        if result.get("status") == "fail" and not failures:
            failures = [issue(
                "REFERENCE_MATCH_LOW",
                f"Reference-match score {float(result.get('score', 0.0)):.3f} is below 0.50.",
                retryable=True,
                details=result.get("metrics"),
            )]
        warnings = []
        if result.get("status") == "warn":
            warnings.append(issue(
                "REFERENCE_MATCH_NEEDS_IMPROVEMENT",
                f"Reference-match score {float(result.get('score', 0.0)):.3f} is below the 0.72 pass threshold.",
                retryable=True,
                details=result.get("metrics"),
            ))
        code = 2 if result.get("status") == "fail" else 0
        return emit(
            result,
            exit_code=code,
            status=str(result.get("status")),
            warnings=warnings,
            errors=failures,
            next_actions=[
                "keep the reference camera fixed; inspect object identity, palette, silhouette and screen-space placement before changing lighting"
            ] if failures or warnings else [],
        )
    elif args.command == "evaluate-scene-quality":
        result = evaluate_scene(
            reference_image=args.reference_image,
            rendered_image=args.rendered_image,
            scene_spec=args.scene_spec,
            summary=args.summary,
        )
        errors = [
            issue(
                str(row.get("code") or "SCENE_QUALITY_FAILED"),
                str(row.get("message") or row.get("code") or "Scene quality gate failed."),
                retryable=True,
                details=row,
            )
            for row in result.get("failures", [])
        ]
        warnings = [
            issue(
                str(row.get("code") or "SCENE_QUALITY_WARNING"),
                str(row.get("message") or row.get("code") or "Scene quality warning."),
                retryable=True,
                details=row,
            )
            for row in result.get("warnings", [])
        ]
        code = 2 if result.get("status") == "fail" else 0
        return emit(
            result,
            exit_code=code,
            status=str(result.get("status")),
            warnings=warnings,
            errors=errors,
            next_actions=[
                "repair only failed gates, rerender the same camera, and rerun evaluate-scene-quality"
            ] if errors or warnings else [],
        )
    elif args.command == "compose-scene-summary":
        if args.manifest:
            try:
                input_paths = load_validation_manifest(args.manifest)
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                return emit(
                    {"status": "fail", "manifest": args.manifest},
                    exit_code=2,
                    status="fail",
                    errors=[issue("INVALID_VALIDATION_MANIFEST", str(exc), retryable=True)],
                )
            quality_policy = input_paths["quality_policy"]
            output_value = args.output or input_paths.get("output")
        else:
            input_paths = {
                "scene_spec": Path(args.scene_spec) if args.scene_spec else None,
                "compile_report": Path(args.compile_report) if args.compile_report else None,
                "validate_scene_reports": [Path(value) for value in args.validate_scene_report or []],
                "overlap_reports": [Path(value) for value in args.overlap_report or []],
                "support_reports": [Path(value) for value in args.support_report or []],
                "facing_reports": [Path(value) for value in args.facing_report or []],
                "render_feedback_reports": [Path(value) for value in args.render_feedback_report or []],
            }
            absent = [name for name, value in input_paths.items() if not value]
            if absent or not args.quality_policy:
                return emit(
                    {"status": "fail", "missing_arguments": absent},
                    exit_code=2,
                    status="fail",
                    errors=[issue(
                        "SCENE_SUMMARY_ARGUMENTS_MISSING",
                        "Use --manifest or provide every required report and --quality-policy.",
                        retryable=True,
                        details={"missing": absent},
                    )],
                )
            quality_policy = args.quality_policy
            output_value = args.output
        if not output_value:
            return emit(
                {"status": "fail", "reason": "missing_output"},
                exit_code=2,
                status="fail",
                errors=[issue("SCENE_SUMMARY_OUTPUT_MISSING", "Provide --output or manifest.output.", retryable=True)],
            )
        missing = [str(path) for path in flatten_evidence_paths(input_paths) if not path.is_file()]
        if missing:
            errors = [issue(
                "SCENE_SUMMARY_EVIDENCE_MISSING",
                "Cannot compose a passing scene summary without every required report.",
                retryable=True,
                details={"missing": missing},
            )]
            return emit(
                {"status": "fail", "missing": missing},
                exit_code=2,
                status="fail",
                errors=errors,
                next_actions=["run the missing validation commands and retry compose-scene-summary"],
            )

        def report(path: Path) -> dict[str, Any]:
            value = _read_json(path)
            nested = value.get("result")
            return nested if isinstance(nested, dict) else value

        result = compose_summary(
            scene_spec=report(input_paths["scene_spec"]),
            compile_report=report(input_paths["compile_report"]),
            validate_reports=[report(path) for path in input_paths["validate_scene_reports"]],
            overlap_reports=[report(path) for path in input_paths["overlap_reports"]],
            support_reports=[report(path) for path in input_paths["support_reports"]],
            facing_reports=[report(path) for path in input_paths["facing_reports"]],
            render_feedback_reports=[report(path) for path in input_paths["render_feedback_reports"]],
            quality_policy=quality_policy,
            evidence_paths={
                key: [str(item) for item in value] if isinstance(value, list) else str(value)
                for key, value in input_paths.items()
                if key not in {"quality_policy", "output"}
            },
        )
        output = new_output(output_value, root=(config.get("runtime") or {}).get("output_root"))
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(_json(result) + "\n", encoding="utf-8")
        errors = [
            issue(
                str(row.get("code") or "SCENE_SUMMARY_GATE_FAILED"),
                str(row.get("message") or "Scene summary gate failed."),
                retryable=True,
                details=row,
            )
            for row in result.get("failures", [])
        ]
        code = 2 if result["status"] == "fail" else 0
        return emit(
            result,
            exit_code=code,
            status=result["status"],
            errors=errors,
            artifacts=[{"role": "scene_validation_summary", "path": str(output)}],
            next_actions=[
                "repair only failed scene-summary gates and recompute all affected evidence"
            ] if errors else [],
        )
    elif args.command == "export-handoff":
        state = _read_json(Path(args.state_json))
        if not state:
            return emit({}, exit_code=2, errors=[issue("HANDOFF_STATE_INVALID", f"Invalid state JSON: {args.state_json}")])
        try:
            result = export_handoff(state, args.output_dir, config=config, skill_root=SKILL_ROOT)
        except (ValueError, OSError, ImportError, RuntimeError) as exc:
            return emit({"status": "fail"}, exit_code=2, errors=[issue("HANDOFF_EXPORT_FAILED", str(exc), retryable=True)])
        return emit(result, artifacts=[{"role": "handoff_manifest", "path": result.get("manifest_path")}, {"role": "asset_lock", "path": result.get("asset_lock")}, {"role": "profile_snapshot", "path": result.get("profile_snapshot")}])
    elif args.command == "verify-handoff":
        result = verify_handoff(args.manifest_path)
        code = 2 if result.get("status") == "fail" else 0
        return emit(result, exit_code=code, status=str(result.get("status")), warnings=result.get("warnings"), errors=result.get("errors"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
