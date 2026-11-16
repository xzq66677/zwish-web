# 配置说明与本地模型部署指南

本文说明 `config.json` 每一项的含义与推荐值，并给出这套工作流所需的**四类本地模型**的最优选择、显存要求和官方下载链接。

> 下载链接均为官方 / 官方合作镜像（Hugging Face `Comfy-Org`、`Tongyi-MAI`、`facebook`，Ollama 官方库）。模型体积较大，建议用支持断点续传的下载工具。

---

## 一、一键安装脚本（install.py）

不想手动一个个下载？终端进到项目目录，运行：

```bash
python install.py
```

脚本会**先体检、再列清单、逐项执行**，每步都实时显示：进度条、百分比、已下/总大小、**当前速度**、**剩余时间**，并且**支持断点续传**（中断后重跑会接着下）。

**先体检（不下载不改动）**：
```bash
python install.py --check
```

**常用参数**：

| 命令 | 作用 |
|---|---|
| `python install.py` | 交互模式：先列清单，每一步问你装不装 |
| `python install.py --check` | 只体检，看看哪些装了、哪些缺 |
| `python install.py --yes` | 全自动：所有可自动项不再逐项询问 |
| `python install.py --only models` | 只装某一阶段：`config/python/browser/ollama/comfy/models`（逗号可组合） |
| `python install.py --mirror https://hf-mirror.com` | 模型走国内镜像（国内网络下载 HF 慢时用） |
| `python install.py --skip-download` | 只生成配置模板和目录，不下载大文件 |

**关于下载策略（重要）**：

- **ComfyUI / Ollama 这类系统级软件**：脚本会检测，**不会擅自装到你的系统**。缺了会给出官方下载链接（[ComfyUI 便携版](https://github.com/comfyanonymous/ComfyUI/releases) / [Ollama](https://ollama.com/download)），你装好后重跑脚本会自动继续。这是因为它们需要管理员权限、解压缩和改 PATH，交给你确认更稳妥。
- **模型文件（HF）**：可以自动下载。四个模型文件共约 10 GB，脚本逐个显示进度和速度；DS 网页通道零额外依赖，**不会安装 playwright 或下载 chromium**。
- **浏览器（DS 网页通道）**：脚本优先检测本机 Chrome/Edge 并直接复用，**无需下载 chromium**（见 6.4 节）。

> 脚本只用 Python 标准库，运行前不需要额外安装任何东西；不上传任何信息。若某次下载中断，直接重跑同一条命令即可续传。

---

## 二、这套工作流需要哪些模型

整个系统由**两套引擎**组成，各管一类模型，不要混用：

| 引擎 | 负责 | 模型角色 | 是否必须 |
|---|---|---|---|
| **ComfyUI** | 真正的生图 | ① 扩散模型 Z-Image Turbo<br>② 文本编码器 Qwen3-4B<br>③ VAE（ae）<br>④ SAM 3.1（自动蒙版，局部重绘用） | ①②③ 必须；④ 仅局部重绘需要 |
| **Ollama** | 对话式提示词精修 / 看图 | ⑤ 自由模式模型（abliterated）<br>⑥ 精细润色模型（qwen3:8b）<br>⑦ 多模态看图模型（gemma3/gemma4） | 至少一个 ⑤或⑥；⑦ 用于看图点评 |

**显存预算提示**：ComfyUI 与 Ollama 是**分开跑**的。一份 8GB 显存的显卡，建议让 ComfyUI 独占显存生图，Ollama 模型按需加载（用完可用 `ollama stop` 释放）。`zimg.py` 会在 ComfyUI 未运行时自动拉起它。

---

## 三、硬件要求与分档配置

### 1. 最低配置（能跑起来）

| 部件 | 最低要求 | 说明 |
|---|---|---|
| **显卡** | NVIDIA 6GB 显存（如 RTX 2060 6G / 3060 6G） | 必须用 int8 / fp8 量化模型 + 小尺寸出图（≤1024×1024）；SAM 3.1 局部重绘建议关掉或换 INT4 版 |
| **内存** | 16GB | 低于此值量化模型加载会大量换页、明显变慢 |
| **硬盘** | 至少 25GB 可用 | int8 三件套约 6–8GB + Ollama 模型 + 输出图 |
| **系统** | Windows 10/11（ComfyUI 便携版）或 Linux | 需支持 CUDA 的 NVIDIA 驱动；Web 服务本身跨平台 |
| **Python** | 3.9+ | Web 服务只用标准库；仅 DS 网页通道需额外装 Playwright |
| **网络** | 首次需联网下载模型 | 下载完可完全离线生图；只有选云端/DS 通道时才需要网络 |

> 6GB 卡属于"能出图但慢"的档位：把 `zimg.py` 的 steps 保持 8、尺寸 768×1024、别同时开 Ollama 大模型即可。若连 6GB 都没有，建议直接用云端通道（本项目的 DS · API / ☁️ 云端），本地只跑 Web 界面。

### 2. 推荐配置（流畅，本项目的默认基准）

| 部件 | 推荐 | 对应配置 |
|---|---|---|
| **显卡** | NVIDIA 8GB（RTX 3060 12G / 4060 8G / 5060 Laptop 8G 等） | int8 UNet + fp8 文本编码器，`models.unet = z_image_turbo_int8_convrot.safetensors` |
| **内存** | 32GB | 可同时驻留一个 4B/8B Ollama 模型 |
| **硬盘** | NVMe SSD 50GB+ | 模型加载与出图速度差异明显 |

这一档就是第九节「最小可用配置示例」的目标，项目默认值即按此调好，开箱即用。

### 3. 高显存配置（RTX 5090 32GB / 4090 24GB 等）

大显存卡不必再迁就量化模型，换用**全精度权重**能获得更稳定的细节与色彩；同时可以把分辨率、批量、常驻模型数拉满。改 `config.json`：

```json
{
  "models": {
    "unet": "z_image_turbo_bf16.safetensors",
    "clip": "qwen_3_4b.safetensors",
    "vae": "ae.safetensors",
    "sam_ckpt": "sam3.1_multiplex_fp16.safetensors"
  },
  "refiner": "qwen3:14b",
  "fine_model": "qwen3:14b",
  "vision_model": "gemma3:27b"
}
```

| 优化项 | 8GB 基准 | 5090 32GB 建议 | 收益 |
|---|---|---|---|
| 扩散模型 | int8（~6GB） | **bf16 全精度**（12.3GB，`z_image_turbo_bf16.safetensors`） | 细节/色彩更稳，无量化损失 |
| 文本编码器 | fp8（~2.5GB） | **bf16 全精度**（`qwen_3_4b.safetensors`，约 8GB） | 提示词理解更准 |
| 出图尺寸 | 1024×1024 | 1536×1536 甚至 2048×2048 | 直接出高分辨率，省去放大 |
| 批量 / steps | 1 张 / 8 步 | 2–4 张并行 / 8–12 步 | Turbo 模型 steps 不必堆太高 |
| Ollama 精修模型 | 4B | **14B / 32B**，且可多个常驻不释放 | 提示词润色质量提升 |
| 看图点评模型 | 4B | **gemma3:27b** | 点评更细、更懂画面 |
| SAM 3.1 | FP16 | FP16（无需降级 INT8） | 蒙版边缘更准 |

> 说明：
> - bf16 全精度文件名来自官方仓库 `Comfy-Org/Z-Image-Turbo`（[模型目录](https://huggingface.co/Comfy-Org/Z-Image-Turbo/tree/main/split_files/diffusion_models)），下载后放进 `ComfyUI/models/diffusion_models/`；文本编码器 `qwen_3_4b.safetensors` 放进 `text_encoders/`。
> - `zimg.py` 默认按 `config.json` 的 `models.*` 文件名加载，换权重只需改文件名，**不用动代码**。
> - 显存再大也建议 ComfyUI 与 Ollama 分时占用；5090 32GB 可同时驻留多个 Ollama 模型，但生图瞬间仍建议 `ollama stop` 腾出显存给 UNet。

---

## 四、ComfyUI 侧模型（生图）

### 4.1 一键下载仓库（推荐）

阿里官方与 ComfyUI 官方已把 Z-Image Turbo 的全套文件打包好，直接用 Hugging Face 下载：

- **官方打包仓库**：https://huggingface.co/Comfy-Org/Z-Image-Turbo
- **原模型仓库**（Tongyi-MAI）：https://huggingface.co/Tongyi-MAI/Z-Image-Turbo
- **国内加速镜像**（文件相同）：https://huggingface.co/buckets/shamozhiying/z_image_turbo-bucket

从仓库的 `Files and versions` 页下载下列文件，放到对应目录：

```
ComfyUI/
├── models/
│   ├── diffusion_models/
│   │   └── z_image_turbo_int8_convrot.safetensors     ← 8GB 显卡首选（约 6.x GB）
│   ├── text_encoders/
│   │   └── qwen_3_4b_fp8_mixed.safetensors            ← 文本编码器
│   └── vae/
│       └── ae.safetensors                              ← VAE
```

### 4.2 每个文件的选择与说明

| 文件 | 放哪个目录 | 推荐版本 | 说明 |
|---|---|---|---|
| 扩散模型 | `models/diffusion_models/` | `z_image_turbo_int8_convrot.safetensors` | **8GB 显存首选**。另有 `bf16`（约 12GB，需更大显存）和 `nvfp4`（更新架构、需新卡）可选 |
| 文本编码器 | `models/text_encoders/` | `qwen_3_4b_fp8_mixed.safetensors` | Qwen3-4B 的 fp8 量化版；同目录还有 `qwen_3_4b.safetensors`（更大）和 `fp4`（更小） |
| VAE | `models/vae/` | `ae.safetensors` | Z-Image 官方 VAE，必装 |
| SAM 3.1 | `models/checkpoints/` | `sam3.1_multiplex_fp16.safetensors` | 仅「局部重绘 / 自动蒙版」需要；纯文生图可不装 |

> 本项目的 `zconfig.py` 里 `models` 一段默认就填的上面这些文件名。若你下载了别的版本（如 bf16），改 `config.json` 的 `models.unet` 等字段即可。

### 4.3 SAM 3.1（自动蒙版，可选但强烈推荐）

- **官方 FP16（推荐）**：https://huggingface.co/Comfy-Org/sam3.1/tree/main/checkpoints
  - 下载 `sam3.1_multiplex_fp16.safetensors`（约 1.75 GB）→ 放到 `ComfyUI/models/checkpoints/`
- **更小的量化版**（8GB 显卡更省显存）：https://huggingface.co/Sparknight/sam3.1-int8-int4-convrot
  - INT8（约 1.19 GB）/ INT4（约 0.98 GB），ComfyUI v0.27+/v0.28+ 原生支持，需对应改文件名
- 原模型：https://huggingface.co/facebook/sam3.1

### 4.4 ComfyUI 本体

- 官方仓库：https://github.com/comfyanonymous/ComfyUI
- Windows 便携版（解压即用，推荐）：https://github.com/comfyanonymous/ComfyUI/releases
- 官方网站与文档：https://www.comfy.org/ 、https://docs.comfy.org/

> SAM 3 节点可能需要安装 ComfyUI 的自定义节点/SAM3 支持包，按所下载模型页面的说明操作即可。

---

## 五、Ollama 侧模型（对话精修 / 看图）

先安装 Ollama：https://ollama.com/download （Windows / macOS / Linux）。安装后在终端用 `ollama run <模型名>` 下载并试用。

### 5.1 自由模式（忠实执行，abliterated 系列）

这类模型对提示词主题不做限制，忠实还原你的想法，适合创意写作。

| 模型 | 显存/体积 | 推荐场景 | 安装命令 |
|---|---|---|---|
| **Qwen3-4B-Instruct-2507-Abliterated**（推荐） | ~2.5 GB | 8GB 卡首选，非思考型，速度快 | `ollama run richardyoung/qwen3-4b-instruct-2507-abliterated` |
| Qwen3-8B-Abliterated | ~5.0 GB | 显存更充裕、要更强表达力 | `ollama run richardyoung/qwen3-8b-abliterated` |
| Qwen3-Abliterated（多尺寸） | 0.4–9 GB | 从 0.6b 到 14b 任选 | `ollama run jaahas/qwen3-abliterated:4b` |

- Ollama 模型页：https://ollama.com/richardyoung/qwen3-4b-instruct-2507-abliterated
- HF 权重（可复现记录）：https://huggingface.co/richardyoung/Qwen3-4B-Instruct-2507-heretic

### 5.2 精细润色（Qwen3 标准版）

标准 Qwen3 指令模型，润色更有导演感，会对内容做常规规范化。

| 模型 | 体积 | 安装命令 |
|---|---|---|
| **qwen3:8b**（本项目默认） | ~5 GB | `ollama run qwen3:8b` |
| qwen3:4b（更省资源） | ~2.5 GB | `ollama run qwen3:4b` |

- Ollama 官方页：https://ollama.com/library/qwen3

### 5.3 看图 / 点评（多模态视觉模型）

| 模型 | 体积 | 安装命令 | 说明 |
|---|---|---|---|
| **gemma3:12b**（推荐，8GB+） | ~8.2 GB | `ollama run gemma3:12b` | Google Gemma 3，多模态，128K 上下文，视觉质量好 |
| gemma3:4b（显存紧张） | ~3.4 GB | `ollama run gemma3:4b` | 同为多模态，更快 |
| qwen3-vl（视觉版） | 3.3–21 GB | `ollama run qwen3-vl:8b` | 另一选择，中文场景友好 |

- Gemma 3 官方页：https://ollama.com/library/gemma3
- Qwen3-VL 官方页：https://ollama.com/library/qwen3-vl

> 本项目默认 `vision_model` 写的是 `gemma4-cc`（你本机的自定义别名）。如果你按上表装了 `gemma3:12b`，请把 `config.json` 的 `vision_model` 改成 `gemma3:12b`。

### 5.4 装好后核对

```bash
ollama list          # 列出已安装模型，确认名字
```

`config.json` 里的模型名要和 `ollama list` 显示的**完全一致**（含 tag）。`refiner_keyword` 用于在 `ollama list` 结果里自动匹配自由模式模型（默认 `abliterated`）。

---

## 六、config.json 逐项说明

复制 `config.example.json` 为 `config.json` 后，按下面填写。

### 6.1 路径与端口

| 配置项 | 默认值 | 说明 / 推荐值 |
|---|---|---|
| `base_dir` | 空（自动探测） | **必填**。指向含 `ComfyUI_windows_portable` 那一层目录，如 `E:/ComfyUI`。正斜杠或双反斜杠 |
| `port` | `8189` | zwish-web 网页服务端口。被占用时换一个，如 `8190` |
| `comfy_port` | `8188` | ComfyUI 服务端口，需和 ComfyUI 实际端口一致 |

### 6.2 Ollama 地址与模型

| 配置项 | 默认值 | 说明 / 推荐值 |
|---|---|---|
| `ollama` | `http://127.0.0.1:11434` | Ollama 服务地址，本机默认不用改 |
| `refiner` | `auto` | 精修模型。`auto`=按关键词扫描；也可直接写具体模型名，如 `richardyoung/qwen3-4b-instruct-2507-abliterated` |
| `refiner_keyword` | `abliterated` | `auto` 模式下的匹配关键词。装了别的自由模式模型就改这里 |
| `fallback_refiner` | `qwen3:8b` | 扫描不到时的兜底模型 |
| `fine_model` | `qwen3:8b` | 前端「精细润色」通道使用的本地模型 |
| `vision_model` | `gemma4-cc` | 看图/点评的多模态模型。按你实际装的改，如 `gemma3:12b` |

### 6.3 ComfyUI 模型文件名

`models` 下的文件名要和 `ComfyUI/models/` 里实际文件一致（见本文第四节）：

| 配置项 | 默认值 | 对应目录 |
|---|---|---|
| `models.unet` | `z_image_turbo_int8_convrot.safetensors` | `diffusion_models/` |
| `models.clip` | `qwen_3_4b_fp8_mixed.safetensors` | `text_encoders/` |
| `models.vae` | `ae.safetensors` | `vae/` |
| `models.sam_ckpt` | `sam3.1_multiplex_fp16.safetensors` | `checkpoints/` |

### 6.4 DeepSeek 网页通道

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `ds_web_browser` | `auto` | **用哪个浏览器**：`auto`=优先本机 Chrome、其次 Edge、再其次 Chromium（推荐，零下载）；也可填 `chrome` / `msedge` / `chromium` / 自定义 exe 绝对路径 |
| `ds_web_chat_url` | `https://chat.deepseek.com/` | 网页地址，一般不用改 |
| `ds_web_headless` | `false` | **首次必须 false**（要看得见浏览器去登录）。登录成功后可改 `true` 后台运行 |
| `ds_web_selectors` | 见示例 | 网页改版导致读不到输入/回复时，在这里更新 CSS 选择器，不用改代码 |

**关于浏览器（重点）**：本通道通过 **CDP（Chrome DevTools Protocol）** 直接控制你**本机已安装的 Chrome / Edge / Chromium**，用 Python 标准库实现 WebSocket 通信——**不需要安装 playwright，也不需要下载 chromium**。启动时会在终端打印"启动浏览器：本机 Chrome/..."。`auto` 模式下优先 Chrome、其次 Edge、再其次 Chromium；如果本机一个都没有，只需装任意一款浏览器即可，无需安装任何 Python 自动化库。

服务器/无桌面环境：有头模式启动失败时会自动降级为 headless 重试；也可在 `config.json` 直接设 `ds_web_headless: true`。首次使用需在弹出的浏览器里登录一次 deepseek.com，登录态保存在 `data/ds_profile`（已 gitignore），之后自动复用。

> 常见误区：如果前端显示 `Failed to fetch`，那是**后端没连上**，与本通道无关。请用 `python server.py` 启动服务，再通过 `http://127.0.0.1:端口/` 访问，不要直接双击打开 `static/index.html`。

### 6.5 追问问题数（ask_count）

许愿追问和修图追问的问题数量可以**自选**，也可让程序按当前通道**自动推荐**：

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `ask_count.min` | `1` | 允许自选的下限 |
| `ask_count.max` | `8` | 允许自选的上限 |
| `ask_count.wish` | `3` | 许愿追问的默认数量 |
| `ask_count.edit` | `2` | 修图追问的默认数量 |
| `ask_count.local_recommend` | `2` | **本地模型推荐次数**——本地上下文窗口有限，问太多会挤占精修/看图空间，故推荐少问 |
| `ask_count.cloud_recommend` | `4` | 云端 / DS 通道推荐次数——上下文大，可多问几个 |

**前端交互**：侧栏「⚙️ 生成参数 → 追问问题数」有三种互斥模式：

1. **🎯 跟随模型（默认）**：选本地模型（自由模式 / 精细润色）→ 用 `local_recommend`（默认 2）；选 DS · API / DS · 网页 / 云端 → 用 `cloud_recommend`（默认 4）。
2. **⚡ 一次问清（不再精修）**：直接用 `max` 上限（默认 8），**不受模型影响**。适合"只问这一轮、出图即定稿"的场景——既然不再多轮微调，上下文用完即释放，就不会累积撑爆，于是可以放开用足、把细节一次问透。这照顾了大多数没有上下文预算意识的用户。
3. **手动自选**：点 1–8 任一数字，按你选的值走，不受模型切换影响；点上方任一模式按钮可切回自动。

**为什么"跟随模型"下本地要少问、而"一次问清"可以放开**：本地 4B/8B 模型的上下文窗口通常只有几 K～几十 K token，且"自由模式"模型对长指令的稳定性更差。**多轮微调**时，每一轮的追问与回答都会累积进上下文，问得越多越容易稀释最终精修指令、甚至超出窗口导致截断——所以跟随模式下本地推荐 2 个。**一次性出图**则相反：只有一轮问答，用完即弃，没有累积风险，所以可以放心把问题数拉到上限，用一次深度追问弥补"不再精修"的损失。

> 注意：「一次问清」只是把**追问数量**拉满，并不会改变生成后能否继续微调——若你中途改主意想继续改图，照常描述即可（只是此时上下文已经用得较多，建议新开会话重新许愿更稳）。

**提示词如何生效**：数量通过 `{N}` 占位符注入 `zprompts.py` 的追问模板（前端/接口传入后替换）。若在「⚙️ 设置 → 📝 内置提示词」里自定义了追问模板且未写 `{N}`，系统会在末尾自动追加"请恰好提出 N 个问题"的硬约束，同样生效。

### 6.6 环境变量覆盖（无需改文件）

- `ZWISH_CONFIG`：指向任意配置文件路径
- `ZWISH_BASE`：覆盖 `base_dir`

---

## 七、 providers.json（DeepSeek / 云端供应商）

复制 `providers.example.json` 为 `providers.json`，填 API Key：

```json
{
  "providers": [
    {
      "name": "DeepSeek",
      "base": "https://api.deepseek.com",
      "format": "openai",
      "key": "sk-你的真实密钥",
      "models": ["deepseek-chat", "deepseek-reasoner"],
      "on": true
    }
  ]
}
```

- DeepSeek API 控制台（获取 Key）：https://platform.deepseek.com/api_keys
- DeepSeek 模型文档：https://api-docs.deepseek.com/
- `deepseek-chat`：日常润色够用、便宜；`deepseek-reasoner`：更强推理，慢一些。

也可完全用网页的 **⚙️ 设置**面板添加/测试/保存供应商，效果相同。`providers.json` 已 gitignore，不会入库。

---

## 八、prompts.json（内置提示词自定义，可选）

内置提示词默认写在 `zprompts.py` 里。想改它们**不必动代码**——两种方式任选：

1. **网页编辑（推荐）**：右上角 **⚙️ 设置 → 📝 内置提示词**，逐段编辑，支持「恢复此条 / 全部恢复默认」，点「保存提示词」立即生效。
2. **手动文件**：复制 `prompts.example.json` 为 `prompts.json`，只填写要覆盖的段落：

```json
{
  "prompt": "你是生图提示词工程师……（你自己的 system 指令）",
  "ds_header": "【系统指令】"
}
```

可覆盖的 key 共 9 个：

| key | 用途 |
|---|---|
| `questions` | 许愿后的 3 个追问 |
| `prompt` | 愿望 + 回答 → 最终提示词（中英双字段） |
| `rewrite` | 根据建议重写提示词 |
| `edit_questions` | 改图时的 2 个追问 |
| `plan` | 微调规划（inpaint / img2img / regenerate / done） |
| `describe` | 本地视觉：客观描述图片 |
| `critique` | 本地视觉：对比提示词点评图片 |
| `ds_header` | DS 网页通道的「系统指令」包裹语 |
| `ds_user_header` | DS 网页通道的「用户任务」包裹语 |

**规则**：value 必须是字符串；留空（`""`）或不写该 key = 用回默认文本。所有通道（DS 网页 / DS API / 云端 / 本地自由模式 / 本地精细润色）共用这套当前生效提示词，一处修改全部统一。`prompts.json` 已 gitignore，不会入库；删除它即完全恢复出厂提示词。

---

## 九、最小可用配置示例（8GB 显卡）

```json
{
  "base_dir": "E:/ComfyUI",
  "port": 8189,
  "comfy_port": 8188,
  "ollama": "http://127.0.0.1:11434",
  "refiner": "auto",
  "refiner_keyword": "abliterated",
  "fine_model": "qwen3:8b",
  "vision_model": "gemma3:4b",
  "models": {
    "unet": "z_image_turbo_int8_convrot.safetensors",
    "clip": "qwen_3_4b_fp8_mixed.safetensors",
    "vae": "ae.safetensors",
    "sam_ckpt": "sam3.1_multiplex_fp16.safetensors"
  }
}
```

配套要装的模型：

```bash
# ComfyUI：从 Comfy-Org/Z-Image-Turbo 下载 3 个文件 + SAM3.1
# Ollama：
ollama run richardyoung/qwen3-4b-instruct-2507-abliterated   # 自由模式
ollama run qwen3:8b                                           # 精细润色
ollama run gemma3:4b                                          # 看图点评（显存更省）
```

启动：`python server.py`，浏览器打开 `http://127.0.0.1:8189`。

---

## 十、下载链接速查表

| 资源 | 链接 |
|---|---|
| ComfyUI 本体 / Windows 便携版 | https://github.com/comfyanonymous/ComfyUI |
| Z-Image Turbo（官方打包） | https://huggingface.co/Comfy-Org/Z-Image-Turbo |
| Z-Image Turbo（原模型） | https://huggingface.co/Tongyi-MAI/Z-Image-Turbo |
| SAM 3.1（官方 FP16） | https://huggingface.co/Comfy-Org/sam3.1/tree/main/checkpoints |
| SAM 3.1（INT8/INT4 小体积） | https://huggingface.co/Sparknight/sam3.1-int8-int4-convrot |
| Ollama 安装 | https://ollama.com/download |
| Qwen3-4B-Abliterated（自由模式，推荐） | https://ollama.com/richardyoung/qwen3-4b-instruct-2507-abliterated |
| Qwen3-8B-Abliterated | https://ollama.com/richardyoung/qwen3-8b-abliterated |
| Qwen3 标准版（精细润色） | https://ollama.com/library/qwen3 |
| Gemma 3（看图点评，推荐） | https://ollama.com/library/gemma3 |
| Qwen3-VL（看图点评备选） | https://ollama.com/library/qwen3-vl |
| DeepSeek API Key | https://platform.deepseek.com/api_keys |
| DeepSeek API 文档 | https://api-docs.deepseek.com/ |
| DS 网页通道依赖 | 无（CDP + Python 标准库，不需要 playwright / chromium） |
| 本机浏览器（DS 网页通道） | [Chrome](https://www.google.com/chrome/) 或 [Edge](https://www.microsoft.com/edge)（二选一，装了即可被复用） |
| 一键安装向导 | 项目内 `python install.py`（详见本文「一、一键安装脚本」） |

> 链接有效性以访问时为准。若某仓库迁移，以对应官方组织主页为准。

---

## 十一、已知限制：一张图里有多个人物时效果偏弱

这是**模型能力边界，不是本项目的代码缺陷**。本节说明原因、判断依据，以及能实际改善的做法。

### 11.1 结论先行

| 问题 | 判断 |
|---|---|
| 是代码 bug 吗？ | ❌ 不是。`zimg.py` 生成的 ComfyUI 工作流完全符合官方 Turbo 推荐参数（8 步、`cfg=1.0`、`res_multistep`、shift=3），提示词也是完整透传，没有任何截断或降质处理。 |
| 是模型局限吗？ | ✅ 主要是。根源在 **Z-Image-Turbo 这个 8 步蒸馏模型**本身。 |
| 需要改代码吗？ | 不需要改工作流；通过**提示词写法**和**工作流选择**即可明显改善，见 11.3。 |

### 11.2 为什么会这样（判断依据）

1. **模型层面——Turbo 是"快而专"的蒸馏版，多样性天然偏低。** Z-Image 是一个 6B 参数模型家族；官方在 Model Zoo 中把 **Z-Image-Turbo 的 Diversity（多样性）明确标为 Low**，而基础版 Z-Image 为 Medium。Turbo 用 8 步蒸馏换取亚秒级速度，代价之一就是对**多主体、复杂空间关系**的表达能力弱于基础版。官方定位里，Turbo 主打写实、指令遵循、中英文文字渲染；基础版才强调"身份、姿态、构图、布局的高多样性"。
2. **社区实测一致印证。** 多篇实测都指出同一现象：Z-Image 对"多主体复杂交互"存在上限——例如"三个人在咖啡馆，左边穿红衣、中间看手机、右边喝咖啡"这类提示，人物关系容易错位；"五只不同品种的狗"可能数量对但部分糊成色块或缺肢体。普遍建议是**单图聚焦 1–2 个主体**，多主体改用"场景 + 关键特征"的关系描述，而非罗列。
3. **本项目默认提示词策略会放大这个短板。** 内置精修模板（`zprompts.py` 的 `EN_RULE`）引导模型输出 **60–120 词、subject/action/scene/composition/... 八段式**的长结构化提示词。这套写法对**单主体 + 复杂氛围**很好用，但 Z-Image 的提示词习惯恰恰相反——它更吃**短而聚焦**的描述（社区经验值 10–20 词），主体一多、关系一复杂，反而容易"想太多而画乱"。所以多人场景里，越长越细的堆叠未必越好。

> 一句话：**Turbo 用 8 步换速度，牺牲了多主体/复杂构图的多样性；项目默认的长提示词模板又偏"堆细节"，两者叠加，让多人场景的短板更明显。**

### 11.3 能实际改善的做法（按成本从低到高）

**① 改提示词写法（零成本，优先试）**
- **控制主体数量**：一张图尽量 1–2 个主要人物；确有第三人时，用"背景中 another figure in the distance"弱化，而不是并列三个人都当主角。
- **用关系代替罗列**：不要"三个人分别……"，改成"a chef in a white coat in the foreground stirring a pot, warm kitchen behind him"——一个主体 + 场景叙述。
- **像写分镜**：把姿态、位置、互动写成可视化短句；避免"充满误解的紧张谈判"这类抽象心理状态。
- **加构图/视角词**：`wide shot`、`from behind`、`two-shot`、`over-the-shoulder` 等，比堆形容词更能决定画面结构。
- **适当缩短 prompt_en**：多人场景可把 60–120 词压到 40–80 词，删掉与主体无关的修饰词。
- 这些都可以直接在网页「⚙️ 设置 → 📝 内置提示词」里改 `精修提示词` 模板，一处修改、所有通道生效（见第八节）。

**② 用"微调"功能逐个加人（低成本，本项目很擅长）**
先生成**单人**图，再用本项目的 **💬 交流修改 / 局部重绘（SAM3）** 逐个加入第二个主体。项目在"单主体 + 局部编辑"上表现稳定，能绕开一次性生成多人的空间推理难题。

**③ 多出几张挑一张（零成本）**
用侧栏「批量出图」一次出 2–4 张（不同种子），多人场景的构图随机性较大，从中挑选最接近的一张再微调，比死磕单张高效。

**④ 换基础版 Z-Image / 上姿态控制（进阶）**
若"多人精准构图"是刚需：
- 换用**基础版 Z-Image**（官方标 Diversity Medium，推荐 28–50 步、CFG 3–5），多样性更好，但速度慢得多，且需要另配工作流——本项目当前的 `zimg.py` 是为 Turbo 调的，**不支持直接切换**。
- 或用 **ControlNet / OpenPose** 先定姿态再生成（社区通用方案），同样需要扩展工作流。

### 11.4 一句话总结

多人效果弱是 **Turbo 蒸馏模型的固有短板**，代码没问题、无需修复；日常用**提示词写法 + 批量挑选 + 局部微调**（①②③）就能显著改善，只有"多人精准构图"这种硬需求才需要考虑进阶方案④。
