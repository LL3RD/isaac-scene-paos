# Validation record

Date: 2026-11-15. Source version: `0.4.1-dev`. No own Git commit/tag, new native
release, remote repository, PR or resource-platform submission has been created.
Use the generated source ZIP's `source-manifest.json` to identify exact files.
The six runtime modules (`catalog`, `contracts`, `lifecycle`, `node`, `service`,
`worker`) remain byte-identical to 0.4.0. Changes are repository packaging,
release preflight/provenance, tests, fixtures and documentation.
Git's default whitespace check reports historical extra EOF blank lines in
`core/domains/home_scene/room_templates.md` and
`core/scripts/isaac_scene/camera_visibility.py`. These were preserved to keep the
core snapshot identical. The documented per-command `-blank-at-eof` override
ignores only that whitespace category without changing Git configuration.

## Current repository checks

| Check | Observed result | Boundary |
| --- | --- | --- |
| Core snapshot | 196 files match `core.lock.json` | Unchanged clean core, not a new engine implementation |
| Source audit | Pass | Text inventory, size/symlink checks, targeted private-path/token patterns; not comprehensive security review |
| Repository tests | 4 passed | Repeatable ZIP, extracted ZIP check, source boundary and self-contained fixture |
| Provider tests | 30 passed | Includes original 28 contracts/lifecycle tests and 2 new release-provenance tests |
| Core in provider Python | 300 run: 250 passed, 50 skipped | USD/optional runtime dependencies unavailable there |
| Core in Isaac Python | 300 run: 298 passed, 2 skipped | Optional externally installed robot assets not configured |
| Real service/worker/core smoke | Pass | Generated meshes; no private library, Gateway transport or AgentTask |
| Skill validation | Both entrypoints passed | Frontmatter/structure check, not behavioral certification |
| Fresh dependency installation | Pass; `pip check` clean | 53 locked versions, wheel hashes checked; isolated Python 3.12.16 environment |

The Isaac core run emitted one trimesh numerical warning while computing an
intermediate degenerate-volume quantity. Tests completed without failures; this
record does not hide the warning or interpret it as a PhysX result.

The smoke checks retrieval, initial floating-support failure, dependency order,
mesh coverage, automatic obstacles, immutable source USD, descendant closure,
unchanged frozen objects, and persistent quarantine/failed validation for an
oversized object. Physics, reachability, render, Gateway and AgentTask are
explicitly `not_run` in this source smoke.

Reproduce the recorded layers separately:

```bash
python scripts/repository.py audit
python scripts/repository.py test --report .local/checks-01
python scripts/smoke.py --isaac-python /path/to/isaac/bin/python --output .local/smoke-01
/path/to/isaac/bin/python -m unittest discover -s core/tests -q
```

Raw reports/logs belong under ignored `.local/`, not in the public repository.
The final provider/repository/core suite and worker smoke were rerun in that
fresh environment. Wheels were downloaded from the package index on a separate
host, transferred to a private cache and installed with `--no-index --find-links
.local/wheels --require-hashes -r requirements.lock`; no installed site-packages
were copied. The initial direct server download was stopped because of bandwidth.
One earlier export test correctly aborted when a documentation sync changed
source during hashing; the frozen-source rerun passed. Failed/intermediate logs
are retained privately rather than counted as final acceptance.

The source-export test checks byte-identical repeated ZIPs and verifies an
extracted ZIP after `git init`; it does not claim byte-identical native builds.

## Dependency retrieval observation

Direct Git HTTPS fetch was unavailable in the validation environment. No global
proxy settings were changed. The default archive route successfully verified
the official fixed-commit sources (archive SHA256, embedded full commit and
extracted file hashes). `--method git` remains available but was not network-
validated here. Both routes refuse modified existing source trees.

## Historical baseline, not a new acceptance result

The prior 0.4.0 handoff recorded a real-asset support fixture, native local
installation, all eight Tools, three managed AgentTasks including cancellation
and camera feedback, and normal shutdown. The original 28 provider tests passed.
These are baseline observations. They do not certify the newly organized source
or a future 0.4.1 native build by inheritance.

Before publishing a tagged release, rebuild from that actual commit, repeat the
native install/Gateway/AgentTask tests and scan the native archives. The new
builder's source identity checks have unit coverage; its complete PyInstaller
release path has not been rerun for this uncommitted development version.
GitHub-hosted CI has been authored but has not run because nothing was pushed.
