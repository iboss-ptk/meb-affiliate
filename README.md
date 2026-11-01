# meb-affiliate

Crawl the [MEB](https://www.mebmarket.com/) ebook catalog **politely** and build a
content-based "more like this" recommender, the starting point for a better
book-discovery entry point that links to MEB through an affiliate link.

> **Status:** working end-to-end on a 60-page random trial crawl of MEB
> (55 books, 5 removed IDs). A full crawl hasn't been run.

## Pipeline

```
robots.txt → sitemaps → candidate URLs → fetch (cached, rate-limited)
   → extract (schema.org JSON-LD → OpenGraph fallback) → SQLite
   → embed (TF-IDF char n-grams | multilingual model) → similar(url)
   → affiliate link
```

| Module | Job |
|---|---|
| `fetch.py` | Obeys robots.txt (including Crawl-delay), waits between requests, retries 429/5xx with backoff, caches pages on disk |
| `discover.py` | Gets book URLs from the sitemaps listed in robots.txt, so no guessing at the URL layout |
| `extract.py` | Parses schema.org `Book`/`Product` JSON-LD, falling back to OpenGraph tags; returns `None` for non-book pages |
| `store.py` | SQLite tables `books` and `visited`; re-runs resume where they stopped |
| `embed.py` | `tfidf` baseline or `semantic` (`BAAI/bge-m3`, multilingual, supports Thai) |
| `recommend.py` | Cosine top-k with a per-author cap, plus an affiliate-link template |

## Usage

```bash
pip install -e '.[dev]'           # add ,semantic for the embedding model

# 1. Check the extractor on ONE real book page before crawling anything
mebreco probe 'https://www.mebmarket.com/<some book page>'

# 2a. Popular books first: render MEB's ranking lists in a headless browser
pip install -e '.[browser]'
CHROMIUM_PATH=/path/to/chrome mebreco seeds   # Top Paid month/week, Bestseller, Top rating, ...
mebreco crawl --source seeds                   # crawl seed books, most popular first

# 2b. Or a random sample from the sitemaps
mebreco crawl --limit 50 --shuffle   # random sample; sitemap order is oldest-first
mebreco crawl --pattern '<regex for book URLs once known>'
mebreco stats

# 3. Index and query
mebreco embed                      # or: --backend semantic
MEB_AFFILIATE_TEMPLATE='https://.../?url={url_enc}' mebreco similar '<book url>'
```

If `probe` prints `null`, MEB doesn't expose JSON-LD/OpenGraph book data, and
`extract.py` needs a site-specific parser (or the crawler should call MEB's own
JSON API if the pages load their data from one; check the browser's Network tab).

## What we know about MEB (checked 2026-11-01)

- `robots.txt` has `User-agent: *` with **no Disallow rules**, and lists 10 sitemaps.
- `sitemap/bookdetail_sitemap1..9.xml` list **408,430 book URLs** of the form
  `https://www.mebmarket.com/index.php?action=BookDetails&book_id=N`
  (IDs 1–482,250 with gaps). `sitemap/sitemap.xml` holds ~29k other pages
  (home/category/series), which the default `--pattern` skips.
- `mebmarket.com` redirects everything except robots.txt and the sitemaps to `www.mebmarket.com`.
- **No JSON-LD.** OpenGraph exists, but `og:description` only repeats the title,
  so `meb.py` parses the HTML: `#book_details_lg` (title, authors, publisher,
  category, series, rating), `#description_listing`, `#wrap_genre_tag`.
- **Removed IDs return the homepage with HTTP 200**, so the parser treats a page
  without `#book_details_lg` as "not a book".
- **Field coverage in the trial (55 books):** description 53, genre tags 47,
  rating 33, series 18; authors/publisher/category/price all 55. Median description ≈ 500 characters.
- **Ranking lists (Top Paid, Bestseller, Top rating, ...) are built by JavaScript**
  that calls MEB's API, so plain HTTP sees empty lists. `mebreco seeds` renders them
  in a headless browser instead. The browser needs `web-asset.mebmarket.com` (scripts)
  and `api.mebmarket.com` (data) to be reachable. Sitemap `priority` is 0.5 for every
  book, so it isn't a popularity signal.
- **The catalog mixes in newspapers and magazines** (≈ 7 of 55 in the trial). MEB's
  own group label (from `og:title`, e.g. `:: e-book มังงะ`) is unreliable: some daily
  newspapers are labelled `หนังสือ`, so `embed` also drops titles starting with `หนังสือพิมพ์`.
- **Crawl time:** 408k pages at one request every 2 s is about **9.5 days** of
  continuous crawling. Crawl in batches with `--limit` (re-runs resume), and look for
  a JSON endpoint behind the book page that could return many books per request.

## Why these choices

- **Sitemaps + structured data, not CSS selectors.** Sitemaps exist so crawlers
  can find pages, and JSON-LD exists so machines can read them. Both change far
  less often than a site's visual layout, and using them puts less load on the
  site than following every link.
- **Character n-grams for the baseline.** Thai doesn't put spaces between
  words, so word-level TF-IDF needs a word splitter (e.g. PyThaiNLP), which
  makes its own errors. Character 2–4-grams avoid that and still match shared
  authors, series names, and topic words.
- **Two backends on purpose.** Whether a neural embedding model beats the
  baseline is a question to measure, not assume.
- **Per-author cap.** Without it, "similar to volume 1" returns volumes 2–10:
  accurate, but the user already knew about them. Discovery is the point.

## Open questions / next steps

1. **Terms and affiliate program.** Read MEB's terms of service and confirm
   crawling is allowed. Also check whether MEB offers a product feed or API to
   affiliates: that would be more stable than crawling, and gets the right link format.
2. **Evaluation before tuning.** There's no click data yet, so start with
   proxies: (a) does the model rank books from the same series/category highly
   when the title is hidden? (b) hand-rate the top 10 results for ~30 seed
   books. Compare `tfidf` and `semantic` on both.
3. **Hybrid embeddings.** Mix in category and author signals, and later add
   user signals (clicks on the discovery site), which gives a hybrid recommender.
4. **Refresh.** Prices and new releases change; re-run `crawl --refresh` on a schedule.

## Tests

```bash
pytest
```
