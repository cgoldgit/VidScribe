# VidScribe 项目入口

生成全片笔记前读取 `instructions.md` 和 `prompts/stitcher_system.md`；多分P 再读 `prompts/episode_section.md`。收到 `jobs/` 或 `visual_jobs/` 的独立任务时只执行该任务，不启动全片流程，不通读字幕、旧书稿或派生更多 agent。修改模块时按需读 `ARCHITECTURE.md`。

- 默认输出 Markdown，默认风格为课堂实录：沿讲解顺序轻度整理，保留例子、推导、操作、纠错与结果。正文参考时间统一 `HH:MM:SS`。
- 区分识别错误、讲师知识错误、故意错误示例与有条件的意见。知识错误只有官方资料、具体计算或执行证据支持才修正，正文写 **勘误**；未证实写 **待核实**。保留报错到修复过程，AI 记忆不算证据。只查疑点，不重查重写全片；受限写作任务把外部查证需求交给控制 agent。
- 每个实际 `##` 大章末加 **本章小结**，2—4 句具体结论及适用条件或易错点；跨包只在真实章末总结。省略广告、优惠与导流口播，coverage 说明省略原因；保留教学工具名、中性比较与必要操作。
- 字幕通过检查后运行 `python src/note_plan.py <video_id>` 和 `python src/note_workflow.py prepare <video_id>`。默认采用 B 平衡流程：独立写作任务各读自己的完整原句，主 agent 只读入口、歧义和边界摘要；完整 JSON 供程序核验。边界上下文用于理解，正文避免重复。
- 先记录视觉需求，再读相关索引与必要高清图。`sprite_index.json` 的完整候选清单和概览图是两个范围；概览未展示不表示没有关键画面。通过 `--start/--end --all-cells` 局部细查。
- 原句去向采用 `source_ranges`，由本地程序展开核验；旧 `source_segments` 继续支持。各任务写 `jobs/<P>/draft.md` 和 `review.json`，本地合稿到 `book.assembled.md`、`note_review.assembled.json`。只修改异常段，不二次生成全文；正式产物替换前先完成实验稿验收。
- 图片核查通过独立短任务完成，主会话不接收图片和全文。`visual_confirmations.json` 保存确认结果；来源、索引、图片 SHA 或用途变化时重新核查。不根据模糊小图猜代码、公式、人名或版本。
- 实验稿截图使用 `capture_frames.py --book-name book.assembled.md`，新任务审计使用 `note_audit.py --book-name book.assembled.md --review-name note_review.assembled.json --require-review --require-editorial` 并另存报告。填写事实审阅与 corrections，待核疑点明示，推广线索逐项复核；旧任务缺新字段先给警告。结构错误必须修复，近重复警告按教学内容复核；门禁不能证明语义无遗漏或事实正确，须对照原文核查主要例子。
- 用户要求多个 agent 时，主 agent 管全局计划、术语、合稿和验收；子 agent 仅处理分配的阅读包与相关画面，写独立草稿，避免同时改 `book.md` 和 `note_review.json`。
- 使用项目 `.venv` 的 Python。依赖以 `requirements.txt` 为准。代码修改后运行相关测试，完整回归为 `python tests/run_tests.py`。
- `note_workflow.py run-one` 才会调用现有 CC 模型；每次是新会话，不 resume，默认限制 8 轮、每任务至多 2 次尝试，不自动重试。只返回计数与异常。失败用量保留在 run.json，使用 `note_usage.py` 汇总。真实效果和节省比例须实测；使用说明见 `docs/balanced_workflow.md`。
- 登录配置、cookies 和供应商凭据属于本机数据。提交、推送、发布和大媒体清理按用户授权执行。

项目不绑定模型供应商；使用 CC 接其他模型时仍按相同文件契约工作。
