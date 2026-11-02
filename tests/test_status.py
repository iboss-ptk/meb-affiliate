import argparse
import json

import httpx

from mebreco.listings import Listing
from mebreco.store import Store


def _store(tmp_path):
    s = Store(tmp_path / "db.sqlite")
    s.upsert_listings([Listing(str(i), f"t{i}", "a", None, 148, None, None, i == 4) for i in range(1, 5)])
    s.set_reps({"1": ("k1", True), "2": ("k1", False), "3": ("k3", True)})
    s.set_state("listing:148:page", "5")
    s.set_state("listing:148:done", "1")
    s.mark("https://www.mebmarket.com/index.php?action=BookDetails&book_id=1", "book")
    s.set_state("neurons:2026-11-01", "1234")  # the UTC day of now=1_793_500_000
    s.commit()
    return s


def test_collect_status_counts_only(tmp_path):
    from mebreco.status import collect_status

    s = _store(tmp_path)
    a = argparse.Namespace(categories=[148, 21])
    st = collect_status(s, a, run={"stage": "crawl", "url": "https://github.com/x/actions/runs/1"}, now=1_793_500_000)
    assert st["categories"] == {"done": 1, "total": 2}
    assert st["listing_pages"] == 5 and st["listings"] == 4 and st["sets"] == 1
    assert st["first_volumes"] == {"total": 2, "crawled": 1}
    assert st["neurons_today"] == {"used": 1234, "limit": 8000}
    assert st["run"]["stage"] == "crawl"
    blob = json.dumps(st, ensure_ascii=False)
    assert "t1" not in blob and "book_id" not in blob  # counts only, no titles or ids


def test_publish_status_puts_json(tmp_path):
    from mebreco.status import publish_status

    got = {}

    def handler(req):
        got["path"], got["body"], got["auth"] = req.url.path, json.loads(req.content), req.headers["Authorization"]
        return httpx.Response(200, json={"name": "status.json", "size": len(req.content)})

    publish_status({"listings": 4}, "https://w.workers.dev/embed", "tok", httpx.Client(transport=httpx.MockTransport(handler)))
    assert got == {"path": "/data/status.json", "body": {"listings": 4}, "auth": "Bearer tok"}


def test_publish_status_never_raises(tmp_path):
    from mebreco.status import publish_status

    def handler(req):
        raise httpx.ConnectError("down")

    # a dashboard hiccup must never break the crawl
    publish_status({"x": 1}, "https://w.workers.dev/embed", "tok", httpx.Client(transport=httpx.MockTransport(handler)))


def test_pipeline_reports_each_stage(tmp_path):
    from mebreco.pipeline import run_pipeline

    a = argparse.Namespace(db=str(tmp_path / "db.sqlite"), time_budget_min=330)
    reported = []
    stages = {
        "pull": lambda a: None,
        "listings": lambda a, deadline: "done",
        "group": lambda a: None,
        "crawl": lambda a, deadline: "done",
        "embed": lambda a, deadline: None,
        "push": lambda a: None,
        "report": lambda a, stage, result=None: reported.append((stage, result)),
    }
    assert run_pipeline(a, stages) == "done"
    assert [s for s, _ in reported] == ["start", "listings", "group", "crawl", "embed", "finished"]
    assert reported[-1] == ("finished", "done")


def test_progress_calls_on_tick_every_n():
    import io

    from mebreco import cli

    seen = []
    p = cli.Progress(total=120, out=io.StringIO(), on_tick=lambda p: seen.append(p.done), tick_every=50)
    for _ in range(120):
        p.tick("book")
    assert seen == [50, 100]
