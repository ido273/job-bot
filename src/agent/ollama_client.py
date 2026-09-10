"""Thin wrapper around Ollama's /api/chat. Deliberately not using Ollama's
native `tools=[...]` function-calling parameter -- see tool_loop.py for why
(short version: gpt-oss's reasoning/"Thinking..." preamble makes a plain
prompt-and-parse-the-last-JSON-block loop the robust choice, per prior manual
testing this project's parsing approach was built around -- see the
Verification step in the project plan for confirming this live once Ollama
is actually deployed).

Every call here can raise requests.RequestException (connection refused,
timeout, non-2xx) -- every caller in this package treats that as "Ollama is
currently unreachable" and degrades accordingly (see scoring.py, loop.py),
never as a hard crash.
"""

import requests

CHAT_PATH = "/api/chat"
TAGS_PATH = "/api/tags"


def chat(messages: list[dict], base_url: str, model: str, timeout: float) -> str:
    resp = requests.post(
        f"{base_url.rstrip('/')}{CHAT_PATH}",
        json={"model": model, "messages": messages, "stream": False},
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()["message"]["content"]


def is_reachable(base_url: str, timeout: float = 10) -> bool:
    try:
        resp = requests.get(f"{base_url.rstrip('/')}{TAGS_PATH}", timeout=timeout)
        resp.raise_for_status()
        return True
    except requests.RequestException:
        return False
