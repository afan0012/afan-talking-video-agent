---
name: afan-talking-video
display_name: afan 口播视频工作台
display_name_en: afan Talking Video Agent
description: 驱动本机安装的 afan 口播视频工作台制作口播/说话人短视频：AI 写稿与改写文案、上传人物视频、配音试听与确认、改口型视频生成、剪辑包装并导出成片。当用户想制作、继续或修改口播视频/数字人视频，或粘贴了 afan 工作台「复制为 AI 指令」的提示词时使用。
description_zh: 调用本机 afan 口播视频工作台，从文案到成片一条龙：写稿、人物视频、配音、改口型、剪辑导出。需要本机已安装并启动 afan 工作台。
description_en: Drive the locally installed afan Talking Video Agent to produce talking-head short videos end to end: script writing and rewriting, person video upload, voice preview and confirmation, lip-sync generation, editing, and final MP4 export. Requires the afan desktop app installed and running on this machine.
category: video
version: 1.0.0
author: afan0012
---

# afan 口播视频智能体

通过本机 HTTP 服务（CLI 自动发现地址与 token）驱动 afan Talking Video Agent。所有命令输出 JSON；**不要**用这个技能执行任意 shell 命令。

## 前置条件

- 本机已安装 afan 工作台（Windows 安装版/便携版，或源码运行），且**服务正在运行**（系统托盘有 afan 图标）。
- 本技能自带的 CLI 在 `scripts/afan_agent_cli.py`，零第三方依赖，系统 Python 3.10+ 即可运行；在技能目录内执行：

```bash
python scripts/afan_agent_cli.py health
```

连不上时提示用户先启动 afan 工作台（桌面快捷方式或托盘图标）。

## 黄金循环（务必遵守）

1. **`guide`** — 先执行一次，了解流水线步骤、前置条件与约束（返回 JSON 契约）。
2. **建项目 / 接指令** — 若用户粘贴了工作台「复制为 AI 指令」的提示词，从中取项目 ID 与已选参数直接继续；否则按需求选择：
   - `create-script --prompt <需求>`（AI 写稿）
   - `extract-reference --video <参考视频> --authorized`（从参考视频改写）
   - `draft --name <名称>` + `transcript <ID> --text <文案>`（用户已有文案）
3. **按 `status <ID>` 的 `next_actions` 推进** — 每完成一步就重新 `status`，它给出的命令就是当前可执行的下一步；不需要自己猜状态机。
4. **长任务一律异步** — 提交（`voice-preview`、`generate-video`、`edit`、`auto-edit` 等）后立即返回 `{id, accepted:true}`，用 `wait <ID> --timeout 600` 等待；`wait` 退出码 1 表示任务失败，先 `status` 看 `error` 再决定重试方式。
5. **成片下载** — `download <ID> --output <路径> --artifact final`（剪辑成片）或 `--artifact lipsync-video`（无包装的改口型原片）。

## 硬性约束

- 配音时长 ≤120 秒，且不得超过人物视频时长 + max(0.8s, 人物时长×5%)，否则 `generate-video` 会被拒绝；超限就缩短文案或调快语速后重新 `voice-preview`。
- 未保存的项目在服务重启后丢失：关键节点后执行 `save <ID>`。
- `wait` 超时不是失败：加大 `--timeout` 继续等待即可。

## 授权与安全

- 人物肖像、声音样音、参考内容的授权由用户负责确认。对话中用户明确说素材是本人的/已获授权后，才可加 `--consent` / `--authorized`；用户未确认时先问，不要擅自加。
- 素材一律使用本机文件路径上传；不要建议用户把素材发给第三方在线服务。

## 常用命令速查

| 场景 | 命令（均在技能目录内执行） |
| --- | --- |
| 查看全部命令与参数 | `python scripts/afan_agent_cli.py schema` |
| AI 写稿 | `create-script --prompt "..."` |
| 确认文案 | `save-rewritten <ID> --text "..."` |
| 上传人物视频 | `upload-person-video <ID> --video <路径> --consent` |
| 配音试听 | `voice-preview <ID> --mode upload --sample <样音> --consent` |
| 确认试听 | `voice-confirm <ID>` |
| 生成改口型 | `generate-video <ID>` |
| 一键智能剪辑 | `auto-edit <ID>` |
| 手工剪辑样式 | `edit <ID> --title "..." --subtitle-style classic-yellow --set music_volume=0.2` |
| 素材库 | `library` / `library-upload` / `library-use <ID> <资产ID> --authorized` |
| 下载 | `download <ID> --output <路径> --artifact final` |

完整 API 说明见 `@references/agent-api.md`。
