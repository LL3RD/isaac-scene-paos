# GenieSim Scene Generation Notes

这个文件说明如何把 GenieSim 原生场景生成思想接入 `isaac-scene-skill-lite`，避免家居场景变成低保真“资产拼贴”。

## GenieSim 原生链路

GenieSim 的 generator 通常不是直接写一串坐标，而是：

1. LLM 生成 `LLM_RESULT.py`，其中包含 `root_scene()`。
2. `root_scene()` 使用 `scene_language` 构建对象和关系。
3. `helper.py` 计算 scene graph、layout 和对象姿态。
4. `utils/usd.py` 将 layout 导出为 `scene.usda`。

lite skill 目前不直接运行完整 GenieSim agent，但应吸收它的两个关键原则：

- 用 scene graph / functional relation 思考场景，而不是只堆物体。
- 用已有背景和资产 metadata 推导位置、支撑关系、朝向和尺寸，而不是任意缩放。

## 在 lite skill 中的推荐做法

- 生成前先选完整背景，再根据背景补物体。
- 把布局写成关系说明，再落成坐标。例如 `sofa faces TV`、`coffee table centered in front of sofa`、`lamp beside reading chair`。
- 每个新增物体都应记录 placement 语义：`floor`、`wall`、`support_object`。
- 支撑关系必须有被支撑物：`cup on coffee_table`，不要只写一个孤立的 `cup` 坐标。
- 对已有背景中的家具，优先视为 scene graph 的已有节点，不要重复生成同类节点。

## Agent 生成时的最小思考模板

```text
1. 房间类型和背景：我要生成什么房间？是否有可用完整背景？
2. 已有节点：背景里可能已有哪些家具？哪些需要避免重复？
3. 主功能组合：这个房间最核心的 1-2 个功能组合是什么？
4. 大件布局：大件靠墙/居中/围绕关系是什么？
5. 小物依附：哪些小物放在哪个支撑面上？
6. 质量检查：材质、比例、穿模、朝向、相机是否合格？
```

## 什么时候可以不用 GenieSim 完整背景

- 用户明确说“从空房间开始”。
- 要生成非常抽象的测试场景。
- 所有可用家居背景 health check 都失败。
- 用户要跨域，例如“仓库里摆家居展示区”，此时背景应按用户指定域选择。

即使用空房间，也应尽量用真实家具 USD，并避免 primitive furniture 成为主体。
