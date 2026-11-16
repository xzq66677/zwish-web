#!/usr/bin/env python
"""zimg - one-command Z-Image generation & editing via local ComfyUI.

Usage:
  python zimg.py "prompt"                          # 文生图（prompt 建议英文）
  python zimg.py "prompt" --w 768 --h 1024 --seed 42
  python zimg.py --img photo.png "change bg to snowy street" --denoise 0.55   # img2img
  python zimg.py --img photo.png --segment "cat" "a white kitten in helmet"  # SAM3 inpaint

Only stdlib is required. The ComfyUI server is started automatically if not running.
路径/模型名全部来自 zconfig（config.json），见 config.example.json。
"""
import argparse, json, os, random, re, shutil, subprocess, sys, time, urllib.request

import zconfig

CFG = zconfig.CFG
PORTABLE = os.path.dirname(CFG["base_dir"]) if CFG["base_dir"] else ""
# base_dir 已经是含 ComfyUI_windows_portable 的那一层；向下组合
COMFY_ROOT = os.path.join(CFG["base_dir"], "ComfyUI_windows_portable", "ComfyUI") if CFG["base_dir"] else ""
HOST = f"http://127.0.0.1:{CFG.get('comfy_port', 8188)}"
INPUT_DIR = CFG.get("input_dir") or (os.path.join(COMFY_ROOT, "input") if COMFY_ROOT else "")
OUTPUT_DIR = CFG.get("output_dir") or (os.path.join(COMFY_ROOT, "output") if COMFY_ROOT else "")

UNET = CFG["models"]["unet"]
CLIP = CFG["models"]["clip"]
VAE = CFG["models"]["vae"]
SAM_CKPT = CFG["models"]["sam_ckpt"]


def http_json(path, payload=None, timeout=30):
    if payload is None:
        return json.load(urllib.request.urlopen(HOST + path, timeout=timeout))
    req = urllib.request.Request(HOST + path, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=timeout))


def ensure_server():
    try:
        http_json("/system_stats", timeout=3)
        return
    except Exception:
        pass
    if not CFG.get("ready"):
        sys.exit("[zimg] 未配置 ComfyUI 路径，请先复制 config.example.json 为 config.json 并填写 base_dir")
    print("[zimg] ComfyUI 未运行，正在启动 ...")
    main_py = CFG["comfyui_main"]
    py_exe = CFG["comfyui_python"]
    root = CFG["base_dir"]
    log_out = os.path.join(root, "comfyui.stdout.log")
    log_err = os.path.join(root, "comfyui.stderr.log")
    port = CFG.get("comfy_port", 8188)
    ps = (
        f"$main = '{main_py}';"
        f"$py = '{py_exe}';"
        "Start-Process -FilePath $py -ArgumentList @('-s', $main, '--windows-standalone-build', "
        f"'--listen', '127.0.0.1', '--port', '{port}') -WorkingDirectory (Split-Path $main) "
        f"-RedirectStandardOutput '{log_out}' -RedirectStandardError '{log_err}' -WindowStyle Hidden"
    )
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True,
                   capture_output=True, timeout=60)
    for _ in range(90):
        try:
            http_json("/system_stats", timeout=3)
            print("[zimg] ComfyUI 已就绪")
            return
        except Exception:
            time.sleep(1)
    sys.exit(f"[zimg] ComfyUI 启动超时，请查看 {log_err}")


def stage_input(img_path):
    if not os.path.isfile(img_path):
        sys.exit(f"[zimg] 找不到输入图片: {img_path}")
    os.makedirs(INPUT_DIR, exist_ok=True)
    base = re.sub(r"[^A-Za-z0-9_.-]", "_", os.path.basename(img_path))
    name = f"zimg_{int(time.time())}_{base}"
    shutil.copyfile(img_path, os.path.join(INPUT_DIR, name))
    return name


def build_workflow(args, image_name=None):
    wf = {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": UNET, "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": CLIP, "type": "lumina2"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": VAE}},
        "4": {"class_type": "CLIPTextEncode", "inputs": {"text": args.prompt, "clip": ["2", 0]}},
        "5": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["4", 0]}},
        "6": {"class_type": "ModelSamplingAuraFlow", "inputs": {"model": ["1", 0], "shift": 3}},
        "10": {"class_type": "KSampler", "inputs": {
            "seed": args.seed, "steps": args.steps, "cfg": 1.0,
            "sampler_name": "res_multistep", "scheduler": "simple", "denoise": args.denoise,
            "model": ["6", 0], "positive": ["4", 0], "negative": ["5", 0]}},
        "11": {"class_type": "VAEDecode", "inputs": {"samples": ["10", 0], "vae": ["3", 0]}},
        "12": {"class_type": "SaveImage", "inputs": {"filename_prefix": args.prefix, "images": ["11", 0]}},
    }
    if args.img:  # image-conditioned modes: encode the source image as the latent
        wf["20"] = {"class_type": "LoadImage", "inputs": {"image": image_name}}
        wf["21"] = {"class_type": "VAEEncode", "inputs": {"pixels": ["20", 0], "vae": ["3", 0]}}
        if args.segment:  # SAM3 open-vocabulary mask -> grow -> restrict edit region
            wf["30"] = {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": SAM_CKPT}}
            wf["31"] = {"class_type": "CLIPTextEncode", "inputs": {"text": args.segment, "clip": ["30", 1]}}
            wf["32"] = {"class_type": "SAM3_Detect", "inputs": {
                "model": ["30", 0], "image": ["20", 0], "conditioning": ["31", 0],
                "threshold": args.threshold, "refine_iterations": 2, "individual_masks": False}}
            wf["33"] = {"class_type": "GrowMask", "inputs": {
                "mask": ["32", 0], "expand": args.grow, "tapered_corners": True}}
            wf["34"] = {"class_type": "SetLatentNoiseMask", "inputs": {
                "samples": ["21", 0], "mask": ["33", 0]}}
            wf["10"]["inputs"]["latent_image"] = ["34", 0]
        else:
            wf["10"]["inputs"]["latent_image"] = ["21", 0]
    else:
        wf["20"] = {"class_type": "EmptySD3LatentImage",
                    "inputs": {"width": args.w, "height": args.h, "batch_size": 1}}
        wf["10"]["inputs"]["latent_image"] = ["20", 0]
    return wf


HELP_CN = """
zimg 速查 —— 本地生图 / 改图（全程离线，约 15 秒一张）
======================================================

你想做什么？

【1】从零画一张图
    zimg "prompt"
    例: zimg "a cozy convenience store on a snowy night, cinematic"
    加尺寸: --w 768 --h 1024      固定复现: --seed 42

【2】给一张图，整体改风格/色调/背景
    zimg --img 图片路径 "change to watercolor style"
    改动幅度: --denoise 0.55 (小 0.3 / 中 0.55 / 大 0.8)

【3】给一张图，只改图里的某个东西（其余不动）
    zimg --img 图片路径 --segment "目标" "变成什么"
    例: zimg --img 照片.png --segment "cat" "a corgi in a spacesuit"
    完全替换用 --denoise 1.0，融合过渡用 0.6~0.8

其他常用：
  --out 目录       顺便把结果复制到指定目录
  --seed 数字      想反复微调同一张图时固定它
  --threshold 0.3  目标检测不到时降低灵敏度
  --grow 12        蒙版向外扩的像素（默认 12）

提示：
  * 真正生图建议用英文提示词；中文也能生成，但英文通常更稳
  * 想多轮对话式精修请用 zwish（自动产出中英双字段）
  * 路径/模型名在 config.json 里配置（参照 config.example.json）
"""


def main():
    if len(sys.argv) >= 2 and sys.argv[1] in ("help", "--help-cn"):
        print(HELP_CN); return

    p = argparse.ArgumentParser(description="Z-Image generate / edit via local ComfyUI")
    p.add_argument("prompt", help="正向提示词（建议英文；img2img/inpaint 时描述修改后的效果）")
    p.add_argument("--prompt-cn", default="", help="对应的中文说明，仅用于历史记录展示")
    p.add_argument("--img", help="输入图片路径（触发 img2img 或 inpaint 模式）")
    p.add_argument("--segment", help="SAM3 分割词：画面里要改的目标，如 cat（需配合 --img）")
    p.add_argument("--w", type=int, default=1024, help="宽度（仅文生图）")
    p.add_argument("--h", type=int, default=1024, help="高度（仅文生图）")
    p.add_argument("--seed", type=int, default=random.randint(0, 2**31 - 1))
    p.add_argument("--steps", type=int, default=8)
    p.add_argument("--denoise", type=float, default=None,
                   help="重绘强度：img2img 默认 0.55；inpaint 默认 1.0")
    p.add_argument("--grow", type=int, default=12, help="蒙版向外扩展像素（inpaint）")
    p.add_argument("--threshold", type=float, default=0.5, help="SAM3 检测阈值")
    p.add_argument("--prefix", default="zimg")
    p.add_argument("--out", help="额外复制结果到此目录")
    args = p.parse_args()

    if args.segment and not args.img:
        sys.exit("[zimg] --segment 需要配合 --img 使用")
    if args.denoise is None:
        args.denoise = 1.0 if args.segment else (0.55 if args.img else 1.0)

    ensure_server()
    image_name = stage_input(args.img) if args.img else None
    mode = "inpaint(SAM3)" if args.segment else ("img2img" if args.img else "t2i")
    print(f"[zimg] mode={mode} seed={args.seed} denoise={args.denoise} steps={args.steps}")

    resp = http_json("/prompt", {"prompt": build_workflow(args, image_name),
                                 "client_id": "zimg-cli"}, timeout=60)
    if resp.get("node_errors"):
        sys.exit("[zimg] 工作流校验失败:\n" + json.dumps(resp["node_errors"], ensure_ascii=False, indent=2))
    pid = resp["prompt_id"]

    t0 = time.time()
    while time.time() - t0 < 300:
        time.sleep(1)
        try:
            entry = http_json(f"/history/{pid}", timeout=15).get(pid)
        except Exception:
            entry = None
        if not entry:
            candidates = []
            if os.path.isdir(OUTPUT_DIR):
                for name in os.listdir(OUTPUT_DIR):
                    if name.startswith(args.prefix + "_") and name.lower().endswith(".png"):
                        path = os.path.join(OUTPUT_DIR, name)
                        if os.path.isfile(path) and os.path.getmtime(path) >= t0 - 3:
                            candidates.append(path)
            if candidates:
                src = max(candidates, key=os.path.getmtime)
                print(f"[zimg] 完成（history 兜底，{round(time.time()-t0,1)}s）: {src}")
                print(f"ZWISH_IMAGE={src}", flush=True)
                log_history(args, src, time.time() - t0)
                if args.out:
                    os.makedirs(args.out, exist_ok=True)
                    dst = shutil.copy(src, args.out)
                    print(f"[zimg] 已复制到: {dst}")
                return
            continue
        status = entry.get("status", {})
        if status.get("status_str") == "error":
            sys.exit("[zimg] 执行失败:\n" + json.dumps(entry, ensure_ascii=False)[:3000])
        outputs = entry.get("outputs")
        if outputs:
            for node in outputs.values():
                for img in node.get("images", []):
                    src = os.path.join(OUTPUT_DIR, img["subfolder"], img["filename"])
                    print(f"[zimg] 完成 ({round(time.time()-t0,1)}s): {src}")
                    print(f"ZWISH_IMAGE={src}", flush=True)
                    log_history(args, src, time.time() - t0)
                    if args.out:
                        os.makedirs(args.out, exist_ok=True)
                        dst = shutil.copy(src, args.out)
                        print(f"[zimg] 已复制到: {dst}")
            return
    sys.exit(f"[zimg] 等待超时（300s），请到 {HOST} 查看队列状态")


def log_history(args, image, elapsed):
    """每次成功生成都落一条历史，供 zwish-web 历史页回看。"""
    try:
        hist = CFG["history_file"]
        os.makedirs(os.path.dirname(hist) or ".", exist_ok=True)
        rec = {"ts": int(time.time()),
               "source": os.environ.get("ZWISH_SOURCE", "cli"),
               "mode": ("inpaint" if args.segment else ("img2img" if args.img else "t2i")),
               "prompt": args.prompt,                       # 真正送进生图的英文词
               "prompt_cn": getattr(args, "prompt_cn", ""),  # 中文说明（展示用）
               "w": args.w, "h": args.h, "seed": args.seed,
               "denoise": args.denoise, "grow": args.grow, "threshold": args.threshold,
               "segment": args.segment, "image": os.path.basename(image),
               "elapsed": round(elapsed, 1)}
        with open(hist, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


if __name__ == "__main__":
    main()
