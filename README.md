# Ticket Extractor

A web scraping application that extracts tickets from multiple ISP portal systems and syncs them with Znuny ticketing system. Provides a dashboard for monitoring tickets, staff performance, and extraction status.

## Features

- **Multi-Portal Extraction**: Scrapes tickets from Dhiraagu, Ooredoo, ROL, and Medianet portals
- **Znuny Integration**: Syncs tickets with Znuny ticketing system via Selenium
- **Real-time Dashboard**: Monitor active tickets, extraction status, and sync state
- **Staff Accountability**: Track staff performance with on-time metrics and response times
- **Field Visits**: Track and manage site visits extracted from Znuny
- **Reports**: Generate date-filtered reports with CSV export
- **Mobile Responsive**: Full mobile support with touch-friendly UI

## Quick Start

### Prerequisites

- Python 3.10+
- Chrome browser (for Selenium)
- Portal credentials (Dhiraagu, Ooredoo, ROL, Medianet)
- Znuny credentials

### Installation

```bash
# Clone the repository
git clone https://github.com/rishal1123/ticket-extractor.git
cd ticket-extractor

# Install dependencies
pip install -r requirements.txt

# Copy and configure environment
cp .env.example .env
# Edit .env with your credentials
```

### Running

```bash
# Start the application
python app.py

# Access dashboard at http://localhost:8000
```

## Project Structure

```
ticket-extractor/
├── app.py                 # Main entry point (FastAPI)
├── config.py              # Configuration from .env
├── database.py            # SQLite database operations
├── znuny_client.py        # Znuny client (Generic Interface REST API via httpx)
│
├── controllers/           # HTTP route handlers
│   ├── pages.py           # HTML page routes
│   ├── api.py             # JSON API endpoints
│   └── admin.py           # Admin API routes
│
├── extractors/            # Portal-specific scrapers
│   ├── base.py            # Base extractor class
│   ├── dhiraagu.py        # Dhiraagu portal
│   ├── ooredoo.py         # Ooredoo portal
│   ├── rol.py             # ROL portal
│   └── medianet.py        # Medianet portal
│
├── models/                # Data models
│   └── ticket.py          # Ticket dataclass
│
├── services/              # Business logic
│   ├── extraction_service.py
│   ├── znuny_service.py
│   ├── stats_service.py
│   ├── config_service.py
│   └── scheduler_service.py
│
├── templates/             # Jinja2 HTML templates
│   ├── base.html          # Base template
│   ├── dashboard.html     # Main dashboard
│   ├── tickets.html       # Tickets list
│   ├── staff_stats.html   # Staff performance
│   ├── field_visits.html  # Site visits
│   ├── reports.html       # Reports page
│   └── admin.html         # Admin panel
│
├── static/                # Static assets
│   └── js/common.js       # Shared JavaScript
│
├── tests/                 # Test files
│   ├── fixtures/          # Test data
│   └── test_*.py          # Test scripts
│
└── utils/                 # Utilities
    ├── browser.py         # Selenium browser manager
    └── logger.py          # Logging utilities
```

## Configuration

Create a `.env` file with the following variables:

```env
# Portal Credentials
DHIRAAGU_URL=https://afas.dhiraagu.com.mv/login
DHIRAAGU_USERNAME=your_username
DHIRAAGU_PASSWORD=your_password

OOREDOO_URL=https://www.ooredoo.mv/webapps/FMS/public/tickets
OOREDOO_USERNAME=your_username
OOREDOO_PASSWORD=your_password

ROL_URL=https://support.rol.net.mv/staff/index.php
ROL_USERNAME=your_username
ROL_PASSWORD=your_password

MEDIANET_URL=https://app.crm.com/crm/service-requests-board
MEDIANET_USERNAME=your_username
MEDIANET_PASSWORD=your_password

# Znuny
ZNUNY_URL=https://your-znuny-server
ZNUNY_USERNAME=your_username
ZNUNY_PASSWORD=your_password

# Settings
EXTRACTION_INTERVAL_MINUTES=5
DASHBOARD_HOST=0.0.0.0
DASHBOARD_PORT=8000
```

## Pages

| Page | URL | Description |
|------|-----|-------------|
| Dashboard | `/` | Main dashboard with stats and active tickets |
| Tickets | `/tickets` | All tickets with filtering |
| Staff | `/staff` | Staff performance statistics |
| Staff Detail | `/staff/{name}` | Individual staff performance |
| Field Visits | `/field-visits` | Site visits management |
| Znuny Tickets | `/znuny-tickets` | Znuny-only tickets |
| Reports | `/reports` | Date-filtered reports |
| Admin | `/admin` | Admin panel and configuration |

## API Endpoints

### Tickets
- `GET /api/tickets` - List tickets with filters
- `GET /api/tickets/{id}` - Get ticket details
- `POST /api/tickets/{id}/check-znuny` - Check Znuny status
- `POST /api/tickets/{id}/sync-znuny` - Sync Znuny details

### Stats
- `GET /api/stats` - Dashboard statistics
- `GET /api/staff-stats` - Staff statistics
- `GET /api/staff-stats-detailed` - Detailed staff metrics

### Field Visits
- `GET /api/field-visits` - List site visits
- `PUT /api/field-visits/{id}` - Update site visit

### Admin
- `POST /api/admin/trigger-extraction` - Manual extraction
- `GET /api/admin/scheduler-status` - Scheduler status
- `POST /api/admin/staff-merge` - Merge staff names

### Health Check
- `GET /api/health` - System health status (database, scheduler, storage)

## Backup & Restore

```bash
# Create backup
python scripts/backup.py

# Create backup and keep only last 7
python scripts/backup.py --keep 7

# List existing backups
python scripts/backup.py --list

# Restore from backup
python scripts/backup.py --restore backups/tickets_backup_20260207_120000.db
```

## Docker Deployment

```bash
# Build and run
docker-compose up -d

# View logs
docker-compose logs -f

# Stop
docker-compose down
```

### Docker Volumes

| Volume | Purpose |
|--------|---------|
| `extractor_data:/app/data` | Database and logs |
| `./.env:/app/.env:ro` | Configuration |

### Backup Database

```bash
docker cp ticket-extractor:/app/data/tickets.db ./backup.db
```

## Staff Accountability

### Performance Metrics

| Metric | Good | Warning | Bad |
|--------|------|---------|-----|
| Time to Create | <5 min | 5-10 min | >10 min |
| % On Time | ≥80% | 50-79% | <50% |

### Color Coding

- 🟢 **Green**: ≥80% on time
- 🟡 **Yellow**: 50-79% on time
- 🔴 **Red**: <50% on time

## Scheduler

The background scheduler runs every 5 minutes (configurable):

1. Extracts tickets from all configured portals
2. Syncs Znuny status for unchecked tickets
3. Logs extraction results

## Timezone

All times are in **Maldives Time (UTC+5)**.

## Development

### Restart After Changes

```bash
# Windows
taskkill //F //IM python.exe
python app.py
```

### Clear Cache

```bash
rmdir /s /q __pycache__
```

## License

Private - All rights reserved.

## Contributing

Contact the repository owner for contribution guidelines.
