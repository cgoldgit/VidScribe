# VidScribe 工作流指令

本文档定义视频转电子书任务的执行顺序、完成条件和失败处理。所有命令均从仓库根目录执行。正文写作规则位于 `prompts/stitcher_system.md`；多分P 单集写作规则位于 `prompts/episode_section.md`。

## 适用范围

当用户提供 YouTube 或 Bilibili 视频链接，并要求生成电子书、教程或技术指南时，执行本工作流。

### 输出格式判定

- 用户只提供视频链接，或要求整理成电子书、教程、技术指南而没有指定格式时，默认只生成 Markdown 成品，不执行 HTML 渲染，也不启动本地网页预览。
- 用户明确要求 HTML、网页版本或浏览器预览时，先完成 Markdown，再生成 HTML。
- 用户要求发布到 GitHub Pages 时，视为同时要求 HTML；发布前必须完成 HTML 渲染。
- 用户明确指定一种或多种输出格式时，以用户要求为准。

Markdown 成品可以引用 `images/` 中的关键画面。“只生成 Markdown”表示不生成新的 `book.html`，不表示跳过字幕、画面索引、截图、图片物化等必要的中间步骤。目标目录中如果已经存在旧的 `book.html`，不得自动删除；完成报告中应说明该文件不是本次生成或更新的成品。

开始前确认：

- 当前 Python 版本不低于 3.12，依赖通过 `python -m pip install -r requirements.txt` 安装。
- 需要登录的平台已通过 `.capture-profile/` 配置登录状态。
- 运行环境能够访问目标视频平台。
- 所有相对路径均以仓库根目录为基准。

不得运行 `uv sync`。当前 `pyproject.toml` 未声明运行依赖，项目以 `requirements.txt` 为依赖清单。

## 单视频工作流

### 1. 提取字幕

```bash
python src/dump_transcript.py "<VIDEO_URL>"
```

成功产物为 `output/<video_id>/transcript.json`，主要字段包括：

- `video_url`、`title`、`video_id`
- `duration`
- `chapters`
- `segments`

脚本会检查末段字幕结束时间与视频总时长的比例。覆盖率低于 50% 时返回退出码 3，不得继续生成书稿。此时应先检查登录状态、网络、地区限制或平台字幕是否完整，然后重新执行本步骤。

Bilibili 登录字幕通常依赖 `.capture-profile/`。需要初始化时执行：

```bash
python src/capture_frames.py --setup-profile
```

字幕脚本还支持：

```text
--cookies-from-profile <dir>
--cookies-file <path>
--cookies-from <browser>
```

Windows 主 Chrome 的 cookies 可能受浏览器加密策略限制，优先使用专用配置或显式 cookies 文件。

#### 平台没有字幕时

确认平台没有 CC、自动字幕或其他可用字幕轨后，使用本地 ASR：

```bash
python src/asr_transcript.py <video_id或VIDEO_URL>
```

该脚本只下载音频，并使用 faster-whisper 生成与平台字幕结构一致的 `transcript.json` 和 `transcript.txt`。

建议先抽样检查质量和速度：

```bash
python src/asr_transcript.py <video_id或VIDEO_URL> --sample-start 900 --sample-dur 180
```

ASR 规则：

- 可在 `output/<video_id>/_asr_prompt.txt` 中提供领域术语。长中文提示优先写入文件，不通过 PowerShell 命令行传递。
- NVIDIA GPU 可使用 CUDA；显存较小时可加 `--compute-type int8_float16`。没有可用 GPU 时脚本回退到 CPU。
- 正式进度保存在 `_asr_progress.jsonl`，重新执行会从已有进度继续。仅在明确需要丢弃进度时使用 `--restart`。
- 完成后仍需通过覆盖率检查；覆盖率不足时不得进入下一步。

无法确认失败属于“平台无字幕”还是“访问失败”时，应停止并向用户说明需要登录、代理或平台字幕状态信息，不得把不完整字幕当作完整输入。

### 2. 生成 Markdown 书稿

Bilibili 视频先生成画面索引：

```bash
python src/sprite_index.py <video_id>
```

产物包括：

- `output/<video_id>/sprite/sheet_*.png`
- `output/<video_id>/sprite_index.json`

YouTube 没有对应的预览图册接口，跳过本命令。

生成书稿前必须读取：

- `output/<video_id>/transcript.json`
- Bilibili 视频的画面索引图和 `sprite_index.json`
- `prompts/stitcher_system.md`

按照写作规范将内容写入：

```text
output/<video_id>/book.md
```

需要配图的位置使用 `SCREENSHOT:HH:MM:SS` 占位符。Bilibili 的截图时间优先取自画面索引；索引图只用于定位，不能替代最终高清截图。

#### 字幕对照稿

单视频可以生成修正版字幕对照稿：

```bash
python src/make_corrected.py <video_id>
```

产物为 `output/<video_id>/transcript.corrected.txt`。只修正明确的识别错误、截断词和口癖，保留讲师原意、语序和逐段结构。不得将其改写成书面文章。

### 3. 截取画面并物化占位符

首选项目自带的专用浏览器配置：

```bash
python src/capture_frames.py <video_id> "<VIDEO_URL>"
```

首次使用或登录状态失效时执行：

```bash
python src/capture_frames.py --setup-profile
```

此步骤需要启动本机 Chrome 并访问专用浏览器配置。受限运行环境应先取得完成该操作所需的本机访问权限。

成功条件：

- 每个截图时间戳都有对应的 `images/shot_HH_MM_SS.png`。
- `book.md` 中不再包含 `SCREENSHOT:` 占位符。
- 命令退出码为 0。
- 当脚本实际执行截图时，`capture_manifest.json` 记录本轮结果；仅复用已有图片时不会重新生成记录。

脚本会检查时间戳越界、跳转失败和空白画面。任一必需帧失败时，命令返回非零退出码，不得把当前状态报告为完成。根据失败清单修正 `book.md` 或 `book.tagged.md` 后重新执行；默认只补缺失或不合格的图片。

重截选项：

```text
--force             重截全部时间戳
--force-invalid     只重截缺失或质量检查失败的时间戳
--materialize-only  不截图，只使用现有图片物化占位符
```

这些选项存在互斥关系，以命令行帮助和参数校验为准。`--materialize-only` 发现缺图或坏图时同样返回失败。

如果执行环境提供已登录浏览器的标签页控制能力，也可以按占位符时间逐帧截图到 `images/`，然后执行：

```bash
python src/capture_frames.py <video_id> --materialize-only
```

这种方式必须满足相同的文件命名、时间准确性和质量检查要求。

#### 浏览器截图无法使用时

仅在浏览器方案不可用或持续失败时使用下载视频的兜底方案：

```bash
python src/extract_frames.py <video_id> "<VIDEO_URL>"
```

该方案可能生成 `output/<video_id>/video_source.mp4`，默认画质取决于未登录状态下可获取的视频流。它属于兜底路径，不作为常规截图方式。

### 4. 按需渲染并预览 HTML

只有用户明确要求 HTML、网页版本、浏览器预览或 GitHub Pages 发布时，才执行本步骤。默认的 Markdown 工作流完成第 3 步后直接进入完成报告。

```bash
python src/post_process.py "<VIDEO_URL>" output/<video_id>/book.md
python -m http.server 8080 --directory output/<video_id>
```

成功产物为 `output/<video_id>/book.html`。使用浏览器打开：

```text
http://localhost:8080/book.html
```

使用 HTTP 服务预览可避免本地 `file://` 页面加载内嵌播放器时受到浏览器限制。预览结束后在服务器终端按 `Ctrl+C`。

### 5. 完成报告

向用户提供：

- Markdown 文件路径
- 本次生成的配套图片目录（如果存在）
- 要求并成功生成 HTML 时，提供 HTML 文件路径和本地预览地址
- 本次未要求 HTML、但目标目录已有旧 `book.html` 时，说明该文件未在本次任务中更新
- 截图失败、网络限制或未验证项
- 截图实际使用的登录状态和可达到的画质范围

如果流程生成了 `audio.*` 或 `video_source.mp4` 等大文件，列出文件后询问用户是否删除。未获得明确确认时不得删除。

## 多分P 合集

多分P 视频先逐集完成字幕、书稿和截图，再进行合并。

批量提取字幕：

```bash
python src/batch_transcripts.py --bvid <BV_ID> --start 1 --end <N>
```

逐集书稿必须读取 `prompts/episode_section.md`。全部单集完成后，列出分P 标题：

```bash
python -m yt_dlp --flat-playlist --print "%(playlist_index)s|%(title)s" "<合集URL>"
```

仓库默认不包含 `books.json`。分册属于编辑决策；先向用户提交分组方案，获得确认后，再为当前合集在根目录临时创建该文件：

```json
{
  "source": "<合集名称>",
  "bvid": "<BV号>",
  "source_url": "<合集链接>",
  "books": [
    {"dir": "第一章 · 入门", "episodes": [1, 2, 3]},
    {"dir": "第二章 · 进阶", "episodes": [4, 5, 6]}
  ]
}
```

`dir` 不得包含 Windows 保留字符 `\ / : * ? " < > |`，也不得以点或空格结尾。`books.json` 只服务于当前合集任务，已被 `.gitignore` 排除；任务结束后可以删除，需要重新合并时再按分册方案生成。

合并前先检查完整性：

```bash
python src/merge_book.py --check
python src/merge_book.py --all
```

缺集、缺图或残留占位符时，正常合并应失败。只有用户明确接受部分产出时才能使用 `--allow-partial`。发布脚本默认拒绝发布带有部分产出标记的书。

## 发布到 GitHub Pages

发布依赖 HTML 成品。用户提出发布要求时，应先按第 4 步生成并验证 `book.html`。

发布一个或全部成品：

```bash
python src/publish.py <video_id>
python src/publish.py --all
```

脚本将允许公开的成品写入本地 `pages` 分支，不切换当前工作区。确认结果后再推送：

```bash
git push origin pages
```

首次发布需要在 GitHub 仓库的 Settings > Pages 中选择 `pages` 分支和 `/ (root)` 目录。

发布范围仅包括：

- `book.html`
- `book.md`
- `images/`
- 存在时的 `transcript.corrected.txt`

原始字幕、浏览器配置、cookies、ASR 进度和其他中间文件不得发布。

## 安全与验证

- `.capture-profile/` 和 cookies 文件包含登录信息，不得提交或发布。
- 当前 ASR 路径可能在 `output/<video_id>/` 下生成 `_cookies.txt`；该目录已被 Git 忽略，但任务结束前仍应检查并清理不再需要的凭据副本。
- 不得在没有用户确认的情况下删除音频、视频或其他大文件。
- 代码修改后运行与改动相关的测试；需要完整回归时执行 `python tests/run_tests.py`。
- 真实平台的登录、字幕和截图行为不在完整自动化测试范围内，相关改动需要单独进行人工冒烟验证。
- 生成或修改 HTML 时，正文与背景的颜色对比度应满足 WCAG AA，普通文字至少为 4.5:1。
