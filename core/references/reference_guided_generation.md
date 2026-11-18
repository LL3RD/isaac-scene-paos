# Reference-guided scene generation

Use this workflow when the user provides an image or asks for a scene close to a named reference. Structural validity and visual similarity are separate gates.

## Unified input

Convert text, one image, or several images into one explicit scene brief:

- room envelope: dimensions or aspect-ratio estimate, openings, floor/wall/ceiling materials;
- camera: projection, height, yaw/pitch, field of view, visible room boundaries;
- object scene graph: semantic role, count, approximate size, color/material/style, screen-space box, support and facing relations;
- zones and circulation: functional groups, wall affinity, minimum walkable paths;
- quality policy: `layout_only`, `realistic`, or `reference_match`.

Keep uncertainty on each inferred field. Do not silently turn an uncertain object into a cube.

## Asset retrieval

For every visible anchor, retrieve in two stages:

1. Semantic recall: category, function, scale and domain produce 20–100 candidates.
2. Visual reranking: compare candidate previews with the reference crop using shape, style, color and material. Record the chosen candidate, preview, score, rejected alternatives and reason.

Persist previews and embeddings:

```bash
python3 scripts/prepare_asset_preview_batch.py \
  --catalog /path/to/semantic_assets.sqlite \
  --query "tall indoor palm in black planter" \
  --canonical-class decor/plant \
  --visual-index /path/to/semantic_asset_visual.sqlite \
  --exclude-source unsuitable_source \
  --recall 200 --limit 32 --output candidates.json

# When an exact functional leaf is sparse, explicitly add a broader class.
# Exact-class rows remain first; fallback rows are only candidates and must
# still pass preview, size, reference-match and orientation review.
python3 scripts/prepare_asset_preview_batch.py \
  --catalog /path/to/semantic_assets.sqlite \
  --query "long modern corporate conference table" \
  --canonical-class furniture/surface/meeting_table \
  --fallback-class furniture/surface/table \
  --visual-index /path/to/semantic_asset_visual.sqlite \
  --recall 200 --limit 32 --output meeting_table_candidates.json

python3 scripts/build_asset_visual_index.py \
  --preview-index /path/to/preview_index.json \
  --output /path/to/semantic_asset_visual.sqlite

python3 scripts/scene_tools.py --config "$ISAAC_SCENE_SKILL_CONFIG" --envelope \
  search-visual-assets "slim silver conference chair" \
  --canonical-class furniture/seating \
  --reference-crop chair_crop.jpg

python3 scripts/scene_tools.py --config "$ISAAC_SCENE_SKILL_CONFIG" --envelope \
  search-visual-assets "long modern corporate conference table" \
  --canonical-class furniture/surface/meeting_table \
  --fallback-class furniture/surface/table \
  --reference-crop table_crop.jpg --target-size-m 3.0 1.2 0.75
```

`prepare_asset_preview_batch.py` preserves the descriptive-query results first,
then backfills from the canonical semantic class when the description has weak
lexical overlap with generic source names. Source include/exclude flags are
only preview-pool controls; the persistent catalog remains semantics-first.
Use `--fallback-class` only for an explicit broader semantic parent or a
functionally interchangeable class. A fallback result is never promoted merely
because the exact class was sparse: its rendered identity, physical dimensions,
support behavior and reference crop must pass the same gates.
Pass the same fallback classes to `search-visual-assets`; otherwise the
preferred leaf and fallback candidates are rendered together but ranked in
separate pools, producing misleading coverage and margin evidence.
Set `HF_HOME` to an administrator-configured model cache; cached execution may set
`HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1`.

After inspecting the winning four-view preview, persist its actual local front
axis instead of guessing it from category:

```bash
python3 scripts/scene_tools.py --config "$ISAAC_SCENE_SKILL_CONFIG" --envelope \
  annotate-semantic-asset --logical-id pcnl:asset-id \
  --front-axis=-Y --style modern --material metal --color silver
```

Apply these hard rules in `reference_match` mode:

- no primitive proxy for visible objects; architectural shell primitives are allowed;
- architectural primitives must remain room-shell geometry. Renaming a visible
  display, appliance, furniture item, lamp, plant, machine, or robot to a panel,
  box, frame, or beam is still a visible-object proxy and must fail brief
  validation;
- declare at least one anchor whenever visible non-shell entities exist, and require every anchor to use a verified real asset;
- do not auto-select a low-confidence result;
- require a sufficiently large source-diverse preview pool and a model-specific
  top-two score margin; being first among weak candidates is not a pass;
- use `equivalence_group` only for duplicate exports or variants whose
  silhouette, structure, support, material family and task-relevant appearance
  are interchangeable. Sharing a broad subtype such as "black cantilever
  chair" is not enough when backrest, armrest, base or upholstery visibly
  differ;
- a small specialized class may satisfy coverage only when every callable
  inventory item has a valid preview and at least 10 candidates were compared;
  this exception does not relax score or top-two margin thresholds;
- require a rendered preview before accepting an asset whose front axis is unknown;
- diversify repeated categories unless the reference clearly uses identical instances;
- cap incidental cross-scene reuse; stable hash order is not a relevance signal;
- if no candidate meets threshold, report `ASSET_GAP` and queue asset acquisition or procedural authoring.

Do not lower the global score, margin or coverage thresholds to rescue one
case. When automatic selection fails only because a sparse exact-class pool or
a near tie remains, an external harness may use
`selection_basis=independent_review_override` only after two distinct
reviewers independently inspect the raw reference crop and at least two
candidate previews. Persist both reviewer IDs, the selected and comparison
preview paths, at least two observed reference features, rejected candidates
and reasons, the original automatic gate failures, and passing identity,
geometry and orientation gates. The validator also requires score >= 0.24 and
at least two compared candidates. This override cannot rescue a category
mismatch, wrong scale/support, unknown orientation, missing preview, or a
single-reviewer preference.

## Layout and orientation

Solve in this order:

1. match the room shell and reference camera;
2. place anchors from screen-space/depth evidence and functional zones;
3. enforce support, clearance, circulation, wall and pairwise facing constraints;
4. insert medium objects relative to their anchors;
5. insert detail objects only after anchors pass.

Treat a room-shell preset as a candidate, not a guaranteed improvement. Compare it with the
primitive-shell baseline using the same objects and fixed camera; accept or reject per scene.
Never keep a preset that introduces object-shell overlap, dominant occlusion, or a reference
score regression beyond the normal A/B tolerance merely because it looks more detailed.

For directional assets, preview the source asset and persist its actual
`front_axis`. Declare `facing_target` plus subject/target translations in the
Scene Brief; compilation deterministically replaces the requested yaw with the
verified local-front-to-world-target solution and records
`orientation_solver`. Validate the rendered front with `inspect-facing`, not
only the bounding box.

## Feedback loop

Keep one comparison camera fixed while repairing:

1. geometry, support and facing;
2. anchor identity, count and silhouette;
3. screen-space placement and scale;
4. materials and palette;
5. lighting/exposure;
6. medium/detail completeness.

Keep lighting and exposure as separate hypotheses. Encode reference-visible
environment illumination and fixtures in `Scene Brief.lighting`; compilation
uses that rig instead of the generic dome/key fallback. Fix missing or
misdirected light, color temperature, shadowing and reflective response before
changing `camera.exposure_ev`. Persist the fixed-camera render feedback that
justifies each lighting change.

Object edits use a multi-gate decision, not the scalar image score alone:

- `presence_evidence`: an object must be visible, text-requested, or functionally required;
- `identity_gate`: category, count, silhouette and support/facing role agree with the observation;
- `geometry_gate`: no new collision, support, scale or circulation failure;
- `composition_score`: use the deterministic score only after the first three gates.

Extra edges or colors can raise a coarse histogram/edge score even when they
come from a hallucinated object. Conversely, removing an unsupported object
can lower that score. The external image-reading agent must therefore record
object-level evidence and may reject the scalar ranking when identity evidence
disagrees, with the reason retained in the Scene Brief assumptions.
The same applies to a semantically better small-area replacement: keep it when
identity and geometry pass and the scalar change is within noise, while still
reporting that the whole-scene reference gate has not passed.

Every anchor replacement must therefore run a fixed-camera A/B regression
against the accepted baseline. Keep the challenger only when mandatory quality
gates do not regress and the whole-scene reference score does not fall by more
than the configured noise tolerance (default `0.005`). Otherwise roll back the
scene change while retaining the candidate and rejection evidence in the
retrieval audit:

```bash
python3 scripts/evaluate_replacement_regression.py \
  --baseline-quality baseline/evaluate_scene_quality_report.json \
  --challenger-quality challenger/evaluate_scene_quality_report.json \
  --reference-regression-tolerance 0.005 \
  --output challenger/replacement_regression.json
```

Run:

```bash
python3 scripts/scene_tools.py --envelope inspect-reference-match reference.jpg render.png
python3 scripts/scene_tools.py --envelope evaluate-scene-quality reference.jpg render.png \
  --scene-spec scene_spec.json --summary summary.json
```

Synthesis 精确重建时，`create-synthesis28-render-pair` 生成的相机是 source/reconstruction parity 的
诊断视角。源 USD 的 `cameraSettings.Perspective` 可能只是作者最后一次编辑视口，不能据此假定它与参考图
一致。只有同参考图运行 `inspect-reference-match`、检查主体可见性并通过固定相机 A/B gate 后，才能把该
视角提升为 reference camera；屋顶、墙体或大件遮住主体时必须保持 `camera_parity=pending`。

当参考相机未知时，用 `create-synthesis28-camera-candidates` 从对象 bounds 与非场景根 anchor 生成确定性
方位、距离和视点高度网格；随后让 `render_scene_camera.py --manifest` 在一个 Isaac 会话中批量渲染，
再用 `rank-synthesis28-camera-renders` 排序。该排序只用于缩小候选范围：最终选择仍须确认主体可见、没有
穿墙/屋顶遮挡，而且要在全新的 Isaac 会话中分别单独重渲最佳 source 与 reconstruction 相机。批量切换
相机可能保留自动曝光或 temporal rendering 状态，因此粗筛分数不得直接作为最终相机证据；只有独立复渲
仍通过参考与 source/reconstruction parity，才能提升为 reference camera。

`inspect-render-feedback` only checks render usability. It must never be treated as proof that the scene matches a reference.

## Quality gates

`reference_match` is accepted only when:

- geometry, support, facing and lighting gates pass;
- every visible anchor uses a verified real asset;
- no unresolved `ASSET_GAP` remains;
- deterministic reference-match score is at least 0.72;
- optional semantic visual score passes when its backend is available;
- a final visual review finds no dominant identity, orientation, occlusion or camera mismatch.

Use `evals/synthesis27_cases.json` and `scripts/evaluate_scene_benchmark.py` for regression. The catalog is portable; provide `--dataset-root` or `SYNTHESIS27_ROOT`.
Convert the resulting failures into a reusable asset/acquisition queue with
`scripts/build_synthesis27_repair_queue.py`; this maps generated proxy or
authored gaps to canonical classes and metadata-only exact Synthesis model IDs.

For a representative regression subset, repeat `--slug` instead of copying or
editing the hidden contract. The regression report keeps five diagnostic layers:
layout (geometry/overlap/support/facing), asset coverage, material/palette,
camera/composition and whole-image visual score. These layers diagnose repairs;
they do not replace the hard quality gates.

```bash
python3 scripts/evaluate_synthesis27_regression.py \
  --contract hidden_contract.json --dataset-root /path/to/run \
  --artifact-set candidate --mode reference_guided \
  --slug office --slug subway_station001 --slug factory001 \
  --output-json regression.json --output-markdown regression.md

python3 scripts/build_synthesis27_repair_queue.py \
  --catalog evals/synthesis27_cases.json --dataset-root /path/to/run \
  --quality-report regression.json --object-catalog /path/to/objects.sqlite \
  --slug office --slug subway_station001 --slug factory001 \
  --output-json repair_queue.json --output-markdown repair_queue.md
```
