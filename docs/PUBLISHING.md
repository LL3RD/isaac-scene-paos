# Platform release workflow

Source commits, local installation and platform publication are separate steps.
This project targets the pinned Forge 1 runtime; `manifest_version: 2` is the
bundle format, **not** Forge 2 support. No command below uploads or approves a
resource. Use the authorized developer UI for submission and local review;
the platform's administrator registration script is not included here.

## 1. Build and review native Nodes

On Linux x86_64 in the locked provider environment, fetch both pinned upstreams.
For development checks (not publishable):

```bash
python adapter/build_release.py --gateway-source .deps/gateway \
  --core-source .deps/paos --output .local/candidate-01 \
  --repository-url https://github.com/LL3RD/isaac-scene-paos --candidate
.local/candidate-01/binaries/isaac_scene_provider/isaac_scene_provider \
  --smoke-test .local/native-smoke-01 --isaac-python /path/to/isaac/bin/python
```

The native provider includes the locked engine and generates its own tiny test
meshes. Its smoke test checks real service/worker execution, **not** Dora/Gateway,
rendering, robot control or physical stability. Run it after relocating the
provider directory to confirm it does not depend on the source checkout.
Packaged deployment JSON must omit `core_root`; external engine overrides are
rejected. Isaac's interpreter, its runtime dependencies and reviewed asset
libraries remain operator-supplied. Do not install provider requirements into Isaac.

For an actual release, review changes, choose an unused release `VERSION`, commit
and tag `v<VERSION>`, then build once with `--tag` instead of `--candidate`.
Development/candidate artifacts are refused by the platform preparation helper.
The build outputs two directory Node archives, a **local-resolver** Skill,
source snapshot/ZIP, notices and `release.json`. Do not rebuild these Nodes after
registration: a rebuild may change their SHA. Do not modify an existing release.

## 2. Prepare and register Nodes

Agree artifact IDs and publication repositories with the platform maintainer.
The provider is an Operator; the separately packaged upstream Gateway is an
Adapter. Source is in this repository (including the build recipe); the pinned
Gateway upstream and license remain recorded in provenance/notices.

```bash
python scripts/platform_release.py nodes --build .local/release-01 \
  --output .local/registration-01 \
  --provider-id APPROVED_PROVIDER_ID --provider-repository operator-isaac-scene \
  --gateway-id APPROVED_GATEWAY_ID --gateway-repository adapter-isaac-scene-gateway
```

Output is two registration JSONs, `node-ids.json` and `provenance.json`, all
**outside** the Node/Skill archives. JSONs are form references or inputs to the
maintainer's registration tool; generating them does not register anything.
Do not publish a duplicate upstream Gateway without maintainer agreement.
An existing registered Node can be reused only when all lock fields and bytes
match this tested build. Substituting a differently built official Gateway needs
a separately validated compatibility change; this helper intentionally rejects it.

Upload each Node via the authorized platform UI, complete review, and wait for
publication. Node archives have `<entrypoint>/<entrypoint>` with an executable
entry plus its runtime libraries. The platform handles artifact storage; there is
no need to manually create a second GitHub Release or change Core source.
The Node source-location extension requires the platform's separate `node-source`
schema, not supplied with the registration guides; retain `provenance.json` and
have the maintainer map it to that schema rather than guessing field names.

## 3. Create the Registry Skill from the same Node bytes

```bash
python scripts/platform_release.py bundle --build .local/release-01 \
  --node-ids .local/registration-01/node-ids.json \
  --registry-url https://REGISTRY_HOST --core-source .deps/paos \
  --output .local/platform-skill-01
```

This is a read-only network operation: GET each public Node record, compare all
seven lock fields, download/hash the exact archives without saving signed URLs,
then generate `resolver: registry` and package with the pinned Core packager.
Missing or inconsistent metadata/downloads stop the command. Flat records and
`metadata`/`artifact` envelopes are supported; the live platform response still
needs acceptance testing. There is no flag to bypass Registry checks.

Output separates `source/`, `artifacts/` and `release-proof.json`. The tar root
contains `skill.yaml`, `SKILL.md`, licenses, a deployment example, profile files
and `archive-manifest.json` (every regular file except itself). It has no wrapper
directory, Node binaries, videos, private settings or registration parameters.
The source ZIP is not a Skill installation bundle.

## 4. Run the exact final bundle and record evidence

Install the final tar on the compatible Linux host, verify identity/locks,
repeat installation for idempotence, start `local-review`, check all eight Tools,
perform an actual build → solve → validate → render task, then stop/restart and
check cleanup. Follow installed SKILL.md for loopback-only Dora/Zenoh setup.
Record real rendered results and task progress, not only terminal startup.
Record the bundle SHA **before** that run; do not reuse a historical video.

```bash
python scripts/platform_release.py evidence \
  --bundle .local/platform-skill-01/artifacts/isaac-scene-paos-VERSION.tar.gz \
  --observed-bundle-sha256 SHA_RECORDED_FOR_THE_RUN \
  --video /path/to/actual-success.mp4 --profile local-review \
  --core-version ACTUAL_CORE_VERSION --environment 'Actual OS, GPU and Isaac runtime' \
  --task-goal 'Build and correct a supported assembly review scene' \
  --success-description 'Describe the observed checks and rendered result' \
  --output .local/evidence-01
```

This prepares `skill.json` plus an evidence record binding bundle/video SHA.
It does not decode the video or judge task success; those remain real-run and
platform-review obligations. Metadata uses `forge_version: 1` and `simulation`;
it does not add a fictitious Forge field to `skill.yaml`. Any bundle change
requires a new run/video. Recheck recorded hashes immediately before upload.

Upload the original bundle, bind its actual video/profile/environment, submit
technical validation, and complete local review. Finally query/download the
published Skill and compare SHA/size. Only then report platform publication.
Keep credentials, local paths, signed URLs, videos and receipts out of Git.
