# 测试

项目提供一个不依赖 pytest 的测试入口；测试文件本身使用无参数函数和 `assert`，安装 pytest 后也可以直接运行。

## 运行方式

```bash
python tests/run_tests.py
python tests/run_tests.py capture
python tests/run_tests.py --list
```

- 第一条运行完整测试集。
- 第二条只运行名称中包含 `capture` 的测试。
- 第三条列出测试名称，不执行测试。

使用 pytest：

```bash
python -m pytest tests
```

## 测试分组

| 文件 | 环境要求 | 主要覆盖范围 |
| --- | --- | --- |
| `test_capture_frames.py` | ffmpeg | 截图质量检查、选帧、占位符、重截参数、失败门禁和 manifest |
| `test_capture_browser.py` | 本机 Chrome；缺少时跳过 | 媒体超时、播放停滞、真实浏览器截图、越界与空帧处理 |
| `test_sprite_index.py` | ffmpeg | 变化点筛选、张数上限、时间戳、缓存、原子下载和平台判断 |
| `test_asr_transcript.py` | 无；全离线 | 多分P URL、试跑隔离、断点续跑和 `--restart` |
| `test_timecode.py` | 无 | 字幕时间戳归一化和覆盖率计算 |
| `test_transcript_quality.py` | 无；全离线 | 覆盖率门槛、批量门禁、坏文件重抓和不覆盖已有产物 |
| `test_timecode_module.py` | 无 | 统一时间戳模块及历史格式兼容性 |
| `test_video_id.py` | 无 | 视频标识的解析与构造 |
| `test_post_process.py` | 无 | HTML 转义、链接限制、CSP、视频卡片和失败退出码 |
| `test_publish.py` | 无；使用模拟 Git 调用 | 发布白名单、部分产出门禁、目录稳定性和落地页转义 |
| `test_merge_book.py` | 无 | 配置校验、暂存目录重建、孤儿图片清理和部分产出门禁 |
| `test_gbk_output.py` | 无 | Windows GBK 控制台兼容和输出降级 |

`test_capture_browser.py` 使用本地 HTTP 服务和测试生成的视频，不访问真实平台，也不需要登录状态。

## 自动测试边界

下列行为需要人工冒烟验证：

- Bilibili 和 YouTube 的真实字幕接口、登录状态和账号权益。
- Bilibili 预览图册的联网获取。
- 真实播放器中的最高画质选择和截图结果。
- `capture_frames.py` 主入口在部分帧失败时的完整命令行输出。
- 真实 Git 仓库中的 pages 发布、幂等更新和目录改名保护。
- 浏览器实拍产生的 `capture_manifest.json`。

HTML 的 CSP 当前仍包含 `'unsafe-inline'`。现有测试验证外部来源限制和已知注入路径，但不能把该配置视为完整的内联脚本防护。

历史审查结论和测试建立原因集中记录在根目录 [AUDIT.md](../AUDIT.md)。
