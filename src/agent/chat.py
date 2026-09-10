"""Shared chat/conversation logic behind the dashboard chat page AND
Telegram/WhatsApp free-text routing -- one implementation, three frontends
(see src/dashboard/app.py, src/dashboard/telegram_poller.py, and the
WhatsApp webhook in src/dashboard/app.py). Conversation history is kept
per-platform (db.chat_messages), not shared -- a Telegram thread and a
dashboard session don't need to see each other's turns.
"""

import logging

import requests

from .. import db
from . import cv_reader, tool_loop
from .jobs_tool import query_jobs
from .status import get_status

logger = logging.getLogger("jobbot.agent.chat")

SYSTEM_PROMPT = (
    "You are the assistant built into a personal job-search bot. The candidate will ask you about jobs "
    "the bot has found, their CV, and application decisions. Use the available tools to answer with real "
    "data instead of guessing -- never invent a job, a status, or CV content. Respond in Hebrew by default, "
    "matching the candidate's own language if they write in something else. Keep answers conversational and "
    "concise. Your final answer must be plain text, not JSON."
)

TOOLS = {
    "query_jobs": {
        "description": "List jobs from the database, optionally filtered.",
        "args_doc": 'status?: string (e.g. "applied", "not_relevant", "found"), source_site?: string, work_mode?: string',
    },
    "read_cv": {
        "description": "Read the candidate's most recently uploaded CV as plain text.",
        "args_doc": "(no args)",
    },
    "get_agent_status": {
        "description": "What the AI agent is currently doing (searching the web, resting, degraded) and when it last ran a cycle.",
        "args_doc": "(no args)",
    },
}

MAX_CHAT_TOOL_CALLS = 6


def _build_dispatch(conn) -> dict:
    return {
        "query_jobs": lambda **kwargs: query_jobs(conn, **kwargs),
        "read_cv": lambda **_kwargs: cv_reader.read_cv(conn),
        "get_agent_status": lambda **_kwargs: str(get_status(conn)),
    }


def handle_chat_message(conn, config, platform: str, user_text: str, thread_id: str = "default") -> str:
    agent_config = config.agent
    history = db.get_chat_history(conn, platform, thread_id, limit=20)

    try:
        reply, _calls_used = tool_loop.run_tool_loop(
            system_prompt=SYSTEM_PROMPT,
            user_message=user_text,
            tools=TOOLS,
            dispatch=_build_dispatch(conn),
            base_url=agent_config["ollama_base_url"],
            model=agent_config["ollama_model"],
            timeout=agent_config["scoring_timeout_seconds"],
            max_tool_calls=MAX_CHAT_TOOL_CALLS,
            history=history,
        )
    except requests.RequestException as exc:
        logger.warning("chat: Ollama unreachable: %s", exc)
        return "לא ניתן להתחבר כרגע לשירות ה-AI המקומי (Ollama). נסה שוב בעוד רגע."

    db.save_chat_message(conn, platform, thread_id, "user", user_text)
    db.save_chat_message(conn, platform, thread_id, "assistant", reply)
    return reply
