"""'More like this' recommendations, plus the affiliate-link hook.

Discovery note: pure nearest-neighbour results tend to be the same author or
the next volume of the same series, which the user already knows about. That
is accurate but doesn't help them discover anything. `max_per_author` caps
how many results one author can take up.
"""

from __future__ import annotations

import os
import re
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


MEB_PUBLIC = "https://www.mebmarket.com/ebook-{id}-{slug}"


def public_url(book: Book) -> str:
    """The link to send visitors to.

    We crawl `index.php?action=BookDetails&book_id=N`, but MEB answers 403 to
    browsers on that URL. Its own pages link `/ebook-<id>-<title>`; the part
    after the ID is decorative (any text works), so we use the title.
    """
    if not book.book_id:
        return book.url
    slug = re.sub(r"[\s/?#%&]+", "-", book.title).strip("-") or "book"
    return MEB_PUBLIC.format(id=book.book_id, slug=quote(slug, safe="-"))


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
        out.append(Rec(b, float(sims[i]), affiliate_link(public_url(b))))
        if len(out) >= k:
            break
    return out


@dataclass
class ProfileRec(Rec):
    because: Book | None = None  # the liked book that contributed most


def _sims_many(index: Index, rows: list[int]) -> np.ndarray:
    """(len(rows), n_books) cosine similarities."""
    from scipy import sparse

    m = index.matrix[rows] @ index.matrix.T
    if sparse.issparse(m):
        m = m.toarray()
    return np.asarray(m)


def recommend_for(
    index: Index,
    books_by_url: dict[str, Book],
    liked: list[str],
    disliked: list[str] = (),
    k: int = 20,
    max_per_author: int | None = 2,
    dislike_weight: float = 0.5,
) -> list[ProfileRec]:
    """Recommendations for a visitor who liked/disliked some books (by URL).

    Score = sum of similarity to each liked book − dislike_weight × sum of
    similarity to each disliked book. Each result remembers which liked book
    pulled it in, for a "because you liked X" line. URLs not in the index are
    ignored.
    """
    pos = {u: index.position(u) for u in [*liked, *disliked] if u in index._pos}
    like_rows = [pos[u] for u in liked if u in pos]
    if not like_rows:
        return []
    like_sims = _sims_many(index, like_rows)
    score = like_sims.sum(axis=0)
    dis_rows = [pos[u] for u in disliked if u in pos]
    if dis_rows:
        score = score - dislike_weight * _sims_many(index, dis_rows).sum(axis=0)
    best_like = like_sims.argmax(axis=0)

    exclude = set(pos)
    per_author: dict[str, int] = {}
    out: list[ProfileRec] = []
    for i in np.argsort(-score):
        url = index.urls[i]
        if url in exclude or url not in books_by_url:
            continue
        b = books_by_url[url]
        if max_per_author is not None and b.authors:
            key = b.authors[0]
            if per_author.get(key, 0) >= max_per_author:
                continue
            per_author[key] = per_author.get(key, 0) + 1
        because = books_by_url.get(index.urls[like_rows[best_like[i]]])
        out.append(ProfileRec(b, float(score[i]), affiliate_link(public_url(b)), because))
        if len(out) >= k:
            break
    return out
