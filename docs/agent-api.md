# Agent API：把 afan 接入其他 AI

afan Talking Video Agent 的所有能力都可以被外部 AI（ZCode、Claude Code、Cursor 等）通过本机 CLI 调用。设计原则：**CLI 只调用本机 FastAPI**，不执行任意 shell 命令、不导入应用进程；成功输出 JSON 到 stdout，失败输出 `{"ok": false, "error": ...}` 到 stderr 并返回退出码 1。

## 快速开始

在项目根目录：

```powershell
python scripts/afan_agent_cli.py health
python scripts/afan_agent_cli.py guide        # 机器可读的工作流契约
python scripts/afan_agent_cli.py schema       # 机器可读的命令目录（自动从 CLI 生成）
python scripts/afan_agent_cli.py status <ID>  # 返回含 next_actions
```

Windows 可直接运行 `afan-agent.bat`。服务地址与 token 自动发现（`AFAN_AGENT_URL` → 数据目录 `current_url.txt` → 默认 8000 端口），无需配置；服务不在默认地址时也可用 `--base-url` 指定。

## UI as prompt：把界面操作变成 AI 提示词

网页工作台顶栏的**「复制为 AI 指令」**按钮会把当前项目的进度（已完成步骤）和已选参数（标题、字幕关键词、封面样式、布局等）生成一段自包含提示词：里面带项目 ID、下一条可执行命令，并指示 AI 先跑 `guide`、之后按 `status` 的 `next_actions` 自主推进。把它粘贴给任何安装了 `afan-talking-video` skill 的 AI 即可继续制作。

这段提示词与界面显示逻辑同源（都来自 `GET /api/workflow` 暴露的工作流契约，见 `app/workflow_contract.py`），不会与 UI 漂移。

## AI 黄金循环

1. `guide` 了解流程与约束；
2. 创建/接手项目（`create-script`、`extract-reference`、`draft`+`transcript`，或用户提示词里的项目 ID）；
3. 循环：`status <ID>` → 执行 `next_actions` 里的命令 → 长任务用 `wait <ID>` 等待 → 直到 `download`。
4. 授权规则：只有用户明确确认素材授权后才加 `--consent` / `--authorized`。

## 命令总览

- **查询**：`health`、`projects`、`jobs`、`models`、`providers`、`status`（含 `next_actions`，`--brief` 精简）、`wait`、`guide`、`schema`
- **文案**：`create-script`、`extract-reference`、`rewrite`、`save-rewritten`、`transcript`
- **素材**：`upload-person-video`、`voice-preview`（`--set` 透传各 TTS 服务参数）、`voice-confirm`、`library`、`library-upload`、`library-use`、`library-remove`
- **生成**：`generate-video`、`cancel-video`、`download`（`--artifact final|voice-preview|lipsync-video`）
- **剪辑**：`edit`、`auto-edit`（常用字段显式旗标 + `--set` 透传）、`music`、`broll`、`broll-clips`、`cover`
- **项目**：`create-job`（旧版一键流水线）、`draft`、`rename`、`save`、`forget`、`delete`

每个参数的准确含义以 `schema` 输出为准。

## 硬性约束

- 配音 ≤120 秒，且不超过人物视频时长 + max(0.8s, 人物时长×5%)，否则生成被拒绝。
- 重活全部异步：提交立即返回 `{id, accepted:true}`，用 `status`/`wait` 轮询。
- 未「保存」的项目在服务重启后丢失；关键节点后 `save <ID>`。

## skill 安装（跨 AI 工具共享）

skill 源文件在仓库 `skills/afan-talking-video/SKILL.md`，更新后重新复制即完成安装：

```powershell
cp skills/afan-talking-video/SKILL.md ~/.agents/skills/afan-talking-video/SKILL.md
```

`~/.agents/skills/` 是安装副本，不要直接改动；后续若接 Claude Desktop 等纯对话客户端，可在 CLI 之上加薄 MCP 适配器（MCP 不参与核心任务执行）。

## 发布到 WorkBuddy 开放平台（open.workbuddy.cn）

WorkBuddy 的「技能」形态与本项目的架构天然匹配：技能跑在用户桌面端，通过 Bash 执行技能包内 `scripts/` 的脚本——只要用户本机装了 afan 工作台，桌面 AI 就能驱动它。

- 技能源文件：`skills/workbuddy/SKILL.md`（frontmatter 按平台必填字段组织：`description_zh`/`description_en`/`category`/`version`/`author` 等）
- 打包命令：`python scripts/build_workbuddy_skill.py`，产物为 `work/workbuddy-skill/afan-talking-video.zip`（含 SKILL.md、`references/agent-api.md`、零依赖版 `scripts/afan_agent_cli.py`）
- 技能包内的 CLI 已支持**零第三方依赖**运行（无 httpx 时自动退回标准库 urllib），用户机器不需要 pip install

上架步骤（需腾讯账号）：在 open.workbuddy.cn 入驻并完成资质审核 → 创建技能（类型选「技能」）→ 上传 zip → 按平台分类列表确认 `category` → 测试通过后提交审核。修改技能只需改仓库源文件后重新打包上传。

## 安全与数据边界

- CLI 与服务只在本机 `127.0.0.1` 通信；写操作需要数据目录 `agent_token.txt` 里的握手 token（CLI 自动读取）。
- 视频、声音、文案素材从本机路径上传到本机服务；是否发送到云端由你在工作流里选择的模型决定，CLI 不会自动外发。
- 上传人物肖像、声音样音、参考内容前必须确认拥有相应授权，并遵守发布平台的 AI 内容规则。
