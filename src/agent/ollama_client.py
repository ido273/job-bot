"""Thin wrapper around Ollama's /api/chat.

Live-tested against the in-cluster Ollama running gpt-oss:20b (see the
project's plan/verification notes): Ollama reliably separates the model's
reasoning into `message.thinking` and returns a *clean* `message.content` --
no "Thinking..." preamble mixed into content, contrary to this module's
original assumption. When the model decides to call a tool, Ollama parses
that into a structured `message.tool_calls` list and leaves `content` EMPTY
-- so a caller that only reads `content` silently sees nothing on a tool
call. tool_loop.py is written against this actual, confirmed behavior
(reads `tool_calls`, not a JSON blob scraped out of content).
"""

import requests

CHAT_PATH = "/api/chat"
TAGS_PATH = "/api/tags"


def chat_raw(messages: list[dict], base_url: str, model: str, timeout: float, tools: list[dict] | None = None) -> dict:
    """Returns the raw `message` dict (role/content/thinking/tool_calls)."""
    payload = {"model": model, "messages": messages, "stream": False}
    if tools:
        payload["tools"] = tools
    resp = requests.post(f"{base_url.rstrip('/')}{CHAT_PATH}", json=payload, timeout=timeout)
    resp.raise_for_status()
    return resp.json()["message"]


def chat(messages: list[dict], base_url: str, model: str, timeout: float) -> str:
    """Plain (no-tools) call -- returns just the final content string.
    Used by scoring.py, where the full description is already in the
    prompt and there's nothing to call a tool for."""
    return chat_raw(messages, base_url, model, timeout).get("content", "")


def is_reachable(base_url: str, timeout: float = 10) -> bool:
    try:
        resp = requests.get(f"{base_url.rstrip('/')}{TAGS_PATH}", timeout=timeout)
        resp.raise_for_status()
        return True
    except requests.RequestException:
        return False
