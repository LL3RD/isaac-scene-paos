# Home Background Catalog

这个文件给上层 agent 选择家居/室内背景。家居场景优先复用已有完整背景；只有没有合适背景或用户明确要求“从空房间开始”时，才生成 primitive room shell。

## 选择原则

- 优先级 1：用户明确指定的背景路径或 asset_id。
- 优先级 2：GenieSim 已有室内背景，例如 `home_*`、`study_room`、`kitchen`、`dining_room`、`office`。
- 优先级 3：Isaac 官方可用室内环境，如果已经下载并能被检索到。
- 优先级 4：primitive floor/walls fallback。只能用于快速草图或资产库确实没有合适背景的情况，必须在结果里记录 `background_quality: "primitive_fallback"`。

## 已知 GenieSim 背景

| 场景类型 | 推荐背景 | profile-relative selector | 用法建议 |
| --- | --- | --- | --- |
| 通用家居 | `geniesim_home_1` | `background/home/home_1/background.usda` | 可作为普通住宅基础，生成前先检查内容和相机 |
| 通用家居 | `geniesim_home_2` | `background/home/home_2/background.usda` | 可作为普通住宅基础，适合补充客厅/卧室家具 |
| 通用家居 | `geniesim_home_3` | `background/home/home_3/background.usda` | 有 common home 引用，优先用于家居真实感测试 |
| 通用房间 | `geniesim_room_1` | `background/room/room_1/background.usda` | 比 primitive room shell 更好，适合从空房间布置 |
| 厨房 | `geniesim_kitchen_1` | `background/kitchen/kitchen_1/background.usda` | 厨房场景优先选它，再补充餐具/台面小物 |
| 厨房 common | `geniesim_common_kitchen` | `background/common/kitchen/kitchen.usd` | 可作为厨房背景或补充资产 |
| 餐厅 | `geniesim_dining_room_01` | `background/dining_room/dining_room_01/background.usda` | 餐厅/餐厨一体场景优先；不要作为普通客厅首选 |
| 书房 | `geniesim_study_1` | `background/study_room/study_1/background.usda` | 家庭办公室、书房场景优先 |
| 书房 | `geniesim_study_2` | `background/study_room/study_2/background.usda` | 可替代 `study_1` |
| 书房 | `geniesim_study_3` | `background/study_room/study_3/background.usda` | 可替代 `study_1` |
| 书房 | `geniesim_study_4` | `background/study_room/study_4/background.usda` | 可替代 `study_1` |
| 办公室 | `geniesim_office_00` | `background/office/office_00/background.usda` | 家庭办公室、办公环境 |
| 办公室 common | `geniesim_common_office` | `background/common/office/Aligned.usd` | 可作为 office 背景或补充 |

## 生成前检查

运行：

```bash
python3 scripts/scene_tools.py list-home-backgrounds --room-type living_room
python3 scripts/scene_tools.py inspect-asset-health /path/to/background.usda
```

检查结果中若 `missing_references` 或 `missing_textures` 很多，优先换另一个背景，不要直接用坏背景生成。

## 背景与家具的关系

- 如果背景本身已经带家具，先复用背景已有的家具，只增补缺失功能区或小物。
- 如果背景是空房间，按 `room_templates.md` 和 `layout_rules.md` 补大件家具。
- 如果背景里已经有同类大件，例如已有 sofa/bed/counter，不要重复放同样的大件，除非用户要求更复杂/更满。
- 家居背景不要混入工业工作台、Fabrica 定位板、机器人，除非用户明确要求跨域场景。
