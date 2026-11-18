"""Portable source-service smoke test using generated meshes, never private assets.

Run in the provider environment on Linux; --isaac-python is the external USD
interpreter. Optional rendering additionally needs Isaac Sim and an NVIDIA GPU.
This tests the real service/worker/core, not Gateway transport or AgentTask.
"""
import argparse
import asyncio
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]


def write(path, value):
    with Path(path).open("x") as stream:
        json.dump(value, stream, indent=2, allow_nan=False)


def box_mesh(size):
    """A visible closed box mesh with its pivot at bottom centre, metres/Z-up."""
    x, y, z = size
    points = [(sx*x/2, sy*y/2, h) for h in (0, z) for sx, sy in ((-1,-1),(1,-1),(1,1),(-1,1))]
    return ('#usda 1.0\n(defaultPrim="Model"\nmetersPerUnit=1\nupAxis="Z")\n'
            'def Xform "Model" {\n def Mesh "Body" {\n'
            f'  point3f[] points = {points}\n'
            '  int[] faceVertexCounts = [4,4,4,4,4,4]\n'
            '  int[] faceVertexIndices = [3,2,1,0,4,5,6,7,0,1,5,4,1,2,6,5,2,3,7,6,3,0,4,7]\n'
            '  uniform token subdivisionScheme = "none"\n'
            '  color3f[] primvars:displayColor = [(0.24,0.48,0.65)]\n }\n}\n')


def prepare(output, interpreter):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    os.chmod(output, 0o700)
    assets = {}
    for name, size, mode in (("bench", [2, 1.4, .8], "floor"), ("part", [.25, .25, .2], "surface"),
                             ("oversize", [4, 4, .2], "surface")):
        asset = output / (name + ".usda")
        with asset.open("x") as stream:
            stream.write(box_mesh(size))
        assets[name] = {"path": str(asset), "label": name, "tags": [name], "support_mode": mode,
                        "front_axis": "not_applicable", "metadata": {"size_m": size},
                        "provenance": {"source_asset_id": name, "license_status": "Apache-2.0",
                            "allowed_use_scope": "synthetic_test", "attribution": "Isaac Scene contributors"}}
    config = {"core_root": str(ROOT / "core"), "isaac_python": str(Path(interpreter).absolute()),
              "work_root": str(output / "jobs"), "gpu_lock": str(output / "gpu.lock"),
              "gpu_index": 0, "job_timeout_s": 180, "assets": assets}
    write(output / "deployment.json", config)
    layout = {"floor_size_m": [5, 4], "camera": {"position_m": [3.5, -4.5, 3.1],
        "look_at_m": [0, 0, .9], "focal_length_mm": 22}, "entities": [
            {"id": "bench", "asset_id": "bench", "position_m": [0, 0, .8], "yaw_deg": 0, "support_id": "floor"},
            *[{"id": name, "asset_id": "part", "position_m": [0, 0, 2], "yaw_deg": 0, "support_id": "bench"}
              for name in ("left_part", "right_part")]]}
    write(output / "layout.json", layout)
    return output, layout


async def exercise(output, layout, render=False):
    sys.path.insert(0, str(ROOT / ".deps/gateway/src"))
    sys.path.insert(0, str(ROOT / "adapter"))
    from forge_tool import ToolContext, ToolExecutionKey, ToolRequest
    from contracts import ENDPOINT, IMPLEMENTATION
    from service import SceneService, Operation

    class Events:
        async def emit(self, event):
            pass

    service = SceneService(output / "deployment.json")
    checks = {}

    async def action(op, arguments):
        context = ToolContext(execution_key=ToolExecutionKey(uuid.uuid4().hex, "attempt"),
            tool_id="isaac." + op.replace("_", ".", 1), implementation_id=IMPLEMENTATION,
            endpoint_id=ENDPOINT, operation=op)
        operation = Operation(service, op)
        await operation.start(ToolRequest(arguments), context, Events())
        await asyncio.gather(*list(service.tasks.values()))
        result = await operation.result(context.execution_key)
        if result.result.status != "succeeded":
            raise RuntimeError(f"{op}: {result.result.status}; inspect private job logs")
        print(op + ": succeeded", flush=True)
        return result.result.outputs

    def inspect(scene):
        return service.query("scene_inspect", {"scene_id": scene["scene_id"]})

    try:
        found = service.query("asset_search", {"query": "part", "source": "registry"})
        assert [a["asset_id"] for a in found["assets"]] == ["part"]
        assert all("path" not in a for a in found["assets"])
        checks["retrieval"] = "pass"
        built = await action("scene_build", {"layout": layout})
        source = service.root / service.scene(built["scene_id"])["job"] / "scene.usda"
        original_sha = hashlib.sha256(source.read_bytes()).hexdigest()
        before = await action("scene_validate", {"scene_id": built["scene_id"]})
        assert before["checks"]["support"] == "fail"
        solved = await action("scene_solve", {"scene_id": built["scene_id"]})
        assert solved["placement"]["status"] == "pass"
        assert solved["placement"]["order"][0] == "bench"
        assert len(solved["placement"]["placed_ids"]) == 3
        contacts = solved["placement"]["contacts"]
        assert any(c["obstacle_ids"] for c in contacts if c["entity_id"] != "bench")
        assert all(c["mesh_coverage"] >= .95 for c in contacts)
        checked = await action("scene_validate", {"scene_id": solved["scene_id"]})
        assert checked["checks"]["support"] == checked["checks"]["dependencies"] == "pass"
        assert hashlib.sha256(source.read_bytes()).hexdigest() == original_sha
        checks.update(dependency_order="pass", mesh_support="pass", automatic_obstacles="pass", immutable_source="pass")
        selected = await action("scene_solve", {"scene_id": solved["scene_id"], "entity_ids": ["bench"]})
        assert set(selected["placement"]["placed_ids"]) == {"bench", "left_part", "right_part"}
        leaf = await action("scene_solve", {"scene_id": solved["scene_id"], "entity_ids": ["right_part"]})
        assert set(leaf["placement"]["frozen_ids"]) == {"bench", "left_part"}
        old = {e["id"]: e for e in inspect(solved)["layout"]["entities"]}
        assert all(e == old[e["id"]] for e in inspect(leaf)["layout"]["entities"] if e["id"] != "right_part")
        checks.update(descendant_closure="pass", frozen_objects="pass")
        bad = copy.deepcopy(layout)
        bad["entities"] = [bad["entities"][0], {"id": "oversize", "asset_id": "oversize",
            "position_m": [0, 0, 1], "yaw_deg": 0, "support_id": "bench"}]
        edited = await action("scene_edit", {"scene_id": solved["scene_id"], "layout": bad})
        failed = await action("scene_solve", {"scene_id": edited["scene_id"]})
        assert failed["placement"]["status"] == "fail" and failed["placement"]["pending_ids"] == ["oversize"]
        repeated = await action("scene_solve", {"scene_id": failed["scene_id"]})
        assert repeated["placement"]["status"] == "fail" and repeated["placement"]["pending_ids"] == ["oversize"]
        gap = await action("scene_validate", {"scene_id": repeated["scene_id"]})
        assert gap["checks"]["support"] == "fail"
        checks["persistent_quarantine"] = "pass"
        if render:
            await action("scene_render", {"scene_id": solved["scene_id"], "profile": "preview"})
        checks.update(render="pass" if render else "not_run", gateway="not_run", agent_task="not_run",
                      physics="not_run", reachability="not_run")
        write(output / "summary.json", {"status": "pass", "fixture": "generated_mesh_boxes", "checks": checks})
    finally:
        await service.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--isaac-python", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--render", action="store_true")
    args = parser.parse_args()
    output, layout = prepare(args.output, args.isaac_python)
    if not args.prepare_only:
        asyncio.run(exercise(output, layout, args.render))


if __name__ == "__main__":
    main()
