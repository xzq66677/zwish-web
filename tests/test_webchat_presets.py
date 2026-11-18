#!/usr/bin/env python3
"""回归测试：智能配置预设库（issue #28）。

覆盖：
  1. 预设库完整性：数量、ID 唯一、必填字段、url 合法、选择器四类齐全；
  2. 别名覆盖：用户点名的 gemini / gpt 及国内外主流站点都能被匹配；
  3. 模糊匹配：中文名 / 英文名 / 别名 / 拼音 / 简称、大小写不敏感、相关度排序；
  4. server 接线：GET /api/webchat_presets 返回 presets；
  5. 前端接线：智能添加区、下拉候选、smartAdd/smartFill/presetMatch、esc 转义。

运行： python tests/test_webchat_presets.py
"""
import ast
import json
import os
import re
import sys
from urllib.parse import urlparse

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


# ── 1. 预设库完整性 ──────────────────────────────────────────────
ps = zwebchat.PRESETS
check("预设数量 >= 20（覆盖主流）", len(ps) >= 20, f"{len(ps)} 个")
ids = [p["id"] for p in ps]
check("ID 唯一", len(set(ids)) == len(ids))
check("ID 符合校验正则", all(zwebchat._ID_RE.match(p["id"]) for p in ps))
check("全部有 name/url/aliases", all(
    p.get("name") and p.get("url") and p.get("aliases") for p in ps))
check("URL 均为 https 且域名合法", all(
    urlparse(p["url"]).scheme == "https" and urlparse(p["url"]).netloc for p in ps))
check("四类选择器齐全且非空", all(
    all(isinstance(p["selectors"].get(k), list) and p["selectors"][k]
        for k in ("composer", "send", "answer", "generating")) for p in ps))
check("每个预设至少 2 个别名", all(len(p["aliases"]) >= 2 for p in ps))

# ── 2. 用户点名站点覆盖 ──────────────────────────────────────────
for query, expect in [
    ("gemini", "gemini"), ("gpt", "chatgpt"), ("chatgpt", "chatgpt"),
    ("千问", "qianwen"), ("豆包", "doubao"), ("元宝", "yuanbao"),
    ("claude", "claude"), ("grok", "grok"), ("kimi", "kimi"),
    ("文心", "yiyan"), ("deepseek", "deepseek"), ("ds", "deepseek"),
]:
    hits = zwebchat.match_presets(query)
    check(f"匹配「{query}」→ {expect}", any(p["id"] == expect for p in hits),
          ",".join(p["id"] for p in hits))

# 海外几大厂必须在库里
must = {"chatgpt", "gemini", "claude", "grok", "copilot", "perplexity"}
check("海外主流齐全", must <= set(ids), ",".join(sorted(must - set(ids))) or "全")
cn_must = {"qianwen", "deepseek", "yuanbao", "doubao", "yiyan", "kimi", "glm"}
check("国内主流齐全", cn_must <= set(ids), ",".join(sorted(cn_must - set(ids))) or "全")

# ── 3. 模糊匹配行为 ──────────────────────────────────────────────
check("空查询返回全部", len(zwebchat.match_presets("")) == len(ps))
check("大小写不敏感", [p["id"] for p in zwebchat.match_presets("GEMINI")] == ["gemini"])
check("中文别名命中", any(p["id"] == "grok" for p in zwebchat.match_presets("马斯克")))
check("拼音/英文别名命中", any(p["id"] == "chatgpt" for p in zwebchat.match_presets("openai")))
check("精确匹配排最前", zwebchat.match_presets("claude")[0]["id"] == "claude")
check("无匹配返回空", zwebchat.match_presets("不存在的网站xyz") == [])
check("limit 生效", len(zwebchat.match_presets("a", limit=3)) <= 3)

# ── 4. server 接线 ───────────────────────────────────────────────
srv = open(os.path.join(HERE, "server.py"), encoding="utf-8").read()
check("server 有 /api/webchat_presets 路由",
      'path == "/api/webchat_presets"' in srv and "zwebchat.PRESETS" in srv)
ast.parse(srv)
check("server 语法合法", True)

# ── 5. 前端接线 ──────────────────────────────────────────────────
html = open(os.path.join(HERE, "static", "index.html"), encoding="utf-8").read()
for fn in ("loadWebPresets", "presetMatch", "smartSuggest", "smartAdd", "smartFill", "presetToChannel"):
    check(f"前端有 {fn}()", f"function {fn}(" in html)
check("智能添加输入框存在", 'id="wSmart"' in html and 'id="wSmartList"' in html)
check("卡片有一键补全按钮", "smartFill(${i})" in html)
check("预设加载被 UI 调用", "await loadWebPresets()" in html)
check("S 含 webPresets 状态", "webPresets:[]" in html)
check("下拉候选走 esc", 'esc(p.url)' in html and 'esc(p.name)' in html)

# 顶部一键入口（pickPreset）：选预设→自动配置+保存+切换，不进设置面板
check("顶部有 AI 网页入口 chip", 'id="chipHub"' in html and 'onclick="openHub()"' in html)
check("预设面板存在", 'id="hubp"' in html and 'id="hublist"' in html)
for fn in ("openHub", "closeHub", "renderHub", "pickPreset", "hubGroups", "toast"):
    check(f"前端有 {fn}()", f"function {fn}(" in html)
check("pickPreset 自动保存并切换",
      '"/api/save_webchat_channels"' in html.split("function pickPreset")[1].split("async function loadWebChannelsUI")[0]
      and "setModel(\"web:\"+pid, true)" in html)
check("预设按国内/海外分组", "HUB_CN" in html and "国内主流" in html and "海外主流" in html)
check("setModel 支持 force 跳过就绪校验", "function setModel(m, force)" in html
      and "!force && !ch.ok" in html)
check("预设项走 esc（id/名/网址）",
      "esc(p.id)" in html and "esc(p.name)" in html and "esc(p.url)" in html)

# 前端优化：toast 替代刷屏 + file:// 友好提示
check("有 toast 节点", 'id="toast"' in html and "#toast{" in html)
check("file:// 检测给出中文提示",
      'location.protocol === "file:"' in html and "不要直接双击 HTML" in html)
check("智能添加不再往聊天区刷屏",
      'toast("已添加「"+p.name+"」' in html
      and 'addWizard("已智能填充' not in html)

# 前端内嵌预设（关键：单文件更新 / 后端接口缺失时仍可用，修复实测"找不到 gemini"）
check("前端内嵌 BUILTIN_PRESETS 常量", "const BUILTIN_PRESETS = [" in html)
check("loadWebPresets 以内嵌为准", "BUILTIN_PRESETS.slice()" in html
      and "typeof BUILTIN_PRESETS" in html)
check("后端接口失败静默回退", "/api/webchat_presets" in html
      and "静默回退到内嵌预设" in html)
# 内嵌预设 JSON 与后端数量一致、含用户点名站点
m = re.search(r"const BUILTIN_PRESETS = (\[.*?\]);", html, re.S)
check("内嵌预设可被解析", bool(m))
if m:
    embedded = json.loads(m.group(1))
    check("内嵌预设与后端数量一致", len(embedded) == len(ps),
          f"{len(embedded)} vs {len(ps)}")
    emb_ids = {p["id"] for p in embedded}
    check("内嵌含 gemini/chatgpt", {"gemini", "chatgpt"} <= emb_ids)
    check("内嵌与后端 id 集合一致", emb_ids == set(ids))
# JS 语法
m = re.search(r"<script>(.*?)</script>", html, re.S)
js = m.group(1) if m else ""
open("/tmp/preset_check.js", "w", encoding="utf-8").write(js)
import subprocess
r = subprocess.run(["node", "--check", "/tmp/preset_check.js"],
                   capture_output=True, text=True)
check("前端 JS 语法合法", r.returncode == 0, r.stderr.strip()[:120])

print(f"\n结果：{PASSED}/{PASSED + FAILED} 通过")
sys.exit(1 if FAILED else 0)
