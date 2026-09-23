"""所有脚本的 print 字面量必须能用 GBK 编码（中文 Windows 控制台打不出 emoji）。

起因：这个项目的脚本里原本有多处 `✅ ❌ ⚠️ 🎬 🤖` 之类的符号出现在 print 里。
在中文 Windows 上，输出被管道或重定向时（AI 调用、写日志、`> out.txt`）用的是 GBK：

  没有 stdout 兜底的脚本会直接崩溃：
      UnicodeEncodeError: 'gbk' codec can't encode character '\\u274c'
  有 `sys.stdout.reconfigure(errors="replace")` 的脚本不会崩，但符号会显示成 `?`，
  日志里的状态标记因此失去原意。

所以本测试要求：print 的字面量本身就得是 GBK 能打的（显示正常，而不是退化成 ?）。
另外还要求每个会打印的脚本都带兜底，挡住**动态内容**（视频标题、文件名里带 emoji 时
同样会崩，那不是字面量能查出来的）。

注意：`post_process.py` 里写进 HTML 的图标（📷 ▶ ⚠️）**不算** —— 那是 UTF-8 网页内容，
浏览器里正常显示。本测试只看 `print(...)` 的参数。
"""
import ast
import glob
import io
import os

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FILES = sorted(glob.glob(os.path.join(REPO, "src", "*.py")))
ENCODING = "gbk"


def _print_literals(tree):
    """取出所有 print(...) 参数里的字符串字面量（含 f-string 的静态片段）。

    只看 print 的参数，所以写文件/写网页用的字符串不会被误判。
    """
    out = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "print"):
            continue
        for arg in node.args:
            for sub in ast.walk(arg):
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                    out.append((sub.lineno, sub.value))
    return out


def test_all_print_literals_are_gbk_encodable():
    bad = []
    for path in FILES:
        tree = ast.parse(io.open(path, encoding="utf-8").read())
        for lineno, text in _print_literals(tree):
            try:
                text.encode(ENCODING)
            except UnicodeEncodeError as e:
                ch = text[e.start]
                bad.append("%s:%d  打不出 %r (U+%04X)"
                           % (os.path.basename(path), lineno, ch, ord(ch)))
    assert not bad, ("这些 print 在中文 Windows 控制台会抛 UnicodeEncodeError：\n  "
                     + "\n  ".join(bad))


def test_every_script_has_the_stdout_guard():
    """除了符号本身，每个脚本还应带上兜底：万一将来又混进打不出的字符
    （比如视频标题里带 emoji），退化成 ? 而不是崩掉。

    两种写法都认：
        sys.stdout.reconfigure(errors="replace")                     # 保留原编码
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # 强制 UTF-8
    （stderr 不用兜：Python 的 stderr 默认错误处理器就是 backslashreplace。）
    """
    missing = []
    for path in FILES:
        text = io.open(path, encoding="utf-8").read()
        if "print(" not in text:
            continue
        if "reconfigure(" not in text or 'errors="replace"' not in text:
            missing.append(os.path.basename(path))
    assert not missing, f"这些脚本缺 stdout 兜底：{missing}"


def test_guard_really_replaces_unencodable_chars():
    """兜底是真的有效：模拟 GBK 控制台打一个 emoji，必须变成 ? 而不是抛异常。"""
    import subprocess
    import sys
    code = (
        "import sys\n"
        "sys.stdout.reconfigure(errors='replace')\n"
        "print('\\u274c x \\u2705')\n"
    )
    env = dict(os.environ, PYTHONIOENCODING="gbk")
    p = subprocess.run([sys.executable, "-c", code], capture_output=True, env=env)
    assert p.returncode == 0, p.stderr.decode("utf-8", "replace")
    assert b"?" in p.stdout, p.stdout
