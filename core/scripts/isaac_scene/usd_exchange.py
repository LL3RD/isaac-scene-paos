"""Non-destructive USD entrypoints and independently referenceable materials."""

from __future__ import annotations

import hashlib
import os
import sysconfig
from pathlib import Path


def _runtime_mdl_file(asset_path):
    """Resolve a bare built-in MDL name without importing/starting Isaac.

    Respect explicit search paths, then only the current Python's Isaac kit.
    Do not basename-repair missing absolute paths, textures or package paths.
    The resolved file remains in the dependency hash, so changes invalidate it.
    """
    name = Path(asset_path)
    if name.suffix.lower() != ".mdl" or name.name != asset_path:
        return None
    roots = []
    for variable in ("PXR_AR_DEFAULT_SEARCH_PATH", "MDL_SYSTEM_PATH"):
        roots.extend(Path(p) for p in os.environ.get(variable, "").split(os.pathsep) if p)
    purelib = sysconfig.get_path("purelib")
    if purelib:
        roots.append(Path(purelib) / "isaacsim/kit/mdl/core/Base")
    for root in roots:
        candidate = root / name
        if candidate.is_file():
            return str(candidate.resolve())
    return None


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def scene_identity(path):
    """Hash the root and composed layer stack, not only a tiny override file.

    Texture dependencies are included when resolvable by USD. Runtime MDL search
    modules are recorded separately rather than mislabelled missing images.
    """
    from pxr import Ar, Usd, Sdf

    path = Path(path).resolve()
    stage = Usd.Stage.Open(str(path))
    if not stage:
        raise ValueError(f"Cannot open USD: {path}")
    files = {str(path)}

    def resident_file(value):
        # A USDZ entry is not a filesystem path. Hash its enclosing archive.
        while Ar.IsPackageRelativePath(value):
            value, _ = Ar.SplitPackageRelativePathOuter(value)
        candidate = Path(value).resolve()
        return str(candidate) if candidate.is_file() else None

    for layer in stage.GetUsedLayers():
        if layer.realPath:
            resident = resident_file(layer.realPath)
            if resident:
                files.add(resident)
    unresolved = set()
    for prim in Usd.PrimRange.Stage(stage, Usd.TraverseInstanceProxies()):
        for attr in prim.GetAttributes():
            if attr.GetTypeName() not in (
                Sdf.ValueTypeNames.Asset,
                Sdf.ValueTypeNames.AssetArray,
            ):
                continue
            value = attr.Get()
            for asset in (
                value
                if attr.GetTypeName() == Sdf.ValueTypeNames.AssetArray
                else [value]
            ) or []:
                if not asset or not asset.path:
                    continue
                resident = (
                    resident_file(asset.resolvedPath) if asset.resolvedPath else None
                )
                if resident is None:
                    resident = _runtime_mdl_file(asset.path)
                if resident:
                    files.add(resident)
                else:
                    unresolved.add(asset.path)
    manifest = {name: sha256(name) for name in sorted(files)}
    import json

    return {
        "scene_path": str(path),
        "scene_sha256": sha256(path),
        "composed_sha256": hashlib.sha256(
            json.dumps(manifest, sort_keys=True).encode()
        ).hexdigest(),
        "files": manifest,
        "unresolved_assets": sorted(unresolved),
    }


def create_revision(source, output, deactivate=()):
    """An editable stronger layer. No guessed new shell, physics or transforms."""
    from pxr import Sdf, Usd, UsdGeom

    source, output = Path(source).resolve(), Path(output).resolve()
    if output.exists() or output == source:
        raise ValueError("Output must be a new USD file")
    if output.suffix.lower() not in {".usd", ".usda", ".usdc"}:
        raise ValueError("Output must use a USD extension")
    src = Usd.Stage.Open(str(source))
    if not src:
        raise ValueError(f"Cannot open {source}")
    for path in deactivate:
        if (
            not Sdf.Path(path).IsAbsoluteRootOrPrimPath()
            or path == "/"
            or not src.GetPrimAtPath(path)
        ):
            raise ValueError(f"Unknown or non-prim deactivation target: {path}")
        if src.GetDefaultPrim() and str(src.GetDefaultPrim().GetPath()) == path:
            raise ValueError("Cannot deactivate the complete default scene root")
    output.parent.mkdir(parents=True, exist_ok=True)
    dst = Usd.Stage.CreateNew(str(output))
    dst.GetRootLayer().subLayerPaths = [os.path.relpath(source, output.parent)]
    for key in (
        "metersPerUnit",
        "upAxis",
        "defaultPrim",
        "startTimeCode",
        "endTimeCode",
        "timeCodesPerSecond",
        "framesPerSecond",
    ):
        if src.HasAuthoredMetadata(key):
            dst.SetMetadata(key, src.GetMetadata(key))
    for path in deactivate:
        dst.OverridePrim(path).SetActive(False)
    dst.GetRootLayer().customLayerData = {
        "edit_source": str(source),
        "edit_source_sha256": sha256(source),
    }
    dst.GetRootLayer().Save()
    return {
        "status": "pending",
        "scene_path": str(output),
        "source_path": str(source),
        "source_sha256": sha256(source),
        "deactivated_prims": list(deactivate),
        "camera_paths": [
            str(p.GetPath()) for p in src.Traverse() if p.IsA(UsdGeom.Camera)
        ],
        "meters_per_unit": UsdGeom.GetStageMetersPerUnit(dst),
        "up_axis": str(UsdGeom.GetStageUpAxis(dst)),
        "validation_pending": [
            "changed entity support/clearance",
            "materials/render",
            "physics if requested",
        ],
    }


def prepare_assets(source, output, prim_paths):
    """Bring resolved materials inside each exported asset's reference boundary.

    Mesh/GeomSubset bindings are resolved by USD (including inherited bindings),
    then bound to local material references. Geometry and source files stay intact.
    """
    from pxr import Sdf, Usd, UsdGeom, UsdShade

    if not prim_paths:
        raise ValueError("Select at least one --prim; never guess asset groups")
    src = Usd.Stage.Open(str(Path(source).resolve()))
    if not src:
        raise ValueError("Cannot open source USD")
    selected = [Sdf.Path(value) for value in prim_paths]
    if len(set(selected)) != len(selected):
        raise ValueError("Select each asset root once")
    for path in selected:
        if (
            not path.IsAbsoluteRootOrPrimPath()
            or path == Sdf.Path.absoluteRootPath
            or not src.GetPrimAtPath(path)
        ):
            raise ValueError(f"Unknown asset prim: {path}")
        if any(path != other and path.HasPrefix(other) for other in selected):
            raise ValueError("Select disjoint asset roots, not nested duplicates")
        if src.GetPrimAtPath(path.AppendChild("__PortableMaterials")):
            raise ValueError(f"Reserved material scope already exists under {path}")
        if any(prim.IsInstance() for prim in Usd.PrimRange(src.GetPrimAtPath(path))):
            raise ValueError(f"Instanced subtree {path}: realize instances in a new export or reference the whole stage")
    source_hash = sha256(source)
    create_revision(source, output)
    dst = Usd.Stage.Open(str(Path(output).resolve()))
    asset_reference = os.path.relpath(
        Path(source).resolve(), Path(output).resolve().parent
    )
    rows = []
    for root in selected:
        UsdGeom.Scope.Define(dst, root.AppendChild("__PortableMaterials"))
        materials = {}
        bound = 0
        missing = []
        for prim in Usd.PrimRange(src.GetPrimAtPath(root)):
            if not (prim.IsA(UsdGeom.Imageable) or prim.IsA(UsdGeom.Subset)):
                continue
            if (
                prim.IsA(UsdGeom.Gprim)
                and UsdGeom.Imageable(prim).ComputeVisibility() == "invisible"
            ):
                continue
            for purpose in ("", "preview", "full"):
                material, _ = UsdShade.MaterialBindingAPI(prim).ComputeBoundMaterial(
                    purpose
                )
                if not material:
                    if purpose == "" and prim.IsA(UsdGeom.Gprim):
                        missing.append(str(prim.GetPath()))
                    continue
                source_mat = str(material.GetPath())
                if source_mat not in materials:
                    name = "M_" + hashlib.sha256(source_mat.encode()).hexdigest()[:16]
                    local = root.AppendChild("__PortableMaterials").AppendChild(name)
                    UsdShade.Material.Define(
                        dst, local
                    ).GetPrim().GetReferences().AddReference(
                        asset_reference, source_mat
                    )
                    # Shader connections outside a Material cannot survive a
                    # subtree reference. Fail visibly instead of claiming repair.
                    for node in Usd.PrimRange(material.GetPrim()):
                        for attr in node.GetAttributes():
                            if any(
                                not connection.GetPrimPath().HasPrefix(
                                    material.GetPath()
                                )
                                for connection in attr.GetConnections()
                            ):
                                raise ValueError(
                                    f"Material graph leaves its root: {source_mat}; bake or package its node graph first"
                                )
                    materials[source_mat] = str(local)
                target = UsdShade.Material(dst.GetPrimAtPath(materials[source_mat]))
                UsdShade.MaterialBindingAPI.Apply(
                    dst.OverridePrim(prim.GetPath())
                ).Bind(target, materialPurpose=purpose)
                bound += 1
        rows.append(
            {
                "prim": str(root),
                "materials": materials,
                "resolved_bindings": bound,
                "unbound_geometry": sorted(set(missing)),
            }
        )
    dst.GetRootLayer().Save()
    return {
        "status": "warn" if any(row["unbound_geometry"] for row in rows) else "pass",
        "scene_path": str(Path(output).resolve()),
        "assets": rows,
        "camera_paths": [
            str(p.GetPath()) for p in src.Traverse() if p.IsA(UsdGeom.Camera)
        ],
        "source_unchanged": source_hash == sha256(source),
        "scope": "Reference/material packaging only; render, dimensions and physics still require verification",
    }
