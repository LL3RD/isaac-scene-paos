# Scene Spec

本 schema 用于新建/批量合成场景，不要求把用户提供的既有 USD 重新建模成 spec。
已有场景局部编辑先用 `inputs_and_roundtrip.md` 的 task + revision layer，保留未改内容；
需要重新求解的子树才进入下述关系/约束流程。

新工作优先使用 `isaac-scene-spec/v2`：用 `entities` 描述场景角色，用 `relations` 描述空间意图，用 `constraints` 和 `validation_targets` 描述验收。外部 harness 负责把未求解关系变成明确 transform；当前确定性 composer 会映射已提供资产和坐标，并把未求解关系返回为 `LAYOUT_RELATIONS_UNRESOLVED`。单房间流程和朝向契约见 `single_room_generation.md`。

```json
{
  "schema_version": "isaac-scene-spec/v2",
  "domain": "warehouse_logistics",
  "entities": [
    {"id": "warehouse", "role": "background", "asset_id": "simple_warehouse", "libraries": ["curated_common"]},
    {"id": "robot", "role": "mobile_robot", "asset_id": "geniesim_g2_omnipicker", "libraries": ["geniesim"], "translate": [0, 0, 0.05]},
    {"id": "pallet", "role": "pallet", "query": "transport pallet", "translate": [2, 0, 0]}
  ],
  "relations": [
    {"type": "near", "subject": "robot", "target": "pallet"}
  ],
  "constraints": {"minimum_corridor_m": 0.8},
  "validation_targets": ["assets_resolve", "support_contact", "no_overlap"]
}
```

下面的旧格式继续支持，便于现有场景复现。

上层 agent 把用户 prompt 转成这个 JSON，再交给 `scripts/scene_tools.py make-scene`。

## 最小结构

```json
{
  "domain": "industrial_assembly",
  "output_path": "runs/example/scene.usda",
  "stage": {
    "up_axis": "Z",
    "meters_per_unit": 1.0,
    "unit_policy": "normalize_references"
  },
  "background": {
    "path": "/absolute/path/to/background.usd",
    "prim": "/World/Background"
  },
  "references": [
    {
      "id": "table",
      "asset_id": "isaac_long_packing_worktable_clean",
      "libraries": ["curated_common"],
      "prim": "/World/Objects/Table",
      "translate": [0, 0, 0],
      "rotate": [0, 0, 0],
      "front_axis": "+Y",
      "facing_target": "/World/Objects/TV",
      "orientation_confidence": "high",
      "orientation_source": "asset contact sheet",
      "physics": "static"
    },
    {
      "id": "franka_move_arm",
      "asset_id": "isaac_franka_panda",
      "libraries": ["curated_common"],
      "prim": "/World/Objects/Robots/FrankaMoveArm",
      "translate": [0.45, 0.20, 1.0041],
      "rotate": [0, 0, -90],
      "physics": "none"
    },
    {
      "id": "franka_hold_arm",
      "asset_id": "isaac_franka_panda",
      "libraries": ["curated_common"],
      "prim": "/World/Objects/Robots/FrankaHoldArm",
      "translate": [-0.45, 0.20, 1.0041],
      "rotate": [0, 0, -90],
      "physics": "none"
    }
  ],
  "fabrica_recipe": {
    "assembly": "car",
    "prim": "/World/FabricaRecipe",
    "translate": [0, 0, 1.0041],
    "rotate": [0, 0, 0],
    "scale": [1, 1, 1]
  },
  "support_colliders": [
    {
      "id": "room_floor_collider",
      "type": "box",
      "prim": "/World/Colliders/RoomFloor",
      "translate": [0, 0, -0.03],
      "size": [9.8, 9.8, 0.08],
      "visible": false,
      "physics": "static"
    }
  ],
  "lights": [
    {
      "type": "distant",
      "prim": "/World/Lights/Key",
      "intensity": 5000,
      "rotate": [-45, 0, 35]
    }
  ],
  "camera": {
    "prim": "/World/Camera",
    "translate": [4, -5, 3],
    "look_at": [0, 0, 1.2],
    "focal_length": 28
  },
  "physics": {
    "gravity": true
  }
}
```

## 字段约定

- `domain`: 可选元信息，建议写 `industrial_assembly`、`home_scene`、`warehouse_logistics`、`lab_scene` 等。`make-scene` 会忽略未知字段，但会把完整 spec 保存到 sidecar，方便后续编辑和交接。
- `room`: 可选元信息，家居场景建议记录 `type`、`style`、`size_m`、`quality_target`。具体家居规则见 `domains/home_scene/`。
- `background.path`: 作为背景引用到 `/World/Background`。
- `stage.meters_per_unit`: 生成层的物理单位，默认 `1.0`（米）。
- `stage.unit_policy`: 默认 `normalize_references`。USD reference 不自动换算源层单位，因此 composer 会在 item 未写 `scale` 时应用 `source_meters_per_unit / target_meters_per_unit`。`preserve_references` 只用于兼容旧场景。
- `references`: 任意 USD 资产引用列表。`physics` 可选 `none`、`static`、`dynamic`、
  `articulated_fixed_base`。`none` 不新增
  物理 opinion，并保留源资产自带的刚体/关节；`static` 为当前场景中的固定陈设，composer 会在场景层
  关闭引用树内继承的动态刚体和关节、优先保留资产已经 author 的碰撞；只有整棵引用没有任何 collider
  时才补 mesh fallback，避免把高模、非流形或 articulated render mesh 全部重新 cooking；`dynamic` 才允许该实体参与重力和动力学。
  `articulated_fixed_base` 用于被移动到新场景坐标的铰接柜体/设备：composer 把源资产中 FixedJoint 的
  body1 改为 kinematic base、停用这些世界锚定 joint；Revolute/PrismaticJoint 设置
  `excludeFromArticulation=true` 以使用支持 kinematic parent 的独立关节求解，并配置有限力闭合保持
  drive。门、抽屉仍是动态刚体且可受外力交互；其碰撞 mesh 覆盖为 PhysX 支持的 `convexHull`，不会因
  triangle-mesh dynamic collision 在运行时退化。
  因此带物理的 SimReady 家具或设备若只作环境陈设，必须显式写 `static`；需要保留可交互关节的固定设备
  应写 `articulated_fixed_base`，不能为了 Viewer 稳定把整棵资产静态化。
- `front_axis`: 资产未旋转时的局部正面轴，只允许 `+X/-X/+Y/-Y`。有正反面的资产必须在插入前检查源资产并填写，不能根据 bbox 猜。
- `camera.look_at`: 推荐用于参考图固定比较相机；与 `translate` 一起确定视线。未提供时才使用显式 `rotate`。
- `camera.translate` 与 `camera.focal_length` 是规范字段；为兼容旧手写/导入 spec，`make-scene` 也接受
  `position` 与 `focal_length_mm`。新生成器应始终输出规范字段，避免相机参数在下游被误读。
- `facing_target`: 该资产应面向的目标 prim。合成后用 `inspect-facing --pair SUBJECT=TARGET` 验证真实 world transform；默认夹角阈值为 `20°`。
- `support_target`: 物体底部由地面、家具或平台承托时使用。地面对象可写 `floor`；桌面小物必须指向
  实际 support mesh。不要用它表达吊装。
- `mount_target`、`mount_anchor_prim` 与 `mount_mode="fixed"`: 吊灯、无影灯或其他刚性安装件使用。
  `mount_target` 指向墙/顶板的真实 mesh，`mount_anchor_prim` 指向资产自身安装锚点。固定安装关系进入依赖图，
  但不会被桌面接触求解器当作普通 surface prop。
- `orientation_confidence` 与 `orientation_source`: 记录正面轴判断的置信度和证据。低置信度资产不得直接进入最终场景，应先做四向预览或更换资产。
- `render_feedback_policy`: 几何验收通过后的渲染反馈策略。建议写 `views=["perspective","top_down"]`、`max_iterations=2`、`preserve_geometry=true`，具体规则见 `render_feedback.md`。
- `references.asset_id`: 推荐用检索到的 `asset_id`，并加 `libraries` 限定资产库。主工作台优先用 `curated_common:isaac_long_packing_worktable_clean`。
- 轮式人形/移动双臂机器人推荐用 `curated_common:geniesim_g2_omnipicker`，例如 `asset_id="geniesim_g2_omnipicker"`、`libraries=["curated_common"]`、初始 `translate.z=0.05`，然后用 `inspect-bounds` 校验。不要默认把移动底盘和人形资产叠放组合成代理机器人。
- `fabrica_recipe`: 引用一整套官方对齐 Fabrica task bundle 的 `scene/scene.usda`。如果需要换零件，只改 `assembly`。
- `support_colliders`: 简单 box 碰撞支撑体列表，用于给视觉背景、地板、桌面、柜面提供确定性碰撞。支持字段：`id`、`type="box"`、`prim`、`translate`、`size`、`visible`、`physics`。如果家居场景开启重力或会播放，至少添加一个房间地板 collider。
- `fabrica_recipe.translate.z`: 如果放在 `isaac_long_packing_worktable_clean` 上，推荐 `1.0041`。该桌面约 `0.9941m`，Fabrica optical board 厚约 `0.01m`，recipe 原点在 optical board 上表面附近，直接用桌面高度会导致定位板嵌入桌面。
- Fabrica 官方双 Franka 预览：推荐在 `references` 中加入两个 `isaac_franka_panda`，位置分别为 `[+0.45,+0.20,surface_z]` 和 `[-0.45,+0.20,surface_z]`，`rotate=[0,0,-90]`。为了让默认 viewer 自动框选到机械臂，预览场景里建议把 prim 放在 `/World/Objects/Robots/...` 下。
- `lights`: 支持 `distant`、`dome`、`rect`、`sphere`。
- `camera`: 第三人称默认镜头。
- `translate/rotate/scale`: 单位为目标 stage 单位、角度、总比例。注意 `translate` 是 USD prim 原点，不是资产底面或接触点。
- `xform_matrix`: 可选的 4×4 row-major USD transform，仅用于需要保留原始 authored matrix 的精确回归；
  与 `translate/rotate/scale` 二选一，并要求有效引用缩放为 `[1,1,1]`，避免矩阵与单位换算暗中叠加。
- `regression_instance_opinions`: 仅限 Synthesis 内部精确回归的实例视觉覆盖描述。它记录源 root layer、
  源实例 prim、reference/payload 挂接 prim 和 wrapper `/Source` 的映射策略；composer 只复制 typed 的子
  transform、active/visibility/purpose/doubleSided、material binding、primvars 与 shader `inputs:*`，并
  保留 inactive 后代。不得复制场景根引用，不得把挂接 prim 的 transform 再乘一次，也不得用于 prompt/
  reference-image 生成、约束布局或 callable 资产元数据。
- `layout_constraints`: 无精确位姿布局的确定性求解提示。可包含 coarse `zone`、`preferred_xy`、
  `yaw_bucket_deg`、`yaw_deltas_deg`、`obstacle_clearance_m`、`support_margin_m`、
  `minimum_mesh_coverage` 与 `mount_surface`；这些是
  约束或粗分区，不是 source authored transform。批量求解后每个成功 entity 会记录
  `placement_solver`，对应 relation 写入新的 `resolved_transform` 和
  `resolution_source="solve_scene_placement_batch"`。
- `semantic_layout_provenance`: `derive-semantic-layout-spec` 写入的 prompt/image 布局来源。其
  `mode=scene_brief_functional_zones_without_pose`、`pose_source=none`；输入中的旧 transform 与 coarse hint
  被忽略，输出 zone/preferred XY 仅来自 Scene Brief/domain pack。该层仍可复用已解析的资产清单和
  support/mount 图，不能据此宣称已从文本完成资产召回。
- `layout_pending_entities`: 没有验证 support/mount 关系或自动求解失败的对象清单。它们保留 identity、
  asset path 和失败证据，但不会被 composer 放在默认原点。补齐关系后必须派生新的 spec 版本再求解。
- `unit_mode`: `auto`（默认）把显式 `scale` 视为总比例；`multiply` 先做单位换算再乘显式艺术缩放；`preserve` 跳过自动换算。极端显式缩放冲突会返回 `UNIT_SCALE_MISMATCH`。
- `allow_unit_mismatch`: 默认 `false`。只有确认源 metadata 错误或用户要求极端比例时才设为 `true`，并在 `layout_notes` 记录理由。
- 详细规则和验收见 `references/units_and_scale.md`。每次生成都要把 `make-scene.unit_audit` 与 sidecar `composition_audit` 交给下游 agent。
- 地面/桌面/平台上的资产必须用 bbox 对齐支撑面。生成后调用 `inspect-bounds` 检查，发现 `min_z` 低于支撑面时，调整该 reference 的 `translate.z` 后重新生成。
- Synthesis 精确回归允许在没有独立 floor mesh owner 时，用多个物体底部估计的共识 floor plane 验证
  `grounded_on_floor_plane`，但误差必须不超过 0.015 m。该证据只用于源场景回归；新生成的动态场景仍须
  提供真实 floor mesh 或 `support_colliders`。
- 家居空房间或室内背景不保证有物理碰撞。即使视觉上有地板，也要用 `support_colliders` 添加 invisible static floor collider 作为兜底。对放置 dynamic 小物的桌面/柜面，如果支撑资产碰撞未验证，也要添加薄 box support collider。
- 不要把机器人、车辆、家具默认写成 `translate.z=0` 并假设不会穿模。只有当 `inspect-bounds` 证明 `bbox.min_z` 已贴近目标支撑面时才接受该高度。
- 对“远处”这种相对关系，scene spec 中应写成明确坐标。若用户没有给数字，目标工作台中心距主工作台中心默认约 `5-7m`，并用 bbox 检查不重叠和通道可通行。
- 对家居大房间或“满满当当”需求，生成后运行 `inspect-layout-coverage scene.usda --min-coverage-xy 0.55`。如果出现 `underused_large_room`，说明家具集中在中心模板，需要把 desk/bookshelf/cabinet/reading corner 等分散到房间边缘和角落。

## 编辑场景

编辑不是特殊 agent。推荐做法是让上层 agent 读取旧 spec 或旧 scene，生成一个新的 spec，再调用 `make-scene` 输出新文件。复杂的 USD 原位编辑可以由上层 agent 自己调用 Isaac/pxr 或扩展本 skill 的工具函数。
