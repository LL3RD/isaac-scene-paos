"""Actual USD regression fixtures: units, joint trees and incomplete evidence."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
try:
    from pxr import Gf, Usd, UsdGeom, UsdPhysics
except ImportError:
    Usd = None

from isaac_scene.articulations import inspect_joints, pose_joints, initialize_articulation, _uniform_scale
from scene_tools import inspect_support_contacts, inspect_scene_overlaps, solve_support_placement
from isaac_scene.scene_task import initialize, check, subject_identity


@unittest.skipIf(Usd is None, "USD runtime required")
class AgentGeometryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def cube(self, stage, path, center, size, mpu):
        cube = UsdGeom.Cube.Define(stage, path)
        cube.CreateSizeAttr(1)
        cube.AddTranslateOp().Set(Gf.Vec3d(*center) / mpu)
        cube.AddScaleOp().Set(Gf.Vec3d(*size) / mpu)
        return cube

    def geometry(self, mpu, *, gap=0.0):
        path = self.root / f"g{mpu}-{gap}.usda"
        stage = Usd.Stage.CreateNew(str(path))
        UsdGeom.SetStageMetersPerUnit(stage, mpu)
        UsdGeom.SetStageUpAxis(stage, "Z")
        self.cube(stage, "/World/Top", (0, 0, .45), (1, 1, .1), mpu)
        self.cube(stage, "/World/Object", (0, 0, .6 + gap), (.2, .2, .2), mpu)
        self.cube(stage, "/World/Background/Wall", (.14, 0, .6), (.1, .4, .2), mpu)
        stage.GetRootLayer().Save()
        return path

    def test_support_m_cm_mm_agree(self):
        for mpu in (1, .01, .001):
            for gap, expected in ((0, "pass"), (.02, "fail"), (-.02, "fail")):
                with self.subTest(mpu=mpu, gap=gap):
                    report = inspect_support_contacts(str(self.geometry(mpu, gap=gap)),
                        prim_pairs=[("/World/Object", "/World/Top")], mesh_aware=True)
                    self.assertEqual(report["status"], expected)
                    self.assertEqual(report["coordinate_unit"], "m")
                    self.assertAlmostEqual(report["contacts"][0]["object_visible_bounds"]["size"][0], .2)

    def test_collision_m_cm_mm_agree(self):
        for mpu in (1, .01, .001):
            report = inspect_scene_overlaps(str(self.geometry(mpu)), object_prim_paths=["/World/Object"])
            self.assertEqual(report["status"], "fail")
            self.assertEqual(report["overlap_count"], 1)
            self.assertAlmostEqual(report["object_bounds"][0]["size"][2], .2)

    def test_empty_overlap_never_passes(self):
        path = self.geometry(1)
        stage = Usd.Stage.Open(str(path))
        UsdGeom.Xform.Define(stage, "/World/Objects")
        stage.GetRootLayer().Save()
        self.assertNotEqual(inspect_scene_overlaps(str(path))["status"], "pass")

    def test_unported_solver_fails_closed_for_cm(self):
        report = solve_support_placement(str(self.geometry(.01)),
            object_prim_path="/World/Object", support_prim_path="/World/Top")
        self.assertEqual(report["errors"][0]["kind"], "placement_requires_meter_stage")

    def robot(self, mpu=1):
        path = self.root / f"robot{mpu}.usda"
        stage = Usd.Stage.CreateNew(str(path))
        UsdGeom.SetStageMetersPerUnit(stage, mpu)
        UsdGeom.SetStageUpAxis(stage, "Z")
        root = UsdGeom.Xform.Define(stage, "/Robot")
        root.AddTranslateOp().Set((3/mpu, -2/mpu, .8/mpu))
        root.AddRotateZOp().Set(35)
        root.AddScaleOp().Set((1/mpu,)*3)
        for name, position in (("Base", (0, 0, 0)), ("Arm", (0, 0, 1)), ("Tip", (0, 0, 2))):
            link = UsdGeom.Xform.Define(stage, "/Robot/"+name)
            link.AddTranslateOp().Set(position)
            UsdPhysics.RigidBodyAPI.Apply(link.GetPrim())
            self.cube(stage, "/Robot/"+name+"/Visual", (0, 0, .1), (.1, .1, .2), 1)
        joint = UsdPhysics.RevoluteJoint.Define(stage, "/Robot/Hinge")
        joint.CreateBody0Rel().SetTargets(["/Robot/Base"])
        joint.CreateBody1Rel().SetTargets(["/Robot/Arm"])
        joint.CreateLocalPos0Attr((0, 0, 1))
        joint.CreateAxisAttr("Y")
        joint.CreateLowerLimitAttr(-90)
        joint.CreateUpperLimitAttr(220)
        slider = UsdPhysics.PrismaticJoint.Define(stage, "/Robot/Slider")
        slider.CreateBody0Rel().SetTargets(["/Robot/Arm"])
        slider.CreateBody1Rel().SetTargets(["/Robot/Tip"])
        slider.CreateLocalPos0Attr((0, 0, 1))
        slider.CreateAxisAttr("X")
        slider.CreateLowerLimitAttr(0)
        slider.CreateUpperLimitAttr(.3)
        stage.GetRootLayer().Save()
        return path

    def test_pose_units_and_frame_continuity(self):
        for mpu in (1, .01, .001):
            source = self.robot(mpu)
            original = source.read_bytes()
            result = pose_joints(source, self.root/f"posed{mpu}.usda", root="/Robot",
                positions={"/Robot/Hinge": 200, "/Robot/Slider": .1})
            audit = result["joint_check"]
            self.assertEqual(audit["status"], "pass")
            self.assertLess(audit["max_gap_m"], 1e-7)
            self.assertAlmostEqual(audit["joints"][0]["position"], 200)
            self.assertAlmostEqual(audit["joints"][1]["position"], .1)
            self.assertEqual(source.read_bytes(), original)
            with self.assertRaises(ValueError):
                pose_joints(source, result["scene_path"], root="/Robot", positions={"/Robot/Hinge": 0})

    def test_invalid_pose_has_no_output(self):
        source = self.robot()
        for positions in ({"/Robot/Hinge": 0}, {"/Robot/Hinge": 800, "/Robot/Slider": 0},
                          {"/Robot/Hinge": float("nan"), "/Robot/Slider": 0}):
            output = self.root/"rejected.usda"
            with self.assertRaises(ValueError):
                pose_joints(source, output, root="/Robot", positions=positions)
            self.assertFalse(output.exists())

    def test_broken_anchor_detected_in_cm(self):
        source = self.robot(.01)
        stage = Usd.Stage.Open(str(source))
        UsdGeom.Xformable(stage.GetPrimAtPath("/Robot/Arm")).GetOrderedXformOps()[0].Set((.02, 0, 1))
        stage.GetRootLayer().Save()
        report = inspect_joints(source)
        self.assertEqual(report["status"], "fail")
        self.assertAlmostEqual(report["max_gap_m"], .02)

    def test_unsupported_scale_and_cycle_rejected(self):
        source = self.robot()
        stage = Usd.Stage.Open(str(source))
        UsdGeom.Xformable(stage.GetPrimAtPath("/Robot/Arm")).AddScaleOp().Set((1, 2, 1))
        stage.GetRootLayer().Save()
        with self.assertRaises(ValueError):
            pose_joints(source, self.root/"badscale.usda", root="/Robot",
                        positions={"/Robot/Hinge": 0, "/Robot/Slider": 0})
        unsupported = inspect_joints(source)
        self.assertEqual(unsupported["status"], "pending")
        self.assertEqual(unsupported["unsupported_details"][0]["code"], "UNSUPPORTED_BODY_SCALE")
        stage = Usd.Stage.Open(str(source))
        UsdGeom.Xformable(stage.GetPrimAtPath('/Robot/Arm')).GetOrderedXformOps()[-1].Set((1, 1, 1))
        joint = UsdPhysics.FixedJoint.Define(stage, '/Robot/Cycle')
        joint.CreateBody0Rel().SetTargets(['/Robot/Tip'])
        joint.CreateBody1Rel().SetTargets(['/Robot/Base'])
        stage.GetRootLayer().Save()
        with self.assertRaisesRegex(ValueError, 'Cyclic'):
            pose_joints(source, self.root/'cycle.usda', root='/Robot',
                        positions={'/Robot/Hinge':0, '/Robot/Slider':0})

    def test_partial_support_report_cannot_cover_all_targets(self):
        source = self.geometry(.01)
        task = initialize(text='two supported objects')
        task['artifacts']['scene'] = str(source)
        task['validation_targets']['support_pairs'] = [
            {'object':'/World/Object', 'support':'/World/Top'},
            {'object':'/World/OtherObject', 'support':'/World/Top'}]
        report = {'command':'inspect-support-contact','subject':subject_identity(source),
            'result':inspect_support_contacts(str(source),
                prim_pairs=[('/World/Object','/World/Top')], mesh_aware=True)}
        (self.root/'support.json').write_text(json.dumps(report))
        task['evidence']['support'] = ['support.json']
        result = check(task, base_dir=self.root)
        self.assertNotIn('support', result['accepted_gates'])
        missing = next(r for r in result['pending'] if r['gate'] == 'support')
        self.assertEqual(missing['missing'], [('/World/OtherObject','/World/Top')])

    def test_unequal_body_scale_pose_preserves_each_scale(self):
        for mpu in (1, .01):
            source = self.robot(mpu)
            stage = Usd.Stage.Open(str(source))
            UsdGeom.Xformable(stage.GetPrimAtPath('/Robot/Arm')).AddScaleOp().Set((.1,)*3)
            UsdGeom.Xformable(stage.GetPrimAtPath('/Robot/Tip')).AddScaleOp().Set((.3,)*3)
            stage.GetRootLayer().Save()
            result = pose_joints(source, self.root/f'scales{mpu}.usda', root='/Robot',
                                positions={'/Robot/Hinge':35, '/Robot/Slider':.02})
            self.assertEqual(result['joint_check']['status'], 'pass')
            self.assertEqual(len(result['joint_check']['body_scale_notes']), 2)
            dst = Usd.Stage.Open(result['scene_path'])
            cache = UsdGeom.XformCache()
            for name, expected in (('Base',1),('Arm',.1),('Tip',.3)):
                self.assertAlmostEqual(_uniform_scale(cache.GetLocalToWorldTransform(dst.GetPrimAtPath('/Robot/'+name))),
                                       expected/mpu, places=5)
            self.assertAlmostEqual(result['joint_check']['joints'][1]['position'], .02)

    def test_solid_gate_requires_complete_checks_and_fine_sampling(self):
        source = self.geometry(1.)
        task = initialize(text='supported object')
        task['artifacts']['scene'] = str(source)
        task['validation_targets']['support_pairs'] = [{'object':'/World/Object','support':'/World/Top'}]
        task['evidence']['support'] = ['support.json']
        for complete, pitch, accepted in ((True,.01,True),(False,.01,False),(True,.02,False)):
            data = inspect_support_contacts(str(source), prim_pairs=[('/World/Object','/World/Top')], mesh_aware=True)
            data.update(contact_mode='solid', sample_pitch_m=pitch)
            data['contacts'][0].update(penetration_check_complete=complete,contact_sampling_complete=True)
            report = {'command':'inspect-support-contact','subject':subject_identity(source),'result':data}
            (self.root/'support.json').write_text(json.dumps(report))
            self.assertEqual('support' in check(task, base_dir=self.root)['accepted_gates'], accepted)

    def test_initialize_uses_explicit_authored_targets_and_updates_state(self):
        from pxr import Sdf
        source = self.robot()
        stage = Usd.Stage.Open(str(source))
        for name, kind, target in (('Hinge','angular',-40.), ('Slider','linear',.1)):
            prim = stage.GetPrimAtPath('/Robot/'+name)
            UsdPhysics.DriveAPI.Apply(prim, kind).CreateTargetPositionAttr(target)
            prim.CreateAttribute(f'state:{kind}:physics:position', Sdf.ValueTypeNames.Float).Set(0.)
            prim.CreateAttribute(f'state:{kind}:physics:velocity', Sdf.ValueTypeNames.Float).Set(2.)
        stage.GetRootLayer().Save()
        before = source.read_bytes()
        planned = initialize_articulation(source, root='/Robot', position_source='drive-targets')
        self.assertEqual(planned['action'], 'plan_only')
        self.assertEqual(planned['positions'], {'/Robot/Hinge':-40., '/Robot/Slider':.10000000149011612})
        result = initialize_articulation(source, root='/Robot', position_source='drive-targets', output=self.root/'init.usda')
        dst = Usd.Stage.Open(result['scene_path'])
        self.assertEqual(result['joint_check']['status'], 'pass')
        self.assertEqual(dst.GetPrimAtPath('/Robot/Hinge').GetAttribute('state:angular:physics:position').Get(), -40.)
        self.assertEqual(dst.GetPrimAtPath('/Robot/Hinge').GetAttribute('state:angular:physics:velocity').Get(), 0.)
        self.assertEqual(source.read_bytes(), before)

    def test_initialize_never_guesses_missing_or_clamps_invalid_values(self):
        source = self.robot()
        target = self.root/'must-not-exist.usda'
        for mode in ('drive-targets','authored-state'):
            result = initialize_articulation(source, root='/Robot', position_source=mode, output=target)
            self.assertEqual(result['status'], 'pending')
            self.assertFalse(target.exists())
        stage = Usd.Stage.Open(str(source))
        UsdPhysics.DriveAPI.Apply(stage.GetPrimAtPath('/Robot/Hinge'), 'angular').CreateTargetPositionAttr(999.)
        UsdPhysics.DriveAPI.Apply(stage.GetPrimAtPath('/Robot/Slider'), 'linear').CreateTargetPositionAttr(.1)
        stage.GetRootLayer().Save()
        result = initialize_articulation(source, root='/Robot', position_source='drive-targets', output=target)
        self.assertEqual(result['status'], 'pending')
        self.assertIn('INITIAL_POSITION_OUTSIDE_LIMITS', [r['code'] for r in result['errors']])
        self.assertFalse(target.exists())

    def test_official_carter_and_spot_when_installed(self):
        if not os.environ.get('ISAAC_TEST_ROBOT_ROOT'):
            self.skipTest('Set ISAAC_TEST_ROBOT_ROOT for optional installed-asset tests')
        base = Path(os.environ['ISAAC_TEST_ROBOT_ROOT'])
        if not base.is_dir():
            self.skipTest('Official robots not installed on this host')
        carter = inspect_joints(base/'NVIDIA/Carter/carter_v1.usd')
        self.assertEqual(carter['status'], 'pass')
        self.assertTrue(carter['body_scale_notes'])
        json.dumps(carter, allow_nan=False)
        spot = base/'BostonDynamics/spot/spot.usd'
        before = inspect_joints(spot)
        self.assertEqual(len(before['initialization_required_joints']), 4)
        result = initialize_articulation(spot, root='/spot', position_source='drive-targets', output=self.root/'spot.usda')
        self.assertEqual(result['joint_check']['status'], 'pass')
        self.assertEqual(len(result['positions']), 12)

    def test_unbounded_limits_are_strict_json(self):
        source=self.robot()
        s=Usd.Stage.Open(str(source))
        j=UsdPhysics.RevoluteJoint(s.GetPrimAtPath('/Robot/Hinge'))
        j.GetLowerLimitAttr().Set(float('-inf'))
        j.GetUpperLimitAttr().Set(float('inf'))
        s.GetRootLayer().Save()
        result=inspect_joints(source)
        self.assertEqual(result['joints'][0]['limits'],[None,None])
        json.dumps(result,allow_nan=False)

    def test_nonfinite_joint_frame_cannot_pass(self):
        source=self.robot()
        s=Usd.Stage.Open(str(source))
        j=UsdPhysics.Joint(s.GetPrimAtPath('/Robot/Hinge'))
        j.GetLocalPos0Attr().Set((float('nan'),0,0))
        s.GetRootLayer().Save()
        self.assertEqual(inspect_joints(source)['status'],'fail')

    def test_checker_detects_broken_robot_without_agent_report(self):
        source = self.robot()
        stage = Usd.Stage.Open(str(source))
        UsdGeom.Xformable(stage.GetPrimAtPath('/Robot/Arm')).GetOrderedXformOps()[0].Set((.1, 0, 1))
        stage.GetRootLayer().Save()
        task = initialize(text='one robot')
        task['artifacts']['scene'] = str(source)
        result = check(task)
        self.assertEqual(result['status'], 'fail')
        self.assertIn('INVALID_JOINT_FRAMES', [r['code'] for r in result['errors']])

    def test_real_panda_pose_when_installed(self):
        if not os.environ.get('ISAAC_TEST_ROBOT_ROOT'):
            self.skipTest('Set ISAAC_TEST_ROBOT_ROOT for optional installed-asset tests')
        source = Path(os.environ['ISAAC_TEST_ROBOT_ROOT']) / 'FrankaRobotics/FrankaEmika/panda_instanceable.usd'
        if not source.exists():
            self.skipTest('Official Panda not installed on this host')
        values = [0, -20, 0, -100, 0, 100, 45]
        positions = {f'/panda/panda_link{i}/panda_joint{i+1}': q for i, q in enumerate(values)}
        positions.update({f'/panda/panda_hand/panda_finger_joint{i}': .02 for i in (1, 2)})
        result = pose_joints(source, self.root/'panda.usda', root='/panda', positions=positions)
        self.assertEqual(result['joint_check']['status'], 'pass')
        self.assertEqual(result['joint_check']['joint_count'], 10)
        self.assertLess(result['joint_check']['max_gap_m'], 1e-6)


if __name__ == "__main__":
    unittest.main()
