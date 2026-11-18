from __future__ import annotations

import json
import math
import sqlite3
from array import array
from pathlib import Path
from typing import Any, Iterable


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        PRAGMA journal_mode=DELETE;
        CREATE TABLE IF NOT EXISTS visual_assets (
            asset_id TEXT PRIMARY KEY,
            source_path TEXT NOT NULL,
            semantic_name TEXT,
            category TEXT,
            preview_index_path TEXT NOT NULL,
            bbox_json TEXT,
            metadata_json TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS visual_assets_source_path_idx
            ON visual_assets(source_path);
        CREATE TABLE IF NOT EXISTS visual_views (
            asset_id TEXT NOT NULL,
            view_name TEXT NOT NULL,
            preview_path TEXT NOT NULL,
            model_id TEXT NOT NULL,
            embedding_dim INTEGER NOT NULL,
            embedding_f32 BLOB NOT NULL,
            PRIMARY KEY(asset_id, view_name, model_id),
            FOREIGN KEY(asset_id) REFERENCES visual_assets(asset_id)
        );
        CREATE INDEX IF NOT EXISTS visual_views_model_idx
            ON visual_views(model_id, asset_id);
        """
    )


def encode_embedding(values: Iterable[float]) -> bytes:
    return array("f", (float(value) for value in values)).tobytes()


def decode_embedding(blob: bytes, expected_dim: int | None = None) -> list[float]:
    values = array("f")
    values.frombytes(blob)
    if expected_dim is not None and len(values) != expected_dim:
        raise ValueError(f"embedding dimension mismatch: expected {expected_dim}, got {len(values)}")
    return list(values)


def normalize_embedding(values: Iterable[float]) -> list[float]:
    vector = [float(value) for value in values]
    norm = math.sqrt(sum(value * value for value in vector))
    if norm <= 1e-12:
        raise ValueError("zero visual embedding")
    return [value / norm for value in vector]


def upsert_asset(
    connection: sqlite3.Connection,
    *,
    asset_id: str,
    source_path: str,
    semantic_name: str | None,
    category: str | None,
    preview_index_path: str,
    bbox: dict[str, Any] | None,
    metadata: dict[str, Any] | None = None,
) -> None:
    connection.execute(
        """INSERT INTO visual_assets(
               asset_id, source_path, semantic_name, category,
               preview_index_path, bbox_json, metadata_json
           ) VALUES(?,?,?,?,?,?,?)
           ON CONFLICT(asset_id) DO UPDATE SET
               source_path=excluded.source_path,
               semantic_name=excluded.semantic_name,
               category=excluded.category,
               preview_index_path=excluded.preview_index_path,
               bbox_json=excluded.bbox_json,
               metadata_json=excluded.metadata_json""",
        (
            asset_id,
            source_path,
            semantic_name,
            category,
            preview_index_path,
            json.dumps(bbox, ensure_ascii=False, sort_keys=True) if bbox else None,
            json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True),
        ),
    )


def upsert_view(
    connection: sqlite3.Connection,
    *,
    asset_id: str,
    view_name: str,
    preview_path: str,
    model_id: str,
    embedding: Iterable[float],
) -> None:
    normalized = normalize_embedding(embedding)
    connection.execute(
        """INSERT INTO visual_views(
               asset_id, view_name, preview_path, model_id,
               embedding_dim, embedding_f32
           ) VALUES(?,?,?,?,?,?)
           ON CONFLICT(asset_id, view_name, model_id) DO UPDATE SET
               preview_path=excluded.preview_path,
               embedding_dim=excluded.embedding_dim,
               embedding_f32=excluded.embedding_f32""",
        (
            asset_id,
            view_name,
            preview_path,
            model_id,
            len(normalized),
            encode_embedding(normalized),
        ),
    )


def search_embeddings(
    index_path: str | Path,
    *,
    model_id: str,
    query_embeddings: dict[str, Iterable[float]],
    weights: dict[str, float],
    topk: int = 20,
    candidate_paths: set[str] | None = None,
    preferred_view_by_path: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    normalized_queries = {
        name: normalize_embedding(values)
        for name, values in query_embeddings.items()
    }
    positive_weights = {
        name: max(0.0, float(weights.get(name, 0.0)))
        for name in normalized_queries
    }
    denominator = sum(positive_weights.values())
    if denominator <= 0:
        raise ValueError("at least one positive visual-search weight is required")
    connection = sqlite3.connect(f"file:{Path(index_path)}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    rows = connection.execute(
        """SELECT a.asset_id, a.source_path, a.semantic_name, a.category,
                  a.preview_index_path, a.bbox_json,
                  v.view_name, v.preview_path, v.embedding_dim, v.embedding_f32
           FROM visual_views v JOIN visual_assets a ON a.asset_id=v.asset_id
           WHERE v.model_id=?""",
        (model_id,),
    )
    by_asset: dict[str, dict[str, Any]] = {}
    for row in rows:
        source_path = str(row["source_path"])
        if candidate_paths is not None and source_path not in candidate_paths:
            continue
        preferred_view = (
            preferred_view_by_path.get(source_path)
            if preferred_view_by_path
            else None
        )
        if preferred_view and str(row["view_name"]) != preferred_view:
            continue
        candidate = decode_embedding(row["embedding_f32"], int(row["embedding_dim"]))
        component_scores = {
            name: sum(a * b for a, b in zip(candidate, query))
            for name, query in normalized_queries.items()
        }
        combined = sum(
            positive_weights[name] * component_scores[name]
            for name in normalized_queries
        ) / denominator
        current = by_asset.get(str(row["asset_id"]))
        if current is None or combined > current["score"]:
            by_asset[str(row["asset_id"])] = {
                "asset_id": row["asset_id"],
                "path": source_path,
                "semantic_name": row["semantic_name"],
                "category": row["category"],
                "preview_index_path": row["preview_index_path"],
                "bbox": json.loads(row["bbox_json"]) if row["bbox_json"] else None,
                "score": round(float(combined), 6),
                "best_view": row["view_name"],
                "view_policy": "verified_front" if preferred_view else "best_available",
                "preview": row["preview_path"],
                "components": {
                    name: round(float(score), 6)
                    for name, score in component_scores.items()
                },
            }
    connection.close()
    return sorted(
        by_asset.values(),
        key=lambda item: (-item["score"], item["asset_id"]),
    )[: max(1, min(topk, 200))]
