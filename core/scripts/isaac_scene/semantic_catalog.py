from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


SPECIALIZED_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("lighting/fixture", ("table lamp", "desk lamp", "floor lamp", "wall lamp", "ceiling light")),
    (
        "electronics/display/wall_mounted",
        (
            "wall mounted display",
            "wall mounted television",
            "wall mounted monitor",
            "wall mounted screen",
            "wall mounted tv",
            "wall display",
            "wall screen",
        ),
    ),
    (
        "electronics/display/mobile_stand",
        (
            "mobile floor standing television",
            "mobile floor standing display",
            "floor standing television",
            "floor standing display",
            "mobile tv stand",
            "wheeled television",
            "wheeled tv",
        ),
    ),
    ("medical/lighting", ("surgical shadowless lamp", "shadowless lamp", "operating light")),
    ("medical/bed", ("operating table", "surgical table", "examination bed", "hospital bed", "patient bed", "delivery bed")),
    ("medical/storage", ("medical cabinet", "instrument cart", "medical cart")),
    ("medical/equipment", ("anesthesia machine", "incubator", "patient monitor", "medical monitor", "medical equipment", "hospital equipment", "clinical equipment", "neonatal equipment", "radiant warmer", "lavage")),
    ("laboratory/equipment", ("laboratory", "microscope", "centrifuge", "spectrometer", "pipette", "test tube", "fume hood", "fume extractor")),
    # Kitchen appliances are split into retrieval leaves before the broad
    # appliance class. Source remains provenance only; search works by object
    # function and form factor.
    ("appliance/kitchen/freezer", ("display freezer", "chest freezer", "ice cream freezer", "freezer")),
    ("appliance/kitchen/refrigerator", ("refrigerator", "fridge")),
    ("appliance/kitchen/microwave", ("microwave",)),
    ("appliance/kitchen/electric_cooker", ("electric cooker", "electriccooker", "rice cooker")),
    ("appliance/kitchen/coffee_maker", ("coffee maker", "coffeemaker", "coffee machine")),
    ("appliance/kitchen/oven", ("countertop oven", "stove", "oven")),
    ("appliance/kitchen", ("air fryer", "dishwasher", "popcorn machine")),
    ("appliance/laundry", ("washing machine", "washingmachine", "clothes dryer", "tumble dryer")),
    ("industrial/robot", ("robotic arm", "roboticarm", "manipulator", "robot arm")),
    ("industrial/conveyor", ("conveyor belt", "conveyorbelt", "conveyor", "belt conveyor")),
    ("industrial/machine", ("cnc machine", "industrial machine", "factory machine", "reflow", "assemblyline", "assembly line", "workshop equipment", "aoi zenith")),
    ("logistics/vehicle", ("agv", "forklift", "pallet truck", "powered gondola")),
    ("transport/vehicle", ("vehicle", "car", "truck", "bus", "train")),
    ("retail/fixture", ("checkout", "cashier", "gondola shelf", "retail shelf", "store shelf")),
    ("transport/station_equipment", ("ticket vending machine", "tvm", "turnstile")),
    ("safety/equipment/fire_extinguisher", ("fire extinguisher", "fireextinguisher")),
    ("safety/equipment/floor_marking", ("safety tape", "floor marking", "floor lane")),
    ("safety/equipment/guardrail", ("safety railing", "guardrail", "corner rail", "metal fencing", "low barrier")),
]

GENERAL_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("architecture/opening", ("door", "window", "gate")),
    ("architecture/partition", ("glass partition", "room divider", "partition")),
    ("architecture/surface/wall", ("wall",)),
    ("architecture/surface/floor", ("ground", "floor")),
    ("architecture/surface/ceiling", ("ceiling",)),
    ("architecture/structure/column", ("column", "pillar")),
    ("furniture/seating/office_chair", ("office chair", "chair office", "task chair", "swivel chair")),
    ("furniture/seating/conference_chair", ("conference chair", "meeting chair", "cantilever chair")),
    ("furniture/seating/lounge_chair", ("armchair", "lounge chair", "sofa chair")),
    ("furniture/seating/sofa", ("sofa", "couch")),
    ("furniture/seating/chair", ("chair",)),
    ("furniture/seating/stool", ("stool",)),
    ("furniture/seating/bench", ("bench",)),
    ("furniture/surface/workbench", ("workbench", "workbenches")),
    ("furniture/surface/meeting_table", ("meeting desk", "meeting table", "conference table", "boardroom table")),
    ("furniture/surface/nightstand", ("nightstand", "bedside table")),
    ("furniture/surface/coffee_table", ("coffee table", "tea table", "teatable")),
    ("furniture/surface/desk", ("desk",)),
    ("furniture/surface/table", ("table",)),
    ("furniture/surface/counter", ("counter",)),
    ("furniture/storage/rack/bike", ("bike rack", "bicycle rack")),
    ("furniture/storage/rack/roof", ("roof rack",)),
    ("furniture/storage/rack/display", ("chart rack", "display rack")),
    ("furniture/storage/rack/industrial", ("part rack", "hood rack")),
    ("furniture/storage/rack/warehouse", ("warehouse rack", "pallet rack", "storage rack", "rack large", "rack long", "rack l", "rack m", "sm rack")),
    ("furniture/storage/rack", ("racking", "rack")),
    ("furniture/storage/shelf", ("shelf", "bookcase", "bookshelf")),
    ("furniture/storage/low_cabinet", ("low cabinet", "sideboard", "credenza")),
    ("furniture/storage/tall_cabinet", ("tall cabinet", "wardrobe", "locker", "vertical filing cabinet")),
    ("furniture/storage/cabinet", ("cabinet", "drawer", "cupboard")),
    ("furniture/storage/tv_stand", ("tv stand", "tvstand")),
    ("furniture/bed", ("bed", "mattress")),
    ("appliance/climate", ("fan", "heater", "air conditioner", "airconditioner")),
    ("electronics/display", ("television", "tv", "monitor", "display", "screen")),
    ("electronics/computer", ("computer", "laptop", "keyboard", "printer", "mouse")),
    ("electronics/communication", ("telephone", "phone", "intercom")),
    ("electronics/retail", ("barcode scanner", "card reader")),
    ("lighting/fixture", ("lamp", "light", "chandelier")),
    ("decor/plant", ("plant", "flower", "tree")),
    ("decor/textile", ("pillow", "cushion", "blanket", "curtain", "towel", "carpet", "rug")),
    ("decor/wall", ("picture", "painting", "poster", "mirror", "clock")),
    ("decor/ornament", ("decoration", "ornament", "vase", "statue", "sculpture")),
    ("toy/general", ("toy", "rubiks cube", "gamepad")),
    ("human/person", ("person", "worker", "man", "woman")),
    ("plumbing/fixture", ("faucet", "sink", "toilet", "bathtub", "shower")),
    ("household/waste", ("trashcan", "trash can", "waste bin", "garbage bin")),
    ("household/container", ("basket", "laundry basket")),
    ("household/cleaning", ("detergent", "air freshener")),
    ("personal/clothing", ("clothes", "clothing", "shoe", "backpack")),
    ("kitchenware/cookware", ("bowl", "pot", "pan", "tray", "glassware", "mug", "pitcher")),
    ("food/grocery", ("snack", "beverage", "flour", "sauce", "sauces", "cereal", "cheese", "fruit", "orange", "pomegranate")),
    ("stationery/writing", ("pen", "pencil", "marker")),
    ("electrical/fixture", ("socket", "plug", "outlet")),
    ("fireplace/fixture", ("hearth", "fireplace")),
    ("safety/equipment", ("fire extinguisher", "security check", "fence", "barrier")),
    ("logistics/container", ("container", "carton", "crate", "box", "pallet")),
    ("retail/cart", ("shopping trolley", "shoppingtrolley", "shopping cart")),
    ("tool/hand_tool", ("screwdriver", "wrench", "hammer", "pliers", "scissors")),
    ("small_prop", ("plate", "folder", "stapler", "book", "cup", "bottle")),
    # A source path often includes the parent room (warehouse/store/hospital).
    # Keep the room-shell catch-all last so an asset named rack, chair or
    # cabinet retains its object semantics.
    ("architecture/room_shell", ("room", "factory", "workshop", "warehouse", "station", "kindergarten", "store")),
]


def normalize_semantic_name(value: str) -> str:
    text = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", value)
    text = text.replace("-", " ").replace("_", " ")
    text = re.sub(r"(?<=[a-zA-Z])\d+(?:\s+\d+)?$", "", text)
    text = re.sub(r"\b[0-9a-f]{12,}\b", "", text, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", text).strip().lower()


def derive_semantic_name(value: str, source_path: str | None = None) -> str:
    """Recover category names hidden behind generic USD filenames such as Aligned.usd."""
    normalized = normalize_semantic_name(value)
    if not source_path or normalized not in {"aligned", "instance", "object", "other"}:
        return normalized
    path = Path(source_path)
    candidate = ""
    if normalized == "aligned":
        candidate = path.parent.name
    elif len(path.parents) > 1:
        candidate = path.parents[1].name
    candidate = re.sub(r"^(?:benchmark|simready)[_-]+", "", candidate, flags=re.IGNORECASE)
    candidate = re.sub(r"[_-]+\d+$", "", candidate)
    derived = normalize_semantic_name(candidate)
    return derived if derived not in {"", "aligned", "instance", "object", "other"} else normalized


def classify_semantics(name: str, category: str = "", asset_type: str = "") -> tuple[str, str]:
    normalized = normalize_semantic_name(name)
    normalized_category = normalize_semantic_name(category)
    normalized_type = normalize_semantic_name(asset_type)
    # Whole scenes and executable/sample stages must not inherit object labels
    # from parent path tokens. Layout and articulated_layout are deliberately
    # excluded: GRScenes uses those kinds for individual floors, walls, doors
    # and windows, not for complete environments.
    if normalized_type in {"scene", "background"}:
        return "environment/scene", normalized or "scene"
    if "behavior tree" in normalized_category:
        return "environment/sample_scene", normalized or "sample scene"
    # Some upstream indices type every file under an environment package as
    # "environment". Treat object containers as objects, while keeping actual
    # environment roots as scenes.
    object_container = any(
        token in f"/{normalized_category.replace(' ', '/')}/"
        for token in ("/props/", "/objects/", "/object/", "/furniture/", "/characters/", "/equipment/")
    )
    if normalized_type == "environment" and not object_container:
        return "environment/scene", normalized or "scene"
    # Character names often contain a profession ("medical", "worker"). The
    # object is still a person; profession belongs in tags, not the asset type.
    if (
        any(token in normalized_category for token in ("people characters", "characters", "people"))
        or normalized_type in {"person", "character", "human"}
        or re.search(r"(?<![a-z0-9])(male|female|adult|person|worker|man|woman)(?![a-z0-9])", normalized)
    ):
        return "human/person", normalized or "person"
    def match_rules(text: str, rules: Iterable[tuple[str, tuple[str, ...]]]) -> tuple[str, str] | None:
        matches: list[tuple[int, int, str]] = []
        for canonical_class, patterns in rules:
            for pattern in patterns:
                if re.search(
                    rf"(?<![a-z0-9]){re.escape(pattern)}(?![a-z0-9])",
                    text,
                ):
                    matches.append((
                        len(pattern.split()),
                        len(pattern),
                        canonical_class,
                    ))
        if matches:
            _token_count, _length, canonical_class = max(matches)
            return canonical_class, normalized or canonical_class.rsplit("/", 1)[-1]
        return None

    # The asset's own name is authoritative. Parent paths often describe an
    # assembly ("rack") while the referenced child is actually a pallet,
    # crate, monitor or other prop.
    name_match = match_rules(normalized, SPECIALIZED_RULES)
    if name_match:
        return name_match
    if (
        re.search(r"(?<![a-z0-9])machine(?![a-z0-9])", normalized)
        and any(token in normalized_category for token in ("industrial", "factory", "workshop", "manufacturing"))
    ):
        return "industrial/machine", normalized or "machine"
    name_match = match_rules(normalized, GENERAL_RULES)
    if name_match:
        return name_match
    # Category/type are fallback evidence for opaque names such as "other" or
    # "instance", never stronger evidence than a recognized asset name.
    fallback_match = match_rules(
        f"{normalized_category} {normalized_type}",
        [*SPECIALIZED_RULES, *GENERAL_RULES],
    )
    if fallback_match:
        return fallback_match
    if asset_type in {"robot"}:
        return "robot/general", normalized or "robot"
    if asset_type in {"sensor"}:
        return "sensor/general", normalized or "sensor"
    return "object/other", normalized or "object"


def infer_retrieval_class(query: str) -> str | None:
    """Infer a useful semantic prefix without over-constraining generic nouns."""
    canonical_class, _ = classify_semantics(query)
    if canonical_class in {"object/other", "environment/scene", "small_prop"}:
        return None
    generic_leaf_classes = {
        "furniture/seating/chair": "furniture/seating",
        "furniture/surface/table": "furniture/surface",
        "furniture/storage/cabinet": "furniture/storage",
    }
    return generic_leaf_classes.get(canonical_class, canonical_class)


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        PRAGMA journal_mode=DELETE;
        CREATE TABLE IF NOT EXISTS semantic_assets (
            logical_id TEXT PRIMARY KEY,
            canonical_class TEXT NOT NULL,
            subtype TEXT NOT NULL,
            semantic_name TEXT NOT NULL,
            aliases_json TEXT NOT NULL,
            domain_tags_json TEXT NOT NULL,
            style_tags_json TEXT NOT NULL,
            material_tags_json TEXT NOT NULL,
            color_tags_json TEXT NOT NULL,
            source_library TEXT NOT NULL,
            source_asset_id TEXT,
            source_path TEXT,
            remote_model_id TEXT,
            availability TEXT NOT NULL,
            callable INTEGER NOT NULL,
            preview_index_path TEXT,
            front_axis TEXT,
            meters_per_unit REAL,
            bbox_json TEXT,
            license_status TEXT NOT NULL,
            usage_count INTEGER NOT NULL,
            scene_slugs_json TEXT NOT NULL,
            metadata_json TEXT NOT NULL,
            search_text TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS semantic_assets_class_idx
            ON semantic_assets(canonical_class, subtype, callable);
        CREATE INDEX IF NOT EXISTS semantic_assets_source_idx
            ON semantic_assets(source_library, callable);
        CREATE UNIQUE INDEX IF NOT EXISTS semantic_assets_source_path_idx
            ON semantic_assets(source_path) WHERE source_path IS NOT NULL;
        CREATE TABLE IF NOT EXISTS semantic_overrides (
            logical_id TEXT PRIMARY KEY,
            patch_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS semantic_assets_fts USING fts5(
            semantic_name, canonical_class, subtype, search_text,
            content='semantic_assets', content_rowid='rowid',
            tokenize='unicode61 remove_diacritics 2'
        );
        """
    )
    # Migrate existing enrichment into the durable override layer once. This
    # protects previews, measurements and verified orientation from a rebuild.
    enriched_rows = connection.execute(
        """SELECT logical_id, preview_index_path, front_axis, meters_per_unit,
                  bbox_json, style_tags_json, material_tags_json, color_tags_json,
                  metadata_json, search_text
           FROM semantic_assets
           WHERE preview_index_path IS NOT NULL
              OR front_axis IS NOT NULL
              OR meters_per_unit IS NOT NULL
              OR bbox_json IS NOT NULL
              OR style_tags_json <> '[]'
              OR material_tags_json <> '[]'
              OR color_tags_json <> '[]'"""
    ).fetchall()
    for row in enriched_rows:
        patch = {
            "preview_index_path": row[1],
            "front_axis": row[2],
            "meters_per_unit": row[3],
            "bbox_json": row[4],
            "style_tags_json": row[5],
            "material_tags_json": row[6],
            "color_tags_json": row[7],
            "metadata_json": row[8],
            "search_text": row[9],
        }
        connection.execute(
            """INSERT OR IGNORE INTO semantic_overrides(logical_id, patch_json, updated_at)
               VALUES(?, ?, ?)""",
            (
                row[0],
                json.dumps(patch, ensure_ascii=False, sort_keys=True),
                datetime.now(timezone.utc).isoformat(),
            ),
        )


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _apply_override(connection: sqlite3.Connection, logical_id: str) -> None:
    row = connection.execute(
        "SELECT patch_json FROM semantic_overrides WHERE logical_id=?",
        (logical_id,),
    ).fetchone()
    if row is None:
        return
    patch = json.loads(row[0])
    allowed = {
        "canonical_class", "subtype", "semantic_name",
        "availability", "callable",
        "preview_index_path", "front_axis", "meters_per_unit", "bbox_json",
        "style_tags_json", "material_tags_json", "color_tags_json",
        "metadata_json", "search_text",
    }
    updates = {key: value for key, value in patch.items() if key in allowed}
    if not updates:
        return
    assignments = ", ".join(f"{key}=?" for key in updates)
    connection.execute(
        f"UPDATE semantic_assets SET {assignments} WHERE logical_id=?",
        [*updates.values(), logical_id],
    )


def upsert(connection: sqlite3.Connection, row: dict[str, Any]) -> None:
    columns = [
        "logical_id", "canonical_class", "subtype", "semantic_name", "aliases_json",
        "domain_tags_json", "style_tags_json", "material_tags_json", "color_tags_json",
        "source_library", "source_asset_id", "source_path", "remote_model_id",
        "availability", "callable", "preview_index_path", "front_axis",
        "meters_per_unit", "bbox_json", "license_status", "usage_count",
        "scene_slugs_json", "metadata_json", "search_text",
    ]
    values = [row.get(column) for column in columns]
    placeholders = ",".join("?" for _ in columns)
    updates = ",".join(f"{column}=excluded.{column}" for column in columns[1:])
    connection.execute(
        f"INSERT INTO semantic_assets ({','.join(columns)}) VALUES ({placeholders}) "
        f"ON CONFLICT(logical_id) DO UPDATE SET {updates}",
        values,
    )
    _apply_override(connection, str(row["logical_id"]))


def rebuild_fts(connection: sqlite3.Connection) -> None:
    connection.execute("INSERT INTO semantic_assets_fts(semantic_assets_fts) VALUES('rebuild')")
    connection.commit()


def synthesis_rows(manifest_path: Path, dataset_root: Path) -> Iterable[dict[str, Any]]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    models: dict[str, dict[str, Any]] = {}
    for scene in manifest.get("scenes", []):
        slug = str(scene["slug"])
        info_path = dataset_root / slug / "scene_info.json"
        data_path = dataset_root / slug / "scene_data.json"
        if not info_path.is_file():
            continue
        articulation: dict[str, bool] = {}
        business_types: dict[str, int] = {}
        if data_path.is_file():
            raw = json.loads(data_path.read_text(encoding="utf-8")).get("Result", {}).get("SceneDataJson")
            if raw:
                parsed = json.loads(raw)
                for instance in parsed.get("extDataArr", []):
                    model_id = str(instance.get("modelID") or "")
                    business_types[model_id] = int(instance.get("modelBusinessType") or 0)
                    articulation[model_id] = bool(instance.get("data", {}).get("isHasArticulus", False))
        info = json.loads(info_path.read_text(encoding="utf-8"))
        for instance in info.get("models", []):
            model_id = str(instance.get("modelID") or instance.get("vaultID") or "")
            if not model_id:
                continue
            state = models.setdefault(model_id, {
                "names": {},
                "scenes": set(),
                "instances": [],
                "server": instance.get("server"),
                "business_type": business_types.get(model_id, 0),
                "is_articulated": articulation.get(model_id, False),
            })
            name = str(instance.get("name") or model_id)
            state["names"][name] = state["names"].get(name, 0) + 1
            state["scenes"].add(slug)
            state["instances"].append({
                "scene": slug,
                "name": name,
                "offset": instance.get("offset"),
                "rotation": instance.get("rotation"),
                "scale": instance.get("scale"),
            })
    for model_id, state in models.items():
        name = sorted(state["names"].items(), key=lambda item: (-item[1], item[0]))[0][0]
        canonical_class, subtype = classify_semantics(name)
        aliases = sorted(state["names"])
        scenes = sorted(state["scenes"])
        search_text = " ".join([name, *aliases, canonical_class.replace("/", " "), subtype, *scenes, "synthesis extwin"])
        yield {
            "logical_id": f"synthesis27:{model_id}",
            "canonical_class": canonical_class,
            "subtype": subtype,
            "semantic_name": normalize_semantic_name(name),
            "aliases_json": _json(aliases),
            "domain_tags_json": _json(sorted({scene.split("_", 1)[0] for scene in scenes})),
            "style_tags_json": "[]",
            "material_tags_json": "[]",
            "color_tags_json": "[]",
            "source_library": "synthesis27",
            "source_asset_id": model_id,
            "source_path": None,
            "remote_model_id": model_id,
            "availability": "remote_metadata_only",
            "callable": 0,
            "preview_index_path": None,
            "front_axis": None,
            "meters_per_unit": None,
            "bbox_json": None,
            "license_status": "verify_before_download",
            "usage_count": len(state["instances"]),
            "scene_slugs_json": _json(scenes),
            "metadata_json": _json({
                "server": state["server"],
                "business_type": state["business_type"],
                "is_articulated": state["is_articulated"],
                "instances": state["instances"],
            }),
            "search_text": search_text,
        }


def pcnl_catalog_rows(path: Path) -> Iterable[dict[str, Any]]:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    rows = connection.execute(
        """SELECT asset_id, path, library, kind, label, category, readiness,
                  callable, metadata_path, interactive_metadata_path, search_text
           FROM assets WHERE callable=1"""
    )
    for source in rows:
        semantic_name = derive_semantic_name(source["label"], source["path"])
        canonical_class, subtype = classify_semantics(
            semantic_name,
            f"{source['category']} {source['search_text']}",
            source["kind"],
        )
        aliases = [] if semantic_name == normalize_semantic_name(source["label"]) else [source["label"]]
        yield {
            "logical_id": f"pcnl:{source['asset_id']}",
            "canonical_class": canonical_class,
            "subtype": subtype,
            "semantic_name": semantic_name,
            "aliases_json": _json(aliases),
            "domain_tags_json": "[]",
            "style_tags_json": "[]",
            "material_tags_json": "[]",
            "color_tags_json": "[]",
            "source_library": source["library"],
            "source_asset_id": source["asset_id"],
            "source_path": source["path"],
            "remote_model_id": None,
            "availability": source["readiness"],
            "callable": int(source["callable"]),
            "preview_index_path": None,
            "front_axis": None,
            "meters_per_unit": None,
            "bbox_json": None,
            "license_status": "existing_local_asset",
            "usage_count": 0,
            "scene_slugs_json": "[]",
            "metadata_json": _json({
                "metadata_path": source["metadata_path"],
                "interactive_metadata_path": source["interactive_metadata_path"],
            }),
            "search_text": f"{semantic_name} {source['search_text']} {canonical_class.replace('/', ' ')} {subtype}",
        }
    connection.close()


def official_index_rows(path: Path) -> Iterable[dict[str, Any]]:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    rows = connection.execute(
        """SELECT asset_id, path, library, asset_type, semantic_name, category,
                  callable, entry_rank, support_reason, search_text
           FROM asset_entries WHERE callable=1"""
    )
    for source in rows:
        semantic_name = derive_semantic_name(source["semantic_name"], source["path"])
        canonical_class, subtype = classify_semantics(
            semantic_name,
            f"{source['category']} {source['search_text']}",
            source["asset_type"],
        )
        aliases = [] if semantic_name == normalize_semantic_name(source["semantic_name"]) else [source["semantic_name"]]
        yield {
            "logical_id": f"indexed:{source['asset_id']}",
            "canonical_class": canonical_class,
            "subtype": subtype,
            "semantic_name": semantic_name,
            "aliases_json": _json(aliases),
            "domain_tags_json": "[]",
            "style_tags_json": "[]",
            "material_tags_json": "[]",
            "color_tags_json": "[]",
            "source_library": source["library"],
            "source_asset_id": source["asset_id"],
            "source_path": source["path"],
            "remote_model_id": None,
            "availability": "local",
            "callable": int(source["callable"]),
            "preview_index_path": None,
            "front_axis": None,
            "meters_per_unit": None,
            "bbox_json": None,
            "license_status": "existing_local_asset",
            "usage_count": 0,
            "scene_slugs_json": "[]",
            "metadata_json": _json({
                "entry_rank": source["entry_rank"],
                "support_reason": source["support_reason"],
            }),
            "search_text": f"{semantic_name} {source['search_text']} {canonical_class.replace('/', ' ')} {subtype}",
        }
    connection.close()


def _synthesis_object_adapter_paths(
    adapters: Iterable[dict[str, Any] | str],
) -> list[tuple[Path, str]]:
    """Resolve enabled external catalogs without opening them for writes."""
    resolved: list[tuple[Path, str]] = []
    for adapter in adapters:
        if isinstance(adapter, str):
            path = Path(adapter)
            allowed_scope = "internal_noncommercial_research"
        elif isinstance(adapter, dict):
            if adapter.get("enabled", True) is False:
                continue
            if str(adapter.get("kind") or "") != "synthesis_object_library":
                continue
            path = Path(str(adapter.get("path") or ""))
            allowed_scope = str(
                adapter.get("allowed_use_scope")
                or "internal_noncommercial_research"
            )
        else:
            continue
        if path.is_file():
            resolved.append((path, allowed_scope))
    return resolved


def _search_synthesis_object_adapter(
    path: Path,
    allowed_scope: str,
    query: str,
    *,
    effective_class: str | None,
    subtype: str | None,
    style_tags: Iterable[str],
    material_tags: Iterable[str],
    color_tags: Iterable[str],
    minimum: list[float] | None,
    maximum: list[float] | None,
) -> list[dict[str, Any]]:
    """Project callable exact-ID objects into the unified result schema.

    The adapter is intentionally strict and read-only. It never promotes an
    object and exposes only rows that remain ready, technically callable, and
    approved for the configured scope in the source catalog.
    """
    if any(tuple(values) for values in (style_tags, material_tags, color_tags)):
        return []
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    rows = connection.execute(
        """SELECT * FROM objects
           WHERE callable=1
             AND qa_status='ready'
             AND license_review_status='verified'
             AND allowed_use_scope=?
             AND wrapper_path IS NOT NULL
           ORDER BY usage_count DESC, model_id""",
        (allowed_scope,),
    ).fetchall()
    connection.close()
    query_text = normalize_semantic_name(query)
    query_tokens = set(re.findall(r"[a-z0-9]+", query_text))
    results: list[dict[str, Any]] = []
    for row in rows:
        canonical_class = str(row["canonical_class"])
        if effective_class and not canonical_class.startswith(
            effective_class.rstrip("/")
        ):
            continue
        row_subtype = str(row["subtype"])
        if subtype and normalize_semantic_name(subtype) not in normalize_semantic_name(row_subtype):
            continue
        aliases = json.loads(row["aliases_json"] or "[]")
        scenes = json.loads(row["scene_slugs_json"] or "[]")
        asset_scopes = json.loads(row["asset_scopes_json"] or "[]")
        if not set(asset_scopes).intersection({"independent_object", "robot"}):
            continue
        searchable = normalize_semantic_name(" ".join([
            str(row["semantic_name"]),
            *[str(value) for value in aliases],
            canonical_class.replace("/", " "),
            row_subtype,
            *[str(value) for value in scenes],
        ]))
        matched = {token for token in query_tokens if token in searchable}
        if not matched:
            continue
        bbox = json.loads(row["bbox_m_json"]) if row["bbox_m_json"] else None
        size = bbox.get("size") if isinstance(bbox, dict) else None
        if (minimum is not None or maximum is not None) and not (
            isinstance(size, list) and len(size) == 3
        ):
            continue
        measured_size = [float(value) for value in size] if size else None
        if minimum is not None and any(
            value < bound for value, bound in zip(measured_size or [], minimum)
        ):
            continue
        if maximum is not None and any(
            value > bound for value, bound in zip(measured_size or [], maximum)
        ):
            continue
        review = json.loads(row["review_report_json"] or "{}")
        coverage = len(matched) / max(len(query_tokens), 1)
        exact_phrase = bool(query_text and query_text in searchable)
        exact_class = bool(effective_class and canonical_class == effective_class)
        quality_score = 1.0
        retrieval_score = min(
            1.0,
            0.62 * coverage
            + 0.26 * quality_score
            + 0.08 * int(exact_phrase)
            + 0.04 * int(exact_class),
        )
        logical_id = str(row["logical_id"] or f"synthesis28:{row['model_id']}")
        results.append({
            "logical_id": logical_id,
            "canonical_class": canonical_class,
            "subtype": row_subtype,
            "semantic_name": row["semantic_name"],
            "source_library": "synthesis28_objects",
            "source_asset_id": row["model_id"],
            "path": row["wrapper_path"],
            "remote_model_id": row["model_id"],
            "availability": "local_verified",
            "callable": True,
            "preview_index_path": row["preview_index_path"],
            "front_axis": row["front_axis"],
            "meters_per_unit": 1.0,
            "bbox": bbox,
            "style_tags": [],
            "material_tags": [],
            "color_tags": [],
            "license_status": "CC-BY-NC-4.0",
            "usage_count": int(row["usage_count"] or 0),
            "scene_slugs": scenes,
            "metadata": {
                "catalog_adapter": "synthesis_object_library",
                "catalog_path": str(path),
                "source_path": row["source_path"],
                "asset_scopes": asset_scopes,
                "front_axis_status": row["front_axis_status"],
                "support_mode": review.get("support_mode"),
                "orientation_mode": review.get("orientation_mode"),
                "mount_anchor_prim": review.get("mount_anchor_prim"),
                "license_policy_id": row["license_policy_id"],
                "allowed_use_scope": row["allowed_use_scope"],
                "attribution_text": row["attribution_text"],
            },
            "asset_quality_score": quality_score,
            "retrieval_score": round(retrieval_score, 6),
            "inferred_class": effective_class,
            "_semantic_rank": -coverage,
        })
    return results


def search(
    catalog_path: str | Path,
    query: str,
    *,
    topk: int = 20,
    canonical_class: str | None = None,
    subtype: str | None = None,
    style_tags: Iterable[str] = (),
    material_tags: Iterable[str] = (),
    color_tags: Iterable[str] = (),
    min_size_m: Iterable[float] | None = None,
    max_size_m: Iterable[float] | None = None,
    callable_only: bool = True,
    infer_class: bool = True,
    diversify: bool = True,
    read_only_adapters: Iterable[dict[str, Any] | str] = (),
) -> list[dict[str, Any]]:
    path = Path(catalog_path)
    if not path.is_file():
        return []
    tokens = re.findall(r"[a-z0-9]+", normalize_semantic_name(query))
    if not tokens:
        return []
    fts_query = " OR ".join(f'"{token}"*' for token in dict.fromkeys(tokens))
    filters = ["semantic_assets_fts MATCH ?"]
    params: list[Any] = [fts_query]
    if callable_only:
        filters.append("a.callable=1")
    effective_class = canonical_class or (infer_retrieval_class(query) if infer_class else None)
    if effective_class:
        filters.append("a.canonical_class LIKE ?")
        params.append(effective_class.rstrip("/") + "%")
    if subtype:
        filters.append("a.subtype LIKE ?")
        params.append("%" + normalize_semantic_name(subtype) + "%")
    tag_filters = (
        ("a.style_tags_json", style_tags),
        ("a.material_tags_json", material_tags),
        ("a.color_tags_json", color_tags),
    )
    for column, values in tag_filters:
        for value in values:
            normalized = normalize_semantic_name(str(value))
            if normalized:
                filters.append(f"{column} LIKE ?")
                params.append(f'%"{normalized}"%')
    needs_size_filter = min_size_m is not None or max_size_m is not None
    # Retrieve a deeper semantic pool so quality metadata and diversity can
    # rerank across all physical source libraries.
    fetch_multiplier = 50
    fetch_limit = max(1, min(topk * fetch_multiplier, 2000))
    params.append(fetch_limit)
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    rows = connection.execute(
        f"""SELECT a.*, bm25(semantic_assets_fts) AS rank
            FROM semantic_assets_fts
            JOIN semantic_assets a ON a.rowid=semantic_assets_fts.rowid
            WHERE {' AND '.join(filters)}
            ORDER BY rank, a.callable DESC, a.usage_count DESC, a.logical_id
            LIMIT ?""",
        params,
    ).fetchall()
    connection.close()
    candidates: list[dict[str, Any]] = []
    minimum = [float(value) for value in min_size_m] if min_size_m is not None else None
    maximum = [float(value) for value in max_size_m] if max_size_m is not None else None
    if minimum is not None and len(minimum) != 3:
        raise ValueError("min_size_m must contain three values")
    if maximum is not None and len(maximum) != 3:
        raise ValueError("max_size_m must contain three values")
    for lexical_index, row in enumerate(rows):
        bbox = json.loads(row["bbox_json"]) if row["bbox_json"] else None
        metadata = json.loads(row["metadata_json"]) if row["metadata_json"] else {}
        size = None
        if isinstance(bbox, dict):
            raw_size = (
                bbox.get("size_meters")
                or bbox.get("size_m")
                or bbox.get("size")
                or bbox.get("extent")
            )
            if isinstance(raw_size, list) and len(raw_size) == 3:
                size = [float(value) for value in raw_size]
        if needs_size_filter and size is None:
            continue
        if minimum is not None and any(value < bound for value, bound in zip(size or [], minimum)):
            continue
        if maximum is not None and any(value > bound for value, bound in zip(size or [], maximum)):
            continue
        entry_rank = float(metadata.get("entry_rank") or 0.0)
        quality_score = (
            0.10 * int(bool(row["callable"]))
            + 0.24 * int(bool(row["preview_index_path"]))
            + 0.20 * int(bool(row["front_axis"]))
            + 0.20 * int(bool(bbox))
            + 0.06 * int(row["meters_per_unit"] is not None)
            + 0.20 * min(max(entry_rank, 0.0), 100.0) / 100.0
        )
        lexical_score = 1.0 - min(lexical_index / max(fetch_limit - 1, 1), 1.0)
        normalized_query = normalize_semantic_name(query)
        exact_name_bonus = 0.08 if normalized_query and normalized_query in str(row["semantic_name"]) else 0.0
        exact_class_bonus = 0.04 if effective_class and row["canonical_class"] == effective_class else 0.0
        retrieval_score = min(
            1.0,
            0.72 * lexical_score + 0.28 * quality_score + exact_name_bonus + exact_class_bonus,
        )
        candidates.append({
            "logical_id": row["logical_id"],
            "canonical_class": row["canonical_class"],
            "subtype": row["subtype"],
            "semantic_name": row["semantic_name"],
            "source_library": row["source_library"],
            "source_asset_id": row["source_asset_id"],
            "path": row["source_path"],
            "remote_model_id": row["remote_model_id"],
            "availability": row["availability"],
            "callable": bool(row["callable"]),
            "preview_index_path": row["preview_index_path"],
            "front_axis": row["front_axis"],
            "meters_per_unit": row["meters_per_unit"],
            "bbox": bbox,
            "style_tags": json.loads(row["style_tags_json"]),
            "material_tags": json.loads(row["material_tags_json"]),
            "color_tags": json.loads(row["color_tags_json"]),
            "license_status": row["license_status"],
            "usage_count": row["usage_count"],
            "scene_slugs": json.loads(row["scene_slugs_json"]),
            "metadata": metadata,
            "asset_quality_score": round(quality_score, 6),
            "retrieval_score": round(retrieval_score, 6),
            "inferred_class": effective_class if canonical_class is None else None,
            "_semantic_rank": float(row["rank"]),
        })
    for adapter_path, allowed_scope in _synthesis_object_adapter_paths(
        read_only_adapters
    ):
        candidates.extend(_search_synthesis_object_adapter(
            adapter_path,
            allowed_scope,
            query,
            effective_class=effective_class,
            subtype=subtype,
            style_tags=style_tags,
            material_tags=material_tags,
            color_tags=color_tags,
            minimum=minimum,
            maximum=maximum,
        ))
    candidates = list({row["logical_id"]: row for row in candidates}.values())
    candidates.sort(
        key=lambda item: (
            -float(item["retrieval_score"]),
            float(item["_semantic_rank"]),
            str(item["logical_id"]),
        )
    )
    limit = max(1, min(topk, 200))
    if not diversify:
        return candidates[:limit]
    result: list[dict[str, Any]] = []
    remaining = list(candidates)
    source_counts: dict[str, int] = {}
    name_counts: dict[str, int] = {}
    while remaining and len(result) < limit:
        def diversified_score(item: dict[str, Any]) -> tuple[float, float, str]:
            source = str(item["source_library"])
            name = str(item["semantic_name"])
            penalty = 0.035 * source_counts.get(source, 0) + 0.055 * name_counts.get(name, 0)
            return (
                float(item["retrieval_score"]) - penalty,
                -float(item["_semantic_rank"]),
                str(item["logical_id"]),
            )

        selected = max(remaining, key=diversified_score)
        remaining.remove(selected)
        result.append(selected)
        source = str(selected["source_library"])
        name = str(selected["semantic_name"])
        source_counts[source] = source_counts.get(source, 0) + 1
        name_counts[name] = name_counts.get(name, 0) + 1
    return result


def taxonomy(
    catalog_path: str | Path,
    *,
    prefix: str | None = None,
    callable_only: bool = False,
    limit: int = 200,
    read_only_adapters: Iterable[dict[str, Any] | str] = (),
) -> dict[str, Any]:
    """Summarize the unified catalog by semantics, never by source library.

    Source libraries remain nested provenance facets so callers can audit or
    filter them without making source layout the retrieval hierarchy.
    """
    path = Path(catalog_path)
    if not path.is_file():
        return {"catalog_path": str(path), "totals": {}, "classes": []}
    filters: list[str] = []
    params: list[Any] = []
    if prefix:
        filters.append("canonical_class LIKE ?")
        params.append(prefix.rstrip("/") + "%")
    if callable_only:
        filters.append("callable=1")
    where = f"WHERE {' AND '.join(filters)}" if filters else ""
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    rows = connection.execute(
        f"""SELECT canonical_class,
                   COUNT(*) AS asset_count,
                   SUM(callable) AS callable_count,
                   SUM(CASE WHEN preview_index_path IS NOT NULL THEN 1 ELSE 0 END) AS preview_count,
                   SUM(CASE WHEN front_axis IS NOT NULL THEN 1 ELSE 0 END) AS oriented_count,
                   SUM(CASE WHEN bbox_json IS NOT NULL THEN 1 ELSE 0 END) AS measured_count,
                   SUM(usage_count) AS observed_instances
            FROM semantic_assets
            {where}
            GROUP BY canonical_class
            ORDER BY asset_count DESC, canonical_class
            LIMIT ?""",
        [*params, max(1, min(int(limit), 1000))],
    ).fetchall()
    classes: list[dict[str, Any]] = []
    for row in rows:
        canonical_class = str(row["canonical_class"])
        callable_clause = " AND callable=1" if callable_only else ""
        source_rows = connection.execute(
            f"""SELECT source_library, COUNT(*) AS count, SUM(callable) AS callable_count
               FROM semantic_assets
               WHERE canonical_class=?{callable_clause}
               GROUP BY source_library
               ORDER BY count DESC, source_library""",
            (canonical_class,),
        ).fetchall()
        name_rows = connection.execute(
            f"""SELECT semantic_name, subtype, COUNT(*) AS count, SUM(usage_count) AS usage_count
               FROM semantic_assets
               WHERE canonical_class=?{callable_clause}
               GROUP BY semantic_name, subtype
               ORDER BY usage_count DESC, count DESC, semantic_name
               LIMIT 8""",
            (canonical_class,),
        ).fetchall()
        classes.append({
            "canonical_class": canonical_class,
            "asset_count": int(row["asset_count"] or 0),
            "callable_count": int(row["callable_count"] or 0),
            "preview_count": int(row["preview_count"] or 0),
            "oriented_count": int(row["oriented_count"] or 0),
            "measured_count": int(row["measured_count"] or 0),
            "observed_instances": int(row["observed_instances"] or 0),
            "semantic_types": [
                {
                    "name": item["semantic_name"],
                    "subtype": item["subtype"],
                    "asset_count": int(item["count"] or 0),
                    "usage_count": int(item["usage_count"] or 0),
                }
                for item in name_rows
            ],
            "provenance": [
                {
                    "source_library": item["source_library"],
                    "asset_count": int(item["count"] or 0),
                    "callable_count": int(item["callable_count"] or 0),
                }
                for item in source_rows
            ],
        })
    total_row = connection.execute(
        f"""SELECT COUNT(*) AS asset_count,
                   SUM(callable) AS callable_count,
                   COUNT(DISTINCT canonical_class) AS class_count,
                   COUNT(DISTINCT source_library) AS source_count
            FROM semantic_assets
            {where}""",
        params,
    ).fetchone()
    connection.close()
    result = {
        "catalog_path": str(path),
        "prefix": prefix,
        "callable_only": callable_only,
        "totals": {
            "asset_count": int(total_row["asset_count"] or 0),
            "callable_count": int(total_row["callable_count"] or 0),
            "class_count": int(total_row["class_count"] or 0),
            "source_count": int(total_row["source_count"] or 0),
        },
        "classes": classes,
    }
    adapter_rows: list[sqlite3.Row] = []
    adapter_paths = _synthesis_object_adapter_paths(read_only_adapters)
    for adapter_path, allowed_scope in adapter_paths:
        adapter_connection = sqlite3.connect(
            f"file:{adapter_path}?mode=ro", uri=True
        )
        adapter_connection.row_factory = sqlite3.Row
        adapter_filters = [
            "callable=1",
            "qa_status='ready'",
            "license_review_status='verified'",
            "allowed_use_scope=?",
        ]
        adapter_params: list[Any] = [allowed_scope]
        if prefix:
            adapter_filters.append("canonical_class LIKE ?")
            adapter_params.append(prefix.rstrip("/") + "%")
        adapter_rows.extend(adapter_connection.execute(
            f"""SELECT canonical_class, semantic_name, subtype, usage_count
                  FROM objects WHERE {' AND '.join(adapter_filters)}""",
            adapter_params,
        ).fetchall())
        adapter_connection.close()
    if not adapter_rows:
        result["read_only_adapters"] = [str(path) for path, _ in adapter_paths]
        return result
    by_class = {row["canonical_class"]: row for row in result["classes"]}
    for adapter_row in adapter_rows:
        class_name = str(adapter_row["canonical_class"])
        entry = by_class.get(class_name)
        if entry is None:
            entry = {
                "canonical_class": class_name,
                "asset_count": 0,
                "callable_count": 0,
                "preview_count": 0,
                "oriented_count": 0,
                "measured_count": 0,
                "observed_instances": 0,
                "semantic_types": [],
                "provenance": [],
            }
            result["classes"].append(entry)
            by_class[class_name] = entry
        entry["asset_count"] += 1
        entry["callable_count"] += 1
        entry["preview_count"] += 1
        entry["measured_count"] += 1
        entry["observed_instances"] += int(adapter_row["usage_count"] or 0)
        semantic_key = (adapter_row["semantic_name"], adapter_row["subtype"])
        semantic = next((item for item in entry["semantic_types"] if (
            item["name"], item["subtype"]
        ) == semantic_key), None)
        if semantic is None:
            semantic = {
                "name": adapter_row["semantic_name"],
                "subtype": adapter_row["subtype"],
                "asset_count": 0,
                "usage_count": 0,
            }
            entry["semantic_types"].append(semantic)
        semantic["asset_count"] += 1
        semantic["usage_count"] += int(adapter_row["usage_count"] or 0)
        provenance = next((item for item in entry["provenance"] if (
            item["source_library"] == "synthesis28_objects"
        )), None)
        if provenance is None:
            provenance = {
                "source_library": "synthesis28_objects",
                "asset_count": 0,
                "callable_count": 0,
            }
            entry["provenance"].append(provenance)
        provenance["asset_count"] += 1
        provenance["callable_count"] += 1
    result["classes"].sort(
        key=lambda row: (-row["asset_count"], row["canonical_class"])
    )
    result["classes"] = result["classes"][:max(1, min(int(limit), 1000))]
    result["totals"] = {
        "asset_count": result["totals"]["asset_count"] + len(adapter_rows),
        "callable_count": result["totals"]["callable_count"] + len(adapter_rows),
        "class_count": len(by_class),
        "source_count": result["totals"]["source_count"] + 1,
    }
    result["read_only_adapters"] = [str(path) for path, _ in adapter_paths]
    return result


def annotate(
    catalog_path: str | Path,
    *,
    logical_id: str | None = None,
    source_path: str | None = None,
    canonical_class: str | None = None,
    subtype: str | None = None,
    semantic_name: str | None = None,
    preview_index_path: str | None = None,
    front_axis: str | None = None,
    meters_per_unit: float | None = None,
    bbox: dict[str, Any] | None = None,
    style_tags: Iterable[str] = (),
    material_tags: Iterable[str] = (),
    color_tags: Iterable[str] = (),
    metadata_patch: dict[str, Any] | None = None,
    rebuild_search_index: bool = True,
) -> dict[str, Any]:
    if not logical_id and not source_path:
        raise ValueError("logical_id or source_path is required")
    if front_axis is not None and front_axis not in {"+X", "-X", "+Y", "-Y"}:
        raise ValueError("front_axis must be one of +X, -X, +Y, -Y")
    if canonical_class is not None and not re.fullmatch(r"[a-z0-9_]+(?:/[a-z0-9_]+)+", canonical_class):
        raise ValueError("canonical_class must be a lowercase slash hierarchy")
    path = Path(catalog_path)
    if not path.is_file():
        raise FileNotFoundError(path)
    connection = sqlite3.connect(path)
    initialize(connection)
    connection.row_factory = sqlite3.Row
    if logical_id:
        row = connection.execute(
            "SELECT * FROM semantic_assets WHERE logical_id=?",
            (logical_id,),
        ).fetchone()
    else:
        row = connection.execute(
            "SELECT * FROM semantic_assets WHERE source_path=?",
            (source_path,),
        ).fetchone()
    if row is None:
        connection.close()
        raise KeyError(logical_id or source_path)

    def merge_tags(column: str, additions: Iterable[str]) -> str:
        current = json.loads(row[column])
        merged = {
            normalize_semantic_name(str(value))
            for value in [*current, *additions]
            if normalize_semantic_name(str(value))
        }
        return _json(sorted(merged))

    metadata = json.loads(row["metadata_json"])
    if metadata_patch:
        metadata.update(metadata_patch)
    style_tags_json = merge_tags("style_tags_json", style_tags)
    material_tags_json = merge_tags("material_tags_json", material_tags)
    color_tags_json = merge_tags("color_tags_json", color_tags)
    normalized_semantic_name = (
        normalize_semantic_name(semantic_name)
        if semantic_name is not None
        else row["semantic_name"]
    )
    normalized_subtype = (
        normalize_semantic_name(subtype)
        if subtype is not None
        else row["subtype"]
    )
    normalized_class = canonical_class or row["canonical_class"]
    added_search_terms = [
        normalized_semantic_name,
        normalized_subtype,
        normalized_class.replace("/", " "),
        *json.loads(style_tags_json),
        *json.loads(material_tags_json),
        *json.loads(color_tags_json),
    ]
    search_text = " ".join(dict.fromkeys([
        *str(row["search_text"]).split(),
        *added_search_terms,
    ]))
    updates = {
        "preview_index_path": preview_index_path if preview_index_path is not None else row["preview_index_path"],
        "canonical_class": normalized_class,
        "subtype": normalized_subtype,
        "semantic_name": normalized_semantic_name,
        "front_axis": front_axis if front_axis is not None else row["front_axis"],
        "meters_per_unit": meters_per_unit if meters_per_unit is not None else row["meters_per_unit"],
        "bbox_json": _json(bbox) if bbox is not None else row["bbox_json"],
        "style_tags_json": style_tags_json,
        "material_tags_json": material_tags_json,
        "color_tags_json": color_tags_json,
        "metadata_json": _json(metadata),
        "search_text": search_text,
    }
    connection.execute(
        """UPDATE semantic_assets
           SET canonical_class=:canonical_class,
               subtype=:subtype,
               semantic_name=:semantic_name,
               preview_index_path=:preview_index_path,
               front_axis=:front_axis,
               meters_per_unit=:meters_per_unit,
               bbox_json=:bbox_json,
               style_tags_json=:style_tags_json,
               material_tags_json=:material_tags_json,
               color_tags_json=:color_tags_json,
               metadata_json=:metadata_json,
               search_text=:search_text
           WHERE logical_id=:logical_id""",
        {**updates, "logical_id": row["logical_id"]},
    )
    override_patch = {
        key: updates[key]
        for key in (
            "canonical_class", "subtype", "semantic_name",
            "preview_index_path", "front_axis", "meters_per_unit", "bbox_json",
            "style_tags_json", "material_tags_json", "color_tags_json",
            "metadata_json", "search_text",
        )
    }
    connection.execute(
        """INSERT INTO semantic_overrides(logical_id, patch_json, updated_at)
           VALUES(?, ?, ?)
           ON CONFLICT(logical_id) DO UPDATE SET
             patch_json=excluded.patch_json,
             updated_at=excluded.updated_at""",
        (
            row["logical_id"],
            _json(override_patch),
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    if rebuild_search_index:
        connection.execute("INSERT INTO semantic_assets_fts(semantic_assets_fts) VALUES('rebuild')")
    connection.commit()
    updated = connection.execute(
        """SELECT logical_id, semantic_name, canonical_class, subtype, source_path,
                  preview_index_path, front_axis, meters_per_unit, bbox_json,
                  style_tags_json, material_tags_json, color_tags_json,
                  metadata_json
           FROM semantic_assets WHERE logical_id=?""",
        (row["logical_id"],),
    ).fetchone()
    connection.close()
    return {
        "logical_id": updated["logical_id"],
        "semantic_name": updated["semantic_name"],
        "canonical_class": updated["canonical_class"],
        "subtype": updated["subtype"],
        "path": updated["source_path"],
        "preview_index_path": updated["preview_index_path"],
        "front_axis": updated["front_axis"],
        "meters_per_unit": updated["meters_per_unit"],
        "bbox": json.loads(updated["bbox_json"]) if updated["bbox_json"] else None,
        "style_tags": json.loads(updated["style_tags_json"]),
        "material_tags": json.loads(updated["material_tags_json"]),
        "color_tags": json.loads(updated["color_tags_json"]),
        "metadata": json.loads(updated["metadata_json"]),
    }


def set_asset_state(
    catalog_path: str | Path,
    *,
    state: str,
    reason: str,
    logical_id: str | None = None,
    source_path: str | None = None,
    evidence_path: str | None = None,
) -> dict[str, Any]:
    """Quarantine or restore a logical asset without touching its source USD."""
    if state not in {"quarantine", "ready"}:
        raise ValueError("state must be quarantine or ready")
    if not reason.strip():
        raise ValueError("reason is required")
    if not logical_id and not source_path:
        raise ValueError("logical_id or source_path is required")
    path = Path(catalog_path)
    if not path.is_file():
        raise FileNotFoundError(path)
    connection = sqlite3.connect(path)
    initialize(connection)
    connection.row_factory = sqlite3.Row
    if logical_id:
        row = connection.execute(
            "SELECT * FROM semantic_assets WHERE logical_id=?",
            (logical_id,),
        ).fetchone()
    else:
        row = connection.execute(
            "SELECT * FROM semantic_assets WHERE source_path=?",
            (source_path,),
        ).fetchone()
    if row is None:
        connection.close()
        raise KeyError(logical_id or source_path)
    metadata = json.loads(row["metadata_json"])
    history = metadata.get("asset_state_history")
    if not isinstance(history, list):
        history = []
    event = {
        "state": state,
        "reason": reason.strip(),
        "evidence_path": evidence_path,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    history.append(event)
    metadata["asset_state_history"] = history[-50:]
    if state == "quarantine":
        if "quarantine_restore" not in metadata:
            metadata["quarantine_restore"] = {
                "callable": int(row["callable"]),
                "availability": str(row["availability"]),
            }
        callable_value = 0
        availability = "quarantined"
    else:
        restore = metadata.pop("quarantine_restore", {})
        callable_value = int(restore.get("callable", 1))
        availability = str(restore.get("availability") or "local")
    metadata_json = _json(metadata)
    connection.execute(
        """UPDATE semantic_assets
           SET callable=?, availability=?, metadata_json=?
           WHERE logical_id=?""",
        (callable_value, availability, metadata_json, row["logical_id"]),
    )
    previous_override = connection.execute(
        "SELECT patch_json FROM semantic_overrides WHERE logical_id=?",
        (row["logical_id"],),
    ).fetchone()
    patch = json.loads(previous_override[0]) if previous_override else {}
    patch.update({
        "callable": callable_value,
        "availability": availability,
        "metadata_json": metadata_json,
    })
    connection.execute(
        """INSERT INTO semantic_overrides(logical_id, patch_json, updated_at)
           VALUES(?, ?, ?)
           ON CONFLICT(logical_id) DO UPDATE SET
             patch_json=excluded.patch_json,
             updated_at=excluded.updated_at""",
        (
            row["logical_id"],
            _json(patch),
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    connection.commit()
    connection.close()
    return {
        "logical_id": row["logical_id"],
        "path": row["source_path"],
        "state": state,
        "callable": bool(callable_value),
        "availability": availability,
        "reason": reason.strip(),
        "evidence_path": evidence_path,
    }
