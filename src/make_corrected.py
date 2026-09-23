"""由 transcript.json 生成 AI 修正版字幕对照稿 transcript.corrected.txt。

原则（用户约定）：逐段保留讲师原始字词与顺序，不合并、不改写为书面语；
仅做两类修改——(1) ASR 错词替换（MAP）；(2) 口癖清理（纯语气词整段删除、
句尾语气词剥离、单字口吃叠词折叠）。MAP 可按视频扩充。

用法: python make_corrected.py <video_id> [<video_id> ...] | --all
"""
import argparse
import json
import os
import re
import sys

import timecode

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ASR 错词 -> 正确词（按长度降序应用，避免子串误伤）
MAP = {
    "深圳市软件工程": "生成式软件工程",
    "英拉SEMBLY": "内联汇编",
    "英外SAMBLY": "内联汇编",
    "inline sembly": "内联汇编",
    "英line sembly": "内联汇编",
    "chain of salt": "chain of thought",
    "chal thought": "chain of thought",
    "chef s": "chain of thought",
    "chap out": "chain of thought",
    "chp out": "chain of thought",
    "CHAPSP": "chain of thought",
    "test time skilling": "test-time scaling",
    "试time skilling": "test-time scaling",
    "cloud opo4.5": "Claude Opus 4.5",
    "MANUEL伯纳姆": "Manuel Blum",
    "hugin face": "Hugging Face",
    "open street map": "OpenStreetMap",
    "home brew": "Homebrew",
    "exterminate js": "xterm.js",
    "xterm js": "xterm.js",
    "deep sv4flash": "DeepSeek",
    "deep sick with the flash": "DeepSeek",
    "deep pick": "DeepSeek",
    "deep chick": "DeepSeek",
    "D4C": "DeepSeek",
    "DIVSK": "DeepSeek",
    "KIMIK3": "Kimi K3",
    "GPT5.6": "GPT-5.6",
    "GBT5.6": "GPT-5.6",
    "GPP5.6": "GPT-5.6",
    "GP5.6": "GPT-5.6",
    "GPT56": "GPT-5.6",
    "cheat gp d": "ChatGPT",
    "CHEGBT": "ChatGPT",
    "拆GBT": "ChatGPT",
    "拆GPT": "ChatGPT",
    "拆GPA": "ChatGPT",
    "terry machine": "Turing machine",
    "church tcs": "Church-Turing 论题",
    "habalton pass": "哈密顿路径",
    "ham alton": "哈密顿",
    "three reset": "3-SAT",
    "justin time": "just-in-time",
    "include pass": "include path",
    "yo mode": "YOLO mode",
    "low list": "allowlist",
    "AI slap": "AI slop",
    "passer": "parser",
    "sober": "solver",
    "agents点MD": "agents.md",
    "AGENTS点MD": "agents.md",
    "agent4点MD": "agents.md",
    "agency md": "agents.md",
    "H4点MD": "agents.md",
    "卢卡": "LUCA",
    "杠I": "-I",
    "在ID里": "在 IDE 里",
    "ID里面": "IDE 里面",
    "ID的": "IDE 的",
    "GBT": "ChatGPT",
    "威尔法尔": "verifier",
    "威尔法": "verifier",
    "VERIFILE": "verifier",
    "VERIFI": "verifier",
    "WIFI": "verifier",
    "linux": "Linux",
    # ── BV1kybV6DE47 软件仓库管理（本地 large-v3 ASR 稿）──
    "GP16": "GPT-6",
    "GP6": "GPT-6",
    "GP5.6": "GPT-5.6",
    "GP5": "GPT-5",
    "义父楼": "逸夫楼",
    "光山玩具": "光栅玩具",
    "光山尺": "光栅尺",
    "巨深赛道": "具身赛道",
    "操系统": "操作系统",
    "外部": "外包",
    "webcoding": "vibe coding",
    "web coding": "vibe coding",
    "webcode": "vibe coding",
    "Vichy Studio": "Visual Studio",
    "hello.ce": "hello.c",
    "hello.ca": "hello.c",
    "git积交": "git 提交",
    "Basic Practice": "best practice",
    "confessional commits": "Conventional Commits",
    "chat gpg": "ChatGPT",
    "fixed井三": "fixes #3",
    "井三": "#3",
    ".ds-store": ".DS_Store",
    ".dstor": ".DS_Store",
    "DS Store": ".DS_Store",
    "command and fund": "command not found",
    "GH或者GLab": "gh 或者 glab",
    "GLab": "glab",
    "一个go说": "一个 goal 说",
    "汉诺坦": "汉诺塔",
    "双机": "双击",
    "灵光一线": "灵光一现",
    "巨声无比": "巨大无比",
    "bysect": "bisect",
    "by set": "bisect",
    "Community History": "commit history",
    "computer object": "commit object",
    "committed object": "commit object",
    "committed message": "commit message",
    "CommitMessage": "commit message",
    "hide是指向": "HEAD 是指向",
    "ZXY的循环": "X、Y、Z 的循环",
    "就可以出科技": "就可以出效果",
    "都是AIS了": "都是 AI slop 了",
    "一个ATI": "一个 API",
    "cs6ea": "cs61a",
    "learn git branching.js.org": "learngitbranching.js.org",
    "Bitkeeper": "BitKeeper",
    "ProGit": "Pro Git",
    "Git Unity": "git init",
    "git unity": "git init",
    "off by onein strlencheck": "off-by-one in strlen check",
    "state of art": "state of the art",
    "Virtual Implement就": "Virtual Implementation 就",
    "NEO VM": "Neovim",
    "MPM": "npm",
    "cherrypick": "cherry-pick",
    "Implantation": "Implementation",
    "Intent and Spat": "Intent 和 Spec、",
    "Information的空间": "Implementation 的空间",
    "Redmi": "README",
    "Readme": "README",
    "AI Stop": "AI Slop",
    "deep-seek": "DeepSeek",
    "全大学会": "全大写会",
    "前移默化": "潜移默化",
    "不定的变好": "不停地变好",
    "省上要的": "省 token 的",
    "快招": "快照",
    "测试用力": "测试用例",
    "报打好": "包打好",
    "或者流打": "或者流水线",
    "CICD": "CI/CD",
    "precommitted hook": "pre-commit hook",
    "interment": "int main(void)",
    "intimate void": "int main(void)",
    "监控号": "尖括号",
    "叫hours": "叫 ours",
    "可以reveal": "可以 revert",
    "agency.md": "AGENTS.md",
    "agent.cmd": "AGENTS.md",
    "考案神": "convention",
    "超系统课": "计算机系统课",
    # ── BV1Q6en6NEUo 软件仓库管理（2）（本地 large-v3 ASR 稿）──
    "jujitsu": "Jujutsu",
    "GIGIT": "Jujutsu",
    "有了Digit": "有了 Jujutsu",
    "GT组": "jj",
    "交流坑": "焦油坑",
    "展察线": "斩杀线",
    "展开线": "斩杀线",
    "Ripple": "repo",
    "这个report status": "这个 repo status",
    "不同的report上": "不同的 repo 上",
    "Vue": "view",
    "现行历史": "线性历史",
    "快兆": "快照",
    "压站": "压栈",
    "TMAX": "tmux",
    "tmax": "tmux",
    "TMUX": "tmux",
    "Walk3": "worktree",
    "Worksheet": "worktree",
    "WorkTree": "worktree",
    "AI slope": "AI slop",
    "slap code": "slop code",
    "生成slot": "生成 slop",
    "Cloud": "Claude",
    "CQO": "SQL",
    "人乐神话": "人月神话",
    "马农": "码农",
    "JSquare": "JavaScript",
    "由征留了": "沉淀了",
    "生存式软件工程": "生成式软件工程",
    "SVM": "SVN",
    "电影器": "编译器",
    "DVC V4 Flash": "DeepSeek V4 Flash",
    "Deep Sea Harness": "DeepSeek Harness",
    "定式变量": "寄存器变量",
    "开发集团机": "开发机",
    "每台讯息": "每台机器",
    "和replace才能合并": "和 rebase 才能合并",
    "他们可以合道": "他们可以合并",
    "那objit也是这样": "那 Git 也是这样",
    "我们护持锁": "我们持有锁",
    "竖杖": "树状",
    "关系性数据库": "关系型数据库",
    "adversary lock": "advisory lock",
    "transaction的bolt": "transaction 的 abort",
    "附用了操系统": "复用了操作系统",
    "contributing.md": "CONTRIBUTING.md",
    "有Committed ID": "有 commit 的 ID",
    "git.ng.c.n": "git 远端",
    "课场会留了时间": "课堂会留了时间",
    "985同大学conference science": "985 大学 computer science",
    "大客承包": "大块文本",
    "我一条门当": "我一条注释",
    "摩尔定律本账一种skin lock": "摩尔定律本质是一种 scaling law",
    # ── BV1Nyeq6qEt8 软件工程的来龙去脉（本地 large-v3 ASR 稿）──
    "大圆模型": "大语言模型",
    "鸡摸场": "基模厂",
    "Skin Law": "scaling law",
    "skin Law": "scaling law",
    "维度的skin了": "维度的 scaling 了",
    "Eclare": "ICLR",
    "DPC-KR1": "DeepSeek-R1",
    "Source才第一次发布": "Sora 才第一次发布",
    "GP4O": "GPT-4o",
    "GT3": "GPT-3",
    "No Brown": "Noam Brown",
    "Enthropy": "Anthropic",
    "马龙": "码农",
    "绘编": "汇编",
    "汇变": "汇编",
    "Dexter": "Dijkstra",
    "苗东百科": "秒懂百科",
    "POOP模型": "瀑布模型",
    "pub的模型": "瀑布模型",
    "ImaginetheDevelopmentofLargeSoftwareSystems": "Managing the Development of Large Software Systems",
    "Minimum Wearable Product": "Minimum Viable Product",
    "反攻代价": "返工代价",
    "减少你反攻的概率": "减少你返工的概率",
    "减少反攻的概率": "减少返工的概率",
    "圆形": "原型",
    "有像无环图": "有向无环图",
    "Data Defendancy": "Data Dependency",
    "Depth Search": "深度优先搜索",
    "preference search": "breadth-first search",
    "这是一个preference": "这是一个 breadth-first",
    "隐碳": "intent",
    "写一个cert": "写一个 assert",
    "我要cert": "我要 assert",
    "任何的cert": "任何的 assert",
    "这个cert": "这个 assert",
    "IFL的语言": "Eiffel 的语言",
    "insure做完了以后": "ensure 做完了以后",
    "Latin space": "latent space",
    "Conveyor Science": "computer science",
    "community science": "computer science",
    "希德的概念": "学到的概念",
    "飞标的": "非标的",
    "飞镖的": "非标的",
    "热转硬的": "热转印的",
    "碳粉袋": "碳带",
    "OJT": "OI",
    "生不红石": "生不逢时",
    "签问": "千问",
    "电子墨水瓶": "电子墨水屏",
    "烧血": "烧写",
    "偷工减量": "偷工减料",
    "特供点料": "偷工减料",
    "造机箱机": "造计算机",
    "大厦四方": "大杀四方",
    "Premium时代": "pre-AI 时代",
    "精力先想清楚": "先想清楚",
    "价格应该怎么做": "架构应该怎么做",
    "干水教材": "教材",
    "向后眼睛": "向后演进",
    "ROIS的模型": "Royce 的模型",
    "implantation": "implementation",
    "Implantation": "Implementation",
    "决策术": "决策树",
    "二精致序列": "二进制序列",
    "闪屯颗粒": "闪存颗粒",
    "出人头底": "出人头地",
    "level print": "label print",
    "极大成者": "集大成者",
    "Bad Smile": "bad smell",
    "软功课": "软件工程课",
    "人工真课": "人工智能课",
    "操信用课": "操作系统课",
    "umr spec": "UML spec",
    "lection notes": "lecture notes",
    "磁像量": "词向量",
    "具体智能": "具身智能",
    "繁华的效果": "泛化的效果",
    "逝者生存": "适者生存",
    "情像3D建模": "请像 3D 建模",
    "豆长一样": "都长一样",
    # ── BV1rXwNzuEmJ 马尔科夫决策过程《RethinkFun 强化学习》1.2（平台 AI 字幕）──
    "absolute greedy策略": "ε-greedy 策略",
    "absolute greedy": "ε-greedy",
    "有app store的概率": "有 ε 的概率",
    "epsent greedy策略": "ε-greedy 策略",
    "epsent grade策略": "ε-greedy 策略",
    "EPSENTGRADY策略": "ε-greedy 策略",
    "epsent greedy": "ε-greedy",
    "epsent grade": "ε-greedy",
    "EPSENTGRADY": "ε-greedy",
    "EPSENT": "ε",  # 逐步衰减 ε 的 ε-greedy
    "EPISOE": "ε",  # 一减 ε 的概率
    "一减EPISOE": "一减 ε",
    "我们引入lambda这个折扣因子": "我们引入伽马这个折扣因子",
    "rule out": "rollout",
    "自自动驾驶": "自动驾驶",
    "超级马力游戏": "超级玛丽游戏",
    "一表示动作空间": "A 表示动作空间",
    "动作A拱足": "动作 A 拱卒",
    "拱足": "拱卒",
    "不能为政治": "不能为正值",
    "必须为政治": "必须为正值",
    "我们既作状态": "我们记作状态",
    "这种情况叫做不分可观测": "这种情况叫做部分可观测",
    "不分可观测": "部分可观测",
    "你歧义的高低": "你棋艺的高低",
    "连续的三三帧图片": "连续的三帧图片",
    "常用套来表示": "常用 τ 来表示",
    "上变公式中的SAAR": "上述公式中的 S、A、R",
    "状态更新为SI": "状态更新为 S1",
    "环境就是走整个游戏程序": "环境就是整个游戏程序",
}
_MAP_ITEMS = sorted(MAP.items(), key=lambda kv: -len(kv[0]))

# 整段即为口癖 -> 删除该段
FILLER_ONLY = {"呃", "嗯", "啊", "哦", "啧", "哎", "哎呀", "哈哈", "哈哈哈", "嘿嘿",
               "对", "对吧", "对啊", "好啊", "好", "好的", "然后", "任何", "额",
               "anyway", "Anyway", "ANYWAY", "呃呃", "嗯嗯", "啊哈", "呜", "喂", "yes", "no"}

# 句尾口癖 -> 剥离
TAIL_FILLER = re.compile(r"(?:对吧|对不对|是吧|是不是|嘛|呀|哦|呃|嗯|哈哈|哈|啊|呢)+$")

# 单字口吃叠词折叠：我我我->我 等
STUTTER = re.compile(r"(我|你|他|它|就|是|有|去|来|啊|呃|嗯|对|但|那|这|也|都|还|又|再|很|太|会|要|想|说|看|做|搞|弄|写|读|问|答|学|教|玩|用|给|把|被|让|使|等)\1+")


# 中文 Windows 控制台默认是 GBK：print 遇到打不出来的字符会直接抛
# UnicodeEncodeError —— 在"报错"这条路径上再报错最难受。让它退化成 ?，绝不崩。
try:
    import sys as _sys
    _sys.stdout.reconfigure(errors="replace")
    _sys.stderr.reconfigure(errors="replace")
except Exception:
    pass


def correct(text: str):
    for old, new in _MAP_ITEMS:
        if old in text:
            text = text.replace(old, new)
    text = text.replace("呃", "").replace("嗯", "")
    text = STUTTER.sub(r"\1", text)
    text = TAIL_FILLER.sub("", text).strip()
    return text


def main():
    ap = argparse.ArgumentParser(description="生成 AI 修正版字幕对照稿")
    ap.add_argument("video_id", nargs="*")
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()
    ids = args.video_id or [d for d in sorted(os.listdir(os.path.join(BASE, "output")))
                            if os.path.isfile(os.path.join(BASE, "output", d, "transcript.json"))]
    for vid in ids:
        src = os.path.join(BASE, "output", vid, "transcript.json")
        segs = json.load(open(src, encoding="utf-8"))["segments"]
        out_lines, dropped, fixed = [], 0, 0
        for seg in segs:
            raw = seg["text"].strip()
            if raw in FILLER_ONLY:
                dropped += 1
                continue
            txt = correct(raw)
            if txt != raw:
                fixed += 1
            if not txt:
                dropped += 1
                continue
            # 行首是 MM:SS（分承载小时，如 [98:26]），与 transcript.txt 对齐
            stamp = timecode.format_mmss(timecode.parse_seconds(seg["start"]))
            out_lines.append(f"[{stamp}] {txt}")
        dst = os.path.join(BASE, "output", vid, "transcript.corrected.txt")
        with open(dst, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(out_lines) + "\n")
        print(f"{vid}: {len(segs)} 段 -> {len(out_lines)} 行（修正 {fixed} 行，删除口癖段 {dropped}）")


if __name__ == "__main__":
    main()
