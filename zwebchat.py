#!/usr/bin/env python
"""zwebchat - 自定义 AI 网页通道（配置驱动的通用网页自动化）。

需求见 issue #23：DS / 元宝之外，用户可在 config.json 的
`custom_web_channels` 里声明任意 AI 对话网站（千问、Grok、文心一言、Kimi…），
即可作为润色通道使用。预置示例不含豆包。

设计：
  * 复用 zds 的 CDP 基础设施（浏览器探测、WebSocket、helper 注入、两把锁）；
  * 每个通道**独立 profile**（默认 data/webchat_<id>_profile，可用 profile 覆盖），
    登录态互不影响；首次使用在可见窗口登录一次后常驻复用；
  * 填充自适应 textarea / contenteditable；发送支持选择器 + aria-disabled
    等待 + 父级兜底 + Enter 兜底；回复探测带 DOM 诊断，便于页面改版时自行调选择器；
  * 通道契约注入（纯文本声明、禁承诺生图、prompt_en English only）；
    英文契约另由 zwish._guarded_parse 运行时守卫兜底（enforce_english_prompt）；
  * 文字提示词会发送到对应网站；看图/点评仍走本地视觉模型，不上传图片；
  * 不绕过任何平台登录与限额，是否使用完全由用户手动选择。
"""
import atexit
import json
import os
import re
import sys
import time

import zconfig
import zds
import zprompts

CFG = zconfig.CFG

# 通道硬约束（与 zyuanbao 同源，措辞通用化）：网页 AI 模型指令跟随弱，
# 必须显式声明纯文本身份并强制英文 prompt_en，否则会出现"帮你重新生成"
# 或中文长句充当 prompt_en（见 issue #21 的真机抓包）。
_CONTRACT = (
    "【通道硬性约束】\n"
    "1. 你是纯文本规划器：不能生成、重绘或\"帮你生成/重新生成\"任何图片，"
    "绝不输出此类承诺或表述。\n"
    "2. 只输出一个 JSON 对象：无 markdown 代码块、无解释、无前后缀文字。\n"
    "3. \"prompt_en\" 必须是纯英文生图词（English only），禁止中文。\n"
    "4. 用户提出任何具体修改点（风格/物体/动作/构图/情绪）时，禁止"
    " action=regenerate，必须输出体现该修改的新 prompt_cn/prompt_en；"
    "regenerate 仅当用户明确只想再随机来一张。\n"
)
_CONTRACT_REMINDER = "【再次强调】只输出一个 JSON；\"prompt_en\" 必须是纯英文；不要承诺生成图片。"

# 未提供选择器时的通用兜底（覆盖常见 React/Vue 聊天 UI；可用配置覆盖）。
_DEFAULT_SELECTORS = {
    "composer": ["textarea[placeholder]", "textarea",
                 'div[contenteditable="true"]', '[contenteditable="true"]'],
    "send": ['button[aria-label*="发送"]', 'div[aria-label="发送"]',
             'div[role="button"]:has-text("发送")', 'button:has-text("发送")',
             '[role="button"]:has-text("发送")', 'button[type="submit"]'],
    "answer": ['[class*="markdown"]', '[class*="answer"]', '[class*="message"] pre'],
    "generating": ['[class*="stop"]', 'button[aria-label*="停止"]'],
}

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")

# ── 智能配置预设库（issue #28）──────────────────────────────────────
# 用户只需输入网站名 / 别名 / 拼音，即可一键填充网址与选择器，无需手查 CSS。
# aliases 用小写，匹配时不区分大小写、中英文与常见简称都支持。
# 选择器为通用兜底（覆盖多数 React/Vue 聊天 UI），站点改版后可按 DOM 诊断微调，
# 与企业级内置通道（DS/元宝）一样遵循"文字外发、图片本地、不绕限额"。
def _preset(cid, name, url, aliases, lang="zh", composer=None, send=None,
            answer=None, generating=None):
    """构造一份预设；未显式给出的选择器按中/英文站点取通用兜底。"""
    if lang == "zh":
        d_send = ['button[aria-label*="发送"]', 'button:has-text("发送")',
                  'div[role="button"]:has-text("发送")', '[role="button"][aria-label*="发送"]',
                  'button[type="submit"]']
        d_answer = ['[class*="markdown"]', '[class*="answer"]', '[class*="message"] pre']
        d_gen = ['[class*="stop"]', 'button[aria-label*="停止"]']
    else:
        d_send = ['button[aria-label*="Send"]', 'button[data-testid*="send"]',
                  'button[type="submit"]', 'button:has-text("Send")',
                  '[role="button"]:has-text("Send")']
        d_answer = ['[class*="markdown"]', '[class*="response"]',
                    '[class*="message"] pre', '[data-testid*="message"]']
        d_gen = ['button[aria-label*="Stop"]', '[class*="stop"]']
    d_composer = ['textarea', '[contenteditable="true"]', 'textarea[placeholder]']
    return {
        "id": cid, "name": name, "url": url, "aliases": aliases,
        "browser": "auto", "headless": False,
        "selectors": {
            "composer": composer or d_composer,
            "send": send or d_send,
            "answer": answer or d_answer,
            "generating": generating or d_gen,
        },
    }


# 国内主流
PRESETS = [
    _preset("qianwen", "通义千问", "https://tongyi.aliyun.com/qianwen/",
            ["千问", "通义千问", "通义", "qwen", "tongyi", "aliyun"],
            composer=["textarea", '[contenteditable="true"]', "textarea#chat-input"],
            answer=['[class*="markdown"]', '[class*="answer-item"]', '[class*="tongyi-markdown"]']),
    _preset("deepseek", "DeepSeek", "https://chat.deepseek.com/",
            ["deepseek", "深度求索", "ds"], lang="en",
            answer=['.ds-markdown', '[class*="assistant"] [class*="markdown"]', '[class*="message"] pre']),
    _preset("yuanbao", "腾讯元宝", "https://yuanbao.tencent.com/chat/",
            ["元宝", "腾讯元宝", "yuanbao", "tencent"],
            answer=['[class*="markdown"]', '[class*="answer"]']),
    _preset("doubao", "豆包", "https://www.doubao.com/chat/",
            ["豆包", "doubao", "字节"],
            composer=["textarea", '[contenteditable="true"]', 'textarea[data-testid*="input"]']),
    _preset("yiyan", "文心一言", "https://yiyan.baidu.com/",
            ["文心一言", "文心", "yiyan", "百度", "baidu"],
            answer=['[class*="markdown"]', '[class*="answer"]', '[class*="result"]']),
    _preset("glm", "智谱清言", "https://chatglm.cn/",
            ["智谱", "清言", "chatglm", "glm", "智谱清言"]),
    _preset("spark", "讯飞星火", "https://xinghuo.xfyun.cn/",
            ["星火", "讯飞", "spark", "xfyun", "讯飞星火"]),
    _preset("kimi", "Kimi", "https://kimi.moonshot.cn/",
            ["kimi", "月之暗面", "moonshot"], lang="en",
            composer=["textarea", '[contenteditable="true"]']),
    _preset("metaso", "秘塔 AI", "https://metaso.cn/",
            ["秘塔", "metaso", "秘塔ai"]),
    _preset("minimax", "MiniMax 海螺", "https://chat.minimaxi.com/",
            ["minimax", "海螺", "abab"], lang="en"),
    _preset("step", "阶跃星辰", "https://www.stepfun.com/",
            ["阶跃", "step", "stepfun", "阶跃星辰"], lang="en"),
    _preset("baichuan", "百川智能", "https://www.baichuan-ai.com/",
            ["百川", "baichuan"]),
    _preset("tiangong", "天工 AI", "https://www.tiangong.cn/",
            ["天工", "tiangong", "昆仑万维"]),
    _preset("sensetime", "商量", "https://chat.sensetime.com/",
            ["商量", "商汤", "sensetime"]),
    _preset("zhihu", "知乎直答", "https://zhida.zhihu.com/",
            ["知乎", "知乎直答", "zhihu"]),
    _preset("pangu", "华为盘古", "https://pangu.huaweicloud.com/",
            ["盘古", "华为", "pangu", "华为盘古"]),
    # 海外主流
    _preset("chatgpt", "ChatGPT", "https://chatgpt.com/",
            ["gpt", "chatgpt", "openai", "gpt4", "gpt-4", "chatgpt"],
            lang="en", composer=['#prompt-textarea', 'textarea', '[contenteditable="true"]'],
            send=['button[data-testid="send-button"]', 'button[aria-label*="Send"]']),
    _preset("gemini", "Gemini", "https://gemini.google.com/",
            ["gemini", "谷歌", "google", "bard", "双子星"], lang="en",
            composer=['textarea[aria-label*="Prompt"]', 'textarea', '[contenteditable="true"]'],
            send=['button[aria-label*="Send"]', 'button[data-testid*="send"]']),
    _preset("claude", "Claude", "https://claude.ai/",
            ["claude", "anthropic"], lang="en",
            composer=['[contenteditable="true"]', 'textarea'],
            send=['button[aria-label*="Send"]']),
    _preset("grok", "Grok", "https://grok.com/",
            ["grok", "马斯克", "xai"], lang="en",
            send=['button[aria-label*="Submit"]', 'button[type="submit"]', 'button:has-text("Send")']),
    _preset("copilot", "Microsoft Copilot", "https://copilot.microsoft.com/",
            ["copilot", "bing", "微软", "必应", "new bing"], lang="en",
            send=['button[aria-label*="Send"]', '[class*="submit"]']),
    _preset("perplexity", "Perplexity", "https://www.perplexity.ai/",
            ["perplexity", "pplx"], lang="en",
            composer=['textarea', '[contenteditable="true"]']),
    _preset("mistral", "Mistral Le Chat", "https://chat.mistral.ai/",
            ["mistral", "le chat", "mixtral"], lang="en"),
    _preset("huggingchat", "HuggingChat", "https://huggingface.co/chat/",
            ["huggingchat", "huggingface", "hf"], lang="en"),
    _preset("poe", "Poe", "https://poe.com/",
            ["poe", "quora"], lang="en"),
]


def match_presets(query, limit=8):
    """按名称/别名/id 模糊匹配预设，返回按相关度排序的预设列表。

    匹配规则：完全相等 > 前缀 > 子串；中文名、英文名、别名、拼音都覆盖。
    query 为空时返回全部预设（前端首次加载展示候选池）。
    """
    q = (query or "").strip().lower()
    if not q:
        return list(PRESETS)
    scored = []
    for p in PRESETS:
        fields = [p["id"], p["name"]] + list(p.get("aliases") or [])
        best = 0
        for f in fields:
            f = str(f).lower()
            if f == q:
                best = max(best, 100)
            elif f.startswith(q):
                best = max(best, 70)
            elif q in f:
                best = max(best, 40)
        if best:
            scored.append((best, p["name"], p))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [p for _, _, p in scored[:limit]]

# ── 自定义通道持久化（issue: 前端网页自配置）─────────────────────
# 用户在网页「⚙️ 设置 → 🌐 网页通道」里增删改的通道列表存这里（已 gitignore），
# 与 prompts.json / providers.json 同属"用户覆盖"文件，避免直接改写 config.json。
# 读取优先级：webchat_channels.json > config.json 的 custom_web_channels。
# 文件不存在/损坏时自动回退到 config.json，保证老用户零改动。
CHANNELS_FILE = os.path.join(zconfig.HERE, "webchat_channels.json")


def load_user_channels():
    """读取用户自配置通道列表（原始 dict 列表）。文件不存在或损坏返回 None 表示"未配置"。"""
    if not os.path.isfile(CHANNELS_FILE):
        return None
    try:
        with open(CHANNELS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        chs = data.get("channels") if isinstance(data, dict) else data
        return chs if isinstance(chs, list) else None
    except Exception as e:
        print(f"[zwebchat] 警告：{CHANNELS_FILE} 读取失败（{e}），回退 config.json",
              file=sys.stderr)
        return None


def save_user_channels(channels):
    """校验并原子写入用户自配置通道列表。返回 (ok, msg, cleaned)。

    cleaned 是规范化后的列表（只保留合法字段），供内存同步与前端回显。
    """
    if not isinstance(channels, list):
        return False, "通道列表必须是数组", []
    cleaned = []
    seen = set()
    for ch in channels:
        if not isinstance(ch, dict):
            continue
        cid = str(ch.get("id") or "").strip()
        if not _ID_RE.match(cid):
            return False, f"非法通道 id：{cid!r}（需小写字母/数字/-/_，≤32 字符）", []
        if cid in seen:
            return False, f"通道 id 重复：{cid}", []
        seen.add(cid)
        url = str(ch.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            return False, f"通道 {cid} 的 url 非法（需 http:// 或 https://）", []
        sels = ch.get("selectors") or {}
        if not isinstance(sels, dict):
            return False, f"通道 {cid} 的 selectors 必须是对象", []
        cleaned.append({
            "id": cid,
            "name": str(ch.get("name") or cid).strip() or cid,
            "enabled": ch.get("enabled") is not False,  # 默认启用
            "url": url,
            "browser": str(ch.get("browser") or "auto").strip() or "auto",
            "headless": bool(ch.get("headless", False)),
            "profile": str(ch.get("profile") or "").strip(),
            "icon": str(ch.get("icon") or "").strip()[:4],  # 顶部入口的 emoji 小图标
            "selectors": {k: (v if isinstance(v, list) else [])
                          for k, v in sels.items() if k in _DEFAULT_SELECTORS},
        })
    try:
        tmp = CHANNELS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"channels": cleaned}, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CHANNELS_FILE)
    except Exception as e:
        return False, f"写入失败：{e}", []
    return True, f"已保存 {len(cleaned)} 个通道", cleaned


_LOGIN_TEXT_JS = """(function(){
  var t=((document.body&&document.body.innerText)||'').toLowerCase();
  return t.indexOf('登录')>=0 || t.indexOf('扫码')>=0 || t.indexOf('二维码')>=0 ||
         t.indexOf('登 录')>=0 || t.indexOf('log in')>=0 || t.indexOf('sign in')>=0;
})()"""


def _channels():
    """读取并校验通道配置，返回 enabled 通道列表。

    优先读网页自配置的 webchat_channels.json；未配置时回退 config.json 的
    custom_web_channels，兼容 #23 的手动配置方式。
    """
    user = load_user_channels()
    raw = user if user is not None else (CFG.get("custom_web_channels") or [])
    out = []
    for ch in raw:
        if not isinstance(ch, dict):
            continue
        if ch.get("enabled") is False:
            continue
        cid = str(ch.get("id") or "").strip()
        if not _ID_RE.match(cid):
            print(f"[zwebchat] 跳过非法通道 id：{cid!r}（需匹配 {_ID_RE.pattern}）",
                  file=sys.stderr)
            continue
        url = str(ch.get("url") or "").strip()
        if not url.startswith("http"):
            print(f"[zwebchat] 跳过通道 {cid}：url 非法（需 http/https）", file=sys.stderr)
            continue
        out.append({
            "id": cid,
            "name": str(ch.get("name") or cid),
            "url": url,
            "browser": ch.get("browser") or "auto",
            "headless": bool(ch.get("headless", False)),
            "profile": str(ch.get("profile") or "").strip(),
            "icon": str(ch.get("icon") or "").strip()[:4],
            "selectors": ch.get("selectors") or {},
        })
    return out


def _channel(cid):
    for ch in _channels():
        if ch["id"] == cid:
            return ch
    raise ValueError(
        f"未找到启用的自定义网页通道 {cid!r}。请在 config.json 的 "
        "custom_web_channels 中添加并设置 enabled: true")


def _sel(ch, key):
    value = (ch.get("selectors") or {}).get(key)
    return value if value else _DEFAULT_SELECTORS[key]


def _profile_dir(ch):
    """通道独立 profile；可用配置的 profile 字段覆盖（高级用法）。"""
    if ch.get("profile"):
        p = ch["profile"]
        return p if os.path.isabs(p) else os.path.join(zconfig.HERE, p)
    return os.path.join(zconfig.HERE, "data", f"webchat_{ch['id']}_profile")


def available(cid):
    """检测通道是否可用，返回 (bool, 说明)。"""
    try:
        ch = _channel(cid)
    except ValueError as e:
        return False, str(e)
    try:
        _, desc = zds.browser_choice()
    except RuntimeError as e:
        return False, str(e)
    return True, (f"{ch['name']} CDP 通道就绪 · 浏览器：{desc}"
                  "（独立 profile，首次需登录一次；文字会发送到该网站）")


def list_all():
    """供设置面板编辑：返回全部通道（含 disabled），附带可用性状态。

    数据源与 _channels() 一致（用户配置文件优先，回退 config.json），
    但不剔除 disabled 项——前端需要展示并让用户重新启用。
    """
    user = load_user_channels()
    raw = user if user is not None else (CFG.get("custom_web_channels") or [])
    out = []
    for ch in raw:
        if not isinstance(ch, dict):
            continue
        cid = str(ch.get("id") or "").strip()
        if not _ID_RE.match(cid):
            continue
        url = str(ch.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            continue
        ok, msg = available(cid)
        out.append({
            "id": cid,
            "name": str(ch.get("name") or cid),
            "enabled": ch.get("enabled") is not False,
            "url": url,
            "browser": ch.get("browser") or "auto",
            "headless": bool(ch.get("headless", False)),
            "profile": str(ch.get("profile") or "").strip(),
            "icon": str(ch.get("icon") or "").strip()[:4],
            "selectors": ch.get("selectors") or {},
            "ok": ok,
            "msg": msg,
        })
    return out


def list_enabled_status():
    """供 /api/config 使用：所有 enabled 通道的可用性状态。"""
    out = []
    for ch in _channels():
        ok, msg = available(ch["id"])
        out.append({"id": ch["id"], "name": ch["name"], "ok": ok, "msg": msg})
    return out


# ── 浏览器单例（每通道一个）────────────────────────────────
_BROWSERS = {}


class _ChannelBrowser(zds._Browser):
    """复用 zds 启动逻辑，仅替换 profile / headless / 浏览器偏好。"""

    def __init__(self, ch):
        self._old_profile = zds.PROFILE
        self._old_headless = zds.CFG.get("ds_web_headless", False)
        self._old_browser = zds.CFG.get("ds_web_browser", "auto")
        zds.PROFILE = _profile_dir(ch)
        zds.CFG["ds_web_headless"] = bool(ch.get("headless", False))
        if ch.get("browser") and ch["browser"] != "auto":
            zds.CFG["ds_web_browser"] = ch["browser"]
        try:
            super().__init__()
        finally:
            zds.PROFILE = self._old_profile
            zds.CFG["ds_web_headless"] = self._old_headless
            zds.CFG["ds_web_browser"] = self._old_browser


def get_browser(ch):
    """获取通道专用常驻浏览器；进程已死则重建。

    与 DS / 元宝共用 zds._BROWSER_LOCK（浏览器创建串行，避免
    monkeypatch PROFILE 的竞态）与 zds._WEB_LOCK（页面会话串行）。
    """
    cid = ch["id"]
    with zds._BROWSER_LOCK:
        b = _BROWSERS.get(cid)
        if b is None or not b.alive():
            b = _ChannelBrowser(ch)
            _BROWSERS[cid] = b
        return b


def shutdown_browsers():
    for b in list(_BROWSERS.values()):
        try:
            b.close()
        except Exception:
            pass
    _BROWSERS.clear()


atexit.register(shutdown_browsers)


def open_login_window(cid):
    """打开通道页面供用户登录（不发送任何内容）。"""
    ch = _channel(cid)
    browser = get_browser(ch)
    ws, target_id = browser.new_tab(ch["url"])
    return browser, ws, target_id


def login_state(cid):
    """探测通道页面登录态摘要，供本机 /api/webchat_status 排障使用。"""
    ch = _channel(cid)
    browser = get_browser(ch)
    ws, target_id = browser.new_tab(ch["url"])
    try:
        time.sleep(3)
        composers = json.dumps(_sel(ch, "composer"), ensure_ascii=False)
        expr = ("(function(){"
                "var t=((document.body&&document.body.innerText)||'');"
                "var tl=t.toLowerCase();"
                "return JSON.stringify({"
                f"hasComposer: !!window.__zfind({composers}),"
                "hasLoginPanel: t.indexOf('登录')>=0 || t.indexOf('扫码')>=0 ||"
                " t.indexOf('二维码')>=0 || tl.indexOf('log in')>=0 ||"
                " tl.indexOf('sign in')>=0,"
                "url: location.href,"
                "title: document.title});})()")
        raw = zds._eval(ws, expr)
        return json.loads(raw) if raw else {"error": "login state probe failed"}
    finally:
        try:
            ws.close()
        except Exception:
            pass
        try:
            browser.close_tab(target_id)
        except Exception:
            pass


def channel_name(cid):
    try:
        return _channel(cid)["name"]
    except ValueError:
        return cid


def web_chat(cid, system, user, timeout=150):
    """在自定义 AI 网页发起一次对话，返回助手回复文本。

    全程持 zds._WEB_LOCK：所有网页自动化（DS / 元宝 / 自定义）串行执行。
    """
    with zds._WEB_LOCK:
        return _web_chat_locked(cid, system, user, timeout)


def _web_chat_locked(cid, system, user, timeout):
    ok, msg = available(cid)
    if not ok:
        raise RuntimeError(msg)
    ch = _channel(cid)

    full = zprompts.ds_wrap(_CONTRACT + "\n" + system,
                            user + "\n\n" + _CONTRACT_REMINDER)
    composers = json.dumps(_sel(ch, "composer"), ensure_ascii=False)
    sends = json.dumps(_sel(ch, "send"), ensure_ascii=False)
    answers = json.dumps(_sel(ch, "answer"), ensure_ascii=False)
    url = ch["url"]

    browser = get_browser(ch)
    ws = target_id = None
    try:
        ws, target_id = browser.new_tab(url)
        time.sleep(2)

        # 等待登录：composer 出现即就绪；登录页文案快速识别并提示。
        login_deadline = time.time() + min(timeout, 120)
        login_grace = time.time() + 15
        prompted = False
        while time.time() < login_deadline:
            if zds._eval(ws, f"!!window.__zfind({composers})"):
                break
            if not prompted and time.time() > login_grace:
                if zds._eval(ws, _LOGIN_TEXT_JS):
                    prompted = True
                    print(f"[zwebchat:{cid}] 请在弹出的浏览器窗口完成登录，"
                          "登录后自动继续…", flush=True)
            time.sleep(1)
        else:
            raise RuntimeError(
                f"等待 {ch['name']} 登录/加载超时。请在程序弹出的浏览器窗口中登录一次"
                f"（登录态保存在 {_profile_dir(ch)}），之后自动复用；"
                "若已登录仍超时，可能是网页改版，请更新 custom_web_channels 的 "
                "selectors.composer。")

        # 填充：textarea 原生 setter；contenteditable execCommand → paste → textContent。
        fill = f"""(function(){{
          var box = window.__zfind({composers});
          if (!box) return 'no-box';
          var text = {zds._js_str(full)};
          try {{ box.focus(); }} catch(e) {{}}
          if (box.tagName === 'TEXTAREA' || box.tagName === 'INPUT') {{
            var proto = box.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype
                                                   : HTMLInputElement.prototype;
            var setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
            setter.call(box, text);
          }} else {{
            try {{
              var sel = window.getSelection();
              var range = document.createRange();
              range.selectNodeContents(box);
              sel.removeAllRanges();
              sel.addRange(range);
              document.execCommand('insertText', false, text);
            }} catch(e) {{}}
            if (((box.innerText||'') + (box.textContent||'')).trim().length === 0) {{
              try {{
                var dt = new DataTransfer();
                dt.setData('text/plain', text);
                box.dispatchEvent(new ClipboardEvent('paste',
                  {{clipboardData: dt, bubbles: true, cancelable: true}}));
              }} catch(e) {{}}
            }}
            if (((box.innerText||'') + (box.textContent||'')).trim().length === 0) {{
              box.textContent = text;
            }}
          }}
          box.dispatchEvent(new Event('input', {{bubbles: true}}));
          box.dispatchEvent(new Event('change', {{bubbles: true}}));
          return ((box.innerText||'') + (box.value||'') + (box.textContent||'')).trim()
                 .length ? 'ok' : 'empty';
        }})();"""
        filled = zds._eval(ws, fill)
        if filled != "ok":
            raise RuntimeError(
                f"未能写入 {ch['name']} 输入框（{filled}）。请更新 custom_web_channels "
                "中该通道的 selectors.composer。")

        # 发送：选择器 → aria-disabled 等待 → 父级兜底 → Enter 兜底。
        send_ready = f"""(function(){{
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
          if (!btn) return 'missing';
          var cls = String(btn.className||'');
          var dis = btn.getAttribute('aria-disabled')==='true' ||
                    /disabled|sendNot/i.test(cls);
          if (dis) return 'disabled';
          btn.click();
          return 'clicked';
        }})();"""
        send_state = "missing"
        send_deadline = time.time() + 10
        while time.time() < send_deadline:
            send_state = zds._eval(ws, send_ready) or "missing"
            if send_state == "clicked":
                break
            time.sleep(0.5)
        if send_state != "clicked":
            # 兜底：合成 Enter 事件提交
            enter = f"""(function(){{
              var box = window.__zfind({composers});
              if (!box) return 'no-box';
              box.focus();
              ['keydown','keypress','keyup'].forEach(function(t){{
                box.dispatchEvent(new KeyboardEvent(t, {{key:'Enter', code:'Enter',
                  keyCode:13, bubbles:true}}));
              }});
              return 'enter';
            }})();"""
            send_state = zds._eval(ws, enter) or "no-box"
        if send_state not in ("clicked", "enter"):
            raise RuntimeError(
                f"{ch['name']} 发送未触发（{send_state}）。请确认输入框已填入文本，"
                "并更新 custom_web_channels 中该通道的 selectors.send。")

        # 回复探测：最后候选节点 3 次稳定 + 无生成中标志。
        probe = f"""(function(){{
          var sels = {answers}, nodes = null;
          for (var i=0; i<sels.length; i++) {{
            try {{ var ns = document.querySelectorAll(sels[i]);
                  if (ns.length) {{ nodes = ns; break; }} }} catch(e) {{}}
          }}
          var text = nodes ? ((nodes[nodes.length-1].innerText||'').trim()) : '';
          var body = (document.body&&document.body.innerText)||'';
          var busySel = window.__zfind({json.dumps(_sel(ch, "generating"), ensure_ascii=False)}) ? 1 : 0;
          var busy = busySel || (/停止生成|正在生成|思考中|generating/i.test(body) ? 1 : 0);
          return JSON.stringify({{count: nodes ? nodes.length : 0,
                                 text: text, busy: busy, bodyLen: body.length}});
        }})();"""
        baseline = _read_probe(zds._eval(ws, probe))
        base_count = baseline.get("count", 0)
        base_text = baseline.get("text", "")

        last_text = ""
        stable = 0
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(1)
            info = _read_probe(zds._eval(ws, probe))
            text = info.get("text", "")
            busy = bool(info.get("busy"))
            grew = info.get("count", 0) > base_count
            changed = bool(text) and text != base_text and text != full
            if text and (grew or changed) and text == last_text and not busy:
                stable += 1
            else:
                stable = 0
            last_text = text if (grew or changed) else ""
            if stable >= 3:
                break
        if not last_text:
            raise RuntimeError(
                f"未读取到 {ch['name']} 回复（候选节点 {baseline.get('count', 0)} 个，"
                f"页面文本 {baseline.get('bodyLen', 0)} 字符）。可能是网页改版、未登录"
                "或触发平台限额；请更新 custom_web_channels 中该通道的 "
                "selectors.answer 后重试。")
        return last_text
    finally:
        try:
            if ws:
                ws.close()
        except Exception:
            pass
        try:
            browser.close_tab(target_id)
        except Exception:
            pass


def _read_probe(raw):
    if not raw:
        return {"count": 0, "text": "", "busy": 0, "bodyLen": 0}
    try:
        d = json.loads(raw)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {"count": 0, "text": "", "busy": 0, "bodyLen": 0}


if __name__ == "__main__":
    chs = _channels()
    print(f"[zwebchat] 启用通道：{[c['id'] for c in chs] or '（无）'}")
    for ch in chs:
        ok, msg = available(ch["id"])
        print(f"  [{'OK' if ok else '--'}] {ch['id']} ({ch['name']}): {msg}")
