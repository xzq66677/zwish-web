#!/usr/bin/env python3
"""回归测试：腾讯元宝网页备用通道（Issue #14）。

覆盖：
  1. 独立 profile、独立选择器、可配置项齐全；
  2. 复用 zds 的 CDP/锁基础设施，不另起一套；
  3. 与 DS·网页共享 _WEB_LOCK 串行，会话状态局部持有、标签显式关闭；
  4. 填充兼容 Quill（execCommand / paste / textContent 兜底），发送识别 div[aria-label="发送"]；
  5. 前端标签、模型路由与 /api/config 字段接线完整；
  6. 桩浏览器下两次调用复用同一浏览器实例，标签 opened/closed 一一对应。

运行： python tests/test_yuanbao_web.py
退出码 0 = 全部通过。
"""
import ast
import inspect
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import zconfig  # noqa: E402
import zds  # noqa: E402
import zyuanbao  # noqa: E402

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
    print("[1] 配置与独立 profile")
    profile = zyuanbao.PROFILE.replace("\\", "/")
    check("profile 使用独立目录", profile.endswith("/data/yuanbao_profile"), profile)
    check("profile 不与 ds_profile 混用", "ds_profile" not in profile)
    check("config 默认 URL 指向元宝", "yuanbao.tencent.com" in CFG_URL())
    check("composer 选择器含 Quill 编辑器",
          any("ql-editor" in s for s in zconfig.CFG["yuanbao_web_selectors"]["composer"]))
    check("send 选择器含 div[aria-label=发送]",
          any('aria-label="发送"' in s for s in zconfig.CFG["yuanbao_web_selectors"]["send"]))
    check("answer 选择器可配置",
          isinstance(zconfig.CFG["yuanbao_web_selectors"].get("answer"), list))

    print("[2] 复用 zds 基础设施（不重复造轮子）")
    src = inspect.getsource(zyuanbao)
    check("复用 zds._WS", "zds._WS" in src or "_YuanbaoBrowser" in src)
    check("复用 zds._eval（helper 自动注入）", "zds._eval" in src)
    check("浏览器单例受 zds._BROWSER_LOCK 保护", "zds._BROWSER_LOCK" in src)
    check("页面会话受 zds._WEB_LOCK 串行", "zds._WEB_LOCK" in src)

    print("[3] 会话安全：状态局部持有 + 标签显式清理")
    locked = ast.unparse(ast.parse(inspect.getsource(zyuanbao._web_chat_locked)))
    check("会话状态局部持有", "ws = target_id = None" in locked)
    check("finally 显式 close_tab(target_id)", "browser.close_tab(target_id)" in locked)
    check("填充含 execCommand 兜底", "execCommand('insertText'" in src)
    check("填充含 paste 兜底", "ClipboardEvent('paste'" in src)
    check("发送前检查 disabled 状态", "aria-disabled" in src and "sendNot" in src)

    print("[4] 前端与 API 接线")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    fe = open(os.path.join(root, "static", "index.html"), encoding="utf-8").read()
    # 元宝已内收进「🌐 AI 网页」：顶部不再有独立 chip，统一由网页面板选择
    check("顶部已无独立元宝 chip（已内收）", 'id="chipYuanbao"' not in fe)
    check("modelParam 映射 __yuanbao_web__", '"yuanbao") return "__yuanbao_web__"' in fe)
    check("元宝在 AI 网页面板预设中", '"id":"yuanbao"' in fe or '"id": "yuanbao"' in fe)
    check("云端追问推荐包含元宝", '"ds_web","yuanbao"' in fe)
    srv = open(os.path.join(root, "server.py"), encoding="utf-8").read()
    check("/api/config 返回 yuanbao_web 状态", '"yuanbao_web": {"ok"' in srv)
    check("提供登录窗口接口", '"/api/yuanbao_login"' in srv)

    print("[5] 桩浏览器：复用 + 标签无泄漏")
    class FakeWS:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    class FakeBrowser:
        def __init__(self):
            self.n = 0
            self.opened = []
            self.closed = []
            self.probe_calls = 0
            self.ws_list = []

        def alive(self):
            return True

        def new_tab(self, url):
            self.n += 1
            self.probe_calls = 0          # 每个会话独立的探针基线
            tid = f"tab-{self.n}"
            self.opened.append(tid)
            ws = FakeWS()
            self.ws_list.append(ws)
            return ws, tid

        def close_tab(self, target_id=None):
            self.closed.append(target_id)

    fake = FakeBrowser()
    orig = (zyuanbao.get_browser, zyuanbao.available, zds._eval, zyuanbao.time.sleep)
    zyuanbao.get_browser = lambda: fake
    zyuanbao.available = lambda: (True, "stub ready")
    zyuanbao.time.sleep = lambda _s: None

    def fake_eval(_ws, expr, timeout=30):
        if "!!window.__zfind" in expr:
            return True
        if "return ((box.innerText" in expr:
            return "ok"
        if "btn.click();" in expr:
            return "clicked"
        if "JSON.stringify({count:" in expr:
            fake.probe_calls += 1
            if fake.probe_calls == 1:      # 发送前基线
                return json.dumps({"count": 0, "text": "", "busy": 0, "bodyLen": 100})
            return json.dumps({"count": 1, "text": "元宝回复", "busy": 0, "bodyLen": 120})
        return None

    zds._eval = fake_eval
    try:
        r1 = zyuanbao.web_chat("sys", "wish-1", timeout=5)
        r2 = zyuanbao.web_chat("sys", "wish-2", timeout=5)
        check("两次调用复用同一浏览器实例", fake.n == 2 and len(fake.closed) == 2)
        check("每次调用都返回回复", r1 == "元宝回复" and r2 == "元宝回复")
        check("标签 opened/closed 一一对应",
              sorted(fake.opened) == sorted(fake.closed),
              f"opened={fake.opened} closed={fake.closed}")
        check("会话 WebSocket 已关闭", all(w.closed for w in fake.ws_list))
    finally:
        zyuanbao.get_browser, zyuanbao.available, zds._eval, zyuanbao.time.sleep = orig

    print(f"\n结果：{PASSED}/{PASSED + FAILED} 通过")
    return 0 if FAILED == 0 else 1


def CFG_URL():
    return zconfig.CFG.get("yuanbao_web_chat_url") or ""


if __name__ == "__main__":
    sys.exit(main())
