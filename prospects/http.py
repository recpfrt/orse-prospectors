"""Polite HTTP fetching: robots.txt, identification, timeouts, size caps, pacing."""

import time
import urllib.error
import urllib.request
import urllib.robotparser
from urllib.parse import urlparse

from . import config

_robots_cache: dict[str, urllib.robotparser.RobotFileParser | None] = {}
_last_request_at = 0.0


def _pace():
    global _last_request_at
    wait = config.DELAY_BETWEEN_REQUESTS_S - (time.monotonic() - _last_request_at)
    if wait > 0:
        time.sleep(wait)
    _last_request_at = time.monotonic()


def _raw_get(url: str) -> tuple[int, str, str]:
    """Returns (status, final_url, text). Text is truncated at MAX_BYTES_PER_PAGE."""
    _pace()
    req = urllib.request.Request(url, headers={
        "User-Agent": config.USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,text/plain;q=0.8",
        "Accept-Language": "nl,en;q=0.5",
    })
    try:
        with urllib.request.urlopen(req, timeout=config.REQUEST_TIMEOUT_S) as resp:
            ctype = resp.headers.get("Content-Type", "")
            if "html" not in ctype and "text" not in ctype:
                return resp.status, resp.geturl(), ""
            data = resp.read(config.MAX_BYTES_PER_PAGE)
            charset = resp.headers.get_content_charset() or "utf-8"
            return resp.status, resp.geturl(), data.decode(charset, errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, url, ""
    except Exception:
        return 0, url, ""


def robots_allows(url: str) -> bool:
    """Standard semantics: 404 -> allowed; network/5xx error -> disallowed (conservative)."""
    parsed = urlparse(url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    if base not in _robots_cache:
        status, _, text = _raw_get(base + "/robots.txt")
        if status == 200:
            rp = urllib.robotparser.RobotFileParser()
            rp.parse(text.splitlines())
            _robots_cache[base] = rp
        elif status in (401, 403):
            _robots_cache[base] = "deny-all"  # type: ignore[assignment]
        elif 400 <= status < 500:
            _robots_cache[base] = None  # no robots.txt -> allowed
        elif status == 0:
            _robots_cache[base] = "unreachable"  # type: ignore[assignment]  DNS/connection failure
        else:
            _robots_cache[base] = "deny-all"  # type: ignore[assignment]  5xx: be conservative
    rp = _robots_cache[base]
    if rp is None:
        return True
    if rp in ("deny-all", "unreachable"):
        return False
    return rp.can_fetch(config.USER_AGENT, url)


def polite_get(url: str) -> tuple[int, str, str]:
    """Fetch only if robots.txt allows it. Returns (status, final_url, text); status -1 = blocked by robots."""
    if not robots_allows(url):
        base = "{0.scheme}://{0.netloc}".format(urlparse(url))
        # 0 = site unreachable (dead domain / connection refused); -1 = robots.txt or bot protection says no
        return (0 if _robots_cache.get(base) == "unreachable" else -1), url, ""
    return _raw_get(url)
