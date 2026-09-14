# afan Talking Video Agent

一个本地运行的短视频口播制作工作台。它把文案、配音、人物口型、字幕和简单剪辑串成一条可检查的流程。

## 能做什么

- 直接输入主题，让 AI 生成口播稿；或上传本地参考视频，提取原文案后生成可编辑的改写稿。
- 上传人物视频并进行人物视频预检。
- 使用上传的声音样音、已保存音色或云端标准音色生成试听配音。
- 将新配音与人物视频交给已配置的改口型适配器，生成改口型视频。
- 为成片生成字幕、关键词高亮、B-roll、背景音乐和封面，并导出 MP4。

## 模型与服务

- 文案生成、改写和部分编辑方案支持 MiMo、百炼、Ollama 以及配置好的 OpenAI 兼容服务。
- ASR、声音复刻、标准配音和改口型依赖对应的模型适配器；不同模型需要不同服务商的接口和权限。
- 改口型支持阿里云百炼 VideoRetalk（云端）、MuseTalk 1.5（本地）；本地声音支持 Qwen3-TTS 的样音复刻与内置音色。
- 本地模型运行时与桌面主程序分离：模型安装好后，程序仅在本机 `127.0.0.1` 自动启动适配服务并连接，不把 PyTorch 和数 GB 模型塞进主程序。
- 云端模型是否可用、是否收费、是否有免费额度，以服务商控制台当前显示为准。

## 本地模型包下载（按需）

Windows 安装包与便携版在 [GitHub Releases](../../releases) 页面下载。本地语音与改口型的模型权重和运行时体积较大，以压缩包形式放在夸克网盘，按需下载：

- **夸克网盘**：<https://pan.quark.cn/s/555ab9c2abd5?pwd=39Bq>（提取码 `39Bq`）
- `afan-voice-engines-*.zip`：本地转写（FunASR SenseVoice）与本地配音（Qwen3-TTS），含开箱即用的运行时，约 9.6 GB
- `afan-musetalk-bundle-cu128-*.zip`：MuseTalk 1.5 改口型（RTX 30/40/50 系适配），含全部权重与运行时，约 10 GB

使用方法：下载后核对分享说明中的 SHA256，右键解压（保持顶层文件夹完整），然后进入「设置 → 本地 AI 引擎 → 一键接入模型包」，选中解压出的文件夹即可；运行时已内置，无需安装 Python/CUDA。RTX 50 系显卡需驱动 ≥570。

## 运行要求

源码运行需要：

- Python 3.10 或更高版本（「一键启动.bat」检测不到可用 Python 时会引导自动安装，下载源为华为云镜像，国内可直连）
- FFmpeg 和 ffprobe（缺失时启动脚本自动下载到数据目录 `tools\ffmpeg`，无需管理员权限、不改系统 PATH；Windows 安装包会随包提供）
- 你自己配置的模型服务密钥（仅使用本地模型的环节可以不填云端密钥）

普通用户推荐直接使用 [Releases](../../releases) 页面的 Windows 安装包或便携版；源码运行参见下文。仓库中的打包脚本用于开发者自行构建。

## Windows 快速开始

在项目根目录双击 `一键启动.bat`（或打开 PowerShell 执行 `.\run.ps1`）：

```powershell
pip install -r requirements.txt
.\run.ps1
```

启动脚本会自动处理三件事，全部使用国内可直连的下载源：找到真正可用的 Python（Microsoft Store 的占位程序会被识别并跳过，缺失时可自动安装）、首次运行安装 pip 依赖（清华镜像优先）、把 FFmpeg 下载到 `tools\ffmpeg`（缺失时）。启动后打开：

<http://127.0.0.1:8000>

### 给其他 AI 调用：CLI

项目提供了一个不执行任意 shell 命令的 CLI 入口。CLI 只调用本机 FastAPI，所有成功结果都是 JSON；长任务立即返回 `id`，再用 `status` 查询，不需要让一个进程一直等待。

在项目根目录执行：

```powershell
python scripts/afan_agent_cli.py health
python scripts/afan_agent_cli.py create-script --prompt "写一段介绍本产品的口播稿"
python scripts/afan_agent_cli.py status <项目ID>
python scripts/afan_agent_cli.py projects
```

Windows 也可以直接运行 `afan-agent.bat`。服务不在默认地址时设置 `AFAN_AGENT_URL`，或给每次命令加 `--base-url http://127.0.0.1:8000`。

常用命令包括 `create-job`（提交人物视频和要求）、`extract-reference`（从本地参考视频提取文案）、`create-script`、`upload-person-video`、`save-rewritten`、`generate-video`、`status` 和 `download`。视频、声音和参考素材仍然通过本地路径上传到本机服务，不会被 CLI 自动发送到第三方；是否调用云端模型由工作流中的模型选择决定。

CLI 是稳定的基础调用入口；以后可以在它之上增加一个薄 MCP 适配器，MCP 不参与核心任务执行。

服务默认只监听本机地址；是否将媒体发送到云端，取决于你在工作流中选择的模型步骤。

### 本地 AI 引擎

进入「设置 → 本地 AI 引擎」可统一查看四类本地能力：Ollama（文案与改写）、FunASR（转写与字幕对轴）、Qwen3-TTS（声音复刻与内置音色）、MuseTalk（视频改口型）。本版本只检测、连接和启动已安装的组件，**不会自动下载模型、权重或 CUDA 运行时**；组件放入应用显示的标准目录后，重新检测即可启用。

文档中出现的 `engines/musetalk-1.5` 是相对于“当前数据目录”的内部目录名，不是固定的 D 盘绝对路径。源码运行时数据目录默认为项目目录；安装版默认为 `%LOCALAPPDATA%\\afan Talking Video Agent`；用户也可以在「偏好设置」改到其他磁盘。普通用户不需要手动填写或记住完整路径。

本源码仓库不附带数 GB 的模型权重和 CUDA/PyTorch 运行时。Windows 显卡驱动与匹配的 Python/CUDA 环境仍需按设备准备；模型默认放在应用的数据目录，可先在「偏好设置」把数据目录迁移到空间充足的磁盘。

本地引擎目前使用官方项目或模型页提供的安装方式。下载、解压或创建模型环境后，将组件按「本地 AI 引擎」显示的标准目录放置即可；程序只识别已存在的文件，不会静默替换成来源不明的模型包。

`scripts/musetalk_remote_server.py`、`scripts/qwen_tts_server.py` 与 `scripts/funasr_server.py` 是桌面端自动启动的本机适配服务，默认只监听本机回环地址。


没有 GPU 或尚未安装模型时，文案、配音和剪辑环节仍可单独测试；MuseTalk只有在各自运行环境准备好后才会真正生成视频。

ASR 可选本地 FunASR（Paraformer 或 SenseVoice）。主程序自身不安装 funasr/PyTorch：转写在 voice 模型包自带的运行时（`runtime-py312-cu128`）里以本机适配服务运行，主程序只通过回环 HTTP 调用。将完整模型放进标准目录（或用「一键接入模型包」导入 voice 包）后即可在设置中选择本地路由。详见 [`docs/local-ai.md`](docs/local-ai.md)。

运行测试需要额外安装开发依赖和 Playwright 浏览器：

```powershell
pip install -r requirements-dev.txt
python -m playwright install chromium
pytest -q
```

## 第三方服务与数据流向

本项目本身是 MIT 协议开源软件，但第三方模型服务有各自的服务条款、价格和数据处理规则。

- 上传到云端的内容只由你在工作流中主动选择的模型步骤决定，例如 ASR、声音生成或 VideoRetalk。
- API Key 默认保存在当前 Windows 用户的 `%LOCALAPPDATA%\afan Talking Video Agent` 数据目录中，不应提交到 Git 或分享给他人。
- 参考视频、声音样音和生成文件默认保存在本机数据目录；请自行确认磁盘空间和备份策略。
- 使用人物肖像、声音样音和参考内容前，必须确认你拥有相应授权，并遵守发布平台的 AI 内容相关规则。

## 打包 Windows 安装包

开发者可以使用 PyInstaller 和 NSIS/IExpress 构建 Windows 包。FFmpeg 必须使用可再分发的构建（如含 `libx264` 的 GPL 构建），并随包提供对应的许可证文本：

```powershell
python scripts/build_windows.py --ffmpeg <path-to-ffmpeg.exe> --ffmpeg-license <path-to-license.txt> --installer
```

打包产物不会自动上传到 GitHub；发布前请人工检查依赖、许可证、安装路径、快捷方式和数据目录。

## 许可证

本项目采用 [MIT License](LICENSE)。FFmpeg 等第三方组件遵循各自许可证，详见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
