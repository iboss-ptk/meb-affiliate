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

import pickle
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
        + [" ".join(b.categories)] * cat_w
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


def build_semantic(books: list[Book], model_name: str = "BAAI/bge-m3", batch_size: int = 32) -> Index:
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as e:  # pragma: no cover
        raise SystemExit("pip install '.[semantic]' to use the semantic backend") from e
    model = SentenceTransformer(model_name)
    emb = model.encode(
        [book_text(b, 1, 1, 1) for b in books],
        batch_size=batch_size,
        normalize_embeddings=True,
        show_progress_bar=True,
    )
    return Index([b.url for b in books], np.asarray(emb, dtype=np.float32), "semantic")


def similarities(index: Index, row: int) -> np.ndarray:
    q = index.matrix[row]
    sims = index.matrix @ q.T
    if sparse.issparse(sims):
        sims = sims.toarray()
    return np.asarray(sims).ravel()
