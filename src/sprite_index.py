"""全片画面索引：拉取 B 站预览图册，拼成带时间戳的九宫格。

B 站播放器为了让用户悬停进度条时看到画面预览，会预先抽取全片每 5 秒一帧、
拼成 10x10 的大图（videoshot 接口）。本脚本把这份现成的「画面相册」取回来，
切成小格、再拼成若干张带时间戳标签的九宫格，供 AI 在写文章之前
先看预算内的画面概览，并按需要局部细查，从而：

  1. 从真实画面里挑截图点（而不是凭字幕猜时间戳）
  2. 对比相邻两格判断画面是否已稳定（动画是否播完）
  3. 判断哪些格是同一页（避免重复配图）

产出:
    output/<video_id>/sprite/sheet_NN.png    九宫格索引图（每格 480x270）
    output/<video_id>/sprite_index.json      格子 → 时间戳 对照表

索引图上的每格是该画面段的代表帧（下一次明显变化前的最后采样格），烧的时间戳
就是这一格自己的时刻，格式 HH:MM:SS（与正文里的 `SCREENSHOT:HH:MM:SS` 一致）。
对照表每格另有 since（这画面从何时开始）与 until（持续到何时）。

张数有上限（默认 2 张）：换页点太多时只保留差异最大的那些，其余并回相邻段，
完整变化候选以文本保存在 candidates，未展示的候选另记在 omitted_candidates。
预览图本身的采样间隔可能漏掉短暂画面，概览不等于全片完整覆盖。

用法:
    python src/sprite_index.py <video_id> [--grid 3] [--max-sheets 2]
                              [--limit N] [--all-cells] [--out DIR]
                              [--start SECONDS_OR_HMS] [--end SECONDS_OR_HMS]

<video_id> 支持 `BV1xx411c7mD` 或 `BV1xx411c7mD_p7`（分P）形式。

注意: 图册分辨率只有 480x270，正文小字看不清，仅供**定位与挑选**；
      最终配图仍应回播放器截取高清帧（capture_frames.py）。
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

import av
import numpy as np

import imageio_ffmpeg
from dotenv import load_dotenv

import timecode
from video_id import split_bilibili_id

# 加载可选的本机 .env；下面的 urllib 请求会自动读取 HTTP_PROXY 等标准变量。
load_dotenv()

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _resilient_stdout() -> None:
    """中文 Windows 控制台默认是 GBK，打不出的字符会直接抛 UnicodeEncodeError。
    这里让打不出的字符退化成 ?，绝不因为一个符号把整轮跑挂掉。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass


_resilient_stdout()

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# 图册固定规格（接口返回 img_x_len / img_y_len，实测均为 10）
SPRITE_COLS = 10
SPRITE_ROWS = 10

# 关键格筛选：不再用一个写死的阈值，改「分档 + 张数上限」。
# 单一阈值（旧版 0.05）在两个方向都会错且都不留痕迹：在长讲座上压不下去
# （实测 5910s 视频 712 格 → 202 段 → 23 张图，承诺的「1~2 张」失效），
# 在「95% 纯黑 + 一行小字」的幻灯片上又会把不同的页当成同一页并掉。
T_SAME = 0.001        # 相邻格变化占比低于此值 = 同一帧（压缩噪声级别）
T_GRAY = 0.05         # 灰区上界：低于此值的变化可能只是字幕滚动/局部小动作
MAX_SEG_SEC = 120.0   # 一张图最多代表多少秒；不够就补「弱变化」点进来
DEFAULT_MAX_SHEETS = 2

# 标签字体候选（Windows 自带；缺失时退回无标签模式）
FONT_CANDIDATES = [
    r"C:/Windows/Fonts/arial.ttf",
    r"C:/Windows/Fonts/segoeui.ttf",
    r"C:/Windows/Fonts/msyh.ttc",
]


def _font_path():
    for p in FONT_CANDIDATES:
        if os.path.isfile(p):
            return p
    return None


def _api(url: str, timeout: int = 30) -> dict:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Referer": "https://www.bilibili.com/"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _download(url: str, path: str) -> None:
    """下载到 .part 再改名：断网 / 超时 / Ctrl-C 不会留下半截文件冒充缓存。"""
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Referer": "https://www.bilibili.com/"})
    tmp = path + ".part"
    try:
        with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
            while True:
                chunk = r.read(65536)
                if not chunk:
                    break
                f.write(chunk)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def _jpeg_complete(path: str) -> bool:
    """JPEG 结尾必须是 FFD9。

    半截下载留下的文件**照样能解码**（实测截到 50% 时 PyAV 仍吐出一整张数组，
    只是下半部全黑），所以「能解码」不足以证明文件完好；但截断的文件一定
    没有结尾标记。缓存命中时也要过这一关，否则坏图会被一直复用。
    """
    try:
        if os.path.getsize(path) < 1024:
            return False
        with open(path, "rb") as f:
            f.seek(-2, os.SEEK_END)
            return f.read(2) == b"\xff\xd9"
    except OSError:
        return False


def load_sprites(urls, sprite_dir: str):
    """下载并解码全部图册；缓存按 URL 绑定，坏缓存自动重下。"""
    tag = os.path.join(sprite_dir, "_sprite_cache.json")
    cached = None
    if os.path.isfile(tag):
        try:
            with open(tag, encoding="utf-8") as f:
                cached = json.load(f)
        except ValueError:
            cached = None
    if cached is not None and cached.get("urls") != list(urls):
        # 同一个视频目录重新抽过一次图册（平台会换 URL），旧 jpg 必须作废，
        # 否则格子与时间戳会错位（缓存键不绑 URL 时的老问题）
        for i in range(len(cached.get("urls") or [])):
            p = os.path.join(sprite_dir, f"_sprite_{i:02d}.jpg")
            if os.path.isfile(p):
                os.remove(p)

    sprites = []
    for i, u in enumerate(urls):
        local = os.path.join(sprite_dir, f"_sprite_{i:02d}.jpg")
        if os.path.isfile(local) and not _jpeg_complete(local):
            print(f"   缓存图册不完整（上次中断留下），重新下载 {i + 1}/{len(urls)} ...")
            os.remove(local)
        if not os.path.isfile(local):
            print(f"   下载图册 {i + 1}/{len(urls)} ...")
            _download(u, local)
        if not _jpeg_complete(local):
            os.remove(local)
            raise RuntimeError(f"图册下载不完整（结尾没有 FFD9）：{os.path.basename(local)}")
        sprites.append(decode_sprite(local))
    with open(tag, "w", encoding="utf-8") as f:
        json.dump({"urls": list(urls)}, f, ensure_ascii=False)
    return sprites


def fetch_cid(bvid: str, p: int) -> tuple:
    """取分P 的 cid 与本集标题、时长。"""
    d = _api(f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}")
    if d.get("code") != 0:
        sys.exit(f"取视频信息失败: {d.get('code')} {d.get('message')}")
    data = d["data"]
    pages = data.get("pages") or []
    page = next((x for x in pages if x.get("page") == p), None)
    if page is None:
        sys.exit(f"找不到分P {p}（该视频共 {len(pages)} 个分P）")
    return page["cid"], data.get("title", ""), page.get("duration", 0), page.get("part", "")


def fetch_sprite(bvid: str, cid: int, retries: int = 3) -> tuple:
    """取预览图册：返回 (图册 URL 列表, 时间戳秒数列表)。

    实测该接口会**偶发返回空 index**（同一请求相隔数秒结果不同），
    故加退避重试；真的没有图册时才返回空。
    """
    for attempt in range(retries):
        try:
            d = _api(f"https://api.bilibili.com/x/player/videoshot"
                     f"?bvid={bvid}&cid={cid}&index=1")
        except Exception:
            d = {}
        if d.get("code") == 0:
            data = d.get("data") or {}
            urls = [u if u.startswith("http") else "https:" + u
                    for u in (data.get("image") or [])]
            times = [int(t) for t in (data.get("index") or [])]
            if urls and times:
                return urls, times
        if attempt < retries - 1:
            time.sleep(1.5)
    return [], []


def decode_sprite(path: str) -> np.ndarray:
    """图册 jpg → numpy 数组 (H, W, 3)。"""
    with av.open(path) as c:
        frame = next(c.decode(video=0))
        return frame.to_ndarray(format="rgb24")


def cut_cells(sprite: np.ndarray, cols: int, rows: int):
    """把一张图册切成 cols*rows 个格子，按行优先返回。"""
    h, w = sprite.shape[:2]
    cw, ch = w // cols, h // rows
    return [sprite[r * ch:(r + 1) * ch, c * cw:(c + 1) * cw]
            for r in range(rows) for c in range(cols)], cw, ch


def changed_ratio(a, b, thresh: int = 25) -> float:
    """两格之间「灰度差超过 thresh 的像素」占比。

    用占比而不是最大块差：画面切换是整幅变化（占比高），
    而底部字幕滚动、讲师小动作只影响局部（占比低）。
    注意占比的量级随素材变化很大（本项目的课：字幕滚动 0.01~0.05，
    但「95% 纯黑 + 一行小字」的幻灯片换页也可能低于 0.05），
    所以不要拿某个固定值当「换页」的唯一判据 —— 见 pick_segments。
    """
    d = np.abs(a.astype(np.int16) - b.astype(np.int16)).mean(axis=2)
    return float((d > thresh).mean())


def neighbor_diffs(cells):
    """d[i] = 第 i-1 格 → 第 i 格的变化像素占比（d[0] 恒为 0.0）。"""
    d = [0.0] * len(cells)
    for i in range(1, len(cells)):
        d[i] = changed_ratio(cells[i - 1], cells[i])
    return d


def pick_segments(cells, times, max_cells, per_sheet=None,
                  gray_hi: float = T_GRAY, max_seg_sec: float = MAX_SEG_SEC):
    """把 n 格切成若干「同一画面」的段，每段一个代表帧。返回 (段列表, d 序列)。

    分档判据（不再写死单一阈值）：

      d < T_SAME            同一帧          → 合并
      T_SAME ≤ d < gray_hi  弱变化          → 默认合并；有空位时按差异大小补进来
                            （字幕滚动 / 局部小动作，也可能是黑底幻灯片换页）
      d ≥ gray_hi           换页            → 保留为新段起点，并标 weak=False

    max_cells 是硬上限（「索引图张数上限 × 每张格数」）：换页点太多时只保留
    差异最大的那些，其余并回相邻段，并在该段的 merged 里记下被并掉了几个，
    不让它们静默消失。

    per_sheet 给定时，补进来的弱变化点**只能用「强变化点本来就需要的那几张图」
    里的空位**，不会为了多几格而多开一张索引图 —— 读图成本只由强变化点决定。

    代表帧取**段末格**：PPT 逐条出现时段首格是最不完整的一帧，段末格才是
    动画播完的画面（方案书层次三「合并保留后一张（动画更完整）」）。
    """
    n = len(cells)
    d = neighbor_diffs(cells)
    if n == 0:
        return [], d
    cand = [i for i in range(1, n) if d[i] > T_SAME]
    strong = [i for i in cand if d[i] >= gray_hi]
    weak = [i for i in cand if d[i] < gray_hi]
    hard = max(0, int(max_cells) - 1)
    if per_sheet and len(strong) + 1 <= int(max_cells):
        need = -(-(len(strong) + 1) // int(per_sheet))       # 强变化点需要几张图
        room = min(hard, need * int(per_sheet) - 1)
    else:
        room = hard
    if len(strong) >= room:
        chosen = sorted(sorted(strong, key=lambda i: -d[i])[:room])
    else:
        chosen = list(strong)
        # 覆盖度补点：反复去切「当前最长的段」，并在其中挑变化最大的一格。
        # 这样补出来的点会铺满全片，而不是全挤在画面最热闹的那一段。
        tried = set()
        while len(chosen) < room:
            bounds = sorted([0] + chosen + [n])
            cands = [(times[bounds[k + 1] - 1] - times[bounds[k]], k)
                     for k in range(len(bounds) - 1)
                     if (bounds[k], bounds[k + 1]) not in tried]
            if not cands:
                break
            span, k = max(cands)
            if span <= max_seg_sec:
                break              # 每格代表的时长都够短了
            lo, hi = bounds[k], bounds[k + 1]
            inside = [i for i in weak if lo < i < hi and i not in chosen]
            if not inside:
                tried.add((lo, hi))
                continue
            mid = (lo + hi) / 2    # 同样显著时取最靠段中间的，补得更均匀
            chosen.append(max(inside, key=lambda i: (d[i], -abs(i - mid))))
        chosen.sort()
    bounds = [0] + chosen
    segs = []
    for j, b in enumerate(bounds):
        end = bounds[j + 1] if j + 1 < len(bounds) else n
        # 预算丢掉的换页不能改变已保留那一页的代表帧。
        # end 仍记录 overview 合并范围，visual_end 记录画面本身的边界。
        visual_end = min([i for i in strong if b < i < end] or [end])
        segs.append({
            "start": b,
            "rep": visual_end - 1,
            "end": end,
            "visual_end": visual_end,
            "weak": bool(b and d[b] < gray_hi),
            "merged": sum(1 for i in cand if b < i < end),
        })
    return segs, d


def candidate_catalog(cells, times, duration, diffs=None, gray_hi=T_GRAY):
    """完整变化目录只保存文本元数据，不受读图预算限制。

    弱变化也保留为待判断候选，不宣称每个候选都是真正换页。
    """
    if not times:
        return []
    diffs = neighbor_diffs(cells) if diffs is None else diffs
    starts = [0] + [i for i in range(1, len(times)) if diffs[i] > T_SAME]
    result = []
    for j, start in enumerate(starts):
        end = starts[j + 1] if j + 1 < len(starts) else len(times)
        rep = end - 1
        result.append({
            "id": f"candidate_{j + 1:04d}", "start_index": start,
            "rep_index": rep, "seconds": times[rep], "time": fmt_hms(times[rep]),
            "since": times[start], "until": times[end] if end < len(times) else duration,
            "weak": bool(start and diffs[start] < gray_hi),
            "ratio": round(diffs[start], 6), "overview_visible": False,
        })
    return result


def fmt_hms(sec) -> str:
    """秒 → HH:MM:SS。索引图标签与对照表共用，避免出现 "98:26" 这种
    说不清是 98 分钟还是 1 小时 38 分的写法（AI 照着写进正文就再也匹配不上
    SCREENSHOT:HH:MM:SS 了）。格式本身见 timecode.format_hms。"""
    return timecode.format_hms(sec)


def compose_sheet(cells, times, out_path: str, grid_n: int, cell_w: int, cell_h: int,
                  font: str) -> None:
    """把最多 grid_n× 个格子拼成一张九宫格，并给每格左上角打上时间戳。"""
    rows = []
    for r in range(grid_n):
        row = []
        for c in range(grid_n):
            i = r * grid_n + c
            if i < len(cells):
                row.append(cells[i])
            else:
                row.append(np.zeros((cell_h, cell_w, 3), dtype=np.uint8))
        rows.append(np.hstack(row))
    sheet = np.vstack(rows)

    raw = sheet.tobytes()
    ff = imageio_ffmpeg.get_ffmpeg_exe()
    h, w = sheet.shape[:2]

    cmd = [ff, "-hide_banner", "-loglevel", "error",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}",
           "-i", "pipe:0"]

    if font:
        # ffmpeg 滤镜参数用冒号分隔，字体路径里的盘符冒号（C:）必须转义
        font_esc = font.replace(":", r"\:")
        chain = []
        for r in range(grid_n):
            for c in range(grid_n):
                i = r * grid_n + c
                if i >= len(cells):
                    continue
                x, y = c * cell_w + 10, r * cell_h + 10
                label = fmt_hms(times[i]).replace(":", r"\:")
                chain.append(
                    f"drawtext=fontfile='{font_esc}':text='{label}':x={x}:y={y}:"
                    f"fontsize=26:fontcolor=white:box=1:boxcolor=black@0.65:boxborderw=7")
        if chain:
            cmd += ["-vf", ",".join(chain)]

    cmd += ["-frames:v", "1", "-y", out_path]
    subprocess.run(cmd, input=raw, check=True)


def main() -> None:
    ap = argparse.ArgumentParser(description="把 B 站预览图册拼成带时间戳的画面索引图")
    ap.add_argument("video_id", help="BV1xx411c7mD 或 BV1xx411c7mD_p7")
    ap.add_argument("--grid", type=int, default=3,
                    help="每张索引图的格数 N（N×N，默认 3 = 九宫格）")
    ap.add_argument("--limit", type=int, default=0,
                    help="最多生成几张索引图（0 = 全部，调试用）")
    ap.add_argument("--max-sheets", type=int, default=DEFAULT_MAX_SHEETS,
                    help=f"索引图张数上限（默认 {DEFAULT_MAX_SHEETS}）；"
                         "长讲座超出时会按差异大小保留最显著的切换点")
    ap.add_argument("--all-cells", action="store_true",
                    help="不做关键格筛选，每 5 秒一格全部输出（调试用，忽略张数上限）")
    ap.add_argument("--change-ratio", type=float, default=T_GRAY,
                    help=f"「灰区」上界：变化占比低于此值视为弱变化（可能只是字幕滚动），"
                         f"默认 {T_GRAY}")
    ap.add_argument("--out", default=None, help="输出目录（默认 output/<video_id>）")
    ap.add_argument("--start", type=timecode.parse_seconds, default=None,
                    help="局部细查起点（秒或 HH:MM:SS），输出到独立 visual_detail 子目录")
    ap.add_argument("--end", type=timecode.parse_seconds, default=None,
                    help="局部细查终点，不包含该时刻；默认到片尾")
    args = ap.parse_args()
    if args.end is not None and args.end <= (args.start or 0):
        ap.error("--end must be greater than --start")

    bvid, p = split_bilibili_id(args.video_id)
    # 画面索引走的是 B 站独有的 videoshot 接口，YouTube 没有等价物。这里显式跳过，
    # 而不是拿 YouTube 的 id 去请求 B 站接口（那会拿到 code=-400，报成
    # "取视频信息失败" 这种看不懂的硬错误）。跳过不算成功，所以把话说清楚。
    if not re.match(r"^BV\w+$", bvid):
        print(f">> 「{args.video_id}」不是 B 站视频：画面索引仅支持 B 站，"
              "本步已跳过（不是完成），请按原方式从字幕推断配图点。")
        return
    root_vdir = os.path.join(BASE_DIR, "output", args.video_id)
    detail = args.start is not None or args.end is not None
    vdir = args.out or (os.path.join(root_vdir, "visual_detail",
                        f"{args.start or 0}_{args.end if args.end is not None else 'end'}")
                       if detail else root_vdir)
    sprite_dir = os.path.join(vdir, "sprite")
    os.makedirs(sprite_dir, exist_ok=True)
    cache_dir = os.path.join(root_vdir, "sprite") if detail and not args.out else sprite_dir
    os.makedirs(cache_dir, exist_ok=True)

    print(f">> 取视频信息 {bvid} p{p} ...")
    cid, title, duration, part = fetch_cid(bvid, p)
    print(f"   {title} / {part}  时长 {duration}s  cid={cid}")

    print(">> 取预览图册 ...")
    urls, times = fetch_sprite(bvid, cid)
    if not urls or not times:
        sys.exit("该视频没有预览图册（videoshot 返回空），需改用其他扫描方式")
    print(f"   图册 {len(urls)} 张，时间戳 {len(times)} 个，覆盖 0~{times[-1]}s")

    cells, cw, ch = [], 0, 0
    for i, sprite in enumerate(load_sprites(urls, cache_dir)):
        part_cells, cw, ch = cut_cells(sprite, SPRITE_COLS, SPRITE_ROWS)
        cells.extend(part_cells)

    # 只保留有时间戳的格子（末尾可能有多余空位）
    n = min(len(cells), len(times))
    cells, times = cells[:n], times[:n]
    if not n:
        sys.exit("预览图册没有可用的采样格")
    scope_start = args.start or 0
    scope_end = min(args.end, duration) if args.end is not None and duration else args.end
    scope_end = scope_end if scope_end is not None else (duration or times[-1] + 1)
    if detail:
        selected = [i for i, t in enumerate(times) if scope_start <= t < scope_end]
        cells, times = [cells[i] for i in selected], [times[i] for i in selected]
        n = len(times)
    if not n:
        sys.exit("所选时间区间没有预览采样帧；请扩大区间或直接截图该时刻")

    grid_n = max(2, args.grid)
    per_sheet = grid_n * grid_n
    max_cells = max(1, args.max_sheets) * per_sheet

    # 分段 + 选代表帧（段末格）
    if args.all_cells:
        segs = [{"start": i, "rep": i, "end": i + 1, "weak": False, "merged": 0}
                for i in range(n)]
        diffs = neighbor_diffs(cells)
        print(f"   --all-cells：{n} 格全部输出（忽略 {args.max_sheets} 张的上限）")
    else:
        segs, diffs = pick_segments(cells, times, max_cells, per_sheet,
                                    args.change_ratio)
        n_weak = sum(1 for s in segs if s["weak"])
        merged = sum(s["merged"] for s in segs)
        sheets = (len(segs) + per_sheet - 1) // per_sheet
        print(f"   画面段 {len(segs)} 段 → {sheets} 张索引图"
              f"（上限 {args.max_sheets} 张 / {max_cells} 格；"
              f"从 {n} 格中省略 {n - len(segs)} 格采样画面"
              + (f"；并入 {merged} 个次要变化点" if merged else "")
              + (f"；其中 {n_weak} 段是补的弱变化，像素上分不出是不是真换页"
                 if n_weak else "") + "）")
    if not args.all_cells and n > per_sheet and len(segs) * 100 // n > 90:
        print("   [!] 几乎没压掉重复：这片子画面一直在变，可加 --max-sheets N 换更粗的地图")

    # 被并掉的变化点里最显著的几个：显式报出来，别让它们静默消失
    starts = {s["start"] for s in segs}
    dropped = [i for i in range(1, n) if diffs and diffs[i] > T_SAME and i not in starts]
    dropped.sort(key=lambda i: -diffs[i])
    merged_top = [{"seconds": times[i], "time": fmt_hms(times[i]),
                   "ratio": round(diffs[i], 4)} for i in dropped[:5]]
    if merged_top:
        print("   被并入相邻段的显著变化点（前几个）："
              + ", ".join(f"{m['time']}({m['ratio']})" for m in merged_top))

    key_cells = [cells[s["rep"]] for s in segs]
    key_times = [times[s["rep"]] for s in segs]
    key_since = [times[s["start"]] for s in segs]
    # until = 这画面持续到的时刻：下一段的起始格时刻；最后一段到**真实片长**
    #（旧版用 times[-1]，即最后一个采样时刻，比片长少几秒，末段长度会算成 0）
    tail = duration or times[-1]
    tail = min(tail, scope_end)
    key_until = [times[s.get("visual_end", s["end"])]
                 if s.get("visual_end", s["end"]) < n else tail for s in segs]
    key_overview_until = [times[s["end"]] if s["end"] < n else tail for s in segs]
    key_weak = [s["weak"] for s in segs]
    key_merged = [s["merged"] for s in segs]

    font = _font_path()
    if not font:
        print("   [!] 未找到字体，索引图将不含时间戳标签")

    # 清掉上一轮留下的索引图：张数变少时它们会一直躺在目录里，而 instructions.md
    # 让 AI 按 sprite/sheet_*.png 通配读取 —— 旧图会被当成本轮的画面地图
    #（产出带旧时间戳的「幽灵段落」），而 sprite_index.json 里根本没有它们。
    # 只清 sheet_*.png，_sprite_*.jpg 是下载缓存，留着。
    stale = sorted(f for f in os.listdir(sprite_dir)
                   if re.fullmatch(r"sheet_\d+\.png", f))
    for f in stale:
        os.remove(os.path.join(sprite_dir, f))
    if stale:
        print(f"   清掉上一轮残留的 {len(stale)} 张索引图")
    # 旧对照表一并清掉：万一之后中途失败（ffmpeg 报错 / Ctrl-C），宁可没有对照表，
    # 也不要「新画面 + 上一轮的时间戳」这种错位组合 —— AI 会照抄旧时间戳，图文对不上。
    jpath = os.path.join(vdir, "sprite_index.json")
    if os.path.exists(jpath):
        os.remove(jpath)

    sheets_meta = []
    m = len(key_cells)
    total = (m + per_sheet - 1) // per_sheet
    if args.limit:
        total = min(total, args.limit)

    for s in range(total):
        chunk_cells = key_cells[s * per_sheet:(s + 1) * per_sheet]
        chunk_times = key_times[s * per_sheet:(s + 1) * per_sheet]
        chunk_until = key_until[s * per_sheet:(s + 1) * per_sheet]
        chunk_since = key_since[s * per_sheet:(s + 1) * per_sheet]
        chunk_weak = key_weak[s * per_sheet:(s + 1) * per_sheet]
        chunk_merged = key_merged[s * per_sheet:(s + 1) * per_sheet]
        name = f"sheet_{s + 1:02d}.png"
        path = os.path.join(sprite_dir, name)
        compose_sheet(chunk_cells, chunk_times, path, grid_n, cw, ch, font)
        sheets_meta.append({
            "file": f"sprite/{name}",
            "cells": [
                {"pos": i, "row": i // grid_n, "col": i % grid_n,
                 "seconds": chunk_times[i],          # 该格画面自己的时刻（截图就用它）
                 "time": fmt_hms(chunk_times[i]),    # 与正文 SCREENSHOT:HH:MM:SS 同格式
                 "since": chunk_since[i],            # 这画面从何时开始
                 "until": chunk_until[i],            # 持续到何时（末段 = 片长）
                 "overview_until": key_overview_until[s * per_sheet + i],
                 "weak": chunk_weak[i],              # 该段是弱变化：可能是字幕滚动而非换页
                 "merged": chunk_merged[i]}          # 段内被并掉的次要变化点个数
                for i in range(len(chunk_cells))
            ],
        })
        print(f"   {name}  第 {s + 1}/{total} 张  ({len(chunk_cells)} 格)")

    candidates = candidate_catalog(cells, times, tail, diffs, args.change_ratio)
    visible = {c["seconds"] for sh in sheets_meta for c in sh["cells"]}
    for candidate in candidates:
        candidate["overview_visible"] = candidate["seconds"] in visible
    omitted = [c["id"] for c in candidates if not c["overview_visible"]]
    index = {
        "schema_version": 2,
        "video_id": args.video_id,
        "bvid": bvid,
        "page": p,
        "cid": cid,
        "title": title,
        "part": part,
        "duration": duration,
        "grid": grid_n,
        "max_sheets": max(1, args.max_sheets),
        "max_cells": max_cells,
        "thresholds": {"same": T_SAME, "gray_hi": args.change_ratio},
        "all_cells": bool(args.all_cells),
        "cell_size": [cw, ch],
        "total_cells": n,
        "key_cells": m,
        "sheets": sheets_meta,
        "merged_top": merged_top,
        "candidates": candidates,
        "omitted_candidates": omitted,
        "coverage": {"scope_start": scope_start, "scope_end": scope_end,
                     "sample_interval_max": max([b - a for a, b in zip(times, times[1:])] or [0]),
                     "omitted_count": len(omitted), "overview_complete": not omitted,
                     "sampling_can_miss_short_scenes": True},
        "note": "每格是该画面段的代表帧（下一次明显变化前的最后采样格）：seconds/time 就是"
                "这一格自己的时刻，正文的 SCREENSHOT 直接用它；since/until 是这段画面的起止"
                "时刻（until 为片长表示它是最后一段）；weak=true 表示这格与前格差异很小，"
                "可能只是字幕滚动，也可能是一张黑底幻灯片换页——看画面自己判断；"
                "merged>0 表示 overview 合并了变化点；candidates 保留完整变化目录，"
                "omitted_candidates 明确记录未展示的候选。可用 --start/--end 局部细查，"
                "默认另存 visual_detail 子目录。预览采样可能漏掉短暂画面，"
                "索引图仅用于定位，最终配图需回播放器截高清帧。",
    }
    with open(jpath, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)

    print(f"\n完成：{total} 张索引图 + 对照表")
    print(f"   索引图：{sprite_dir}")
    print(f"   对照表：{jpath}")


if __name__ == "__main__":
    main()
