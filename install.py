#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""install.py - 懒人一键安装向导（zwish-web 配套）

一条命令跑起来，先告诉你「要装什么、装到哪了、速度多少、还剩多久」：
    python install.py

设计原则：
  * 只用 Python 标准库，无需先装任何东西。
  * 先「体检 + 列清单」，再逐项执行；每步都有进度条、实时速度、剩余时间。
  * 大文件支持断点续传（Ctrl+C 中断后再跑会接着下）。
  * ComfyUI/Ollama 这类系统级软件：自动检测，缺了给官方链接和指引，
    不擅自安装到你的系统（需要管理员权限/解压缩/改 PATH，交给你确认更稳妥）。
  * DS 网页通道零额外依赖（CDP + Python 标准库）；模型文件（HF）可自动下载，支持 --only 挑选。

常用参数：
    python install.py                 # 交互模式：先列清单，逐项问你装不装
    python install.py --check         # 只体检，不下载不安装
    python install.py --yes           # 全自动（自动项全装，不再逐项询问）
    python install.py --only models   # 只装模型（可选 python/browser/ollama/comfy/models/all）
    python install.py --mirror        # 模型走国内 HF 镜像（hf-mirror.com）
    python install.py --skip-download # 只生成配置模板和目录，不下载大文件

说明：本脚本不上传任何信息；下载直接连官方源（或 --mirror 镜像）。
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_EXAMPLE = os.path.join(HERE, "config.example.json")
CONFIG_FILE = os.path.join(HERE, "config.json")

RESET = "\033[0m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
CYAN = "\033[36m"
BOLD = "\033[1m"

# Windows 终端默认可能不支持 ANSI，探测后决定用不用颜色
USE_COLOR = sys.stdout.isatty() and os.name != "nt"


def c(text, *codes):
    if USE_COLOR and codes:
        return "".join(codes) + str(text) + RESET
    return str(text)


def hr(title=""):
    line = "─" * 60
    if title:
        print(c(f"\n{line}\n {title}\n{line}", BOLD))
    else:
        print(c(line, CYAN))


def human(n):
    """字节数转人类可读。"""
    try:
        n = float(n)
    except (TypeError, ValueError):
        return str(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024


def ask(prompt, default_yes=True):
    """简单的是/否询问。"""
    try:
        ans = input(c(f"{prompt} [{'Y/n' if default_yes else 'y/N'}]: ", CYAN)).strip().lower()
    except EOFError:
        ans = ""
    if not ans:
        return default_yes
    return ans in ("y", "yes", "是", "1")


# ── HTTP 下载（带进度、速度、剩余时间、断点续传）───────────────
def download(url, dest, mirror=None, label=None):
    """下载单个文件到 dest，返回 (成功bool, 说明)。支持断点续传。"""
    label = label or os.path.basename(dest)
    if mirror and "huggingface.co" in url:
        # 国内镜像：把域名替换掉，路径保持一致
        url = url.replace("https://huggingface.co", mirror.rstrip("/"))

    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    existing = os.path.getsize(dest) if os.path.isfile(dest) else 0

    req = urllib.request.Request(url)
    headers = {"User-Agent": "Mozilla/5.0 zwish-installer"}
    if existing:
        headers["Range"] = f"bytes={existing}-"
    for k, v in headers.items():
        req.add_header(k, v)

    try:
        resp = urllib.request.urlopen(req, timeout=60)
    except urllib.error.HTTPError as e:
        if e.code == 416 and existing:  # Range 不满足，文件可能已完整
            return True, "已完整存在，跳过"
        return False, f"HTTP {e.code} {e.reason}"
    except Exception as e:
        return False, f"连接失败：{e}"

    # 处理响应：206 表示续传，200 表示从头下（忽略已有部分）
    total = None
    mode = "ab" if resp.status == 206 else "wb"
    if mode == "wb":
        existing = 0
    cr = resp.headers.get("Content-Range")
    if cr and "/" in cr:
        try:
            total = int(cr.split("/")[-1])
        except ValueError:
            pass
    if total is None:
        cl = resp.headers.get("Content-Length")
        if cl:
            total = (existing + int(cl)) if mode == "ab" else int(cl)

    done = existing
    t0 = time.time()
    last_print = 0
    try:
        with open(dest, mode) as f:
            while True:
                chunk = resp.read(1024 * 1024)  # 1MB 块
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                now = time.time()
                if now - last_print >= 0.3 or (total and done >= total):
                    last_print = now
                    elapsed = max(now - t0, 0.001)
                    speed = (done - existing) / elapsed
                    pct = (done / total * 100) if total else 0
                    bar_len = 24
                    filled = int(bar_len * done / total) if total else 0
                    bar = "█" * filled + "░" * (bar_len - filled)
                    eta = ""
                    if total and speed > 0:
                        rem = max(total - done, 0) / speed
                        eta = f"  剩余 {fmt_eta(rem)}"
                    sys.stdout.write(
                        f"\r  {c(bar, GREEN)} {pct:5.1f}%  "
                        f"{human(done)}/{human(total) if total else '?'}  "
                        f"{human(speed)}/s{eta}   "
                    )
                    sys.stdout.flush()
        resp.close()
    except KeyboardInterrupt:
        print(f"\n{c('已暂停（进度已保存，重跑本脚本会续传）', YELLOW)}")
        return False, "interrupted"
    except Exception as e:
        return False, f"下载中断：{e}（重跑可续传）"

    print()  # 进度条后换行
    elapsed = max(time.time() - t0, 0.001)
    avg = (done - existing) / elapsed
    return True, f"完成 · {human(done)} · 平均 {human(avg)}/s"


def fmt_eta(sec):
    sec = int(sec)
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m{sec % 60}s"
    return f"{sec // 3600}h{(sec % 3600) // 60}m"


# ── 探测函数 ────────────────────────────────────────────────
def check_python():
    ok = sys.version_info >= (3, 9)
    return ok, f"Python {sys.version.split()[0]}（{'满足' if ok else '需 3.9+'}）"


def check_pip():
    try:
        import pip  # noqa: F401
        return True, "pip 可用"
    except ImportError:
        return False, "pip 不可用"


def check_zds_dep():
    """DS 网页通道用 CDP + Python 标准库，无需 playwright / websocket-client。"""
    return True, "DS 网页通道零额外依赖（CDP + 标准库，无需 playwright）"


def check_browser():
    """检测本机浏览器；返回 (是否有本机浏览器, 说明)。"""
    found = []
    for exe in ("chrome", "google-chrome", "msedge", "chromium", "chromium-browser"):
        p = shutil.which(exe)
        if p:
            found.append(p)
    if os.name == "nt":
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        local = os.environ.get("LOCALAPPDATA", "")
        for base in (pf, pf86, local):
            for rel in (("Google", "Chrome", "Application", "chrome.exe"),
                        ("Microsoft", "Edge", "Application", "msedge.exe")):
                p = os.path.join(base, *rel)
                if os.path.isfile(p):
                    found.append(p)
    elif sys.platform == "darwin":
        for app in ("/Applications/Google Chrome.app", "/Applications/Microsoft Edge.app"):
            if os.path.exists(app):
                found.append(app)
    found = list(dict.fromkeys(found))
    if found:
        return True, f"检测到本机浏览器：{os.path.basename(found[0])}（DS 网页通道可直接用）"
    return False, "未检测到 Chrome/Edge（DS 网页通道需要任意一款本机浏览器）"


def check_ollama():
    exe = shutil.which("ollama")
    if exe:
        return True, f"Ollama 已安装：{exe}"
    if os.name == "nt":
        p = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Ollama", "ollama.exe")
        if os.path.isfile(p):
            return True, f"Ollama 已安装：{p}"
    return False, "Ollama 未安装"


def ollama_models():
    """读取本地已装的 ollama 模型名。"""
    exe = shutil.which("ollama")
    if not exe:
        return []
    try:
        r = subprocess.run([exe, "list"], capture_output=True, text=True, timeout=20)
        names = []
        for line in r.stdout.splitlines()[1:]:
            if line.strip():
                names.append(line.split()[0])
        return names
    except Exception:
        return []


def find_comfy_root():
    """尝试从 config.json 推断 ComfyUI 根目录。"""
    if os.path.isfile(CONFIG_FILE):
        try:
            cfg = json.load(open(CONFIG_FILE, encoding="utf-8"))
            base = cfg.get("base_dir")
            if base:
                root = os.path.join(base, "ComfyUI_windows_portable", "ComfyUI")
                if os.path.isdir(root):
                    return root
        except Exception:
            pass
    # 常见位置兜底
    for p in ("E:/ComfyUI/ComfyUI_windows_portable/ComfyUI",
              os.path.join(HERE, "ComfyUI_windows_portable", "ComfyUI")):
        if os.path.isdir(p):
            return p
    return None


def check_comfy():
    root = find_comfy_root()
    if root:
        return True, root
    return False, "未找到 ComfyUI 目录（请在 config.json 填写 base_dir，或下载 Windows 便携版）"


# ── 下载任务定义（HF 路径经核对：Comfy-Org/Z-Image-Turbo 官方仓库）────
HF_ZIMAGE = "https://huggingface.co/Comfy-Org/Z-Image-Turbo/resolve/main"
HF_SAM = "https://huggingface.co/Comfy-Org/sam3.1/resolve/main"

MODEL_TASKS = [
    # (label, url, ComfyUI 内的相对目标路径, 预计大小)
    ("扩散模型 Z-Image-Turbo int8",
     f"{HF_ZIMAGE}/split_files/diffusion_models/z_image_turbo_int8_convrot.safetensors",
     "models/diffusion_models/z_image_turbo_int8_convrot.safetensors", "约 6.2 GB"),
    ("文本编码器 Qwen3-4B fp8",
     f"{HF_ZIMAGE}/split_files/text_encoders/qwen_3_4b_fp8_mixed.safetensors",
     "models/text_encoders/qwen_3_4b_fp8_mixed.safetensors", "约 2 GB"),
    ("VAE",
     f"{HF_ZIMAGE}/split_files/vae/ae.safetensors",
     "models/vae/ae.safetensors", "约 160 MB"),
    ("SAM 3.1（局部重绘用，可跳过）",
     f"{HF_SAM}/checkpoints/sam3.1_multiplex_fp16.safetensors",
     "models/checkpoints/sam3.1_multiplex_fp16.safetensors", "约 1.75 GB"),
]

OLLAMA_MODELS = [
    ("自由模式（忠实执行）", "richardyoung/qwen3-4b-instruct-2507-abliterated", "约 2.5 GB"),
    ("精细润色", "qwen3:8b", "约 5 GB"),
    ("看图点评（多模态）", "gemma3:4b", "约 3 GB"),
]


# ── 各阶段执行 ──────────────────────────────────────────────
def stage_config():
    """生成配置模板（不覆盖已有）。"""
    hr("阶段 0 · 配置文件")
    if os.path.isfile(CONFIG_FILE):
        print(f"  {c('✓', GREEN)} config.json 已存在，保持不动")
        return
    if os.path.isfile(CONFIG_EXAMPLE):
        shutil.copyfile(CONFIG_EXAMPLE, CONFIG_FILE)
        print(f"  {c('✓', GREEN)} 已从 config.example.json 生成 config.json")
        print(f"  {c('!', YELLOW)} 请编辑 config.json 的 base_dir，指向含 ComfyUI_windows_portable 的目录")
    else:
        print(f"  {c('!', YELLOW)} 找不到 config.example.json，跳过")


def stage_python(yes):
    hr("阶段 1 · Python 环境")
    ok, msg = check_python()
    print(f"  {'✓' if ok else '✗'} {msg}")
    ok2, msg2 = check_pip()
    print(f"  {'✓' if ok2 else '✗'} {msg2}")
    print(f"  ✓ {check_zds_dep()[1]}")
    print(f"  {c('→ 无需 pip install 任何自动化库，DS 网页通道开箱即用', GREEN)}")


def stage_browser(yes):
    hr("阶段 2 · 浏览器（DS 网页通道）")
    ok, msg = check_browser()
    print(f"  {'✓' if ok else '○'} {msg}")
    if ok:
        print(f"  {c('→ 零下载：本项目直接复用你的本机浏览器（CDP 控制）', GREEN)}")
        return
    print(f"  {c('!', YELLOW)} DS 网页通道需要任意一款本机浏览器；请安装 Chrome 或 Edge：")
    print("      https://www.google.com/chrome/   /   https://www.microsoft.com/edge")
    print(f"  {c('→ 装好浏览器即可，无需安装 playwright，也无需下载 chromium', GREEN)}")


def stage_ollama(yes):
    hr("阶段 3 · Ollama（本地对话模型）")
    ok, msg = check_ollama()
    print(f"  {'✓' if ok else '○'} {msg}")
    if not ok:
        print(f"  {c('!', YELLOW)} Ollama 需要系统级安装，请从官网下载安装（约 1 分钟）：")
        print("      https://ollama.com/download")
        print("    安装后重新运行本脚本即可自动继续。")
        return
    installed = ollama_models()
    print(f"  已装模型：{', '.join(installed) if installed else '（无）'}")
    exe = shutil.which("ollama")
    if not exe:
        print(f"  {c('!', YELLOW)} 当前终端找不到 ollama 命令，若刚安装请重开终端再跑。")
        return
    for label, model, size in OLLAMA_MODELS:
        base = model.split(":")[0]
        if any(m == model or m.startswith(base + ":") for m in installed):
            print(f"  {c('✓', GREEN)} {label}：{model} 已存在，跳过")
            continue
        print(f"\n  → {label}：{model} ({size})")
        if yes or ask(f"    下载 {model}？", True):
            print(f"    执行：ollama pull {model}（Ollama 自身会显示进度）")
            try:
                subprocess.check_call([exe, "pull", model])
                print(f"    {c('✓ 完成', GREEN)}")
            except Exception as e:
                print(f"    {c('✗ 失败：' + str(e), RED)}")


def stage_comfy(yes):
    hr("阶段 4 · ComfyUI 本体")
    ok, msg = check_comfy()
    if ok:
        print(f"  {c('✓', GREEN)} 已找到：{msg}")
        return
    print(f"  {c('!', YELLOW)} {msg}")
    print("  ComfyUI  Windows 便携版（解压即用，推荐）：")
    print("      https://github.com/comfyanonymous/ComfyUI/releases")
    print("  下载 ComfyUI_windows_portable 的 .7z/.zip，解压到例如 E:/ComfyUI/，")
    print("  然后在 config.json 把 base_dir 指向该目录。")


def stage_models(yes, mirror):
    hr("阶段 5 · 生图模型文件（Hugging Face）")
    root = find_comfy_root()
    if not root:
        print(f"  {c('!', YELLOW)} 未找到 ComfyUI 目录，无法确定下载位置。")
        print("     请先装 ComfyUI 并在 config.json 填好 base_dir，或先跑阶段 4。")
        print(f"     参考手动下载：https://huggingface.co/Comfy-Org/Z-Image-Turbo")
        return
    print(f"  目标目录：{root}")
    print(f"  下载源：{'国内镜像 ' + mirror if mirror else 'Hugging Face 官方'}")
    print("\n  将下载以下文件：")
    for label, url, rel, size in MODEL_TASKS:
        dest = os.path.join(root, *rel.split("/"))
        existed = os.path.isfile(dest)
        print(f"    {'✓ 已存在' if existed else '○ 待下载'}  {label:<32} {size:<10} → {rel}")
    print()
    for label, url, rel, size in MODEL_TASKS:
        dest = os.path.join(root, *rel.split("/"))
        if os.path.isfile(dest) and os.path.getsize(dest) > 1024 * 1024:
            print(f"  {c('✓', GREEN)} {label}：已存在，跳过")
            continue
        if not (yes or ask(f"  下载 {label}（{size}）？", True)):
            print(f"  - 跳过 {label}")
            continue
        print(f"\n  ↓ {c(label, BOLD)}  →  {rel}")
        ok, info = download(url, dest, mirror=mirror, label=label)
        tag = "✓" if ok and info != "interrupted" else "✗"
        print(f"  {c(tag, GREEN if tag == '✓' else RED)} {label}：{info}")


def stage_check_all():
    """体检总览，返回各阶段的 (名称, ok, msg)。"""
    hr("体检 · 当前环境")
    rows = [
        ("Python 3.9+", *check_python()),
        ("pip", *check_pip()),
        ("DS 网页依赖（CDP 零依赖）", *check_zds_dep()),
        ("本机浏览器（DS 网页需要）", *check_browser()),
        ("Ollama（对话模型）", *check_ollama()),
        ("ComfyUI（生图）", *check_comfy()),
    ]
    for name, ok, msg in rows:
        mark = c("✓", GREEN) if ok else c("○", YELLOW)
        print(f"  {mark} {name:<26} {msg}")
    return rows


# ── 主流程 ──────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="zwish-web 懒人一键安装向导")
    ap.add_argument("--check", action="store_true", help="只体检，不下载不安装")
    ap.add_argument("--yes", action="store_true", help="全自动：自动项不再逐项询问")
    ap.add_argument("--only", default="all",
                    help="只执行某个阶段：python/browser/ollama/comfy/models/config/all（逗号分隔）")
    ap.add_argument("--mirror", default="", help="HF 镜像前缀，如 https://hf-mirror.com")
    ap.add_argument("--skip-download", action="store_true", help="只生成配置模板和目录，不下载大文件")
    args = ap.parse_args()

    print(c(r"""
  ╔══════════════════════════════════════════════════════════╗
  ║        zwish-web · 懒人一键安装向导                     ║
  ║  一条命令看清：要装什么 · 装到哪 · 速度 · 剩余时间       ║
  ╚══════════════════════════════════════════════════════════╝
""", CYAN))

    stages = set(s.strip() for s in args.only.split(",") if s.strip())
    if "all" in stages:
        stages = {"config", "python", "browser", "ollama", "comfy", "models"}
    valid = {"config", "python", "browser", "ollama", "comfy", "models"}
    unknown = stages - valid
    if unknown:
        print(c(f"未知阶段：{', '.join(unknown)}；可选：{', '.join(sorted(valid))}", RED))
        sys.exit(2)

    # 体检（任何模式都先体检，让用户心里有数）
    stage_check_all()

    if args.check:
        hr("体检完成（--check 模式，未做任何改动）")
        return

    if not args.yes:
        hr("安装清单")
        print("  接下来会按顺序处理，每一步都可以选择装/不装：")
        print("    0 配置文件     生成 config.json（不覆盖已有）")
        print("    1 Python 环境   检查版本（DS 网页通道零额外依赖）")
        print("    2 浏览器        复用本机 Chrome/Edge（CDP 控制，零下载）")
        print("    3 Ollama        检测系统软件 + 拉取对话模型（自带进度）")
        print("    4 ComfyUI       检测本体，缺了给官方下载链接")
        print("    5 模型文件      Z-Image / Qwen3 / VAE / SAM3（带进度条、断点续传）")
        if not ask("\n  开始？", True):
            print("  已取消。可随时重跑：python install.py")
            return

    # 按顺序执行
    if "config" in stages:
        stage_config()
    if "python" in stages:
        stage_python(args.yes)
    if "browser" in stages:
        stage_browser(args.yes)
    if "ollama" in stages:
        stage_ollama(args.yes)
    if "comfy" in stages:
        stage_comfy(args.yes)
    if "models" in stages and not args.skip_download:
        stage_models(args.yes, args.mirror)

    hr(f"完成{' 🎉' if len(stages) > 1 else '（本阶段）'}")
    print("  下一步：")
    print("    1) 确认 config.json 的 base_dir 指向 ComfyUI_windows_portable 所在目录")
    print("    2) 终端运行：python server.py")
    print("    3) 浏览器打开 http://127.0.0.1:8189（端口以 config.json 为准）")
    print("  详细配置与排错见 DEPLOYMENT.md")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(f"\n{c('已中断。进度已保存，重跑 python install.py 会从断点继续。', YELLOW)}")
