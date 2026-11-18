# Isaac 场景生成与 PhyAgentOS 接入

[English](README.md)

外部 Agent 理解文本/参考图、检索资产、规划布局并查看真实渲染；本项目执行确定性的
场景合成、修订和承托求解。不修改 PhyAgentOS Core，不内置模型 API 或隐藏推理循环。
项目自有代码采用 Apache-2.0；依赖与资产不因此获得重新授权，见 [NOTICE](NOTICE)。

## 代码怎么找

- `adapter/`：Tool 契约、受控检索、任务执行、Isaac worker、Dora 接入、测试和构建器。
- `core/`：原有清洗版核心，共 196 个源码/资源文件，完整保留依赖、说明与回归测试。
- `scripts/`：依赖获取、检查、打包和不依赖私有资产的最小样例。
- `tests/`：仓库打包测试；`docs/`：PR 草稿和本次验证记录。
- `SKILL.md`：Agent 的调用流程和边界。

一个生产 provider Node、一个 `isaac.scene` Endpoint，8 个操作：检索、场景检查、产物读取
为 Query；生成、编辑、求解、验证、渲染为 Action，无 Session。另一个上游 Gateway Node
负责 HTTP。逐项 Tool/operation 映射、并发和生命周期说明见英文 README。

`core.lock.json` 锁定核心每个文件的哈希；旧 ZIP 的哈希只作来源记录，不再需要另找 ZIP。
不为了减少文件数拆掉成熟核心；新的接入代码与旧核心分开维护。

## 先复现测试，再接真实资产

目标环境为 Linux x86_64、Python 3.12.16。在仓库根目录执行；从源码 ZIP 解压时先执行
`git init`，再使用检查工具。不要把依赖安装进 Isaac 环境。

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --require-hashes -r requirements.lock
python scripts/repository.py fetch gateway paos
python scripts/repository.py audit
python scripts/repository.py test
```

`requirements.lock` 固定 53 个依赖的版本与 Linux x86_64 / CPython 3.12 安装包 SHA256，
但仓库不附带离线安装包。获取上游时核对完整 commit，
默认下载固定 commit 的源码归档，同时核对归档 SHA256、内嵌 commit 和解压后的文件哈希；
也可加 `--method git` 使用 Git。遇到已有修改或版本不一致会停止，不自动重置或覆盖。
当前验证路线使用 Dora 0.4.1，不能直接换成最新 Forge 2.x。
缺少 USD/可选几何库时部分核心测试会跳过；不把跳过计作通过。

本仓库生成自己的箱体网格，不需要 Synthesis、NVIDIA Factory 或 Fabrica：

```bash
python scripts/smoke.py --isaac-python /path/to/isaac/bin/python \
  --output .local/smoke-01
```

该样例实际运行 service → worker → core，检查检索、依赖顺序、网格承托、自动障碍物、
承托子树、冻结对象、不覆盖原版本和超大物体的持续隔离。它不是 Gateway/AgentTask 验收。
`--render` 可另行真实渲染，需要 Isaac Sim 与 NVIDIA GPU；每次使用新的输出目录。
基础几何体只用于验证可复现性，不代表丰富工厂的视觉效果。

接入真实资产时，在仓库外填写 `deployment.example.json`，将 `core_root` 指向本仓库 `core/`。
资产审批仍由部署方负责，数据、安装路径、日志和凭证不入库。源代码 Dora/Gateway 启动步骤、
本地端口、环境变量和打包安装边界见英文 README 与 `SKILL.md`。

## 提交与交接

当前 `VERSION` 是开发版本，未创建新二进制发布。源码交接可直接运行：

```bash
python scripts/repository.py export --output dist/isaac-scene-paos-source.zip
```

ZIP 只含源码和哈希清单，不含 `.git`、依赖缓存、私有配置和历史实验。相同源码重复打包
应得到同一 SHA256。正式 Native 构建则必须有实际仓库 URL、干净 commit、对应版本 tag
和锁定依赖；命令见英文 README。不宣称 Native 构建逐字节相同。

源码仓库为 `https://github.com/LL3RD/isaac-scene-paos`。按英文 README 的显式文件列表暂存、检查、commit。
[PR 草稿](docs/PR.md) 已列出范围、测试和限制；[验证记录](docs/VALIDATION.md) 区分历史结果
与当前重跑。不要在大型工作区根目录执行 `git add .`，也不要上传整个历史 handoff。
从 0.3.x 升级需新建 `work_root`，没有旧 ledger 迁移。

## 当前边界

检索是关键词加类别/尺寸筛选；求解覆盖地面和水平承托，不含吊装/壁挂。当前输出为静态
review scene，不证明 IK 可达、PhysX 稳定、机器人控制或训练就绪。Gateway 仅限回环地址
或部署方认证边界；这是可信单用户执行器，不是沙箱，也没有关闭 SDK 遥测的保证。

2026-11-18 提供的登记指南已描述有权限开发者上传、技术校验、本地审核和批准发布流程；
目标服务与账号权限仍需和维护者确认，旧指南描述的是人工登记。GitHub 源码提交、
本地安装和平台发布是三件事。当前构建仍是本地安装候选包；平台版还需已发布 Node、
registry 精确依赖锁及绑定确切 Skill 包的真实运行视频。源码公开不代表资源已登记。
