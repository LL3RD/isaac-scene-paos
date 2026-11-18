# Handoff Contract

Use handoff state to continue work without replaying conversation history.

## Export

Provide a JSON state containing:

- `goal`
- `domain`
- `phase`
- `delivery_scope` and `task_path` for multi-input tasks; scope falls back to the task's declared scope, never to simulation by default
- `scene_path`, `scene_spec_path`, `validation_path` when available
- `selected_assets`
- `unit_audit` copied from the final `make-scene` result
- `completed_steps`
- `open_issues`
- `next_recommended_actions`

Run `export-handoff state.json output_dir` with a new directory. It rebases USD root
asset paths using USD APIs, hashes composed external layers/textures, copies small
artifacts and writes a credential-masked **private** profile snapshot. Filename collisions/existing output
are errors, not permission to overwrite. USD export requires the USD Python runtime.
Export pass means packaging succeeded, not visual acceptance or simulation readiness.

The directory is internal-only (mode 0700); `handoff.json`, `asset_lock.json` and
`profile.private.json` retain deployment paths for reproduction. They are not
anonymized. A separate `public-summary.json` contains only fixed status values,
counts, known artifact roles, sizes and hashes: no free-form goal, paths, asset
IDs or profile. Share that summary separately when only progress is needed.
It cannot replace the internal manifest or serve as visual/physical acceptance.
Portable scene sharing still requires reviewed dependency relocation; this
release does not rewrite every transitive USD or texture reference into a bundle.

## Resume

1. Run `verify-handoff output_dir/handoff.json`.
2. Run `doctor` on the receiving environment.
3. Resolve logical asset IDs against the receiving profile.
4. Continue from `phase` and `next_recommended_actions`.
5. Preserve `open_issues`; do not silently mark them complete.

The default handoff is thin: it does not copy large official or shared libraries.
Referenced layers/textures remain external and are recorded by ID, path and hash.
`verify-handoff` detects changed/missing dependencies. Relocation still needs path
mapping and a rerun of checks on the receiving machine; it is not an offline asset bundle.
