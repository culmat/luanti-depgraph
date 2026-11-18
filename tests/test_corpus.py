"""The corpus holds the code of the ContentDB release, not whatever the default branch has now."""

import hashlib
import io
import subprocess
import zipfile
from pathlib import Path

import httpx
import pytest

from luanti_depgraph import corpus
from luanti_depgraph.corpus import MARKER, clone_sparse, extract_release_zip, fetch_package


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd,
                          check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture()
def repo(tmp_path) -> tuple[str, str, str]:
    """A repo with a released commit and a newer one on the default branch."""
    src = tmp_path / "src"
    src.mkdir()
    _git("init", "-q", cwd=src)
    (src / "init.lua").write_text("released = true\n")
    (src / "mod.conf").write_text("name = demo\n")
    (src / "texture.png").write_bytes(b"\x89PNG")
    _git("add", ".", cwd=src)
    _git("commit", "-qm", "release", cwd=src)
    released = _git("rev-parse", "HEAD", cwd=src)
    (src / "init.lua").write_text("released = false\n")
    _git("commit", "-qam", "work in progress", cwd=src)
    return f"file://{src}", released, _git("rev-parse", "HEAD", cwd=src)


def test_clone_at_release_commit(tmp_path, repo):
    url, released, _tip = repo
    head, pinned = clone_sparse(url, tmp_path / "out", released)
    assert (head, pinned) == (released, True)
    assert (tmp_path / "out" / "init.lua").read_text() == "released = true\n"
    assert not (tmp_path / "out" / "texture.png").exists()  # sparse: code and metadata only


def test_unknown_commit_falls_back_to_default_branch(tmp_path, repo):
    url, _released, tip = repo
    head, pinned = clone_sparse(url, tmp_path / "out", "0" * 40)
    assert (head, pinned) == (tip, False)
    assert (tmp_path / "out" / "init.lua").read_text() == "released = false\n"


def test_markers(tmp_path, repo):
    url, released, tip = repo
    pkg = {"author": "someone", "name": "demo", "release": 7, "repo": url, "commit": released}
    client = httpx.Client(transport=httpx.MockTransport(lambda _r: httpx.Response(500)))
    marker = tmp_path / "someone" / "demo" / MARKER

    result = fetch_package(tmp_path, pkg, client)
    assert result.status == "pinned" and marker.read_text() == f"7 git {released} release\n"
    assert fetch_package(tmp_path, pkg, client).status == "unchanged"

    # A checkout from before pinning (default branch, old marker) is fetched again once.
    marker.write_text(f"7 git {tip}\n")
    assert fetch_package(tmp_path, pkg, client).status == "pinned"
    # ...but not when no release commit is known.
    marker.write_text(f"7 git {tip}\n")
    assert fetch_package(tmp_path, pkg | {"commit": None}, client).status == "unchanged"

    # The release commit can't be fetched: default branch, and no retry while the release stands.
    marker.unlink()
    result = fetch_package(tmp_path, pkg | {"commit": "0" * 40}, client)
    assert result.status == "cloned" and marker.read_text() == f"7 git {tip} head\n"
    assert fetch_package(tmp_path, pkg | {"commit": "0" * 40}, client).status == "unchanged"
    assert fetch_package(tmp_path, pkg | {"release": 8}, client).status == "pinned"


def _zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("demo/init.lua", "x = 1\n")
        z.writestr("demo/screenshot.png", "png")
    return buf.getvalue()


def test_release_zip_from_cdn_url_is_checked(tmp_path, monkeypatch):
    data = _zip()
    asked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(str(request.url))
        return httpx.Response(200, content=data)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    cdn = "https://cdn.content.luanti.org/uploads/abc.zip"
    extract_release_zip("someone", "demo", tmp_path / "ok", client, cdn, hashlib.sha256(data).hexdigest())
    assert asked == [cdn]
    assert (tmp_path / "ok" / "demo" / "init.lua").exists()
    assert not (tmp_path / "ok" / "demo" / "screenshot.png").exists()
    with pytest.raises(ValueError, match="sha256"):
        extract_release_zip("someone", "demo", tmp_path / "bad", client, cdn, "00" * 32)
    assert not (tmp_path / "bad").exists()
    # Without a url, the ContentDB download link.
    extract_release_zip("someone", "demo", tmp_path / "api", client)
    assert asked[-1] == f"{corpus.BASE_URL}/packages/someone/demo/download/"
