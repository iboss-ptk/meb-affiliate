"""Parser for MEB book pages (www.mebmarket.com/index.php?action=BookDetails&book_id=N).

Selectors were checked against real pages on 2026-11-01. Run `mebreco probe`
on a known book if results suddenly come back empty: the layout may have changed.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit

from bs4 import BeautifulSoup

from .extract import Book, _float, _meta


def is_meb(url: str) -> bool:
    return urlsplit(url).netloc.endswith("mebmarket.com")


def _text(el) -> str | None:
    if el is None:
        return None
    t = el.get_text("\n", strip=True)
    return t or None


def _query(href: str, key: str) -> str | None:
    return (parse_qs(urlsplit(href).query).get(key) or [None])[0]


def _dedupe(xs):
    return list(dict.fromkeys(x for x in xs if x))


_GROUP = re.compile(r"::\s*e-book\s+(.+?)(?:\s+โดย\s|$)")


def _group(soup: BeautifulSoup) -> str | None:
    """og:title looks like 'One Piece วันพีซ เล่ม 25:: e-book มังงะ โดย EIICHIRO ODA'."""
    m = _GROUP.search((_meta(soup, "og:title") or [""])[0])
    return m.group(1).strip() if m else None


def parse_meb(url: str, soup: BeautifulSoup) -> Book | None:
    box = soup.find(id="book_details_lg") or soup.find(id="book_details")
    if box is None:  # missing/removed book: MEB shows the homepage instead
        return None
    title = _text(box.find(class_="book_name")) or (_meta(soup, "og:title") or [""])[0].split("::")[0].strip()
    if not title:
        return None

    links = box.find_all("a", href=True)
    # MEB separates co-authors with " /", which ends up on the link text.
    authors = _dedupe(
        a.get_text(strip=True).rstrip("/ ").strip() for a in links if "type=author_name" in a["href"]
    )
    publishers = _dedupe(a.get_text(strip=True) for a in links if "publisher_id=" in a["href"])
    categories = _dedupe(a.get_text(strip=True) for a in links if "category_id=" in a["href"])
    series = _dedupe(a.get_text(strip=True) for a in links if "action=series_detail" in a["href"])

    tag_box = soup.find(id="wrap_genre_tag")
    tags = _dedupe(a.get_text(strip=True) for a in tag_box.find_all("a", class_="genre_tag")) if tag_box else []

    rating = _float(_text(box.find(class_="rating_text")))
    count_txt = _text(box.find(class_="total_rating_text")) or ""
    m = re.search(r"[\d,]+", count_txt)
    rating_count = int(m.group().replace(",", "")) if m else None

    img = soup.find(id="image_book_detail_large")
    image = img.get("src") if img else None

    return Book(
        url=url,
        title=title,
        authors=authors,
        publisher=publishers[0] if publishers else None,
        categories=categories,
        description=_text(soup.find(id="description_listing")),
        price=_float((_meta(soup, "product:price:amount") or [None])[0]),
        currency=(_meta(soup, "product:price:currency") or [None])[0],
        image=image or (_meta(soup, "og:image") or [None])[0],
        source="meb",
        book_id=_query(url, "book_id") or (_meta(soup, "product:retailer_item_id") or [None])[0],
        series=series[0] if series else None,
        tags=tags,
        rating=rating,
        rating_count=rating_count,
        group=_group(soup),
    )
