"""MEB category listing pages: every book in a category, ~40 per request.

Listing pages give enough to filter and group books (title, author,
category, SET bundle or not) before deciding which detail pages to fetch.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Listing:
    book_id: str
    title: str
    author: str | None
    publisher: str | None
    category_id: int
    category_name: str | None
    rating_count: int | None
    is_set: bool
