# Home Asset Quality Rules

家居场景的真实感主要由资产质量、比例、材质和组合关系决定。上层 agent 不能只看 asset_id 命中关键词，还要做质量筛选。

## 资产选择优先级

1. 完整室内背景：真实 room/home/kitchen/study/office 背景优先于 primitive shell。
2. 带材质的真实 USD 家具：sofa、bed、table、cabinet、chair、counter、lamp、plant 等优先用 GenieSim 或 Isaac 官方资产。
3. 同类别相近资产：找不到 `floor lamp` 时可用普通 `lamp`，找不到 `TV` 时可用 monitor/screen，但要记录替代。
4. primitive fallback：只适合墙、地板、简单屏幕、简单装饰板、临时占位盒；不适合作为默认 sofa、bed、cabinet、bookshelf。

## 禁止或强烈避免

- 不要用 primitive room shell 作为默认家居背景。
- 不要把工业工作台、托盘、Fabrica fixture、机械臂、仓库道具混入普通家居。
- 不要对真实家具做极端缩放。没有 bbox 依据时，大件家具任一轴 scale 建议保持在 `0.5-2.0`；超过这个范围需要在 `layout_notes` 写明原因。
- 不要把小物放大成大件，也不要把大件缩小成摆件。
- 不要用明显缺贴图的资产做视觉主角。

## 材质和贴图检查

对背景和主家具运行：

```bash
python3 scripts/scene_tools.py inspect-asset-health /path/to/asset.usda
```

判断：

- `status=pass`：可优先使用。
- `status=warn` 且缺失很少：可以使用，但结果里记录风险。
- `status=fail` 或主材质贴图缺失：换资产或修复路径后再用。

注意：有些 `.usd` 是二进制或 crate 文件，纯文本检查可能无法完整发现引用。若 `checked_as_text=false`，只能说明快速检查不足，需要用 Isaac Sim 打开验证。

## 比例检查

生成后用：

```bash
python3 scripts/scene_tools.py inspect-bounds scene.usda --root /World/Objects --floor-z 0
```

上层 agent 要检查：

- sofa/bed/table/cabinet 等大件尺寸是否符合房间尺度。
- coffee table 高度通常应低于 sofa 坐面附近；desk/table/counter 高度不能明显异常。
- 小物体尺寸应明显小于支撑面，不应占满整张桌子。
- wall art/clock/TV 的位置应在墙面高度范围内，而不是漂浮在房间中央。

## 真实感补全

- 每个房间至少有一个主功能组合，例如 `sofa + coffee table + TV`。
- 每个关键支撑面至少放 2 个合理小物，例如 book/cup/vase/remote。
- 至少有 1-2 个软装或生活化元素，例如 plant、lamp、wall art、books、rug。
- 灯光要匹配房间：家居默认使用柔和 dome light 加一到两个 warm area/rect lights，不要只用冷白强顶光。
