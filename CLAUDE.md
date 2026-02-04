# Ticket Extractor - Claude Documentation

## Overview

Ticket Extractor is a web scraping application that extracts tickets from multiple ISP portal systems (Dhiraagu, Ooredoo, ROL, Medianet) and syncs them with a Znuny ticketing system. It provides a dashboard for monitoring tickets, staff performance, and extraction status.

## Architecture

```
Extractor/
├── main.py              # CLI entry point, runs extraction + dashboard
├── dashboard.py         # FastAPI web server (port 8000)
├── database.py          # SQLite database operations
├── config.py            # Configuration from .env
├── znuny_client.py      # Selenium-based Znuny integration
├── extractors/          # Portal-specific scrapers
│   ├── base.py          # BaseExtractor abstract class
│   ├── dhiraagu.py      # Dhiraagu portal extractor
│   ├── ooredoo.py       # Ooredoo portal extractor
│   ├── rol.py           # ROL portal extractor
│   └── medianet.py      # Medianet portal extractor
├── models/
│   └── ticket.py        # Ticket dataclass
├── utils/
│   ├── browser.py       # Selenium browser manager
│   └── logger.py        # Logging utilities
├── templates/           # Jinja2 HTML templates (Bootstrap 5)
│   ├── base.html        # Base template with navbar, modal, CSS
│   ├── dashboard.html   # Main dashboard (extends base.html)
│   ├── tickets.html     # All tickets view (extends base.html)
│   ├── staff_stats.html # Staff performance stats (extends base.html)
│   ├── staff_detail.html# Individual staff performance detail (extends base.html)
│   └── admin.html       # Admin panel (extends base.html)
├── static/
│   ├── js/common.js     # Shared JavaScript functions
│   └── favicon.svg      # Application favicon
├── tickets.db           # SQLite database
└── .env                 # Environment variables (credentials)
```

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
```

### 3. Extractors (extractors/)

All extractors inherit from `BaseExtractor` and implement:
- `login()` - Portal authentication
- `extract_tickets()` - Scrape tickets from portal
- `logout()` - Cleanup
- `is_logged_in()` - Session check

**Session persistence:** Browser sessions are cached per-portal to avoid repeated logins.

**Completion tracking:** When a ticket disappears from the portal, it's automatically marked as complete.

### 4. Dashboard API (dashboard.py)

**Pages:**
- `/` - Main dashboard with stats
- `/tickets` - All tickets with filtering (includes staff filter)
- `/staff` - Staff performance statistics with % On Time metrics
- `/staff/{name}` - Individual staff performance detail page
- `/admin` - Admin panel with extraction logs

**Key API Endpoints:**
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/stats` | GET | Dashboard statistics |
| `/api/tickets` | GET | List tickets (with filters including staff) |
| `/api/tickets/{id}` | GET | Single ticket details |
| `/api/tickets/{id}/check-znuny` | POST | Check if ticket exists in Znuny |
| `/api/tickets/{id}/sync-znuny` | POST | Fetch Znuny details for ticket |
| `/api/tickets/{id}/znuny-articles` | GET | Get Znuny articles |
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

### 5. Znuny Integration (znuny_client.py)

Uses Selenium to interact with Znuny web interface:
- Searches tickets by title containing portal ticket ID
- Fetches ticket details: creator, creation time, articles
- Caches open tickets for efficiency

**Key Classes:**
- `ZnunyClient` - Main client for Znuny operations
- `ZnunyArticle` - Article/note data structure
- `ZnunyTicketDetails` - Full ticket details

### 6. Configuration (.env)

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
**Time to Create** = `znuny_created_at - created_at`

This measures how long it took staff to create the ticket in Znuny after it appeared in the extractor. This is the key accountability metric.

**Note:** The calculation uses `created_at` (entered to extractor) NOT `portal_created_at` (created on ISP portal) because staff can only act on tickets after they appear in the extractor.

## Running the Application

```bash
# Start with dashboard and scheduler
python main.py

# Dashboard only
python dashboard.py
```

The app runs on http://localhost:8000 by default.

## Scheduler

The background scheduler (in dashboard.py):
1. Runs every 5 minutes (configurable)
2. Extracts tickets from all configured portals
3. Syncs Znuny status for unchecked tickets
4. Logs all extraction results

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
4. Register in `dashboard.py` `get_extractor_class()`
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
