# Home Physics And Support Rules

家居场景只要开启 Isaac Sim 播放/重力，就必须显式考虑支撑碰撞。很多室内背景是视觉场景，不保证地板、桌面、柜面都有可用碰撞体；如果 agent 只把物体摆到视觉地板或桌面上，播放后物体可能直接掉下去。

## 必须规则

- 家居/室内场景默认开启重力时，必须在 scene spec 中加入 `support_colliders`。
- 对空房间、room shell、GenieSim room/home 背景，至少加入一个覆盖房间地面的 invisible static floor collider。
- 如果小物是 dynamic，或用户会播放仿真，且桌/柜/书桌资产未确认有碰撞体，则为对应支撑面加入 invisible static tabletop/support collider。
- 纯展示小物可以用 `physics: "none"` 或 `physics: "static"`，不要把装饰小物默认设成 dynamic 后又不给支撑碰撞。
- 不要假设背景自带地板碰撞；即使视觉上有地板，也要用 `support_colliders` 做兜底。

## Floor Collider 模板

如果房间 bbox 约为 `10m x 10m`，可添加：

```json
{
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
  ]
}
```

参数含义：

- `translate` 是 box 中心。
- `size` 是 box 的实际 XYZ 尺寸，单位米。
- floor collider 顶面应略高于或贴近视觉地板，通常让 `top_z = 0.0-0.02m`。
- `visible: false` 只隐藏渲染，不代表没有碰撞。

## 支撑面 Collider 模板

对桌面/柜面可加薄 box：

```json
{
  "id": "coffee_table_top_collider",
  "type": "box",
  "prim": "/World/Colliders/CoffeeTableTop",
  "translate": [0.0, -0.35, 0.37],
  "size": [1.0, 0.55, 0.03],
  "visible": false,
  "physics": "static"
}
```

建议：

- collider XY 尺寸略小于真实桌面，避免视觉上旁边的物体被空气托住。
- collider 顶面和小物 `min_z` 对齐，或者略低 `0.005-0.01m`。
- 对 coffee table、desk、side table、cabinet top、bookshelf shelf 等支撑小物的位置都可以加。

## 验证流程

生成后必须运行：

```bash
python3 scripts/scene_tools.py --config "$ISAAC_SCENE_SKILL_CONFIG" validate-scene scene.usda
python3 scripts/scene_tools.py --config "$ISAAC_SCENE_SKILL_CONFIG" inspect-bounds scene.usda --root /World/Objects --floor-z 0
```

如果开启播放后仍掉落，检查：

- 是否忘了 `support_colliders`。
- collider 是否被放到地板/桌面下面太远。
- dynamic 小物是否放在没有 tabletop collider 的桌面上。
- 资产自身是否带有已启用的 dynamic rigid body，导致被重力影响。
