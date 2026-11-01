# meb-affiliate

Crawl the [MEB](https://www.mebmarket.com/) ebook catalog **politely** and build a
content-based "more like this" recommender, the starting point for a better
book-discovery entry point that links to MEB through an affiliate link.

> **Status:** the pipeline is built and tested against hand-written fixtures.
> It has **not been run against MEB yet**: the build environment couldn't reach
> the site. Step 1 below checks whether the generic extractor works on real pages.

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

# 2. Small crawl first, then widen
mebreco crawl --limit 50
mebreco crawl --pattern '<regex for book URLs once known>'
mebreco stats

# 3. Index and query
mebreco embed                      # or: --backend semantic
MEB_AFFILIATE_TEMPLATE='https://.../?url={url_enc}' mebreco similar '<book url>'
```

If `probe` prints `null`, MEB doesn't expose JSON-LD/OpenGraph book data, and
`extract.py` needs a site-specific parser (or the crawler should call MEB's own
JSON API if the pages load their data from one; check the browser's Network tab).

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
