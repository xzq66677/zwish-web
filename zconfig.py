"""zconfig - zwish-web 唯一的路径/模型配置来源（脱敏版）。

优先级：环境变量 ZWISH_CONFIG 指定的文件 > 本目录 config.json > 自动探测。
仓库只包含 config.example.json；你自己的 config.json 已被 .gitignore 排除。

本模块导入时**不会**因找不到 ComfyUI 目录而退出，只会给出警告并置 ready=False，
方便在没有 ComfyUI 的机器上做语法检查 / 单元测试。真正调用生图时才需要配置好。
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

_DEFAULTS = {
    # ── 路径 ──────────────────────────────────────────────
    "base_dir": "",                     # 含 ComfyUI_windows_portable 的那一层目录
    "port": 8189,                       # zwish-web 服务端口
    "comfy_port": 8188,                 # ComfyUI 服务端口
    "data_dir": "",                     # 留空 = 本目录/data
    "static_dir": "",                   # 留空 = 本目录/static
    "uploads_dir": "",                  # 留空 = 本目录/uploads
    "providers_file": "",               # 留空 = 本目录/providers.json
    "cloud_file": "",                   # 留空 = 本目录/cloud.json（旧版兼容）
    "history_file": "",                 # 留空 = 本目录/history.jsonl
    # ── 本地模型 ──────────────────────────────────────────
    "ollama": "http://127.0.0.1:11434",
    "refiner": "auto",                  # auto=按 keyword 扫描，或直接填模型名
    "refiner_keyword": "abliterated",   # auto 模式下扫描 ollama list 的关键词（自由模式模型）
    "fallback_refiner": "qwen3:8b",
    "fine_model": "qwen3:8b",           # 精细润色·本地模型
    "vision_model": "gemma4-cc",        # 看图点评用的多模态模型
    "models": {
        "unet": "z_image_turbo_int8_convrot.safetensors",
        "clip": "qwen_3_4b_fp8_mixed.safetensors",
        "vae": "ae.safetensors",
        "sam_ckpt": "sam3.1_multiplex_fp16.safetensors",
    },
    # ── 追问问题数（许愿追问 / 修图追问）──────────────────────
    # 本地模型上下文窗口有限，推荐值设小；云端上下文大，可多问。
    "ask_count": {
        "wish": 3,             # 许愿追问默认数量
        "edit": 2,             # 修图追问默认数量
        "min": 1,              # 允许自选的下限
        "max": 8,              # 允许自选的上限
        "local_recommend": 2,  # 本地模型（自由模式/精细润色）推荐次数
        "cloud_recommend": 4,  # 云端 / DS 通道推荐次数
    },
    # ── DeepSeek 网页自动化（DS · 网页）──────────────────────
    "ds_web_browser": "auto",           # auto=本机 Chrome/Edge 优先；可填 chrome/msedge/chromium/自定义exe路径
    "ds_web_chat_url": "https://chat.deepseek.com/",
    "ds_web_headless": False,           # 首次登录需要 False（看得见浏览器）
    "ds_web_selectors": {               # 网页改版时只需在这里调整，不用改代码
        "composer": ["textarea#chat-input", "textarea[placeholder]",
                     '[contenteditable="true"]', "textarea"],
        "answer": [".ds-markdown", '[class*="assistant"] [class*="markdown"]',
                   '[class*="message"] pre', '[class*="assistant"]'],
        "generating": ['button:has-text("停止")', '[class*="stop"]',
                       'button[aria-label*="停止"]'],
    },
}

# 内置 DeepSeek API 预置（settings 里「一键预置」写入 providers.json）
DS_PRESET = {
    "name": "DeepSeek",
    "base": "https://api.deepseek.com",
    "format": "openai",
    "key": "",
    "models": ["deepseek-chat", "deepseek-reasoner"],
    "on": True,
}


def _find_base():
    cands = [
        os.environ.get("ZWISH_BASE", ""),
        os.path.join(HERE, "ComfyUI_windows_portable"),
        os.path.join(HERE, "..", "ComfyUI_windows_portable"),
        r"D:\ComfyUI\ComfyUI_windows_portable",
        r"C:\ComfyUI\ComfyUI_windows_portable",
        r"E:\ComfyUI\ComfyUI_windows_portable",
    ]
    for c in cands:
        if c and os.path.isdir(c):
            return os.path.dirname(os.path.abspath(c))
    return ""


def load():
    cfg = dict(_DEFAULTS)
    cands = [os.environ.get("ZWISH_CONFIG"), os.path.join(HERE, "config.json")]
    for c in cands:
        if c and os.path.isfile(c):
            try:
                user = json.load(open(c, encoding="utf-8"))
            except Exception as e:
                print(f"[zconfig] 解析 {c} 失败：{e}，使用默认配置", file=sys.stderr)
                break
            # 顶层键覆盖；嵌套 dict（models / ds_web_selectors）逐键合并
            for k, v in user.items():
                if v is None:
                    continue
                if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                    cfg[k].update(v)
                else:
                    cfg[k] = v
            break

    root = cfg.get("base_dir") or _find_base()
    cfg["base_dir"] = root
    cfg["ready"] = bool(root and os.path.isdir(root))
    if not cfg["ready"]:
        print("[zconfig] 警告：未找到 ComfyUI 目录，生图功能不可用。"
              "请把 config.example.json 复制为 config.json 并填写 base_dir", file=sys.stderr)
        root = root or HERE

    # 目录 / 文件默认路径
    def d(key, val):
        if not cfg.get(key):
            cfg[key] = val

    d("data_dir", os.path.join(HERE, "data"))
    d("static_dir", os.path.join(HERE, "static"))
    d("uploads_dir", os.path.join(HERE, "uploads"))
    d("providers_file", os.path.join(HERE, "providers.json"))
    d("cloud_file", os.path.join(HERE, "cloud.json"))
    d("history_file", os.path.join(HERE, "history.jsonl"))
    cfg.setdefault("output_dir", os.path.join(root, "ComfyUI_windows_portable", "ComfyUI", "output"))
    cfg.setdefault("input_dir", os.path.join(root, "ComfyUI_windows_portable", "ComfyUI", "input"))
    cfg.setdefault("comfyui_main", os.path.join(root, "ComfyUI_windows_portable", "ComfyUI", "main.py"))
    cfg.setdefault("comfyui_python", os.path.join(root, "ComfyUI_windows_portable", "python_embeded", "python.exe"))
    cfg.setdefault("ds_profile_dir", os.path.join(cfg["data_dir"], "ds_profile"))

    for key in ("data_dir", "uploads_dir"):
        try:
            os.makedirs(cfg[key], exist_ok=True)
        except OSError:
            pass
    return cfg


CFG = load()


def ask_recommend(model):
    """按当前润色通道返回推荐的问题数。

    model 为 None / 本地模型 → 本地推荐值（上下文有限，问少些）；
    model 为 __ds_api__ / __ds_web__ / __cloud__:... → 云端推荐值。
    """
    ac = CFG.get("ask_count", {}) or {}
    is_cloud = isinstance(model, str) and (
        model.startswith("__cloud__") or model.startswith("__ds_")
    )
    return int(ac.get("cloud_recommend" if is_cloud else "local_recommend", 2))


def clamp_ask_count(n, default=None):
    """把自选次数夹到 [min, max] 区间；非数字时回退 default。"""
    ac = CFG.get("ask_count", {}) or {}
    lo, hi = int(ac.get("min", 1)), int(ac.get("max", 8))
    try:
        n = int(n)
    except (TypeError, ValueError):
        n = int(default if default is not None else ac.get("edit", 2))
    return max(lo, min(hi, n))
