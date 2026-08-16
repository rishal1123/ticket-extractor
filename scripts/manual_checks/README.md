# Manual portal/Znuny checks

These are **not automated tests** — they're ad-hoc scripts for manually poking at a
live ISP portal or the live Znuny REST API to inspect page/response structure while
developing an extractor. They are not run by pytest or CI.

- `check_znuny_client.py` — exercises `ZnunyClient` against the real Znuny REST API.
- `explore_rol.py`, `explore_rol2.py`, `explore_medianet.py`, `explore_znuny.py`,
  `explore_znuny_search.py` — drive a real Selenium Chrome browser against the live
  ROL/Medianet/Znuny web UI, logging in with credentials from `.env` and printing
  page structure. Note the app itself uses **Playwright**, not Selenium — these
  predate that and were never converted.

## Running

These require `selenium` and `webdriver_manager`, which are **not** in
`requirements.txt`/`requirements-dev.txt` (kept out of normal installs on purpose,
since nothing else in the app uses Selenium):

```bash
pip install selenium webdriver_manager
python scripts/manual_checks/explore_rol.py
```

Each script needs real portal/Znuny credentials in `.env` and will open a visible
browser window. Run them one at a time, by hand, when debugging a specific portal —
not as part of any automated pipeline.
