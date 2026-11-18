#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归测试：DS·网页 / 元宝·网页 内收进「🌐 AI 网页」（issue #33）。

覆盖：
  1. 前端顶部不再有独立 DS·网页 / 元宝·网页 chip，统一由网页面板进入；
  2. 通用引擎 zwebchat 对 deepseek / yuanbao 做委托（web_chat / available /
     open_login_window / login_state），复用原引擎与已登录 profile；
  3. 委托只在 URL 指向官方站点时生效，用户自建同名通道不被劫持；
  4. DS / 元宝 仍在 AI 网页面板的预设库中，用户可正常添加使用；
  5. 死锁防护：委托在取 zds._WEB_LOCK 之前完成（不可重入锁）。

运行： python tests/test_yuanbao_dsintegrate.py
"""
import ast
import inspect
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


def main():
    fe = open(os.path.join(HERE, "static", "index.html"), encoding="utf-8").read()
    src = open(os.path.join(HERE, "zwebchat.py"), encoding="utf-8").read()

    print("[1] 顶部独立入口已移除（内收）")
    check("无 chipDsWeb", 'id="chipDsWeb"' not in fe)
    check("无 chipYuanbao", 'id="chipYuanbao"' not in fe)
    check("无 chipDsWeb 的 JS 引用", '$("chipDsWeb")' not in fe)
    check("无 chipYuanbao 的 JS 引用", '$("chipYuanbao")' not in fe)
    check("setModel 不再校验 ds_web 就绪", 'm === "ds_web" && !S.dsWebOk' not in fe)
    check("setModel 不再校验 yuanbao 就绪", 'm === "yuanbao" && !S.ybOk' not in fe)

    print("[1b] 打开 AI 网页面板时自动补加载预设")
    import re as _re
    m = _re.search(r"async function openHub\(\)\s*\{(.*?)\n\}", fe, _re.S)
    hub_body = m.group(1) if m else ""
    check("openHub 为 async", "async function openHub" in fe)
    check("openHub 空预设时调 loadWebPresets", "loadWebPresets()" in hub_body)
    check("openHub 仍会 renderHub", "renderHub()" in hub_body)

    print("[2] 通用引擎对 DS / 元宝 委托")
    import zwebchat
    check("_BUILTIN_HOSTS 含 deepseek", "deepseek" in zwebchat._BUILTIN_HOSTS)
    check("_BUILTIN_HOSTS 含 yuanbao", "yuanbao" in zwebchat._BUILTIN_HOSTS)
    d = zwebchat._delegate("deepseek")
    check("_delegate(deepseek) 返回 zds", d is not None and d.__name__ == "zds",
          getattr(d, "__name__", str(d)))
    y = zwebchat._delegate("yuanbao")
    check("_delegate(yuanbao) 返回 zyuanbao", y is not None and y.__name__ == "zyuanbao",
          getattr(y, "__name__", str(y)))
    check("_delegate(其他通道) 返回 None", zwebchat._delegate("qianwen") is None)
    check("_delegate(未知 id) 返回 None", zwebchat._delegate("nope") is None)

    print("[3] 委托函数接线完整")
    for fn in ("web_chat", "available", "open_login_window", "login_state"):
        body = ast.unparse(ast.parse(inspect.getsource(getattr(zwebchat, fn))))
        check(f"{fn} 内含 _delegate 委托", "_delegate(" in body)
    # profile 复用：DS / 元宝 走各自原 profile
    prof = ast.unparse(ast.parse(inspect.getsource(zwebchat._profile_dir)))
    check("_profile_dir 复用 DS profile", "zds.PROFILE" in prof)
    check("_profile_dir 复用元宝 profile", "zyuanbao.PROFILE" in prof)

    print("[4] 死锁防护：委托在取锁之前")
    # 用真实源码（去掉 docstring）判断执行顺序：_delegate 必须在 with zds._WEB_LOCK 之前
    raw_src = inspect.getsource(zwebchat.web_chat)
    body_only = raw_src.split('"""')
    code = "".join(body_only[2:]) if len(body_only) >= 3 else raw_src  # 跳过 docstring
    i_delegate = code.find("_delegate(")
    i_lock = code.find("with zds._WEB_LOCK")
    check("web_chat 先委托后取锁", i_delegate != -1 and i_lock != -1 and i_delegate < i_lock,
          f"delegate@{i_delegate} lock@{i_lock}")
    check("委托分支直接 return", "return mod.web_chat(" in code)

    print("[5] 自建同名通道不被劫持")
    # 构造一个 URL 不指向官方的 deepseek 通道，_delegate 应返回 None
    orig_channels = zwebchat._channels
    try:
        zwebchat._channels = lambda: [{
            "id": "deepseek", "name": "自建DS", "url": "https://example.com/chat",
            "browser": "auto", "headless": False, "profile": "", "icon": "",
            "selectors": {},
        }]
        check("自建 deepseek(非官方URL) 不委托", zwebchat._delegate("deepseek") is None)
        zwebchat._channels = lambda: [{
            "id": "deepseek", "name": "DeepSeek", "url": "https://chat.deepseek.com/",
            "browser": "auto", "headless": False, "profile": "", "icon": "",
            "selectors": {},
        }]
        check("官方 deepseek URL 正常委托", zwebchat._delegate("deepseek") is not None)
    finally:
        zwebchat._channels = orig_channels

    print("[6] 预设库仍可用（用户可从面板添加）")
    check("预设含 deepseek", any(p["id"] == "deepseek" for p in zwebchat.PRESETS))
    check("预设含 yuanbao", any(p["id"] == "yuanbao" for p in zwebchat.PRESETS))
    check("前端预设库含 deepseek", '"id":"deepseek"' in fe or '"id": "deepseek"' in fe)
    check("前端预设库含 yuanbao", '"id":"yuanbao"' in fe or '"id": "yuanbao"' in fe)

    print(f"\n结果：{PASSED}/{PASSED + FAILED} 通过")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
