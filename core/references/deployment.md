# Deployment and distribution boundary

This is a portable local-tool release candidate, not a PhyAgentOS service yet.
It contains deterministic code, domain packs, schemas and tests, but no asset
library, credentials, host profile, Viewer service or authenticated downloader.
Do not install over a working skill until its deployment and regression pass.

## Private configuration

The default config is deliberately unconfigured. Discovery and text planning
work without assets; `doctor` reports pending, not simulation readiness.
Copy `config/deployment.example.json` **outside the skill**, configure the
installed Isaac Python, task output root and asset libraries, then select it:

```bash
python3 scripts/scene_tools.py --config /path/to/private/deployment.json --envelope doctor
python3 scripts/scene_tools.py --config /path/to/private/deployment.json --envelope list-libraries
```

`ISAAC_SCENE_SKILL_CONFIG` selects the same external file. `${VAR}` placeholders
are expanded recursively; missing variables are errors. Use absolute paths for
runtime, libraries and indexes. Config is administrator-owned, not model input.
Only configure libraries actually installed; missing optional domain libraries
are warnings, required missing assets fail. Configuration never enables SSH or
network probing. Keep credentials in an external secret store, not this JSON.

Asset library names are logical: `industrial`, `grscenes_scenes`,
`asset_catalog`, `isaac_official`, `geniesim`, `fabrica_task_bundles`,
`curated_common`. The official index scanner is `unique_asset_index`.
Legacy specs with deployment-specific library names need an explicit mapping;
do not rewrite source USD paths blindly. Wrapper assets referencing private
installations are not bundled; register them privately if needed.

## Runtime and output ownership

Use the configured Isaac Python for USD, PhysX and rendering. The tested private
runtime was Isaac Sim 6.0.1 / Python 3.12; Blender roundtrip used Blender 4.2.
No bundled environment or dependency-upgrade script is supplied. Optional solid
contact checks need trimesh, rtree and manifold3d in the same runtime. Missing
backends remain pending. `ISAAC_TEST_ROBOT_ROOT` opts into installed robot tests.

For normal CLI writes, `runtime.output_root` constrains explicit output/report
paths. Existing outputs are rejected; select a new revision. `make-scene` also
checks its spec-derived USD and sidecar. The placement solver can refresh only
its own current-invocation outputs. Keep each task root private and separate.

These are accidental-overwrite guards, **not an OS sandbox**. Direct Python
helpers, catalog maintenance and some batch-derived outputs remain trusted
local tools. CLI JSON, diagnostic stdout/stderr, USD references and internal
handoffs can contain deployment paths. Do not expose this CLI wholesale as a
network API. A later provider must enforce input/dependency allowlists, strict
schemas, authenticated artifact access, process ownership and GPU serialization.

## Share only reviewed artifacts

`export-handoff` creates an internal reproduction bundle plus an allowlist-only
`public-summary.json`. Only the latter excludes free text, paths and deployment
configuration by construction; it cannot resume a scene. See `handoff_contract.md`.

Build a source ZIP with `scripts/package_release.py --output /path/to/new.zip`.
The committed `config/release_files.txt` is an exact file allowlist. The packager
rejects unlisted source files, symlinks, known machine-path patterns and common
credential patterns, excludes runtime caches, and includes file hashes. It does
not certify unknown secrets, asset licensing or Git history. Never ZIP the parent
handoff/private directory. Registering nodes and publishing to PhyAgentOS belong
to the next phase and require that platform's own manifest and version locking.
