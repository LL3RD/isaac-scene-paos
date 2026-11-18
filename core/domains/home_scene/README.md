# Home Scene Domain

这个领域包用于让上层 agent 用同一套 `scene_tools.py` 生成家居/室内生活场景。它结合两类思路：

- SAGE 风格的分阶段布局：房间结构 -> 大件家具 -> 功能组合 -> 支撑面小物 -> 装饰 -> 反馈检查。
- GenieSim 风格的 scene graph / scene-language 思想：优先复用完整背景和真实资产，用对象关系指导坐标，而不是只靠 primitive 和手写坐标拼装。

## 适用范围

- 客厅、卧室、厨房、餐厅、书房、家庭办公室、浴室、储藏间等单房间或少量房间组合。
- 用户要求“从空房间开始生成”“把房间布置满一点”“更像真实家居”“在仓库里放家居陈设”等场景。
- 不适合直接生成完整机器人任务逻辑；机器人任务仍由上层 agent 或 IsaacLab 任务代码定义。

## 使用顺序

1. 解析用户 prompt，补齐 `room_type`、`style`、房间尺寸、主要功能、必须出现的物体。
2. 读取 `background_catalog.md`。如果用户指定背景/房间 shell，优先保留；否则优先选择 GenieSim 完整室内背景，不要默认 primitive room shell。
3. 对候选背景运行 `list-home-backgrounds` 和 `inspect-asset-health`；背景引用/贴图明显坏时换背景。
4. 读取 `room_templates.md` 选择房间模板，先列出必须大件和关键功能组合。
5. 读取 `furniture_index.md` 和 `asset_quality.md`，把家具和小物转成检索 query；优先真实 USD 资产，最后才用 primitive。
6. 读取 `physics_support.md`，为房间地板和必要支撑面规划 `support_colliders`。
7. 读取 `spatial_coverage.md`，根据背景 bbox 规划多个功能区，不要把所有家具堆到中心。
8. 读取 `geniesim_scenegen_notes.md`，先写关系和支撑语义，再落成坐标。
9. 按 `layout_rules.md` 分阶段写 JSON scene spec。
10. 用 `make-scene` 生成，再用 `inspect-bounds`、`inspect-layout-coverage`、`inspect-asset-health`、`validation_checklist.md` 和 `feedback_loop.md` 做自检。发现穿模、悬浮、过空、覆盖率不足、方向不对、材质缺失或关键物缺失时，修正 spec 后重新生成。

## 核心原则

- 家居场景不是简单堆物体。要有功能区、锚点家具、相互朝向、可通行空间、支撑面小物和墙面装饰。
- 真实背景优先。普通家居场景默认不要从 primitive 空壳开始，除非用户明确要求空房间。
- 物理支撑必须显式兜底。只要开启重力或用户会播放仿真，就要加入 `support_colliders`；不要假设视觉地板/桌面一定有碰撞。
- 大房间必须按背景 bbox 分区。若背景是 `10m x 10m`，不要只用 `5m x 4m` 小客厅模板堆在中心。
- 大件优先，小件依附大件。先放 sofa/bed/table/counter/cabinet，再放 chair/lamp/TV/monitor，再放 books/cups/plates/vases 等小物。
- 真实资产优先。primitive fallback 只能作为辅助几何或占位，不能成为家居主体。
- 物体 placement 分三类：`floor`、`wall`、`support_object`。不要把“放在桌上”的物体当作地面物体，也不要把“靠墙放”的家具当作 wall-mounted。
- 比例要可信。不要为了“填满房间”把 bookshelf、cabinet、TV cabinet 等大件随意放大数倍。
- 每个支撑面尽量有 2 个以上合理小物；开放式 shelf/bookshelf 尽量放多件小物，但不要为了数量造成穿模或遮挡。
- 保留通道。普通家居行走通道建议至少 `0.6-0.8m`；门口、厨房操作区、桌椅前方不要被大件挡住。
- 家具方向要服务于功能关系：chair 面向 table/desk，sofa 面向 coffee table/TV，bed 的床头靠墙，nightstand 靠床边。

## 推荐输出元信息

上层 agent 可以在 JSON scene spec 里保留这些额外字段，`make-scene` 会忽略未知字段，但 sidecar 会记录它们，方便编辑和交接：

```json
{
  "domain": "home_scene",
  "room": {
    "type": "living_room",
    "style": "modern bright",
    "size_m": [5.0, 4.0],
    "quality_target": "complete lived-in room"
  },
  "layout_notes": {
    "anchor_furniture": ["sofa", "coffee_table", "tv_stand"],
    "functional_groups": ["sofa-coffee_table-tv", "bookshelf-reading_lamp"],
    "circulation_clearance_m": 0.7,
    "support_colliders_required": true,
    "min_layout_coverage_xy": 0.55
  }
}
```

## 参考来源

- SAGE: Scalable Agentic 3D Scene Generation for Embodied AI, NVlabs/sage. 本领域包只总结其公开代码中的高层布局与检查思想，不依赖 SAGE server、TRELLIS、Objaverse 或其 LLM/VLM 服务。
- GenieSim generator: 参考本地 GenieSim 的 `generator/app.py`、`helper.py` 和 `scene_language/` 的 scene graph / layout 思路；lite skill 不直接运行完整 GenieSim agent，但把背景复用、关系布局和资产质量检查作为家居生成约束。
