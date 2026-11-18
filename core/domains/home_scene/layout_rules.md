# Home Layout Rules

这些规则给上层 agent 规划 scene spec。它们是可扩展的布局约束，不是固定坐标。

## 分阶段生成

1. 房间 shell：确定房间类型、尺寸、风格、门窗/墙体/背景。单房间优先，除非用户明确要多房间。
2. 房间 bbox 和物理支撑：用背景 bbox 估算可用空间，并向内收缩得到 usable bbox；在 spec 中加入 floor/support colliders。
3. 大件家具：先放 bed/sofa/table/counter/cabinet 等 anchor。大件决定空间结构，优先靠墙、靠角或放在功能区中心。
4. 功能分区：大房间要拆成主客厅、书桌/办公、阅读角、储物展示等多个区域，不要全堆在中心。
5. 功能组合：围绕 anchor 放相关家具，例如 sofa-coffee table-TV、bed-nightstand-lamp、desk-chair-monitor、dining table-chairs。
6. 支撑面小物：在每个可支撑物体上放合理小物。桌、柜、书架、台面、床头柜都是 support；lamp、植物、电视等通常不是 support。
7. 墙面/装饰：加入 picture、clock、wall shelf、plant、vase 等，让房间不空。
8. 检查反馈：检查缺失、碰撞、悬浮、穿模、比例异常、朝向错误、通道不足、布局覆盖率不足；必要时修改 spec 重生成。

## placement 类型

- `floor`: 物体底部接触地面，包括靠墙的沙发、柜子、桌椅、冰箱、床、落地灯。
- `wall`: 物体直接安装在墙上，包括 picture、wall clock、wall shelf、wall-mounted TV。
- `support_object`: 物体放在另一物体上或内部，包括 lamp on desk、book on shelf、plate on table。

不要把 `beside/next to/in front of/behind` 误写成 support；这些关系仍然是地面物体之间的 XY 关系。

## 空间关系

- `edge`: 靠墙或靠房间边缘。适合 bed headboard、sofa、bookshelf、wardrobe、TV stand、counter、refrigerator。
- `middle`: 放在功能区中心。适合 coffee table、dining table、kitchen island。
- `near`: 近但留通行空间，默认 bbox 边缘间距 `0.3-0.8m`。
- `close`: 接近使用关系，默认 bbox 边缘间距 `0.05-0.25m`，例如 chair 到 table、nightstand 到 bed。
- `far`: 分属不同功能区，默认中心距离大于两个大件最大边长之和。
- `face_to`: 交互对象朝向目标，例如 chair face_to desk、sofa face_to TV。
- `center_align`: 与目标中心线对齐，例如 coffee table 与 sofa 对齐、TV stand 与 sofa 对齐。
- `around`: 多个同类围绕中心物，例如 dining chairs around dining table。

## 房间通道与门窗

- 主要通道建议保留 `0.6-0.8m` 以上；厨房、餐桌、床侧和桌椅前方尤其重要。
- 不要用大件挡住门、主要窗户、柜门或冰箱门的前方区域。
- 单房间可以没有显式门，但如果有门窗，门窗所在墙面附近应避免放置遮挡大件。
- 如果没有门窗几何，也要在布局上留出一个“入口侧”，不要让房间四周完全被家具封死。

## 大房间覆盖率

- 先用 `inspect-bounds --prim /World/Background` 看背景尺寸。
- 对完整家居背景，优先用 `inspect-layout-coverage` 输出的 `layout_bounds` 作为真实可摆区域；如果背景总 bbox 被窗帘/床/已有家具拉大，用 `--layout-prim` 指向房间壳。
- 背景任一轴大于 `6m` 时，不要使用固定 `[-2.5, 2.5] x [-2, 2]` 的小房间模板。
- “满满当当”的客厅要让多个功能区分散到房间边缘和角落，生成后用 `inspect-layout-coverage --min-coverage-xy 0.55 --max-coverage-xy 0.90 --min-boundary-margin-m 0.35` 检查。
- 如果 `coverage_xy.min < 0.55`，优先把 desk、bookshelf、cabinet、reading chair、plants 移到更靠墙/靠角位置，而不是继续在中心加小物。
- 如果出现 `objects_too_close_to_room_boundary` 或 `overfilled_room_bounds`，说明布局过度贴边。把 floor furniture 从背景 visual bbox 边缘向内移动，或减小 scale；不要把墙体/窗帘/门框厚度当作可用室内空间。
- 背景 visual bbox 只能作为粗略外框；floor furniture 的可用范围应是 `layout_bounds` 向内收缩 `0.35-0.8m` 后的 usable bbox，并且要扣除资产自身 bbox half extent。

## 支撑面小物

- 每张桌、书桌、床头柜、餐桌、厨房台面、开放柜面至少尝试放 `2+` 个小物。
- 开放式 shelf/bookshelf 可以放 `5+` 个 books/decor/container，但前提是资产尺寸允许，不要强行堆叠造成穿模。
- 小物必须使用支撑面高度对齐：先生成，后用 bbox 检查 `min_z` 是否贴近支撑面。
- 如果小物可能受重力影响，必须确认支撑面有碰撞；否则在 `support_colliders` 中为桌面/柜面添加 invisible static collider。
- 小物不要直接散落地面，除非用户明确要“杂乱/凌乱”效果。

## 风格一致性

- 用户没有指定时，默认 `modern bright`，即明亮、整洁、少量自然材质。
- 同一房间的大件家具风格应一致：现代、木质、工业风、极简、温馨等不要混乱混搭。
- 材质和颜色可以有对比，但不要出现完全不合语境的工业零件、工厂托盘或机械臂。

## 与 scene spec 的关系

- 这些规则由上层 agent 转成 `references` 里的明确坐标、旋转、scale 和 support 关系。
- `make-scene` 只执行 spec，不会自动理解 sofa 应该面向 TV；上层 agent 必须把朝向写成 `rotate`。
- 生成后必须用 `inspect-bounds` 做高度检查；XY 重叠和通道检查由上层 agent 根据 bbox 或可视化反馈完成。
- 大房间生成后必须用 `inspect-layout-coverage` 做覆盖率检查；如果家具过度集中，先改布局而不是推 viewer。
- 大房间生成后也必须检查边界安全距离；如果家具 union bbox 几乎撑满背景 bbox，先向内收缩布局再推 viewer。
