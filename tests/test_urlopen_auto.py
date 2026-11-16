#!/usr/bin/env python3
"""回归测试：urlopen_auto 按目标主机自动选路（审查 #4 修复）。

覆盖审查发现的边界：通用云端通道 base 若配成本机 OpenAI 兼容服务
（如 LM Studio 127.0.0.1:1234），应直连而非走系统代理；公网地址仍走代理。

运行： python tests/test_urlopen_auto.py
退出码 0 = 全部通过。
"""
import http.server
import json
import os
import sys
import threading
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import zconfig

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond)))
    flag = "PASS" if cond else "FAIL"
    print(f"[{flag}] {name}" + (f"  ({detail})" if detail else ""))


class _OK(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self._reply()

    def do_POST(self):
        self._reply()

    def _reply(self):
        b = b'{"ok":true}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def log_message(self, *a):
        pass


class _Poison(http.server.BaseHTTPRequestHandler):
    """模拟系统代理：所有请求返回 404。"""
    def _r(self):
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_GET = _r
    do_POST = _r

    def log_message(self, *a):
        pass


def _serve(h):
    s = http.server.ThreadingHTTPServer(("127.0.0.1", 0), h)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    return s, s.server_address[1]


def _code(fn):
    try:
        with fn() as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception as e:
        return f"{type(e).__name__}: {e}"


def main():
    # --- 断言 1：_is_loopback 主机判断 ---
    for url, expect in [
        ("http://127.0.0.1:11434/v1/chat", True),
        ("http://127.0.0.2:1234/v1", True),      # 127.0.0.0/8 整段
        ("http://localhost:8188/prompt", True),
        ("http://[::1]:8188/prompt", True),       # IPv6
        ("https://api.deepseek.com/chat", False),
        ("http://cloud.example.com/v1", False),
    ]:
        check(f"_is_loopback({url}) == {expect}", zconfig._is_loopback(url) == expect)

    req = urllib.request.Request("http://127.0.0.1:1234/v1", data=b"{}")
    check("_is_loopback 支持 Request 对象", zconfig._is_loopback(req) is True)

    local, lport = _serve(_OK)
    proxy, pport = _serve(_Poison)
    saved = {k: os.environ.get(k) for k in
             ("no_proxy", "NO_PROXY", "http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY")}
    try:
        # 构造 Bug 环境：无绕过列表 + 系统代理指向毒代理
        for k in ("no_proxy", "NO_PROXY"):
            os.environ.pop(k, None)
        poison = f"http://127.0.0.1:{pport}"
        for k in ("http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY"):
            os.environ[k] = poison

        local_url = f"http://127.0.0.1:{lport}/v1/chat"
        cloud_url = "http://cloud.example.invalid/chat"

        # --- 断言 2：本机地址经 urlopen_auto 直连（修复点）---
        check("urlopen_auto(本机) 直连返回 200",
              _code(lambda: zconfig.urlopen_auto(local_url, timeout=5)) == 200)

        # --- 断言 3：公网地址经 urlopen_auto 仍走代理（未误伤）---
        check("urlopen_auto(公网) 仍走系统代理返回 404",
              _code(lambda: zconfig.urlopen_auto(cloud_url, timeout=5)) == 404)

        # --- 断言 4：Request 对象形式同样正确 ---
        req = urllib.request.Request(local_url, data=b"{}",
                                     headers={"Content-Type": "application/json"})
        check("urlopen_auto(Request 本机) 直连返回 200",
              _code(lambda: zconfig.urlopen_auto(req, timeout=5)) == 200)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        local.shutdown()
        proxy.shutdown()

    passed = sum(1 for _, ok in RESULTS if ok)
    total = len(RESULTS)
    print(f"\n结果：{passed}/{total} 通过")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
