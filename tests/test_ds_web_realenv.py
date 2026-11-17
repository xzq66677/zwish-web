#!/usr/bin/env python3
"""回归测试：DS·网页真实环境回归与并发修复（Issue #10 + #9）。

覆盖四组修复，无需真实浏览器/网络：
  1. helper 免疫导航：_eval 每次求值都幂等注入 JS_HELPER（#10 主因）；
  2. 发送选择器兼容 div[role=button]，兜底查找覆盖 role=button（#10）；
  3. web_chat 全程串行化 + get_browser 单例加锁，并发无标签泄漏（#9）；
  4. 会话状态（ws/target_id）由调用方局部持有，finally 显式关闭自己的标签。

运行： python tests/test_ds_web_reuse.py
退出码 0 = 全部通过。
"""
import ast
import inspect
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import zds

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond)))
    flag = "PASS" if cond else "FAIL"
    print(f"[{flag}] {name}" + (f"  ({detail})" if detail and not cond else ""))


# ──────────────────────────── 1. helper 免疫导航（#10）────────────────────────────
class _RecordingWS:
    """记录每次 evaluate 的表达式，模拟 CDP。"""

    def __init__(self, reply=None):
        self.calls = []
        self._reply = reply

    def call(self, method, params=None, timeout=30):
        self.calls.append((method, (params or {}).get("expression", "")))
        if self._reply is not None:
            return self._reply
        return {"result": {"value": None}}


def main():
    ws = _RecordingWS(reply={"result": {"value": "ok"}})
    zds._eval(ws, "1+1")
    check("_eval 只发一次 Runtime.evaluate", len(ws.calls) == 1)
    expr = ws.calls[0][1]
    check("_eval 表达式自带 helper 注入（前缀为 JS_HELPER）",
          expr.startswith(zds.JS_HELPER) and "1+1" in expr)
    check("helper 注入是幂等定义（if (!window.__zfind)）",
          "if (!window.__zfind)" in zds.JS_HELPER)
    # 导航丢失场景：页面导航后 window.__zfind 消失，再次 _eval 仍会重新注入
    zds._eval(ws, "2+2")
    check("第二次 _eval 同样携带 helper（免疫导航重建）",
          len(ws.calls) == 2 and ws.calls[1][1].startswith(zds.JS_HELPER))

    # ──────────────────── 2. 发送选择器兼容 role=button（#10）────────────────────
    check("_SEND_SELS 覆盖 div[role=button] 文本匹配",
          any('role="button"' in s and "发送" in s for s in zds._SEND_SELS))
    check("_SEND_SELS 覆盖 role=button aria-label",
          any(s.startswith('[role="button"][aria-label') for s in zds._SEND_SELS))
    check("_SEND_SELS 保留原生 button 选择器",
          any(s.startswith('button:') for s in zds._SEND_SELS))
    src_web = inspect.getsource(zds._web_chat_locked)
    check("兜底查找覆盖 button,[role=button]",
          'button,[role="button"]' in src_web)
    check("generating 追加 role=button 停止选择器",
          'div[role="button"]:has-text("停止")' in src_web)

    # ──────────────────── 3. 并发修复（#9）────────────────────
    # 3a. get_browser 单例并发只创建一个实例
    created = []

    class _FakeProc:
        def poll(self):
            return None

    class _FakeBrowser:
        def __init__(self):
            time.sleep(0.03)  # 放大竞态窗口
            created.append(self)
            self.proc = _FakeProc()
            self.closed = []
            self.opened = []

        def alive(self):
            return self.proc.poll() is None

        def new_tab(self, url):
            tid = f"tab-{len(self.opened)+1}"
            self.opened.append(tid)
            return _RecordingWS(), tid

        def close_tab(self, tid):
            self.closed.append(tid)

        def close(self):
            pass

    orig_browser = zds._Browser
    orig_singleton = zds._BROWSER_SINGLETON
    zds._Browser = _FakeBrowser
    zds._BROWSER_SINGLETON = None
    try:
        ts = [threading.Thread(target=zds.get_browser) for _ in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        check("8 线程并发 get_browser 只创建 1 个浏览器（#9 竞态）", len(created) == 1,
              f"实际创建 {len(created)} 个")

        # 3b. new_tab 不再写共享实例字段；close_tab 显式接收 target_id
        b = zds.get_browser()
        ws1, tid1 = b.new_tab("u1")
        ws2, tid2 = b.new_tab("u2")
        check("new_tab 返回 (ws, target_id) 且不覆盖共享字段",
              not hasattr(b, "target_id") and tid1 != tid2)
        # 模拟原 bug 场景：A 开 TAB-A 后 B 开 TAB-B，A 关闭自己 → 必须关 TAB-A
        b.close_tab(tid1)
        check("close_tab 显式传参时关闭自己的标签（不再串线）",
              b.closed == [tid1])

        # 3c. web_chat 全程串行：并发两个会话，标签 opened/closed 一一对应
        #     （桩掉 available/zprompts，缩短真实等待）
        orig_avail, orig_wrap, orig_sleep = zds.available, zds.zprompts.ds_wrap, zds.time.sleep
        zds.available = lambda: (True, "ok")
        zds.zprompts.ds_wrap = lambda s, u: u
        zds.time.sleep = lambda *_: None  # 跳过所有等待

        # 让登录检测立即通过、回复立即稳定返回
        def fake_eval(wsx, expr, timeout=30):
            import json as _json
            if "box.tagName" in expr:
                return "ok"        # fill 成功
            if "querySelectorAll" in expr or "btn.click" in expr or "clicked" in expr:
                return "clicked"   # send 成功
            if "busy" in expr:
                return _json.dumps({"text": "回复文本", "busy": 0})
            if "innerText" in expr:
                return ""          # prev 抓取（无旧回复）
            if expr.startswith("!!window.__zfind"):
                return True        # 登录检测通过
            return None

        orig_eval = zds._eval
        zds._eval = fake_eval
        try:
            b2 = created[0]
            base_opened, base_closed = len(b2.opened), len(b2.closed)
            results = []
            errors = []

            def worker(tag):
                try:
                    results.append(zds.web_chat("sys", f"wish-{tag}"))
                except Exception as e:
                    errors.append(repr(e))

            t1 = threading.Thread(target=worker, args=(1,))
            t2 = threading.Thread(target=worker, args=(2,))
            t1.start(); time.sleep(0.02); t2.start()
            t1.join(); t2.join()
            check("并发两个 web_chat 均正常返回", not errors and len(results) == 2,
                  f"errors={errors[:2]}")
            d_open = b2.opened[base_opened:]
            d_closed = b2.closed[base_closed:]
            check("串行化下标签 opened/closed 一一对应无泄漏",
                  len(d_open) == 2 and sorted(d_open) == sorted(d_closed),
                  f"opened={d_open} closed={d_closed}")
            check("每次会话返回回复文本", all(r == "回复文本" for r in results))
        finally:
            zds._eval = orig_eval
            zds.available = orig_avail
            zds.zprompts.ds_wrap = orig_wrap
            zds.time.sleep = orig_sleep
    finally:
        zds._Browser = orig_browser
        zds._BROWSER_SINGLETON = orig_singleton

    # 3d. 锁结构核对：web_chat 持锁调用 _web_chat_locked；两把锁分离（无重入死锁）
    src = ast.unparse(ast.parse(inspect.getsource(zds.web_chat)))
    check("web_chat 全程持 _WEB_LOCK 串行化", "with _WEB_LOCK" in src and "_web_chat_locked" in src)
    check("get_browser 使用独立 _BROWSER_LOCK（避免重入死锁）",
          "_BROWSER_LOCK" in ast.unparse(ast.parse(inspect.getsource(zds.get_browser))))

    # 3e. finally 显式关闭自己的标签（源码核对）
    src_lock = ast.unparse(ast.parse(inspect.getsource(zds._web_chat_locked)))
    check("会话状态局部持有（ws = target_id = None）", "ws = target_id = None" in src_lock)
    check("finally 显式 close_tab(target_id)", "browser.close_tab(target_id)" in src_lock)
    # AST 检查 close() 方法体（非注释）不再访问 self.ws 属性
    import textwrap
    close_tree = ast.parse(textwrap.dedent(inspect.getsource(zds._Browser.close)))
    attrs = [n.attr for n in ast.walk(close_tree) if isinstance(n, ast.Attribute)]
    check("_Browser.close 不再引用共享 self.ws", "ws" not in attrs, f"attrs={attrs}")

    passed = sum(1 for _, ok in RESULTS if ok)
    total = len(RESULTS)
    print(f"\n结果：{passed}/{total} 通过")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
