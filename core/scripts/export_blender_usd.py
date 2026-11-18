"""Export a Blender lookdev scene without losing physical curve thickness.

Run with Blender --background --python export_blender_usd.py -- --input a.blend
--output b.usdc. Keeps the blend file untouched; exports visual USD, not physics.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

import bpy


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    parser.add_argument(
        "--render-output",
        type=Path,
        help="Optional actual Cycles lookdev image before export",
    )
    parser.add_argument(
        "--camera",
        help="Authored Blender camera object name, required with --render-output",
    )
    parser.add_argument("--samples", type=int, default=128)
    parser.add_argument("--resolution", type=int, nargs=2, default=[1280, 800])
    parser.add_argument(
        "--allow-unbaked-preview",
        action="store_true",
        help="Export unsupported graphs only as an explicitly unverified preview",
    )
    args = parser.parse_args(sys.argv[sys.argv.index("--") + 1 :])
    source, output = (
        args.input.expanduser().resolve(),
        args.output.expanduser().resolve(),
    )
    if not source.is_file() or source.suffix.lower() != ".blend":
        parser.error("--input must be an existing .blend file")
    if output == source or output.exists():
        parser.error("--output must be a new file, never the source")
    if output.suffix.lower() not in {".usd", ".usda", ".usdc"}:
        parser.error("--output must be a USD-family path")
    report_path = (args.report or output.with_suffix(".export.json")).resolve()
    if report_path in {source, output} or report_path.exists():
        parser.error("report must be a separate new file")
    output.parent.mkdir(parents=True, exist_ok=True)
    if (output.parent / "textures").exists():
        parser.error(
            "Use a fresh export directory: an existing textures/ can silently reuse unrelated maps"
        )
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    bpy.ops.wm.open_mainfile(filepath=str(source), use_scripts=False)
    scene = bpy.context.scene
    linked_files = {str(source): source_hash}
    missing_images = []
    for image in bpy.data.images:
        if image.source == "FILE" and not image.packed_file:
            path = Path(bpy.path.abspath(image.filepath)).resolve()
            if path.is_file():
                linked_files[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
            elif image.users:
                missing_images.append(image.filepath)
    unsupported = []
    supported = {
        "OUTPUT_MATERIAL",
        "BSDF_PRINCIPLED",
        "TEX_IMAGE",
        "NORMAL_MAP",
        "UVMAP",
        "TEX_COORD",
        "RGB",
        "VALUE",
        "REROUTE",
    }
    for material in bpy.data.materials:
        if not material.use_nodes or not material.users:
            continue
        for node in material.node_tree.nodes:
            if node.type not in supported and any(
                socket.is_linked for socket in node.outputs
            ):
                unsupported.append(
                    {"material": material.name, "node": node.name, "type": node.type}
                )
    if missing_images or (unsupported and not args.allow_unbaked_preview):
        report_path.write_text(
            json.dumps(
                {
                    "status": "pending",
                    "missing_images": missing_images,
                    "unsupported_nodes": unsupported,
                    "next_action": "Bake connected procedural/mixed shaders to PBR images; reconnect simple Principled inputs and retry in a new directory",
                },
                indent=2,
            )
            + "\n"
        )
        raise RuntimeError(
            "Missing textures or unbaked shader graphs; see export report"
        )
    if args.render_output:
        if (
            not args.camera
            or not scene.objects.get(args.camera)
            or scene.objects[args.camera].type != "CAMERA"
        ):
            parser.error("--render-output requires an existing --camera")
        render_output = args.render_output.resolve()
        render_report = render_output.with_suffix(".render.json")
        if (
            render_output.exists()
            or render_report.exists()
            or render_output in {source, output, report_path}
        ):
            parser.error("Use distinct new render and report paths")
        if args.samples < 1 or min(args.resolution) < 64:
            parser.error("Invalid Cycles samples/resolution")
        scene.camera = scene.objects[args.camera]
        scene.render.engine = "CYCLES"
        scene.cycles.samples = args.samples
        scene.cycles.use_denoising = True
        scene.render.resolution_x, scene.render.resolution_y = args.resolution
        scene.render.resolution_percentage = 100
        scene.render.image_settings.file_format = "PNG"
        scene.render.filepath = str(render_output)
        render_output.parent.mkdir(parents=True, exist_ok=True)
        bpy.ops.render.render(write_still=True)
        image = bpy.data.images.load(str(render_output), check_existing=False)
        import numpy as np

        pixels = np.asarray(image.pixels[:]).reshape(-1, 4)[:, :3]
        capture_ok = bool(
            np.isfinite(pixels).all() and pixels.mean() > 0.001 and pixels.std() > 0.001
        )
        render_report.write_text(
            json.dumps(
                {
                    "schema_version": "isaac-scene-blender-render/v1",
                    "status": "pass" if capture_ok else "fail",
                    "capture_verified": capture_ok,
                    "subject": {
                        "scene_path": str(source),
                        "scene_sha256": source_hash,
                        "composed_sha256": source_hash,
                    },
                    "profile": {
                        "name": "review",
                        "renderer": "Cycles",
                        "samples": args.samples,
                        "resolution": args.resolution,
                    },
                    "camera": args.camera,
                    "camera_matrix": [list(row) for row in scene.camera.matrix_world],
                    "view_transform": scene.view_settings.view_transform,
                    "exposure": scene.view_settings.exposure,
                    "dependency_hashes": linked_files,
                    "outputs": [str(render_output)],
                    "output_sha256": {
                        str(render_output): hashlib.sha256(
                            render_output.read_bytes()
                        ).hexdigest()
                    },
                },
                indent=2,
            )
            + "\n"
        )
    bpy.ops.object.select_all(action="DESELECT")
    converted = []
    for obj in scene.objects:
        # A Blender bevel is evaluated surface geometry. Exporting the control
        # spline alone can turn radius=1 into USD widths=2, losing bevel_depth.
        if not obj.hide_render and (
            obj.type == "FONT"
            or (
                obj.type == "CURVE"
                and (obj.data.bevel_depth > 0 or obj.data.bevel_object)
            )
        ):
            obj.hide_set(False)
            obj.select_set(True)
            converted.append(obj.name)
    if converted:
        bpy.context.view_layer.objects.active = scene.objects[converted[0]]
        bpy.ops.object.convert(target="MESH")
    bpy.ops.wm.usd_export(
        filepath=str(output),
        selected_objects_only=False,
        export_materials=True,
        export_textures=True,
        overwrite_textures=False,
        relative_paths=True,
        generate_preview_surface=True,
        export_uvmaps=True,
        export_normals=True,
        visible_objects_only=True,
    )
    report = {
        "status": "exported_requires_runtime_validation",
        "source": str(source),
        "source_sha256": source_hash,
        "output": str(output),
        "output_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        "blender_version": bpy.app.version_string,
        "evaluated_curve_and_text_objects": converted,
        "source_unchanged": hashlib.sha256(source.read_bytes()).hexdigest()
        == source_hash,
        "unsupported_nodes": unsupported,
        "material_transfer_verified": False,
        "external_source_files": linked_files,
        "validation_pending": [
            "USD dependencies/units",
            "same-camera runtime render",
            "collision and physics if requested",
        ],
        "lighting_policy": "preserve authored lighting; calibrate target renderer explicitly, never silently",
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {
                "output": str(output),
                "converted": len(converted),
                "source_unchanged": report["source_unchanged"],
            }
        )
    )


if __name__ == "__main__":
    main()
