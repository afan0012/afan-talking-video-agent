"""Model-service connection and route persistence.

This module owns the wire-level bookkeeping for configurable providers.  It
does not know about FastAPI routes or workflow jobs; callers provide the data
paths and, for route loading, a validator that knows which credentials are
currently available.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import httpx
from fastapi import HTTPException

from app.model_registry import is_generic_text_model
from app.storage import read_json, write_json_atomic


# A provider is an account/endpoint, while a connection is one concrete API
# adapter exposed by that provider.  OpenAI compatibility applies to one
# connection's protocol, not automatically to every capability of a service.
SERVICE_CAPABILITIES = {
    "chat": {"title": "文案创作与改写", "steps": {"script", "rewrite", "edit_plan"}, "adapter": "openai-chat"},
    "asr": {"title": "语音识别（ASR）", "steps": {"asr"}, "adapter": "openai-transcriptions"},
    "tts": {"title": "直接配音（TTS）", "steps": {"direct_tts"}, "adapter": "openai-speech"},
    "voice_clone": {"title": "声音复刻", "steps": {"voice_clone"}, "adapter": "provider-adapter"},
    "lipsync": {"title": "视频改口型", "steps": {"lipsync"}, "adapter": "provider-adapter"},
}

# Generic OpenAI-compatible connections are intentionally text-only.  Audio
# and lip-sync providers need a provider-specific adapter before being enabled.
SUPPORTED_SERVICE_ADAPTERS = {"openai-chat"}


def clean_service_url(value: str) -> str:
    url = value.strip().rstrip("/")
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise HTTPException(400, "接口地址必须是有效的 http:// 或 https:// 地址，且不能包含账号密码。")
    return url


def clean_service_models(value: Any) -> list[str]:
    if isinstance(value, str):
        values = value.replace("，", ",").split(",")
    elif isinstance(value, list):
        values = value
    else:
        values = []
    result: list[str] = []
    for item in values:
        model = str(item or "").strip()
        if not model or "\n" in model or "\r" in model or len(model) > 160:
            continue
        if model not in result:
            result.append(model)
    return result


def service_models_url(base_url: str) -> str:
    return base_url.rstrip("/") + "/models"


def discover_service_models(base_url: str, api_key: str) -> list[str]:
    """Read a standard OpenAI-compatible model list and keep text models only."""
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        response = httpx.get(service_models_url(base_url), headers=headers, timeout=10)
    except (httpx.HTTPError, OSError) as error:
        raise HTTPException(503, f"无法读取供应商模型列表：{error}") from error
    if not response.is_success:
        raise HTTPException(503, f"供应商模型列表返回 HTTP {response.status_code}。")
    try:
        payload = response.json()
    except ValueError as error:
        raise HTTPException(503, "供应商返回的模型列表不是有效 JSON。") from error

    # OpenAI: {data: [{id: ...}]}; common compatible variants use models/name.
    raw_models = payload.get("data", payload.get("models", [])) if isinstance(payload, dict) else payload
    if not isinstance(raw_models, list):
        raise HTTPException(503, "供应商返回的模型列表格式不正确。")
    names = [item.get("id") or item.get("name") if isinstance(item, dict) else item for item in raw_models]
    return [model for model in clean_service_models(names) if is_generic_text_model(model)][:200]


def load_service_connections(path: Path) -> list[dict[str, Any]]:
    raw = read_json(path, [])
    if not isinstance(raw, list):
        return []
    items: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict) or not item.get("id") or not item.get("name"):
            continue
        try:
            base_url = clean_service_url(str(item.get("base_url") or ""))
        except HTTPException:
            continue
        connections = []
        for connection in item.get("connections", []):
            if not isinstance(connection, dict):
                continue
            capability = str(connection.get("capability") or "")
            adapter = str(connection.get("adapter") or "")
            models = clean_service_models(connection.get("models"))
            if capability in SERVICE_CAPABILITIES and adapter and models:
                connections.append({"capability": capability, "adapter": adapter, "models": models})
        if connections:
            items.append({
                "id": str(item["id"]), "name": str(item["name"]).strip(), "base_url": base_url,
                "api_key": str(item.get("api_key") or ""), "kind": str(item.get("kind") or "compatible"),
                "connections": connections,
            })
    return items


def save_service_connections(path: Path, items: list[dict[str, Any]]) -> None:
    write_json_atomic(path, items)


def service_model_value(provider_id: str, capability: str, model: str) -> str:
    return f"service:{provider_id}:{capability}:{model}"


def service_connection_for_model(
    model: str,
    capability: str,
    path: Path,
) -> tuple[dict[str, Any], dict[str, Any], str] | None:
    if not model.startswith("service:"):
        return None
    parts = model.split(":", 3)
    if len(parts) != 4:
        return None
    _, provider_id, selected_capability, selected_model = parts
    if selected_capability != capability:
        return None
    for provider in load_service_connections(path):
        if provider["id"] != provider_id:
            continue
        for connection in provider["connections"]:
            if (
                connection["capability"] == capability
                and connection["adapter"] in SUPPORTED_SERVICE_ADAPTERS
                and selected_model in connection["models"]
            ):
                return provider, connection, selected_model
    return None


def service_model_options(step: str, path: Path) -> list[dict[str, str]]:
    options: list[dict[str, str]] = []
    for provider in load_service_connections(path):
        for connection in provider["connections"]:
            capability = connection["capability"]
            definition = SERVICE_CAPABILITIES.get(capability, {})
            if step not in definition.get("steps", set()) or connection["adapter"] not in SUPPORTED_SERVICE_ADAPTERS:
                continue
            for model in connection["models"]:
                options.append({
                    "value": service_model_value(provider["id"], capability, model),
                    "label": f"{provider['name']} · {model}",
                    "provider": provider["name"], "capability": capability,
                })
    return options


def load_custom_providers(path: Path) -> list[dict[str, Any]]:
    data = read_json(path, [])
    return [
        item for item in data
        if isinstance(item, dict) and item.get("id") and item.get("base_url") and item.get("api_key")
    ] if isinstance(data, list) else []


def save_custom_providers(path: Path, items: list[dict[str, Any]]) -> None:
    write_json_atomic(path, items)


def normalize_local_ollama_url(value: str) -> str:
    url = value.strip().rstrip("/")
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
        raise HTTPException(400, "Ollama 地址必须是 http:// 或 https:// 开头的本机地址。")
    if (parsed.hostname or "").lower() not in {"localhost", "127.0.0.1", "::1"}:
        raise HTTPException(400, "本地 Ollama 仅允许连接 localhost 或 127.0.0.1。")
    if parsed.path not in {"", "/", "/v1"}:
        raise HTTPException(400, "Ollama 地址请填写服务根地址或以 /v1 结尾的地址。")
    return url


def load_local_ollama(path: Path) -> dict[str, str] | None:
    try:
        saved = read_json(path, {})
        if not isinstance(saved, dict):
            return None
        base_url = normalize_local_ollama_url(str(saved.get("base_url") or ""))
        model = str(saved.get("model") or "").strip()
        return {"base_url": base_url, "model": model} if model else None
    except HTTPException:
        return None


def save_local_ollama(path: Path, config: dict[str, str]) -> None:
    write_json_atomic(path, config)


def ollama_provider(model: str, path: Path) -> dict[str, str] | None:
    return load_local_ollama(path) if model == "local:ollama" else None


def custom_provider(model: str, path: Path) -> dict[str, Any] | None:
    if not model.startswith("custom:"):
        return None
    provider_id = model.split(":", 1)[1]
    return next((item for item in load_custom_providers(path) if item["id"] == provider_id), None)


def load_model_routes(
    path: Path,
    defaults: dict[str, str],
    is_valid: Callable[[str, str], bool],
) -> dict[str, str]:
    routes = dict(defaults)
    if not path.is_file():
        return routes
    saved = read_json(path, {})
    if not isinstance(saved, dict):
        return routes
    for step, model in saved.items():
        if step in routes and isinstance(model, str) and is_valid(step, model):
            routes[step] = model
    return routes


def save_model_routes(path: Path, routes: dict[str, str]) -> None:
    write_json_atomic(path, routes)
