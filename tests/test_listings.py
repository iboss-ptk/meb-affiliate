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
