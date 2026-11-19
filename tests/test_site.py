import json
import subprocess
import threading

import httpx

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


def test_server_serves_the_built_site(db_file, tmp_path):
    site.export(db_file, tmp_path)
    with site.server(tmp_path, port=0) as httpd:
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{httpd.server_port}"
        try:
            page = httpx.get(f"{base}/")
            assert "<html" in page.text.lower() and page.headers["cache-control"] == "no-store"
            assert httpx.get(f"{base}/data/meta.json").json() == db.meta(db.connect(db_file))
        finally:
            httpd.shutdown()
