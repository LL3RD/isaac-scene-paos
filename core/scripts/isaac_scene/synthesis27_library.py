from __future__ import annotations

import json
import math
import sqlite3
from collections import Counter, defaultdict, deque
from pathlib import Path
from typing import Any, Iterable

from .semantic_catalog import classify_semantics, normalize_semantic_name


SCHEMA_VERSION = "synthesis27-asset-library/v2"
SOURCE_COORDINATE_FRAME = "synthesis_scene_local"
SOURCE_ROTATION_CONVENTION = "euler_degrees_source_order_unverified"
SOURCE_UNITS = "source_unknown"
DEFAULT_SEMANTIC_OVERRIDES = Path(__file__).resolve().parents[2] / "config" / "synthesis27_semantic_overrides.json"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _finite_vector(value: Any) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 3
        and all(isinstance(item, (int, float)) and math.isfinite(float(item)) for item in value)
    )


def _scope(business_type: int, *, primary_root: bool) -> str:
    if business_type == 1:
        return "independent_object"
    if business_type == 2:
        return "scene_root" if primary_root else "scene_layout_component"
    if business_type == 5:
        return "robot"
    return "unknown"


def _acquisition_index(path: Path | None) -> dict[str, dict[str, Any]]:
    if path is None or not path.is_file():
        return {}
    payload = _read_object(path)
    result: dict[str, dict[str, Any]] = {}
    for row in payload.get("assets", []):
        if not isinstance(row, dict):
            continue
        model_id = str(row.get("model_id") or "")
        if model_id:
            result[model_id] = row
    return result


def _semantic_override_index(path: Path | None) -> dict[str, dict[str, str]]:
    """Load reviewed model-ID labels without coupling them to the generic classifier.

    The file is intentionally a small declarative layer: it can correct known
    Synthesis identifiers while every unknown asset continues through the
    source-agnostic ``classify_semantics`` path.
    """
    if path is None:
        return {}
    payload = _read_object(path)
    rows = payload.get("overrides", {})
    if not isinstance(rows, dict):
        raise ValueError(f"semantic overrides must contain an object named overrides: {path}")
    result: dict[str, dict[str, str]] = {}
    required = ("canonical_class", "subtype", "semantic_name")
    for model_id, row in rows.items():
        if not isinstance(model_id, str) or not isinstance(row, dict):
            raise ValueError(f"invalid semantic override entry in {path}")
        values = {field: str(row.get(field) or "").strip() for field in required}
        if not all(values.values()):
            raise ValueError(f"semantic override for {model_id} is missing a required field")
        result[model_id] = values
    return result


def _anomaly(
    anomalies: list[dict[str, Any]],
    *,
    scene_slug: str,
    code: str,
    message: str,
    severity: str = "error",
    source_index: int | None = None,
    raw: Any = None,
) -> None:
    anomalies.append({
        "scene_slug": scene_slug,
        "code": code,
        "severity": severity,
        "message": message,
        "source_index": source_index,
        "raw": raw,
    })


def _feature_key(feature: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(feature.get("name") or ""),
        str(feature.get("url") or ""),
        str(feature.get("shadowUrl") or ""),
    )


def collect(
    manifest_path: Path,
    dataset_root: Path,
    *,
    acquisition_plan: Path | None = None,
    semantic_overrides: Path | None = DEFAULT_SEMANTIC_OVERRIDES,
) -> dict[str, Any]:
    manifest = _read_object(manifest_path)
    acquisition = _acquisition_index(acquisition_plan)
    overrides = _semantic_override_index(semantic_overrides)
    assets_state: dict[str, dict[str, Any]] = {}
    instances: list[dict[str, Any]] = []
    scenes: list[dict[str, Any]] = []
    external_features: list[dict[str, Any]] = []
    anomalies: list[dict[str, Any]] = []
    seen_instance_keys: set[tuple[str, str]] = set()

    for scene_entry in manifest.get("scenes", []):
        if not isinstance(scene_entry, dict):
            continue
        slug = str(scene_entry.get("slug") or "")
        if not slug:
            _anomaly(
                anomalies,
                scene_slug="",
                code="manifest_scene_missing_slug",
                message="manifest scene entry has no slug",
                raw=scene_entry,
            )
            continue
        info_path = dataset_root / slug / "scene_info.json"
        data_path = dataset_root / slug / "scene_data.json"
        if not info_path.is_file() or not data_path.is_file():
            _anomaly(
                anomalies,
                scene_slug=slug,
                code="scene_source_missing",
                message=f"missing {info_path.name if not info_path.is_file() else data_path.name}",
            )
            continue
        try:
            info = _read_object(info_path)
            outer = _read_object(data_path)
            scene_data_raw = outer.get("Result", {}).get("SceneDataJson")
            if not isinstance(scene_data_raw, str):
                raise ValueError("Result.SceneDataJson is not a JSON string")
            scene_data = json.loads(scene_data_raw)
            if not isinstance(scene_data, dict):
                raise ValueError("decoded SceneDataJson is not an object")
        except (json.JSONDecodeError, ValueError, TypeError) as exc:
            _anomaly(
                anomalies,
                scene_slug=slug,
                code="scene_source_invalid",
                message=str(exc),
            )
            continue

        info_models = info.get("models", [])
        ext_data = scene_data.get("extDataArr", [])
        if not isinstance(info_models, list) or not isinstance(ext_data, list):
            _anomaly(
                anomalies,
                scene_slug=slug,
                code="scene_arrays_invalid",
                message="scene_info.models and SceneDataJson.extDataArr must be arrays",
            )
            continue

        ext_queues: dict[str, deque[tuple[int, dict[str, Any]]]] = defaultdict(deque)
        non_model_ext: list[tuple[int, dict[str, Any]]] = []
        for ext_index, ext in enumerate(ext_data):
            if not isinstance(ext, dict):
                _anomaly(
                    anomalies,
                    scene_slug=slug,
                    code="ext_entry_invalid",
                    message="extDataArr entry is not an object",
                    source_index=ext_index,
                    raw=ext,
                )
                continue
            model_id = str(ext.get("modelID") or "")
            if model_id:
                ext_queues[model_id].append((ext_index, ext))
            else:
                non_model_ext.append((ext_index, ext))

        resolved_count = 0
        primary_root_assigned = False
        scene_model_ids: set[str] = set()
        for info_index, model in enumerate(info_models):
            if not isinstance(model, dict):
                _anomaly(
                    anomalies,
                    scene_slug=slug,
                    code="info_model_invalid",
                    message="scene_info model entry is not an object",
                    source_index=info_index,
                    raw=model,
                )
                continue
            model_id = str(model.get("modelID") or model.get("vaultID") or "")
            if not model_id:
                _anomaly(
                    anomalies,
                    scene_slug=slug,
                    code="info_model_missing_id",
                    message="scene_info model has neither modelID nor vaultID",
                    source_index=info_index,
                    raw=model,
                )
                continue
            queue = ext_queues.get(model_id)
            if not queue:
                _anomaly(
                    anomalies,
                    scene_slug=slug,
                    code="model_instance_unmatched",
                    message=f"no remaining extDataArr occurrence for modelID {model_id}",
                    source_index=info_index,
                    raw=model,
                )
                continue
            ext_index, ext = queue.popleft()
            id_in_scene = str(ext.get("idInScene") or "")
            if not id_in_scene:
                _anomaly(
                    anomalies,
                    scene_slug=slug,
                    code="instance_missing_id",
                    message=f"extDataArr occurrence for {model_id} has no idInScene",
                    source_index=ext_index,
                    raw=ext,
                )
                continue
            instance_key = (slug, id_in_scene)
            if instance_key in seen_instance_keys:
                _anomaly(
                    anomalies,
                    scene_slug=slug,
                    code="duplicate_instance_id",
                    message=f"duplicate idInScene {id_in_scene}",
                    source_index=ext_index,
                    raw=ext,
                )
                continue
            seen_instance_keys.add(instance_key)

            offset = model.get("offset")
            rotation = model.get("rotation")
            scale = model.get("scale")
            if not all(_finite_vector(value) for value in (offset, rotation, scale)):
                _anomaly(
                    anomalies,
                    scene_slug=slug,
                    code="invalid_source_pose",
                    message=f"model {model_id} has a missing, non-finite, or non-3D pose vector",
                    source_index=info_index,
                    raw={"offset": offset, "rotation": rotation, "scale": scale},
                )
                continue

            business_type = int(ext.get("modelBusinessType") or ext.get("data", {}).get("modelBusinessType") or 0)
            is_primary_root = business_type == 2 and not primary_root_assigned
            if is_primary_root:
                primary_root_assigned = True
            data = ext.get("data") if isinstance(ext.get("data"), dict) else {}
            name = str(model.get("name") or data.get("latestName") or model_id)
            server = str(model.get("server") or "")
            view_id = model.get("viewID")
            instance = {
                "instance_id": f"synthesis27:{slug}:{id_in_scene}",
                "scene_slug": slug,
                "id_in_scene": id_in_scene,
                "model_id": model_id,
                "logical_asset_id": f"synthesis27:{model_id}",
                "name": name,
                "business_type": business_type,
                "asset_scope": _scope(business_type, primary_root=is_primary_root),
                "is_primary_scene_root": is_primary_root,
                "offset": [float(value) for value in offset],
                "rotation": [float(value) for value in rotation],
                "scale": [float(value) for value in scale],
                "coordinate_frame": SOURCE_COORDINATE_FRAME,
                "rotation_convention": SOURCE_ROTATION_CONVENTION,
                "units": SOURCE_UNITS,
                "groups": data.get("groups"),
                "sub_type": data.get("subType"),
                "is_articulated": bool(data.get("isHasArticulus", False)),
                "server": server,
                "view_id": view_id,
                "source_info_index": info_index,
                "source_ext_index": ext_index,
            }
            instances.append(instance)
            resolved_count += 1
            scene_model_ids.add(model_id)

            state = assets_state.setdefault(model_id, {
                "names": Counter(),
                "scene_slugs": set(),
                "asset_scopes": set(),
                "business_types": set(),
                "servers": set(),
                "is_articulated": False,
                "usage_count": 0,
            })
            state["names"][name] += 1
            state["scene_slugs"].add(slug)
            state["asset_scopes"].add(instance["asset_scope"])
            state["business_types"].add(business_type)
            if server:
                state["servers"].add(server)
            state["is_articulated"] = state["is_articulated"] or instance["is_articulated"]
            state["usage_count"] += 1

        for model_id, queue in sorted(ext_queues.items()):
            while queue:
                ext_index, ext = queue.popleft()
                _anomaly(
                    anomalies,
                    scene_slug=slug,
                    code="ext_model_occurrence_unmatched",
                    message=f"unused extDataArr occurrence for modelID {model_id}",
                    source_index=ext_index,
                    raw=ext,
                )

        feature_rows = info.get("features", [])
        feature_map = {
            _feature_key(feature): feature
            for feature in feature_rows
            if isinstance(feature, dict)
        }
        for ext_index, ext in non_model_ext:
            business_type = int(ext.get("modelBusinessType") or 0)
            data = ext.get("data") if isinstance(ext.get("data"), dict) else {}
            match = next(
                (
                    feature
                    for feature in feature_map.values()
                    if (
                        str(feature.get("url") or "") == str(ext.get("isaacUrl") or "")
                        or str(feature.get("shadowUrl") or "") == str(ext.get("webUrl") or "")
                        or str(feature.get("name") or "") == str(data.get("latestName") or "")
                    )
                ),
                None,
            )
            if business_type != 3 or match is None:
                _anomaly(
                    anomalies,
                    scene_slug=slug,
                    code="unresolved_non_model_ext",
                    message="non-model extDataArr entry is not a resolvable 3DGS feature",
                    source_index=ext_index,
                    raw=ext,
                )
                continue
            offset = match.get("offset")
            rotation = match.get("rotation")
            scale = match.get("scale")
            if not all(_finite_vector(value) for value in (offset, rotation, scale)):
                _anomaly(
                    anomalies,
                    scene_slug=slug,
                    code="invalid_feature_pose",
                    message="3DGS feature has an invalid source pose",
                    source_index=ext_index,
                    raw=match,
                )
                continue
            feature_id = str(ext.get("idInScene") or "")
            external_features.append({
                "feature_id": f"synthesis27:{slug}:feature:{feature_id}",
                "scene_slug": slug,
                "id_in_scene": feature_id,
                "business_type": business_type,
                "asset_scope": "external_feature_3dgs",
                "name": str(match.get("name") or data.get("latestName") or ""),
                "gs_id": str(ext.get("gsId") or data.get("gsId") or ""),
                "isaac_url": str(ext.get("isaacUrl") or match.get("url") or ""),
                "web_url": str(ext.get("webUrl") or match.get("shadowUrl") or ""),
                "offset": [float(value) for value in offset],
                "rotation": [float(value) for value in rotation],
                "scale": [float(value) for value in scale],
                "coordinate_frame": SOURCE_COORDINATE_FRAME,
                "rotation_convention": SOURCE_ROTATION_CONVENTION,
                "units": SOURCE_UNITS,
                "source_ext_index": ext_index,
            })

        scenes.append({
            "scene_slug": slug,
            "scene_id": str(scene_entry.get("id") or ""),
            "name": str(scene_entry.get("name") or slug),
            "category": str(scene_entry.get("category") or ""),
            "scene_info_path": str(info_path),
            "scene_data_path": str(data_path),
            "reference_image": str(scene_entry.get("reference_image") or ""),
            "camera": info.get("camera"),
            "scene_setting": scene_data.get("sceneSetting"),
            "resolved_instance_count": resolved_count,
            "unique_model_count": len(scene_model_ids),
            "external_feature_count": sum(1 for row in external_features if row["scene_slug"] == slug),
        })

    assets: list[dict[str, Any]] = []
    for model_id, state in sorted(assets_state.items()):
        names: Counter[str] = state["names"]
        primary_name = sorted(names.items(), key=lambda item: (-item[1], item[0]))[0][0]
        semantic_name = normalize_semantic_name(primary_name)
        canonical_class, subtype = classify_semantics(primary_name)
        override = overrides.get(model_id)
        if override:
            semantic_name = override["semantic_name"]
            canonical_class = override["canonical_class"]
            subtype = override["subtype"]
        business_types = sorted(state["business_types"])
        if len(business_types) != 1:
            _anomaly(
                anomalies,
                scene_slug="",
                code="model_business_type_conflict",
                message=f"modelID {model_id} appears with business types {business_types}",
                raw={"model_id": model_id, "business_types": business_types},
            )
        remote = acquisition.get(model_id, {})
        remote_has_usd = bool(remote.get("has_usd", False))
        availability = str(remote.get("availability") or "remote_metadata_only")
        assets.append({
            "model_id": model_id,
            "logical_id": f"synthesis27:{model_id}",
            "semantic_name": semantic_name,
            "aliases": sorted(names),
            "canonical_class": canonical_class,
            "subtype": subtype,
            "business_type": business_types[0] if len(business_types) == 1 else None,
            "is_articulated": bool(state["is_articulated"] or remote.get("is_articulated", False)),
            "asset_scopes": sorted(state["asset_scopes"]),
            "servers": sorted(state["servers"]),
            "usage_count": int(state["usage_count"]),
            "scene_count": len(state["scene_slugs"]),
            "scene_slugs": sorted(state["scene_slugs"]),
            "remote_has_usd": remote_has_usd,
            "download_file_id": None,
            "download_record_name": None,
            "download_resolution_status": "unresolved",
            "is_open_free": None,
            "license_observed_at": None,
            "availability": availability,
            "source_path": None,
            "callable": False,
            "license_status": str(remote.get("license_status") or "verify_before_authenticated_download"),
            "acquisition_priority": remote.get("priority"),
            "acquisition_score": remote.get("score"),
            "preview_index_path": None,
            "preview_status": "pending",
            "front_axis": None,
            "front_axis_status": "pending",
            "meters_per_unit": None,
            "bbox": None,
            "up_axis": None,
            "joint_count": None,
            "articulation_root_count": None,
            "structural_validation": None,
            "insertion_smoke_status": "pending",
            "insertion_smoke_report": None,
            "qa_status": "pending_download",
        })

    instances.sort(key=lambda row: (row["scene_slug"], row["source_info_index"], row["id_in_scene"]))
    scenes.sort(key=lambda row: row["scene_slug"])
    external_features.sort(key=lambda row: (row["scene_slug"], row["source_ext_index"]))
    anomalies.sort(key=lambda row: (
        row["scene_slug"],
        row["source_index"] if row["source_index"] is not None else -1,
        row["code"],
    ))
    return {
        "schema_version": SCHEMA_VERSION,
        "assets": assets,
        "instances": instances,
        "scenes": scenes,
        "external_features": external_features,
        "anomalies": anomalies,
    }


def _write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.write_text(
        "".join(_json(row) + "\n" for row in rows),
        encoding="utf-8",
    )


def _initialize_database(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        PRAGMA foreign_keys=ON;
        CREATE TABLE library_meta (
            key TEXT PRIMARY KEY,
            value_json TEXT NOT NULL
        );
        CREATE TABLE assets (
            model_id TEXT PRIMARY KEY,
            logical_id TEXT UNIQUE NOT NULL,
            semantic_name TEXT NOT NULL,
            aliases_json TEXT NOT NULL,
            canonical_class TEXT NOT NULL,
            subtype TEXT NOT NULL,
            business_type INTEGER,
            is_articulated INTEGER NOT NULL,
            asset_scopes_json TEXT NOT NULL,
            servers_json TEXT NOT NULL,
            usage_count INTEGER NOT NULL,
            scene_count INTEGER NOT NULL,
            scene_slugs_json TEXT NOT NULL,
            remote_has_usd INTEGER NOT NULL,
            download_file_id TEXT,
            download_record_name TEXT,
            download_resolution_status TEXT NOT NULL,
            is_open_free INTEGER,
            license_observed_at TEXT,
            availability TEXT NOT NULL,
            source_path TEXT,
            callable INTEGER NOT NULL,
            license_status TEXT NOT NULL,
            acquisition_priority TEXT,
            acquisition_score REAL,
            preview_index_path TEXT,
            preview_status TEXT NOT NULL,
            front_axis TEXT,
            front_axis_status TEXT NOT NULL,
            meters_per_unit REAL,
            bbox_json TEXT,
            up_axis TEXT,
            joint_count INTEGER,
            articulation_root_count INTEGER,
            structural_validation_json TEXT,
            insertion_smoke_status TEXT NOT NULL,
            insertion_smoke_report_json TEXT,
            qa_status TEXT NOT NULL
        );
        CREATE TABLE instances (
            instance_id TEXT PRIMARY KEY,
            scene_slug TEXT NOT NULL REFERENCES scenes(scene_slug),
            id_in_scene TEXT NOT NULL,
            model_id TEXT NOT NULL REFERENCES assets(model_id),
            name TEXT NOT NULL,
            business_type INTEGER NOT NULL,
            asset_scope TEXT NOT NULL,
            is_primary_scene_root INTEGER NOT NULL,
            offset_json TEXT NOT NULL,
            rotation_json TEXT NOT NULL,
            scale_json TEXT NOT NULL,
            coordinate_frame TEXT NOT NULL,
            rotation_convention TEXT NOT NULL,
            units TEXT NOT NULL,
            groups_json TEXT,
            sub_type TEXT,
            is_articulated INTEGER NOT NULL,
            server TEXT,
            view_id_json TEXT,
            source_info_index INTEGER NOT NULL,
            source_ext_index INTEGER NOT NULL,
            UNIQUE(scene_slug, id_in_scene)
        );
        CREATE TABLE scenes (
            scene_slug TEXT PRIMARY KEY,
            scene_id TEXT,
            name TEXT NOT NULL,
            category TEXT,
            scene_info_path TEXT NOT NULL,
            scene_data_path TEXT NOT NULL,
            reference_image TEXT,
            camera_json TEXT,
            scene_setting_json TEXT,
            resolved_instance_count INTEGER NOT NULL,
            unique_model_count INTEGER NOT NULL,
            external_feature_count INTEGER NOT NULL
        );
        CREATE TABLE external_features (
            feature_id TEXT PRIMARY KEY,
            scene_slug TEXT NOT NULL REFERENCES scenes(scene_slug),
            id_in_scene TEXT,
            business_type INTEGER NOT NULL,
            asset_scope TEXT NOT NULL,
            name TEXT,
            gs_id TEXT,
            isaac_url TEXT,
            web_url TEXT,
            offset_json TEXT NOT NULL,
            rotation_json TEXT NOT NULL,
            scale_json TEXT NOT NULL,
            coordinate_frame TEXT NOT NULL,
            rotation_convention TEXT NOT NULL,
            units TEXT NOT NULL,
            source_ext_index INTEGER NOT NULL
        );
        CREATE TABLE ingest_anomalies (
            anomaly_id INTEGER PRIMARY KEY AUTOINCREMENT,
            scene_slug TEXT,
            code TEXT NOT NULL,
            severity TEXT NOT NULL,
            message TEXT NOT NULL,
            source_index INTEGER,
            raw_json TEXT
        );
        CREATE INDEX assets_semantic_idx ON assets(canonical_class, subtype, semantic_name);
        CREATE INDEX assets_availability_idx ON assets(callable, availability);
        CREATE INDEX instances_model_idx ON instances(model_id);
        CREATE INDEX instances_scene_idx ON instances(scene_slug, asset_scope);
        """
    )


def _write_database(path: Path, library: dict[str, Any]) -> None:
    if path.exists():
        path.unlink()
    connection = sqlite3.connect(path)
    _initialize_database(connection)
    connection.execute(
        "INSERT INTO library_meta(key,value_json) VALUES(?,?)",
        ("schema_version", _json(library["schema_version"])),
    )
    for scene in library["scenes"]:
        connection.execute(
            """INSERT INTO scenes VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                scene["scene_slug"], scene["scene_id"], scene["name"], scene["category"],
                scene["scene_info_path"], scene["scene_data_path"], scene["reference_image"],
                _json(scene["camera"]), _json(scene["scene_setting"]),
                scene["resolved_instance_count"], scene["unique_model_count"],
                scene["external_feature_count"],
            ),
        )
    for asset in library["assets"]:
        connection.execute(
            """INSERT INTO assets VALUES (
                   ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
               )""",
            (
                asset["model_id"], asset["logical_id"], asset["semantic_name"],
                _json(asset["aliases"]), asset["canonical_class"], asset["subtype"],
                asset["business_type"], int(asset["is_articulated"]),
                _json(asset["asset_scopes"]), _json(asset["servers"]),
                asset["usage_count"], asset["scene_count"], _json(asset["scene_slugs"]),
                int(asset["remote_has_usd"]), asset["download_file_id"],
                asset["download_record_name"], asset["download_resolution_status"],
                (
                    int(asset["is_open_free"])
                    if asset["is_open_free"] is not None else None
                ),
                asset["license_observed_at"],
                asset["availability"], asset["source_path"],
                int(asset["callable"]), asset["license_status"],
                asset["acquisition_priority"], asset["acquisition_score"],
                asset["preview_index_path"], asset["preview_status"],
                asset["front_axis"], asset["front_axis_status"], asset["meters_per_unit"],
                _json(asset["bbox"]) if asset["bbox"] is not None else None,
                asset["up_axis"], asset["joint_count"], asset["articulation_root_count"],
                _json(asset["structural_validation"])
                if asset["structural_validation"] is not None else None,
                asset["insertion_smoke_status"],
                _json(asset["insertion_smoke_report"])
                if asset["insertion_smoke_report"] is not None else None,
                asset["qa_status"],
            ),
        )
    for instance in library["instances"]:
        connection.execute(
            """INSERT INTO instances VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                instance["instance_id"], instance["scene_slug"], instance["id_in_scene"],
                instance["model_id"], instance["name"], instance["business_type"],
                instance["asset_scope"], int(instance["is_primary_scene_root"]),
                _json(instance["offset"]), _json(instance["rotation"]), _json(instance["scale"]),
                instance["coordinate_frame"], instance["rotation_convention"], instance["units"],
                _json(instance["groups"]), instance["sub_type"], int(instance["is_articulated"]),
                instance["server"], _json(instance["view_id"]),
                instance["source_info_index"], instance["source_ext_index"],
            ),
        )
    for feature in library["external_features"]:
        connection.execute(
            """INSERT INTO external_features VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                feature["feature_id"], feature["scene_slug"], feature["id_in_scene"],
                feature["business_type"], feature["asset_scope"], feature["name"],
                feature["gs_id"], feature["isaac_url"], feature["web_url"],
                _json(feature["offset"]), _json(feature["rotation"]), _json(feature["scale"]),
                feature["coordinate_frame"], feature["rotation_convention"], feature["units"],
                feature["source_ext_index"],
            ),
        )
    for anomaly in library["anomalies"]:
        connection.execute(
            """INSERT INTO ingest_anomalies(
                   scene_slug,code,severity,message,source_index,raw_json
               ) VALUES(?,?,?,?,?,?)""",
            (
                anomaly["scene_slug"], anomaly["code"], anomaly["severity"],
                anomaly["message"], anomaly["source_index"], _json(anomaly["raw"]),
            ),
        )
    connection.commit()
    connection.close()


def _taxonomy(assets: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, dict[str, Any]] = {}
    for asset in assets:
        cls = grouped.setdefault(asset["canonical_class"], {
            "canonical_class": asset["canonical_class"],
            "asset_count": 0,
            "callable_count": 0,
            "subtypes": defaultdict(int),
            "model_ids": [],
        })
        cls["asset_count"] += 1
        cls["callable_count"] += int(asset["callable"])
        cls["subtypes"][asset["subtype"]] += 1
        cls["model_ids"].append(asset["model_id"])
    rows = []
    for value in grouped.values():
        rows.append({
            **value,
            "subtypes": dict(sorted(value["subtypes"].items())),
            "model_ids": sorted(value["model_ids"]),
        })
    rows.sort(key=lambda row: row["canonical_class"])
    return {"schema_version": SCHEMA_VERSION, "classes": rows}


def _readme(summary: dict[str, Any]) -> str:
    return "\n".join([
        "# Synthesis-27 unified asset library",
        "",
        "This is a semantics-first logical library built from the 27 Synthesis reference scenes.",
        "Exact stable `modelID` is the asset key; provenance is metadata, not the taxonomy.",
        "",
        f"- Stable assets: {summary['asset_count']}",
        f"- Parsed model instances: {summary['instance_count']}",
        f"- Scene/layout roots and components: {summary['layout_instance_count']}",
        f"- Independent objects: {summary['independent_instance_count']}",
        f"- Robots: {summary['robot_instance_count']}",
        f"- External 3DGS features: {summary['external_feature_count']}",
        f"- Remote USD declared: {summary['remote_usd_declared_count']}",
        f"- Locally callable and validated: {summary['callable_count']}",
        f"- Ingest errors: {summary['error_count']}",
        "",
        "## Important invariants",
        "",
        "- `remote_has_usd=true` does not make an asset callable.",
        "- `source_path` remains null until the individual package is downloaded and normalized.",
        "- Source poses are preserved in the Synthesis scene-local frame; units and Euler order are unverified.",
        "- Default generation retrieval should use `independent_object` and `robot`; roots/layout require explicit scope.",
        "- Assets with the same display name but different `modelID` remain distinct.",
        "",
        "## Files",
        "",
        "- `synthesis27_assets.sqlite`: authoritative queryable library.",
        "- `assets.json` / `assets.jsonl`: stable model inventory.",
        "- `instances.jsonl`: original scene usages and source poses.",
        "- `scenes.json`: source scene, camera and render-setting metadata.",
        "- `external_features.jsonl`: non-model 3DGS features.",
        "- `taxonomy.json`: semantic-first class view.",
        "- `availability_report.json`: logical versus callable coverage.",
        "",
    ])


def _preserve_existing_asset_state(
    database_path: Path,
    assets: list[dict[str, Any]],
) -> None:
    """Carry verified acquisition/QA state across deterministic source rebuilds."""
    if not database_path.is_file():
        return
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    existing_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(assets)")
    }
    state_columns = [
        "download_file_id",
        "download_record_name",
        "download_resolution_status",
        "is_open_free",
        "license_observed_at",
        "availability",
        "source_path",
        "callable",
        "license_status",
        "preview_index_path",
        "preview_status",
        "front_axis",
        "front_axis_status",
        "meters_per_unit",
        "bbox_json",
        "up_axis",
        "joint_count",
        "articulation_root_count",
        "structural_validation_json",
        "insertion_smoke_status",
        "insertion_smoke_report_json",
        "qa_status",
    ]
    selected = [name for name in state_columns if name in existing_columns]
    if not selected:
        connection.close()
        return
    rows = {
        str(row["model_id"]): dict(row)
        for row in connection.execute(
            "SELECT model_id," + ",".join(selected) + " FROM assets"
        )
    }
    connection.close()
    json_fields = {
        "bbox_json": "bbox",
        "structural_validation_json": "structural_validation",
        "insertion_smoke_report_json": "insertion_smoke_report",
    }
    for asset in assets:
        previous = rows.get(asset["model_id"])
        if not previous:
            continue
        for name in selected:
            value = previous.get(name)
            if name in json_fields:
                if value is not None:
                    asset[json_fields[name]] = json.loads(value)
                continue
            if name in {"callable", "is_open_free"}:
                asset[name] = bool(value) if value is not None else None
            else:
                asset[name] = value


def write_library(output_dir: Path, library: dict[str, Any]) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    database_path = output_dir / "synthesis27_assets.sqlite"
    _preserve_existing_asset_state(database_path, library["assets"])
    _write_database(database_path, library)
    (output_dir / "assets.json").write_text(
        json.dumps(library["assets"], ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    _write_jsonl(output_dir / "assets.jsonl", library["assets"])
    _write_jsonl(output_dir / "instances.jsonl", library["instances"])
    _write_jsonl(output_dir / "external_features.jsonl", library["external_features"])
    (output_dir / "scenes.json").write_text(
        json.dumps(library["scenes"], ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (output_dir / "taxonomy.json").write_text(
        json.dumps(_taxonomy(library["assets"]), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    counts = Counter(instance["asset_scope"] for instance in library["instances"])
    error_count = sum(1 for row in library["anomalies"] if row["severity"] == "error")
    summary = {
        "schema_version": SCHEMA_VERSION,
        "scene_count": len(library["scenes"]),
        "asset_count": len(library["assets"]),
        "instance_count": len(library["instances"]),
        "layout_instance_count": counts["scene_root"] + counts["scene_layout_component"],
        "independent_instance_count": counts["independent_object"],
        "robot_instance_count": counts["robot"],
        "external_feature_count": len(library["external_features"]),
        "remote_usd_declared_count": sum(1 for row in library["assets"] if row["remote_has_usd"]),
        "callable_count": sum(1 for row in library["assets"] if row["callable"]),
        "metadata_only_count": sum(1 for row in library["assets"] if not row["callable"]),
        "anomaly_count": len(library["anomalies"]),
        "error_count": error_count,
        "scope_counts": dict(sorted(counts.items())),
        "availability_counts": dict(sorted(Counter(
            row["availability"] for row in library["assets"]
        ).items())),
        "logical_coverage": 1.0 if library["assets"] else 0.0,
        "callable_coverage": (
            sum(1 for row in library["assets"] if row["callable"]) / len(library["assets"])
            if library["assets"] else 0.0
        ),
    }
    (output_dir / "availability_report.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (output_dir / "ingest_anomalies.json").write_text(
        json.dumps(library["anomalies"], ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (output_dir / "README.md").write_text(_readme(summary), encoding="utf-8")
    return summary
