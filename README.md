# Job Search Bot — Build Instructions

## For Claude Code

Read `job-bot-spec.md` in full before writing any code — it contains the complete
project context (candidate profile, anti-blocking strategy, security
rules, design language) that applies across all phases.

**Build only Phase 1 right now.** Phases 2 and 3 are documented in
`job-bot-spec.md` so you understand where the architecture is heading (e.g. why
the Phase 1 database schema already includes an unused `status` field),
but do not implement anything from Phase 2 or Phase 3 until explicitly
asked.

Within Phase 1: scaffold the project and get the Telegram notification
path working end-to-end with **one site (AllJobs)** first. Stop there
and let me test that a real message arrives before building the
Drushim and JobMaster scrapers. (Amended mid-build: WhatsApp added as a
second, independently-selectable channel — see job-bot-spec.md's Phase 1
amendment note and the NOTIFICATIONS section.)

## For me (order of operations)

1. Open this project folder in Claude Code.
2. First message: *"Read job-bot-spec.md, then build Phase 1 only, starting
   with the AllJobs scraper + Telegram notification end-to-end."*
3. Test: confirm a real Telegram message arrives for a real AllJobs
   listing.
4. Once that works, ask Claude Code to add the Drushim and JobMaster
   scrapers (still Phase 1).
5. Deploy Phase 1 to k3s on Proxmox, let it run for a day or two,
   confirm it's stable and not getting blocked.
6. Only then, open a new instruction: *"Read job-bot-spec.md again, Phase 1 is
   done and deployed. Now build Phase 2 (dashboard)."* Before sending
   this, install `taste-skill` (see job-bot-spec.md "Optional: design skills"
   section) so the dashboard UI comes out well-designed by default.
7. Once the dashboard renders, run the design review pass prompt from
   job-bot-spec.md using `apple-design-skill` to audit the liquid-glass
   implementation, color contrast, and RTL behavior, then fix what it
   flags.
8. Repeat the same pattern for Phase 3 once Phase 2 is stable.

## Files in this repo

- `job-bot-spec.md` — full specification, all 3 phases, plus global rules
  (anti-blocking, secrets/.gitignore, design language) that apply
  throughout.
- `README.md` — this file.
- `.env.example` — placeholder env vars (create this in Phase 1;
  never commit the real `.env`).
- `secret.example.yaml` — placeholder Kubernetes Secret manifest
  (create this in Phase 1; never commit the real one).

## Phase 1 — Setup & Run (built)

### 1. Get a Telegram bot token + chat ID

1. In Telegram, message **@BotFather** → `/newbot` → follow the prompts. It
   gives you a token like `123456789:AAExampleTokenNotReal`.
2. Message your new bot anything (e.g. "hi") so it's allowed to message you
   back.
3. Get your numeric chat ID: message **@userinfobot**, or open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` after step 2 and read
   `message.chat.id` from the JSON.

### 2. Configure

```bash
cp .env.example .env
# edit .env: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
```

`config/config.yaml` holds everything else (role keywords, exclude
keywords, location filters, polling interval, anti-blocking settings) as
structured fields — no code changes needed to tune matching.

### 3. Test the Telegram path end-to-end (do this first)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m src.main --test-notifications
```

Confirm a "✅ Job bot Telegram connection OK." message arrives in Telegram
before running a real scrape cycle.

### 3b. WhatsApp (Cloud API) — optional second channel

Uses Meta's official Cloud API, not Twilio. Setup:

1. Go to [developers.facebook.com](https://developers.facebook.com/apps) →
   create an app → type "Business" → add the **WhatsApp** product.
2. In WhatsApp → API Setup you get a **temporary access token** and a
   **Phone number ID** (Meta gives you a free test sender number — you
   don't need your own WhatsApp Business number for testing).
3. Under "To" / recipient list on that same page, add **your own** phone
   number and verify it via the code Meta sends — Cloud API test mode
   only delivers to numbers explicitly added there.
4. On your phone, send any WhatsApp message to the test number shown in
   the dashboard (opens the 24h messaging window — same idea as messaging
   the Telegram bot first).
5. Add to `.env`: `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`,
   `WHATSAPP_RECIPIENT_NUMBER` (your number, international format, no
   `+`, e.g. `972501234567`).
6. Test just this channel without touching `notifications.channels`:
   ```bash
   python -m src.main --test-notifications --channel whatsapp
   ```
7. Once confirmed, add `whatsapp` to `notifications.channels` in
   `config/config.yaml` (alongside or instead of `telegram`) so real
   matches go out on it too.

The temporary access token from step 2 expires in ~24h — for anything
longer-lived, generate a permanent token via a System User in Meta
Business Settings and swap it into `.env`.

### 4. Run one scrape cycle locally

```bash
python -m src.main --once --skip-startup-jitter
```

Logs which sites were checked, how many new/matched jobs were found, and
sends a Telegram message for each match. `data/jobbot.sqlite3` is created
automatically and tracks seen URLs so nothing is notified twice.

### 5. Run continuously (docker-compose, for local/always-on testing)

```bash
docker compose up --build -d
docker compose logs -f
```

### 6. Deploy to k3s (primary target)

```bash
cp k8s/secret.example.yaml k8s/secret.yaml   # fill in real values (Telegram/WhatsApp/dashboard auth)
kubectl apply -f k8s/secret.yaml
kubectl apply -f k8s/cronjob.yaml
kubectl apply -f k8s/dashboard.yaml
```

The CronJob fires every 10 minutes; the container adds its own random
jitter on top before actually scraping (see `polling.jitter_minutes` in
`config/config.yaml`), so it isn't hitting AllJobs at a predictable
timestamp every time. `k8s/secret.yaml` and `.env` are both gitignored —
only the `*.example.*` templates are meant to be committed.

`config.yaml` lives on the shared `job-bot-data` PVC (not a ConfigMap) so
the Phase 2 dashboard can write to it; it's seeded automatically from the
image's default on first run.

### Currently built

- **AllJobs** scraper (`src/scrapers/alljobs.py`) — live end-to-end, parses
  real listing pages (verified against both RTL and English/LTR job card
  markup, skips closed listings).
- Drushim and JobMaster are configured as `enabled: false` placeholders in
  `config/config.yaml` — next step per the plan above, once AllJobs is
  confirmed stable.
- Notification channels are pluggable (`src/notifiers/`): **Telegram**
  (Bot API) and **WhatsApp** (Meta Cloud API), independently selectable
  via `notifications.channels` in `config/config.yaml`. Every active
  channel gets every match. Both tested live.
- Each match notification includes a short, non-LLM requirements summary
  (`src/summarizer.py`: years-of-experience if mentioned, known tools/skills
  found, plus the listing's own "דרישות"/"Requirements" bullets when present).
- The `jobs` table stores the full scraped listing text (`description`
  column) so nothing needs to be re-scraped later for the summary or for
  Phase 3's CV/cover-letter tailoring.
- The notifier interface (`src/notifiers/base.py`) accepts an optional
  `buttons` list per channel (Telegram inline keyboard / WhatsApp reply
  buttons) so a later phase can attach action buttons without another
  interface change. No button actions are wired up yet — Phase 1 only.

## Phase 2 — Dashboard (built)

FastAPI + Jinja2 + vanilla JS (no npm/build step), RTL Hebrew, green
liquid-glass design. Reads/writes the same SQLite file the scraper uses.

### Setup & run locally

```bash
# .env needs DASHBOARD_USERNAME / DASHBOARD_PASSWORD (added to .env.example)
source .venv/bin/activate
uvicorn src.dashboard.app:app --reload --port 8000
# open http://127.0.0.1:8000 (basic auth prompt uses DASHBOARD_USERNAME/PASSWORD)
```

### docker-compose

```bash
docker compose up --build -d
# dashboard: http://localhost:8000
```

### What's there

- **Jobs** (`/jobs`) — every matched job, filter by status/site/work mode,
  sort, status counts strip, a static LinkedIn search-link card (built from
  current role keywords + primary location, since LinkedIn isn't scraped).
- **Job detail** (`/jobs/{id}`) — full description, and the application-
  tracking form: status (found/applied/interview/rejected/offer/other),
  date applied, CV version used, cover letter, notes.
- **Sites** (`/sites`) — toggle active/paused, add a site as a
  "pending, no scraper yet" placeholder, remove one. This table is now
  the live source of truth for which sites the scraper runs (was
  config.yaml's `enabled:` flag in Phase 1) — a site still needs an
  actual scraper module in `src/scrapers/` before toggling it active
  does anything.
- **CVs** (`/cvs`) — upload PDF/DOCX (10MB max), multiple versions,
  selectable per job when marking it applied.
- **Settings** (`/settings`) — role/exclude keywords and locations as
  tag-input chips (writes to `config/config.yaml`, comments preserved via
  `ruamel.yaml`); notification rules (immediate / rate-limited / digest /
  count-batch — one active at a time, parameters kept even when you
  switch away and back).

Every route is behind HTTP basic auth (`DASHBOARD_USERNAME`/`PASSWORD`).

### Manual controls: Scan now / Pause / Resume (built)

A "🔍 סרוק עכשיו" (Scan now) button and "⏸️ השהה" / "▶️ הפעל" (Pause/Resume)
toggle in the nav bar on every dashboard page. Same two actions are also
available as bot commands, case-insensitive exact-match:

| Action | Recognized text |
|---|---|
| Scan now | `חיפוש`, `scan`, `search` |
| Pause | `כיבוי`, `pause`, `off` |
| Resume | `הפעלה`, `resume`, `on` |

**Telegram** works immediately, no setup beyond what Phase 1 already has —
the dashboard runs a background long-polling listener
(`src/dashboard/telegram_poller.py`), started automatically with the
dashboard process. Only messages from your configured `TELEGRAM_CHAT_ID`
are honored.

**WhatsApp requires a webhook**, which is a hard requirement of the Cloud
API (no polling mode exists) — not something any amount of code here can
route around. To wire it up:

1. Get the dashboard reachable at a public HTTPS URL. The current
   `k8s/dashboard.yaml` is NodePort-only (LAN-only, no TLS), which Meta's
   servers can't reach. The simplest zero-config option for a home lab is
   [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/)
   (free, no port forwarding, no domain purchase needed) pointed at the
   `job-bot-dashboard` Service; a real domain + Ingress + cert-manager
   works too if you already have that set up.
2. In Meta for Developers → your app → WhatsApp → Configuration, set the
   webhook URL to `https://<your-public-url>/webhooks/whatsapp`, and the
   verify token to whatever you put in `WHATSAPP_WEBHOOK_VERIFY_TOKEN`.
3. Subscribe to the `messages` webhook field.
4. Copy the app's secret (App Settings → Basic) into `WHATSAPP_APP_SECRET`
   — the webhook verifies this via HMAC signature on every request, so
   requests not actually from Meta are rejected (this matters: these
   commands can pause your scraper).

Until that's set up, WhatsApp *outbound* notifications (job matches, etc.)
keep working exactly as before — only the *inbound* commands need it.

Pausing only stops the scraper's own scheduled runs (CronJob tick /
continuous-loop cycle); "Scan now" always runs regardless of pause state,
since it's an explicit request, not a scheduled one. Both a bot command
and a dashboard-click "Scan now" share one lock, so overlapping requests
no-op instead of running two scrapes at once.

## Non-negotiables (repeated here as a quick check before every phase)

- No secrets or CVs ever committed — `.gitignore` from the first
  commit.
- No scraping of LinkedIn — search-link only.
- Notification rules and search criteria are DB/config-driven, never
  hardcoded — the dashboard (Phase 2) must be able to change them live.
- AI-generated application content (Phase 3) is manually triggered per
  job, never automatic.
- Dashboard UI is fully Hebrew/RTL.
