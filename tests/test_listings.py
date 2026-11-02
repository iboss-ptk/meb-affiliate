from mebreco.listings import Listing
from mebreco.store import Store


def L(book_id, title="t", category_id=148, author="A", is_set=False):
    return Listing(book_id, title, author, "P", category_id, "cat", None, is_set)


def test_upsert_listings_dedupes_across_categories(tmp_path):
    s = Store(tmp_path / "db.sqlite")
    s.upsert_listings([L("1", category_id=148)])
    s.upsert_listings([L("1", category_id=4), L("2")])
    rows = {r["book_id"]: r for r in s.listings()}
    assert len(rows) == 2
    assert rows["1"]["category_id"] == 148  # first sighting kept


def test_set_reps_and_rep_ids(tmp_path):
    s = Store(tmp_path / "db.sqlite")
    s.upsert_listings([L("9"), L("10"), L("11")])
    s.set_reps({"9": ("k1", True), "10": ("k1", False), "11": ("k2", True)})
    assert s.rep_ids() == ["11", "9"]  # numeric order, newest first
    assert {r["book_id"]: r["series_key"] for r in s.listings()}["10"] == "k1"


def test_parse_card_shapes():
    from mebreco.listings import parse_card

    c = parse_card("479902", ["-10%", "SET Giant Killing เล่ม 1-62",
                              "Tsujitomo/Masaya Tsunamoto / Siam Inter Comics", "การ์ตูนทั่วไป", "No Rating"], 148)
    assert c.is_set and c.title == "Giant Killing เล่ม 1-62"
    assert (c.author, c.publisher) == ("Tsujitomo/Masaya Tsunamoto", "Siam Inter Comics")
    assert c.rating_count is None and c.category_name == "การ์ตูนทั่วไป"

    c = parse_card("479532", ["เกาะอลวน รักอลเวง 16", "Takeshi Fujishiro / Gift Book Publishing", "การ์ตูนทั่วไป", "1 Rating"], 148)
    assert (c.title, c.is_set, c.rating_count, c.category_id) == ("เกาะอลวน รักอลเวง 16", False, 1, 148)

    c = parse_card("1", ["Solo Title", "Only Author", "นิยายรัก", "1,204 Rating"], 21)
    assert (c.author, c.publisher, c.rating_count) == ("Only Author", None, 1204)

    assert parse_card("1", ["-10%"], 148) is None


def _fake_fetch(pages):
    calls = []

    def fetch(category_id, n):
        calls.append((category_id, n))
        return pages.get(n, [])

    return fetch, calls


CARD = lambda i: (str(i), [f"Book {i}", "A / P", "การ์ตูนทั่วไป", "No Rating"])


def test_crawl_categories_resumes_and_stops(tmp_path):
    from mebreco.listings import crawl_categories

    s = Store(tmp_path / "db.sqlite")
    fetch, calls = _fake_fetch({1: [CARD(1), CARD(2)], 2: [CARD(3)], 3: []})
    assert crawl_categories(s, [148], max_pages=10, delay=0, user_agent="ua", fetch=fetch) == {148: 3}
    assert s.get_state("listing:148:done") == "1" and len(s.listings()) == 3
    calls.clear()
    assert crawl_categories(s, [148], max_pages=10, delay=0, user_agent="ua", fetch=fetch) == {148: 0}
    assert calls == []  # done categories aren't fetched again


def test_crawl_categories_max_pages_resumes_later(tmp_path):
    from mebreco.listings import crawl_categories

    s = Store(tmp_path / "db.sqlite")
    fetch, calls = _fake_fetch({1: [CARD(1)], 2: [CARD(2)], 3: [CARD(3)]})
    crawl_categories(s, [148], max_pages=2, delay=0, user_agent="ua", fetch=fetch)
    assert calls == [(148, 1), (148, 2)] and s.get_state("listing:148:done") is None
    calls.clear()
    crawl_categories(s, [148], max_pages=5, delay=0, user_agent="ua", fetch=fetch)
    assert calls[0] == (148, 3)  # resumed after the last saved page


def test_listings_cli_fails_on_empty_first_page(tmp_path, monkeypatch):
    import contextlib

    import pytest

    from mebreco import cli, listings

    monkeypatch.setattr(listings, "playwright_fetch", lambda delay, user_agent: contextlib.nullcontext(_fake_fetch({})[0]))
    with pytest.raises(SystemExit) as e:
        cli.main(["--db", str(tmp_path / "db.sqlite"), "listings", "--categories", "148"])
    assert e.value.code not in (0, None)


def test_category_not_done_when_page_only_has_books_seen_elsewhere(tmp_path):
    from mebreco.listings import crawl_categories

    s = Store(tmp_path / "db.sqlite")
    s.upsert_listings([L("1", category_id=4), L("2", category_id=4)])  # seen via another category
    fetch, calls = _fake_fetch({1: [CARD(1), CARD(2)], 2: [CARD(3)], 3: []})
    crawl_categories(s, [148], max_pages=10, delay=0, user_agent="ua", fetch=fetch)
    assert (148, 2) in calls and s.get_state("listing:148:done") == "1"
    assert {r["book_id"] for r in s.listings()} == {"1", "2", "3"}


def test_category_done_when_page_repeats_previous(tmp_path):
    from mebreco.listings import crawl_categories

    s = Store(tmp_path / "db.sqlite")
    fetch, calls = _fake_fetch({1: [CARD(1)], 2: [CARD(2)], 3: [CARD(2)], 4: [CARD(9)]})
    crawl_categories(s, [148], max_pages=10, delay=0, user_agent="ua", fetch=fetch)
    assert calls[-1] == (148, 3) and s.get_state("listing:148:done") == "1"
