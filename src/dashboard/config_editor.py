"""Reads/writes config/config.yaml's `matching:` block for the dashboard's
Settings page, using ruamel.yaml's round-trip mode so the file's extensive
comments survive an edit. The scraper's own src/config.py stays on plain
PyYAML (unaffected, no round-trip needed there).
"""

from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

_yaml = YAML()
_yaml.preserve_quotes = True
_yaml.indent(mapping=2, sequence=4, offset=2)  # matches config.yaml's existing list style
_yaml.width = 4096  # never wrap long lines (e.g. the anti-blocking User-Agent strings)


def _load(config_path: str):
    with open(config_path, encoding="utf-8") as f:
        return _yaml.load(f)


def _save(config_path: str, data) -> None:
    with open(config_path, "w", encoding="utf-8") as f:
        _yaml.dump(data, f)


def get_matching_config(config_path: str) -> dict[str, Any]:
    data = _load(config_path)
    matching = data.get("matching", {})
    return {
        "role_keywords": list(matching.get("role_keywords", [])),
        "exclude_keywords": list(matching.get("exclude_keywords", [])),
        "min_years_to_exclude": matching.get("seniority", {}).get("min_years_to_exclude", 3),
        "primary_locations": list(matching.get("locations", {}).get("primary", [])),
        "remote_hybrid_keywords": list(matching.get("locations", {}).get("remote_hybrid_keywords", [])),
    }


def _replace_list_in_place(mapping, key: str, values: list[str]) -> None:
    """Mutates an existing ruamel CommentedSeq (clear + extend) rather than
    assigning a fresh plain list to the key -- reassignment can orphan a
    comment ruamel attached to that key's position in the document.

    A comment sitting between this list's last item and the *next* sibling
    key (e.g. a comment introducing the following field) is stored by
    ruamel as a token on the last item's index, not on the next key -- so
    clearing the list orphans it too unless it's explicitly moved to
    whatever index is now last after the edit.
    """
    seq = mapping.get(key)
    if seq is None or not hasattr(seq, "clear"):
        mapping[key] = values
        return

    old_last_index = len(seq) - 1
    trailing_comment = seq.ca.items.pop(old_last_index, None) if old_last_index >= 0 else None

    seq.clear()
    seq.extend(values)

    if trailing_comment is not None and len(seq) > 0:
        seq.ca.items[len(seq) - 1] = trailing_comment


def save_matching_config(
    config_path: str,
    role_keywords: list[str],
    exclude_keywords: list[str],
    min_years_to_exclude: int,
    primary_locations: list[str],
    remote_hybrid_keywords: list[str],
) -> None:
    data = _load(config_path)
    matching = data.setdefault("matching", {})
    _replace_list_in_place(matching, "role_keywords", role_keywords)
    _replace_list_in_place(matching, "exclude_keywords", exclude_keywords)
    matching.setdefault("seniority", {})["min_years_to_exclude"] = min_years_to_exclude
    locations = matching.setdefault("locations", {})
    _replace_list_in_place(locations, "primary", primary_locations)
    _replace_list_in_place(locations, "remote_hybrid_keywords", remote_hybrid_keywords)
    _save(config_path, data)


def get_notification_channels(config_path: str) -> list[str]:
    data = _load(config_path)
    return list(data.get("notifications", {}).get("channels", ["telegram"]))


def get_agent_config(config_path: str) -> dict[str, Any]:
    """Only the two AI-agent knobs meant to be dashboard-editable without a
    redeploy -- the rest (Ollama/SearxNG URLs, model, timeouts, tool-call
    cap) stay hand-edited in config.yaml, same precedent as anti_blocking
    not having a UI either."""
    data = _load(config_path)
    agent = data.get("agent", {})
    return {
        "min_relevance_score": agent.get("min_relevance_score", 6),
        "reminder_offsets_minutes": list(agent.get("reminder_offsets_minutes", [30, 60, 120])),
    }


def save_agent_config(config_path: str, min_relevance_score: int, reminder_offsets_minutes: list[int]) -> None:
    data = _load(config_path)
    agent = data.setdefault("agent", {})
    agent["min_relevance_score"] = min_relevance_score
    _replace_list_in_place(agent, "reminder_offsets_minutes", reminder_offsets_minutes)
    _save(config_path, data)
