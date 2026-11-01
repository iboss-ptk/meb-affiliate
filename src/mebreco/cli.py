"""Command line entry point.

    mebreco crawl  --site https://www.mebmarket.com [--pattern REGEX] [--limit N]
    mebreco probe  URL                # show what the extractor sees on one page
    mebreco embed  [--backend tfidf|semantic]
    mebreco similar URL [-k 10]
    mebreco stats
"""

from __future__ import annotations

import argparse
import random
import json
import sys

from .discover import iter_urls
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
        try:
            r = f.get(url, use_cache=not a.refresh)
        except DisallowedByRobots:
            s.mark(url, "disallowed")
            continue
        except Exception as e:  # keep crawling; record and move on
            print(f"error {url}: {e}", file=sys.stderr)
            s.mark(url, "error")
            continue
        if r.status != 200:
            s.mark(url, f"http_{r.status}")
            continue
        book = extract_book(url, r.text)
        if book:
            s.upsert(book)
            s.mark(url, "book")
            print(f"book  {book.title[:70]}")
        else:
            s.mark(url, "not_book")
        if n % 50 == 0:
            s.commit()
    s.commit()
    print(json.dumps(s.stats(), ensure_ascii=False))


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
    idx = build_semantic(books, a.model) if a.backend == "semantic" else build_tfidf(books)
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
