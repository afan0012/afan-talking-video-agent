"""Read-only discovery for the application's supported local AI engines.

This module intentionally *never* installs packages, downloads weights, or
starts a GPU process.  Its job is to give the desktop UI one truthful answer:
which local components are already present and can safely be selected.

Every engine uses a stable directory below ``<data root>/engines``.  A user may
put an officially downloaded component there by hand; the next refresh then
detects it automatically.  This is the prerequisite for an installer later,
but does not pretend that an untested download/install flow already exists.
"""

from __future__ import annotations

import shutil
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Mapping

from app.funasr_local import (
    FUNASR_ENGINE_LAYOUT,
    FUNASR_MODEL_SPECS,
    discover_runtime_python,
    funasr_available,
)


def _has_config(path: Path) -> bool:
    return any((path / name).is_file() for name in ("config.json", "configuration.json"))


def funasr_root(data_root: Path) -> Path:
    return data_root / "engines" / "funasr"


def funasr_engine_status(data_root: Path, settings: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Inspect the two supported FunASR model layouts without loading them."""
    settings = settings or {}
    configured = str(settings.get("FUNASR_ENGINE_ROOT") or "").strip()
    root = Path(configured).expanduser() if configured else funasr_root(data_root)
    root = root.resolve()
    layouts = {
        model_id: {name: root / relative for name, relative in layout.items()}
        for model_id, layout in FUNASR_ENGINE_LAYOUT.items()
    }
    models: list[dict[str, Any]] = []
    for model_id, paths in layouts.items():
        present = _has_config(paths["model"])
        # The VAD/punctuation components are separate official snapshots.  A
        # missing one must not be hidden: otherwise the first recognition
        # unexpectedly tries to fetch it from the network.
        required = ["model", *[key for key in ("vad", "punc") if key in paths]]
        missing = [key for key in required if not _has_config(paths[key])]
        models.append({
            "id": model_id,
            "label": FUNASR_MODEL_SPECS[model_id]["label"],
            "installed": present and not missing,
            "missing": missing,
            "path": str(paths["model"]),
        })
    return {
        "id": "funasr",
        "root": str(root),
        "custom": bool(configured),
        "runtime_installed": funasr_available(root, settings),
        "runtime_python": discover_runtime_python(root, settings),
        "models": models,
        "installed": funasr_available(root, settings) and any(item["installed"] for item in models),
        "automatic_download": False,
    }


def ollama_engine_status(saved: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Discover a loopback Ollama server and its downloaded chat models.

    Only the well-known local endpoint is queried.  The application must never
    scan a LAN address or turn a user's Ollama into an accidental remote API.
    """
    saved = saved or {}
    base_url = str(saved.get("base_url") or "http://127.0.0.1:11434/v1").rstrip("/")
    root_url = base_url[:-3] if base_url.endswith("/v1") else base_url
    result: dict[str, Any] = {
        "id": "ollama",
        "base_url": base_url,
        "cli_installed": bool(shutil.which("ollama")),
        "running": False,
        "models": [],
        "selected_model": str(saved.get("model") or ""),
        "installed": False,
        "automatic_download": False,
        "error": "",
    }
    try:
        request = urllib.request.Request(f"{root_url}/api/tags", headers={"Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=2.5) as response:
            import json

            payload = json.loads(response.read().decode("utf-8"))
        models = [str(item.get("name") or "").strip() for item in payload.get("models", []) if isinstance(item, dict)]
        result.update(running=True, models=[item for item in models if item], installed=bool(models))
    except (OSError, ValueError, urllib.error.URLError) as error:
        result["error"] = str(error)
    return result


# ── 模型包一键接入：识别发布包结构，用户只需选中包根目录（或其上层） ──
# 发布包布局约定（models/engines/runtime 的相对位置固定，允许整体挪盘）：
#   afan-voice-engines\models\funasr | models\qwen3-tts | runtime-py312[(-cu128)]
#   afan-musetalk-bundle\engines\musetalk-1.5 | runtime\python\python310
_FUNASR_COMPONENTS = ("SenseVoiceSmall", "paraformer-zh", "fsmn-vad", "ct-punc")
_QWEN_TOKENIZER_DIRS = ("tokenizer", "Qwen3-TTS-Tokenizer-12Hz")
_QWEN_MODEL_DIRS = (
    "custom-voice-0.6b", "custom-voice-1.7b",
    "base-0.6b", "base-1.7b",
    "Qwen3-TTS-12Hz-0.6B-CustomVoice", "Qwen3-TTS-12Hz-1.7B-CustomVoice",
    "Qwen3-TTS-12Hz-0.6B-Base", "Qwen3-TTS-12Hz-1.7B-Base",
)
_WALK_PRUNE = {"Lib", "lib", "include", "share", "tcl", "models", "engines", "data",
               ".git", "__pycache__", "node_modules", "site-packages", "pip-cache",
               "cache", "downloads", "tmp", "outputs"}


def _looks_like_funasr_dir(path: Path) -> bool:
    if not path.is_dir():
        return False
    try:
        entries = list(path.iterdir())
    except OSError:
        return False
    found = 0
    for component in entries:
        if component.name in _FUNASR_COMPONENTS and component.is_dir():
            if any((component / name).is_file() for name in ("config.json", "configuration.json")):
                found += 1
    return found >= 1


def _looks_like_qwen_dir(path: Path) -> bool:
    if not path.is_dir():
        return False
    tokenizer = any((path / name / "config.json").is_file() for name in _QWEN_TOKENIZER_DIRS)
    voice = any((path / name / "config.json").is_file() for name in _QWEN_MODEL_DIRS)
    return tokenizer and voice


def _looks_like_musetalk_dir(path: Path) -> bool:
    return (
        (path / "models" / "musetalkV15" / "unet.pth").is_file()
        and (path / "scripts" / "inference.py").is_file()
    )


def _classify_python(python_exe: Path) -> list[str]:
    """按 site-packages 里已安装的包判断解释器能跑哪个引擎。"""
    if python_exe.parent.name == "Scripts":
        env_root = python_exe.parent.parent
    else:
        env_root = python_exe.parent
    site = env_root / "Lib" / "site-packages"
    if not site.is_dir():
        return []
    roles: list[str] = []
    for pkg, role in (
        ("qwen_tts", "qwen_tts"),
        ("funasr", "funasr"),
        ("diffusers", "musetalk"),
        ("torch", "torch"),
    ):
        if (site / pkg).is_dir():
            roles.append(role)
    return roles


def _fix_broken_venv(python_exe: Path) -> str | None:
    """发布包整体挪盘后 venv 的 pyvenv.cfg 指向旧盘符；发现 base 还在包内就自动改回。"""
    if python_exe.parent.name != "Scripts":
        return None
    venv_root = python_exe.parent.parent
    cfg = venv_root / "pyvenv.cfg"
    if not cfg.is_file():
        return None
    try:
        text = cfg.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    home = None
    for line in text.splitlines():
        if line.strip().lower().startswith("home"):
            home = Path(line.partition("=")[2].strip().strip('"'))
            break
    if home is None or not str(home).strip() or home.exists():
        return None
    # base 就在 venv 的同级目录（发布包内），直接指回去。
    candidate = venv_root.parent / home.name
    if not (candidate / "python.exe").is_file():
        return None
    try:
        cfg.write_text(text.replace(str(home), str(candidate)), encoding="utf-8")
    except OSError:
        return None
    return str(candidate)


def _find_interpreters(bundle_root: Path) -> dict[str, str]:
    found: dict[str, list[str]] = {}
    fixed: list[str] = []
    level = 0
    stack = [(bundle_root, 0)]
    while stack:
        current, depth = stack.pop()
        try:
            entries = sorted(current.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_file() and entry.name.lower() == "python.exe":
                roles = _classify_python(entry)
                fixed_note = _fix_broken_venv(entry)
                if fixed_note:
                    fixed.append(str(entry))
                for role in roles:
                    # 保留第一个（排序后路径最短的）匹配解释器。
                    found.setdefault(role, [str(entry), fixed_note or ""])
            elif entry.is_dir() and depth < 3 and entry.name not in _WALK_PRUNE:
                stack.append((entry, depth + 1))
        level += 1
    result = {f"{role}_python": paths[0] for role, paths in found.items()}
    result["python_notes"] = {role: paths[1] for role, paths in found.items() if paths[1]}
    return result, fixed


def analyze_model_bundle(root: Path) -> dict[str, Any]:
    """识别发布模型包：返回各引擎目录、解释器路径与自动修复记录。

    root 可以是包根目录，也可以是其上层文件夹（向下最多看两层）。
    """
    root = root.expanduser().resolve()

    def scan(candidate: Path) -> dict[str, Any]:
        found: dict[str, Any] = {"bundle_root": str(candidate)}
        for models_dir in (candidate / "models", candidate):
            funasr_dir = models_dir / "funasr"
            qwen_dir = models_dir / "qwen3-tts"
            if found.get("funasr_dir") is None and _looks_like_funasr_dir(funasr_dir):
                found["funasr_dir"] = funasr_dir
            if found.get("qwen_dir") is None and _looks_like_qwen_dir(qwen_dir):
                found["qwen_dir"] = qwen_dir
        for engines_dir in (candidate / "engines", candidate):
            musetalk_dir = engines_dir / "musetalk-1.5"
            if _looks_like_musetalk_dir(musetalk_dir):
                found["musetalk_dir"] = musetalk_dir
                break
        # HeyGem Lite 发布包是文件形态（compose + 镜像 tar，或整体 .tar.zst），
        # 认目录里有没有这几个标志性文件即可，其余交给 heygem 引擎模块处理。
        if (
            (candidate / "docker-compose-lite.yml").is_file()
            or (candidate / "HeyGem-Lite-image.tar").is_file()
            or (candidate / "HeyGem-Lite-模型包.tar.zst").is_file()
        ):
            found["heygem_dir"] = candidate
        return found

    analysis: dict[str, Any] = {"bundle_root": None, "funasr_dir": None, "qwen_dir": None,
                                "musetalk_dir": None, "heygem_dir": None,
                                "funasr_python": None, "qwen_tts_python": None,
                                "musetalk_python": None, "fixes": []}
    candidates = [root]
    try:
        candidates.extend(sorted(p for p in root.iterdir() if p.is_dir()))
        for p in list(root.iterdir()):
            if p.is_dir():
                candidates.extend(sorted(q for q in p.iterdir() if q.is_dir()))
    except OSError:
        pass

    bundle_root = None
    roots_scanned: set[Path] = set()
    for candidate in candidates:
        found = scan(candidate)
        if not any(found.get(key) for key in ("funasr_dir", "qwen_dir", "musetalk_dir", "heygem_dir")):
            continue
        if bundle_root is None:
            bundle_root = candidate
            analysis["bundle_root"] = str(candidate)
        for key in ("funasr_dir", "qwen_dir", "musetalk_dir", "heygem_dir"):
            if analysis.get(key) is None and found.get(key):
                analysis[key] = str(found[key])
        roots_scanned.add(candidate)
    merged_fixes: list[str] = []
    for scanned_root in roots_scanned:
        interpreters, fixes = _find_interpreters(scanned_root)
        for key, value in interpreters.items():
            if analysis.get(key) is None and value:
                analysis[key] = value
        merged_fixes.extend(fixes)
    analysis["fixes"] = merged_fixes
    return analysis
