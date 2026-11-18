"""Fixed worker entry point. Loads the unchanged scene core in Isaac's interpreter."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import copy

from catalog import Catalog


def core_scripts(config, location=None):
    """Resolve the packaged engine first; source runs may use an operator override."""
    location = Path(location or __file__).resolve().parent
    bundled = location / "scene_core"
    if bundled.is_dir():
        if config.get("core_root"):
            raise ValueError("Packaged provider uses its locked engine; omit core_root")
        root = bundled
    else:
        root = Path(config["core_root"]) if config.get("core_root") else location.parent / "core"
    scripts = root.resolve() / "scripts"
    if not (scripts / "scene_tools.py").is_file():
        raise ValueError("Scene engine is unavailable; check the provider installation")
    return scripts


def write(path, value):
    with path.open("x") as stream:
        json.dump(value, stream, indent=2, allow_nan=False)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--request", type=Path, required=True)
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    request = json.loads(args.request.read_text())
    op, parameters = request["operation"], request["arguments"]
    directory = Path(request["job_dir"])
    scripts = core_scripts(config)
    sys.path.insert(0, str(scripts))
    import scene_tools
    catalog = Catalog(config)
    core_config = {"libraries": [], "runtime": {"output_root": str(directory)}}
    scene = directory / "scene.usda"
    if op in {"scene_build", "scene_edit"}:
        layout = parameters["layout"]
        camera = layout["camera"]
        spec = {
            "schema_version": "isaac-scene-spec/v2",
            "domain": "generic_scene",
            "stage": {"up_axis": "Z", "meters_per_unit": 1., "unit_policy": "normalize_references"},
            "physics": {"gravity": True},
            "entities": [],
            "support_colliders": [{"id": "floor", "prim": "/World/Architecture/Floor", "type": "box",
                "size": [*layout["floor_size_m"], .1], "translate": [0, 0, -.05],
                "visible": True, "physics": "static", "color": [.28, .31, .34]}],
            "camera": {"prim": "/World/Camera", "translate": camera["position_m"],
                "look_at": camera["look_at_m"], "focal_length": camera["focal_length_mm"]},
            "lights": [{"type": "dome", "prim": "/World/Lights/Environment", "intensity": 650},
                       {"type": "rect", "prim": "/World/Lights/Key", "intensity": 2000,
                        "translate": [1, -2, 4], "look_at": [0, 0, .8], "width": 3, "height": 2}],
        }
        for entity in layout["entities"]:
            asset = catalog.resolve(entity["asset_id"])
            item = {"id": entity["id"], "prim": "/World/Objects/" + entity["id"],
                    "role": "object", "logical_id": entity["asset_id"],
                    "path": asset["path"], "translate": entity["position_m"],
                    "rotate": [0, 0, entity["yaw_deg"]], "physics": "static",
                    "support_target": entity["support_id"]}
            if asset["front_axis"] in {"+X", "-X", "+Y", "-Y"}:
                item["front_axis"] = asset["front_axis"]
            item["asset_provenance"] = asset["provenance"]
            item["layout_constraints"] = {"preferred_xy": entity["position_m"][:2],
                                          "yaw_deltas_deg": entity.get("yaw_deltas_deg", [0])}
            item["allowed_yaws_deg"] = [(entity["yaw_deg"] + delta + 180) % 360 - 180
                                       for delta in entity.get("yaw_deltas_deg", [0])]
            if "scale" in asset:
                item.update(scale=asset["scale"], unit_mode="multiply")
            spec["entities"].append(item)
        write(directory / "layout.json", layout)
        write(directory / "input-spec.json", spec)
        made = scene_tools.make_scene(core_config, spec, output=str(scene))
        write(directory / "composition.private.json", made)
        if made.get("status") not in {"pass", "success", "ok"}:
            raise RuntimeError("composition failed")
        write(directory / "summary.json", {"operation": op, "object_count": len(layout["entities"]),
            "units": "m", "up_axis": "Z", "delivery_scope": "review_scene", "physics": "not_run"})
        write(directory / "result.json", {})
        write(directory / "pending.json", [])
        return
    source = Path(request["source_dir"])
    scene = source / "scene.usda"
    if op == "scene_solve":
        spec = json.loads((source / "input-spec.json").read_text())
        layout = json.loads((source / "layout.json").read_text())
        # Recheck live catalog approval at execution, including frozen objects.
        for entity in spec["entities"]:
            asset = catalog.resolve(entity["logical_id"])
            if not asset["placement_supported"] or asset["path"] != entity["path"]:
                raise ValueError("asset approval or entrypoint changed")
            # Keep the original allowed orientations across repeated solves;
            # relative quarter turns must not accumulate into unapproved yaw.
            entity["layout_constraints"]["yaw_deltas_deg"] = [
                round((angle - entity["rotate"][2] + 180) % 360 - 180, 8)
                for angle in entity["allowed_yaws_deg"]]
        selected = set(parameters.get("entity_ids", [e["id"] for e in spec["entities"]]))
        # Moving an anchor invalidates its entire support subtree.
        while True:
            expanded = selected | {e["id"] for e in spec["entities"] if e["support_target"] in selected}
            if expanded == selected:
                break
            selected = expanded
        solved = scene_tools.solve_scene_placement_batch(core_config, spec,
            source_spec_path=str(source / "input-spec.json"), scene_path=str(scene),
            output_spec_path=str(directory / "input-spec.json"), output_scene_path=str(directory / "scene.usda"),
            entity_ids=sorted(selected), quarantine_failed=True, minimum_mesh_coverage=.95,
            support_margin=.01, obstacle_clearance=.03, clearance_z=0., max_candidates=4096)
        write(directory / "placement.private.json", solved)
        updated = json.loads((directory / "input-spec.json").read_text())
        entities = {e["id"]: e for e in updated["entities"]}
        revised = copy.deepcopy(layout)
        revised["entities"] = []
        for entity in layout["entities"]:
            if entity["id"] in entities:
                item = entities[entity["id"]]
                revised["entities"].append({**entity, "position_m": item["translate"],
                    "yaw_deg": (item["rotate"][2] + 180) % 360 - 180})
                if "yaw_deltas_deg" in entity:
                    deltas = [round((a - item["rotate"][2]) % 360, 8) for a in item["allowed_yaws_deg"]]
                    revised["entities"][-1]["yaw_deltas_deg"] = [d if d <= 180 else d - 360 for d in deltas]
        pending = sorted({e["id"] for e in updated.get("layout_pending_entities", [])})
        placement = {"status": solved["status"], "order": solved["graph"]["order"],
            "placed_ids": [r["entity_id"] for r in solved["placements"] if r["status"] == "pass"],
            "pending_ids": pending, "frozen_ids": [e["id"] for e in layout["entities"] if e["id"] not in selected],
            "contacts": []}
        prim_ids = {e["prim"]: e["id"] for e in spec["entities"]}
        failures = {e.get("entity_id"): e for e in solved.get("errors", [])}
        for row in solved["placements"]:
            kinds = {d.get("kind") for d in failures.get(row["entity_id"], {}).get("details", [])}
            reason = ("placed" if row["status"] == "pass" else
                      "no_valid_support_placement" if "no_valid_support_placement" in kinds else
                      "support_unavailable" if "missing_placement_prim" in kinds else "placement_failed")
            placement["contacts"].append({"entity_id": row["entity_id"], "status": row["status"],
                "obstacle_ids": [prim_ids[p] for p in row["automatic_obstacles"] if p in prim_ids],
                "mesh_coverage": (row.get("solution") or {}).get("mesh_support_coverage"), "reason": reason})
        # Existing pending gaps also prevent a falsely green re-solve.
        if pending:
            placement["status"] = "fail"
        write(directory / "layout.json", revised)
        write(directory / "pending.json", pending)
        write(directory / "summary.json", {"placement": placement, "physics": "not_run", "reachability": "not_run"})
        write(directory / "result.json", {"placement": placement})
        return
    if op == "scene_validate":
        dependencies = scene_tools.validate_scene(str(scene))
        layout = json.loads((source / "layout.json").read_text())
        pairs = [("/World/Objects/" + entity["id"], "/World/Architecture/Floor" if entity["support_id"] == "floor"
                  else "/World/Objects/" + entity["support_id"]) for entity in layout["entities"]]
        support = scene_tools.inspect_support_contacts(str(scene), prim_pairs=pairs, mesh_aware=True)
        if json.loads((source / "pending.json").read_text()):
            support["status"] = "fail"
        write(directory / "validation.private.json", {"dependencies": dependencies, "support": support})
        checks = {"dependencies": dependencies["status"], "support": support["status"],
                  "physics": "not_run", "reachability": "not_run"}
        write(directory / "summary.json", checks)
        write(directory / "result.json", {"scene_id": parameters["scene_id"], "checks": checks})
        return
    if op != "scene_render":
        raise ValueError("unsupported worker operation")
    # The isolated renderer inherits the owner's and GPU's leases. If the provider
    # exits unexpectedly, the leases remain held until its workers also exit.
    leases = tuple(int(value) for value in os.environ.get("ISAAC_SCENE_LEASE_FDS", "").split(",") if value)
    with tempfile.TemporaryDirectory(prefix="isaac-scene-render-") as kit_root:
        command = [sys.executable, str(scripts / "render_scene_camera.py"), "--scene", str(scene),
                   "--camera", "/World/Camera", "--output", str(directory / "image.png"),
                   "--render-profile", parameters["profile"], "--single-gpu", "--kit-portable-root", kit_root,
                   "--render-report", str(directory / "render.private.json")]
        subprocess.run(command, check=True, pass_fds=leases)
    report = json.loads((directory / "render.private.json").read_text())
    if report.get("status") == "fail":
        raise RuntimeError("render failed")
    write(directory / "summary.json", {"render_profile": parameters["profile"], "source_scene_id": parameters["scene_id"],
        "scene_sha256": hashlib.sha256(scene.read_bytes()).hexdigest(), "physics": "not_run"})
    write(directory / "result.json", {"scene_id": parameters["scene_id"]})


if __name__ == "__main__":
    main()
