"""Find candidate book URLs from the sitemaps that robots.txt points to.

We don't hard-code MEB's URL layout (we haven't been able to inspect it yet).
Instead we collect every URL from the sitemaps and let `extract` decide which
pages are books. Pass `--pattern` once the real layout is known to save requests.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Iterator

from .fetch import PoliteFetcher

NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"


def parse_sitemap(xml_text: str) -> tuple[list[str], list[str]]:
    """Return (child_sitemaps, page_urls) from a sitemap or sitemap index."""
    root = ET.fromstring(xml_text.strip().encode())
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
        resp = fetcher.get(sm)
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
