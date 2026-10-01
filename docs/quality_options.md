# 笔记质量、配图覆盖与多模态成本方案

研究日期：2026-10-01。目标是课堂实录式笔记：读者能沿着讲解顺序理解例子、推导、操作和结果，同时控制 CC 工作流中的视觉输入。本文区分当前证据、候选方案和需要真实样本验证的效果。

## 当前证据与问题边界

已有流程是字幕 → Bilibili 预览图册索引 → agent 写正文并放截图占位符 → 浏览器截取高清图片 → 物化 Markdown。`capture_frames.py` 已处理媒体等待、seek、稳定帧选择、空白图与失败记录。这些能力能降低截取失败，但不能保证写作 agent 已选中所有应该出现的画面。

初始代码的 `sprite_index.py` 默认 `--grid 3 --max-sheets 2`，最多展示 18 个代表格；超额变化点会并入相邻段。把视觉输入预算同时用作候选保留上限，会让部分变化失去独立入口。`merged` 提供了风险提示，却不能告诉 agent 被合并的变化是否包含关键图解。

桌面现有样本 `output/BV1Rch76WEfQ/sprite_index.json` 已扩大到 `max_sheets=14`：703 个原始采样格变成 126 个关键格。主 agent 的文件核查显示正文有 29 个图片引用，未发现引用文件缺失或字节完全相同的图。用户仍反馈漏图和重复，说明“文件存在且字节不同”不足以证明“重要画面已覆盖且内容不重复”。不能把该样本的问题完全归因于默认 18 格上限；需要检查 agent 选图、语义近重复以及正文关联。

预览图册是平台采样结果，不能重建两次采样之间从未出现过的短暂画面。480×270 的格子也不足以可靠抄录小字、代码和公式。后文方案都保留这两个限制。

## 方案选择

| 方案 | 输入与处理 | 收益方向 | 成本和限制 |
|---|---|---|---|
| A：最低视觉成本 | agent 主要读分段字幕与时间信息；工具生成配图候选，只有不确定图解才读取图片 | 视觉调用最少，易在 CC 中运行 | 不能依靠字幕还原图中关系；纯文本选择会漏掉字幕未描述的图 |
| B：成本与质量平衡，优先实施 | 保留完整候选清单；按章节分批展示小索引；先做图文计划，再写课堂实录；最终图片做缺失与近重复报告 | 把发现候选和 agent 看图的预算分开；有机会追查漏图与重复原因 | 依然需要视觉确认；不保证所有图都被识别，需真实课程验收 |
| C：本地 OCR 辅助 B | 本地识别文字与框位置并缓存；字幕和 OCR 文本供 CC 阅读；无文字图、低置信度图、难辨代码再看高清局部 | 同模板换文字更容易被发现；减少反复读取整图 | 新增运行库与模型；OCR 错字、公式和图形关系仍需核实 |
| D：本地视频扫描实验 | 从本地媒体密集采样，先做变化检测，再做稳定区间、内容聚类和复核 | 对短暂出现的页更有发现机会；平台无图册时可用 | 需要视频文件、解码时间和调参；场景切分与完整图解识别是不同工程问题 |

B 应作为当前默认方向，C 作为后续可选能力，D 先用小样本验证。A 可以作为明确接受视觉信息损失的省钱模式。没有实测 API usage 前，不能给出 token 节省百分比，也不能把索引图数量当作 token 成本。

## B 的具体工作流

1. **工具保留候选，agent 分批读取。** 候选记录包含稳定 ID、原始时间、所属区间、变化程度、被合并数量与来源。展示预算只限制某一批给 agent 看多少图。长区间、明显变化和字幕提到“这张图”“看结果”的位置进入待复核清单。
2. **先做图文计划。** 每个教学单元记录字幕起止、定义、例子、推导或操作、结果、应配图 ID、为什么需要这张图。没有可用图时记录证据不足，不能凭字幕编造时间。整段看完再决定哪些图应进入正文。
3. **按教学单元写正文。** 保留讲师展开的过程，例如输入是什么、为什么做这一步、发生了什么、结论如何得到。每个主要例子都要交代具体条件与结果。导读与小结可以简短，正文不能再次变成同一份摘要。
4. **按计划截高清帧。** 实际截取时间必须保留，避免稳定帧偏移后只留下原始请求时间。代码、公式、小字以高清图为证据；看不清时不要补出貌似完整的内容。
5. **工具检查，agent 复核。** 检查占位符、文件缺失、同一候选重复使用、图文关联与近重复图片。近重复只产生待复核项；同一背景上增加一行代码、公式下标或箭头可能就是新的教学步骤，不能自动删。
6. **保留复用信息。** 图的确认结果和局部文字读取结果写入缓存；写作、检查和改写阶段通过 ID 引用证据，避免各 agent 反复打开相同高清图。缓存键需要绑定源文件或内容哈希、工具版本和配置，避免陈旧结果被误用。

CC 可担任主 agent，按章节分配“材料核对”“正文写作”“覆盖复核”。每个 subagent 只接收自己的章节材料与帧 ID；主 agent 管全局目录、术语和跨章重复。多个 agent 同时读取整片字幕与整套高清图会增加重复上下文，不能仅靠并行降低成本。

## GitHub 可借鉴来源

以下仓库均已查看实际实现。维护日期取自 GitHub `commits/HEAD.atom` 的首条记录；它只证明该时间有提交，不证明功能在本项目视频上有效。未运行其完整管线，也未采用作者 README 中的准确率数字作为验收结果。

| 来源 | 许可证与核查到的最近提交 | 已确认的实现与可移植思想 | 限制与接入方式 |
|---|---|---|---|
| [PySceneDetect](https://github.com/Breakthrough/PySceneDetect) | BSD-3-Clause；[2026-09-21 提交](https://github.com/Breakthrough/PySceneDetect/commit/81c414cb4b706e58648f98efd381024790b1565f) | [`AdaptiveDetector`](https://github.com/Breakthrough/PySceneDetect/blob/main/scenedetect/detectors/adaptive_detector.py) 比较局部变化与邻域均值，配合最低内容变化和最短场景长度；可用于 D 的候选区间 | 颜色变化小的同模板幻灯片可能漏检；动画、镜头移动也会触发。只负责候选，不能充当知识点覆盖检查 |
| [video-slide-extractor](https://github.com/larry-xue/video-slide-extractor) | MIT；[2026-09-06 提交](https://github.com/larry-xue/video-slide-extractor/commit/221454d1cc296e75a2ddf83cd3ed587d5922c232) | [`index.js`](https://github.com/larry-xue/video-slide-extractor/blob/master/index.js) 实现分块差异、持续运动区域屏蔽和阈值校准；[`cli.js`](https://github.com/larry-xue/video-slide-extractor/blob/master/cli.js) 的 `grabPoint` 在页停留区间内取帧并限制到下一页中点 | 核心未实现非相邻重复图消除或 OCR。手写白板、游戏和持续运动画面效果弱；可借鉴局部运动处理，不必引入 Node 管线 |
| [slide-extractor](https://github.com/liaw-boy/slide-extractor) | MIT；另有说明源内容使用边界的 [NOTICE](https://github.com/liaw-boy/slide-extractor/blob/main/NOTICE)；[2026-05-18 提交](https://github.com/liaw-boy/slide-extractor/commit/2cfe615014e04d3572eebfbc61cf426448328023) | [`slide_extractor.py`](https://github.com/liaw-boy/slide-extractor/blob/main/slide_extractor.py) 用中文双字组与英文词的 OCR 内容聚类，为每组选文字量较多的代表，含 OCR 缓存；[`slide_review.py`](https://github.com/liaw-boy/slide-extractor/blob/main/slide_review.py) 支持复核候选 | 实际主路径是 OCR 聚类，不能把文件开头旧说明中的 pHash 规则当主算法。短停留、低文字量、纯图与相关内容同词汇会出错；借鉴复核入口与内容差异信号，不能照搬全局聚类删图 |
| [RapidOCR](https://github.com/RapidAI/RapidOCR) | Apache-2.0；模型来源另见仓库 `MODEL_LICENSES.md`；[2026-09-23 提交](https://github.com/RapidAI/RapidOCR/commit/549847df506a32421086d2a08d19646ee958a6e0) | [`python/rapidocr/main.py`](https://github.com/RapidAI/RapidOCR/blob/main/python/rapidocr/main.py) 输出文字、框坐标与置信度；[`config.yaml`](https://github.com/RapidAI/RapidOCR/blob/main/python/rapidocr/config.yaml) 提供 ONNX Runtime 等引擎配置；可为 C 提供本地文字证据 | OCR 可帮助发现不同文字，无法理解箭头关系、曲线含义或证明步骤；优先识别高清候选，预览格仅作线索。首次使用涉及依赖和模型下载，应作为可选功能单独验证 |

[LectureLens](https://github.com/r0mar1n/LectureLens) 的分阶段产物与 [`revisit_detector.py`](https://github.com/r0mar1n/LectureLens/blob/main/src/revisit_detector.py) 也有参考价值：先比较直方图，再用像素相似度确认回看页。不过此次未找到明确许可证，其生成器还调用 Gemini，因此只参考阶段拆分思路，不直接引入代码，也不推荐作为本项目的省 token 完整替代。

## 验收与实现边界

先用用户指出的漏图和重复图标出一份小规模人工基准，再比较修改前后的结果。至少覆盖同模板换公式、PPT 逐条动画、讲师小窗、回看旧页、代码运行前后、纯图与短暂页面。

| 指标 | 如何核查 |
|---|---|
| 关键画面召回 | 人工标注应出现的教学画面，确认它是否进入候选、计划和最终笔记；分别定位在哪一步丢失 |
| 语义重复 | 人工检查相似图是否包含新的教学变化；SHA 相同率只能检查字节重复 |
| 课堂过程保留 | 检查指定例子的条件、步骤、中间推导和结果是否仍能从笔记独立理解 |
| 证据准确 | 随机回看图片实际时间、正文所述内容与原视频；文件存在不能替代该检查 |
| 成本 | 记录模型、章节数、视觉读取次数、图尺寸、输入输出 token、缓存命中与运行时间；使用真实 usage 对比 |

本文是方案与来源研究记录。OCR、完整视频扫描、自适应运动屏蔽、人工选图 UI、真实模型 token 统计仍是候选能力，不因写入本文而视为已实现。当前代码改动和离线测试是否完成，以项目变更及测试报告为准；真实平台配图召回与笔记可读性必须由真实样本验证。
