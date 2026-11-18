# Robot Index

这个索引用于帮助上层 agent 快速选择机器人资产。优先使用 `curated_common` 中的 `asset_id`，不要在常见机器人场景里先扫完整官方库。

## 选择原则

- 用户说“轮式人形机器人”“轮式双臂移动操作机器人”“移动双臂搬运机器人”时，优先用 `geniesim_g2_omnipicker`。
- 不要默认把 `isaac_iw_hub` 和其他人形机器人资产叠放组合成“轮式人形机器人”。只有用户明确要求临时概念展示、且检索不到完整机器人时，才可以把这种组合标记为 fallback。
- 用户说“机器人狗”“四足机器人”“Go2”时，用 `isaac_unitree_go2`。
- 用户说“移动底盘”“AMR”“运输车”但不要求人形上半身时，用 `isaac_carter_v1`、`isaac_nova_carter` 或 `isaac_iw_hub`。
- 用户说“Franka/Panda”“UR5E/UR10E”时，用对应机械臂资产，不要用移动机器人替代。
- 所有机器人放置后都必须跑 `inspect-bounds`，不要假设 `translate.z=0` 就贴地。

## Curated 机器人资产

| 逻辑 asset_id / query | 类型 | 适用场景 | 首选库 |
| --- | --- | --- | --- |
| `franka panda` | 机械臂 | Fabrica 双臂装配、Panda/Franka 操作 | `isaac_official` |
| `ur5e robot` | 机械臂 | UR5E 工业机械臂操作 | `isaac_official` |
| `ur10e robot` | 机械臂 | UR10E 工业机械臂操作 | `isaac_official` |
| `unitree go2` | 四足机器人 | 机器人狗、巡检、陪同搬运展示 | `isaac_official` |
| `geniesim_g2_omnipicker` | 轮式人形/移动双臂 | 轮式人形机器人、移动双臂搬运、托盘搬运 | `geniesim` |
| `geniesim_g1_omnipicker` | 双臂人形操作机器人 | G1 omnipicker、人形双臂抓取展示 | `geniesim` |
| `agibot a2d` | 人形机器人 | 用户明确指定 Agibot A2D | `isaac_official` |
| `carter` / `nova carter` | 轮式移动底盘 | AMR、运输车、移动机器人 | `isaac_official` |
| `iw hub` | 轮式移动底盘 | AMR、移动底盘、运输平台 | `isaac_official` |

不要在 reference 中固化服务器绝对路径。通过 `search-assets` 在当前 profile 解析逻辑 ID；资产尚未下载时保留该角色并返回 `pending` 或低置信度 observation。

## 推荐检索命令

```bash
python3 scripts/scene_tools.py search-assets "wheeled humanoid mobile dual arm robot" --libraries curated_common --types robot --topk 5
python3 scripts/scene_tools.py search-assets "robot dog go2" --libraries curated_common --types robot --topk 5
python3 scripts/scene_tools.py search-assets "franka panda robot arm" --libraries curated_common --types robot --topk 5
```

## 姿态与关节完整性

先检查实际 USD，不根据机器人名字猜连杆原点：

```bash
python3 scripts/scene_tools.py --envelope inspect-articulation-joints scene.usda --root /World/Robot
python3 scripts/scene_tools.py --envelope pose-articulation scene.usda --root /World/Robot \
  --positions joints.json --output scene_v02.usda
```

`joints.json` 是“完整绝对 joint prim 路径 → 数值”的 JSON 对象。旋转关节用度，
移动关节用米；从检查结果读取路径、当前值和 limits。必须填写该根下所有可动关节，
包括夹爪；不要把弧度当成度。工具按 USD localPos/localRot 建立关节树，保留 mesh-local
变换、材质和刚体，只在新层写姿态/已有 drive target，并重新定位移动底座的世界固定锚点。
不允许覆盖输出。非均匀缩放、闭环、多父节点、未支持关节必须报错，不能绕过工具拆散连杆。

`inspect-articulation-joints` 检查可见、启用的 internal Fixed/Revolute/PrismaticJoint：
连接间隙（默认 2 mm）、轴/框架误差及关节限位。移动关节允许沿其轴位移；
厘米/毫米场景、引用缩放都会换算成米。原始模型默认静态零位也可能超限，需设置有效姿态。
两个刚体各自的正均匀缩放可以不同；工具分别换算锚点，再在无尺度旋转框架中比较角度。
非均匀缩放、剪切、镜像不在支持范围，返回 pending；已确认的断链/超限仍返回 fail。
JSON `limits` 的 `null` 端点表示无限制，不是零；NaN 不合法。
零关节、未支持类型不能宣称机器人已验收。该检查不证明自碰撞、末端可达、世界锚定或动力学；
这些保持单独待验。批量场景必须扫描全部机器人，不能只验第一台。

### 显式初始化已有姿态

静态连杆变换、`state:*:physics:position` 和 drive target 是不同数据。资产有合法驱动目标，
不代表打开 USD 后已经处于该姿态。检查报告分别给出这些值及 `initialization_required`。

```bash
# 不提供 output 时仅列计划；根据资产实际 authored 数据选择来源。
python3 scripts/scene_tools.py --envelope initialize-articulation scene.usda \
  --root /World/Robot --from drive-targets
python3 scripts/scene_tools.py --envelope initialize-articulation scene.usda \
  --root /World/Robot --from drive-targets --output scene_v02.usda
python3 scripts/scene_tools.py --envelope inspect-articulation-joints scene_v02.usda --root /World/Robot
```

也可显式选择 `--from authored-state`。所有可动关节必须有有限、合法的已写入位置；
缺项、超限时 pending，不在两个来源间偷偷回退、不补零、不截断限位。成功时仅写新层，
保留各刚体缩放，更新已有 drive/state 属性；源文件不变。这是静态初始化，不执行 PhysX reset，
不产生 dynamics 验收证据。下一步仍需检查地面接触、自碰撞、真实物理初始化及控制器行为。

## 放置高度参考（仅初值）

- `geniesim_g2_omnipicker`: 建议初始 `translate.z = 0.05`，然后用 `inspect-bounds` 确认 `bbox.min_z` 约为 `0.01m`。
- `geniesim_g1_omnipicker`: 建议初始 `translate.z = 0.22`，然后检查 bbox。
- `isaac_unitree_go2`: 建议初始 `translate.z = 0.459`，然后检查 bbox。

这些数值只作为初始值。最终仍以场景中的 `inspect-bounds` 输出为准。
