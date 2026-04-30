"""OpenAI-compatible HTTP API (LM Studio local server, etc.)."""
from __future__ import annotations

import logging
import threading
from typing import Any

import requests

log = logging.getLogger(__name__)


class RequestCancelled(RuntimeError):
    """Raised when an in-flight OpenAI-compatible request is cancelled."""


_ACTIVE_RESPONSES_LOCK = threading.Lock()
_ACTIVE_RESPONSES: set[requests.Response] = set()


def cancel_active_requests() -> None:
    """Best-effort abort for in-flight HTTP responses."""
    with _ACTIVE_RESPONSES_LOCK:
        active = list(_ACTIVE_RESPONSES)
    for resp in active:
        try:
            resp.close()
        except Exception:
            pass


def chat_completions(
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict[str, Any]],
    *,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: str | None = "auto",
    temperature: float = 0.3,
    max_tokens: int | None = None,
    timeout_s: int = 600,
    cancel_event: threading.Event | None = None,
) -> dict[str, Any]:
    """POST ``/v1/chat/completions``. Returns the parsed JSON object."""
    url = base_url.rstrip("/") + "/chat/completions"
    key = (api_key or "").strip() or "lm-studio"
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    body: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "stream": False,
    }
    if max_tokens is not None:
        body["max_tokens"] = max_tokens
    if tools:
        body["tools"] = tools
        if tool_choice:
            body["tool_choice"] = tool_choice
    if cancel_event is not None and cancel_event.is_set():
        raise RequestCancelled("request cancelled before send")
    r = requests.post(url, json=body, headers=headers, timeout=timeout_s)
    with _ACTIVE_RESPONSES_LOCK:
        _ACTIVE_RESPONSES.add(r)
    try:
        if cancel_event is not None and cancel_event.is_set():
            raise RequestCancelled("request cancelled after send")
        if r.status_code >= 400:
            log.debug("chat/completions %s: %s", r.status_code, (r.text or "")[:2000])
        r.raise_for_status()
        if cancel_event is not None and cancel_event.is_set():
            raise RequestCancelled("request cancelled before parse")
        data = r.json()
        if not isinstance(data, dict):
            raise ValueError("chat/completions: expected a JSON object response")
        return data
    finally:
        with _ACTIVE_RESPONSES_LOCK:
            _ACTIVE_RESPONSES.discard(r)


def list_models(
    base_url: str,
    api_key: str,
    *,
    timeout_s: float = 10.0,
) -> list[str]:
    """GET ``/v1/models`` → sorted unique model ids."""
    url = base_url.rstrip("/") + "/models"
    key = (api_key or "").strip() or "lm-studio"
    headers = {"Authorization": f"Bearer {key}"}
    r = requests.get(url, headers=headers, timeout=timeout_s)
    r.raise_for_status()
    data = r.json()
    out: list[str] = []
    for item in (data.get("data") or []):
        if isinstance(item, dict):
            mid = item.get("id")
            if mid:
                out.append(str(mid))
    return sorted(set(out))


def ollama_messages_to_openai(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Map Ollama-style ``images: [base64]`` user messages to OpenAI content parts."""
    out: list[dict[str, Any]] = []
    for m in messages:
        role = m.get("role", "user")
        images = m.get("images")
        if images:
            parts: list[dict[str, Any]] = []
            txt = m.get("content", "") or ""
            if txt:
                parts.append({"type": "text", "text": str(txt)})
            for raw in images:
                b64 = str(raw).strip()
                url = b64 if b64.startswith("data:") else f"data:image/png;base64,{b64}"
                parts.append({"type": "image_url", "image_url": {"url": url}})
            out.append({"role": role, "content": parts})
        else:
            row = {k: v for k, v in m.items() if k != "images"}
            out.append(row)
    return out
