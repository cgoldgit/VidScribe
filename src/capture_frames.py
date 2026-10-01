"""Primary frame materialization: screenshots taken directly from the platform
player in a logged-in browser (high quality, from the platform).

Usage:
    python capture_frames.py --setup-profile
    python capture_frames.py <video_id> [video_url] [--method auto|dedicated]
                             [--materialize-only] [--force | --force-invalid]
                             [--profile-dir PATH]

Capture method (project policy, Chrome >= 136 compatible):
  dedicated  Playwright launches Chrome with a DEDICATED user-data-dir
             (<repo>/.capture-profile by default). Chrome forbids any remote
             debugging (port or pipe) on the DEFAULT user-data-dir since
             v136, so this profile is initialized once via --setup-profile:
             log in to the platforms (bilibili / YouTube) one time and the
             cookies persist for unattended captures afterwards. The dedicated
             directory does not conflict with your daily Chrome (both can run
             at the same time).

  v2 pipeline layers (see README "截帧管线 v2"):
    A 启动层   headless 优先，失败回退 headful；不弹窗打扰。
    B 探针层   抓取前查询 playurl，取顶档原生分辨率，viewport 1:1 设置（不自嗨放大）。
    C 流锁定层 路由拦截 playurl 响应，只保留顶档最高码率变体（优先 AVC），
              根除"自动档从 360P 起播、暂停冻结升档"导致的糊图。
    D 页面层   默认嵌入播放器 player.html（轻量）；video 选择器超时自动降级主站观看页。
    E 截帧层   seek(t) -> await seeked -> pause -> <video> 元素截图；
              visibility CSS 隐藏一切非 video 元素（顶栏/控制栏/引流条/推荐层/黑边 UI）。
    F QA 层    截图解码后按「是否整幅单色/全黑」判定空帧，偏移 +2s 自动重试一次；
              重试后仍不合格则该帧算失败、不留文件（错图比缺图更糟：看不出来）。
    G 稳定层   截前先播一小段并采样，挑「画面已稳定且信息量最大」的时刻（见下）。

Deliberately excluded (project policy): the Codex/ChatGPT in-app browser
(separate profile, not logged in), a clean Playwright-managed Chromium
(not logged in), win32 screen capture (takes over the screen), and any remote
debugging against the DEFAULT Chrome profile (blocked by Chrome >= 136). When
running inside the Codex desktop app, prefer the ChatGPT browser extension
(@Chrome) over this script; see instructions.md.

No video or audio file is downloaded by any browser method.

After capturing, the SCREENSHOT placeholders in book.md are materialized into
images/shot_HH_MM_SS.png links (same naming post_process.py expects), and the
original tagged draft is kept as book.tagged.md.
"""
import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from typing import List

from extract_frames import materialize, current_source, requested_timestamps
import timecode
from video_id import split_bilibili_id

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_PROFILE_DIR = os.path.join(BASE_DIR, ".capture-profile")


def _resilient_stdout() -> None:
    """中文 Windows 控制台默认是 GBK，打不出的字符会直接抛 UnicodeEncodeError ——
    在「报错」这条路径上再报错最难受（失败清单还没打出来就崩了）。
    这里让打不出的字符退化成 ?，绝不崩。符号本身也尽量只用 GBK 能打的。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass


_resilient_stdout()

# E 层：除 <video> 外全部隐藏（visibility 保留布局，通杀各遮挡层）
HIDE_CSS = ("body *{visibility:hidden!important}"
            "video, video *{visibility:visible!important}")

# F 层判据：**不看文件字节数**。实测「95% 纯黑 + 一行小字」的合法幻灯片压缩后
# 远小于原来的 10000 字节门槛，按字节数判会把正确画面当成黑帧换掉。
# 真正的空帧（媒体没加载出来）是**整幅单色**，所以改判像素统计：
BLANK_STD = 2.0              # 灰度标准差低于此值 = 整幅一个颜色（不分黑白）
BLANK_LEVEL = 12             # 灰度高于此值算「有内容的像素」
# 「有内容的像素」占比低于此值 = 整幅接近全黑。门槛必须**远低于**真实画面的
# 内容占比：实测黑底 + 一行 28px 真字只占 0.39% 像素。早先这里写的是
# 「99 分位灰度 < 12」，而一行小字永远站不上 99 百分位（要 ≥1% 像素才够），
# 于是把最该救的那类画面判成了黑帧 —— 判据本身必须按「有没有内容」来写。
BLANK_CONTENT_RATIO = 0.0005
MIN_SHOT_BYTES = 10000       # 仅在缺 av/numpy 无法解码时退回的字节数兜底
MIN_SHOT_SIDE = 100          # 图宽/高低于此值视为异常
# 截到空帧时用采样到的「边缘密度」判断那一刻本来是不是就没内容（见 _shoot）：
# 低于此值说明那一刻确实是黑屏，不是拍坏了 —— 那就别重拍，免得把 2 秒后的
# 另一页拍下来挂回原时间戳（错图静默入库）。
BLANK_INK = 0.01

# 媒体等待/seek 的超时（毫秒）。没有超时的话，媒体加载失败时
# 'loadeddata' / 'seeked' 永不触发，page.evaluate 会**永久挂住**整轮抓帧。
READY_TIMEOUT_MS = 15000
SEEK_TIMEOUT_MS = 10000
# 采样循环的墙钟上限 = 采样窗口时长 + 这点余量（给缓冲留些余地）
SAMPLE_WALL_SLACK_MS = 6000

# G 层：稳定帧检测 —— AI 给的时间戳常落在"画面还在变化"的瞬间（PPT 逐条出现、
# 插画缩小让位……），直接截图会拍到半成品。做法：在时间戳前后各播一段并采样，
# 选「画面已稳定 且 信息量最大」的时刻来截。
# 关键：不能只等"画面不动" —— 内容被"缩掉"时画面同样会不动，那反而更差
#（实测：120s 完整插画 → 122s 缩成两张小图并排，等到"稳定"反而拍到了瘪的）。
STABLE_INTERVAL_MS = 400     # 采样间隔（毫秒）
STABLE_BACK_SEC = 3.0        # 向目标时刻「之前」多看多少秒
STABLE_FWD_SEC = 6.0         # 向目标时刻「之后」多看多少秒
STABLE_BLOCK_THRESH = 8.0    # 「分块最大差」低于此值视为画面没变（0-255 灰度刻度）
STABLE_NEED = 3              # 连续几次稳定采样才算稳定（3×0.4s ≈ 1.2s）
STABLE_MAX_DIST = 6.0        # 选定时刻与目标时刻的最大允许偏离（秒）
# 提前收工：画面已稳定、且已越过目标时刻 MIN_FWD 秒、且信息量连续 SETTLE 拍
# 不再增长时，就不再空等剩下几秒。MIN_FWD 保证仍会朝「之后」看一段，
# 这样「PPT 逐条出现」还是等得到（不能为了省时间牺牲这个）。
STABLE_MIN_FWD_SEC = 3.0     # 越过目标时刻后至少再看多少秒才允许提前收工
STABLE_SETTLE = 3            # 信息量连续几拍不再增长算「长完了」
STABLE_INK_EPS = 0.01        # 边缘密度增长超过此值才算「还在长内容」


def detect_platform(video_id: str, video_url: str) -> str:
    if video_url:
        if "youtube.com" in video_url or "youtu.be" in video_url:
            return "youtube"
        if "bilibili.com" in video_url:
            return "bilibili"
    if video_id.startswith("BV"):
        return "bilibili"
    return ""


def build_url(platform: str, video_id: str, sec: int) -> str:
    if platform == "bilibili":
        bvid, p = split_bilibili_id(video_id)
        return (f"https://player.bilibili.com/player.html?bvid={bvid}&p={p}"
                f"&t={sec}&autoplay=1&high_quality=1&danmaku=0")
    return f"https://www.youtube.com/embed/{video_id}?start={sec}&autoplay=1"


def build_main_url(platform: str, video_id: str, sec: int) -> str:
    """D 层兜底页面：主站观看页。"""
    if platform == "bilibili":
        bvid, p = split_bilibili_id(video_id)
        return f"https://www.bilibili.com/video/{bvid}/?p={p}&t={sec}"
    return f"https://www.youtube.com/watch?v={video_id}&t={sec}s"


PLACEHOLDER_RE = re.compile(r"SCREENSHOT:(\d{2}:\d{2}:\d{2})(?:\.\d+)?")
ANY_PLACEHOLDER_RE = re.compile(r"SCREENSHOT:([^\s)\]]+)")


def fmt_ts(sec: float) -> str:
    """秒 → HH:MM:SS（全项目统一的截图时间戳格式）。

    格式本身见 timecode.format_hms；这里保留本处旧有的四舍五入
    （fmt_ts(9.6) == "00:00:10"）。
    """
    return timecode.format_hms(round(sec))


def parse_timestamps(source_md: str):
    with open(source_md, encoding="utf-8") as f:
        content = f.read()
    return requested_timestamps(content)


def malformed_placeholders(source_md: str):
    """挑出格式不对的 SCREENSHOT 占位符（如写成 MM:SS 的 `SCREENSHOT:98:26`）。

    这些占位符匹配不上正则，会被**静默跳过**：不截图、不报错，materialize 也
    原样留着占位符。索引图标签一度就写成 MM:SS，很容易让 AI 照着写成
    `SCREENSHOT:98:26`，所以这里显式报出来。
    """
    with open(source_md, encoding="utf-8") as f:
        content = f.read()
    good = set(PLACEHOLDER_RE.findall(content))
    return sorted({m for m in ANY_PLACEHOLDER_RE.findall(content) if m not in good})


def shots_spec(timestamps, platform, video_id):
    """Build [seconds, image-name, embed-url, main-url] rows."""
    shots = []
    for ts in timestamps:
        sec = timecode.parse_seconds(ts)
        shots.append([sec, "shot_" + ts.replace(":", "_"),
                      build_url(platform, video_id, sec),
                      build_main_url(platform, video_id, sec)])
    return shots


def frames_missing(timestamps, imgdir, force=False):
    """缺图、或图不合格（空文件 / 半截 / 整幅单色）都算「需要重截」。

    判据从「文件是否存在」升级为「能不能用」：上一轮中断留下的半张图、
    或旧版本写下的黑帧，都不该再被当成成品 —— 这是「重复图 / 错图」的入口。

    force=True 时忽略「已存在且合格」，全部时间戳都算待重截 —— 给「登录大会员后
    重跑升级画质」用。它**只**改变「要尝试哪些帧」，不放宽任何质量闸门
    （越界 / seek 被忽略 / 空帧仍然不写盘），失败也由 _shoot 保住盘上原有的图。

    命令行上的 `--force-invalid` 要的就是**下面这个默认判据**（只列缺的 / 不合格的），
    所以这里不需要再给它加开关；它与 `--force` 互斥，由 main() 拦。
    """
    if force:
        return list(timestamps)
    out = []
    for ts in timestamps:
        path = os.path.join(imgdir, "shot_" + ts.replace(":", "_") + ".png")
        if not os.path.isfile(path) or not _frame_usable(path)[0]:
            out.append(ts)
    return out


def find_chrome_exe():
    candidates = []
    if sys.platform == "win32":
        for env in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
            base = os.environ.get(env)
            if base:
                candidates.append(os.path.join(base, "Google", "Chrome", "Application", "chrome.exe"))
    elif sys.platform == "darwin":
        candidates.append("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    else:
        import shutil
        for name in ("google-chrome", "google-chrome-stable"):
            exe = shutil.which(name)
            if exe:
                return exe
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return None


def setup_profile(profile_dir: str, open_url: str = "https://www.bilibili.com") -> None:
    """One-time init: open Chrome with the dedicated profile for manual login."""
    exe = find_chrome_exe()
    if not exe:
        sys.exit("Chrome executable not found")
    os.makedirs(profile_dir, exist_ok=True)
    print("opening Chrome with the dedicated capture profile ...")
    print("log in to the platforms you need (bilibili / YouTube) in that window,")
    print("then CLOSE the window to finish setup.")
    subprocess.run([exe, f"--user-data-dir={profile_dir}", open_url])
    print("setup finished; cookies are persisted in", profile_dir)


# ─────────────────────────────────────────────
# v2 pipeline internals
# ─────────────────────────────────────────────

def _force_top_quality(page):
    """C 层：拦截 playurl，只留顶档最高码率变体（优先 AVC 兼容性）。"""
    def handle(route):
        # 注意：route 处理函数**必须**调用 fulfill / continue_ / abort 之一，
        # 什么都不做等于让这个请求永远挂着（播放器取不到流 → 该轮全部"媒体未就绪"）。
        # 旧版在 fetch 失败时会抛 NameError（except 里又用了一次没赋值的 resp）。
        resp = None
        try:
            resp = route.fetch()
            j = resp.json()
            vids = j["data"]["dash"]["video"]
            top = max(v["id"] for v in vids)
            cands = [v for v in vids if v["id"] == top]
            avc = [v for v in cands if v["codecs"].startswith("avc")] or cands
            j["data"]["dash"]["video"] = [max(avc, key=lambda v: v["bandwidth"])]
            route.fulfill(response=resp, json=j)
            return
        except Exception:
            pass
        try:
            if resp is not None:
                route.fulfill(response=resp)   # 改不动就原样放行
            else:
                route.continue_()
        except Exception:
            pass                               # 页面已经关了，没得救
    page.route("**/*playurl*", handle)


def _probe_native_size(page, video_id):
    """B 层：查询顶档原生分辨率 [w,h]，用于 1:1 viewport。"""
    bvid, p = split_bilibili_id(video_id)
    try:
        return page.evaluate("""async ({bvid, p}) => {
            const pages = ((await (await fetch('https://api.bilibili.com/x/web-interface/view?bvid=' + bvid)).json()).data || {}).pages || [];
            const pg = pages.filter(x => x.page === p)[0] || pages[0] || {};
            const cid = pg.cid;
            const d = (await (await fetch('https://api.bilibili.com/x/player/playurl?bvid=' + bvid + '&cid=' + cid + '&qn=120&fnval=4048', {credentials:'include'})).json()).data;
            const vids = ((d || {}).dash || {}).video || [];
            if (!vids.length) return null;
            const top = Math.max.apply(null, vids.map(v => v.id));
            const v = vids.filter(x => x.id === top).sort((a, b) => b.bandwidth - a.bandwidth)[0];
            return [v.width, v.height];
        }""", {"bvid": bvid, "p": p})
    except Exception:
        return None


# G 层 JS：seek 到目标时刻，并**校验是否真的跳过去了**。
# 实测坑 1：播放器不可 seek 时 currentTime 赋值会被静默忽略，而 seeked 事件照常立即
# 触发 —— 结果多个时间戳截出同一张图且不报错。必须显式比对。
# 实测坑 2：赋值给**当前所在位置**时事件可能不触发，所以位置已经对了就不等它，
# 否则会白等到超时（这也是"回跳到同一个时刻"会挂住的原因）。
# 实测坑 3：媒体加载失败（cookie 过期 / 403 / 需要会员 / 地区限制）时 readyState
# 停在 0，'loadeddata' 永不触发 —— 两个 await 都必须带超时，否则 page.evaluate
# 永不返回，整轮抓帧无输出、无异常地永久挂住。
def _seek_js(ready_ms: int = READY_TIMEOUT_MS, seek_ms: int = SEEK_TIMEOUT_MS) -> str:
    """生成 SEEK_JS（超时可注入，测试里用短超时免得白等）。"""
    return _SEEK_JS_TMPL.replace("__READY_MS__", str(ready_ms)) \
                        .replace("__SEEK_MS__", str(seek_ms))


_SEEK_JS_TMPL = """async (sec) => {
    const v = document.querySelector('video');
    if (!v) return {ok: false, reason: '页面里没有 <video> 元素'};
    v.muted = true;
    const sleep = ms => new Promise(r => setTimeout(r, ms));
    // 轮询而不是等 loadeddata 事件：seek 会让 readyState 掉回 1，而
    // 'loadeddata' 每个加载周期只触发一次、不会再补发，等事件会误判成"媒体没加载"。
    const waitReady = async (ms) => {
        const t0 = Date.now();
        while (v.readyState < 2) {
            if (Date.now() - t0 > ms) return false;
            await sleep(100);
        }
        return true;
    };
    if (!(await waitReady(__READY_MS__)))
        return {ok: false, reason: '媒体未就绪 readyState=' + v.readyState
                + '（加载失败 / 未登录 / 地区或会员限制）'};

    const dur = isFinite(v.duration) ? v.duration : 0;
    const target = dur ? Math.max(0, Math.min(sec, dur - 0.5)) : sec;
    if (Math.abs(v.currentTime - target) > 0.05) {
        v.currentTime = target;
        // 同样轮询：seeked 事件可能早于监听、也可能压根不触发，
        // 以「不再 seeking」为准，再比对位置来判断 seek 有没有被忽略。
        await sleep(50);
        const t0 = Date.now();
        while (v.seeking && Date.now() - t0 < __SEEK_MS__) await sleep(100);
        if (v.seeking)
            return {ok: false, reason: 'seek 无响应（超时仍在 seeking）', requested: sec,
                    asked: target, actual: v.currentTime, dur: dur};
    }
    v.pause();
    const actual = v.currentTime;
    const ok = Math.abs(actual - target) < 1.5;
    return {ok: ok, reason: ok ? '' : 'seek 被忽略（currentTime 赋值未生效）',
            requested: sec, asked: target, actual: actual, dur: dur};
}"""
SEEK_JS = _seek_js()

# G 层 JS：从当前时刻起边播边采样，记录每一拍的「画面变化量」和「信息量」。
# 关键点 1：用「分块最大差」而非全画面平均差 —— 幻灯片只占画面一小块时
#（讲师特写 + PPT 画中画版式），整幅平均会把局部翻页/动画彻底稀释掉。
# 关键点 2：同时记录「边缘密度」作为信息量代理，用来识破"内容被缩掉"的情况。
# 关键点 3：**必须有墙钟上限** —— 播放停滞（缓冲耗尽 / 网络抖动）时 v.paused 与
# v.ended 都是 false，而 currentTime 不再推进，原来的 while 会变成页内死循环，
# page.evaluate 永不返回，整轮抓帧静默卡死。
# 关键点 4：提前收工 —— 画面已稳定、已越过目标时刻 minFwdSec 秒、且信息量连续
# 若干拍不再增长时，不再空等剩下的秒数（每张图最多省 3 秒实时播放）。
SAMPLE_JS = """async ({intervalMs, windowSec, blockThresh, needStable, targetSec,
                     wallMs, minFwdSec, settle, inkEps}) => {
    const v = document.querySelector('video');
    if (!v) return {samples: [], stalled: false, reason: '页面里没有 <video> 元素'};
    const c = document.createElement('canvas');
    c.width = 32; c.height = 18;
    const ctx = c.getContext('2d', {willReadFrequently: true});
    const grab = () => { ctx.drawImage(v, 0, 0, 32, 18); return ctx.getImageData(0, 0, 32, 18).data; };
    const gray = (d, i) => (d[i] + d[i+1] + d[i+2]) / 3;
    // 8x6 分块（每块 4x3 像素），返回「变化最大的那一块」的平均差
    const blockDiff = (a, b) => {
        let mx = 0;
        for (let by = 0; by < 6; by++) {
            for (let bx = 0; bx < 8; bx++) {
                let s = 0;
                for (let y = by * 3; y < by * 3 + 3; y++) {
                    for (let x = bx * 4; x < bx * 4 + 4; x++) {
                        const i = (y * 32 + x) * 4;
                        s += Math.abs(gray(a, i) - gray(b, i));
                    }
                }
                mx = Math.max(mx, s / 12);
            }
        }
        return mx;
    };
    // 边缘密度：相邻像素灰度差超过阈值的比例，作为「画面信息量」的代理
    const inkDensity = (d) => {
        let e = 0, n = 0;
        for (let y = 0; y < 18; y++) {
            for (let x = 0; x < 31; x++) {
                const i1 = (y * 32 + x) * 4, i2 = i1 + 4;
                if (Math.abs(gray(d, i1) - gray(d, i2)) > 25) e++;
                n++;
            }
        }
        return e / n;
    };
    const sleep = ms => new Promise(r => setTimeout(r, ms));
    try { await v.play(); } catch (e) { return {samples: [], stalled: false, reason: 'play() 被拒绝: ' + e}; }
    const out = [];
    let prev = null, run = 0, stalled = false;
    let peakInk = -1, quiet = 0;
    const t0 = v.currentTime, wall0 = Date.now();
    while (v.currentTime - t0 < windowSec) {
        if (v.paused || v.ended) break;                        // 播完 / 被页面暂停
        if (Date.now() - wall0 > wallMs) { stalled = true; break; }   // 墙钟兜底
        if (v.readyState >= 2) {          // 缓冲中不取样，免得把黑屏当成"稳定"
            const cur = grab();
            const d = prev ? blockDiff(prev, cur) : 0;
            run = (d < blockThresh) ? run + 1 : 0;
            const ink = inkDensity(cur);
            out.push({t: v.currentTime, diff: d, ink: ink, stable: run >= needStable});
            prev = cur;
            if (ink > peakInk + inkEps) { peakInk = ink; quiet = 0; } else { quiet += 1; }
            if (run >= needStable && v.currentTime - targetSec >= minFwdSec
                && quiet >= settle) break;                     // 稳定且长完了
        }
        await sleep(intervalMs);
    }
    v.pause();
    return {samples: out, stalled: stalled, reason: ''};
}"""


@dataclass
class ShotResult:
    """一次截图尝试的结果：够 main 判定成败，也够日志说明「为什么没出图」。"""

    ok: bool
    name: str
    requested: float
    duration: float = 0.0
    picked: float = 0.0
    actual: float = 0.0
    how: str = ""
    retried: bool = False
    reason: str = ""


def _pick_stable(samples, target, bounds=None):
    """从采样序列里挑最适合截图的时刻，返回 (秒, 选法标签)。

    按优先级：
      1. 距目标 STABLE_MAX_DIST 秒以内、画面已稳定 → 取其中信息量最大的一拍。
         既能等 PPT 逐条出现播完（信息量增长后稳定），
         也不会被"内容缩小让位"骗到（那种情况稳定后信息量反而更低）。
      2. 没有这样的点 → 离目标最近的稳定点（标签 stable-far）。
      3. 全程没有稳定点（讲师持续走动 / 镜头一直平移）→ 取距目标
         STABLE_MAX_DIST 以内信息量最大的一拍（标签 unstable）。

    旧版这里的兜底是 `samples[-1]["t"]`，也就是采样窗口的最后一拍
    （约等于目标 +6 秒）：文件名和正文链接写着 00:02:00，画面却来自 00:02:05.9，
    而且 STABLE_MAX_DIST 从来没被用上（死代码）。

    话虽如此：现在的采样窗口是 [目标-3, 目标+6]，正好落在 ±STABLE_MAX_DIST 之内，
    所以第 1 档的「近点」过滤在生产里几乎恒等于"全部稳定点"，第 2 档很少命中 ——
    它是**将来放宽采样窗口时的安全网**，别把它当成现在的主力判据。
    """
    if bounds is not None:
        samples = [s for s in samples if bounds[0] <= s["t"] < bounds[1]]
        if not samples:
            return target, "indexed-target"
    if not samples:
        return target, "no-samples"
    near_stable = [s for s in samples
                   if s.get("stable") and abs(s["t"] - target) <= STABLE_MAX_DIST]
    if near_stable:
        return max(near_stable, key=lambda s: s["ink"])["t"], "stable-near"
    stable = [s for s in samples if s.get("stable")]
    if stable:
        return min(stable, key=lambda s: abs(s["t"] - target))["t"], "stable-far"
    pool = [s for s in samples if abs(s["t"] - target) <= STABLE_MAX_DIST] or samples
    return max(pool, key=lambda s: s["ink"])["t"], "unstable"


def _frame_usable(path: str):
    """F 层判据：解码得出、尺寸正常、不是整幅单色也不是整幅全黑。

    返回 (True, "") 或 (False, 原因)。刻意**不用文件字节数**：实测
    「95% 纯黑 + 一行小字」的合法幻灯片压缩后远小于原先 10000 字节的门槛，
    按字节数判会把本来正确的画面当黑帧换掉（换成更差的）。
    也顺带挡住上一轮中断留下的半截文件（解不开）。
    """
    if not os.path.isfile(path) or os.path.getsize(path) == 0:
        return False, "文件为空"
    try:
        import av
        import numpy as np
    except ImportError:                      # 没装解码库时退回字节数兜底
        n = os.path.getsize(path)
        return (n >= MIN_SHOT_BYTES,
                "" if n >= MIN_SHOT_BYTES else f"仅 {n} 字节（无 av/numpy 可解码）")
    try:
        with av.open(path) as c:
            arr = next(c.decode(video=0)).to_ndarray(format="rgb24")
    except Exception as e:
        return False, f"图片无法解码（{type(e).__name__}，可能是半截文件）"
    h, w = arr.shape[:2]
    if w < MIN_SHOT_SIDE or h < MIN_SHOT_SIDE:
        return False, f"尺寸异常 {w}x{h}"
    gray = np.asarray(arr, dtype=np.float32).mean(axis=2)
    std = float(gray.std())
    if std < BLANK_STD:
        return False, f"整幅一个颜色（std={std:.2f}）"
    content = float((gray > BLANK_LEVEL).mean())
    if content < BLANK_CONTENT_RATIO:
        return False, f"整幅接近全黑（有内容的像素只占 {content * 100:.3f}%）"
    return True, ""


def _unlink(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _sampled_ink(samples, target):
    """采样序列里离 target 最近那一拍的「边缘密度」（越小 = 画面越空）。

    没有任何采样时返回 None（没证据，不据此下结论）。
    """
    if not samples:
        return None
    return min(samples, key=lambda s: abs(s["t"] - target)).get("ink")


def _grab(page, out_path: str):
    """定住画面 → 隐藏 UI → 拍 <video> 元素 → 判空帧。返回 (ok, 原因)。"""
    page.wait_for_timeout(200)
    page.add_style_tag(content=HIDE_CSS)
    page.locator("video").screenshot(path=out_path)
    return _frame_usable(out_path)


def _shoot(page, sec, name, imgdir, urls, bounds=None) -> ShotResult:
    """D+E+G+F 层：embed 优先/主站兜底；越界与 seek 校验；稳定帧挑选；元素截图；空帧重拍。

    任何一步不合格都返回 ok=False **且不留下图片文件** —— 错图比缺图更糟：
    下游（materialize → book.md → 网页）根本看不出那张图是错的。
    """
    r = ShotResult(ok=False, name=name, requested=float(sec))
    last_err = ""
    for url in urls:
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_selector("video", timeout=20000)
            last_err = ""
            break
        except Exception as e:
            last_err = (str(e) or "").splitlines()[0] or type(e).__name__
    if last_err:
        r.reason = "页面/播放器未加载：" + last_err
        return r
    page.add_style_tag(content=HIDE_CSS)

    start = max(0.0, sec - STABLE_BACK_SEC)
    info = page.evaluate(SEEK_JS, start)
    if not info.get("ok"):
        r.reason = f"seek 到采样起点失败：{info.get('reason') or '未知'}"
        return r
    r.duration = float(info.get("dur") or 0)

    # 越界检查只能在这边做：SEEK_JS 内部是「先夹断、再拿夹断后的值跟自己比」，
    # 越界的时间戳夹完必然相等，永远返回 ok —— 静默失败 B 结构上就发现不了。
    # 留 0.5 秒余量：要片尾最后一帧（sec == 片长）是合法请求，只晚半秒不算越界。
    if r.duration and sec > r.duration + 0.5:
        r.reason = f"时间戳越界：片长 {r.duration:.0f}s，却要求 {sec}s 的画面"
        return r

    smp = page.evaluate(SAMPLE_JS, {
        "intervalMs": STABLE_INTERVAL_MS,
        "windowSec": STABLE_BACK_SEC + STABLE_FWD_SEC,
        "blockThresh": STABLE_BLOCK_THRESH,
        "needStable": STABLE_NEED,
        "targetSec": sec,
        "wallMs": int((STABLE_BACK_SEC + STABLE_FWD_SEC) * 1000) + SAMPLE_WALL_SLACK_MS,
        "minFwdSec": STABLE_MIN_FWD_SEC,
        "settle": STABLE_SETTLE,
        "inkEps": STABLE_INK_EPS}) or {}
    samples = smp.get("samples") or []
    if smp.get("stalled"):
        print("  [!] 采样期间播放停滞（缓冲/网络），已提前收工")
    if samples:
        print(f"  G: 采样 {len(samples)} 帧（覆盖 {samples[-1]['t'] - samples[0]['t']:.1f}s）")
    else:
        print("  G: 采样失败，按目标时刻截图" + (f"（{smp.get('reason')}）"
                                                 if smp.get("reason") else ""))
    pick, how = _pick_stable(samples, sec, bounds)
    r.picked, r.how = pick, how
    if samples:
        best = max(samples, key=lambda s: s["ink"])
        print(f"     目标 {sec}s → 选定 {pick:.1f}s [{how}]"
              f"（最高信息量 {best['ink']:.3f} 在 {best['t']:.1f}s）")

    info = page.evaluate(SEEK_JS, pick)
    if not info.get("ok"):
        r.reason = f"回跳到选定时刻 {pick:.1f}s 失败：{info.get('reason') or '未知'}"
        return r
    r.actual = float(info.get("actual") or 0.0)
    if bounds is not None and not (bounds[0] <= r.actual < bounds[1]):
        r.reason = "播放器实际停点越过索引画面边界，需重新选择时间点"
        return r

    # 先拍进 .partial.png：只有验合格才 os.replace 成正式图，
    # 这样误判/失败都不会破坏 images/ 里可能已经存在的同名图。
    path = os.path.join(imgdir, name + ".png")
    tmp = os.path.join(imgdir, name + ".partial.png")
    ok, why = _grab(page, tmp)
    if not ok:
        ink = _sampled_ink(samples, pick)
        if ink is not None and ink < BLANK_INK:
            # 采样时那一刻边缘密度就是 0 —— 说明目标时刻**本身**是黑屏，不是拍坏了。
            # 重拍只会把 2 秒后的另一页拍下来挂回原时间戳（又变成"错图静默入库"）。
            print(f"  QA: {why}；采样显示目标时刻本身就是黑屏，不重拍")
            _unlink(tmp)
            r.reason = f"空帧/黑帧：{why}（目标时刻的画面本身没有内容）"
            return r
        print(f"  QA: {why}，偏移 +2s 重拍一次")
        retry = pick + 2.0
        if bounds is not None and not (bounds[0] <= retry < bounds[1]):
            _unlink(tmp)
            r.reason = f"空帧/黑帧：{why}（偏移重拍会越过索引画面边界，需人工换时间点）"
            return r
        if r.duration:                       # 别夹出片外（否则等于重拍同一张）
            retry = min(retry, max(0.0, r.duration - 0.5))
        info2 = page.evaluate(SEEK_JS, retry)
        if info2.get("ok"):
            r.retried = True
            r.picked, r.actual = retry, float(info2.get("actual") or 0.0)
            if bounds is not None and not (bounds[0] <= r.actual < bounds[1]):
                _unlink(tmp)
                r.reason = "重拍实际停点越过索引画面边界，需重新选择时间点"
                return r
            ok, why = _grab(page, tmp)       # 重拍后必须重新验，不能想当然
        else:
            ok, why = False, "重拍 seek 失败：" + (info2.get("reason") or "未知")

    if not ok:
        _unlink(tmp)                         # 不合格的图不许留在 images/ 里
        r.reason = f"空帧/黑帧：{why}"
        return r
    os.replace(tmp, path)
    r.ok = True
    print(f"saved {name}.png  实际 {r.actual:.1f}s" + ("（已重拍）" if r.retried else ""))
    return r


def indexed_bounds(imgdir, video_id):
    """仅用匹配当前视频的完整目录约束截图，不依赖 overview 是否展示。"""
    path = os.path.join(os.path.dirname(imgdir), "sprite_index.json")
    try:
        with open(path, encoding="utf-8") as f:
            index = json.load(f)
        if index.get("video_id") != video_id:
            return []
        return [(float(c["since"]), float(c["until"]))
                for c in index.get("candidates", [])
                if float(c["since"]) < float(c["until"])]
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return []


def _run_capture(p, shots, imgdir, profile_dir, video_id, platform, headless) -> List[ShotResult]:
    ctx = p.chromium.launch_persistent_context(
        profile_dir, channel="chrome", headless=headless,
        viewport={"width": 1280, "height": 720},
        args=["--autoplay-policy=no-user-gesture-required"])
    try:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        _force_top_quality(page)
        if platform == "bilibili":
            page.goto("https://www.bilibili.com", wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(1000)
            dims = _probe_native_size(page, video_id)
            if dims:
                print(f">> native stream size: {dims[0]}x{dims[1]} (viewport 1:1)")
                page.set_viewport_size({"width": dims[0], "height": dims[1]})
        results: List[ShotResult] = []
        intervals = indexed_bounds(imgdir, video_id)
        for sec, name, embed, main in shots:
            try:
                bounds = next((b for b in intervals if b[0] <= sec < b[1]), None)
                results.append(_shoot(page, sec, name, imgdir, [embed, main], bounds))
            except Exception as e:
                # 单帧的任何异常（元素消失 / 跨域污染 / 页面里冒出第二个 <video> ……）
                # 只作废这一帧，不能报废整轮（旧版异常会穿出去，整轮只剩半截图）
                results.append(ShotResult(
                    ok=False, name=name, requested=float(sec),
                    reason=f"{type(e).__name__}: {(str(e) or '').splitlines()[0]}"))
        return results
    finally:
        ctx.close()


MANIFEST_NAME = "capture_manifest.json"


def _read_manifest(path: str):
    """读回上一次的 manifest（读不出来/坏了当没有）。

    它只是旁证：坏了不该挡住截图，更不该被当成「盘上的图有问题」的证据。
    返回 {帧名: 记录}。
    """
    try:
        with open(path, encoding="utf-8") as f:
            old = json.load(f)
    except (OSError, ValueError):
        return {}
    frames = old.get("frames") if isinstance(old, dict) else None
    return {f["name"]: f for f in frames or []
            if isinstance(f, dict) and f.get("name")}


def write_manifest(vdir, video_id, platform, method, source_md, timestamps, results):
    """把本轮每帧的结果写进 output/<video_id>/capture_manifest.json（与 sprite_index.json 同级）。

    为什么需要它：G 层会在目标时刻 ±6 秒内挑「信息量最大」的一拍，文件名/图注
    写着 00:02:00、画面却可能来自 00:02:05.9 —— 在此之前这个信息只打印到控制台，
    事后没有任何办法审计「这张图到底截自哪一秒」。manifest 把 ShotResult 原样
    落盘，requested（目标秒）/ picked（选定秒）/ actual（实际秒）/ retried（是否
    +2s 重拍）/ reason（为什么没出图）就都留了痕。

    重跑时**按帧名合并**，而不是整份覆盖：一轮下来常常只补 1~2 张缺图，覆盖会把
    其余十几张图的记录一起抹掉 —— 那恰恰是这份文件唯一的用处。同时按当前稿件
    「剪枝」：稿子里已经没有的时间戳不再保留（那张图也不再被正文引用）；本轮没有
    重截的帧沿用上一次的记录。失败帧**照样写进去**（ok=False + reason），不静默
    消失 —— manifest 里没有它反而更糟，看起来像「这一帧没人管过」。
    """
    path = os.path.join(vdir, MANIFEST_NAME)
    names = {"shot_" + ts.replace(":", "_") for ts in timestamps}
    frames = {n: f for n, f in _read_manifest(path).items() if n in names}
    for r in results:
        frames[r.name] = asdict(r)
    data = {
        "video_id": video_id,
        "platform": platform,
        "method": method,                 # 这一轮的截帧方法（dedicated …）
        "source": os.path.basename(source_md),   # 本轮按哪份稿件的时间戳截（book.md / book.tagged.md）
        "frames_needed": len(names),      # 稿件需要的帧数：条目比它少就是还有帧没截过
        "frames": [frames[n] for n in sorted(frames)],
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return path


def try_dedicated(shots, imgdir, profile_dir, video_id="", platform=""):
    """Capture with the dedicated logged-in profile via Playwright (v2 pipeline).

    返回 (status, detail, results)，status ∈ {unavailable, ok, partial, failed}。
    partial 不再触发 headless→headful 重试：那不是浏览器的问题，换个模式重跑
    只会把同样的时间戳再跑一遍（旧版会这么白跑一整轮）。
    """
    if not os.path.isdir(profile_dir) or not os.listdir(profile_dir):
        return ("unavailable", "capture profile not initialized (run: python capture_frames.py --setup-profile)", [])
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return ("unavailable", "Playwright not installed (python -m pip install playwright)", [])
    last_err = ""
    for headless in (True, False):  # A 层：headless 优先，回退 headful
        try:
            with sync_playwright() as p:
                results = _run_capture(p, shots, imgdir, profile_dir, video_id, platform, headless)
        except Exception as e:
            last_err = f"headless={headless} failed: {(str(e) or '').splitlines()[0]}"
            continue
        bad = [r for r in results if not r.ok]
        if not bad:
            return ("ok", f"captured {len(results)} frames", results)
        return ("partial", f"{len(bad)}/{len(results)} 帧未产出", results)
    return ("failed", last_err, [])


METHOD_ORDER = ["dedicated"]
METHOD_DESC = {
    "dedicated": "dedicated capture profile (v2: headless, forced top quality, native size, precise seek)",
}


def main() -> None:
    ap = argparse.ArgumentParser(description="Capture frames directly from the platform player")
    ap.add_argument("video_id", nargs="?")
    ap.add_argument("video_url", nargs="?", default=None)
    ap.add_argument("--method", default="auto", choices=["auto"] + METHOD_ORDER)
    ap.add_argument("--materialize-only", action="store_true",
                    help="skip capture; materialize placeholders from existing images only")
    ap.add_argument("--force", action="store_true",
                    help="recapture every timestamp in the source draft, including frames "
                         "that already exist and pass QA (use it after logging in to a "
                         "higher membership tier); as always, a frame that fails QA writes "
                         "nothing and leaves the existing image untouched. "
                         "Cannot be combined with --force-invalid or --materialize-only")
    ap.add_argument("--force-invalid", action="store_true",
                    help="recapture only the timestamps whose image is missing or fails QA "
                         "(empty / truncated / uniform / blank / too small) - the same rule a "
                         "plain run uses, spelled out explicitly. Unlike --force it does NOT "
                         "re-shoot frames that already exist and pass QA, so use --force to "
                         "upgrade quality after logging in to a higher membership tier. "
                         "Neither flag bypasses any QA gate. "
                         "Cannot be combined with --force or --materialize-only")
    ap.add_argument("--setup-profile", action="store_true",
                    help="one-time: open Chrome with the dedicated profile to log in")
    ap.add_argument("--profile-dir", default=DEFAULT_PROFILE_DIR,
                    help="dedicated Chrome user-data-dir (default: <repo>/.capture-profile)")
    args = ap.parse_args()

    if args.setup_profile:
        setup_profile(args.profile_dir)
        return

    # 互斥检查都放在碰任何文件之前：报错就一个字都不写盘。
    if args.materialize_only and (args.force or args.force_invalid):
        # 自相矛盾：--materialize-only 只物化既有图、一张都不截，--force /
        # --force-invalid 只影响「要截哪些帧」。静默忽略会让人以为已经强制重截过
        # —— 那正是本次要修的「空转」的翻版，所以直接拒绝这个组合。
        flag = "--force" if args.force else "--force-invalid"
        ap.error(f"{flag} cannot be combined with --materialize-only "
                 f"({flag} recaptures frames; --materialize-only only links existing ones)")

    if args.force and args.force_invalid:
        # 一个说「全部重截」（升画质用），一个说「只补缺的/坏的」：同时给必然有一个
        # 是误输入，而两种做法差着「要不要把合格的图也换掉」，不能猜。
        ap.error("--force and --force-invalid cannot be combined "
                 "(--force recaptures every timestamp; --force-invalid recaptures "
                 "only the frames that are missing or fail QA)")

    if args.video_id:
        platform = detect_platform(args.video_id, args.video_url)
        if not platform:
            sys.exit("unsupported platform: pass the video url as 2nd arg")
    else:
        platform = ""

    if not args.video_id:
        ap.error("video_id is required (unless using --setup-profile)")

    vdir = os.path.join(BASE_DIR, "output", args.video_id)
    book = os.path.join(vdir, "book.md")
    tagged = os.path.join(vdir, "book.tagged.md")
    imgdir = os.path.join(vdir, "images")
    os.makedirs(imgdir, exist_ok=True)

    source_md = current_source(book, tagged)
    if not os.path.exists(source_md):
        sys.exit("no book markdown found: " + source_md)
    timestamps = parse_timestamps(source_md)
    bad_ph = malformed_placeholders(source_md)
    if bad_ph:
        print(f"[!] {len(bad_ph)} 个 SCREENSHOT 占位符格式不对，会被直接忽略："
              + ", ".join("SCREENSHOT:" + b for b in bad_ph[:5])
              + (" …" if len(bad_ph) > 5 else "")
              + "（应为 HH:MM:SS，例如 SCREENSHOT:01:38:26）")
    if not timestamps:
        sys.exit("no SCREENSHOT placeholders found in " + source_md)

    if args.materialize_only:
        missing = frames_missing(timestamps, imgdir)
        if missing:
            # 硬门禁的对偶面：缺图就不能报成功，否则这一步会以退出码 0 收场，
            # 把"缺图"从另一头放过去（instructions.md 让 AI 跑的就是这条命令）。
            print(f"× {len(missing)} 帧缺失或不合格，不物化：" + ", ".join(missing))
            print("请先补拍（python src/capture_frames.py <video_id>），"
                  "不要在这个状态下发布。")
            sys.exit(1)
        materialize(book, tagged, imgdir)
        return

    missing = frames_missing(timestamps, imgdir, force=args.force)
    if not missing:
        print("all frames present; nothing to capture")
        materialize(book, tagged, imgdir)
        return
    shots = shots_spec(missing, platform, args.video_id)
    mode = ("--force: all of them, existing images will be replaced" if args.force else
            "--force-invalid: only the frames that are missing or fail QA"
            if args.force_invalid else "")
    print(f">> {len(missing)} frame(s) to capture" + (f" ({mode})" if mode else ""))
    order = METHOD_ORDER if args.method == "auto" else [args.method]
    used = None
    for name in order:
        print(f"\n>>> trying [{name}] {METHOD_DESC[name]}")
        status, detail, results = try_dedicated(shots, imgdir, args.profile_dir,
                                                args.video_id, platform)
        if status == "ok":
            used = name
            print(f"<<< [{name}] OK: captured {len(timestamps)} frames")
            break
        if status == "partial":
            # 硬门禁：有帧没出图就不再往下走（不 materialize、退出码 1）。
            # 错图会被下游当成成品，而缺图至少看得见。
            failed = [r for r in results if not r.ok]
            print(f"<<< [{name}] × {len(failed)}/{len(results)} 帧没出图"
                  f"（已产出的帧留在 images/，下次只补缺的）:")
            for r in failed:
                print(f"      × {fmt_ts(r.requested)}  {r.reason}")
            # 失败帧也要留档（含失败原因），别等失败修好之后才发现「当时为什么没出图」无从查起。
            write_manifest(vdir, args.video_id, platform, name, source_md, timestamps, results)
            print(f"manifest: output/{args.video_id}/{MANIFEST_NAME}")
            print("\n请先修掉上面这些时间戳（写在 book.tagged.md / book.md 里），"
                  "再跑同一条命令：只会补拍缺的帧，已出的图不会重跑。")
            sys.exit(1)
        print(f"<<< [{name}] {detail}")

    if used is None:
        sys.exit("\nAll browser capture methods failed. Use the fallback:\n"
                 f'  python extract_frames.py {args.video_id} "{args.video_url or ""}"')

    write_manifest(vdir, args.video_id, platform, used, source_md, timestamps, results)
    print(f"manifest: output/{args.video_id}/{MANIFEST_NAME}（每张图实际截自哪一秒）")
    materialize(book, tagged, imgdir)


if __name__ == "__main__":
    main()
