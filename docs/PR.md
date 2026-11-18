# PR draft

Suggested title: `feat: add reproducible Isaac scene ToolEndpoint and Skill`

Target: `LL3RD/isaac-scene-paos`, **not an automatic addition of this whole tree
to PhyAgentOS-core**. This is a reusable PR draft; no opened PR or platform
publication approval is implied.

## Summary

Add an Isaac Sim scene provider using the existing Forge ToolEndpoint contract.
External Agents own planning, asset selection and visual feedback; the provider
owns deterministic execution. No Core or Gateway source modification is required.

- Eight operations across three Query and five Action tools; one provider Node
  and one Endpoint. The upstream Gateway remains a separate HTTP-owning Node.
- Read-only controlled Synthesis/semantic catalog retrieval and operator registry.
- Complete immutable layout/spec revisions; dependency-ordered support solving,
  visible-mesh checks, automatic obstacles, descendant closure and persistent gaps.
- Durable status/result/cancel, bounded admission, GPU serialization and scoped
  cooperative process cleanup.
- Include the unchanged clean scene core, per-file source locks, pinned upstream
  commits, dependency versions, bilingual documentation and Apache-2.0 licensing.
- Add a self-contained synthetic-mesh worker smoke test and deterministic source
  export. Release builds require clean tagged source and record artifact digests.

## Review map

Start with `adapter/contracts.py` (public API), then `catalog.py`, `service.py`,
`worker.py`, `lifecycle.py` and `node.py`. `adapter/build_release.py` handles native
packaging; `scripts/repository.py` handles source/setup checks.
The 196 files under `core/` are an unchanged snapshot, not 196 newly rewritten
components. `core.lock.json` makes that boundary independently checkable.

## Validation

Commands and actual results are in [VALIDATION.md](VALIDATION.md). Keep this
section synchronized when updating the PR; do not substitute historical 0.4.0
evidence for a rerun of a changed transport, core or build.

```bash
python scripts/repository.py audit
python scripts/repository.py test
python scripts/smoke.py --isaac-python /path/to/isaac/bin/python --output .local/review-01
```

The smoke uses the real service/worker/core but not Gateway transport or a managed
AgentTask. GPU rendering, native release installation and an independent visual
review must be reported separately. The CI workflow has no Isaac license or GPU.

## Compatibility and limits

- Linux x86_64, Python 3.12.16; Core/Gateway commits in `upstream.lock.json`.
- Dora 0.4.1 and Forge Msgs 1.3.0; not a Forge 2.x migration or latest-main claim.
- Metres, Z-up, yaw degrees. Static review scenes and floor/surface support only.
- No robot control, dynamic stability, IK, complete collision/material acceptance
  or training-readiness certification.
- Trusted local single-user execution, not sandboxing or telemetry suppression.
- Fresh work directory required on upgrade from 0.3.x; no ledger migration.
- No private catalogs, source scenes, credentials, machine profiles or raw logs.

## Maintainer decisions before publication

- Confirm the target branch and initial release version in `LL3RD/isaac-scene-paos`.
- Confirm source ownership and third-party redistribution notices.
- Create the reviewed source commit/tag, build and verify immutable artifacts.
- Repeat native installation and Gateway/AgentTask acceptance for that release.
- Keep resource registration/publication separate from source merge. Registration
  guidance supplied on 2026-11-18 describes authorized uploads and review; confirm
  service/account access with maintainers. Published Node dependencies, registry
  locks and exact-bundle successful-run evidence remain publication prerequisites.

## 中文摘要

将已有 Isaac 场景能力整理为独立的 Node＋Skill 接入，包含受控资产检索、完整 spec 修订、
承托依赖求解及取消清理机制。保留原有核心，补齐依赖锁、无私有资产样例、可重复源码打包
和验证记录。Agent 继续负责布局决策与视觉反馈；本次不改 Core，不上传资产，不声称动态
仿真或装配任务已经验证，也不把源码合并等同于平台发布。
