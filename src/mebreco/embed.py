"""Turn books into vectors.

Two backends, so we can measure whether the expensive one is worth it:

- `tfidf` (default, baseline): character n-grams over title/author/category/
  description. Thai has no spaces between words, so word-level TF-IDF needs a
  segmenter; character n-grams sidestep that and still catch shared names,
  series titles, and topical vocabulary. Cheap and fully explainable.
- `semantic`: a multilingual sentence-embedding model (default `BAAI/bge-m3`,
  which supports Thai). Catches meaning across different wording, but costs a
  GPU or a lot of CPU time and is harder to debug.

Field weighting is done by repetition in the input text, which is crude but
transparent. Tune it once there's an evaluation (see README).
"""

from __future__ import annotations

import hashlib
import math
import pickle
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from scipy import sparse
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.preprocessing import normalize

from .extract import Book


def book_text(b: Book, title_w: int = 2, author_w: int = 2, cat_w: int = 2) -> str:
    parts = (
        [b.title] * title_w
        + [" ".join(b.authors)] * author_w
        + [" ".join(b.categories + b.tags + ([b.group] if b.group else []))] * cat_w
        + ([b.series] if b.series else [])
        + ([b.publisher] if b.publisher else [])
        + ([b.description] if b.description else [])
    )
    return "\n".join(p for p in parts if p)


class Index:
    def __init__(self, urls: list[str], matrix, backend: str) -> None:
        self.urls = urls
        self.matrix = matrix  # rows are L2-normalised, so dot product == cosine
        self.backend = backend
        self._pos = {u: i for i, u in enumerate(urls)}

    def position(self, url: str) -> int:
        return self._pos[url]

    def save(self, path: Path | str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as f:
            pickle.dump((self.urls, self.matrix, self.backend), f)

    @classmethod
    def load(cls, path: Path | str) -> "Index":
        with open(path, "rb") as f:
            urls, matrix, backend = pickle.load(f)
        return cls(urls, matrix, backend)


def build_tfidf(books: list[Book]) -> Index:
    vec = TfidfVectorizer(
        analyzer="char_wb",
        ngram_range=(2, 4),
        min_df=2 if len(books) > 50 else 1,
        max_df=0.5 if len(books) > 50 else 1.0,
        sublinear_tf=True,
        max_features=500_000,
    )
    m = vec.fit_transform([book_text(b) for b in books])
    return Index([b.url for b in books], normalize(m), "tfidf")


def _batches(todo: list[int], texts: list[str], batch_size: int, max_chars: int | None) -> list[list[int]]:
    """Group text indices into batches of at most batch_size texts and
    (if set) max_chars characters; a single over-long text gets its own batch."""
    out: list[list[int]] = []
    cur: list[int] = []
    size = 0
    for i in todo:
        n = len(texts[i])
        if cur and (len(cur) >= batch_size or (max_chars and size + n > max_chars)):
            out.append(cur)
            cur, size = [], 0
        cur.append(i)
        size += n
    if cur:
        out.append(cur)
    return out


class QuotaExceeded(Exception):
    """The embedding service refused for today (rate/usage limit)."""


class Budget:
    """Daily cap on estimated Workers AI usage, so embedding stays in the free
    allowance (10k neurons/day) even on a paid plan, where overage is billed
    rather than refused. Thai runs ~1.6 tokens/char; bge-m3 is 1,075 neurons
    per million tokens. Usage is kept in state per UTC day."""

    TOKENS_PER_CHAR = 1.6
    NEURONS_PER_M_TOKENS = 1075

    def __init__(self, store, per_day: int = 8000, now=time.time) -> None:
        self.store, self.per_day, self.now = store, per_day, now

    @classmethod
    def neurons(cls, chars: int) -> int:
        return math.ceil(chars * cls.TOKENS_PER_CHAR * cls.NEURONS_PER_M_TOKENS / 1_000_000)

    def _key(self) -> str:
        return "neurons:" + datetime.fromtimestamp(self.now(), timezone.utc).strftime("%Y-%m-%d")

    def allow(self, chars: int) -> bool:
        used = int(self.store.get_state(self._key(), "0"))
        cost = self.neurons(chars)
        if used + cost > self.per_day:
            return False
        self.store.set_state(self._key(), str(used + cost))
        self.store.commit()
        return True

    def exhausted(self) -> None:
        self.store.set_state(self._key(), str(self.per_day))
        self.store.commit()


def _build_cached(
    books: list[Book], model_name: str, embed_batch, backend: str, store=None, batch_size: int = 32,
    max_chars: int | None = None, budget: Budget | None = None,
) -> Index:
    """Embed books, reusing vectors cached by a hash of the exact input text,
    so a re-run only embeds new or changed books."""
    texts = [book_text(b, 1, 1, 1) for b in books]
    hashes = [hashlib.sha1(t.encode()).hexdigest() for t in texts]
    cached = store.cached_vectors(model_name, hashes) if store else {}
    vecs: dict[str, np.ndarray] = {h: np.frombuffer(v, dtype=np.float32) for h, v in cached.items()}
    todo, seen = [], set()
    for i, h in enumerate(hashes):
        if h not in vecs and h not in seen:  # identical texts are embedded once
            seen.add(h)
            todo.append(i)
    done = 0
    for chunk in _batches(todo, texts, batch_size, max_chars):
        if budget and not budget.allow(sum(len(texts[i]) for i in chunk)):
            print("\ndaily embedding budget reached; the rest waits for tomorrow", flush=True)
            break
        try:
            raw = embed_batch([texts[i] for i in chunk])
        except QuotaExceeded as e:
            if budget:
                budget.exhausted()
            print(f"\nembedding service refused ({e}); the rest waits for tomorrow", flush=True)
            break
        emb = normalize(np.asarray(raw, dtype=np.float32))
        new = [(hashes[i], e.astype(np.float32)) for i, e in zip(chunk, emb)]
        vecs.update(new)
        if store:  # save as we go, so an interrupted run resumes
            store.cache_vectors(model_name, [(h, v.tobytes()) for h, v in new])
            store.commit()
        done += len(chunk)
        print(f"\rembedded {done}/{len(todo)}", end="", flush=True)
    print(f"\nembedded {done} new/changed, reused {len(books) - len(todo)} cached, "
          f"{len(todo) - done} waiting")
    keep = [i for i, h in enumerate(hashes) if h in vecs]  # books without a vector stay out of the index
    matrix = (np.vstack([vecs[hashes[i]] for i in keep]) if keep else np.zeros((0, 0))).astype(np.float32)
    return Index([books[i].url for i in keep], matrix, backend)


def build_semantic(books: list[Book], model_name: str = "BAAI/bge-m3", batch_size: int = 32, store=None) -> Index:
    """Embed books locally with a sentence-embedding model."""
    model = None

    def embed_batch(texts):
        nonlocal model
        if model is None:
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as e:  # pragma: no cover
                raise SystemExit("pip install '.[semantic]' to use the semantic backend") from e
            model = SentenceTransformer(model_name)
        return model.encode(texts, batch_size=batch_size, normalize_embeddings=True)

    return _build_cached(books, model_name, embed_batch, "semantic", store, batch_size)


CF_MODEL = "@cf/baai/bge-m3"


# Workers AI caps one bge-m3 request at 60k tokens, and Thai runs ~1.6 tokens
# per character, so keep each request well under ~37k characters.
CF_MAX_CHARS = 25_000


def build_cloudflare(
    books: list[Book], url: str, token: str, store=None, batch_size: int = 50, client=None,
    max_chars: int = CF_MAX_CHARS, budget: Budget | None = None,
) -> Index:
    """Embed books with bge-m3 on Cloudflare Workers AI, through our own Worker
    (embed-worker/). Same model as the local `semantic` backend, no GPU needed;
    fits in Workers AI's free daily allowance for this catalog."""
    import httpx

    http = client or httpx.Client(timeout=120)

    def embed_batch(texts):
        delay = 2.0
        for attempt in range(6):
            r = http.post(url, json={"texts": texts}, headers={"Authorization": f"Bearer {token}"})
            if r.status_code == 200:
                return r.json()["vectors"]
            if r.status_code in (401, 400):
                raise SystemExit(f"embedding worker refused the request: HTTP {r.status_code} {r.text[:200]}")
            if r.status_code == 502 and re.search(r"quota|limit|3036|4006", r.text, re.I):
                raise QuotaExceeded(r.text[:200])
            time.sleep(delay)  # 429 / 5xx: back off and retry
            delay *= 2
        if r.status_code == 429:
            raise QuotaExceeded("HTTP 429 after retries")
        raise SystemExit(f"embedding worker kept failing: HTTP {r.status_code} {r.text[:200]}")

    return _build_cached(books, CF_MODEL, embed_batch, "cloudflare", store, batch_size, max_chars, budget)


def similarities(index: Index, row: int) -> np.ndarray:
    q = index.matrix[row]
    sims = index.matrix @ q.T
    if sparse.issparse(sims):
        sims = sims.toarray()
    return np.asarray(sims).ravel()
