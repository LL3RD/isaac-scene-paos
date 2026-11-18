"""Negative/positive pocket fixtures; no renderer or model is involved."""
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from isaac_scene.contacts import inspect_solid_contacts, load_solid, ContactUnavailable

READY = all(importlib.util.find_spec(n) for n in ('pxr', 'trimesh', 'manifold3d', 'rtree'))
if READY:
    from pxr import Gf, Usd, UsdGeom


@unittest.skipUnless(READY, 'USD and solid-contact backend required')
class SolidContactTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def fixture(self, *, mpu=1., center=(0,0,.2)):
        path = self.root/f'pocket-{mpu}-{center}.usda'
        s = Usd.Stage.CreateNew(str(path))
        UsdGeom.SetStageMetersPerUnit(s, mpu)
        UsdGeom.SetStageUpAxis(s, 'Z')
        def box(name, xyz, size):
            p = UsdGeom.Cube.Define(s, name)
            p.CreateSizeAttr(1.)
            p.AddTranslateOp().Set(Gf.Vec3d(*xyz)/mpu)
            p.AddScaleOp().Set(Gf.Vec3d(*size)/mpu)
        box('/Part',center,(.2,.2,.2))
        box('/Fixture/Bottom',(0,0,.05),(1,1,.1))
        box('/Fixture/Left',(-.45,0,.3),(.1,1,.5))
        box('/Fixture/Right',(.45,0,.3),(.1,1,.5))
        box('/Fixture/Back',(0,.45,.3),(.8,.1,.5))
        box('/Fixture/Front',(0,-.45,.3),(.8,.1,.5))
        s.GetRootLayer().Save()
        return path

    def check(self, path, **kwargs):
        return inspect_solid_contacts(path,[('/Part','/Fixture')],**kwargs)

    def test_pocket_contact_below_rim_m_cm_mm(self):
        for mpu in (1., .01, .001):
            p = self.fixture(mpu=mpu)
            original = p.read_bytes()
            r = self.check(p)
            self.assertEqual(r['status'], 'pass', r)
            self.assertEqual(r['contacts'][0]['intersection_volume_m3'], 0)
            self.assertAlmostEqual(r['contacts'][0]['mesh_support_coverage'], 1.)
            self.assertEqual(p.read_bytes(), original)

    def test_floating_is_not_contact(self):
        r = self.check(self.fixture(center=(0,0,.25)))
        self.assertEqual(r['status'], 'fail')
        self.assertEqual(r['contacts'][0]['status'], 'insufficient_solid_contact')

    def test_true_floor_and_sidewall_penetration(self):
        for center, mesh in (((0,0,.18), '/Fixture/Bottom'),((.35,0,.2), '/Fixture/Right')):
            r = self.check(self.fixture(center=center))
            self.assertEqual(r['status'], 'fail')
            self.assertEqual(r['contacts'][0]['status'], 'solid_interpenetration')
            self.assertGreater(r['contacts'][0]['intersection_volume_m3'], 1e-6)
            self.assertTrue(r['contacts'][0]['overlap_localization_complete'])
            self.assertIn(mesh, [x['support_mesh'] for x in r['contacts'][0]['overlap_sources']])

    def test_contained_solid_is_not_a_hollow_pocket(self):
        p = self.fixture(center=(0,0,.05))
        s=Usd.Stage.Open(str(p))
        UsdGeom.Xformable(s.GetPrimAtPath('/Part')).GetOrderedXformOps()[1].Set((.05,)*3)
        s.GetRootLayer().Save()
        r=self.check(p)
        self.assertEqual(r['contacts'][0]['status'],'solid_interpenetration')

    def test_open_or_unsupported_geometry_stays_pending(self):
        p=self.fixture()
        s=Usd.Stage.Open(str(p))
        s.RemovePrim('/Fixture')
        m=UsdGeom.Mesh.Define(s,'/Fixture')
        m.CreatePointsAttr([(-1,-1,.1),(1,-1,.1),(1,1,.1)])
        m.CreateFaceVertexCountsAttr([3])
        m.CreateFaceVertexIndicesAttr([0,1,2])
        m.CreateSubdivisionSchemeAttr('none')
        s.GetRootLayer().Save()
        self.assertEqual(self.check(p)['status'],'pending')
        s.RemovePrim('/Fixture')
        UsdGeom.Sphere.Define(s,'/Fixture')
        s.GetRootLayer().Save()
        self.assertEqual(self.check(p)['status'],'pending')

    def test_probe_budget_does_not_silently_subsample(self):
        r=self.check(self.fixture(),max_samples=1)
        self.assertEqual(r['status'],'pending')
        self.assertIn('budget',r['contacts'][0]['detail'])

    def test_invalid_pairs_and_missing_backend_never_pass(self):
        p=self.fixture()
        r=inspect_solid_contacts(p,[('/Part','/Part')])
        self.assertEqual(r['status'],'fail')
        self.assertEqual(inspect_solid_contacts(p,[])['status'],'pending')
        with patch.dict(sys.modules, {'manifold3d':None}):
            self.assertEqual(self.check(p)['status'],'pending')

    def test_missing_units_cannot_use_usd_fallback_as_evidence(self):
        p=self.fixture()
        s=Usd.Stage.Open(str(p))
        s.ClearMetadata('metersPerUnit')
        s.GetRootLayer().Save()
        r=self.check(p)
        self.assertEqual(r['status'],'pending')
        self.assertEqual(r['warnings'][0]['kind'],'authored_units_required')

    def quad_box(self, stage, name, *, left_handed=False, mirrored=False):
        mesh=UsdGeom.Mesh.Define(stage,name)
        mesh.CreatePointsAttr([(-.3,-.225,0),(.3,-.225,0),(.3,.225,0),(-.3,.225,0),
                              (-.3,-.225,.04),(.3,-.225,.04),(.3,.225,.04),(-.3,.225,.04)])
        faces=[(0,3,2,1),(4,5,6,7),(0,1,5,4),(1,2,6,5),(2,3,7,6),(3,0,4,7)]
        if left_handed:
            faces=[tuple(reversed(f)) for f in faces]
            mesh.CreateOrientationAttr('leftHanded')
        mesh.CreateFaceVertexCountsAttr([4]*6)
        mesh.CreateFaceVertexIndicesAttr([i for f in faces for i in f])
        mesh.CreateSubdivisionSchemeAttr('none')
        if mirrored:
            mesh.AddScaleOp().Set((-1,1,1))
        return mesh

    def test_quad_box_bottom_is_not_a_support_surface(self):
        from scene_tools import inspect_support_contacts
        for left in (False,True):
            for mirror in (False,True):
                p=self.root/f'box-{left}-{mirror}.usda'
                s=Usd.Stage.CreateNew(str(p))
                UsdGeom.SetStageMetersPerUnit(s,1.)
                UsdGeom.SetStageUpAxis(s,'Z')
                self.quad_box(s,'/Deck',left_handed=left,mirrored=mirror)
                cube=UsdGeom.Cube.Define(s,'/Load')
                cube.CreateSizeAttr(1.)
                tr=cube.AddTranslateOp();tr.Set((0,0,.05))
                cube.AddScaleOp().Set((.1,.1,.1))
                s.GetRootLayer().Save()
                pairs=[('/Load','/Deck')]
                r=inspect_support_contacts(str(p),prim_pairs=pairs,mesh_aware=True)
                self.assertEqual(r['contacts'][0]['status'],'penetrating_support',r)
                r=inspect_solid_contacts(p,pairs)
                self.assertEqual(r['contacts'][0]['status'],'solid_interpenetration',r)
                tr.Set((0,0,.09));s.GetRootLayer().Save()
                r=inspect_support_contacts(str(p),prim_pairs=pairs,mesh_aware=True)
                self.assertEqual(r['status'],'pass',r)
                self.assertEqual(inspect_solid_contacts(p,pairs)['status'],'pass')

    def test_nonplanar_and_concave_quads_are_not_guessed_solids(self):
        s=Usd.Stage.CreateInMemory()
        for name,point in (('/Warp',(.3,.225,.08)),('/Concave',(0,0,.04))):
            mesh=self.quad_box(s,name)
            points=mesh.GetPointsAttr().Get();points[6]=point
            mesh.GetPointsAttr().Set(points)
            with self.assertRaises(ContactUnavailable):
                load_solid(mesh.GetPrim(),1.)


if __name__ == '__main__':
    unittest.main()
