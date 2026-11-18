#!/usr/bin/env python3
"""代码审查修复回归：esc 引号转义 / loopback 精确判断 / 历史原子写 / 缓存上限 / 超时对齐。

运行：python tests/test_review_fixes.py
"""
import inspect
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

_passed = _failed = 0


def check(name, cond):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  [PASS] {name}")
    else:
        _failed += 1
        print(f"  [FAIL] {name}")


# ── 1. zconfig._is_loopback：ipaddress 精确判断，域名字符串不误判 ──
print("[1] _is_loopback 精确判断 127.0.0.0/8")
import zconfig  # noqa: E402

check("127.0.0.1 是本机", zconfig._is_loopback("http://127.0.0.1:8189/"))
check("127.7.7.7（127/8 内）是本机", zconfig._is_loopback("http://127.7.7.7/"))
check("localhost 是本机", zconfig._is_loopback("http://localhost:8189/"))
check("[::1] 是本机", zconfig._is_loopback("http://[::1]:8189/"))
check("127.0.0.1.evil.com 不是本机（原前缀判断会误判）",
      not zconfig._is_loopback("http://127.0.0.1.evil.com/"))
check("公网域名不是本机", not zconfig._is_loopback("https://api.deepseek.com/"))
check("无 host 的 URL 不是本机", not zconfig._is_loopback("file:///tmp/x"))

# ── 2. 前端 esc()：属性上下文（引号）也转义 ──
print("[2] esc() 转义引号，阻断属性逃逸")
import subprocess  # noqa: E402

html = open(os.path.join(HERE, "static", "index.html"), encoding="utf-8").read()
m = re.search(r"function esc\(s\)\{.*?\}", html, re.S)
esc_src = m.group(0) if m else ""
node_test = esc_src + """
const evil = '" onmouseover="alert(1)';
const out = esc(evil);
const ok1 = !out.includes('"') && out.includes('&quot;');
const ok2 = !esc("it's").includes("'") && esc("it's").includes("&#39;");
const ok3 = esc('<b>&').includes('&lt;b&gt;&amp;');
console.log(JSON.stringify({ok1, ok2, ok3}));
"""
r = subprocess.run(["node", "-e", node_test], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
try:
    res = __import__("json").loads(r.stdout.strip() or "{}")
except Exception:
    res = {}
check("双引号被转义（属性无法逃逸）", res.get("ok1") is True)
check("单引号被转义（onclick 字符串无法逃逸）", res.get("ok2") is True)
check("原有 & < > 转义不回归", res.get("ok3") is True)
check("画廊 showLb 参数走 esc", "showLb('${esc(i.url)}')" in html)
check("画廊 img src 走 esc", 'src="${esc(i.url)}"' in html)
check("删除按钮参数走 esc", "delImage('${esc(j.image)}')" in html)

# ── 3. 历史删除：原子替换写，不丢并发追加、不产生半行 ──
print("[3] history 删除原子写")
import server  # noqa: E402

old_hist = server.HIST
try:
    with tempfile.TemporaryDirectory() as td:
        server.HIST = os.path.join(td, "history.jsonl")
        with open(server.HIST, "w", encoding="utf-8") as f:
            f.write('{"ts": 1, "image": "a.png", "p": "keep"}\n')
            f.write('{"ts": 2, "image": "b.png", "p": "del"}\n')
            f.write('{"ts": 3, "image": "c.png", "p": "keep2"}\n')
        server._delete_history_entry(2, "b.png")
        lines = open(server.HIST, encoding="utf-8").read().strip().splitlines()
        check("目标记录被删除", len(lines) == 2 and "b.png" not in "\n".join(lines))
        check("其余记录完整保留（无半行）",
              all(__import__("json").loads(x) for x in lines))
        check("无临时文件残留", not os.path.exists(server.HIST + ".tmp"))
        server._delete_history_entry(2, "b.png")  # 再删一次，幂等
        check("重复删除幂等", len(open(server.HIST, encoding="utf-8").read().splitlines()) == 2)
        server._write_history_lines([])
        check("clear 走原子写且文件为空", os.path.getsize(server.HIST) == 0)
finally:
    server.HIST = old_hist

# ── 4. 缓存上限与 JOBS 清理 ──
print("[4] DESCRIBE_CACHE 上限 / JOBS 清理")
server.DESCRIBE_CACHE.clear()
for i in range(server._DESCRIBE_LIMIT + 10):
    server._cache_desc(f"k{i}", f"v{i}")
check("DESCRIBE_CACHE 不超过上限", len(server.DESCRIBE_CACHE) <= server._DESCRIBE_LIMIT)
check("淘汰的是最早写入的条目", "k0" not in server.DESCRIBE_CACHE
      and f"k{server._DESCRIBE_LIMIT + 9}" in server.DESCRIBE_CACHE)
server.DESCRIBE_CACHE.clear()
src = inspect.getsource(server.start_job)
check("start_job 内含已结束任务清理", "_JOBS_KEEP" in src and "pop" in src)

# ── 5. 前后端超时预算对齐 ──
print("[5] 超时预算对齐")
import zds  # noqa: E402

sig = inspect.signature(zds.web_chat)
check("web_chat 默认 timeout=150", sig.parameters["timeout"].default == 150)
zsrc = inspect.getsource(zds._web_chat_locked)
check("登录等待封顶 30s（min(timeout, 30)）", "min(timeout, 30)" in zsrc)
check("最坏总耗时 150+30=180s ≤ 前端 180s", sig.parameters["timeout"].default + 30 <= 180)

print(f"\n结果：{_passed}/{_passed + _failed} 通过")
sys.exit(1 if _failed else 0)
