#!/usr/bin/env python3
"""Bounded held-pose smoke test, not a manipulation-task certification.

Counts actual PhysX steps, samples peak drift at app updates and reads the run's
physics log. Never saves the played stage or disables existing physics.
"""

from __future__ import annotations

import argparse
import json
import inspect
import math
import sys
import tempfile
import time
from pathlib import Path


def pose_delta(before, after, meters_per_unit=1.0):
    if not all(
        math.isfinite(v) for v in [*before[0], *before[1], *after[0], *after[1]]
    ):
        return None, None
    distance = math.dist(before[0], after[0]) * meters_per_unit
    dot = abs(sum(x * y for x, y in zip(before[1], after[1])))
    angle = math.degrees(2 * math.acos(max(-1.0, min(1.0, dot))))
    return distance, angle


def physics_messages(path):
    if not path.is_file():
        return None
    return [
        line.strip()
        for line in path.read_text(errors="replace").splitlines()
        if ("[Warning]" in line or "[Error]" in line)
        and any(token in line.lower() for token in ("physx", "physics"))
    ]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("scene", type=Path)
    p.add_argument("--output", required=True, type=Path)
    p.add_argument(
        "--physics-steps",
        "--frames",
        dest="physics_steps",
        type=int,
        default=240,
        help="Actual physics steps; --frames is a compatibility alias",
    )
    p.add_argument("--settle-frames", type=int, default=30)
    p.add_argument("--max-updates", type=int, default=10000)
    p.add_argument("--timeout-seconds", type=float, default=180)
    p.add_argument("--translation-threshold-m", type=float, default=0.02)
    p.add_argument("--rotation-threshold-deg", type=float, default=2.0)
    a = p.parse_args()
    if a.physics_steps < 1 or a.max_updates < 1 or a.timeout_seconds <= 0:
        p.error("Step/update/time budgets must be positive")
    if a.output.exists() or a.output.resolve() == a.scene.resolve():
        p.error("Use a new report path, never overwrite an input")
    a.output.parent.mkdir(parents=True, exist_ok=True)
    from isaac_scene.usd_exchange import scene_identity, sha256

    # Importing pxr before SimulationApp can load an incompatible USD library.
    subject = {"scene_path": str(a.scene.resolve()), "scene_sha256": sha256(a.scene)}
    cache = Path(tempfile.mkdtemp(prefix="isaac-dynamics-"))
    log = cache / "runtime.log"
    sys.argv = [sys.argv[0], "--portable-root", str(cache), f"--/log/file={log}"]
    from isaacsim import SimulationApp

    app = None
    report = {
        "schema_version": "isaac-scene-dynamic-stability/v1",
        "status": "fail",
        "subject": subject,
        "scene": str(a.scene.resolve()),
        "physics_steps": 0,
        "rigid_body_count": 0,
        "physics_log_pass": False,
        "log_path": str(log),
        "scope": "Held-pose drift sampled at app updates; not continuous contact or task execution proof",
    }
    try:
        app = SimulationApp(
            {
                "headless": True,
                "width": 320,
                "height": 240,
                "multi_gpu": False,
                "fast_shutdown": True,
                "renderer": "RaytracedLighting",
                "disable_viewport_updates": True,
            }
        )
        subject = scene_identity(a.scene)
        report["subject"] = subject
        print("Dynamics: runtime started and source fingerprinted", flush=True)
        import carb
        import omni.physx
        import omni.timeline
        import omni.usd
        from pxr import UsdGeom, UsdPhysics

        carb.settings.get_settings().set_bool("/physics/updateToUsd", True)
        context = omni.usd.get_context()
        if not context.open_stage(str(a.scene.resolve())):
            raise RuntimeError("Could not open stage")
        print("Dynamics: stage opened", flush=True)
        for _ in range(max(1, a.settle_frames)):
            app.update()
        stage = context.get_stage()
        unit = UsdGeom.GetStageMetersPerUnit(stage)
        paths = [
            str(prim.GetPath())
            for prim in stage.Traverse()
            if prim.HasAPI(UsdPhysics.RigidBodyAPI)
            and prim.GetAttribute("physics:rigidBodyEnabled").Get() is not False
        ]

        def sample():
            cache = UsdGeom.XformCache()
            poses = {}
            for path in paths:
                prim = stage.GetPrimAtPath(path)
                if not prim or not prim.IsActive():
                    raise RuntimeError(f"Rigid body disappeared: {path}")
                matrix = cache.GetLocalToWorldTransform(prim)
                quat = matrix.ExtractRotationQuat().GetNormalized()
                poses[path] = (
                    list(matrix.ExtractTranslation()),
                    [quat.GetReal(), *quat.GetImaginary()],
                )
            return poses

        before = sample()
        print(f"Dynamics: sampling {len(paths)} rigid bodies", flush=True)
        peaks = {path: [0.0, 0.0] for path in paths}
        nonfinite = set()
        steps = [0]

        def tick(dt):
            steps[0] += 1

        subscription = omni.physx.get_physx_interface().subscribe_physics_step_events(
            tick
        )
        if subscription is None:
            raise RuntimeError("No PhysX step subscription")
        timeline = omni.timeline.get_timeline_interface()
        timeline.play()
        started = time.monotonic()
        updates = 0
        try:
            while (
                paths
                and steps[0] < a.physics_steps
                and updates < a.max_updates
                and time.monotonic() - started < a.timeout_seconds
            ):
                app.update()
                updates += 1
                for path, pose in sample().items():
                    distance, angle = pose_delta(before[path], pose, unit)
                    if distance is None:
                        nonfinite.add(path)
                    else:
                        peaks[path] = [
                            max(peaks[path][0], distance),
                            max(peaks[path][1], angle),
                        ]
        finally:
            timeline.stop()
            subscription = None
        bodies = [
            {
                "prim": path,
                "peak_translation_delta_m": v[0],
                "peak_rotation_delta_deg": v[1],
                "finite": path not in nonfinite,
                "moved": path in nonfinite
                or v[0] > a.translation_threshold_m
                or v[1] > a.rotation_threshold_deg,
            }
            for path, v in peaks.items()
        ]
        moved = [row for row in bodies if row["moved"]]
        report.update(
            physics_steps=steps[0],
            sampled_app_updates=updates,
            rigid_body_count=len(paths),
            meters_per_unit=unit,
            bodies=bodies,
            moved_bodies=moved,
            moved_body_count=len(moved),
            pose_stability_pass=bool(paths)
            and steps[0] >= a.physics_steps
            and not moved,
            thresholds={
                "translation_m": a.translation_threshold_m,
                "rotation_deg": a.rotation_threshold_deg,
            },
        )
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
    messages = physics_messages(log)
    report["physics_messages"] = messages
    report["physics_log_pass"] = messages is not None and not messages
    report["source_unchanged"] = (
        bool(subject.get("composed_sha256"))
        and scene_identity(a.scene)["composed_sha256"] == subject["composed_sha256"]
    )
    if (
        not report.get("error")
        and report.get("pose_stability_pass")
        and report["physics_log_pass"]
        and report["source_unchanged"]
    ):
        report["status"] = "pass"
    elif not report.get("error") and not report["rigid_body_count"]:
        report.update(
            status="pending", reason="No enabled rigid bodies; cannot certify dynamics"
        )
    a.output.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    print(
        json.dumps(
            {
                key: report[key]
                for key in (
                    "status",
                    "physics_steps",
                    "rigid_body_count",
                    "physics_log_pass",
                )
            }
        )
    )
    exit_code = 0 if report["status"] == "pass" else 2
    if app is not None:
        if "exit_code" in inspect.signature(app.close).parameters:
            app.close(exit_code=exit_code)
        else:
            app.config["fast_shutdown"] = False
            app.close()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
