# Job Search Bot — Full Specification

**Owner:** Ido Elmalem — Junior DevOps candidate, Rehovot → relocating to Jerusalem
**Deployment target:** k3s on Proxmox (home lab)
**Design language:** Clean, green-tinted, "liquid glass" (frosted glass / translucent panels) UI

This spec is split into 3 build phases. Send Phase 1 to Claude Code first, verify it works end-to-end, then send Phase 2, then Phase 3. Each phase section below is written so it can be pasted as its own prompt.

---

## Candidate Profile (context for matching — used in all phases)

Junior DevOps Engineer, RHCSA & CKA certified, AWS Solutions Architect (in progress).
Skills: AWS (EKS, IAM, S3, Route53, Bedrock, Secrets Manager), Terraform, Docker, Kubernetes, GitOps, ArgoCD, GitHub Actions, Linux (RHEL/Ubuntu), Prometheus/Grafana, Python, Bash, Proxmox home lab. No prior professional DevOps job — targeting entry-level/junior roles only.

---

## Global Config — Search Criteria (must be editable, not hardcoded)

These live in a config file (Phase 1) and later become editable from the dashboard UI (Phase 2):

- **Job sites:** AllJobs, Drushim, JobMaster (build in this order). LinkedIn: do NOT scrape — surface as a manual search link only, due to aggressive bot-blocking.
- **Role keywords (match ANY):** DevOps, Junior DevOps, DevOps Engineer, Cloud Engineer, Infrastructure Engineer, Site Reliability Engineer, SRE, Kubernetes Engineer, Platform Engineer, and Hebrew equivalents (דבאופס, מהנדס תשתיות, ג'וניור דבאופס).
- **Seniority filter:** entry-level / junior / 0–2 years only. Exclude anything explicitly requiring 3+ years.
- **Exclude keywords:** Senior, Team Lead, Head of, Manager, 5+ years, 10+ years.
- **Location filter:** Rehovot, Jerusalem, Tel Aviv, Center district as primary. Anywhere in Israel is acceptable IF the listing is hybrid or remote-friendly. Fully on-site roles far from Rehovot/Jerusalem/Center are filtered out.
- All of the above must be stored as **structured fields** (arrays/lists), not free text, so they can later be rendered as editable form fields in the dashboard.

---

## Anti-Blocking Strategy (applies to all scraper modules)

- Run from the home IP (Proxmox), not cloud — lower block risk than datacenter IPs.
- Randomize check timing per site with jitter (±2–3 min around the 10-minute interval) — never fire all sites at the same exact timestamp.
- Stagger requests between sites (small random delay before each site's scrape run).
- Realistic browser-like User-Agent and headers; rotate among 2–3 realistic UAs.
- Rate-limit within a site: minimum 2–3s between requests to that site.
- Detect CAPTCHA/block pages (specific response patterns per site) and back off — do not retry in a loop. On detection: log it, send a **distinct "scraper degraded" Telegram alert** (see Phase 1), and skip that site for the next N cycles before retrying.
- Respect robots.txt per site.
- LinkedIn is excluded from scraping entirely — dashboard shows a static "search on LinkedIn" link with the current keyword/location filters pre-filled into the URL instead.

---

## Security & Secrets

- Telegram bot token, chat ID, and any future LLM API key are **never hardcoded**. Loaded from environment variables / a `.env` file.
- `.env`, any CV files, and any personal config with your job-search criteria go in `.gitignore` from the first commit — this repo may eventually go on GitHub.
- Provide a `.env.example` with placeholder values so the structure is documented without exposing real secrets.
- If a dashboard is exposed via Ingress (Phase 2), it must sit behind basic auth at minimum, since it holds your personal application history.
- Kubernetes: secrets (Telegram token, future API keys) go in a `Secret` manifest, not a `ConfigMap`, and are never committed in plaintext — provide a `secret.example.yaml` template instead.

---

## Phase 1 — Core Scraper + Notifications (terminal/CronJob only, no dashboard yet)

> **Amended 2026-08-31** (mid-Phase-1, after the AllJobs scraper + Telegram
> path was already working end-to-end): added WhatsApp as a second,
> independently-selectable notification channel; notifications now include a
> short requirements summary; the DB stores the full scraped description;
> and the notifier interface is designed to carry optional interactive
> buttons for a later phase. See NOTIFICATIONS below — it supersedes the
> single-channel "Telegram notification" language elsewhere in this section.

```
Build a job-search monitoring bot, Phase 1 of 3 (scraper +
Telegram/WhatsApp notifications only — no dashboard in this phase).

CANDIDATE PROFILE
Junior DevOps Engineer, RHCSA & CKA certified, AWS Solutions Architect
(in progress). Skills: AWS (EKS, IAM, S3, Route53, Bedrock, Secrets
Manager), Terraform, Docker, Kubernetes, GitOps, ArgoCD, GitHub Actions,
Linux (RHEL/Ubuntu), Prometheus/Grafana, Python, Bash. No prior
professional DevOps job — targeting entry-level/junior roles only.

GOAL
A Python service that polls job-listing sites every ~10 minutes (with
jitter, see below), filters new postings against my criteria, and
sends matches — with a direct link and a short requirements summary —
to every active notification channel (Telegram and/or WhatsApp).

CRITERIA (store as structured config, not hardcoded)
- Job sites: AllJobs (alljobs.co.il), Drushim (drushim.co.il),
  JobMaster (jobmaster.co.il), in that build order. Do NOT scrape
  LinkedIn — too bot-resistant for v1.
- Role keywords (match ANY): "DevOps", "Junior DevOps", "DevOps
  Engineer", "Cloud Engineer", "Infrastructure Engineer", "Site
  Reliability Engineer", "SRE", "Kubernetes Engineer", "Platform
  Engineer", plus Hebrew equivalents: "דבאופס", "מהנדס תשתיות",
  "ג'וניור דבאופס"
- Seniority filter: entry-level/junior/0-2 years only. Exclude
  listings requiring 3+ years.
- Exclude keywords: "Senior", "Team Lead", "Head of", "Manager",
  "5+ years", "10+ years"
- Location filter: Rehovot, Jerusalem, Tel Aviv, Center district as
  primary; anywhere in Israel accepted IF hybrid/remote-friendly;
  filter out fully on-site roles far from those areas.
- All criteria as structured YAML lists/fields (not free text) — this
  file will later be edited via a web dashboard, so keep the schema
  clean and self-explanatory.

ANTI-BLOCKING (important — apply to every scraper module)
- Use `requests` + `BeautifulSoup` for static HTML; only fall back to
  `playwright` (headless) if a site requires JS rendering.
- Randomize the check interval with jitter (target ~10 min, ±2-3 min
  random offset) instead of a fixed cron tick.
- Add a small random delay before each site's scrape run so sites
  aren't hit at the same instant.
- Rate-limit within a site: minimum 2-3s between requests.
- Realistic, rotating User-Agent headers (2-3 realistic browser UAs).
- Respect robots.txt per site.
- Detect CAPTCHA/block responses (per-site pattern) and back off
  instead of retrying in a loop: log it, send a distinct Telegram
  alert like "⚠️ AllJobs scraper degraded / blocked, skipping for now",
  and skip that site for the next few cycles before retrying.

NOTIFICATIONS (amended)
- Two channels, both independently selectable, never hardcoded to one:
  - **Telegram** — Bot API (as originally built).
  - **WhatsApp** — Meta's official **Cloud API** (the free-tier developer
    API under a Meta for Developers app + WhatsApp product). Not Twilio,
    not an unofficial/reverse-engineered library.
- Which channel(s) are active is a **config setting**
  (`notifications.channels: [telegram]` / `[whatsapp]` / `[telegram,
  whatsapp]`), not a code branch — every active channel gets every match.
- Notification sending is a **pluggable interface**: one module per
  channel implementing a common `send_job_match(job, summary, buttons=None)`
  contract, so a channel can be added/removed without touching the
  scraper or matching logic.
- Each notification (any channel) includes: title, company, location,
  hybrid/remote/onsite tag, a clickable link, and a **short requirements
  summary** — a few bullet points or 1-2 sentences pulled from the
  listing's description (seniority level, key skills/tools mentioned).
  Built with plain-text heuristics in Phase 1 (no LLM call — that's
  Phase 3's job); keep it short, this is a notification, not the full
  listing.
- The notifier interface supports an **optional `buttons` argument**
  per channel (Telegram inline keyboard / WhatsApp interactive reply
  buttons) so a later phase can attach "Cover letter" / "Tailor CV"
  action buttons (job ID embedded in the button payload, so a tap is
  unambiguous — no free-text parsing) without rewriting the notifier.
  **No button actions are implemented in Phase 1** — the parameter just
  needs to exist and be wired through.

ARCHITECTURE
- Python 3.11.
- SQLite for dedup — store seen job URLs (or hash of title+company) so
  the same listing is never notified twice. Every URL a scraper
  encounters is recorded for dedup, whether or not it matched.
- Matching logic as a pluggable function (`is_match(job) -> bool`) so
  it can be swapped for an LLM-based scorer in a later phase without
  touching the rest of the pipeline.
- Config in a single YAML file, loaded at startup; secrets (Telegram
  bot token/chat ID, WhatsApp Cloud API access token/phone-number-ID/
  recipient number) from environment variables / .env — never
  hardcoded. Provide a .env.example with placeholders.
- Structured logging: which sites were checked, how many new jobs
  found per site, any scraping errors or blocks, and which
  notification channels a match was sent to.
- Each site scraper is a separate module implementing a common
  interface `fetch_new_jobs() -> list[Job]`, so sites can be added or
  removed independently later.
- Design the SQLite schema so a future dashboard (Phase 2) can read
  from it directly — include fields for: url, title, company,
  location, work_mode (onsite/hybrid/remote), source_site, found_at,
  a nullable `status` field (for future use: applied / interview /
  rejected / etc — leave null/unused in this phase), and (amended)
  `description` — the **full** scraped listing text, stored so the
  requirements summary above and Phase 3's CV/cover-letter tailoring
  never need to re-scrape a listing to get its full text again.

SECURITY
- .env and any file containing personal search criteria go in
  .gitignore from the first commit.
- No secrets committed anywhere, even as examples — only
  .env.example / secret.example.yaml with placeholder values. This
  now covers WhatsApp Cloud API credentials too, not just Telegram's.

DEPLOYMENT
- Docker container.
- Kubernetes CronJob manifest (~10 min schedule) as the primary
  target — deploying to my existing k3s cluster on Proxmox. Telegram
  token/chat ID AND WhatsApp Cloud API token/phone-number-ID/recipient
  number as a Kubernetes Secret (provide secret.example.yaml, not a
  real secret).
- docker-compose.yml as a simpler local-testing alternative.
- README covering: getting a Telegram bot token + chat ID via
  BotFather, setting up a Meta for Developers app + WhatsApp Cloud API
  test number, configuring the YAML file, and deploying to k3s.

Start by scaffolding the project and getting the Telegram notification
path working end-to-end with ONE site (AllJobs) so I can test it sends
me a real message, before building the other two scrapers. (Amended:
once Telegram is confirmed working, get WhatsApp Cloud API set up and
tested as its own step before the description-storage and
notification-content changes above.)
```

---

## Phase 2 — Web Dashboard (send only after Phase 1 is tested and working)

> **Built 2026-08-31.** FastAPI + Jinja2 + vanilla JS (no build pipeline),
> RTL Hebrew, green liquid-glass panels. Implementation notes worth
> recording here since they resolve ambiguity in the prompt below:
> - **Notification rules**: one mode active at a time
>   (immediate/rate_limited/digest/count_batch), switchable live from
>   Settings — not four simultaneous send paths on one match, since
>   "send immediately" and "hold for a batch" can't both happen to the
>   same match. Every mode's parameters stay saved regardless of which
>   is active.
> - **Sites table** is the dashboard-editable on/off registry;
>   `config.yaml`'s per-site block remains scraper-internal tuning
>   (search URLs, robots.txt). Adding a site from the UI never fakes a
>   scraper module, per the spec's own instruction below.
> - **Config edits** use `ruamel.yaml` round-trip so the comments
>   throughout `config.yaml` survive an edit from the Settings page.
> - **k8s deployment changed**: config.yaml now lives on the same PVC
>   as the database (not a ConfigMap), since the dashboard needs to
>   write to it. Seeded automatically from the image's default on first
>   run. See `k8s/dashboard.yaml` and the updated `k8s/cronjob.yaml`.
>
> **Amended 2026-08-31** (feature 6, manual controls, added after Phase 2
> was already built and running): "Scan now" calls the scraper's run
> function directly, in-process, from the dashboard (the k8s-Job
> alternative the prompt offered would've needed a new `kubernetes`
> client dependency plus RBAC/ServiceAccount setup for no real benefit,
> since the dashboard can already import and call the same code the
> CronJob runs — less moving parts, works identically in venv/
> docker-compose/k8s). Two real platform constraints shaped the bot-command
> side: **Telegram** supports long-polling, so its listener needs no
> public URL and runs as a background thread inside the dashboard
> process. **WhatsApp Cloud API has no polling mode** — inbound messages
> only arrive via a webhook Meta pushes to, which is a hard platform
> requirement, not a design choice here. That webhook is implemented
> (`POST /webhooks/whatsapp`, HMAC-verified) but won't receive anything
> until the dashboard is reachable at a public HTTPS URL, which the
> current NodePort-only k8s setup doesn't provide — see the README for
> the Cloudflare Tunnel note. Also added, since these commands trigger
> real state changes and neither channel is naturally access-controlled
> by itself: inbound commands are only honored from the configured
> `TELEGRAM_CHAT_ID` / `WHATSAPP_RECIPIENT_NUMBER` — anything else is
> silently ignored.

Covers: visual overview of all jobs found, application tracking (replaces the Excel tracker), notification rules editable from the UI, config editing from the UI, CV upload from the UI, green liquid-glass design.

```
Phase 2 of the job-search bot project: add a web dashboard on top of
the existing scraper + Telegram bot (Phase 1 is done and working —
scraper writes to SQLite, Telegram sends alerts).

GOAL
A small web app (FastAPI backend + a clean frontend) that reads/writes
the same SQLite database the scraper uses, so I have one single source
of truth instead of a separate spreadsheet.

DESIGN
- Full UI in Hebrew, right-to-left (RTL) layout — labels, buttons,
  table headers, forms, everything. Use `dir="rtl"` at the page/root
  level and a font that renders Hebrew cleanly. Job data itself (job
  titles/company names) may naturally be a mix of Hebrew and English
  since listings come in both — display as-is without forcing
  translation.
- Clean, minimal, uncluttered layout.
- Color palette: green-toned (e.g. soft sage/emerald accents on a
  neutral base — not neon).
- "Liquid glass" aesthetic: translucent/frosted-glass panels
  (backdrop-blur, subtle transparency, soft shadows, rounded corners)
  for cards and panels — similar to modern frosted-glass UI trends.
- Mobile-friendly, since I'll check this from my phone as often as
  desktop.

FEATURES

1. Job listing view
   - Table/card view of every job the scraper has found (from the
     existing SQLite `jobs` table).
   - Columns/fields: title, company, location, work mode (onsite/
     hybrid/remote), source site, date found, status, direct link.
   - Filter and sort by any of these fields.
   - A LinkedIn "search externally" card/section with a pre-built
     search URL using my current keyword + location filters (since we
     don't scrape LinkedIn directly).

2. Application tracking (replaces the Excel tracker)
   - Extend the `jobs` table (or add a related table) with: status
     (enum: found / applied / interview / rejected / offer / other),
     date_applied, cv_version_used (text/file reference), cover_letter
     used (text/file reference), and free-text notes.
   - Editable directly from the dashboard — status changes, notes,
     etc. — no separate spreadsheet needed going forward.
   - A summary view: count of jobs by status (e.g. "12 applied, 3
     interviews, 2 rejected"), and by source site.

3. CV upload
   - A page/section to upload a CV file (PDF or DOCX) directly from
     the dashboard, stored server-side (not committed to git — add to
     .gitignore / stored in a mounted volume, not the repo).
   - Support multiple CV versions if I want to keep more than one
     tailored version, each selectable when marking a job "applied".

4. Config editing from the dashboard
   - A settings page that edits the same YAML config the scraper reads
     — role keywords, exclude keywords, location list, seniority
     rules — as proper form fields (multi-select / tag inputs), not a
     raw text editor.
   - Changes take effect on the scraper's next run (scraper re-reads
     config each cycle, or on a config-reload signal — pick whichever
     is simpler to implement reliably).

4b. Manage scraped sites from the dashboard
   - A dedicated "Sites" section listing every site currently being
     scraped (name, base URL, status: active/paused, last successful
     check timestamp, last error if any — reuse the "scraper degraded"
     signal from the anti-blocking logic).
   - Ability to toggle a site active/paused without removing its
     config or historical data.
   - Ability to fully remove a site from the rotation.
   - Ability to add a new site: since a brand-new site needs an actual
     scraper module written for its HTML structure, "adding" a site
     from the UI should NOT attempt to auto-generate a scraper.
     Instead: the form takes a name + base URL + notes, saves it as
     "pending — scraper not yet implemented" in the same sites table,
     and it's clearly shown as inactive until a scraper module for it
     is added to the codebase. This keeps expectations honest — the
     dashboard manages which sites are in rotation, but a human (me +
     Claude Code) still has to build the scraper for a genuinely new
     site.
   - This means the scraper's site registry (which modules are loaded)
     should be driven by the same `sites` table the dashboard edits —
     e.g. each site module registers itself with a matching name/key,
     and the runner only invokes modules for sites marked "active".

5. Notification rules (editable from dashboard)
   - Settings for how Telegram alerts are batched/sent. Support ALL of
     these as configurable options (not mutually exclusive — user can
     combine them):
     a) Send every match immediately (default / current Phase 1
        behavior).
     b) Batch: send at most N notifications per hour, queuing the rest
        for the next window.
     c) Batch: send a digest every X minutes containing all matches
        found in that window.
     d) Batch: send once M new matching jobs have accumulated,
        regardless of time elapsed.
   - These settings must live in the database (a `settings` table or
     similar), NOT hardcoded in the notifier's code. The dashboard
     reads and writes this table directly, and the notification logic
     reads it on every send cycle — so changing the mode or its
     parameters (N, X minutes, M jobs) from the dashboard takes effect
     immediately, with no code change or redeploy required.
   - Default to (a) "send immediately" since for junior roles fast
     application matters — but the mechanism must support switching
     between all 4 modes live, from the UI, at any time.

6. Manual controls: "Scan now" + Pause/Resume (amended, added after
   Phase 2 was already live)
   - A "Scan now" button on the dashboard that triggers an immediate
     scrape cycle across all active sites, without waiting for the
     next scheduled run. Implement via a k8s Job created from the
     CronJob's template with a scoped ServiceAccount/RBAC (Job-create
     only), OR — if that's more rework than it's worth given how the
     scraper is packaged — expose the scrape logic as an internal
     function/API the dashboard calls directly. Pick whichever fits.
   - A Pause/Resume toggle on the dashboard. Store as a flag in the
     same `settings` table already used for notification rules. The
     scraper's regular scheduled runs check this flag first and no-op
     immediately if paused — don't touch the CronJob schedule itself.
   - Same two controls via bot commands, Telegram AND WhatsApp,
     recognized case-insensitive, exact-match, no NLU:
     - "חיפוש" / "scan" / "search" → trigger an immediate scan (same
       action as the dashboard button)
     - "כיבוי" / "pause" / "off" → pause
     - "הפעלה" / "resume" / "on" → resume
   - Reply with a short confirmation either way (e.g. "🔍 Scanning
     now…", "⏸️ Paused", "▶️ Resumed").

ARCHITECTURE
- FastAPI backend, serving both the API and the frontend (or a
  lightweight frontend framework if simpler — your choice, keep it
  minimal, no heavy build pipeline).
- Reads/writes the same SQLite file the Phase 1 scraper already uses —
  do not create a second database.
- Basic auth on the whole dashboard (single user — just me) since it
  will hold my full application history and possibly my CV.
- Dockerize as a separate container from the scraper; both connect to
  the same SQLite file via a shared volume when deployed to k3s.
- Kubernetes Deployment + Service + a small Ingress (or NodePort — your
  call) so I can reach it from my phone on the home network.

Keep this phase strictly to what's listed above — no AI/LLM features
yet, that's Phase 3.
```

**Design review pass (after the dashboard is built and running):**

Once Phase 2 is built and you can see the dashboard rendering, run a
follow-up prompt using the `apple-design-skill` (install instructions
below) to audit and refine it:

```
Review the dashboard's UI against the apple-design-skill guidelines,
focusing on:
- references/hig/liquid-glass.md — check the glass panel implementation
  (blur, transparency, contrast against the green palette) against the
  cross-platform Liquid Glass guidance.
- references/hig/color.md and typography.md — verify the green palette
  has sufficient contrast for readability, especially job status
  badges and table text.
- Accessibility and RTL guidance — verify the Hebrew RTL layout
  doesn't break any of the interaction patterns (forms, dropdowns,
  the sites-management table) and that focus order makes sense in RTL.

Give me a prioritized list of fixes, then implement the high-priority
ones.
```

This is a review pass, not a rebuild — run it after the dashboard
works functionally, so the skill has real rendered UI to critique
rather than a spec to guess from.

**Note on site management:** the "Sites" feature (listing scraped sites,
toggling active/paused, adding a new site as a placeholder, removing a
site) is included in feature 4b above and in the prompt block — it lets
you control which sites are in rotation from the dashboard, but adding
a genuinely new site still requires a scraper module to be written for
it (the dashboard can't auto-generate HTML parsing logic for a site it's
never seen).

---

## Phase 3 — AI-Assisted Application (manual trigger only, full control)

Covers: LLM-tailored CV suggestions and cover-message drafts — **only when explicitly requested per job**, never automatic.

```
Phase 3 of the job-search bot project: add optional AI assistance for
tailoring applications — triggered manually per job, never automatic.

GOAL
From the dashboard (Phase 2), let me click a button on any specific
job listing to generate:
1. A tailored cover message / cover letter draft for that job.
2. Suggested edits or a tailored summary/bullet emphasis for my CV,
   based on that job's description.

This must NEVER run automatically on new matches — only when I
explicitly click "Generate" on a specific job. I want full control
over API cost and over what gets sent where.

ARCHITECTURE
- A new dashboard action per job row/card: "Generate application
  materials".
- Backend call to an LLM API (Claude), sending: my base CV (uploaded
  in Phase 2), the job title + description scraped for that listing,
  and my known background context (bootcamp capstone, certifications,
  IDF background — but never the specific IDF unit name in generated
  output).
- Output shown in the dashboard as an editable draft — never sent or
  saved as final automatically. I review and edit before using it.
- Store the generated draft (cover letter text) linked to that job
  row, alongside the "cv_version_used" field from Phase 2, so my
  tracking stays accurate.
- API key loaded from environment variable / Kubernetes Secret, never
  hardcoded or committed.
- No automatic rate limiting needed since this is manually triggered
  per job — but log each generation call (job id, timestamp) so I can
  see my own usage if needed.

Keep the tone/structure consistent with my existing cover letters:
acknowledge the experience gap honestly, pivot immediately to concrete
proof points (certifications, capstone specifics), frame IDF
leadership as ownership under pressure (without naming the specific
unit), and end with a direct, confident closing.
```

---

## Suggested order of operations

1. Send **Phase 1** prompt to Claude Code. Test that it actually sends you a real Telegram message for a real AllJobs listing before moving on.
2. Once Phase 1 runs reliably in k3s for a day or two, send **Phase 2**.
3. Only after the dashboard is stable, send **Phase 3**.

Each phase's prompt assumes the previous phase's code already exists in the repo — Claude Code will need the actual codebase in context (open the project folder), not just this text.

---

## Optional: design skills for Phase 2

Two community Claude Code skills are useful for the dashboard's UI —
one for building, one for reviewing afterward. They're optional but
recommended given the specific design bar (green, liquid-glass, RTL)
in this spec.

### 1. taste-skill — use while building

Improves the initial visual quality of what Claude Code generates
(layout, spacing, motion) instead of a generic/boilerplate look.
Install once, before sending the Phase 2 prompt:

```
npx skills add https://github.com/Leonxlnx/taste-skill
```

This installs several variants; the default (`design-taste-frontend`)
is a safe general choice. If you want a calmer/more premium feel
(closer to the liquid-glass aesthetic), install the soft variant
instead or in addition:

```
npx skills add https://github.com/Leonxlnx/taste-skill --skill "high-end-visual-design"
```

Once installed, Claude Code will apply it automatically while building
the Phase 2 dashboard — no change needed to the Phase 2 prompt itself.

### 2. apple-design-skill — use after building, as a review pass

A design *auditor*, not a builder — it checks finished UI against
Apple's Human Interface Guidelines (generalized to be framework
agnostic), including a dedicated Liquid Glass guide and RTL/accessibility
coverage. Use it after the dashboard renders, not before.

Install for Claude Code:

```
git clone https://github.com/dickwu/apple-design-skill.git
claude install-skill ./apple-design-skill
```

Then, once Phase 2 is built and running, send the **design review pass
prompt** included at the end of the Phase 2 section above. It asks
Claude Code to check the glass-panel implementation, color contrast,
and RTL/accessibility behavior against the skill's reference docs, and
to fix what it finds — high-priority issues first.

**Order matters:** install taste-skill *before* Phase 2 (it shapes what
gets built), and run apple-design-skill *after* Phase 2 is functional
(it reviews what already exists). Running the reviewer before there's
real UI to look at wastes the pass.
