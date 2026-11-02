# Full-catalog crawl on GitHub Actions — design

Date: 2026-11-02 · Status: draft for approval

## Intent

The recommender currently knows 3,076 popular MEB books. MEB's sitemaps list
408,731 book pages. We want the useful part of the whole catalog in the
recommender, without hammering MEB and without needing the laptop to stay on.

Crawling every sitemap URL at the pace MEB tolerates (one request per ~4 s;
2 s got us blocked for ~10 minutes on 2026-11-01) would take ~19 days of
nonstop crawling, and most of those pages are things we'd never recommend
(magazine/newspaper issues, volume 7 of a series, boxed sets).

## Goals

- Every book in MEB's non-periodical categories is **known** (ID, title,
  author/publisher, category, rating count) via category listing pages,
  ~40 books per request.
- Only the **first volume of each series** (and every standalone book) gets
  its detail page fetched (description, tags, series) and embedded.
- Runs unattended on **GitHub Actions** (repo is public → unlimited minutes),
  resumable across the 6-hour job limit, until done.
- Results land in R2, where the laptop app can pull them from.

## Non-goals

- Daily incremental updates (later; reuse the same pieces).
- Moving the web app off the laptop.
- Bookmarklet / MEB-shelf import.
- Crawling magazines, newspapers, audiobooks, or SET bundles.

## Anti-goals (failure even if it "works")

- Going faster than ~1 request / 4 s, or running more than one crawler at a
  time. GitHub runners get a new IP per job; that must never be used to dodge
  a block. If MEB blocks us, we back off and wait, not rotate.
- Publishing scraped MEB content: no book data in the repo, in Actions logs,
  or in Actions artifacts (artifacts on a public repo are downloadable).
- Leaking secrets in logs or the repo.

## What we learned about MEB (recon, 2026-11-02)

- Category listing: `https://www.mebmarket.com/index.php?store=category&action=book_list&category_id={id}&condition=new&page_no={n}`
  - Filled in by JavaScript (needs a headless browser, as `seeds.py` does).
  - 40 books per page; pages go to the end of the category (category 228 had
    books on page 200; page 1000 was empty → empty page = end).
  - Each card shows: book ID (in `/ebook-{id}-…` link), title, author /
    publisher, category name, rating count, discount badge, `SET` badge.
  - No series ID on cards. Volume is usually in the title: `เล่ม 3`,
    trailing number (`เกาะอลวน รักอลเวง 16`), `ฉบับ 1580` (periodical
    issue), `SET … เล่ม 1-73` (bundle).
- The category list itself comes from the page's own JS (`Store.getCategoriesPageAll`);
  category links appear on store home pages (`?store=fiction`, `?store=cartoon`,
  `?store=other`, text books at `?action=text_books_home_page&text_book_id=all`).
  Magazines are `category_id=1`; newspapers have their own group.
- Book detail pages (`index.php?action=BookDetails&book_id=N`, already parsed
  by `meb.py`) carry a `series_detail` link → exact series name.
- robots.txt allows everything; no crawl-delay.

## Design

### Stage 1 — Listing pass (discover)

1. Collect the category list once per run from the store home pages (fiction,
   cartoon, other/general, text books), excluding magazines, newspapers and
   audiobooks. Log category count only.
2. For each category, walk `page_no = 1, 2, …` until a page adds no new IDs
   (reuse `seeds.paginate`). Same rate limit, user agent and block handling
   as the book crawler.
3. Store each card in a new `listings` table:
   `book_id, title, author, publisher, category_id, category_name,
   rating_count, is_set, seen_at`. A book in several categories is one row
   (categories accumulate).
4. Progress per category (`last_page`, `done`) lives in `state`, so a job
   killed at 6 h resumes where it stopped.

### Stage 2 — Series grouping (local, no requests)

- Skip `is_set` rows and periodical issues (`ฉบับ N`, `ฉบับที่ N`).
- `series_key = normalise(title without volume marker) + first author`.
  Volume markers to strip: `เล่ม N`, `เล่มที่ N`, `Vol. N`/`vol.N`, `ภาค N`,
  `(ตอน N-M)`, `(N เล่มจบ)`, trailing bare number.
- Per `series_key`, the **representative** is the lowest volume number;
  ties → lowest book_id. Standalone books are their own representative.
- Write `listings.series_key` and `listings.is_rep`.
- Heuristic will be imperfect; acceptable. Once a representative's detail page
  is fetched, its real `series` field (from `meb.py`) is stored too, so the
  grouping can be audited/refined later.

### Stage 3 — Detail pass (fetch + embed)

1. Queue = representatives not yet in `books` (existing `visited` rules:
   blocked/errored are retried).
2. Crawl with the existing `crawl_one` / `Progress` / block cool-down.
3. After the crawl budget for the job is spent, run
   `mebreco embed` (Cloudflare backend; cached vectors are reused, only new
   books cost tokens), within the daily neuron budget described in
   "Calls made after the builder check" (item 1).

### Running on GitHub Actions

- Workflow `.github/workflows/crawl.yml`, `ubuntu-latest`, Python 3.13 +
  Playwright Chromium.
- Triggers: `schedule` every 6 hours + `workflow_dispatch`.
  `concurrency: { group: crawl, cancel-in-progress: false }` → never two at once.
- Each job: download state from R2 → run stages until ~5 h 30 m elapsed
  (time budget flag) → upload state to R2 → exit 0. Stage order per job:
  finish Stage 1 if incomplete; else Stage 2 (seconds) then Stage 3.
- Block handling across jobs: if a job stops because of a block, record
  `blocked_until = now + 6 h` in state; the next job exits immediately if
  still within it.
- Logs: counts, percentages and ETA only — no titles, descriptions or URLs
  (`Progress` gets a "quiet" mode).

### State storage: Cloudflare R2

- Private bucket `mebreco-data`, objects: `books.sqlite` (gzip) and
  `index.pkl`. One writer at a time (guaranteed by `concurrency`).
- Uploads/downloads go through `PUT/GET /data/<name>` routes on the existing
  `mebreco-embed` Worker, which has an R2 binding and the same bearer-token
  check. So Actions and the laptop need only `MEBRECO_EMBED_URL` and
  `MEBRECO_EMBED_TOKEN`; no Cloudflare API token exists anywhere (user
  decision 2026-11-02, to minimise manual setup). Worker request bodies are
  capped at 100 MB on the free plan → objects ≤ 95 MB; chunk in Phase 2.
- Laptop: `mebreco pull` downloads the latest DB + index from R2 (same wrangler
  login), then restart `mebreco serve`.
- Seeding R2: the first job starts from the current laptop DB (3,076 books,
  950+ cached vectors), uploaded once by hand with `mebreco push`.

## Phase 1: vertical slice (approved by the user, build first)

Exercise every piece end to end at tiny scale before the full run:

- CLI limits: `--categories 148 --max-listing-pages 5` (การ์ตูนทั่วไป: many
  numbered volumes and SET bundles, a hard case for grouping). ~200 listings.
- Stage 2 grouping prints a report (counts + example groups, written to the
  local terminal / a file, not to public Actions logs beyond counts).
- Stage 3 detail-fetches only the representatives, embeds them via the Worker
  within the daily budget.
- Push state to R2; on the laptop, `mebreco pull` then `mebreco serve` shows
  the new books.
- Workflow is `workflow_dispatch` only, with inputs for the two limits. No
  `schedule` until the user approves the full run after reviewing the slice.

Phase 2 (full run) = remove the limits and enable the 6-hour schedule.

## Decided by the user

- Run on GitHub Actions; repo made public (done 2026-11-02) for unlimited minutes.
- Exclude magazines and newspapers; **include text books**.
- Series: crawl only the first volume.
- Daily updates are out of scope for now.

## Left to the builder

- Exact volume-marker regexes and normalisation (with unit tests on real
  title shapes listed above).
- How categories are discovered (store home pages vs the page's own category
  menu) as long as magazines/newspapers/audiobooks are excluded and the list
  is logged as a count.
- SQLite schema details/migrations, gzip, R2 object naming.
- Time-budget margins, commit cadence.

## Calls made after the builder check (author's calls, not the user's)

1. **Embedding stays free, decoupled from crawling.** Detail crawling can
   outpace the free tier (~20k books/day ≈ 30M tokens vs ~9M free), and on
   Workers Paid overage is billed, not refused. So the embed step enforces a
   **client-side daily budget of 8,000 neurons (~7.4M tokens)**, tracked in
   `state` per UTC day using the 1.6 tokens/char estimate; it also treats
   429/quota errors as "stop for today", not a crash. Unembedded books simply
   wait; embedding catches up after crawling finishes. Cost: $0 on either plan.
2. **Pacing unit = one page navigation.** A rendered listing page counts as
   one request for the 4 s rule (assets/XHR it triggers don't count
   separately; images/fonts/trackers stay blocked as in `seeds.py`). We do
   **not** call MEB's private JSON API — same stance as `seeds.py`.
3. **No Worker-readable format now.** The data only has to live in R2; a
   future Worker API would get its own export. Dropped from goals.
4. **Scale & R2 objects.** Plan for up to ~150k detail pages. Vectors move
   out of SQLite into chunked `.npy` files (≤ 200 MB per object, each with
   an id list), so no single R2 object approaches wrangler's upload limit;
   `books.sqlite.gz` without vectors stays small. Transfers keep using
   the Worker's `/data/<name>` routes (see State storage).
5. **Laptop is read-only after the one-time seeding push.** `mebreco pull`
   replaces the local DB/index; local `seeds`/`crawl`/`update` aren't run
   while the Actions crawl owns the data. Refreshing ranking lists moves into
   the workflow later (part of the daily-update work, out of scope now).
6. **`listings` is only a crawl queue in this project.** The web app keeps
   showing crawled + embedded books; "liking volume 7 = liking volume 1" and
   listing-based onboarding are later work.

## Open questions for the user

None outstanding.
