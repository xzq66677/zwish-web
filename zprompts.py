"""zprompts - 全部内置提示词模板，集中管理、可被用户覆盖。

为什么独立成模块：
  1. API 路（DeepSeek / OpenAI 兼容）与网页路（Playwright 自动化 deepseek.com）
     必须共用**同一套内置提示词**，行为才能一致；
  2. 云端/网页通道要求内置提示词——这些模板固定注入，不随用户对话漂移；
  3. 中英双字段：中文说明给对话区展示，english_prompt 才是真正送进 ComfyUI 的生图提示词。

用户自定义（重要）：
  用户在网页「⚙️ 设置 → 内置提示词」里编辑后，写入 prompts.json（已 gitignore）。
  本模块每次生成 system 时都会读取该文件：填了自定义文本就用自定义的，留空则用下方默认值。
  提供 prompts.example.json 作为模板；点「恢复默认」即清空对应自定义项。
  这样 DS · 网页 / DS · API / ☁️ 云端 三条通道共用同一套「当前生效提示词」。

所有函数返回 (system, user) 二元组，调用方直接交给 llm_call()。
"""
import json
import os

import zconfig

HERE = zconfig.HERE
PROMPTS_FILE = os.path.join(HERE, "prompts.json")

# ── 通用片段 ──────────────────────────────────────────────
JSON_ONLY = "只输出 JSON，不要输出任何解释、不要用 ``` 包裹。"
EN_RULE = (
    "同时给出中英双字段："
    '"prompt_cn" 是给用户看的中文说明（友好、口语、60字以内）；'
    '"prompt_en" 是真正送进生图模型的英文提示词，'
    "用 subject + action + scene + composition + lighting + style + details + quality 的结构，"
    "具体、有画面感、英文原生表达，60-120 词。"
)


def _j(obj_hint):
    return f"{JSON_ONLY}格式：{obj_hint}"


# ── 默认 system 模板（key -> 完整 system 文本）────────────
# 这些是「恢复默认」时回退的文本。用户可整段覆盖。
DEFAULT_SYSTEMS = {
    "questions": (
        "你是专业生图向导。用户许了一个愿望。请提出{N}个对最终画面效果影响最大的问题"
        "（风格、构图、氛围、关键细节），每题给2-4个简短选项。问题要具体、不重复、不问废话。"
        "问题数量以{N}为准，不多不少。"
        + _j('{"questions":[{"q":"问题","options":["选项1","选项2"]}]}')
    ),
    "prompt": (
        "你是生图提示词工程师。根据用户愿望和问答结果，输出一条最终提示词。\n" + EN_RULE +
        "结构：主体+动作+场景+构图+光线+风格+质量词。"
        + _j('{"prompt_cn":"中文说明","prompt_en":"English prompt","w":宽,"h":高}')
        + "宽高从16的倍数里选，默认1024x1024，竖图768x1024，横图1024x768。"
    ),
    "rewrite": (
        "你是生图提示词工程师。用户对当前提示词提出修改建议。请认真吸收建议，"
        "重新输出完整的最终提示词（保持主体+场景+构图+光线+风格+质量词的完整结构，"
        "不要只输出修改片段），特别突出用户强调的部分。\n" + EN_RULE
        + _j('{"prompt_cn":"中文说明","prompt_en":"English prompt","w":宽,"h":高}')
    ),
    "edit_questions": (
        "你是微调向导。用户要对生成图做修改。结合图片实际内容与用户反馈，提出{N}个最能明确修改效果的问题"
        "（例如：新主体的性别/年龄/着装？动作姿态保持还是改变？风格保持还是变化？）每题2-3个简短选项。"
        "问题数量以{N}为准，不多不少。"
        "注意：答案已经能从反馈里直接看出的信息不要再问（例如用户说了'男子'就不要再问性别）；"
        "也不要问与这次修改无关的问题。"
        + _j('{"questions":[{"q":"问题","options":["选项1","选项2"]}]}')
    ),
    "plan": (
        "你是生图微调规划器。用户对当前图片提出了修改意见。判断修改类型并输出执行计划："
        "- 换/增删画面中的具体物体或人物动作 → action=inpaint，segment=画面里要改的目标词(英文名词，如 dog)"
        "- 改整体风格/色调/光线 → action=img2img"
        "- 只是想换个结果再试试 → action=regenerate"
        "- 用户表示满意 → action=done"
        "重要：先看【图片实际内容】确认画面主体是谁/是什么。用户的动作要求（如飞起来、跳起来）"
        "必须作用于实际存在的主体本身，绝不要引入画面里不存在的新主体"
        "（例如画面里是狗，就不能凭空变成鸟）。"
        "新提示词同样给出中英双字段（prompt_cn 中文说明，prompt_en 英文生图词）。\n" + EN_RULE
        + _j('{"action":"...","segment":"...","prompt_cn":"中文说明","prompt_en":"English prompt",'
             '"denoise":0.8,"note":"一句中文说明"}')
        + "denoise(0.3-1.0)：换物体用1.0，改颜色材质0.6-0.8。"
    ),
    "describe": (
        "你是看图助手。用不超过两句中文客观描述图片：主体是谁/是什么、正在做什么、"
        "场景与风格。不评价好坏，不给建议。"
    ),
    "critique": (
        "你是专业画面评审。对比提示词与生成图，从构图、光影、色彩、细节、与提示词的匹配度评价。"
        '输出JSON：{"overall":"一句话总评","suggestions":["改进建议1（具体可执行，可包含参数建议如denoise降到0.7）",'
        '"建议2","建议3"]}。中文，建议要具体。'
    ),
}

# 前端编辑器展示用的中文名与说明（与 key 对应）
LABELS = {
    "questions": ("追问向导", "许愿后的问题，决定追问风格与选项。数量由前端传入（{N} 为占位符，会替换为自选值）；本地模型推荐较少。"),
    "prompt": ("精修提示词", "把愿望与回答合成为最终提示词（中英双字段）。"),
    "rewrite": ("交流重写", "根据用户的修改建议重写完整提示词。"),
    "edit_questions": ("改图追问", "对已有图片微调时的问题，数量可自选（{N} 为占位符）。本地模型上下文有限，推荐较少。"),
    "plan": ("微调规划", "判断改图类型并输出执行计划与新提示词。"),
    "describe": ("看图描述", "本地视觉模型客观描述图片内容（不上云）。"),
    "critique": ("看图点评", "本地视觉模型对比提示词点评生成图（不上云）。"),
}

# DS 网页把 system 拼进消息时用的两段包裹语，也开放自定义
DS_WRAP = {
    "ds_header": "【系统指令，请严格遵守】",
    "ds_user_header": "【用户任务】",
}


# ── 用户覆盖：prompts.json ────────────────────────────────
def load_overrides():
    """读取用户自定义提示词；文件不存在或格式错误则返回空字典。"""
    try:
        with open(PROMPTS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_overrides(data):
    """保存用户自定义提示词（只写入字符串值，空串视为恢复默认）。"""
    clean = {k: str(v) for k, v in (data or {}).items()
             if isinstance(v, str) and v.strip()}
    with open(PROMPTS_FILE, "w", encoding="utf-8") as f:
        json.dump(clean, f, ensure_ascii=False, indent=2)
    return clean


def system(key):
    """取当前生效的 system 文本：用户自定义优先，否则用默认。"""
    if not isinstance(key, str):
        return ""
    ov = load_overrides()
    if ov.get(key, "").strip():
        return ov[key]
    if key in DEFAULT_SYSTEMS:
        return DEFAULT_SYSTEMS[key]
    return DS_WRAP.get(key, "")


def effective():
    """返回 {key: 当前生效文本}，供前端展示与导出。"""
    keys = list(DEFAULT_SYSTEMS) + list(DS_WRAP)
    return {k: system(k) for k in keys}


def state():
    """返回编辑器需要的完整状态：默认值、当前生效值、用户已改的 key、标签说明。"""
    ov = load_overrides()
    custom_keys = [k for k, v in ov.items() if isinstance(v, str) and v.strip()]
    return {
        "defaults": {**DEFAULT_SYSTEMS, **DS_WRAP},
        "effective": effective(),
        "custom": custom_keys,
        "labels": {k: {"name": v[0], "desc": v[1]} for k, v in LABELS.items()},
        "labels_extra": {
            "ds_header": {"name": "DS 网页·指令包裹语", "desc": "DS 网页通道把内置提示词拼进消息时的开头标记。"},
            "ds_user_header": {"name": "DS 网页·任务包裹语", "desc": "DS 网页通道拼接用户输入时的开头标记。"},
        },
        "file": os.path.basename(PROMPTS_FILE),
    }


# ── 问题数注入 ────────────────────────────────────────────
def _apply_count(system_text, count):
    """把自选问题数注入 system 模板。

    - 模板含 {N}（默认模板）→ 直接替换为实际数字；
    - 用户自定义模板没有 {N} → 在末尾追加一条硬性数量约束，仍生效。
    """
    try:
        n = int(count)
    except (TypeError, ValueError):
        n = 2
    if n < 1:
        n = 1
    if "{N}" in system_text:
        return system_text.replace("{N}", str(n))
    return system_text + f"\n注意：本次请恰好提出 {n} 个问题，不多不少。"


# ── 对外生成函数：system 走 system(key)，user 部分代码生成 ──
def questions(wish, count=None):
    sys_text = _apply_count(system("questions"), count if count is not None else 3)
    return sys_text, f"愿望：{wish}"


def prompt(wish, answers):
    return system("prompt"), (f"愿望：{wish}\n用户回答：\n{answers or '（无，按你的最佳判断补全画面）'}")


def rewrite(qa_ctx, current_cn, current_en, feedback, w, h):
    user = (f"{qa_ctx}\n当前中文提示词：{current_cn}\n当前英文提示词：{current_en}\n"
            f"用户修改建议：{feedback}\n保持宽高约 {w}x{h}")
    return system("rewrite"), user


def edit_questions(wish, current_cn, history, feedback, image_desc, count=None):
    user = (f"图片实际内容：{image_desc or '（未知）'}\n当前提示词：{current_cn or '无'}\n"
            f"历史操作：{history or '无'}\n用户反馈：{feedback}\n原始愿望：{wish or '（无）'}")
    sys_text = _apply_count(system("edit_questions"), count if count is not None else 2)
    return sys_text, user


def plan(wish, current_cn, current_en, history, feedback, image_desc):
    extra = f"\n图片实际内容（vision）：{image_desc}" if image_desc else ""
    user = (f"原始愿望：{wish}\n当前中文提示词：{current_cn or '无'}\n"
            f"当前英文提示词：{current_en or '无'}\n历史操作：{history or '无'}"
            f"\n用户反馈：{feedback}{extra}")
    return system("plan"), user


# 供 server.py import 的两个本地视觉常量（改成函数，取当前生效值）
DESCRIBE_SYSTEM = DEFAULT_SYSTEMS["describe"]
CRITIQUE_SYSTEM = DEFAULT_SYSTEMS["critique"]


def ds_wrap(system_text, user_text):
    """DS 网页通道：把 system 与 user 拼成一条消息，包裹语可被用户自定义。"""
    return (f"{system('ds_header')}\n{system_text}\n\n"
            f"{system('ds_user_header')}\n{user_text}")
