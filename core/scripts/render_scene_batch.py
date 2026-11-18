#!/usr/bin/env python3
"""Render fixed-camera stills from multiple USD scenes in one Isaac session."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

from isaacsim import SimulationApp

from isaac_scene.render_feedback import analyze_image
from isaac_scene.render_profiles import PROFILE_NAMES, path_tracing_settings, resolve_render_profile
from isaac_scene.output_policy import new_output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--render-profile", choices=PROFILE_NAMES, default="review")
    parser.add_argument("--single-gpu", action="store_true")
    parser.add_argument("--kit-portable-root", type=Path)
    parser.add_argument("--max-black-frame-retries", type=int, default=2)
    parser.add_argument("--black-frame-extra-settle-frames", type=int, default=192)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()

    payload = json.loads(args.manifest.read_text(encoding="utf-8"))
    jobs = payload.get("jobs") or []
    if not jobs:
        parser.error("manifest.jobs must not be empty")
    outputs = [new_output(row["output"]) for row in jobs]
    if args.report:
        outputs.append(new_output(args.report))
    if len(outputs) != len(set(outputs)):
        parser.error("Use a distinct new output for every job and report")

    profile_path = Path(__file__).resolve().parents[1] / "config" / "render_profiles.json"
    profile = resolve_render_profile(profile_path, args.render_profile)
    app_config = {
        "headless": True,
        "width": int(profile["width"]),
        "height": int(profile["height"]),
        "anti_aliasing": int(profile["anti_aliasing"]),
        "renderer": profile["renderer"],
    }
    if args.single_gpu:
        app_config.update({"multi_gpu": False, "active_gpu": 0, "physics_gpu": 0})
    if args.kit_portable_root:
        args.kit_portable_root.mkdir(parents=True, exist_ok=True)
        sys.argv = [sys.argv[0], "--portable-root", str(args.kit_portable_root)]

    app = SimulationApp(app_config)
    started_at = time.monotonic()
    import carb
    import omni.kit.renderer_capture
    import omni.usd
    from omni.kit.viewport.utility import (
        capture_viewport_to_file,
        get_active_viewport,
        next_viewport_frame_async,
    )
    from pxr import Sdf

    settings = carb.settings.get_settings()
    settings.set_int("/persistent/app/viewport/displayOptions", 0)
    for setting_path, value in path_tracing_settings(profile).items():
        if isinstance(value, bool):
            settings.set_bool(setting_path, value)
        else:
            settings.set_int(setting_path, value)

    context = omni.usd.get_context()
    viewport = get_active_viewport()
    if viewport is None:
        raise SystemExit("No active viewport")
    viewport.set_texture_resolution((int(profile["width"]), int(profile["height"])))

    async def capture(camera_path: str, output: Path) -> None:
        viewport.camera_path = Sdf.Path(camera_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        await next_viewport_frame_async(viewport, n_frames=int(profile["camera_settle_frames"]))
        helper = capture_viewport_to_file(viewport, file_path=str(output))
        if not await helper.wait_for_result(completion_frames=180):
            raise RuntimeError("viewport capture failed")
        omni.kit.renderer_capture.acquire_renderer_capture_interface().wait_async_capture()
        await next_viewport_frame_async(viewport, n_frames=2)

    results: list[dict[str, object]] = []
    for index, row in enumerate(jobs):
        scene = Path(row["scene"]).resolve()
        camera_path = str(row["camera"])
        output = Path(row["output"]).resolve()
        print(f"BATCH_OPEN {index + 1}/{len(jobs)} {scene}", flush=True)
        if not context.open_stage(str(scene)):
            raise SystemExit(f"Failed to open scene: {scene}")
        for _ in range(int(profile["settle_frames"])):
            app.update()
        stage = context.get_stage()
        camera = stage.GetPrimAtPath(camera_path)
        if not camera or camera.GetTypeName() != "Camera":
            raise SystemExit(f"Camera not found: {camera_path} in {scene}")

        attempts = 0
        feedback: dict[str, object] = {}
        for attempt in range(max(args.max_black_frame_retries, 0) + 1):
            if attempt:
                for _ in range(max(args.black_frame_extra_settle_frames, 1)):
                    app.update()
            task = asyncio.ensure_future(capture(camera_path, output))
            while not task.done():
                app.update()
            task.result()
            attempts = attempt + 1
            feedback = analyze_image(str(output), view_kind="perspective")
            failure_codes = {str(x.get("code")) for x in feedback.get("failures") or []}
            if "BLACK_OR_EMPTY_RENDER" not in failure_codes:
                break
        else:
            raise SystemExit(f"Render remained black/empty: {output}")
        print(f"BATCH_DONE {output}", flush=True)
        results.append(
            {
                "scene": str(scene),
                "camera": camera_path,
                "output": str(output),
                "attempts": attempts,
                "feedback_status": feedback.get("status"),
            }
        )

    report = {
        "schema_version": "isaac-scene-batch-render-report/v1",
        "profile": profile,
        "jobs": results,
        "elapsed_seconds": round(time.monotonic() - started_at, 3),
    }
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    app.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
