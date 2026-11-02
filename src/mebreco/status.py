"""Crawl progress for the dashboard: counts only (no titles, URLs or book ids),
written to R2 as status.json and shown by the Worker at its root URL."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone

import httpx

from .sync import data_url

BOOK_URL = "https://www.mebmarket.com/index.php?action=BookDetails&book_id={}"


def collect_status(store, a, run: dict, now: float | None = None, neuron_limit: int = 8000) -> dict:
    now = time.time() if now is None else now
    cats = list(getattr(a, "categories", []) or [])
    db = store.db
    reps = store.rep_ids()
    crawled = 0
    for i in reps:
        row = db.execute("SELECT status FROM visited WHERE url=?", (BOOK_URL.format(i),)).fetchone()
        crawled += bool(row and row[0] in ("book", "not_book"))
    day = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")
    blocked_until = float(store.get_state("blocked_until", "0"))
    return {
        "updated": now,
        "categories": {"done": sum(store.get_state(f"listing:{c}:done") == "1" for c in cats), "total": len(cats)},
        "listing_pages": sum(int(store.get_state(f"listing:{c}:page", "0")) for c in cats),
        "listings": db.execute("SELECT COUNT(*) FROM listings").fetchone()[0],
        "sets": db.execute("SELECT COUNT(*) FROM listings WHERE is_set=1").fetchone()[0],
        "first_volumes": {"total": len(reps), "crawled": crawled},
        "books": db.execute("SELECT COUNT(*) FROM books").fetchone()[0],
        "embedded": db.execute("SELECT COUNT(*) FROM emb_cache WHERE model='@cf/baai/bge-m3'").fetchone()[0],
        "neurons_today": {"used": int(store.get_state(f"neurons:{day}", "0")), "limit": neuron_limit},
        "blocked_until": blocked_until if blocked_until > now else None,
        "run": run,
    }


def publish_status(status: dict, embed_url: str, token: str, client: httpx.Client | None = None) -> None:
    """Best effort: a dashboard hiccup must never break the crawl."""
    try:
        http = client or httpx.Client(timeout=30)
        http.put(data_url(embed_url, "status.json"), content=json.dumps(status).encode(),
                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    except Exception as e:  # noqa: BLE001
        print(f"status update failed: {type(e).__name__}")
