# ISP Ticket Formatter

Paste an unformatted ISP dashboard dump; it auto-detects the ISP (Ooredoo,
Dhiraagu, Medianet — ROL stubbed), formats a clean standardized ticket block, and
lets you copy it (rich text, bold heading) to the clipboard.

It ships in two front ends over one shared domain layer (`model/`):

| Front end | Entry point | Use |
|-----------|-------------|-----|
| Desktop (tkinter) | `python main.py` | Local Windows app, auto-copies on paste |
| Web (Flask)       | `python webapp.py` | Browser app, containerized for Portainer |

## Run the web app locally

```bash
pip install -r requirements.txt
python webapp.py          # dev server on http://localhost:8000
```

For a production-style run: `gunicorn -b 0.0.0.0:8000 -w 2 webapp:app`

## Deploy with Docker / Portainer

The repo includes a `Dockerfile`, a `docker-compose.yml`, and a `.env` file.
The host port is configurable via `HOST_PORT` in `.env` (defaults to **8087**);
the container itself always listens on `8000`.

**Portainer (Stacks):**
1. Portainer → **Stacks** → **Add stack**.
2. Either:
   - **Web editor** — paste the contents of `docker-compose.yml`, or
   - **Repository** — point it at this Git repo (Compose path `docker-compose.yml`).
3. (Optional) Set `HOST_PORT` under **Environment variables** to change the
   published port.
4. **Deploy the stack.** Portainer builds the image and starts the container.
5. Open `http://<docker-host>:8087`.

The container exposes a `/healthz` endpoint used by the compose healthcheck.

**Plain Docker:**
```bash
docker compose up -d          # build + run; reads HOST_PORT from .env (8087)
# or
docker build -t isp-ticket-formatter .
docker run -d -p 8087:8000 --name isp-ticket-formatter isp-ticket-formatter
```

Change the published port via `HOST_PORT` in `.env` (e.g. `HOST_PORT=9000`).

## Adding an ISP

Subclass `BaseFormatter` in `model/formatters.py` and register it in
`ALL_FORMATTERS`. Both front ends pick it up automatically.