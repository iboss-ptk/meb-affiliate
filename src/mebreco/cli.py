"""Command line entry point.

    mebreco crawl  --site https://www.mebmarket.com [--pattern REGEX] [--limit N]
    mebreco seeds                     # popular book IDs from MEB ranking lists
    mebreco update                    # daily: new books + popularity + refresh stale
    mebreco probe  URL                # show what the extractor sees on one page
    mebreco embed  [--backend tfidf|semantic|cloudflare]
    mebreco similar URL [-k 10]
    mebreco stats
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from pathlib import Path

from .discover import book_id_of, iter_urls, new_book_urls
from .embed import Index, build_cloudflare, build_semantic, build_tfidf
from .extract import extract_book
from .fetch import DisallowedByRobots, PoliteFetcher
from .recommend import similar
from .store import Store

DB = "data/books.sqlite"
BOOK_URL = "https://www.mebmarket.com/index.php?action=BookDetails&book_id={}"
INDEX = "data/index.npz"


class Blocked(Exception):
    pass


class Progress:
    """One live status line, plus a guard against getting blocked.

    After `block_streak` 403/429 answers in a row we assume the site is
    rate-limiting us: wait `cooldown` seconds and carry on. If it's still
    blocking after `max_cooldowns` waits in a row, give up (raise Blocked);
    blocked URLs stay retryable, so the next run picks them up.
    """

    def __init__(self, total: int | None, block_streak: int = 5, cooldown: float = 900, max_cooldowns: int = 2,
                 out=sys.stderr, quiet: bool = False) -> None:
        self.quiet = quiet  # public CI logs: counts only, no book titles
        self.total, self.block_streak, self.cooldown, self.max_cooldowns = total, block_streak, cooldown, max_cooldowns
        self.out = out
        self.tty = out.isatty()
        self.done = 0
        self.counts: dict[str, int] = {}
        self.streak = 0
        self.cooldowns = 0
        self.start = time.monotonic()
        self.note = ""

    def tick(self, status: str, note: str = "") -> None:
        self.done += 1
        self.counts[status] = self.counts.get(status, 0) + 1
        self.note = note or self.note
        if status in ("http_403", "http_429"):
            self.streak += 1
        else:
            self.streak = self.cooldowns = 0
        self.show()
        if self.streak >= self.block_streak:
            if self.cooldowns >= self.max_cooldowns:
                self.show(final=True)
                raise Blocked(f"{self.streak} blocked answers in a row after {self.cooldowns} cool-downs")
            self.cooldowns += 1
            self.streak = 0
            print(f"\nblocked {self.block_streak}x in a row: cooling down {self.cooldown / 60:.0f} min "
                  f"({self.cooldowns}/{self.max_cooldowns})", file=self.out, flush=True)
            time.sleep(self.cooldown)

    def line(self) -> str:
        el = time.monotonic() - self.start
        ok = self.counts.get("book", 0)
        bad = self.done - ok - self.counts.get("not_book", 0)
        head = f"[{self.done:,}"
        eta = ""
        if self.total:
            head += f"/{self.total:,}] {100 * self.done / self.total:4.1f}%"
            if self.done:
                left = (self.total - self.done) * el / self.done
                eta = f" · ETA {int(left // 3600)}h{int(left % 3600 // 60):02d}m"
        else:
            head += "]"
        tail = "" if self.quiet else f" · {self.note[:40]}"
        return f"{head} · {ok:,} books · {bad:,} errors{eta}{tail}"

    def show(self, final: bool = False) -> None:
        if self.tty:
            print("\r\033[K" + self.line(), end="\n" if final else "", file=self.out, flush=True)
        else:  # a log file: one line per book
            print(self.line(), file=self.out, flush=True)


BLOCK_HOLD_OFF = 6 * 3600  # after being blocked, don't crawl again for this long


def crawl_urls(f, s: Store, urls, p: "Progress", deadline: float | None = None,
               refresh: bool = False, limit: int = 0) -> str:
    """Crawl `urls` with progress and block handling.

    Returns "done", "deadline" (time budget reached) or "blocked"; when
    blocked, sets state `blocked_until` so later runs hold off instead of
    retrying from wherever they run.
    """
    try:
        for url in urls:
            if deadline is not None and time.time() >= deadline:
                return "deadline"
            if limit and p.done >= limit:
                break
            if not refresh and s.visited(url):
                continue
            status, title = crawl_one(f, s, url, fresh=refresh)
            s.commit()  # cheap next to the request delay; keeps `stats` and the web progress bar live
            p.tick(status, title or "")
        if p.tty:
            p.show(final=True)
        return "done"
    except Blocked as e:
        s.set_state("blocked_until", str(time.time() + BLOCK_HOLD_OFF))
        print(f"stopped: {e}. Holding off for {BLOCK_HOLD_OFF // 3600} h.", file=sys.stderr)
        return "blocked"
    finally:
        s.commit()


def cmd_crawl(a: argparse.Namespace) -> None:
    f = PoliteFetcher(delay=a.delay)
    s = Store(a.db)
    if a.source == "seeds":
        ids = s.seed_ids()
        if not ids:
            sys.exit("no seeds yet; run `mebreco seeds` first")
        urls = [BOOK_URL.format(i) for i in ids]
    elif a.source == "reps":
        ids = s.rep_ids()
        if not ids:
            sys.exit("no first volumes yet; run `mebreco listings` and `mebreco group` first")
        urls = [BOOK_URL.format(i) for i in ids]
    else:
        urls = iter_urls(f, a.site, a.pattern, a.sitemap)
    if a.shuffle and a.source == "sitemap":
        # Sitemaps are in ID order (oldest first); a random sample is more
        # representative for trial runs. Seeded so re-runs continue the same order.
        urls = list(urls)
        random.Random(a.seed).shuffle(urls)
    total = None
    if isinstance(urls, list):  # known up front: skip done ones now so the total is right
        urls = [u for u in urls if a.refresh or not s.visited(u)]
        if a.limit:
            urls = urls[: a.limit]
        total = len(urls)
    p = Progress(total, a.block_streak, a.cooldown * 60, quiet=a.quiet)
    try:
        crawl_urls(f, s, urls, p, refresh=a.refresh, limit=a.limit)
    except KeyboardInterrupt:
        p.show(final=True)
        print("interrupted; progress saved", file=sys.stderr)
    print(json.dumps(s.stats(), ensure_ascii=False))


def crawl_one(f: PoliteFetcher, s: Store, url: str, fresh: bool = False) -> tuple[str, str | None]:
    """Fetch one book page, store the result, and return (status, title)."""
    title = None
    try:
        r = f.get(url, use_cache=not fresh)
    except DisallowedByRobots:
        status = "disallowed"
    except Exception as e:  # keep crawling; record and move on
        print(f"error {url}: {e}", file=sys.stderr)
        status = "error"
    else:
        if r.status != 200:
            status = f"http_{r.status}"
        elif book := extract_book(url, r.text):
            s.upsert(book)
            title = book.title
            status = "book"
        else:
            status = "not_book"
            s.delete_book(url)  # removed from MEB since we last saw it
    s.mark(url, status)
    return status, title


def cmd_update(a: argparse.Namespace) -> None:
    """Incremental update, meant to run daily (e.g. from cron).

    1. New books: only IDs above the last seen maximum, from the newest
       sitemaps (unchanged sitemaps cost a 304, not a download).
    2. Popularity: re-read MEB's ranking lists (`--seeds`, needs the browser extra).
    3. Refresh: re-fetch books whose data is stale, popular ones first,
       within a request budget. Book pages have no ETag, so each is a full fetch.
    """
    f = PoliteFetcher(delay=a.delay)
    s = Store(a.db)
    counts: dict[str, int] = {}

    def bump(status: str, kind: str) -> None:
        counts[f"{kind}:{status}"] = counts.get(f"{kind}:{status}", 0) + 1

    # 1. new books
    last = s.get_state("max_sitemap_id")
    if last is None and a.since_id is None:
        _, max_id = new_book_urls(f, a.site, above_id=10**12)  # just learn the current max
        s.set_state("max_sitemap_id", str(max_id))
        s.commit()
        print(f"first update: baseline set at book_id {max_id}; later runs crawl books added after it")
    else:
        above = a.since_id if a.since_id is not None else int(last)
        urls, max_id = new_book_urls(f, a.site, above_id=above)
        print(f"{len(urls)} new books since book_id {above}")
        todo = urls[: a.max_new] if a.max_new else urls
        for url in todo:
            bump(crawl_one(f, s, url, fresh=True)[0], "new")
        # If capped, only advance the marker past what we crawled, so the next run resumes.
        marker = book_id_of(todo[-1]) if len(todo) < len(urls) else max(above, max_id)
        s.set_state("max_sitemap_id", str(marker))
        s.commit()

    # 2. popularity
    if a.seeds:
        cmd_seeds(argparse.Namespace(db=a.db, lists=None, max_pages=a.max_pages))

    # 3. refresh stale books
    now = time.time()
    stale = s.stale_urls(now - a.popular_days * 86400, now - a.days * 86400, a.refresh_budget)
    print(f"refreshing {len(stale)} stale books")
    for i, url in enumerate(stale, 1):
        bump(crawl_one(f, s, url, fresh=True)[0], "refresh")
        if i % 50 == 0:
            s.commit()
    s.commit()
    print(json.dumps({"this_run": counts, "totals": s.stats()}, ensure_ascii=False))


def cmd_seeds(a: argparse.Namespace) -> None:
    from .fetch import DEFAULT_UA
    from .seeds import LISTS, collect

    s = Store(a.db)
    names = a.lists or list(LISTS)
    for name, ids in collect(names, max_pages=a.max_pages, user_agent=DEFAULT_UA):
        s.save_seeds(name, ids)
        s.commit()
        print(f"{name:12} {len(ids)} books")
    print(f"unique seed books: {len(s.seed_ids())}")


def cmd_probe(a: argparse.Namespace) -> None:
    f = PoliteFetcher(delay=a.delay)
    r = f.get(a.url, use_cache=False)
    book = extract_book(a.url, r.text)
    print(f"HTTP {r.status}, {len(r.text)} chars")
    print(json.dumps(book.to_dict() if book else None, ensure_ascii=False, indent=2))
    if not book:
        print("No JSON-LD/OpenGraph book data found: a site-specific parser is needed.", file=sys.stderr)


def cmd_embed(a: argparse.Namespace) -> None:
    excluded = tuple(a.exclude_group)
    # MEB's own group label is inconsistent: some daily newspapers are filed
    # under หนังสือ (general books). Their titles do start with หนังสือพิมพ์.
    books = [
        b for b in Store(a.db).books()
        if b.group not in excluded and not b.title.startswith(excluded)
    ]
    if not books:
        sys.exit("no books in the database; run `crawl` first")
    if a.backend is None:
        a.backend = "cloudflare" if os.environ.get("MEBRECO_EMBED_URL") else "tfidf"
    if a.backend == "semantic":
        idx = build_semantic(books, a.model, store=Store(a.db))
    elif a.backend == "cloudflare":
        url, token = os.environ.get("MEBRECO_EMBED_URL"), os.environ.get("MEBRECO_EMBED_TOKEN")
        if not (url and token):
            sys.exit("set MEBRECO_EMBED_URL and MEBRECO_EMBED_TOKEN (see embed-worker/ and .env)")
        from .embed import Budget

        store = Store(a.db)
        idx = build_cloudflare(books, url, token, store=store, budget=Budget(store, a.max_neurons_per_day))
    else:
        idx = build_tfidf(books)  # refits from scratch; seconds even for 400k books
    idx.save(a.index)
    print(f"indexed {len(books)} books with {idx.backend} -> {a.index}")


def cmd_similar(a: argparse.Namespace) -> None:
    idx = Index.load(a.index)
    books = {b.url: b for b in Store(a.db).books()}
    if a.url not in idx.urls:
        sys.exit("unknown url; it must be in the crawled catalog")
    print(f"Because you looked at: {books[a.url].title}\n")
    for r in similar(idx, books, a.url, a.k, a.max_per_author or None):
        print(f"{r.score:.3f}  {r.book.title}  — {', '.join(r.book.authors)}\n       {r.link}")


def cmd_serve(a: argparse.Namespace) -> None:
    try:
        import uvicorn

        from .web import create_app
    except ImportError:
        sys.exit("pip install '.[web]' to run the web app")
    if not Path(a.index).exists():
        sys.exit(f"no index at {a.index}; run `mebreco embed` first")
    uvicorn.run(create_app(a.db, a.index), host=a.host, port=a.port)


def cmd_listings(a: argparse.Namespace) -> None:
    from . import listings
    from .fetch import DEFAULT_UA

    s = Store(a.db)
    try:
        with listings.playwright_fetch(a.delay, DEFAULT_UA) as fetch:
            saved = listings.crawl_categories(s, a.categories, a.max_listing_pages, a.delay, DEFAULT_UA, fetch)
    except listings.EmptyCategory as e:
        sys.exit(f"{e} (wrong category id, or MEB changed its page layout)")
    for cid, n in saved.items():
        print(f"category {cid}: {n} listings")


def cmd_group(a: argparse.Namespace) -> None:
    from collections import Counter

    from .listings import Listing
    from .series import assign_reps, group_report, volume_of

    s = Store(a.db)
    rows = [Listing(r["book_id"], r["title"], r["author"], r["publisher"], r["category_id"],
                    r["category_name"], r["rating_count"], bool(r["is_set"])) for r in s.listings()]
    assignments = assign_reps(rows)
    s.set_reps(assignments)
    s.commit()
    print(json.dumps(group_report(assignments, rows)))
    if a.examples:  # titles: local terminal only, never in public CI logs
        titles = {r.book_id: r.title for r in rows}
        sizes = Counter(k for k, _ in assignments.values())
        for key, n in sizes.most_common(a.examples):
            members = sorted((volume_of(titles[b])[1] or 1, b) for b, (k, _) in assignments.items() if k == key)
            print(f"{n:3} × {key.split('|')[0][:50]}  volumes {[v for v, _ in members]}  rep {members[0][1]}")


def _worker_env() -> tuple[str, str]:
    url, token = os.environ.get("MEBRECO_EMBED_URL"), os.environ.get("MEBRECO_EMBED_TOKEN")
    if not (url and token):
        sys.exit("set MEBRECO_EMBED_URL and MEBRECO_EMBED_TOKEN (see embed-worker/ and .env)")
    return url, token


def cmd_push(a: argparse.Namespace) -> None:
    from .sync import push

    push(Path(a.db), Path(a.index), *_worker_env())
    print("pushed db + index to R2")


def cmd_pull(a: argparse.Namespace) -> None:
    from .sync import pull

    if pull(Path(a.db), Path(a.index), *_worker_env()):
        print("pulled db + index from R2; restart `mebreco serve` to use them")
    else:
        print("nothing in R2 yet; local files unchanged")


def cmd_pipeline(a: argparse.Namespace) -> None:
    from .pipeline import run_pipeline

    print(f"pipeline: {run_pipeline(a)}")


def cmd_stats(a: argparse.Namespace) -> None:
    print(json.dumps(Store(a.db).stats(), ensure_ascii=False, indent=2))


def load_dotenv(path: str | Path = ".env") -> None:
    """Minimal .env reader (KEY=value lines); real environment variables win."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def main(argv: list[str] | None = None) -> None:
    load_dotenv()
    p = argparse.ArgumentParser(prog="mebreco")
    p.add_argument("--db", default=DB)
    p.add_argument("--index", default=INDEX)
    sub = p.add_subparsers(required=True)

    c = sub.add_parser("crawl")
    c.add_argument("--source", choices=["sitemap", "seeds", "reps"], default="sitemap",
                   help="seeds = popular books from `mebreco seeds`, most popular first; "
                        "reps = first volumes from `mebreco group`, newest first")
    c.add_argument("--quiet", action="store_true", help="no book titles in the output (public logs)")
    c.add_argument("--site", default="https://www.mebmarket.com")
    c.add_argument(
        "--pattern",
        default=r"action=BookDetails&book_id=\d+",
        help="regex a URL must match to be fetched (default: MEB book detail pages)",
    )
    c.add_argument("--sitemap", action="append", help="explicit sitemap URL(s)")
    c.add_argument("--limit", type=int, default=0)
    c.add_argument("--delay", type=float, default=4.0,
                   help="seconds between requests (MEB blocked us for ~10 min after ~17 min at 2s)")
    c.add_argument("--block-streak", type=int, default=5, help="403/429s in a row before cooling down")
    c.add_argument("--cooldown", type=float, default=15, help="minutes to wait when blocked")
    c.add_argument("--shuffle", action="store_true", help="visit URLs in random order")
    c.add_argument("--seed", type=int, default=0)
    c.add_argument("--refresh", action="store_true", help="re-fetch already visited pages")
    c.set_defaults(fn=cmd_crawl)

    sd = sub.add_parser("seeds", help="collect popular book IDs from MEB ranking lists")
    sd.add_argument("--lists", nargs="*", help="default: all known lists")
    sd.add_argument("--max-pages", type=int, default=20, help="pages per list (~40 books each)")
    sd.set_defaults(fn=cmd_seeds)

    u = sub.add_parser("update", help="incremental daily update")
    u.add_argument("--site", default="https://www.mebmarket.com")
    u.add_argument("--delay", type=float, default=2.0)
    u.add_argument("--since-id", type=int, help="treat books above this ID as new (overrides saved state)")
    u.add_argument("--max-new", type=int, default=0, help="cap new books per run (0 = all)")
    u.add_argument("--seeds", action=argparse.BooleanOptionalAction, default=True,
                   help="refresh popularity lists (needs the browser extra)")
    u.add_argument("--max-pages", type=int, default=20, help="pages per ranking list (~40 books each)")
    u.add_argument("--popular-days", type=float, default=7, help="refresh popular books older than this")
    u.add_argument("--days", type=float, default=90, help="refresh other books older than this")
    u.add_argument("--refresh-budget", type=int, default=300, help="max books to refresh per run")
    u.set_defaults(fn=cmd_update)

    pr = sub.add_parser("probe")
    pr.add_argument("url")
    pr.add_argument("--delay", type=float, default=2.0)
    pr.set_defaults(fn=cmd_probe)

    e = sub.add_parser("embed")
    e.add_argument("--backend", choices=["tfidf", "semantic", "cloudflare"], default=None,
                   help="cloudflare = bge-m3 via our Workers AI worker (embed-worker/); "
                        "default: cloudflare if MEBRECO_EMBED_URL is set, else tfidf")
    e.add_argument("--model", default="BAAI/bge-m3")
    e.add_argument("--max-neurons-per-day", type=int, default=8000,
                   help="cloudflare backend: daily cap (free allowance is 10,000)")
    e.add_argument(
        "--exclude-group",
        nargs="*",
        default=["หนังสือพิมพ์"],
        help="MEB groups to leave out of recommendations (default: newspapers)",
    )
    e.set_defaults(fn=cmd_embed)

    sm = sub.add_parser("similar")
    sm.add_argument("url")
    sm.add_argument("-k", type=int, default=10)
    sm.add_argument("--max-per-author", type=int, default=2, help="0 = no cap")
    sm.set_defaults(fn=cmd_similar)

    sv = sub.add_parser("serve", help="run the recommendation web app")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8000)
    sv.set_defaults(fn=cmd_serve)

    ls = sub.add_parser("listings", help="save MEB category listing cards (~40 books per page)")
    ls.add_argument("--categories", type=int, nargs="+", required=True)
    ls.add_argument("--max-listing-pages", type=int, default=10_000, help="stop after this page number")
    ls.add_argument("--delay", type=float, default=4.0)
    ls.set_defaults(fn=cmd_listings)

    gr = sub.add_parser("group", help="group listings into series; pick first volumes to fetch")
    gr.add_argument("--examples", type=int, default=0, help="also print the N largest series (local use)")
    gr.set_defaults(fn=cmd_group)

    sub.add_parser("push", help="upload db + index to R2 (via the Worker)").set_defaults(fn=cmd_push)
    sub.add_parser("pull", help="replace local db + index with R2's copy").set_defaults(fn=cmd_pull)

    pl = sub.add_parser("pipeline", help="one unattended run: pull, list, group, crawl, embed, push")
    pl.add_argument("--categories", type=int, nargs="+", required=True)
    pl.add_argument("--max-listing-pages", type=int, default=10_000)
    pl.add_argument("--time-budget-min", type=float, default=330, help="stop starting new work after this")
    pl.add_argument("--delay", type=float, default=4.0)
    pl.add_argument("--max-neurons-per-day", type=int, default=8000)
    pl.add_argument("--quiet", action="store_true", help="accepted for clarity; pipeline output never has titles")
    pl.set_defaults(fn=cmd_pipeline)

    st = sub.add_parser("stats")
    st.set_defaults(fn=cmd_stats)

    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
