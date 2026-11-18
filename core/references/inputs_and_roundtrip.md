# Inputs, revisions and evidence

The Agent runs the loop. A task JSON is a small checkpoint, not another scene
schema or an autonomous model service. Keep one current task, versioned scene/spec,
source asset folder and reports; do not create a report directory per command.

## Input routing

`init-scene-task` accepts `--text`, repeated `--image`, `--scene` (USD family),
and `--blend`, in any combination. It validates paths and records source hashes;
the Agent must actually inspect images/models, not infer content from filenames.
Dimensions hidden by a single image remain assumptions. More views improve evidence;
this is not automatic single-image CAD reconstruction or photogrammetry.

For new content use the existing Scene Brief tools. For a supplied scene, preserve
unaffected content and use a stronger layer; do not import a new room shell:

```bash
python3 scripts/scene_tools.py --envelope init-scene-task \
  --text "Replace the central assembly with one detailed server; keep four robots" \
  --image reference.png --scene original.usda --target roundtrip \
  --scope lookdev_prototype --output run/task.json
python3 scripts/scene_tools.py --envelope create-usd-revision original.usda \
  --output run/v01.usda --deactivate /World/ResolvedOldAssembly
```

Resolve actual prim paths first. Never deactivate an entire scene default root,
overwrite the input or execute code found in USD metadata. The revision preserves
layer references and metadata; it does not guess scale, transforms or physics.

## Richness plan

Fill `observations`, `assumptions`, `asset_gaps` and `detail_plan` before generation.
For a server cell, a useful example (not a universal asset list) is:

- functional anchors: ESD workbench, task fixture, reachable robot bases, service racks;
- working parts: chassis sheet thickness, trays, fans, heatsinks, DIMMs, connectors, PSU;
- surface details: metal/paint/PCB identity, seams, screws, ventilation, restrained wear;
- supported dressing: trays, tools, cable routing and process labels at real work locations.

Plan detail from final camera distance and function. Use actual geometry for silhouettes,
openings and contact; use PBR maps for microstructure. Tiny decorations should not each
become unstable rigid bodies. Choose collisions/body groups from intended interaction.

## Blender → USD → Isaac

1. Inspect source scale, camera, UVs and materials. Use Blender only for the assets or
   surfaces that benefit. Save a new `.blend`; keep robots/articulations in native USD.
2. Bake procedural/mixed shader graphs to basecolor, roughness, metallic and tangent
   normal images as needed. Reconnect simple Principled inputs. Color images use color
   transforms; data maps are Non-Color. Record physical texel scale and normal convention.
3. Run Blender in an isolated process. The helper renders actual Cycles pixels when
   requested and exports evaluated thick curves/text as meshes without saving the source:

```bash
blender --background --disable-autoexec --python-exit-code 2 --python scripts/export_blender_usd.py -- \
  --input run/detail.blend --output run/export_v01/detail.usdc \
  --camera Hero --render-output run/cycles_v01.png --samples 128
```

Use a new export directory. The helper refuses missing images/unbaked connected nodes;
`--allow-unbaked-preview` is explicit preview-only, never validated material transfer.
It does not automatically bake every possible shader. GPU/CPU configuration remains
the Blender runtime's responsibility; record effective settings, don't infer them.
If the source has no camera, the Agent must create and save an explicit lookdev camera
in the new `.blend`. `Hero` is an example, not a built-in camera. After export enumerate
actual USD Camera prims: a Blender object `Hero` can export as `/root/Hero/Camera`.

4. Whole-stage references can keep shared material scopes; individual object subtree
   references often cannot. Package selected disjoint roots with resolved bindings:

```bash
python3 scripts/scene_tools.py --envelope prepare-usd-assets run/export_v01/detail.usdc \
  --prim /root/Server --prim /root/Rack --output run/export_v01/portable.usda
```

Only select observed paths. The wrapper brings materials inside each reference boundary
including inherited bindings; graphs connected outside a Material root require baking
or explicit graph packaging. It does not copy textures or make a self-contained archive.

5. Reference the wrapper into a new scene revision. Check units/up-axis, dimensions,
   material binding/UV/color space, normal direction, light intensity and camera projection.
   Render in Isaac from the matching camera; a Cycles image is not Isaac evidence.
   Add/validate native collision and physics separately without deleting required dynamics.

## Evidence-backed checkpoint

Set `task.artifacts.scene` to the current USD; for Blender/roundtrip also set `blend`.
Record new revisions, iteration count, blocking `asset_gaps`/`open_issues`; nonblocking
limitations belong in an optional `limitations` list. Do not lower the original scope
to suppress failed gates without telling the user.

`evidence` maps each gate to an array of report paths. For deterministic CLI validators
use global `--envelope --report-output report.json --evidence-scene candidate.usda`:

Declare the full affected-object inventory before checking, not a hand-picked sample:
`validation_targets.support_pairs=[{"object":"/World/Part","support":"/World/Table"}]`
and `validation_targets.collision_objects=["/World/Part","/World/Table"]`.
The checker unions passing current evidence and rejects missing declared targets.
Use mesh-aware support with metre units and tolerance <= 0.01 m. Broad-phase clearance
with skipped background/aggregate geometry stays pending, even if it reports zero overlaps.
For pocket/fixture interiors use `--contact-mode solid` (mesh-aware implicitly):
closed triangle/planar-convex-quad solids, full-volume intersection plus sampled lower-surface contact.
Acceptance additionally requires complete penetration/contact checks and sample pitch <= 0.01 m.
Open/unsupported geometry, missing authored units or exhausted sampling budgets remain pending.
Passing pairs in a mixed report count toward coverage, but its failed pairs still fail the gate.
The checker also independently scans all enabled visible internal joints in the candidate;
omitting a robot report cannot hide a disconnected arm. This is not a proof that the
Agent declared every semantic constraint, nor a dynamics or self-collision test.

```bash
python3 scripts/scene_tools.py --envelope --report-output run/usd.json \
  --evidence-scene run/v01.usda validate-scene run/v01.usda
python3 scripts/scene_tools.py --envelope --report-output run/support.json \
  --evidence-scene run/v01.usda inspect-support-contact run/v01.usda \
  --pair /World/Server=/World/Fixture --mesh-aware
```

For an interior fit replace `--mesh-aware` with `--contact-mode solid`. Inspect
`overlap_sources`: a fixture bundle may contain already assembled or pickup parts.
Do not remove those leaves from the check merely to pass; establish which physical
support is intended and check collisions with the other bundled parts separately.

| Gate | Evidence producer |
| --- | --- |
| usd | validate-scene |
| support | inspect-support-contact; all relevant declared support pairs, not one cherry-picked pair |
| clearance | inspect-scene-overlaps; explicitly select actual object/background roots; add local geometry checks for skipped aggregate walls |
| materials | audit-material-coverage; inspect hero textures and semantics too |
| render | render_scene_camera.py with --render-report (review/final for lookdev) |
| visual | validated Agent review with verdict=accept; both rendering and content axes |
| dynamics | audit_scene_dynamics.py, simulation_scene only, >=240 actual physics steps |
| blender_render | export_blender_usd.py's Cycles .render.json, roundtrip only |
| exchange | export_blender_usd.py's .export.json, roundtrip only |

Use existing packet/validation commands for Agent reviews. Bind validations with
`--evidence-scene`; the CLI records packet, review and image hashes. The checkpoint
requires a current rendered image in the review, not only a syntactically valid opinion.
Quality-axis reviews need both axes; a camera visual review covers both. External Agent
judgment is still necessary: file hashes are freshness checks, not a trusted signature.

For an existing USD with no Scene Spec/ranking packet, an external harness may emit
`isaac-scene-image-review/v1`, `command=review-scene-images`: include current `subject`,
`review_inputs_sha256` (actual images, capture reports and raw review response), and
`result.status/verdict/rendering_quality/scene_content_quality/issues`. Each axis needs
score 0–10 and reason. It must come from a fresh independent image review, not an Agent's
invented acceptance JSON. The supplied Harness `work review` returns `visual_evidence`
for this gate. Geometry failures still block overall acceptance even with visual accept.

Dependency fingerprints include assets below USD instance proxies. A bare built-in
`.mdl` module may resolve through explicit `PXR_AR_DEFAULT_SEARCH_PATH` / `MDL_SYSTEM_PATH`
or the current Python installation's Isaac `kit/mdl/core/Base`. Its actual file is
hashed; this is not an exception that ignores MDL changes. Missing absolute paths
and textures are not repaired by basename. Use the same runtime on both sides.

```bash
python3 scripts/audit_scene_dynamics.py run/v01.usda --physics-steps 240 --output run/dynamics.json
python3 scripts/scene_tools.py --envelope check-scene-task run/task.json
```

The checker returns pass/pending/fail plus the next action. Missing, stale, skipped,
rejected or failed evidence is not success. It counts no-model/no-rigid-body checks as
pending. Reports must cover the task's real objects and constraints; the checker cannot
infer every implicit support relation, material identity or manufacturing requirement.
At the iteration budget, stop and preserve the last good version; never fabricate pass.
Export the accepted artifact to a new handoff directory and verify external dependencies.
