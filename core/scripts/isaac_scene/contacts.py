"""Read-only solid contact checks for pockets/fixtures, not a PhysX simulation.

Manifold Boolean intersection checks the full solids (including containment).
Deterministic downward-surface probes measure contact, separately from penetration.
Unsupported/open geometry and probe budgets remain pending, never a guessed pass.
"""
from __future__ import annotations

import math


class ContactUnavailable(ValueError):
    pass


def _validate_linear_faces(mesh):
    """Allow triangles and planar convex quads, never guess n-gon tessellation."""
    import numpy as np
    counts = list(mesh.GetFaceVertexCountsAttr().Get() or [])
    indices = list(mesh.GetFaceVertexIndicesAttr().Get() or [])
    points = np.asarray(mesh.GetPointsAttr().Get(), dtype=float)
    if (mesh.GetHoleIndicesAttr().Get() or not counts or sum(counts) != len(indices)
            or any(n not in (3, 4) for n in counts)
            or not len(points) or min(indices) < 0 or max(indices) >= len(points)):
        raise ContactUnavailable(f"Requires triangles or planar convex quads without holes: {mesh.GetPath()}")
    offset = 0
    for n in counts:
        face = points[indices[offset:offset+n]]
        offset += n
        if n == 3:
            continue
        edges = np.roll(face, -1, axis=0) - face
        scale = float(np.linalg.norm(edges, axis=1).max())
        normal = np.cross(edges[0], edges[1])
        length = float(np.linalg.norm(normal))
        if not np.isfinite(face).all() or scale == 0 or length <= scale*scale*1e-12:
            raise ContactUnavailable(f"Degenerate quad: {mesh.GetPath()}")
        normal /= length
        if (abs(float(np.dot(face[3]-face[0], normal))) > scale*1e-8
                or any(float(np.dot(np.cross(edges[i], edges[(i+1)%4]), normal))
                       <= scale*scale*1e-12 for i in range(4))):
            raise ContactUnavailable(f"Nonplanar or nonconvex quad needs evaluated triangles: {mesh.GetPath()}")


def load_solid(prim, mpu, *, max_triangles=200000, components=None):
    import numpy as np
    import trimesh
    from pxr import Usd, UsdGeom
    from .support import world_mesh_points_and_triangles

    meshes, paths, count = [], [], 0
    for leaf in Usd.PrimRange(prim, Usd.TraverseInstanceProxies()):
        if not leaf.IsA(UsdGeom.Boundable):
            continue
        imageable = UsdGeom.Imageable(leaf)
        if (imageable.ComputeVisibility() == UsdGeom.Tokens.invisible
                or imageable.ComputePurpose() not in (UsdGeom.Tokens.default_, UsdGeom.Tokens.render)):
            continue
        if not (leaf.IsA(UsdGeom.Mesh) or leaf.IsA(UsdGeom.Cube)):
            raise ContactUnavailable(f"Unsupported visible geometry {leaf.GetTypeName()}: {leaf.GetPath()}")
        if leaf.IsA(UsdGeom.Mesh):
            usd_mesh = UsdGeom.Mesh(leaf)
            if usd_mesh.GetSubdivisionSchemeAttr().Get() != UsdGeom.Tokens.none:
                raise ContactUnavailable(f"Subdivision surface needs an evaluated triangle snapshot: {leaf.GetPath()}")
            _validate_linear_faces(usd_mesh)
            if usd_mesh.GetPointsAttr().GetNumTimeSamples():
                raise ContactUnavailable(f"Animated mesh needs an explicit time snapshot: {leaf.GetPath()}")
        geometry = world_mesh_points_and_triangles(leaf, Usd, UsdGeom,
            render_visible_only=True, coordinate_scale=mpu)
        if not geometry:
            raise ContactUnavailable(f"Empty visible geometry: {leaf.GetPath()}")
        vertices, faces = geometry[0]
        count += len(faces)
        if count > max_triangles:
            raise ContactUnavailable(f"Triangle budget exceeded: {count} > {max_triangles}")
        vertices, faces = np.asarray(vertices, dtype=float), np.asarray(faces, dtype=np.int64)
        if not np.isfinite(vertices).all():
            raise ContactUnavailable(f"Nonfinite mesh points: {leaf.GetPath()}")
        # world_mesh_points_and_triangles already applies authored orientation
        # and transform handedness. Do not flip it a second time here.
        mesh = trimesh.Trimesh(vertices=vertices, faces=faces, process=False)
        # USD normals/UV seams can duplicate positions. Weld only numerical
        # duplicates; do not fill holes, repair winding or replace with a hull.
        mesh.merge_vertices(digits_vertex=10)
        if not mesh.is_volume:
            raise ContactUnavailable(f"Mesh is not a closed, consistently oriented positive solid: {leaf.GetPath()}")
        meshes.append(mesh)
        paths.append(str(leaf.GetPath()))
        if components is not None:
            components[str(leaf.GetPath())] = mesh
    if not meshes:
        raise ContactUnavailable(f"No supported visible solid: {prim.GetPath()}")
    if len(meshes) == 1:
        solid = meshes[0]
    else:
        # Work near the origin to avoid float32 Boolean precision loss in large factories.
        origin = np.mean(np.vstack([m.bounds for m in meshes]), axis=0)
        moved = [m.copy() for m in meshes]
        for m in moved:
            m.apply_translation(-origin)
        solid = trimesh.boolean.union(moved, engine="manifold", check_volume=True)
        solid.apply_translation(origin)
    if not solid.is_volume:
        raise ContactUnavailable("Solid union did not produce a valid volume")
    return solid, {"mesh_prims": paths, "source_triangle_count": count,
                   "solid_triangle_count": len(solid.faces), "volume_m3": float(solid.volume)}


def downward_probes(mesh, pitch, max_samples):
    """Uniform barycentric grid per downward triangle; area weights avoid mesh-density bias."""
    import numpy as np
    points, weights, normals = [], [], []
    count = 0
    for face, normal, area in zip(mesh.triangles, mesh.face_normals, mesh.area_faces):
        if normal[2] >= -.1 or area <= 0:
            continue
        longest = max(float(np.linalg.norm(face[i] - face[(i+1) % 3])) for i in range(3))
        n = max(1, math.ceil(longest / pitch))
        size = (n+1) * (n+2) // 2
        count += size
        if count > max_samples:
            raise ContactUnavailable(f"Contact probe budget exceeded: > {max_samples}; use a smaller object root or explicit coarser diagnostic")
        for i in range(n+1):
            for j in range(n+1-i):
                points.append(face[0] + (face[1]-face[0])*(i/n) + (face[2]-face[0])*(j/n))
                weights.append(area / size)
                normals.append(normal)
    if not points:
        raise ContactUnavailable("No downward-facing contact surface")
    return np.asarray(points), np.asarray(weights), np.asarray(normals)


def _intersection(object_solid, support_solid):
    import numpy as np
    import trimesh
    a, b = object_solid.copy(), support_solid.copy()
    origin = (a.bounds.mean(axis=0) + b.bounds.mean(axis=0)) * .5
    a.apply_translation(-origin)
    b.apply_translation(-origin)
    intersection = trimesh.boolean.intersection([a, b], engine="manifold", check_volume=True)
    overlap = 0.0 if intersection.is_empty else float(intersection.volume)
    if not math.isfinite(overlap) or overlap < -1e-12:
        raise ContactUnavailable("Boolean returned an invalid overlap volume")
    overlap = max(0.0, overlap)
    volume_tolerance = max(1e-12, min(float(a.volume), float(b.volume)) * 1e-8)
    return overlap, volume_tolerance, a, b, origin


def _overlap_sources(object_solid, components, *, max_candidates=32):
    """Localize a known overlap without confusing a bundled part with its fixture."""
    rows, checked = [], 0
    for path, mesh in components.items():
        if ((object_solid.bounds[1] < mesh.bounds[0]).any()
                or (mesh.bounds[1] < object_solid.bounds[0]).any()):
            continue
        if checked >= max_candidates:
            return rows, False
        checked += 1
        volume, tolerance, *_ = _intersection(object_solid, mesh)
        if volume > tolerance:
            rows.append({"support_mesh": path, "intersection_volume_m3": volume})
    return rows, True


def solid_contact(object_solid, support_solid, *, tolerance_m, minimum_coverage, pitch_m, max_samples):
    import numpy as np
    import trimesh
    overlap, volume_tolerance, a, b, origin = _intersection(object_solid, support_solid)
    result = {"contact_model": "solid", "geometry_scope": "render_visible_mesh_not_runtime_collider",
              "penetration_method": "manifold_full_solid_intersection", "penetration_check_complete": True,
              "intersection_volume_m3": overlap, "numerical_volume_tolerance_m3": volume_tolerance,
              "sample_pitch_m": pitch_m, "physics_verified": False}
    if overlap > volume_tolerance:
        return {**result, "status": "solid_interpenetration", "contact_sampling_complete": False}
    points, weights, normals = downward_probes(a, pitch_m, max_samples)
    closest, distances, indices = trimesh.proximity.closest_point(b, points)
    opposite = np.einsum("ij,ij->i", normals, b.face_normals[indices]) < -.5
    # Lateral proximity is not upward support; keep the lower/upper orientation explicit.
    supported = ((distances <= tolerance_m) & opposite & (b.face_normals[indices, 2] > .1)
                 & (closest[:, 2] <= points[:, 2] + tolerance_m))
    coverage = float(weights[supported].sum() / weights.sum())
    result.update(contact_sampling_complete=True, contact_point_count=len(points),
                  supported_contact_point_count=int(supported.sum()), mesh_support_coverage=coverage,
                  minimum_contact_distance_m=float(distances.min()),
                  maximum_contact_distance_m=float(distances.max()),
                  contact_area_estimate_m2=float(weights[supported].sum()),
                  contact_point_samples_m=(points[supported][:12] + origin).tolist())
    return {**result, "status": "pass" if coverage >= minimum_coverage else "insufficient_solid_contact"}


def inspect_solid_contacts(scene, pairs, *, tolerance_m=.005, minimum_coverage=.95,
                           pitch_m=.01, max_samples=20000):
    from pxr import Usd, UsdGeom, Sdf
    result = {"scene_path": str(scene), "status": "pending", "coordinate_unit": "m", "contact_mode": "solid",
              "mesh_aware": True, "minimum_mesh_coverage": minimum_coverage, "tolerance_m": tolerance_m,
              "sample_pitch_m": pitch_m, "contacts": [], "errors": [], "warnings": [],
              "limitations": ["Visual solid intersection and sampled contact only; not runtime collider, friction, stability, IK or dynamics proof"]}
    if (not math.isfinite(tolerance_m) or not 0 <= tolerance_m <= .01 or not 0 < minimum_coverage <= 1
            or not math.isfinite(pitch_m) or not 0 < pitch_m <= .1 or type(max_samples) is not int or max_samples < 1):
        raise ValueError("Invalid contact tolerances, coverage or probe budget")
    try:
        import manifold3d  # noqa: F401: dependency preflight before loading geometry
        import trimesh
        import rtree  # noqa: F401
    except ImportError as exc:
        result["warnings"].append({"kind": "contact_backend_unavailable", "detail": str(exc)})
        return result
    stage = Usd.Stage.Open(str(scene))
    if not stage:
        raise ValueError(f"Cannot open {scene}")
    if not stage.HasAuthoredMetadata("metersPerUnit"):
        result["warnings"].append({"kind": "authored_units_required",
            "detail": "USD's fallback centimetres are not evidence of the asset's physical units"})
        return result
    mpu = float(UsdGeom.GetStageMetersPerUnit(stage))
    if not math.isfinite(mpu) or mpu <= 0 or UsdGeom.GetStageUpAxis(stage) != UsdGeom.Tokens.z:
        raise ValueError("Solid support requires valid metresPerUnit and Z-up")
    result["meters_per_unit"] = mpu
    result["backend"] = {"trimesh": trimesh.__version__, "boolean": "manifold"}
    cache, components = {}, {}
    for obj, sup in pairs:
        row = {"object": obj, "support": sup}
        if (not Sdf.Path(obj).IsAbsolutePath() or not Sdf.Path(sup).IsAbsolutePath()
                or Sdf.Path(obj).HasPrefix(Sdf.Path(sup)) or Sdf.Path(sup).HasPrefix(Sdf.Path(obj))):
            result["errors"].append({**row, "kind": "distinct_nonoverlapping_prim_roots_required"})
            continue
        try:
            for path in (obj, sup):
                if path not in cache:
                    prim = stage.GetPrimAtPath(path)
                    if not prim:
                        raise ContactUnavailable(f"Missing prim: {path}")
                    components[path] = {}
                    cache[path] = load_solid(prim, mpu, components=components[path])
            a, am = cache[obj]
            b, bm = cache[sup]
            row.update(solid_contact(a, b, tolerance_m=tolerance_m, minimum_coverage=minimum_coverage,
                                    pitch_m=pitch_m, max_samples=max_samples), object_geometry=am, support_geometry=bm)
            if row["status"] == "solid_interpenetration":
                try:
                    sources, complete = _overlap_sources(a, components[sup])
                    row.update(overlap_sources=sources, overlap_localization_complete=complete)
                except (ContactUnavailable, ValueError, RuntimeError, IndexError) as exc:
                    # The full-solid failure is already established; optional
                    # localization failure must not turn it into a pending result.
                    row.update(overlap_localization_complete=False, overlap_localization_detail=str(exc))
            if row["status"] != "pass":
                result["errors"].append({**row, "kind": row["status"]})
        except (ContactUnavailable, ValueError, RuntimeError, IndexError) as exc:
            row.update(status="pending", reason="contact_geometry_or_backend_unavailable", detail=str(exc))
            result["warnings"].append(row.copy())
        result["contacts"].append(row)
    result["status"] = "fail" if result["errors"] else ("pending" if result["warnings"] or not pairs else "pass")
    return result
