#!/usr/bin/env python3
"""回归测试：DS·网页通道修复（Issue #6）。

覆盖三项修复，无需真实浏览器/网络：
  1. 浏览器常驻复用：get_browser 单例 + alive 判断 + atexit 清理；
  2. web_chat 登录等待含快速失败与明确指引（源码结构核对）；
  3. web_chat finally 只关标签页不杀进程（常驻语义）。

运行： python tests/test_ds_web_reuse.py
退出码 0 = 全部通过。
"""
import ast
import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import zds

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond)))
    flag = "PASS" if cond else "FAIL"
    print(f"[{flag}] {name}" + (f"  ({detail})" if detail else ""))


def main():
    # --- 1. 常驻复用机制存在 ---
    check("存在 get_browser 单例入口", callable(getattr(zds, "get_browser", None)))
    check("存在 shutdown_browser 清理", callable(getattr(zds, "shutdown_browser", None)))
    check("存在 close_tab（关标签不杀进程）", hasattr(zds._Browser, "close_tab"))
    check("存在 alive（复用存活判断）", hasattr(zds._Browser, "alive"))

    # --- 2. 单例复用行为：不启动真实浏览器，用桩验证 ---
    class _FakeProc:
        def poll(self):
            return None  # 模拟存活

    class _FakeBrowser:
        def __init__(self):
            self.proc = _FakeProc()
            self.count = 0

        def alive(self):
            return self.proc.poll() is None

        def close(self):
            self.count += 1

    zds._BROWSER_SINGLETON = _FakeBrowser()
    b1 = zds.get_browser()
    b2 = zds.get_browser()
    check("进程存活时复用同一实例", b1 is b2 and b1.count == 0)

    class _DeadProc:
        def poll(self):
            return 1  # 模拟已退出

    dead = _FakeBrowser()
    dead.proc = _DeadProc()
    zds._BROWSER_SINGLETON = dead
    zds._Browser_orig = zds._Browser
    zds._Browser = _FakeBrowser  # 替换构造，避免真启动浏览器
    try:
        b3 = zds.get_browser()
        check("进程已死时自动重建", b3 is not dead)
    finally:
        zds._Browser = zds._Browser_orig
        zds._BROWSER_SINGLETON = None
        if b3 is not None and isinstance(b3, _FakeBrowser):
            pass  # 桩实例，无需清理

    # --- 3. web_chat 源码结构核对（AST，避免注释干扰）---
    # 注：web_chat 现为「持锁入口」，逻辑主体在 _web_chat_locked（#9 串行化）。
    src = inspect.getsource(zds._web_chat_locked)
    tree = ast.parse(src)
    text = ast.unparse(tree)
    entry = ast.unparse(ast.parse(inspect.getsource(zds.web_chat)))
    check("web_chat 入口持 _WEB_LOCK 串行化",
          "with _WEB_LOCK" in entry and "_web_chat_locked" in entry)
    check("web_chat 使用 get_browser（常驻复用）", "get_browser()" in text)
    check("web_chat 不再调用 _Browser() 直接新建", "_Browser()" not in text)
    check("web_chat finally 不再整体关闭浏览器",
          "browser.close()" not in text)
    check("登录等待含快速失败（登录页检测）", "登录" in src and "login_grace" in text)
    check("超时报错含独立 profile 提示", "ds_profile" in src and "新浏览器窗口" in src)

    # --- 4. 前端 api() 超时保护 ---
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    fe = open(os.path.join(root, "static", "index.html"), encoding="utf-8").read()
    check("前端 api() 使用 AbortController 超时",
          "AbortController" in fe and "180000" in fe)
    check("前端超时给出 DS 通道指引", "DS·网页" in fe or "DS·API" in fe)

    passed = sum(1 for _, ok in RESULTS if ok)
    total = len(RESULTS)
    print(f"\n结果：{passed}/{total} 通过")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
