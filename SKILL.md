---
name: isaac-scene-paos
description: Generate, revise, inspect and render review scenes through the PhyAgentOS Forge ToolEndpoint bridge to isaac-scene-skill-lite. Use when a configured local Isaac scene provider is available; retain external Agent planning and visual feedback.
---

# Isaac scene provider

This Skill uses a manifest-v2 bundle and Linux x86_64 Node artifacts.
Read the installed `skill.yaml` for the version, resolver and exact Node locks.
Source availability or installation alone does not prove platform publication.
Use only a trusted single-user local deployment. The Gateway must remain on
loopback or behind the operator's authenticated access boundary.

## Agent workflow

1. Interpret the user's text/image; distinguish observations from inferred sizes.
2. Discover `isaac.asset.search`. It searches approved independent objects in
   read-only catalogs plus the operator registry. Filter by `source`, canonical
   class, subtype and metre size. Use English semantic keywords (the Agent may
   translate user text); ranking is lexical plus structured filters, not an
   embedding model. Retain logical IDs and provenance. No catalog promotion,
   download or source-scene pose lookup occurs. Empty results are asset gaps.
3. Read the ToolSpec before invoking. Construct a complete `layout` with logical
   asset/object IDs, support graph, metre positions, Z up, degree yaw and camera.
   Positions refer to asset pivots. For automatic placement, XY gives a coarse
   preferred region and Z is only an initial value, not a support measurement.
   Select assets with `placement_supported=true`: floor/surface support only.
   Ceiling/wall/free/unknown assets require a future dedicated mount contract.
4. Call `isaac.scene.build`; poll the returned invocation's status and result.
   HTTP 202 is admission, not completion. Action `timeout_ms` must cover the whole
   job (e.g. 600000 for rendering), not just the HTTP handshake.
5. Call `isaac.scene.solve` on the built revision. The existing core orders the
   support graph, solves visible-mesh bottom contact and footprint capacity,
   preserves Agent-chosen yaw by default, and treats already placed objects as obstacles.
   To allow quarter-turn alternatives, set each entity's `yaw_deltas_deg` from
   `[0, 90, -90, 180]`. Only permit rotations consistent with its observed front.
   It creates a new full spec and scene; source files never change. Optional
   `entity_ids` expands through support descendants; all other objects freeze.
   Check `placement.status` and `pending_ids`: action completion is not geometry
   acceptance. Failed objects are quarantined, including downstream failures;
   pending gaps persist on re-solve. Recover them with a complete revised layout,
   not hidden deltas or silently relaxed coverage. Asset approval is rechecked
   at admission and in the worker. Call `isaac.scene.validate` on the solved ID.
   Executor success means the check ran; inspect the
   separate dependencies/support verdict. Physics and IK remain `not_run`.
6. Render with `isaac.scene.render` (`preview` or `review`). Fetch image chunks
   through `isaac.artifact.read`, decode locally, verify the returned SHA256, and
   inspect the actual image alongside the original request.
7. For an edit, call `isaac.scene.edit` with the old scene ID and a **complete new
   layout**. Keep the source intact; never accumulate hidden deltas. Revalidate
   and rerender. The endpoint edits only revisions it created, not arbitrary USD.

Inspect existing revisions using `isaac.scene.inspect`. Rendering/validation
artifacts have opaque IDs. Source USD/spec and detailed reports remain private
because they contain dependency paths. This bridge does not export portable
asset archives or start a Viewer.

## Execution boundaries

- No shell, Python code, arbitrary path, download URL or secret is accepted by tools.
- The host operator supplies the USD runtime and reviewed asset registry. The
  packaged provider carries its locked scene engine; omit `core_root` from its
  deployment configuration. It rejects external engine overrides. Source-only
  development can use the repository's `core/` or an explicit operator override.
  Native USD/MDL dependencies are trusted, not sandboxed.
- Outputs go into fresh job directories. Editing never overwrites a revision.
- Actions support status/result/cancel. Cancel acceptance is not termination;
  wait for a terminal result before reusing resources.
- Workers inherit a unique job marker. Completion/cancellation includes cleanup
  of that job's detached helpers, not just its original process group. If cleanup
  fails, no result is published, held GPU leases remain locked and new actions
  are refused until operator cleanup/restart. Never kill by process name.
- Durable replay protection binds an invocation to its attempt and arguments.
  A new HTTP POST gets a new Gateway invocation ID: this is **not** client-wide
  exactly-once delivery. After a network timeout, reconcile the known invocation
  rather than blindly submitting another build.
- Interrupted jobs become `unknown`, never silently rerun. Owner/GPU leases are
  inherited by workers. The GPU lease coordinates cooperating providers only;
  it cannot control unrelated Isaac/Viewer processes.
- All composed references are static review assets in this phase. No robot
  control, PhysX stability, reachability, collision-free assembly or training
  readiness is certified. `scene_validate` currently checks dependencies and
  declared supports, not a complete overlap/material/physics acceptance gate.

## Operator setup

The Synthesis adapter requires callable/ready/verified rows in the operator-approved use scope
and independent-object/robot scope. General semantic catalogs require an exact
reviewed-ID allowlist; broad callable flags alone are insufficient. Root allowlists
restrict entrypoints, not transitive USD/MDL dependencies. This is not sandboxing.
Per-job process cleanup uses cooperative markers, not
OS containment: a child that strips it is outside this guarantee. Native SDK
telemetry/network behavior remains an operator deployment concern; this change
does not disable telemetry or certify an offline runtime.

Copy `deployment.example.json` to an operator-owned private location, replace its
placeholder runtime/work paths, and configure only reviewed assets/catalogs.
Set `ISAAC_SCENE_DEPLOYMENT` to that JSON. The bundle contains no external USD
libraries, Isaac installation or credentials. Both Node archives must be verified
against the locks in `skill.yaml`: a registry bundle resolves registered Nodes;
a local bundle needs `forge-node install --archive` and `forge-node verify`.
The `local-review` profile uses Gateway `127.0.0.1:19043`.

Before `paos skill start`, set `ZENOH_CONFIG` to the installed profile's
`profiles/local-review/zenoh.json`. Start the pinned Dora coordinator explicitly
with `--interface 127.0.0.1 --control-interface 127.0.0.1`, then its local daemon.
Do not allow default `dora up` to expose a trusted local deployment on the LAN.
Reuse an existing coordinator only with operator-confirmed compatibility.

For a managed Agent task, activate this installed Skill in the current turn,
freeze its live ToolSpecs/runtime identity through `AgentTaskCoordinator`, then
invoke through that coordinator and reconcile terminal results before finalize.
Record the semantic verifier mode: `off` finalizes execution facts only, not
visual realism, physical validity or the user's complete assembly objective.

For source development and publication steps, see the
[source repository](https://github.com/LL3RD/isaac-scene-paos). Those development
files are not required inside an installed Skill. Publication is a separate
operator workflow: do not upload, register or release resources while carrying
out an ordinary scene-generation request. Verify listeners are loopback-only
before invoking tools. Stop the dataflow when testing is finished.
Both Tool message inputs use `queue_size: 128`; do not replace them with Dora
0.4.1's default one-message queue. This is bounded buffering, not a durable
transport guarantee. Reconcile status after any lost/ambiguous control response.
