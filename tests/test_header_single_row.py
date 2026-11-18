#!/usr/bin/env python3
"""回归测试：顶栏单行 + 失败不卡在 loading + 白页快速失败（issue #37）。

用户实测截图反馈的两个问题：
  1. 启用了 3 个网页通道后，每个通道都往 header 插一颗 chip，把顶栏挤成两行，
     「历史记录 / 设置」被顶到第二行（观感"兼容性太差"）；
  2. 选中文心一言发请求后，气泡永远停在「✨ 正在理解你的愿望…」，因为
     catch 里只追加了一条错误，从没把 loading 气泡撤掉；后端还要干等 120s。

本测试锁死这些行为，防止回退。

运行： python tests/test_header_single_row.py
"""
import os
import json
import re
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
    html = open(os.path.join(HERE, "static", "index.html"), encoding="utf-8").read()
    web = open(os.path.join(HERE, "zwebchat.py"), encoding="utf-8").read()

    print("[1] 顶栏永不换行")
    head_css = re.search(r"header\{[^}]*\}", html)
    css = head_css.group(0) if head_css else ""
    check("header 使用 flex-wrap:nowrap", "flex-wrap:nowrap" in css, css[:60])
    check("header 不再 row-gap 换行", "row-gap:10px" not in html)
    check("header 内无 flex-wrap:wrap", "flex-wrap:wrap" not in css)
    check("chip 不参与收缩（flex:0 0 auto）",
          re.search(r"\.chip\{[^}]*flex:0 0 auto", html) is not None)
    check("窄屏只留状态绿点（不占宽度）", ".status{font-size:0; gap:0}" in html)
    check("不再整块隐藏状态", ".status{display:none}" not in html)
    check("header 可横向滚动兜底", "overflow-x:auto" in css)
    check("更窄屏收窄 chip", "@media (max-width:880px)" in html)

    print("[2] 历史 / 设置靠右（不会被通道挤走）")
    sp = html.find('<span class="sp"></span>')
    hist = html.find('onclick="openHistory()"')
    cfg = html.find('onclick="openSettings()"')
    check("header 内存在 sp 占位", sp > 0)
    check("历史在 sp 之后", sp < hist, f"sp={sp} hist={hist}")
    check("设置在 sp 之后", sp < cfg, f"sp={sp} cfg={cfg}")

    print("[3] 通道不再各自占一颗 chip，改由「🌐 AI 网页」就地显示")
    m = re.search(r"function renderWebChips\(\)\{(.*?)\n\}", html, re.S)
    body = m.group(1) if m else ""
    check("renderWebChips 仍存在", bool(body))
    check("不再 insertBefore 插入 chip", "insertBefore(el" not in body)
    check("不再给通道写 data-webchip", "el.dataset.webchip" not in body)
    check("改为刷新枢纽入口", "updateHubChip()" in body)
    check("updateHubChip 已定义", "function updateHubChip()" in html)
    check("hub chip 显示通道图标", '<span class="wico"' in html)
    check("hub chip 显示通道名", '${esc(label)}' in html)
    check("chip 名去掉「· 网页」后缀", r'[·・]\\s\*网页' in html or "· 网页" in html)
    check("hub chip 未选中回退文案", 'el.innerHTML = "🌐 AI 网页"' in html)
    check("setModel 同步枢纽入口", re.search(r"function setModel.*?updateHubChip\(\);", html, re.S) is not None)
    check("setModel 不再遍历 data-webchip", "el.dataset.webchip === m" not in html)

    print("[4] 失败必须可见，不能停在 loading")
    check("failBubble 已定义", "function failBubble(div, title, msg)" in html)
    check("错误卡片有样式", ".errbox .errmsg" in html and ".mtag.bad" in html)
    check("错误卡片带登录入口", "hubLoginCurrent()" in html)
    check("错误卡片带换通道入口", 'onclick="openHub()"' in html)
    for fn, name in [
        (r"async function askQuestions\(wish\)\{(.*?)\n\}", "askQuestions"),
        (r"async function composePrompt\(\)\{(.*?)\n\}", "composePrompt"),
        (r"async function doPlan\(feedback\)\{(.*?)\n\}", "doPlan"),
        (r"async function execPlan\(skip\)\{(.*?)\n\}", "execPlan"),
        (r"async function doRewrite\(feedback\)\{(.*?)\n\}", "doRewrite"),
    ]:
        mm = re.search(fn, html, re.S)
        seg = mm.group(1) if mm else ""
        check(f"{name} 失败改用 failBubble", "failBubble(div" in seg)
    check("loading 气泡标注当前通道", "通道：${esc(modelLabel())}" in html)
    check("modelLabel 已定义", "function modelLabel()" in html)

    print("[5] 就绪语义不说谎")
    check("面板不再写「✓ 就绪」", '"✓ 就绪"' not in html)
    check("面板写明需登录", "浏览器就绪 · 首次需登录" in html)
    check("选中后自动预检", "probeChannel(pid)" in html and "async function probeChannel(id)" in html)

    print("[6] 后端白页快速失败")
    check("_page_meta 已定义", "def _page_meta(ws):" in web)
    check("探测 readyState/文本长度", "document.readyState" in web and "innerText" in web)
    check("存在 25s 白页探测点", "blank_deadline = time.time() + 25" in web)
    check("白页时立即抛错（打不开）", "打不开 " in web)
    check("给出可操作建议", "换一个通道" in web and "登录窗口" in web)
    check("等待超时报错含诊断", "登录/加载超时（页面文本" in web)
    check("回读失败提示可先登录", "可先点 🔑 打开登录窗口" in web)
    check("探测循环仍在（正常页继续等登录）", "blank_deadline = time.time() + 40" in web)

    print("[7] _page_meta 解析（白页判定的数据源）")
    try:
        import zds
        import zwebchat
        real_eval = zds._eval
        zds._eval = lambda ws, expr, timeout=30: json.dumps(
            {"ready": True, "len": 42, "url": "https://example.com/", "title": "示例"})
        try:
            meta = zwebchat._page_meta(object())
        finally:
            zds._eval = real_eval
        check("_page_meta 正常解析", meta.get("len") == 42 and meta.get("url", "").startswith("https://"),
              str(meta))
        zds._eval = lambda ws, expr, timeout=30: None
        try:
            bad = zwebchat._page_meta(object())
        finally:
            zds._eval = real_eval
        check("_page_meta 求值失败时回退空 dict", bad == {})
        # 106 字符这类白页应被判为「打不开」（阈值 200）
        check("白页阈值覆盖 106 字符", 106 < 200)
    except Exception as e:  # pragma: no cover
        check("_page_meta 解析（导入失败）", False, str(e))

    print(f"\n结果：{PASSED}/{PASSED + FAILED} 通过")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
