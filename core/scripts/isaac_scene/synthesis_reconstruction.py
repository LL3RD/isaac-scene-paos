from __future__ import annotations

import json
import hashlib
import math
import re
import sqlite3
from collections import Counter
from pathlib import Path
from statistics import median
from typing import Any


def _safe_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]+", "_", value).strip("_") or "Asset"


def _json_value(value: Any, fallback: Any) -> Any:
    if value in (None, ""):
        return fallback
    return json.loads(str(value))


def _load_relations(path: str | Path) -> dict[str, dict[str, Any]]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    result: dict[str, dict[str, dict[str, Any]]] = {}
    for scene in data.get("scenes") or []:
        if not isinstance(scene, dict) or not scene.get("scene_slug"):
            continue
        result[str(scene["scene_slug"])] = {
            "scene_path": scene.get("scene_path"),
            "relations": {
                str(row["instance_id"]): row
                for row in scene.get("relations") or []
                if isinstance(row, dict) and row.get("instance_id")
            },
        }
    return result


def _load_catalog(path: str | Path) -> tuple[dict[str, dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    connection = sqlite3.connect(str(path))
    connection.row_factory = sqlite3.Row
    try:
        objects = {
            str(row["model_id"]): dict(row)
            for row in connection.execute("SELECT * FROM objects ORDER BY model_id")
        }
        scenes: dict[str, list[dict[str, Any]]] = {}
        for row in connection.execute(
            "SELECT * FROM instances ORDER BY scene_slug, source_prim_path, instance_id"
        ):
            item = dict(row)
            scenes.setdefault(str(item["scene_slug"]), []).append(item)
    finally:
        connection.close()
    return objects, scenes


def _instance_prim(instance: dict[str, Any]) -> str:
    source_leaf = str(instance.get("source_prim_path") or "").rsplit("/", 1)[-1]
    suffix = str(instance.get("instance_id") or "").rsplit(":", 1)[-1]
    return f"/World/Objects/{_safe_id(source_leaf)}_{_safe_id(suffix)}"


def _matrix_multiply(left: list[list[float]], right: list[list[float]]) -> list[list[float]]:
    return [
        [sum(float(left[row][k]) * float(right[k][column]) for k in range(4)) for column in range(4)]
        for row in range(4)
    ]


def _matrix_inverse(matrix: list[list[float]]) -> list[list[float]]:
    augmented = [
        [float(value) for value in matrix[row]] + [1.0 if row == column else 0.0 for column in range(4)]
        for row in range(4)
    ]
    for column in range(4):
        pivot = max(range(column, 4), key=lambda row: abs(augmented[row][column]))
        if abs(augmented[pivot][column]) <= 1e-15:
            raise ValueError("Wrapper normalization matrix is singular")
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        divisor = augmented[column][column]
        augmented[column] = [value / divisor for value in augmented[column]]
        for row in range(4):
            if row == column:
                continue
            factor = augmented[row][column]
            augmented[row] = [
                augmented[row][index] - factor * augmented[column][index]
                for index in range(8)
            ]
    return [row[4:] for row in augmented]


def _wrapper_normalization_matrix(path: str | Path) -> list[list[float]]:
    """Read the single normalization transform authored on wrapper /Object/Source."""
    identity = [[1.0 if row == column else 0.0 for column in range(4)] for row in range(4)]
    text = Path(path).read_text(encoding="utf-8")
    marker = "matrix4d xformOp:transform"
    start = text.find(marker)
    if start < 0:
        return identity
    value_start = text.find("=", start)
    end = text.find("uniform token[] xformOpOrder", start)
    block = text[value_start + 1:end if end >= 0 else None]
    values = [float(value) for value in re.findall(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?", block)]
    # Exactly 16 matrix scalars are expected from the stable wrapper contract.
    if len(values) != 16:
        raise ValueError(f"Unable to parse wrapper normalization matrix: {path}")
    return [values[index:index + 4] for index in range(0, 16, 4)]


def _instance_attachment_evidence(
    prim: Any,
    model_id: str,
    UsdGeom: Any,
) -> dict[str, Any]:
    """Locate the child on which a Synthesis model arc is authored.

    Source scenes commonly attach a model reference at ``owner/v_0`` while the
    object wrapper references the same source root directly at ``/Source``.
    The attachment prefix must therefore be removed from copied opinion paths,
    and its local transform must be absorbed into the reconstructed root pose.
    """
    identity = [[1.0 if row == column else 0.0 for column in range(4)] for row in range(4)]
    owner_path = str(prim.GetPath())
    candidates = [prim, *list(prim.GetChildren())]
    for candidate in candidates:
        for spec in candidate.GetPrimStack():
            for item_list in (spec.referenceList, spec.payloadList):
                items: list[Any] = []
                for bucket in (
                    "prependedItems", "appendedItems", "explicitItems",
                    "addedItems", "orderedItems",
                ):
                    try:
                        items.extend(list(getattr(item_list, bucket)))
                    except (AttributeError, TypeError):
                        pass
                if not any(model_id in str(item.assetPath) for item in items):
                    continue
                candidate_path = str(candidate.GetPath())
                relative = candidate_path[len(owner_path):]
                matrix = identity
                if relative:
                    value = UsdGeom.Xformable(candidate).GetLocalTransformation()
                    matrix = [
                        [float(value[row][column]) for column in range(4)]
                        for row in range(4)
                    ]
                return {
                    "source_attachment_path": relative,
                    "source_attachment_local_matrix": matrix,
                }
    return {
        "source_attachment_path": "",
        "source_attachment_local_matrix": identity,
    }


def _support_target(
    relation: dict[str, Any],
    instances: dict[str, dict[str, Any]],
    prims: dict[str, str],
    geometry_suffixes: dict[str, str],
) -> str | None:
    support_id = str(relation.get("support_instance_id") or "")
    target = str(relation.get("support_target") or "")
    support = instances.get(support_id)
    if not support or support_id not in prims:
        return None
    geometry_root = prims[support_id] + geometry_suffixes.get(support_id, "")
    source_owner = str(support.get("source_prim_path") or "")
    if target == source_owner:
        return geometry_root
    if source_owner and target.startswith(source_owner + "/"):
        # Normalized wrappers preserve the instance subtree under /Source.
        # Raw regression fallbacks may have been referenced below an authored
        # scene attachment prim, so only their owner root is guaranteed.
        if geometry_suffixes.get(support_id):
            return geometry_root + target[len(source_owner):]
        return geometry_root
    return geometry_root


def _support_cycle_nodes(relations: dict[str, dict[str, Any]]) -> set[str]:
    edges = {
        instance_id: str(row.get("support_instance_id"))
        for instance_id, row in relations.items()
        if row.get("regression_status") == "pass" and row.get("support_instance_id")
    }
    state: dict[str, int] = {}
    stack: list[str] = []
    cycles: set[str] = set()

    def visit(node: str) -> None:
        status = state.get(node, 0)
        if status == 2:
            return
        if status == 1:
            if node in stack:
                cycles.update(stack[stack.index(node):])
            return
        state[node] = 1
        stack.append(node)
        target = edges.get(node)
        if target in edges:
            visit(str(target))
        stack.pop()
        state[node] = 2

    for node in sorted(edges):
        visit(node)
    return cycles


def build_reconstruction_spec(
    scene_slug: str,
    instances: list[dict[str, Any]],
    objects: dict[str, dict[str, Any]],
    relations: dict[str, dict[str, Any]],
    source_scene_path: str | None = None,
    source_local_bounds: dict[str, dict[str, list[float]]] | None = None,
    binding_local_bounds: dict[str, dict[str, list[float]]] | None = None,
    scene_feature_paths: list[str] | None = None,
    instance_attachments: dict[str, dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build an exact-pose, object-library reconstruction spec for one scene.

    The export is an internal regression fixture. Assets that have not passed
    the public retrieval gates remain explicitly marked regression-only.
    """
    by_instance = {str(row["instance_id"]): row for row in instances}
    prims = {instance_id: _instance_prim(row) for instance_id, row in by_instance.items()}
    binding_paths: dict[str, str] = {}
    geometry_suffixes: dict[str, str] = {}
    for instance_id, instance in by_instance.items():
        asset = objects.get(str(instance.get("model_id") or "")) or {}
        wrapper = str(asset.get("wrapper_path") or "")
        if asset.get("wrapper_status") == "structural_pass" and wrapper:
            binding_paths[instance_id] = wrapper
            geometry_suffixes[instance_id] = "/Source"
        else:
            binding_paths[instance_id] = str(instance.get("source_path") or asset.get("source_path") or "")
            geometry_suffixes[instance_id] = ""
    entities: list[dict[str, Any]] = []
    relation_rows: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    supported = 0
    pending_support = 0
    cycle_nodes = _support_cycle_nodes(relations)

    for instance_id, instance in by_instance.items():
        model_id = str(instance.get("model_id") or "")
        asset = objects.get(model_id) or {}
        wrapper = str(asset.get("wrapper_path") or "")
        binding_path = binding_paths.get(instance_id) or ""
        binding_mode = "normalized_wrapper" if geometry_suffixes.get(instance_id) else "raw_source_regression"
        if not binding_path or not Path(binding_path).is_file():
            missing.append({
                "instance_id": instance_id,
                "model_id": model_id,
                "semantic_name": instance.get("semantic_name"),
                "reason": "structural_wrapper_unavailable",
            })
            continue
        xform = _json_value(instance.get("xform_json"), {})
        matrix = xform.get("world_matrix")
        if not (isinstance(matrix, list) and len(matrix) == 4):
            missing.append({
                "instance_id": instance_id,
                "model_id": model_id,
                "semantic_name": instance.get("semantic_name"),
                "reason": "world_matrix_unavailable",
            })
            continue
        source_world_matrix = [[float(value) for value in row] for row in matrix]
        normalization_matrix = _wrapper_normalization_matrix(wrapper) if binding_mode == "normalized_wrapper" else [
            [1.0, 0.0, 0.0, 0.0],
            [0.0, 1.0, 0.0, 0.0],
            [0.0, 0.0, 1.0, 0.0],
            [0.0, 0.0, 0.0, 1.0],
        ]
        wrapper_bounds = (
            _json_value(asset.get("bbox_m_json"), {})
            if binding_mode == "normalized_wrapper"
            else (binding_local_bounds or {}).get(instance_id, {})
        )
        source_bounds = (source_local_bounds or {}).get(instance_id)
        alignment: dict[str, Any] = {
            "mode": "wrapper_normalization_inverse",
            "wrapper_normalization_matrix": normalization_matrix,
        }
        if source_bounds and wrapper_bounds.get("min") and wrapper_bounds.get("max"):
            source_min = [float(value) for value in source_bounds["min"]]
            source_max = [float(value) for value in source_bounds["max"]]
            wrapper_min = [float(value) for value in wrapper_bounds["min"]]
            wrapper_max = [float(value) for value in wrapper_bounds["max"]]
            source_size = [source_max[index] - source_min[index] for index in range(3)]
            wrapper_size = [wrapper_max[index] - wrapper_min[index] for index in range(3)]
            size_error = max(abs(source_size[index] - wrapper_size[index]) for index in range(3))
            alignment.update({
                "source_local_bounds_m": source_bounds,
                "wrapper_local_bounds_m": wrapper_bounds,
                "size_max_abs_error_m": size_error,
            })
        attachment = (instance_attachments or {}).get(instance_id) or {}
        attachment_matrix = attachment.get("source_attachment_local_matrix") or [
            [1.0, 0.0, 0.0, 0.0],
            [0.0, 1.0, 0.0, 0.0],
            [0.0, 0.0, 1.0, 0.0],
            [0.0, 0.0, 0.0, 1.0],
        ]
        alignment["source_attachment_path"] = str(attachment.get("source_attachment_path") or "")
        alignment["source_attachment_local_matrix"] = attachment_matrix
        # The wrapper normalization is a deterministic coordinate-frame change.
        # Reversing that change is exact; aligning only the two AABB centers was
        # an approximation that hid instance-level child transforms and stale
        # extents hints.  Visual opinions authored on the source scene instance
        # are replayed separately by the composer below the referenced wrapper.
        # The attachment prim composes the referenced source root at a different
        # namespace.  Its authored xform ops are commonly same-name overrides of
        # that source root, not an extra parent transform; multiplying them again
        # double-applies assets such as the kitchen microwave.  The attachment is
        # therefore path-mapping evidence only.
        matrix = _matrix_multiply(_matrix_inverse(normalization_matrix), source_world_matrix)
        relation = relations.get(instance_id) or {}
        exact_target = None
        if relation.get("regression_status") == "pass" and instance_id not in cycle_nodes:
            if relation.get("relation") == "grounded_on_floor_plane":
                exact_target = "floor"
            else:
                exact_target = _support_target(relation, by_instance, prims, geometry_suffixes)
        entity = {
            "id": instance_id,
            "role": "scene_object",
            "semantic_name": instance.get("semantic_name"),
            "model_id": model_id,
            "logical_id": asset.get("logical_id"),
            "path": binding_path,
            "prim": prims[instance_id],
            "unit_mode": "preserve",
            "xform_matrix": matrix,
            "source_world_matrix": source_world_matrix,
            "wrapper_normalization_matrix": normalization_matrix,
            "regression_alignment": alignment,
            "geometry_prim_suffix": geometry_suffixes.get(instance_id, ""),
            "asset_binding_mode": binding_mode,
            "license_gate_bypassed_for_internal_regression": binding_mode == "raw_source_regression",
            "physics": "none",
            "asset_scope": instance.get("asset_scope"),
            "source_prim_path": instance.get("source_prim_path"),
            "source_xform_ops": xform.get("xform_ops") or [],
            "regression_only": not bool(asset.get("callable")),
            "qa_status": asset.get("qa_status"),
            "license_status": asset.get("license_status"),
        }
        if source_scene_path:
            entity["regression_instance_opinions"] = {
                "source_layer": str(source_scene_path),
                "source_prim_path": str(instance.get("source_prim_path") or ""),
                "source_attachment_path": str(attachment.get("source_attachment_path") or ""),
                "target_prim_suffix": geometry_suffixes.get(instance_id, ""),
                "policy": "visual_instance_overrides_v1",
            }
        if exact_target:
            fixed_mount = relation.get("relation") == "fixed_to_ceiling_mesh"
            if fixed_mount:
                entity["mount_target"] = exact_target
                mount_anchor = _support_target(
                    {
                        "support_instance_id": instance_id,
                        "support_target": relation.get("mount_anchor_prim"),
                    },
                    by_instance,
                    prims,
                    geometry_suffixes,
                )
                if mount_anchor:
                    entity["mount_anchor_prim"] = mount_anchor
                entity["mount_mode"] = "fixed"
            else:
                entity["support_target"] = exact_target
                if relation.get("relation") == "grounded_on_floor_plane":
                    entity["support_plane_z_m"] = relation.get("floor_z_m")
                    entity["support_mode"] = "ground"
            supported += 1
            relation_rows.append({
                "type": "fixed_to" if fixed_mount else "grounded_on" if exact_target == "floor" else "supported_by",
                "subject": instance_id,
                "target": exact_target,
                "resolved_transform": matrix[3][:3],
                "evidence": {
                    "source": "synthesis28_mesh_support_relations",
                    "relation": relation.get("relation"),
                    "confidence": relation.get("confidence"),
                    "mesh_support_coverage": relation.get("mesh_support_coverage"),
                    "signed_gap_m": relation.get("signed_gap_m"),
                    "mount_anchor_prim": relation.get("mount_anchor_prim"),
                },
            })
        elif relation:
            pending_support += 1
            entity["support_observation"] = {
                "status": "pending",
                "relation": relation.get("relation"),
                "proposed_target": relation.get("support_target"),
                "confidence": relation.get("confidence"),
                **({"reason": "support_dependency_cycle"} if instance_id in cycle_nodes else {}),
            }
        else:
            pending_support += 1
            entity["support_observation"] = {
                "status": "pending",
                "reason": "support_relation_unavailable",
            }
        entities.append(entity)

    included_ids = {str(row["id"]) for row in entities}
    # A passed target can still point at a skipped wrapper. Keep that evidence
    # pending rather than emitting a dangling graph edge.
    for entity in entities:
        target = entity.get("support_target") or entity.get("mount_target")
        if not target:
            continue
        owner_exists = target == "floor" or any(
            target == row["prim"] or str(target).startswith(str(row["prim"]) + "/")
            for row in entities
            if str(row["id"]) in included_ids
        )
        if owner_exists:
            continue
        entity.pop("support_target", None)
        entity.pop("mount_target", None)
        entity.pop("mount_anchor_prim", None)
        entity.pop("mount_mode", None)
        entity["support_observation"] = {"status": "pending", "reason": "support_wrapper_unavailable"}
        pending_support += 1
    emitted_edges = {
        (str(entity["id"]), str(entity.get("support_target") or entity.get("mount_target")))
        for entity in entities
        if entity.get("support_target") or entity.get("mount_target")
    }
    relation_rows = [
        row for row in relation_rows
        if (str(row["subject"]), str(row["target"])) in emitted_edges
    ]

    stage_references = [
        {
            "id": f"synthesis28_stage_feature:{scene_slug}:{_safe_id(path)}",
            "role": "scene_feature",
            "path": str(source_scene_path),
            "reference_prim_path": path,
            "prim": path,
            "unit_mode": "preserve",
            "physics": "none",
            "regression_only": True,
        }
        for path in scene_feature_paths or []
        if source_scene_path
    ]
    report = {
        "scene_slug": scene_slug,
        "source_instance_count": len(instances),
        "exported_entity_count": len(entities),
        "missing_entity_count": len(missing),
        "verified_support_edge_count": len(relation_rows),
        "verified_ground_edge_count": sum(row["type"] == "grounded_on" for row in relation_rows),
        "verified_mesh_support_edge_count": sum(row["type"] == "supported_by" for row in relation_rows),
        "verified_fixed_mount_edge_count": sum(row["type"] == "fixed_to" for row in relation_rows),
        "pending_support_count": pending_support,
        "demoted_cycle_edge_count": len(cycle_nodes),
        "callable_entity_count": sum(not bool(row["regression_only"]) for row in entities),
        "regression_only_entity_count": sum(bool(row["regression_only"]) for row in entities),
        "raw_source_regression_entity_count": sum(
            row.get("asset_binding_mode") == "raw_source_regression" for row in entities
        ),
        "instance_opinion_entity_count": sum(
            bool(row.get("regression_instance_opinions")) for row in entities
        ),
        "scene_feature_reference_count": len(stage_references),
        "missing": missing,
    }
    spec = {
        "schema_version": "isaac-scene-spec/v2",
        "domain": scene_slug,
        "stage": {"up_axis": "Z", "meters_per_unit": 1.0, "unit_policy": "preserve_references"},
        "room": {"type": scene_slug, "quality_target": "synthesis28_reconstruction"},
        "entities": entities,
        "references": stage_references,
        "relations": relation_rows,
        "constraints": {
            "support_graph_source": "synthesis28_mesh_support_relations",
            "semantic_truth": False,
        },
        "validation_targets": [
            "assets_resolve", "pose_parity", "plausible_bounds", "no_severe_overlap",
            "support_contact", "material_parity", "camera_parity", "render_feedback",
        ],
        "reconstruction_provenance": {
            "mode": "internal_regression_exact_pose",
            "scene_slug": scene_slug,
            "source_scene_path": source_scene_path,
            "object_identity": "model_id",
            "instance_identity": "scene_slug+instance_id",
            "transform_source": "authored_usd_world_matrix",
            "support_source": "stage11_mesh_surface_regression",
            "public_retrieval_gates_bypassed": any(
                row.get("asset_binding_mode") == "raw_source_regression" for row in entities
            ),
            "note": "Non-callable objects and source stage features are retained only as explicitly marked internal regression fixtures.",
        },
        "reconstruction_report": report,
    }
    return spec, report


def export_reconstruction_specs(
    catalog_path: str | Path,
    support_relations_path: str | Path,
    output_dir: str | Path,
    *,
    scene_slugs: list[str] | None = None,
) -> dict[str, Any]:
    objects, scenes = _load_catalog(catalog_path)
    relations = _load_relations(support_relations_path)
    requested = sorted(set(scene_slugs or scenes))
    unknown = sorted(set(requested) - set(scenes))
    if unknown:
        raise ValueError(f"Unknown Synthesis scene slugs: {', '.join(unknown)}")
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    rows = []
    for scene_slug in requested:
        relation_scene = relations.get(scene_slug) or {}
        source_scene_path = str(relation_scene.get("scene_path") or "") or None
        local_bounds: dict[str, dict[str, list[float]]] = {}
        raw_binding_bounds: dict[str, dict[str, list[float]]] = {}
        instance_attachments: dict[str, dict[str, Any]] = {}
        scene_feature_paths: list[str] = []
        if source_scene_path:
            from pxr import Usd, UsdGeom  # type: ignore

            stage = Usd.Stage.Open(source_scene_path)
            if not stage:
                raise ValueError(f"Unable to open Synthesis source scene: {source_scene_path}")
            scene_feature_paths = [
                path for path in ("/Environment", "/Render", "/root/gs_3d")
                if stage.GetPrimAtPath(path)
            ]
            cache = UsdGeom.BBoxCache(
                Usd.TimeCode.Default(),
                [UsdGeom.Tokens.default_, UsdGeom.Tokens.render],
            )
            for instance in scenes[scene_slug]:
                prim = stage.GetPrimAtPath(str(instance.get("source_prim_path") or ""))
                if not prim:
                    continue
                instance_attachments[str(instance["instance_id"])] = _instance_attachment_evidence(
                    prim,
                    str(instance.get("model_id") or ""),
                    UsdGeom,
                )
                # ComputeLocalBound/ComputeWorldBound include the instance's own
                # transform in this USD build. ComputeUntransformedBound yields
                # the authored subtree geometry in the owner prim's local frame.
                box = cache.ComputeUntransformedBound(prim).ComputeAlignedRange()
                minimum, maximum = box.GetMin(), box.GetMax()
                local_bounds[str(instance["instance_id"])] = {
                    "min": [float(minimum[index]) for index in range(3)],
                    "max": [float(maximum[index]) for index in range(3)],
                }
                asset = objects.get(str(instance.get("model_id") or "")) or {}
                if asset.get("wrapper_status") == "structural_pass" and asset.get("wrapper_path"):
                    continue
                binding_path = str(instance.get("source_path") or asset.get("source_path") or "")
                binding_stage = Usd.Stage.Open(binding_path) if binding_path else None
                binding_root = binding_stage.GetDefaultPrim() if binding_stage else None
                if not binding_root:
                    continue
                binding_cache = UsdGeom.BBoxCache(
                    Usd.TimeCode.Default(),
                    [UsdGeom.Tokens.default_, UsdGeom.Tokens.render],
                )
                binding_box = binding_cache.ComputeUntransformedBound(binding_root).ComputeAlignedRange()
                binding_min, binding_max = binding_box.GetMin(), binding_box.GetMax()
                raw_binding_bounds[str(instance["instance_id"])] = {
                    "min": [float(binding_min[index]) for index in range(3)],
                    "max": [float(binding_max[index]) for index in range(3)],
                }
        spec, report = build_reconstruction_spec(
            scene_slug,
            scenes[scene_slug],
            objects,
            relation_scene.get("relations") or {},
            source_scene_path=source_scene_path,
            source_local_bounds=local_bounds,
            binding_local_bounds=raw_binding_bounds,
            scene_feature_paths=scene_feature_paths,
            instance_attachments=instance_attachments,
        )
        output = root / f"{scene_slug}.scene_spec.json"
        output.write_text(json.dumps(spec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        rows.append({"output_spec": str(output), **report})
    result = {
        "schema_version": "synthesis28-reconstruction-export/v1",
        "status": "pass" if len(rows) == len(requested) else "fail",
        "catalog_path": str(catalog_path),
        "support_relations_path": str(support_relations_path),
        "output_dir": str(root),
        "scene_count": len(rows),
        "source_instance_count": sum(int(row["source_instance_count"]) for row in rows),
        "exported_entity_count": sum(int(row["exported_entity_count"]) for row in rows),
        "missing_entity_count": sum(int(row["missing_entity_count"]) for row in rows),
        "verified_support_edge_count": sum(int(row["verified_support_edge_count"]) for row in rows),
        "verified_ground_edge_count": sum(int(row["verified_ground_edge_count"]) for row in rows),
        "verified_mesh_support_edge_count": sum(int(row["verified_mesh_support_edge_count"]) for row in rows),
        "verified_fixed_mount_edge_count": sum(int(row["verified_fixed_mount_edge_count"]) for row in rows),
        "pending_support_count": sum(int(row["pending_support_count"]) for row in rows),
        "demoted_cycle_edge_count": sum(int(row["demoted_cycle_edge_count"]) for row in rows),
        "raw_source_regression_entity_count": sum(
            int(row["raw_source_regression_entity_count"]) for row in rows
        ),
        "instance_opinion_entity_count": sum(
            int(row["instance_opinion_entity_count"]) for row in rows
        ),
        "scene_feature_reference_count": sum(
            int(row["scene_feature_reference_count"]) for row in rows
        ),
        "scenes": rows,
    }
    (root / "index.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def evaluate_reconstruction_scene(
    spec_path: str | Path,
    scene_path: str | Path,
    *,
    matrix_tolerance: float = 1e-8,
    bounds_tolerance_m: float = 1e-4,
    max_failures: int = 100,
) -> dict[str, Any]:
    """Compare a reconstructed scene with original Synthesis instance geometry."""
    from pxr import Usd, UsdGeom  # type: ignore

    spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
    provenance = spec.get("reconstruction_provenance") or {}
    source_scene_path = str(provenance.get("source_scene_path") or "")
    if not source_scene_path:
        raise ValueError("Reconstruction spec has no source_scene_path")
    source_stage = Usd.Stage.Open(source_scene_path)
    rebuilt_stage = Usd.Stage.Open(str(scene_path))
    if not source_stage or not rebuilt_stage:
        raise ValueError("Unable to open source or reconstructed USD stage")
    purposes = [UsdGeom.Tokens.default_, UsdGeom.Tokens.render]
    source_xforms = UsdGeom.XformCache(Usd.TimeCode.Default())
    rebuilt_xforms = UsdGeom.XformCache(Usd.TimeCode.Default())
    source_bounds = UsdGeom.BBoxCache(Usd.TimeCode.Default(), purposes)
    rebuilt_bounds = UsdGeom.BBoxCache(Usd.TimeCode.Default(), purposes)
    failures: list[dict[str, Any]] = []
    matrix_pass = 0
    bounds_pass = 0
    compared = 0

    def aligned(cache: Any, prim: Any) -> list[float]:
        box = cache.ComputeWorldBound(prim).ComputeAlignedRange()
        minimum, maximum = box.GetMin(), box.GetMax()
        return [float(minimum[i]) for i in range(3)] + [float(maximum[i]) for i in range(3)]

    for entity in spec.get("entities") or []:
        if not isinstance(entity, dict):
            continue
        source_prim = source_stage.GetPrimAtPath(str(entity.get("source_prim_path") or ""))
        rebuilt_path = str(entity.get("prim") or "")
        rebuilt_prim = rebuilt_stage.GetPrimAtPath(rebuilt_path)
        if not source_prim or not rebuilt_prim:
            if len(failures) < max_failures:
                failures.append({
                    "entity_id": entity.get("id"),
                    "reason": "missing_prim",
                    "source_valid": bool(source_prim),
                    "rebuilt_valid": bool(rebuilt_prim),
                })
            continue
        compared += 1
        rebuilt_matrix = rebuilt_xforms.GetLocalToWorldTransform(rebuilt_prim)
        expected_matrix = entity.get("xform_matrix") or []
        matrix_error = max(
            abs(float(expected_matrix[row][column]) - float(rebuilt_matrix[row][column]))
            for row in range(4) for column in range(4)
        )
        source_box = aligned(source_bounds, source_prim)
        rebuilt_box = aligned(rebuilt_bounds, rebuilt_prim)
        bounds_error = max(abs(source_box[index] - rebuilt_box[index]) for index in range(6))
        if matrix_error <= matrix_tolerance:
            matrix_pass += 1
        if bounds_error <= bounds_tolerance_m:
            bounds_pass += 1
        if matrix_error > matrix_tolerance or bounds_error > bounds_tolerance_m:
            if len(failures) < max_failures:
                failures.append({
                    "entity_id": entity.get("id"),
                    "source_prim": str(source_prim.GetPath()),
                    "rebuilt_prim": str(rebuilt_prim.GetPath()),
                    "matrix_max_abs_error": matrix_error,
                    "bounds_max_abs_error_m": bounds_error,
                    "source_bounds_m": source_box,
                    "rebuilt_bounds_m": rebuilt_box,
                })
    expected = len([row for row in spec.get("entities") or [] if isinstance(row, dict)])
    status = "pass" if compared == expected and matrix_pass == expected and bounds_pass == expected else "fail"
    return {
        "schema_version": "synthesis28-reconstruction-evaluation/v1",
        "status": status,
        "spec_path": str(spec_path),
        "source_scene_path": source_scene_path,
        "reconstructed_scene_path": str(scene_path),
        "expected_entity_count": expected,
        "compared_entity_count": compared,
        "matrix_pass_count": matrix_pass,
        "bounds_pass_count": bounds_pass,
        "matrix_tolerance": matrix_tolerance,
        "bounds_tolerance_m": bounds_tolerance_m,
        "failure_count": expected - min(matrix_pass, bounds_pass, compared),
        "failures": failures,
    }


def _normalized_usd_value(value: Any, Sdf: Any) -> tuple[Any, list[str]]:
    assets: list[str] = []
    if isinstance(value, Sdf.AssetPath):
        path = str(value.resolvedPath or value.path)
        if path:
            assets.append(path)
        return {"asset": path}, assets
    if isinstance(value, dict):
        result = {}
        for key, child in sorted(value.items(), key=lambda item: str(item[0])):
            normalized, child_assets = _normalized_usd_value(child, Sdf)
            result[str(key)] = normalized
            assets.extend(child_assets)
        return result, assets
    if isinstance(value, (str, bool, int)) or value is None:
        return value, assets
    if isinstance(value, float):
        return round(value, 12), assets
    try:
        rows = list(value)
    except TypeError:
        return str(value), assets
    normalized_rows = []
    for child in rows:
        normalized, child_assets = _normalized_usd_value(child, Sdf)
        normalized_rows.append(normalized)
        assets.extend(child_assets)
    return normalized_rows, assets


def _binding_inventory(root: Any, Usd: Any, UsdGeom: Any, UsdShade: Any, Sdf: Any) -> dict[str, Any]:
    material_cache: dict[str, tuple[str, list[str]]] = {}

    def material_signature(material: Any) -> tuple[str, list[str]]:
        if not material:
            return "__unbound__", []
        path = str(material.GetPath())
        cached = material_cache.get(path)
        if cached:
            return cached
        material_prim = material.GetPrim()
        rows = []
        assets: list[str] = []
        for prim in Usd.PrimRange(material_prim):
            if not prim.IsA(UsdShade.Shader):
                continue
            shader = UsdShade.Shader(prim)
            shader_row: dict[str, Any] = {
                "name": prim.GetName(),
                "id": str(shader.GetIdAttr().Get() or ""),
                "inputs": [],
            }
            for shader_input in sorted(shader.GetInputs(), key=lambda item: str(item.GetBaseName())):
                value, value_assets = _normalized_usd_value(shader_input.Get(), Sdf)
                assets.extend(value_assets)
                connection = shader_input.GetConnectedSource()
                connection_row = None
                if connection:
                    source_api, source_name, source_type = connection
                    connection_row = {
                        "shader": source_api.GetPrim().GetName(),
                        "name": str(source_name),
                        "type": str(source_type),
                    }
                shader_row["inputs"].append({
                    "name": str(shader_input.GetBaseName()),
                    "value": value,
                    "connection": connection_row,
                })
            rows.append(shader_row)
        rendered = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        signature = hashlib.sha256(rendered.encode("utf-8")).hexdigest()
        result = signature, sorted(assets)
        material_cache[path] = result
        return result

    bindings: Counter[str] = Counter()
    texture_assets: Counter[str] = Counter()
    mesh_count = 0
    subset_count = 0
    bound_count = 0

    def bound_material(prim: Any) -> Any:
        current = prim
        root_path = str(root.GetPath())
        while current and str(current.GetPath()).startswith(root_path):
            relationships = sorted(
                (
                    relationship for relationship in current.GetRelationships()
                    if relationship.GetName() == "material:binding"
                    or relationship.GetName().startswith("material:binding:")
                ),
                key=lambda relationship: relationship.GetName(),
            )
            for relationship in relationships:
                targets = relationship.GetTargets()
                if targets:
                    material_prim = current.GetStage().GetPrimAtPath(targets[0])
                    if material_prim and material_prim.IsA(UsdShade.Material):
                        return UsdShade.Material(material_prim)
            if current == root:
                break
            current = current.GetParent()
        return UsdShade.Material()

    for prim in Usd.PrimRange(root):
        geometry_key = None
        if prim.IsA(UsdGeom.Mesh):
            mesh = UsdGeom.Mesh(prim)
            points = mesh.GetPointsAttr().Get() or []
            faces = mesh.GetFaceVertexCountsAttr().Get() or []
            geometry_key = f"mesh:{len(points)}:{len(faces)}"
            mesh_count += 1
        elif prim.GetTypeName() == "GeomSubset":
            subset = UsdGeom.Subset(prim)
            indices = subset.GetIndicesAttr().Get() or []
            geometry_key = f"subset:{str(subset.GetElementTypeAttr().Get() or '')}:{len(indices)}"
            subset_count += 1
        if geometry_key is None:
            continue
        material = bound_material(prim)
        signature, assets = material_signature(material)
        if material:
            bound_count += 1
        bindings[f"{geometry_key}:{signature}"] += 1
        texture_assets.update(assets)
    return {
        "mesh_count": mesh_count,
        "subset_count": subset_count,
        "bound_primitive_count": bound_count,
        "binding_signatures": dict(sorted(bindings.items())),
        "texture_assets": dict(sorted(texture_assets.items())),
        "unique_material_count": len(material_cache),
    }


def evaluate_reconstruction_materials(
    spec_path: str | Path,
    scene_path: str | Path,
    *,
    max_failures: int = 100,
) -> dict[str, Any]:
    """Compare composed material bindings, shader inputs and texture assets."""
    from pxr import Sdf, Usd, UsdGeom, UsdShade  # type: ignore

    spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
    provenance = spec.get("reconstruction_provenance") or {}
    source_scene_path = str(provenance.get("source_scene_path") or "")
    if not source_scene_path:
        raise ValueError("Reconstruction spec has no source_scene_path")
    source_stage = Usd.Stage.Open(source_scene_path)
    rebuilt_stage = Usd.Stage.Open(str(scene_path))
    if not source_stage or not rebuilt_stage:
        raise ValueError("Unable to open source or reconstructed USD stage")
    failures = []
    compared = 0
    binding_pass = 0
    texture_pass = 0
    source_bound = rebuilt_bound = 0
    for entity in spec.get("entities") or []:
        if not isinstance(entity, dict):
            continue
        source_prim = source_stage.GetPrimAtPath(str(entity.get("source_prim_path") or ""))
        rebuilt_prim = rebuilt_stage.GetPrimAtPath(str(entity.get("prim") or ""))
        if not source_prim or not rebuilt_prim:
            if len(failures) < max_failures:
                failures.append({"entity_id": entity.get("id"), "reason": "missing_prim"})
            continue
        compared += 1
        source = _binding_inventory(source_prim, Usd, UsdGeom, UsdShade, Sdf)
        rebuilt = _binding_inventory(rebuilt_prim, Usd, UsdGeom, UsdShade, Sdf)
        source_bound += int(source["bound_primitive_count"])
        rebuilt_bound += int(rebuilt["bound_primitive_count"])
        bindings_match = source["binding_signatures"] == rebuilt["binding_signatures"]
        textures_match = source["texture_assets"] == rebuilt["texture_assets"]
        if bindings_match:
            binding_pass += 1
        if textures_match:
            texture_pass += 1
        if not bindings_match or not textures_match:
            if len(failures) < max_failures:
                failures.append({
                    "entity_id": entity.get("id"),
                    "semantic_name": entity.get("semantic_name"),
                    "model_id": entity.get("model_id"),
                    "bindings_match": bindings_match,
                    "textures_match": textures_match,
                    "source": source,
                    "rebuilt": rebuilt,
                })
    expected = len([row for row in spec.get("entities") or [] if isinstance(row, dict)])
    status = "pass" if compared == expected and binding_pass == expected and texture_pass == expected else "fail"
    return {
        "schema_version": "synthesis28-material-parity/v1",
        "status": status,
        "spec_path": str(spec_path),
        "source_scene_path": source_scene_path,
        "reconstructed_scene_path": str(scene_path),
        "expected_entity_count": expected,
        "compared_entity_count": compared,
        "binding_pass_count": binding_pass,
        "texture_pass_count": texture_pass,
        "source_bound_primitive_count": source_bound,
        "rebuilt_bound_primitive_count": rebuilt_bound,
        "failure_count": expected - min(compared, binding_pass, texture_pass),
        "failures": failures,
    }


def _comparison_camera_from_bounds(
    minimum: list[float],
    maximum: list[float],
) -> dict[str, Any]:
    """Return a stable oblique camera for scenes without authored viewport metadata."""
    center = [(float(minimum[index]) + float(maximum[index])) * 0.5 for index in range(3)]
    size = [max(float(maximum[index]) - float(minimum[index]), 0.0) for index in range(3)]
    radius = max(size + [1.0])
    target = [center[0], center[1], float(minimum[2]) + size[2] * 0.42]
    eye = [
        center[0] + radius * 0.85,
        center[1] - radius * 1.05,
        center[2] + radius * 0.72,
    ]
    return {
        "mode": "bounds_fallback",
        "position": eye,
        "target": target,
        "up": [0.0, 0.0, 1.0],
        "focal_length_mm": 30.0,
        "horizontal_aperture_mm": 36.0,
        "vertical_aperture_mm": 20.25,
        "bounds_m": {"min": list(map(float, minimum)), "max": list(map(float, maximum))},
    }


def _camera_metadata(stage: Any) -> dict[str, Any] | None:
    custom = stage.GetRootLayer().customLayerData or {}
    authored = (custom.get("cameraSettings") or {}).get("Perspective") or {}
    source = "cameraSettings.Perspective"
    if authored.get("position") is None or authored.get("target") is None:
        authored = custom.get("extwin_camera") or {}
        source = "extwin_camera"
    eye = authored.get("position")
    target = authored.get("target")
    if eye is None or target is None:
        return None
    eye_values = [float(value) for value in eye]
    target_values = [float(value) for value in target]
    if len(eye_values) != 3 or len(target_values) != 3:
        return None
    if not all(math.isfinite(value) for value in eye_values + target_values):
        return None
    if math.sqrt(sum((eye_values[index] - target_values[index]) ** 2 for index in range(3))) <= 1e-4:
        return None
    return {
        "mode": "source_viewport_metadata",
        "metadata_source": source,
        "position": eye_values,
        "target": target_values,
        "up": [0.0, 0.0, 1.0],
        "focal_length_mm": 30.0,
        "horizontal_aperture_mm": 36.0,
        "vertical_aperture_mm": 20.25,
    }


def _source_instance_bounds(stage: Any, spec: dict[str, Any], Usd: Any, UsdGeom: Any) -> tuple[list[float], list[float]]:
    cache = UsdGeom.BBoxCache(
        Usd.TimeCode.Default(),
        [UsdGeom.Tokens.default_, UsdGeom.Tokens.render],
    )
    rows: list[tuple[list[float], list[float]]] = []
    for entity in spec.get("entities") or []:
        if not isinstance(entity, dict):
            continue
        prim = stage.GetPrimAtPath(str(entity.get("source_prim_path") or ""))
        if not prim:
            continue
        bounds = cache.ComputeWorldBound(prim).ComputeAlignedRange()
        minimum = [float(bounds.GetMin()[index]) for index in range(3)]
        maximum = [float(bounds.GetMax()[index]) for index in range(3)]
        if all(math.isfinite(value) for value in minimum + maximum):
            rows.append((minimum, maximum))
    if not rows:
        raise ValueError("Unable to compute source instance bounds for comparison camera")
    return (
        [min(row[0][index] for row in rows) for index in range(3)],
        [max(row[1][index] for row in rows) for index in range(3)],
    )


def create_reconstruction_render_pair(
    spec_path: str | Path,
    scene_path: str | Path,
    output_dir: str | Path,
) -> dict[str, Any]:
    """Author source/reconstruction comparison layers with one identical camera.

    The layers sublayer the original stages, so source assets, materials, and
    lights stay untouched. This command only adds a regression camera.
    """
    from pxr import Gf, Sdf, Usd, UsdGeom  # type: ignore

    spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
    provenance = spec.get("reconstruction_provenance") or {}
    source_path = Path(str(provenance.get("source_scene_path") or "")).expanduser()
    rebuilt_path = Path(scene_path).expanduser()
    if not source_path.is_file():
        raise ValueError(f"Source scene is unavailable: {source_path}")
    if not rebuilt_path.is_file():
        raise ValueError(f"Reconstructed scene is unavailable: {rebuilt_path}")
    source_stage = Usd.Stage.Open(str(source_path))
    if not source_stage:
        raise ValueError(f"Unable to open source scene: {source_path}")
    camera = _camera_metadata(source_stage)
    if camera is None:
        minimum, maximum = _source_instance_bounds(source_stage, spec, Usd, UsdGeom)
        camera = _comparison_camera_from_bounds(minimum, maximum)

    output_root = Path(output_dir)
    output_root.mkdir(parents=True, exist_ok=True)
    scene_slug = str(provenance.get("scene_slug") or Path(spec_path).stem.split(".")[0])
    camera_path = "/Regression/ComparisonCamera"
    outputs: dict[str, str] = {}
    eye = Gf.Vec3d(*camera["position"])
    target = Gf.Vec3d(*camera["target"])
    distance = max(float((eye - target).GetLength()), 1.0)
    for role, input_path in (("source", source_path), ("reconstruction", rebuilt_path)):
        output = output_root / f"{scene_slug}.{role}.comparison.usda"
        stage = Usd.Stage.CreateNew(str(output))
        if not stage:
            raise ValueError(f"Unable to create comparison layer: {output}")
        stage.GetRootLayer().subLayerPaths = [str(input_path.resolve())]
        regression = UsdGeom.Xform.Define(stage, "/Regression")
        camera_prim = UsdGeom.Camera.Define(stage, camera_path)
        camera_prim.CreateFocalLengthAttr().Set(float(camera["focal_length_mm"]))
        camera_prim.CreateHorizontalApertureAttr().Set(float(camera["horizontal_aperture_mm"]))
        camera_prim.CreateVerticalApertureAttr().Set(float(camera["vertical_aperture_mm"]))
        camera_prim.CreateClippingRangeAttr().Set(
            Gf.Vec2f(max(distance * 1e-5, 0.01), max(distance * 100.0, 1000.0))
        )
        matrix = Gf.Matrix4d().SetLookAt(eye, target, Gf.Vec3d(*camera["up"])).GetInverse()
        UsdGeom.Xformable(camera_prim.GetPrim()).MakeMatrixXform().Set(matrix)
        stage.SetDefaultPrim(regression.GetPrim())
        stage.GetRootLayer().customLayerData = {
            "synthesis28Comparison": {
                "role": role,
                "sceneSlug": scene_slug,
                "inputScene": str(input_path.resolve()),
                "cameraMode": str(camera["mode"]),
                "cameraPosition": Gf.Vec3d(*camera["position"]),
                "cameraTarget": Gf.Vec3d(*camera["target"]),
            }
        }
        stage.GetRootLayer().Save()
        outputs[role] = str(output)
    return {
        "schema_version": "synthesis28-render-pair/v1",
        "status": "pass",
        "scene_slug": scene_slug,
        "spec_path": str(spec_path),
        "source_scene_path": str(source_path),
        "reconstructed_scene_path": str(rebuilt_path),
        "camera_path": camera_path,
        "camera": camera,
        "source_comparison_scene": outputs["source"],
        "reconstruction_comparison_scene": outputs["reconstruction"],
    }


def create_reference_camera_candidates(
    spec_path: str | Path,
    scene_path: str | Path,
    output_scene: str | Path,
    manifest_path: str | Path,
    *,
    azimuth_count: int = 8,
    azimuth_center_deg: float | None = None,
    azimuth_span_deg: float = 360.0,
    distance_scales: tuple[float, ...] = (0.78, 1.08),
    eye_height_offsets_m: tuple[float, ...] = (1.50, 2.10),
) -> dict[str, Any]:
    """Author a compact, deterministic indoor camera search grid."""
    from pxr import Gf, Usd, UsdGeom  # type: ignore

    if azimuth_count < 4 or azimuth_count > 32:
        raise ValueError("azimuth_count must be between 4 and 32")
    if not 0.0 < azimuth_span_deg <= 360.0:
        raise ValueError("azimuth_span_deg must be in (0, 360]")
    if not distance_scales or any(value <= 0.0 for value in distance_scales):
        raise ValueError("distance_scales must contain positive values")
    if not eye_height_offsets_m or any(value <= 0.0 for value in eye_height_offsets_m):
        raise ValueError("eye_height_offsets_m must contain positive values")
    spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
    input_path = Path(scene_path).expanduser()
    if not input_path.is_file():
        raise ValueError(f"Scene is unavailable: {input_path}")
    source_stage = Usd.Stage.Open(str(input_path))
    if not source_stage:
        raise ValueError(f"Unable to open scene: {input_path}")
    cache = UsdGeom.BBoxCache(
        Usd.TimeCode.Default(),
        [UsdGeom.Tokens.default_, UsdGeom.Tokens.render],
    )
    bounds_rows = []
    anchor_centers = []
    for entity in spec.get("entities") or []:
        if not isinstance(entity, dict):
            continue
        prim = source_stage.GetPrimAtPath(str(entity.get("source_prim_path") or ""))
        if not prim:
            prim = source_stage.GetPrimAtPath(str(entity.get("prim") or ""))
        if not prim:
            continue
        bounds = cache.ComputeWorldBound(prim).ComputeAlignedRange()
        minimum = [float(bounds.GetMin()[index]) for index in range(3)]
        maximum = [float(bounds.GetMax()[index]) for index in range(3)]
        if not all(math.isfinite(value) for value in minimum + maximum):
            continue
        bounds_rows.append((minimum, maximum))
        if entity.get("asset_scope") not in {"scene_root", "scene_layout_component", "external_feature"}:
            size = [maximum[index] - minimum[index] for index in range(3)]
            if max(size) >= 0.03:
                anchor_centers.append([(minimum[index] + maximum[index]) * 0.5 for index in range(3)])
    if not bounds_rows:
        raise ValueError("No finite entity bounds are available for camera candidates")
    scene_min = [min(row[0][index] for row in bounds_rows) for index in range(3)]
    scene_max = [max(row[1][index] for row in bounds_rows) for index in range(3)]
    if anchor_centers:
        target_xy = [median(row[index] for row in anchor_centers) for index in range(2)]
        anchor_z = median(row[2] for row in anchor_centers)
    else:
        target_xy = [(scene_min[index] + scene_max[index]) * 0.5 for index in range(2)]
        anchor_z = (scene_min[2] + scene_max[2]) * 0.5
    floor_z = 0.0 if scene_min[2] <= 0.25 <= scene_max[2] else scene_min[2]
    target_z = min(max(anchor_z, floor_z + 0.85), floor_z + 1.35)
    horizontal_extent = max(scene_max[0] - scene_min[0], scene_max[1] - scene_min[1])
    base_distance = min(max(horizontal_extent * 0.35, 3.5), 24.0)
    distances = [base_distance * value for value in distance_scales]
    eye_heights = [floor_z + value for value in eye_height_offsets_m]
    if azimuth_center_deg is None:
        azimuths = [360.0 * index / azimuth_count for index in range(azimuth_count)]
    else:
        azimuths = [
            (azimuth_center_deg + azimuth_span_deg * (index / (azimuth_count - 1) - 0.5)) % 360.0
            for index in range(azimuth_count)
        ]

    output = Path(output_scene)
    output.parent.mkdir(parents=True, exist_ok=True)
    stage = Usd.Stage.CreateNew(str(output))
    if not stage:
        raise ValueError(f"Unable to create camera candidate scene: {output}")
    stage.GetRootLayer().subLayerPaths = [str(input_path.resolve())]
    root = UsdGeom.Xform.Define(stage, "/Regression")
    cameras_root = UsdGeom.Xform.Define(stage, "/Regression/CameraCandidates")
    stage.SetDefaultPrim(root.GetPrim())
    candidates = []
    candidate_index = 0
    target = Gf.Vec3d(float(target_xy[0]), float(target_xy[1]), float(target_z))
    for height_index, eye_z in enumerate(eye_heights):
        for distance_index, distance in enumerate(distances):
            for azimuth_index, azimuth_deg in enumerate(azimuths):
                radians = math.radians(azimuth_deg)
                eye = Gf.Vec3d(
                    float(target[0]) + distance * math.cos(radians),
                    float(target[1]) + distance * math.sin(radians),
                    float(eye_z),
                )
                candidate_id = f"C{candidate_index:03d}"
                camera_path = f"/Regression/CameraCandidates/{candidate_id}"
                camera_prim = UsdGeom.Camera.Define(stage, camera_path)
                camera_prim.CreateFocalLengthAttr().Set(24.0)
                camera_prim.CreateHorizontalApertureAttr().Set(36.0)
                camera_prim.CreateVerticalApertureAttr().Set(27.0)
                camera_prim.CreateClippingRangeAttr().Set(
                    Gf.Vec2f(max(distance * 1e-5, 0.01), max(distance * 100.0, 1000.0))
                )
                matrix = Gf.Matrix4d().SetLookAt(eye, target, Gf.Vec3d(0.0, 0.0, 1.0)).GetInverse()
                UsdGeom.Xformable(camera_prim.GetPrim()).MakeMatrixXform().Set(matrix)
                candidates.append({
                    "candidate_id": candidate_id,
                    "camera_path": camera_path,
                    "render_filename": f"{candidate_id}.png",
                    "position": [float(value) for value in eye],
                    "target": [float(value) for value in target],
                    "azimuth_deg": azimuth_deg,
                    "distance_m": distance,
                    "eye_height_m": eye_z,
                    "focal_length_mm": 24.0,
                    "height_index": height_index,
                    "distance_index": distance_index,
                })
                candidate_index += 1
    stage.GetRootLayer().Save()
    manifest = {
        "schema_version": "synthesis28-camera-candidates/v1",
        "status": "pass",
        "spec_path": str(spec_path),
        "scene_path": str(input_path),
        "candidate_scene_path": str(output),
        "candidate_count": len(candidates),
        "search_basis": {
            "scene_bounds_m": {"min": scene_min, "max": scene_max},
            "anchor_center_count": len(anchor_centers),
            "target": [float(value) for value in target],
            "floor_z_m": floor_z,
            "base_distance_m": base_distance,
            "azimuth_count": azimuth_count,
            "azimuth_center_deg": azimuth_center_deg,
            "azimuth_span_deg": azimuth_span_deg,
            "azimuths_deg": azimuths,
            "distance_scales": list(distance_scales),
            "eye_height_offsets_m": list(eye_height_offsets_m),
        },
        "candidates": candidates,
    }
    manifest_output = Path(manifest_path)
    manifest_output.parent.mkdir(parents=True, exist_ok=True)
    manifest_output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {**manifest, "manifest_path": str(manifest_output)}


def rank_reference_camera_renders(
    manifest_path: str | Path,
    reference_image: str | Path,
    render_dir: str | Path,
    output_path: str | Path,
) -> dict[str, Any]:
    """Rank a rendered camera grid with the deterministic reference metric."""
    from isaac_scene.quality import compare_images

    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    root = Path(render_dir)
    rows = []
    for candidate in manifest.get("candidates") or []:
        if not isinstance(candidate, dict):
            continue
        rendered = root / str(candidate.get("render_filename") or "")
        comparison = compare_images(reference_image, rendered)
        rows.append({
            **candidate,
            "render_path": str(rendered),
            "reference_status": comparison.get("status"),
            "reference_score": float(comparison.get("score") or 0.0),
            "metrics": comparison.get("metrics") or {},
            **({"failures": comparison.get("failures")} if comparison.get("failures") else {}),
        })
    rows.sort(key=lambda row: (-row["reference_score"], row["candidate_id"]))
    best = rows[0] if rows else None
    status = "fail" if not best else "pass" if best["reference_score"] >= 0.72 else "warn"
    result = {
        "schema_version": "synthesis28-camera-ranking/v1",
        "status": status,
        "manifest_path": str(manifest_path),
        "reference_image": str(reference_image),
        "render_dir": str(root),
        "rendered_candidate_count": sum(Path(row["render_path"]).is_file() for row in rows),
        "candidate_count": len(rows),
        "best": best,
        "top_candidates": rows[:10],
        "confirmation_required": bool(best),
        "confirmation_policy": "Re-render the selected camera alone in fresh source and reconstruction sessions before promotion.",
        "limitations": [
            "The scalar score ranks camera hypotheses but does not prove object identity.",
            "Batch camera switches can retain temporal-rendering or auto-exposure state; coarse ranking is not final evidence.",
            "Promote a camera only after independent re-render, subject visibility, and occlusion review.",
        ],
    }
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {**result, "output": str(output)}
