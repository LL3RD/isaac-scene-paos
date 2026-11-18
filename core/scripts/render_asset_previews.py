#!/usr/bin/env python3
"""Render deterministic four-view previews for USD asset retrieval candidates."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import re
import time
from pathlib import Path
from typing import Any

from isaacsim import SimulationApp
from isaac_scene.semantic_catalog import annotate as annotate_semantic_asset
from isaac_scene.preview_manifest import asset_is_complete, load_or_create, upsert_asset, write_atomic


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--asset-list", type=Path, required=True, help="JSON array or object containing an assets array.")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--offset", type=int, default=0, help="Skip this many candidates before applying --limit.")
    parser.add_argument("--limit", type=int, default=0, help="Zero renders every candidate.")
    parser.add_argument("--resolution", type=int, default=384)
    parser.add_argument(
        "--views",
        nargs="+",
        choices=["neg_y", "pos_x", "pos_y", "neg_x", "pos_z", "neg_z"],
        default=["neg_y", "pos_x", "pos_y", "neg_x"],
    )
    parser.add_argument("--settle-frames", type=int, default=20)
    parser.add_argument("--min-output-bytes", type=int, default=4000)
    parser.add_argument("--semantic-catalog", type=Path, default=None)
    parser.add_argument(
        "--show-grid",
        action="store_true",
        help="Keep the Isaac viewport grid; hidden by default for retrieval previews.",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume an existing compatible preview_index.json and skip still-valid pass rows.",
    )
    return parser.parse_args()


def safe_id(value: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9_.-]+", "_", value).strip("_.")
    return normalized[-160:] or "asset"


def read_assets(path: Path) -> list[dict[str, Any]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(value, dict):
        value = value.get("assets", value.get("hits", value.get("result", {}).get("hits", [])))
    if not isinstance(value, list):
        raise ValueError("asset-list must be an array or contain assets/hits")
    rows: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if isinstance(item, str):
            rows.append({"asset_id": f"asset_{index:04d}", "path": item})
        elif isinstance(item, dict) and item.get("path"):
            rows.append(item)
    return rows


def main() -> int:
    args = parse_args()
    assets = read_assets(args.asset_list)
    if args.offset > 0:
        assets = assets[args.offset:]
    if args.limit > 0:
        assets = assets[: args.limit]
    args.output_dir.mkdir(parents=True, exist_ok=True)

    app = SimulationApp({
        "headless": True,
        "width": args.resolution,
        "height": args.resolution,
        "anti_aliasing": 2,
    })

    import carb
    import omni.kit.renderer_capture
    import omni.usd
    from omni.kit.viewport.utility import capture_viewport_to_file, get_active_viewport, next_viewport_frame_async
    from pxr import Gf, Sdf, Usd, UsdGeom, UsdLux

    context = omni.usd.get_context()
    def configure_viewport_background() -> None:
        if args.show_grid:
            return
        settings = carb.settings.get_settings()
        settings.set_int(
            "/persistent/app/viewport/displayOptions",
            0,
        )
        settings.set("/rtx/background/source/type", 0)
        settings.set("/rtx/background/source/color", (0.18, 0.18, 0.18))
        settings.set("/rtx/index/backgroundColor", (0.18, 0.18, 0.18))
    configure_viewport_background()
    viewport = get_active_viewport()
    if viewport is None:
        raise SystemExit("No active viewport")
    viewport.set_texture_resolution((args.resolution, args.resolution))

    def world_bounds(stage: Usd.Stage) -> tuple[list[float], list[float]]:
        cache = UsdGeom.BBoxCache(
            Usd.TimeCode.Default(),
            [UsdGeom.Tokens.default_, UsdGeom.Tokens.render, UsdGeom.Tokens.proxy],
            useExtentsHint=True,
        )
        root = stage.GetDefaultPrim()
        if not root or not root.IsValid():
            root = stage.GetPseudoRoot()
        aligned = cache.ComputeWorldBound(root).ComputeAlignedBox()
        minimum = aligned.GetMin()
        maximum = aligned.GetMax()
        values = [*minimum, *maximum]
        if not all(math.isfinite(float(value)) for value in values):
            raise RuntimeError(f"Non-finite asset bounds: {values}")
        if max(float(maximum[i] - minimum[i]) for i in range(3)) <= 1e-6:
            raise RuntimeError(f"Degenerate asset bounds: {values}")
        return [float(value) for value in minimum], [float(value) for value in maximum]

    def add_preview_rig(
        stage: Usd.Stage,
        minimum: list[float],
        maximum: list[float],
        *,
        include_ground: bool,
        light_scale: float,
        dome_scale: float,
    ) -> None:
        stage.DefinePrim("/Preview", "Xform")
        dome = UsdLux.DomeLight.Define(stage, "/Preview/DomeLight")
        dome.CreateIntensityAttr().Set(700.0 * light_scale * dome_scale)
        key = UsdLux.DistantLight.Define(stage, "/Preview/KeyLight")
        key.CreateIntensityAttr().Set(1600.0 * light_scale)
        key.CreateAngleAttr().Set(4.0)
        UsdGeom.Xformable(key.GetPrim()).AddRotateXYZOp().Set(Gf.Vec3f(45.0, -35.0, -25.0))
        fill = UsdLux.DistantLight.Define(stage, "/Preview/FillLight")
        fill.CreateIntensityAttr().Set(650.0 * light_scale)
        fill.CreateAngleAttr().Set(6.0)
        UsdGeom.Xformable(fill.GetPrim()).AddRotateXYZOp().Set(Gf.Vec3f(55.0, 145.0, 20.0))

        if include_ground:
            extent_x = max(maximum[0] - minimum[0], 0.1)
            extent_y = max(maximum[1] - minimum[1], 0.1)
            ground = UsdGeom.Cube.Define(stage, "/Preview/Ground")
            ground.CreateSizeAttr().Set(1.0)
            transform = UsdGeom.Xformable(ground.GetPrim())
            transform.AddTranslateOp().Set(Gf.Vec3d(
                (minimum[0] + maximum[0]) * 0.5,
                (minimum[1] + maximum[1]) * 0.5,
                minimum[2] - 0.0125,
            ))
            backdrop_span = max(extent_x, extent_y, maximum[2] - minimum[2]) * 8.0
            transform.AddScaleOp().Set(Gf.Vec3f(backdrop_span, backdrop_span, 0.025))

    def author_camera(stage: Usd.Stage, view: str, minimum: list[float], maximum: list[float]) -> str:
        center = [(minimum[index] + maximum[index]) * 0.5 for index in range(3)]
        size = [maximum[index] - minimum[index] for index in range(3)]
        distance = max(1.0, max(size) * 2.4)
        direction = {
            "neg_y": (0.0, -1.0, 0.0),
            "pos_x": (1.0, 0.0, 0.0),
            "pos_y": (0.0, 1.0, 0.0),
            "neg_x": (-1.0, 0.0, 0.0),
            "pos_z": (0.0, 0.0, 1.0),
            "neg_z": (0.0, 0.0, -1.0),
        }[view]
        vertical = view in {"pos_z", "neg_z"}
        eye = Gf.Vec3d(
            center[0] + direction[0] * distance,
            center[1] + direction[1] * distance,
            center[2] + direction[2] * distance + (0.0 if vertical else size[2] * 0.12),
        )
        target = Gf.Vec3d(center[0], center[1], center[2])
        path = f"/Preview/Cameras/{view}"
        camera = UsdGeom.Camera.Define(stage, path)
        camera.CreateFocalLengthAttr().Set(35.0)
        camera.CreateClippingRangeAttr().Set(Gf.Vec2f(max(0.001, distance / 1000.0), distance * 20.0))
        up = Gf.Vec3d(0.0, 1.0, 0.0) if vertical else Gf.Vec3d(0.0, 0.0, 1.0)
        matrix = Gf.Matrix4d().SetLookAt(eye, target, up).GetInverse()
        UsdGeom.Xformable(camera.GetPrim()).AddTransformOp().Set(matrix)
        return path

    async def capture(camera_path: str, output_path: Path) -> None:
        output_path.unlink(missing_ok=True)
        viewport.camera_path = Sdf.Path(camera_path)
        await next_viewport_frame_async(viewport, n_frames=8)
        helper = capture_viewport_to_file(viewport, file_path=str(output_path))
        if not await helper.wait_for_result(completion_frames=60):
            raise RuntimeError(f"Capture failed: {camera_path}")
        omni.kit.renderer_capture.acquire_renderer_capture_interface().wait_async_capture()
        await next_viewport_frame_async(viewport, n_frames=2)
        if not output_path.is_file() or output_path.stat().st_size < args.min_output_bytes:
            raise RuntimeError(f"Preview missing or too small: {output_path}")

    index_path = args.output_dir / "preview_index.json"
    manifest = load_or_create(
        index_path,
        resolution=args.resolution,
        views=args.views,
        resume=args.resume,
    )
    existing_rows = {
        str(row.get("asset_id")): row
        for row in manifest["assets"]
        if row.get("asset_id")
    }
    if args.semantic_catalog:
        if not args.semantic_catalog.is_file():
            raise SystemExit(f"Semantic catalog not found: {args.semantic_catalog}")
    failures = 0
    for index, asset in enumerate(assets, start=1):
        asset_id = str(asset.get("namespaced_id") or asset.get("asset_id") or f"asset_{index:04d}")
        path = Path(str(asset["path"]))
        output_root = args.output_dir / safe_id(asset_id)
        output_root.mkdir(parents=True, exist_ok=True)
        existing = existing_rows.get(asset_id)
        if args.resume and asset_is_complete(
            existing,
            required_views=args.views,
            min_output_bytes=args.min_output_bytes,
        ):
            print(f"ASSET_PREVIEW_SKIP {index}/{len(assets)} {asset_id} pass", flush=True)
            continue
        row: dict[str, Any] = {
            "asset_id": asset_id,
            "path": str(path),
            "semantic_name": asset.get("semantic_name"),
            "category": asset.get("category"),
            "status": "running",
            "previews": {},
        }
        upsert_asset(manifest, row)
        write_atomic(index_path, manifest)
        print(f"ASSET_PREVIEW_START {index}/{len(assets)} {asset_id}", flush=True)
        try:
            if not path.is_file():
                raise FileNotFoundError(path)
            if not context.open_stage(str(path)):
                raise RuntimeError(f"Failed to open stage: {path}")
            configure_viewport_background()
            for _ in range(args.settle_frames):
                app.update()
            stage = context.get_stage()
            minimum, maximum = world_bounds(stage)
            meters_per_unit = float(UsdGeom.GetStageMetersPerUnit(stage))
            size = [maximum[i] - minimum[i] for i in range(3)]
            row["meters_per_unit"] = meters_per_unit
            row["bbox"] = {
                "min": minimum,
                "max": maximum,
                "size": size,
                "size_meters": [value * meters_per_unit for value in size],
            }
            add_preview_rig(
                stage,
                minimum,
                maximum,
                include_ground=bool(asset.get("preview_ground", True)),
                light_scale=max(0.05, float(asset.get("preview_light_scale", 1.0))),
                dome_scale=max(0.0, float(asset.get("preview_dome_scale", 1.0))),
            )
            cameras = {view: author_camera(stage, view, minimum, maximum) for view in args.views}
            for _ in range(8):
                app.update()
            for view, camera_path in cameras.items():
                output = output_root / f"{view}.png"
                task = asyncio.ensure_future(capture(camera_path, output))
                while not task.done():
                    app.update()
                task.result()
                row["previews"][view] = str(output)
            row["status"] = "pass"
            if args.semantic_catalog:
                annotate_semantic_asset(
                    args.semantic_catalog,
                    source_path=str(path),
                    preview_index_path=str(index_path),
                    bbox=row["bbox"],
                    meters_per_unit=meters_per_unit,
                    metadata_patch={"measurement_source": "four_view_preview"},
                    rebuild_search_index=False,
                )
        except Exception as exc:
            failures += 1
            row["status"] = "fail"
            row["error"] = f"{type(exc).__name__}: {exc}"
            print(f"ASSET_PREVIEW_FAIL {asset_id} {row['error']}", flush=True)
        finally:
            upsert_asset(manifest, row)
            write_atomic(index_path, manifest)
            try:
                context.close_stage()
                for _ in range(4):
                    app.update()
            except Exception:
                pass
        print(f"ASSET_PREVIEW_DONE {asset_id} {row['status']}", flush=True)

    app.close()
    print(json.dumps({"assets": len(assets), "failed": failures, "index": str(index_path)}))
    return 2 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
