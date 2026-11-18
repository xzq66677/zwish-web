#!/usr/bin/env python
"""zyuanbao - 腾讯元宝网页自动化（本机实验性备用通道）。

背景：DS 网页额度用尽时的手动备用通道（功能需求见 issue #14），已正式纳入主仓库。
请勿自动轮换账号或绕过任何平台限额——是否使用本通道完全由用户在顶部手动选择。

设计：
  * 复用 zds 的 CDP 基础设施（浏览器探测、WebSocket、helper 注入、两把锁）；
  * 使用**独立 profile** data/yuanbao_profile，与 DeepSeek 登录态完全隔离；
  * 首次使用会在可见浏览器窗口弹出登录页（微信 / 手机 / QQ），登录一次后自动复用；
  * 文字提示词发送到腾讯元宝；看图/点评仍由本地视觉模型完成，不上传图片。
"""
import atexit
import json
import os
import time

import zconfig
import zds
import zprompts

CFG = zconfig.CFG
PROFILE = CFG.get("yuanbao_profile_dir") or os.path.join(
    zconfig.HERE, "data", "yuanbao_profile")

# 默认选择器：基于 2026-11-17 真实页面结构；可在 config.json 的
# yuanbao_web_selectors 中覆盖，无需改代码。
_COMPOSER_SELS = ['div.ql-editor[contenteditable="true"]', '[contenteditable="true"]']
_SEND_SELS = ['div[aria-label="发送"]', '[aria-label*="发送"]',
              'button:has-text("发送")', '[role="button"]:has-text("发送")']
_ANSWER_SELS = ['.hyc-content-md', '[class*="hyc-content"]', '[class*="markdown"]',
                '[class*="conv-item"] [class*="content"]']

# 元宝通道硬约束：实测（2026-11-18）元宝会把"改风格"类反馈回答成对话式
# "帮你重新生成"，或用中文长句充当 prompt_en。除共享模板外，这里对每条
# 消息额外注入通道级契约，并在末尾重复强调（对弱指令模型更有效）。
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


def _sel(key, default):
    sels = CFG.get("yuanbao_web_selectors") or {}
    value = sels.get(key)
    return value if value else default


def available():
    """检测元宝网页通道是否可用，返回 (bool, 说明)。"""
    try:
        _, desc = zds.browser_choice()
    except RuntimeError as e:
        return False, str(e)
    return True, (f"元宝 CDP 通道就绪 · 浏览器：{desc}"
                  "（独立 profile，首次需登录一次；文字会发送到腾讯元宝）")


class _YuanbaoBrowser(zds._Browser):
    """复用 zds 的启动逻辑，仅替换 profile 与 headless 配置。"""

    def __init__(self):
        old_profile = zds.PROFILE
        old_headless = zds.CFG.get("ds_web_headless", False)
        zds.PROFILE = PROFILE
        zds.CFG["ds_web_headless"] = bool(CFG.get("yuanbao_web_headless", False))
        try:
            super().__init__()
        finally:
            zds.PROFILE = old_profile
            zds.CFG["ds_web_headless"] = old_headless


_SINGLETON = None


def get_browser():
    """获取元宝专用常驻浏览器；进程已死则重建。

    与 DS 通道共用 zds._BROWSER_LOCK，避免并发首启拉起多个浏览器；
    页面会话与 DS 共用 zds._WEB_LOCK，网页自动化全程串行。
    """
    global _SINGLETON
    with zds._BROWSER_LOCK:
        if _SINGLETON is None or not _SINGLETON.alive():
            _SINGLETON = _YuanbaoBrowser()
        return _SINGLETON


def shutdown_browser():
    global _SINGLETON
    if _SINGLETON is not None:
        _SINGLETON.close()
        _SINGLETON = None


atexit.register(shutdown_browser)


def open_login_window():
    """打开元宝页面供用户登录（不发送任何内容），返回 (browser, ws, target_id)。

    登录窗口登记到 zds 的残留表：对话结束后由 close_login_tabs 统一关闭，
    避免登录窗口永久残留占用桌面（Issue: 对话完后原网页自动删除）。
    """
    browser = get_browser()
    url = CFG.get("yuanbao_web_chat_url") or "https://yuanbao.tencent.com/"
    ws, target_id = browser.new_tab(url)
    zds.register_login_tab(browser, target_id)
    return browser, ws, target_id


def login_state():
    """返回当前元宝标签页的登录态摘要，供本机诊断使用。"""
    browser = get_browser()
    url = CFG.get("yuanbao_web_chat_url") or "https://yuanbao.tencent.com/"
    ws, target_id = browser.new_tab(url)
    try:
        time.sleep(3)
        composers = json.dumps(_sel("composer", _COMPOSER_SELS), ensure_ascii=False)
        raw = zds._eval(ws, f"""(function(){{
          var t=((document.body&&document.body.innerText)||'');
          return JSON.stringify({{
            hasComposer: !!window.__zfind({composers}),
            hasLoginPanel: t.indexOf('微信登录')>=0 || t.indexOf('扫码')>=0 ||
                           t.indexOf('未登录')>=0,
            url: location.href,
            title: document.title
          }});
        }})()""")
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


def web_chat(system, user, timeout=150):
    """在元宝网页发起一次对话，返回助手回复文本。

    与 DS 通道一样全程持 zds._WEB_LOCK 串行，避免共享浏览器被并发操作。
    """
    with zds._WEB_LOCK:
        return _web_chat_locked(system, user, timeout)


def _web_chat_locked(system, user, timeout):
    ok, msg = available()
    if not ok:
        raise RuntimeError(msg)

    full = zprompts.ds_wrap(_CONTRACT + "\n" + system,
                            user + "\n\n" + _CONTRACT_REMINDER)
    composers = json.dumps(_sel("composer", _COMPOSER_SELS), ensure_ascii=False)
    sends = json.dumps(_sel("send", _SEND_SELS), ensure_ascii=False)
    answers = json.dumps(_sel("answer", _ANSWER_SELS), ensure_ascii=False)
    url = CFG.get("yuanbao_web_chat_url") or "https://yuanbao.tencent.com/"

    browser = get_browser()
    ws = target_id = None
    msg_sent = False
    try:
        ws, target_id = browser.new_tab(url)
        time.sleep(2)

        # 等待登录：编辑器出现即视为就绪；未登录时提示用户在可见窗口登录。
        login_deadline = time.time() + min(timeout, 120)
        login_grace = time.time() + 10
        prompted = False
        while time.time() < login_deadline:
            if zds._eval(ws, f"!!window.__zfind({composers})"):
                break
            if not prompted and time.time() > login_grace:
                on_login = zds._eval(ws, """(function(){
                  var t=((document.body&&document.body.innerText)||'');
                  return t.indexOf('微信登录')>=0 || t.indexOf('扫码')>=0 ||
                         t.indexOf('未登录')>=0;
                })()""")
                if on_login:
                    prompted = True
                    print("[zyuanbao] 请在弹出的元宝浏览器窗口完成登录"
                          "（微信/手机/QQ），登录后自动继续…", flush=True)
            time.sleep(1)
        else:
            raise RuntimeError(
                "等待元宝登录/加载超时。请在程序弹出的元宝浏览器窗口中登录一次"
                "（登录态保存在 data/yuanbao_profile），之后自动复用。")

        # 填充 Quill 编辑器：优先 execCommand，失败则 paste，再兜底 textContent。
        fill = f"""(function(){{
          var box = window.__zfind({composers});
          if (!box) return 'no-box';
          var text = {zds._js_str(full)};
          try {{ box.focus(); }} catch(e) {{}}
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
          box.dispatchEvent(new Event('input', {{bubbles: true}}));
          box.dispatchEvent(new Event('change', {{bubbles: true}}));
          return ((box.innerText||'') + (box.textContent||'')).trim().length ? 'ok' : 'empty';
        }})();"""
        filled = zds._eval(ws, fill)
        if filled != "ok":
            raise RuntimeError(
                "未能写入元宝输入框（%s）。请在设置中确认 yuanbao_web_selectors.composer。"
                % filled)

        # 等待发送按钮可用（React 受控状态更新），再点击。
        send_ready = f"""(function(){{
          var btn = window.__zfind({sends});
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
            raise RuntimeError(
                "元宝发送按钮不可用（%s）。请确认输入框已成功填入文本，"
                "或在 yuanbao_web_selectors.send 中更新选择器。" % send_state)
        msg_sent = True

        # 等待回复：取最后一个候选答案节点，连续 3 次稳定且无生成中标志。
        probe = f"""(function(){{
          var sels = {answers}, nodes = null;
          for (var i=0; i<sels.length; i++) {{
            try {{ var ns = document.querySelectorAll(sels[i]);
                  if (ns.length) {{ nodes = ns; break; }} }} catch(e) {{}}
          }}
          var text = nodes ? ((nodes[nodes.length-1].innerText||'').trim()) : '';
          var body = (document.body&&document.body.innerText)||'';
          var busy = /停止生成|正在生成|思考中/.test(body) ? 1 : 0;
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
            changed = text and text != base_text and text != full
            if text and (grew or changed) and text == last_text and not busy:
                stable += 1
            else:
                stable = 0
            last_text = text if (grew or changed) else ""
            if stable >= 3:
                break
        if not last_text:
            raise RuntimeError(
                "未读取到元宝回复（候选节点 %s 个，页面文本 %s 字符）。"
                "可能是网页改版、未登录或触发平台限额；"
                "请在 yuanbao_web_selectors.answer 中更新选择器后重试。"
                % (baseline.get("count", 0), baseline.get("bodyLen", 0)))
        return last_text
    finally:
        if msg_sent:
            try:
                zds.delete_chat_record(ws)
            except Exception:
                pass
        try:
            zds.close_login_tabs(browser)
        except Exception:
            pass
        try:
            if ws:
                ws.close()
        except Exception:
            pass
        if zds._cleanup_cfg().get("close_tab", True):
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
    ok, msg = available()
    print(f"[zyuanbao] {msg}")
    if ok:
        print(json.dumps(login_state(), ensure_ascii=False, indent=2))
