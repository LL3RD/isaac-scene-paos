"""Input/checkpoint, USD roundtrip and negative-evidence regressions."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from isaac_scene.scene_task import initialize, check, subject_identity
from isaac_scene.usd_exchange import (
    create_revision,
    prepare_assets,
    scene_identity,
    sha256,
)
from isaac_scene.handoff import export_handoff, verify_handoff
from audit_scene_dynamics import pose_delta, physics_messages

try:
    from pxr import Usd, UsdGeom, UsdShade, Sdf
except ImportError:
    Usd = None


class TaskTests(unittest.TestCase):
    def test_cli_does_not_overwrite_existing_evidence(self):
        import contextlib
        import io
        import scene_tools

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.usda"
            source.write_text("keep this source")
            with contextlib.redirect_stdout(io.StringIO()):
                code = scene_tools.main(
                    [
                        "--envelope",
                        "--evidence-scene",
                        str(source),
                        "--report-output",
                        str(source),
                        "validate-scene",
                        str(source),
                    ]
                )
            self.assertEqual(code, 2)
            self.assertEqual(source.read_text(), "keep this source")

    def test_roundtrip_requires_both_endpoints(self):
        task = initialize(text="server", target="roundtrip")
        self.assertTrue({"blender_render", "exchange"} <= set(task["required_gates"]))
        task["required_gates"] = []
        missing = {row["gate"] for row in check(task)["pending"]}
        self.assertTrue({"blender_render", "exchange"} <= missing)

    def test_no_input_or_missing_source_rejected(self):
        with self.assertRaises(ValueError):
            initialize()
        with self.assertRaises(ValueError):
            initialize(images=["/missing.png"])
        with self.assertRaises(ValueError):
            initialize(text="factory", target="blender", scope="simulation_scene")

    def test_pending_is_not_success_and_budget_stops(self):
        task = initialize(text="four arms assembling one server")
        self.assertEqual(check(task)["status"], "pending")
        task["iteration"] = 3
        self.assertEqual(check(task)["next_action"], "stop_and_preserve_last_good")

    def test_valid_report_is_not_visual_acceptance(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            blend = root / "candidate.blend"
            blend.write_bytes(b"test-file-not-a-render")
            image = root / "render.png"
            image.write_bytes(b"hashed-fixture")
            task = initialize(text="test", target="blender")
            task["artifacts"]["blend"] = str(blend)
            subject = subject_identity(blend)
            render = {
                "schema_version": "isaac-scene-blender-render/v1",
                "subject": subject,
                "status": "pass",
                "capture_verified": True,
                "profile": {"name": "review"},
                "outputs": [str(image)],
                "output_sha256": {str(image): sha256(image)},
            }
            visual = {
                "command": "validate-visual-agent-review",
                "subject": subject,
                "review_inputs_sha256": {str(image): sha256(image)},
                "result": {"status": "pass", "verdict": "revise"},
            }

            def save():
                (root / "render.json").write_text(json.dumps(render))
                (root / "visual.json").write_text(json.dumps(visual))

            task["evidence"] = {"render": ["render.json"], "visual": ["visual.json"]}
            save()
            self.assertEqual(check(task, base_dir=root)["status"], "pending")
            visual["result"]["verdict"] = "accept"
            save()
            self.assertEqual(check(task, base_dir=root)["status"], "pass")
            render["profile"]["name"] = "final"
            save()
            self.assertEqual(check(task, base_dir=root)["status"], "pending")
            render["profile_verified"] = True
            save()
            self.assertEqual(check(task, base_dir=root)["status"], "pass")
            visual["ok"] = False
            save()
            self.assertEqual(check(task, base_dir=root)["status"], "fail")
            visual.pop("ok")
            save()
            image.write_bytes(b"changed")
            self.assertNotEqual(check(task, base_dir=root)["status"], "pass")

    def test_source_change_and_required_gates_cannot_be_waived(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.blend"
            source.write_bytes(b"original")
            task = initialize(blend=source, target="blender")
            source.write_bytes(b"changed")
            task["required_gates"] = []
            result = check(task)
            self.assertEqual(result["errors"][0]["code"], "SOURCE_CHANGED")
            self.assertTrue(result["pending"])

    def test_direct_image_review_requires_current_images_and_both_axes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            blend, image = root / "candidate.blend", root / "render.png"
            blend.write_bytes(b"fixture scene")
            image.write_bytes(b"fixture image")
            subject = subject_identity(blend)
            task = initialize(text="test", target="blender")
            task["artifacts"]["blend"] = str(blend)
            render = {"schema_version": "isaac-scene-blender-render/v1", "subject": subject,
                      "status": "pass", "capture_verified": True, "profile": {"name": "review"},
                      "outputs": [str(image)], "output_sha256": {str(image): sha256(image)}}
            visual = {"schema_version": "isaac-scene-image-review/v1", "command": "review-scene-images",
                      "subject": subject, "review_inputs_sha256": {str(image): sha256(image)},
                      "result": {"status": "pass", "verdict": "accept", "issues": [],
                                 "rendering_quality": {"score": 8, "reason": "visible"},
                                 "scene_content_quality": {"score": 7, "reason": "visible"}}}
            (root / "render.json").write_text(json.dumps(render))
            task["evidence"] = {"render": ["render.json"], "visual": ["visual.json"]}

            def status():
                (root / "visual.json").write_text(json.dumps(visual))
                return check(task, base_dir=root)["status"]

            self.assertEqual(status(), "pass")
            visual["result"]["verdict"] = "revise"
            self.assertNotEqual(status(), "pass")
            visual["result"]["verdict"] = "accept"
            axis = visual["result"].pop("rendering_quality")
            self.assertNotEqual(status(), "pass")
            visual["result"]["rendering_quality"] = axis
            visual["result"]["issues"] = [{"severity": "high"}]
            self.assertNotEqual(status(), "pass")
            visual["result"]["issues"] = []
            self.assertEqual(status(), "pass")
            image.write_bytes(b"modified after review")
            self.assertNotEqual(status(), "pass")

    def test_pose_meter_conversion_and_nonfinite(self):
        before = ([0, 0, 0], [1, 0, 0, 0])
        self.assertAlmostEqual(
            pose_delta(before, ([0, 0, 2], [1, 0, 0, 0]), 0.01)[0], 0.02
        )
        self.assertEqual(
            pose_delta(before, ([float("nan"), 0, 0], [1, 0, 0, 0])), (None, None)
        )

    def test_missing_log_is_not_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "physics.log"
            self.assertIsNone(physics_messages(log))
            log.write_text(
                "[Warning] [omni.physx] rejected joint\n[Warning] audio unavailable\n"
            )
            self.assertEqual(len(physics_messages(log)), 1)


@unittest.skipIf(Usd is None, "Requires USD Python runtime")
class USDTests(unittest.TestCase):
    def test_scene_validator_accepts_non_world_root(self):
        import scene_tools

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "blender.usda"
            stage = Usd.Stage.CreateNew(str(path))
            root = UsdGeom.Xform.Define(stage, "/root")
            stage.SetDefaultPrim(root.GetPrim())
            UsdGeom.Cube.Define(stage, "/root/Cube")
            stage.GetRootLayer().Save()
            report = scene_tools.validate_scene(str(path))
            self.assertEqual(report["status"], "pass")
            self.assertEqual(report["default_prim"], "/root")

    def test_usdz_input_and_revision(self):
        from pxr import UsdUtils

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.fixture(root)
            package = root / "source.usdz"
            self.assertTrue(UsdUtils.CreateNewUsdzPackage(str(source), str(package)))
            identity = scene_identity(package)
            self.assertEqual(list(identity["files"]), [str(package)])
            create_revision(package, root / "edited.usda")
            stage = Usd.Stage.Open(str(root / "edited.usda"))
            self.assertTrue(stage.GetPrimAtPath("/World/A/Cube"))

    def fixture(self, root):
        source = root / "source.usda"
        stage = Usd.Stage.CreateNew(str(source))
        world = UsdGeom.Xform.Define(stage, "/World")
        stage.SetDefaultPrim(world.GetPrim())
        UsdGeom.SetStageMetersPerUnit(stage, 0.01)
        UsdGeom.SetStageUpAxis(stage, "Z")
        UsdGeom.Xform.Define(stage, "/World/A")
        UsdGeom.Cube.Define(stage, "/World/A/Cube")
        UsdGeom.Cube.Define(stage, "/World/B")
        mat = UsdShade.Material.Define(stage, "/World/Looks/Metal")
        shader = UsdShade.Shader.Define(stage, "/World/Looks/Metal/Shader")
        shader.CreateIdAttr("UsdPreviewSurface")
        shader.CreateInput("roughness", Sdf.ValueTypeNames.Float).Set(0.3)
        mat.CreateSurfaceOutput().ConnectToSource(shader.ConnectableAPI(), "surface")
        UsdShade.MaterialBindingAPI.Apply(stage.GetPrimAtPath("/World/A")).Bind(mat)
        stage.GetRootLayer().Save()
        return source

    def test_revision_preserves_source_units_and_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.fixture(root)
            digest = sha256(source)
            create_revision(source, root / "edit.usda", ["/World/B"])
            edit = Usd.Stage.Open(str(root / "edit.usda"))
            self.assertEqual(UsdGeom.GetStageMetersPerUnit(edit), 0.01)
            self.assertFalse(edit.GetPrimAtPath("/World/B").IsActive())
            self.assertEqual(digest, sha256(source))
            with self.assertRaises(ValueError):
                create_revision(source, root / "bad.usda", ["/World"])
            with self.assertRaises(ValueError):
                create_revision(source, root / "edit.usda")

    def test_subtree_reference_retains_inherited_material(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.fixture(root)
            packaged = root / "portable.usda"
            result = prepare_assets(source, packaged, ["/World/A"])
            self.assertEqual(result["status"], "pass")
            stage = Usd.Stage.CreateInMemory()
            stage.DefinePrim("/OnlyAsset").GetReferences().AddReference(
                str(packaged), "/World/A"
            )
            mat, _ = UsdShade.MaterialBindingAPI(
                stage.GetPrimAtPath("/OnlyAsset/Cube")
            ).ComputeBoundMaterial()
            self.assertTrue(mat)
            self.assertTrue(mat.GetPrim().IsDefined())
            self.assertGreater(len(list(Usd.PrimRange(mat.GetPrim()))), 1)
            self.assertTrue(str(mat.GetPath()).startswith("/OnlyAsset/"))
            self.assertTrue(mat.ComputeSurfaceSource()[0])

    def test_nested_assets_and_outside_material_graph_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.fixture(root)
            with self.assertRaises(ValueError):
                prepare_assets(source, root / "bad.usda", ["/World/A", "/World/A/Cube"])
            stage = Usd.Stage.Open(str(source))
            shader = UsdShade.Shader.Define(stage, "/World/Outside")
            shader.CreateIdAttr("UsdPreviewSurface")
            mat = UsdShade.Material(stage.GetPrimAtPath("/World/Looks/Metal"))
            mat.CreateSurfaceOutput().ConnectToSource(
                shader.ConnectableAPI(), "surface"
            )
            stage.GetRootLayer().Save()
            with self.assertRaises(ValueError):
                prepare_assets(source, root / "bad2.usda", ["/World/A"])

    def test_referenced_layer_change_invalidates_subject(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.fixture(root)
            wrapper = root / "wrapper.usda"
            create_revision(source, wrapper)
            first = scene_identity(wrapper)
            stage = Usd.Stage.Open(str(source))
            stage.GetPrimAtPath("/World/B").SetActive(False)
            stage.GetRootLayer().Save()
            second = scene_identity(wrapper)
            self.assertEqual(first["scene_sha256"], second["scene_sha256"])
            self.assertNotEqual(first["composed_sha256"], second["composed_sha256"])

    def test_handoff_rebases_relative_paths_and_detects_changed_dependencies(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.fixture(root)
            wrapper = root / "wrapper.usda"
            create_revision(source, wrapper)
            handoff = export_handoff(
                {"scene_path": str(wrapper)}, root / "out", config={}, skill_root=root
            )
            self.assertEqual(verify_handoff(handoff["manifest_path"])["status"], "pass")
            stage = Usd.Stage.Open(str(root / "out" / "wrapper.usda"))
            self.assertTrue(stage.GetPrimAtPath("/World/A/Cube"))
            with self.assertRaises(ValueError):
                export_handoff(
                    {"scene_path": str(wrapper)},
                    root / "out",
                    config={},
                    skill_root=root,
                )
            changed = Usd.Stage.Open(str(source))
            changed.GetPrimAtPath("/World/B").SetActive(False)
            changed.GetRootLayer().Save()
            self.assertEqual(verify_handoff(handoff["manifest_path"])["status"], "fail")

    def test_handoff_preserves_task_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.json"
            task.write_text(
                json.dumps(initialize(text="server", scope="lookdev_prototype"))
            )
            result = export_handoff(
                {"task_path": str(task)}, root / "handoff", config={}, skill_root=root
            )
            manifest = verify_handoff(result["manifest_path"])["manifest"]
            self.assertEqual(manifest["delivery_scope"], "lookdev_prototype")
            self.assertTrue((root / "handoff/task.json").is_file())


if __name__ == "__main__":
    unittest.main()
