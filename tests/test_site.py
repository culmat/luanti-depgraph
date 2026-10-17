import json
import subprocess

from luanti_depgraph import __version__, buildinfo, db, site


def test_export_writes_ui_and_data_versions(db_file, tmp_path):
    site.export(db_file, tmp_path)
    ui = json.loads((tmp_path / "data" / "site.json").read_text())
    assert ui["ldg_version"] == __version__
    assert "deployed_at" in ui
    data = json.loads((tmp_path / "data" / "meta.json").read_text())
    assert data == db.meta(db.connect(db_file))
    # The test suite runs from a checkout, so both carry the commit.
    assert data["ldg_commit"] == ui["commit"]
    assert ui["commit"][:12] in (tmp_path / "index.html").read_text()


def test_source_commit_ignores_foreign_repositories(tmp_path, monkeypatch):
    """An installed package inside another project's repository must not report that project's commit."""
    git = ["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@example.org"]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "other project"], check=True)
    installed = tmp_path / ".venv" / "lib" / "luanti_depgraph"
    installed.mkdir(parents=True)
    monkeypatch.setattr(buildinfo, "SOURCE", installed)
    assert buildinfo.source_commit() == {}
