# VidScribe 项目入口

生成笔记前读取 `instructions.md` 和 `prompts/stitcher_system.md`；多分P 再读 `prompts/episode_section.md`。修改模块时按需读 `ARCHITECTURE.md`。这些详细文件按任务读取。

- 默认输出 Markdown，默认风格为课堂实录：沿讲解顺序轻度整理，保留例子、推导、操作、纠错与结果。正文参考时间统一 `HH:MM:SS`。
- 字幕通过检查后运行 `python src/note_plan.py <video_id>`，读轻量 `note_plan.summary.md` 后逐包读完整原句，完整 JSON 供程序核验。边界上下文用于理解，正文避免重复。关键词线索不能替代阅读原句。
- 先记录视觉需求，再读相关索引与必要高清图。`sprite_index.json` 的完整候选清单和概览图是两个范围；概览未展示不表示没有关键画面。通过 `--start/--end --all-cells` 局部细查。
- 将原句去向和配图处理记录到 `note_review.json`。已有审阅继续编辑，来源变动重新核对。不得凭字数、图数或空需求清单宣布完成。
- 对图的确认说明写入审阅，后续引用已确认信息，避免反复读取同一高清图。不根据模糊小图猜代码、公式、人名或版本。
- 图片物化后运行 `python src/note_audit.py <video_id> --require-review`。结构错误必须修复，近重复警告按教学内容复核；门禁不能证明语义无遗漏，须对照原文核查主要例子。
- 用户要求多个 agent 时，主 agent 管全局计划、术语、合稿和验收；子 agent 仅处理分配的阅读包与相关画面，写独立草稿，避免同时改 `book.md` 和 `note_review.json`。
- 使用项目 `.venv` 的 Python。依赖以 `requirements.txt` 为准。代码修改后运行相关测试，完整回归为 `python tests/run_tests.py`。
- 真实平台效果、模型 usage 与 token 节省需实际验收；无法核查时说明限制。可选方案见 `docs/quality_options.md`。
- 登录配置、cookies 和供应商凭据属于本机数据。提交、推送、发布和大媒体清理按用户授权执行。

项目不绑定模型供应商；使用 CC 接其他模型时仍按相同文件契约工作。
