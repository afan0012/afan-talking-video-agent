"""Core workflow data models.

The web layer should not own the shape of a job.  These dataclasses are shared
by the API handlers, background workflow and persistence adapter, so adding a
field or migrating an old job has one obvious home.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def now() -> str:
    """Return a local ISO-8601 timestamp with timezone information."""
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


@dataclass
class BrollClip:
    """One B-roll insert: local asset name, time window and display mode."""

    name: str
    start: float
    duration: float
    enabled: bool = True
    title: str = ""
    # ``replace`` keeps legacy behaviour; ``pip`` overlays a small window.
    mode: str = "replace"
    pip_position: str = "bottom-right"
    pip_scale: float = 0.32
    pip_margin: int = 24


@dataclass
class Job:
    """Durable state for one end-to-end talking-head production job."""

    id: str
    source_name: str
    instruction: str
    create_voice: bool
    voice_id: str | None
    status: str = "queued"
    stage: str = "等待处理"
    progress: int = 0
    transcript: str = ""
    rewritten_text: str = ""
    error: str | None = None
    output_name: str | None = None
    # 参考视频仅接收用户主动上传的本地文件。
    reference_content_authorized: bool = False
    asr_model: str = "mimo-v2.5-asr"
    rewrite_model: str = "mimo-v2.5"
    voice_clone_model: str = "mimo-v2.5-tts-voiceclone"
    direct_tts_model: str = "mimo-v2.5-tts"
    lipsync_model: str = "videoretalk"
    duration: float = 0
    reference_duration: float = 0
    person_name: str | None = None
    person_video_name: str | None = None
    person_audio_name: str | None = None
    person_duration: float = 0
    person_risks: list[str] = field(default_factory=list)
    person_status: str = "等待上传人物视频"
    script_confirmed: bool = False
    preview_confirmed: bool = False
    preview_duration: float = 0
    duration_delta: float = 0
    duration_status: str = "等待人物视频与声音试听"
    duration_strategy: str = "keep_video"
    cancel_requested: bool = False
    timeline: list[dict[str, Any]] = field(default_factory=list)
    # 配音音频的 ASR 真实逐句时间轴（05 板块烧字幕用，替代按字数估算）。
    voice_timeline: list[dict[str, Any]] = field(default_factory=list)
    subtitle_name: str | None = None
    audio_name: str | None = None
    preview_audio_name: str | None = None
    voice_mode: str = "upload"
    # 选自本地素材库的声音样本，存放在当前项目目录内。
    voice_sample_name: str | None = None
    # 素材库中的用户自定义名称，仅用于界面展示；文件路径仍使用 voice_sample_name。
    voice_sample_label: str | None = None
    voice_speed: str = "standard"
    voice_emotion: str = "natural"
    # 阿里云 CosyVoice 精细控制（官方 API 参数）。
    voice_rate: float = 1.0
    voice_volume: int = 50
    voice_pitch: float = 1.0
    voice_seed: int = 0
    voice_lang: str = "auto"
    voice_instruction: str = ""
    fish_model: str = "s2-pro"
    fish_style: str = ""
    fish_speed: float = 1.0
    fish_volume: float = 0.0
    fish_temperature: float = 0.5
    fish_top_p: float = 0.7
    fish_quality_guard: bool = True
    reference_text: str = ""
    voice_reference_hash: str | None = None
    # 百炼 Qwen3-TTS 注册返回 fallback_mode=true 时透出的提示；None 表示音色正常。
    voice_quality_note: str | None = None
    video_risks: list[str] = field(default_factory=list)
    trim_start: float = 0
    trim_end: float | None = None
    title: str = ""
    sticker: str = ""
    music_name: str | None = None
    subtitle_enabled: bool = True
    cover_name: str | None = None
    edit_output_name: str | None = None
    # ── 剪辑样式（Step 5）──
    title_font_size: str = "h/18"
    title_color: str = "white"
    title_position: str = "top"
    subtitle_font_size: int = 42
    subtitle_color: str = "FFFFFF"
    subtitle_margin_v: int = 72
    subtitle_keywords: str = ""
    subtitle_keyword_color: str = "FFFF00"
    cover_text: str = ""
    cover_style: str = "diagonal-yellow"
    music_volume: float = 0.14
    # B-roll is deliberately local-first: only user-provided/licensed footage
    # is inserted, never scraped third-party clips.
    broll_name: str | None = None
    broll_enabled: bool = False
    broll_start: float = 5.0
    broll_duration: float = 4.0
    # 旧项目只有单值字段时由 JobStore 加载时自动迁移为一条 clip。
    broll_clips: list[BrollClip] = field(default_factory=list)
    # 主体口播视频的顶层布局。旧任务默认为全屏；旧的 BrollClip.mode 仅保留兼容。
    layout_mode: str = "fullscreen"
    layout_background_type: str = "blur"
    layout_background_name: str | None = None
    layout_background_color: str = "202020"
    # 画中画默认是右下角的小窗；0.72 会占满画面中央，不符合常见口播成片习惯。
    layout_foreground_scale: float = 0.32
    layout_foreground_position: str = "bottom-right"
    layout_foreground_margin: int = 24
    layout_blur: int = 18
    current_step: int = 1
    created_at: str = field(default_factory=now)
    updated_at: str = field(default_factory=now)
