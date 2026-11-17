#!/usr/bin/env python3
"""统一测试入口：一键运行全量回归。

运行：python tests/run_all.py
"""
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def main():
    scripts = sorted(
        f for f in os.listdir(HERE)
        if f.startswith("test_") and f.endswith(".py"))
    total_pass = total_fail = 0
    failed = []
    t0 = time.time()
    for f in scripts:
        print(f"\n▶ {f}")
        r = subprocess.run([sys.executable, os.path.join(HERE, f)],
                           capture_output=True, text=True, cwd=ROOT, timeout=300)
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
    print("全部通过 ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
