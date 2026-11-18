# Home Scene Feedback Loop

家居场景除了几何正确，还要“看起来像真实房间”。建议上层 agent 在生成后做 1-3 轮轻量反馈，不要只依赖 `validate-scene`。

## 必要检查

1. `validate-scene`：USD 能否打开，是否有 `/World`。
2. `inspect-bounds`：是否穿地、悬浮、明显高度错误。
3. `inspect-layout-coverage`：大房间里家具是否过度集中在中心，或为了填满房间而过度贴近背景边界。
4. 检查 scene spec 是否包含 `support_colliders`，尤其是房间地板 collider。
5. `inspect-asset-health`：背景和主家具是否有明显缺失引用或贴图。
6. 默认相机截图：能否看到完整房间、主功能组合和关键小物。
7. 俯视图或高位透视图：检查家具关系、通道和拥挤程度。

## 视觉反馈问题

截图后让上层 agent 回答这些问题：

- 这个房间一眼能看出是什么类型吗？
- 是否有真实的墙、地板、家具材质，而不是只有灰色 primitive？
- 主功能组合是否成立，例如客厅的 sofa/coffee table/TV 是否互相朝向？
- 房间是否过空，还是所有物体都挤在一角？
- 家具是否贴到墙体/房间外包围盒边界，导致部分物体看起来跑到房间外？
- 小物是否放在合理支撑面上？
- 开启播放时，地板/桌面/柜面是否有 collider 能托住物体？
- 大房间四周是否也被合理利用，而不是家具全部挤在中央？
- 相机是否只拍到了墙、屋顶或局部，而没有展示房间整体？

## 修正优先级

1. 换背景：如果 primitive shell 或背景贴图坏，先换背景。
2. 补物理支撑：如果会播放仿真，先添加 floor/tabletop support colliders。
3. 调布局覆盖：如果 `underused_large_room`，先把功能区分散到房间边缘和角落；如果 `objects_too_close_to_room_boundary` 或 `overfilled_room_bounds`，先把贴边家具向室内移动并保留通道。
4. 换主家具：如果 sofa/bed/table/cabinet 比例或材质太差，先替换主家具。
5. 补小物：加 books/cup/lamp/plant/wall art，但不要掩盖主布局问题。
6. 调灯光和相机：最后提升观感。

## 终止条件

- 必须物体都存在。
- 没有严重穿模/悬浮。
- 没有物理支撑缺失；播放时地板/桌面物体不会直接掉落。
- 大房间没有 `underused_large_room`、`objects_too_close_to_room_boundary`、`overfilled_room_bounds` warning，除非用户明确要留大量空地或贴墙陈列。
- 主功能组合成立。
- 默认截图能看到房间整体。
- 背景和主家具不是 primitive fallback，除非用户明确接受 fallback。
