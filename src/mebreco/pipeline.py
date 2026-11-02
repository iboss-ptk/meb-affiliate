"""One unattended crawl run (GitHub Actions): pull state from R2, list
categories, pick first volumes, fetch them, embed, push state back.

Every stage is resumable, and the run stops starting new work once its time
budget is spent, so back-to-back runs finish a large crawl piece by piece.
"""

from __future__ import annotations

import argparse
import time

from .store import Store

RESERVE_MIN = 45  # of the time budget, kept for embedding (≤25 min) and pushing state
EMBED_RESERVE_MIN = 20  # stop embedding this long before the end, leaving time to push


def default_stages() -> dict:
    from . import cli, listings, sync
    from .fetch import DEFAULT_UA, PoliteFetcher
    from .series import assign_reps
    from .listings import Listing

    def pull(a):
        url, token = cli._worker_env()
        if not sync.pull(cli.Path(a.db), cli.Path(a.index), url, token):
            print("nothing in R2 yet; starting from the local database")

    def do_listings(a, deadline):
        s = Store(a.db)
        listings.crawl_categories(s, a.categories, a.max_listing_pages, a.delay, DEFAULT_UA, deadline=deadline)
        finished = all(
            s.get_state(f"listing:{c}:done") == "1" or int(s.get_state(f"listing:{c}:page", "0")) >= a.max_listing_pages
            for c in a.categories
        )
        return "done" if finished else "deadline"

    def group(a):
        s = Store(a.db)
        rows = [Listing(r["book_id"], r["title"], r["author"], r["publisher"], r["category_id"],
                        r["category_name"], r["rating_count"], bool(r["is_set"])) for r in s.listings()]
        s.set_reps(assign_reps(rows))
        s.commit()

    def crawl(a, deadline):
        s = Store(a.db)
        urls = [u for u in (cli.BOOK_URL.format(i) for i in s.rep_ids()) if not s.visited(u)]
        p = cli.Progress(len(urls), quiet=True, deadline=deadline, on_tick=lambda _: report(a, "crawl"))
        return cli.crawl_urls(PoliteFetcher(delay=a.delay), s, urls, p, deadline=deadline)

    def embed(a, deadline):
        cli.cmd_embed(argparse.Namespace(db=a.db, index=a.index, backend="cloudflare", model=None,
                                         exclude_group=["หนังสือพิมพ์"], max_neurons_per_day=a.max_neurons_per_day,
                                         deadline=deadline))

    def push(a):
        sync.push(cli.Path(a.db), cli.Path(a.index), *cli._worker_env())
        print("pushed state to R2")

    run = {"started": time.time(), "url": _run_url()}

    def report(a, stage, result=None):
        from .status import collect_status, publish_status

        run.update(stage=stage, result=result)
        publish_status(collect_status(Store(a.db), a, dict(run), neuron_limit=a.max_neurons_per_day), *cli._worker_env())

    return {"pull": pull, "listings": do_listings, "group": group, "crawl": crawl, "embed": embed, "push": push,
            "report": report}


def _run_url() -> str | None:
    import os

    if os.environ.get("GITHUB_RUN_ID"):
        return f"{os.environ['GITHUB_SERVER_URL']}/{os.environ['GITHUB_REPOSITORY']}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
    return None


def run_pipeline(a: argparse.Namespace, stages: dict | None = None) -> str:
    """Returns "done", "deadline", "blocked" or "held-off"."""
    st = stages or default_stages()
    report = st.get("report", lambda a, stage, result=None: None)
    end = time.time() + a.time_budget_min * 60
    deadline = end - RESERVE_MIN * 60  # MEB-facing work stops here
    st["pull"](a)  # state (incl. blocked_until) lives in R2, so read it first
    blocked_until = float(Store(a.db).get_state("blocked_until", "0"))
    if time.time() < blocked_until:
        print(f"MEB blocked us recently; holding off for another {(blocked_until - time.time()) / 3600:.1f} h")
        report(a, "finished", "held-off")
        return "held-off"
    result = "error"
    report(a, "start")
    try:
        if st["listings"](a, deadline) != "done":
            result = "deadline"
            return result
        report(a, "listings")
        st["group"](a)
        report(a, "group")
        result = st["crawl"](a, deadline)
        report(a, "crawl")
        st["embed"](a, end - EMBED_RESERVE_MIN * 60)  # never touches MEB, so it runs even after a block
        report(a, "embed")
        return result
    finally:
        st["push"](a)
        report(a, "finished", result)
