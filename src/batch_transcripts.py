"""批量抓取多分P 视频的字幕。

单集版 dump_transcript.py 每次都会重新用无头 Chrome 导出一次 cookies（约 10 秒），
100 集就是白等十几分钟。本脚本把 cookies 导出一次、全程复用，并跳过已完成的集数，
失败不中断（最后打印失败清单，重跑即可补齐）。

用法：
    python src/batch_transcripts.py --bvid BV1dr4y1n7vA --start 1 --end 100
    python src/batch_transcripts.py --bvid BV1dr4y1n7vA --only 1,2,3
"""
import argparse
import json
import os
import sys
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import get_video_dir
from login_utils import DEFAULT_PROFILE_DIR, export_cookies, has_login_cookie
from scraper import get_transcript
from transcript_quality import coverage_ratio, is_usable, MIN_COVERAGE
from video_id import make_bilibili_episode_id


def parse_args():
    ap = argparse.ArgumentParser(description="批量抓取 B 站多分P 视频字幕")
    ap.add_argument("--bvid", required=True, help="视频号，如 BV1dr4y1n7vA")
    ap.add_argument("--start", type=int, default=1, help="起始分P（含）")
    ap.add_argument("--end", type=int, default=0, help="结束分P（含），0 表示到最后一P")
    ap.add_argument("--only", default="", help="只抓这些分P，逗号分隔，如 1,2,3")
    ap.add_argument("--force", action="store_true", help="已存在的也重新抓")
    ap.add_argument("--retries", type=int, default=2, help="单集失败重试次数")
    return ap.parse_args()


def transcript_file_ready(path):
    """已有字幕文件必须可读、有正文且通过覆盖率门槛，才能跳过。"""
    try:
        with open(path, encoding="utf-8") as f:
            return is_usable(json.load(f))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return False


def main():
    args = parse_args()

    if args.only:
        pages = [int(x) for x in args.only.replace("，", ",").split(",") if x.strip()]
    else:
        # 用 view 接口拿总 P 数，避免多打一次网络请求
        from scraper import _http_json
        view = _http_json(f"https://api.bilibili.com/x/web-interface/view?bvid={args.bvid}")
        total = len((view.get("data") or {}).get("pages") or [])
        end = args.end or total
        pages = list(range(args.start, min(end, total) + 1))

    todo = []
    for p in pages:
        episode_id = make_bilibili_episode_id(args.bvid, p)
        path = os.path.join(get_video_dir(episode_id), "transcript.json")
        if args.force or not transcript_file_ready(path):
            todo.append(p)

    print(f">> 共 {len(pages)} 集，待抓 {len(todo)} 集（已有且通过检查的自动跳过）")
    if not todo:
        print("√ 全部已就绪，无需抓取")
        return

    cookies_file = None
    if os.path.isdir(DEFAULT_PROFILE_DIR) and os.listdir(DEFAULT_PROFILE_DIR):
        try:
            cookies_file = export_cookies()
            if not has_login_cookie(cookies_file):
                print(">> 专用配置无登录态，请先运行: python src/capture_frames.py --setup-profile")
        except Exception as e:
            print(f">> 导出 cookies 失败: {e}")

    ok, failed = [], []
    t0 = time.time()
    try:
        for i, p in enumerate(todo, 1):
            url = f"https://www.bilibili.com/video/{args.bvid}?p={p}"
            print(f"\n[{i}/{len(todo)}] p{p}  {url}")
            result = None
            coverage = None
            for attempt in range(1, args.retries + 2):
                try:
                    candidate = get_transcript(url, cookies_file=cookies_file)
                    coverage = coverage_ratio(candidate) if candidate else None
                    if is_usable(candidate):
                        result = candidate
                        break
                    if candidate and candidate.get("segments") and coverage is not None:
                        print(f"    第 {attempt} 次覆盖率仅 {coverage:.1%}，"
                              f"低于 {MIN_COVERAGE:.0%}")
                except Exception as e:
                    print(f"    第 {attempt} 次失败: {e}")
                if attempt <= args.retries:
                    time.sleep(3 * attempt)
            if not result:
                print(f"    × p{p} 抓取失败（已跳过，稍后可重跑补齐）")
                failed.append(p)
                continue

            segs = result.get("segments") or []
            out_file = os.path.join(get_video_dir(result["video_id"]), "transcript.json")
            with open(out_file, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
            coverage_text = f"{coverage:.1%}" if coverage is not None else "无法计算"
            print(f"    √ {result['video_id']}  覆盖率 {coverage_text}  {len(segs)} 段")
            ok.append(p)
    finally:
        if cookies_file and os.path.exists(cookies_file):
            os.remove(cookies_file)

    mins = (time.time() - t0) / 60
    print(f"\n{'='*60}")
    print(f"完成 {len(ok)}/{len(todo)} 集，用时 {mins:.1f} 分钟")
    if failed:
        print(f"× 失败 {len(failed)} 集: {failed}")
        print(f"   重跑可补齐: python src/batch_transcripts.py --bvid {args.bvid} "
              f"--only {','.join(str(p) for p in failed)}")
        sys.exit(1)
    print("√ 全部成功")


if __name__ == "__main__":
    main()
