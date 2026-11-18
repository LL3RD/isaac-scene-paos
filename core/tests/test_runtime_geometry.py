"""Regression: native USD instance geometry and runtime material fingerprints."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from isaac_scene.usd_exchange import _runtime_mdl_file, scene_identity
from isaac_scene.support import world_mesh_points_and_triangles

try:
    from pxr import Sdf, Usd, UsdGeom
except ImportError:
    Usd = None


@unittest.skipIf(Usd is None, "Requires USD runtime")
class RuntimeGeometryTests(unittest.TestCase):
    def instance_scene(self, root):
        texture = root / "surface.png"
        texture.write_bytes(b"fixture-v1")
        asset = Usd.Stage.CreateNew(str(root / "asset.usda"))
        model = UsdGeom.Xform.Define(asset, "/Model")
        asset.SetDefaultPrim(model.GetPrim())
        mesh = UsdGeom.Mesh.Define(asset, "/Model/Surface")
        mesh.CreatePointsAttr([(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)])
        mesh.CreateFaceVertexCountsAttr([4])
        mesh.CreateFaceVertexIndicesAttr([0, 1, 2, 3])
        mesh.GetPrim().CreateAttribute("test:texture", Sdf.ValueTypeNames.Asset).Set(Sdf.AssetPath(str(texture)))
        asset.GetRootLayer().Save()
        scene = Usd.Stage.CreateNew(str(root / "scene.usda"))
        inst = UsdGeom.Xform.Define(scene, "/Instance")
        inst.GetPrim().GetReferences().AddReference(str(root / "asset.usda"))
        inst.GetPrim().SetInstanceable(True)
        inst.AddTranslateOp().Set((2, 3, 4))
        scene.GetRootLayer().Save()
        return scene, inst.GetPrim(), texture

    def test_instance_mesh_is_read_without_expansion(self):
        with tempfile.TemporaryDirectory() as directory:
            stage, prim, _ = self.instance_scene(Path(directory))
            meshes = world_mesh_points_and_triangles(prim, Usd, UsdGeom, render_visible_only=True)
            self.assertEqual(len(meshes), 1)
            self.assertEqual(meshes[0][0][0], (2, 3, 4))
            self.assertEqual(len(meshes[0][1]), 2)
            self.assertTrue(prim.IsInstance())

    def test_instance_texture_changes_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stage, _, texture = self.instance_scene(root)
            before = scene_identity(root / "scene.usda")
            self.assertIn(str(texture), before["files"])
            texture.write_bytes(b"fixture-v2")
            self.assertNotEqual(before["composed_sha256"], scene_identity(root / "scene.usda")["composed_sha256"])

    def test_bare_runtime_mdl_is_hashed_not_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            modules = root / "modules"
            modules.mkdir()
            mdl = modules / "SceneTest.mdl"
            mdl.write_text("// dependency fixture v1\n")
            stage = Usd.Stage.CreateNew(str(root / "scene.usda"))
            prim = stage.DefinePrim("/Shader", "Shader")
            prim.CreateAttribute("info:mdl:sourceAsset", Sdf.ValueTypeNames.Asset).Set(Sdf.AssetPath("SceneTest.mdl"))
            stage.GetRootLayer().Save()
            with patch.dict(os.environ, {"PXR_AR_DEFAULT_SEARCH_PATH": str(modules)}):
                before = scene_identity(root / "scene.usda")
                self.assertIn(str(mdl), before["files"])
                self.assertEqual(before["unresolved_assets"], [])
                mdl.write_text("// dependency fixture v2\n")
                self.assertNotEqual(before["composed_sha256"], scene_identity(root / "scene.usda")["composed_sha256"])
                for missing in ["missing/SceneTest.mdl", "/missing/SceneTest.mdl", "SceneTest.png"]:
                    self.assertIsNone(_runtime_mdl_file(missing))


if __name__ == "__main__":
    unittest.main()
