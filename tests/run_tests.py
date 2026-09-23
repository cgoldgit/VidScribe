"""不依赖 pytest 的测试入口（本机 .venv 里没装 pytest）。

用法:
    python tests/run_tests.py                  # 跑全部（浏览器那一组会自动跳过）
    python tests/run_tests.py capture          # 只跑文件名含 capture 的
    python tests/run_tests.py --list           # 只列测试名

装了 pytest 的话，`python -m pytest tests` 也能跑同一批用例。
"""
import importlib.util
import os
import sys
import traceback
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

# 中文 Windows 控制台是 GBK：断言信息和 traceback 里出现打不出的字符时，
# 连"报告失败"本身都会崩（实测被它坑过一次），所以让它退化成 ? 而不是抛异常。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass


def _load_modules(filter_: str = ""):
    mods = []
    for f in sorted(os.listdir(HERE)):
        if not f.startswith("test_") or not f.endswith(".py"):
            continue
        if filter_ and filter_ not in f:
            continue
        name = f[:-3]
        spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, f))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        mods.append((name, mod))
    return mods


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    only_list = "--list" in sys.argv
    filter_ = args[0] if args else ""
    mods = _load_modules(filter_)

    passed, failed, skipped = [], [], []
    for modname, mod in mods:
        tests = [(n, getattr(mod, n)) for n in sorted(vars(mod))
                 if n.startswith("test_") and callable(getattr(mod, n))]
        if only_list:
            for n, _ in tests:
                print(f"{modname}.{n}")
            continue
        print(f"\n=== {modname} ({len(tests)} 个用例) ===")
        for n, fn in tests:
            label = f"{modname}.{n}"
            try:
                fn()
            except unittest.SkipTest as e:
                skipped.append(label)
                print(f"  SKIP  {n}  ({e})")
            except Exception:
                failed.append(label)
                print(f"  FAIL  {n}")
                print("        " + traceback.format_exc().replace("\n", "\n        ").rstrip())
            else:
                passed.append(label)
                print(f"  ok    {n}")

    if only_list:
        return 0
    print(f"\n{'=' * 60}\n通过 {len(passed)}  失败 {len(failed)}  跳过 {len(skipped)}")
    for label in failed:
        print("  FAILED:", label)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
