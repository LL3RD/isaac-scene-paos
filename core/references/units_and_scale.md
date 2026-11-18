# Units And Scale

USD reference composition does not convert geometry when source and target layers use different `metersPerUnit`. Treat unit normalization as a mandatory composition step.

## Asset package entrypoint compatibility

Do not assume that a file named `Aligned.usd` is already normalized. Some asset packages store raw
Y-up geometry in that binary layer and author the Y-up-to-Z-up correction in a sibling
`Aligned.usda`. When `object_parameters.json` declares a non-target up axis and the sibling wrapper
references the raw layer with a non-identity alignment transform, select the wrapper. The raw layer
must fail with `RAW_ASSET_REQUIRES_ALIGNMENT_WRAPPER`; do not compensate with an entity rotation or
translation because that leaves bbox, support and front-axis evidence inconsistent.

## Required policy

1. Author generated stages in meters unless the user explicitly requires another unit: `stage.meters_per_unit=1.0`.
2. Keep `stage.unit_policy=normalize_references` (the default).
3. Run `inspect-asset-units` for every background, support asset, robot and assembly bundle selected from a new library.
4. Let `make-scene` apply `source_meters_per_unit / target_meters_per_unit` when an item has no `scale`.
5. Treat an explicit item `scale` as the total composed scale. Do not multiply unit conversion twice.
6. Use `unit_mode=multiply` only when an artistic scale must be applied after unit conversion.
7. Use `unit_mode=preserve` or `allow_unit_mismatch=true` only with a written reason in `layout_notes`.
8. After composition, verify physical bbox dimensions and support contact; unit metadata alone is not sufficient.

## Scene Spec examples

Automatic centimeter-to-meter conversion:

```json
{
  "stage": {"meters_per_unit": 1.0, "unit_policy": "normalize_references"},
  "entities": [
    {"id": "factory", "role": "background", "path": "/assets/Factory.usd"}
  ]
}
```

If `Factory.usd` authors `metersPerUnit=0.01`, the effective reference scale is `[0.01,0.01,0.01]`.

Unit conversion plus deliberate 1.5x enlargement:

```json
{
  "id": "prop",
  "role": "object",
  "path": "/assets/prop.usd",
  "scale": [1.5, 1.5, 1.5],
  "unit_mode": "multiply"
}
```

## Commands and acceptance

`inspect-support-contact` 和 `inspect-scene-overlaps` 的坐标、距离与阈值统一为**米**，
报告包含 `coordinate_unit=m` 和源 `meters_per_unit`。传 `0.005` 表示 5 mm，
即使 Factory 使用厘米也不能传 `0.5`。旧报告无单位标记时必须重跑。
`inspect-bounds` 为兼容旧布局代码保留 stage-unit 输出，明确标为 `coordinate_unit=stage_unit`；
使用其数值前乘 `meters_per_unit`。`solve-support-placement` 当前仅接受米制 stage，
非米制返回明确失败；可建米制 wrapper 再求解，不能靠放大容差补偿。
`solid` 接触检查要求 stage 显式声明 `metersPerUnit`，不把 USD 默认厘米当成资产单位证据。
内部关节两端允许不同的正均匀刚体缩放，各自作用于局部锚点；角度在去尺度框架中比较。
非均匀缩放/剪切/镜像仍属未支持，不通过扩大距离容差处理。

```bash
python3 scripts/scene_tools.py --config "$ISAAC_SCENE_SKILL_CONFIG" --envelope inspect-asset-units /path/to/asset.usd
python3 scripts/scene_tools.py --config "$ISAAC_SCENE_SKILL_CONFIG" --envelope make-scene spec.json
python3 scripts/scene_tools.py --config "$ISAAC_SCENE_SKILL_CONFIG" inspect-bounds scene.usda --root /World/Objects
```

Accept a scene only when:

- `make-scene.result.unit_audit.references` covers every referenced background, object and Fabrica bundle.
- No `UNIT_SCALE_MISMATCH`, `SOURCE_UNITS_UNAUTHORED` or waived mismatch remains unexplained.
- Bbox sizes are plausible for the semantic role. Compare robots, support furniture, doors, pallets and room/background dimensions rather than checking one asset in isolation.
- The sidecar `composition_audit` and handoff `unit_audit` are preserved for the receiving agent.

Measured asset example (verify your installed version): Factory and the Isaac heavy-duty packing table author centimeters (`0.01`); Franka and the Simple Warehouse author meters (`1.0`). A scene that scales the table but not Factory is invalid even if every file resolves.
