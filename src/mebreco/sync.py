"""Move the catalog DB and index to and from Cloudflare R2.

Transfers go through `/data/<name>` on our own Worker (embed-worker/), which
has an R2 binding and the same bearer-token check as `/embed`. So neither
GitHub Actions nor the laptop needs a Cloudflare API token.
"""

from __future__ import annotations

import gzip
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path

import httpx

OBJECTS = ("books.sqlite.gz", "index.npz.gz")
MAX_OBJECT = 95 * 1024 * 1024  # Worker request bodies are capped at 100 MB (free plan)


def data_url(embed_url: str, name: str) -> str:
    base = embed_url.rstrip("/").removesuffix("/embed")
    return f"{base}/data/{name}"


def _client(client: httpx.Client | None) -> httpx.Client:
    return client or httpx.Client(timeout=600)


def push(db: Path, index: Path, embed_url: str, token: str, client: httpx.Client | None = None) -> None:
    http = _client(client)
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/octet-stream"}
    with tempfile.TemporaryDirectory() as tmp:
        snap = Path(tmp) / "db.sqlite"
        src, dst = sqlite3.connect(db), sqlite3.connect(snap)
        src.backup(dst)  # consistent copy even if something is writing
        src.close()
        dst.close()
        for name, path in zip(OBJECTS, (snap, Path(index))):
            body = gzip.compress(path.read_bytes())
            if len(body) > MAX_OBJECT:
                raise SystemExit(f"{name} is {len(body) / 2**20:.0f} MB gzipped; over the 95 MB per-object limit")
            r = http.put(data_url(embed_url, name), content=body, headers=headers)
            if r.status_code != 200:
                raise SystemExit(f"upload of {name} failed: HTTP {r.status_code}")


def pull(db: Path, index: Path, embed_url: str, token: str, client: httpx.Client | None = None) -> bool:
    """Replace the local DB and index with R2's copy. Returns False, changing
    nothing, if R2 doesn't have both objects yet."""
    http = _client(client)
    headers = {"Authorization": f"Bearer {token}"}
    Path(db).parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=Path(db).parent) as tmp:
        staged = []
        for name, target in zip(OBJECTS, (Path(db), Path(index))):
            r = http.get(data_url(embed_url, name), headers=headers)
            if r.status_code == 404:
                return False
            if r.status_code != 200:
                raise SystemExit(f"download of {name} failed: HTTP {r.status_code}")
            out = Path(tmp) / target.name
            with gzip.open(_write(Path(tmp) / name, r.content), "rb") as gz, open(out, "wb") as f:
                shutil.copyfileobj(gz, f)
            staged.append((out, target))
        for out, target in staged:  # only touch local files once both downloads worked
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(out, target)
    return True


def _write(path: Path, data: bytes) -> Path:
    path.write_bytes(data)
    return path
