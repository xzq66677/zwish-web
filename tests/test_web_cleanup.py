#!/usr/bin/env python3
"""回归测试：对话完后原网页自动删除（清理聊天记录 + 关标签页/登录窗口）。

覆盖（全部桩注入，不启动浏览器、不访问网络）：
  1. 默认配置：delete_chat / close_tab / close_login 默认全开；
  2. cleanup_state / save_cleanup_cfg：原子写 + 只保留合法 bool，回读一致；
  3. delete_chat_record：关闭时直接 disabled；无按钮返回 no-button；
     命中"删除对话"后点确认；命中"新建对话"不点确认；通道级选择器覆盖生效；
     JS 异常/求值异常被吞掉，不抛错；
  4. register_login_tab / close_login_tabs：按浏览器实例分组登记与关闭，
     close_login=false 时不关；关闭后表清空；
  5. 三条web_chat的finally：消息确已发出才删记录、始终尝试关登录窗口、
     close_tab=false 时不关本次标签；
  6. config.example.json / 前端接线：web_cleanup 段存在、三个开关 id 与
     /api/web_cleanup、/api/save_web_cleanup 接线齐全。

运行： python tests/test_web_cleanup.py
"""
import inspect
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

PASSED = FAILED = 0


def check(name, cond, detail=""):
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"[PASS] {name}" + (f"  ({detail})" if detail else ""))
    else:
        FAILED += 1
        print(f"[FAIL] {name}" + (f"  ({detail})" if detail else ""))


import zconfig  # noqa: E402
import zds  # noqa: E402

# 用临时文件替换清理配置路径，避免测试污染用户真实的 web_cleanup.json
_ORIG_FILE = zds.CLEANUP_FILE
_TMP = tempfile.mkdtemp(prefix="zwish_cleanup_")
zds.CLEANUP_FILE = os.path.join(_TMP, "web_cleanup.json")
_ORIG_CFG = dict(zds.CFG)


class FakeWS:
    """桩 ws：call 记录表达式，按命中规则返回不同结果。"""

    def __init__(self, eval_results=None):
        self.closed = False
        self.exprs = []
        self._results = list(eval_results or [])

    def close(self):
        self.closed = True


class FakeBrowser:
    def __init__(self, tag="b"):
        self.tag = tag
        self.closed_tabs = []

    def close_tab(self, tid):
        self.closed_tabs.append(tid)


def main():
    # ── 1. 默认配置 ──
    if os.path.isfile(zds.CLEANUP_FILE):
        os.remove(zds.CLEANUP_FILE)
    cfg = zds._cleanup_cfg()
    check("默认 delete_chat 开", cfg.get("delete_chat") is True)
    check("默认 close_tab 开", cfg.get("close_tab") is True)
    check("默认 close_login 开", cfg.get("close_login") is True)

    # ── 2. 持久化 ──
    st = zds.cleanup_state()
    check("cleanup_state 回传 file 字段", st.get("file") == "web_cleanup.json")
    saved = zds.save_cleanup_cfg({"delete_chat": False, "close_tab": True, "close_login": False})
    check("save 只接受 bool", saved == {"delete_chat": False, "close_tab": True, "close_login": False})
    check("原子写落盘", os.path.isfile(zds.CLEANUP_FILE))
    with open(zds.CLEANUP_FILE, encoding="utf-8") as f:
        raw = json.load(f)
    check("落盘内容一致", raw == saved)
    cfg2 = zds._cleanup_cfg()
    check("回读生效", cfg2["delete_chat"] is False and cfg2["close_login"] is False)
    # 恢复默认
    zds.save_cleanup_cfg({"delete_chat": True, "close_tab": True, "close_login": True})

    # ── 3. delete_chat_record ──
    # 关闭开关
    zds.save_cleanup_cfg({"delete_chat": False, "close_tab": True, "close_login": True})
    check("关闭时直接 disabled", zds.delete_chat_record(FakeWS()) == "disabled")
    zds.save_cleanup_cfg({"delete_chat": True, "close_tab": True, "close_login": True})

    check("无 ws 返回 no-ws", zds.delete_chat_record(None) == "no-ws")

    # 桩 _eval：record_results 依次返回
    orig_eval = zds._eval
    try:
        zds._eval = lambda ws, expr, **kw: "no-button"
        check("找不到按钮返回 no-button", zds.delete_chat_record(FakeWS()) == "no-button")

        zds._eval = lambda ws, expr, **kw: "clicked:button:has-text(\"新建对话\")"
        r = zds.delete_chat_record(FakeWS())
        check("命中新建对话→clicked 且不追加确认", r.startswith("clicked") and "|" not in r, r)

        def _del_then_confirm(ws, expr, **kw):
            return "clicked:button:has-text(\"删除对话\")" if "sels" in expr else "confirmed"
        zds._eval = _del_then_confirm
        r = zds.delete_chat_record(FakeWS())
        check("命中删除对话→点二次确认", "clicked" in r and "confirmed" in r, r)

        # 通道级选择器覆盖：捕获传入的 selectors 是否进入 JS
        seen = {}
        def _capture(ws, expr, **kw):
            seen["expr"] = expr
            return "no-button"
        zds._eval = _capture
        zds.delete_chat_record(FakeWS(), selectors=['.my-delete-btn'])
        check("通道级选择器覆盖生效", '.my-delete-btn' in seen["expr"])

        # JS 异常被吞
        def _boom(ws, expr, **kw):
            raise RuntimeError("cdp dead")
        zds._eval = _boom
        check("求值异常被吞为 error", zds.delete_chat_record(FakeWS()) == "error")
    finally:
        zds._eval = orig_eval

    # ── 4. 登录窗口登记与关闭 ──
    b1, b2 = FakeBrowser("b1"), FakeBrowser("b2")
    zds._LOGIN_TABS.clear()
    zds.register_login_tab(b1, "t1")
    zds.register_login_tab(b1, "t2")
    zds.register_login_tab(b2, "t3")
    check("按浏览器分组登记", set(zds._LOGIN_TABS) == {id(b1), id(b2)})
    n = zds.close_login_tabs(b1)
    check("只关 b1 的窗口", n == 2 and set(b1.closed_tabs) == {"t1", "t2"} and not b2.closed_tabs)
    check("关闭后该组清空", id(b1) not in zds._LOGIN_TABS)

    zds.register_login_tab(b2, "t4")
    zds.save_cleanup_cfg({"delete_chat": True, "close_tab": True, "close_login": False})
    check("close_login=false 不关", zds.close_login_tabs(b2) == 0 and not b2.closed_tabs)
    # 恢复
    zds.save_cleanup_cfg({"delete_chat": True, "close_tab": True, "close_login": True})

    # ── 5. 三条 web_chat 的 finally 接线（源码级断言）──
    for mod_name, fname in (("zds", "_web_chat_locked"),
                            ("zyuanbao", "_web_chat_locked"),
                            ("zwebchat", "_web_chat_locked")):
        mod = __import__(mod_name)
        src = inspect.getsource(getattr(mod, fname))
        check(f"{mod_name}: finally 有 msg_sent 门控删记录",
              "msg_sent" in src and "delete_chat_record" in src)
        check(f"{mod_name}: finally 清理登录窗口",
              "close_login_tabs" in src)
        check(f"{mod_name}: close_tab 配置门控",
              'close_tab' in src)
    # open_login_window 登记
    for mod_name in ("zyuanbao", "zwebchat"):
        mod = __import__(mod_name)
        src = inspect.getsource(mod.open_login_window)
        check(f"{mod_name}: open_login_window 登记登录窗口",
              "register_login_tab" in src)

    # ── 6. config.example / 前端接线 ──
    with open(os.path.join(HERE, "config.example.json"), encoding="utf-8") as f:
        ex = json.load(f)
    check("config.example 有 web_cleanup 段", "web_cleanup" in ex)
    check("web_cleanup 三键齐全",
          all(k in ex["web_cleanup"] for k in ("delete_chat", "close_tab", "close_login")))

    with open(os.path.join(HERE, "static", "index.html"), encoding="utf-8") as f:
        html = f.read()
    for token in ('id="ckDelChat"', 'id="ckCloseTab"', 'id="ckCloseLogin"',
                  "function saveCleanup", "function loadCleanup",
                  "/api/web_cleanup", "/api/save_web_cleanup"):
        check(f"前端含 {token}", token in html)
    with open(os.path.join(HERE, "server.py"), encoding="utf-8") as f:
        srv = f.read()
    check("server GET /api/web_cleanup", '"/api/web_cleanup"' in srv)
    check("server POST /api/save_web_cleanup", '"/api/save_web_cleanup"' in srv)

    # 恢复现场
    zds.CLEANUP_FILE = _ORIG_FILE
    zds.CFG.clear()
    zds.CFG.update(_ORIG_CFG)
    zds._LOGIN_TABS.clear()

    print(f"\n结果：{PASSED}/{PASSED + FAILED} 通过")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
