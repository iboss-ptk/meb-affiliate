"""Tests use hand-written fixtures, NOT real MEB pages.

They check the generic logic (robots, sitemaps, JSON-LD/OG parsing, ranking).
Whether MEB actually exposes JSON-LD has to be checked with `mebreco probe`.
"""

import json

import httpx
import pytest

from mebreco.discover import iter_urls
from mebreco.embed import build_tfidf
from mebreco.extract import Book, extract_book
from mebreco.fetch import DisallowedByRobots, PoliteFetcher
from mebreco.recommend import affiliate_link, similar
from mebreco.store import Store

SITE = "https://shop.test"


def page_jsonld(title, author, genre, desc):
    ld = {
        "@context": "https://schema.org",
        "@graph": [
            {"@type": "BreadcrumbList", "name": "nav"},
            {
                "@type": "Book",
                "name": title,
                "author": [{"@type": "Person", "name": author}],
                "genre": genre,
                "description": desc,
                "image": {"@type": "ImageObject", "url": "https://img.test/c.jpg"},
                "offers": {"@type": "Offer", "price": "1,299.00", "priceCurrency": "THB"},
            },
        ],
    }
    return f'<html><head><script type="application/ld+json">{json.dumps(ld, ensure_ascii=False)}</script></head></html>'


ROBOTS = f"User-agent: *\nDisallow: /private/\nSitemap: {SITE}/sitemap_index.xml\n"
INDEX = f"""<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<sitemap><loc>{SITE}/sm1.xml</loc></sitemap></sitemapindex>"""
SM1 = f"""<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url><loc>{SITE}/book/1</loc></url><url><loc>{SITE}/about</loc></url>
<url><loc>{SITE}/private/x</loc></url></urlset>"""


def make_fetcher(tmp_path, routes, calls=None):
    def handler(req):
        if calls is not None:
            calls.append(str(req.url))
        body = routes.get(str(req.url))
        return httpx.Response(200, text=body) if body is not None else httpx.Response(404)

    return PoliteFetcher(cache_dir=tmp_path / "cache", delay=0, client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_extract_jsonld_book_from_graph():
    b = extract_book(f"{SITE}/book/1", page_jsonld("ดาบพิฆาตอสูร", "Koyoharu", ["มังงะ"], "เรื่องราวของ..."))
    assert b.title == "ดาบพิฆาตอสูร"
    assert b.authors == ["Koyoharu"]
    assert b.categories == ["มังงะ"]
    assert b.price == 1299.0 and b.currency == "THB"
    assert b.image == "https://img.test/c.jpg"


def test_extract_og_fallback_requires_book_type():
    og = '<meta property="og:type" content="book"><meta property="og:title" content="T"><meta property="book:author" content="A">'
    assert extract_book("u", og).authors == ["A"]
    assert extract_book("u", '<meta property="og:type" content="website"><meta property="og:title" content="Home">') is None


def test_discover_respects_sitemap_index_and_robots(tmp_path):
    calls = []
    f = make_fetcher(tmp_path, {f"{SITE}/robots.txt": ROBOTS, f"{SITE}/sitemap_index.xml": INDEX, f"{SITE}/sm1.xml": SM1}, calls)
    urls = list(iter_urls(f, SITE))
    assert urls == [f"{SITE}/book/1", f"{SITE}/about", f"{SITE}/private/x"]
    assert list(iter_urls(f, SITE, pattern=r"/book/")) == [f"{SITE}/book/1"]
    with pytest.raises(DisallowedByRobots):
        f.get(f"{SITE}/private/x")
    assert f"{SITE}/private/x" not in calls


def test_cache_avoids_refetch(tmp_path):
    calls = []
    f = make_fetcher(tmp_path, {f"{SITE}/robots.txt": "", f"{SITE}/book/1": "hi"}, calls)
    f.get(f"{SITE}/book/1")
    f.get(f"{SITE}/book/1")
    assert calls.count(f"{SITE}/book/1") == 1


def test_store_roundtrip(tmp_path):
    s = Store(tmp_path / "db.sqlite")
    s.upsert(Book(url="u", title="ก", authors=["x"], categories=["c"]))
    s.upsert(Book(url="u", title="ข", authors=["x"], categories=["c"]))
    s.commit()
    assert [b.title for b in s.books()] == ["ข"]


def corpus():
    data = [
        ("1", "ดาบพิฆาตอสูร เล่ม 1", "A", "มังงะ แฟนตาซี", "นักล่าอสูร ต่อสู้ ดาบ"),
        ("2", "ดาบพิฆาตอสูร เล่ม 2", "A", "มังงะ แฟนตาซี", "นักล่าอสูร ต่อสู้ ดาบ"),
        ("3", "ดาบพิฆาตอสูร เล่ม 3", "A", "มังงะ แฟนตาซี", "นักล่าอสูร ต่อสู้ ดาบ"),
        ("4", "มหาเวทย์ผนึกมาร", "B", "มังงะ แฟนตาซี", "คำสาป ต่อสู้ อสูร"),
        ("5", "เศรษฐศาสตร์ฉบับเข้าใจง่าย", "C", "ธุรกิจ", "การเงิน ตลาด เงินเฟ้อ"),
        ("6", "ลงทุนหุ้นให้รวย", "D", "ธุรกิจ", "การเงิน หุ้น ตลาด"),
    ]
    return [Book(url=u, title=t, authors=[a], categories=c.split(), description=d) for u, t, a, c, d in data]


def test_similar_ranks_topical_and_caps_same_author():
    books = corpus()
    idx = build_tfidf(books)
    by = {b.url: b for b in books}
    # No cap: the same-series volumes dominate.
    uncapped = [r.book.url for r in similar(idx, by, "1", k=3, max_per_author=None)]
    assert set(uncapped[:2]) == {"2", "3"}
    # Cap of 2 => at most 1 more book by the seed's author, freeing a slot for discovery.
    capped = [r.book.url for r in similar(idx, by, "1", k=3, max_per_author=2)]
    assert sum(by[u].authors == ["A"] for u in capped) == 1
    assert capped[1] == "4"  # the other fantasy manga beats the business books


def test_public_url_uses_ebook_slug():
    from mebreco.recommend import public_url

    b = Book(url="https://www.mebmarket.com/index.php?action=BookDetails&book_id=481844", title="ดึง", book_id="481844")
    assert public_url(b) == "https://www.mebmarket.com/ebook-481844-%E0%B8%94%E0%B8%B6%E0%B8%87"
    b2 = Book(url="u", title="A / B? 50% off", book_id="7")
    assert public_url(b2) == "https://www.mebmarket.com/ebook-7-A-B-50-off"
    assert public_url(Book(url="u", title="x")) == "u"


def test_affiliate_link_template():
    assert affiliate_link("https://x/a?b=1") == "https://x/a?b=1"
    assert affiliate_link("https://x/a", "https://aff/?u={url_enc}") == "https://aff/?u=https%3A%2F%2Fx%2Fa"


def test_seed_pagination_stops_at_empty_page():
    from mebreco.seeds import paginate

    pages = {1: ["a", "b"], 2: ["c", "b"], 3: []}
    asked = []

    def fetch(n):
        asked.append(n)
        return pages.get(n, ["never"])

    assert paginate(fetch, max_pages=10) == ["a", "b", "c"]
    assert asked == [1, 2, 3]
    assert paginate(fetch, max_pages=1) == ["a", "b"]


def test_seed_ordering(tmp_path):
    from mebreco.seeds import ids_from_hrefs

    assert ids_from_hrefs(["/x?action=BookDetails&book_id=9", "/y?book_id=9", "/z?book_id=3", "/nope"]) == ["9", "3"]
    assert ids_from_hrefs([
        "https://www.mebmarket.com/ebook-477756-%E0%B9%80",
        "https://www.mebmarket.com/ebook-466497-x",
        "https://www.mebmarket.com/ebook-477756-%E0%B9%80",
        "https://www.mebmarket.com/?action=text_books_home_page&text_book_id=all",
    ]) == ["477756", "466497"]
    s = Store(tmp_path / "db.sqlite")
    s.save_seeds("topmonth", ["a", "b", "c"])
    s.save_seeds("bestseller", ["c", "d"])
    s.save_seeds("topmonth", ["a", "b", "c"])  # re-run replaces, doesn't duplicate
    # rank 1 on any list first (a and c; c is on two lists), then rank 2, ...
    assert s.seed_ids() == ["c", "a", "b", "d"]


def _book_sitemap(ids):
    locs = "".join(f"<url><loc>{SITE}/index.php?action=BookDetails&amp;book_id={i}</loc></url>" for i in ids)
    return f'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{locs}</urlset>'


def test_new_book_urls_reads_newest_sitemaps_only(tmp_path):
    from mebreco.discover import new_book_urls

    robots = "".join(f"Sitemap: {SITE}/sitemap/bookdetail_sitemap{i}.xml\n" for i in (1, 2, 3))
    routes = {
        f"{SITE}/robots.txt": robots,
        f"{SITE}/sitemap/bookdetail_sitemap1.xml": _book_sitemap([1, 2, 3]),
        f"{SITE}/sitemap/bookdetail_sitemap2.xml": _book_sitemap([4, 5, 6]),
        f"{SITE}/sitemap/bookdetail_sitemap3.xml": _book_sitemap([7, 9]),
    }
    calls = []
    f = make_fetcher(tmp_path, routes, calls)
    urls, max_id = new_book_urls(f, SITE, above_id=5)
    assert [u.rsplit("=", 1)[1] for u in urls] == ["6", "7", "9"]
    assert max_id == 9
    assert f"{SITE}/sitemap/bookdetail_sitemap1.xml" not in calls  # stopped early
    # baseline mode (huge threshold) must report the real max, not the threshold
    assert new_book_urls(f, SITE, above_id=10**12) == ([], 9)


def test_conditional_get_reuses_cache_on_304(tmp_path):
    hits = []

    def handler(req):
        hits.append(req.headers.get("If-None-Match"))
        if str(req.url).endswith("robots.txt"):
            return httpx.Response(404)
        if req.headers.get("If-None-Match") == '"v1"':
            return httpx.Response(304)
        return httpx.Response(200, text="<xml/>", headers={"ETag": '"v1"'})

    f = PoliteFetcher(cache_dir=tmp_path / "c", delay=0, client=httpx.Client(transport=httpx.MockTransport(handler)))
    first = f.get_fresh(f"{SITE}/s.xml")
    second = f.get_fresh(f"{SITE}/s.xml")
    assert (first.text, first.not_modified) == ("<xml/>", False)
    assert (second.text, second.not_modified) == ("<xml/>", True)


def test_stale_urls_popular_first(tmp_path):
    import time as _t

    s = Store(tmp_path / "db.sqlite")
    for bid in ("1", "2", "3"):
        s.upsert(Book(url=f"u{bid}", title=bid, book_id=bid))
    s.db.execute("UPDATE books SET fetched_at = ?", (_t.time() - 10 * 86400,))  # all 10 days old
    s.save_seeds("topmonth", ["3"])
    s.commit()
    now = _t.time()
    # popular refresh after 7 days, others after 90: only the popular one is due
    assert s.stale_urls(now - 7 * 86400, now - 90 * 86400, 10) == ["u3"]
    # others after 5 days: everything due, popular first
    assert s.stale_urls(now - 7 * 86400, now - 5 * 86400, 10)[0] == "u3"


def _catalog():
    def bk(i, title, author, cat, desc):
        return Book(url=f"u{i}", title=title, authors=[author], categories=[cat], description=desc, book_id=str(i))

    return [
        bk(1, "Dragon Empress", "A", "romance-china", "palace intrigue empress emperor concubine"),
        bk(2, "Palace Concubine", "B", "romance-china", "palace concubine emperor intrigue harem"),
        bk(3, "Emperor's Harem", "C", "romance-china", "emperor harem palace concubine intrigue"),
        bk(4, "Space Marines", "D", "scifi", "spaceship laser galaxy war marines"),
        bk(5, "Galaxy Wars", "E", "scifi", "galaxy spaceship war laser fleet"),
        bk(6, "Cooking Basics", "F", "food", "recipe kitchen cooking rice soup"),
    ]


def test_recommend_for_combines_likes_and_excludes_picks():
    from mebreco.recommend import recommend_for

    books = _catalog()
    idx = build_tfidf(books)
    by_url = {b.url: b for b in books}
    recs = recommend_for(idx, by_url, liked=["u1", "u2"], k=3)
    urls = [r.book.url for r in recs]
    assert "u1" not in urls and "u2" not in urls
    assert urls[0] == "u3"
    assert recs[0].because.url in {"u1", "u2"}

    # disliking the remaining palace book pushes it down below sci-fi
    recs = recommend_for(idx, by_url, liked=["u4"], disliked=["u3"], k=5)
    urls = [r.book.url for r in recs]
    assert urls[0] == "u5" and "u3" not in urls
    assert recommend_for(idx, by_url, liked=["missing"]) == []


def test_web_api(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from mebreco.web import create_app

    books = _catalog()
    for b in books:
        b.group = "นิยาย" if b.categories[0] != "food" else "หนังสือ"
    s = Store(tmp_path / "db.sqlite")
    for b in books:
        s.upsert(b)
    s.save_seeds("topmonth", ["2", "1", "4", "6"])  # 3 and 5 aren't popular
    s.commit()
    build_tfidf(books).save(tmp_path / "idx.pkl")
    c = TestClient(create_app(tmp_path / "db.sqlite", tmp_path / "idx.pkl"))

    assert c.get("/").status_code == 200
    g = {x["group"]: x for x in c.get("/api/genres", params={"min_books": 1}).json()}
    assert g["นิยาย"]["count"] == 3 and g["หนังสือ"]["count"] == 1

    ids = lambda r: [b["id"] for b in r.json()]
    assert ids(c.get("/api/onboard")) == ["2", "1", "4", "6"]  # popularity order, popular only
    assert ids(c.get("/api/onboard", params={"groups": "นิยาย", "exclude": "2"})) == ["1", "4"]
    assert ids(c.get("/api/onboard", params={"categories": "scifi"})) == ["4"]

    recs = c.post("/api/recommend", json={"liked": ["1", "2"], "k": 2}).json()
    assert recs[0]["id"] == "3" and recs[0]["because"]["id"] in {"1", "2"}
    assert c.post("/api/recommend", json={"liked": ["nope"]}).status_code == 400
    assert ids(c.get("/api/books", params={"ids": ["4", "nope"]})) == ["4"]
    st = c.get("/api/status").json()
    assert (st["seed_books"], st["crawled"], st["in_recommendations"]) == (4, 4, 6)


def test_progress_cools_down_then_gives_up(monkeypatch):
    import io

    from mebreco import cli

    slept = []
    monkeypatch.setattr(cli.time, "sleep", slept.append)
    p = cli.Progress(total=20, block_streak=2, cooldown=60, max_cooldowns=1, out=io.StringIO())
    p.tick("book", "A")
    p.tick("http_403"); p.tick("http_403")  # first streak -> one cool-down
    assert slept == [60]
    p.tick("book")  # success resets
    p.tick("http_403")
    p.tick("http_403")  # cool-down count was reset by the success
    assert slept == [60, 60]
    p.tick("http_403")
    with pytest.raises(cli.Blocked):
        p.tick("http_403")
    assert "[8/20]" in p.line() and "2 books" in p.line() and "6 errors" in p.line()


def test_blocked_urls_are_retried(tmp_path):
    s = Store(tmp_path / "db.sqlite")
    for url, status in [("a", "book"), ("b", "http_403"), ("c", "http_404"), ("d", "error"), ("e", "http_503")]:
        s.mark(url, status)
    assert [u for u in "abcdef" if s.visited(u)] == ["a", "c"]


def test_cloudflare_backend_batches_and_caches(tmp_path):
    import numpy as np

    from mebreco.embed import CF_MODEL, build_cloudflare

    calls = []

    def handler(req):
        texts = json.loads(req.content)["texts"]
        calls.append(len(texts))
        assert req.headers["Authorization"] == "Bearer t0k"
        return httpx.Response(200, json={"vectors": [[float(len(t)), 1.0] for t in texts]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    books = _catalog()
    s = Store(tmp_path / "db.sqlite")
    idx = build_cloudflare(books, "https://w/embed", "t0k", store=s, batch_size=4, client=client)
    assert calls == [4, 2] and idx.backend == "cloudflare"
    assert np.allclose(np.linalg.norm(idx.matrix, axis=1), 1.0)  # normalised -> dot == cosine
    assert len(s.cached_vectors(CF_MODEL, [])) == 0
    build_cloudflare(books, "https://w/embed", "t0k", store=s, batch_size=4, client=client)
    assert calls == [4, 2]  # second run is fully cached


def test_embed_batches_respect_char_budget():
    from mebreco.embed import _batches

    texts = ["a" * 10, "b" * 10, "c" * 25, "d" * 5, "e" * 5]
    assert _batches([0, 1, 2, 3, 4], texts, batch_size=10, max_chars=20) == [[0, 1], [2], [3, 4]]
    assert _batches([0, 1, 2, 3, 4], texts, batch_size=2, max_chars=None) == [[0, 1], [2, 3], [4]]


def test_progress_quiet_hides_titles():
    import io

    from mebreco import cli

    p = cli.Progress(total=3, out=io.StringIO(), quiet=True)
    p.tick("book", "ชื่อลับ")
    assert "ชื่อลับ" not in p.line() and "[1/3]" in p.line()


def test_crawl_urls_sets_blocked_until(tmp_path, monkeypatch):
    import io
    import time as _t

    from mebreco import cli

    monkeypatch.setattr(cli, "crawl_one", lambda f, s, url, fresh=False: ("http_403", None))
    s = Store(tmp_path / "db.sqlite")
    p = cli.Progress(total=5, block_streak=2, max_cooldowns=0, out=io.StringIO())
    assert cli.crawl_urls(None, s, ["a", "b", "c"], p, deadline=None) == "blocked"
    assert float(s.get_state("blocked_until")) > _t.time() + 5 * 3600


def test_crawl_urls_stops_at_deadline(tmp_path, monkeypatch):
    import io

    from mebreco import cli

    calls = []
    monkeypatch.setattr(cli, "crawl_one", lambda f, s, url, fresh=False: calls.append(url) or ("book", "t"))
    s = Store(tmp_path / "db.sqlite")
    assert cli.crawl_urls(None, s, ["a"], cli.Progress(1, out=io.StringIO()), deadline=1.0) == "deadline"
    assert calls == []
    assert cli.crawl_urls(None, s, ["a"], cli.Progress(1, out=io.StringIO()), deadline=None) == "done"
    assert calls == ["a"]


def test_crawl_source_reps_uses_rep_ids(tmp_path, monkeypatch):
    from mebreco import cli
    from mebreco.listings import Listing

    s = Store(tmp_path / "db.sqlite")
    s.upsert_listings([Listing(i, "t", "a", None, 148, None, None, False) for i in ("5", "6")])
    s.set_reps({"5": ("k", True), "6": ("k", False)})
    s.commit()
    got = []
    monkeypatch.setattr(cli, "crawl_one", lambda f, st, url, fresh=False: got.append(url) or ("book", None))
    cli.main(["--db", str(tmp_path / "db.sqlite"), "crawl", "--source", "reps", "--quiet", "--delay", "0"])
    assert got == [cli.BOOK_URL.format("5")]


def test_budget_counts_per_utc_day(tmp_path):
    from mebreco.embed import Budget

    clock = [1_793_500_000.0]  # some instant; next UTC day = +86400
    s = Store(tmp_path / "db.sqlite")
    b = Budget(s, per_day=100, now=lambda: clock[0])
    assert Budget.neurons(40_000) == 69  # 40k chars * 1.6 tok/char * 1075 neurons/M tok = 68.8 -> 69
    assert b.allow(40_000) is True
    assert b.allow(40_000) is False  # 138 > 100
    clock[0] += 86_400
    assert b.allow(40_000) is True
    b.exhausted()
    assert b.allow(1) is False


def _embed_client(calls, status=200):
    def handler(req):
        texts = json.loads(req.content)["texts"]
        calls.append(len(texts))
        if status != 200:
            return httpx.Response(status, text="Too many requests")
        return httpx.Response(200, json={"vectors": [[float(len(t)), 1.0] for t in texts]})

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_build_cached_partial_on_budget(tmp_path):
    from mebreco.embed import Budget, book_text, build_cloudflare

    books = _catalog()
    first4 = sum(len(book_text(b, 1, 1, 1)) for b in books[:4])
    clock = [1_793_500_000.0]
    s = Store(tmp_path / "db.sqlite")
    budget = Budget(s, per_day=Budget.neurons(first4), now=lambda: clock[0])
    calls = []
    idx = build_cloudflare(books, "https://w/embed", "t", store=s, batch_size=4, client=_embed_client(calls), budget=budget)
    assert calls == [4] and len(idx.urls) == 4  # second batch over budget: index has embedded books only
    clock[0] += 86_400
    idx = build_cloudflare(books, "https://w/embed", "t", store=s, batch_size=4, client=_embed_client(calls), budget=budget)
    assert calls == [4, 2] and len(idx.urls) == 6


def test_build_cloudflare_stops_on_429(tmp_path, monkeypatch):
    from mebreco import embed

    monkeypatch.setattr(embed.time, "sleep", lambda s: None)
    s = Store(tmp_path / "db.sqlite")
    budget = embed.Budget(s, per_day=10_000)
    calls = []
    idx = embed.build_cloudflare(_catalog(), "https://w/embed", "t", store=s, batch_size=4,
                                 client=_embed_client(calls, status=429), budget=budget)
    assert len(idx.urls) == 0 and budget.allow(1) is False  # today marked spent; no crash


def _fake_stages(log, listing_results, crawl_result="done"):
    results = iter(listing_results)
    return {
        "pull": lambda a: log.append("pull"),
        "listings": lambda a, deadline: log.append("listings") or next(results),
        "group": lambda a: log.append("group"),
        "crawl": lambda a, deadline: log.append("crawl") or crawl_result,
        "embed": lambda a: log.append("embed"),
        "push": lambda a: log.append("push"),
    }


def test_pipeline_order_and_resume(tmp_path):
    import argparse

    from mebreco.pipeline import run_pipeline

    a = argparse.Namespace(db=str(tmp_path / "db.sqlite"), time_budget_min=330)
    log = []
    stages = _fake_stages(log, ["deadline", "done"])
    assert run_pipeline(a, stages) == "deadline"
    assert log == ["pull", "listings", "push"]  # out of time: skip ahead to saving
    log.clear()
    assert run_pipeline(a, stages) == "done"
    assert log == ["pull", "listings", "group", "crawl", "embed", "push"]


def test_pipeline_embeds_and_pushes_after_block(tmp_path):
    import argparse

    from mebreco.pipeline import run_pipeline

    a = argparse.Namespace(db=str(tmp_path / "db.sqlite"), time_budget_min=330)
    log = []
    assert run_pipeline(a, _fake_stages(log, ["done"], crawl_result="blocked")) == "blocked"
    assert log == ["pull", "listings", "group", "crawl", "embed", "push"]  # embedding doesn't touch MEB


def test_pipeline_holds_off_while_blocked(tmp_path):
    import argparse
    import time as _t

    from mebreco.pipeline import run_pipeline

    s = Store(tmp_path / "db.sqlite")
    s.set_state("blocked_until", str(_t.time() + 3600))
    s.commit()
    a = argparse.Namespace(db=str(tmp_path / "db.sqlite"), time_budget_min=330)
    log = []
    assert run_pipeline(a, _fake_stages(log, ["done"])) == "held-off"
    assert log == ["pull"]  # state comes from R2, so pull first, then stop without pushing


def test_pipeline_pushes_even_if_a_stage_crashes(tmp_path):
    import argparse

    from mebreco.pipeline import run_pipeline

    a = argparse.Namespace(db=str(tmp_path / "db.sqlite"), time_budget_min=330)
    log = []
    stages = _fake_stages(log, ["done"])
    stages["crawl"] = lambda a, deadline: (_ for _ in ()).throw(RuntimeError("boom"))
    with pytest.raises(RuntimeError):
        run_pipeline(a, stages)
    assert log[-1] == "push"


def test_index_roundtrip_dense_and_sparse(tmp_path):
    import numpy as np
    from scipy import sparse

    from mebreco.embed import Index

    sp = build_tfidf(_catalog())
    dense = Index(["a", "ข"], np.eye(2, dtype=np.float32), "cloudflare")
    for idx in (sp, dense):
        idx.save(tmp_path / "i.npz")
        back = Index.load(tmp_path / "i.npz")
        assert back.urls == idx.urls and back.backend == idx.backend
        a = back.matrix.toarray() if sparse.issparse(back.matrix) else back.matrix
        b = idx.matrix.toarray() if sparse.issparse(idx.matrix) else idx.matrix
        assert np.allclose(a, b)


class _Boom:
    ran = False

    def __reduce__(self):
        return (setattr, (_Boom, "ran", True))


def test_index_load_refuses_pickles(tmp_path):
    import pickle

    from mebreco.embed import Index

    (tmp_path / "evil.npz").write_bytes(pickle.dumps(_Boom()))
    with pytest.raises(ValueError):
        Index.load(tmp_path / "evil.npz")
    assert _Boom.ran is False
