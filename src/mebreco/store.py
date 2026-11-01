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
CREATE TABLE IF NOT EXISTS visited (
  url TEXT PRIMARY KEY,
  status TEXT NOT NULL,   -- book | not_book | http_<code> | disallowed | error
  visited_at REAL NOT NULL
);
"""

LIST_FIELDS = ("authors", "categories", "tags")


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
        return self.db.execute("SELECT 1 FROM visited WHERE url=?", (url,)).fetchone() is not None

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
        return s
