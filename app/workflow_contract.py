"""工作流契约：口播视频生产流水线的机器可读唯一描述。

这是「UI as prompt」的真相源：网页 UI 的步骤提示、CLI 的 ``guide`` 命令、
``status`` 的 ``next_actions`` 以及外部 AI 的提示词，都应从这份纯数据推导，
而不是各自硬编码一遍。本模块只允许纯数据与纯函数，禁止导入网络、文件或
FastAPI 相关能力（见 docs/ARCHITECTURE.md 的模块边界约定）。
"""

from __future__ import annotations

from typing import Any

CONTRACT_VERSION = 1

# generate-video 的硬性门槛（与 app/main.py run_video_generation 内的校验一致）。
MAX_PREVIEW_SECONDS = 120
DURATION_DELTA_ABS = 0.8
DURATION_DELTA_RATIO = 0.05

WORKFLOW: dict[str, Any] = {
    "version": CONTRACT_VERSION,
    "product": "afan Talking Video Agent",
    "summary": "本地口播短视频流水线：文案 → 改写确认 → 人物视频 → 配音与改口型 → 剪辑导出。",
    "cli": {
        "entry": "python scripts/afan_agent_cli.py",
        "windows": "afan-agent.bat",
        "discovery": "服务地址与 token 自动从 AFAN_AGENT_URL / current_url.txt / agent_token.txt 解析，无需手动配置。",
        "output": "成功输出 JSON 到 stdout；失败输出 {\"ok\": false, \"error\": ...} 到 stderr 并返回退出码 1。",
    },
    "steps": [
        {
            "step": 1,
            "title": "获取文案",
            "description": "三选一：AI 直接写稿、从参考视频提取、或人工粘贴文案。",
            "actions": [
                {
                    "command": "create-script --prompt <文案需求>",
                    "endpoint": "POST /api/projects/ai-script",
                    "params": {"prompt": "必填，口播稿需求", "model": "可选，文案模型 ID"},
                    "precondition": "无",
                },
                {
                    "command": "extract-reference --video <参考视频路径> --instruction <改写要求> --authorized",
                    "endpoint": "POST /api/projects/extract-upload",
                    "params": {"video": "必填，本地参考视频", "instruction": "可选，改写要求", "authorized": "确认拥有参考内容授权"},
                    "precondition": "无",
                },
                {
                    "command": "draft --name <项目名> 然后 transcript <项目ID> --text <文案>",
                    "endpoint": "POST /api/projects/draft + POST /api/projects/{id}/transcript",
                    "params": {"name": "可选，项目名", "text": "必填，完整文案"},
                    "precondition": "无",
                },
            ],
        },
        {
            "step": 2,
            "title": "确认文案",
            "description": "改写或直接保存最终口播文案；rewritten_text 非空即视为文案就绪。",
            "actions": [
                {
                    "command": "rewrite <项目ID> --instruction <改写要求>",
                    "endpoint": "POST /api/projects/{id}/rewrite",
                    "params": {"instruction": "必填", "model": "可选，改写模型 ID"},
                    "precondition": "项目已有 transcript，且不在 running 中",
                },
                {
                    "command": "save-rewritten <项目ID> --text <最终文案>",
                    "endpoint": "POST /api/projects/{id}/rewritten",
                    "params": {"text": "必填，确认后的文案"},
                    "precondition": "无（人工确认后调用）",
                },
            ],
        },
        {
            "step": 3,
            "title": "上传人物视频",
            "description": "上传出镜人物视频，后台自动检测与抽帧。",
            "actions": [
                {
                    "command": "upload-person-video <项目ID> --video <人物视频路径> --consent",
                    "endpoint": "POST /api/projects/{id}/person-video",
                    "params": {"video": "必填", "consent": "必须为 true（肖像授权）"},
                    "precondition": "无",
                },
            ],
            "gate": "person_duration > 0（person_status 显示人物视频已就绪）",
        },
        {
            "step": 4,
            "title": "配音试听 → 确认 → 改口型",
            "description": "先生成配音试听，确认后才能启动改口型视频生成。",
            "actions": [
                {
                    "command": "voice-preview <项目ID> --mode upload --sample <声音样音> --consent",
                    "endpoint": "POST /api/projects/{id}/voice-preview",
                    "params": {
                        "mode": "upload|saved|direct（默认 upload；direct 为云端标准音色，无需授权）",
                        "sample": "mode=upload 时必填（或项目已绑定素材库声音）",
                        "voice_id": "mode=saved 时必填",
                        "consent": "upload/saved 模式必须为 true（声音授权）",
                        "speed": "slow|standard|fast",
                        "emotion": "natural|warm|steady",
                        "reference_text": "可选，≤12000 字",
                        "set": "--set key=value 透传其余服务端表单参数（CosyVoice/Fish/MiniMax 等）",
                    },
                    "precondition": "文案已就绪；任务异步执行，用 status/wait 轮询",
                },
                {
                    "command": "voice-confirm <项目ID>",
                    "endpoint": "POST /api/projects/{id}/voice-confirm",
                    "params": {},
                    "precondition": "试听已生成（preview_audio_name 非空）且试听满意",
                },
                {
                    "command": "generate-video <项目ID> [--strategy keep_video|trim_tail] [--lipsync-model <ID>]",
                    "endpoint": "POST /api/projects/{id}/generate-video",
                    "params": {"strategy": "keep_video|trim_tail", "lipsync_model": "可选，改口型模型 ID"},
                    "precondition": "见 constraints.generate_video_gates",
                },
                {
                    "command": "cancel-video <项目ID>",
                    "endpoint": "POST /api/projects/{id}/cancel-video",
                    "params": {},
                    "precondition": "仅当 current_step=4 且 status=running",
                },
            ],
            "gate": "preview_confirmed=true",
        },
        {
            "step": 5,
            "title": "剪辑与导出",
            "description": "改口型完成后可叠加标题、字幕、B-roll、背景音乐、封面并导出成片。",
            "actions": [
                {
                    "command": "edit <项目ID> [--title ... --subtitle-style ... --cover-text ... --set key=value ...]",
                    "endpoint": "POST /api/projects/{id}/edit",
                    "params": {"set": "常用项均可显式传参；未覆盖的服务端字段用 --set 透传"},
                    "precondition": "output_name 已生成（改口型完成）",
                },
                {
                    "command": "auto-edit <项目ID> [--locked <保持人工决定的字段>]",
                    "endpoint": "POST /api/projects/{id}/auto-edit",
                    "params": {"locked": "可选，逗号分隔的不交给 AI 的字段名"},
                    "precondition": "output_name 已生成",
                },
                {
                    "command": "download <项目ID> --output <保存路径> [--artifact final|voice-preview|lipsync-video]",
                    "endpoint": "GET /api/projects/{id}/download[/artifact]",
                    "params": {"output": "必填，本地 MP4 路径", "artifact": "final=剪辑成片（含 edit_output），lipsync-video=改口型原片，voice-preview=试听音频"},
                    "precondition": "对应产物已生成",
                },
            ],
        },
    ],
    "constraints": {
        "max_preview_seconds": MAX_PREVIEW_SECONDS,
        "duration_rule": f"配音时长不得超过人物视频时长 + max({DURATION_DELTA_ABS}s, 人物时长×{DURATION_DELTA_RATIO})，否则生成被拒绝；超限时重新 voice-preview 或改写文案缩短。",
        "generate_video_gates": [
            "status 不是 running",
            "preview_confirmed 为 true（必须先 voice-confirm）",
            "person_duration > 0（人物视频已处理完成）",
            "preview_duration ≤ 120 秒",
            "配音时长满足 duration_rule",
        ],
        "async_pattern": "所有重活（抽帧、TTS、改口型、剪辑）都是异步：提交后立即返回 {id, accepted:true}，用 status/wait <id> 轮询，status 里的 next_actions 会提示下一步。",
        "project_survival": "未点「保存」的项目只存在于服务内存，服务重启即丢失；关键节点后执行 save <项目ID>。",
    },
    "safety": [
        "上传人物肖像、声音样音或参考内容前，必须确认拥有相应授权（对应 --consent / --authorized 旗标）。",
        "素材只从本机路径上传；是否发送到云端由各步骤选择的模型决定，CLI 不会擅自外发。",
    ],
}


def step_titles() -> list[str]:
    """步骤标题列表，供 UI/CLI 简要展示。"""
    return [item["title"] for item in WORKFLOW["steps"]]
