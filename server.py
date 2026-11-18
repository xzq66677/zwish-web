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

_JOBS_KEEP = 50          # 保留最近 N 个已结束任务，防止长驻运行内存无界增长
_DESCRIBE_LIMIT = 200    # 看图描述缓存上限
_UPLOAD_MAX = 25 * 1024 * 1024  # 上传图片体积上限 25MB


def _looks_like_image(raw):
    """魔数校验：是否为 PNG / JPEG / WEBP / GIF。"""
    if not raw or len(raw) < 12:
        return False
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return True
    if raw[:3] == b"\xff\xd8\xff":
        return True
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return True
    if raw[:4] in (b"GIF8",):
        return True
    return False


def _is_loopback_host(h):
    """Host/Origin 的主机名是否为本机回环（ipaddress 精确判断，域名字符串不误判）。"""
    h = (h or "").strip("[]").lower()
    if h == "localhost":
        return True
    import ipaddress
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def _split_hostport(raw, default_port):
    """把 Host 头 / Origin 的 host:port 拆成 (host, port)。

    支持 IPv6 字面量（[::1]:8189）；无端口时用 default_port
    （Host 缺端口 → 服务监听端口；Origin 缺端口 → scheme 默认端口）。
    """
    raw = (raw or "").strip()
    if not raw:
        return "", default_port
    if raw.startswith("["):                       # IPv6：[::1]:8189
        inner, _, rest = raw[1:].partition("]")
        if rest.startswith(":") and rest[1:].isdigit():
            return inner.lower(), int(rest[1:])
        return inner.lower(), default_port
    if ":" in raw:
        host, _, port = raw.rpartition(":")
        if port.isdigit():
            return host.strip("[]").lower(), int(port)
    return raw.strip("[]").lower(), default_port


def _mask_providers(ps):
    """API Key 脱敏：只回 has_key 标志，不把 key 明文发给浏览器。

    设置面板保存时 key 留空表示"保持不变"，由 _merge_provider_keys 回填。
    """
    out = []
    for p in ps or []:
        q = dict(p)
        q["has_key"] = bool(p.get("key"))
        q["key"] = ""
        out.append(q)
    return out


def _merge_provider_keys(incoming):
    """保存供应商时回填被脱敏遮掉的 key：key 为空且原名已有 key → 沿用旧 key。

    （如需清除 key，删除该供应商后重建即可。）
    """
    old = {}
    for p in (zwish.PROVIDERS or zwish.load_providers()):
        old[p.get("name")] = p
    out = []
    for p in incoming or []:
        p = dict(p)
        if not p.get("key") and p.get("name") in old:
            p["key"] = old[p["name"]].get("key", "")
        out.append(p)
    return out


def _write_history_lines(lines):
    """原子写历史文件：先写临时文件再 os.replace。

    zimg 子进程以追加模式写同一文件；若直接 "w" 覆盖写，删除期间
    恰好生成完一条新记录会被抹掉，读取方也可能读到半行 JSON。
    os.replace 在同一文件系统上是原子的，可避免中间状态暴露。
    """
    tmp = HIST + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("".join(x + "\n" for x in lines if x))
    os.replace(tmp, HIST)


def _delete_history_entry(ts, image):
    """从 history.jsonl 中删除一条记录（原子替换写回）。"""
    if not os.path.isfile(HIST):
        return
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
    _write_history_lines(keep)


def _cache_desc(key, desc):
    """写入看图描述缓存并做上限截断（淘汰最早写入的条目）。"""
    DESCRIBE_CACHE[key] = desc
    if len(DESCRIBE_CACHE) > _DESCRIBE_LIMIT:
        for k in list(DESCRIBE_CACHE)[: len(DESCRIBE_CACHE) - _DESCRIBE_LIMIT]:
            DESCRIBE_CACHE.pop(k, None)


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
        finished = [k for k, v in JOBS.items() if v.get("status") in ("done", "failed")]
        if len(finished) > _JOBS_KEEP:
            finished.sort(key=lambda k: JOBS[k].get("started", 0))
            for k in finished[: len(finished) - _JOBS_KEEP]:
                JOBS.pop(k, None)
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

    def _same_origin(self, need_origin=False):
        """本机服务安全边界（防 CSRF / DNS rebinding）。

        - Host 头必须指向本机回环（拦 DNS rebinding：外部页面把域名解析到 127.0.0.1 再访问）；
        - POST 请求若带 Origin（浏览器对跨源 POST 一定带），其 **host 和端口必须与请求
          的 Host 完全一致**，且仍要求回环主机（issue #20：原先只比主机不比端口，
          本机其他端口的页面也能通过校验发起状态变更）。
        """
        host_raw = self.headers.get("Host") or ""
        host, hport = _split_hostport(host_raw, PORT)
        if host and not _is_loopback_host(host):
            return False
        origin = self.headers.get("Origin")
        if origin:
            o = urlparse(origin)
            ohost = (o.hostname or "").lower()
            if not _is_loopback_host(ohost):
                return False
            try:
                oport = o.port
            except ValueError:                 # 非法端口 → 直接拒绝
                return False
            if oport is None:
                oport = 443 if (o.scheme or "").lower() == "https" else 80
            if ohost != host or oport != hport:
                return False
        return True

    def do_GET(self):
        path = urlparse(self.path).path
        if not self._same_origin():
            return self._json({"error": "请求来源不是本机，已被拒绝"}, 403)
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
            try:
                import zyuanbao
                yb_web_ok, yb_web_msg = zyuanbao.available()
            except Exception as e:
                yb_web_ok, yb_web_msg = False, str(e)
            try:
                import zwebchat
                custom_channels = zwebchat.list_enabled_status()
            except Exception as e:
                custom_channels = []
            return self._json({
                "cloud": bool(ps), "vision_model": zwish.VISION,
                "comfy_port": CFG.get("comfy_port", 8188),
                "providers": _mask_providers(zwish.PROVIDERS or zwish.load_providers()),
                "ds_web": {"ok": ds_web_ok, "msg": ds_web_msg},
                "yuanbao_web": {"ok": yb_web_ok, "msg": yb_web_msg},
                "custom_channels": custom_channels,
                "ask_count": CFG.get("ask_count", {}),
                "ready": CFG.get("ready", False),
            })
        if path == "/api/prompts":
            import zprompts
            return self._json(zprompts.state())
        if path == "/api/providers":
            return self._json({"providers": _mask_providers(zwish.PROVIDERS or zwish.load_providers())})
        if path == "/api/webchat_channels":
            # 网页自配置通道列表（含 disabled），供设置面板编辑
            try:
                import zwebchat
                return self._json({"channels": zwebchat.list_all(),
                                   "source": "user" if zwebchat.load_user_channels() is not None else "config"})
            except Exception as e:
                return self._json({"error": str(e)})
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
        if not self._same_origin():
            return self._json({"error": "请求来源不是本机，已被拒绝（防跨源篡改配置）"}, 403)
        try:
            if path == "/api/upload":
                n = int(self.headers.get("content-length", 0))
                if n <= 0 or n > _UPLOAD_MAX:
                    return self._json({"error": f"图片体积超出上限（{_UPLOAD_MAX // (1024*1024)}MB）"}, 413)
                raw = self.rfile.read(n)
                if not _looks_like_image(raw):
                    return self._json({"error": "不是受支持的图片格式（仅 PNG / JPEG / WEBP / GIF）"}, 400)
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
                if not p.get("key"):
                    # key 已脱敏遮蔽：按名称回填存储中的真实 key 再测试
                    for old in (zwish.PROVIDERS or zwish.load_providers()):
                        if old.get("name") == p.get("name") and old.get("key"):
                            p = {**old, **{k: v for k, v in p.items() if k != "key"}}
                            break
                try:
                    r = zwish.test_provider(p)
                    return self._json(r)
                except Exception as e:
                    return self._json({"ok": False, "error": str(e)})
            if path == "/api/save_providers":
                zwish.save_providers(_merge_provider_keys(b.get("providers", [])))
                return self._json({"ok": True, "providers": _mask_providers(zwish.PROVIDERS)})

            # ── 内置提示词自定义（DS 网页 / API / 云端共用）──
            if path == "/api/save_prompts":
                import zprompts
                saved = zprompts.save_overrides(b.get("prompts", {}))
                return self._json({"ok": True, "custom": list(saved.keys())})

            if path == "/api/save_webchat_channels":
                # 网页自配置：整体保存自定义网页通道列表（含校验）
                import zwebchat
                ok, msg, cleaned = zwebchat.save_user_channels(b.get("channels"))
                if not ok:
                    return self._json({"ok": False, "error": msg})
                return self._json({"ok": True, "msg": msg, "channels": cleaned})

            if path == "/api/open_folder":
                try: os.startfile(OUTDIR)
                except Exception as e: return self._json({"ok": False, "error": str(e)})
                return self._json({"ok": True})

            # ── 元宝通道：打开登录窗口（本地实验通道，见 issue #14）──
            if path == "/api/yuanbao_login":
                try:
                    import zyuanbao
                    _, ws, _tid = zyuanbao.open_login_window()
                    try:
                        ws.close()
                    except Exception:
                        pass
                    return self._json({"ok": True,
                                       "msg": "已打开元宝登录窗口，请在其中用微信/手机/QQ 登录"})
                except Exception as e:
                    return self._json({"ok": False, "error": str(e)})

            # ── 自定义 AI 网页通道（issue #23）：登录窗口 / 登录态探测 ──
            if path == "/api/webchat_login":
                try:
                    import zwebchat
                    cid = str(b.get("id") or "")
                    _, ws, _tid = zwebchat.open_login_window(cid)
                    try:
                        ws.close()
                    except Exception:
                        pass
                    return self._json({"ok": True, "msg":
                        f"已打开 {zwebchat.channel_name(cid)} 登录窗口，"
                        "请在其中登录一次（登录态自动复用）"})
                except Exception as e:
                    return self._json({"ok": False, "error": str(e)})
            if path == "/api/webchat_status":
                try:
                    import zwebchat
                    return self._json(zwebchat.login_state(str(b.get("id") or "")))
                except Exception as e:
                    return self._json({"error": str(e)})

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
                        _cache_desc(key, desc)
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
                    _cache_desc(key, d["image_desc"])
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
                _delete_history_entry(ts, image)
                return self._json({"ok": True})
            if path == "/api/clear_history":
                _write_history_lines([])
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
