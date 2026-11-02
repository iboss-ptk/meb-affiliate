"""MEB category listing pages: every book in a category, ~40 per request.

Listing pages give enough to filter and group books (title, author,
category, SET bundle or not) before deciding which detail pages to fetch.
"""

from __future__ import annotations

import contextlib
import re
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass


@dataclass
class Listing:
    book_id: str
    title: str
    author: str | None
    publisher: str | None
    category_id: int
    category_name: str | None
    rating_count: int | None
    is_set: bool


LIST_URL = "https://www.mebmarket.com/index.php?store=category&action=book_list&category_id={}&condition=new&page_no={}"
DISCOUNT = re.compile(r"^-\d+%$")
RATING = re.compile(r"^([\d,]+|No) Rating", re.I)

# Each card is the nearest ancestor of a book link that holds no more than two
# book links (cover + title); its text lines are title, author / publisher,
# category, rating, with an optional discount badge first.
CARDS_JS = """() => {
  const out = [], seen = new Set();
  for (const a of document.querySelectorAll("a[href*='/ebook-']")) {
    const id = a.href.match(/ebook-(\\d+)/)[1]; if (seen.has(id)) continue; seen.add(id);
    let c = a; while (c.parentElement && c.parentElement.querySelectorAll("a[href*='/ebook-']").length <= 2) c = c.parentElement;
    out.push([id, c.innerText.split("\\n").map(s => s.trim()).filter(Boolean)]);
  }
  return out;
}"""

Fetch = Callable[[int, int], list[tuple[str, list[str]]]]


class EmptyCategory(Exception):
    pass


def parse_card(book_id: str, lines: list[str], category_id: int) -> Listing | None:
    lines = [ln for ln in lines if not DISCOUNT.match(ln)]
    if not lines:
        return None
    title, rest = lines[0], lines[1:]
    is_set = title.startswith("SET ")
    if is_set:
        title = title[4:].strip()
    rating = None
    rating_idx = next((i for i, ln in enumerate(rest) if RATING.match(ln)), None)
    if rating_idx is not None:
        m = RATING.match(rest[rating_idx]).group(1)
        rating = None if m.lower() == "no" else int(m.replace(",", ""))
        rest = rest[:rating_idx]
    author = publisher = category = None
    if rest:
        # author names can contain "/", so split on the last " / "
        author, _, publisher = rest[0].rpartition(" / ") if " / " in rest[0] else (rest[0], "", None)
        publisher = publisher or None
    if len(rest) > 1:
        category = rest[-1]
    return Listing(book_id, title, author, publisher, category_id, category, rating, is_set)


@contextlib.contextmanager
def playwright_fetch(delay: float, user_agent: str) -> Iterator[Fetch]:
    """A fetch(category_id, page_no) backed by one headless browser page,
    pausing `delay` seconds before every navigation after the first."""
    from playwright.sync_api import sync_playwright

    from .seeds import BLOCK_HOSTS, BLOCK_TYPES, _launch

    with sync_playwright() as p:
        browser = _launch(p)
        ctx = browser.new_context(user_agent=user_agent)
        ctx.route("**/*", lambda r: r.abort()
                  if r.request.resource_type in BLOCK_TYPES or any(h in r.request.url for h in BLOCK_HOSTS)
                  else r.continue_())
        page = ctx.new_page()
        first = [True]

        def fetch(category_id: int, n: int):
            if not first[0]:
                time.sleep(delay)
            first[0] = False
            page.goto(LIST_URL.format(category_id, n), wait_until="networkidle", timeout=60_000)
            return page.evaluate(CARDS_JS)

        try:
            yield fetch
        finally:
            browser.close()


def crawl_categories(store, category_ids: list[int], max_pages: int, delay: float, user_agent: str,
                     fetch: Fetch | None = None, deadline: float | None = None) -> dict[int, int]:
    """Save listing cards for each category, page by page, up to page `max_pages`.

    Resumable: the last saved page per category is kept in state
    (`listing:<id>:page`); a page that adds no new books marks the category
    done (`listing:<id>:done`). Returns listings saved per category this call.
    """
    if fetch is None:
        with playwright_fetch(delay, user_agent) as f:
            return crawl_categories(store, category_ids, max_pages, delay, user_agent, f, deadline)
    saved: dict[int, int] = {}
    for cid in category_ids:
        saved[cid] = 0
        if store.get_state(f"listing:{cid}:done") == "1":
            continue
        n = int(store.get_state(f"listing:{cid}:page", "0")) + 1
        known = {r["book_id"] for r in store.listings()}
        while n <= max_pages:
            if deadline and time.time() >= deadline:
                return saved
            cards = [c for c in (parse_card(i, lines, cid) for i, lines in fetch(cid, n)) if c]
            new = [c for c in cards if c.book_id not in known]
            if not new:
                if n == 1 and not cards:
                    raise EmptyCategory(f"category {cid}: first page has no books")
                store.set_state(f"listing:{cid}:done", "1")
                store.commit()
                break
            store.upsert_listings(new)
            known.update(c.book_id for c in new)
            saved[cid] += len(new)
            store.set_state(f"listing:{cid}:page", str(n))
            store.commit()
            print(f"category {cid}: page {n}, {saved[cid]} new listings", file=sys.stderr, flush=True)
            n += 1
    return saved
