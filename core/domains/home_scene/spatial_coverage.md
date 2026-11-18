# Home Spatial Coverage Rules

当用户说“房间很满”“客厅满满当当”“很多家居资产”时，不要把所有家具堆在房间中心。必须先估计房间/背景 bbox，再把家具分散到多个功能区。

## 必须先看房间 bbox

生成或初版生成后运行：

```bash
python3 scripts/scene_tools.py --config "$ISAAC_SCENE_SKILL_CONFIG" inspect-bounds scene.usda --prim /World/Background --root /World --floor-z 0
```

记录：

- `background.min/max`
- `background.size`
- 房间可用宽度和深度

如果背景是完整 GenieSim 家居背景，不要直接把 `/World/Background` 的总 bbox 当作可用室内区域。背景总 bbox 可能被床、窗帘、门、灯或已有道具拉大。生成后应让 `inspect-layout-coverage` 自动推断 `layout_bounds`；如果自动推断不准确，用 `--layout-prim` 指向真实房间壳，例如 `/World/Background/background/home_02`。

如果背景尺寸大于 `6m x 6m`，不要继续使用固定 `5m x 4m` 小客厅模板。要按背景 bbox 分区。

## 分区布局

大房间建议拆成 3-5 个功能区：

- 主客厅区：sofa、coffee table、TV/TV stand。
- 书桌/办公区：desk、chair、monitor/lamp/books。
- 阅读角：armchair、side table、floor lamp、plant。
- 储物/展示区：bookshelf/cabinet、books/vases/decor。
- 入口/过道留白：保留通行，不要用小物填满。

## 覆盖率与边界要求

对“满满当当”的家居场景，生成后运行：

```bash
python3 scripts/scene_tools.py --config "$ISAAC_SCENE_SKILL_CONFIG" inspect-layout-coverage scene.usda --min-coverage-xy 0.55 --max-coverage-xy 0.90 --min-boundary-margin-m 0.35
```

判断：

- 如果背景任一轴大于 `6m`，`coverage_xy.min` 建议至少 `0.55`。
- 如果 `coverage_xy.min < 0.55`，说明家具过度集中，应把 bookshelf/cabinet/desk/reading corner 移到更靠墙或更靠角的位置。
- 如果 `coverage_xy.max > 0.90` 或出现 `objects_too_close_to_room_boundary`，说明家具过度贴边。背景 visual bbox 通常包含墙体、门框、窗帘、装饰厚度，不是完整可用室内区域。
- 对 `10m x 10m` 房间，所有家具只覆盖 `5m x 4m` 会显得集中在中间，应扩大到约 `6m-8m` 的有效布局范围；但不要让 object union bbox 撑满整个背景 bbox。
- 对普通 floor furniture，`margins_xy` 建议至少 `0.35m`；对需要靠墙的大柜、TV cabinet、书架，可接近墙但仍要按自身 bbox 保持合理室内侧，不能穿到墙外。

## 坐标策略

不要先假设房间是 `[-2.5, 2.5] x [-2, 2]`。应根据 `layout_bounds` 而不是完整 visual background bbox 算布局边界：

- `usable_min_x = bg_min_x + wall_clearance`
- `usable_max_x = bg_max_x - wall_clearance`
- `usable_min_y = bg_min_y + wall_clearance`
- `usable_max_y = bg_max_y - wall_clearance`
- `wall_clearance` 默认 `0.4-0.8m`，根据家具深度调整。

摆放时必须用资产实际 bbox，而不是只用资产原点：

- `asset_min_x >= usable_min_x`
- `asset_max_x <= usable_max_x`
- `asset_min_y >= usable_min_y`
- `asset_max_y <= usable_max_y`
- 如果只知道资产大概尺寸，先估算 half extent，把 `translate.x/y` 限制在 `usable_bounds` 再向内扣除 half extent。

摆放建议：

- TV/TV stand、bookshelf、cabinet 靠某一侧墙。
- sofa 面向 TV，距离 TV 不要超过房间深度的一半。
- desk/reading corner 放到另一侧或角落，别和主客厅组合完全重合。
- plants/floor lamp/side table 用来填角落和边界，但保留主要通道。

## 常见失败模式

- 房间 bbox 是 `10m x 10m`，但家具 translate 只在 `x=-2.5..2.5, y=-2.3..1.8`。
- 为了修复“太集中”，把家具反向推到 `background.min/max` 边缘，导致 object bbox 离墙只有几厘米甚至穿墙。
- 只检查 `translate` 是否在房间范围内，没有检查资产 bbox 是否完整留在可用室内区域。
- 书桌、书柜、阅读椅、植物全围着 coffee table 放，导致中心拥挤、四周空。
- 只检查物体是否存在，不检查房间覆盖率。
- 相机看起来很满，但俯视图显示家具都堆在中间。
