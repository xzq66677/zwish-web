#!/usr/bin/env python3
"""回归测试：网页通道前端自配置（设置面板 🌐 网页通道）。

覆盖：
  1. 存储层：webchat_channels.json 原子读写、校验（非法 id / 重复 / 非法 url /
     selectors 类型）、损坏文件回退；_channels() 优先用户配置、回退 config.json；
  2. server 接线：GET /api/webchat_channels、POST /api/save_webchat_channels；
  3. list_all() 含 disabled 项且带可用性状态；
  4. 前端接线：tab/view、加载/保存/登录/状态函数、chip 刷新、esc 转义。

运行： python tests/test_webchat_config_ui.py
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import zwebchat  # noqa: E402

PASSED = FAILED = 0


def check(name, cond, detail=""):
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"[PASS] {name}" + (f"  ({detail})" if detail else ""))
    else:
        FAILED += 1
        print(f"[FAIL] {name}" + (f"  ({detail})" if detail else ""))


SAMPLE = [
    {"id": "qianwen", "name": "千问", "url": "https://tongyi.aliyun.com/",
     "enabled": True, "selectors": {"composer": ["textarea"]}},
    {"id": "grok", "name": "Grok", "url": "https://grok.com/",
     "enabled": False, "selectors": {}},
]


def main():
    print("[1] 存储层：校验规则")
    orig_file = zwebchat.CHANNELS_FILE
    td = tempfile.mkdtemp()
    zwebchat.CHANNELS_FILE = os.path.join(td, "webchat_channels.json")
    try:
        ok, msg, cleaned = zwebchat.save_user_channels(SAMPLE)
        check("合法列表保存成功", ok and len(cleaned) == 2, msg)
        check("规范化补默认字段", cleaned[0].get("browser") == "auto"
              and cleaned[0].get("headless") is False)
        check("enabled=false 保留", cleaned[1].get("enabled") is False)

        ok, _, _ = zwebchat.save_user_channels([{"id": "Bad ID!", "url": "https://x.com"}])
        check("非法 id 被拒", not ok)
        ok, _, _ = zwebchat.save_user_channels([
            {"id": "dup", "url": "https://x.com"}, {"id": "dup", "url": "https://y.com"}])
        check("重复 id 被拒", not ok)
        ok, _, _ = zwebchat.save_user_channels([{"id": "x", "url": "ftp://x.com"}])
        check("非 http(s) url 被拒", not ok)
        ok, _, _ = zwebchat.save_user_channels([{"id": "x", "url": "https://x.com", "selectors": "bad"}])
        check("selectors 非对象被拒", not ok)
        ok, _, _ = zwebchat.save_user_channels("not a list")
        check("非数组被拒", not ok)

        print("[2] 存储层：读取与回退")
        loaded = zwebchat.load_user_channels()
        check("回读已保存列表", isinstance(loaded, list) and len(loaded) == 2)
        open(zwebchat.CHANNELS_FILE, "w").write("{broken json!!")
        check("损坏文件返回 None（回退）", zwebchat.load_user_channels() is None)
        os.remove(zwebchat.CHANNELS_FILE)
        check("无文件返回 None（回退 config.json）", zwebchat.load_user_channels() is None)

        print("[3] _channels() 优先用户配置、回退 config.json")
        zwebchat.save_user_channels([SAMPLE[0]])
        ids = [c["id"] for c in zwebchat._channels()]
        check("用户配置生效（qianwen）", "qianwen" in ids, str(ids))
        check("用户配置覆盖 config.json", ids == ["qianwen"], str(ids))
        zwebchat.save_user_channels([SAMPLE[1]])
        check("disabled 通道不进入启用列表", zwebchat._channels() == [])

        print("[4] list_all() 含 disabled + 状态")
        zwebchat.save_user_channels(SAMPLE)
        allc = zwebchat.list_all()
        check("list_all 含 disabled 项", len(allc) == 2)
        check("每项带 ok/msg 状态", all("ok" in c and "msg" in c for c in allc))
        check("disabled 项标记正确", [c["enabled"] for c in allc] == [True, False])
    finally:
        zwebchat.CHANNELS_FILE = orig_file

    print("[5] server 接口接线")
    srv = open(os.path.join(HERE, "server.py"), encoding="utf-8").read()
    check("GET /api/webchat_channels", '"/api/webchat_channels"' in srv)
    check("POST /api/save_webchat_channels", '"/api/save_webchat_channels"' in srv)
    check("保存接口调用 save_user_channels", "save_user_channels" in srv)
    check("列表接口调用 list_all", "list_all" in srv)
    check("已定义 CHANNELS_FILE", hasattr(zwebchat, "CHANNELS_FILE")
          and "webchat_channels.json" in zwebchat.CHANNELS_FILE)

    print("[6] 前端接线")
    fe = open(os.path.join(HERE, "static", "index.html"), encoding="utf-8").read()
    check("新增网页通道 tab", 'id="tabWeb"' in fe and "setSetTab('web')" in fe)
    check("新增 viewWeb", 'id="viewWeb"' in fe)
    check("加载函数 loadWebChannelsUI", "function loadWebChannelsUI" in fe)
    check("保存函数 saveWebChannels", "function saveWebChannels" in fe)
    check("登录函数 webLogin", "function webLogin" in fe)
    check("状态函数 webStatus", "function webStatus" in fe)
    check("保存后刷新 chip", "renderWebChips()" in fe)
    check("setSetTab 处理 web", 'web:["tabWeb","viewWeb"]' in fe)
    check("通道字段用 esc 转义", 'esc(c.name||"")' in fe and 'esc(c.url||"")' in fe)
    import re
    import subprocess
    js = re.search(r"<script>(.*?)</script>", fe, re.S).group(1)
    r = subprocess.run(["node", "--check"], input=js, text=True,
                       capture_output=True, timeout=30)
    check("前端 JS 语法有效", r.returncode == 0, (r.stderr or "")[:200])

    print(f"\n结果：{PASSED}/{PASSED + FAILED} 通过")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
