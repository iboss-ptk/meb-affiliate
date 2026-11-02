# Crawl Vertical Slice (Phase 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One manually triggered GitHub Actions run that lists 5 pages of MEB category 148, keeps first volumes, fetches + embeds them, and stores the result in R2 for the laptop to pull.

**Architecture:** New pure modules for card parsing (`listings.py`) and series grouping (`series.py`); listing crawl reuses the Playwright setup and `paginate` from `seeds.py`; detail crawl reuses `crawl_one`/`Progress`; embedding reuses `build_cloudflare` plus a daily neuron budget; R2 transfer goes through `/data/<name>` routes on the existing Worker (R2 binding), so no Cloudflare API token is needed anywhere. A `pipeline` command chains the stages under a time budget for Actions.

**Tech Stack:** Python 3.11+ (venv uses 3.13), Playwright Chromium, SQLite, httpx, numpy, Cloudflare R2 via an R2 binding on the existing `mebreco-embed` Worker, GitHub Actions `ubuntu-latest`.

**Spec:** `docs/superpowers/specs/2026-11-02-full-catalog-crawl-design.md` (this plan = "Phase 1: vertical slice")

## Global Constraints

- One page navigation / HTTP request every ≥ 4.0 s (`--delay 4`); never two crawlers at once (workflow `concurrency: crawl`).
- Do not call MEB's private JSON API (`ajax.php`); render pages like `seeds.py`.
- Public logs (Actions) print counts/percentages/ETA only — no titles, descriptions, URLs, book IDs.
- No scraped data in the repo or in Actions artifacts. Data lives in R2 bucket `mebreco-data`.
- Embedding budget: ≤ 8,000 neurons per UTC day; estimate tokens = chars × 1.6, neurons = tokens × 1075 / 1,000,000. 429 / budget → stop embedding for today, not crash.
- After a stop caused by blocking, set state `blocked_until = now + 6 h`; `pipeline` exits immediately while `now < blocked_until`.
- Secrets only via env: `MEBRECO_EMBED_URL`, `MEBRECO_EMBED_TOKEN` (the Worker's base URL is derived from `MEBRECO_EMBED_URL` by dropping `/embed`). No Cloudflare API token.
- One R2 object per request ≤ 95 MB (Worker request-body limit is 100 MB on the free plan); slice data is far below. Chunking for bigger files is Phase 2.
- Deferred to Phase 2 (author's call, slice is small): chunked vector files, category auto-discovery, 6-hour schedule.

## Review Focus

1. Listing page that renders 0 cards on page 1 (category id wrong / layout change) → `listings` reports 0 and exits non-zero, not silently "done" (test in Task 2).
2. Titles with no volume marker, or with numbers that aren't volumes (`ขายหัวเราะ ฉบับ 1580`, `Kingdom เล่ม 1-73`, `ยุค 70`, `80s`) → periodicals/SETs skipped, `ยุค 70` not treated as volume 70 (tests in Task 3).
3. Embedding budget exhausted mid-run → index built from books that have vectors only; un-embedded books are picked up next day (test in Task 5).
4. R2 object missing on first `pull`/first pipeline run → clear message / start from local DB, not a crash (test in Task 6).
5. Pipeline killed by the time budget mid-stage → next run resumes (listing progress per category in `state`; detail crawl uses `visited`) (test in Task 7).

---

### Task 0: Commit the session's existing work

The branch `claude/book-scraper-recommendations-ul5kwc` has uncommitted work from earlier (defusedxml fix, seeds fixes, web app, progress/blocks, Cloudflare backend, `embed-worker/`, `.gitignore`, spec + this plan). Commit it first so later task commits stay small.

- [ ] **Step 1:** `git status` — confirm `.env`, `data/`, `embed-worker/node_modules/` are NOT listed (ignored).
- [ ] **Step 2:** `.venv/bin/python -m pytest -q` — all pass.
- [ ] **Step 3:** Commit in logical groups (fixes; web app; crawl safeguards; cloudflare embedding + worker; docs).

### Task 1: Listings table and state helpers

**Files:**
- Modify: `src/mebreco/store.py` (SCHEMA + methods)
- Test: `tests/test_listings.py` (new)

**Interfaces:**
- Produces:
  - `Listing` dataclass in `src/mebreco/listings.py` (created here, filled in Task 2): `book_id: str, title: str, author: str | None, publisher: str | None, category_id: int, category_name: str | None, rating_count: int | None, is_set: bool`
  - `Store.upsert_listings(rows: list[Listing]) -> None` (book in several categories keeps one row; `category_id` of first sighting kept)
  - `Store.listings() -> list[sqlite3.Row]` with columns above + `series_key TEXT, is_rep INTEGER, seen_at REAL`
  - `Store.set_reps(assignments: dict[str, tuple[str, bool]]) -> None` (book_id → (series_key, is_rep))
  - `Store.rep_ids() -> list[str]` (book_ids with `is_rep=1`, ordered by book_id desc = newest first)

- [ ] **Step 1: Write failing tests** `test_upsert_listings_dedupes_across_categories` (same book_id in categories 148 and 4 → one row), `test_set_reps_and_rep_ids` (three rows, two reps → `rep_ids()` returns the two, newest first).
- [ ] **Step 2: Run** `.venv/bin/python -m pytest tests/test_listings.py -q` — expect FAIL (ImportError).
- [ ] **Step 3: Implement** `listings` table (`book_id TEXT PRIMARY KEY`) in `SCHEMA` and the methods above; create `src/mebreco/listings.py` with only the `Listing` dataclass.
- [ ] **Step 4: Run** the same command — expect PASS; run full suite `.venv/bin/python -m pytest -q` — all pass.
- [ ] **Step 5: Commit** `feat(store): listings table for category crawl`

### Task 2: Category listing crawl

**Files:**
- Modify: `src/mebreco/listings.py`, `src/mebreco/cli.py` (new `listings` subcommand)
- Test: `tests/test_listings.py`

**Interfaces:**
- Consumes: `Listing`, `Store.upsert_listings` (Task 1); `seeds.paginate`, `seeds._launch`, `BLOCK_TYPES`, `BLOCK_HOSTS` (existing).
- Produces:
  - `parse_card(book_id: str, lines: list[str], category_id: int) -> Listing | None`
  - `crawl_categories(store: Store, category_ids: list[int], max_pages: int, delay: float, user_agent: str) -> dict[int, int]` (category → listings saved); resumes from `state["listing:{id}:page"]`, sets `state["listing:{id}:done"]="1"` when a page adds nothing.
  - CLI: `mebreco listings --categories 148 [--max-listing-pages 5] [--delay 4]`

Card lines come from the rendered card's `innerText.split("\n")` (trimmed, empties dropped); the card element is the nearest ancestor of the `a[href*='/ebook-']` link that contains ≤ 2 such links (see recon in spec). URL: `https://www.mebmarket.com/index.php?store=category&action=book_list&category_id={id}&condition=new&page_no={n}`.

- [ ] **Step 1: Write failing tests** for `parse_card` with these real line shapes (from recon 2026-11-02):

```python
def test_parse_card_shapes():
    c = parse_card("479909", ["-11%", "SET", "ชีวิตใหม่ในโลกต่างมิติ… 10 เล่ม 1-5", "EMA TOYAMA / Siam Inter Comics", "การ์ตูนทั่วไป", "2 Rating"], 148)
    assert c.is_set and c.author == "EMA TOYAMA" and c.publisher == "Siam Inter Comics" and c.rating_count == 2
    c = parse_card("479532", ["เกาะอลวน รักอลเวง 16", "Takeshi Fujishiro / Gift Book Publishing", "การ์ตูนทั่วไป", "1 Rating"], 148)
    assert (c.title, c.is_set, c.category_name) == ("เกาะอลวน รักอลเวง 16", False, "การ์ตูนทั่วไป")
    c = parse_card("479902", ["-10%", "SET", "Giant Killing เล่ม 1-62", "Tsujitomo / Siam Inter Comics", "การ์ตูนทั่วไป", "No Rating"], 148)
    assert c.rating_count is None
    assert parse_card("1", ["-10%"], 148) is None
```

  and `test_crawl_categories_resumes_and_stops`, using a fake `fetch_page(category_id, n) -> list[tuple[str, list[str]]]` injected via a `fetch` parameter (add `fetch=None` to `crawl_categories`; default builds the Playwright fetcher): pages 1–2 have cards, page 3 empty → `state listing:148:done == "1"`; second call fetches nothing. Plus `test_listings_cli_fails_on_empty_first_page` (fake fetch returns [] for page 1 → `SystemExit` with non-zero code).
- [ ] **Step 2: Run** `pytest tests/test_listings.py -q` — FAIL.
- [ ] **Step 3: Implement** `parse_card` (drop `^-\d+%$` lines; `SET` line → `is_set`; first remaining line = title; line containing `" / "` = author / publisher; line matching `^(\d+|No) Rating` = rating; line before rating that isn't author = category) and `crawl_categories` (Playwright page per run; `time.sleep(delay)` between navigations; `paginate` per category starting at saved page; commit after every page; `Progress`-style count line only).
- [ ] **Step 4: Run** tests — PASS. Then a live check: `.venv/bin/mebreco --db /tmp/slice.sqlite listings --categories 148 --max-listing-pages 2` → prints `category 148: 80 listings` (≈40/page), and `sqlite3 /tmp/slice.sqlite "select count(*), sum(is_set) from listings"` shows non-zero SETs.
- [ ] **Step 5: Commit** `feat: crawl MEB category listings`

### Task 3: Series grouping and report

**Files:**
- Create: `src/mebreco/series.py`; Modify: `src/mebreco/cli.py` (`group` subcommand)
- Test: `tests/test_series.py`

**Interfaces:**
- Consumes: `Store.listings`, `Store.set_reps` (Task 1).
- Produces:
  - `volume_of(title: str) -> tuple[str, int | None]` — (title without volume marker, volume number or None)
  - `is_periodical(title: str) -> bool` — `ฉบับ N` / `ฉบับที่ N`
  - `assign_reps(rows: list[Listing-like]) -> dict[str, tuple[str, bool]]` — skips `is_set` and periodicals (not in output); `series_key = normalised base + "|" + (author or "")`; rep = lowest volume, tie → lowest book_id; volume None counts as volume 1.
  - `group_report(assignments, rows) -> dict` with keys `listings, skipped_sets, skipped_periodicals, series, reps`
  - CLI: `mebreco group [--examples 10]` prints the report counts, and with `--examples N` the N largest series (title base + volumes) — examples only to the local terminal.

- [ ] **Step 1: Write failing tests**:

```python
@pytest.mark.parametrize("title,base,vol", [
    ("เกาะอลวน รักอลเวง 16", "เกาะอลวน รักอลเวง", 16),
    ("คนสวยคนนี้ย้อนมายุค 70 เพื่อคุณสามีเลยนะคะ เล่ม 3", "คนสวยคนนี้ย้อนมายุค 70 เพื่อคุณสามีเลยนะคะ", 3),
    ("ยมลแห่งยมโลก เล่ม 12 (ฉบับการ์ตูน)", "ยมลแห่งยมโลก (ฉบับการ์ตูน)", 12),
    ("คนโง่ที่ไม่เคยมีรัก เล่ม 1 (ตอน 1-20)", "คนโง่ที่ไม่เคยมีรัก", 1),
    ("ทะลุมิติไปเป็นเจ้าของฟาร์มผู้ร่ำรวยในยุค 70 ภาคพิเศษ เล่ม 3 (4 เล่มจบ)", "ทะลุมิติไปเป็นเจ้าของฟาร์มผู้ร่ำรวยในยุค 70 ภาคพิเศษ", 3),
    ("Kaiju No.8 Vol. 2", "Kaiju No.8", 2),
    ("หวนคืนมาอ้อนรักคุณทหารมาดขรึม 80s", "หวนคืนมาอ้อนรักคุณทหารมาดขรึม 80s", None),
    ("ไม่ตลก", "ไม่ตลก", None),
])
def test_volume_of(title, base, vol): assert volume_of(title) == (base, vol)

def test_periodical(): assert is_periodical("ขายหัวเราะ ฉบับ 1580") and not is_periodical("ยมลแห่งยมโลก เล่ม 12 (ฉบับการ์ตูน)")

def test_assign_reps():
    L = lambda i, t, a="X", s=False: Listing(i, t, a, None, 148, None, None, s)
    rows = [L("10", "เกาะอลวน รักอลเวง 16"), L("11", "เกาะอลวน รักอลเวง 3"), L("12", "เกาะอลวน รักอลเวง 1"),
            L("13", "Giant Killing เล่ม 1-62", s=True), L("14", "ขายหัวเราะ ฉบับ 1580"), L("15", "ไม่ตลก")]
    a = assign_reps(rows)
    assert set(a) == {"10", "11", "12", "15"}
    assert a["12"][1] and not a["10"][1] and not a["11"][1] and a["15"][1]
    assert a["10"][0] == a["12"][0] != a["15"][0]
```
- [ ] **Step 2: Run** `pytest tests/test_series.py -q` — FAIL.
- [ ] **Step 3: Implement** in `series.py`. Marker order matters: strip `(N เล่มจบ)`, `(ตอน …)`, then `เล่มที่ N` / `เล่ม N` / `Vol. N` / `vol.N` / `ภาค N` (volume = N), else a trailing bare integer preceded by a space (volume), except when the token before it is `ยุค` or the number is followed by letters (`80s`). Normalise base: collapse spaces, strip, casefold.
- [ ] **Step 4: Run** tests — PASS; live: `.venv/bin/mebreco --db /tmp/slice.sqlite group --examples 10` shows counts and sensible series.
- [ ] **Step 5: Commit** `feat: group listings into series, keep first volumes`

### Task 4: Detail crawl of representatives, quiet logs, block hold-off

**Files:**
- Modify: `src/mebreco/cli.py` (`crawl --source reps`, `Progress(quiet=...)`, block state)
- Test: `tests/test_pipeline.py`

**Interfaces:**
- Consumes: `Store.rep_ids` (Task 1); existing `crawl_one`, `Progress`, `Blocked`, `BOOK_URL`.
- Produces:
  - `Progress(..., quiet: bool = False)` — quiet: line has counts/%/ETA, no note (title).
  - `--source reps` in `crawl`, plus global flag `--quiet` on `crawl`.
  - `crawl_urls(f, s, urls, progress, deadline: float | None) -> str` returning `"done" | "deadline" | "blocked"`; on `"blocked"` sets `state blocked_until = time.time() + 6*3600`. `cmd_crawl` becomes a thin wrapper over it.

- [ ] **Step 1: Write failing tests** `test_progress_quiet_hides_titles` (quiet line for note "ชื่อลับ" doesn't contain it) and `test_crawl_urls_sets_blocked_until` (fake `crawl_one` via monkeypatch always `("http_403", None)`, `block_streak=2, max_cooldowns=0` → returns `"blocked"`, `state blocked_until > now`), `test_crawl_urls_stops_at_deadline` (deadline in the past → returns `"deadline"` before fetching).
- [ ] **Step 2: Run** — FAIL.
- [ ] **Step 3: Implement** as specified; `Progress` gets `max_cooldowns=0` support (raise immediately after streak).
- [ ] **Step 4: Run** full suite — PASS.
- [ ] **Step 5: Commit** `feat: crawl first volumes; quiet logs; 6h hold-off after a block`

### Task 5: Daily embedding budget

**Files:**
- Modify: `src/mebreco/embed.py`, `src/mebreco/cli.py` (`embed --max-neurons-per-day 8000`)
- Test: `tests/test_pipeline.py`

**Interfaces:**
- Consumes: `_build_cached`, `build_cloudflare` (existing), `Store.get_state/set_state`.
- Produces:
  - `class Budget: __init__(self, store, per_day: int = 8000, now=time.time)`, `.allow(chars: int) -> bool` (adds estimated neurons for today's UTC date key `neurons:YYYY-MM-DD` if within budget), `.exhausted() -> None` (marks today spent).
  - `_build_cached(..., budget: Budget | None = None)` — before each batch `budget.allow(sum chars)`; when it returns False, or `embed_batch` raises `QuotaExceeded`, stop embedding and build the index from books that already have vectors (books without vectors are left out).
  - `class QuotaExceeded(Exception)` raised by `build_cloudflare`'s `embed_batch` on HTTP 429 after retries, and on 502 whose body mentions quota/`3036`/`4006`.

- [ ] **Step 1: Write failing tests** `test_budget_counts_per_utc_day` (per_day=100; 1075 neurons per 1M tokens: `allow(40_000)` → 40k×1.6=64k tokens ≈ 68.8 neurons True; second `allow(40_000)` False; with `now` moved to next UTC day → True) and `test_build_cached_partial_on_budget` (6 books, budget allows only the first batch of 4 → `len(idx.urls) == 4`, re-run next day embeds the remaining 2).
- [ ] **Step 2: Run** — FAIL.
- [ ] **Step 3: Implement** as specified (neurons = chars × 1.6 × 1075 / 1e6, rounded up).
- [ ] **Step 4: Run** full suite — PASS.
- [ ] **Step 5: Commit** `feat: cap Workers AI embedding at 8k neurons/day`

### Task 6: R2 push / pull through the Worker

**Files:**
- Modify: `embed-worker/wrangler.jsonc` (add `"r2_buckets": [{ "binding": "DATA", "bucket_name": "mebreco-data" }]`), `embed-worker/src/index.js` (routes)
- Create: `src/mebreco/sync.py`; Modify: `src/mebreco/cli.py` (`push`, `pull`)
- Test: `tests/test_sync.py`

**Interfaces:**
- Worker routes (same `Authorization: Bearer <EMBED_TOKEN>` check as `/embed`; names must match `^[a-z0-9._-]{1,64}$`):
  - `PUT /data/<name>` → stores the request body stream in `env.DATA` under `<name>`; returns `{"name", "size"}`; 413 if `Content-Length` > 95 MB.
  - `GET /data/<name>` → streams the object (`application/octet-stream`), 404 if missing.
- Python:
  - `OBJECTS = ("books.sqlite.gz", "index.pkl.gz")`
  - `data_url(embed_url: str, name: str) -> str` — `https://…workers.dev/embed` → `https://…workers.dev/data/<name>`
  - `push(db: Path, index: Path, embed_url: str, token: str, client: httpx.Client | None = None) -> None` — sqlite online backup to a temp copy, gzip both, `PUT` each.
  - `pull(db: Path, index: Path, embed_url: str, token: str, client=None) -> bool` — `GET` each; on 404 return False and change nothing; otherwise write to temp, gunzip, `os.replace` over `db`/`index`.
  - CLI `mebreco push`, `mebreco pull` (prints "nothing in R2 yet" when `pull` returns False).

- [ ] **Step 1: Write failing tests** with `httpx.MockTransport`: `test_data_url`, `test_push_puts_two_gzipped_objects` (2 PUTs to `/data/books.sqlite.gz` and `/data/index.pkl.gz`, bearer header present, bodies gunzip cleanly), `test_pull_missing_object_returns_false` (404 → False, local db untouched), `test_pull_roundtrip` (push then pull into new paths via an in-memory fake server → db readable, same `stats()`).
- [ ] **Step 2: Run** `pytest tests/test_sync.py -q` — FAIL.
- [ ] **Step 3: Implement** Python side as specified (never print object contents).
- [ ] **Step 4: Implement** Worker routes; `npx wrangler deploy --dry-run` (in `embed-worker/`) shows bindings `env.AI` and `env.DATA`.
- [ ] **Step 5: Run** tests — PASS. Live (after the user enables R2): `npx wrangler r2 bucket create mebreco-data`, `npx wrangler deploy`; then `curl -s -o /dev/null -w '%{http_code}' -X PUT <base>/data/x` without auth → `401`; `.venv/bin/mebreco push`; `.venv/bin/mebreco --db /tmp/p.sqlite --index /tmp/p.pkl pull` → `mebreco --db /tmp/p.sqlite stats` matches local.
- [ ] **Step 6: Commit** `feat: push/pull data to R2 via the Worker`

### Task 7: Pipeline command and GitHub workflow

**Files:**
- Modify: `src/mebreco/cli.py` (`pipeline`); Create: `.github/workflows/crawl.yml`; Modify: `README.md` (short "Cloud crawl" section)
- Test: `tests/test_pipeline.py`

**Interfaces:**
- Consumes: everything above.
- Produces: `mebreco pipeline --categories 148 --max-listing-pages 5 --time-budget-min 330 --delay 4 --quiet` which, in order: exits 0 if `blocked_until` in future; `pull` (start from local/empty DB if nothing in R2); `listings` until done or deadline; `group` (counts only); `crawl --source reps` until deadline; `embed` (cloudflare, budget); `push`. Each stage checks the deadline; `push` always runs (in `finally`) if anything changed.

- [ ] **Step 1: Write failing test** `test_pipeline_order_and_resume` — monkeypatch the stage functions with recorders; with deadline reached after listings, the second invocation resumes (listing stage reports done, proceeds to crawl); `push` called both times; blocked_until in future → no stage runs.
- [ ] **Step 2: Run** — FAIL.
- [ ] **Step 3: Implement** `cmd_pipeline`; workflow:
  - `on: workflow_dispatch` with inputs `categories` (default `"148"`), `max_listing_pages` (default `"5"`); **no `schedule`**.
  - `concurrency: { group: crawl, cancel-in-progress: false }`, `timeout-minutes: 350`, `permissions: { contents: read }`.
  - Steps: checkout; setup-python 3.13; `pip install -e '.[browser]'`; `playwright install --with-deps chromium`; run `mebreco pipeline … --quiet` with env from the two secrets `MEBRECO_EMBED_URL`, `MEBRECO_EMBED_TOKEN`. No `upload-artifact`.
- [ ] **Step 4: Run** full suite — PASS; `python -c "import yaml,sys; yaml.safe_load(open('.github/workflows/crawl.yml'))"` parses (use `pip install pyyaml` in a scratch venv if missing, or `actionlint` if installed).
- [ ] **Step 5: Commit** `feat: pipeline command + manual GitHub Actions crawl (slice)`

### After the tasks (user + me)

1. User: enable R2 in the dashboard. Me: create bucket, deploy Worker with the binding, `gh secret set` the two secrets from `.env`.
2. Seed R2 once from the laptop: `mebreco push`.
3. Push branch, open PR; user merges to `main`; user clicks **Actions → Crawl → Run workflow**.
4. Laptop: `mebreco pull`, `mebreco group --examples 10` (grouping review), restart `mebreco serve`; review with the user before Phase 2.
