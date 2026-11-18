# 渲染反馈闭环

材质诊断允许在未关闭布局问题时做 preview/review，但必须记录未通过项且冻结该次 A/B 几何。
只有标记为 final/simulation_scene 的交付才要求相关几何、内容和相机门禁已通过。
光照/材质 A/B 不移动、替换或缩放资产；内容修正开启新 spec 版本，不混入同一 A/B。

## 真实感优先级与跨渲染器

真实感不能靠全场统一噪声或扩大资产数量解决。先对最终可见主体做近景检查，再回到全景：

1. **形体尺度**：板厚、接缝、倒角、螺钉、通风口、线缆和接触阴影。法线贴图不能替代轮廓几何。
2. **表面身份**：裸铝/镀锌钢、粉末涂层、橡胶、PCB、玻璃分别处理；裸金属不能按灰度刷成油漆。
   参数随真实表面工艺变化，不为多样性随机改色。保留屏幕、品牌色、发光件和已绑定纹理。
3. **多尺度纹理**：basecolor、roughness、normal 使用正确色彩空间、米制 UV/投影比例；微划痕、
   橘皮纹和低频磨损强度克制。不要给每种材质同一个 roughness 或大幅 bump。
4. **照明来源**：窗与灯具支持合理的主辅光、软阴影和高光形状。先区分表面与照明问题，再调曝光。
5. **受控内容**：可辨认的工艺阶段、物料容器、工具、标签和走线。使用密度应服务功能与画面，
   不是布满地面；允许合理的空台面、维修净空和同规格重复。

每次真实感交付至少保留整体与主材质近景。分别记录问题、证据和限制，不把 texture coverage
或 renderer 名称转换成主观“真实度分”。新模型必须记录其来源与验证状态。

**Blender 与 Isaac 不是“渲染图片导入仿真”的关系。** Blender 适合建模、UV、纹理烘焙与 Cycles
lookdev；Isaac 继续持有米制 USD、对象 ID、位姿、碰撞与运行时语义。图片仅作验收，不是仿真资产。
不必把已经良好的 Isaac/MDL 资产整场搬去 Blender；只有几何/材质编辑有收益时才往返。

- 单独外观样机可交付 `.blend` + 实际 Cycles 图，显式写 `lookdev_prototype` 和未做的物理验证。
- 回流时传几何 + UV + PBR maps。复杂程序化节点、噪声、bump、材质层混合先烘焙；默认 USD
  Preview Surface 导出并不保证任意 Cycles/MDL 节点无损等价。
- 带 bevel 的 Blender 线缆/把手曲线和文字用 `scripts/export_blender_usd.py` 导出：它在内存中
  求值为 mesh，不修改源 blend。直接导出 control spline 可能丢失实体粗细；Blender 4.2 实测
  默认 radius=1 导成 widths=2，而原 bevel_depth 只有毫米级。仍需检查目标渲染，不能只看导出成功。
- 导入 Isaac 后复查单位、轴向、材质绑定/纹理坐标、法线方向和尺度、透明与金属响应，再用相同
  相机与可比曝光做实际渲染。动态/关节另验，不能用 Blender render pass 代替。
- 新的详细程序化模型不是可见 primitive proxy：它需有可辨认结构、合理尺寸和部件/材质分层，
  经多视角预览验证后才能用于真实感场景；没有正确 CAD 时不要宣称制造精度或厂商一致性。

参考：[Blender USD 导出](https://docs.blender.org/manual/en/4.2/files/import_export/usd.html)，
[Omniverse 材质映射限制](https://docs.omniverse.nvidia.com/connect/latest/blender/material-mapper.html)。

## 三档渲染 profile

`config/render_profiles.json` 是唯一 profile 真值，`render_scene_camera.py --render-profile` 读取它：

- `preview`：960×540 Ray Traced Lighting，用于布局、相机和大体灯光快速迭代；不能作为最终视觉证据。
- `review`：1280×720 Ray Traced Lighting，用于稳定的人类/Agent 评审和可选 instance-mask 证据；未显式指定时保持该兼容默认。
- `final`：1920×1080 Path Tracing，1024 total SPP、8 次反弹并启用 OptiX denoiser；只在几何、内容和相机冻结后运行。

宽高和 settle frames 可以命令行覆盖；报告记录有效配置、完整依赖指纹、相机矩阵、输出哈希和耗时。A/B 比较必须使用同一完整场景指纹和相机；不同 profile 不得悄悄改变场景、相机、曝光或材质。材质 A/B 可改变材质，但必须单独记录该差异并保留几何/相机证据。

```bash
python3 scripts/render_scene_camera.py --scene scene.usda --camera /World/Camera \
  --output preview.png --render-profile preview --render-report preview.render.json
python3 scripts/render_scene_camera.py --scene scene.usda --camera /World/Camera \
  --output review.png --render-profile review --render-report review.render.json
python3 scripts/render_scene_camera.py --scene scene.usda --camera /World/Camera \
  --output final.png --render-profile final --render-report final.render.json
```

## 分离视觉归因

同一组固定场景/相机渲染必须生成两份独立 packet：

- `rendering_quality` 只评价曝光、噪声/锯齿、全局照明与阴影、反射/透射和材质可读性；不能因对象少或资产错误降低渲染器分数。
- `scene_content_quality` 只评价 prompt 覆盖、资产身份与质量、丰富度、空间/功能合理性和建筑布景；不能因 Path Tracing 更漂亮而提高同一内容的分数。

每个问题必须标记 `primary_cause=rendering|scene_content|geometry_uncertain`。不确定是否悬空、穿插或接触时写 `geometry_uncertain` 并回到确定性几何检查，不能仅凭 RGB 下结论。

材质“可辨认”不等于表面真实。塑料感诊断先检查：可见表面是否只有常量
`diffuseColor/roughness/metallic`、是否存在 albedo/ORM/normal、贴图材质绑定到程序化 Cube 时是否有
`primvars:st` 或 `project_uvw=true`、OmniPBR 是否误写旧参数名 `metallic/roughness` 而不是
`metallic_constant/reflection_roughness_constant`。Path Tracing 不能修复缺失的纹理坐标或重复常量材质；它只会更准确地渲染错误输入。

工业 lookdev 可保留输入场景和对象姿态，生成独立材质/灯光 sublayer：

```bash
python3 scripts/apply_industrial_pbr_lookdev.py --scene final_scene.usda \
  --output pbr_scene.usda --material-root /path/to/Modular_Warehouse/Props/Source/materials \
  --directional-lighting --report pbr_scene.report.json
```

默认保留叉车、AGV、机器人等引用资产的自带材质，只对程序化壳体、无效 legacy OmniPBR 或已确认的
constant-only 涂层做受限重绑。比较“仅材质”和“材质+灯光”时必须使用同一相机；HDRI 不应通过未封闭房间开口直接暴露成室外天空。

固定相机后先运行 `audit-material-coverage`。优先看 `priority_gaps`，处理顺序是：
`texture_declared_unmapped` → 大面积 `unbound` → 大面积 `constant_basic/constant_pbr` →
缺少 roughness/normal 的 color-only texture。coverage 使用相机投影 bbox 作为影响代理，不等同于可见像素，
因此只负责发现高影响材质缺口；是否真的改善必须由同相机 RGB 对比确认。

优先用独立 Isaac 进程复现。渲染器先 RT 预热再切换 PT，有限增加 settle 并复抓；仍黑就输出失败报告与
非零退出码，不能使用旧图片或 USD pass 兜底。每次选择新输出路径。Kit MCP、USD Code MCP 和
`isaac-sim-remote` 可用于受控反馈；若只能在已预热 Viewer 捕获，明确记录 backend、场景/相机/有效参数，
保存实际原图并标记 fresh-process reproduction pending，不能假装独立运行已经通过。
不要仅因独立进程黑帧就归咎或禁用 Kit MCP；不要未经用户需要切换其当前 Viewer。
Kit 开 stage 或切换 renderer 后可能恢复默认参数；final 会在切换后重新应用配置，
并比较实际值与请求值。只有 `profile_verified=true` 才可作为 final 档位证据。

Blender 导出、独立对象材质边界修复和双端证据命令见 `inputs_and_roundtrip.md`。
若黑图里仍能看见正确轮廓/发光点，先检查实际 light prim 的 intensity/exposure/normalize、
USD 单位及色彩/曝光设置，不要只增加采样数。跨渲染器灯光不承诺数值等价；在新 layer 中记录
源/目标值与同相机对照，逐次校准。一次场景的曝光倍率不能写成所有 Blender 输入的固定换算。

```bash
python3 scripts/scene_tools.py --envelope create-quality-review-packet comparison.json \
  --axis rendering_quality --output rendering.packet.json
python3 scripts/scene_tools.py --envelope create-quality-review-packet comparison.json \
  --axis scene_content_quality --output content.packet.json
python3 scripts/scene_tools.py --envelope validate-quality-agent-review \
  rendering.packet.json rendering.review.json --output rendering.validation.json
```

1. 固定相机分别渲染透视图和俯视图，保留每轮图片。
2. 运行 `inspect-render-feedback`，检查平均亮度、中位亮度、阴影比例、高光裁切、P05–P95 对比跨度和全局色偏。透视图还必须检查下半部主体区域；不能让大面积亮墙或天花板把几乎全黑的桌椅、机器人掩盖成全图通过。
3. 根据结构化建议只调整 dome/fill/key light、灯具尺寸、色温或 `camera.exposure_ev`。白色房间先调曝光，避免把灯光强度压到接近零仍由自动曝光拉白。
4. 让外部 agent 同时读取截图与原始 prompt/参考图，检查主体可读性、阴影/热点是否自然、可见灯具是否与照明一致、氛围是否匹配。相机候选必须通过 visual review packet/review 契约；视觉意见只能选择已渲染候选或形成受限 lighting/camera 建议，不能直接写对象姿态。
5. 使用同一相机重渲染并复检，最多两轮。仍失败时保留报告并交给用户，不无限调参。

当 Agent 指出的是布局或工艺关系问题时，不允许把自然语言意见直接翻译为最终
`translate/rotate`。先把意见归一为 `functional_groups`、连接端口、工作包络、可见必需项或相机约束，
再生成新的 pose-free spec 并重新运行批量求解、overlap/support/facing 验证。前后截图必须作为两个
候选写入 `isaac-scene-visual-review-packet/v1`，Agent 输出写入
`isaac-scene-visual-agent-review/v1` 并通过 `validate-visual-agent-review`；`preferred_candidate_id`
只表示本轮相对更优，不等于最终质量已经接受。若 verdict 仍是 `revise`，阶段报告必须明确列出未关闭的
P0，且不得把像素亮度通过冒充真实感通过。

阶段截图至少同时保留原始 capture 和展示 crop。展示 crop 可以去除 Viewer 菜单、工具栏、坐标轴和黑边，
但不得修补、生成或删除场景像素；地面网格等真实 viewport 内容若未在渲染端关闭，必须在报告中标为限制。
最终交付图仍要求独立 RGB/离屏渲染，不以裁切过的 Viewer 截图替代。

参考图任务优先在 Scene Brief 的 `lighting` 中显式写环境光和灯具。`dome.texture`、灯具
`temperature`、`exposure`、`angle`、`radius` 以及 `look_at` 会被实际写入 USD；顶灯不要
只给位置而依赖默认朝向，面光源应使用 `look_at` 指向照明区域。`disk` 必须写成
`DiskLight`，不能用 DistantLight 代替。灯具参数生效后再调整相机曝光。

```bash
python3 scripts/scene_tools.py --config "$ISAAC_SCENE_SKILL_CONFIG" --envelope \
  inspect-render-feedback perspective.png --view-kind perspective
python3 scripts/scene_tools.py --config "$ISAAC_SCENE_SKILL_CONFIG" --envelope \
  inspect-render-feedback top_down.png --view-kind top_down
```

判断原则：

- `UNDEREXPOSED_RENDER`: 优先增加环境/辅光、扩大面光源，再小幅提高曝光；不要只把主灯加到很强。
- `SUBJECT_REGION_UNDEREXPOSED`: 全图平均可能合格，但主体区域仍不可读；优先补朝向主体的面光/辅光，不能只依赖背景墙亮度。
- `HIGHLIGHT_CLIPPING`: 降低曝光或主灯强度，保留材质纹理。
- `OVEREXPOSED_RENDER`: 优先把建议的 `exposure_delta_ev` 累加到 `camera.exposure_ev`；Scene Spec 会把它编译为 `OmniRtxCameraExposureAPI_1` 的物理曝光属性。Isaac Sim 6.0.1 用 `exposure:time = 0.02 × 2^EV` 实现逐档翻倍/减半，不依赖 viewport 自动曝光。
- `LOW_CONTRAST_RENDER`: 拉开主辅光比例或改善材质/背景分离，不要继续整体提亮。
- `BLACK_OR_EMPTY_RENDER`: 先检查相机、天花板遮挡和 capture 状态，再检查灯光。
- 指标通过不等于美术质量通过；明显圆形热点、灯光方向无来源、主体仍不可读时，外部 agent 应继续做一次受限灯光修正。
- instance mask 只统计直接可见像素。把 prompt anchor 的可辨认像素比例、截边、单体支配和有效对象像素占比与全图亮度一起检查；AABB 可见而像素过小不能算完成。
- 开放式 room shell 的候选必须优先朝向已有墙/窗/门背景；看回未封闭开口形成的大块白区要降权。允许从已知开口外侧取景，但相机不得穿入物体。
- 外部 Agent 的首选仍须和 baseline 做硬门禁比较；视觉分更高不能覆盖 anchor 丢失、截边增加、输入指纹变化或独立复渲失败。
- 暗色风格可以覆盖阈值，但必须在 spec 的 `render_feedback_policy` 中记录理由和自定义阈值。

Scene Spec 建议记录：

```json
{
  "render_feedback_policy": {
    "views": ["perspective", "top_down"],
    "max_iterations": 2,
    "preserve_geometry": true
  },
  "camera": {
    "exposure_ev": -1.25
  }
}
```
