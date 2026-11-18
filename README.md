# Isaac Scene for PhyAgentOS

[简体中文](README_zh.md)

An external Agent plans a scene, selects approved assets and reviews real renders;
this provider executes deterministic composition, editing and support placement.
It extends PhyAgentOS through a Forge ToolEndpoint without modifying Core.
Apache-2.0 applies to project-owned code; see [NOTICE](NOTICE) for dependency and asset boundaries.

## Scope and structure

One production provider Node (`adapter/node.py` → `isaac_scene_provider`), one
Endpoint (`isaac.scene`), eight operations (3 Query, 5 Action), no Session.
The separately built upstream Gateway Node owns HTTP; the provider does not.

| Gateway Tool | Operation | Semantics |
| --- | --- | --- |
| `isaac.asset.search` | `asset_search` | Query |
| `isaac.scene.inspect` | `scene_inspect` | Query |
| `isaac.artifact.read` | `artifact_read` | Query |
| `isaac.scene.build` | `scene_build` | Action |
| `isaac.scene.edit` | `scene_edit` | Action |
| `isaac.scene.solve` | `scene_solve` | Action |
| `isaac.scene.validate` | `scene_validate` | Action |
| `isaac.scene.render` | `scene_render` | Action |

```text
adapter/       contracts, catalogs, service, worker, Dora binding, tests and builder
core/          unchanged clean scene engine, its resources and regression tests
scripts/       repository checks/setup/export and a self-contained smoke demo
tests/         repository packaging tests
docs/          PR draft and validation record
SKILL.md       external Agent workflow and execution boundaries
```

`core/` contains 196 source/resource files retained intact to preserve internal
imports, CLI resources and regression tests. Its per-file hashes are in
`core.lock.json`; the historical ZIP hash in `upstream.lock.json` is provenance,
not a dependency that users must find. This is a source integration, not a wheel.
`adapter/contracts.py` is the single ToolSpec/schema source; profiles/manifests
are generated, not maintained as duplicate API definitions.

## Reproduce source tests

Target: Linux x86_64, Python 3.12.16. Use a dedicated environment, not Isaac's
environment. Run these commands from the repository root. A downloaded source
ZIP has no `.git`; run `git init` after extraction before using the audit tools.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --require-hashes -r requirements.lock
python scripts/repository.py fetch gateway paos
python scripts/repository.py audit
python scripts/repository.py test
```

`fetch` downloads the GitHub source archive for each full pinned commit, checks
both its SHA256 and embedded commit, then verifies every extracted file. Use
`--method git` for a Git checkout instead. Modified/mismatched existing trees
are refused; no reset or overwrite occurs. Only fetch and dependency installation require network access. The
Gateway checkout provides `forge_tool` through `.deps/gateway/src`; do not install
an unrelated latest Forge SDK. The lock records Dora 0.4.1 / Forge Msgs 1.3.0;
Forge 2.x compatibility is **not** asserted. The lock pins all 53 package versions
and the SHA256 of their CPython 3.12 / Linux x86_64 compatible wheels. Availability
still depends on the package index; it is not an included wheel mirror. For an
operator-owned offline wheel cache, add `--no-index --find-links /path/to/wheels`
to installation. Build provenance also records installed versions.

Tests requiring USD or optional geometry engines may skip outside Isaac. Counts,
skips and execution limits must be reported separately; see [validation](docs/VALIDATION.md).
The GitHub workflow runs source tests only, never a GPU/Isaac certification.
Use `python scripts/repository.py test --report .local/checks-01` to save private
per-suite logs and a machine-readable count/skip summary.

## Reproduce the real worker/core path without private assets

Supply a separately installed Isaac Sim Python with `pxr` (historical runtime:
Isaac Sim 6.0.1). The demo generates its own three mesh assets and private
deployment file; it downloads no asset library and uses no proprietary models.

```bash
python scripts/smoke.py --isaac-python /path/to/isaac/bin/python \
  --output .local/smoke-01
```

This invokes the real provider service and worker subprocess. It checks retrieval,
support order, mesh coverage, placed-object obstacles, descendant selection,
frozen objects, immutable revisions and persistent quarantine for an oversized
object. It does **not** test Gateway transport or AgentTask. Add `--render` for an
actual GPU-rendered preview; use a new output directory for each run. Outputs and
logs remain private. A geometric test is not visual realism or physical stability.

To use real catalogs, copy `deployment.example.json` outside tracked source and
replace all paths/policies. `core_root` must point at this checkout's `core/`.
Keep approval records operator-owned. Do not bundle third-party asset libraries.

## Run the source Node through Dora/Gateway

Prepare a private deployment as above (or run the demo with `--prepare-only`).
Activate the provider environment, then generate a fresh profile:

```bash
export PYTHONPATH="$PWD/.deps/gateway/src"
python adapter/node.py --write-profile .local/profile-01 \
  --config "$PWD/.local/smoke-01/deployment.json" \
  --python "$PWD/.venv/bin/python" \
  --gateway "$PWD/.deps/gateway/src/forge_gateway/__main__.py"
export ZENOH_CONFIG="$PWD/.local/profile-01/zenoh.json"
```

Use a compatible local Dora coordinator/daemon. Bind both coordinator interfaces
to loopback (`--interface 127.0.0.1 --control-interface 127.0.0.1`); check
`dora coordinator --help` and `dora daemon --help` for the pinned CLI. Do not invoke
default `dora up` or interrupt another user's runtime. From the generated profile
directory, run `dora run dataflow.yaml`. Its default Gateway is `127.0.0.1:19042`.
The native bundle profile instead uses port 19043. See [SKILL.md](SKILL.md) for
Action polling/cancellation, managed AgentTask activation and shutdown semantics.

Execution slots: 2; admission queue: at most 8; render GPU lease: serialized.
Both Tool input queues are 128. Units: metres, Z-up, yaw degrees. Actions have
status/result/cancel; interruption becomes `unknown`, not automatic replay.

## Build, commit and release

`VERSION` is a development version. No new Node binaries are published here.
Before a native release: choose an unused release
version, run tests, review the staged diff, commit, then tag `v<VERSION>`.
The builder requires clean committed/tagged source, both pinned upstream
checkouts and the exact build dependency lock. It rejects development versions.

```bash
python adapter/build_release.py --gateway-source .deps/gateway \
  --core-source .deps/paos --output .local/build-01 \
  --repository-url https://github.com/LL3RD/isaac-scene-paos --tag v0.4.1
```

The source repository is separate from platform resource registration. The command
will not work until VERSION/commit/tag requirements are met. Build on Linux x86_64
using standalone Python, not Conda/Isaac's Python. It produces provider and
Gateway archives, a manifest-v2 Skill, source ZIP, dependency notices and
`release.json` with source commit/tag, upstream pins, file hashes and artifact
sizes/SHA256. Source ZIP includes the core; Native workers still use the external
Isaac interpreter and configured checkout. Generated build logs are private.
Scan native archives before redistribution. Native builds are traceable, **not
claimed bit-for-bit reproducible**; timestamps/toolchain may change binary bytes.

For a deterministic source-only handoff, no commit is required:

```bash
python scripts/repository.py export --output dist/isaac-scene-paos-source.zip
```

Inside this standalone repository only, review and stage explicit source paths:

```bash
git add .gitignore LICENSE NOTICE VERSION README.md README_zh.md SKILL.md \
  requirements.lock upstream.lock.json core.lock.json deployment.example.json \
  adapter core scripts tests docs .github
git -c core.whitespace=-blank-at-eof diff --cached --check
git diff --cached --stat
git commit -m "feat: add reproducible Isaac scene ToolEndpoint and Skill"
```

Commit identity, remote URL, push, PR creation and tags remain the maintainer's
choice. [PR draft](docs/PR.md) is ready to adapt to the agreed owning repository.
The per-command whitespace option preserves two historical extra EOF blank lines
in the unchanged core snapshot; it does not change Git configuration.
Do not push raw handoff folders or stage files from a larger workspace root.
Use a fresh `work_root` on upgrade from 0.3.x; no ledger migration is provided.

## Limits and upstream process

Trusted single-user deployment, not OS sandboxing. Root allowlists cover asset
entrypoints, not transitive USD/MDL execution. Cleanup tracks cooperative
descendants, not hostile processes. Secrets, installation paths, catalogs,
jobs/logs, screenshots and SDK telemetry configuration stay operator-side.
Retrieval is lexical plus structured filters, not embedding-based. Support solve
covers floor/horizontal surfaces, not wall/ceiling mounts. All references are
static review assets; no IK, robot control, PhysX stability or RL readiness claim.

The upstream [development guide](https://github.com/PhyAgentOS/PhyAgentOS-core/blob/main/docs/user_development_guide/track_b/SKILL_DEVELOPMENT.md)
and [contribution guide](https://github.com/PhyAgentOS/PhyAgentOS-core/blob/main/CONTRIBUTING.md)
separate GitHub source review, local installation and resource publication.
Registration instructions supplied on 2026-11-18 describe authorized developer
uploads followed by technical checks and local review. Confirm the target service
and account permissions with its maintainers; older guides describe manual
registration. This repository still builds local-install candidates, not registered
resources. Platform publication requires published Node dependencies, registry
locks and successful-run evidence tied to the exact Skill bundle. Source review
and a merged PR do not grant publication approval.
