# Scene Brief：文本/图片到可执行场景

`Scene Brief v1` 是外部 agent harness 与确定性场景执行器之间的统一输入。它保存观察证据、置信度、相机假设、语义资产需求、布局关系和实例位姿；不要让执行器暗中调用另一个 VLM。

## 工作流

1. 用 `init-scene-brief` 从文本、参考图路径、房间类型和初始尺寸创建 brief。
2. harness 读取文本/图片，补齐可见对象、`screen_bbox_norm`、尺寸、相机、置信度与证据。
3. 按 `canonical_class` 从统一语义目录召回，按风格/材质/颜色/形态和参考 crop 重排。
4. 资产确定后写入逻辑 ID 或路径、scale 和经过预览验证的朝向证据：
   有正面的资产写 `front_axis`；轴对称资产写 `orientation_mode=axial`
   与 `primary_axis`，不要虚构正面。
5. 求解或明确写入每个实例的平移/旋转，并设置 `support_target`、
   `facing_target`、`wall_affinity`。编译器会用 `front_axis` 和双方位置
   重算 facing yaw，并在 Scene Spec 中记录 `orientation_solver`。`facing_mode=auto`
   对桌、台、工作台等平面目标使用旋转后足迹的最近点，避免长桌侧边椅子全部朝
   中心扇形内扣；显示器、电视等目标仍使用中心。可用 `target_center` 或
   `nearest_target_footprint` 显式覆盖。
   当 harness 只有文本功能描述、尚无实例位姿时，不必手工伪造 transform：可填写 top-level `zones`
   和 `layout_rules`，或使用命中 domain pack 的默认功能区，然后对 pose-free 对象清单运行
   `derive-semantic-layout-spec`。每个 zone 的 `bounds_norm` 为房间可用 bbox 内的
   `[xmin,ymin,xmax,ymax]`；`distribution` 支持 `center/grid/perimeter`。规则按 semantic term、
   layout role 或 entity id 匹配，桌面小物默认继承承托家具的 zone/preferred XY。
   必须整体求解的对象使用 `functional_groups`：`anchor_match` 选择桌/床/设备锚点，
   `members[].match` 选择椅子或配套家具，`per_anchor` 与 `offsets_m` 定义锚点局部槽位，
   `yaw_offsets_deg` 随锚点旋转。`facing_policy=toward_anchor` 只在成员已有可信 `front_axis` 时转为严格
   facing；缺前向轴时保留 `pending_front_axis` 和模板 yaw。同一功能组按 `members` 声明顺序求解，
   因而应先声明必须先成为障碍物的围栏、桌体或机架，再声明控制台、座椅等外侧成员。
   `offsets_m` 默认表示双方 placement reference（地面对象通常是真实底部接触中心）之间的偏移；需要按
   资产根 pivot 表达机器人基座等关系时写 `offset_reference=asset_origin`，编译器使用 inventory 中
   `asset_local_geometry.bottom_contact_center_m` 自动换算，禁止 Agent 手工补 world delta。连接端口仍使用双方
   asset-root local 坐标，并由同一 placement-reference 证据补偿非中心 pivot。
   对滚筒输送带、格栅或托盘这类允许物体跨越孔隙的真实支撑，可在匹配该类小物的
   `layout_rules` 中显式写 `minimum_mesh_coverage`；桌面显示器等连续支撑仍应保持默认 `0.95`。
6. 运行 `validate-scene-brief`，再运行 `compile-scene-brief`。`realistic` 和
   `reference_match` 默认使用 `architectural-shell=auto`：优先选已验证且足够空的门、窗、灯资产壳，
   命中后移除重复的程序化房间碰撞体和灯；没有命中则保留程序化房间并返回可恢复 warning。
7. 只有 `unresolved_count=0` 才运行 `make-scene`。否则把具体 unresolved observation 返回 harness。
8. 参考图能支持判断时，写 `lighting.mode=reference_guided`、环境光与显式
   灯具；无法判断时省略该段并使用默认 rig。固定相机与曝光后完成几何、朝向、
   灯光和参考图相似度反馈。

```bash
python3 scripts/scene_tools.py --config "$ISAAC_SCENE_SKILL_CONFIG" --envelope \
  init-scene-brief --text "现代会议室" --reference-image reference.jpg \
  --room-type meeting_room --room-size-m 8 9 3.2 \
  --quality-policy reference_match --output scene_brief.json

python3 scripts/scene_tools.py --config "$ISAAC_SCENE_SKILL_CONFIG" --envelope \
  validate-scene-brief scene_brief.json

python3 scripts/scene_tools.py --config "$ISAAC_SCENE_SKILL_CONFIG" --envelope \
  compile-scene-brief scene_brief.json --output scene_spec.json \
  --scene-output scene.usda

python3 scripts/scene_tools.py --config "$ISAAC_SCENE_SKILL_CONFIG" --envelope \
  derive-semantic-layout-spec pose_free_inventory_spec.json scene_brief.json \
  --output-spec semantic_spec.json --report semantic_compile.json
```

`derive-semantic-layout-spec` 的 inventory 只提供资产身份、真实尺寸、房间壳、support/mount 图和可选
front-axis；不得含 `xform_matrix`、source transform、`regression_alignment` 或
`regression_instance_opinions`。它也会丢弃旧的 `translate/rotate/layout_constraints/placement_solver`，从而
保证区域由 Scene Brief/domain semantics 重建。Synthesis 回归可用 Stage 17 已去特权字段的 constraint
spec 作为固定 inventory A/B，但必须明确这仍复用了对象清单与承托图，不是从 prompt 自动完成资产检索。

功能组不是后处理 delta。编译器直接写
`layout_constraints.functional_group={id,anchor_id,member_slot,offset_local_m,yaw_offset_deg}`；batch solver
按锚点依赖连续求解，并在锚点移动后重算成员目标。最终运行
`inspect-functional-groups solved_spec.json`，不由 agent 手工累加位移。

默认要求资产壳具备 `doors`、`windows` 和 `lights`。brief 可用
`room.architectural_shell_requirements.features` 改写要求，命令行也可重复传入
`--required-shell-feature`。调试时用 `--architectural-shell none` 禁用自动选择；只有明确需要双层
壳体对照时才传 `--keep-procedural-shell`。

## 证据与不确定性

- 图片只能提供投影证据，遮挡后的尺寸和深度必须标置信心，不能伪装成精确测量。
- `criticality=anchor` 的对象必须有尺寸、资产绑定和明确位姿。
- `reference_match` 的 anchor 还必须在 `asset_binding.reference_match_evidence`
  中记录 `status=pass` 和持久化的 `preview_index_path`。检索分数、margin、
  候选池覆盖或正面检查失败时必须保留为 `ASSET_GAP`，不能因为它是当前第一名
  就标记 resolved。
- 不得为单个场景降低全局阈值。只有自动失败属于小类库存不足或近似并列、且两名
  独立审查者均查看原始 crop 与候选四视图时，才可使用
  `selection_basis=independent_review_override`；完整必填证据见
  `reference_guided_generation.md`，缺一项即由 validator 拒绝。
- query 不是可执行资产绑定。只有逻辑 ID 或真实路径才算 resolved。
- chair、sofa、bed、desk、display、machine、robot、vehicle 等有正反面的资产
  必须带 `front_axis`。会议桌、圆桌、双面货架等无唯一正面的资产使用
  `orientation_mode` 与 `primary_axis`；编译器会保留这些字段，并且不再因缺少
  虚构 `front_axis` 而误报 unresolved。
- `room.architectural_elements` 只能表示墙、玻璃隔断、框、梁、吊顶轨道等
  room shell。参考图中的电视、显示器、桌椅、柜体、灯具、植物或设备不能改名为
  panel/box 来绕过真实资产和 proxy 门禁。
- `floor`、`wall`、`ceiling` 和四面墙方向是合法结构目标；实体关系目标使用对象 ID。
- 对参考图任务，比较相机必须写 `position_m`、`look_at_m` 和焦距，并在迭代中固定。
- `camera.exposure_ev` 与 `lighting` 分开记录。先修正灯具方向、强度、颜色和
  阴影，再微调曝光；不要仅靠提高曝光掩盖缺灯、背光或材质错误。
- `lighting.environment.intensity` 和灯具 `intensity` 会直接写入 UsdLux，不是 `[0,1]`
  归一化权重。必须用一次实际渲染校准；若希望表达相对强弱，请另存为规划证据，不能把
  `0.35` 之类的归一化值直接当作工业室内 DomeLight 强度。

## 外部 harness 的职责

harness 负责读图、语言理解、规划、检索取舍、求解/重试和终止判断。本 skill 负责 schema 校验、语义资产查询、Scene Spec 编译、USD 合成和确定性检查。这样 Codex 或其他 agent 都能读取同一份 brief，不会把模型选择和 provider 配置耦合进 skill。
