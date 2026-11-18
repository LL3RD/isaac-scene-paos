# Harness Contract

## Ownership

- External harness: own the model loop, context assembly, planning, permissions, retries, budgets, checkpoints, handoffs and termination.
- Lite skill: own capability discovery, environment truth, deterministic tools, observations and artifacts.

Do not let the skill call an LLM internally. Do not make the harness guess shell commands, asset paths or error meaning when a stable capability can provide them.

## Capability discovery

Read `config/capabilities.json` or run `--envelope capabilities`. Treat `mutability` as the minimum side-effect class:

- `read_only`: inspect only.
- `cache_write`: may refresh derived indexes.
- `artifact_write`: creates new user artifacts.

This release has no remote-process capability. Mutability is descriptive, not
authorization. Local CLI output is internal; a network adapter must validate
inputs, bound resources and return controlled artifact identifiers separately.

## Observation loop

Use `--envelope` and inspect:

- `ok`: whether the command completed successfully.
- `status`: `pass`, `warn`, `pending`, `skip` or `fail`.
- `errors[].code`: stable error class.
- `errors[].retryable`: whether a changed input or external state can recover.
- `result`: task-specific observations.
- `artifacts`: newly created files or remote results.
- `next_recommended_actions`: compact recovery suggestions.
- `result.unit_audit`: source/target `metersPerUnit`, expected conversion, effective scale and any waiver for every USD reference.

On failure, preserve the last good artifact and change only the failed constraint. Avoid regenerating the entire scene unless the observation says the base scene is invalid.

Treat `UNIT_SCALE_MISMATCH` as a retryable planning error. Run `inspect-asset-units`, then remove a duplicated explicit scale or choose `unit_mode=multiply`; do not waive it without recording the physical-size reason.

## Content iteration loop

Richness is functional completeness plus controlled detail, not an object-count target. Generate and review in three reusable phases:

1. `anchors`: architecture and prompt-required functional anchors.
2. `functional`: furniture/equipment groups, access, facing and declared connections.
3. `detail`: supported props and restrained dressing.

After every phase, render at least a perspective and a complementary diagnostic view, then create a content iteration packet. An independent Agent returns semantic observations and repair intents; it never returns transforms. Validate the review and create a scene revision request.

- `advance_phase`: keep the accepted phase and add only the next layer.
- `regenerate_complete_spec`: regenerate a complete pose-free Scene Brief and complete spec, preserving unaffected verified bindings and rebuilding the affected dependency closure. Do not accumulate coordinate deltas.
- `freeze_content`: accepted content and frozen cameras permit final evidence. Camera/material previews may run earlier with explicit open issues; they do not waive content acceptance.
- `stop_for_human_review`: preserve the last good artifact when the bounded iteration budget is exhausted.

The core hard invariants are domain independent: verified identity, physical support/contact, declared directional or functional relations, declared circulation/access, and content acceptance before final. Counts, dimensions and scene-specific relations come from the prompt, asset measurements or the selected domain pack. Missing or uncertain assets remain gaps; do not substitute another category merely to satisfy density.

## Context discipline

Tool adapters must validate action-specific required fields and types before execution;
return a compact structured correction on malformed calls, never silently reinterpret
an argument object as shell/argv. Bind deterministic reports to the inspected USD and
propagate inner `status=fail`, even if a wrapper process exits zero.

Freeze scene dependencies after a verified render. Later edits use a new stronger layer;
old scene, images and reviews must remain reproducible. Enforce the revision budget in
the execution boundary rather than only asking the model to count. Keep provider credentials
outside model-authored programs. Do not turn missing reports into fabricated success JSON.

Use `init-scene-task` / `check-scene-task` from `inputs_and_roundtrip.md` for new
multi-input work. Bind validator reports to the current scene with `--evidence-scene`.
`ok=true` means a command ran, not that an Agent accepted content or physics passed.
The task checker is a bounded evidence gate, not a hidden LLM or universal task solver.

- Load only the selected domain pack.
- Keep chosen asset IDs, concise reasons, validation state and artifact paths.
- Do not retain full search dumps after selection.
- Export a handoff before context replacement or agent transfer.
