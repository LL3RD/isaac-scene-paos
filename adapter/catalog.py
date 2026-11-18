"""Read-only catalog policy. Catalogs are trusted operator data, not a sandbox.

Synthesis approvals are consumed, never changed. The general semantic catalog
requires an explicit reviewed-ID allowlist: callable alone is not object QA.
Entrypoint roots restrict accidental selection; transitive USD dependencies are
still trusted and must be audited by the operator. No source scene poses are read.
"""
from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3


def read_rows(path, sql, parameters=()):
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute(sql, parameters)]


def decoded(row, key, default):
    return json.loads(row.get(key) or json.dumps(default))


class Catalog:
    def __init__(self, config):
        self.config = config
        self.registry = config.get("assets", {})
        self.policy = config.get("catalogs", {})

    def _path(self, path, roots):
        resolved = Path(path).resolve()
        if not resolved.is_file() or not any(resolved.is_relative_to(Path(r).resolve()) for r in roots):
            raise ValueError("ASSET_UNAVAILABLE")
        return str(resolved)

    def _synthesis(self, asset_id=None):
        policy = self.policy.get("synthesis")
        if not policy:
            return []
        sql = """SELECT * FROM objects WHERE callable=1 AND qa_status='ready'
                 AND license_review_status='verified' AND allowed_use_scope=?"""
        params = [policy["allowed_use_scope"]]
        if asset_id:
            sql += " AND logical_id=?"
            params.append(asset_id)
        result = []
        for row in read_rows(policy["path"], sql + " ORDER BY model_id", params):
            scopes = set(decoded(row, "asset_scopes_json", []))
            if not scopes.intersection({"independent_object", "robot"}):
                continue
            review = decoded(row, "review_report_json", {})
            bbox = decoded(row, "bbox_m_json", {})
            result.append({"asset_id": row["logical_id"], "label": row["semantic_name"],
                "tags": decoded(row, "aliases_json", []), "canonical_class": row["canonical_class"],
                "subtype": row["subtype"], "source": "synthesis28_objects",
                "metadata": {"size_m": bbox["size"]} if bbox.get("size") else {},
                "front_axis": row["front_axis"] or ("not_applicable" if row["front_axis_status"] == "not_applicable" else "unknown"),
                "support_mode": review.get("support_mode", "unknown"),
                "provenance": {"source_asset_id": row["model_id"], "license_status": "CC-BY-NC-4.0",
                    "allowed_use_scope": row["allowed_use_scope"], "attribution": row["attribution_text"] or ""},
                "path": row["wrapper_path"], "roots": policy["entrypoint_roots"]})
        return result

    def _semantic(self, asset_id=None):
        policy = self.policy.get("semantic")
        if not policy:
            return []
        approvals = policy.get("approved_assets", {})
        ids = [asset_id] if asset_id in approvals else ([] if asset_id else list(approvals))
        result = []
        # Chunk IDs to stay below SQLite's portable parameter limit.
        for start in range(0, len(ids), 400):
            chunk = ids[start:start + 400]
            rows = read_rows(policy["path"], "SELECT * FROM semantic_assets WHERE callable=1 AND logical_id IN ("
                             + ",".join("?" for _ in chunk) + ")", chunk)
            for row in rows:
                approval = approvals[row["logical_id"]]
                if row["availability"] not in {"local", "local_verified", "native_usd_asset"}:
                    continue
                if approval.get("asset_scope") not in {"independent_object", "robot"}:
                    continue
                if row["source_library"] != approval["source_library"] or row["license_status"] != approval["license_status"]:
                    continue
                bbox = decoded(row, "bbox_json", {})
                size = bbox.get("size_meters") or bbox.get("size_m") or bbox.get("size")
                result.append({"asset_id": row["logical_id"], "label": row["semantic_name"],
                    "tags": decoded(row, "aliases_json", []), "canonical_class": row["canonical_class"],
                    "subtype": row["subtype"], "source": row["source_library"],
                    "metadata": {"size_m": size} if size else {},
                    "front_axis": row["front_axis"] or "unknown", "support_mode": approval["support_mode"],
                    "provenance": {"source_asset_id": row["source_asset_id"], "license_status": row["license_status"],
                        "allowed_use_scope": approval["allowed_use_scope"], "attribution": approval["attribution"]},
                    "path": row["source_path"], "roots": policy["entrypoint_roots"]})
        return result

    def _registry(self, asset_id=None):
        result = []
        for key, value in self.registry.items():
            if asset_id and key != asset_id:
                continue
            result.append({"asset_id": key, "label": value["label"], "tags": value.get("tags", []),
                "metadata": value.get("metadata", {}), "canonical_class": value.get("canonical_class", "object/other"),
                "subtype": value.get("subtype", ""), "source": "operator_registry",
                "front_axis": value.get("front_axis", "unknown"), "support_mode": value.get("support_mode", "unknown"),
                "provenance": value.get("provenance", {"source_asset_id": key, "license_status": "operator_reviewed",
                    "allowed_use_scope": "operator_local_review", "attribution": "Operator supplied asset"}),
                "path": value["path"], "roots": [str(Path(value["path"]).parent)],
                **({"scale": value["scale"]} if "scale" in value else {})})
        return result

    def _checked(self, row):
        row = dict(row)
        row["path"] = self._path(row["path"], row.pop("roots"))
        row["placement_supported"] = row["support_mode"] in {"floor", "surface"}
        # This module also runs in Isaac's unchanged Python (stdlib only).
        # The provider additionally validates the complete public output schema.
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.:-]{0,159}", row["asset_id"]):
            raise ValueError("INVALID_CATALOG_ID")
        if row["front_axis"] not in {"+X", "-X", "+Y", "-Y", "unknown", "not_applicable"}:
            raise ValueError("INVALID_CATALOG_AXIS")
        if row["support_mode"] not in {"floor", "surface", "ceiling", "wall", "free", "unknown"}:
            raise ValueError("INVALID_CATALOG_SUPPORT")
        json.dumps(self.public(row), allow_nan=False)
        return row

    @staticmethod
    def public(row):
        return {key: value for key, value in row.items() if key not in {"path", "scale", "roots"}}

    def resolve(self, asset_id):
        rows = self._registry(asset_id) + self._synthesis(asset_id) + self._semantic(asset_id)
        if len(rows) != 1:
            raise ValueError("ASSET_NOT_APPROVED_OR_AMBIGUOUS")
        return self._checked(rows[0])

    def search(self, arguments):
        selected = arguments.get("source", "all")
        rows = []
        for name, method in (("registry", self._registry), ("synthesis", self._synthesis), ("semantic", self._semantic)):
            if selected in {"all", name}:
                rows.extend(method())
        tokens = set(re.findall(r"\w+", arguments["query"].casefold()))
        ranked = []
        for row in rows:
            if arguments.get("canonical_class") and not row["canonical_class"].startswith(arguments["canonical_class"].rstrip("/") + "/") and row["canonical_class"] != arguments["canonical_class"].rstrip("/"):
                continue
            if arguments.get("subtype", "").casefold() not in row["subtype"].casefold():
                continue
            size = row["metadata"].get("size_m")
            if any(k in arguments for k in ("min_size_m", "max_size_m")) and not size:
                continue
            if size and (any(x < y for x, y in zip(size, arguments.get("min_size_m", [0]*3))) or
                         any(x > y for x, y in zip(size, arguments.get("max_size_m", [100]*3)))):
                continue
            text = " ".join([row["asset_id"], row["label"], row["canonical_class"], row["subtype"], *row["tags"]]).casefold()
            score = sum(token in text for token in tokens)
            if not score:
                continue
            try:
                row = self._checked(row)
            except (ValueError, OSError):
                continue
            ranked.append((score, row["asset_id"], self.public(row)))
        ranked.sort(key=lambda item: (-item[0], item[1]))
        return {"assets": [r[2] for r in ranked[:arguments.get("limit", 10)]],
                "registry_scope": "operator_approved_catalogs"}
