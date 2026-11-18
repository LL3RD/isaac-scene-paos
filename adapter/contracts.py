"""Small public surface. Paths, processes and deployment settings are never tool inputs."""
from jsonschema import Draft202012Validator

ENDPOINT = "isaac.scene"
IMPLEMENTATION = "isaac.scene.v0_4"
PROTOCOL = "forge.tool.endpoint/v1alpha1"
ID = {"type": "string", "pattern": "^[A-Za-z][A-Za-z0-9_]{0,63}$"}
ASSET_ID = {"type": "string", "pattern": "^[A-Za-z][A-Za-z0-9_.:-]{0,159}$"}
REF = {"type": "string", "pattern": "^[a-f0-9]{32}$"}
NUMBER = {"type": "number", "minimum": -100, "maximum": 100}
VECTOR = {"type": "array", "items": NUMBER, "minItems": 3, "maxItems": 3}


def obj(properties, required=None):
    return {"type": "object", "properties": properties,
            "required": list(properties) if required is None else required,
            "additionalProperties": False}


ENTITY = obj({"id": ID, "asset_id": ASSET_ID, "position_m": VECTOR,
              "yaw_deg": {"type": "number", "minimum": -180, "maximum": 180},
              "support_id": ID,
              "yaw_deltas_deg": {"type": "array", "items": {"enum": [0, 90, -90, 180]},
                                 "minItems": 1, "maxItems": 4, "uniqueItems": True}},
             ["id", "asset_id", "position_m", "yaw_deg", "support_id"])
CAMERA = obj({"position_m": VECTOR, "look_at_m": VECTOR,
              "focal_length_mm": {"type": "number", "minimum": 12, "maximum": 100}})
LAYOUT = obj({
    "entities": {"type": "array", "items": ENTITY, "minItems": 1, "maxItems": 32},
    "floor_size_m": {"type": "array", "items": {"type": "number", "minimum": 2, "maximum": 30},
                     "minItems": 2, "maxItems": 2},
    "camera": CAMERA,
})
SCENE = obj({"scene_id": REF})
SIZE = {"type": "array", "items": {"type": "number", "minimum": 0, "maximum": 100}, "minItems": 3, "maxItems": 3}
TEXT = {"type": "string", "maxLength": 160}
SOLVE = obj({"status": {"enum": ["pass", "fail"]},
    "order": {"type": "array", "items": ID},
    "placed_ids": {"type": "array", "items": ID},
    "pending_ids": {"type": "array", "items": ID},
    "frozen_ids": {"type": "array", "items": ID},
    "contacts": {"type": "array", "items": obj({"entity_id": ID,
        "status": {"enum": ["pass", "fail"]}, "obstacle_ids": {"type": "array", "items": ID},
        "mesh_coverage": {"anyOf": [{"type": "number", "minimum": 0, "maximum": 1}, {"type": "null"}]},
        "reason": {"enum": ["placed", "no_valid_support_placement", "support_unavailable", "placement_failed"]}})}})
INSPECT_LAYOUT = {**LAYOUT, "properties": {**LAYOUT["properties"],
    "entities": {**LAYOUT["properties"]["entities"], "minItems": 0}}}
SCHEMAS = {
    "asset_search": obj({"query": {"type": "string", "minLength": 1, "maxLength": 120},
                         "source": {"enum": ["all", "registry", "synthesis", "semantic"]},
                         "canonical_class": TEXT, "subtype": TEXT,
                         "min_size_m": SIZE, "max_size_m": SIZE,
                         "limit": {"type": "integer", "minimum": 1, "maximum": 20}}, ["query"]),
    "scene_inspect": SCENE,
    "scene_build": obj({"layout": LAYOUT}),
    "scene_edit": obj({"scene_id": REF, "layout": LAYOUT}),
    "scene_solve": obj({"scene_id": REF, "entity_ids": {"type": "array", "items": ID,
                        "minItems": 1, "maxItems": 32, "uniqueItems": True}}, ["scene_id"]),
    "scene_validate": SCENE,
    "scene_render": obj({"scene_id": REF, "profile": {"enum": ["preview", "review"]}}),
    "artifact_read": obj({"artifact_id": REF,
                          "offset": {"type": "integer", "minimum": 0, "maximum": 100000000},
                          "length": {"type": "integer", "minimum": 1, "maximum": 65536}}, ["artifact_id"]),
}
QUERIES = {"asset_search", "scene_inspect", "artifact_read"}
DESCRIPTIONS = {
    "asset_search": "Read-only retrieval from operator-approved catalogs and registry; filter by class, subtype and size. Returns logical IDs, provenance and support mode, never paths. Missing candidates are gaps.",
    "scene_inspect": "Read an immutable scene revision. Units: metres, Z up, yaw degrees. Does not prove physics or reachability.",
    "scene_build": "Compose a new review scene using registered USD assets. Agent supplies a complete layout in metres, Z up.",
    "scene_edit": "Create a new revision from a complete replacement layout; preserve the previous revision. No in-place edits or accumulated deltas.",
    "scene_solve": "Create a new fully solved revision using the core support dependency graph and visible mesh contacts. Input positions are coarse XY preferences; solve height and avoid placed objects. Selected anchors expand to their support descendants; other objects freeze. Failures are quarantined, never silently placed at origin. Inspect status and pending_ids.",
    "scene_validate": "Check USD dependencies and declared supports. Geometric findings are separate from executor success. No PhysX/IK certification.",
    "scene_render": "Render a real still with the existing Isaac renderer. Serialized GPU use. Returns artifact IDs, not server paths.",
    "artifact_read": "Read a bounded base64 chunk of a generated artifact by ID. USD/spec files remain internal because they contain dependency paths.",
}


def output_schema(operation):
    artifact = obj({"artifact_id": REF, "role": {"enum": ["image", "summary"]},
                    "size_bytes": {"type": "integer", "minimum": 0},
                    "sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"}})
    if operation == "artifact_read":
        return obj({"artifact_id": REF, "offset": {"type": "integer"}, "total_bytes": {"type": "integer"},
                    "eof": {"type": "boolean"}, "base64": {"type": "string", "maxLength": 87384}})
    if operation == "asset_search":
        asset = obj({"asset_id": ASSET_ID, "label": TEXT, "tags": {"type": "array", "items": TEXT},
                     "metadata": obj({"size_m": SIZE, "bottom_z_m": NUMBER, "support_height_m": NUMBER}, []),
                     "canonical_class": TEXT, "subtype": TEXT, "source": TEXT,
                     "front_axis": {"enum": ["+X", "-X", "+Y", "-Y", "unknown", "not_applicable"]},
                     "support_mode": {"enum": ["floor", "surface", "ceiling", "wall", "free", "unknown"]},
                     "placement_supported": {"type": "boolean"},
                     "provenance": obj({"source_asset_id": TEXT, "license_status": TEXT,
                                        "allowed_use_scope": TEXT, "attribution": {"type": "string", "maxLength": 2000}})})
        return obj({"assets": {"type": "array", "items": asset, "maxItems": 20},
                    "registry_scope": {"const": "operator_approved_catalogs"}})
    if operation == "scene_inspect":
        return obj({"scene_id": REF, "parent_scene_id": {"anyOf": [REF, {"type": "null"}]}, "layout": INSPECT_LAYOUT,
                    "pending_ids": {"type": "array", "items": ID},
                    "delivery_scope": {"const": "review_scene"}})
    properties = {"scene_id": REF, "artifacts": {"type": "array", "items": artifact},
                  "delivery_scope": {"const": "review_scene"}}
    if operation == "scene_validate":
        properties["checks"] = obj({"dependencies": {"enum": ["pass", "warn", "fail", "pending"]},
                                    "support": {"enum": ["pass", "warn", "fail", "pending"]},
                                    "physics": {"const": "not_run"}, "reachability": {"const": "not_run"}})
    if operation == "scene_solve":
        properties["placement"] = SOLVE
    return obj(properties)


def validate(operation, arguments):
    import json
    json.dumps(arguments, allow_nan=False)
    Draft202012Validator(SCHEMAS[operation]).validate(arguments)


def tool_specs():
    return [{"tool_id": "isaac." + op.replace("_", ".", 1), "implementation_id": IMPLEMENTATION,
             "endpoint_id": ENDPOINT, "operation": op, "semantics": "query" if op in QUERIES else "action",
             "description": DESCRIPTIONS[op], "input_schema": schema, "output_schema": output_schema(op),
             "readiness": []} for op, schema in SCHEMAS.items()]
