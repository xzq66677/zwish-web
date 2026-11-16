"""zds - DeepSeek 网页对话自动化（「DS · 网页」通道，零依赖 CDP 版）。

原理：直接通过 Chrome DevTools Protocol (CDP) 控制**本机已安装的 Chrome / Edge**，
不需要 playwright，不需要下载 chromium，也不需要 websocket-client ——
WebSocket 客户端用 Python 标准库（socket + hashlib + base64 + struct）实现。

登录态保存在 data/ds_profile（已 gitignore），首次使用需手动登录一次。
网页改版时只需在 config.json 的 ds_web_selectors 里更新 CSS 选择器，不用改代码。

配置 ds_web_browser：auto(默认，本机 Chrome>Edge>Chromium) / chrome / msedge / chromium / 自定义 exe 绝对路径。
"""
import base64
import hashlib
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import time
import urllib.parse
import urllib.request

import zconfig
import zprompts

CFG = zconfig.CFG
PROFILE = CFG.get("ds_profile_dir") or os.path.join(zconfig.HERE, "data", "ds_profile")

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_SEND_SELS = ['button:has-text("发送")', '[data-testid="send-button"]',
              'button[aria-label*="发送"]', 'button[type="submit"]',
              'button:has-text("Send")', 'button[aria-label*="Send"]']

# 注入到页面里的公共 JS：提供 window.__zfind(selectors)，
# 兼容 Playwright 风格的后缀 :has-text("文字")（CDP 原生 CSS 不支持）。
JS_HELPER = r"""
(function(){
  if (!window.__zfind) {
    window.__zfind = function(sels){
      var list = (typeof sels === 'string') ? [sels] : (sels || []);
      for (var i=0;i<list.length;i++){
        var raw = String(list[i]||''), txt=null, sel=raw;
        var m = raw.match(/:has-text\("([^"]*)"\)/);
        if (m){ txt=m[1]; sel=raw.replace(m[0],''); }
        if (!sel.trim()) continue;
        var nodes;
        try { nodes = document.querySelectorAll(sel); } catch(e){ continue; }
        for (var j=0;j<nodes.length;j++){
          var n = nodes[j];
          if (txt && (n.textContent||'').indexOf(txt)<0) continue;
          return n;
        }
      }
      return null;
    };
  }
})();
"""


# ─────────────────────────────────────────────────────────────
# 最小 WebSocket 客户端（仅用标准库，够 CDP 用）
# ─────────────────────────────────────────────────────────────
class _WS:
    def __init__(self, url, timeout=30):
        u = urllib.parse.urlparse(url)
        host = u.hostname or "127.0.0.1"
        port = u.port or 80
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        path = u.path or "/"
        if u.query:
            path += "?" + u.query
        req = (f"GET {path} HTTP/1.1\r\n"
               f"Host: {host}:{port}\r\n"
               "Upgrade: websocket\r\n"
               "Connection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\n"
               "Sec-WebSocket-Version: 13\r\n\r\n")
        self.sock.sendall(req.encode("ascii"))
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError("WebSocket 握手失败：连接被关闭")
            buf += chunk
        head, _, self._buf = buf.partition(b"\r\n\r\n")
        status = head.split(b"\r\n", 1)[0].decode("ascii", "replace")
        if "101" not in status:
            raise RuntimeError("WebSocket 握手被拒绝：" + status)
        expected = base64.b64encode(
            hashlib.sha1((key + _WS_GUID).encode("ascii")).digest()).decode("ascii")
        accept = ""
        for line in head.split(b"\r\n")[1:]:
            if line.lower().startswith(b"sec-websocket-accept:"):
                accept = line.split(b":", 1)[1].strip().decode("ascii", "replace")
                break
        if accept and accept != expected:
            raise RuntimeError("WebSocket 握手校验失败（Sec-WebSocket-Accept 不匹配）")
        self._id = 0

    def _exact(self, n):
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise RuntimeError("WebSocket 连接已关闭")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _frame(self, opcode, payload):
        mask = os.urandom(4)
        n = len(payload)
        if n < 126:
            head = bytes([0x80 | opcode, 0x80 | n])
        elif n < 65536:
            head = bytes([0x80 | opcode, 0xFE]) + struct.pack("!H", n)
        else:
            head = bytes([0x80 | opcode, 0xFF]) + struct.pack("!Q", n)
        body = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(head + mask + body)

    def send(self, obj):
        self._frame(1, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def recv(self):
        chunks = []
        while True:
            b1, b2 = self._exact(2)
            opcode = b1 & 0x0F
            masked = bool(b2 & 0x80)
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack("!H", self._exact(2))[0]
            elif n == 127:
                n = struct.unpack("!Q", self._exact(8))[0]
            mask = self._exact(4) if masked else None
            data = self._exact(n)
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if opcode == 0x9:  # ping → pong
                pm = os.urandom(4)
                self.sock.sendall(bytes([0x8A, 0x80 | len(data)]) + pm +
                                  bytes(b ^ pm[i % 4] for i, b in enumerate(data)))
                continue
            if opcode == 0x8:
                raise RuntimeError("WebSocket 收到关闭帧")
            if opcode in (0x1, 0x0):
                chunks.append(data)
                if b1 & 0x80:  # FIN
                    return b"".join(chunks).decode("utf-8", "replace")
            # opcode 0x2 (binary) 忽略

    def call(self, method, params=None, timeout=30):
        self._id += 1
        mid = self._id
        self.sock.settimeout(timeout)
        self.send({"id": mid, "method": method, "params": params or {}})
        while True:
            msg = json.loads(self.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(str(msg["error"]))
                return msg.get("result") or {}

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────
# 浏览器探测
# ─────────────────────────────────────────────────────────────
def find_local_browsers():
    """探测本机已安装的桌面浏览器，返回 [(channel, 可执行文件路径)]。"""
    found = []

    def add(channel, path):
        if path and os.path.isfile(path) and not any(c == channel for c, _ in found):
            found.append((channel, path))

    for exe, channel in (("google-chrome", "chrome"), ("chrome", "chrome"),
                         ("microsoft-edge", "msedge"), ("msedge", "msedge"),
                         ("chromium", "chromium"), ("chromium-browser", "chromium")):
        p = shutil.which(exe)
        if p:
            add(channel, p)

    if sys.platform == "win32":
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        local = os.environ.get("LOCALAPPDATA", "")
        for base in (pf, pf86, local):
            add("chrome", os.path.join(base, "Google", "Chrome", "Application", "chrome.exe"))
            add("msedge", os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe"))
    elif sys.platform == "darwin":
        add("chrome", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
        add("msedge", "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge")
    return found


def browser_choice():
    """返回 (浏览器可执行文件路径, 说明)；找不到时抛 RuntimeError。"""
    pref = str(CFG.get("ds_web_browser", "auto") or "auto").strip().lower()
    local = find_local_browsers()

    if pref not in ("auto", "chrome", "msedge", "chromium", "bundled", ""):
        if os.path.isfile(pref):
            return pref, f"自定义浏览器：{pref}"
        raise RuntimeError(f"配置的 ds_web_browser 路径不存在：{pref}")

    order = {"chrome": 0, "msedge": 1, "chromium": 2}
    if pref in ("chrome", "msedge", "chromium"):
        hit = next((p for c, p in local if c == pref), None)
        if not hit:
            raise RuntimeError(f"本机未检测到 {pref}，请安装 Chrome/Edge，或把 ds_web_browser 改成 auto / 自定义 exe 路径")
        return hit, f"本机 {pref.capitalize()}"

    for channel, exe in sorted(local, key=lambda x: order.get(x[0], 9)):
        return exe, f"本机 {channel.capitalize()}"
    raise RuntimeError(
        "本机未检测到 Chrome / Edge / Chromium。请安装任意一款浏览器：\n"
        "  Chrome: https://www.google.com/chrome/   Edge: https://www.microsoft.com/edge\n"
        "本通道零额外依赖，装好浏览器即可用（不需要 playwright / 不需要下载 chromium）。")


def available():
    """检测 DS 网页通道是否可用，返回 (bool, 说明)。"""
    try:
        _, desc = browser_choice()
    except RuntimeError as e:
        return False, str(e)
    return True, f"CDP 通道就绪 · 浏览器：{desc}（零额外依赖，无需 playwright / chromium）"


# ─────────────────────────────────────────────────────────────
# CDP 浏览器封装
# ─────────────────────────────────────────────────────────────
class _Browser:
    def __init__(self):
        self.exe, self.desc = browser_choice()
        os.makedirs(PROFILE, exist_ok=True)
        port_file = os.path.join(PROFILE, "DevToolsActivePort")
        try:
            for name in ("DevToolsActivePort", "SingletonLock", "SingletonSocket",
                         "SingletonCookie"):
                fp = os.path.join(PROFILE, name)
                if os.path.isfile(fp) or os.path.islink(fp):
                    os.remove(fp)
        except OSError:
            pass

        want_headless = bool(CFG.get("ds_web_headless", False))
        # 有头模式可能因「无真实桌面 / DISPLAY 无效」秒退；失败后自动降级 headless 重试一次
        modes = [want_headless] if want_headless else [False, True]
        port = None
        for use_headless in modes:
            for name in ("DevToolsActivePort", "SingletonLock", "SingletonSocket",
                         "SingletonCookie"):
                fp = os.path.join(PROFILE, name)
                try:
                    if os.path.isfile(fp) or os.path.islink(fp):
                        os.remove(fp)
                except OSError:
                    pass

            args = [self.exe,
                    "--remote-debugging-port=0",
                    f"--user-data-dir={PROFILE}",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--disable-blink-features=AutomationControlled",
                    "about:blank"]
            # 无显卡/容器环境必需；普通桌面环境加上也无害
            args.insert(1, "--disable-dev-shm-usage")
            args.insert(1, "--disable-gpu")
            if sys.platform != "win32":
                # Linux 容器 / root 环境下 Chrome 沙箱会拒绝启动
                args.insert(1, "--no-sandbox")
            if use_headless:
                args.insert(1, "--headless=new")

            print(f"[zds] 启动浏览器：{self.desc}" + ("（headless）" if use_headless else ""))
            self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            deadline = time.time() + 20
            while time.time() < deadline:
                if self.proc.poll() is not None:
                    break
                if os.path.isfile(port_file):
                    try:
                        with open(port_file, encoding="utf-8") as f:
                            port = int(f.readline().strip())
                        if port:
                            break
                    except Exception:
                        pass
                time.sleep(0.2)
            if port:
                break
            self._kill()
            port = None
            if not use_headless:
                print("[zds] 有头模式启动失败（可能无图形桌面），自动切换 headless 重试")

        if not port:
            raise RuntimeError(
                "浏览器启动失败：可能 profile 目录被占用，或浏览器本身无法运行。"
                "请关闭 data/ds_profile 对应的浏览器后重试；服务器环境可在 config.json 设 ds_web_headless=true。")
        self.port = port
        try:
            ver = json.loads(zconfig.urlopen_local(
                f"http://127.0.0.1:{port}/json/version", timeout=10).read())
            self.browser_ws = ver.get("webSocketDebuggerUrl")
        except Exception as e:
            self._kill()
            raise RuntimeError(f"读取 CDP 版本信息失败：{e}")
        self.ws = None

    def new_tab(self, url):
        target = urllib.parse.quote(url, safe=":/?=&%#")
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/json/new?{target}", method="PUT")
        tab = json.loads(zconfig.urlopen_local(req, timeout=15).read())
        self.target_id = tab.get("id")
        self.ws = _WS(tab["webSocketDebuggerUrl"], timeout=30)
        self.ws.call("Page.enable")
        self.ws.call("Runtime.enable")
        self.ws.call("Runtime.evaluate", {"expression": JS_HELPER})
        return self.ws

    def close_tab(self, target_id=None):
        """关闭单个标签页（不杀浏览器进程），供复用模式清理会话。"""
        tid = target_id or getattr(self, "target_id", None)
        if not tid:
            return
        try:
            zconfig.urlopen_local(
                f"http://127.0.0.1:{self.port}/json/close/{tid}", timeout=5).read()
        except Exception:
            pass

    def alive(self):
        """浏览器进程是否仍在运行（供复用判断）。"""
        return self.proc is not None and self.proc.poll() is None

    def _kill(self):
        try:
            if self.proc.poll() is None:
                self.proc.terminate()
        except Exception:
            pass

    def close(self):
        try:
            if self.ws:
                self.ws.close()
        except Exception:
            pass
        try:
            if self.browser_ws:
                w = _WS(self.browser_ws, timeout=5)
                w.call("Browser.close", timeout=5)
                w.close()
        except Exception:
            pass
        self._kill()


# ── 浏览器常驻复用：一次进程内只启动一次，多次对话复用同一实例 ──
_BROWSER_SINGLETON = None


def get_browser():
    """获取常驻浏览器；进程已死则自动重建。

    登录态保存在 data/ds_profile（独立 profile，与日常浏览器隔离），
    首次在该弹出的窗口里登录一次后，后续调用自动复用，无需重复登录。
    """
    global _BROWSER_SINGLETON
    if _BROWSER_SINGLETON is None or not _BROWSER_SINGLETON.alive():
        _BROWSER_SINGLETON = _Browser()
    return _BROWSER_SINGLETON


def shutdown_browser():
    """退出时清理常驻浏览器进程。"""
    global _BROWSER_SINGLETON
    if _BROWSER_SINGLETON is not None:
        _BROWSER_SINGLETON.close()
        _BROWSER_SINGLETON = None


import atexit as _atexit
_atexit.register(shutdown_browser)


def _eval(ws, expr, timeout=30):
    try:
        r = ws.call("Runtime.evaluate",
                    {"expression": expr, "returnByValue": True, "awaitPromise": True},
                    timeout=timeout)
    except Exception:
        return None
    if r.get("exceptionDetails") or (r.get("result") or {}).get("subtype") == "error":
        return None
    return (r.get("result") or {}).get("value")


def _js_str(s):
    return json.dumps(s, ensure_ascii=False)


def web_chat(system, user, timeout=240):
    """在 DeepSeek 网页发起一次对话，返回助手回复文本。

    system 作为内置提示词随用户消息一起发送（网页端没有独立 system 输入，
    故拼在消息头部；与 API 通道 system/user 分离的效果等价）。
    """
    ok, msg = available()
    if not ok:
        raise RuntimeError(msg)

    full = zprompts.ds_wrap(system, user)
    sels = CFG["ds_web_selectors"]
    composers = json.dumps(sels.get("composer", ["textarea"]), ensure_ascii=False)
    answers = json.dumps(sels.get("answer", [".ds-markdown"]), ensure_ascii=False)
    generating = json.dumps(sels.get("generating", []) + ['button:has-text("停止生成")'],
                            ensure_ascii=False)
    sends = json.dumps(_SEND_SELS, ensure_ascii=False)
    url = CFG.get("ds_web_chat_url") or "https://chat.deepseek.com/"

    browser = get_browser()
    try:
        ws = browser.new_tab(url)
        # 页面加载（含 redirect）
        time.sleep(2)

        # 等待登录：以出现输入框为准。首次需用户在弹出的浏览器里登录。
        # 快速失败：宽限期后若检测到停在登录页，立即给出明确指引，不傻等满超时。
        login_deadline = time.time() + timeout
        login_grace = time.time() + 15  # 给页面加载留 15s，避免慢网误判
        while time.time() < login_deadline:
            if _eval(ws, f"!!window.__zfind({composers})"):
                break
            if time.time() > login_grace:
                on_login = _eval(ws, """(function(){
                  var t=(document.body&&document.body.innerText)||'';
                  t=t.toLowerCase();
                  return t.indexOf('登录')>=0 || t.indexOf('登 录')>=0 ||
                         t.indexOf('log in')>=0 || t.indexOf('sign in')>=0;
                })()""")
                if on_login:
                    raise RuntimeError(
                        "检测到 DeepSeek 未登录。请注意：本通道使用独立的浏览器配置"
                        "（data/ds_profile），与你日常使用的浏览器登录态互不相通——"
                        "请在程序弹出的那个新浏览器窗口里登录一次，之后自动复用。")
            time.sleep(1)
        else:
            raise RuntimeError(
                f"等待 DeepSeek 网页登录/加载超时（{timeout}s）。注意：本通道使用独立的浏览器"
                "配置（data/ds_profile），请在程序弹出的那个新浏览器窗口里完成登录（不是你"
                "日常使用的浏览器）；登录后自动复用。若已登录仍超时，可能是网页改版，"
                "请更新 config.json 的 ds_web_selectors.composer。")

        # 填入文本：用原生 setter 触发 React/Vue 的 input 事件
        fill = f"""(function(){{
          var box = window.__zfind({composers});
          if (!box) return 'no-box';
          var text = {_js_str(full)};
          if (box.tagName === 'TEXTAREA' || box.tagName === 'INPUT') {{
            var proto = box.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
            var setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
            setter.call(box, text);
          }} else {{
            box.textContent = text;
          }}
          box.dispatchEvent(new Event('input', {{bubbles:true}}));
          box.dispatchEvent(new Event('change', {{bubbles:true}}));
          box.focus();
          return 'ok';
        }})();"""
        if _eval(ws, fill) != "ok":
            raise RuntimeError("找不到 DeepSeek 输入框，请更新 config.json 的 ds_web_selectors.composer")

        time.sleep(0.4)
        # 发送：优先点按钮，找不到就按回车
        send = f"""(function(){{
          var btn = window.__zfind({sends});
          if (!btn) {{
            var box = window.__zfind({composers});
            var p = box ? box.parentElement : null;
            for (var k=0; k<4 && p; k++) {{
              var bs = p.querySelectorAll('button');
              if (bs.length) {{ btn = bs[bs.length-1]; break; }}
              p = p.parentElement;
            }}
          }}
          if (btn) {{ btn.click(); return 'clicked'; }}
          var box2 = window.__zfind({composers});
          if (!box2) return 'no-box';
          ['keydown','keypress','keyup'].forEach(function(t){{
            box2.dispatchEvent(new KeyboardEvent(t, {{key:'Enter', code:'Enter', keyCode:13, bubbles:true}}));
          }});
          return 'enter';
        }})();"""
        _eval(ws, send)

        # 等回复：抓最后一条助手文本，连续 3 次稳定且不在生成中
        prev = _eval(ws, f"""(function(){{var n=window.__zfind({answers});
          return n ? (n.innerText||'').trim() : '';}})()""") or ""
        last_text = ""
        stable = 0
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(1)
            probe = f"""(function(){{
              var n=window.__zfind({answers});
              var busy=window.__zfind({generating}) ? 1 : 0;
              return JSON.stringify({{text:n?(n.innerText||'').trim():'', busy:busy}});
            }})()"""
            raw = _eval(ws, probe)
            try:
                info = json.loads(raw) if raw else {"text": "", "busy": 0}
            except Exception:
                info = {"text": "", "busy": 0}
            text = info.get("text") or ""
            busy = bool(info.get("busy"))
            if text and text != prev and text == last_text and not busy:
                stable += 1
            else:
                stable = 0
            last_text = text
            if stable >= 3:
                break
        if not last_text or last_text == prev:
            raise RuntimeError("未读取到新的 DeepSeek 回复，可能网页结构变化或会话异常，请重试一次")
        return last_text
    finally:
        # 常驻复用：只断开本会话的 WebSocket 并关闭本次标签页，浏览器进程保留，
        # 下次调用直接复用（登录态与进程都不重建）。进程整体退出时由 atexit 清理。
        try:
            if ws:
                ws.close()
        except Exception:
            pass
        try:
            browser.close_tab()
        except Exception:
            pass


if __name__ == "__main__":
    ok, msg = available()
    print(f"[zds] {msg}")
    if ok:
        print("[zds] 自测：启动浏览器 + CDP 通信")
        try:
            b = _Browser()
            try:
                ws = b.new_tab("data:text/html,<h1 id='zok'>CDP OK</h1>")
                print("[zds] 页面读取：", _eval(ws, "document.getElementById('zok').textContent"))
            finally:
                b.close()
            print("[zds] 自测通过：无需 playwright，CDP 通道可用")
        except Exception as e:
            print(f"[zds] 自测失败：{e}")
