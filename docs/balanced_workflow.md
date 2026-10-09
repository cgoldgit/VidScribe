# B 平衡流程：保留讲解细节，减少重复上下文

默认风格仍是课堂实录。原句、例子、推导、操作和纠错完整交给写作任务；本地程序处理范围展开、编号、合稿和常规检查。主 agent 管理状态、跨包衔接和异常，不把全片图片和所有草稿重新带入长会话。

## 本地准备

从仓库根目录执行，使用项目虚拟环境。字幕和 B 站索引通过检查后：

```powershell
.\.venv\Scripts\python.exe src\note_plan.py <video_id>
.\.venv\Scripts\python.exe src\note_workflow.py prepare <video_id>
```

`note_plan.py` 默认紧凑展示：每句有稳定 ID 和开始时间，原文逐字保留，完整结束时间、关键词线索和机器元数据留在 `note_plan.json`。`--detail lean` 将候选表改为 TSV，全部定位字段与未展示候选仍保留；`--detail full` 可恢复旧完整展示。仅切换展示不改变来源，已有人工 `note_review.json` 不覆盖。切换已准备任务的展示会改变包 SHA，应使用新的实验目录。

`prepare` 生成 `note_jobs.json` 和每包 `jobs/P001/{task.md,job.json,review.template.json}`。主 agent 只需读取入口和任务状态。来源、阅读包、确认术语或写作规则变化后，旧任务拒绝复用；已有 draft/review 不会被 prepare 覆盖。做同视频对照实验时使用新的 VIDEO_DIR，将 transcript 和可用 sprite/index 复制到实验目录，每个命令加 `--out VIDEO_DIR`。

## 独立写作

每包使用独立上下文。可由外部助手按 `task.md` 填写，也可显式调用当前本机 CC：

```powershell
.\.venv\Scripts\python.exe src\note_workflow.py run-one <video_id> --job jobs/P001
```

这是调用模型并产生费用的命令。脚本不改变模型或供应商配置，不使用 resume/continue，每个任务启动新的 `claude -p` 进程。默认最多 8 轮、每任务至多 2 次尝试；没有自动重试，控制 agent 应先解释并处理失败。`--max-turns`、`--max-attempts` 可显式调整，但不能无限增加。工具只提供 Read/Write/Edit，指定任务产物的写入规则，禁用 MCP 工具和嵌套 agent。实际权限仍由用户的 CC 配置决定，这不是文件系统沙箱。

新写作任务采用 `prompts/writer_system.md`，集中保留正文、事实、推广、小结与来源规则，省去控制流程的信息块、编号、合稿和截图操作说明。任务 SHA 绑定独立规则和模板；旧任务继续验证原规则，重新 prepare 不覆盖已有任务。若旧 job 输入不匹配，使用新的实验目录。

`run-one` 默认 `--input-mode inline`：程序在启动前完整载入规则、确认术语、本包原句和审阅模板，直接放入一次输入，减少模型为取文件反复请求。来源原文不截断，其他包不载入。`--input-mode files` 保留按任务路径读取的方式。视觉任务仍只在需要时读取图片；inline 不把图片编码进文字。公共规则置于变量材料之前，但供应商缓存是否命中仍以实际 usage 为准。

不调用模型的输入检查：

```powershell
.\.venv\Scripts\python.exe src\note_workflow.py inspect <video_id> --job jobs/P001
```

只返回输入字符数、内嵌文件数和输入方式，不输出提示词或原文，不创建 run.json。结构化模式还返回 `prompt_layout`、`shared_prefix_chars` 与 `shared_prefix_sha256`，用于核对各包公共前缀是否一致；哈希一致不代表供应商缓存已命中。

可选结构化返回仅用于完整 inline 文字任务：

```powershell
.\.venv\Scripts\python.exe src\note_workflow.py inspect <video_id> --job jobs/P001 --output-mode structured
.\.venv\Scripts\python.exe src\note_workflow.py run-one <video_id> --job jobs/P001 --output-mode structured
```

此模式使用 CC `--json-schema`，不给模型文件工具。模型返回完整 `draft_md` 和实际 `review`；程序检查字段类型、每句恰好一次、包归属、小节映射、事实记录和章末小结后再落盘。`source_sha256`、`packet_id`、覆盖项的包 ID 和 `editorial_policy_version` 由可信任务补齐，不让模型重复生成；已输出却与任务冲突的常量会被拒绝。corrections 的 display 必须是正文中的完整勘误/待核说明，不能只写标记。真实疑点和未完边界才写数组，没有就用 `[]`。

标准 prepare 生成的任务使用 `compiled` 提示：完整写作规则、固定返回契约、确认术语在前，包 ID、归属范围和完整阅读包在后。程序省去待填模板、文件操作与路径回传说明；原句、边界上下文、全部画面候选、事实和小结要求仍完整载入。若人工修改了 task.md，使用 `compatibility` 布局保留整份自定义任务，避免编译时丢失附加要求。公共前缀改善可缓存性，实际命中仍核对 usage。

默认仍为 `--output-mode files`，视觉任务也继续使用文件方式；structured 与 `--input-mode files` 的组合会在调用模型前拒绝。供应商兼容性、长包的输出上限和语义质量需要按课程验收。结构化失败保留用量及安全错误代码，不写无效主草稿，也不自动重试。`prompt_chars` 不含 schema；`schema_chars` 单独记录 schema 大小，实际费用以完整 usage 为准。

同一个任务的 `run-one`、文字缓存操作和本地恢复共用 `.note_job.lock`，第二个进程会在模型调用前拒绝；不同任务可各自执行。来源、任务或计划在模型运行时改变，会拒绝接收返回结果并保留已消耗用量。已有 `running` 状态或遗留锁不会自动清理；控制 agent 应先确认原进程已停止并检查历史与产物，保留记录后处理，不能据此重复调用模型。

## 失败候选的局部修复

字段结构合格的结构化返回先保存到 `jobs/P001/candidates/A001/`，包含 `draft.md`、`review.json` 和绑定本次输入的 manifest。随后才检查覆盖、事实与小结；通过后生成任务主草稿。字段类型不合格或缺少结构化返回时不保存候选，也不保存原始模型响应和报错文本。`run.json` 的 `validation.code/path` 指明可信字段位置，方便检查缺项或错误归属。

当最后一次结构化尝试因覆盖、小结或落盘问题失败，可对照来源只修该候选中的问题，再执行：

```powershell
.\.venv\Scripts\python.exe src\note_workflow.py recover-writing <video_id> --job jobs/P001
# 如需核对尝试号，只允许最后一次：
.\.venv\Scripts\python.exe src\note_workflow.py recover-writing <video_id> --job jobs/P001 --attempt 1
```

此命令不调用模型，不增加尝试次数。它重新检查原输入、返回字段、每句覆盖、事实记录和小结，通过后记录 `recovery.json` 并将当前状态设为 produced；历史中付费失败的状态与用量继续保留。只有最后一次失败的结构化候选可恢复；来源或任务改变时拒绝。已有主草稿或审阅仅在字节与候选完全一致时接受，允许补齐落盘中断留下的半对文件，人工编辑产物不覆盖。修复后仍须合稿、对照来源及完成图片核查。

恢复候选的 manifest 保留初始产物哈希，局部修改会记 `candidate_edited`。`note_usage.py` 分开报告 `recovered_jobs`，恢复后再修改主稿、审阅或任务元数据会标 `changed_after_recovery`，旧的付费用量照常统计。它不推算没有实际调用的重试费用。

任务只写 `draft.md`、`review.json`，结果向主 agent 返回路径、歧义和边界提示。不要回传全文，不让主 agent 再读一次全片字幕。确认过的课程术语可放在 `TERMS.confirmed.md`；有疑问的词仍保留原词和来源，不猜版本、数字或人名。

整理时去除无教学价值的广告、优惠、赞助宣传和关注入群口播，对应范围记 `omitted` 并说明理由；保留工具名、中性比较、必要的扫码操作和混在推广中的教学解释。每个实际 `##` 大章末写 **本章小结**，2—4 句具体结论和适用条件或易错点。跨包的大章在 `boundary_notes` 标明尚未结束，控制 agent 只在真实章末补一次小结，原例子和推导不能缩成小结。

检查识别错误、讲师知识错误、故意错误示例和意见的条件，采用 `prompts/stitcher_system.md` 的证据规则。`run-one` 写作工具没有联网或执行工具，不能声称已完成外部事实核查；有疑点时记录 `unresolved`，正文写 **待核实**。控制 agent 只针对这些疑点查适用的官方或原始资料、做具体计算或安全执行验证；证实错误后局部修稿，正文写 **勘误** 说明原说法、修正、条件和证据。AI 记忆不算证据，教学中的故意错误与修复过程仍保留。

`draft.md` 使用未编号的 `##`、`###` 标题；每个标题前可写稳定锚点：

```markdown
<!-- section:P001.s01 -->
## 演示一个例子

例子的条件、操作和结果。

**待核实**：这一说法的适用版本尚不明确，需核对对应官方说明。

![待核的界面用途](SCREENSHOT:00:01:20)

**本章小结**：操作完成后，应结合实际反馈判断是否成功。若出现错误，先核对本章演示的输入条件，再按已说明的步骤修正。
```

`review.json` 的 source_sha256 来自本任务模板。核心示例：

```json
{
  "source_sha256": "复制任务模板的实际SHA",
  "packet_id": "P001",
  "coverage": [
    {"packet_id":"P001","source_ranges":[["S000001","S000025"]],"status":"written","section_id":"P001.s01","reason":""},
    {"packet_id":"P001","source_ranges":[["S000026","S000026"]],"status":"omitted","section_id":"","reason":"重复口癖，没有信息增量"}
  ],
  "visual_requests": [
    {"time":"00:01:20","status":"pending","reason":"读者需要看到操作后的界面反馈","section_id":"P001.s01"}
  ],
  "visual_review": "pending",
  "visual_review_reason": "已提出本包配图需求，尚未看图",
  "fact_review": "complete",
  "fact_review_reason": "已检查本包事实疑点；下列项证据不足，正文保留待核标记，待控制 agent 查证",
  "corrections": [
    {
      "kind": "lecture_error",
      "status": "unresolved",
      "section_id": "P001.s01",
      "source_ranges": [["S000010", "S000011"]],
      "original": "原讲解中有疑问的具体说法",
      "replacement": "",
      "display": "**待核实**：这一说法的适用版本尚不明确，需核对对应官方说明。",
      "evidence": []
    }
  ],
  "uncertainties": [],
  "boundary_notes": []
}
```

范围包含两端，必须只属于当前包，每句恰好一次。旧 `source_segments` 仍支持；一条不能同时提供列表和范围。省略须有理由，写入须有本包实际标题。跨包例子尚未完成时记录 `boundary_notes`，主 agent 对照边界原句接续，不能压成一句小结。

上面事实记录是字段示例：`display` 必须逐字出现在对应正文中，且含 **勘误** 或 **待核实**；实际任务只能填写实际发现的问题。`kind` 为 `lecture_error` 或 `transcription`，`status` 为 `verified` 或 `unresolved`，小节用 `section_id`（旧流程可用实际 `section` 标题）。已核实项的 `replacement` 要写具体修正，`evidence` 非空，条目使用 `kind: reference|calculation|execution`、`detail`，参考网页另附实际 `url`。未解决项不能假装有证据，也不能空口写「已查证」。明显识别错字可直接还原，影响结论的识别歧义须留记录。没有发现疑点时 `corrections: []`，说明检查范围，不编造更正。

`fact_review` 初始为 `pending`。完成疑点分类、核查或明确的待核展示后才能标 `complete`，`fact_review_reason` 说明检查范围与局限。`complete` 表示审阅处理完成，允许如实存在 `unresolved`，不等于所有内容都已验证正确；交付报告继续列出待核项。

## 已验收文字包的复用

控制 agent 对照本包完整原句确认例子、条件、操作、推导和结果得到保留，再将已验收的包入缓存。以下标志记录控制 agent 已完成来源审阅，不能仅凭 produced、coverage 或绿灯测试就填写：

```powershell
.\.venv\Scripts\python.exe src\note_workflow.py cache-writing <video_id> --job jobs/P001 --accept-source-review
```

缓存仅接收事实审阅已处理、章节闭合、没有已物化图片的文字包；未核图状态与待核事实照实保留。跨包未结束的大章须先完成衔接验收，此类包不能绕过小结检查入缓存。默认保存到被版本控制忽略的 `output/.note_writing_cache/`，可用 `--cache-dir <目录>` 指定独立实验缓存。

字幕修订后在新的输出目录重新分包、prepare，再尝试复用：

```powershell
# 只查缓存，不调用模型：
.\.venv\Scripts\python.exe src\note_workflow.py reuse-writing <video_id> --out <新目录> --job jobs/P001
# 命中直接复用；未命中才按本次参数调用一次模型：
.\.venv\Scripts\python.exe src\note_workflow.py run-one <video_id> --out <新目录> --job jobs/P001 --reuse-cache
```

缓存键绑定视频、标题、包 ID、本包全部内容、前后相邻包、句子时间与归属、候选索引、术语、写作规则、任务文字和审阅模板；只排除总字幕 SHA 和搬迁产生的目录路径。本包或邻包变化、重新编号、时间变化和规则变化都会失效。只有全部内容一致且缓存校验和正确，程序才能把旧来源 SHA 重新绑定到新任务。既有草稿/审阅不覆盖，模型仍在运行时禁止缓存操作，损坏缓存会明确报错。

复用记录保存为 `reuse.json`，保留旧来源、缓存键和落盘结果校验和；模型尝试历史仍保留。`note_usage.py` 单独报告历史 `reused_jobs`，复用不冒充一次零 token 的模型调用，也不推算未测得的节省费用。复用后改稿会标记 `changed_after_reuse`，单独计入 `changed_reused_jobs`，当前稿件需要重新对照来源；既有模型用量照常统计。缓存改善修订与续跑，首次生成仍逐包读取完整来源。

同一输入发现事实或表达问题后，先修正文和审阅并重新对照来源，再用 `cache-writing --accept-source-review --replace-cache` 更新已验收缓存；不加 `--replace-cache` 时拒绝覆盖不同的旧缓存。缓存保留前一版产物校验和用于追踪，正式稿和其他任务的已有文件仍不受影响。

## 本地合稿与局部核查

```powershell
.\.venv\Scripts\python.exe src\note_workflow.py assemble <video_id>
```

生成 `book.assembled.md`、`note_review.assembled.json`、`assembly_summary.md`。程序按清单合稿、编号、映射小节，并检查来源、范围覆盖和标题归属。先完成全部输入检查再写实验稿，正式 `book.md` 和 `note_review.json` 不在本步骤写入范围。

主 agent 读取 summary，再检查提示的跨包边界、歧义、术语冲突和主要例子；只修问题段落。修改小节后同步审阅映射；重新合稿会从各任务 draft 重建实验稿，正式成品仍须另行验收。如果需要自定书名与信息块，可在合稿前写 `book_header.md`。

## 独立图片核查与复用

先对实验稿截取候选帧，保留原图供复核：

```powershell
.\.venv\Scripts\python.exe src\capture_frames.py <video_id> "<VIDEO_URL>" --book-name book.assembled.md
.\.venv\Scripts\python.exe src\note_workflow.py visual <video_id> --batch-size 4
```

浏览器不可用时，兜底支持同样的 `--book-name`、`--out`：

```powershell
.\.venv\Scripts\python.exe src\extract_frames.py <video_id> "<VIDEO_URL>" --book-name book.assembled.md
```

截图物化只影响选中的稿件及其 `book.assembled.tagged.md` 副本。`visual` 本地生成 `visual_jobs.json`，每批默认 4 张相关候选，最多允许 12 张；这限制一次输入，不限制全片必须处理的图片总数。缺帧记录 missing/pending，不能据此宣称完成。按 manifest 的 job 路径执行：

```powershell
.\.venv\Scripts\python.exe src\note_workflow.py run-one <video_id> --job visual_jobs/V<实际hash>
.\.venv\Scripts\python.exe src\note_workflow.py apply-visual <video_id>
```

视觉任务只读取这一小组图片、预期用途，输出本目录 `confirmation.json`：

```json
{
  "source_sha256": "复制task.md的实际SHA",
  "images": [
    {"time":"00:01:20","image_sha256":"复制该帧的实际SHA","status":"keep","description":"只填写能从图中确认的可见内容","reason":"界面反馈与本段演示相符","uncertainties":[]}
  ]
}
```

每张输入必须恰好有一个决定。`keep` 需要已确认图注；`omit` 说明理由；`uncertain` 继续 pending，不猜代码、公式、型号或数字。图片小变化可能有教学意义，不自动因相似而删除。

`apply-visual` 把确认结果写入 `visual_confirmations.json` 并更新实验稿和实验审阅。缓存绑定来源、索引、图片 SHA、请求用途和小节。图变了或用途变了，过去的 keep/omit 都要重审；被省略图片的注释位置保留，方便新确认恢复。任何包未审阅且没有处理需求，整本仍为 pending；空列表不能成为无漏图证明。

主 agent 读取确认摘要与异常，不把全部图片重新读入会话。确实需要新细节时才请求相关高清图或局部细查。这个缓存是已生成的检查结果复用，与供应商的输入 token 缓存是两种机制。

## 验收、正式产物与用量

先审计实验稿并另存报告：

```powershell
.\.venv\Scripts\python.exe src\note_audit.py <video_id> --require-review --require-editorial --book-name book.assembled.md --review-name note_review.assembled.json --report output\<video_id>\note_audit.assembled.json
.\.venv\Scripts\python.exe src\note_usage.py <video_id>
```

结构检查通过后仍需处理相似图片警告、跨包例子与内容待核项。零错误不能证明语义完整；预算达到上限也不能宣布完成。控制 agent 验收后再把实验稿和审阅记录作为正式文件，已有成品先保留可恢复副本，并执行默认审计。不会自动发布或渲染 HTML。

`--require-editorial` 是新任务编辑验收要求：每章有非空小结，事实审阅已处理，勘误或待核记录与正文对应，已核实项附证据。程序仅检查结构，无法证明来源的真实性及其与结论的关系。推广词只形成待审警告，由控制 agent 判断是否广告或教学内容；不自动删句或图片。旧任务缺新字段在不带该参数时兼容为警告，补齐后再按新规则验收。只修改疑点所在章节与审阅，不新增全片复读重写循环。

每个自动任务的 `run.json` 保存当前状态与尝试 history，包括失败已经消耗的 usage。`produced` 只表示文件非空，不等于 validated；限制超出、模型报错或缺产物都作为失败处理。日志仅保存计数和失败类型，不存模型正文、原始 stdout/stderr；未知用量不能解释为零。继续运行、缓存或恢复前检查尝试编号为正整数、唯一且不超出当前编号；历史与当前记录的用量、费用冲突时拒绝继续。初始尚未调用的任务允许 0 次；旧缺失 history 时保留上一付费快照再执行。

`note_usage.json` 汇总写作和视觉任务的各次尝试，区分未缓存输入、缓存读取、缓存创建和输出，不将 total_tokens 再叠加。提供每个计数的完整性及已知尝试数，费用仅标估算，以供应商实际账单为准。手工任务没有自动 usage，仍需单独收集。进程在返回 usage 前中断，已发生的费用可能无法恢复，报告会保留未知。

新 run.json 另存 `elapsed_seconds`、`num_turns`、`prompt_chars`、`embedded_inputs`、`input_mode` 和限制值。失败用量照常汇总；安全的 `failure_code` 区分轮次上限、模型错误、缺产物和超时，不保存原始报错。旧日志缺这些指标时为未知。`elapsed_seconds_sum` 是各尝试时长相加，并发时不代表项目墙钟总时长。失败后已存在文件时先验收或局部修复，不因 failed 状态直接重跑整包。

离线基准不调用模型、不改源视频目录；可选使用本地 `tokenizer.json`（需要 `tokenizers`，不是核心运行依赖），不指定则仅测字符和准备时间：

```powershell
.\.venv\Scripts\python.exe src\note_benchmark.py <video_id> --report output\benchmark_report.json
# 本地分词器已存在时：
.\.venv\Scripts\python.exe src\note_benchmark.py <video_id> --tokenizer <tokenizer.json> --report output\benchmark_report.json
```

报告必须写在源视频目录之外。分词估算不含 CC 系统/工具提示、历史或图片，不能用作实际费用。2026-10-08 的本地计时与小型模型对照见 `docs/performance_review_2026-10-08.md`。

## 已验证范围

新增流程用临时字幕、本地图像、fake CC runner 测试覆盖、路径、旧产物保护、失败历史及缓存失效。2026-10-08 已跑一次 13 句合成输入的 CC 对照，优化方式正常生成且能合稿；没有重生 100 分钟视频。真实视频的视觉质量、整片节省比例和供应商实际收费仍未验证。

2026-10-08 完整回归为 263 项通过、0 失败、1 项跳过，修复与测量细节见性能报告。跳过的是本机无创建文件符号链接权限的实际路径测试；常规路径越界和输入保护测试已通过。真实样本的独立任务准备已离线验证，16 包完整原句均保留，原笔记与审阅记录哈希未变。

第二轮实现结构化返回和文字缓存后，完整回归为 277 项通过、0 失败、1 项跳过。另完成一次 13 句真实模型对照，以及真实视频单点修订的缓存失效测试；结果与质量边界见 [第二轮优化报告](performance_optimization_v2_2026-10-08.md)。

第三轮参考开源项目实现公共提示编译、失败候选局部恢复、安全字段诊断和同任务锁。完整回归为 333 项通过、0 失败、3 项跳过，跳过项为本机无法创建实际符号链接的路径测试。16 包、2,552 句真实字幕完整保留，结构化 prompt 的离线分词估算从 99,719 降至 90,775（约 9%）；这不包含 schema、CC 系统上下文、历史或图片。13 句模型样本完成来源核对，严格审计仍如实标记视觉未核。详细用量、研究来源和回归结果见 [第三轮优化报告](performance_optimization_v3_2026-10-08.md)。

本机已只读确认 CC 2.1.224 以及已有 `--output-format`、`--no-session-persistence`、工具和权限参数；具体模型兼容与收费按本机配置。参数概念参考 [Claude Code CLI 文档](https://code.claude.com/docs/en/cli-reference)，上下文和缓存计费参考 [成本文档](https://code.claude.com/docs/en/costs)。
