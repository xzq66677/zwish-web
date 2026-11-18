# zwish-web · 许愿式生图向导

> 一个跑在本机的「许愿式」AI 生图工作台：你只用说一句模糊的愿望，向导会追问几个关键问题、合成精修提示词、调用本地 ComfyUI 出图，之后还能用自然语言多轮微调（自动判断整图改还是蒙版局部改）。
>
> 特点：**提示词润色支持本地 / DeepSeek API / DeepSeek 网页多通道**；**对话用中文，真正生图用英文提示词**；看图与点评始终走本地视觉模型，图片不出本机。

> [!TIP]  
> **第一次使用？先看 👉 [DEPLOYMENT.md（配置说明 + 本地模型部署 + 下载链接 + 一键安装脚本）](DEPLOYMENT.md)** —— 本文下面的「快速开始」只讲怎么跑起来，模型的下载、显存要求、逐项配置都在这份文档里。
>
> **想一键装好依赖和模型？** 终端运行 `python install.py`，脚本会列出要下载的东西、实时显示进度/速度/剩余时间。详见 [DEPLOYMENT.md 的一键安装脚本节](DEPLOYMENT.md#一一键安装脚本installpy)。

## 界面预览

| 许愿 → 追问 | 中英双字段精修卡片 |
| ------- | --------- |
| 许愿追问    | 精修卡片      |

| 云端供应商设置 | 内置提示词自定义 |
| ------- | -------- |
| 供应商设置   | 提示词设置    |

> 截图为界面演示（对话内容为示例数据）。完整主界面见 [docs/screenshots/01-main.png](docs/screenshots/01-main.png)；追问问题数的「跟随模型」态见 [06-ask-count.png](docs/screenshots/06-ask-count.png)、「⚡ 一次问清」态见 [07-ask-once.png](docs/screenshots/07-ask-once.png)。

---

## 功能一览

- **许愿式向导**：愿望 → 追问 → 精修 → 生成 → 无限轮对话微调；**追问数量三态可选**：🎯 跟随模型（本地少问 / 云端多问）、⚡ 一次问清（不再精修，用足上下文把细节一次问透）、手动 1–8
- **中英双字段精修**：对话区展示中文说明 `prompt_cn`，真正送进生图模型的是结构化英文词 `prompt_en`
- **多润色通道**（可随时用顶部标签切换）：
  | 通道       | 说明                                                  | 是否需要联网       |
  | -------- | --------------------------------------------------- | ------------ |
  | 自由模式     | 本地模型，忠实执行你的想法                                       | 否            |
  | 精细润色     | 本地 `qwen3:8b`，润色更细腻、更有导演感                           | 否            |
  | DS · API | DeepSeek 官方 API，**内置提示词**                           | 是（需 API Key） |
  | DS · 网页  | CDP 控制本机浏览器访问 `chat.deepseek.com`，**零额外依赖 + 内置提示词** | 是（需登录一次）     |
  | 元宝 · 网页 | CDP 控制本机浏览器访问 `yuanbao.tencent.com`，**独立 profile + 内置提示词**（备用通道） | 是（需登录一次）     |
  | ☁️ 云端    | 设置面板里任意 OpenAI/Anthropic 兼容供应商                      | 是            |
- **生图三模式**：文生图 / img2img 整图改 / SAM 3 自动蒙版局部重绘
- **看图点评**：本地视觉模型看着图给建议，一键应用
- **历史记录**：提示词 / 参数 / 种子 / 耗时落盘，可载入继续改、复制、删除
- **隐私**：服务只绑定 `127.0.0.1`；密钥、登录态、历史图片全部 gitignore，不进仓库

---

## 目录结构

```
zwish-web-github/
├── server.py               # Web 服务（stdlib http.server，无第三方依赖）
├── static/index.html       # 前端单页
├── zwish.py                # 向导核心：追问/精修/重写/规划 + 多通道路由
├── zimg.py                 # 生图 CLI：文生图/img2img/SAM3 inpaint
├── zprompts.py             # 内置提示词模板（多通道共用，可被用户覆盖）
├── zds.py                  # DeepSeek 网页自动化（CDP 控制本机浏览器，零依赖，按需）
├── zconfig.py              # 唯一的路径/模型配置来源
├── install.py              # 懒人一键安装向导（列清单 + 进度/速度/断点续传）
├── config.example.json     # 配置模板 → 复制为 config.json
├── providers.example.json  # 云端供应商模板 → 复制为 providers.json
├── prompts.example.json    # 内置提示词自定义模板 → 复制为 prompts.json（可选）
├── DEPLOYMENT.md           # 配置说明 + 本地模型部署 + 下载链接
├── data/                   # 运行数据（已 gitignore）
└── README.md
```

---

## 环境要求

- **生图**：本地 ComfyUI（含 Z-Image Turbo / Qwen3 文本编码器 / Z-Image VAE / SAM 3.1），服务地址 `127.0.0.1:XX`（XX 为可配置端口，见 `DEPLOYMENT.md`）。首次运行会自动拉起 ComfyUI。
- **对话精修**：本地 [Ollama](https://ollama.com/)（默认 `127.0.0.1:11434`），至少有一个模型。
- **看图 / 点评**：Ollama 里的多模态模型（默认 `gemma3:4b`，与安装脚本一致，可配）。
- **Python 3.9+**，Web 服务本身只用标准库。

> 各模型的**推荐版本、显存要求与下载链接**，见 **[DEPLOYMENT.md](DEPLOYMENT.md)**。

「DS · 网页」通道**零额外依赖**：不需要 `pip install playwright`，也不需要下载 chromium。它通过 Chrome DevTools Protocol（CDP）直接控制你**本机已装的 Chrome / Edge**，WebSocket 通信用 Python 标准库实现。

> 只要本机装了任意一款 Chrome / Edge / Chromium 即可（`config.json` 里 `ds_web_browser: "auto"` 自动探测，也可指定 `chrome` / `msedge` / `chromium` / 自定义 exe 路径）。脚本会直接告诉你检测到哪个浏览器。没有浏览器时安装一个即可，无需装任何 Python 自动化库。

---

## 快速开始

### 1. 配置

```bash
cp config.example.json config.json
```

编辑 `config.json`，把 `base_dir` 指向**包含 `ComfyUI_windows_portable` 文件夹的那一层目录**，其余按需修改。逐项说明见 [DEPLOYMENT.md](DEPLOYMENT.md#六configjson-逐项说明)。

> 也可用环境变量 `ZWISH_CONFIG` 指向任意配置文件，或用 `ZWISH_BASE` 覆盖 `base_dir`。

### 2. 启动网页向导

```bash
python server.py
```

浏览器自动打开 `http://127.0.0.1:<port>`（默认 8189）。加 `--no-open` 可不自动开浏览器。

### 3. 许个愿

在输入框里写一句想画的画面 → 回答 3 个追问 → 点「合成提示词」→ 看中英双字段卡片 → 「开始生成」。生成完可以继续用中文描述想改哪里。

### 命令行用法

```bash
# 许愿向导（交互）
python zwish.py "一只戴宇航员头盔的柴犬在冲浪"

# 向导 JSON 模式（给脚本/agent 调用）
python zwish.py questions "愿望" --json
python zwish.py prompt "愿望" --answers "写实;夕阳光" --json
python zwish.py plan "改成水彩风" --current "当前提示词" --json

# 直接生图（prompt 建议英文）
python zimg.py "a cozy cabin in foggy mountains, photorealistic" --w 768 --h 1024 --seed 42
python zimg.py --img photo.png "change to watercolor style" --denoise 0.55
python zimg.py --img photo.png --segment "cat" "a white kitten in a spacesuit"
```

---

## DeepSeek 通道配置

### DS · API（推荐）

1. 网页右上角 **⚙️ 设置** → 点「⚡ 一键预置 DeepSeek」→ 填入你的 DeepSeek **API Key** → 保存。
   - 或复制 `providers.example.json` 为 `providers.json`，填好 `key` 字段。
2. 顶部选「DS · API」。此时追问 / 精修 / 规划走 DeepSeek，并使用 `zprompts.py` 里的**内置提示词**；看图与点评仍走本地模型。

### DS · 网页（无需 API Key，用网页登录态）

1. 确保本机已装 Chrome / Edge（零额外依赖，无需安装任何 Python 库）。
2. 顶部选「DS · 网页」，首次会弹出浏览器 → 登录 `chat.deepseek.com` 一次。
   - ⚠️ **注意**：程序弹出的浏览器使用独立的配置目录 `data/ds_profile`，与你日常使用的浏览器**登录态互不相通**——请务必在**程序弹出的那个新窗口**里登录，而不是你平时用的浏览器。登录一次后自动复用，无需重复登录。
   - 未登录时程序会快速检测并给出明确提示（不再长时间等待）；登录成功后浏览器进程常驻复用，后续对话不会反复弹窗重启。
3. 登录态保存在 `data/ds_profile/`（已 gitignore），之后自动复用。
4. 该通道把内置提示词随任务一起发给网页对话，效果与 API 通道等价。若 DeepSeek 网页改版导致读不到输入/回复，在 `config.json` 的 `ds_web_selectors` 里更新选择器即可，无需改代码。

### ☁️ 云端（通用）

设置面板可添加任意 OpenAI 兼容（`/chat/completions`）或 Anthropic（`/v1/messages`）供应商，支持多个、启停切换、一键测连接。顶部选「☁️ 云端」并在下拉框选模型。

### 元宝 · 网页（备用通道，无需 API Key）

DeepSeek 网页额度用尽时，可手动切换到腾讯元宝网页通道（Issue #14）。这是一条**备用**通道，不自动轮换账号、不绕过任何平台限额或登录要求。

1. 确保本机已装 Chrome / Edge（与 DS·网页共用同一套 CDP 基础设施，零额外依赖）。
2. 顶部选「元宝 · 网页」。首次使用会弹出浏览器，请在**程序弹出的窗口**里用微信扫码 / 手机 / QQ 登录一次。
   - 该窗口使用独立配置目录 `data/yuanbao_profile`，与 `data/ds_profile` 和你的日常浏览器**完全隔离**。
   - 也可先手动打开登录窗口：`POST /api/yuanbao_login`。
3. 登录态保存在 `data/yuanbao_profile/`（已 gitignore），之后自动复用；浏览器进程常驻。
4. 文字提示词会发送到腾讯元宝；**图片与看图点评仍走本地视觉模型，不上传**。
5. 元宝网页改版导致定位失败时，在 `config.json` 的 `yuanbao_web_selectors` 更新 `composer` / `send` / `answer` 即可，无需改代码。

> 与 DS·网页共用 `zds._WEB_LOCK`，两条网页通道串行执行，不会同时操作同一个浏览器。使用前请自行确认腾讯元宝的数据政策与使用条款。

### 自定义内置提示词（DS 网页 / API / 云端共用）

点右上角 **⚙️ 设置 → 📝 内置提示词**，可直接编辑注入到 **DS · 网页 / DS · API / ☁️ 云端** 通道的每一段 system 模板（追问、精修、重写、改图追问、微调规划、看图描述、看图点评，以及 DS 网页的包裹语）。改完点「保存提示词」立即生效；某段留空或点「恢复此条 / 全部恢复默认」即回退到出厂文本。

- 也可以手动复制 `prompts.example.json` 为 `prompts.json`，只填想覆盖的段落（值为字符串，留空即默认）。`prompts.json` 已 gitignore。
- 说明：本地「自由模式 / 精细润色」两条通道同样走 `zprompts.py`，因此自定义对它们也生效——一处修改，所有通道统一。
- 想恢复出厂提示词：把 `prompts.json` 删掉，或在面板里点「♻️ 全部恢复默认」。

---

## 中英双字段是怎么工作的

精修模型被要求同时输出：

```json
{
  "prompt_cn": "戴宇航员头盔的柴犬在海边冲浪，夕阳光，皮克斯风格",
  "prompt_en": "a shiba inu wearing an astronaut helmet surfing on the beach, golden hour lighting, pixar style, ...",
  "w": 768,
  "h": 1024
}
```

- `prompt_cn` 只用于对话区展示和历史记录，方便你阅读修改。
- `prompt_en` 才是真正送进 ComfyUI `CLIPTextEncode` 的生图提示词。
- 老模型若只回了中文 `prompt`，程序会原样带走（Qwen 文本编码器也支持中文），不会报错。

---

## 隐私与安全

- Web 服务只绑定 `127.0.0.1`，不对局域网/公网开放。
- 以下文件含敏感信息或个人配置，**全部已 gitignore，绝不入库**：`config.json`、`providers.json`（API Key）、`prompts.json`（自定义提示词）、`cloud.json`、`data/`（含 DeepSeek 登录态）、`history.jsonl`、`*.png`。
- **看图与点评永远使用本地视觉模型**，图片不会上传到 DeepSeek 或任何云端。只有文字提示词会发给所选的云端/DeepSeek 通道。
- 使用云端 / DeepSeek 通道前，请自行确认所选模型服务商的数据政策。

---

## 常见问题

- **提示「未找到 ComfyUI 目录」**：检查 `config.json` 的 `base_dir` 是否指向含 `ComfyUI_windows_portable` 的那一层。
- **ComfyUI 端口打不开**：确认 ComfyUI 已启动且实际端口与 `comfy_port` 配置一致；`zimg.py` 通常会自动拉起，也可手动启动后重试。
- **DS 网页通道显示未就绪**：检查本机是否装了 Chrome / Edge（无需安装 playwright 或下载 chromium）；报错提示会写明检测到哪个浏览器。首次需手动登录一次。
- **设置面板提示 `Failed to fetch`**：说明前端没连上后端。请用 `python server.py` 启动服务，并在浏览器打开 `http://127.0.0.1:端口/`（端口见 `config.json`）；不要直接双击打开 `static/index.html`。
- **DS API 报「API Key 为空」/「未找到供应商」**：到设置面板一键预置并填 Key，或检查 `providers.json`。
- **DS 网页读不到回复**：DeepSeek 页面结构可能变化，在 `config.json` 的 `ds_web_selectors` 更新选择器。
- **一张图里有多个人物时效果偏弱**：这是 **Z-Image-Turbo 8 步蒸馏模型的固有局限**（官方标注 Diversity=Low，非代码缺陷；本项目工作流参数已符合官方推荐）。用"1–2 个主体 + 场景关系"的提示词、批量多出几张挑选、或先生成单人再局部加人，可明显改善。完整原因与 4 种应对方案见 [DEPLOYMENT.md 第十一节](DEPLOYMENT.md#十一已知限制一张图里有多个人物时效果偏弱)。

---

## 变更记录

### 2026-11-18 · 英文提示词契约（enforce_english_prompt）

**真机实测发现**：元宝通道对"改成手绘线稿风"这类修改反馈，会返回合法 JSON 但把 `prompt_en` 写成整段中文（如"基于输入图进行风格重绘：保留猫娘少女…"），完全无视模板里的 English 要求；系统随后拿中文词直接重画——用户看到的是"没给提示词，直接重新生图"。另有一条路径：修改请求被回答成对话式"帮你重新生成"，`action=regenerate` 且不带新提示词，同样按旧词重画。

**修复（三层）**：

- **模板加固**（`zprompts.py`）：`plan` 模板明确 `regenerate` 仅限"用户只想再随机来一张且未提修改点"；用户提出任何具体修改点时禁止 regenerate；`prompt_en` 标注"必须纯英文"；`img2img` 不输出 segment。
- **运行时守卫**（`zwish.py`）：新增 `_en_violation` / `_retry_english` / `_guarded_parse`。精修、重写、微调规划三条链路中，`prompt_en` 缺失或含中文时自动带纠错指令重试一次；仍失败则抛出明确错误，**绝不静默拿中文/旧词生图**。`action=regenerate` 不带新提示词视为合法重掷（前端自动沿用当前英文词换种子），不误伤。可用 `config.json` 的 `enforce_english_prompt: false` 关闭（恢复旧的中文直通行为，照顾只会输出中文的本地模型）。
- **通道硬约束**（`zyuanbao.py`）：每条消息前置通道契约（纯文本通道身份、禁止承诺生成图片、`prompt_en` 必须 English only、修改点禁止 regenerate），并在消息末尾重复强调——对弱指令跟随的网页模型更有效。

**回归**：`tests/test_prompt_enforcement.py`（26 项：中文纠正、重试耗尽报错、合法重掷不误伤、开关关闭回退、契约注入、模板加固），全仓 152/152 通过。

---

### 2026-11-16 · 本机请求代理与启动器修复

本轮集中修复了 Windows 环境下三个相互独立的问题，均带回归测试（共 22 项断言全部通过）。

**#1 — 系统代理开启时，本机服务请求被代理返回 404**

- **现象**：Windows 开启系统代理且绕过列表未含 loopback 时，`urllib` 读取 WinINET 代理配置，把发往 `127.0.0.1` / `localhost` / `::1` 的 Ollama、ComfyUI、CDP 请求也转发给代理，导致「自由模式」追问、生图、看图点评全部 404。
- **修复**（`zconfig.py` / `zwish.py` / `zimg.py` / `zds.py`）：新增无代理 opener `LOCAL_OPENER` 与 `urlopen_local()`，所有本机请求强制直连；云端 API 与模型下载仍走默认 opener，遵循系统代理，**不受影响**。未使用全局 `install_opener`，避免误伤云端。
- **回归**：`tests/test_loopback_no_proxy.py`（7/7）。

**#2 — 首次生图自动启动 ComfyUI 后卡住**

- **现象**：`ensure_server()` 用 `capture_output=True` 启动 PowerShell，其拉起的 ComfyUI 长期进程继承了 stdout/stderr 管道句柄，`subprocess.run()` 等不到 EOF 而一直阻塞。
- **修复**（`zimg.py`）：启动器输出改写到普通日志文件句柄（非管道），既不卡在等 EOF，又保留了启动失败时的错误信息。
- **回归**：`tests/test_ensure_server_no_hang.py`（5/5）。

**#3 — 代码审查发现的边界遗漏**

- **现象**：通用云端通道（`#1` 之外）的 `base` 若被配置成本机 OpenAI 兼容服务（如 `127.0.0.1:1234` 的 LM Studio），原先仍走系统代理，会复现与 #1 相同的 404。
- **修复**（`zconfig.py` / `zwish.py`）：新增 `urlopen_auto()`，按目标主机自动选路——本机 loopback 直连、公网地址走代理；`llm_cloud()` 的两处请求改用此函数，两种场景都正确。同时修正了 `no_proxy` 相关注释（Windows 浏览器主要读注册表，不依赖该环境变量）。
- **说明**：经实测，Python `urllib` 的 `no_proxy` 不支持 CIDR（`127.0.0.0/8`）或 `127.` 前缀写法；主修复不依赖 `no_proxy`，故未采用该兜底方案。
- **回归**：`tests/test_urlopen_auto.py`（10/10）。

> 运行全部回归：
> ```bash
> python tests/test_loopback_no_proxy.py
> python tests/test_ensure_server_no_hang.py
> python tests/test_urlopen_auto.py
> ```

---

### 2026-11-17 · 元宝网页备用通道与 Windows 测试入口修复

**#14 / PR #16 — 新增「元宝 · 网页」备用通道**：DeepSeek 网页额度用尽时的手动备用通道（用户主动选择，不自动轮换账号、不绕过任何限额）。复用 `zds.py` 的 CDP 基础设施（浏览器探测 / WebSocket / helper 注入 / 两把锁），独立 `data/yuanbao_profile` 登录态与选择器；文字提示词发往腾讯元宝，图片与看图点评仍走本地。新增 `zyuanbao.py`、`/api/yuanbao_login`、前端标签与路由、`config.example.json` 配置项，回归 `tests/test_yuanbao_web.py`（25 项：profile 隔离、锁复用、会话清理、Quill 填充、发送状态、桩浏览器复用与无标签泄漏）。合并后已实证全仓 143/143 通过。

**#15 — 统一测试入口在 Windows 默认控制台崩溃**：`tests/run_all.py` 原先打印 `▶` / `✓`，在 GBK(cp936) 控制台抛 `UnicodeEncodeError`。修复用三重保险：输出前缀改 ASCII（`>` / `[OK]`）、stdout/stderr 强制 UTF-8 + `errors=replace`、子进程固定 `PYTHONIOENCODING=utf-8`；并让入口跳过会反向调用自身的元测试以避免递归。回归 `tests/test_run_all_windows.py`（7 项，含 GBK 环境实证退出码 0）。

---

### 2026-11-17 · 代码审查第二轮：并发之外的安全边界与健壮性

针对外部审查发现的问题逐条修复（其中并发缺陷已在上一轮 PR #11 解决），本轮带回归 `tests/test_review_fixes.py`（24 项），全仓测试 90/90 通过。

- **🔴 esc() 属性逃逸**（`static/index.html`）：`esc()` 只转义 `& < >`，而它被用在 `data-o="..."`、`onclick="fn('${x}')"` 等属性上下文，LLM 返回的追问选项属不可信输入，`'" onmouseover=…'` 可逃逸属性。修复：`esc()` 补上 `"` → `&quot;`、`'` → `&#39;`，并把画廊 `showLb`/`src`、删除按钮 `delImage` 的参数改为走 `esc()`。
- **🔴→已修 并发单例**：审查指出的 8 线程创建 8 个浏览器实例、误关他人标签等问题，上一轮已用 `_WEB_LOCK` + `_BROWSER_LOCK` 两把锁解决（见 2026-11-17 上一节），本轮回归 `test_ds_web_realenv.py` 覆盖。
- **🟡 历史文件并发写**（`server.py`）：`/api/delete_history` 原先全量读 + `"w"` 覆盖写，与 `zimg` 子进程的追加写并发时会丢记录/读到半行 JSON。改为临时文件 + `os.replace` 原子替换，删除逻辑抽为 `_delete_history_entry()`。
- **🟡 缓存无界增长**（`server.py`）：`JOBS` 只增不删（长驻内存泄漏）、`DESCRIBE_CACHE` 无上限。现在 `start_job` 仅保留最近 50 个已结束任务，描述缓存上限 200 条、淘汰最早写入。
- **🟡 前后端超时错配**（`zds.py`）：前端 180s 放弃后，后端最坏还要跑 240+240s，占用浏览器且用户重试会叠加请求。现在 `web_chat` 默认 `timeout=150`、登录阶段封顶 30s，最坏总耗时 ≈180s，与前端对齐。
- **💭 loopback 误判**（`zconfig.py`）：`host.startswith("127.")` 会把 `http://127.0.0.1.evil.com/` 判为本机而绕过代理。改用 `ipaddress.ip_address(host).is_loopback` 精确判断，域名字符串一律不视为本机。
- **审查遗留项已全部落地（同日第二轮后续）**：
  - **API Key 脱敏 + 来源校验**（`server.py` / `static/index.html`）：`/api/config`、`/api/providers` 不再把 key 明文回传浏览器，只回 `has_key` 标志；设置面板 key 输入框显示"已配置（留空保持不变）"，保存时 key 留空=沿用旧 key（`_merge_provider_keys` 回填）。所有请求校验 Host 头（拦 DNS rebinding），POST 请求校验 Origin（拦外部页面跨源篡改配置/触发删除）。`/api/test_provider` 按 名称 回填真实 key 后再测试。
  - **上传校验**：`/api/upload` 限 25MB + PNG/JPEG/WEBP/GIF 魔数校验。
  - **报错可见**：`load_providers()` 解析失败不再静默返回空列表，打印警告到 stderr。
  - **统一测试入口**：`python tests/run_all.py` 一键跑全量（当前 8 个文件、101 项断言）。
  - 回归：`tests/test_security_fixes.py`（21 项）。
- **审查未采纳/留待后续**：Windows 真机验证「ComfyUI 未启动时生成」不卡 900s（见 Issue #12）。

> 运行本轮回归：
> ```bash
> python tests/test_review_fixes.py
> ```

---

### 2026-11-17 · DS·网页通道体验优化与真实环境回归修复

其中 #9/#10 来自真机实测发现的真实回归，本轮改动全部带回归测试（`test_ds_web_reuse.py` 14 项、`test_ui_prompt_actions.py` 10 项、`test_ds_web_realenv.py` 20 项）。

**#6 — 未登录时卡「正在理解你的愿望…」240 秒，且每次请求重启浏览器**

- **现象**：DS·网页通道未登录时，自动化脚本一直等不到回复，前端无超时提示；且每次调用都冷启动浏览器，重复登录态加载，单次请求可达数分钟。
- **修复**（`zds.py` / `static/index.html`）：① 检测到未登录状态约 15 秒内快速失败并提示；② Chrome 实例常驻复用，会话结束不关闭浏览器；③ 前端 `api()` 加 AbortController 180 秒超时兜底。
- **回归**：`tests/test_ds_web_reuse.py`。

**#5 — 「💬 交流修改」提升为主操作**

- **说明**：多数用户拿到润色结果后的第一反应是"用中文说哪里要改"，而非先翻译成英文。将「交流修改」提升为卡片主按钮，「开始生成」次之，「手动改英文词」降级为文字链接；点击输入框自动聚焦，轮播图场景可用 `noFocus` 关闭。

**#9/#10 — 真机实测发现的三个并发与页面结构回归**

- **现象 1（helper 失效）**：CDP 注入的 `window.__zfind` 助手函数在客户端导航（如登录跳转）后随 JS 上下文重建而丢失，登录检测在旧引用上空转，请求挂满 240 秒。
- **现象 2（发送控件）**：DeepSeek 网页的发送按钮是 `div[role="button"]` 而非原生 `<button>`，且 React 受控组件对合成 `KeyboardEvent` 回车不响应，消息实际未发出。
- **现象 3（并发串线）**：多个并发请求共享同一个 `target_id` / WebSocket，先结束的请求会关掉后请求正在使用的标签，造成误关与标签泄漏。
- **修复**（`zds.py`）：① 每次 `Runtime.evaluate` 都把 helper 源码与表达式拼在同一次求值里，天然免疫导航丢失；② `_SEND_SELS` 补充 `div[role="button"]`（含"发送"文案 / aria-label / aria-disabled 变体），并新增兜底遍历输入框 4 层父级内的所有可点击节点；③ 会话改用独立的浏览器单例锁 + 会话锁两把锁——`ThreadingHTTPServer` 下的并发请求按会话串行执行，标签随会话在 `finally` 中显式清理，不再泄漏。
- **回归**：`tests/test_ds_web_realenv.py`（含 8 线程并发单例与标签无泄漏断言）。

> 运行全部回归：
> ```bash
> python tests/test_ds_web_reuse.py
> python tests/test_ui_prompt_actions.py
> python tests/test_ds_web_realenv.py
> ```

---

## 免责声明与使用规范

### 作者免责

本工具为**个人学习与技术实验性质的开源项目**，按「现状」提供，不附带任何明示或暗示的保证（包括但不限于适销性、特定用途适用性、不侵权保证）。

- 作者仅提供代码与技术手段，**不参与、不指导、不鼓励任何具体内容的创作**，对使用者利用本工具生成、传播的任何内容**不承担任何责任**。
- 因使用、修改、分发本工具，或因使用本工具生成的内容而产生的**任何直接或间接损失、法律责任、纠纷、行政处罚或第三方索赔**，均由使用者自行承担，与作者无关。
- 本工具依赖的第三方模型、服务（ComfyUI、Ollama、DeepSeek、Hugging Face 上的各模型等）各有其独立许可与条款，**其合规性与使用后果由使用者自行判断和承担**，作者不对第三方内容或服务作任何背书。
- 作者保留随时修改、暂停或终止本项目的权利，无义务为任何特定用途维护或支持。

### 创作者自我规范（重要）

技术是中性的，但使用技术的人有责任。**作为创作者，请你严格自律：**

1. **遵守法律**：不得利用本工具生成违反所在地法律法规的内容，尤其严禁涉及未成年人、暴力、恐怖、歧视、侵权等违法违规内容。
2. **尊重他人**：不得生成冒犯、诽谤、骚扰他人，或侵犯他人肖像权、名誉权、隐私权的内容；不得冒充真实个人。
3. **尊重知识产权**：注意训练数据与生成内容的版权风险，不得用于侵犯他人著作权、商标权或制造虚假信息。
4. **年龄分级自觉**：本工具内置的所谓"自由模式"仅是模型技术特性，并非内容许可；对生成内容你负有完全的审查与分级责任，**不得向未成年人传播不适宜内容**。
5. **平台合规**：若将生成内容发布到任何平台，请遵守该平台的内容政策与标注要求。
6. **自我审查义务**：生成前后请自行审阅内容，对不适宜公开或传播的内容主动停止扩散。你是对自己作品负责的第一责任人。

> 简言之：**能力越大，自律越强。** 请始终在法律与道德的框架内使用本工具，把创作自由建立在自我约束之上。

### 许可证

自用工具，欢迎 fork 改造，但 fork 与二次分发同样须遵守上述免责与使用规范，并保留本声明。
