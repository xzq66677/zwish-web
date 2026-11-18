#!/usr/bin/env python3
"""回归测试：顶部 AI 通道显示 + 各家 AI 形象图标。

覆盖：
  1. pickPreset 的"已配置"分支：禁用的通道会被自动启用并保存，再 renderWebChips + setModel；
  2. 图标机制：AI_ICONS 映射、hubIcon 对图片路径的识别、chip/panel 的 <img> 渲染与 onerror 回退；
  3. server：/icons/<id>.png 静态路由存在且对文件名做白名单校验；
  4. 形象图资源：static/icons/ 目录与映射表中的图片一一存在。

运行： python tests/test_top_channel_icons.py
"""
import os
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
    srv = open(os.path.join(HERE, "server.py"), encoding="utf-8").read()

    print("[1] pickPreset 已配置分支：禁用通道自动启用")
    # 定位 pickPreset 函数体内的关键标志
    m = re.search(r"async function pickPreset\(pid\)\s*\{(.*?)\n\}", html, re.S)
    body = m.group(1) if m else ""
    check("pickPreset 函数存在", bool(body))
    check("已配置分支含 enabled===false 判断", "exist.enabled === false" in body)
    check("自动置 enabled=true", re.search(r"exist\.enabled\s*=\s*true", body) is not None)
    check("保存启用状态", "/api/save_webchat_channels" in body)
    check("重绘顶部 chip", "renderWebChips()" in body)
    check("切换模型", 'setModel("web:"+pid, true)' in body)

    print("[2] 图标机制")
    check("AI_ICONS 映射存在", "const AI_ICONS" in html)
    check("形象图总开关存在", "AI_ICON_ON" in html)
    check("hubIcon 优先读 AI_ICONS", "const built = AI_ICONS[p.id]" in html)
    check("hubIcon 识别图片扩展名", r"/\.(png|svg|webp|jpg|jpeg)$/i" in html)
    check("顶部 chip 渲染 img", '<img src="${esc(ic)}"' in html)
    check("面板图标渲染 img", 'class="hlogo" style="background' in html)
    check("图片加载失败回退 emoji", "this.parentNode.textContent='🌐'" in html)
    check("chip 图标 CSS 存在", ".chip .wico" in html)

    print("[3] server 图标静态路由")
    check("路由判断 /icons/", 'path.startswith("/icons/")' in srv)
    check("文件名白名单校验", 're.fullmatch(r"[a-z0-9_-]+\\.(png|svg|webp|jpg|jpeg)"' in srv)
    check("映射到 static/icons 目录", 'os.path.join(STATIC, "icons", name)' in srv)

    print("[4] 形象图资源与映射一致")
    m = re.search(r"const AI_ICONS = AI_ICON_ON \? \{(.*?)\} : \{\};", html, re.S)
    icon_block = m.group(1) if m else ""
    ids = re.findall(r"(\w+)\s*:\s*\[\"/icons/", icon_block)
    check("映射含至少 9 个 AI", len(ids) >= 9, f"{len(ids)} 个")
    check("无图通道未误配（元宝/文心/星火回退 emoji）",
          "yuanbao:" not in icon_block and "yiyan:" not in icon_block and "spark:" not in icon_block)
    icons_dir = os.path.join(HERE, "static", "icons")
    missing = [i for i in ids if not os.path.isfile(os.path.join(icons_dir, i + ".png"))]
    if os.path.isdir(icons_dir):
        check(f"映射图片全部存在（{len(ids)} 个）", not missing,
              "缺失: " + ",".join(missing) if missing else "")
    else:
        print("[SKIP] static/icons/ 尚未生成（未启用形象图，emoji 回退生效）")

    print("[5] 生图风格融入开关")
    check("AI_STYLE 映射存在", "const AI_STYLE" in html)
    check("风格开关 localStorage", 'STYLE_KEY = "zwishAiStyleOn"' in html)
    check("styleOn 读取开关", "function styleOn()" in html)
    check("toggleStyle 写开关", "function toggleStyle(on)" in html)
    check("styleHint 仅对 web: 通道生效", 'S.model.startsWith("web:")' in html)
    check("风格提示注入 prompt_en 链路(plan)", 'wish:(S.wish||"") + styleHint()' in html)
    check("设置面板有风格开关 UI", 'id="ckStyle"' in html)
    check("加载设置时同步开关", "$(\"ckStyle\").checked = styleOn()" in html)
    # 映射覆盖主要通道
    m = re.search(r"const AI_STYLE = \{(.*?)\};", html, re.S)
    style_block = m.group(1) if m else ""
    style_ids = re.findall(r"(\w+)\s*:\s*\"", style_block)
    check("风格映射含 >=10 通道", len(style_ids) >= 10, f"{len(style_ids)} 个")

    print(f"\n结果：{PASSED}/{PASSED + FAILED} 通过")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
