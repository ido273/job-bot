"""Tool-calling loop used by discovery.py and chat.py.

Originally written as a manual prompt-and-parse-JSON ReAct loop, on the
assumption that a 20B open-weight reasoning model's tool-calling via Ollama
would be unreliable. Live-tested against the actual in-cluster Ollama
running gpt-oss:20b before shipping (see the project plan's verification
step) -- that assumption was wrong in an important way: Ollama's native
`tools=[...]` mechanism works reliably for this model, returning a clean,
structured `message.tool_calls` list. What actually doesn't work is reading
`message.content` for a tool decision -- Ollama leaves `content` EMPTY on a
tool call (the call lives only in `tool_calls`), so a manual
"parse the last JSON block out of content" approach silently sees nothing
and treats an intended tool call as an empty final answer. This module uses
`tool_calls` directly instead.

Each tool is declared as {"description": str, "parameters": <JSON Schema>}
(the same subset of JSON Schema Ollama's own tools= param expects) --
`web_tools`/`jobs_tool`/`cv_reader`'s actual Python functions are unchanged;
only how their signatures are *described* to the model changed.
"""

import logging

from . import ollama_client

logger = logging.getLogger("jobbot.agent.tool_loop")

MAX_TOOL_RESULT_CHARS = 6000  # keeps one noisy fetch_page from blowing the context window


def _to_ollama_tools(tools: dict[str, dict]) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": spec["description"],
                "parameters": spec.get("parameters") or {"type": "object", "properties": {}},
            },
        }
        for name, spec in tools.items()
    ]


def run_tool_loop(
    system_prompt: str,
    user_message: str,
    tools: dict[str, dict],
    dispatch: dict[str, callable],
    base_url: str,
    model: str,
    timeout: float,
    max_tool_calls: int,
    history: list[dict] | None = None,
) -> tuple[str, int]:
    """Returns (final_answer_text, tool_calls_used). Raises requests.RequestException
    if Ollama itself is unreachable -- callers treat that as "agent unreachable"."""
    ollama_tools = _to_ollama_tools(tools)
    messages = [{"role": "system", "content": system_prompt}, *(history or []), {"role": "user", "content": user_message}]

    calls_used = 0
    while True:
        # Once the budget is spent, the next call is made with tools=None --
        # the model then physically cannot return a tool_calls (nothing was
        # offered), so the loop's "no tool_calls -> final answer" branch below
        # is guaranteed to fire on this turn. No separate forced-final-turn
        # code path needed.
        allow_tools = calls_used < max_tool_calls
        message = ollama_client.chat_raw(messages, base_url, model, timeout, tools=ollama_tools if allow_tools else None)
        tool_calls = message.get("tool_calls") or []

        if not tool_calls:
            return message.get("content", ""), calls_used

        # Trimmed to role/content/tool_calls when fed back into history --
        # dropping `thinking` keeps prior turns' reasoning out of context the
        # model doesn't need to re-read.
        messages.append({"role": "assistant", "content": message.get("content", ""), "tool_calls": tool_calls})

        for call in tool_calls:
            fn_info = call.get("function", {})
            name = fn_info.get("name")
            args = fn_info.get("arguments")
            if not isinstance(args, dict):
                args = {}
            fn = dispatch.get(name)
            if fn is None:
                result = f"Error: unknown tool {name!r}. Available tools: {', '.join(dispatch)}"
            else:
                try:
                    result = str(fn(**args))
                except Exception as exc:  # noqa: BLE001 -- any tool failure must not kill the loop
                    logger.warning("tool=%s args=%r failed: %s", name, args, exc)
                    result = f"Error calling {name}: {exc}"
            calls_used += 1

            if len(result) > MAX_TOOL_RESULT_CHARS:
                result = result[:MAX_TOOL_RESULT_CHARS] + "\n…(truncated)"
            messages.append({"role": "tool", "content": result})
