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


def test_build_records_phase_timings(db_file):
    meta = db.meta(db.connect(db_file))
    # Offline: no ContentDB fetches, so only the local phases are timed.
    assert {"seconds_details", "seconds_analyze", "seconds_refs", "seconds_history"} <= meta.keys()
    assert not any(k.startswith("seconds_fetch_") for k in meta)


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


def test_conditionally_loaded_files(db_file, index):
    report = lint(MODS / "crossgame_mod", db.connect(db_file), index)
    refs = {(r["modname"], r["kind"]): r for r in report["refs"]}
    assert refs[("mcl_sounds", "global")]["requires"] == ["mcl_core"]
    assert refs[("mcl_core", "global")]["guarded"]
    ldg001 = [f for f in report["findings"] if f["code"] == "LDG001"]
    assert ldg001 and all(f["severity"] == "warning" for f in ldg001)
    games = {g["game"]: g["status"] for g in report["games"]}
    assert games["Luanti/minetest_game"] == "ok"  # compat/mcl.lua isn't loaded there
    assert games["ryvnf/mineclonia"] == "risky"  # loaded, but mcl_sounds is missing in the fixture game


def test_history_carries_forward(tmp_path, db_file):
    import json
    import shutil

    from .conftest import FIXTURES

    raw = tmp_path / "cache" / "raw"
    raw.mkdir(parents=True)
    for f in (FIXTURES / "contentdb").glob("*.json"):
        shutil.copy(f, raw / f.name)
    deps = json.loads((raw / "dependencies.json").read_text())
    for d in deps:
        if d["name"] == "pipeworks":
            d["depends"] = ["xcompat", "fakelib"]  # a new hard dependency appears
    (raw / "dependencies.json").write_text(json.dumps(deps))
    new_db = tmp_path / "new.sqlite"
    shutil.copy(db_file, new_db)  # the previous build
    old = db.connect(db_file)
    old_date = json.loads(old.execute("SELECT value FROM meta WHERE key = 'built_at'").fetchone()[0])[:10]
    db.build(new_db, tmp_path / "cache", tmp_path / "corpus", offline=True, log=lambda _m: None)
    conn = db.connect(new_db)
    rows = {r["modname"]: dict(r) for r in conn.execute(
        "SELECT * FROM dep_history WHERE package = 'mt-mods/pipeworks'")}
    assert rows["xcompat"]["first_seen"] == old_date
    assert rows["fakelib"]["first_seen"] == rows["fakelib"]["last_seen"]
