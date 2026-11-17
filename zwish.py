#!/usr/bin/env python
"""zwish - 许愿式生图：愿望 → 小模型追问 → 精修提示词 → 生成 → 多轮微调。

终端交互:  python zwish.py "我想画……"
Agent 调用: python zwish.py questions|prompt|plan --json ...

润色通道（可由 --model / 网页选择）：
  auto            本地自由模式（abliterated，忠实执行）
  __local_fine__   本地精细润色（qwen3:8b）
  __ds_api__       DeepSeek API（云端，内置提示词）
  __ds_web__       DeepSeek 网页自动化（内置提示词，CDP 零依赖）
  __cloud__:供应商/模型  通用云端（OpenAI/Anthropic 兼容）

精修统一输出中英双字段：prompt_cn（对话展示）+ prompt_en（送进生图）。
"""
import argparse, base64, json, os, random, re, subprocess, sys, time, urllib.request
from types import SimpleNamespace

import zconfig
import zprompts

CFG = zconfig.CFG
HERE = zconfig.HERE
OLLAMA = CFG["ollama"]
ZIMG = os.path.join(HERE, "zimg.py")
FALLBACK = CFG.get("fallback_refiner") or "qwen3:8b"
FINE_MODEL = CFG.get("fine_model") or "qwen3:8b"
HIST = CFG["history_file"]
VISION = CFG["vision_model"]
PROV_FILE = CFG["providers_file"]
CLOUD_FILE = CFG["cloud_file"]
PROVIDERS = []
CLOUD = None
try:
    if os.path.isfile(CLOUD_FILE):
        CLOUD = json.load(open(CLOUD_FILE, encoding="utf-8"))
except Exception:
    CLOUD = None


def http_json(url, payload, timeout=180):
    """本机 Ollama 请求：显式直连，避免系统代理劫持 loopback。"""
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    return json.load(zconfig.urlopen_local(req, timeout=timeout))


def pick_refiner():
    """按关键词扫描本地模型；优先自由模式模型。"""
    kw = (CFG.get("refiner_keyword") or "abliterated").lower()
    explicit = CFG.get("refiner") or "auto"
    if explicit and explicit != "auto":
        return explicit
    try:
        out = subprocess.run(["ollama", "list"], capture_output=True, text=True, timeout=30).stdout
        for line in out.splitlines()[1:]:
            name = line.split()[0]
            if kw and kw in name.lower():
                return name
        names = [l.split()[0] for l in out.splitlines()[1:] if l.strip()]
        return names[0] if names else FALLBACK
    except Exception:
        return FALLBACK


def llm_json(model, system, user, fmt_json=True):
    payload = {
        "model": model, "stream": False, "think": False,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "options": {"temperature": 0.4},
    }
    if fmt_json:
        payload["format"] = "json"
    resp = http_json(f"{OLLAMA}/api/chat", payload)
    return resp.get("message", {}).get("content", "")


def load_providers():
    """providers.json 优先；旧 cloud.json 自动迁移为单供应商。"""
    global PROVIDERS
    try:
        if os.path.isfile(PROV_FILE):
            PROVIDERS = json.load(open(PROV_FILE, encoding="utf-8")).get("providers", [])
        elif os.path.isfile(CLOUD_FILE):
            c = json.load(open(CLOUD_FILE, encoding="utf-8"))
            PROVIDERS = [{"name": "默认云端", "base": c.get("api_base", ""),
                          "format": "openai", "key": c.get("api_key", ""),
                          "models": [c.get("model", "")], "on": True}]
        else:
            PROVIDERS = []
    except Exception as e:
        # 解析失败不再静默返回空列表——至少让用户在服务日志里看到原因
        print(f"[zwish] 警告：providers.json 解析失败（{e}），云端供应商列表不可用，"
              f"请检查 {PROV_FILE} 是否为合法 JSON", file=sys.stderr)
        PROVIDERS = []
    return PROVIDERS


def save_providers(ps):
    global PROVIDERS
    PROVIDERS = ps
    os.makedirs(os.path.dirname(PROV_FILE) or ".", exist_ok=True)
    with open(PROV_FILE, "w", encoding="utf-8") as f:
        json.dump({"providers": ps}, f, ensure_ascii=False, indent=2)


def find_provider(sel):
    """sel 形如 "供应商名/模型名"；找不到就落到第一个启用的供应商。"""
    ps = [p for p in (PROVIDERS or load_providers()) if p.get("on", True)]
    if not ps:
        raise ValueError("没有启用的云端供应商，请到 ⚙️ 设置里添加")
    name, _, mdl = sel.partition("/")
    for p in ps:
        if p.get("name") == name and mdl:
            return p, mdl
    for p in ps:
        if mdl and mdl in p.get("models", []):
            return p, mdl
    return ps[0], (mdl or (ps[0].get("models") or [""])[0])


def llm_cloud(p, model, system, user):
    base = (p.get("base") or "").rstrip("/")
    key = p.get("key", "")
    if (p.get("format") or "openai").lower() == "anthropic":
        req = urllib.request.Request(base + "/v1/messages",
            data=json.dumps({"model": model, "max_tokens": 4096, "system": system,
                             "messages": [{"role": "user", "content": user}]}).encode(),
            headers={"Content-Type": "application/json", "x-api-key": key,
                     "anthropic-version": "2023-06-01"})
        r = json.load(zconfig.urlopen_auto(req, timeout=300))
        return "".join(c.get("text", "") for c in r.get("content", []) if c.get("type") == "text")
    req = urllib.request.Request(base + "/chat/completions",
        data=json.dumps({"model": model, "temperature": 0.4,
                         "messages": [{"role": "system", "content": system},
                                      {"role": "user", "content": user}]}).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
    r = json.load(zconfig.urlopen_auto(req, timeout=300))
    return r["choices"][0]["message"]["content"]


def find_ds_provider():
    """找到内置的 DeepSeek 供应商（按名字/地址识别）。"""
    ps = (PROVIDERS or load_providers())
    for p in ps:
        blob = (p.get("name", "") + " " + p.get("base", "")).lower()
        if "deepseek" in blob:
            return p
    raise ValueError("未找到 DeepSeek 供应商。请到 ⚙️ 设置点「一键预置 DeepSeek」并填入 API Key")


def llm_ds_api(system, user):
    """DeepSeek 官方 API 通道（OpenAI 兼容），使用内置提示词。"""
    p = find_ds_provider()
    if not (p.get("key") or "").strip():
        raise ValueError("DeepSeek 供应商的 API Key 为空，请到 ⚙️ 设置填写")
    mdl = (p.get("models") or ["deepseek-chat"])[0]
    return llm_cloud(p, mdl, system, user), f"DeepSeek/{mdl}"


def llm_ds_web(system, user):
    """DeepSeek 网页自动化通道，使用内置提示词（CDP 控制本机浏览器，零额外依赖）。"""
    import zds
    text = zds.web_chat(system, user)
    return text, "DeepSeek/网页"


def llm_call(model, system, user):
    """统一入口：返回 (文本, 实际模型名)。

    model:
      __ds_api__  → DeepSeek API
      __ds_web__  → DeepSeek 网页自动化
      __local_fine__ → 本地精细润色模型
      __cloud__[:sel]  → 通用云端
      其他/None → 本地自由模式模型
    """
    if model == "__ds_api__":
        return llm_ds_api(system, user)
    if model == "__ds_web__":
        return llm_ds_web(system, user)
    if model == "__local_fine__":
        m = FINE_MODEL
        return llm_json(m, system, user), m
    if model == "__cloud__" or (model and model.startswith("__cloud__:")):
        sel = model.split(":", 1)[1] if ":" in model else ""
        p, mdl = find_provider(sel)
        return llm_cloud(p, mdl, system, user), f"{p.get('name')}/{mdl}"
    m = model or pick_refiner()
    return llm_json(m, system, user), m


def test_provider(p):
    t0 = time.time()
    txt = llm_cloud(p, (p.get("models") or ["hi"])[0], "只回答两个字：成功", "只回答两个字：成功")
    return {"ok": True, "elapsed": round(time.time() - t0, 1), "reply": txt[:30]}


def _parse_json(text):
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError(f"[zwish] 模型输出无法解析: {text[:300]}")
    return json.loads(m.group(0))


def _normalize_prompt(d, wish=None):
    """把中英双字段补全并兜底：老模型只回 prompt 时自动补另一字段。"""
    cn = (d.get("prompt_cn") or d.get("prompt") or "").strip()
    en = (d.get("prompt_en") or "").strip()
    if not en:
        # 老模型/纯中文输出：把中文原样带走，并在 note 里提示（ ComfyUI 的 Qwen 编码器也支持中文）
        en = (d.get("prompt_en") or cn or wish or "").strip()
    if not cn:
        cn = en
    d["prompt_cn"] = cn
    d["prompt"] = cn          # 兼容旧前端
    d["prompt_en"] = en
    d.setdefault("w", 1024)
    d.setdefault("h", 1024)
    return d


def gen_questions(wish, model=None, count=None):
    system, user = zprompts.questions(wish, count)
    text, used = llm_call(model or pick_refiner(), system, user)
    d = _parse_json(text)
    return used, d.get("questions", [])


def gen_prompt(wish, answers, model=None):
    system, user = zprompts.prompt(wish, answers)
    text, used = llm_call(model or pick_refiner(), system, user)
    d = _normalize_prompt(_parse_json(text), wish)
    return used, d


def rewrite_prompt(qa_ctx, current_cn, feedback, model=None, w=None, h=None, current_en=""):
    system, user = zprompts.rewrite(qa_ctx, current_cn, current_en, feedback, w or 1024, h or 1024)
    text, used = llm_call(model or pick_refiner(), system, user)
    d = _normalize_prompt(_parse_json(text), current_cn)
    if w: d["w"] = int(d.get("w", w))
    if h: d["h"] = int(d.get("h", h))
    return used, d


def describe_image(image_path, model=None):
    model = model or VISION
    b64 = base64.b64encode(open(image_path, "rb").read()).decode()
    r = http_json(f"{OLLAMA}/api/chat", {
        "model": model, "stream": False, "think": False,
        "messages": [
            {"role": "system", "content": zprompts.system("describe")},
            {"role": "user", "content": "描述这张图片。", "images": [b64]}],
        "options": {"temperature": 0.3}}, timeout=300)
    return (r.get("message", {}).get("content", "") or "").strip()


def gen_edit_questions(wish, current_cn, history, feedback, image_desc, model=None, count=None):
    system, user = zprompts.edit_questions(wish, current_cn, history, feedback, image_desc, count)
    text, used = llm_call(model or pick_refiner(), system, user)
    d = _parse_json(text)
    return used, d.get("questions", [])


def gen_plan(wish, current_cn, history, feedback, model=None, image_path=None,
             image_desc=None, current_en=""):
    if image_path and not image_desc:
        try:
            image_desc = describe_image(image_path)
        except Exception:
            image_desc = None
    system, user = zprompts.plan(wish, current_cn, current_en, history, feedback, image_desc)
    text, used = llm_call(model or pick_refiner(), system, user)
    d = _parse_json(text)
    if d.get("prompt_cn") or d.get("prompt_en") or d.get("prompt"):
        _normalize_prompt(d, current_cn)
    d["image_desc"] = image_desc or None
    return used, d


# ── CLI ────────────────────────────────────────────────────
def cmd_questions(args):
    model, qs = gen_questions(args.wish, args.model)
    if not qs: sys.exit("[zwish] 未生成问题")
    if args.json:
        print(json.dumps({"refiner": model, "questions": qs}, ensure_ascii=False))
    else:
        print("[zwish] 向导有 3 个问题帮你把愿望变成画面：")
        for i, q in enumerate(qs, 1):
            print(f"  {i}. {q['q']}")
            for j, o in enumerate(q.get("options", []), 1):
                print(f"     {j}. {o}")
            print("     （可直接输入自定义答案，回车跳过）")


def cmd_prompt(args):
    model, d = gen_prompt(args.wish, args.answers, args.model)
    if not d.get("prompt_en"): sys.exit("[zwish] 未生成提示词")
    if args.json:
        print(json.dumps({"refiner": model, **d}, ensure_ascii=False))
    else:
        print(f"[zwish] 精修提示词（{d.get('w',1024)}x{d.get('h',1024)}）：")
        print(f"  中文：{d.get('prompt_cn','')}")
        print(f"  生图(英)：{d.get('prompt_en','')}")


def cmd_plan(args):
    model, d = gen_plan(args.wish, args.current, args.history, args.feedback, args.model)
    if not d.get("action"): sys.exit("[zwish] 未生成计划")
    print(json.dumps({"refiner": model, **d}, ensure_ascii=False))


def run_zimg(cmd_args):
    r = subprocess.run([sys.executable, ZIMG] + cmd_args, capture_output=True, text=True, timeout=600)
    tail = (r.stdout or "").strip().splitlines()
    for line in tail:
        if "完成" in line or "已复制" in line: print("  " + line.strip())
    if r.returncode != 0:
        print((r.stderr or r.stdout or "")[-500:])


def _pick_model_menu():
    print("[zwish] 用哪个模型润色提示词？")
    print("  1. 自由模式（默认）· 本地 abliterated，忠实执行")
    print("  2. 精细润色·本地   · qwen3:8b，润色更细腻")
    print("  3. DS · API         · DeepSeek 云端，内置提示词（需 API Key）")
    print("  4. DS · 网页        · 浏览器自动化，内置提示词（需登录）")
    print("  5. ☁️ 云端供应商    · 设置面板里配置的模型")
    sel = input("  选择（回车/1/2/3/4/5）：").strip()
    return {
        "": None, "1": None, "2": "__local_fine__", "3": "__ds_api__",
        "4": "__ds_web__", "5": "__cloud__",
    }.get(sel, None)


def interactive(args):
    model = args.model if args.model else _pick_model_menu()
    label = {
        None: "本地自由模式", "__local_fine__": "本地精细润色",
        "__ds_api__": "DeepSeek API", "__ds_web__": "DeepSeek 网页", "__cloud__": "云端",
    }.get(model, model)
    print(f"[zwish] 精修模型: {label} | 生成管线: Z-Image Turbo | 生图用英文词\n")
    wish = (args.wish or args.wish_pos or "").strip() or input("许个愿（想画什么）：").strip()
    if not wish: return
    model, qs = gen_questions(wish, model)
    answers = []
    for i, q in enumerate(qs, 1):
        opts = q.get("options", [])
        print(f"\n{i}. {q['q']}")
        for j, o in enumerate(opts, 1): print(f"   {j}. {o}")
        a = input("   你的选择（数字/自定义/回车跳过）：").strip()
        if a.isdigit() and 1 <= int(a) <= len(opts): a = opts[int(a) - 1]
        if a: answers.append(f"{q['q']} → {a}")
    model, d = gen_prompt(wish, json.dumps(answers, ensure_ascii=False), model)
    cn = d.get("prompt_cn", wish)
    en = d.get("prompt_en", cn)
    w, h = int(d.get("w", 1024)), int(d.get("h", 1024))
    qa_ctx = f"愿望：{wish}\n回答：{json.dumps(answers, ensure_ascii=False)}"

    while True:
        print(f"\n[zwish] 精修提示词（{w}x{h}）:")
        print(f"  中文：{cn}")
        print(f"  生图(英)：{en}\n")
        sel = input("[zwish] 回车=开始生成 | 1=交流修改 | 2=直接给出提示词：").strip()
        if sel == "1":
            fb = input("[zwish] 说出你的修改建议：").strip()
            if not fb:
                print("  （未输入建议，返回）"); continue
            model, d2 = rewrite_prompt(qa_ctx, cn, fb, model, w, h, en)
            cn = d2.get("prompt_cn", cn); en = d2.get("prompt_en", en)
            w, h = int(d2.get("w", w)), int(d2.get("h", h))
            print("[zwish] 已根据建议重写提示词，请再确认"); continue
        if sel == "2":
            manual = input("[zwish] 输入完整提示词（英文生图词）：").strip()
            if manual:
                en = manual; print("[zwish] 已更新提示词，请再确认")
            continue
        break  # 回车 → 用当前英文提示词生成

    seed = random.randint(0, 2**31 - 1)
    run_zimg([en, "--w", str(w), "--h", str(h), "--seed", str(seed), "--prefix", "zwish"])
    outdir = CFG["output_dir"]
    last_img = max((os.path.join(outdir, f) for f in os.listdir(outdir) if f.startswith("zwish")),
                   key=os.path.getmtime, default=None) if os.path.isdir(outdir) else None
    if last_img:
        op = input("[zwish] 打开文件夹直接看图？（回车=打开 / s=跳过）：").strip().lower()
        if op != "s":
            try: os.startfile(outdir)
            except Exception as e: print(f"  打开失败({e})，手动去: {outdir}")
    hist = []
    while True:
        fb = input("\n[zwish] 接下来想用它做什么？（回车结束 / 描述要修改的地方）：").strip()
        if not fb: break
        model, plan = gen_plan(wish, cn, json.dumps(hist, ensure_ascii=False), fb, model,
                               image_path=last_img, current_en=en)
        if plan.get("image_desc"):
            print(f"[zwish] 👀 看图：{plan['image_desc']}")
        act = plan.get("action", "regenerate")
        print(f"[zwish] 计划: {act} | {plan.get('note','')}")
        if act == "done": break
        cn = plan.get("prompt_cn", cn); en = plan.get("prompt_en", en)
        base = ["--img", last_img] if (last_img and act in ("img2img", "inpaint")) else []
        if act == "inpaint": base += ["--segment", plan.get("segment", "")]
        cargs = [en] + base + ["--denoise", str(plan.get("denoise", 1.0)),
                               "--seed", str(random.randint(0, 2**31 - 1)), "--prefix", "zwish"]
        if act == "regenerate":
            cargs = [en, "--w", str(w), "--h", str(h), "--seed", str(random.randint(0, 2**31-1)), "--prefix", "zwish"]
        run_zimg(cargs)
        last_img = max((os.path.join(outdir, f) for f in os.listdir(outdir) if f.startswith("zwish")),
                       key=os.path.getmtime, default=last_img) if os.path.isdir(outdir) else last_img
        hist.append({"feedback": fb, "action": act})
    print(f"[zwish] 完成，图片在 {outdir}/")


HELP_CN = """
zwish 使用速查（许愿式生图向导）
================================

最简单的用法 —— 直接许愿，向导自动开工：

  zwish 我想画一只在月球上钓鱼的橘猫
      → 1. 选润色模型：自由模式 / 精细润色 / DS API / DS 网页 / 云端
      → 2. 向导问你 3 个问题（选选项即可）
      → 3. 合成中英双字段：中文说明给你看，英文词送进生图
      → 4. 三选一：回车生成 / 1 交流修改 / 2 手写英文词
      → 5. 打开看图，可无限轮微调；直接回车结束

常用子命令（给脚本/agent 用的 JSON 模式）：

  zwish questions "愿望" --json               生成 3 个追问
  zwish prompt "愿望" --answers "答案"         合成精修提示词（含 prompt_en）
  zwish plan "修改意见" --current "当前提示词"  判断微调方式

润色通道（--model 指定）：
  （不填）         本地自由模式，忠实执行
  __local_fine__ 本地精细润色 qwen3:8b
  __ds_api__       DeepSeek API（内置提示词，需在设置里配 Key）
  __ds_web__       DeepSeek 网页自动化（内置提示词，CDP 控制本机浏览器 + 登录，零额外依赖）
  __cloud__        设置面板里的云端供应商

说明：
  * 真正生图用 prompt_en（英文）；对话区展示 prompt_cn（中文）
  * 看图/点评永远走本地视觉模型，图片不出本机
  * 生成由 zimg 完成；路径配置见 config.json（参照 config.example.json）
"""


def main():
    argv = sys.argv[1:]

    if argv and argv[0] in ("help", "--help-cn"):
        print(HELP_CN); return

    if argv and argv[0] in ("questions", "prompt", "plan"):
        p = argparse.ArgumentParser(description="许愿式生图向导")
        def add_common(sp):
            sp.add_argument("wish"); sp.add_argument("--model"); return sp
        sub = p.add_subparsers(dest="cmd", required=True)
        q = add_common(sub.add_parser("questions")); q.add_argument("--json", action="store_true")
        pr = add_common(sub.add_parser("prompt")); pr.add_argument("--answers"); pr.add_argument("--json", action="store_true")
        pl = add_common(sub.add_parser("plan")); pl.add_argument("feedback"); pl.add_argument("--wish"); pl.add_argument("--current"); pl.add_argument("--history")
        args = p.parse_args(argv)
        if args.cmd == "questions": return cmd_questions(args)
        if args.cmd == "prompt": return cmd_prompt(args)
        if args.cmd == "plan": return cmd_plan(args)

    model = None
    explicit_wish = None
    wish_parts = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--model" and i + 1 < len(argv):
            model = argv[i + 1]; i += 2
        elif a == "--wish" and i + 1 < len(argv):
            explicit_wish = argv[i + 1]; i += 2
        else:
            wish_parts.append(a); i += 1
    args = SimpleNamespace(model=model, wish=explicit_wish,
                           wish_pos=" ".join(wish_parts) if wish_parts else None)
    return interactive(args)


if __name__ == "__main__":
    main()
