import httpx

from .logger import get_logger

logger = get_logger("flaresolverr")

# Cookie sameSite values FlareSolverr may return → Playwright accepts these values
_SAMESITE_MAP = {"strict": "Strict", "lax": "Lax", "none": "None", "no_restriction": "None", "unspecified": "Lax"}


class FlareSolverrClient:
    """HTTP client for FlareSolverr — solves Cloudflare challenges and returns cookies."""

    def __init__(self, url: str):
        self.url = url.rstrip("/")

    def solve(self, target_url: str, timeout_ms: int = 60000) -> dict | None:
        """Ask FlareSolverr to bypass a Cloudflare-protected URL.

        Returns {"cookies": [...playwright-format...], "user_agent": "..."} or None on failure.
        """
        try:
            resp = httpx.post(
                f"{self.url}/v1",
                json={"cmd": "request.get", "url": target_url, "maxTimeout": timeout_ms},
                timeout=timeout_ms / 1000 + 15,
            )
            data = resp.json()
        except Exception as e:
            logger.warning(f"FlareSolverr unreachable at {self.url}: {e}")
            return None

        if data.get("status") != "ok":
            logger.warning(f"FlareSolverr returned error: {data.get('message', data)}")
            return None

        solution = data.get("solution", {})
        raw_cookies = solution.get("cookies", [])
        user_agent = solution.get("userAgent", "")

        playwright_cookies = []
        for c in raw_cookies:
            domain = c.get("domain", "")
            # Playwright requires domain to start with "." for host-scoped cookies
            if domain and not domain.startswith("."):
                domain = "." + domain
            same_site_raw = (c.get("sameSite") or "lax").lower()
            same_site = _SAMESITE_MAP.get(same_site_raw, "Lax")
            cookie: dict = {
                "name": c["name"],
                "value": c["value"],
                "domain": domain,
                "path": c.get("path", "/"),
                "httpOnly": bool(c.get("httpOnly", False)),
                "secure": bool(c.get("secure", False)),
                "sameSite": same_site,
            }
            expiry = c.get("expiry") or c.get("expires")
            if expiry and isinstance(expiry, (int, float)) and expiry > 0:
                cookie["expires"] = float(expiry)
            playwright_cookies.append(cookie)

        logger.info(
            f"FlareSolverr solved {target_url} — {len(playwright_cookies)} cookies "
            f"(cf_clearance={'cf_clearance' in {c['name'] for c in playwright_cookies}})"
        )
        return {"cookies": playwright_cookies, "user_agent": user_agent}

    def is_available(self) -> bool:
        """Quick liveness check — returns False if FlareSolverr is not reachable."""
        try:
            resp = httpx.get(f"{self.url}/", timeout=3)
            return resp.status_code == 200
        except Exception:
            return False