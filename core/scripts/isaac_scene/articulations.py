"""USD joint-frame integrity and bounded, static tree posing (not IK/PhysX).

Use authored joint frames, not robot-specific DH tables or guessed link origins.
All edits are stronger-layer opinions; referenced meshes and source files stay intact.
"""
from __future__ import annotations

import math
from pathlib import Path


def _frame(joint, side):
    from pxr import Gf
    position = Gf.Vec3d(getattr(joint, f"GetLocalPos{side}Attr")().Get())
    rotation = Gf.Quatd(getattr(joint, f"GetLocalRot{side}Attr")().Get())
    if (not all(math.isfinite(v) for v in (*position, rotation.GetReal(), *rotation.GetImaginary()))
            or rotation.GetLength() < 1e-12):
        raise ValueError("Joint frame must have finite translation and a nonzero finite quaternion")
    matrix = Gf.Matrix4d(1)
    matrix.SetRotate(rotation.GetNormalized())
    matrix.SetTranslateOnly(position)
    return matrix


def _uniform_scale(matrix):
    from pxr import Gf
    if not all(math.isfinite(matrix[i, j]) for i in range(4) for j in range(4)):
        raise ValueError("Body transform must be finite")
    axes = [matrix.TransformDir(Gf.Vec3d(*(int(i == j) for i in range(3)))) for j in range(3)]
    lengths = [v.GetLength() for v in axes]
    if (not all(math.isfinite(v) and v > 1e-9 for v in lengths)
            or max(lengths) - min(lengths) > 1e-5 * max(lengths)
            or matrix.GetDeterminant() <= 0
            or any(abs(Gf.Dot(axes[i], axes[j])) > lengths[i] * lengths[j] * 1e-5
                   for i in range(3) for j in range(i))):
        raise ValueError("Only positive uniform rigid-body scale is supported")
    return sum(lengths) / 3


def _rigid_frame(matrix):
    """Keep the world origin; remove positive uniform scale from the basis only."""
    from pxr import Gf
    scale = _uniform_scale(matrix)
    result = Gf.Matrix4d(matrix)
    for i in range(3):
        for j in range(3):
            result[i, j] = matrix[i, j] / scale
    return result


def _authored_number(prim, name, factor=1.0):
    attr = prim.GetAttribute(name)
    if not attr or not attr.HasAuthoredValueOpinion():
        return None
    value = attr.Get()
    return float(value) * factor if isinstance(value, (int, float)) and math.isfinite(value) else None


def _limits(schema):
    values = [float(schema.GetLowerLimitAttr().Get()), float(schema.GetUpperLimitAttr().Get())]
    if any(math.isnan(v) for v in values):
        raise ValueError("Joint limits must not contain NaN")
    return values


def _json_limits(values):
    # JSON has no Infinity token. Null endpoints explicitly mean unbounded.
    return [v if math.isfinite(v) else None for v in values] if values is not None else None


def _joint_rows(stage, root_path):
    from pxr import Usd, UsdGeom, UsdPhysics
    root = stage.GetPrimAtPath(root_path)
    if not root:
        raise ValueError(f"Missing robot root: {root_path}")
    rows = []
    for prim in Usd.PrimRange(root, Usd.TraverseInstanceProxies()):
        if not prim.IsA(UsdPhysics.Joint):
            continue
        joint = UsdPhysics.Joint(prim)
        if joint.GetJointEnabledAttr().Get() is False:
            continue
        bodies = []
        for rel in (joint.GetBody0Rel(), joint.GetBody1Rel()):
            targets = rel.GetTargets()
            if len(targets) > 1:
                raise ValueError(f"Multiple joint bodies: {prim.GetPath()}")
            body = stage.GetPrimAtPath(targets[0]) if targets else None
            if targets and (not body or not UsdGeom.Xformable(body)):
                raise ValueError(f"Missing/non-transformable joint body: {targets}")
            bodies.append(body)
        if any(body and UsdGeom.Imageable(body).ComputeVisibility() == UsdGeom.Tokens.invisible
               for body in bodies):
            continue
        rows.append((joint, bodies))
    return rows


def inspect_joints(scene, *, roots=None, tolerance_m=0.002, angle_tolerance_deg=0.5):
    """Check every enabled, visible internal joint; world anchors are reported separately."""
    from pxr import Gf, Usd, UsdGeom, UsdPhysics
    stage = Usd.Stage.Open(str(scene))
    if not stage:
        raise ValueError(f"Cannot open {scene}")
    if not 0 < tolerance_m <= .01 or not 0 < angle_tolerance_deg <= 2:
        raise ValueError("Joint tolerances must be (0,.01] metres and (0,2] degrees")
    mpu = UsdGeom.GetStageMetersPerUnit(stage)
    if not math.isfinite(mpu) or mpu <= 0:
        raise ValueError("Invalid metersPerUnit")
    cache = UsdGeom.XformCache(Usd.TimeCode.Default())
    rows, seen, errors, unsupported, world_anchors = [], set(), [], [], []
    unsupported_details, scale_notes, initialization_required = [], [], []
    for root in roots or ["/"]:
        for joint, bodies in _joint_rows(stage, root):
            name = str(joint.GetPath())
            if name in seen:
                continue
            seen.add(name)
            if any(body is None for body in bodies):
                world_anchors.append(name)
                continue
            prim = joint.GetPrim()
            if not any(prim.IsA(t) for t in (UsdPhysics.FixedJoint, UsdPhysics.RevoluteJoint, UsdPhysics.PrismaticJoint)):
                unsupported.append(name)
                continue
            b0, b1 = [cache.GetLocalToWorldTransform(body) for body in bodies]
            try:
                s0, s1 = _uniform_scale(b0), _uniform_scale(b1)
            except ValueError as exc:
                unsupported.append(name)
                unsupported_details.append({"joint": name, "code": "UNSUPPORTED_BODY_SCALE", "detail": str(exc)})
                continue
            if abs(s0 - s1) > 1e-5 * s0:
                scale_notes.append({"joint": name, "body0_scale": s0, "body1_scale": s1,
                                    "method": "scale_each_local_anchor_then_compare_rigid_world_frames"})
            # Each local anchor is transformed by its own body, not the other
            # body's scale. Rotations must never be extracted from a scale ratio.
            try:
                f0, f1 = [_rigid_frame(_frame(joint, side) * body)
                          for side, body in enumerate((b0, b1))]
            except ValueError as exc:
                errors.append({"joint": name, "code": "INVALID_JOINT_FRAME", "detail": str(exc)})
                continue
            delta = f1.ExtractTranslation() - f0.ExtractTranslation()
            relative = f1 * f0.GetInverse()
            is_revolute = prim.IsA(UsdPhysics.RevoluteJoint)
            is_prismatic = prim.IsA(UsdPhysics.PrismaticJoint)
            schema = UsdPhysics.RevoluteJoint(prim) if is_revolute else UsdPhysics.PrismaticJoint(prim)
            axis = Gf.Vec3d(0)
            axis_index = "XYZ".index(str(schema.GetAxisAttr().Get())) if (is_revolute or is_prismatic) else 0
            axis[axis_index] = 1
            world_axis = f0.TransformDir(axis).GetNormalized()
            along = Gf.Dot(delta, world_axis)
            gap = (delta - along * world_axis).GetLength() * mpu if is_prismatic else delta.GetLength() * mpu
            rotation = relative.ExtractRotation()
            if is_revolute:
                axis_error = math.degrees(math.acos(max(-1., min(1., Gf.Dot(
                    f1.TransformDir(axis).GetNormalized(), world_axis)))))
                quat = relative.ExtractRotationQuat().GetNormalized()
                position = math.degrees(2 * math.atan2(Gf.Dot(quat.GetImaginary(), axis), quat.GetReal()))
                position = (position + 180) % 360 - 180
                lower, upper = _limits(schema)
                if math.isfinite(lower) and math.isfinite(upper) and lower <= upper:
                    equivalents = [position + 360 * k for k in range(
                        math.floor((lower - position) / 360), math.ceil((upper - position) / 360) + 1)]
                    valid = [v for v in equivalents if lower - angle_tolerance_deg <= v <= upper + angle_tolerance_deg]
                    if valid:
                        position = min(valid, key=abs)
            else:
                axis_error = rotation.GetAngle()
                position = along / s0 if is_prismatic else 0
            problems = []
            if gap > tolerance_m:
                problems.append("JOINT_ANCHOR_GAP")
            if axis_error > angle_tolerance_deg:
                problems.append("JOINT_FRAME_MISALIGNMENT")
            limits = None
            drive_target = state_position = None
            if is_revolute or is_prismatic:
                limits = _limits(schema)
                eps = angle_tolerance_deg if is_revolute else tolerance_m / (s0 * mpu)
                if limits[0] <= limits[1] and not limits[0] - eps <= position <= limits[1] + eps:
                    problems.append("JOINT_LIMIT_EXCEEDED")
                mode = "angular" if is_revolute else "linear"
                factor = 1.0 if is_revolute else s0 * mpu
                drive_target = _authored_number(prim, f"drive:{mode}:physics:targetPosition", factor)
                state_position = _authored_number(prim, f"state:{mode}:physics:position", factor)
                if "JOINT_LIMIT_EXCEEDED" in problems and drive_target is not None:
                    target_native = drive_target / factor
                    if limits[0] <= target_native <= limits[1]:
                        initialization_required.append(name)
            row = {"joint": name, "type": prim.GetTypeName(), "body0": str(bodies[0].GetPath()),
                   "body1": str(bodies[1].GetPath()), "gap_m": gap, "frame_error_deg": axis_error,
                   "position": position if not is_prismatic else position * s0 * mpu,
                   "position_unit": "m" if is_prismatic else "deg",
                   "limits": _json_limits([v * s0 * mpu for v in limits] if is_prismatic else limits),
                   "null_limit_endpoint_means": "unbounded",
                   "limit_unit": "m" if is_prismatic else "deg",
                   "drive_target": drive_target, "authored_state_position": state_position,
                   "initialization_required": name in initialization_required,
                   "status": "fail" if problems else "pass", "problems": problems}
            rows.append(row)
            errors.extend({"joint": name, "code": code} for code in problems)
    return {"status": "fail" if errors else ("pending" if unsupported or not rows else "pass"),
            "scene_path": str(Path(scene).resolve()), "roots": roots or ["/"], "meters_per_unit": mpu,
            "tolerance_m": tolerance_m, "angle_tolerance_deg": angle_tolerance_deg,
            "joint_count": len(rows), "max_gap_m": max((r["gap_m"] for r in rows), default=0),
            "joints": rows, "errors": errors, "unsupported_joints": unsupported,
            "unsupported_details": unsupported_details, "body_scale_notes": scale_notes,
            "initialization_required_joints": initialization_required,
            "world_anchors_not_certified": world_anchors,
            "limitations": ["Static internal joint frames only; not IK, collision, world anchoring or dynamics certification"]}


def pose_joints(source, output, *, root, positions):
    """Pose a directed, acyclic joint tree using explicit complete joint positions.

    Revolute values are degrees; prismatic values are metres. Fixed joints follow.
    Anchor origins use their own body scales. Motions operate in rigid world
    frames; each child's original scale is preserved. Body mesh-local transforms
    are untouched. Unsupported graphs/scales fail before file creation.
    """
    from pxr import Gf, Sdf, Usd, UsdGeom, UsdPhysics
    from .usd_exchange import sha256
    if not isinstance(positions, dict) or not positions:
        raise ValueError("positions must be a nonempty map of absolute joint paths to degrees/metres")
    source, output = Path(source).resolve(), Path(output).resolve()
    if output.exists() or output == source:
        raise ValueError("Output must be a new USD revision")
    stage = Usd.Stage.Open(str(source))
    if not stage:
        raise ValueError(f"Cannot open {source}")
    mpu = UsdGeom.GetStageMetersPerUnit(stage)
    if not math.isfinite(mpu) or mpu <= 0:
        raise ValueError("Invalid metersPerUnit")
    joints = _joint_rows(stage, root)
    movable, children, edges, world_anchors = {}, set(), [], []
    for joint, bodies in joints:
        prim, name = joint.GetPrim(), str(joint.GetPath())
        if any(body is None for body in bodies):
            if not prim.IsA(UsdPhysics.FixedJoint) or bodies[0] is not None or bodies[1] is None:
                raise ValueError(f"Only a world-to-base fixed joint is supported: {name}")
            world_anchors.append((joint, bodies[1]))
            continue
        a, b = [str(body.GetPath()) for body in bodies]
        if not all(Sdf.Path(p).HasPrefix(Sdf.Path(root)) for p in (a, b)) or b in children:
            raise ValueError("Joint tree requires internal bodies and one parent per child")
        children.add(b)
        kind = "fixed"
        if prim.IsA(UsdPhysics.RevoluteJoint):
            kind, schema = "angular", UsdPhysics.RevoluteJoint(prim)
        elif prim.IsA(UsdPhysics.PrismaticJoint):
            kind, schema = "linear", UsdPhysics.PrismaticJoint(prim)
        elif not prim.IsA(UsdPhysics.FixedJoint):
            raise ValueError(f"Unsupported joint type: {name}")
        if kind != "fixed":
            movable[name] = schema
        edges.append((joint, a, b, kind))
    if set(positions) != set(movable):
        raise ValueError(f"Provide every movable joint exactly once; missing={sorted(set(movable)-set(positions))}; unknown={sorted(set(positions)-set(movable))}")
    cache = UsdGeom.XformCache(Usd.TimeCode.Default())
    body_paths = {p for _, a, b, _ in edges for p in (a, b)}
    worlds = {p: cache.GetLocalToWorldTransform(stage.GetPrimAtPath(p)) for p in body_paths}
    scales = {p: _uniform_scale(m) for p, m in worlds.items()}
    done = body_paths - children
    remaining, ordered = list(edges), []
    while remaining:
        ready = [edge for edge in remaining if edge[1] in done]
        if not ready:
            raise ValueError("Cyclic or disconnected joint graph")
        for joint, a, b, kind in ready:
            motion = Gf.Matrix4d(1)
            if kind != "fixed":
                name = str(joint.GetPath())
                value = positions[name]
                if type(value) not in (int, float) or not math.isfinite(value):
                    raise ValueError(f"Finite numeric joint position required: {name}")
                native = value if kind == "angular" else value / (mpu * scales[a])
                schema = movable[name]
                low, high = _limits(schema)
                if low <= high and not low <= native <= high:
                    raise ValueError(f"Joint limit exceeded: {name}: {native} outside [{low}, {high}]")
                axis = Gf.Vec3d(0)
                axis["XYZ".index(str(schema.GetAxisAttr().Get()))] = 1
                if kind == "angular":
                    motion.SetRotate(Gf.Rotation(axis, native))
                else:
                    motion.SetTranslate(axis * (value / mpu))
            child_scale = Gf.Matrix4d(1)
            child_scale.SetScale(scales[b])
            joint_world = _rigid_frame(_frame(joint, 0) * worlds[a])
            worlds[b] = _frame(joint, 1).GetInverse() * child_scale * motion * joint_world
            ordered.append((joint, a, b, kind))
            done.add(b)
            remaining.remove((joint, a, b, kind))
    # Compose edits in an anonymous layer first. A rejected pose leaves no candidate.
    layer = Sdf.Layer.CreateAnonymous("pose.usda")
    layer.subLayerPaths = [str(source)]
    dst = Usd.Stage.Open(layer)
    for key in ("metersPerUnit", "upAxis", "defaultPrim"):
        if stage.HasAuthoredMetadata(key):
            dst.SetMetadata(key, stage.GetMetadata(key))
    root_prim = dst.GetPrimAtPath(root)
    if root_prim.IsInstance():
        root_prim.SetInstanceable(False)
    for joint, a, b, kind in ordered:
        body = dst.GetPrimAtPath(b)
        if body.IsInstanceProxy():
            raise ValueError("Nested instance body cannot be posed; use a non-instanceable articulation root")
        parent = body.GetParent()
        parent_world = UsdGeom.XformCache().GetLocalToWorldTransform(parent)
        local = worlds[b] * parent_world.GetInverse()
        xform = UsdGeom.Xformable(body)
        if xform.GetResetXformStack():
            local = worlds[b]
        op = xform.MakeMatrixXform()
        op.Set(local)
        if kind != "fixed":
            j = dst.GetPrimAtPath(joint.GetPath())
            drive = UsdPhysics.DriveAPI(j, kind)
            if drive:
                value = positions[str(joint.GetPath())]
                drive.GetTargetPositionAttr().Set(value if kind == "angular" else value / (mpu * scales[a]))
            # Keep an authored runtime state consistent with the new static pose.
            # Do not invent a state API on assets that do not use one.
            state = j.GetAttribute(f"state:{kind}:physics:position")
            if state and state.HasAuthoredValueOpinion():
                value = positions[str(joint.GetPath())]
                state.Set(value if kind == "angular" else value / (mpu * scales[a]))
                velocity = j.GetAttribute(f"state:{kind}:physics:velocity")
                if velocity and velocity.HasAuthoredValueOpinion():
                    velocity.Set(0.0)
    # Rebase moved world anchors instead of disabling physical constraints.
    for joint, body in world_anchors:
        j = UsdPhysics.Joint(dst.GetPrimAtPath(joint.GetPath()))
        world = UsdGeom.XformCache().GetLocalToWorldTransform(dst.GetPrimAtPath(body.GetPath()))
        frame = _frame(j, 1) * world
        j.GetLocalPos0Attr().Set(Gf.Vec3f(frame.ExtractTranslation()))
        j.GetLocalRot0Attr().Set(Gf.Quatf(frame.RemoveScaleShear().ExtractRotationQuat()))
    audit = inspect_joints(layer.identifier, roots=[root])
    if audit["status"] != "pass":
        raise ValueError(f"Pose failed joint-frame verification: {audit['errors']}")
    output.parent.mkdir(parents=True, exist_ok=True)
    layer.customLayerData = {"edit_source": str(source), "edit_source_sha256": sha256(source),
                             "pose_scope": "static_joint_tree_not_ik_or_dynamics"}
    layer.Export(str(output))
    return {"status": "pass", "scene_path": str(output), "source_path": str(source),
            "root": root, "positions": positions, "joint_check": audit,
            "limitations": audit["limitations"]}


def initialize_articulation(source, *, root, position_source, output=None):
    """Plan explicit initialization from authored data; optionally write a new pose.

    No guessed zero positions, clamping, fallback between sources, IK or simulation.
    A missing/invalid value blocks the entire operation before any file is written.
    """
    from pxr import Usd, UsdGeom, UsdPhysics
    if position_source not in {"drive-targets", "authored-state"}:
        raise ValueError("position_source must be drive-targets or authored-state")
    stage = Usd.Stage.Open(str(source))
    if not stage:
        raise ValueError(f"Cannot open {source}")
    mpu, cache = UsdGeom.GetStageMetersPerUnit(stage), UsdGeom.XformCache()
    if not math.isfinite(mpu) or mpu <= 0:
        raise ValueError("Invalid metersPerUnit")
    positions, evidence, errors = {}, [], []
    for joint, bodies in _joint_rows(stage, root):
        prim, name = joint.GetPrim(), str(joint.GetPath())
        if prim.IsA(UsdPhysics.FixedJoint):
            continue
        if not (prim.IsA(UsdPhysics.RevoluteJoint) or prim.IsA(UsdPhysics.PrismaticJoint)):
            errors.append({"joint": name, "code": "UNSUPPORTED_JOINT_TYPE"})
            continue
        if any(b is None for b in bodies):
            errors.append({"joint": name, "code": "UNSUPPORTED_WORLD_JOINT"})
            continue
        angular = prim.IsA(UsdPhysics.RevoluteJoint)
        kind = "angular" if angular else "linear"
        attr_name = (f"drive:{kind}:physics:targetPosition" if position_source == "drive-targets"
                     else f"state:{kind}:physics:position")
        native = _authored_number(prim, attr_name)
        if native is None:
            errors.append({"joint": name, "code": "AUTHORED_POSITION_REQUIRED", "attribute": attr_name})
            continue
        schema = UsdPhysics.RevoluteJoint(prim) if angular else UsdPhysics.PrismaticJoint(prim)
        low, high = _limits(schema)
        if low <= high and not low <= native <= high:
            errors.append({"joint": name, "code": "INITIAL_POSITION_OUTSIDE_LIMITS", "value": native,
                           "limits": _json_limits([low, high])})
            continue
        scale = _uniform_scale(cache.GetLocalToWorldTransform(bodies[0]))
        value = native if angular else native * scale * mpu
        positions[name] = value
        evidence.append({"joint": name, "attribute": attr_name, "native_value": native,
                         "value": value, "unit": "deg" if angular else "m"})
    result = {"status": "pending" if errors or not positions else "pass", "source_path": str(Path(source).resolve()),
              "root": root, "position_source": position_source, "positions": positions,
              "position_evidence": evidence, "errors": errors, "mutated_scene": False,
              "action": "plan_only", "physics_verified": False}
    if output and result["status"] == "pass":
        result.update(pose_joints(source, output, root=root, positions=positions))
        result.update(action="new_static_pose_revision", mutated_scene=False)
    return result
