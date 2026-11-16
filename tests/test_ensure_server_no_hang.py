#!/usr/bin/env python3
"""回归测试：验证 ensure_server 的启动器调用不会因继承管道句柄而卡死。

对应 Issue #2：capture_output=True 时，PowerShell Start-Process 拉起的
ComfyUI 长期进程继承 stdout/stderr 管道，subprocess.run 等不到 EOF 卡死。
修复改用 stdout/stderr=非管道目标（DEVNULL 或普通日志文件句柄），
避免创建会被继承的管道，同时保留启动错误信息便于排查。

本测试用标准库在任意平台复现"子进程继承管道句柄导致 communicate 阻塞"的
机制，并断言源码中 ensure_server 未使用 capture_output / PIPE。
运行： python tests/test_ensure_server_no_hang.py
"""
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import zimg

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond)))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))


def main():
    # --- 断言 1（机制复现）：继承管道句柄会让 communicate 阻塞 ---
    # 派生一个"启动后立刻退出、但留下一个持有 stdout 的长期子进程"的命令。
    # capture_output=True 时，run() 需等到该子进程退出才返回 → 明显慢于 sleep 时长。
    hold = 2  # 子进程持有管道 2 秒
    if os.name == "nt":
        cmd = ["cmd", "/c", f"start /b cmd /c ping -n {hold+1} 127.0.0.1 >nul"]
    else:
        cmd = ["sh", "-c", f"(sleep {hold} &) ; echo started"]

    t0 = time.time()
    subprocess.run(cmd, capture_output=True, timeout=15)
    elapsed_capture = time.time() - t0
    # sh 版本因子 shell 后台 sleep 继承管道，run 会等到 sleep 结束（≈hold 秒）
    check("capture_output 会因继承句柄阻塞（机制复现）",
          elapsed_capture >= hold - 0.5,
          f"capture_output 耗时 {elapsed_capture:.2f}s（阈值 {hold-0.5}s）")

    # --- 断言 2（修复机制）：DEVNULL 让 run() 立即返回，不等子进程 ---
    t0 = time.time()
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
    elapsed_devnull = time.time() - t0
    check("DEVNULL 让启动器调用立即返回",
          elapsed_devnull < hold - 0.5,
          f"DEVNULL 耗时 {elapsed_devnull:.2f}s（阈值 {hold-0.5}s）")

    check("修复后明显快于修复前", elapsed_devnull < elapsed_capture,
          f"{elapsed_devnull:.2f}s < {elapsed_capture:.2f}s")

    # --- 断言 3（源码核对，用 AST 精确检查调用参数，避免注释文字干扰）---
    import ast
    import inspect
    src = inspect.getsource(zimg.ensure_server)
    tree = ast.parse(src)
    run_kwargs = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "run"):
            run_kwargs = [kw.arg for kw in node.keywords]
            break
    # 核心保证：绝不用 capture_output / PIPE（会制造继承句柄导致卡死）。
    # stdout/stderr 用 DEVNULL 或普通文件句柄都可接受（均非管道）。
    check("ensure_server 未使用 capture_output",
          "capture_output" not in (run_kwargs or []),
          f"run kwargs={run_kwargs}")
    check("ensure_server stdout/stderr 用非管道目标（DEVNULL 或文件句柄）",
          ("stdout" in run_kwargs and "stderr" in run_kwargs)
          and "subprocess.PIPE" not in src,
          f"run kwargs={run_kwargs}")

    passed = sum(1 for _, ok in RESULTS if ok)
    total = len(RESULTS)
    print(f"\n结果：{passed}/{total} 通过")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
