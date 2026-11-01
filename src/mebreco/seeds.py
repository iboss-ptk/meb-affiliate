"""Collect popular book IDs from MEB's ranking lists (Top Paid, Bestseller, ...).

These lists are filled in by JavaScript after the page loads, so plain HTTP
gets an empty page. We render them in a headless browser instead, the same
way a visitor's browser would, and read the book links from the finished page.
We don't call MEB's private API directly.

Requires: pip install '.[browser]' and a Chromium binary (set CHROMIUM_PATH,
or let Playwright use its own after `playwright install chromium`).
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Iterable

BASE = "https://www.mebmarket.com/index.php?store=all&action=book_list&condition={}"

# Names come from MEB's own menu config (config in the book_list page).
LISTS = {
    "topmonth": "Top Paid of month",
    "topweek": "Top Paid of week",
    "paid": "Top Paid",
    "bestseller": "Hall of fame: Best Seller",
    "halloffame": "Hall of fame",
    "toprating": "Top rating",
    "award": "Award",
    "new_hit": "New Hits",
}

BOOK_ID = re.compile(r"[?&]book_id=(\d+)")
BLOCK_TYPES = {"image", "media", "font"}
BLOCK_HOSTS = ("googletagmanager.com", "google-analytics.com", "facebook.net", "doubleclick.net")


def _launch(p):
    exe = os.environ.get("CHROMIUM_PATH")
    return p.chromium.launch(executable_path=exe, args=["--no-sandbox"]) if exe else p.chromium.launch()


def ids_from_hrefs(hrefs: Iterable[str]) -> list[str]:
    """Book IDs in first-seen order (= rank on the page)."""
    out: dict[str, None] = {}
    for h in hrefs:
        m = BOOK_ID.search(h or "")
        if m:
            out.setdefault(m.group(1), None)
    return list(out)


def collect(lists: Iterable[str], max_scrolls: int = 40, pause: float = 2.0, user_agent: str | None = None):
    """Yield (list_name, [book_id, ...]) per ranking list."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = _launch(p)
        ctx = browser.new_context(user_agent=user_agent) if user_agent else browser.new_context()

        def route(r):
            req = r.request
            if req.resource_type in BLOCK_TYPES or any(h in req.url for h in BLOCK_HOSTS):
                return r.abort()
            return r.continue_()

        ctx.route("**/*", route)
        page = ctx.new_page()
        for name in lists:
            page.goto(BASE.format(name), wait_until="networkidle", timeout=60_000)
            seen = 0
            for _ in range(max_scrolls):
                hrefs = page.eval_on_selector_all("a[href*='book_id=']", "els => els.map(e => e.href)")
                n = len(ids_from_hrefs(hrefs))
                if n == seen:  # nothing new loaded since last scroll
                    break
                seen = n
                page.mouse.wheel(0, 20_000)
                time.sleep(pause)
                page.wait_for_load_state("networkidle")
            hrefs = page.eval_on_selector_all("a[href*='book_id=']", "els => els.map(e => e.href)")
            yield name, ids_from_hrefs(hrefs)
            time.sleep(pause)
        browser.close()
