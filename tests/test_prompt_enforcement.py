#!/usr/bin/env python3
"""回归测试：英文提示词契约（enforce_english_prompt）。

背景（真机实测 2026-11-18）：元宝通道对"改风格"类反馈会用中文长句充当
prompt_en（如"基于输入图进行风格重绘：保留猫娘少女…"），或对修改请求只回
action=regenerate 不给新提示词——系统随后拿旧词/中文词直接重画。

覆盖：
  1. prompt_en 含中文 → 自动带纠错指令重试一次，纠正后成功；
  2. 重试仍含中文 → 明确抛 ValueError（绝不静默用中文生图）；
  3. action=regenerate 且不带新提示词 → 合法重掷，不误伤、不重试、
     且不得把中文 prompt_cn 兜底成 prompt_en；
  4. enforce_english_prompt=false → 恢复旧的中文直通行为；
  5. zyuanbao 通道契约注入（纯文本声明 + 禁 regenerate + 英文要求）；
  6. zprompts.plan 模板包含 regenerate 限制与"必须纯英文"。

运行： python tests/test_prompt_enforcement.py
"""
import inspect
import json
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


import zwish  # noqa: E402
import zprompts  # noqa: E402
import zyuanbao  # noqa: E402
import zconfig  # noqa: E402


def _j(obj):
    return json.dumps(obj, ensure_ascii=False)


def main():
    print("[1] 单元：_has_cjk / _en_violation")
    check("中文判定", zwish._has_cjk("基于输入图进行风格重绘"))
    check("英文判定为否", not zwish._has_cjk("a hand-drawn line art catgirl, soft lines"))
    check("缺失判定", zwish._en_violation({"prompt_en": ""}) == "prompt_en 缺失")
    check("含中文判定", zwish._en_violation({"prompt_en": "中文"}) == "prompt_en 含中文")
    check("合规判定", zwish._en_violation({"prompt_en": "an english prompt"}) is None)

    print("[2] gen_plan：中文 prompt_en → 重试纠正")
    calls = []

    def ok_second(model, system, user):
        calls.append(user)
        if len(calls) == 1:
            # 首次：复刻真机违规输出（prompt_en 为中文长句）
            return _j({"action": "img2img", "segment": "catgirl",
                       "prompt_cn": "改为手绘线稿风",
                       "prompt_en": "基于输入图进行风格重绘：保留猫娘少女侧身回眸的姿态，改为手绘线稿",
                       "denoise": 0.65, "note": "仅调整画风"}), "yuanbao"
        # 纠错重试：返回纯英文
        assert "【纠错】" in user and "prompt_en" in user, "重试必须携带纠错指令"
        return _j({"action": "img2img", "prompt_cn": "改为手绘线稿风",
                   "prompt_en": "a soft hand-drawn line art catgirl in pale pink kimono, "
                                "delicate rounded strokes, gentle expression, moonlit window, "
                                "minimal light gray tones with soft pink accents",
                   "denoise": 0.65, "note": "仅调整画风"}), "yuanbao"

    orig = zwish.llm_call
    zwish.llm_call = ok_second
    try:
        used, d = zwish.gen_plan("一只猫娘在窗边", "清冷猫娘", "[]", "改成手绘线稿风",
                                 model="__yuanbao_web__",
                                 image_desc="一位猫娘少女侧身回眸")
        check("违规触发一次纠错重试", len(calls) == 2, f"calls={len(calls)}")
        check("重试后 prompt_en 为纯英文",
              d.get("prompt_en", "").startswith("a soft hand-drawn") and
              not zwish._has_cjk(d.get("prompt_en")))
        check("不再保留中文 prompt_en", "基于输入图" not in (d.get("prompt_en") or ""))
    finally:
        zwish.llm_call = orig

    print("[3] gen_plan：重试仍违规 → 明确报错")
    calls2 = []

    def always_chinese(model, system, user):
        calls2.append(user)
        return _j({"action": "img2img", "prompt_cn": "改",
                   "prompt_en": "基于输入图进行风格重绘", "denoise": 0.6}), "yuanbao"

    zwish.llm_call = always_chinese
    try:
        raised = None
        try:
            zwish.gen_plan("愿望", "当前", "[]", "改风格", model="__yuanbao_web__")
        except ValueError as e:
            raised = str(e)
        check("两次违规后抛 ValueError", raised is not None and "prompt_en" in raised,
              (raised or "")[:80])
        check("恰好调用两次（1 次原始 + 1 次纠错）", len(calls2) == 2, f"calls={len(calls2)}")
    finally:
        zwish.llm_call = orig

    print("[4] gen_plan：合法 regenerate 重掷不误伤")
    calls3 = []

    def reroll(model, system, user):
        calls3.append(user)
        return _j({"action": "regenerate", "prompt_cn": "保持原画面换个构图感觉",
                   "note": "换个结果再试试"}), "yuanbao"

    zwish.llm_call = reroll
    try:
        used, d = zwish.gen_plan("愿望", "当前", "[]", "换一张试试", model="__yuanbao_web__")
        check("regenerate 不触发重试", len(calls3) == 1, f"calls={len(calls3)}")
        check("regenerate 不把中文兜底成 prompt_en", d.get("prompt_en") == "")
    finally:
        zwish.llm_call = orig

    print("[5] enforce_english_prompt=false 恢复中文直通")
    zwish.CFG["enforce_english_prompt"] = False
    try:
        calls4 = []

        def chinese_once(model, system, user):
            calls4.append(user)
            return _j({"action": "img2img", "prompt_cn": "改",
                       "prompt_en": "基于输入图进行风格重绘"}), "yuanbao"

        zwish.llm_call = chinese_once
        used, d = zwish.gen_plan("愿望", "当前", "[]", "改风格", model="__yuanbao_web__")
        check("关闭后不重试", len(calls4) == 1)
        check("关闭后中文直通（旧行为）", d.get("prompt_en") == "基于输入图进行风格重绘")
    finally:
        zwish.llm_call = orig
        zwish.CFG["enforce_english_prompt"] = True

    print("[6] gen_prompt / rewrite_prompt 同样受守卫")
    def prompt_flow(model, system, user):
        if "【纠错】" in user:
            return _j({"prompt_cn": "一只猫", "prompt_en": "a cute cat on a windowsill",
                       "w": 1024, "h": 768}), "yuanbao"
        return _j({"prompt_cn": "一只猫", "prompt_en": "一只可爱的猫坐在窗台上",
                   "w": 1024, "h": 768}), "yuanbao"

    zwish.llm_call = prompt_flow
    try:
        used, d = zwish.gen_prompt("一只猫", "无", model="__yuanbao_web__")
        check("gen_prompt 中文被纠正为英文", d.get("prompt_en") == "a cute cat on a windowsill")
        used, d = zwish.rewrite_prompt("qa", "当前", "改可爱一点", model="__yuanbao_web__")
        check("rewrite_prompt 中文被纠正为英文",
              d.get("prompt_en") == "a cute cat on a windowsill")
    finally:
        zwish.llm_call = orig

    print("[7] zyuanbao 通道契约注入")
    src = inspect.getsource(zyuanbao)
    check("声明纯文本通道（禁止承诺生图）", "不能生成、重绘" in zyuanbao._CONTRACT)
    check("契约含英文强制", "English only" in zyuanbao._CONTRACT)
    check("契约含 regenerate 限制", "禁止\n    \" action=regenerate" in zyuanbao._CONTRACT or
          "禁止 action=regenerate" in zyuanbao._CONTRACT.replace("\n", " ").replace('"\n    "', ' '))
    check("消息构建注入契约", "_CONTRACT + \"\\n\" + system" in src)
    check("消息末尾重复强调", "_CONTRACT_REMINDER" in src)
    cap = {}
    real_wrap = zprompts.ds_wrap
    zprompts.ds_wrap = lambda s, u: cap.update(system=s, user=u) or real_wrap(s, u)
    try:
        # 直接检查 web_chat 实现链的源码（契约拼接在 _web_chat_locked 里，避免真开浏览器）
        wb = inspect.getsource(zyuanbao.web_chat) + inspect.getsource(zyuanbao._web_chat_locked)
        check("web_chat 前置契约", "_CONTRACT + \"\\n\" + system" in wb)
        check("web_chat 末尾强调", "user + \"\\n\\n\" + _CONTRACT_REMINDER" in wb)
    finally:
        zprompts.ds_wrap = real_wrap

    print("[8] zprompts.plan 模板加固")
    plan_text = zprompts.system("plan") if hasattr(zprompts, "system") else ""
    plan_default = zprompts.DEFAULT_SYSTEMS["plan"]
    check("模板含 regenerate 限制", "禁止 regenerate" in plan_default)
    check("模板要求 prompt_en 纯英文", "必须纯英文" in plan_default)
    check("img2img 不带 segment", "不要输出 segment" in plan_default)

    print(f"\n结果：{PASSED}/{PASSED + FAILED} 通过")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
