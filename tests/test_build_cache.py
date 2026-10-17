"""Nightly builds reuse work for unchanged corpus packages: analysis facts and code index rows."""

import shutil
import sqlite3
import sys
import types
from pathlib import Path

import pytest

from luanti_depgraph import analyze, db
from luanti_depgraph.corpus import MARKER
from luanti_depgraph.luanticonf import scan_package

MODS = Path(__file__).parent / "fixtures" / "mods"


@pytest.mark.parametrize("fixture", sorted(p.name for p in MODS.iterdir()))
def test_facts_survive_json(fixture):
    pkg = scan_package(MODS / fixture)
    for mod in pkg.mods:
        facts = analyze.analyze_mod(mod, pkg.root)
        assert analyze.facts_from_json(analyze.facts_to_json(facts)) == facts


def _rows(path: Path, sql: str) -> list:
    return sqlite3.connect(path).execute(sql).fetchall()


def test_rebuild_reuses_cached_analysis(db_file):
    root = db_file.parent
    again = root / "again.sqlite"
    logged: list[str] = []
    stats = db.build(again, root / "cache", root / "corpus", offline=True, log=logged.append)
    assert stats["analysis_cached_packages"] == 1
    assert any("1 unchanged" in m for m in logged)
    refs = "SELECT m.name, r.* FROM ref r JOIN mod m ON m.id = r.mod_id ORDER BY 1, 3, 4, 5"
    assert _rows(again, refs) == _rows(db_file, refs)

    # --analyze-only re-runs the analyzer even though the release is unchanged.
    stats = db.build(again, root / "cache", root / "corpus", analyze_only=True, log=lambda _m: None)
    assert stats["analysis_cached_packages"] == 0
    assert _rows(again, refs) == _rows(db_file, refs)


# ------------------------------------------------------------------ code index


class _FakeModel:
    """Deterministic stand-in for the embedding model, so the test needs no download."""

    @classmethod
    def from_pretrained(cls, _name):
        return cls()

    def encode(self, texts):
        import numpy as np
        return np.array([[len(t) % 7 + 1, t.count("\n") + 1, 1.0] for t in texts], dtype=np.float32)


@pytest.fixture()
def fake_model(monkeypatch):
    pytest.importorskip("numpy")
    monkeypatch.setitem(sys.modules, "model2vec", types.SimpleNamespace(StaticModel=_FakeModel))


def _corpus(root: Path) -> Path:
    corpus = root / "corpus"
    for i, fixture in enumerate(("crossgame_mod", "my_modpack", "undeclared_mod")):
        dest = corpus / "someone" / fixture
        shutil.copytree(MODS / fixture, dest)
        (dest / MARKER).write_text(f"{100 + i} test\n")
    return corpus


def _index_content(path: Path) -> list:
    # Everything but the ids, which are renumbered when rows are copied.
    return _rows(path, "SELECT c.package, c.mod, c.file, c.line_start, c.kind, c.symbol, c.text, v.emb"
                       " FROM chunk c JOIN vec v ON v.id = c.id ORDER BY 1, 2, 3, 4, 6")


def test_code_index_copies_unchanged_packages(tmp_path, fake_model):
    from luanti_depgraph.codesearch import build_index

    corpus = _corpus(tmp_path)
    out = tmp_path / "code.sqlite"
    first = build_index(corpus, out, log=lambda _m: None)
    assert first["reused_packages"] == 0
    fresh = _index_content(out)

    second = build_index(corpus, out, log=lambda _m: None)
    assert second["reused_packages"] == 3
    assert second["chunks"] == first["chunks"]
    assert _index_content(out) == fresh

    # A new release of one package: only that one is chunked again, the result is the same.
    (corpus / "someone" / "my_modpack" / MARKER).write_text("999 test\n")
    third = build_index(corpus, out, log=lambda _m: None)
    assert third["reused_packages"] == 2
    assert _index_content(out) == fresh
    assert _rows(out, "SELECT count(*) FROM chunk c LEFT JOIN vec v ON v.id = c.id WHERE v.id IS NULL") == [(0,)]
    assert _rows(out, "SELECT count(*) FROM chunk_fts WHERE chunk_fts MATCH 'register'")[0][0] > 0


def test_code_index_ignores_unusable_previous(tmp_path, fake_model):
    from luanti_depgraph.codesearch import build_index

    corpus = _corpus(tmp_path)
    old = tmp_path / "old.sqlite"
    sqlite3.connect(old).execute("CREATE TABLE meta (key TEXT, value TEXT)")  # no package_ref
    stats = build_index(corpus, tmp_path / "code.sqlite", log=lambda _m: None, previous=old)
    assert stats["reused_packages"] == 0
