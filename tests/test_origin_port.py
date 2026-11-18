#!/usr/bin/env python3
"""回归测试：Origin 端口精确比对（issue #20）。

原先 `_same_origin()` 只比对回环主机不比对端口——本机其他端口的页面
（如 127.0.0.1:3000 的开发服务器）也能向 127.0.0.1:8189 发起状态变更。
修复后 Origin 的 host:port 必须与请求 Host 完全一致。

覆盖：
  1. _split_hostport：IPv6 字面量、缺省端口、非法输入；
  2. _same_origin：同 host 同端口放行；同 host 不同端口拒绝（#20 回归）；
     localhost / 127.0.0.1 混用拒绝；Host 缺端口对齐服务端口；
     https 默认端口、非法端口、外部主机、无 Origin。

运行： python tests/test_origin_port.py
"""
import email.message
import os
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


import server  # noqa: E402


def _origin_case(host, origin):
    h = object.__new__(server.H)
    m = email.message.Message()
    m["Host"] = host
    if origin:
        m["Origin"] = origin
    h.headers = m
    return h._same_origin()


def main():
    print("[1] _split_hostport")
    sp = server._split_hostport
    check("IPv6 带端口", sp("[::1]:8189", 8189) == ("::1", 8189))
    check("IPv6 缺端口", sp("[::1]", 8189) == ("::1", 8189))
    check("常规 host:port", sp("127.0.0.1:8189", 9999) == ("127.0.0.1", 8189))
    check("缺端口用默认", sp("127.0.0.1", 8189) == ("127.0.0.1", 8189))
    check("空 Host 用默认", sp("", 8189) == ("", 8189))
    check("IPv6 多冒号不误拆", sp("[::ffff:127.0.0.1]:8189", 1) == ("::ffff:127.0.0.1", 8189))

    print("[2] _same_origin 端口精确比对")
    check("同 host 同端口放行",
          _origin_case("127.0.0.1:8189", "http://127.0.0.1:8189") is True)
    check("同 host 不同端口拒绝（#20 回归）",
          _origin_case("127.0.0.1:8189", "http://127.0.0.1:3000") is False)
    check("localhost 自一致放行",
          _origin_case("localhost:8189", "http://localhost:8189") is True)
    check("主机不一致拒绝（127.0.0.1 vs localhost）",
          _origin_case("127.0.0.1:8189", "http://localhost:8189") is False)
    check("Host 缺端口对齐服务端口",
          _origin_case("127.0.0.1", "http://127.0.0.1:8189") is True)
    check("https 显式同端口放行（scheme 不参与）",
          _origin_case("127.0.0.1:8189", "https://127.0.0.1:8189") is True)
    check("https 缺端口默认 443 拒绝",
          _origin_case("127.0.0.1:8189", "https://127.0.0.1") is False)
    check("Origin 非法端口拒绝",
          _origin_case("127.0.0.1:8189", "http://127.0.0.1:abc") is False)
    check("无 Origin 仅 Host 校验放行",
          _origin_case("127.0.0.1:8189", None) is True)
    check("外部主机仍拒绝",
          _origin_case("127.0.0.1:8189", "http://evil.example:8189") is False)
    check("IPv6 自一致放行",
          _origin_case("[::1]:8189", "http://[::1]:8189") is True)
    check("IPv6 端口不一致拒绝",
          _origin_case("[::1]:8189", "http://[::1]:3000") is False)

    print(f"\n结果：{PASSED}/{PASSED + FAILED} 通过")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
