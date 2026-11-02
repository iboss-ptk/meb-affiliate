"""Prototype web app: cold-start onboarding + "because you liked X" results.

Flow: pick genres -> tap popular books you've liked -> recommendations from
the content index, refined with 👍/👎. No accounts; the page keeps the
visitor's picks in localStorage and sends them with each request.

Run: mebreco serve   (needs: pip install '.[web]', plus `mebreco embed`)
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .embed import Index
from .extract import Book
from .recommend import affiliate_link, public_url, recommend_for
from .store import Store

STATIC = Path(__file__).parent / "static"


class Catalog:
    """Books that are both crawled and in the index, plus popularity ranks."""

    def __init__(self, db: str | Path, index_path: str | Path) -> None:
        self.index = Index.load(index_path)
        indexed = set(self.index.urls)
        self.books = [b for b in Store(db).books() if b.url in indexed and b.book_id]
        self.by_url = {b.url: b for b in self.books}
        self.by_id = {b.book_id: b for b in self.books}
        ranks = Store(db).seed_ranks()
        # Onboarding shows only books on MEB's ranking lists: the ones a
        # visitor is most likely to recognise.
        self.popular = sorted(
            (b for b in self.books if b.book_id in ranks), key=lambda b: (ranks[b.book_id], b.book_id)
        )


def card(b: Book) -> dict:
    return {
        "id": b.book_id,
        "title": b.title,
        "authors": b.authors,
        "image": b.image,
        "group": b.group,
        "categories": b.categories,
        "rating": b.rating,
        "rating_count": b.rating_count,
        "price": b.price,
        "url": affiliate_link(public_url(b)),
    }


class RecRequest(BaseModel):
    liked: list[str] = Field(default_factory=list, max_length=200)
    disliked: list[str] = Field(default_factory=list, max_length=200)
    k: int = Field(default=24, ge=1, le=100)


def create_app(db: str | Path = "data/books.sqlite", index_path: str | Path = "data/index.pkl") -> FastAPI:
    cat = Catalog(db, index_path)
    app = FastAPI(title="mebreco")

    @app.get("/")
    def home() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @app.get("/api/genres")
    def genres(min_books: int = 3) -> list[dict]:
        """Groups (นิยาย, มังงะ, ...) with their common categories, from popular books."""
        groups: dict[str, Counter] = {}
        sizes: Counter = Counter()
        for b in cat.popular:
            g = b.group or "อื่น ๆ"
            sizes[g] += 1
            groups.setdefault(g, Counter()).update(b.categories)
        return [
            {
                "group": g,
                "count": n,
                "categories": [{"name": c, "count": k} for c, k in groups[g].most_common() if k >= min_books],
            }
            for g, n in sizes.most_common()
        ]

    @app.get("/api/onboard")
    def onboard(
        groups: list[str] = Query(default=[]),
        categories: list[str] = Query(default=[]),
        exclude: list[str] = Query(default=[]),
        limit: int = Query(default=24, ge=1, le=100),
    ) -> list[dict]:
        """Popular books to pick from, most popular first."""
        skip = set(exclude)
        out = []
        for b in cat.popular:
            if b.book_id in skip:
                continue
            if groups and (b.group or "อื่น ๆ") not in groups:
                continue
            if categories and not set(b.categories) & set(categories):
                continue
            out.append(card(b))
            if len(out) >= limit:
                break
        return out

    @app.post("/api/recommend")
    def recommend(req: RecRequest) -> list[dict]:
        liked = [cat.by_id[i].url for i in req.liked if i in cat.by_id]
        if not liked:
            raise HTTPException(400, "like at least one known book")
        disliked = [cat.by_id[i].url for i in req.disliked if i in cat.by_id]
        recs = recommend_for(cat.index, cat.by_url, liked, disliked, k=req.k)
        return [
            {**card(r.book), "score": round(r.score, 4),
             "because": {"id": r.because.book_id, "title": r.because.title} if r.because else None}
            for r in recs
        ]

    @app.get("/api/status")
    def status() -> dict:
        """Crawl progress, read live from the DB (the crawler commits per book)."""
        st = Store(db).stats()
        last = Store(db).db.execute("SELECT MAX(visited_at) FROM visited").fetchone()[0]
        return {
            "seed_books": st["seed_books"],
            "crawled": st["seed_books_crawled"],
            "in_recommendations": len(cat.books),
            "last_fetch": last,
        }

    @app.get("/api/books")
    def books(ids: list[str] = Query(default=[])) -> list[dict]:
        """Cards for saved picks (returning visitors)."""
        return [card(cat.by_id[i]) for i in ids if i in cat.by_id]

    return app
