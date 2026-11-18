"""Preservation contracts for conservative material enrichment (requires USD)."""
from pathlib import Path
import json
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
try:
    from pxr import Gf, Sdf, Usd, UsdGeom, UsdShade
    from apply_object_surface_lookdev import _classify_neutral_material, _source_roughness, apply_object_lookdev
except ImportError:
    Usd = None


@unittest.skipIf(Usd is None, "requires pxr USD runtime")
class SurfacePreservationTests(unittest.TestCase):
    def setUp(self):
        self.stage = Usd.Stage.CreateInMemory()
        self.material = UsdShade.Material.Define(self.stage, "/Looks/Surface")
        self.shader = UsdShade.Shader.Define(self.stage, "/Looks/Surface/Shader")
        self.shader.CreateIdAttr("UsdPreviewSurface")
        self.shader.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f).Set(Gf.Vec3f(.7))
        self.material.CreateSurfaceOutput().ConnectToSource(self.shader.ConnectableAPI(), "surface")

    def test_neutral_dielectric_is_eligible(self):
        self.assertEqual("painted_light_gray", _classify_neutral_material(self.material)[0])

    def test_bare_metal_is_not_repainted(self):
        self.shader.CreateInput("metallic", Sdf.ValueTypeNames.Float).Set(1)
        self.assertEqual((None, "authored_metallic_surface"), _classify_neutral_material(self.material))

    def test_connected_inputs_are_preserved(self):
        node = UsdShade.Shader.Define(self.stage, "/Looks/Surface/Noise")
        node.CreateIdAttr("ProceduralNoise")
        node.CreateOutput("out", Sdf.ValueTypeNames.Float)
        self.shader.CreateInput("roughness", Sdf.ValueTypeNames.Float).ConnectToSource(node.ConnectableAPI(), "out")
        self.assertEqual((None, "connected_surface_inputs"), _classify_neutral_material(self.material))

    def test_specular_workflow_is_preserved(self):
        self.shader.CreateInput("useSpecularWorkflow", Sdf.ValueTypeNames.Int).Set(1)
        self.assertEqual((None, "specular_workflow"), _classify_neutral_material(self.material))

    def test_roughness_is_not_forced_to_global_value(self):
        for value in [.13, .31, .78]:
            self.shader.CreateInput("roughness", Sdf.ValueTypeNames.Float).Set(value)
            self.assertAlmostEqual(value, _source_roughness(self.material))

    def test_transparent_and_colored_identity_are_preserved(self):
        self.shader.CreateInput("opacity", Sdf.ValueTypeNames.Float).Set(.4)
        self.assertEqual("transparent", _classify_neutral_material(self.material)[1])
        self.shader.GetInput("opacity").Set(1)
        self.shader.GetInput("diffuseColor").Set(Gf.Vec3f(.9, .1, .1))
        self.assertEqual("colored_identity", _classify_neutral_material(self.material)[1])

    def test_source_cannot_be_output(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "scene.usda"
            with self.assertRaises(ValueError):
                apply_object_lookdev(path, Path(directory) / "spec.json", path, Path(directory))

    def test_layer_preserves_source_metal_subsets_and_roughness(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.usda"
            scene = Usd.Stage.CreateNew(str(source))
            for name, metallic, roughness in [("Paint", 0, .27), ("Steel", 1, .14), ("Faces", 0, .62)]:
                mesh = UsdGeom.Mesh.Define(scene, f"/World/Equipment/{name}")
                mat = UsdShade.Material.Define(scene, f"/Looks/{name}")
                shader = UsdShade.Shader.Define(scene, f"/Looks/{name}/Shader")
                shader.CreateIdAttr("UsdPreviewSurface")
                shader.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f).Set(Gf.Vec3f(.7))
                shader.CreateInput("metallic", Sdf.ValueTypeNames.Float).Set(metallic)
                shader.CreateInput("roughness", Sdf.ValueTypeNames.Float).Set(roughness)
                mat.CreateSurfaceOutput().ConnectToSource(shader.ConnectableAPI(), "surface")
                binding = UsdShade.MaterialBindingAPI.Apply(mesh.GetPrim())
                binding.Bind(mat)
                if name == "Faces":
                    subset = binding.CreateMaterialBindSubset("PrintedLabel", [0])
                    UsdShade.MaterialBindingAPI.Apply(subset.GetPrim()).Bind(mat)
            scene.GetRootLayer().Save()
            before = source.read_bytes()
            # A small material fixture exercises composition without Isaac renderer.
            coating = Usd.Stage.CreateNew(str(root / "opaque__metal__painted02_gray_b_warehouse_a.usd"))
            mat = UsdShade.Material.Define(coating, "/Coating")
            UsdShade.Shader.Define(coating, "/Coating/Shader")
            coating.SetDefaultPrim(mat.GetPrim())
            coating.GetRootLayer().Save()
            spec = root / "spec.json"
            spec.write_text(json.dumps({"references": [{"id": "equipment", "prim": "/World/Equipment", "canonical_class": "industrial/machine"}]}))
            output = root / "lookdev.usda"
            report = apply_object_lookdev(source, spec, output, root)
            self.assertEqual(1, report["binding_count"])
            self.assertEqual(1, report["preserved_surface_counts"]["face_material_subsets"])
            self.assertEqual(1, report["preserved_surface_counts"]["authored_metallic_surface"])
            self.assertEqual(before, source.read_bytes())
            result = Usd.Stage.Open(str(output))
            steel, _ = UsdShade.MaterialBindingAPI(result.GetPrimAtPath("/World/Equipment/Steel")).ComputeBoundMaterial()
            self.assertEqual("/Looks/Steel", str(steel.GetPath()))
            paint, _ = UsdShade.MaterialBindingAPI(result.GetPrimAtPath("/World/Equipment/Paint")).ComputeBoundMaterial()
            rough = result.GetPrimAtPath(paint.GetPath().AppendChild("Shader")).GetAttribute("inputs:reflection_roughness_constant").Get()
            self.assertAlmostEqual(.27, rough)
            with self.assertRaises(FileExistsError):
                apply_object_lookdev(source, spec, output, root)


if __name__ == "__main__":
    unittest.main()
