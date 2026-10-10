import json

from typer.testing import CliRunner

from luanti_depgraph import db, graph, queries
from luanti_depgraph.cli import app
from luanti_depgraph.lint import lint

from .conftest import MODS


def test_lint_reports_undeclared_dependencies(db_file, index):
    report = lint(MODS / "undeclared_mod", db.connect(db_file), index)
    codes = {(f["code"], f["mod"]) for f in report["findings"]}
    assert ("LDG001", "undeclared_mod") in codes  # default.* used, not declared
    assert ("LDG002", "undeclared_mod") in codes  # mcl_core checked but not in optional_depends
    assert ("LDG008", "undeclared_mod") in codes  # minetest namespace
    assert ("LDG010", "undeclared_mod") in codes  # no LICENSE
    assert report["counts"]["error"] >= 1
    games = {g["game"]: g["status"] for g in report["games"]}
    assert games["Luanti/minetest_game"] == "ok"
    assert games["ryvnf/mineclonia"] == "risky"


def test_lint_without_database_still_works():
    report = lint(MODS / "legacy_mod", None)
    assert any(f["code"] == "LDG007" for f in report["findings"])


def test_lint_flags_taken_names(db_file, index, tmp_path):
    mod = tmp_path / "pipeworks"
    mod.mkdir()
    (mod / "mod.conf").write_text("name = pipeworks\ndescription = x\n")
    (mod / "init.lua").write_text("")
    report = lint(mod, db.connect(db_file), index)
    assert any(f["code"] == "LDG009" for f in report["findings"])
    report = lint(mod, db.connect(db_file), index, author="mt-mods")
    assert not any(f["code"] == "LDG009" for f in report["findings"])


def test_corpus_analysis_in_database(db_file):
    conn = db.connect(db_file)
    mods = {r["name"] for r in conn.execute("SELECT name FROM mod")}
    assert mods == {"core_part", "extra_part"}
    sibling = conn.execute("SELECT * FROM v_undeclared_refs WHERE modname = 'core_part'").fetchone()
    assert sibling["same_package"] == 1
    assert db.meta(conn)["packages"] == 10


def test_sql_is_read_only(db_file):
    conn = db.connect(db_file)
    assert queries.sql(conn, "SELECT count(*) FROM package")["rows"] == [[10]]
    try:
        queries.sql(conn, "DELETE FROM package")
    except ValueError:
        pass
    else:
        raise AssertionError("write query was accepted")


def test_graph_matches_sql(db_file, tmp_path):
    graph_file = tmp_path / "g.lbug"
    counts = graph.build_graph(db_file, graph_file)
    assert counts["Package"] == 10
    result = graph.cypher(graph_file, "MATCH (p:Package)-[:DEPENDS_ON]->(n:ModName {name: 'default'}) "
                                      "RETURN count(p)")
    sql_count = db.connect(db_file).execute("SELECT count(*) FROM dep WHERE modname = 'default'").fetchone()[0]
    assert result["rows"] == [[sql_count]]


def test_cli_json(db_file, monkeypatch):
    monkeypatch.setenv("LDG_DB", str(db_file))
    result = CliRunner().invoke(app, ["providers", "default", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["games"][0]["package"] == "Luanti/minetest_game"
    result = CliRunner().invoke(app, ["resolve", "TenPlus1/ethereal", "--game", "mineclonia"])
    assert result.exit_code == 1
