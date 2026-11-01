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
