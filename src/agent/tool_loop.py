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

Known Ollama gotcha guarded against here: Ollama has no `tool_choice`
parameter, and a model can get stuck calling the exact same tool with the
exact same arguments repeatedly -- e.g. after a tool error, instead of
adjusting and retrying differently. `max_tool_calls` already guarantees the
loop can't spin forever (it forces a no-tools final turn once the budget is
spent -- see the `allow_tools` comment below), but without this guard a
stuck model would burn its *entire* budget on identical repeated calls
before ever reaching a real answer. `_IDENTICAL_CALL_LIMIT` cuts that off
early instead of waiting for the full budget to drain.
"""

import json
import logging

from . import ollama_client

logger = logging.getLogger("jobbot.agent.tool_loop")

MAX_TOOL_RESULT_CHARS = 6000  # keeps one noisy fetch_page from blowing the context window
IDENTICAL_CALL_LIMIT = 2  # same (tool, args) called back-to-back this many times -> stop offering tools


def _call_signature(name: str, args: dict) -> tuple:
    return (name, json.dumps(args, sort_keys=True, default=str))


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
    last_signature = None
    identical_streak = 0

    while True:
        # Once the budget is spent -- or the model is stuck repeating the
        # same call (see IDENTICAL_CALL_LIMIT) -- the next call is made with
        # tools=None. The model then physically cannot return tool_calls
        # (nothing was offered), so the "no tool_calls -> final answer"
        # branch below is guaranteed to fire on this turn. No separate
        # forced-final-turn code path needed for either case.
        allow_tools = calls_used < max_tool_calls and identical_streak < IDENTICAL_CALL_LIMIT
        message = ollama_client.chat_raw(messages, base_url, model, timeout, tools=ollama_tools if allow_tools else None)
        tool_calls = message.get("tool_calls") or []

        if not tool_calls:
            # Also the landing spot for the "empty response after a failed
            # tool call" gotcha: content may legitimately be "" here (a
            # confirmed real behavior, not just a hypothetical) -- callers
            # already treat an empty final answer as a clean no-op (chat.py's
            # UI shows a fallback string, scoring.py/discovery.py's JSON
            # parse of "" cleanly yields None/{}), so returning it as-is
            # rather than erroring is correct, not a bug to paper over here.
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

            signature = _call_signature(name, args)
            if signature == last_signature:
                identical_streak += 1
            else:
                identical_streak = 1
                last_signature = signature
            if identical_streak >= IDENTICAL_CALL_LIMIT:
                logger.warning("tool=%s args=%r repeated %d times in a row -- forcing a final answer next turn", name, args, identical_streak)

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
