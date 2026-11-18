#!/usr/bin/env python3
"""回归测试：自定义 AI 网页通道（issue #23，zwebchat 配置驱动引擎）。

覆盖（全部桩注入，不启动浏览器、不访问网络）：
  1. 配置校验：非法 id / 非法 url / disabled 通道被跳过；
  2. zwish 路由：__web__:<id> 分发到 zwebchat.web_chat，显示名正确；
  3. 引擎会话：同一通道复用同一浏览器实例、标签 opened/closed 一一对应、
     不同通道各自独立浏览器与 profile；
  4. 通道契约注入（前置 + 末尾强调），通道名进入错误信息；
  5. 填充/发送 JS：textarea 与 contenteditable 双路径、aria-disabled 检查、
     Enter 兜底、DOM 诊断；
  6. server / 前端接线：/api/config 的 custom_channels、webchat_login /
     webchat_status 接口、动态 data-webchip、modelParam 映射、askIsCloud；
  7. config.example.json：千问 / Grok / 文心一言预设存在且 enabled=false，
     且不含豆包。

运行： python tests/test_webchat_channels.py
"""
import json
import os
import sys

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
import zwebchat  # noqa: E402
import zwish  # noqa: E402
import zprompts  # noqa: E402


class FakeWS:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class FakeBrowser:
    def __init__(self, tag):
        self.tag = tag
        self.n = 0
        self.opened = []
        self.closed = []
        self.ws_list = []

    def alive(self):
        return True

    def new_tab(self, url):
        self.n += 1
        tid = f"{self.tag}-tab-{self.n}"
        self.opened.append(tid)
        ws = FakeWS()
        self.ws_list.append(ws)
        return ws, tid

    def close_tab(self, target_id=None):
        self.closed.append(target_id)


def _with_channels(channels, browser_map, eval_fn, fn):
    """临时替换通道配置、浏览器实例与 _eval，执行 fn 后恢复。"""
    orig_channels = zconfig.CFG.get("custom_web_channels")
    orig_get = zwebchat.get_browser
    orig_eval = zds._eval
    zconfig.CFG["custom_web_channels"] = channels
    zwebchat.get_browser = lambda ch: browser_map[ch["id"]]
    zds._eval = eval_fn
    try:
        return fn()
    finally:
        zconfig.CFG["custom_web_channels"] = orig_channels
        zwebchat.get_browser = orig_get
        zds._eval = orig_eval


def main():
    # 与本机用户配置隔离：安装目录里可能存在真实的 webchat_channels.json，
    # load_user_channels() 会优先读取它，从而覆盖下面基于 CFG 的打桩数据，
    # 导致断言随用户配置漂移。测试内统一置空，保证结果确定。
    _orig_load_user_channels = zwebchat.load_user_channels
    zwebchat.load_user_channels = lambda: None

    print("[1] 配置校验（_channels）")
    base = zconfig.CFG.get("custom_web_channels")
    zconfig.CFG["custom_web_channels"] = [
        {"id": "good", "name": "好通道", "url": "https://a.example.com/"},
        {"id": "Bad ID", "name": "非法id", "url": "https://b.example.com/"},
        {"id": "nourl", "name": "无url"},
        {"id": "off", "name": "已禁用", "url": "https://c.example.com/", "enabled": False},
        {"id": "good2", "name": "第二通道", "url": "https://d.example.com/", "enabled": True},
    ]
    try:
        chs = zwebchat._channels()
        ids = [c["id"] for c in chs]
        check("合法通道保留", ids == ["good", "good2"], str(ids))
        check("disabled 通道跳过", "off" not in ids)
        check("非法 id 跳过", "Bad ID" not in ids)
        check("无 url 跳过", "nourl" not in ids)
        check("profile 默认独立目录",
              zwebchat._profile_dir(chs[0]).endswith("data" + os.sep + "webchat_good_profile"))
    finally:
        zconfig.CFG["custom_web_channels"] = base

    print("[2] zwish 路由 __web__:<id>")
    dispatched = {}
    base_chs = zconfig.CFG.get("custom_web_channels")
    zconfig.CFG["custom_web_channels"] = [
        {"id": "good", "name": "好通道", "url": "https://a.example.com/"}]

    def fake_webchat(cid, system, user, timeout=150):
        dispatched["cid"] = cid
        return "回复文本"

    import zwebchat as _zwc
    orig_webchat = _zwc.web_chat
    try:
        _zwc.web_chat = fake_webchat
        text, used = zwish.llm_call("__web__:good", "sys", "user")
        check("分发到 zwebchat.web_chat", dispatched.get("cid") == "good")
        check("返回回复", text == "回复文本")
        check("显示名带通道名", "好通道" in used, used)
    finally:
        _zwc.web_chat = orig_webchat
        zconfig.CFG["custom_web_channels"] = base_chs
    try:
        zwish.llm_call("__web__:nope", "sys", "user")
        check("未知通道抛错", False)
    except (ValueError, RuntimeError) as e:
        check("未知通道抛错", "nope" in str(e), str(e)[:80])

    print("[3] 引擎会话：复用 / 清理 / 通道隔离")
    chs_cfg = [
        {"id": "chana", "name": "通道A", "url": "https://a.example.com/"},
        {"id": "chanb", "name": "通道B", "url": "https://b.example.com/"},
    ]
    browser_map = {"chana": FakeBrowser("a"), "chanb": FakeBrowser("b")}
    probe_seen_ws = set()

    def eval_fn(_ws, expr, timeout=30):
        if "!!window.__zfind" in expr:
            return True
        if "登录" in expr and "hasComposer" not in expr:
            return False                      # 非登录页
        if "insertText" in expr or "tagName === 'TEXTAREA'" in expr:
            return "ok"                       # 填充成功
        if "btn.click()" in expr:
            return "clicked"                  # 发送成功
        if "JSON.stringify({count:" in expr:
            if id(_ws) not in probe_seen_ws:  # 每次会话首个探针 = 发送前基线
                probe_seen_ws.add(id(_ws))
                return json.dumps({"count": 0, "text": "", "busy": 0, "bodyLen": 100})
            return json.dumps({"count": 1, "text": "AI回复", "busy": 0, "bodyLen": 120})
        return None

    captured = {}

    real_wrap = zprompts.ds_wrap
    zprompts.ds_wrap = lambda s, u: captured.update(system=s, user=u) or real_wrap(s, u)
    try:
        def run_two():
            r1 = zwebchat.web_chat("chana", "sys", "u1")
            r2 = zwebchat.web_chat("chana", "sys", "u2")
            zwebchat.web_chat("chanb", "sys", "u3")
            return r1, r2

        r1, r2 = _with_channels(chs_cfg, browser_map, eval_fn, run_two)
        a, b = browser_map["chana"], browser_map["chanb"]
        check("同通道复用同一浏览器", a.n == 2 and b.n == 1)
        check("回复正确返回", r1 == "AI回复" and r2 == "AI回复")
        check("标签一一对应无泄漏",
              sorted(a.opened) == sorted(a.closed) and sorted(b.opened) == sorted(b.closed),
              f"A={a.opened}/{a.closed} B={b.opened}/{b.closed}")
        check("会话 WebSocket 已关闭", all(w.closed for w in a.ws_list + b.ws_list))
        check("契约前置注入", "通道硬性约束" in (captured.get("system") or ""))
        check("契约末尾强调", "再次强调" in (captured.get("user") or ""))
    finally:
        zprompts.ds_wrap = real_wrap

    print("[4] 通道名进入错误信息 / Enter 兜底 / DOM 诊断")
    browser_map2 = {"chana": FakeBrowser("a2")}

    def eval_fn2(_ws, expr, timeout=30):
        if "!!window.__zfind" in expr:
            return True
        if "登录" in expr and "hasComposer" not in expr:
            return False
        if "insertText" in expr or "tagName === 'TEXTAREA'" in expr:
            return "ok"
        if "btn.click()" in expr:
            return "missing"                  # 发送按钮始终找不到
        if "keyCode:13" in expr:
            return "enter"                    # Enter 兜底成功
        if "JSON.stringify({count:" in expr:
            if id(_ws) not in probe_seen_ws:
                probe_seen_ws.add(id(_ws))
                return json.dumps({"count": 0, "text": "", "busy": 0, "bodyLen": 100})
            return json.dumps({"count": 1, "text": "AI回复", "busy": 0, "bodyLen": 120})
        return None

    def run_enter():
        return zwebchat.web_chat("chana", "sys", "u")

    r = _with_channels(chs_cfg[:1], browser_map2, eval_fn2, run_enter)
    check("发送按钮缺失时 Enter 兜底", r == "AI回复")

    def eval_fn3(_ws, expr, timeout=30):
        if "!!window.__zfind" in expr:
            return True
        if "登录" in expr and "hasComposer" not in expr:
            return False
        if "insertText" in expr or "tagName === 'TEXTAREA'" in expr:
            return "ok"
        if "btn.click()" in expr or "keyCode:13" in expr:
            return "missing"                  # 发送完全失败
        return None

    def run_fail():
        try:
            zwebchat.web_chat("chana", "sys", "u")
            return None
        except RuntimeError as e:
            return str(e)

    err = _with_channels(chs_cfg[:1], browser_map2, eval_fn3, run_fail)
    check("发送失败报通道名与选择器指引",
          err is not None and "通道A" in err and "selectors.send" in err, (err or "")[:80])

    print("[5] server / 前端接线")
    srv = open(os.path.join(HERE, "server.py"), encoding="utf-8").read()
    check("/api/config 返回 custom_channels", '"custom_channels": custom_channels' in srv)
    check("提供 webchat_login 接口", '"/api/webchat_login"' in srv)
    check("提供 webchat_status 接口", '"/api/webchat_status"' in srv)
    fe = open(os.path.join(HERE, "static", "index.html"), encoding="utf-8").read()
    check("modelParam 映射 __web__:", '"__web__:" + S.model.slice(4)' in fe)
    check("动态 chip data-webchip", "data-webchip" in fe)
    check("setModel 可用性守卫", "find(c => \"web:\"+c.id === m)" in fe)
    check("askIsCloud 含 web: 前缀", 'S.model.startsWith("web:")' in fe)

    print("[6] config.example.json 预设（不含豆包）")
    cfg_ex = json.load(open(os.path.join(HERE, "config.example.json"), encoding="utf-8"))
    presets = cfg_ex.get("custom_web_channels") or []
    pids = [p.get("id") for p in presets]
    check("千问预设存在", "qianwen" in pids, str(pids))
    check("Grok 预设存在", "grok" in pids)
    check("文心一言预设存在", "yiyan" in pids)
    check("预设默认全部关闭", all(p.get("enabled") is False for p in presets))
    chan_blob = json.dumps(presets, ensure_ascii=False).lower()
    check("预设不含豆包", "豆包" not in chan_blob and "doubao" not in chan_blob)

    print(f"\n结果：{PASSED}/{PASSED + FAILED} 通过")
    zwebchat.load_user_channels = _orig_load_user_channels
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
