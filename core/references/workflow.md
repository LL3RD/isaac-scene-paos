# Workflow Notes

## 生成

先按 `inputs_and_roundtrip.md` 区分新建与已有 USD 编辑。以下空场景/brief 流程不用于重建用户已完成的背景。

1. 从用户 prompt 提取：背景、主体资产、空间关系、物理、灯光、相机、是否推 viewer。
2. 判断领域：仅明确要求 Fabrica recipe 时读本文件的 Fabrica 约束；其他工业装配读命中 domain；家居/室内生活场景先读 `domains/home_scene/README.md`、`room_templates.md`、`layout_rules.md` 和 `validation_checklist.md`。
3. 用 `search-semantic-assets` 检索类别与候选，再用 `search-assets` 解析已知 ID/路径，不要猜路径；对来自新库的背景、机器人、支撑物和 recipe 运行 `inspect-asset-units`。
4. Fabrica 装配场景用 `get-fabrica-recipe`，优先复用 recipe scene，再添加背景、机器人、工作台、灯光等外部元素。
5. 家居场景按“房间 shell -> 大件家具 -> 功能组合 -> 支撑面小物 -> 墙面/装饰 -> 检查反馈”分阶段写 spec，不要把工业默认工作台/托盘/机器人混进普通家居。
6. 按 `references/units_and_scale.md` 写米制 scene spec，并尽量保存在输出目录旁边，方便之后编辑。
7. 生成后先检查 `make-scene.unit_audit`，再跑 `validate-scene` 和 `inspect-bounds`。含真实背景时必须再跑 `inspect-scene-overlaps`；对关键承载关系运行 `inspect-support-contact --pair OBJECT=SUPPORT`。单位冲突、穿地、悬浮或穿插时先修正 spec，写新版本。
8. 空间检查通过后做真实截图及视觉反馈；Viewer 由独立部署管理，不属于本发行包。
9. Viewer 若启用自动播放，或交付目标包含物理仿真，必须用 `scripts/audit_scene_dynamics.py` 在 Isaac Sim
   中完成至少 240 个实际 PhysX 步，在 app 更新时采样全部启用刚体的峰值 world-pose 漂移。默认
   `0.02m` / `2°` 用于固定姿态 smoke test，不用于要求运动的任务；零刚体或缺日志为 pending。把
   仅用于环境展示的引用显式设为 `physics: "static"`；固定底座但需交互门/抽屉的设备使用
   `physics: "articulated_fixed_base"`，再重新合成和复测。动态回归还必须检查 PhysX 日志，不得用
   USD 可加载、静态截图、AABB overlap pass，或运行时拒绝关节后的表面稳定代替有效物理稳定性。

## Synthesis 精确回归与通用生成边界

- 精确回归可读取源场景的 authored world matrix、support/mount 真值和
  `regression_instance_opinions`，用于建立对象库重建的可达到上限。
- wrapper 保持模型级、可复用；源场景根层只对具体实例写入的子 mesh transform、inactive 状态或 shader
  输入，由 composer 临时映射到该实例 `/Source`，不回写 wrapper 或对象目录。
- 通用文本/参考图生成必须删除 `xform_matrix` 和 `regression_instance_opinions`，只保留对象候选、房间
  特征、ground/support/mount 依赖、zone、facing、尺寸和相机约束，再由批量布局器生成新 spec。
- 内部回归用 `derive-constraint-layout-spec` 把 exact fixture 压缩成粗 zone/yaw 输入。输出还会移除
  `source_world_matrix`、source xform/alignment 和原完整场景 feature references，只保留独立 scene-root
  wrapper 中自带的墙、门、窗、材质和灯具；这仍是 coarse-oracle regression，不等于 prompt-only。
- prompt/image 路径用 `derive-semantic-layout-spec`。输入必须是 pose-free inventory/support graph；功能区来自
  Scene Brief 显式 `zones/layout_rules` 或命中 domain pack 的默认 layout profile。编译器拒绝 exact
  regression 字段并丢弃任何旧 pose/coarse hint，因此可以和 coarse-oracle 做严格 A/B。当前 Synthesis
  试验若复用固定对象清单和 support graph，报告必须把这部分 privileged inventory context 单独列出。
- `functional_groups` 把椅子、操作位或配套家具绑定到锚点局部槽位。batch solver 为成员增加锚点依赖，
  用组包络约束锚点真实 support mesh，再按锚点最新 transform 求成员目标，并让已放成员进入后续障碍物。
  连续生产单元应设置 `atomic=true`，并为关键成员写 `position_tolerance_m`。设备物理接驳使用双方局部
  `connection.anchor_port_m/member_port_m/tolerance_m`，机器人服务范围使用
  `work_envelope.target_offset_m/radius_m`；组检查必须同时通过相对位置、朝向、端口和工作包络。
  当前工作包络是保守的机器人基座到任务点距离门禁，不等同于 IK、关节限位或碰撞可达性验证。
  连接端口或基座关系不得只使用 bbox `size/2`。先运行 `inspect-asset-local-geometry`，把测得的
  `visible_bounds_m` 与 `bottom_contact_center_m` 写入 pose-free inventory 的 `asset_bounds_m` 和
  `asset_local_geometry`。`offset_reference=asset_origin` 用于基座/pivot 关系；默认
  `placement_reference` 用于普通地面和支撑布局。检查输出的 attachment face 仍只是几何候选，不自动证明
  AOI 入口、插槽或机器人任务 frame 的语义身份。
  对连续工艺链，把人员操作方向与物流/工艺方向分开记录：`operator_axis` 只描述人员接近面，
  `flow_axis`/`primary_axis` 描述物料通过方向。已由源场景、多视图或资产文档验证的接口应在
  `connection.anchor_port/member_port` 中记录 `role`、`normal_axis`、`evidence_status` 和
  `evidence_source`，并启用 `require_semantic_evidence=true` 与 `require_opposed_normals=true`。
  即使两个 bbox 点重合，接到封闭外壳、操作面或同向法向的连接也必须失败。
  结束后运行 `inspect-functional-groups solved_spec.json`；`pending_front_axis` 不能替代 `inspect-facing`。
- 复用旧模板的 procedural room shell 时，`derive-semantic-layout-spec` 会以当前 Scene Brief 房间尺寸重建
  floor/wall colliders 与 usable bounds；不要只修改 usable bounds 而保留旧地板尺寸。
- 批量布局失败时默认 quarantine；宁可把对象列为 pending，也不得把未求解对象留在 `[0,0,0]`、地面或
  错误支撑面。共享 wrapper 缺少源实例关节/variant 配置时，应补可检索 asset configuration，而不是恢复
  `regression_instance_opinions` 绕过通用生成边界。
- source/reconstruction 高分只证明重建管线没有丢信息，不等于从无上下文 prompt 或单张参考图恢复成功。

## 检索策略

- 默认发现入口为 `search-semantic-assets`，先类别/功能/尺寸、后预览。以下 `search-assets` 快库顺序
  仅用于已有逻辑 ID 或来源级路径解析，不应替代统一语义库和 Synthesis 对象检索。
- 如果需要完整快库候选列表，加 `--exhaustive`；如果仍然没有，再用 `--allow-slow` 或 `--libraries isaac_official`。
- 按 canonical class、功能、尺寸与入口验证选择资产；来源仅作 provenance，不用某一个工作台或机器人作为所有场景默认。
- 机器人型号/完整形体要求见 `references/robot_index.md`；不得拼叠代理冒充完整机器人。
- 家居专用质量与布局知识放在 `domains/home_scene/`；工业 recipe 只在明确需要该装配件时使用。
- 明确资产缺口后可在任务目录补原创建模，不自动把未验证模型写入共享库。
- `list-libraries --counts` 默认不统计慢库；显式需要时用 `--include-slow-counts`。

## 通用摆放与防穿模

### 生成前必须避免的结构性错误

1. **先布局、后换建筑壳。** 门、墙、柱、窗和内置柜决定可用区。建筑壳必须在
   `derive-semantic-layout-spec` 和 `solve-scene-placement-batch` 之前固定。若 shell ID、路径、scale、
   prim allow-list/visibility 发生变化，所有已有地面、靠墙、壁挂和承托对象以及其 support/functional-group
   子树一律失效并重新求解；不能把换壳当作纯材质 pass。`--preserve-existing-layout` 只用于冻结几何 A/B。
2. **把 support_target 当成 support pass。** 声明关系后必须在最终旋转/缩放下验证真实容量。先比较旋转后
   footprint 与真实水平承托三角形，搜索允许的 0/90/180/270° 方向，再选择能容纳它的支撑物；随后用已放
   同台面物体作障碍物。只搭到台面边缘或完全没有 XY 覆盖都必须 quarantine，不得计入
   `grounded_or_supported_count`。
3. **用整个 wrapper 的 composed bbox 求物体底面。** 资产内部常含 `RectLight`、隐藏低模、collision proxy
   或 guide。底面、footprint、物体高度必须来自 render-visible Mesh，并在报告中留下
   `object_bounds_source=render_visible_mesh_points`；否则辅助节点可能贴住台面，而真正可见的机身仍然悬空。

靠墙功能组以 anchor 的依赖闭包移动。例如电视柜是 floor-supported anchor，电视依赖电视柜；壳体变化后
先重求电视柜与墙的 clearance，再从柜体新 transform 恢复电视相对位置。禁止只移动电视或只移动柜体。
如果墙体是单一空心/聚合 mesh，AABB broad phase 会被跳过；此时必须有局部墙面/可用区 clearance 证据，
不能用 `inspect-scene-overlaps` 的 aggregate skip 作为无穿模证明。

- `translate` 表示 USD prim 的原点，不表示底面、脚底、轮底或桌面接触点。不同资产的原点可能在几何中心、机身中心、基座中心或任意建模位置。
- 不要默认把地面物体、机器人、车辆、桌子、家具的 `translate.z` 写成 `0` 后就认为贴地。必须用 world-space bbox 校验。
- 地面承托用旋转/缩放后的可见 mesh 底面与真实支撑三角形求解；容差不是抬高间隙。不要统一加 5–20 mm clearance 导致悬浮。
- 桌面/托盘/fixture 使用真实有效支撑面和 mesh coverage；输出完整新 spec 或可重放的局部约束版本，不手动累加 delta。
- 旋转和缩放会改变 bbox。先应用最终 `rotate/scale`，再检查 bbox；不要用未旋转资产的高度估算最终接触高度。
- 机器人、四足机器人、轮式机器人、人形机器人也遵循同一规则。很多机器人资产的原点不在脚底/轮底，直接 `z=0` 会穿进地面；这不是某个机器人特例，而是 USD 资产 pivot 的通用问题。
- 已知初始高度建议：`geniesim_g2_omnipicker` 可先用 `translate.z=0.05`，`isaac_unitree_go2` 可先用 `translate.z=0.459`，`geniesim_g1_omnipicker` 可先用 `translate.z=0.22`；这些只是初值，最终必须以 `inspect-bounds` 结果为准。
- 可用命令检查顶层 reference prim 的包围盒：

```bash
python3 scripts/scene_tools.py --config "$ISAAC_SCENE_SKILL_CONFIG" inspect-bounds scene.usda --root /World/Objects --floor-z 0 --tolerance 0.005
```

- `inspect-bounds --floor-z` 只比较物体最低点和一个标量高度。它无法发现工厂原点已有设备，也无法证明桌腿接触的是可见地面。
- 有背景的场景必须运行对象—背景占用检查；必要时用 `--object` 把 `/World/FabricaRecipe` 等关键根加入检查：

```bash
python3 scripts/scene_tools.py --config "$ISAAC_SCENE_SKILL_CONFIG" inspect-scene-overlaps scene.usda \
  --object /World/Objects/MainTable \
  --object /World/FabricaRecipe
```

- 对承载关系使用显式 prim 配对。支撑面取 support world bbox 的 `max_z`，并同时要求 XY 投影相交：

```bash
python3 scripts/scene_tools.py --config "$ISAAC_SCENE_SKILL_CONFIG" inspect-support-contact scene.usda \
  --pair /World/Objects/MainTable=/World/Colliders/FactoryFloor \
  --pair /World/Objects/Robots/Arm=/World/Objects/MainTable
```

- AABB 的 XY 相交不能识别桌面孔洞、L 型台面、弧形缺口或分离的承载块。小物放在这类
  非矩形 mesh 上时必须追加 `--mesh-aware`，让物体底部接触点落在真实顶面三角形上；默认
  要求覆盖率至少 `0.95`。若返回 `SUPPORT_MESH_MISS`，应调整 XY/朝向或更换支撑关系，
  不得只因为 bbox 相交就放行：

```bash
python3 scripts/scene_tools.py --config "$ISAAC_SCENE_SKILL_CONFIG" inspect-support-contact scene.usda \
  --pair /World/Objects/Monitor=/World/Objects/Desk/TopMesh --mesh-aware
```

- 上述 `surface` 模式检查“物体在支撑顶面之上”，不能用夹具顶沿判定槽内零件穿透。
  对内部嵌入关系先明确真实 support prim，再用完整实体检查：

```bash
python3 scripts/scene_tools.py --config "$ISAAC_SCENE_SKILL_CONFIG" inspect-support-contact scene.usda \
  --pair /World/Part=/World/Fixture --contact-mode solid
```

  `solid` 对可见、封闭的三角网格、经验证的平面凸四边形网格或 Cube 做布尔相交（包含完全包裹），再以默认
  1 cm 间距采样向下表面接触。`overlap_sources` 定位碰到的 support mesh；资产中的自带
  零件不能当空气忽略。开口网格、细分面、未支持形状、缺少 authored 单位或预算耗尽
  返回 pending，不补洞、不替换凸包、不自动降采样。覆盖率是保守几何条件，不证明重心、
  摩擦、夹紧稳定性或运行时碰撞；接受时仍需声明完整碰撞对象清单。

  表面模式只采用校正 USD orientation 和镜像变换后的朝上三角形，不把盒体底面当作承托面。
  它仍不是实体穿插检查；安装框架、封闭平台及槽内零件须另验实体关系。

- mesh-aware 检查失败时，使用只读求解器在真实顶面三角形上搜索候选位姿。它同时检查物体
  footprint、支撑边距、显式障碍物和可选的 facing target，只返回局部平移增量与 yaw 增量：

```bash
python3 scripts/scene_tools.py --config "$ISAAC_SCENE_SKILL_CONFIG" --envelope solve-support-placement scene.usda \
  --object /World/Objects/Monitor \
  --support /World/Objects/Desk/TopMesh \
  --obstacle /World/Objects/Keyboard \
  --architectural-obstacle-root /World/Objects/RoomShell \
  --facing-target /World/Objects/Chair --front-axis=-Y
```

  `--architectural-obstacle-root` 会遍历真实墙、窗、框、低吊顶等可见 Boundable，但只保留与物体
  对齐到支撑面后的 Z 区间有实质穿透的候选；因此地板接触和高于矮物体的天花不会误阻塞。检查
  solution 中的候选数、选中 prim、path tokens 和 skipped 分类，避免把聚合 bbox 当成整间房障碍。
  `scene_root` 同时含家具时默认以建筑路径 token 过滤，特殊壳命名可重复传
  `--architectural-path-token`。多墙合并的空心 mesh 会记为 `volumetric_aggregate`；背景 overlap
  检查使用同一过滤。若独立 floor collider 与壳体装饰地板存在 0–0.25 m 旧数据偏差，浅层 floor
  会记为 `support_contact_layer`，但应在后续 spec 中把 collider 顶面校准到真实地板，而非扩大阈值。

- 单物体诊断时，把 `suggested_local_translation_delta` 和 `recommended_yaw_delta_deg` 写入一个新的
  Scene Spec 版本，再运行 `make-scene`。多物体任务不要由 agent 手工累计 delta：为每个对象设置
  `support_target`，使用批量规划器直接输出新的完整 spec/scene：

```bash
python3 scripts/scene_tools.py --config "$ISAAC_SCENE_SKILL_CONFIG" --envelope solve-scene-placement-batch \
  input_spec.json input_scene.usda \
  --output-spec solved_spec.json --output-scene solved_scene.usda
```

  批量规划器按依赖图执行 `ground_object → support_furniture → supported_prop`，每次重组同一工作场景，
  并把同一承托面上已经确定的对象自动作为障碍物；存在 `scene_root` 时还会默认启用建筑壳障碍层。
  已通过的 baseline 做增量丰富时，可重复传 `--entity-id` 只开放新增实体，其他对象保持冻结；若替换的是
  桌、床等功能组 anchor，则开放 anchor、其支撑子树和相对布局成员的闭包，避免只移动桌子而留下椅子。
  新物体因真实 mesh 支撑容量不足而失败时，回滚新增物并写 gap，不得挪走或删除 baseline 必需实体。
  复合资产存在把手、立柱等高出工作面的几何时，未指定层高的对象使用检测到的最高真实水平承托面，
  不使用整体 bbox 顶面。滚筒或格栅允许跨缝承托时，覆盖阈值只能在对应对象检查中局部放宽，并记录
  实际 mesh coverage；若求解落到侧轨等语义错误位置，仍应撤回该对象。
  输入 spec/scene 保持只读；输出仍须运行 support、background overlap 和 facing 检查。
  若壳体约束使大件完全无解，不得关闭壳体门禁。先判断资产尺寸是否与门洞/吊顶/房间净高兼容；
  更换资产为首选。内部回归必须保留同一资产时，只能在新 spec 中用可审计的最小单轴适配，并记录
  原尺寸、适配尺寸、比例、目标净空和原因，再完整重算其下游支撑对象。
- 汇总时优先使用 `schemas/validation_manifest_v1.schema.json`。每类报告字段都是非空数组；同一
  支撑物有普通接触和 mesh-aware 两份报告时，把两份都列进 `support_reports`。任一失败则总门禁失败。

- `inspect-scene-overlaps` 是保守的 AABB broad phase。跨度超过 `--max-background-span-m` 的地板/壳体默认跳过，改由 `inspect-support-contact` 验证；对确认无害的复杂背景 prim 可显式加 `--ignore-background-prim`，并在 `layout_notes` 说明。

- 如果输出包含 `below_floor`，新 agent 应回到 JSON spec，按 `suggested_delta_z` 增加对应 reference 的 `translate.z`，重新 `make-scene`，再检查一遍。
- 如果输出包含 `OBJECT_BACKGROUND_OVERLAP`，应保持工位内部相对坐标，整体平移桌子、机器人、recipe 和相机到背景空闲区域；只抬高桌子通常会破坏地面接触且不能解决侧向占用。
- 对桌面物体，`inspect-bounds --floor-z 0` 只能发现穿地，不能自动知道桌面高度；agent 应从已知资产 metadata、bbox 或前一步检查得到支撑面高度，再用对应 `surface_z` 手动校验。
- 吊灯、无影灯和壁挂设备不得复用桌面 `support_target`。使用 `mount_target`、`mount_anchor_prim` 和
  `mount_mode="fixed"`，并验证 authored anchor 与真实墙/顶板 mesh 的距离。只有语义名称或 bbox 靠近
  天花板仍是 pending；必须命中 mount mesh 才能通过。
- XY 平面也要检查 bbox。默认不同独立资产之间至少留 `0.05-0.10m` 安全间隙；机器人通行或操作区域建议留 `0.6-0.8m` 以上通道。

## 空间关系量化

- 用户说“旁边”时，资产应靠近目标但不接触，通常让两个 bbox 的边缘距离约 `0.2-0.8m`，并保持朝向目标。
- 用户说“远处”时，不要只偏移一点点。若没有更明确数字，目标桌/目标区域中心距主工位中心默认取 `5-7m`；如果场景空间不够，至少应大于主工作台最大边长的 `2x`，并在结果中保留通道。
- 多个工作台同时出现时，主工作台、目标工作台和机器人路径要形成清晰布局：主工作台在中心，目标工作台在远端，移动机器人在两者之间或主工作台附近，不能和任一桌子 bbox 重叠。
- 默认镜头需要覆盖用户要求的关键关系。如果要求看到远处目标工作台，camera 应同时覆盖主工作台、机器人和目标工作台，否则应调远或提高相机。

## Fabrica 约束

- 不要把 car 的 fixture 和 stool 的 parts 混用。
- 定位板、pickup fixture、散件、assembled reference、plan_info 应来自同一个 `package_root`。
- 当前官方对齐 bundles 使用 SDF collision，`sdf_margin_m=0.001`。
- 当前官方对齐 bundles 不再内置 `OfficialFabricaTable`；定位板/fixture/零件以 optical board 上表面附近为 recipe 原点。要放到工作台上时，不要只用桌面高度，要用 `桌面高度 + optical_board_thickness_m`，否则 1cm 厚的定位板会嵌入桌面。对 `isaac_long_packing_worktable_clean`，推荐 `fabrica_recipe.translate.z = 1.0041`。
- 如果用户要求 Fabrica 官方双 Franka/Panda 装配布局，使用 bundle metadata 中记录的官方 workcell evidence：`move_arm` 在 `[+0.45, +0.20, surface_z]`，`hold_arm` 在 `[-0.45, +0.20, surface_z]`，两个臂 yaw 都是 `-90°`，共享装配中心约 `[0, -0.15, surface_z]`。不要依赖某台服务器上的 Fabrica 源码绝对路径。
- 这些 bundle 是场景/任务资产，不等于完整 RL 环境；训练逻辑由 IsaacLab/Arena 下游任务补充。

## 泛化原则

- skill 不内置“必须是工厂”的默认逻辑。
- 新 agent 可以生成仓库、实验室、家居、办公室、厨房等任意场景，只要能检索到合适 USD 资产。
- 家居场景有独立领域包，见 `domains/home_scene/`。其规则只指导上层 agent 如何规划和检查，不改变底层 `make-scene` 的确定性执行方式。
- 家居场景生成前先运行 `list-home-backgrounds` 选择背景，并对背景和主家具运行 `inspect-asset-health`。如果背景或主家具材质/引用明显坏，先换资产，不要用 primitive 直接糊上。
- 家居场景如果开启重力或会播放，spec 必须包含 `support_colliders`；先加房间地板 collider，再按需要给桌面/柜面加支撑 collider。
- 家居大房间生成后运行 `inspect-layout-coverage --min-coverage-xy 0.55 --max-coverage-xy 0.90 --min-boundary-margin-m 0.35`。若出现 `underused_large_room`，说明布局过度集中，需要按背景 bbox 重新分区；若出现 `objects_too_close_to_room_boundary` 或 `overfilled_room_bounds`，说明家具贴到了背景 visual bbox 边缘，需要按 usable bbox 向内收缩布局。
- 库内没有正确物体时记为 gap；用户范围允许建模则创建独立、详细、可验证的新模型。简单 primitive
  只用于显式 layout preview，不得计为真实感任务中已完成的家具/设备。
