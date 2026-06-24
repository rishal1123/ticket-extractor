# Ticket Extractor - Claude Documentation

## Overview

Ticket Extractor is a web scraping application that extracts tickets from multiple ISP portal systems (Dhiraagu, Ooredoo, ROL, Medianet) and syncs them with a Znuny ticketing system. It provides a dashboard for monitoring tickets, staff performance, and extraction status.

## Architecture

The application follows MVC (Model-View-Controller) pattern with a service layer:

```
Extractor/
├── main.py              # CLI entry point, runs extraction + dashboard
├── app.py               # MVC FastAPI app (recommended entry point)
├── dashboard.py         # Legacy entry point (deprecated, redirects to app.py)
├── database.py          # SQLite database operations (Repository, ~2970 lines)
├── config.py            # Configuration from DB + .env fallback + APP_VERSION
├── znuny_client.py      # Znuny integration via Generic Interface REST API (httpx)
│
├── models/              # Data Models
│   └── ticket.py        # Ticket dataclass with serialization
│
├── services/            # Service Layer - Business Logic
│   ├── __init__.py      # Service exports
│   ├── extraction_service.py  # Portal extraction logic
│   ├── znuny_service.py       # Znuny sync logic (3-layer caching)
│   ├── stats_service.py       # Statistics/analytics/CSV export
│   ├── config_service.py      # Configuration management (DB-stored)
│   └── scheduler_service.py   # Background job scheduling (dual-thread)
│
├── controllers/         # Controller Layer - HTTP Handlers
│   ├── __init__.py      # Router exports
│   ├── dependencies.py  # DI, error handling, query params, response helpers
│   ├── pages.py         # HTML page routes
│   ├── api.py           # JSON API routes (/api/*)
│   ├── admin.py         # Admin API routes (/api/admin/*, /api/settings/*)
│   ├── field_visits.py  # Site visit routes (/api/field-visits/*)
│   └── znuny_only.py    # Znuny-only ticket routes (/api/znuny-only/*)
│
├── extractors/          # Portal-specific scrapers (Playwright)
│   ├── __init__.py      # Exports all 4 extractors
│   ├── base.py          # BaseExtractor abstract class
│   ├── dhiraagu.py      # Dhiraagu AFAS extractor
│   ├── ooredoo.py       # Ooredoo FMS extractor
│   ├── rol.py           # ROL Kayako extractor
│   └── medianet.py      # Medianet CRM.COM extractor (SPA)
│
├── middleware/           # HTTP Middleware
│   ├── __init__.py      # Middleware exports
│   └── security.py      # Rate limiting, security headers, input sanitization
│
├── templates/           # View Layer - Jinja2 HTML templates
│   ├── base.html        # Base template with navbar, modal, CSS
│   ├── dashboard.html   # Main dashboard (extends base.html)
│   ├── tickets.html     # All tickets view (extends base.html)
│   ├── staff_stats.html # Staff performance stats (extends base.html)
│   ├── staff_detail.html# Individual staff performance detail
│   ├── reports.html     # Reports page with date-filtered statistics
│   ├── field_visits.html# Site visits management (extends base.html)
│   ├── znuny_tickets.html # Znuny-only tickets (extends base.html)
│   └── admin.html       # Admin panel with Status, Staff & Config tabs
│
├── static/              # Static assets
│   └── js/common.js     # Shared JavaScript functions
│
├── utils/               # Utilities
│   ├── browser.py       # Playwright browser manager + asyncio monkeypatch
│   └── logger.py        # Logging utilities (console + rotating file)
│
├── data/                # Runtime data (gitignored)
│   ├── tickets.db       # SQLite database (includes credentials in app_settings)
│   └── browser_sessions/# Playwright persistent browser contexts per portal
│
├── .env                 # Environment variables (local dev fallback only)
├── entrypoint.sh        # Docker entrypoint (DB migration check + app start)
├── Dockerfile           # Docker container configuration (Playwright + Chromium)
└── docker-compose.yml   # Docker Compose orchestration
```

### Layer Responsibilities

| Layer | Directory | Responsibility |
|-------|-----------|----------------|
| **Model** | `models/` | Data structures, serialization |
| **View** | `templates/` | Jinja2 HTML templates, UI rendering |
| **Controller** | `controllers/` | HTTP request handling, routing, dependency injection |
| **Service** | `services/` | Business logic, orchestration |
| **Repository** | `database.py` | Data persistence, queries, migrations |
| **Middleware** | `middleware/` | Security headers, rate limiting, input sanitization |
| **Utility** | `utils/` | Browser management, logging |

### Entry Points
- `app.py` - Main entry point (recommended, MVC architecture with FastAPI lifespan)
- `main.py` - CLI with options for extraction modes (--once, --portal, --dashboard-only)
- `dashboard.py` - Legacy entry point (deprecated, redirects to app.py)

## Browser Technology

**Playwright** (sync API) is used for all ISP portal web scraping. Each portal gets its own Chromium instance with persistent browser contexts for session reuse. **Znuny** is no longer scraped — it is accessed through its Generic Interface REST API (`httpx`).

| Component | Technology | Notes |
|-----------|------------|-------|
| ISP Extractors | Playwright sync API | Per-portal persistent context in `data/browser_sessions/{portal}/` |
| Znuny Client | Generic Interface REST API (httpx) | No browser; agent UserLogin+Password auth, self-signed cert (`verify=False`) |
| Browser Manager | `utils/browser.py` | Thread-local Playwright instances, 1920x1080 viewport, asyncio monkeypatch |

### Playwright Asyncio Monkeypatch

`utils/browser.py` applies a module-level monkeypatch to `PlaywrightContextManager.__enter__` that clears the asyncio running-loop marker before Playwright's own check runs. This is required because:

1. uvicorn's main thread has a running asyncio event loop
2. Playwright's sync API refuses to start if it detects a running asyncio loop
3. After a Playwright greenlet session crashes or isn't stopped cleanly, the stale "running" loop marker persists in the thread
4. The monkeypatch intercepts at the exact point of failure, clearing `asyncio._set_running_loop(None)` right before Playwright checks

This single global fix covers all Playwright call sites (BrowserManager, extractors) regardless of thread. (The Znuny client no longer uses Playwright — it calls the REST API via `httpx` — so it is unaffected by this.)

**IMPORTANT:** Never call `pw.stop()` to shut down Playwright — it can hang forever if the browser was killed mid-operation. Instead, kill the Playwright driver process by PID using `psutil.Process(pid).kill()`. The driver PID is at `pw._impl_obj._connection._transport._proc.pid`.

### Memory Limits Per Portal

| Portal | Memory Limit (default) | Images | Timeout | Notes |
|--------|-------------|--------|---------|-------|
| Dhiraagu | 2000 MB | on | 10s (default) | Runs **Firefox** headed under Xvfb in-app (`BROWSER_ENGINE="firefox"`); Chrome's renderer crashes in the container, Gecko renders reliably and passes Cloudflare. Images ON for the challenge |
| Ooredoo | 1500 MB | off | 10s (default) | DataTables-based portal |
| ROL | 1500 MB | off | 30s | Kayako helpdesk (slow) |
| Medianet | 1500 MB | off | 60s | React SPA, uses `wait_until="commit"` |
| Znuny | N/A | — | 10s | Self-signed cert (`ignore_https_errors=True`) |

**Configurable via Admin → Config → "Browser Memory Limits".** Per-portal limits are stored in `app_settings` (`mem_limit_<portal>`, MB, clamped 256–8192) via `Database.get_memory_limits()` / `set_memory_limits()` and `GET/POST /api/settings/memory-limits`. The extractor resolves the live value with `BaseExtractor.memory_limit_mb()` (config override → class default `MEMORY_LIMIT_MB`); takes effect on the next browser reset (no restart needed). The values above are the defaults when unset.

**Memory reduction:** all browsers launch with Site Isolation disabled (`--disable-features=IsolateOrigins,site-per-process`, collapses per-site/iframe renderer processes), a renderer-process cap, tiny disk/media caches, and no software rasterizer (`utils/browser.py` `CHROMIUM_ARGS`). Non-Cloudflare portals also disable image loading (`DISABLE_IMAGES = True` → `--blink-settings=imagesEnabled=false`). When a browser exceeds its limit, `_check_memory_and_reset()` recycles it (session persists on disk).

### Session & Error Recovery
- Browser sessions persist to disk (cookies, localStorage) across restarts
- On extraction failure: browser killed, retried up to 3 times with 5s delays
- After 3 consecutive failures: session directory cleared (`shutil.rmtree`) for fresh start next cycle
- Memory over limit: browser reset but session data preserved on disk
- Zero-ticket debouncing: 3 consecutive zero-ticket cycles required before marking tickets complete
- Playwright driver processes killed by PID (never `pw.stop()` which can hang)
- Asyncio monkeypatch in `utils/browser.py` prevents "Sync API inside asyncio loop" errors
- Dead worker threads auto-detected and restarted by worker health check (every 5 min)

## Template Architecture

All templates use Jinja2 inheritance from `base.html`:

### base.html (Parent Template)
Provides:
- Bootstrap 5 CSS/JS + Bootstrap Icons
- Responsive mobile CSS (breakpoints at 768px, 576px)
- Collapsible navbar with hamburger menu
- Loading overlay (full-screen spinner)
- Ticket detail modal (shared across all pages)
- Common CSS variables (--primary-color, --secondary-color, etc.)
- Portal badge colors (Dhiraagu orange, Ooredoo purple, ROL blue, Medianet teal)

### Template Blocks
| Block | Purpose |
|-------|---------|
| `title` | Page title |
| `nav_icon` | Bootstrap icon name for navbar |
| `nav_title` | Full navbar title (desktop) |
| `nav_title_short` | Short title (mobile) |
| `nav_extra` | Extra content in navbar |
| `nav_buttons` | Additional navbar buttons |
| `extra_css` | Page-specific CSS |
| `content` | Main page content |
| `extra_html` | Extra HTML after content |
| `script` | Page-specific JavaScript |

### Shared JavaScript (static/js/common.js)
Key functions available to all pages:
- `toMaldivesTime(dateStr)` - Convert ISO to MVT Date object
- `formatMaldivesDateTime(iso)` - Format as "05 Feb 19:55" (GB locale, 24h)
- `formatMaldivesDateTimeFull(iso)` - Format as "05 Feb 2026 19:55 MVT"
- `formatTimeDiff(ms)` - Format milliseconds as "5m", "2h 30m", "1d 5h"
- `formatRelativeTime(iso)` - Format as "5m ago", "2h ago"
- `getNowMaldives()` - Current time in MVT
- `getDateRange(filter)` - Get date range for filters (today, yesterday, week)
- `showTicketDetail(ticketId, callbacks)` - Show ticket detail modal (fetches ticket + articles + visits)
- `renderTicketRow(ticket, onClick)` - Render ticket table row with portal badge, status, time-to-create
- `renderPagination(total, page, pageSize, callback)` - Render Bootstrap pagination
- `showLoading(show)` - Show/hide loading overlay
- `escapeHtml(text)` - XSS prevention via DOM text node
- Global `fetch()` override: adds `cache: 'no-store'` to all local API calls

## Key Components

### 1. Database Schema (database.py)

13 tables total:

**tickets table:**
| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER | Primary key |
| portal | TEXT | Source portal (dhiraagu, ooredoo, rol, medianet) |
| ticket_id | TEXT | Portal's ticket ID (UNIQUE with portal) |
| address | TEXT | Customer address |
| account | TEXT | Account ID (ROL display ID, Medianet account #) |
| customer_name | TEXT | Customer name |
| ticket_type | TEXT | Type of ticket |
| portal_created_at | DATETIME | When ticket was created on ISP portal |
| service_type | TEXT | Service type |
| status | TEXT | Ticket status |
| kpi | TEXT | KPI information |
| notes | TEXT | Portal notes |
| in_znuny | BOOLEAN | Whether ticket exists in Znuny |
| znuny_ticket_id | TEXT | Znuny ticket number |
| znuny_created_at | DATETIME | When ticket was created in Znuny |
| znuny_created_by | TEXT | Staff who created ticket in Znuny |
| znuny_address | TEXT | Address from Znuny phone ticket |
| znuny_url | TEXT | Direct URL to ticket in Znuny |
| portal_url | TEXT | Direct URL to ticket in ISP portal |
| znuny_search_count | INTEGER | Account search attempts for closed Znuny tickets (max 3) |
| created_at | DATETIME | When ticket was first extracted (entered to extractor) |
| updated_at | DATETIME | Last update time |
| completed_at | DATETIME | When ticket was marked complete |

**znuny_articles table:** Stores article/note history from Znuny tickets.
| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER | Primary key |
| ticket_id | INTEGER | FK to tickets.id |
| znuny_ticket_id | TEXT | Znuny ticket number |
| article_number | INTEGER | Article sequence number (UNIQUE with znuny_ticket_id) |
| sender | TEXT | Article sender |
| via | TEXT | Communication channel |
| subject | TEXT | Article subject |
| created_at | DATETIME | Article creation time |
| created_at_str | TEXT | Original time string |
| created_by | TEXT | Staff who created article |
| body | TEXT | Article content |

**znuny_tickets table:** All Znuny tickets (both ISP-linked and orphan).
| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER | Primary key |
| znuny_ticket_id | TEXT | Znuny ticket number (UNIQUE) |
| title | TEXT | Ticket title/subject |
| state | TEXT | open / closed |
| queue | TEXT | Queue assignment |
| priority | TEXT | Ticket priority |
| owner | TEXT | Ticket owner/assignee |
| created_at | DATETIME | Znuny creation time |
| created_by | TEXT | Staff who created ticket |
| closed_at | DATETIME | When ticket was closed |
| time_to_close_minutes | REAL | Duration from creation to close |
| article_count | INTEGER | Number of articles |
| last_article_by | TEXT | Who created last article |
| last_article_at | DATETIME | When last article was created |
| znuny_url | TEXT | Direct URL to Znuny ticket |
| isp_ticket_id | INTEGER | FK to tickets.id (if linked to ISP portal) |
| first_seen_at | DATETIME | When first discovered by sync |
| updated_at | DATETIME | Last update time |

**site_visits table:** OAN Site Visit tracking.
| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER | Primary key |
| ticket_id | INTEGER | FK to tickets.id (ISP portal ticket) |
| znuny_ticket_id | TEXT | Parent Znuny ticket number |
| article_id | INTEGER | Article number (UNIQUE with znuny_ticket_id) |
| site_type | TEXT | Type of site |
| service_provider | TEXT | ISP provider name |
| scheduled_time | TEXT | Scheduled time slot |
| assigned_to | TEXT | Staff assigned to visit |
| visit_date | DATE | Scheduled visit date (YYYY-MM-DD) |
| article_created_at | DATETIME | When the site visit article was created |
| ticket_completed_at | DATETIME | When ticket was closed/completed |
| time_taken_minutes | INTEGER | Duration in minutes |
| status | TEXT | pending / completed |
| address | TEXT | Customer address |
| customer_name | TEXT | Customer name |
| znuny_url | TEXT | Direct URL to Znuny ticket |
| created_at | DATETIME | When first extracted (MVT timezone) |
| updated_at | DATETIME | Last update time |

**Other tables:**
| Table | Purpose |
|-------|---------|
| `extraction_logs` | Extraction run history with counts |
| `login_stats` | Portal login events (attempt, success, failed, session_reused) |
| `system_logs` | Application event logging (level, source, message) |
| `app_settings` | Configuration key-value store (cfg_* prefix for portal creds) |
| `staff_performance_daily` | Daily performance cache (staff, date, tickets, on_time) |
| `ticket_notes_history` | Note change tracking |

### 2. Ticket Model (models/ticket.py)

```python
@dataclass
class Ticket:
    portal: str
    ticket_id: str
    address: Optional[str]
    account: Optional[str]           # ROL display ID / Medianet account #
    customer_name: Optional[str]
    ticket_type: Optional[str]
    portal_created_at: Optional[datetime]  # When created on ISP portal
    service_type: Optional[str]
    status: Optional[str]
    kpi: Optional[str]
    notes: Optional[str]
    created_at: Optional[datetime]         # When first extracted to our DB
    updated_at: Optional[datetime]
    completed_at: Optional[datetime]       # When disappeared from portal
    id: Optional[int]
    in_znuny: bool
    znuny_ticket_id: Optional[str]
    znuny_created_at: Optional[datetime]   # When created in Znuny
    znuny_created_by: Optional[str]        # Staff who created in Znuny
    znuny_address: Optional[str]           # Address from Znuny
    znuny_url: Optional[str]               # Direct URL to Znuny ticket
    portal_url: Optional[str]              # Direct URL to ISP portal ticket
    time_to_create_minutes: Optional[float] # Pre-calculated time difference
```

Methods: `to_dict()`, `from_dict(cls, data)`

### 3. Extractors (extractors/)

All extractors inherit from `BaseExtractor` and implement:
- `login()` - Portal authentication
- `extract_tickets()` - Scrape tickets from portal
- `logout()` - Cleanup
- `is_logged_in()` - Session check

**Session persistence:** Playwright persistent browser contexts stored in `data/browser_sessions/{portal}/` survive browser restarts and memory resets.

**Extract-once / no notes (load reduction):** ISP portals are only used for the *original* extraction of a ticket. The first time a ticket is seen, the extractor opens its detail page once to capture full fields (address/customer/account/type) — but **notes are never fetched**. On every later cycle, an already-known ticket (still active in the DB) is registered as present only: `base.run()` routes it to `Database.touch_tickets_seen()` (updates `updated_at`, no field overwrite) instead of re-opening its detail page. This avoids re-clicking every open ticket each cycle. Ongoing ticket updates (notes, status changes, articles) come from the Znuny sync, not the ISP portals.
- `BaseExtractor.is_known_ticket(ticket_id)` — was the ticket active in the DB at run start? (set from `get_active_ticket_ids` before `extract_tickets()`).
- `BaseExtractor.presence_ticket(ticket_id)` — minimal Ticket emitted for known tickets so they count as found (not completed) without a detail fetch.
- Ooredoo no longer opens detail pages at all (its list rows already carry every kept field).

**Completion tracking:** When a ticket disappears from the portal, it's automatically marked as complete (with 3-cycle debounce for zero-ticket results). `fetch_completion_notes()` is a no-op now (notes are not fetched from ISP portals).

**Error recovery:** After 3 failed extraction attempts, session directory is cleared for a completely fresh start on the next cycle.

**Per-portal memory limits:** Override `MEMORY_LIMIT_MB` class variable in subclass (e.g., Medianet uses 1500 MB vs default 800 MB).

### 4. Services Layer (services/)

Business logic separated from HTTP handlers:

| Service | File | Responsibility |
|---------|------|----------------|
| `ExtractionService` | extraction_service.py | Run portal extractions, maps portal names to extractor classes |
| `ZnunyService` | znuny_service.py | Znuny sync, ticket checking, article fetch, site visit extraction |
| `StatsService` | stats_service.py | Dashboard stats, staff metrics, reports, CSV exports |
| `ConfigService` | config_service.py | DB-stored config management, password masking |
| `SchedulerService` | scheduler_service.py | Background job scheduling (dual persistent worker threads) |

### 5. Controllers Layer (controllers/)

HTTP route handlers with dependency injection:

| Router | File | Prefix | Purpose |
|--------|------|--------|---------|
| `pages_router` | pages.py | (none) | HTML pages (/, /tickets, /staff, /admin, etc.) |
| `api_router` | api.py | `/api` | Core JSON API (~30 endpoints) |
| `admin_router` | admin.py | `/api/admin` | Admin operations (~15 endpoints) |
| `settings_router` | admin.py | `/api/settings` | Settings management (3 endpoints) |
| `field_visits_router` | field_visits.py | `/api/field-visits` | Site visit management (6 endpoints) |
| `znuny_only_router` | znuny_only.py | `/api/znuny-only` | Znuny-only tickets (4 endpoints) |

**Shared infrastructure** (`dependencies.py`):
- `get_db()` - Database singleton injection
- `@handle_errors("operation")` - Consistent error handling decorator
- `DateFilterParams` / `PaginationParams` / `TicketFilterParams` - Query parameter models
- `paginated_response()` / `success_response()` / `error_response()` - Response helpers

### 6. Middleware (middleware/)

**SecurityMiddleware** (`security.py`):
- Rate limiting: 120 req/min, 20 req/sec per client IP
- Security headers: X-Content-Type-Options, X-Frame-Options, X-XSS-Protection, Referrer-Policy
- Cache control: no-cache for API/page responses, allows caching for /static
- Exempt paths: /api/health, /static, /favicon.ico
- Helper functions: `sanitize_input()`, `sanitize_sql_like()`, `validate_ticket_id()`, `validate_portal_name()`

### 7. API Endpoints

**Pages:**
- `/` - Main dashboard with stats
- `/tickets` - All tickets with filtering (includes staff filter)
- `/staff` - Staff performance statistics with % On Time metrics
- `/staff/{name}` - Individual staff performance detail page
- `/field-visits` - Site visits/field visits management
- `/znuny-tickets` - Znuny-only tickets (orphan tickets not linked to ISP portals)
- `/reports` - Reports with date-filtered statistics (Today, Yesterday, 7 Days, 30 Days)
- `/admin` - Admin panel with Status, Staff Management & Config tabs
- `/login` - Admin login page

**Core API Endpoints:**
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/health` | GET | Health check (DB, scheduler, storage status) |
| `/api/stats` | GET | Dashboard statistics |
| `/api/portals` | GET | List available portals |
| `/api/tickets` | GET | List tickets (filters: portal, status, staff, dates, search) |
| `/api/tickets/{id}` | GET | Single ticket details |
| `/api/tickets/{id}/check-znuny` | POST | Check if ticket exists in Znuny |
| `/api/tickets/{id}/sync-znuny` | POST | Fetch Znuny details for ticket |
| `/api/tickets/{id}/znuny-articles` | GET | Get Znuny articles |
| `/api/tickets/{id}/site-visits` | GET | Get site visits for ticket |
| `/api/tickets/check-all-znuny` | POST | Bulk check all tickets in Znuny |
| `/api/articles` | GET | List articles (date_from, date_to, staff filters) |
| `/api/staff-stats` | GET | Basic staff stats |
| `/api/staff-stats-detailed` | GET | Detailed stats with on-time metrics |
| `/api/staff/{name}/tickets` | GET | Tickets created by staff |
| `/api/staff/{name}/znuny-tickets` | GET | Znuny-only tickets by staff |
| `/api/staff/{name}/articles` | GET | Articles by staff |
| `/api/staff/{name}/performance` | GET | 14-day daily performance trend |
| `/api/staff-names` | GET | List of all staff names |
| `/api/staff-delays` | GET | Delayed tickets grouped by staff |
| `/api/znuny-sync-status` | GET | Sync status (count not in Znuny, last sync time) |
| `/api/sync-znuny-details` | POST | Bulk sync Znuny details |
| `/api/extraction-logs` | GET | Extraction run history |
| `/api/reports/staff-csv` | GET | Export staff stats as CSV |
| `/api/tickets-csv` | GET | Export tickets as CSV |
| `/api/config` | GET/POST | Get (masked) / Save config |
| `/api/config/raw` | GET | Get config (passwords visible) |
| `/api/config/upload` | POST | Upload .env file |

**Admin Endpoints:**
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/admin/scheduler-status` | GET | Scheduler status & next run |
| `/api/admin/trigger-extraction` | POST | Manually trigger extraction |
| `/api/admin/login` | POST | Admin authentication |
| `/api/admin/change-password` | POST | Change admin password |
| `/api/admin/login-summary` | GET | Portal login statistics |
| `/api/admin/login-stats` | GET | Login event history |
| `/api/admin/system-logs` | GET | System logs (level, search, pagination) |
| `/api/admin/log-stats` | GET | Log statistics summary |
| `/api/admin/clear-old-logs` | POST | Purge logs older than N days |
| `/api/admin/delayed-tickets` | GET | Delayed tickets analysis |
| `/api/admin/staff-list` | GET | All staff with counts per source |
| `/api/admin/staff-merge-preview` | GET | Preview merge operation |
| `/api/admin/staff-merge` | POST | Execute staff name merge |
| `/api/admin/report-portal-stats` | GET | Portal stats for reports |
| `/api/settings` | GET | Get all app settings |
| `/api/settings/performance-thresholds` | GET/POST | Get/update performance thresholds |
| `/api/settings/operating-hours` | GET/POST | Get/update operating hours schedule |

**Field Visits Endpoints:**
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/field-visits` | GET | List site visits with filters |
| `/api/field-visits/{id}` | PUT | Update site visit |
| `/api/field-visits/sync` | POST | Sync site visits from Znuny |
| `/api/field-visits/assigned-staff` | GET | List assigned staff |
| `/api/field-visits/staff-stats` | GET | Per-staff statistics |
| `/api/field-visits/by-date` | GET | Visits aggregated by date |

**Znuny-Only Endpoints:**
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/znuny-only/stats` | GET | Summary stats |
| `/api/znuny-only/tickets` | GET | List with filters (state, creator, linked, queue, owner) |
| `/api/znuny-only/staff-stats` | GET | Stats by creator |
| `/api/znuny-only/staff-names` | GET | Staff who created Znuny-only tickets |
| `/api/znuny-only/queue-names` | GET | Distinct queue names |
| `/api/znuny-only/owner-names` | GET | Distinct owner names |

### 8. Znuny Integration (znuny_client.py)

Calls the Znuny **Generic Interface REST API** (web service `GenericTicketConnectorREST`) at `https://10.241.1.110` using `httpx` (no browser). Auth: agent `UserLogin`+`Password` (`ZNUNY_USERNAME`/`ZNUNY_PASSWORD`) sent with every request; TLS verification disabled for the self-signed cert.

Operations used (standard route mappings):
- `POST /TicketSearch` → returns matching `TicketID`s (by `Title`, `Fulltext`, `Services`/`StateType`, `TicketNumber`)
- `GET /Ticket/:TicketID` → full ticket + articles (`AllArticles=1`); ids are comma-joined to batch (chunks of 20)

What it does:
- Searches tickets by title containing the portal ticket ID (`Title=*id*`)
- Fetches ticket details: creator, creation time, articles (with full bodies)
- Extracts site visits from "OAN Site Visit Arranged" articles
- Searches tickets by account number via `Fulltext=*account*`
- TTL + article-count caching for optimized sync cycles

**Creator-name resolution:** the API returns the ticket/article creator as a numeric `CreateBy` id. Agent-authored articles (`SenderType=agent`) expose the staff member's display name in `From`, so a `{user_id: name}` map is harvested (class-level, persists across the sync) and used to resolve ticket/article creators. Owner login is the fallback when an id hasn't been seen yet.

**Times:** the API returns timestamps in Znuny's system timezone, which is **UTC** (`OTRSTimeZone`). `_parse_dt()` parses them as UTC and converts to Maldives time (`MALDIVES_TZ`, +05:00). Do **not** tag the raw string as `MALDIVES_TZ` directly — that stores everything 5 hours early.

**Key Classes:**
- `ZnunyClient` - Main client with class-level shared HTTP session + caches (persist across instances)
- `ZnunyArticle` - Article/note data structure (article_number, sender, via, subject, created_at, created_by, body)
- `ZnunyTicketDetails` - Full ticket details with articles list (includes owner, state, queue, priority, total_article_count)
- `SiteVisit` - Parsed site visit data (includes address, customer_name)
- `ZnunyClientSync` - Backward compat wrapper

**Key Methods:**
- `get_open_tickets()` - Open ISP-service tickets via `TicketSearch`+`TicketGet` (cached 6 min)
- `search_by_title()` - Search tickets by title substring (`Title=*term*`)
- `check_ticket_sync()` - Check if ISP ticket exists in Znuny
- `get_ticket_details()` - Cached detail fetching (TTL + article-count check)
- `search_closed_by_account()` - Search tickets (any state) by account via `Fulltext`
- `extract_isp_ticket_id_from_title()` - Parse ISP portal/ticket_id from Znuny title (pure regex)
- `get_site_visit_tickets()` - Get tickets with "site visit" in title

**Class-level shared state** (persists across sync cycles):
- `_shared_http` - httpx client (connection reuse)
- `_shared_open_tickets_cache`, `_shared_cache_timestamp` - Open-ticket cache (6min TTL)
- `_shared_details_cache` - Per-ticket detail cache (max 200 entries)
- `_number_to_id_cache` - Ticket number → internal TicketID map
- `_user_names` - Harvested user id → display-name map
- `_page_lock` - RLock for thread-safe cache mutations
- Legacy `_shared_playwright`/`_shared_context`/`_shared_page`/`_shared_logged_in` attrs are retained (always cleared/None) so the scheduler's browser-reset code keeps working unchanged.

### 9. Configuration (DB-stored)

Portal credentials and settings are stored in the SQLite database (`app_settings` table) with a `cfg_` key prefix. The `.env` file serves as a fallback for local development.

**Config priority:** Database → `.env` file → default value

**Config keys in `app_settings` table:**
```
cfg_DHIRAAGU_URL, cfg_DHIRAAGU_USERNAME, cfg_DHIRAAGU_PASSWORD
cfg_OOREDOO_URL, cfg_OOREDOO_USERNAME, cfg_OOREDOO_PASSWORD
cfg_ROL_URL, cfg_ROL_USERNAME, cfg_ROL_PASSWORD
cfg_MEDIANET_URL, cfg_MEDIANET_USERNAME, cfg_MEDIANET_PASSWORD
cfg_ZNUNY_URL, cfg_ZNUNY_USERNAME, cfg_ZNUNY_PASSWORD
cfg_EXTRACTION_INTERVAL_MINUTES, cfg_ZNUNY_SYNC_INTERVAL_MINUTES
cfg_DASHBOARD_HOST, cfg_DASHBOARD_PORT
operating_hours_enabled, operating_hours_start, operating_hours_end
```

**How credentials are populated:**
1. **Upload .env file** via Admin → Config tab → "Upload .env" button
2. **Manual edit** via Admin → Config tab form fields
3. **Fallback**: `.env` file values via `os.getenv()` (local dev only)

**Architecture note:** `config.py` uses raw `sqlite3` to read from `app_settings` (not the `Database` class) to avoid circular imports, since `database.py` imports `Config` for `DATABASE_PATH`.

**Auto-restart:** After uploading .env or saving config, extractions automatically restart with new credentials.

**Credential guard:** Extractions are skipped if no portal credentials exist in the database yet.

**Current version:** `APP_VERSION = "1.6.2"` in `config.py`

## Important Timestamps

The system tracks multiple timestamps for each ticket:

1. **portal_created_at** - When the ticket was created on the ISP portal
2. **created_at** - When the ticket was first extracted into this app (entered to extractor)
3. **znuny_created_at** - When the ticket was created in Znuny
4. **updated_at** - When the ticket record was last updated
5. **completed_at** - When the ticket was marked complete (disappeared from portal)

### Time to Create Calculation
**Time to Create** = `znuny_created_at - created_at`

This measures the time difference between when a ticket entered the extractor and when it was created in Znuny. A positive value indicates staff created the Znuny ticket after the extractor picked it up.

**Note:** The calculation uses `created_at` (entered to extractor) NOT `portal_created_at` (created on ISP portal) because staff can only act on tickets after they appear in the extractor.

## Running the Application

```bash
# Recommended: Start MVC app with scheduler
python app.py

# Alternative: Using main.py with CLI options
python main.py
python main.py --once              # Single extraction run, then exit
python main.py --portal dhiraagu   # Extract from specific portal only
python main.py --dashboard-only    # Run web server only (no extraction)
python main.py --no-dashboard      # Run scheduler only (no web server)
python main.py --visible           # Show browser windows (default: headless)

# Legacy (deprecated)
python dashboard.py
```

The app runs on http://localhost:8000 by default.

### Development: Restarting After Code Changes

**IMPORTANT:** After modifying Python files, always restart the server to load new code:

```bash
# Windows (git-bash) - Kill existing Python processes and restart
taskkill //F //IM python.exe
sleep 2 && python app.py

# Or find and kill process on port 8000
netstat -ano | grep 8000  # Find PID
taskkill //F //PID <pid>
```

**Common issues:**
- Old server still running on port 8000 → new code not loaded
- Port already in use error → kill the existing process first
- Changes not reflected → clear `__pycache__` directories

## Docker Deployment

### Quick Start

```bash
# Build and run
docker compose up -d

# View logs
docker logs ticket-extractor -f

# Stop
docker compose down
```

### First-Time Setup

1. Deploy the container: `docker compose up -d`
2. Open `http://server:8003/admin` → Config tab
3. Upload your `.env` file (or manually enter credentials)
4. Extractions start automatically after credentials are saved

No `.env` file mount needed - credentials are stored in the SQLite database inside the persistent volume.

### Portainer Deployment

Deploy as a **standalone Compose stack** (a "Docker Standalone" environment, not Swarm) — the stack relies on `build:`, `shm_size`, healthcheck and `depends_on`, which Swarm ignores. Recommended: a **Git repository stack** pointing at this repo so Portainer builds the image (the `Dockerfile` is needed; a paste-only Web Editor stack can't `build:`).

- **Ports:** app on host `8003`, **noVNC on host `6080`** (`http://<host>:6080/vnc.html`) for the manual Cloudflare bypass.
- **noVNC password:** add a stack **Environment variable** `VNC_PASSWORD` in the Portainer UI — the compose reads `${VNC_PASSWORD:-}`. If left unset, noVNC is open with no auth, so set it whenever `6080` is reachable.
- **Restart vs. redeploy:** Portainer's container **Restart** button does a `docker restart` (same container, `/tmp` preserved). `entrypoint.sh` cleans the stale `/tmp/.X99-lock` and verifies Xvfb before exporting `DISPLAY`, so headed-Chrome/Dhiraagu survives restarts. To pick up new code/deps, **Update the stack** / **re-pull & redeploy** (recreates the container).
- **Line endings:** `entrypoint.sh` (and other `*.sh`) are pinned to LF via `.gitattributes`, and the `Dockerfile` strips CR (`sed -i 's/\r$//'`) before `chmod +x`. This prevents the classic `bad interpreter: /bin/bash^M` failure when the stack is built from a Windows-edited repo.

### Dockerfile Details

```dockerfile
FROM python:3.11-slim
# pip install → playwright install-deps chromium → playwright install chromium
# Separate install-deps (apt packages) from browser download for reliability
```

Key points:
- Uses `playwright install-deps chromium` for system dependencies (separate from browser download)
- Then `playwright install chromium` for the browser binary
- `curl` installed for healthcheck
- `entrypoint.sh` runs DB migration check (`Database()` init) before starting the app
- `shm_size: '2gb'` in docker-compose for Chromium stability

### Persistent Volumes

| Volume | Purpose |
|--------|---------|
| `extractor_data:/app/data` | Named volume for persistent data (database with credentials, browser sessions) |

The database is stored at `/app/data/tickets.db` inside the container, persisted via the named volume `ticket-extractor-data`. Credentials are stored in the `app_settings` table within this database.

**Backup the database:**
```bash
docker cp ticket-extractor:/app/data/tickets.db ./backup_tickets.db
```

**Restore database:**
```bash
docker cp ./backup_tickets.db ticket-extractor:/app/data/tickets.db
docker compose restart
```

### Docker Compose Features

- Playwright with Chromium for web scraping
- Named volume for persistent database, credentials, and browser sessions (`ticket-extractor-data`)
- Credentials stored in DB (no `.env` file mount needed)
- Port mapping: 8003 (host) → 8000 (container)
- Automatic restart on failure
- 2GB shared memory for Chromium stability
- Health check with auto-restart on failure
- Log rotation (10MB max, 3 files)
- **FlareSolverr sidecar** (`ghcr.io/flaresolverr/flaresolverr`) for Cloudflare-protected portals (e.g. Dhiraagu); app is wired via `FLARESOLVERR_URL=http://flaresolverr:8191`
- App start is gated on FlareSolverr being healthy (`depends_on: condition: service_healthy`); FlareSolverr healthcheck hits its root URL via `python -m urllib` (no curl/wget assumption)
- `entrypoint.sh` runs the DB init/migration check (`Database()`) and fails fast before launching `app.py`
- **Xvfb (virtual display) for Dhiraagu's headed Chrome.** `entrypoint.sh` cleans any stale `/tmp/.X99-lock` + `/tmp/.X11-unix/X99` (a `restart: unless-stopped` restart reuses the container filesystem, so a leftover lock would make a fresh Xvfb refuse `:99` → `Missing X server or $DISPLAY` → Dhiraagu breaks on every restart after the first), then starts Xvfb and **verifies the socket is live before exporting `DISPLAY`** (so DISPLAY never points at a dead server).
- **noVNC for manual Cloudflare bypass.** `x11vnc` + `websockify` serve the noVNC web client on port `6080`; open `http://<host>:6080/vnc.html` to watch and **drive the headed Chrome by hand** when the automatic Cloudflare bypass can't clear an interactive challenge (Turnstile/CAPTCHA). It drives the *same* persistent browser the extractor uses, so the `cf_clearance` you obtain is saved to the session and reused by later automated cycles. Secure it with `VNC_PASSWORD` (env/compose); if unset, noVNC is open with no auth.
- **Dhiraagu runs Firefox headed in-app.** Chrome's renderer crashes inside the container (the Turnstile widget shows a grey "sad face" — a renderer crash that persists even with WebGL removed, OOM ruled out, and no `SIGILL`). The fix was to switch engines: `DhiraaguExtractor.BROWSER_ENGINE = "firefox"`. Gecko renders reliably headless on Linux (CI workhorse) and still passes Cloudflare when headed. It runs **in the app container** under Xvfb+fluxbox, with noVNC on `:6080` for the manual challenge solve. The other 3 portals run Chromium headless. (An earlier dedicated-Chrome-sidecar-over-CDP approach was removed — `connect_over_cdp` is Chrome-only and the Chrome renderer crashed regardless.)

### Dhiraagu Firefox — internals

- `BrowserManager(engine="firefox")`: `start()` uses `playwright.firefox.launch_persistent_context(...)` instead of chromium. Firefox is configured via `firefox_user_prefs` (`dom.webdriver.enabled=False`, `useAutomationExtension=False`, `permissions.default.image=2` when images are off) — NOT Chrome flags (`CHROMIUM_ARGS`, `--enable-automation`, channel are chromium-only). The Chrome-specific stealth init script is skipped for Firefox (a fake `window.chrome` would be a tell).
- `DhiraaguExtractor`: `BROWSER_ENGINE="firefox"`, `FORCE_HEADED=True`, `BROWSER_CHANNEL=None`, `USE_FLARESOLVERR_FALLBACK=False`. The Playwright Page API is engine-agnostic, so the login/extraction code is unchanged. Profile persists in `data/browser_sessions/dhiraagu/`.
- Auto-pause/auto-resume for manual CF still applies (Dhiraagu keeps `FORCE_HEADED=True`, which flags a persistent challenge for the pause); the operator solves via noVNC in the same headed Firefox window.
- `entrypoint.sh` clears stale browser locks for both engines: Chrome `Singleton*` and Firefox `lock`/`.parentlock`.

### Manual Cloudflare bypass workflow (noVNC) — auto-pause / auto-resume

When a headed portal (Dhiraagu) hits a Cloudflare challenge the browser **can't clear on its own** (interactive Turnstile/CAPTCHA), the extractor **automatically pauses** that portal and waits for a human — it does NOT keep hammering the challenge or wipe the session.

**How auto-pause works** (`extractors/base.py`):
- `navigate_to()` sets `self._cf_manual_required` when a CF challenge persists after auto-clear (and FlareSolverr, if enabled) — but only for `FORCE_HEADED` portals (a headless browser has nothing for an operator to see/click).
- `run()` then sets `BaseExtractor._manual_cf_paused[portal] = True`, returns status `paused`, and **keeps the browser alive and parked on the challenge** (it is NOT killed, the session dir is NOT cleared, and the login-failure cooldown does NOT count it).
- A `system_logs` warning is written telling the operator to open noVNC; the paused portal also appears in `/api/admin/scheduler-status` under `manual_cf_paused`.

**Operator steps:**
1. Open `http://<host>:6080/vnc.html` — you'll see the real Chrome window parked on the Dhiraagu Cloudflare challenge.
2. Solve the Turnstile checkbox / CAPTCHA by hand. (You only need to clear CF — the extractor logs in automatically afterward with the stored credentials.)

**Auto-resume:** on each cycle, `run()` calls `_resume_if_cf_cleared()`, which **passively** checks the parked page (no navigation — so it never reloads the page out from under you mid-solve). Once the page is no longer a CF challenge, it clears the pause and the normal extraction cycle resumes; `login()` reuses the now-cleared page. Because it's the **same persistent context** (`data/browser_sessions/dhiraagu/`), the `cf_clearance` cookie persists and later automated cycles reuse the warmed session.

- Caveat: a `user_data_dir` can only be open in one Chrome at a time, so you can't run a second browser on that profile — you take the wheel of the existing one. If the parked browser dies (crash / the 1-hour restart), the next paused cycle recreates it and re-presents the login page. As noted in the Dhiraagu portal notes, this is an occasional rescue, not a per-cycle need, as long as cadence stays low.

## Scheduler

The background scheduler (managed by `SchedulerService` in `services/scheduler_service.py`) runs two independent persistent worker threads:

| Job | Default Interval | Config Variable | Description |
|-----|-----------------|-----------------|-------------|
| **Portal Extraction** | 5 min | `EXTRACTION_INTERVAL_MINUTES` | Extracts tickets from all configured ISP portals |
| **Znuny Sync** | 3 min | `ZNUNY_SYNC_INTERVAL_MINUTES` | Checks ISP tickets in Znuny, syncs details & site visits |
| **Worker Health Check** | 5 min | N/A | Checks if worker threads are alive, restarts dead ones |
| **Browser Restart** | 1 hour | N/A | Kills all browsers and Playwright drivers, clears all caches |
| **Log Cleanup** | Daily at midnight | N/A | Deletes logs older than 2 days from all log tables |

**Architecture:**
- Two persistent daemon threads (ExtractionWorker, ZnunySyncWorker) stay alive between cycles
- `threading.Event` signals workers to run (avoids creating new threads each cycle)
- Per-worker reset events (`_extraction_reset_requested`, `_znuny_reset_requested`) avoid race conditions
- Skip-on-overlap: if a job is still running when next cycle fires, that cycle is skipped
- Both jobs run immediately on startup, then repeat at configured intervals
- Memory monitoring: logs per-portal and total browser memory usage after each extraction
- **Credential guard:** Jobs skip execution if no portal credentials exist in the database
- **Operating hours:** Configurable daily schedule (default 7 AM - 10 PM MVT). Jobs are skipped outside hours. Configure via Admin → Config → Operating Hours.
- **Worker health check:** Every 5 min, `_worker_health_check()` → `_check_and_restart_workers()` detects dead worker threads and spawns replacements. Outer try/except on worker loops logs `"worker thread DIED"` and enables auto-restart.
- **1-hour browser restart:** Scheduled every 1 hour via `_scheduled_browser_restart()`. Calls `_nuke_all_browsers()` which kills ALL chromium/node processes system-wide via `psutil.process_iter()`, clears all Python-side references (portal browsers, Znuny shared state, all caches including detail cache, open tickets cache, login check). Signals workers to reset and triggers immediate re-extraction/sync.
- **Process killing:** `_nuke_all_browsers()` kills all chromium and playwright-node processes by name (no PID tracking needed). Never calls `pw.stop()` (can hang forever). Workers clear their own thread-local references on next cycle via per-worker reset events.
- **Visual indicators:** Dashboard and admin portal cards show stale state (red pulsing border when >15 min old) and paused state (dimmed/grayed when outside operating hours).

## Portal-Specific Notes

### Dhiraagu
- Portal: Filament (Laravel admin panel) at `https://afas.dhiraagu.com.mv`
- Login: "Third Party" button → email/password form
- Extraction: Table with pagination (`wire:click="nextPage"`)
- Detail page: clicked once per **new** ticket only, to read `[id='data.{field}']` selectors (known tickets are presence-only, no click)
- Notes: not fetched (updates come from Znuny)

### Ooredoo
- Portal: CBS Middleware/FMS at `https://www.ooredoo.mv/webapps/FMS/public/tickets`
- Login: Standard email/password form
- Extraction: DataTables with "Show All" option (tries -1, 100, 50 entries); all kept fields come from the list row, so detail pages are never opened
- Notes: not fetched (updates come from Znuny)

### ROL
- Portal: Kayako helpdesk at `https://support.rol.net.mv/staff/index.php`
- Login: Standard username/password with `expect_navigation()` context
- Display ID (ROL250141) stored in `account` field, internal ID in `ticket_id`
- **Important:** Uses `account` field for Znuny search
- Detail page (for address) is opened once per **new** ticket only; notes not fetched
- High timeout (30s) due to portal slowness

### Medianet
- Portal: CRM.COM React SPA at `https://app.crm.com/crm/service-requests-board`
- Login: Two-step (email first, then password) at lighter `/account/login` URL
- SPA navigation: Uses `wait_until="commit"` (not "load") with 60s timeout
- Board-based Kanban UI with ticket type dropdown (React Select)
- Columns: New, Survey, Installation, etc. (Closed is skipped)
- Higher memory limit: 1500 MB (vs 800 MB default)
- Account # extracted from contact name parentheses via regex
- Board cards carry only id+status; the detail page (everything else) is opened once per **new** ticket only. Known tickets are presence-only, so their fields are captured once at first sighting; notes not fetched

## Common Tasks

### Adding a New Portal
1. Create new extractor in `extractors/` inheriting from `BaseExtractor`
2. Implement `login()`, `extract_tickets()`, `logout()`, `is_logged_in()`
3. Optionally override `MEMORY_LIMIT_MB` for heavier portals
4. Add portal name to `config.py` (`get_all_portals()`, `get_portal_by_name()`)
5. Register in `services/extraction_service.py` `PORTAL_EXTRACTORS` dict
6. Add to `extractors/__init__.py`

### Modifying Ticket Fields
1. Update `models/ticket.py` dataclass
2. Add migration in `database.py` `_init_db()`
3. Update `_row_to_ticket()` in `database.py`
4. Update templates if displaying new fields

### Adding New API Endpoints
1. Choose appropriate controller (api.py, admin.py, field_visits.py, znuny_only.py)
2. Add service method if business logic needed
3. Add database method if data access needed
4. Use `@handle_errors("operation")` decorator
5. Use `db: Database = Depends(get_db)` for database access

### Adding New Dashboard Features
1. Add API endpoint in appropriate controller
2. Create/update template in `templates/`
3. Add any required database methods in `database.py`
4. If shared UI component, add to `static/js/common.js`

## Timezone

All times are in **Maldives Time (UTC+5)** - see `MVT` constant in `database.py` and `MVT_OFFSET` in `common.js`.

## Mobile Support

The application is fully responsive:
- Collapsible navbar with hamburger menu on mobile
- Touch-friendly button sizes (44px minimum)
- Responsive tables with horizontal scroll
- Hidden non-essential columns on small screens (d-none d-md-table-cell)
- Compact filter buttons with shortened labels
- Auto-refresh every 30 seconds on most pages

## Staff Accountability Metrics

### Staff Stats Page (`/staff`)
The main staff statistics page shows all staff with their performance metrics:
- **Tickets Created**: Number of tickets staff created in Znuny
- **Articles Added**: Total articles/notes written by staff
- **Tickets Updated**: Unique tickets where staff added articles
- **% On Time**: Percentage of tickets created within 5 minutes (color-coded)
- **Avg Time**: Average time to create tickets
- **Breakdown**: Count of tickets within 5min / 5-10min / over 10min

**Color Coding:**
- Green row: >=80% on time
- Yellow row: 50-79% on time
- Red row: <50% on time

**Features:**
- Click staff name to view individual performance detail
- Export CSV button for reports
- Date range filter (Today, Yesterday, Week, Month, All) with custom date inputs
- Exclude Negative Time toggle

### Individual Staff Detail Page (`/staff/{name}`)
Detailed performance view for a single staff member:
- **Performance Summary Cards**: Total ISP tickets, Znuny Only, Articles, Site Visits, On Time %, Avg Time
- **Response Time Breakdown**: Horizontal bar chart with percentage boxes (5min/5-10min/over 10min)
- **Daily Performance Trend**: Last 14 days showing tickets, on-time count, percentage, avg time
- **ISP Tickets Table**: Paginated with time-to-create colors
- **Znuny-Only Tickets Table**: Staff's orphan Znuny tickets
- **Articles Table**: All articles created by staff
- **Site Visits Table**: All visits assigned to staff
- **Export CSV**: Export tickets for this staff member

### Delayed Tickets Analysis (Dashboard)
- Filters by date range (Today, Yesterday, 7 Days)
- Filters by delay threshold (>5m, >10m, >30m, >1h)
- Shows: Total delayed, Avg delay, Max delay, Staff involved
- Clickable rows to see ticket details

### Key Performance Indicators
| Metric | Good | Warning | Bad |
|--------|------|---------|-----|
| Time to Create | <5 min | 5-10 min | >10 min |
| % On Time | >=80% | 50-79% | <50% |

### Definition: "On Time"
A ticket is considered "On Time" if it was created in Znuny within **5 minutes** of appearing in the extractor.

**Calculation:** `znuny_created_at - created_at <= 5 minutes`

Performance thresholds are configurable via Admin → Config → Performance Thresholds (good, warning, bad, critical minutes).

### Negative Time / Historical Tickets
Some tickets have **negative time differences** - these are historical tickets where the Znuny ticket existed before the extractor first saw it.

**Exclude Negative Time Toggle:**
- Located in the Staff Stats page filter section
- **ON (default):** Excludes tickets with negative time from all calculations
- **OFF:** Includes all tickets, showing negative avg times for historical data

## Reports Page (`/reports`)

Standalone reports page with date-filtered statistics.

### Report Sections
1. **Summary Stats Cards**: ISP Tickets, Znuny Only, Articles, Site Visits, Avg On Time %, Active Staff
2. **Tickets by Portal**: Breakdown by ISP with total, in Znuny, and pending counts
3. **Performance Breakdown**: Within 5min, 5-10min, Over 10min counts with percentages
4. **Staff Performance Table**: Ranked staff list with metrics (clickable rows to view staff detail)
5. **Tickets**: List of ISP tickets extracted in the period
6. **Articles**: List of Znuny articles created in the period
7. **Site Visits**: List of site visits in the period

## Admin Panel Features

### Status Tab (Admin → Status)
- Scheduler status (Running badge, extraction/sync intervals, next run time)
- "Run Now" button for manual extraction trigger
- Summary cards: Today's logins, sessions reused, reuse rate, total extractions
- Portal status cards with last event per portal
- Login events table, Extraction logs table, Login statistics table
- System logs section with level filter (All/Info/Warn/Error), search, pagination

### Staff Management Tab (Admin → Staff)
Merge duplicate staff names when the same person uses different names:
1. **Staff List Table**: Shows all staff with record counts per source (ISP tickets, Znuny tickets, Articles, Site Visits)
2. **Merge Staff Names**: Select source → target, preview affected records, execute merge
3. **Recent Merges Log**: Shows history of merge operations

### Config Tab (Admin → Config)
- Portal credential sections (Dhiraagu, Ooredoo, ROL, Medianet, Znuny) with toggle password visibility
- App settings (Extraction interval, Dashboard host/port)
- Performance thresholds (Good/Warning/Late/Critical minutes)
- Operating hours (Enable/disable, Start hour, End hour) - daily schedule for extraction & sync
- Security section (Change admin password)
- Upload .env file button

## Site Visits / Field Visits Feature

The application tracks site visits extracted from Znuny "OAN Site Visit Arranged" and "Preventative Maintenance - Site Visit" articles.

### Site Visits Page (`/field-visits`)
- **Summary Cards**: Total visits, Pending, Completed, Avg Duration
- **Pending Section**: Collapsible table of pending visits at top
- **Filters**: Date range (Today, Yesterday, Week, Month, All), Staff, Status
- **Staff Performance Table**: Per-staff breakdown with visit counts and durations
- **Visits Table**: Date, Time, Assigned To, Provider, Site Type, Status, Extracted, Duration, Znuny link, Edit button (admin only)
- **Edit Modal**: Update date, time, assigned staff, status

### Site Visit Completion & Duration

A site visit is marked as **completed** when any of these occur:
1. **Znuny ticket closed** - ticket disappears from open tickets list
2. **Follow-up article added** - a new article is added after the site visit article
3. **ISP ticket completed** - the linked ISP portal ticket disappears

**Duration Calculation:** `Completion Time - Scheduled Visit Time`

## Znuny Sync Optimization

The Znuny integration uses several optimization strategies:

### Open-Ticket Fetching
- `TicketSearch` with `Services=["OAN"]` + `StateType="Open"` returns all open ISP ticket ids
- A single batched `TicketGet` (comma-separated ids, chunks of 20) returns title/state/queue/owner/priority for the whole set
- Replaces the previous service-view page scrape

### TTL-Based Caching
- **Cache TTL**: 6 minutes (configurable via `CACHE_TTL_SECONDS = 360`)
- Open tickets list cached to avoid repeated `TicketSearch`/`TicketGet`
- Ticket details cached per-ticket with TTL validation
- Ticket number → TicketID resolution cached to skip redundant searches

### 2-Layer Detail Fetching
1. **TTL cache hit**: Return cached details instantly (no request)
2. **Article-count check**: `TicketGet` runs, but the cached object is reused if the max article number is unchanged (avoids rebuilding/re-parsing)

### Article Processing
- `TicketGet` with `AllArticles=1` returns every article including full bodies in one call (no per-article clicking)
- `via` derived from `CommunicationChannelID` (1=Email, 2=Phone, 3=Internal); address parsed from the first Phone article body
- Articles stored for ALL Znuny tickets (not just ISP-linked) to enable change tracking

### Smart Skip Logic
- Skips tickets that are already fully synced
- Prioritizes tickets with "site visit" in title
- Step 0 processed ticket IDs tracked to avoid duplicate processing
- Pending visit IDs derived via set arithmetic

### Sync Cycle Steps
- **Step 0**: Sync newly linked ISP tickets (detail fetch for recently matched)
- **Step 1**: Get open tickets from Znuny service view (cached)
- **Step 1.5**: Check unchecked ISP tickets against Znuny open list + search form
- **Step 1.7**: Account search for completed ISP tickets not found in Znuny (5 per cycle, 3-strike rejection)
- **Step 1.6**: Handle closed tickets with pending site visits
- **Step 2**: Process all open tickets (2-layer caching per ticket)
- **Step 3**: Mark closed Znuny tickets

### Account Search (Step 1.7)
When ISP tickets disappear from the portal but weren't found in Znuny open tickets, searches Znuny tickets by account number via the REST API:
1. `TicketSearch` with `Fulltext=*account*` (searches From/To/Cc/Subject/Body, any state)
2. Batched `TicketGet` on the matches for title + `Created` time + URL
3. Match the ISP ticket ID against result titles (with `extract_isp_ticket_id_from_title()` fallback)
- **Rate limited**: 5 tickets per sync cycle
- **3-strike rule**: After 3 failed searches, ticket is marked as "Rejected on Portal"
- **Tracked by**: `znuny_search_count` column on tickets table
- **Data extracted**: `znuny_created_at` and `znuny_url` stored via `update_znuny_details()`

**Results:** Steady-state sync cycle runs in ~2-3 minutes including account searches. Step 2 completes in <2s when all cache hits.

## Portal & Znuny URLs

| Portal | URL Pattern |
|--------|-------------|
| Dhiraagu | `https://afas.dhiraagu.com.mv/orders/hdc/{ticket_id}?activeRelationManager=notes` |
| Ooredoo | `https://www.ooredoo.mv/webapps/FMS/public/tickets/ticket_info/{ticket_id}` |
| ROL | `https://support.rol.net.mv/staff/index.php?/Tickets/Ticket/View/{ticket_id}/inbox/55/-1/-1` |
| Medianet | Captured during extraction (UUID-based URLs stored in `portal_url`) |
| Znuny | Captured during sync (dynamic ticket IDs stored in `znuny_url`) |

## Static File Versioning

- `APP_VERSION` is defined in `config.py` (currently `"1.6.2"`)
- Templates use `?v={{ app_version }}` query strings on static file URLs
- Global `fetch()` override in `common.js` adds `cache: 'no-store'` to all local API calls
- When you update static files, increment `APP_VERSION` to bust browser cache

## Troubleshooting

### Server Won't Start / Port Already in Use
```bash
netstat -ano | grep 8000
taskkill //F //PID <pid>
# Or: taskkill //F //IM python.exe
```

### Code Changes Not Taking Effect
1. Kill existing server process
2. Restart: `python app.py`
3. If needed, clear `__pycache__` directories

### Database Errors
- Check `system_logs` table in Admin → Status → System Logs
- Logs auto-cleaned after 2 days (daily job at midnight)
- If database is corrupted, delete `tickets.db` and restart (fresh DB created)

### Extraction Failures
- Check Admin → Status → Extraction Logs for per-portal status
- After 3 consecutive failures, browser session is auto-cleared for fresh start
- Check Login Stats for portal authentication issues
- Medianet SPA timeouts: ensure `wait_until="commit"` is used (not "load")

### Playwright / Browser Issues
- **"Playwright Sync API inside the asyncio loop"**: Should be handled by monkeypatch in `utils/browser.py`. If seen, ensure `import utils.browser` runs before any `sync_playwright()` call.
- **All portals stale for hours**: Check system_logs for "worker thread DIED" messages. The watchdog auto-restarts dead workers every 5 min.
- **pw.stop() hanging**: Never call `pw.stop()` — kill processes by name via `_nuke_all_browsers()` in scheduler_service.py.
- **Browser memory leaks**: The 1-hour scheduled restart (`_scheduled_browser_restart()`) kills all browsers and clears all caches proactively. Memory is also monitored per-portal after each extraction cycle.
- **Cross-thread Playwright crashes**: Never call Playwright methods from a different thread than the one that created the instance. `_nuke_all_browsers()` kills processes by name (safe cross-thread) and signals workers to reset their own instances.
