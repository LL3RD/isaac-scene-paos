# 单房间生成与朝向

## 生成顺序

1. 从无家具的建筑壳开始，记录室内可用 bbox、入口和不可占用区；默认优先使用
   `config/room_shells.json` 中的参数化预设，再回退到 primitive 空壳。不要引用自带
   家具的完整背景。保留 primitive 空壳作为严格通用能力回归线。
2. 把需求拆成 `anchor → functional group → prop`：先桌/床/沙发/设备，再椅子/柜子/显示器，最后少量有语义的小物。
   重复组合优先写 domain/brief `functional_groups`，不要逐实例手写世界坐标。局部偏移需包含真实资产
   半尺寸与 clearance；组包络整体落在真实地板/支撑 mesh 上后，再让装饰物适应剩余空间。
3. 每类角色单独检索 3–5 个候选，检查单位、bbox、材质和语义后再选。会议室、办公室、教室、病房、商店等未命中专用 domain 时使用 `generic_scene`。
4. 先求平面布局，再按 bbox 对齐地面或支撑面；为入口、座椅使用区、柜门和机器人留出通道。
5. 批量求解时把 `scene_root` 的真实墙、窗、框和与物体高度相交的低吊顶加入障碍层；同承托面
   已放对象继续增量加入障碍集合。若 scene root 还含家具/设备，只接受建筑路径语义；空心多墙
   聚合 mesh 不得把整房 AABB 当实体。若高柜无解，先做资产—壳体尺寸兼容性诊断，不得关掉壳体检查。
6. 合成后依次跑依赖、bbox、对象—建筑壳重叠、支撑、朝向和覆盖率检查；只修改失败约束并重生成。

## 朝向契约

有正反面的资产（chair、sofa、bed、desk、TV、cabinet、robot、machine）必须记录：

- `front_axis`: 源资产在零旋转时的局部正面轴，取 `+X/-X/+Y/-Y`。
- `facing_target`: 应面向的目标 prim。
- `facing_mode`: 默认 `auto`；桌/台类目标按旋转后的最近足迹点求解，其他目标
  朝中心。需要时显式设为 `target_center` 或 `nearest_target_footprint`。
- `orientation_source`: metadata、源资产四向预览、已验证 override 或人工检查。
- `orientation_confidence`: `high/medium/low`。

插入前先检查资产本体。metadata 不明确时，使用同一相机和尺度查看 `0/90/180/270°` 四向预览，确认座椅开口、靠背、屏幕法线、床头或设备操作面；AABB 只能判断尺寸，不能判断正面。用下式求初始 Z yaw：

`yaw = heading(solved_target_point - subject) - heading(front_axis)`

把 `front_axis` 和 `facing_target` 写进 scene spec。`make-scene` 会把契约写到 USD prim；随后运行：

```bash
python3 scripts/scene_tools.py --config "$ISAAC_SCENE_SKILL_CONFIG" --envelope inspect-facing scene.usda \
  --pair /World/Objects/Chair=/World/Objects/Table --max-angle-deg 20
```

编译阶段和验收阶段都默认让桌/台侧边座椅朝向目标足迹最近点，适合长桌两侧的
平行椅子排；只有“必须看向目标中心”时才用 `facing_mode=target_center` 和
`inspect-facing --target-mode center`。

失败时先判断：

- 所有同类资产都相差固定 `90°/180°`：修正该资产的 `front_axis`/override。
- 只有某个实例错误：修正该实例的 Z rotation。
- 正面正确但体验仍不合理：检查前方净空、背部靠墙、目标遮挡和通道；朝向通过不等于布局通过。

## 最小验收

- 主体组合完整且场景用途一眼可辨。
- 所有 floor/support 对象接触正确，无明显穿模和对象—背景占用冲突。
- 每个 directional asset 的 facing error `≤20°`，关键交互对象建议 `≤15°`。
- 默认透视图检查语义/遮挡，俯视图检查通道、对称和朝向。
