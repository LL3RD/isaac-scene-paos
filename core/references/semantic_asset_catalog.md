# Unified semantic asset catalog

The retrieval surface is semantic-first. Physical source libraries remain intact and are recorded as provenance; do not reorganize or duplicate large USD trees by source. In other words, agents browse one logical asset library, not separate "Isaac/GenieSim/GRScenes/Fabrica" shelves.

Configured `semantic_catalog.read_only_adapters` participate at query time without copying rows or
USD packages into the primary database. The Synthesis object adapter opens its SQLite catalog with
`mode=ro` and exposes only exact-ID rows that are still `callable=1`, `qa_status=ready`, license
verified, and approved for the configured `allowed_use_scope`. Preserve the returned attribution
and scope in every Scene Spec and handoff; do not widen internal research approval.

## Taxonomy

Every logical asset has:

- `canonical_class`: hierarchy such as `furniture/seating/office_chair`, `medical/equipment`, or `industrial/machine`;
- `subtype` and normalized semantic name;
- aliases, domain, style, material and color tags;
- source library, source asset ID/path and license status;
- callable/availability state;
- preview index, bbox, meters-per-unit and verified front axis;
- optional reference-scene usage.

Search class/subtype first, then visually rerank previews. Source library is a tie-breaker and provenance field, not the primary category.

The hierarchy is functional rather than vendor-specific:

- `architecture/*`: shell surfaces, openings, partitions and structure;
- `furniture/{seating,surface,storage}/*`;
- `appliance/*`, `electronics/*`, `lighting/*` and `plumbing/*`;
- `medical/*`, `laboratory/*`, `industrial/*`, `logistics/*`, `retail/*` and `transport/*`;
- `decor/*`, `household/*`, `kitchenware/*`, `food/*`, `tool/*` and `small_prop`;
- `robot/*`, `sensor/*`, `human/*` and complete `environment/*` stages.

The CLI infers a semantic prefix from ordinary queries. For example, `椅子`
expands to `chair` and searches all `furniture/seating/*`, while `会议椅`
narrows to `furniture/seating/conference_chair`. Use `--broad` only when the
inferred class is visibly wrong. Results are then reranked by lexical match,
callability, root-entry quality, preview, bbox, units and verified front axis,
with a small diversity penalty so one source or one repeated model does not
fill the entire candidate list.

Classify the asset's own semantic name before its parent path. A pallet nested
under a `rack_assembly/props` directory is a pallet, not a rack. Split
polysemous types when they affect placement: warehouse, industrial, bike,
roof and display racks are separate leaves under `furniture/storage/rack/*`.
Keep high-frequency appliances in functional leaves instead of one broad
bucket. The current kitchen leaves are
`appliance/kitchen/refrigerator`, `freezer`, `microwave`, `electric_cooker`,
`coffee_maker`, and `oven`. Preserve broad `appliance/kitchen` only for assets
whose function is not yet resolved.

Use explicit facets instead of packing every requirement into free text:

```bash
python3 scripts/scene_tools.py --config "$ISAAC_SCENE_SKILL_CONFIG" --envelope \
  search-semantic-assets "conference chair" \
  --canonical-class furniture/seating --subtype cantilever \
  --style modern --material metal --color black \
  --min-size-m 0.4 0.4 0.8 --max-size-m 0.8 0.8 1.2
```

Size-constrained search only returns measured assets. If the pool becomes too
small, render/measure more candidates rather than silently ignoring the size
constraint.

Use the semantic tree as the discovery entry point:

```bash
python3 scripts/scene_tools.py --config "$ISAAC_SCENE_SKILL_CONFIG" --envelope \
  list-semantic-classes --prefix furniture/seating --callable-only
```

The result is grouped by `canonical_class` and semantic type. Counts from
GRScenes, GenieSim, Isaac official, Fabrica and Synthesis are nested under
`provenance`; they are not top-level library buckets. Keep the original USD
trees in place and resolve logical IDs back to their paths instead of copying
or moving dependency-heavy packages.

`build_semantic_asset_catalog.py` also writes a sibling
`semantic_assets.taxonomy.json`. This is the human/agent-readable semantic
tree; SQLite remains the authoritative resolver from logical ID to source
path. Never make a second physical copy of a USD merely to put it in a
semantic folder: doing so can break relative texture, MDL, payload and
sublayer dependencies.

`render_asset_previews.py --semantic-catalog ...` writes the preview index and
bbox back to the logical catalog. Long batches should use `--resume`: a row is
skipped only when its status is `pass` and every requested preview still
exists above `--min-output-bytes`; interrupted/running or incomplete rows are
rerendered. The manifest is atomically checkpointed after each asset. Build reusable CLIP embeddings with
`build_asset_visual_index.py`; use `search-visual-assets` for text/reference
crop reranking. Record agent-verified `front_axis`, style, material and color
with `annotate-semantic-asset`. When an opaque source filename was classified
too broadly, the same command may correct `--canonical-class`, `--subtype` and
`--semantic-name`; this updates the unified view without moving the source USD.

If previewing proves that a USD cannot open, has degenerate/non-finite bounds,
or is only a support layer instead of a complete insertable asset, quarantine
the logical record:

```bash
python3 scripts/scene_tools.py --config "$ISAAC_SCENE_SKILL_CONFIG" --envelope \
  set-semantic-asset-state --logical-id pcnl:asset-id \
  --state quarantine --reason "degenerate preview bounds" \
  --evidence /path/to/preview_index.json
```

This is reversible and never deletes the source. After repairing the package,
rerender previews and restore it with `--state ready`; the original callable
and availability state is recovered from the audit metadata.

## Build and search

On the configured asset host:

```bash
python3 scripts/build_semantic_asset_catalog.py \
  --output /path/to/catalog/semantic_assets.sqlite \
  --pcnl-catalog /path/to/catalog/assets.sqlite \
  --official-index /path/to/catalog/unique_assets.sqlite \
  --synthesis-manifest /path/to/synthesis27_metadata/manifest.json \
  --synthesis-root /path/to/synthesis27_metadata

python3 scripts/scene_tools.py --config "$ISAAC_SCENE_SKILL_CONFIG" --envelope \
  search-semantic-assets "office chair" --canonical-class furniture/seating --topk 20
```

Use `--include-unavailable` during planning to expose exact metadata-only Synthesis candidates. Never insert one until acquisition, dependency normalization and license verification mark it callable.

## Synthesis-28 scene kits

Object search alone often returns a semantically valid but visually incoherent mix. Build the compact
scene-kit catalog from the Synthesis object SQLite and environment catalog, then search it before
per-request retrieval:

```bash
python3 scripts/scene_tools.py --config "$ISAAC_SCENE_SKILL_CONFIG" --envelope \
  build-synthesis28-scene-kits --output /path/to/scene_kits.json
python3 scripts/scene_tools.py --config "$ISAAC_SCENE_SKILL_CONFIG" --envelope \
  search-synthesis28-scene-kits "clinical consultation room" \
  --domain medical --catalog /path/to/scene_kits.json --topk 5
```

Each kit is a pose-free scene signature: domain/room aliases, instance and unique-model counts,
semantic composition, density tiers, and same-scene asset-family candidates. It must not contain
instance transforms, matrices, source camera opinions, or a reference to insert the complete scene.
`plan-scene-enrichment` reads the configured catalog automatically and adds matched scene slugs plus
callable same-scene candidate IDs to the selection policy. Final insertion still requires ordinary
identity, availability, license, preview, unit, orientation, support, collision and render gates.

## Synthesis-27 library

Build the isolated reference library before mixing in external sources:

```bash
python3 scripts/build_synthesis27_asset_library.py \
  --manifest /path/to/synthesis_remaining/manifest.json \
  --dataset-root /path/to/synthesis_remaining \
  --acquisition-plan /path/to/synthesis27_acquisition_plan.json \
  --output-dir /path/to/synthesis27_asset_library \
  --strict
```

The authoritative SQLite contains `assets`, `instances`, `scenes`,
`external_features` and `ingest_anomalies`. It also exports deterministic
JSON/JSONL, a semantic taxonomy and an availability report. The 27 reference
scenes currently resolve to 488 exact stable model IDs and 2,711 model
instances, plus one external 3DGS feature.

Deduplicate assets only by exact `modelID`; use `scene_slug + idInScene` for
instances. Display names are aliases because distinct IDs may share one name.
Pair `scene_info.models` to `scene_data.extDataArr` by a FIFO occurrence queue
per model ID so repeated chairs, cartons or conveyors keep distinct instance
IDs and poses. Preserve `offset/rotation/scale` as
`synthesis_scene_local` source data with unknown units and unverified Euler
order until an acquired USD proves the conversion.

Keep asset scopes separate:

- the first business-type 2 instance is the primary `scene_root`;
- later type 2 instances are `scene_layout_component`;
- type 1 is `independent_object`;
- type 5 is `robot`;
- type 3 without `modelID` is an external 3DGS feature, not a model asset.

Default object retrieval includes independent objects and robots. Root/layout
assets require explicit scope.

Maintain two evaluation modes:

- `independent_reconstruction`: source assets are excluded; measures generalization.
- `reference_asset_reconstruction`: individually acquired and normalized source objects are allowed; measures maximum fidelity and layout generation.

Do not import the complete source scene in either mode. For source objects:

1. verify access and license;
2. download the individual USD package through the supported authenticated API/UI;
3. resolve textures, MDL and sublayers into a self-contained package;
4. inspect units/bbox, render four previews and determine front axis;
5. attach semantic/style/material/color tags and visual embedding;
6. set `callable=1` only after Isaac Sim validation passes.

Asset acquisition is a separate, administrator-owned workflow. This distribution accepts already-acquired local packages; it contains no login-token capture or protected-download tools.

When the catalog is local but `pxr` validation runs on a separate compute host, import the
returned report instead of copying the remote SQLite database back. This keeps
the local package path authoritative:

```bash
python3 scripts/apply_synthesis27_runtime_validation.py \
  --library /local/synthesis27_assets.sqlite \
  --report /local/copied_runtime_validation.json \
  --output /local/runtime_validation_import.json --strict
```

This advances only `local_acquired` rows with a passing report to
`local_structural_validated`; it deliberately leaves `callable=0` until
preview, front-axis, license and insertion-smoke gates pass.

After a full generated-scene evaluation, also run
`build_synthesis27_repair_queue.py`. It groups proxy/authored gaps by canonical
class, ranks scenes by proxy ratio, anchor coverage and reference score, and
links each gap to same-scene Synthesis model IDs. Those matches are acquisition
candidates, not permission to insert metadata-only assets.
