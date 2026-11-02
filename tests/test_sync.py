import gzip
import sqlite3

import httpx

from mebreco.store import Store
from mebreco.sync import data_url, pull, push

EMBED = "https://w.example.workers.dev/embed"


def fake_r2():
    objects, seen = {}, []

    def handler(req):
        seen.append((req.method, req.url.path, req.headers.get("Authorization")))
        name = req.url.path.removeprefix("/data/")
        if req.method == "PUT":
            objects[name] = req.content
            return httpx.Response(200, json={"name": name, "size": len(req.content)})
        if name not in objects:
            return httpx.Response(404, text="not found")
        return httpx.Response(200, content=objects[name])

    return httpx.Client(transport=httpx.MockTransport(handler)), objects, seen


def make_db(path, n=3):
    s = Store(path)
    for i in range(n):
        s.set_state(f"k{i}", "v")
    s.commit()
    return s


def test_data_url():
    assert data_url(EMBED, "books.sqlite.gz") == "https://w.example.workers.dev/data/books.sqlite.gz"


def test_push_puts_two_gzipped_objects(tmp_path):
    make_db(tmp_path / "db.sqlite")
    (tmp_path / "index.pkl").write_bytes(b"pickle-bytes")
    client, objects, seen = fake_r2()
    push(tmp_path / "db.sqlite", tmp_path / "index.pkl", EMBED, "tok", client)
    assert [(m, p) for m, p, _ in seen] == [("PUT", "/data/books.sqlite.gz"), ("PUT", "/data/index.npz.gz")]
    assert all(auth == "Bearer tok" for _, _, auth in seen)
    assert gzip.decompress(objects["index.npz.gz"]) == b"pickle-bytes"
    assert gzip.decompress(objects["books.sqlite.gz"]).startswith(b"SQLite format 3")


def test_pull_missing_object_returns_false(tmp_path):
    (tmp_path / "db.sqlite").write_bytes(b"local")
    client, _, _ = fake_r2()
    assert pull(tmp_path / "db.sqlite", tmp_path / "index.pkl", EMBED, "tok", client) is False
    assert (tmp_path / "db.sqlite").read_bytes() == b"local" and not (tmp_path / "index.pkl").exists()


def test_pull_roundtrip(tmp_path):
    make_db(tmp_path / "db.sqlite", n=5)
    (tmp_path / "index.pkl").write_bytes(b"idx")
    client, _, _ = fake_r2()
    push(tmp_path / "db.sqlite", tmp_path / "index.pkl", EMBED, "tok", client)
    out = tmp_path / "out"
    out.mkdir()
    assert pull(out / "db.sqlite", out / "index.pkl", EMBED, "tok", client) is True
    assert (out / "index.pkl").read_bytes() == b"idx"
    assert sqlite3.connect(out / "db.sqlite").execute("SELECT COUNT(*) FROM state").fetchone()[0] == 5


def test_pull_cli_reports_empty_r2(tmp_path, monkeypatch, capsys):
    from mebreco import cli, sync

    client, _, _ = fake_r2()
    monkeypatch.setattr(sync, "_client", lambda c: client)
    monkeypatch.setenv("MEBRECO_EMBED_URL", EMBED)
    monkeypatch.setenv("MEBRECO_EMBED_TOKEN", "tok")
    cli.main(["--db", str(tmp_path / "new" / "db.sqlite"), "--index", str(tmp_path / "new" / "i.pkl"), "pull"])
    assert "nothing in R2 yet" in capsys.readouterr().out
