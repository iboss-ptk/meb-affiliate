"""'More like this' recommendations, plus the affiliate-link hook.

Discovery note: pure nearest-neighbour results tend to be the same author or
the next volume of the same series, which the user already knows about. That
is accurate but doesn't help them discover anything. `max_per_author` caps
how many results one author can take up.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import quote

import numpy as np

from .embed import Index, similarities
from .extract import Book


@dataclass
class Rec:
    book: Book
    score: float
    link: str


def affiliate_link(url: str, template: str | None = None) -> str:
    """Wrap a product URL in an affiliate link.

    The format depends on whichever affiliate programme you join, so it isn't
    hard-coded. Set MEB_AFFILIATE_TEMPLATE, e.g.
    'https://partner.example/track?id=YOURID&url={url_enc}'.
    Supported placeholders: {url} and {url_enc} (URL-encoded).
    """
    template = template or os.environ.get("MEB_AFFILIATE_TEMPLATE")
    if not template:
        return url
    return template.format(url=url, url_enc=quote(url, safe=""))


def similar(
    index: Index,
    books_by_url: dict[str, Book],
    url: str,
    k: int = 10,
    max_per_author: int | None = 2,
) -> list[Rec]:
    row = index.position(url)
    sims = similarities(index, row)
    order = np.argsort(-sims)
    # The seed's own author already "used" one slot: the user is looking at it.
    per_author: dict[str, int] = {a: 1 for a in books_by_url[url].authors}
    out: list[Rec] = []
    for i in order:
        cand_url = index.urls[i]
        if cand_url == url:
            continue
        b = books_by_url[cand_url]
        if max_per_author is not None and b.authors:
            key = b.authors[0]
            if per_author.get(key, 0) >= max_per_author:
                continue
            per_author[key] = per_author.get(key, 0) + 1
        out.append(Rec(b, float(sims[i]), affiliate_link(cand_url)))
        if len(out) >= k:
            break
    return out
