# NocBot External API Guide

Authenticated, rate-limited JSON endpoints for external callers (scripts, other
systems). This is separate from the unauthenticated endpoints under `/api/*`
that back the web dashboard itself (`/api/data`, `/api/lots`, `/api/health`,
etc.) — those stay open because they're only ever called by pages the browser
already loaded from this server.

> **Keep this file in sync.** Whenever an authenticated endpoint is added,
> removed, or its request/response shape changes, update this guide in the
> same change.

## Authentication

Every authenticated endpoint requires an `X-API-Key` header:

```
X-API-Key: <your key>
```

Keys can come from either of two places, checked in order — a key from
either one works:

1. **Admin-managed keys (recommended)** — issue and revoke keys from
   **Admin > API Keys** (`/admin/api-keys`) in the web dashboard, admin role
   required. Each key has a name/label, is shown **once** in full at creation
   time, and only its SHA-256 hash is stored afterward (`Api_Keys` table via
   `Library/DBManager.py`, checked in `Library/APIAuth.py`). Revoking or
   deleting a key from that page takes effect immediately, on the next
   request — no restart needed. If a key is lost, revoke it and issue a new
   one; it can't be recovered from the DB.
2. **Static env-var keys (legacy/bootstrap)** — the `NOCBOT_API_KEYS`
   environment variable (comma-separated, no spaces needed around commas),
   e.g. in `env/active.env` or `env/test.env`:

   ```
   NOCBOT_API_KEYS=prod-key-abc123,partner-key-def456
   ```

   These aren't tracked in the DB (no name, no last-used, no per-key
   revocation from the UI) and changing this list requires a process restart
   (env vars are only re-read via `os.getenv` per request, but the process's
   actual environment doesn't change until it's reloaded). Prefer
   admin-managed keys for anything issued after this page existed; this path
   mainly exists for keys that need to work before the DB/admin UI is
   available.

- There's no scoping either way — any valid key has full access to every
  authenticated endpoint.
- Missing/invalid key → `401 {"error": "Missing or invalid API key"}`.
- Keys are compared with a constant-time comparison
  (`secrets.compare_digest` for env keys, hash equality for DB keys); treat
  them like passwords — don't log full request headers, don't commit real
  keys to the repo.

## Rate limiting

Each API key gets its own rolling 60-second window, enforced in-process
(`Library/RateLimiter.py`). Default: **60 requests/minute per key**,
configurable via:

```
API_RATE_LIMIT_PER_MINUTE=60
```

Responses include:

- `X-RateLimit-Limit` — the configured per-minute limit
- `X-RateLimit-Remaining` — requests left in the current window

When exceeded, the endpoint returns `429` with a `Retry-After` header (seconds)
and body:

```json
{"error": "Rate limit exceeded", "retry_after_seconds": 12}
```

Note: the limiter is in-memory and per-process. If NocBot is ever run behind
multiple worker processes, each worker enforces its own window (effective
limit becomes `limit × workers`) — fine for the current single-process
deployment, but worth revisiting if that changes.

## Endpoints

### `GET /api/ont/<ont_id>`

Look up an ONT by ID. Checks the local database first; if the ONT isn't
found there, falls back to a live SMX search and — if SMX has it — caches
the result back into the database so the next lookup for the same ID is
served from the DB.

**Headers**

| Header      | Required | Value             |
|-------------|----------|-------------------|
| `X-API-Key` | Yes      | Your API key      |

**Path parameters**

| Param     | Description                                                    |
|-----------|------------------------------------------------------------------|
| `ont_id`  | ONT identifier. Alphanumeric, `_` and `-` only.                 |

**Responses**

- `200` — found (DB or SMX):

  ```json
  {
    "source": "database",
    "ont": {
      "ont_id": "P2-Lot20028-G-02",
      "fsan": "HWTC12345678",
      "olt_id": "OLT-01",
      "linked_pon": "PON-1-2-3",
      "ne_level": -22.4,
      "fe_level": -18.1,
      "ont_state": "up",
      "pon_error_up": 0,
      "pon_error_down": 0,
      "uptime": "12 days, 04:33:10",
      "...": "remaining ONT table columns"
    }
  }
  ```

  `source` is `"database"` if the record was already cached locally, or
  `"smx"` if it required a live SMX search (slower — an SMX round trip vs.
  a local read).

- `400` — invalid `ont_id` format:
  `{"error": "Invalid ont_id format"}`

- `401` — missing/invalid API key (see Authentication above)

- `404` — not found in the database or in SMX:
  `{"error": "ONT '<id>' not found in database or SMX"}`

- `429` — rate limit exceeded (see Rate limiting above)

- `502` — SMX had a match but the follow-up signal-level lookup failed:
  `{"error": "ONT '<id>' matched in SMX but signal lookup failed"}`

**Examples**

curl:

```bash
curl -H "X-API-Key: prod-key-abc123" \
  http://localhost:5000/api/ont/P2-Lot20028-G-02
```

PowerShell:

```powershell
Invoke-RestMethod -Uri "http://localhost:5000/api/ont/P2-Lot20028-G-02" `
  -Headers @{ "X-API-Key" = "prod-key-abc123" }
```

Python:

```python
import requests

resp = requests.get(
    "http://localhost:5000/api/ont/P2-Lot20028-G-02",
    headers={"X-API-Key": "prod-key-abc123"},
)
resp.raise_for_status()
print(resp.json())
```

## Managing keys (Admin > API Keys)

`/admin/api-keys` (admin role required):

- **New API Key** button → name it → the raw key is displayed once in a
  dismissible banner with a copy button. It is never shown again; the page's
  table only ever displays a short prefix (e.g. `nbsDHhk4gT9k…`) for
  identification.
- Table shows: name, key prefix, status (Active/Revoked), created date,
  created by (the admin username that issued it), last used.
- **Revoke** flips a key inactive without deleting its row (history/audit
  trail preserved); revoked keys fail auth immediately. **Reactivate**
  un-revokes it (same key value still works). **Delete** removes the row
  permanently.
- Backing routes: `web.api_keys` (list), `web.save_api_key` (create),
  `web.toggle_api_key` (revoke/reactivate), `web.delete_api_key` (delete) —
  all in `routes/web.py`, all `@admin_required`.

## Request log

Every request to an authenticated endpoint — success or failure — is
recorded in the `Api_Request_Log` table (`Library/DBManager.py`), written by
an `after_request` hook on `api_bp` (`routes/api.py`) using the
`request.api_auth_*` attributes `Library/APIAuth.py` sets during the auth
check. Unauthenticated dashboard endpoints (`/api/data`, `/api/lots`,
`/api/health`, etc.) never set those attributes, so they're not logged here.

View it at **Admin > API Keys > Request Log** (`/admin/api-keys/log`,
`web.api_request_log`), filterable by key and status code. Each row has:
timestamp, method, endpoint, resolved key label (the DB key's name,
`env-key` for an env-var key, or `invalid`/`missing` for a failed auth
attempt), a truncated key prefix (never the full key), remote IP, status
code, and duration in ms.

Writes go through `Library/APIRequestLog.py`'s `log_request()`; reads (for
the admin page) go through that module's `get_page()`. There's no retention/
pruning yet — the table grows unbounded, which is fine at current traffic
but worth revisiting (e.g. a periodic delete of rows older than N days) if
this ever gets noisy.

## Adding a new authenticated endpoint

1. Import and apply both decorators, auth first so unauthenticated/invalid
   callers never consume rate-limit budget:

   ```python
   from Library.APIAuth import require_api_key
   from Library.RateLimiter import rate_limited

   @api_bp.route('/your-endpoint')
   @require_api_key
   @rate_limited
   def your_endpoint():
       ...
   ```

2. Document it in this file: method, path, headers, params, all response
   codes/shapes, and a request example.
3. If it exposes a new secret-bearing config value, add it (with a safe
   placeholder) to `env/.env.example`.
