#!/usr/bin/env python3
"""回归测试：验证 loopback 请求绕过系统代理，云端请求仍走系统代理。

对应 Issue #1：Windows 开启系统代理且 ProxyOverride 未含 loopback 时，
本机 Ollama / ComfyUI / CDP 请求被转发到代理，返回 HTTP 404。

测试思路（无需 Windows、无需真实 Ollama/ComfyUI）：
  1. 起一个本地假服务模拟 Ollama，和一个"毒代理"（对任何请求返回 404）。
  2. 让本机地址的默认 opener 强制走毒代理（复现 Bug 环境）。
  3. 断言 zconfig.urlopen_local 能直连本地假服务（修复点生效）。
  4. 断言云端 URL 的默认 opener 仍走毒代理（没误伤云端代理）。
  5. 断言 no_proxy 环境变量已补齐 loopback。

运行： python tests/test_loopback_no_proxy.py
退出码 0 = 全部通过。
"""
import http.server
import json
import os
import sys
import threading
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import zconfig

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond)))
    flag = "PASS" if cond else "FAIL"
    print(f"[{flag}] {name}" + (f"  ({detail})" if detail else ""))


class _LocalHandler(http.server.BaseHTTPRequestHandler):
    """模拟本机 Ollama：正常返回 200 + JSON。"""

    def do_GET(self):
        self._reply()

    def do_POST(self):
        self._reply()

    def _reply(self):
        body = json.dumps({"ok": True, "path": self.path}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class _PoisonProxy(http.server.BaseHTTPRequestHandler):
    """模拟 Windows 系统代理：对转发来的请求统一返回 404（复现 Bug 现象）。"""

    def do_GET(self):
        self._reply()

    def do_POST(self):
        self._reply()

    def _reply(self):
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


def _serve(handler):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, srv.server_address[1]


def main():
    # --- 断言 1：无代理 opener 确实未加载任何系统代理 ---
    # 说明：build_opener(ProxyHandler({})) 传入空字典时，Python 不会注册任何
    # 代理处理器（没有代理 = 直连），因此 opener 内不应存在"带代理条目"的
    # ProxyHandler。这与默认 opener 会在有 http_proxy 时加载代理形成对照。
    active_proxies = {}
    for h in zconfig.LOCAL_OPENER.handlers:
        if isinstance(h, urllib.request.ProxyHandler):
            active_proxies.update(getattr(h, "proxies", {}) or {})
    check("LOCAL_OPENER 未启用任何系统代理", not active_proxies,
          f"active_proxies={active_proxies}")

    # --- 断言 2：no_proxy 已补齐 loopback ---
    np = (os.environ.get("no_proxy", "") + "," + os.environ.get("NO_PROXY", "")).lower()
    for host in ("localhost", "127.0.0.1", "::1"):
        check(f"no_proxy 含 {host}", host in np)

    local, lport = _serve(_LocalHandler)
    proxy, pport = _serve(_PoisonProxy)

    saved = {k: os.environ.get(k) for k in
             ("no_proxy", "NO_PROXY", "http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY")}
    try:
        # --- 构造 Bug 环境：清空绕过列表，把系统代理指向"毒代理" ---
        for k in ("no_proxy", "NO_PROXY"):
            os.environ.pop(k, None)
        poison = f"http://127.0.0.1:{pport}"
        for k in ("http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY"):
            os.environ[k] = poison

        local_url = f"http://127.0.0.1:{lport}/api/tags"
        cloud_url = "http://cloud.example.invalid/chat/completions"

        # --- 断言 3（复现）：默认 opener 访问本地会被毒代理拦成 404 ---
        try:
            with urllib.request.urlopen(local_url, timeout=5) as r:
                default_code = r.status
        except urllib.error.HTTPError as e:
            default_code = e.code
        except Exception as e:
            default_code = f"{type(e).__name__}: {e}"
        check("默认 opener 访问本机被代理拦截（Bug 可复现）",
              default_code == 404, f"got {default_code}")

        # --- 断言 4（核心修复）：urlopen_local 访问本机直连成功 ---
        try:
            with zconfig.urlopen_local(local_url, timeout=5) as r:
                code, payload = r.status, json.load(r)
            check("urlopen_local 访问本机直连返回 200",
                  code == 200 and payload.get("ok") is True,
                  f"status={code}, body={payload}")
        except Exception as e:
            check("urlopen_local 访问本机直连返回 200", False,
                  f"{type(e).__name__}: {e}")

        # --- 断言 5：云端默认 opener 仍走系统代理（没误伤云端） ---
        try:
            with urllib.request.urlopen(cloud_url, timeout=5) as r:
                cloud_code = r.status
        except urllib.error.HTTPError as e:
            cloud_code = e.code
        except Exception as e:
            cloud_code = f"{type(e).__name__}: {e}"
        check("云端默认 opener 仍走系统代理", cloud_code == 404,
              f"got {cloud_code}")
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
