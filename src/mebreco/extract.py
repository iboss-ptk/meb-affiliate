"""Extract book metadata from a product page.

Order of preference:
1. schema.org JSON-LD (`Book` or `Product`), which is meant for machines and so
   doesn't change when the site's visual layout changes.
2. OpenGraph / meta tags as a fallback.

MEB pages are handled by `mebreco.meb.parse_meb` first: MEB has OpenGraph
tags, but its og:description only repeats the title, and the real description,
genre tags, series, and rating are only in the HTML.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

from bs4 import BeautifulSoup

BOOK_TYPES = {"Book", "Product", "EBook"}


@dataclass
class Book:
    url: str
    title: str
    authors: list[str] = field(default_factory=list)
    publisher: str | None = None
    categories: list[str] = field(default_factory=list)
    description: str | None = None
    isbn: str | None = None
    price: float | None = None
    currency: str | None = None
    image: str | None = None
    language: str | None = None
    source: str = "jsonld"
    book_id: str | None = None
    series: str | None = None
    tags: list[str] = field(default_factory=list)
    rating: float | None = None
    rating_count: int | None = None
    group: str | None = None  # MEB's broad type, e.g. มังงะ, นิยาย, หนังสือพิมพ์

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _as_list(v: Any) -> list[Any]:
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def _name(v: Any) -> str | None:
    if isinstance(v, dict):
        v = v.get("name")
    if isinstance(v, str) and v.strip():
        return v.strip()
    return None


def _walk(node: Any):
    """Yield every dict in a JSON-LD blob, including @graph members and nesting."""
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def _types(d: dict) -> set[str]:
    return {t for t in _as_list(d.get("@type")) if isinstance(t, str)}


def _float(v: Any) -> float | None:
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _image(v: Any) -> str | None:
    first = next(iter(_as_list(v)), None)
    if isinstance(first, dict):
        first = first.get("url") or first.get("contentUrl")
    return first.strip() if isinstance(first, str) and first.strip() else None


def _from_jsonld(url: str, soup: BeautifulSoup) -> Book | None:
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        for d in _walk(data):
            if not (_types(d) & BOOK_TYPES):
                continue
            title = _name(d.get("name"))
            if not title:
                continue
            offers = next(iter(_as_list(d.get("offers"))), {}) or {}
            cats = [c for c in (_name(x) for x in _as_list(d.get("genre")) + _as_list(d.get("category"))) if c]
            return Book(
                url=url,
                title=title,
                authors=[a for a in (_name(x) for x in _as_list(d.get("author"))) if a],
                publisher=_name(d.get("publisher")) or _name(d.get("brand")),
                categories=cats,
                description=(d.get("description") or "").strip() or None,
                isbn=d.get("isbn") or d.get("gtin13") or d.get("sku"),
                price=_float(offers.get("price")) if isinstance(offers, dict) else None,
                currency=offers.get("priceCurrency") if isinstance(offers, dict) else None,
                image=_image(d.get("image")),
                language=_name(d.get("inLanguage")),
                source="jsonld",
            )
    return None


def _meta(soup: BeautifulSoup, *keys: str) -> list[str]:
    out = []
    for k in keys:
        for tag in soup.find_all("meta", attrs={"property": k}) + soup.find_all("meta", attrs={"name": k}):
            c = (tag.get("content") or "").strip()
            if c:
                out.append(c)
    return out


def _from_meta(url: str, soup: BeautifulSoup) -> Book | None:
    og_type = (_meta(soup, "og:type") or [""])[0].lower()
    # Only treat the page as a book when it says so; a generic page with an
    # og:title would otherwise pollute the catalog.
    if og_type not in {"book", "books.book", "product", "og:product"}:
        return None
    title = (_meta(soup, "og:title") or [None])[0]
    if not title:
        return None
    return Book(
        url=url,
        title=title,
        authors=_meta(soup, "book:author", "books:author", "author"),
        categories=_meta(soup, "book:tag", "article:tag"),
        description=(_meta(soup, "og:description", "description") or [None])[0],
        isbn=(_meta(soup, "book:isbn", "books:isbn") or [None])[0],
        price=_float((_meta(soup, "product:price:amount", "og:price:amount") or [None])[0]),
        currency=(_meta(soup, "product:price:currency", "og:price:currency") or [None])[0],
        image=(_meta(soup, "og:image") or [None])[0],
        source="meta",
    )


def extract_book(url: str, html: str) -> Book | None:
    from .meb import is_meb, parse_meb

    soup = BeautifulSoup(html, "html.parser")
    if is_meb(url):
        # MEB serves its homepage with HTTP 200 for missing IDs, so the
        # generic fallbacks must not run there: they'd store junk.
        return parse_meb(url, soup)
    return _from_jsonld(url, soup) or _from_meta(url, soup)
