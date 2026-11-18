"""Builds from ContentDB's daily dump match builds from the API, and fall back to it."""

import json
import sqlite3
import zipfile
from datetime import date
from pathlib import Path

import httpx
import pytest

from luanti_depgraph import db, dump

CONTENTDB = Path(__file__).parent / "fixtures" / "contentdb"
DETAILS = {
    "mt-mods/pipeworks": {
        "license": "LGPL-3.0-only", "media_license": "CC-BY-SA-4.0", "dev_state": "ACTIVELY_DEVELOPED",
        "maintainers": ["VanessaE", "BuckarooBanzay"], "tags": ["technology"],
        "issue_tracker": "https://github.com/mt-mods/pipeworks/issues",
        "long_description": "Tubes that carry items between machines.",
        "game_support": [{"supports": True, "confidence": 10,
                          "game": {"author": "Luanti", "name": "minetest_game", "type": "game"}}],
    },
}
RELEASES = {
    "mt-mods/pipeworks": [
        {"id": 201, "title": "2024-05-01", "release_date": "2024-05-01T10:00:00", "commit": "b" * 40,
         "min_minetest_version": {"name": "5.4"}, "max_minetest_version": None, "downloads": 900,
         "size": 1234, "sha256": "ab" * 32, "url": "https://cdn.example/b.zip"},
        {"id": 150, "title": "2021-01-01", "release_date": "2021-01-01T10:00:00", "commit": "a" * 40,
         "min_minetest_version": {"name": "5.0"}, "max_minetest_version": None, "downloads": 100,
         "size": 1000, "sha256": None, "url": "https://cdn.example/a.zip"},
    ],
}
ALIASES = {"Luanti/minetest_game": [{"author": "Minetest", "name": "minetest_game"}]}


def _fixture(name: str):
    return json.loads((CONTENTDB / f"{name}.json").read_text())


def make_dump(path: Path, version: int = 1, drop: str | None = None) -> Path:
    """A dump zip with the fixture packages, laid out like ContentDB's."""
    deps = {(d["author"], d["name"]): d for d in _fixture("dependencies")}
    scores = {(s["author"], s["name"]): s for s in _fixture("scores")}
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("backup/index.json", json.dumps({"version": version, "created_at": "2026-11-18T05:00:00"}))
        z.writestr("backup/licenses.json", "[]")
        for p in _fixture("packages"):
            key = f"{p['author']}/{p['name']}"
            d = deps[(p["author"], p["name"])]
            s = scores.get((p["author"], p["name"]), {})
            pkg = {**p, "provides": d["provides"], "depends": d["depends"],
                   "optional_depends": d["optional_depends"], "aliases": ALIASES.get(key, []),
                   "score": s.get("score"), "downloads": s.get("downloads"), "maintainers": [p["author"]],
                   "game_support": [], "tags": [], **DETAILS.get(key, {})}
            if drop:
                pkg.pop(drop)
            releases = RELEASES.get(key, [{"id": p["release"], "release_date": "2025-01-01T00:00:00",
                                           "commit": None, "min_minetest_version": None,
                                           "max_minetest_version": None}])
            if f"backup/{p['author']}/index.json" not in z.namelist():
                z.writestr(f"backup/{p['author']}/index.json", json.dumps({"username": p["author"]}))
            z.writestr(f"backup/{key}/index.json", json.dumps(pkg))
            z.writestr(f"backup/{key}/releases.json", json.dumps(releases))
            z.writestr(f"backup/{key}/screenshots.json", "[]")
    return path


@pytest.fixture()
def dump_zip(tmp_path) -> Path:
    return make_dump(tmp_path / "backup-2026-11-18.zip")


@pytest.fixture()
def serve_dump(monkeypatch, dump_zip):
    """Make the build "download" `dump_zip`."""
    def download(dump_dir, log=print, **_kw):
        dump_dir.mkdir(parents=True, exist_ok=True)
        target = dump_dir / dump_zip.name
        target.write_bytes(dump_zip.read_bytes())
        return target
    monkeypatch.setattr(dump, "download", download)


class _NoAPI:
    def __getattr__(self, name):
        raise AssertionError(f"API called: {name}")


def _rows(path: Path, sql: str) -> list:
    return sorted(sqlite3.connect(path).execute(sql).fetchall())


def test_load_gives_api_shaped_lists(dump_zip):
    loaded = dump.load(dump_zip)
    assert loaded.created_at == "2026-11-18T05:00:00"
    keys = [(p["author"], p["name"]) for p in loaded.raw["packages"]]
    assert keys == sorted(keys) and len(keys) == len(_fixture("packages"))
    assert loaded.raw["updates"]["Minetest/minetest_game"] == 100
    pipeworks = loaded.details[("mt-mods", "pipeworks")]
    assert pipeworks["latest_release"]["id"] == 201 and len(pipeworks["releases"]) == 2
    assert next(p for p in loaded.raw["packages"] if p["name"] == "pipeworks")["commit"] == "b" * 40


@pytest.mark.parametrize("kwargs", [{"version": 2}, {"drop": "depends"}])
def test_load_rejects_unknown_format(tmp_path, kwargs):
    with pytest.raises(dump.DumpFormatError):
        dump.load(make_dump(tmp_path / "backup-2026-11-18.zip", **kwargs))


def test_dump_build_matches_api_build(db_file, tmp_path, serve_dump):
    """Same packages, dependencies and aliases as the build from the API fixtures."""
    out = tmp_path / "dump.sqlite"
    stats = db.build(out, tmp_path / "cache", tmp_path / "corpus", source="dump", log=lambda _m: None)
    assert stats["contentdb_source"] == "dump" and stats["contentdb_requests"] == 0
    assert stats["details"] == "dump" and stats["dump_created_at"] == "2026-11-18T05:00:00"
    for sql in ("SELECT author, name, type, title, repo, release_id, downloads, score FROM package",
                "SELECT p.name, modname FROM provides JOIN package p ON p.id = package_id",
                "SELECT p.name, modname, optional FROM dep JOIN package p ON p.id = package_id",
                "SELECT alias, p.name FROM package_alias JOIN package p ON p.id = package_id"):
        assert _rows(out, sql) == _rows(db_file, sql), sql
    # Details come with the dump, without --details.
    assert _rows(out, "SELECT license, min_engine, latest_release_at FROM package WHERE name = 'pipeworks'") == [
        ("LGPL-3.0-only", "5.4", "2024-05-01T10:00:00")]
    assert _rows(out, "SELECT confidence FROM game_support") == [(10,)]

    # --offline reads the details from the cached dump again.
    again = db.build(out, tmp_path / "cache", tmp_path / "corpus", offline=True, log=lambda _m: None)
    assert again["contentdb_source"] == "dump" and again["details"] == "dump"
    assert _rows(out, "SELECT license FROM package WHERE name = 'pipeworks'") == [("LGPL-3.0-only",)]


class _FixtureAPI:
    requests = 0

    def packages(self):
        return _fixture("packages")

    def dependencies(self):
        return _fixture("dependencies")

    def scores(self):
        return _fixture("scores")

    def updates(self):
        return _fixture("updates")


def test_auto_falls_back_to_api(tmp_path, monkeypatch):
    def unavailable(*_a, **_kw):
        raise dump.DumpUnavailable("404")
    monkeypatch.setattr(dump, "download", unavailable)
    logged: list[str] = []
    raw, loaded, origin = db._fetch_raw(_FixtureAPI(), tmp_path, "auto", False, logged.append, {})
    assert loaded is None and origin == {"source": "api"}
    assert raw["packages"] == _fixture("packages")
    assert any("falling back to the API" in m for m in logged)
    with pytest.raises(dump.DumpUnavailable):
        db._fetch_raw(_NoAPI(), tmp_path, "dump", False, logged.append, {})


def test_auto_falls_back_on_unknown_format(tmp_path, monkeypatch):
    bad = make_dump(tmp_path / "backup-2026-11-18.zip", version=99)
    monkeypatch.setattr(dump, "download", lambda *_a, **_kw: bad)
    _raw, loaded, origin = db._fetch_raw(_FixtureAPI(), tmp_path, "auto", False, lambda _m: None, {})
    assert loaded is None and origin["source"] == "api"


def test_download_takes_yesterdays_until_todays_is_out(tmp_path, dump_zip):
    asked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(request.url.path)
        if request.url.path.endswith("backup-2026-11-18.zip"):
            return httpx.Response(404)
        return httpx.Response(200, content=dump_zip.read_bytes())

    client = httpx.Client(transport=httpx.MockTransport(handler))
    dump_dir = tmp_path / "dump"
    dump_dir.mkdir()
    (dump_dir / "backup-2026-11-10.zip").write_bytes(b"old")
    path = dump.download(dump_dir, lambda _m: None, today=date(2026, 11, 18), client=client)
    assert path.name == "backup-2026-11-17.zip"
    assert [p.name for p in dump_dir.iterdir()] == ["backup-2026-11-17.zip"]
    # Cached: the next run only asks whether today's is out yet.
    asked.clear()
    assert dump.download(dump_dir, lambda _m: None, today=date(2026, 11, 18), client=client) == path
    assert asked == ["/uploads/backup-2026-11-18.zip"]
