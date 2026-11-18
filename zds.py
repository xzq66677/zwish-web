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
import threading
import time
import urllib.parse
import urllib.request

import zconfig
import zprompts

CFG = zconfig.CFG
PROFILE = CFG.get("ds_profile_dir") or os.path.join(zconfig.HERE, "data", "ds_profile")
# 对话后清理开关的持久化文件（已 gitignore），与 prompts.json 同属"用户覆盖"
CLEANUP_FILE = os.path.join(zconfig.HERE, "web_cleanup.json")

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_SEND_SELS = ['button:has-text("发送")', '[data-testid="send-button"]',
              'button[aria-label*="发送"]', 'button[type="submit"]',
              'div[role="button"]:has-text("发送")', '[role="button"][aria-label*="发送"]',
              '[data-testid="send-button"] div', 'div[role="button"][aria-disabled="false"]:has-text("发送")',
              'button:has-text("Send")', 'button[aria-label*="Send"]',
              '[role="button"][aria-label*="Send"]']

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
        """新建标签页，返回 (ws, target_id)。

        会话状态由调用方持有，不写入共享实例字段——浏览器进程虽常驻复用，
        但 target_id/ws 属于单次会话，写回共享字段会让并发请求互相覆盖
        （Issue #9：close_tab 读共享 target_id 会关错标签并泄漏会话）。
        """
        target = urllib.parse.quote(url, safe=":/?=&%#")
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/json/new?{target}", method="PUT")
        tab = json.loads(zconfig.urlopen_local(req, timeout=15).read())
        ws = _WS(tab["webSocketDebuggerUrl"], timeout=30)
        ws.call("Page.enable")
        ws.call("Runtime.enable")
        ws.call("Runtime.evaluate", {"expression": JS_HELPER})
        return ws, tab.get("id")

    def close_tab(self, target_id=None):
        """关闭指定标签页（不杀浏览器进程），供复用模式清理会话。"""
        if not target_id:
            return
        try:
            zconfig.urlopen_local(
                f"http://127.0.0.1:{self.port}/json/close/{target_id}", timeout=5).read()
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
        # self.ws 已随 #9 改为会话局部状态（不再挂在实例上），这里只关
        # 浏览器级 WebSocket 与进程；会话 ws 由 web_chat 的 finally 自行关闭。
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
# 两把锁职责不同，避免不可重入锁嵌套死锁（web_chat 持 _WEB_LOCK 时会调 get_browser）：
#   _WEB_LOCK      —— 串行化整个网页会话（Issue #9）
#   _BROWSER_LOCK  —— 仅保护单例的创建/替换
_WEB_LOCK = threading.Lock()
_BROWSER_LOCK = threading.Lock()


def get_browser():
    """获取常驻浏览器；进程已死则自动重建。

    登录态保存在 data/ds_profile（独立 profile，与日常浏览器隔离），
    首次在该弹出的窗口里登录一次后，后续调用自动复用，无需重复登录。

    加锁原因：ThreadingHTTPServer 下两个请求可能同时发现单例为 None，
    各自 _Browser() 会拉起两个浏览器进程（竞态实测 8 线程创建 8 个）。
    """
    global _BROWSER_SINGLETON
    with _BROWSER_LOCK:
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
    # 每次求值都把 JS_HELPER 与目标表达式拼进同一次 evaluate：
    # DeepSeek 的客户端导航会重建 JS 上下文，new_tab 时注入的 window.__zfind
    # 会随导航消失（Issue #10）；helper 定义是幂等的（if (!window.__zfind)），
    # 每次重注入代价极低，且不再依赖任何注入时机。
    try:
        r = ws.call("Runtime.evaluate",
                    {"expression": JS_HELPER + "\n" + expr,
                     "returnByValue": True, "awaitPromise": True},
                    timeout=timeout)
    except Exception:
        return None
    if r.get("exceptionDetails") or (r.get("result") or {}).get("subtype") == "error":
        return None
    return (r.get("result") or {}).get("value")


def _js_str(s):
    return json.dumps(s, ensure_ascii=False)


# ── 对话后清理：删网站聊天记录 + 关残留网页（Issue: 对话完后原网页自动删除）──
# 用户选择的两项：① 删除 AI 网站上的本次聊天记录（账号不留痕）；
#                ② 关闭自动化打开的标签页 / 残留登录窗口。
# 设计原则：best-effort，任何一步失败都只记日志、绝不影响已经拿到的回复文本。
#
#  Per-channel cleanup 配置（config.json 的 web_cleanup 段）：
#    "web_cleanup": {
#      "delete_chat": true,   # 对话后尝试删除网站聊天记录（默认开）
#      "close_tab": true,     # 对话后关闭本次标签页（默认开；finally 里本就关）
#      "close_login": true    # 对话后关闭该浏览器残留的登录窗口（默认开）
#    }
#  删除按钮选择器各站点不同，通用兜底见下；可在 config 里用 web_cleanup.delete_selectors
#  覆盖，或在自定义通道里用 selectors.delete_chat 覆盖。

# 通用「删除 / 新建对话」按钮选择器：命中"删除/清空"优先，退而求其次点"新对话"。
# 说明：多数站点没有直接的"删除单条对话"按钮，点"新对话/New chat"可让本次会话
# 从当前视图消失、下次打开不再续上（是否从账号历史彻底移除由站点自身决定）。
_DELETE_CHAT_SELS = [
    # 删除 / 清空当前会话（最贴合"删聊天记录"）
    'button[aria-label*="删除对话"]', 'button[title*="删除对话"]',
    'button:has-text("删除对话")', '[role="button"]:has-text("删除对话")',
    '[aria-label*="清空对话"]', 'button:has-text("清空对话")',
    '[aria-label*="删除会话"]', 'button:has-text("删除会话")',
    '[aria-label*="Delete chat"]', '[title*="Delete chat"]',
    'button:has-text("Delete chat")', '[data-testid*="delete-chat"]',
    # 新建对话（兜底：让本次会话不再续上）
    '[aria-label*="新建对话"]', '[title*="新建对话"]',
    'button:has-text("新对话")', 'button:has-text("新建对话")',
    '[aria-label*="New chat"]', '[title*="New chat"]',
    'button:has-text("New chat")', '[data-testid*="new-chat"]',
]
# 删除后的二次确认弹窗按钮
_DELETE_CONFIRM_SELS = [
    'button:has-text("确定")', 'button:has-text("删除")',
    'button:has-text("确认")', '[role="button"]:has-text("删除")',
    'button:has-text("Delete")', 'button:has-text("Confirm")',
]

# 记录每个浏览器实例打开过的「登录窗口」target_id，供对话后统一关闭。
# key = id(browser)，value = set(target_id)。仅本地内存，进程退出自然清空。
_LOGIN_TABS = {}
_LOGIN_TABS_LOCK = threading.Lock()


def _cleanup_cfg():
    """读取对话后清理配置：web_cleanup.json（UI 保存）> config.json > 默认值。"""
    base = {"delete_chat": True, "close_tab": True, "close_login": True}
    c = CFG.get("web_cleanup")
    if isinstance(c, dict):
        base.update(c)
    try:
        with open(CLEANUP_FILE, encoding="utf-8") as f:
            ov = json.load(f)
        if isinstance(ov, dict):
            base.update({k: v for k, v in ov.items() if k in base})
    except (OSError, ValueError):
        pass
    return base


def cleanup_state():
    """供前端展示：当前生效的清理开关 + 配置文件名。"""
    cfg = _cleanup_cfg()
    return {**cfg, "file": os.path.basename(CLEANUP_FILE)}


def save_cleanup_cfg(data):
    """保存 UI 的清理开关（只写 bool 值，原子写）。返回清理后的字典。"""
    src = data if isinstance(data, dict) else {}
    clean = {k: bool(src.get(k, _cleanup_cfg().get(k, True)))
             for k in ("delete_chat", "close_tab", "close_login")}
    tmp = CLEANUP_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(clean, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CLEANUP_FILE)
    return clean


def register_login_tab(browser, target_id):
    """登记一个登录窗口标签页，供对话结束后 close_login_tabs 统一关闭。"""
    if not browser or not target_id:
        return
    with _LOGIN_TABS_LOCK:
        _LOGIN_TABS.setdefault(id(browser), set()).add(target_id)


def close_login_tabs(browser):
    """关闭该浏览器实例残留的登录窗口（不影响浏览器进程与登录态 profile）。"""
    if not _cleanup_cfg().get("close_login", True):
        return 0
    if not browser:
        return 0
    with _LOGIN_TABS_LOCK:
        tids = list(_LOGIN_TABS.pop(id(browser), set()))
    n = 0
    for tid in tids:
        try:
            browser.close_tab(tid)
            n += 1
        except Exception:
            pass
    return n


def delete_chat_record(ws, selectors=None, timeout=8):
    """尽力删除当前 AI 网站上的本次对话记录。返回命中描述（便于日志/测试）。

    best-effort：找不到按钮 / JS 异常都返回状态串，绝不抛错。
    selectors 可覆盖默认列表（自定义通道用 selectors.delete_chat）。
    """
    if not _cleanup_cfg().get("delete_chat", True):
        return "disabled"
    if not ws:
        return "no-ws"
    sels = selectors if isinstance(selectors, list) and selectors else _DELETE_CHAT_SELS
    sels_js = json.dumps(sels, ensure_ascii=False)
    confirm_js = json.dumps(_DELETE_CONFIRM_SELS, ensure_ascii=False)
    expr = f"""(function(){{
      var sels = {sels_js};
      var btn = window.__zfind(sels);
      if (!btn) return 'no-button';
      var which = '';
      for (var i=0;i<sels.length;i++){{
        try {{ var n = window.__zfind([sels[i]]);
               if (n === btn) {{ which = sels[i]; break; }} }} catch(e) {{}}
      }}
      try {{ btn.click(); }} catch(e) {{ return 'click-error'; }}
      return 'clicked:' + (which || 'unknown');
    }})()"""
    try:
        hit = _eval(ws, expr, timeout=timeout) or "none"
    except Exception:
        return "error"
    # 若点的是"删除"，稍等确认弹窗再点"确定"（新建对话一般无确认，快速跳过）
    if isinstance(hit, str) and hit.startswith("clicked") and (
            "删除" in hit or "delete" in hit.lower()):
        time.sleep(0.6)
        conf = f"""(function(){{ var b = window.__zfind({confirm_js});
          if (b) {{ try {{ b.click(); return 'confirmed'; }} catch(e) {{}} }}
          return 'no-confirm'; }})()"""
        try:
            hit += "|" + str(_eval(ws, conf, timeout=timeout) or "no-confirm")
        except Exception:
            hit += "|confirm-error"
    return hit


def web_chat(system, user, timeout=150):
    # 默认 timeout=150：登录阶段另计最多 30s，最坏总耗时 ≈ 180s，
    # 与前端 api() 的 AbortController 180s 超时预算对齐，避免前端先放弃
    # 而后端线程仍在占用浏览器，用户重试时请求叠加。
    """在 DeepSeek 网页发起一次对话，返回助手回复文本。

    system 作为内置提示词随用户消息一起发送（网页端没有独立 system 输入，
    故拼在消息头部；与 API 通道 system/user 分离的效果等价）。
    """
    # DS·网页是共享单浏览器的交互自动化，全程持锁串行化（Issue #9）：
    # 并发请求只会排队等待，不会互相关闭标签或覆盖会话状态。
    with _WEB_LOCK:
        return _web_chat_locked(system, user, timeout)


def _web_chat_locked(system, user, timeout):
    ok, msg = available()
    if not ok:
        raise RuntimeError(msg)

    full = zprompts.ds_wrap(system, user)
    sels = CFG["ds_web_selectors"]
    composers = json.dumps(sels.get("composer", ["textarea"]), ensure_ascii=False)
    answers = json.dumps(sels.get("answer", [".ds-markdown"]), ensure_ascii=False)
    generating = json.dumps(sels.get("generating", [])
                            + ['button:has-text("停止生成")',
                               'div[role="button"]:has-text("停止")',
                               '[role="button"][aria-label*="停止"]'],
                            ensure_ascii=False)
    sends = json.dumps(_SEND_SELS, ensure_ascii=False)
    url = CFG.get("ds_web_chat_url") or "https://chat.deepseek.com/"

    browser = get_browser()
    ws = target_id = None  # 会话状态局部持有（Issue #9），finally 显式传参
    msg_sent = False
    try:
        ws, target_id = browser.new_tab(url)
        # 页面加载（含 redirect）
        time.sleep(2)

        # 等待登录：以出现输入框为准。首次需用户在弹出的浏览器里登录。
        # 快速失败：宽限期后若检测到停在登录页，立即给出明确指引，不傻等满超时。
        # 登录阶段预算封顶 30s（未登录 ~15s 即可判定），把超时预算留给回复等待，
        # 使整个会话的最坏耗时 ≈ timeout + 30，与前端 180s 超时预算对齐。
        login_deadline = time.time() + min(timeout, 30)
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
              var bs = p.querySelectorAll('button,[role="button"]');
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
        msg_sent = True

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
        # 对话后清理（Issue: 对话完后原网页自动删除）：
        # ① 消息确已发出时，尽力删掉本站在账号里的聊天记录；
        # ② 关闭该浏览器残留的登录窗口。均在 ws 仍存活时执行，best-effort。
        if msg_sent:
            try:
                delete_chat_record(ws)
            except Exception:
                pass
        try:
            close_login_tabs(browser)
        except Exception:
            pass
        # 常驻复用：只断开本会话的 WebSocket 并关闭本次标签页（显式传本次的
        # target_id，不读共享状态），浏览器进程保留，下次调用直接复用。
        # 进程整体退出时由 atexit 清理。
        try:
            if ws:
                ws.close()
        except Exception:
            pass
        if _cleanup_cfg().get("close_tab", True):
            try:
                browser.close_tab(target_id)
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
                ws, tid = b.new_tab("data:text/html,<h1 id='zok'>CDP OK</h1>")
                print("[zds] 页面读取：", _eval(ws, "document.getElementById('zok').textContent"))
            finally:
                b.close()
            print("[zds] 自测通过：无需 playwright，CDP 通道可用")
        except Exception as e:
            print(f"[zds] 自测失败：{e}")
