"""Command line entry point.

    mebreco crawl  --site https://www.mebmarket.com [--pattern REGEX] [--limit N]
    mebreco seeds                     # popular book IDs from MEB ranking lists
    mebreco update                    # daily: new books + popularity + refresh stale
    mebreco probe  URL                # show what the extractor sees on one page
    mebreco embed  [--backend tfidf|semantic]
    mebreco similar URL [-k 10]
    mebreco stats
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time

from .discover import book_id_of, iter_urls, new_book_urls
from .embed import Index, build_semantic, build_tfidf
from .extract import extract_book
from .fetch import DisallowedByRobots, PoliteFetcher
from .recommend import similar
from .store import Store

DB = "data/books.sqlite"
BOOK_URL = "https://www.mebmarket.com/index.php?action=BookDetails&book_id={}"
INDEX = "data/index.pkl"


def cmd_crawl(a: argparse.Namespace) -> None:
    f = PoliteFetcher(delay=a.delay)
    s = Store(a.db)
    n = 0
    if a.source == "seeds":
        ids = s.seed_ids()
        if not ids:
            sys.exit("no seeds yet; run `mebreco seeds` first")
        urls = [BOOK_URL.format(i) for i in ids]
    else:
        urls = iter_urls(f, a.site, a.pattern, a.sitemap)
    if a.shuffle and a.source == "sitemap":
        # Sitemaps are in ID order (oldest first); a random sample is more
        # representative for trial runs. Seeded so re-runs continue the same order.
        urls = list(urls)
        random.Random(a.seed).shuffle(urls)
    for url in urls:
        if a.limit and n >= a.limit:
            break
        if not a.refresh and s.visited(url):
            continue
        n += 1
        crawl_one(f, s, url, fresh=a.refresh)
        if n % 50 == 0:
            s.commit()
    s.commit()
    print(json.dumps(s.stats(), ensure_ascii=False))


def crawl_one(f: PoliteFetcher, s: Store, url: str, fresh: bool = False) -> str:
    """Fetch one book page, store the result, and return its status."""
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
            print(f"book  {book.title[:70]}")
            status = "book"
        else:
            status = "not_book"
            s.delete_book(url)  # removed from MEB since we last saw it
    s.mark(url, status)
    return status


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
            bump(crawl_one(f, s, url, fresh=True), "new")
        # If capped, only advance the marker past what we crawled, so the next run resumes.
        marker = book_id_of(todo[-1]) if len(todo) < len(urls) else max(above, max_id)
        s.set_state("max_sitemap_id", str(marker))
        s.commit()

    # 2. popularity
    if a.seeds:
        cmd_seeds(argparse.Namespace(db=a.db, lists=None, max_scrolls=a.max_scrolls))

    # 3. refresh stale books
    now = time.time()
    stale = s.stale_urls(now - a.popular_days * 86400, now - a.days * 86400, a.refresh_budget)
    print(f"refreshing {len(stale)} stale books")
    for i, url in enumerate(stale, 1):
        bump(crawl_one(f, s, url, fresh=True), "refresh")
        if i % 50 == 0:
            s.commit()
    s.commit()
    print(json.dumps({"this_run": counts, "totals": s.stats()}, ensure_ascii=False))


def cmd_seeds(a: argparse.Namespace) -> None:
    from .fetch import DEFAULT_UA
    from .seeds import LISTS, collect

    s = Store(a.db)
    names = a.lists or list(LISTS)
    for name, ids in collect(names, max_scrolls=a.max_scrolls, user_agent=DEFAULT_UA):
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
    if a.backend == "semantic":
        idx = build_semantic(books, a.model, store=Store(a.db))
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


def cmd_stats(a: argparse.Namespace) -> None:
    print(json.dumps(Store(a.db).stats(), ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="mebreco")
    p.add_argument("--db", default=DB)
    p.add_argument("--index", default=INDEX)
    sub = p.add_subparsers(required=True)

    c = sub.add_parser("crawl")
    c.add_argument("--source", choices=["sitemap", "seeds"], default="sitemap",
                   help="seeds = popular books from `mebreco seeds`, most popular first")
    c.add_argument("--site", default="https://www.mebmarket.com")
    c.add_argument(
        "--pattern",
        default=r"action=BookDetails&book_id=\d+",
        help="regex a URL must match to be fetched (default: MEB book detail pages)",
    )
    c.add_argument("--sitemap", action="append", help="explicit sitemap URL(s)")
    c.add_argument("--limit", type=int, default=0)
    c.add_argument("--delay", type=float, default=2.0, help="seconds between requests")
    c.add_argument("--shuffle", action="store_true", help="visit URLs in random order")
    c.add_argument("--seed", type=int, default=0)
    c.add_argument("--refresh", action="store_true", help="re-fetch already visited pages")
    c.set_defaults(fn=cmd_crawl)

    sd = sub.add_parser("seeds", help="collect popular book IDs from MEB ranking lists")
    sd.add_argument("--lists", nargs="*", help="default: all known lists")
    sd.add_argument("--max-scrolls", type=int, default=40)
    sd.set_defaults(fn=cmd_seeds)

    u = sub.add_parser("update", help="incremental daily update")
    u.add_argument("--site", default="https://www.mebmarket.com")
    u.add_argument("--delay", type=float, default=2.0)
    u.add_argument("--since-id", type=int, help="treat books above this ID as new (overrides saved state)")
    u.add_argument("--max-new", type=int, default=0, help="cap new books per run (0 = all)")
    u.add_argument("--seeds", action=argparse.BooleanOptionalAction, default=True,
                   help="refresh popularity lists (needs the browser extra)")
    u.add_argument("--max-scrolls", type=int, default=40)
    u.add_argument("--popular-days", type=float, default=7, help="refresh popular books older than this")
    u.add_argument("--days", type=float, default=90, help="refresh other books older than this")
    u.add_argument("--refresh-budget", type=int, default=300, help="max books to refresh per run")
    u.set_defaults(fn=cmd_update)

    pr = sub.add_parser("probe")
    pr.add_argument("url")
    pr.add_argument("--delay", type=float, default=2.0)
    pr.set_defaults(fn=cmd_probe)

    e = sub.add_parser("embed")
    e.add_argument("--backend", choices=["tfidf", "semantic"], default="tfidf")
    e.add_argument("--model", default="BAAI/bge-m3")
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

    st = sub.add_parser("stats")
    st.set_defaults(fn=cmd_stats)

    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
