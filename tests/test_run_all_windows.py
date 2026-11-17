#!/usr/bin/env python3
"""回归：run_all.py 在 GBK(cp936) 控制台不崩溃（Issue #15）。

用 PYTHONIOENCODING=gbk 启动子进程，模拟 Windows 默认控制台编码，
验证：
  1. 入口打印用 ASCII，不会因 ▶ 符号抛 UnicodeEncodeError；
  2. 子进程固定 PYTHONIOENCODING=utf-8，测试脚本内部符号输出不二次触发；
  3. 源码层面三重保险都在（防止被误删）。
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
RUN_ALL = os.path.join(HERE, "run_all.py")

PASSED = FAILED = 0


def check(name, cond, detail=""):
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"[PASS] {name}" + (f"  ({detail})" if detail else ""))
    else:
        FAILED += 1
        print(f"[FAIL] {name}" + (f"  ({detail})" if detail else ""))


def main():
    print("[1] 源码层面三重保险")
    src = open(RUN_ALL, encoding="utf-8").read()
    # 用 AST 抽取所有字符串常量（含 f-string 的文本片段），断言输出文本里没有 ▶/✓。
    # 模块 docstring 里的说明文字允许出现这两个字符，但它不是 print 输出——
    # 因此只检查含 "print" 上下文的字符串不可靠，改为检查实际 print 调用的参数。
    import ast
    tree = ast.parse(src)
    printed_text = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "print":
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    printed_text.append(arg.value)
                elif isinstance(arg, ast.JoinedStr):
                    for v in arg.values:
                        if isinstance(v, ast.Constant) and isinstance(v.value, str):
                            printed_text.append(v.value)
    joined = "".join(printed_text)
    check("输出前缀用 ASCII（print 文本无 ▶）", "▶" not in joined and "> " in joined)
    check("[OK] 替代 ✓（print 文本无 ✓）", "✓" not in joined and "[OK]" in joined)
    check("stdout reconfigure UTF-8", "reconfigure" in src and 'encoding="utf-8"' in src)
    check("子进程固定 PYTHONIOENCODING", 'PYTHONIOENCODING' in src)

    print("[2] 实证：以 GBK 编码启动入口（模拟 Windows cp936）")
    env = {**os.environ, "PYTHONIOENCODING": "gbk"}
    r = subprocess.run([sys.executable, RUN_ALL],
                       capture_output=True, text=True, cwd=ROOT,
                       env=env, timeout=300, errors="replace")
    check("GBK 环境下入口退出码为 0", r.returncode == 0,
          f"rc={r.returncode}")
    check("无 UnicodeEncodeError", "UnicodeEncodeError" not in (r.stderr or ""))
    check("全部测试仍通过", "[OK]" in (r.stdout or ""))

    print(f"\n结果：{PASSED}/{PASSED + FAILED} 通过")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
