# 本地 AI 引擎

本项目的本地模型均为可选组件。打开设置、刷新引擎状态和开始普通项目都不会下载模型或权重；只有已完整安装的组件才能在「模型分配」中选中。

## 统一规则

模型放在当前数据目录下的 `engines/`。这个目录在开发时位于项目目录；安装版默认在 `%LOCALAPPDATA%\afan Talking Video Agent`，也可以先在「偏好设置」迁移到其他磁盘。

| 能力 | 本地组件 | 标准目录 | 用途 |
| --- | --- | --- | --- |
| 文案、改写 | Ollama | Ollama 自己的模型目录 | 软件只检测 `127.0.0.1:11434` 与已下载模型 |
| 语音识别 | FunASR | `engines/funasr/` | 参考视频转写、配音字幕对轴 |
| 声音 | Qwen3-TTS | `engines/qwen3-tts/` | 样音复刻、官方内置音色 |
| 改口型 | MuseTalk 1.5 | `engines/musetalk-1.5/` | 人物视频口型驱动 |

主程序只和本机回环地址通信，不使用 SSH、AutoDL 地址或局域网地址。Qwen3-TTS 与 MuseTalk 会在实际使用时按需启动本机适配服务。

## 国内网络下载指引

所有自动下载都默认走国内可直连的源，不需要代理：

- **Python**：「一键启动.bat」检测不到可用 Python 时，可自动从华为云镜像安装官方 Python（`mirrors.huaweicloud.com/python/`）。
- **依赖包**：pip 安装 `requirements.txt` / `requirements-local-ai.txt` 时优先使用清华镜像（`pypi.tuna.tsinghua.edu.cn/simple`），失败自动回退官方源。
- **FFmpeg**：缺失时运行 `python scripts/ensure_ffmpeg.py` 自动下载到数据目录 `tools\ffmpeg\bin`（npmmirror 镜像优先，GitHub 发布包回退），无需管理员权限、不改系统 PATH。
- **MuseTalk 引擎包**：模型权重优先经 hf-mirror.com 镜像下载，失败再回退 HuggingFace 官方源。手动下载模型时也可以把 huggingface.co 域名替换为 hf-mirror.com。

## FunASR 本地语音识别

主程序自身不安装 funasr/PyTorch。转写由 `scripts/funasr_server.py` 在 voice 模型包自带的运行时（`runtime-py312-cu128`，已内置 funasr 与 CUDA 版 torch）里以本机回环服务运行；主程序按需自动启动，不需要任何手动安装步骤。

将已下载的官方模型放入固定目录（或用「一键接入模型包」导入 voice 模型包）：

```text
engines/funasr/
  paraformer-zh/   # Paraformer 模型（含 config.json 或 configuration.json）
  SenseVoiceSmall/ # SenseVoice 模型（含 config.json 或 configuration.json）
  fsmn-vad/        # VAD 模型
  ct-punc/         # Paraformer 所需标点模型
```

- 本地 FunASR Paraformer：中文口播优先，需要 `paraformer-zh + fsmn-vad + ct-punc`。
- 本地 FunASR SenseVoice：多语种转写，需要 `SenseVoiceSmall + fsmn-vad`。

程序只使用这些已存在的本地目录；缺任何必要组件时对应模型不会出现在可选列表中，也不会在第一次转写时联网补下载。

## Qwen3-TTS 与 MuseTalk

Qwen3-TTS 需要 `Qwen3-TTS-Tokenizer-12Hz`，以及至少一个 `Qwen3-TTS-12Hz-*-Base` 或 `Qwen3-TTS-12Hz-*-CustomVoice` 模型目录；MuseTalk 需要官方源码、`musetalkV15` 权重与其依赖模型。设置页会逐项检测并展示对应标准目录。

这两个 GPU 引擎应使用独立 Python 环境：将可运行的解释器置于各自引擎目录的 `.venv\Scripts\python.exe`（Windows）后，主程序会优先使用它启动本机适配服务。这样不会把 PyTorch/CUDA 依赖塞进桌面主程序。

## 选择规则

`auto` 先使用已配置的云端模型；只有本地 FunASR 运行时和完整权重都已检测到时，才会回退到本地 Paraformer。手动选择本地路由时，如果组件不完整，任务会给出明确错误，不会静默切换到云端或开始下载。
