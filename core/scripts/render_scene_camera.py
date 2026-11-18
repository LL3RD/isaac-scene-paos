#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import inspect
import sys
import time
from pathlib import Path

from isaac_scene.usd_exchange import scene_identity, sha256
from isaac_scene.output_policy import new_output

from isaac_scene.render_feedback import analyze_image
from isaac_scene.render_profiles import PROFILE_NAMES, path_tracing_settings, resolve_render_profile


def main() -> int:
    parser = argparse.ArgumentParser(description="Render a deterministic still from an authored USD camera.")
    parser.add_argument("--scene", type=Path, required=True)
    parser.add_argument("--camera", action="append", default=[])
    parser.add_argument("--output", type=Path, action="append", default=[])
    parser.add_argument(
        "--manifest",
        type=Path,
        help="Camera-candidate manifest; renders every candidate in one Isaac session.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Required with --manifest; candidate filenames come from the manifest.",
    )
    parser.add_argument(
        "--render-profile",
        choices=PROFILE_NAMES,
        default="review",
        help="preview=fast iteration, review=stable Agent review, final=Path Tracing evidence.",
    )
    parser.add_argument("--width", type=int, default=None, help="Override the selected profile width.")
    parser.add_argument("--height", type=int, default=None, help="Override the selected profile height.")
    parser.add_argument(
        "--spec",
        type=Path,
        help="Solved Scene Spec used to add temporary logical instance/class labels.",
    )
    parser.add_argument(
        "--evidence-dir",
        type=Path,
        help="Write raw instance masks and pixel-visibility summaries; requires --spec.",
    )
    parser.add_argument("--evidence-rt-subframes", type=int, default=None)
    parser.add_argument("--settle-frames", type=int, default=None)
    parser.add_argument(
        "--single-gpu",
        action="store_true",
        help="Disable Kit multi-GPU initialization; useful for deterministic or cache-isolated rendering.",
    )
    parser.add_argument(
        "--camera-settle-frames",
        type=int,
        default=None,
        help="Frames to settle after each camera switch; important for auto exposure and temporal rendering.",
    )
    parser.add_argument(
        "--show-grid",
        action="store_true",
        help="Keep the Isaac viewport grid; hidden by default for final renders.",
    )
    parser.add_argument(
        "--kit-portable-root",
        type=Path,
        help=(
            "Place Kit portable state and DerivedDataCache under this local directory. "
            "Use a node-local path such as /tmp on NFS-hosted Isaac installations."
        ),
    )
    parser.add_argument(
        "--render-report",
        type=Path,
        help="Write the resolved profile, fixed scene/camera inputs, outputs, and elapsed time as JSON.",
    )
    parser.add_argument(
        "--max-black-frame-retries",
        type=int,
        default=1,
        help="Retry a structurally valid but black/empty capture after additional renderer settling.",
    )
    parser.add_argument(
        "--black-frame-extra-settle-frames",
        type=int,
        default=96,
        help="Additional app updates before retrying a cold-cache black frame.",
    )
    args = parser.parse_args()
    if args.evidence_dir and not args.spec:
        parser.error("--spec is required with --evidence-dir")

    profile_path = Path(__file__).resolve().parents[1] / "config" / "render_profiles.json"
    profile = resolve_render_profile(
        profile_path,
        args.render_profile,
        width=args.width,
        height=args.height,
        settle_frames=args.settle_frames,
        camera_settle_frames=args.camera_settle_frames,
        evidence_rt_subframes=args.evidence_rt_subframes,
    )
    args.width = int(profile["width"])
    args.height = int(profile["height"])
    args.settle_frames = int(profile["settle_frames"])
    args.camera_settle_frames = int(profile["camera_settle_frames"])
    args.evidence_rt_subframes = int(profile["evidence_rt_subframes"])

    render_jobs: list[tuple[str, Path]] = []
    if args.manifest:
        if not args.output_dir:
            parser.error("--output-dir is required with --manifest")
        manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
        for candidate in manifest.get("candidates") or []:
            if not isinstance(candidate, dict):
                continue
            camera_path = str(candidate.get("camera_path") or "")
            filename = str(candidate.get("render_filename") or "")
            if camera_path and filename:
                if Path(filename).name != filename or filename in {".", ".."}:
                    parser.error("render_filename must be a filename, not a path")
                render_jobs.append((camera_path, args.output_dir / filename))
        if not render_jobs:
            parser.error(f"No renderable camera candidates in {args.manifest}")
    else:
        if not args.camera or not args.output:
            parser.error("use --camera/--output or --manifest/--output-dir")
        if len(args.camera) != len(args.output):
            parser.error("--camera and --output counts must match")
        render_jobs = list(zip(args.camera, args.output))

    args.render_report = args.render_report or render_jobs[0][1].with_suffix(".render.json")
    planned = [args.render_report, *[p for _, p in render_jobs]]
    if len({p.resolve() for p in planned}) != len(planned):
        parser.error("Render outputs and report must use distinct paths")
    if args.evidence_dir:
        new_output(args.evidence_dir)
    for path in [args.render_report, *[p for _, p in render_jobs]]:
        if path.exists() or path.resolve() == args.scene.resolve():
            parser.error(f"Use new output/report paths: {path}")
    subject = {"scene_path": str(args.scene.resolve()), "scene_sha256": sha256(args.scene)}
    app_config = {
        "fast_shutdown": True,
        "headless": True,
        "width": args.width,
        "height": args.height,
        "anti_aliasing": int(profile["anti_aliasing"]),
        "renderer": "RaytracedLighting",  # Warm shader/material resources before optional PT.
    }
    if args.single_gpu:
        app_config.update({"multi_gpu": False, "active_gpu": 0, "physics_gpu": 0})
    if args.kit_portable_root:
        args.kit_portable_root.mkdir(parents=True, exist_ok=True)
        # SimulationApp forwards unknown process arguments to Kit. Rebuild the
        # argv it observes so Kit receives a two-token --portable-root option
        # instead of selecting the package-local (often NFS-backed) cache.
        sys.argv = [sys.argv[0], "--portable-root", str(args.kit_portable_root)]
    else:
        sys.argv = [sys.argv[0]]
    from isaacsim import SimulationApp
    app = None
    exit_code = 2
    try:
        app = SimulationApp(app_config)
        subject = scene_identity(args.scene)
        exit_code = render(app, args, profile, render_jobs, subject)
    except Exception as exc:
        report = {"schema_version": "isaac-scene-render-report/v1", "status": "fail",
                  "subject": subject, "profile": profile, "capture_verified": False,
                  "error": f"{type(exc).__name__}: {exc}", "outputs": []}
        args.render_report.parent.mkdir(parents=True, exist_ok=True)
        args.render_report.write_text(json.dumps(report, indent=2) + "\n")
        print(report["error"], file=sys.stderr)
    finally:
        if app is not None:
            if "exit_code" in inspect.signature(app.close).parameters:
                app.close(exit_code=exit_code)
            else:
                app.config["fast_shutdown"] = False
                app.close()
    return exit_code


def render(app, args, profile, render_jobs, subject):
    started_at = time.monotonic()
    import carb
    import numpy as np
    import omni.kit.renderer_capture
    import omni.usd
    from omni.kit.viewport.utility import capture_viewport_to_file, get_active_viewport, next_viewport_frame_async
    from pxr import Sdf, Semantics, UsdGeom
    from isaac_scene.camera_visibility import summarize_instance_visibility

    settings = carb.settings.get_settings()
    def apply_profile_settings():
        for setting_path, value in path_tracing_settings(profile).items():
            if isinstance(value, bool):
                settings.set_bool(setting_path, value)
            else:
                settings.set_int(setting_path, value)

    context = omni.usd.get_context()
    if not context.open_stage(str(args.scene)):
        raise RuntimeError(f"Failed to open scene: {args.scene}")
    if not args.show_grid:
        settings.set_int(
            "/persistent/app/viewport/displayOptions",
            0,
        )
    for _ in range(args.settle_frames):
        app.update()
    if profile["renderer"] == "PathTracing":
        settings.set_string("/rtx/rendermode", "PathTracing")
        # Kit stage/renderer initialization can overwrite previously set values.
        for _ in range(2):
            app.update()
        apply_profile_settings()
        for _ in range(args.settle_frames):
            app.update()
    stage = context.get_stage()
    tracked_entities: list[dict[str, object]] = []
    if args.evidence_dir:
        spec = json.loads(args.spec.read_text(encoding="utf-8"))
        for index, entity in enumerate(spec.get("entities") or []):
            if not isinstance(entity, dict) or entity.get("asset_scope") in {"scene_root", "scene_layout_component", "external_feature"}:
                continue
            prim_path = str(entity.get("prim") or entity.get("source_prim_path") or "")
            prim = stage.GetPrimAtPath(prim_path)
            if not prim or not prim.IsValid():
                continue
            logical_id = str(entity.get("id") or prim.GetName())
            canonical_class = str(entity.get("canonical_class") or "scene_object")
            instance_semantics = Semantics.SemanticsAPI.Apply(prim, f"camera_instance_{index}")
            instance_semantics.CreateSemanticTypeAttr().Set("scene_instance")
            instance_semantics.CreateSemanticDataAttr().Set(logical_id)
            class_semantics = Semantics.SemanticsAPI.Apply(prim, f"camera_class_{index}")
            class_semantics.CreateSemanticTypeAttr().Set("scene_class")
            class_semantics.CreateSemanticDataAttr().Set(canonical_class)
            tracked_entities.append({
                "id": logical_id,
                "prim": prim_path,
                "canonical_class": canonical_class,
                "criticality": entity.get("criticality"),
                "generation_layer": entity.get("generation_layer"),
                "placement_policy": entity.get("placement_policy"),
            })
        for _ in range(4):
            app.update()
    viewport = get_active_viewport()
    if viewport is None:
        raise RuntimeError("No active viewport")
    viewport.set_texture_resolution((args.width, args.height))
    async def capture(camera_path: str, output: Path) -> None:
        camera = stage.GetPrimAtPath(camera_path)
        if not camera or not camera.IsValid() or camera.GetTypeName() != "Camera":
            raise RuntimeError(f"Camera not found: {camera_path}")
        viewport.camera_path = Sdf.Path(camera_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        await next_viewport_frame_async(viewport, n_frames=args.camera_settle_frames)
        apply_profile_settings()
        await next_viewport_frame_async(viewport, n_frames=args.camera_settle_frames)
        helper = capture_viewport_to_file(viewport, file_path=str(output))
        if not await helper.wait_for_result(completion_frames=120):
            raise RuntimeError("viewport capture failed")
        omni.kit.renderer_capture.acquire_renderer_capture_interface().wait_async_capture()
        await next_viewport_frame_async(viewport, n_frames=2)

    capture_attempts: dict[str, int] = {}
    for camera_path, output in render_jobs:
        for attempt in range(max(args.max_black_frame_retries, 0) + 1):
            if attempt:
                for _ in range(max(args.black_frame_extra_settle_frames, 1)):
                    app.update()
            task = asyncio.ensure_future(capture(camera_path, output))
            while not task.done():
                app.update()
            task.result()
            if not output.is_file() or output.stat().st_size < 4000:
                raise RuntimeError(f"Render output missing or too small: {output}")
            feedback = analyze_image(str(output), view_kind="perspective")
            failure_codes = {str(row.get("code")) for row in feedback.get("failures") or []}
            if "BLACK_OR_EMPTY_RENDER" not in failure_codes:
                capture_attempts[str(output.resolve())] = attempt + 1
                break
            if attempt >= max(args.max_black_frame_retries, 0):
                raise RuntimeError(
                    f"Render remained black/empty after {attempt + 1} capture attempt(s): {output}"
                )
            print(
                f"Black/empty capture detected; settling {args.black_frame_extra_settle_frames} "
                f"additional frames before retry {attempt + 2}: {output}",
                file=sys.stderr,
            )
        print(output)
        if args.evidence_dir:
            import omni.replicator.core as rep

            evidence_root = args.evidence_dir / output.stem
            evidence_root.mkdir(parents=True, exist_ok=True)
            render_product = rep.create.render_product(
                camera_path,
                (args.width, args.height),
                name=f"CameraEvidence_{output.stem}",
            )
            annotator = rep.AnnotatorRegistry.get_annotator(
                "instance_segmentation_fast",
                init_params={"colorize": False, "semanticFilter": "scene_instance:*"},
            )
            annotator.attach(render_product)
            rep.orchestrator.step(rt_subframes=args.evidence_rt_subframes, delta_time=0.0)
            payload = annotator.get_data()
            mask = np.asarray(payload.get("data") if isinstance(payload, dict) else payload)
            info = payload.get("info", {}) if isinstance(payload, dict) else {}
            mapping = {str(key): value for key, value in (info.get("idToLabels") or {}).items()}
            np.save(evidence_root / "instance.npy", mask)
            (evidence_root / "instance_mapping.json").write_text(
                json.dumps({"idToLabels": mapping, "idToSemantics": info.get("idToSemantics") or {}}, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            summary = summarize_instance_visibility(mask, mapping, tracked_entities)
            summary.update({
                "camera_path": camera_path,
                "render_path": str(output.resolve()),
                "instance_mask_path": str((evidence_root / "instance.npy").resolve()),
                "mapping_path": str((evidence_root / "instance_mapping.json").resolve()),
                "temporary_semantics": True,
            })
            (evidence_root / "visibility.json").write_text(
                json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            annotator.detach()
            render_product.destroy()
            print(evidence_root / "visibility.json")
    if scene_identity(args.scene)["composed_sha256"] != subject["composed_sha256"]:
        raise RuntimeError("Scene dependencies changed during rendering")
    requested_settings = path_tracing_settings(profile)
    effective_settings = {key: settings.get(key) for key in requested_settings}
    if effective_settings != requested_settings:
        raise RuntimeError(f"Renderer reset requested settings: {effective_settings}; expected {requested_settings}")
    if args.render_report:
        scene_path = args.scene.resolve()
        scene_fingerprint = hashlib.sha256(scene_path.read_bytes()).hexdigest()
        report = {
            "schema_version": "isaac-scene-render-report/v1",
            "status": "pass",
            "subject": subject,
            "capture_verified": True,
            "profile_verified": True,
            "backend": "fresh Isaac process; RT warmup then requested renderer",
            "profile": profile,
            "effective_path_tracing": effective_settings,
            "camera_matrices": {camera: [list(row) for row in UsdGeom.Xformable(stage.GetPrimAtPath(camera)).ComputeLocalToWorldTransform(0)]
                                for camera, _ in render_jobs},
            "output_sha256": {str(output.resolve()): sha256(output) for _, output in render_jobs},
            "scene_path": str(scene_path),
            "scene_sha256": scene_fingerprint,
            "fixed_camera_paths": [camera for camera, _ in render_jobs],
            "outputs": [str(output.resolve()) for _, output in render_jobs],
            "capture_attempts": capture_attempts,
            "elapsed_seconds": round(time.monotonic() - started_at, 3),
        }
        args.render_report.parent.mkdir(parents=True, exist_ok=True)
        args.render_report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(args.render_report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
