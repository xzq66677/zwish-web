#!/usr/bin/env python3
"""审查遗留项修复回归：key 脱敏 / key 回填 / 来源校验 / 上传校验。

运行：python tests/test_security_fixes.py
"""
import email.message
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

_passed = _failed = 0


def check(name, cond):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  ✓ {name}")
    else:
        _failed += 1
        print(f"  ✗ {name}")


print("[1] API Key 脱敏与回填")
import server  # noqa: E402

masked = server._mask_providers([{"name": "A", "key": "sk-123", "base": "x"},
                                 {"name": "B", "key": "", "base": "y"}])
check("key 不再明文回传", all(p["key"] == "" for p in masked))
check("has_key 标志正确", masked[0]["has_key"] is True and masked[1]["has_key"] is False)
check("其余字段保留", masked[0]["base"] == "x")

old = zwish_provs = [{"name": "A", "key": "sk-old", "base": "x"}]
server.zwish.PROVIDERS = old
merged = server._merge_provider_keys([{"name": "A", "key": "", "base": "x"},
                                      {"name": "B", "key": "sk-new", "base": "z"}])
check("key 留空 → 沿用旧 key", merged[0]["key"] == "sk-old")
check("填了新 key → 用新 key", merged[1]["key"] == "sk-new")
server.zwish.PROVIDERS = []

print("[2] Host / Origin 来源校验")
for host, origin, expect, desc in [
    ("127.0.0.1", None, True, "本机 Host 放行"),
    ("localhost", None, True, "localhost 放行"),
    ("evil.com", None, False, "外部 Host 拒绝（DNS rebinding）"),
    ("127.0.0.1.evil.com", None, False, "伪装 127 前缀的域名拒绝"),
    ("127.0.0.1", "http://127.0.0.1:8189", True, "本机 Origin 放行"),
    ("127.0.0.1", "http://evil.com", False, "跨源 POST 拒绝（CSRF）"),
    ("127.0.0.1", "http://127.0.0.1.evil.com", False, "伪装域名的 Origin 拒绝"),
]:
    h = object.__new__(server.H)
    m = email.message.Message()
    m["Host"] = host
    if origin:
        m["Origin"] = origin
    h.headers = m
    check(desc, h._same_origin() is expect)

print("[3] 上传校验")
png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
jpg = b"\xff\xd8\xff\xe0" + b"\x00" * 16
webp = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 8
check("PNG 魔数通过", server._looks_like_image(png))
check("JPEG 魔数通过", server._looks_like_image(jpg))
check("WEBP 魔数通过", server._looks_like_image(webp))
check("纯文本拒绝", not server._looks_like_image(b"hello world, not an image!"))
check("空数据拒绝", not server._looks_like_image(b""))
import inspect  # noqa: E402

post_src = inspect.getsource(server.H.do_POST)
check("upload 有体积上限", "_UPLOAD_MAX" in post_src)
check("upload 有魔数校验", "_looks_like_image" in post_src)

print("[4] providers 解析失败可见")
import subprocess  # noqa: E402

code = (
    "import sys, tempfile, os\n"
    f"sys.path.insert(0, {HERE!r})\n"
    "import zwish\n"
    "d = tempfile.mkdtemp()\n"
    "p = os.path.join(d, 'providers.json')\n"
    "open(p, 'w').write('{bad json!!')\n"
    "zwish.PROV_FILE = p\n"
    "print('RESULT:', zwish.load_providers(), file=sys.stderr)\n"
)
r = subprocess.run([sys.executable, "-c", code],
                   capture_output=True, text=True, cwd=HERE, timeout=30)
check("解析失败打印警告到 stderr", "解析失败" in (r.stderr or ""))
check("仍返回空列表不崩溃", "RESULT: []" in (r.stderr or ""))

print(f"\n结果：{_passed}/{_passed + _failed} 通过")
sys.exit(1 if _failed else 0)
