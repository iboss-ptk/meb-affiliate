"""Polite HTTP fetching: robots.txt, rate limiting, retries, and an on-disk cache.

Every request goes through `PoliteFetcher.get`, so these rules can't be bypassed
by accident elsewhere in the code.
"""

from __future__ import annotations

import hashlib
import time
import urllib.robotparser
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx

DEFAULT_UA = "mebreco/0.1 (book-discovery research; contact: see repo README)"


class DisallowedByRobots(Exception):
    pass


@dataclass
class Response:
    url: str
    status: int
    text: str
    from_cache: bool


class PoliteFetcher:
    def __init__(
        self,
        cache_dir: Path | str = "data/cache",
        delay: float = 2.0,
        user_agent: str = DEFAULT_UA,
        max_retries: int = 4,
        timeout: float = 30.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.delay = delay
        self.user_agent = user_agent
        self.max_retries = max_retries
        self.client = client or httpx.Client(
            headers={"User-Agent": user_agent},
            timeout=timeout,
            follow_redirects=True,
        )
        self._robots: dict[str, urllib.robotparser.RobotFileParser] = {}
        self._last_request = 0.0

    # -- robots ---------------------------------------------------------------
    def robots(self, url: str) -> urllib.robotparser.RobotFileParser:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._robots:
            rp = urllib.robotparser.RobotFileParser()
            r = self._request(f"{origin}/robots.txt")
            if r.status_code >= 400:
                rp.parse([])  # no robots.txt => everything allowed
            else:
                rp.parse(r.text.splitlines())
            self._robots[origin] = rp
            crawl_delay = rp.crawl_delay(self.user_agent)
            if crawl_delay:
                self.delay = max(self.delay, float(crawl_delay))
        return self._robots[origin]

    def allowed(self, url: str) -> bool:
        return self.robots(url).can_fetch(self.user_agent, url)

    def sitemaps(self, url: str) -> list[str]:
        return list(self.robots(url).site_maps() or [])

    # -- fetching -------------------------------------------------------------
    def _cache_path(self, url: str) -> Path:
        return self.cache_dir / (hashlib.sha1(url.encode()).hexdigest() + ".html")

    def _throttle(self) -> None:
        wait = self.delay - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    def _request(self, url: str) -> httpx.Response:
        backoff = self.delay
        for attempt in range(self.max_retries + 1):
            self._throttle()
            try:
                r = self.client.get(url)
            except httpx.TransportError:
                if attempt == self.max_retries:
                    raise
            else:
                if r.status_code not in (429, 500, 502, 503, 504):
                    return r
                if attempt == self.max_retries:
                    return r
                retry_after = r.headers.get("Retry-After", "")
                if retry_after.isdigit():
                    backoff = max(backoff, float(retry_after))
            time.sleep(backoff)
            backoff *= 2
        raise RuntimeError("unreachable")

    def get(self, url: str, use_cache: bool = True) -> Response:
        cached = self._cache_path(url)
        if use_cache and cached.exists():
            return Response(url, 200, cached.read_text(encoding="utf-8"), True)
        if not self.allowed(url):
            raise DisallowedByRobots(url)
        r = self._request(url)
        if r.status_code == 200:
            cached.write_text(r.text, encoding="utf-8")
        return Response(str(r.url), r.status_code, r.text, False)
