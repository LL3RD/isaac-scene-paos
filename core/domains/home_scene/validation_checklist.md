# Home Scene Validation Checklist

生成家居场景后，上层 agent 应按这个清单自检。若失败，先修改 JSON scene spec，再重新生成；不要直接推 viewer。

## 必须通过

- 房间类型明确，主要功能可一眼看出，例如客厅有 sofa/coffee table/TV 组合，卧室有 bed/nightstand，厨房有 counter/refrigerator/stove/sink。
- 背景不是默认 primitive 空壳，除非用户明确要求空房间或记录了 `background_quality: "primitive_fallback"`。
- 背景和主家具通过 `inspect-asset-health`，没有大量缺失 references/textures。
- 如果开启重力或需要播放仿真，scene spec 中存在 `support_colliders`，至少有一个覆盖房间地面的 invisible static collider。
- 大件家具数量合理，不空也不过度拥挤。
- 对大房间或“满满当当”需求，`inspect-layout-coverage` 没有 `underused_large_room`、`objects_too_close_to_room_boundary`、`overfilled_room_bounds` warning。
- 所有 floor furniture 的 bbox 都在可用室内区域内；靠墙家具也不能只让 `translate` 在房间内而让实际 bbox 穿墙。
- 每个关键大件都在合理位置：bed/sofa/bookshelf/cabinet 多数靠墙，coffee table/dining table 多数在功能区中心。
- 交互朝向正确：chair 面向 table/desk，sofa 面向 coffee table/TV，TV 面向 seating，bed headboard 靠墙。
- 没有明显穿地、悬浮、桌面嵌入、物体互相重叠。
- 支撑面小物放在正确支撑物上，不直接漂浮，不用 `floor` 伪装桌面摆放。
- 通道可通行，门口、床侧、桌椅前、厨房工作区没有被大件堵死。
- 相机能看到房间主体和关键功能组合，不只拍到墙、屋顶或局部小物。

## 完整度目标

- 客厅：至少 sofa、coffee table、TV/TV stand 或 bookshelf、灯/植物/墙面装饰、小物。
- 卧室：至少 bed、nightstand、lamp、wardrobe/dresser、小物。
- 厨房：至少 counter/cabinet、refrigerator、stove/oven、sink、若干餐具/厨具。
- 书房：至少 desk、chair、bookshelf/cabinet、monitor/laptop、desk objects。
- 餐厅：至少 dining table、chairs、餐具、sideboard/装饰。

## 常见失败模式

- 房间太空：只放 2-3 个大件，没有小物、墙面和功能组合。
- 房间太乱：小物铺满地面或所有物体集中在一个角落。
- 支撑关系错：台灯、杯子、书被放在地上；电视/画框没有挂墙或放在支撑物上。
- 物理支撑错：视觉上有地板/桌面，但没有碰撞 collider，播放后物体掉落。
- 空间覆盖错：背景很大，但家具 translate 只集中在中心几米范围内，四周大片空白。
- 空间覆盖过度：家具 union bbox 几乎撑满背景 visual bbox，导致书架、植物、桌椅贴墙或跑到墙外。
- 朝向错：椅子背对桌子，沙发背对电视，床头朝房间中央。
- 尺寸错：小物巨大化，大件过小，家具比例和房间尺寸不匹配。
- 低保真拼贴：primitive room shell 加若干单体家具，缺少真实背景、材质、灯光和生活化细节。
- 贴图缺失：主家具或背景变成灰白/无材质，通常需要换资产或修复 texture path。
- 资产语义错：把工厂工作台、Fabrica 零件、托盘、机器人默认混进普通家居。

## 可选反馈迭代

- 如果上层 agent 能获取截图，推荐用一张默认透视图加一张俯视图检查。
- 透视图检查真实感、风格、主体是否可见。
- 俯视图检查 XY 布局、通道、家具朝向和拥挤程度。
- 发现问题时优先做少量编辑：移动/旋转/删除/替换/补小物，而不是整场重生成。
