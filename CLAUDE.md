# Ticket Extractor - Claude Documentation

## Overview

Ticket Extractor is a web scraping application that extracts tickets from multiple ISP portal systems (Dhiraagu, Ooredoo, ROL, Medianet) and syncs them with a Znuny ticketing system. It provides a dashboard for monitoring tickets, staff performance, and extraction status.

## Architecture

The application follows MVC (Model-View-Controller) pattern with a service layer:

```
Extractor/
├── main.py              # CLI entry point, runs extraction + dashboard
├── app.py               # MVC FastAPI app (alternative entry point)
├── dashboard.py         # FastAPI web server (port 8000) - main controller
├── database.py          # SQLite database operations (Repository)
├── config.py            # Configuration from .env + APP_VERSION
├── znuny_client.py      # Selenium-based Znuny integration
│
├── models/              # Data Models
│   └── ticket.py        # Ticket dataclass
│
├── services/            # Service Layer - Business Logic
│   ├── __init__.py      # Service exports
│   ├── extraction_service.py  # Portal extraction logic
│   ├── znuny_service.py       # Znuny sync logic
│   ├── stats_service.py       # Statistics/analytics
│   ├── config_service.py      # Configuration management (.env)
│   └── scheduler_service.py   # Background job scheduling
│
├── controllers/         # Controller Layer - HTTP Handlers
│   ├── __init__.py      # Router exports
│   ├── pages.py         # HTML page routes
│   ├── api.py           # JSON API routes
│   └── admin.py         # Admin API routes
│
├── extractors/          # Portal-specific scrapers
│   ├── base.py          # BaseExtractor abstract class
│   ├── dhiraagu.py      # Dhiraagu portal extractor
│   ├── ooredoo.py       # Ooredoo portal extractor
│   ├── rol.py           # ROL portal extractor
│   └── medianet.py      # Medianet portal extractor
│
├── templates/           # View Layer - Jinja2 HTML templates
│   ├── base.html        # Base template with navbar, modal, CSS
│   ├── dashboard.html   # Main dashboard (extends base.html)
│   ├── tickets.html     # All tickets view (extends base.html)
│   ├── staff_stats.html # Staff performance stats (extends base.html)
│   ├── staff_detail.html# Individual staff performance detail
│   ├── reports.html     # Reports page with date-filtered statistics
│   └── admin.html       # Admin panel with Status, Staff & Config tabs
│
├── static/              # Static assets
│   ├── js/common.js     # Shared JavaScript functions
│   └── favicon.svg      # Application favicon
│
├── utils/               # Utilities
│   ├── browser.py       # Selenium browser manager
│   └── logger.py        # Logging utilities
│
├── tickets.db           # SQLite database
├── .env                 # Environment variables (credentials)
├── Dockerfile           # Docker container configuration
└── docker-compose.yml   # Docker Compose orchestration
```

### Layer Responsibilities

| Layer | Directory | Responsibility |
|-------|-----------|----------------|
| **Model** | `models/` | Data structures, validation |
| **View** | `templates/` | HTML templates, UI rendering |
| **Controller** | `controllers/`, `dashboard.py` | HTTP request handling, routing |
| **Service** | `services/` | Business logic |
| **Repository** | `database.py` | Data persistence, queries |

### Entry Points
- `app.py` - Main entry point (recommended, MVC architecture)
- `dashboard.py` - Legacy entry point (deprecated, redirects to app.py)
- `main.py` - CLI with options for extraction modes

## Template Architecture

All templates use Jinja2 inheritance from `base.html`:

### base.html (Parent Template)
Provides:
- Bootstrap 5 CSS/JS
- Responsive mobile CSS (breakpoints at 768px, 576px)
- Collapsible navbar with hamburger menu
- Loading overlay
- Ticket detail modal (shared across all pages)
- Common CSS variables (--primary-color, --secondary-color, etc.)

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
- `formatMaldivesDateTime(iso)` - Format datetime in Maldives timezone
- `formatTimeDiff(ms)` - Format milliseconds as "5m", "2h 30m", "1d 5h"
- `formatRelativeTime(iso)` - Format as "5 min ago", "2 hours ago"
- `getDateRange(filter)` - Get date range for filters (today, yesterday, week)
- `showTicketDetail(ticketId, callbacks)` - Show ticket detail modal
- `renderTicketRow(ticket, onClick)` - Render ticket table row
- `renderPagination(total, page, pageSize, callback)` - Render pagination
- `showLoading(show)` - Show/hide loading overlay
- `escapeHtml(text)` - Escape HTML entities

## Key Components

### 1. Database Schema (database.py)

**tickets table:**
| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER | Primary key |
| portal | TEXT | Source portal (dhiraagu, ooredoo, rol, medianet) |
| ticket_id | TEXT | Portal's ticket ID |
| address | TEXT | Customer address |
| account | TEXT | Account ID (used for ROL display ID) |
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
| created_at | DATETIME | When ticket was first extracted (entered to extractor) |
| updated_at | DATETIME | Last update time |
| completed_at | DATETIME | When ticket was marked complete |

**znuny_articles table:** Stores article/note history from Znuny tickets.
| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER | Primary key |
| ticket_id | INTEGER | FK to tickets.id |
| znuny_ticket_id | TEXT | Znuny ticket number |
| article_number | INTEGER | Article sequence number |
| sender | TEXT | Article sender |
| via | TEXT | Communication channel |
| subject | TEXT | Article subject |
| created_at | DATETIME | Article creation time |
| created_at_str | TEXT | Original time string |
| created_by | TEXT | Staff who created article |
| body | TEXT | Article content |

**extraction_logs table:** Logs each extraction run with counts.

**login_stats table:** Tracks portal login events for monitoring.

### 2. Ticket Model (models/ticket.py)

```python
@dataclass
class Ticket:
    portal: str
    ticket_id: str
    address: Optional[str]
    account: Optional[str]           # ROL display ID (e.g., ROL250141)
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
```

### 3. Extractors (extractors/)

All extractors inherit from `BaseExtractor` and implement:
- `login()` - Portal authentication
- `extract_tickets()` - Scrape tickets from portal
- `logout()` - Cleanup
- `is_logged_in()` - Session check

**Session persistence:** Browser sessions are cached per-portal to avoid repeated logins.

**Completion tracking:** When a ticket disappears from the portal, it's automatically marked as complete.

### 4. Services Layer (services/)

Business logic separated from HTTP handlers:

| Service | File | Responsibility |
|---------|------|----------------|
| `ExtractionService` | extraction_service.py | Run portal extractions |
| `ZnunyService` | znuny_service.py | Znuny sync, ticket checking, article fetch |
| `StatsService` | stats_service.py | Dashboard stats, staff metrics, reports |
| `ConfigService` | config_service.py | Environment config management |
| `SchedulerService` | scheduler_service.py | Background job scheduling, extraction timing |

### 5. Controllers Layer (controllers/)

HTTP route handlers (MVC app only):

| Controller | File | Routes |
|------------|------|--------|
| `pages_router` | pages.py | HTML pages (/, /tickets, /staff, /admin) |
| `api_router` | api.py | JSON API endpoints (/api/*) |
| `admin_router` | admin.py | Admin API (/api/admin/*) |

### 6. API Endpoints

**Pages:**
- `/` - Main dashboard with stats
- `/tickets` - All tickets with filtering (includes staff filter)
- `/staff` - Staff performance statistics with % On Time metrics
- `/staff/{name}` - Individual staff performance detail page
- `/field-visits` - Site visits/field visits management
- `/znuny-tickets` - Znuny-only tickets (orphan tickets not linked to ISP portals)
- `/reports` - Reports with date-filtered statistics (Today, Yesterday, 7 Days, 30 Days)
- `/admin` - Admin panel with Status, Staff Management & Config tabs

**Key API Endpoints:**
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/stats` | GET | Dashboard statistics |
| `/api/tickets` | GET | List tickets (with filters: staff, date_from, date_to) |
| `/api/tickets/{id}` | GET | Single ticket details |
| `/api/tickets/{id}/check-znuny` | POST | Check if ticket exists in Znuny |
| `/api/tickets/{id}/sync-znuny` | POST | Fetch Znuny details for ticket |
| `/api/tickets/{id}/znuny-articles` | GET | Get Znuny articles |
| `/api/articles` | GET | List articles (supports date_from, date_to, staff) |
| `/api/staff-stats` | GET | Basic staff stats (supports date_from, date_to) |
| `/api/staff-stats-detailed` | GET | Detailed staff stats with on-time metrics |
| `/api/staff/{name}/tickets` | GET | Get tickets created by specific staff |
| `/api/staff/{name}/performance` | GET | Get daily performance trend for staff |
| `/api/staff-names` | GET | Get list of all staff names |
| `/api/reports/staff-csv` | GET | Export staff stats as CSV |
| `/api/tickets-csv` | GET | Export tickets as CSV (supports staff filter) |
| `/api/sync-znuny-details` | POST | Bulk sync all Znuny details |
| `/api/admin/trigger-extraction` | POST | Manually trigger extraction |
| `/api/admin/scheduler-status` | GET | Get scheduler status |
| `/api/admin/login-summary` | GET | Login statistics summary |

### 7. Znuny Integration (znuny_client.py)

Uses Selenium to interact with Znuny web interface:
- Searches tickets by title containing portal ticket ID
- Fetches ticket details: creator, creation time, articles
- Caches open tickets for efficiency

**Key Classes:**
- `ZnunyClient` - Main client for Znuny operations
- `ZnunyArticle` - Article/note data structure
- `ZnunyTicketDetails` - Full ticket details

### 8. Configuration (.env)

```env
# Portal credentials
DHIRAAGU_URL=https://afas.dhiraagu.com.mv/login
DHIRAAGU_USERNAME=xxx
DHIRAAGU_PASSWORD=xxx

OOREDOO_URL=https://www.ooredoo.mv/webapps/FMS/public/tickets
OOREDOO_USERNAME=xxx
OOREDOO_PASSWORD=xxx

ROL_URL=https://support.rol.net.mv/staff/index.php
ROL_USERNAME=xxx
ROL_PASSWORD=xxx

MEDIANET_URL=https://app.crm.com/crm/service-requests-board
MEDIANET_USERNAME=xxx
MEDIANET_PASSWORD=xxx

# Znuny API
ZNUNY_URL=https://10.241.1.110
ZNUNY_USERNAME=xxx
ZNUNY_PASSWORD=xxx

# Settings
EXTRACTION_INTERVAL_MINUTES=5
ZNUNY_SYNC_INTERVAL_MINUTES=1
DASHBOARD_HOST=0.0.0.0
DASHBOARD_PORT=8000
```

## Important Timestamps

The system tracks multiple timestamps for each ticket:

1. **portal_created_at** - When the ticket was created on the ISP portal
2. **created_at** - When the ticket was first extracted into this app (entered to extractor)
3. **znuny_created_at** - When the ticket was created in Znuny
4. **updated_at** - When the ticket record was last updated
5. **completed_at** - When the ticket was marked complete (disappeared from portal)

### Time to Create Calculation
**Time to Create** = `created_at - znuny_created_at`

This measures the time difference between when a ticket entered the extractor and when it was created in Znuny. A positive value indicates staff created the Znuny ticket after the extractor picked it up.

**Note:** The calculation uses `created_at` (entered to extractor) NOT `portal_created_at` (created on ISP portal) because staff can only act on tickets after they appear in the extractor.

## Running the Application

```bash
# Recommended: Start MVC app with scheduler
python app.py

# Alternative: Using main.py
python main.py

# Legacy (deprecated)
python dashboard.py
```

The app runs on http://localhost:8000 by default.

### Development: Restarting After Code Changes

**IMPORTANT:** After modifying Python files, always restart the server to load new code:

```bash
# Windows - Kill existing Python processes and restart
taskkill //F //IM python.exe
python app.py

# Or find and kill process on port 8000
netstat -ano | grep 8000  # Find PID
taskkill //F //PID <pid>
```

**Common issues:**
- Old server still running on port 8000 → new code not loaded
- Port already in use error → kill the existing process first
- Changes not reflected → clear `__pycache__` directories

```bash
# Clear Python cache (if needed)
rmdir /s /q __pycache__
rmdir /s /q models\__pycache__
rmdir /s /q extractors\__pycache__
```

## Docker Deployment

### Quick Start

```bash
# Copy example env file and configure
cp .env.example .env
# Edit .env with your credentials

# Build and run
docker-compose up -d

# View logs
docker-compose logs -f

# Stop
docker-compose down
```

### Configuration

Create a `.env` file from the example template:
```bash
cp .env.example .env
```

Edit `.env` with your portal credentials. Changes to `.env` persist on the host and are loaded on container restart.

### Persistent Volumes

| Volume | Purpose |
|--------|---------|
| `extractor_data:/app/data` | Named volume for persistent data (database, logs) |
| `./.env:/app/.env:ro` | Environment config (read-only in container) |

The database is stored at `/app/data/tickets.db` inside the container, persisted via the named volume `ticket-extractor-data`.

**Backup the database:**
```bash
docker cp ticket-extractor:/app/data/tickets.db ./backup_tickets.db
```

**Restore database:**
```bash
docker cp ./backup_tickets.db ticket-extractor:/app/data/tickets.db
docker-compose restart
```

### Docker Compose Features

- Chrome with Selenium for web scraping
- Named volume for persistent database (`ticket-extractor-data`)
- Environment loaded from `.env` file (persists across restarts)
- Automatic restart on failure
- 2GB shared memory for Chrome stability
- Health check with auto-restart on failure
- Log rotation (10MB max, 3 files)

### Updating Configuration

To update credentials or settings:
1. Edit `.env` on the host machine
2. Restart the container: `docker-compose restart`

## Scheduler

The background scheduler (managed by `SchedulerService` in `services/scheduler_service.py`) runs two independent jobs:

| Job | Default Interval | Config Variable | Description |
|-----|-----------------|-----------------|-------------|
| **Portal Extraction** | 5 min | `EXTRACTION_INTERVAL_MINUTES` | Extracts tickets from all configured ISP portals |
| **Znuny Sync** | 1 min | `ZNUNY_SYNC_INTERVAL_MINUTES` | Checks ISP tickets in Znuny, syncs details & site visits |

- Jobs run on independent schedules (Znuny sync runs more frequently for faster ticket linking)
- Znuny sync uses a threading lock to prevent overlap (if sync takes >1 min, next cycle is skipped)
- Both jobs run immediately on startup, then repeat at their configured intervals
- The scheduler is started automatically via the FastAPI lifespan in `app.py`

## Portal-Specific Notes

### Dhiraagu
- URL: `https://afas.dhiraagu.com.mv/orders/hdc`
- Has "Third Party" button for login
- Pagination handled automatically

### Ooredoo
- URL: `https://www.ooredoo.mv/webapps/FMS/public/tickets`
- Uses standard login form
- Account ID stored in `account` field

### ROL
- URL: `https://support.rol.net.mv/staff/index.php`
- Display ID (ROL250141) stored in `account` field
- Internal ID stored in `ticket_id`
- **Important:** Uses `account` field for Znuny search

### Medianet
- URL: `https://app.crm.com/crm/service-requests-board`
- Two-step login (email first, then password)
- Board-based UI with multiple ticket types
- Columns: New, Survey, Installation, etc. (Closed is skipped)

## Common Tasks

### Adding a New Portal
1. Create new extractor in `extractors/` inheriting from `BaseExtractor`
2. Implement `login()`, `extract_tickets()`, `logout()`, `is_logged_in()`
3. Add configuration in `config.py`
4. Register in `services/extraction_service.py` `get_extractor_class()`
5. Add to `extractors/__init__.py`

### Modifying Ticket Fields
1. Update `models/ticket.py` dataclass
2. Add migration in `database.py` `_init_db()`
3. Update `_row_to_ticket()` in `database.py`
4. Update templates if displaying new fields

### Adding New Dashboard Features
1. Add API endpoint in `dashboard.py`
2. Create/update template in `templates/`
3. Add any required database methods in `database.py`
4. If shared UI component, add to `static/js/common.js`

## Timezone

All times are in **Maldives Time (UTC+5)** - see `MVT` constant in `database.py`.

## Browser Management

- Uses Selenium with Chrome WebDriver
- `webdriver_manager` auto-downloads correct driver version
- Headless mode used for scheduled extractions
- Sessions persisted per-portal for efficiency

## Mobile Support

The application is fully responsive:
- Collapsible navbar with hamburger menu on mobile
- Touch-friendly button sizes (44px minimum)
- Responsive tables with horizontal scroll
- Hidden non-essential columns on small screens
- Compact filter buttons with shortened labels

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
- 🟢 Green row: ≥80% on time
- 🟡 Yellow row: 50-79% on time
- 🔴 Red row: <50% on time

**Features:**
- Click staff name to view individual performance detail
- Export CSV button for reports
- Date range filter (Today, Week, Month, All)

### Individual Staff Detail Page (`/staff/{name}`)
Detailed performance view for a single staff member:
- **Performance Summary Cards**: Total tickets, On Time %, Avg Time, Articles
- **Response Time Breakdown**: Visual bar chart with percentages
- **Daily Performance Trend**: Last 14 days showing tickets, on-time count, percentage, avg time
- **Tickets List**: All tickets created by staff with pagination
- **Export CSV**: Export tickets for this staff member

### Tickets Page Staff Filter (`/tickets`)
The tickets page includes a "Created By" filter to view tickets by specific staff member.

### Delayed Tickets Analysis (Dashboard)
- Filters by date range (Today, Yesterday, 7 Days)
- Filters by delay threshold (>5m, >10m, >30m, >1h)
- Shows: Total delayed, Avg delay, Max delay, Staff involved
- Clickable rows to see ticket details

### CSV Export
Two CSV export endpoints are available:
1. **Staff Report CSV** (`/api/reports/staff-csv`): Summary stats for all staff
2. **Tickets CSV** (`/api/tickets-csv`): Export tickets with optional staff filter

### Key Performance Indicators
| Metric | Good | Warning | Bad |
|--------|------|---------|-----|
| Time to Create | <5 min | 5-10 min | >10 min |
| % On Time | ≥80% | 50-79% | <50% |
| Articles per Ticket | >2 | 1-2 | 0-1 |

### Definition: "On Time"
A ticket is considered "On Time" if it was created in Znuny within **5 minutes** of appearing in the extractor.

**Calculation:** `znuny_created_at - created_at <= 5 minutes`

### Negative Time / Historical Tickets
Some tickets have **negative time differences** - these are historical tickets where the Znuny ticket existed before the extractor first saw it. This happens with tickets created before the extractor was running.

**Exclude Negative Time Toggle:**
- Located in the Staff Stats page filter section
- **ON (default):** Excludes tickets with negative time from all calculations
- **OFF:** Includes all tickets, showing negative avg times for historical data

**API Parameter:** `/api/staff-stats-detailed?exclude_negative=true|false`

## Reports Page (`/reports`)

Standalone reports page accessible from the main navigation bar with date-filtered statistics.

### Time Period Filters
- **Today** - Current day's statistics
- **Yesterday** - Previous day's statistics
- **7 Days** - Last week's statistics
- **30 Days** - Last month's statistics

### Report Sections
1. **Summary Stats Cards**: ISP Tickets, Znuny Only, Articles, Site Visits, Avg On Time %, Active Staff
2. **Tickets by Portal**: Breakdown by ISP with total, in Znuny, and pending counts
3. **Performance Breakdown**: Within 5min, 5-10min, Over 10min counts with percentages
4. **Staff Performance Table**: Ranked staff list with metrics (clickable rows to view staff detail)
5. **Tickets**: List of ISP tickets extracted in the period (clickable for detail)
6. **Articles**: List of Znuny articles created in the period with staff and subject
7. **Site Visits**: List of site visits in the period with staff, time, provider, status

### Export
- **Export CSV** button downloads staff statistics for the selected period

## Admin Panel Features

### Staff Management Tab (Admin → Staff)
Merge duplicate staff names when the same person uses different names:

1. **Staff List Table**: Shows all staff with record counts per source (ISP tickets, Znuny tickets, Articles, Site Visits)
2. **Merge Staff Names**:
   - Select source name (to be replaced)
   - Select target name (to keep)
   - Preview shows affected record counts per table
   - Merge updates all records across all tables
3. **Recent Merges Log**: Shows history of merge operations

### API Endpoints
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/admin/staff-list` | GET | Get all staff names with counts |
| `/api/admin/staff-merge-preview` | GET | Preview merge operation |
| `/api/admin/staff-merge` | POST | Execute staff merge |
| `/api/admin/report-portal-stats` | GET | Get portal stats for reports |

## Portal & Znuny URLs

Tickets include clickable links to their source portal and Znuny:

| Portal | URL Pattern |
|--------|-------------|
| Dhiraagu | `https://afas.dhiraagu.com.mv/orders/hdc/{ticket_id}?activeRelationManager=notes` |
| Ooredoo | `https://www.ooredoo.mv/webapps/FMS/public/tickets/ticket_info/{ticket_id}` |
| ROL | `https://support.rol.net.mv/staff/index.php?/Tickets/Ticket/View/{ticket_id}/inbox/55/-1/-1` |
| Medianet | Captured during extraction (UUID-based URLs) |
| Znuny | Captured during sync (dynamic ticket IDs) |

**Implementation:**
- Dhiraagu, Ooredoo, ROL: URLs generated from patterns using `ticket_id`
- Medianet: URLs captured from browser during extraction (stored in `portal_url`)
- Znuny: URLs captured during sync process (stored in `znuny_url`)

## Static File Versioning

Static files (CSS, JS, favicon) use query string versioning for cache busting.

**How it works:**
- `APP_VERSION` is defined in `config.py` (e.g., `"1.0.0"`)
- Templates use `?v={{ app_version }}` query strings on static file URLs
- When you update static files, increment `APP_VERSION` to bust browser cache

**Example:**
```html
<script src="/static/js/common.js?v=1.0.0"></script>
```

**When to update:**
- After modifying `static/js/common.js`
- After modifying `static/favicon.svg`
- After any CSS changes in `base.html`

## Troubleshooting

### Server Won't Start / Port Already in Use
```bash
# Find process using port 8000
netstat -ano | grep 8000

# Kill by PID (Windows)
taskkill //F //PID <pid>

# Or kill all Python processes
taskkill //F //IM python.exe
```

### Code Changes Not Taking Effect
1. Ensure old server process is killed (see above)
2. Clear Python bytecode cache:
   ```bash
   rmdir /s /q __pycache__
   rmdir /s /q models\__pycache__
   ```
3. Restart the server

### Database Errors
- Check `system_logs` table in Admin panel for logged errors
- Database errors are logged to console and `system_logs` table
- If database is corrupted, delete `tickets.db` and restart (fresh DB created)

### Browser Cache Issues
- Increment `APP_VERSION` in `config.py` after static file changes
- Hard refresh browser: Ctrl+Shift+R

## Site Visits / Field Visits Feature

The application tracks site visits extracted from Znuny "OAN Site Visit Arranged" articles.

### Site Visits Table (`site_visits`)
| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER | Primary key |
| znuny_ticket_id | TEXT | Parent Znuny ticket number |
| article_id | INTEGER | Article number containing site visit info |
| portal_ticket_id | TEXT | Linked ISP portal ticket ID |
| visit_date | TEXT | Scheduled visit date (YYYY-MM-DD) |
| scheduled_time | TEXT | Scheduled time slot |
| assigned_to | TEXT | Staff assigned to visit |
| service_provider | TEXT | ISP provider name |
| site_type | TEXT | Type of site |
| status | TEXT | pending / completed |
| ticket_completed_at | DATETIME | When ticket was closed/completed (from Znuny) |
| time_taken_minutes | REAL | Duration in minutes (completed_at - scheduled_time) |
| znuny_url | TEXT | Direct URL to Znuny ticket |
| created_at | DATETIME | When first extracted (MVT timezone) |
| updated_at | DATETIME | Last update time |

### Site Visits Page (`/field-visits`)
- **Filters**: Date range (Today, Yesterday, Week, Month, All), Staff, Status
- **Stats Cards**: Total visits, Completed, Pending, Avg Duration
- **Staff Stats**: Per-staff breakdown with visit counts and durations
- **Table Columns**: Date, Time, Assigned To, Provider, Site Type, Status, Extracted, Duration, Znuny link, Actions
- **Edit Modal**: Update assigned staff, status, time taken

### Site Visit Extraction
Site visits are extracted from Znuny articles with subject containing "OAN Site Visit Arranged" or "Preventative Maintenance - Site Visit". The article body is parsed for:
- Visit date and time
- Assigned staff name
- Service provider
- Site type

### Site Visit Completion & Duration

A site visit is marked as **completed** when any of these occur:
1. **Znuny ticket closed** - ticket disappears from open tickets list
2. **Follow-up article added** - a new article is added after the site visit article
3. **ISP ticket completed** - the linked ISP portal ticket disappears

**Duration Calculation:**
```
Duration = Completion Time - Scheduled Visit Time
```

Where:
- **Completion Time** = Last article's `created_at` time from Znuny (or follow-up article time)
- **Scheduled Visit Time** = `visit_date` + `scheduled_time` (e.g., "2026-02-07 14:00:00")

**Scheduled Time Formats Supported:**
| Format | Example | Parsed As |
|--------|---------|-----------|
| HH:MM | 14:00 | 14:00:00 |
| HHMM | 1400 | 14:00:00 |
| HH:MM:SS | 14:00:00 | 14:00:00 |
| Invalid | "now" | NULL (no duration) |

**Note:** Duration is calculated from the scheduled visit time, not when the site visit article was created. This measures how long the actual field work took from the scheduled appointment time.

### API Endpoints
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/field-visits` | GET | List site visits with filters |
| `/api/field-visits/{id}` | PUT | Update site visit |
| `/api/field-visits/assigned-staff` | GET | Get list of assigned staff |
| `/api/field-visits/staff-stats` | GET | Get per-staff statistics |

## Dashboard Portal Cards

Portal stat cards on the dashboard are clickable to open their respective ISP portals:
- Click Dhiraagu card → opens Dhiraagu AFAS portal
- Click Ooredoo card → opens Ooredoo FMS portal
- Click ROL card → opens ROL support portal
- Click Medianet card → opens Medianet CRM portal
- Click Znuny Only card → opens `/znuny-tickets` page

Portal URLs are loaded from `config.py` and passed to the template context.

## Znuny Sync Status Card

The "Not in Znuny" card shows real-time sync status with dynamic coloring:

| State | Background | Badge | Description |
|-------|------------|-------|-------------|
| All Synced | Green (#27ae60) | OK | All tickets are synced with Znuny |
| Pending | Red (#dc3545) | Pending | N tickets need Znuny sync |

The card displays:
- **Count**: Number of tickets not yet in Znuny
- **Status Badge**: "OK" (green) or "Pending" (red)
- **Last Sync Time**: Relative time since last sync (e.g., "5 min ago")

Data from `/api/znuny-sync-status`:
- `not_in_znuny`: Count of tickets needing sync
- `last_sync_time`: ISO timestamp of last sync

## Dashboard Today's Activity

The dashboard shows today's activity via "Today:" badges on each portal card:

| Stat | Description |
|------|-------------|
| **Today: N** | Tickets first seen by extractor today (per portal) |
| **Znuny Only Today** | Znuny-only tickets created today |

### Today's Znuny Card
A dedicated card shows today's Znuny activity:
- **Tickets entered today**: Count of tickets synced to Znuny today
- **Articles badge**: Number of articles created today

Links to `/staff` page for detailed staff performance.

These stats are returned by the `/api/stats` endpoint:
- `today_extracted`: Dict of tickets per portal (e.g., `{"dhiraagu": 5, "ooredoo": 3}`)
- `today_znuny_by_portal`: Dict of Znuny entries per portal
- `today_znuny_entries`: Count of tickets entered to Znuny today
- `today_articles_created`: Count of articles created today

## Dashboard Action Buttons

Action buttons are located in the page content (not navbar) for better UX:

| Button | Location | Action |
|--------|----------|--------|
| **Check Znuny** | Dashboard | Check all tickets against Znuny system |
| **Sync Details** | Dashboard | Bulk sync Znuny details for all tickets |
| **Run Now** | Admin panel | Manually trigger extraction |
| **Export CSV** | Staff page | Export staff statistics |

This design keeps the navbar clean and places actions contextually on relevant pages.

## Znuny Sync Optimization

The Znuny integration uses several optimization strategies:

### TTL-Based Caching
- **Cache TTL**: 5 minutes (configurable via `CACHE_TTL_SECONDS`)
- Open tickets list is cached to avoid repeated dashboard fetches
- Ticket details are cached per-ticket with TTL validation
- Cache is automatically invalidated after TTL expires

### Selective Article Processing
- Only clicks articles that need body content:
  - Site visit articles (subject contains "site visit")
  - First Phone article (for address extraction)
- Other articles use basic info from table (no clicking needed)
- Reduces sync time by 50-65%

### Smart Skip Logic
- Skips tickets that are already fully synced
- Prioritizes tickets with "site visit" in title
- Skips tickets without pending site visits
- Skips tickets already processed in Step 0 (ISP detail sync) to avoid duplicate Selenium calls

### Duplicate Call Prevention
- Step 0 processed ticket IDs are tracked in `step0_processed_ids` set
- Main loop skips any ticket already fetched in Step 0
- Pending visit IDs derived via set arithmetic instead of re-querying DB
- Single `get_tickets_needing_znuny_details()` method replaces duplicate DB queries

### Key Methods
- `_is_cache_valid()` - Check if cache is within TTL
- `clear_cache()` - Force clear all caches
- `get_ticket_details(skip_body_fetch=True)` - Fast mode without article bodies
