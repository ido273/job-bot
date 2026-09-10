"""gpt-oss is a reasoning model: even when told to respond with pure JSON, its
raw output is typically a "Thinking..." preamble (sometimes containing
stray/example braces of its own) followed by the real answer. The robust way
to get structured data out of it isn't "the whole response is JSON" -- it's
"scan for balanced {...} spans and take the last one that actually parses."
Used for every structured exchange with the model in this package: tool
actions (tool_loop.py), scoring results (scoring.py), discovery candidates
(discovery.py).
"""

import json


def _balanced_json_spans(text: str) -> list[str]:
    """Returns every top-level balanced {...} substring, in the order they
    appear. Doesn't attempt to understand braces inside string literals with
    perfect fidelity -- good enough for LLM output, not a full JSON tokenizer."""
    spans = []
    depth = 0
    start = None
    in_string = False
    escape = False
    for i, ch in enumerate(text):
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    spans.append(text[start : i + 1])
                    start = None
    return spans


def extract_last_json_block(text: str) -> dict | None:
    """Returns the last balanced {...} span in `text` that parses as a JSON
    object, or None if nothing in the text does. Deliberately scans backward
    (last valid block wins) -- reasoning preamble can itself contain
    braces/examples earlier in the text; the model's actual answer always
    comes last."""
    if not text:
        return None
    for span in reversed(_balanced_json_spans(text)):
        try:
            parsed = json.loads(span)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None
