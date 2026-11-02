"""SQLite storage for the catalog. One row per book URL; re-crawls upsert."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from .extract import Book

SCHEMA = """
CREATE TABLE IF NOT EXISTS books (
  url TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  authors TEXT NOT NULL DEFAULT '[]',
  publisher TEXT,
  categories TEXT NOT NULL DEFAULT '[]',
  description TEXT,
  isbn TEXT,
  price REAL,
  currency TEXT,
  image TEXT,
  language TEXT,
  source TEXT,
  book_id TEXT,
  series TEXT,
  tags TEXT NOT NULL DEFAULT '[]',
  rating REAL,
  rating_count INTEGER,
  "group" TEXT,
  fetched_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS seeds (
  book_id TEXT NOT NULL,
  list TEXT NOT NULL,      -- MEB ranking list, e.g. topmonth, bestseller
  rank INTEGER NOT NULL,   -- 1 = top of that list
  seen_at REAL NOT NULL,
  PRIMARY KEY (book_id, list)
);
CREATE TABLE IF NOT EXISTS state (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS emb_cache (
  model TEXT NOT NULL,
  text_hash TEXT NOT NULL,  -- sha1 of the exact text that was embedded
  vec BLOB NOT NULL,        -- float32
  PRIMARY KEY (model, text_hash)
);
CREATE TABLE IF NOT EXISTS listings (
  book_id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  author TEXT,
  publisher TEXT,
  category_id INTEGER NOT NULL,   -- category where we first saw it
  category_name TEXT,
  rating_count INTEGER,
  is_set INTEGER NOT NULL,        -- MEB "SET" bundle of several volumes
  series_key TEXT,                -- filled by series grouping
  is_rep INTEGER,                 -- 1 = the volume we fetch for its series
  seen_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS visited (
  url TEXT PRIMARY KEY,
  status TEXT NOT NULL,   -- book | not_book | http_<code> | disallowed | error
  visited_at REAL NOT NULL
);
"""

LIST_FIELDS = ("authors", "categories", "tags")


def is_retryable(status: str) -> bool:
    return status in ("error", "http_403", "http_429") or status.startswith("http_5")


class Store:
    def __init__(self, path: Path | str = "data/books.sqlite") -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def upsert(self, book: Book) -> None:
        d = book.to_dict()
        for f in LIST_FIELDS:
            d[f] = json.dumps(d[f], ensure_ascii=False)
        d["fetched_at"] = time.time()
        cols = ", ".join(f'"{k}"' for k in d)
        params = ", ".join(f":{k}" for k in d)
        updates = ", ".join(f'"{k}"=excluded."{k}"' for k in d if k != "url")
        self.db.execute(
            f"INSERT INTO books ({cols}) VALUES ({params}) ON CONFLICT(url) DO UPDATE SET {updates}", d
        )

    def mark(self, url: str, status: str) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO visited (url, status, visited_at) VALUES (?, ?, ?)",
            (url, status, time.time()),
        )

    def visited(self, url: str) -> bool:
        """True if there's no point fetching `url` again in a normal crawl.
        Blocks (403/429), server errors and network errors are retried."""
        row = self.db.execute("SELECT status FROM visited WHERE url=?", (url,)).fetchone()
        return row is not None and not is_retryable(row[0])

    def save_seeds(self, list_name: str, book_ids: list[str]) -> None:
        now = time.time()
        self.db.execute("DELETE FROM seeds WHERE list=?", (list_name,))
        self.db.executemany(
            "INSERT INTO seeds (book_id, list, rank, seen_at) VALUES (?, ?, ?, ?)",
            [(b, list_name, i + 1, now) for i, b in enumerate(book_ids)],
        )

    def seed_ids(self) -> list[str]:
        """Seed book IDs, most popular first: best rank on any list, then
        how many lists the book appears on."""
        rows = self.db.execute(
            "SELECT book_id FROM seeds GROUP BY book_id ORDER BY MIN(rank), COUNT(*) DESC, book_id"
        )
        return [r[0] for r in rows]

    def seed_ranks(self) -> dict[str, int]:
        """book_id -> best rank on any MEB ranking list."""
        return {r[0]: r[1] for r in self.db.execute("SELECT book_id, MIN(rank) FROM seeds GROUP BY book_id")}

    def upsert_listings(self, rows) -> None:
        """Insert listing cards; a book seen again (e.g. in another category)
        keeps its first row, only refreshing title/rating/seen_at."""
        now = time.time()
        self.db.executemany(
            """
            INSERT INTO listings (book_id, title, author, publisher, category_id, category_name,
                                  rating_count, is_set, seen_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(book_id) DO UPDATE SET
              title=excluded.title, rating_count=excluded.rating_count, seen_at=excluded.seen_at
            """,
            [(r.book_id, r.title, r.author, r.publisher, r.category_id, r.category_name,
              r.rating_count, int(r.is_set), now) for r in rows],
        )

    def listings(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM listings ORDER BY CAST(book_id AS INTEGER)").fetchall()

    def set_reps(self, assignments: dict[str, tuple[str, bool]]) -> None:
        self.db.executemany(
            "UPDATE listings SET series_key=?, is_rep=? WHERE book_id=?",
            [(key, int(rep), bid) for bid, (key, rep) in assignments.items()],
        )

    def rep_ids(self) -> list[str]:
        """Books to fetch: one per series plus standalones, newest first."""
        rows = self.db.execute("SELECT book_id FROM listings WHERE is_rep=1 ORDER BY CAST(book_id AS INTEGER) DESC")
        return [r[0] for r in rows]

    def get_state(self, key: str, default: str | None = None) -> str | None:
        row = self.db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set_state(self, key: str, value: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO state (key, value) VALUES (?, ?)", (key, value))

    def delete_book(self, url: str) -> None:
        self.db.execute("DELETE FROM books WHERE url=?", (url,))

    def stale_urls(self, popular_before: float, other_before: float, limit: int) -> list[str]:
        """Books due a refresh: popular ones (on any MEB ranking list) go
        stale sooner, and are refreshed first; then the longest-unrefreshed."""
        rows = self.db.execute(
            """
            SELECT b.url FROM books b
            LEFT JOIN (SELECT book_id, MIN(rank) AS r FROM seeds GROUP BY book_id) s
              ON s.book_id = b.book_id
            WHERE (s.r IS NOT NULL AND b.fetched_at < ?) OR b.fetched_at < ?
            ORDER BY s.r IS NULL, s.r, b.fetched_at
            LIMIT ?
            """,
            (popular_before, other_before, limit),
        )
        return [r[0] for r in rows]

    def cached_vectors(self, model: str, hashes: list[str]) -> dict[str, bytes]:
        out: dict[str, bytes] = {}
        for i in range(0, len(hashes), 500):
            chunk = hashes[i : i + 500]
            q = ",".join("?" * len(chunk))
            for h, v in self.db.execute(
                f"SELECT text_hash, vec FROM emb_cache WHERE model=? AND text_hash IN ({q})", [model, *chunk]
            ):
                out[h] = v
        return out

    def cache_vectors(self, model: str, items: list[tuple[str, bytes]]) -> None:
        self.db.executemany(
            "INSERT OR REPLACE INTO emb_cache (model, text_hash, vec) VALUES (?, ?, ?)",
            [(model, h, v) for h, v in items],
        )
        self.db.commit()

    def commit(self) -> None:
        self.db.commit()

    def books(self) -> list[Book]:
        out = []
        for r in self.db.execute("SELECT * FROM books ORDER BY url"):
            d = dict(r)
            d.pop("fetched_at")
            for f in LIST_FIELDS:
                d[f] = json.loads(d[f])
            out.append(Book(**d))
        return out

    def stats(self) -> dict[str, int]:
        rows = self.db.execute("SELECT status, COUNT(*) FROM visited GROUP BY status").fetchall()
        s = {k: v for k, v in rows}
        s["books"] = self.db.execute("SELECT COUNT(*) FROM books").fetchone()[0]
        seeds, done = self.db.execute(
            """
            SELECT COUNT(*), COUNT(b.url) FROM (SELECT DISTINCT book_id FROM seeds) sd
            LEFT JOIN books b ON b.book_id = sd.book_id
            """
        ).fetchone()
        s["seed_books"] = seeds
        s["seed_books_crawled"] = done
        return s
