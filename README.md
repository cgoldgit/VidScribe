# VidScribe

VidScribe 是一个由 AI 助手编排的视频内容整理工具，支持将 YouTube 和 Bilibili 视频转换为结构化的 Markdown 文档，并可按需生成可在浏览器中阅读的 HTML 页面。生成结果可包含关键画面、视频时间锚点和内嵌播放器，适合整理课程、技术讲解和操作演示类视频。

项目负责字幕提取、画面索引、截图、HTML 渲染和发布；AI 助手根据字幕与画面完成内容重构。完整执行规则见 [instructions.md](instructions.md)，模块设计与数据流见 [ARCHITECTURE.md](ARCHITECTURE.md)。

默认笔记采用课堂实录风格，沿讲解顺序保留例子、推导、演示和结果。字幕先由本地工具分成完整阅读包，agent 按包写作并记录来源去向；图片按视觉需求查看，概览展示预算与完整候选目录分开，避免为了减少读图而丢掉候选。成品检查会报告缺图、待处理需求、含糊时间和相似图片，语义完整性仍需对照来源复核。

Claude Code 的项目入口是 [CLAUDE.md](CLAUDE.md)。可直接要求它按 `instructions.md` 生成课堂实录式笔记，并完成 `note_review.json` 与 `note_audit.py --require-review` 检查。不同成本与质量方案、GitHub 参考和工程限制见 [quality_options.md](docs/quality_options.md)。

## 默认输出格式

VidScribe 默认生成 Markdown 成品，不自动渲染 HTML，也不启动本地网页预览。Markdown 中如有关键画面，成品还会引用同目录下的 `images/`，因此交付内容可能包括 `book.md` 和配套图片。

只有在提示词中明确要求 HTML、浏览器预览或 GitHub Pages 发布时，工作流才会继续生成 `book.html`。例如，只需要 Markdown 时可以这样提交任务：

```text
请按照 instructions.md 将下面的视频整理成一份结构清晰的 Markdown 文档，保留有助于理解内容的关键画面。本次只需要 Markdown 成品，不生成 HTML 页面：
<视频链接>
```

需要网页版本时，可以明确追加 HTML 要求：

```text
请按照 instructions.md 整理下面的视频，同时生成 Markdown 文档和可在浏览器中阅读的 HTML 页面：
<视频链接>
```

## 主要功能

- 提取 YouTube 和 Bilibili 的平台字幕，并检查字幕覆盖范围。
- 在平台没有可用字幕时，使用 faster-whisper 在本地转写音频。
- 为 Bilibili 视频生成带时间戳的画面索引，辅助选择关键画面。
- 使用独立的 Chrome 配置保存登录状态，截取会员或登录后可见的视频画面。
- 默认生成 Markdown 成品，并可按需渲染带视频时间锚点和播放器的 HTML 页面。
- 将成品整理到独立的 `pages` 分支，供 GitHub Pages 发布。

## 环境要求

- Python 3.12 或更高版本
- Google Chrome；登录和浏览器截帧时使用
- Git；仅在需要发布到 GitHub Pages 时使用
- 可访问目标视频平台的网络环境

建议在虚拟环境中安装依赖：

```bash
python -m venv .venv
```

激活虚拟环境后安装依赖。Windows PowerShell：

```powershell
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

macOS 或 Linux：

```bash
source .venv/bin/activate
python -m pip install -r requirements.txt
```

项目当前以 `requirements.txt` 作为依赖清单，`pyproject.toml` 未声明运行依赖，因此不应直接使用 `uv sync` 创建运行环境。

本地语音转写可以使用 NVIDIA GPU，也可以回退到 CPU。GPU 通常速度更快；CPU 模式无需显卡，但处理长视频需要更长时间。

## 快速开始

在项目根目录打开一个能够读取文件、执行本地命令并修改工作区的 AI 编程助手，然后提交视频链接，并要求它按照 `instructions.md` 完成处理。未指定输出格式时，工作流也会采用 Markdown 默认值。例如：

```text
请按照 instructions.md 将这个视频整理成文档：
https://www.bilibili.com/video/BVxxxxxxxxxx/
```

助手会依次获取字幕、整理正文、选择并截取关键画面，最终生成 Markdown。明确要求 HTML、网页预览或发布时，才会继续执行 HTML 渲染。部分步骤可能需要人工参与：

- 首次处理需要登录的视频时，初始化专用 Chrome 配置并完成登录：

  ```bash
  python src/capture_frames.py --setup-profile
  ```

- 平台没有字幕时，需要运行本地语音转写。具体参数由助手根据设备和视频情况选择。
- 截图时间超出视频范围或平台拒绝加载时，助手会报告失败项，需要修正后重试。

## 输出文件

单个视频的文件保存在 `output/<video_id>/`。常见产物包括：

| 路径 | 用途 |
| --- | --- |
| `transcript.json` | 字幕、视频元数据和章节信息 |
| `note_plan.summary.md`、`note_plan.json`、`note_packets/` | 轻量阅读入口、完整清单与原句包，含来源 SHA 与候选画面线索 |
| `note_review.json` | agent 填写的原句去向、视觉需求与审阅记录 |
| `note_audit.json` | 本地文件、时间、审阅一致性和图片相似检查报告 |
| `book.md` | 包含正文和图片引用的 Markdown 成品 |
| `book.tagged.md` | 随当前稿同步的截图占位符对照稿 |
| `images/` | 关键画面截图 |
| `book.html` | 明确要求 HTML、网页预览或发布时生成的 HTML 成品 |
| `transcript.corrected.txt` | 可选的修正版字幕对照稿 |

生成 HTML 后，使用本地 HTTP 服务预览，可以保证内嵌播放器按浏览器规则正常加载：

```bash
python -m http.server 8080 --directory output/<video_id>
```

随后访问 `http://localhost:8080/book.html`。结束预览时在终端按 `Ctrl+C`。

## 常见问题

### 无法获取字幕或字幕不完整

登录后可见的字幕需要有效的平台登录状态。先运行以下命令，在打开的专用 Chrome 窗口中登录对应平台并关闭窗口：

```bash
python src/capture_frames.py --setup-profile
```

随后重新执行工作流。也可以向字幕脚本提供 Netscape 格式的 cookies 文件；参数和安全要求见 [instructions.md](instructions.md)。`.capture-profile/` 和 cookies 文件包含登录信息，不应提交到版本库。

### 视频没有平台字幕

可以使用本地 ASR 生成与平台字幕结构一致的 `transcript.json`：

```bash
python src/asr_transcript.py <video_id或视频URL>
```

脚本默认自动选择设备。使用 NVIDIA GPU 时可按显存情况设置 `--compute-type`；没有可用 GPU 时会使用 CPU。转写过程会保存进度，重新执行可继续处理。详细参数可运行：

```bash
python src/asr_transcript.py --help
```

### YouTube 请求或内嵌播放器无法加载

字幕提取命令和浏览器页面分别发起网络请求。处理受网络环境限制的视频时，需要确保终端能够访问视频接口，同时浏览器能够访问播放器域名。

终端请求的代理可以写在项目根目录的 `.env` 文件中，例如：

```text
HTTPS_PROXY=http://127.0.0.1:7890
```

脚本启动时会自动加载该文件，下载字幕、音频、视频和预览图册都适用；文件不纳入版本控制。浏览器截图经过浏览器自身，它的代理需要在浏览器或系统设置中单独配置。

### 截图步骤失败

截图工具会拒绝保存越界时间戳、无效跳转和空白画面。根据命令输出修正 `book.md` 或 `book.tagged.md` 中对应的时间戳后重新运行；已经生成且通过检查的图片可以复用。完整处理规则见 [instructions.md](instructions.md)。

## 发布到 GitHub Pages

仓库已配置 Git 远程地址并完成 HTML 生成后，可将一个或多个成品写入本地 `pages` 分支：

```bash
python src/publish.py <video_id>
git push origin pages
```

发布全部已生成 HTML 的视频：

```bash
python src/publish.py --all
git push origin pages
```

首次发布后，在 GitHub 仓库的 **Settings > Pages** 中选择 `pages` 分支和 `/ (root)` 目录。发布脚本只收集 `book.html`、`book.md`、图片，以及存在时的 `transcript.corrected.txt`；原始字幕和其他中间文件不会写入 `pages` 分支。

## 项目结构

| 路径 | 内容 |
| --- | --- |
| `instructions.md` | AI 助手执行完整工作流时遵循的操作规程 |
| `CLAUDE.md` | Claude Code 的精简项目入口 |
| `docs/quality_options.md` | 成本取舍、GitHub 参考与待验证能力 |
| `ARCHITECTURE.md` | 系统模块、数据流、文件契约和安全边界 |
| `src/` | 字幕、截图、渲染、合并与发布脚本 |
| `prompts/` | 单视频和多分集内容重构规则 |
| `tests/` | 自动测试与测试说明 |
| `output/` | 本地成品和中间文件，不纳入版本控制 |

运行完整测试：

```bash
python tests/run_tests.py
```
