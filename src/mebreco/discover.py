"""Find candidate book URLs from the sitemaps that robots.txt points to.

We don't hard-code MEB's URL layout (we haven't been able to inspect it yet).
Instead we collect every URL from the sitemaps and let `extract` decide which
pages are books. Pass `--pattern` once the real layout is known to save requests.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Iterator

from defusedxml import DefusedXmlException
from defusedxml.ElementTree import fromstring as safe_fromstring

from .fetch import PoliteFetcher

NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"


def parse_sitemap(xml_text: str) -> tuple[list[str], list[str]]:
    """Return (child_sitemaps, page_urls) from a sitemap or sitemap index.

    Raises ET.ParseError on malformed XML or on DTD/entity tricks (sitemaps
    are untrusted remote input, so we parse with defusedxml).
    """
    try:
        root = safe_fromstring(xml_text.strip().encode())
    except DefusedXmlException as e:
        raise ET.ParseError(f"unsafe XML rejected: {e}") from e
    locs = [el.text.strip() for el in root.iter(f"{NS}loc") if el.text]
    if root.tag == f"{NS}sitemapindex":
        return locs, []
    return [], locs


def iter_urls(
    fetcher: PoliteFetcher,
    site: str,
    pattern: str | None = None,
    extra_sitemaps: list[str] | None = None,
) -> Iterator[str]:
    queue = list(extra_sitemaps or []) or fetcher.sitemaps(site)
    if not queue:
        queue = [site.rstrip("/") + "/sitemap.xml"]
    rx = re.compile(pattern) if pattern else None
    seen_maps: set[str] = set()
    seen_urls: set[str] = set()
    while queue:
        sm = queue.pop(0)
        if sm in seen_maps:
            continue
        seen_maps.add(sm)
        resp = fetcher.get_fresh(sm)
        if resp.status != 200:
            continue
        try:
            children, urls = parse_sitemap(resp.text)
        except ET.ParseError:
            continue
        queue.extend(children)
        for u in urls:
            if u not in seen_urls and (rx is None or rx.search(u)):
                seen_urls.add(u)
                yield u


BOOK_ID_RX = re.compile(r"[?&]book_id=(\d+)")


def book_id_of(url: str) -> int | None:
    m = BOOK_ID_RX.search(url)
    return int(m.group(1)) if m else None


def _sitemap_number(url: str) -> int:
    m = re.search(r"(\d+)\.xml", url)
    return int(m.group(1)) if m else 0


def new_book_urls(fetcher: PoliteFetcher, site: str, above_id: int) -> tuple[list[str], int]:
    """Book URLs with book_id > above_id, plus the highest book_id in the
    sitemaps that were read (0 if none).

    MEB numbers its book sitemaps in ID order (bookdetail_sitemap1 = oldest),
    so we read them newest-first and stop at the first one that reaches back
    to `above_id`. Usually that's just the last one or two files, and an
    unchanged sitemap costs a 304 rather than a re-download.
    """
    maps = [m for m in fetcher.sitemaps(site) if "bookdetail_sitemap" in m]
    maps.sort(key=_sitemap_number)
    found: list[tuple[int, str]] = []
    max_id = 0
    for sm in reversed(maps):
        resp = fetcher.get_fresh(sm)
        if resp.status != 200:
            continue
        try:
            _, urls = parse_sitemap(resp.text)
        except ET.ParseError:
            continue
        ids = [(book_id_of(u), u) for u in urls]
        ids = [(i, u) for i, u in ids if i is not None]
        if not ids:
            continue
        max_id = max(max_id, max(i for i, _ in ids))
        found.extend((i, u) for i, u in ids if i > above_id)
        if min(i for i, _ in ids) <= above_id:
            break  # older sitemaps can't contain anything newer
    found.sort()
    return [u for _, u in found], max_id
