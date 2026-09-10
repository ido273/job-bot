"""Manual ReAct-style tool-calling loop, used by discovery.py and chat.py
(anything that needs the model to call tools across multiple turns).

Ollama exposes a native `tools=[...]` function-calling parameter, but this
project deliberately doesn't use it -- see ollama_client.py's module
docstring. Instead: the system prompt documents each tool in plain text and
tells the model to respond with *only* `{"tool": "<name>", "args": {...}}` to
act, or its final answer (whatever shape the caller's own prompt asked for --
this module doesn't care, see below) once it's done. Each turn's response is
run through parsing.extract_last_json_block; a dict with a "tool" key
dispatches, anything else is treated as the final answer and returned as-is.

Bounded by `max_tool_calls` -- once hit, one more turn is forced with an
explicit "stop calling tools, answer now" instruction, so a cycle can never
loop on tool calls indefinitely (this is the discovery loop's actual
liveness safeguard, per max_tool_calls_per_cycle in config.yaml).
"""

import logging

from . import ollama_client
from .parsing import extract_last_json_block

logger = logging.getLogger("jobbot.agent.tool_loop")

MAX_TOOL_RESULT_CHARS = 6000  # keeps one noisy fetch_page from blowing the context window


def _render_tool_docs(tools: dict[str, dict]) -> str:
    lines = []
    for name, spec in tools.items():
        lines.append(f"- {name}({spec.get('args_doc', '')}): {spec['description']}")
    return "\n".join(lines)


def _tool_instructions(tools: dict[str, dict]) -> str:
    return (
        "Available tools:\n"
        f"{_render_tool_docs(tools)}\n\n"
        "To call a tool, respond with ONLY a single JSON object, nothing else:\n"
        '{"tool": "<tool_name>", "args": {...}}\n'
        "When you are done using tools and ready to answer, respond with your "
        "final answer instead (do not wrap it in a tool call)."
    )


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
    full_system = f"{system_prompt}\n\n{_tool_instructions(tools)}"
    messages = [{"role": "system", "content": full_system}, *(history or []), {"role": "user", "content": user_message}]

    calls_used = 0
    while True:
        raw = ollama_client.chat(messages, base_url, model, timeout)
        parsed = extract_last_json_block(raw)
        wants_tool = isinstance(parsed, dict) and "tool" in parsed

        if not wants_tool:
            return raw, calls_used

        if calls_used >= max_tool_calls:
            messages.append({"role": "assistant", "content": raw})
            messages.append(
                {
                    "role": "user",
                    "content": "Tool call budget reached. Do not call any more tools -- respond now with your final answer.",
                }
            )
            final_raw = ollama_client.chat(messages, base_url, model, timeout)
            return final_raw, calls_used

        tool_name = parsed.get("tool")
        args = parsed.get("args") if isinstance(parsed.get("args"), dict) else {}
        fn = dispatch.get(tool_name)
        if fn is None:
            tool_result = f"Error: unknown tool {tool_name!r}. Available tools: {', '.join(dispatch)}"
        else:
            try:
                tool_result = str(fn(**args))
            except Exception as exc:  # noqa: BLE001 -- any tool failure must not kill the loop
                logger.warning("tool=%s args=%r failed: %s", tool_name, args, exc)
                tool_result = f"Error calling {tool_name}: {exc}"
        calls_used += 1

        if len(tool_result) > MAX_TOOL_RESULT_CHARS:
            tool_result = tool_result[:MAX_TOOL_RESULT_CHARS] + "\n…(truncated)"

        messages.append({"role": "assistant", "content": raw})
        messages.append(
            {
                "role": "user",
                "content": f"[Result of {tool_name}]\n{tool_result}\n\nContinue: call another tool, or give your final answer.",
            }
        )
