"""read_cv() tool -- extracts plain text from the most recently uploaded CV
(src/dashboard/app.py's /cvs upload feature), so the agent can ground
scoring/discovery/chat in what's actually on the CV instead of guessing.
"""

import logging
from pathlib import Path

import pypdf
from docx import Document

from .. import db

logger = logging.getLogger("jobbot.agent.cv_reader")

MAX_CV_CHARS = 6000


def _extract_pdf(path: Path) -> str:
    reader = pypdf.PdfReader(str(path))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def _extract_docx(path: Path) -> str:
    document = Document(str(path))
    return "\n".join(p.text for p in document.paragraphs)


def read_cv(conn) -> str:
    cvs = db.list_cvs(conn)
    if not cvs:
        return "No CV has been uploaded yet."

    cv = cvs[0]  # list_cvs is already ordered by uploaded_at DESC -- most recent first
    path = Path(cv["stored_path"])
    if not path.exists():
        return f"CV record {cv['label']!r} exists but its file is missing on disk."

    try:
        if path.suffix.lower() == ".pdf":
            text = _extract_pdf(path)
        elif path.suffix.lower() == ".docx":
            text = _extract_docx(path)
        else:
            return f"Unsupported CV file type: {path.suffix}"
    except Exception as exc:  # noqa: BLE001 -- a corrupt/odd CV file must not crash the caller
        logger.warning("read_cv failed for %s: %s", path, exc)
        return f"Could not read CV file: {exc}"

    text = text.strip()
    if len(text) > MAX_CV_CHARS:
        text = text[:MAX_CV_CHARS] + "\n…(truncated)"
    return text or "CV file was read but contained no extractable text."
