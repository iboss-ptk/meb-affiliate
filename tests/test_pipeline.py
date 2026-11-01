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


def test_affiliate_link_template():
    assert affiliate_link("https://x/a?b=1") == "https://x/a?b=1"
    assert affiliate_link("https://x/a", "https://aff/?u={url_enc}") == "https://aff/?u=https%3A%2F%2Fx%2Fa"
