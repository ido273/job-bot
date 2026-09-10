"""Phase 2 dashboard: FastAPI app serving both the API and the Jinja2/vanilla-JS
frontend, reading/writing the same SQLite file the scraper (src/main.py) uses.
No build pipeline -- static HTML/CSS/JS served directly.
"""

import hashlib
import hmac
import logging
import os
import threading
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlencode

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import bot_commands, db, job_actions, main
from ..agent import status as agent_status
from ..agent.chat import handle_chat_message
from ..config import DEFAULT_CONFIG_PATH, load_config
from ..logging_setup import setup_logging
from ..models import Job
from ..notifiers import build_channel
from . import config_editor, reminder_checker, telegram_poller
from .auth import require_auth

logger = logging.getLogger("jobbot.dashboard.app")

BASE_DIR = Path(__file__).parent
ALLOWED_CV_EXTENSIONS = {".pdf", ".docx"}
MAX_CV_SIZE_BYTES = 10 * 1024 * 1024
STATUS_OPTIONS = ["found", "applied", "interview", "rejected", "offer", "not_relevant", "remind_later", "other"]
NOTIFICATION_MODES = ["immediate", "rate_limited", "digest", "count_batch"]

# Resolved the same way src.config.load_config() resolves it, so the
# dashboard's config.yaml read/write path always matches what the scraper
# actually loads.
CONFIG_PATH = os.environ.get("CONFIG_PATH", DEFAULT_CONFIG_PATH)
_config = load_config(CONFIG_PATH)
setup_logging(_config.logging_level)  # otherwise nothing (incl. the bot poller) ever logs anywhere
DB_PATH = _config.db_path
CVS_DIR = Path(DB_PATH).parent / "cvs"
CVS_DIR.mkdir(parents=True, exist_ok=True)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    telegram_thread, telegram_stop = telegram_poller.start(_config)
    reminder_thread, reminder_stop = reminder_checker.start(_config)
    yield
    telegram_stop.set()
    reminder_stop.set()
    telegram_thread.join(timeout=5)
    reminder_thread.join(timeout=5)


app = FastAPI(title="Job Bot Dashboard", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def get_conn():
    conn = db.connect(DB_PATH, _config.sites)
    try:
        yield conn
    finally:
        conn.close()


def _split_lines(text: str) -> list[str]:
    return [line.strip() for line in text.replace(",", "\n").split("\n") if line.strip()]


def _scanner_context(conn) -> dict:
    """Scan-now/pause state shown in the nav on every page (base.html)."""
    return {"is_paused": db.is_paused(conn), "scan_running": main.is_scan_running()}


# --- Jobs ---------------------------------------------------------------------


@app.get("/")
def root():
    return RedirectResponse(url="/jobs")


@app.get("/jobs")
def jobs_list(
    request: Request,
    status: str | None = None,
    source_site: str | None = None,
    work_mode: str | None = None,
    sort: str = "found_at",
    order: str = "desc",
    conn=Depends(get_conn),
    _user: str = Depends(require_auth),
):
    jobs = db.list_jobs(
        conn,
        status=status or None,
        source_site=source_site or None,
        work_mode=work_mode or None,
        sort=sort,
        order=order,
        exclude_statuses=["not_relevant"],
    )
    matching = config_editor.get_matching_config(CONFIG_PATH)
    linkedin_params = {
        "keywords": " OR ".join(matching["role_keywords"][:6]),
        "location": matching["primary_locations"][0] if matching["primary_locations"] else "Israel",
    }
    linkedin_url = "https://www.linkedin.com/jobs/search/?" + urlencode(linkedin_params)
    return templates.TemplateResponse(
        "jobs.html",
        {
            "request": request,
            "active_page": "jobs",
            "jobs": jobs,
            "status_counts": db.status_counts(conn),
            "site_counts": db.source_site_counts(conn),
            "status_options": STATUS_OPTIONS,
            "sites": [row["name"] for row in db.list_sites(conn)],
            "current_status": status or "",
            "current_source_site": source_site or "",
            "current_work_mode": work_mode or "",
            "sort": sort,
            "order": order,
            "linkedin_url": linkedin_url,
            **_scanner_context(conn),
        },
    )


@app.post("/jobs/add")
def jobs_add(
    title: str = Form(...),
    company: str = Form(""),
    location: str = Form(""),
    work_mode: str = Form("onsite"),
    url: str = Form(...),
    description: str = Form(""),
    status: str = Form("found"),
    conn=Depends(get_conn),
    _user: str = Depends(require_auth),
):
    title = title.strip()
    url = url.strip()
    if not title or not url:
        raise HTTPException(status_code=400, detail="Title and URL are required")
    # source_site="manual" is the same "which list did this come from" tag
    # pattern web_search/alljobs/drushim etc already use -- writes into the
    # SAME jobs table scraped listings use, not a separate list, so it's
    # sortable/filterable/editable identically to a scraped job everywhere
    # else in the dashboard. No notification: this path never touches
    # notification_engine, only db.save_job() (same call the scraper makes).
    job = Job(
        url=url,
        title=title,
        company=company.strip(),
        location=location.strip(),
        work_mode=work_mode if work_mode in {"onsite", "hybrid", "remote"} else "onsite",
        source_site="manual",
        description=description.strip(),
        status=status if status in STATUS_OPTIONS else "found",
    )
    db.save_job(conn, job)
    return RedirectResponse(url="/jobs", status_code=303)


@app.get("/jobs/{job_id}")
def job_detail(request: Request, job_id: int, conn=Depends(get_conn), _user: str = Depends(require_auth)):
    job = db.get_job(conn, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return templates.TemplateResponse(
        "job_detail.html",
        {
            "request": request,
            "active_page": "jobs",
            "job": job,
            "status_options": STATUS_OPTIONS,
            "cvs": db.list_cvs(conn),
            **_scanner_context(conn),
        },
    )


@app.post("/jobs/{job_id}")
def job_update(
    job_id: int,
    status: str = Form(""),
    date_applied: str = Form(""),
    cv_version_used: str = Form(""),
    cover_letter: str = Form(""),
    notes: str = Form(""),
    conn=Depends(get_conn),
    _user: str = Depends(require_auth),
):
    if db.get_job(conn, job_id) is None:
        raise HTTPException(status_code=404, detail="Job not found")
    db.update_job_tracking(conn, job_id, status or None, date_applied or None, cv_version_used or None, cover_letter or None, notes or None)
    return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)


# --- Sites ----------------------------------------------------------------


@app.get("/sites")
def sites_list(request: Request, conn=Depends(get_conn), _user: str = Depends(require_auth)):
    return templates.TemplateResponse(
        "sites.html",
        {"request": request, "active_page": "sites", "sites": db.list_sites(conn), **_scanner_context(conn)},
    )


@app.post("/sites/add")
def sites_add(
    name: str = Form(...),
    display_name: str = Form(...),
    base_url: str = Form(""),
    notes: str = Form(""),
    conn=Depends(get_conn),
    _user: str = Depends(require_auth),
):
    key = name.strip().lower().replace(" ", "_")
    if not key:
        raise HTTPException(status_code=400, detail="Site name required")
    db.add_site(conn, key, display_name.strip() or key, base_url.strip(), notes.strip())
    return RedirectResponse(url="/sites", status_code=303)


@app.post("/sites/{name}/toggle")
def sites_toggle(name: str, conn=Depends(get_conn), _user: str = Depends(require_auth)):
    rows = {row["name"]: row for row in db.list_sites(conn)}
    row = rows.get(name)
    if row is None:
        raise HTTPException(status_code=404, detail="Site not found")
    db.set_site_status(conn, name, "paused" if row["status"] == "active" else "active")
    return RedirectResponse(url="/sites", status_code=303)


@app.post("/sites/{name}/delete")
def sites_delete(name: str, conn=Depends(get_conn), _user: str = Depends(require_auth)):
    db.delete_site(conn, name)
    return RedirectResponse(url="/sites", status_code=303)


# --- CVs --------------------------------------------------------------------


@app.get("/cvs")
def cvs_list(request: Request, conn=Depends(get_conn), _user: str = Depends(require_auth)):
    return templates.TemplateResponse(
        "cvs.html",
        {"request": request, "active_page": "cvs", "cvs": db.list_cvs(conn), **_scanner_context(conn)},
    )


@app.post("/cvs/upload")
async def cvs_upload(
    label: str = Form(...),
    file: UploadFile = File(...),
    conn=Depends(get_conn),
    _user: str = Depends(require_auth),
):
    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED_CV_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Only .pdf or .docx files are accepted")

    contents = await file.read()
    if len(contents) > MAX_CV_SIZE_BYTES:
        raise HTTPException(status_code=400, detail="File too large (max 10MB)")

    stored_name = f"{uuid.uuid4().hex}{ext}"
    stored_path = CVS_DIR / stored_name
    stored_path.write_bytes(contents)

    db.save_cv(conn, file.filename or stored_name, str(stored_path), label.strip() or file.filename or stored_name)
    return RedirectResponse(url="/cvs", status_code=303)


@app.post("/cvs/{cv_id}/delete")
def cvs_delete(cv_id: int, conn=Depends(get_conn), _user: str = Depends(require_auth)):
    cv = db.get_cv(conn, cv_id)
    if cv is None:
        raise HTTPException(status_code=404, detail="CV not found")
    stored_path = Path(cv["stored_path"])
    if stored_path.exists():
        stored_path.unlink()
    db.delete_cv(conn, cv_id)
    return RedirectResponse(url="/cvs", status_code=303)


# --- Settings -----------------------------------------------------------------


@app.get("/settings")
def settings_page(request: Request, conn=Depends(get_conn), _user: str = Depends(require_auth)):
    matching = config_editor.get_matching_config(CONFIG_PATH)
    notification_settings = db.get_settings(conn)
    agent_settings = config_editor.get_agent_config(CONFIG_PATH)
    return templates.TemplateResponse(
        "settings.html",
        {
            "request": request,
            "active_page": "settings",
            "matching": matching,
            "settings": notification_settings,
            "notification_modes": NOTIFICATION_MODES,
            "agent_settings": agent_settings,
            **_scanner_context(conn),
        },
    )


@app.post("/settings/agent")
def settings_agent_save(
    min_relevance_score: int = Form(6),
    reminder_offsets_minutes: str = Form("30,60,120"),
    _user: str = Depends(require_auth),
):
    offsets = []
    for part in reminder_offsets_minutes.replace(",", " ").split():
        try:
            offsets.append(int(part))
        except ValueError:
            continue
    config_editor.save_agent_config(
        CONFIG_PATH,
        min_relevance_score=min_relevance_score,
        reminder_offsets_minutes=offsets or [30, 60, 120],
    )
    return RedirectResponse(url="/settings", status_code=303)


@app.post("/settings/matching")
def settings_matching_save(
    role_keywords: str = Form(""),
    exclude_keywords: str = Form(""),
    min_years_to_exclude: int = Form(3),
    primary_locations: str = Form(""),
    remote_hybrid_keywords: str = Form(""),
    _user: str = Depends(require_auth),
):
    config_editor.save_matching_config(
        CONFIG_PATH,
        role_keywords=_split_lines(role_keywords),
        exclude_keywords=_split_lines(exclude_keywords),
        min_years_to_exclude=min_years_to_exclude,
        primary_locations=_split_lines(primary_locations),
        remote_hybrid_keywords=_split_lines(remote_hybrid_keywords),
    )
    return RedirectResponse(url="/settings", status_code=303)


@app.post("/settings/notifications")
def settings_notifications_save(
    notification_mode: str = Form(...),
    rate_limit_per_hour: str = Form("10"),
    digest_minutes: str = Form("60"),
    batch_count: str = Form("5"),
    conn=Depends(get_conn),
    _user: str = Depends(require_auth),
):
    if notification_mode not in NOTIFICATION_MODES:
        raise HTTPException(status_code=400, detail="Invalid notification mode")
    db.set_setting(conn, "notification_mode", notification_mode)
    db.set_setting(conn, "rate_limit_per_hour", rate_limit_per_hour)
    db.set_setting(conn, "digest_minutes", digest_minutes)
    db.set_setting(conn, "batch_count", batch_count)
    return RedirectResponse(url="/settings", status_code=303)


# --- Chat (dashboard side of the shared AI agent chat interface) -------------
# Telegram's side lives in telegram_poller.py; WhatsApp's in the webhook
# handler below -- all three call the same src/agent/chat.py logic.

CHAT_THREAD_ID = "default"


@app.get("/chat")
def chat_page(request: Request, conn=Depends(get_conn), _user: str = Depends(require_auth)):
    return templates.TemplateResponse(
        "chat.html",
        {
            "request": request,
            "active_page": "chat",
            "history": db.get_chat_history(conn, "dashboard", CHAT_THREAD_ID, limit=50),
            **_scanner_context(conn),
        },
    )


@app.post("/api/chat/send")
async def api_chat_send(request: Request, conn=Depends(get_conn), _user: str = Depends(require_auth)):
    body = await request.json()
    message = str(body.get("message", "")).strip()
    if not message:
        raise HTTPException(status_code=400, detail="message is required")
    reply = handle_chat_message(conn, _config, "dashboard", message, thread_id=CHAT_THREAD_ID)
    return {"reply": reply}


@app.get("/api/agent/status")
def api_agent_status(conn=Depends(get_conn), _user: str = Depends(require_auth)):
    return agent_status.get_status(conn)


# --- Manual controls: Scan now / Pause / Resume --------------------------------


def _redirect_back(request: Request) -> RedirectResponse:
    return RedirectResponse(url=request.headers.get("referer", "/jobs"), status_code=303)


@app.post("/scraper/scan")
def scraper_scan(request: Request, background_tasks: BackgroundTasks, _user: str = Depends(require_auth)):
    if not main.is_scan_running():
        background_tasks.add_task(main.trigger_manual_scan)
    return _redirect_back(request)


@app.post("/scraper/pause")
def scraper_pause(request: Request, conn=Depends(get_conn), _user: str = Depends(require_auth)):
    db.set_paused(conn, True)
    return _redirect_back(request)


@app.post("/scraper/resume")
def scraper_resume(request: Request, conn=Depends(get_conn), _user: str = Depends(require_auth)):
    db.set_paused(conn, False)
    return _redirect_back(request)


# --- WhatsApp inbound webhook (bot commands) -----------------------------------
# Meta's Cloud API has no polling mode -- this endpoint only receives anything
# once the dashboard is reachable at a public HTTPS URL Meta can reach (see
# README). Telegram's listener (telegram_poller.py) needs no such thing.


def _handle_whatsapp_chat(sender: str, text: str) -> None:
    chat_conn = db.connect(DB_PATH, _config.sites)
    try:
        reply = handle_chat_message(chat_conn, _config, "whatsapp", text)
    except Exception:
        logger.exception("Chat agent failed to answer a WhatsApp message")
        reply = "אירעה שגיאה בעת עיבוד ההודעה. נסה שוב."
    finally:
        chat_conn.close()
    try:
        channel = build_channel("whatsapp", _config)
        channel.send_text_to(sender, reply)
    except ValueError:
        pass  # whatsapp not configured -- nothing to send to


@app.get("/webhooks/whatsapp")
def whatsapp_webhook_verify(request: Request):
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token", "")
    challenge = request.query_params.get("hub.challenge", "")
    expected_token = os.environ.get("WHATSAPP_WEBHOOK_VERIFY_TOKEN", "")
    if mode == "subscribe" and expected_token and hmac.compare_digest(token, expected_token):
        return PlainTextResponse(challenge)
    raise HTTPException(status_code=403, detail="Webhook verification failed")


@app.post("/webhooks/whatsapp")
async def whatsapp_webhook_receive(request: Request, conn=Depends(get_conn)):
    body = await request.body()

    app_secret = os.environ.get("WHATSAPP_APP_SECRET", "")
    if app_secret:
        signature = request.headers.get("X-Hub-Signature-256", "")
        expected_sig = "sha256=" + hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected_sig):
            raise HTTPException(status_code=403, detail="Invalid webhook signature")
    else:
        logger.warning("WHATSAPP_APP_SECRET not set -- webhook signature not verified.")

    try:
        channel = build_channel("whatsapp", _config)
    except ValueError:
        return {"status": "ignored", "reason": "whatsapp not configured"}

    payload = await request.json()
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            for msg in change.get("value", {}).get("messages", []):
                sender = msg.get("from", "")

                # Only the configured recipient may issue commands -- these
                # trigger real actions, so this isn't optional.
                if sender != channel.recipient_number:
                    logger.warning("Ignoring WhatsApp command from unrecognized sender=%s", sender)
                    continue

                if msg.get("type") == "interactive":
                    # A job notification's interactive-button reply -- id is
                    # the same "<action>:<job_id>[:<extra>]" payload the
                    # button was built with (see notifiers/base.py).
                    button_id = msg.get("interactive", {}).get("button_reply", {}).get("id", "")
                    if button_id:
                        result = job_actions.handle_button_action(button_id, conn, _config)
                        if result.replace_buttons:
                            # Cloud API has no message-edit endpoint -- "replace
                            # buttons" degrades to "send a new message with them"
                            # (e.g. the "⏰ הזכר לי" submenu). See notifiers/whatsapp.py.
                            channel.send_buttons(result.reply_text, result.replace_buttons)
                        else:
                            channel.send_text_to(sender, result.reply_text)
                    continue

                text = msg.get("text", {}).get("body", "")
                action = bot_commands.match_command(text)
                if action:
                    reply = bot_commands.handle_command(action, conn)
                    channel.send_text_to(sender, reply)
                elif text.strip():
                    # Same shared chat agent as Telegram's free-text routing
                    # (telegram_poller.py) -- implemented now even while
                    # WhatsApp is off by default in config, so turning it back
                    # on later doesn't require rebuilding this. Backgrounded
                    # (own DB connection) so an Ollama call doesn't hold up
                    # this webhook response -- Meta expects a fast 200.
                    threading.Thread(target=_handle_whatsapp_chat, args=(sender, text), daemon=True).start()

    return {"status": "ok"}
