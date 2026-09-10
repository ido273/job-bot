"""query_jobs() tool -- read-only access to the jobs table for chat, so
questions like "what have I already applied to" or "what did I mark not
relevant" are answered from real data, not guesses.
"""

from .. import db

MAX_ROWS = 30


def query_jobs(conn, status: str | None = None, source_site: str | None = None, work_mode: str | None = None) -> str:
    status = status or None
    source_site = source_site or None
    work_mode = work_mode or None
    rows = db.list_jobs(conn, status=status, source_site=source_site, work_mode=work_mode)[:MAX_ROWS]
    if not rows:
        return "No jobs match that filter."

    lines = [f"{len(rows)} job(s):"]
    for row in rows:
        score = f", score={row['relevance_score']}" if row["relevance_score"] is not None else ""
        lines.append(
            f"- id={row['id']} \"{row['title']}\" @ {row['company'] or '?'} "
            f"({row['location'] or '?'}, {row['work_mode'] or '?'}) status={row['status'] or 'found'} "
            f"source={row['source_site']}{score} url={row['url']}"
        )
    return "\n".join(lines)
