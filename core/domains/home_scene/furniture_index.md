# Furniture And Object Index

这个索引用于把中文/自然语言需求转成资产检索 query。优先查 `geniesim` 和 `isaac_official`；未来如果有 curated home assets，可先查 `curated_common` 或新增 `curated_home`。

## 检索策略

- 大件家具：使用英文核心词加同义词，例如 `sofa couch living room furniture`。
- 小物：先检索常见类别，不要一开始就用很长描述。例如先搜 `mug`，再搜 `ceramic mug cup`。
- 如果 query 命中多个风格，选择尺寸、语义、材质最接近房间风格的资产，并优先选择 `inspect-asset-health` 通过的资产。
- 如果真实资产找不到，优先换相近真实资产；只有墙、地板、简单屏幕、简单装饰板、简单盒体这类几何物才适合 primitive fallback。
- 不要把工业工作台、托盘、Fabrica fixture 默认用于家居场景，除非用户明确要求“仓库里摆家居”或“工业风家居”。
- 主家具检索时先搜短 query，再用 asset health、bbox、类别过滤，不要因为名字命中就直接采用。
- 大件家具不要任意极端缩放；若需要调尺寸，先用 bbox 判断真实尺寸，再保持等比例或小幅缩放。

## 常用类别

| 需求 | 推荐 query | placement | 说明 |
| --- | --- | --- | --- |
| 沙发 | `sofa couch living room` | floor | 通常靠墙或面向电视/茶几 |
| 茶几 | `coffee table living room` | floor | 放在 sofa 前方，保留腿部空间 |
| 电视 | `television tv screen` | wall/support_object | 壁挂用 wall，放电视柜上用 support_object |
| 电视柜 | `tv stand media console cabinet` | floor | 靠墙，面向 sofa |
| 书架 | `bookshelf bookcase shelf` | floor | 通常靠墙，内部/上方可放 books/decor |
| 床 | `bed bedroom` | floor | 床头靠墙，床侧留通道 |
| 床头柜 | `nightstand bedside table` | floor | 靠床侧，通常放 lamp/book |
| 衣柜 | `wardrobe closet dresser cabinet` | floor | 靠墙，不挡门 |
| 书桌 | `desk office table study` | floor | 椅子面向桌面 |
| 办公椅 | `office chair desk chair` | floor | 放在 desk 前，面向 desk |
| 餐桌 | `dining table` | floor | 居中或靠功能区中心 |
| 餐椅 | `dining chair chair` | floor | 围绕餐桌，面向餐桌 |
| 橱柜/台面 | `kitchen counter cabinet` | floor | 靠墙/L形/U形 |
| 冰箱 | `refrigerator fridge` | floor | 靠墙，不堵通道 |
| 炉灶/烤箱 | `stove oven cooktop` | floor/support_object | 与 counter 组合 |
| 水槽 | `sink kitchen sink bathroom sink` | support_object/floor | 与 counter/vanity 组合 |
| 台灯 | `lamp table lamp desk lamp` | support_object | 放在 desk/nightstand/side table 上 |
| 落地灯 | `floor lamp standing lamp` | floor | 沙发/阅读角旁 |
| 植物 | `plant indoor potted plant` | floor/support_object | 角落或桌面 |
| 画/相框 | `picture frame wall art painting` | wall | 挂墙，不要放地上 |
| 时钟 | `wall clock clock` | wall | 通常挂墙 |
| 书 | `book books` | support_object | 放书架、桌面、床头柜 |
| 杯子 | `mug cup glass` | support_object | 桌面/厨房台面 |
| 餐盘 | `plate bowl dish` | support_object | 餐桌/厨房台面 |
| 键盘/显示器 | `keyboard monitor computer screen` | support_object | 书桌上 |
| 地毯 | `rug carpet living room` | floor | 放在 sofa/coffee table 功能区下方，提升家居感 |
| 窗帘 | `curtain drape window` | wall | 靠窗/墙面，不要漂浮 |
| 镜子 | `mirror wall mirror` | wall/floor | 卧室/浴室/玄关 |
| 柜面装饰 | `vase decor ornament` | support_object | 放在柜、桌、书架上 |

## 中文映射

- 客厅：living room、sofa、coffee table、TV、TV stand、bookshelf、plant、floor lamp。
- 卧室：bedroom、bed、nightstand、wardrobe、dresser、lamp、mirror。
- 厨房：kitchen、counter、cabinet、refrigerator、stove、sink、plate、mug、pan。
- 书房：study、home office、desk、office chair、bookshelf、monitor、keyboard、book。
- 餐厅：dining room、dining table、dining chair、sideboard、plate、cup。
