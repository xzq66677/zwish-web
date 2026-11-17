#!/usr/bin/env python3
"""Issue #5 回归测试：提示词卡片操作按钮的视觉层级与聚焦行为。

验证点：
1. 「💬 交流修改」为 primary 主按钮且位于第一位（贴合"对话用中文"的主路径）
2. 「🚀 开始生成」为次级按钮，位于第二位
3. 「✍️ 手动改英文词」降级为 .btn.link 文字链接样式，位于第三位
4. 新增 .btn.link 样式定义存在
5. renderPromptCard 支持自动聚焦输入框；手动编辑（editPrompt）重渲染不抢焦点
"""
import re, sys

PASS = 0
FAIL = 0

def check(name, ok, detail=""):
    global PASS, FAIL
    tag = "PASS" if ok else "FAIL"
    print(f"[{tag}] {name}" + (f" —— {detail}" if detail and not ok else ""))
    PASS += ok
    FAIL += (not ok)

html = open("static/index.html", encoding="utf-8").read()

# --- 断言 1~3：renderPromptCard 函数体内 actions 区按钮顺序与样式 ---
fn = re.search(r'function renderPromptCard\(.*?\n\}', html, re.S)
check("提取 renderPromptCard 函数体", bool(fn))
m = re.search(r'<div class="actions">(.*?)</div>`\)', fn.group(0) if fn else "", re.S)
check("定位到提示词卡片 actions 区", bool(m))
if m:
    actions = m.group(1)
    order = re.findall(r'class="btn[^"]*"[^>]*onclick="([^"\(]+)', actions)
    labels = re.findall(r'>([^<>]+)</button>', actions)
    check("交流修改为 primary 且第一位",
          "btn primary" in actions.split("</button>")[0] and "交流修改" in labels[0],
          f"labels={labels}")
    check("开始生成为第二位普通按钮",
          len(labels) > 1 and "开始生成" in labels[1] and 'class="btn"' in actions.split("</button>")[1])
    check("手动改英文词降级为 link 且第三位",
          len(labels) > 2 and "手动改英文词" in labels[2] and "btn link" in actions.split("</button>")[2])

# --- 断言 4：.btn.link 样式存在 ---
check(".btn.link 样式已定义", ".btn.link{" in html)

# --- 断言 5：自动聚焦 + 手动编辑不抢焦点 ---
sig = re.search(r'function renderPromptCard\(note, refiner, opts\)', html)
check("renderPromptCard 支持 opts 参数", bool(sig))
check("卡片渲染后自动聚焦输入框",
      bool(re.search(r'if \(!\(opts && opts\.noFocus\)\) setTimeout\(\(\)=>\{ try\{ inp\.focus\(\); \}catch\(e\)\{\} \}, 60\);', html)))
check("手动编辑重渲染传 noFocus 避免抢焦点",
      'renderPromptCard("已手动更新英文生图词", null, {noFocus:true})' in html)

# --- 断言 6：JS 语法整体有效（提取脚本块交给 node --check） ---
import subprocess, tempfile, os
scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
js = "\n".join(scripts)
with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
    f.write(js); tmp = f.name
r = subprocess.run(["node", "--check", tmp], capture_output=True, text=True, encoding="utf-8", errors="replace")
os.unlink(tmp)
check("前端 JS 语法有效 (node --check)", r.returncode == 0, r.stderr[:300])

print(f"\n结果：{PASS}/{PASS + FAIL} 通过")
sys.exit(1 if FAIL else 0)
