#!/usr/bin/env python3
"""统一测试入口：一键运行全量回归。

运行：python tests/run_all.py

Windows 兼容：默认控制台多为 GBK(cp936)，直接打印 ▶/✓ 等符号会抛
UnicodeEncodeError。这里用三重保险：
  1. 输出统一用 ASCII 前缀（> / [OK]），不依赖控制台代码页；
  2. stdout/stderr 支持 reconfigure 时强制 UTF-8 + errors=replace；
  3. 子进程固定 PYTHONIOENCODING=utf-8，避免测试脚本内部的中文/符号输出二次触发。
"""
import os
import subprocess
import sys
import time

# 保险 2：显式 UTF-8 输出（Python 3.7+ 支持 reconfigure）
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def main():
    # 跳过元测试 test_run_all_windows.py：它会反向调用本入口，若被执行会无限递归
    scripts = sorted(
        f for f in os.listdir(HERE)
        if f.startswith("test_") and f.endswith(".py")
        and f != "test_run_all_windows.py")
    total_pass = total_fail = 0
    failed = []
    t0 = time.time()
    # 保险 3：子进程固定 UTF-8，避免测试脚本符号输出在 GBK 控制台崩溃；
    # 父进程也必须用 UTF-8 解码，否则会按 cp936 读 UTF-8 输出并抛
    # UnicodeDecodeError（Issue #17）。
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    for f in scripts:
        print(f"\n> {f}")  # 保险 1：ASCII 前缀
        r = subprocess.run([sys.executable, os.path.join(HERE, f)],
                           capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           cwd=ROOT, timeout=300, env=env)
        print("\n".join("  " + x for x in (r.stdout or "").strip().splitlines()[-3:]))
        if r.returncode == 0:
            # 从输出提取 "结果：x/y 通过"
            for line in (r.stdout or "").splitlines():
                if "结果" in line and "通过" in line:
                    nums = "".join(c if c.isdigit() or c == "/" else " " for c in line).split()
                    if nums and "/" in nums[0]:
                        p, _, t = nums[0].partition("/")
                        total_pass += int(p)
                        total_fail += int(t) - int(p)
                    break
        else:
            failed.append(f)
            print(f"  [stderr] {(r.stderr or '').strip()[-500:]}")
    print(f"\n{'=' * 50}")
    print(f"共 {len(scripts)} 个测试文件，断言 {total_pass}/{total_pass + total_fail} 通过，"
          f"耗时 {time.time() - t0:.1f}s")
    if failed:
        print("失败的测试文件：", ", ".join(failed))
        return 1
    print("[OK] 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
