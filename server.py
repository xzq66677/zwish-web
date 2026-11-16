#!/usr/bin/env python
"""zwish-web - 许愿式生图 Web 向导。http://127.0.0.1:PORT  （只绑定本机）

复用 zwish 精修逻辑 + zimg 生成管线。启动: python server.py [--no-open]
路径全部来自 zconfig（config.json），见 config.example.json。
"""
import json, os, random, re, subprocess, sys, threading, time, traceback, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import zconfig

CFG = zconfig.CFG
BASE = CFG["base_dir"]
HERE = zconfig.HERE
sys.path.insert(0, HERE)
import zwish  # noqa: E402

OUTDIR = CFG["output_dir"]
STATIC = CFG["static_dir"]
UPLOADS = CFG["uploads_dir"]
HIST = CFG["history_file"]
PORT = CFG.get("port", 8189)
os.makedirs(UPLOADS, exist_ok=True)

JOBS = {}
LOCK = threading.Lock()
DESCRIBE_CACHE = {}  # {path:mtime -> vision 描述}，避免每轮微调重复读图


def run_job(job):
    job["status"] = "running"
    t0 = time.time()
    try:
        env = {**os.environ, "ZWISH_SOURCE": "web"}
        p = subprocess.run([sys.executable, "-u", zwish.ZIMG] + job["args"],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=900, env=env)
        out = (p.stdout or "") + (p.stderr or "")
        if p.returncode != 0:
            job["status"] = "failed"
            job["error"] = out[-800:]
        else:
            marks = re.findall(r"ZWISH_IMAGE=(.+)", out)
            m = re.search(r"完成 \([\d.]+s\): (.+)", out)
            image_path = (marks[-1].strip() if marks else (m.group(1).strip() if m else ""))
            if not image_path:
                prefix = "zwishweb"
                if "--prefix" in job["args"]:
                    prefix = job["args"][job["args"].index("--prefix") + 1]
                cand = [os.path.join(OUTDIR, f) for f in os.listdir(OUTDIR)
                        if f.startswith(prefix + "_") and f.lower().endswith(".png")] if os.path.isdir(OUTDIR) else []
                image_path = max(cand, key=os.path.getmtime) if cand else ""
            if image_path:
                job["status"] = "done"
                job["image"] = os.path.basename(image_path)
            else:
                job["status"] = "failed"
                job["error"] = "zimg 已完成但没有返回图片路径"
    except Exception as e:
        job["status"] = "failed"
        job["error"] = str(e)
    job["elapsed"] = round(time.time() - t0, 1)


def start_job(args):
    jid = f"job_{int(time.time()*1000)}_{random.randint(100,999)}"
    job = {"id": jid, "args": args, "status": "queued", "started": time.time()}
    with LOCK:
        JOBS[jid] = job
    threading.Thread(target=run_job, args=(job,), daemon=True).start()
    return job


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _file(self, path, ctype):
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError:
            self._json({"error": "not found"}, 404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            return self._file(os.path.join(STATIC, "index.html"), "text/html; charset=utf-8")
        if path.startswith("/assets/"):
            name = os.path.basename(path[len("/assets/"):])
            fp = os.path.join(OUTDIR, name)
            if os.path.isfile(fp):
                return self._file(fp, "image/png")
            return self._json({"error": "not found"}, 404)
        if path == "/api/gallery":
            files = sorted((f for f in os.listdir(OUTDIR) if f.lower().endswith(".png")),
                           key=lambda f: -os.path.getmtime(os.path.join(OUTDIR, f)))[:60] if os.path.isdir(OUTDIR) else []
            return self._json({"images": [{"name": f, "url": "/assets/" + f} for f in files]})
        if path == "/api/config":
            ps = [p for p in (zwish.PROVIDERS or zwish.load_providers()) if p.get("on", True)]
            try:
                import zds
                ds_web_ok, ds_web_msg = zds.available()
            except Exception as e:
                ds_web_ok, ds_web_msg = False, str(e)
            return self._json({
                "cloud": bool(ps), "vision_model": zwish.VISION,
                "comfy_port": CFG.get("comfy_port", 8188),
                "providers": zwish.PROVIDERS or zwish.load_providers(),
                "ds_web": {"ok": ds_web_ok, "msg": ds_web_msg},
                "ask_count": CFG.get("ask_count", {}),
                "ready": CFG.get("ready", False),
            })
        if path == "/api/prompts":
            import zprompts
            return self._json(zprompts.state())
        if path == "/api/providers":
            return self._json({"providers": zwish.PROVIDERS or zwish.load_providers()})
        if path == "/api/history":
            recs = []
            if os.path.isfile(HIST):
                with open(HIST, encoding="utf-8") as f:
                    for line in f:
                        try:
                            recs.append(json.loads(line))
                        except Exception:
                            pass
            return self._json({"records": recs[-200:][::-1]})
        if path.startswith("/api/job/"):
            jid = path.rsplit("/", 1)[1]
            with LOCK:
                job = JOBS.get(jid)
            return self._json(job or {"error": "unknown job"})
        return self._json({"error": "unknown path"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            if path == "/api/upload":
                n = int(self.headers.get("content-length", 0))
                raw = self.rfile.read(n)
                base = re.sub(r"[^A-Za-z0-9_.-]", "_", self.headers.get("X-Filename", "img.png"))
                fp = os.path.join(UPLOADS, f"{int(time.time())}_{base}")
                with open(fp, "wb") as f:
                    f.write(raw)
                return self._json({"path": fp, "name": os.path.basename(fp)})

            n = int(self.headers.get("content-length", 0))
            raw = self.rfile.read(n)
            try:
                b = json.loads(raw.decode("utf-8"))
            except UnicodeDecodeError:
                b = json.loads(raw.decode("gbk"))  # 终端 GBK 兜底
            model = b.get("model") or None  # None → zwish 自动选自由模式

            # ── 供应商管理 ──
            if path == "/api/test_provider":
                p = b.get("provider") or {}
                try:
                    r = zwish.test_provider(p)
                    return self._json(r)
                except Exception as e:
                    return self._json({"ok": False, "error": str(e)})
            if path == "/api/save_providers":
                zwish.save_providers(b.get("providers", []))
                return self._json({"ok": True, "providers": zwish.PROVIDERS})

            # ── 内置提示词自定义（DS 网页 / API / 云端共用）──
            if path == "/api/save_prompts":
                import zprompts
                saved = zprompts.save_overrides(b.get("prompts", {}))
                return self._json({"ok": True, "custom": list(saved.keys())})

            if path == "/api/open_folder":
                try: os.startfile(OUTDIR)
                except Exception as e: return self._json({"ok": False, "error": str(e)})
                return self._json({"ok": True})

            if path == "/api/delete":
                name = os.path.basename(b.get("name", ""))
                fp = os.path.join(OUTDIR, name)
                if not name.lower().endswith(".png") or not os.path.isfile(fp):
                    return self._json({"error": "not found"}, 404)
                os.remove(fp)
                return self._json({"ok": True})

            # ── 许愿 → 追问 / 精修 / 重写 ──
            if path == "/api/questions":
                cnt = zconfig.clamp_ask_count(
                    b.get("count"), default=zconfig.ask_recommend(model))
                model, qs = zwish.gen_questions(b.get("wish", ""), model, count=cnt)
                return self._json({"refiner": model, "count": cnt, "questions": qs})
            if path == "/api/prompt":
                answers = b.get("answers")
                if isinstance(answers, list):
                    answers = "\n".join(f"- {a}" for a in answers)
                model, d = zwish.gen_prompt(b.get("wish", ""), answers, model)
                return self._json({"refiner": model, **d})
            if path == "/api/rewrite":
                model, d = zwish.rewrite_prompt(b.get("qa", ""), b.get("current", ""),
                                                b.get("feedback", ""), model,
                                                b.get("w"), b.get("h"),
                                                current_en=b.get("current_en", ""))
                return self._json({"refiner": model, **d})

            # ── 微调：看图 → 追问 → 规划 ──
            if path in ("/api/plan_questions", "/api/plan"):
                img = b.get("img")
                if img and img.startswith("/assets/"):
                    img = os.path.join(OUTDIR, os.path.basename(img))
                key = f"{img}:{int(os.path.getmtime(img))}" if img and os.path.isfile(img) else None
                desc = DESCRIBE_CACHE.get(key) if key else None
                if key and not desc:
                    try:
                        desc = zwish.describe_image(img)
                        DESCRIBE_CACHE[key] = desc
                    except Exception:
                        desc = None
                if path == "/api/plan_questions":
                    cnt = zconfig.clamp_ask_count(
                        b.get("count"), default=zconfig.ask_recommend(model))
                    model, qs = zwish.gen_edit_questions(
                        b.get("wish", ""), b.get("current", ""), b.get("history", ""),
                        b.get("feedback", ""), desc or "", model, count=cnt)
                    return self._json({"refiner": model, "count": cnt, "questions": qs})
                model, d = zwish.gen_plan(
                    b.get("wish", ""), b.get("current", ""), b.get("history", ""),
                    b.get("feedback", ""), model,
                    image_path=(img if key else None), image_desc=desc,
                    current_en=b.get("current_en", ""))
                if key and d.get("image_desc"):
                    DESCRIBE_CACHE[key] = d["image_desc"]
                return self._json({"refiner": model, "vision_model": zwish.VISION, **d})

            # ── 看图点评（本地视觉，不上云）──
            if path == "/api/critique":
                import base64
                img = os.path.basename(b.get("image", ""))
                fp = os.path.join(OUTDIR, img)
                if not os.path.isfile(fp):
                    return self._json({"error": "image not found"}, 404)
                cModel = b.get("model") or zwish.VISION
                b64 = base64.b64encode(open(fp, "rb").read()).decode()
                import zprompts
                r = zwish.http_json(f"{zwish.OLLAMA}/api/chat", {
                    "model": cModel, "stream": False, "think": False,
                    "messages": [
                        {"role": "system", "content": zprompts.system("critique")},
                        {"role": "user",
                         "content": f"当前提示词：{b.get('prompt', '（无）')}\n请评审这张生成图，给出总评和3条改进建议。",
                         "images": [b64]}],
                    "options": {"temperature": 0.5}}, timeout=300)
                text = r.get("message", {}).get("content", "")
                m = re.search(r"\{.*\}", text, re.S)
                d = json.loads(m.group(0)) if m else {"overall": text[:600], "suggestions": []}
                return self._json({"refiner": cModel, **d})

            # ── 历史 ──
            if path == "/api/delete_history":
                ts = int(b.get("ts", 0))
                image = os.path.basename(b.get("image", ""))
                if os.path.isfile(HIST):
                    keep = []
                    with open(HIST, encoding="utf-8") as f:
                        for line in f:
                            skip = False
                            try:
                                r = json.loads(line)
                                if r.get("ts") == ts and r.get("image") == image:
                                    skip = True
                            except Exception:
                                skip = True
                            if not skip:
                                keep.append(line.rstrip("\n"))
                    with open(HIST, "w", encoding="utf-8") as f:
                        f.write("\n".join(x for x in keep if x) + ("\n" if keep else ""))
                return self._json({"ok": True})
            if path == "/api/clear_history":
                open(HIST, "w").close()
                return self._json({"ok": True})

            # ── 生成：prompt 英文进 ComfyUI，prompt_cn 仅做历史展示 ──
            if path == "/api/generate":
                prompt_en = (b.get("prompt_en") or b.get("prompt") or "").strip()
                if not prompt_en:
                    return self._json({"error": "prompt required"}, 400)
                prompt_cn = b.get("prompt_cn") or b.get("current_cn") or ""
                img = b.get("img")
                if img and img.startswith("/assets/"):  # 画廊引用 → 磁盘路径
                    img = os.path.join(OUTDIR, os.path.basename(img))
                    if not os.path.isfile(img):
                        return self._json({"error": "input image not found"}, 400)
                seg = b.get("segment")
                args = []
                if img and seg:
                    args += ["--img", img, "--segment", seg]
                elif img:
                    args += ["--img", img]
                else:
                    args += ["--w", str(int(b.get("w", 1024))), "--h", str(int(b.get("h", 1024)))]
                args += [prompt_en, "--prompt-cn", prompt_cn]
                if b.get("denoise") is not None:
                    args += ["--denoise", str(b["denoise"])]
                if b.get("steps") is not None:
                    args += ["--steps", str(b["steps"])]
                if b.get("grow") is not None:
                    args += ["--grow", str(b["grow"])]
                if b.get("threshold") is not None:
                    args += ["--threshold", str(b["threshold"])]
                args += ["--seed", str(b.get("seed") or random.randint(0, 2**31 - 1)),
                         "--prefix", b.get("prefix", "zwishweb")]
                job = start_job(args)
                return self._json({"job": job["id"]})

            return self._json({"error": "unknown path"}, 404)
        except ValueError as e:
            return self._json({"error": str(e)}, 500)
        except Exception as e:
            traceback.print_exc()
            return self._json({"error": str(e)}, 500)


def main():
    if "--no-open" not in sys.argv:
        threading.Timer(0.8, lambda: webbrowser.open(f"http://127.0.0.1:{PORT}")).start()
    print(f"[zwish-web] http://127.0.0.1:{PORT}  (Ctrl+C 退出)")
    if not CFG.get("ready"):
        print("[zwish-web] 警告：ComfyUI 路径未配置，生图会失败。请复制 config.example.json 为 config.json 并填写 base_dir")
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()


if __name__ == "__main__":
    main()
