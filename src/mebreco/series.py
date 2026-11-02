"""Group listing cards into series and pick one book to fetch per series.

MEB listing cards carry no series ID, but the volume is almost always in the
title: "เล่ม 3", "Vol.22", "ภาค 2", or a bare trailing number ("เกาะอลวน
รักอลเวง 16"). The series key is the title before that marker plus the
author, and the representative is the lowest volume. Heuristic by design:
the detail page's real series name is stored once the representative is
fetched, so grouping can be audited later.
"""

from __future__ import annotations

import re
from collections import defaultdict

# Explicit volume marker; the first one wins ("PATH A WAY Vol.22 (… เล่ม 22)").
MARKER = re.compile(r"\s*(?:เล่มที่|เล่ม|(?<![A-Za-z])vol\.?|ภาค)\s*0*(\d{1,4})(?!\d)", re.I)
TRAILING_GROUP = re.compile(r"\s*(\([^()]*\)|\[[^\[\]]*\])\s*$")  # "(ฉบับการ์ตูน)", "[e-book only]"
TRAILING_DASH = re.compile(r"\s+-\s+\D*$")  # "12 - Temple"
BARE_NUMBER = re.compile(r"^(.*\S)\s+0*(\d{1,4})$")
PERIODICAL = re.compile(r"ฉบับ(?:ที่)?\s*\d")


def volume_of(title: str) -> tuple[str, int | None]:
    """(title without its volume marker, volume number or None)."""
    m = MARKER.search(title)
    if m:
        return title[: m.start()].strip(), int(m.group(1))
    t = title
    while (stripped := TRAILING_GROUP.sub("", t)) != t:
        t = stripped
    t = TRAILING_DASH.sub("", t)
    m = BARE_NUMBER.match(t)
    if m and not m.group(1).endswith("ยุค"):  # "ย้อนกลับไปยุค 70" is an era, not volume 70
        return m.group(1).strip(), int(m.group(2))
    return title, None


def is_periodical(title: str) -> bool:
    return bool(PERIODICAL.search(title))


def _normalise(base: str) -> str:
    return re.sub(r"\s+", " ", base).strip(" -:~").casefold()


def assign_reps(rows) -> dict[str, tuple[str, bool]]:
    """book_id -> (series_key, is_representative). SET bundles and periodical
    issues are left out entirely."""
    groups: dict[str, list[tuple[int, int, str]]] = defaultdict(list)
    for r in rows:
        if r.is_set or is_periodical(r.title):
            continue
        base, vol = volume_of(r.title)
        key = f"{_normalise(base)}|{r.author or ''}"
        groups[key].append((vol or 1, int(r.book_id), r.book_id))
    out: dict[str, tuple[str, bool]] = {}
    for key, members in groups.items():
        rep = min(members)[2]
        for _, _, bid in members:
            out[bid] = (key, bid == rep)
    return out


def group_report(assignments: dict[str, tuple[str, bool]], rows) -> dict[str, int]:
    sets = sum(1 for r in rows if r.is_set)
    return {
        "listings": len(rows),
        "skipped_sets": sets,
        "skipped_periodicals": sum(1 for r in rows if not r.is_set and is_periodical(r.title)),
        "series": len({k for k, _ in assignments.values()}),
        "reps": sum(1 for _, rep in assignments.values() if rep),
    }
