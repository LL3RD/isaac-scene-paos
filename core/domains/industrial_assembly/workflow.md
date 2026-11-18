# Industrial Assembly Workflow

1. Resolve the actual process, background, support, workpieces, tools, staging and testing as separate roles. Add robots only when requested or justified; industrial assembly is not synonymous with Fabrica.
2. Inspect units for the background, table, manipulators and whole Fabrica bundle; keep the generated stage in meters and never assume references convert layer units.
3. For a Fabrica task, retrieve a whole bundle by assembly name; do not mix package roots. For other products, search each required part by identity and dimensions, not a single full-scene query. A lexical chassis match is not proof of a server chassis.
4. Place fixtures/workpieces using their actual support and mounting relations. Fabrica optical boards use recipe coordinates; do not inject that layout into unrelated manufacturing tasks.
5. Ground support assets by bbox and mount robots around a shared reachable workspace. Before accepting the workcell origin, run `inspect-scene-overlaps` against the background; factory backgrounds often contain equipment at `[0,0,0]`.
6. Validate unit audit, plausible physical dimensions, dependencies, support contact, part overlap, recipe consistency and camera coverage. Use explicit `inspect-support-contact --pair OBJECT=SUPPORT` pairs for the workbench-to-floor, robot-to-workbench and recipe-to-workbench contacts.
7. Treat `inspect-bounds --floor-z` as a pivot/bottom-height check only. It does not replace background occupancy or designated-support validation.
8. Build a legible process chain (staging → assembly → inspection/test) before incidental clutter. Allocate tool reach, component bins, cable routing and required maintenance clearance around the chosen task, not fixed object counts.
9. Review hero surfaces up close: bare metal, powder paint, rubber and workpiece materials must remain distinct. Detailed original modeling is allowed when in scope and recorded as such; do not substitute a primitive or wrong asset and report it as resolved.

## Background realism: visible process, not object count

For background-only edits, preserve foreground objects, joints, physics, cameras
and existing lights unless the user includes them. Use a stronger revision layer.
Register new equipment, support/mounting relations and occupied space; recheck
clearance and runtime stability. A detailed background is still collision geometry.

Plan three **camera-visible** depth bands, not arbitrary distance buckets:

- Near: transfer fixtures, work-in-progress carriers and material access connect
  to the main task without blocking its tool reach or silhouette.
- Middle: staging → process → inspection/test has identifiable input/output
  ports, controllers, guards and process-dependent occupancy. Repeated empty
  conveyors or unrelated identical machines are not a production flow.
- Far/upper: structural bays, panel joints, openings and circulation continue
  beyond the station; lights, ducts and cable trays have hangers and endpoints.
  Do not spend the detail budget entirely above the selected camera's field of view.

Use manufactured silhouettes: folded panels, extrusion grooves, door reveals,
feet, rollers, hinges, handles and routed cables. Distinguish galvanized/brushed
metal, powder paint, polymer, glass and floor coatings with plausible UV scale,
roughness and restrained wear. Uniform noise and identical roughness do not
establish material identity; avoid dirtying every surface equally.

Compare baseline and candidate with **the same camera, resolution and render
profile**, plus a background diagnostic view and overview. The independent
reviewer separates rendering quality, background process/structure and unchanged
foreground limitations. Record inferred details and actual asset provenance in
the task; keep scene-specific builders, object counts and images out of the core.

Support surfaces must name the actual contact geometry: a conveyor assembly's
photo-eye or guard top is not its roller-bed height. For pallets spanning rollers
or perforations, keep the generic coverage report and separately verify the real
contact lines, support polygon, gaps and overhang. Do not lift the pallet to a
sensor's bounding-box top or lower all coverage thresholds to force acceptance.
The current generic vertex-coverage gate does not certify discrete roller bridging.
